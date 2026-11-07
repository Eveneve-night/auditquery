"""B-owned D4-07 online SQL session service and framework-free HTTP adapter.

The service accepts only the frozen v2 request envelope.  It composes the
existing D4-01/D4-04/D4-05 runtime, the B-side online proposal port and the
D4-02 safety/execution handoff.  Dependencies are injected so ordinary tests
remain offline and no credential file is read by this module.
"""

from __future__ import annotations

from collections import deque
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import json
from pathlib import Path
import secrets
from threading import RLock
from time import perf_counter
from typing import Any, Callable, Mapping, Protocol

from ..agent.clarification_recovery import (
    ClarificationRecoveryError,
    D4ClarificationRecoveryRuntime,
    RecoverySession,
    SqlProposal,
    build_clarification_answer,
)
from ..agent.domain_pack import DomainPackRegistry
from ..agent.explicit_count import explicit_count_values
from ..agent.intent_routing import (
    apply_intent_result,
    create_conversation_state,
    load_source_catalog,
)
from ..agent.selective_routing import build_slot_observations
from ..llm.errors import ProviderError, ProviderErrorCode
from ..nl2sql.online_proposal import (
    MODEL_ID,
    PROVIDER_ID,
    OnlineSqlProposalError,
    OnlineSqlProposalPort,
    ProposalTelemetry,
    StructuredGateway,
)
from .online_contracts import (
    OnlineContractError,
    OnlineContractValidator,
    OnlineRequestGuard,
    OnlineSessionBinding,
    PendingClarificationBinding,
)


PROJECT_ROOT = Path(__file__).resolve().parents[3]
MAX_REQUEST_BYTES = 64 * 1024


class OnlineSqlApiError(RuntimeError):
    """Stable, content-safe transport or session error."""

    def __init__(
        self, code: str, http_status: int = 400, *, retryable: bool = False
    ) -> None:
        self.code = code
        self.http_status = http_status
        self.retryable = retryable
        super().__init__(code)


@dataclass(frozen=True)
class OnlineHttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


@dataclass(frozen=True)
class SafeDiagnosticEvent:
    """Content-free internal diagnostic event with a fixed field allowlist."""

    event_id: str
    component: str
    operation: str
    exception_class: str
    error_code: str | None
    session_id_digest: str
    request_id_digest: str


class SafeDiagnosticSink(Protocol):
    def emit(self, event: SafeDiagnosticEvent) -> None: ...


class InMemoryDiagnosticSink:
    """Bounded process-local sink; it never receives exception text or traceback."""

    def __init__(self, max_events: int = 100) -> None:
        if (
            not isinstance(max_events, int)
            or isinstance(max_events, bool)
            or max_events < 1
        ):
            raise ValueError("max_events must be a positive integer")
        self._events: deque[SafeDiagnosticEvent] = deque(maxlen=max_events)
        self._lock = RLock()

    def emit(self, event: SafeDiagnosticEvent) -> None:
        if not isinstance(event, SafeDiagnosticEvent):
            raise TypeError("event must be SafeDiagnosticEvent")
        with self._lock:
            self._events.append(event)

    def snapshot(self) -> tuple[SafeDiagnosticEvent, ...]:
        with self._lock:
            return tuple(self._events)


class SqlProposalPortLike(Protocol):
    last_telemetry: ProposalTelemetry | None

    def propose(
        self,
        *,
        conversation_state: Mapping[str, Any],
        slot_observations: Mapping[str, Any],
    ) -> SqlProposal: ...


@dataclass
class _SessionRecord:
    recovery: RecoverySession
    binding: OnlineSessionBinding
    last_response: dict[str, Any]
    elapsed_ms: float = 0.0


@dataclass
class _WorkflowResult:
    session: RecoverySession
    proposal: SqlProposal | None = None
    execution: Any = None
    telemetry: ProposalTelemetry | None = None


class _CapturingProposalPort:
    def __init__(self, inner: SqlProposalPortLike) -> None:
        self.inner = inner
        self.last_proposal: SqlProposal | None = None

    def propose(self, *, conversation_state, slot_observations) -> SqlProposal:
        proposal = self.inner.propose(
            conversation_state=conversation_state,
            slot_observations=slot_observations,
        )
        self.last_proposal = proposal
        return proposal


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _sql_intent_result() -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "task_type": "intent",
        "status": "success",
        "data": {
            "intent": "sql",
            "needs_clarification": False,
            "required_sources": ["chinook_sql"],
            "missing_slots": [],
            "reason": "routable",
            "normalized_query": None,
        },
        "error": None,
        "warnings": [],
    }


def _error_message(code: str) -> str:
    messages = {
        "provider_rate_limited": "模型服务当前限流，请稍后重试。",
        "provider_timeout": "模型服务响应超时，请稍后重试。",
        "provider_unavailable": "模型服务暂时不可用，请稍后重试。",
        "policy_limit": "本次请求已达到安全预算上限。",
        "provider_quota_exhausted": "模型服务配额不足。",
        "model_response_invalid": "模型返回未通过结构校验。",
        "model_contract_mismatch": "模型或服务配置与冻结契约不一致。",
        "sql_table_mismatch": "SQL 候选超出预选单表范围。",
        "sql_proposal_declined": "当前信息不足，未生成 SQL 候选。",
        "sql_proposal_failed": "SQL 候选生成失败。",
        "online_sql_runtime_failed": "在线 SQL 流程失败。",
    }
    return messages.get(code, "请求未能完成。")


def _provider_failure(exc: BaseException) -> tuple[str, bool, int]:
    current: BaseException | None = exc
    proposal_code: str | None = None
    provider: ProviderError | None = None
    while current is not None:
        if isinstance(current, OnlineSqlProposalError):
            proposal_code = current.code
        if isinstance(current, ProviderError):
            provider = current
            break
        current = current.__cause__
    if provider is not None:
        mapping = {
            ProviderErrorCode.RATE_LIMIT: ("provider_rate_limited", True, 1),
            ProviderErrorCode.TIMEOUT: ("provider_timeout", True, 1),
            ProviderErrorCode.NETWORK: ("provider_unavailable", True, 1),
            ProviderErrorCode.TRANSIENT: ("provider_unavailable", True, 1),
            ProviderErrorCode.CIRCUIT_OPEN: ("provider_unavailable", True, 0),
            ProviderErrorCode.POLICY_LIMIT: ("policy_limit", False, 0),
            ProviderErrorCode.QUOTA: ("provider_quota_exhausted", False, 1),
            ProviderErrorCode.INVALID_RESPONSE: ("model_response_invalid", False, 1),
            ProviderErrorCode.MODEL_MISMATCH: ("model_contract_mismatch", False, 1),
        }
        return mapping.get(provider.code, ("sql_proposal_failed", False, 1))
    mapping = {
        "sql_proposal_schema_invalid": ("model_response_invalid", False, 1),
        "sql_proposal_model_contract_mismatch": (
            "model_contract_mismatch",
            False,
            1,
        ),
        "sql_proposal_table_mismatch": ("sql_table_mismatch", False, 1),
        "sql_proposal_declined": ("sql_proposal_declined", False, 1),
    }
    return mapping.get(proposal_code, ("sql_proposal_failed", True, 1))


class OnlineSqlService:
    """Stateful, single-table online SQL orchestration for the frozen v2 API."""

    def __init__(
        self,
        gateway: StructuredGateway | None = None,
        *,
        table_name: str | None = "Artist",
        project_root: Path | str = PROJECT_ROOT,
        proposal_port: SqlProposalPortLike | None = None,
        now: Callable[[], str] = _utc_now,
        timer: Callable[[], float] = perf_counter,
        diagnostic_sink: SafeDiagnosticSink | None = None,
        infer_count_slots: bool = False,
    ) -> None:
        if proposal_port is None and gateway is None:
            raise TypeError("gateway or proposal_port is required")
        self.project_root = Path(project_root).resolve()
        registry = DomainPackRegistry(
            pack_root=self.project_root / "domain-packs",
            schema_root=self.project_root / "configs" / "schemas" / "d4",
        )
        domain_pack = registry.load("chinook_music_store", "1.0.0")
        self.recovery = D4ClarificationRecoveryRuntime(domain_pack)
        self.source_catalog = load_source_catalog(
            self.project_root
            / "configs"
            / "routing"
            / "d4-01"
            / "source-catalog.v1.json"
        )
        self.proposal_port = proposal_port or OnlineSqlProposalPort(
            gateway, table_name=table_name, domain_pack=domain_pack  # type: ignore[arg-type]
        )
        self.table_name = table_name
        self.infer_count_slots = infer_count_slots
        self.validator = OnlineContractValidator()
        self.guard = OnlineRequestGuard(self.validator)
        self.now = now
        self.timer = timer
        self.diagnostic_sink = (
            diagnostic_sink
            if diagnostic_sink is not None
            else InMemoryDiagnosticSink()
        )
        self._diagnostic_digest_key = secrets.token_bytes(32)
        self._sessions: dict[str, _SessionRecord] = {}
        self._lock = RLock()
        self._model_calls = 0
        self._input_tokens = 0
        self._output_tokens = 0
        self._estimated_cost_cny = 0.0

    def submit(self, raw_request: Mapping[str, Any]) -> dict[str, Any]:
        try:
            request = self.validator.validate_request(raw_request)
        except OnlineContractError as exc:
            raise OnlineSqlApiError(exc.code, exc.http_status) from exc
        with self._lock:
            self.proposal_port.last_telemetry = None
            if request["operation"] == "submit_query":
                return self._submit_query(request)
            return self._submit_clarification(request)

    def state(self, session_id: str) -> dict[str, Any]:
        if not isinstance(session_id, str) or not session_id:
            raise OnlineSqlApiError("invalid_session_id")
        with self._lock:
            record = self._sessions.get(session_id)
            if record is None:
                raise OnlineSqlApiError("session_not_found", 404)
            return deepcopy(record.last_response)

    def health(self) -> dict[str, Any]:
        with self._lock:
            return {
                "status": "ok",
                "mode": "online_sql_single_table",
                "provider_id": PROVIDER_ID,
                "model": MODEL_ID,
                "table": self.table_name,
                "session_count": len(self._sessions),
                "model_calls": self._model_calls,
                "input_tokens": self._input_tokens,
                "output_tokens": self._output_tokens,
                "estimated_cost_cny": round(self._estimated_cost_cny, 10),
                "credential_files_read": 0,
                "sql_requires_d4_02_safety_gate": True,
            }

    def _submit_query(self, request: Mapping[str, Any]) -> dict[str, Any]:
        session_id = str(request["session_id"])
        if session_id in self._sessions:
            raise OnlineSqlApiError("session_already_registered", 409)
        if request["turn_id"] != 0:
            raise OnlineSqlApiError("initial_turn_required", 409)
        if request["state_revision"] != 0:
            raise OnlineSqlApiError("initial_state_revision_required", 409)
        started = self.timer()
        try:
            state = apply_intent_result(
                create_conversation_state(session_id, str(request["query"])),
                self.source_catalog,
                _sql_intent_result(),
            )
            session = self.recovery.start(
                conversation_state=state,
                slot_observations=build_slot_observations({}),
                request_id=str(request["request_id"]),
                recorded_at=self.now(),
            )
            if self.infer_count_slots:
                self._complete_explicit_count(session, str(request["query"]))
            outcome = self._execute_ready(session)
        except Exception as exc:
            elapsed_ms = max(0.0, (self.timer() - started) * 1000.0)
            return self._workflow_error(request, exc, "submit_query", elapsed_ms)
        elapsed_ms = max(0.0, (self.timer() - started) * 1000.0)
        try:
            binding = self._binding_for(
                session,
                fallback_turn=int(request["turn_id"]),
                fallback_revision=int(request["state_revision"]),
            )
            response = self._response_for(
                session,
                binding=binding,
                proposal=outcome.proposal,
                execution=outcome.execution,
                telemetry=outcome.telemetry,
                elapsed_ms=elapsed_ms,
            )
            self._validate_response(response, session)
        except Exception as exc:
            return self._workflow_error(request, exc, "submit_query", elapsed_ms)
        # Response validation must finish before publishing the session or
        # registering its replay/pending binding.
        try:
            self.guard.register(binding)
        except OnlineContractError as exc:
            raise OnlineSqlApiError(exc.code, exc.http_status) from exc
        self._sessions[session_id] = _SessionRecord(
            recovery=session,
            binding=binding,
            last_response=response,
            elapsed_ms=elapsed_ms,
        )
        if outcome.telemetry is not None:
            self._record_telemetry(outcome.telemetry)
        return deepcopy(response)

    def _complete_explicit_count(self, session: RecoverySession, query: str) -> None:
        values = explicit_count_values(query, self.recovery.domain_pack)
        for _ in range(len(values)):
            question = session.pending_question
            if question is None or question.slot_id not in values:
                break
            value = values[question.slot_id]
            option = next((item for item in question.options if item.value == value), None)
            if option is None:
                break
            answer = build_clarification_answer({
                "answer_id": "auto." + secrets.token_hex(12),
                "question_id": question.question_id,
                "session_id": question.session_id,
                "request_id": question.request_id,
                "turn_id": question.turn_id,
                "state_revision": question.state_revision,
                "slot_id": question.slot_id,
                "values": [value],
                "evidence_refs": [option.evidence_ref],
            })
            self.recovery._append_audit(
                session, event_type="explicit_count_resolution", slot_id=question.slot_id,
                action="clarify", reason_code="explicit_count_from_query",
                evidence_refs=(option.evidence_ref,), value_count=1,
                artifact={"slot_id": question.slot_id, "source": "explicit_count_template"},
            )
            self.recovery.resume(session, answer, recorded_at=self.now())

    def _execute_ready(self, session: RecoverySession) -> _WorkflowResult:
        outcome = _WorkflowResult(session=session)
        if session.workflow_status == "accepted_for_safety_handoff":
            capture = _CapturingProposalPort(self.proposal_port)
            outcome.execution = self.recovery.handoff_and_execute(session, proposal_port=capture)
            outcome.proposal = capture.last_proposal
            outcome.telemetry = getattr(self.proposal_port, "last_telemetry", None)
        return outcome

    def _workflow_error(self, request, exc, operation, elapsed_ms) -> dict[str, Any]:
        code, retryable, calls = ("online_sql_runtime_failed", False, 0)
        if isinstance(exc, ClarificationRecoveryError) and exc.code == "sql_proposal_failed":
            code, retryable, calls = _provider_failure(exc)
        self._emit_diagnostic(
            exc, operation=operation,
            session_id=str(request["session_id"]), request_id=str(request["request_id"]),
            error_code=exc.code if isinstance(exc, ClarificationRecoveryError) else None,
            event_id=("online_sql.domain_error.v1" if isinstance(exc, ClarificationRecoveryError)
                      else "online_sql.unexpected_exception.v1"),
        )
        telemetry = getattr(self.proposal_port, "last_telemetry", None)
        if telemetry is not None:
            calls = telemetry.attempt_count
            self._record_telemetry(telemetry)
        else:
            self._model_calls += calls
        return self._error_response(request, code, retryable, calls,
                                    elapsed_ms=elapsed_ms, telemetry=telemetry)

    def _submit_clarification(self, request: Mapping[str, Any]) -> dict[str, Any]:
        record = self._sessions.get(str(request["session_id"]))
        if record is None:
            try:
                self.guard.validate_and_consume(request)
            except OnlineContractError as exc:
                raise OnlineSqlApiError(exc.code, exc.http_status) from exc
            raise OnlineSqlApiError("session_not_found", 404)
        started = self.timer()
        working = deepcopy(record.recovery)
        prepared_response: dict[str, Any] | None = None
        workflow_started = False

        def consume(
            validated: dict[str, Any], _binding: OnlineSessionBinding
        ) -> _WorkflowResult:
            nonlocal prepared_response, workflow_started
            workflow_started = True
            answer = build_clarification_answer(
                {
                    key: validated[key]
                    for key in (
                        "answer_id",
                        "question_id",
                        "session_id",
                        "request_id",
                        "turn_id",
                        "state_revision",
                        "slot_id",
                        "values",
                        "evidence_refs",
                    )
                }
            )
            self.recovery.resume(working, answer, recorded_at=self.now())
            outcome = self._execute_ready(working)
            next_binding = self._binding_for(
                outcome.session,
                fallback_turn=int(request["turn_id"]),
                fallback_revision=int(request["state_revision"]),
            )
            _binding.request_id = next_binding.request_id
            _binding.turn_id = next_binding.turn_id
            _binding.state_revision = next_binding.state_revision
            _binding.pending = next_binding.pending
            prepared_response = self._response_for(
                outcome.session, binding=_binding, proposal=outcome.proposal,
                execution=outcome.execution, telemetry=outcome.telemetry,
                elapsed_ms=record.elapsed_ms + max(0.0, (self.timer() - started) * 1000.0),
            )
            self._validate_response(prepared_response, outcome.session)
            return outcome

        try:
            _, _, outcome = self.guard.apply_atomically(request, consume)
        except OnlineContractError as exc:
            if workflow_started:
                return self._workflow_error(
                    request, exc, "submit_clarification",
                    record.elapsed_ms + max(0.0, (self.timer() - started) * 1000.0),
                )
            raise OnlineSqlApiError(exc.code, exc.http_status) from exc
        except ClarificationRecoveryError as exc:
            return self._workflow_error(request, exc, "submit_clarification",
                record.elapsed_ms + max(0.0, (self.timer() - started) * 1000.0))
        except Exception as exc:
            return self._workflow_error(request, exc, "submit_clarification",
                record.elapsed_ms + max(0.0, (self.timer() - started) * 1000.0))

        # The Guard committed the already validated next binding atomically.
        # Keep the authoritative object used by Guard rather than its snapshot.
        assert prepared_response is not None
        response = prepared_response
        record.recovery = outcome.session
        record.last_response = response
        record.elapsed_ms = response["usage"]["elapsed_ms"]
        if outcome.telemetry is not None:
            self._record_telemetry(outcome.telemetry)
        return deepcopy(response)

    def _response_for(
        self,
        session: RecoverySession,
        *,
        binding: OnlineSessionBinding,
        proposal: SqlProposal | None = None,
        execution: Any = None,
        telemetry: ProposalTelemetry | None = None,
        elapsed_ms: float,
    ) -> dict[str, Any]:
        decision = self._decision_payload(session)
        status: str
        route: str
        message: str
        sql: dict[str, Any] | None = None
        if session.pending_question is not None:
            status, route = "clarification_required", "clarification"
            message = "需要补充一项结构化信息后才能继续。"
        elif session.workflow_status == "executed":
            status, route = "success", "sql"
            message = "只读 SQL 已通过安全门并完成执行。"
        elif session.workflow_status == "safety_rejected":
            status, route = "refused", "sql"
            message = "SQL 候选未通过安全门，数据库未执行。"
            decision = dict(decision)
            decision["action"] = "reject"
            decision["reason_code"] = self._last_safety_reason(session)
        elif session.workflow_status == "stopped":
            status, route = "refused", "sql"
            message = "当前请求未达到安全执行条件。"
            # Keep the last internal decision intact for causal audit, while
            # exposing the terminal workflow outcome required by the public
            # refused-response contract.
            decision = dict(decision)
            decision["action"] = "escalate"
            decision["reason_code"] = session.stop_reason
        else:
            status, route = "refused", "sql"
            message = "当前请求未达到安全执行条件。"

        if proposal is not None:
            sql = {
                "proposal_id": proposal.proposal_id,
                "candidate_sql": proposal.sql,
                "tables": list(proposal.tables),
                "safety_status": (
                    "approved" if session.safety_approved is True else "rejected"
                ),
                "execution_attempts": session.local_sqlite_execution_attempts,
                "columns": list(execution.columns) if execution is not None else [],
                "rows": [list(row) for row in execution.rows]
                if execution is not None
                else [],
                "row_count": execution.row_count if execution is not None else 0,
                "truncated": execution.truncated if execution is not None else False,
            }
        pending = (
            binding.pending_question_payload()
            if binding.pending is not None
            else None
        )
        usage = self._usage_payload(session, telemetry, elapsed_ms)
        return {
            "schema_version": "2.0.0",
            "session_id": binding.session_id,
            "request_id": binding.request_id,
            "turn_id": binding.turn_id,
            "state_revision": binding.state_revision,
            "route": route,
            "status": status,
            "message": message,
            "decision": decision,
            "pending_question": pending,
            "candidate_evidence": [
                {
                    "candidate_id": item.candidate_id,
                    "display_name": item.display_name,
                    "candidate_kind": item.candidate_kind,
                    "score": item.raw_score,
                    "evidence_refs": list(item.evidence_refs),
                }
                for item in session.candidates[:10]
            ],
            "sql": sql,
            "citations": [],
            "trace": [self._audit_payload(item) for item in session.audit_events],
            "usage": usage,
            "error": None,
            "warnings": ["uncalibrated_raw_score"],
        }

    def _error_response(
        self,
        request: Mapping[str, Any],
        code: str,
        retryable: bool,
        model_calls: int,
        *,
        elapsed_ms: float = 0.0,
        telemetry: ProposalTelemetry | None = None,
    ) -> dict[str, Any]:
        response = {
            "schema_version": "2.0.0",
            "session_id": request["session_id"],
            "request_id": request["request_id"],
            "turn_id": request["turn_id"],
            "state_revision": request["state_revision"],
            "route": "unknown",
            "status": "error",
            "message": _error_message(code),
            "decision": None,
            "pending_question": None,
            "candidate_evidence": [],
            "sql": None,
            "citations": [],
            "trace": [],
            "usage": {
                "model_calls": model_calls,
                "input_tokens": telemetry.input_tokens if telemetry else 0,
                "output_tokens": telemetry.output_tokens if telemetry else 0,
                "estimated_cost_cny": telemetry.estimated_cost_cny if telemetry else 0,
                "elapsed_ms": round(max(elapsed_ms, 0.0), 3),
                "stopped_reason": code,
            },
            "error": {
                "code": code,
                "retryable": retryable,
                "message": _error_message(code),
            },
            "warnings": [],
        }
        self.validator.validate_response(response)
        return response

    @staticmethod
    def _binding_for(
        session: RecoverySession, *, fallback_turn: int, fallback_revision: int
    ) -> OnlineSessionBinding:
        question = session.pending_question
        if question is None:
            return OnlineSessionBinding(
                session.session_id,
                session.request_id,
                fallback_turn,
                fallback_revision,
            )
        return OnlineSessionBinding(
            session.session_id,
            session.request_id,
            question.turn_id,
            question.state_revision,
            PendingClarificationBinding.from_question(question),
        )

    @staticmethod
    def _decision_payload(session: RecoverySession) -> dict[str, Any]:
        decision = session.current_decision
        typed = decision["typed_decision"]
        thresholds = decision["trace_event"]["thresholds"]
        return {
            "contract_version": typed["contract_version"],
            "action": decision["action"],
            "reason_code": decision["reason_code"],
            "score_kind": "uncalibrated_raw_score",
            "raw_score": decision["raw_score"],
            "calibration_version": None,
            "thresholds": {
                "accept_min": thresholds["accept_min"],
                "clarify_min": thresholds["clarify_min"],
                "abstain_below": thresholds["abstain_below"],
                "retrieval_min_score": None,
            },
            "evidence_refs": list(typed["evidence_refs"]),
            "missing_or_conflicting_slots": [
                item["slot_id"]
                for item in typed["missing_or_conflicting_slots"]
            ],
            "accept_authorizes_sql_execution": False,
            "requires_d4_02_safety_gate": True,
        }

    @staticmethod
    def _audit_payload(event: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "sequence": event["sequence"],
            "stage": event["event_type"],
            "action": event["action"],
            "reason_code": event["reason_code"],
            "evidence_refs": list(event["evidence_refs"]),
            "artifact_sha256": event["artifact_sha256"],
        }

    @staticmethod
    def _last_safety_reason(session: RecoverySession) -> str:
        for event in reversed(session.audit_events):
            if event["event_type"] == "safety_gate":
                return str(event["reason_code"] or "sql_safety_rejected")
        return "sql_safety_rejected"

    @staticmethod
    def _usage_payload(
        session: RecoverySession,
        telemetry: ProposalTelemetry | None,
        elapsed_ms: float,
    ) -> dict[str, Any]:
        return {
            "model_calls": telemetry.attempt_count if telemetry is not None else 0,
            "input_tokens": telemetry.input_tokens if telemetry is not None else 0,
            "output_tokens": telemetry.output_tokens if telemetry is not None else 0,
            "estimated_cost_cny": (
                telemetry.estimated_cost_cny if telemetry is not None else 0
            ),
            "elapsed_ms": round(max(elapsed_ms, 0.0), 3),
            "stopped_reason": session.stop_reason,
        }

    def _record_telemetry(self, telemetry: ProposalTelemetry) -> None:
        self._model_calls += telemetry.attempt_count
        self._input_tokens += telemetry.input_tokens
        self._output_tokens += telemetry.output_tokens
        self._estimated_cost_cny += telemetry.estimated_cost_cny

    def _emit_diagnostic(
        self,
        exc: BaseException,
        *,
        operation: str,
        session_id: str,
        request_id: str,
        error_code: str | None = None,
        event_id: str = "online_sql.unexpected_exception.v1",
    ) -> None:
        event = SafeDiagnosticEvent(
            event_id=event_id,
            component="online_sql_service",
            operation=operation,
            exception_class=type(exc).__name__,
            error_code=error_code,
            session_id_digest=self._diagnostic_digest(session_id),
            request_id_digest=self._diagnostic_digest(request_id),
        )
        try:
            self.diagnostic_sink.emit(event)
        except Exception:
            # Diagnostics must never weaken failure-closed behavior or replace
            # the original stable public error with a sink-specific failure.
            return

    def _diagnostic_digest(self, value: str) -> str:
        return hmac.new(
            self._diagnostic_digest_key,
            value.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _validate_response(
        self, response: Mapping[str, Any], session: RecoverySession
    ) -> None:
        self.validator.validate_response(
            response,
            safety_gate_approved=session.safety_approved is True,
            readonly_execution_attempts=session.local_sqlite_execution_attempts,
        )


class OnlineSqlApplication:
    """Minimal local HTTP routing surface for C-side integration."""

    def __init__(self, service: OnlineSqlService) -> None:
        if not isinstance(service, OnlineSqlService):
            raise TypeError("service must be OnlineSqlService")
        self.service = service

    def handle(
        self,
        method: str,
        path: str,
        *,
        content_type: str = "",
        body: bytes = b"",
    ) -> OnlineHttpResponse:
        try:
            if method == "GET" and path == "/api/v2/health":
                return self._json(200, self.service.health())
            if method == "GET" and path.startswith("/api/v2/sessions/"):
                session_id = path.removeprefix("/api/v2/sessions/")
                if not session_id or "/" in session_id:
                    raise OnlineSqlApiError("route_not_found", 404)
                return self._json(200, self.service.state(session_id))
            if path in {"/api/v2/query", "/api/v2/clarification"}:
                if method != "POST":
                    raise OnlineSqlApiError("method_not_allowed", 405)
                request = self._request_json(content_type, body)
                expected = (
                    "submit_query"
                    if path == "/api/v2/query"
                    else "submit_clarification"
                )
                if request.get("operation") != expected:
                    raise OnlineSqlApiError("operation_route_mismatch")
                return self._json(200, self.service.submit(request))
            raise OnlineSqlApiError("route_not_found", 404)
        except OnlineSqlApiError as exc:
            return self._error(exc)

    @staticmethod
    def _request_json(content_type: str, body: bytes) -> dict[str, Any]:
        if len(body) > MAX_REQUEST_BYTES:
            raise OnlineSqlApiError("request_too_large", 413)
        if content_type.split(";", 1)[0].strip().casefold() != "application/json":
            raise OnlineSqlApiError("content_type_required", 415)
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise OnlineSqlApiError("invalid_json") from exc
        if not isinstance(value, dict):
            raise OnlineSqlApiError("invalid_online_request")
        return value

    @staticmethod
    def _json(status: int, value: Mapping[str, Any]) -> OnlineHttpResponse:
        return OnlineHttpResponse(
            status=status,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
            body=json.dumps(
                value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8"),
        )

    @classmethod
    def _error(cls, error: OnlineSqlApiError) -> OnlineHttpResponse:
        return cls._json(
            error.http_status,
            {
                "error": {
                    "code": error.code,
                    "retryable": error.retryable,
                    "message": "请求未能完成。",
                }
            },
        )


__all__ = [
    "InMemoryDiagnosticSink",
    "MAX_REQUEST_BYTES",
    "OnlineHttpResponse",
    "OnlineSqlApiError",
    "OnlineSqlApplication",
    "OnlineSqlService",
    "SafeDiagnosticEvent",
    "SafeDiagnosticSink",
]
