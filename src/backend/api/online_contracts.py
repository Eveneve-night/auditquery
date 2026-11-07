"""D4-07 online API contract loading and fail-closed semantic validation.

This module is HTTP-framework independent.  It validates the frozen A-owned
JSON schemas with an explicit referencing registry and then applies the
stateful checks which JSON Schema cannot express.  It never calls a model,
opens a credential file, or authorizes SQL execution.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import json
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Mapping, TypeVar

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError, ValidationError
from referencing import Registry, Resource


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SCHEMA_ROOT = PROJECT_ROOT / "configs" / "schemas" / "d4-07"


class OnlineContractError(RuntimeError):
    """Stable, content-safe contract failure suitable for an API response."""

    def __init__(self, code: str, http_status: int = 400) -> None:
        self.code = code
        self.http_status = http_status
        super().__init__(code)


def _load_object(path: Path, code: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise OnlineContractError(code, 500) from exc
    if not isinstance(value, dict):
        raise OnlineContractError(code, 500)
    return value


_T = TypeVar("_T")
_SUPPORTED_VERSIONS = {"1.0.0": "v1", "2.0.0": "v2"}


class OnlineContractValidator:
    """Validate frozen D4-07 request/response wrappers by exact major version."""

    def __init__(self, schema_root: Path | str = DEFAULT_SCHEMA_ROOT) -> None:
        root = Path(schema_root)
        schemas: dict[str, dict[str, Any]] = {}
        for suffix in ("v1", "v2"):
            schemas[f"definitions_{suffix}"] = _load_object(
                root / f"shared-api-definitions.{suffix}.schema.json",
                "invalid_online_shared_schema",
            )
            schemas[f"request_{suffix}"] = _load_object(
                root / f"online-request.{suffix}.schema.json",
                "invalid_online_request_schema",
            )
            schemas[f"response_{suffix}"] = _load_object(
                root / f"online-response.{suffix}.schema.json",
                "invalid_online_response_schema",
            )
        try:
            for schema in schemas.values():
                Draft202012Validator.check_schema(schema)
            registry: Registry = Registry()
            for suffix in ("v1", "v2"):
                definitions = schemas[f"definitions_{suffix}"]
                schema_id = definitions.get("$id")
                if not isinstance(schema_id, str) or not schema_id:
                    raise SchemaError("shared schema has no $id")
                registry = registry.with_resource(
                    schema_id, Resource.from_contents(definitions)
                )
            self._requests = {
                version: Draft202012Validator(
                    schemas[f"request_{suffix}"],
                    registry=registry,
                    format_checker=FormatChecker(),
                )
                for version, suffix in _SUPPORTED_VERSIONS.items()
            }
            self._responses = {
                version: Draft202012Validator(
                    schemas[f"response_{suffix}"],
                    registry=registry,
                    format_checker=FormatChecker(),
                )
                for version, suffix in _SUPPORTED_VERSIONS.items()
            }
        except SchemaError as exc:
            raise OnlineContractError("invalid_online_schema", 500) from exc

    def validate_request(self, value: Mapping[str, Any]) -> dict[str, Any]:
        request = self._mapping(value, "invalid_online_request")
        validator = self._select_validator(
            request, self._requests, "invalid_online_request"
        )
        self._validate(validator, request, "invalid_online_request")
        return request

    def validate_response(
        self,
        value: Mapping[str, Any],
        *,
        safety_gate_approved: bool = False,
        readonly_execution_attempts: int = 0,
    ) -> dict[str, Any]:
        response = self._mapping(value, "invalid_online_response")
        validator = self._select_validator(
            response, self._responses, "invalid_online_response", http_status=500
        )
        self._validate(
            validator, response, "invalid_online_response", http_status=500
        )
        self._validate_response_semantics(
            response,
            safety_gate_approved=safety_gate_approved,
            readonly_execution_attempts=readonly_execution_attempts,
        )
        return response

    @staticmethod
    def _select_validator(
        value: Mapping[str, Any],
        validators: Mapping[str, Draft202012Validator],
        code: str,
        *,
        http_status: int = 400,
    ) -> Draft202012Validator:
        version = value.get("schema_version")
        validator = validators.get(version) if isinstance(version, str) else None
        if validator is None:
            raise OnlineContractError(code, http_status)
        return validator

    @staticmethod
    def _mapping(value: Mapping[str, Any], code: str) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise OnlineContractError(code)
        return dict(value)

    @staticmethod
    def _validate(
        validator: Draft202012Validator,
        value: Mapping[str, Any],
        code: str,
        *,
        http_status: int = 400,
    ) -> None:
        try:
            validator.validate(value)
        except ValidationError as exc:
            raise OnlineContractError(code, http_status) from exc

    @staticmethod
    def _validate_response_semantics(
        response: Mapping[str, Any],
        *,
        safety_gate_approved: bool,
        readonly_execution_attempts: int,
    ) -> None:
        status = response["status"]
        route = response["route"]
        decision = response["decision"]
        sql = response["sql"]
        citations = response["citations"]

        if status == "clarification_required" and route != "clarification":
            raise OnlineContractError("clarification_route_mismatch", 500)
        if status == "error" and route != "unknown":
            raise OnlineContractError("error_route_mismatch", 500)
        if status in {"success", "refused"} and route not in {"sql", "rag"}:
            raise OnlineContractError("terminal_route_mismatch", 500)
        if status == "success" and decision["action"] != "accept":
            raise OnlineContractError("success_decision_mismatch", 500)
        if status == "refused" and decision["action"] not in {"reject", "escalate"}:
            raise OnlineContractError("refused_decision_mismatch", 500)
        if citations and route != "rag":
            raise OnlineContractError("citation_route_mismatch", 500)

        if route == "sql":
            if citations:
                raise OnlineContractError("sql_citations_forbidden", 500)
            if decision is None or decision["requires_d4_02_safety_gate"] is not True:
                raise OnlineContractError("sql_safety_gate_requirement_missing", 500)
        elif sql is not None:
            raise OnlineContractError("sql_result_route_mismatch", 500)

        if route == "rag" and status == "success" and not citations:
            raise OnlineContractError("rag_success_requires_citation", 500)
        if route == "rag" and decision["requires_d4_02_safety_gate"] is not False:
            raise OnlineContractError("rag_safety_gate_mismatch", 500)
        if status == "refused" and citations:
            raise OnlineContractError("refused_citations_forbidden", 500)
        if route == "rag" and citations:
            ids = [item["evidence_id"] for item in citations]
            if len(ids) != len(set(ids)) or set(ids) != set(decision["evidence_refs"]):
                raise OnlineContractError("invalid_online_response", 500)
            for citation in citations:
                if citation["document_id"].startswith("doc.") and citation["evidence_id"] != (
                    f"{citation['document_id']}.p{citation['page']}"
                ):
                    raise OnlineContractError("invalid_online_response", 500)

        pending = response["pending_question"]
        if response["schema_version"] == "2.0.0" and pending is not None:
            for field_name in (
                "session_id",
                "request_id",
                "turn_id",
                "state_revision",
            ):
                if pending[field_name] != response[field_name]:
                    raise OnlineContractError(
                        "pending_question_binding_mismatch", 500
                    )
            option_keys = [
                _clarification_value_key(item["value"])
                for item in pending["options"]
            ]
            if len(option_keys) != len(set(option_keys)):
                raise OnlineContractError("pending_question_option_conflict", 500)

        if route == "sql" and status == "success" and sql is None:
            raise OnlineContractError("sql_success_requires_result", 500)

        if sql is None:
            if safety_gate_approved or readonly_execution_attempts:
                raise OnlineContractError("sql_proof_without_result", 500)
            return

        declared_attempts = sql["execution_attempts"]
        if declared_attempts != readonly_execution_attempts:
            raise OnlineContractError("execution_attempt_count_mismatch", 500)
        if sql["safety_status"] == "approved":
            if not safety_gate_approved:
                raise OnlineContractError("safety_approval_unproven", 500)
            if declared_attempts != 1 or status != "success":
                raise OnlineContractError("approved_sql_not_executed", 500)
            if sql["row_count"] != len(sql["rows"]):
                raise OnlineContractError("sql_row_count_mismatch", 500)
        else:
            if status != "refused":
                raise OnlineContractError("unapproved_sql_status_mismatch", 500)
            if safety_gate_approved or declared_attempts != 0:
                raise OnlineContractError("unapproved_sql_execution_forbidden", 500)
            if sql["columns"] or sql["rows"] or sql["row_count"] != 0:
                raise OnlineContractError("unapproved_sql_result_forbidden", 500)


def _clarification_value_key(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class ClarificationOptionBinding:
    """One server-offered value and its inseparable evidence reference."""

    value: str | int | float | bool
    display_name: str
    evidence_ref: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "display_name": self.display_name,
            "evidence_ref": self.evidence_ref,
        }


@dataclass(frozen=True)
class PendingClarificationBinding:
    """Server-owned pending question used by the v2 runtime gate."""

    question_id: str
    slot_id: str
    allowed_values: frozenset[Any] = frozenset()
    allowed_evidence_refs: frozenset[str] = frozenset()
    max_values: int = 1
    question_text: str = ""
    options: tuple[ClarificationOptionBinding, ...] = ()
    source_session_id: str | None = None
    source_request_id: str | None = None
    source_turn_id: int | None = None
    source_state_revision: int | None = None

    @classmethod
    def from_question(cls, question: Any) -> "PendingClarificationBinding":
        """Project a D4-05 ClarificationQuestion without accepting client input."""

        try:
            options = tuple(
                ClarificationOptionBinding(
                    value=item.value,
                    display_name=item.display_name,
                    evidence_ref=item.evidence_ref,
                )
                for item in question.options
            )
            identifiers = (
                question.question_id,
                question.session_id,
                question.request_id,
                question.slot_id,
            )
            if any(not isinstance(item, str) or not item for item in identifiers):
                raise ValueError("invalid identifiers")
            if not isinstance(question.prompt, str) or not question.prompt:
                raise ValueError("invalid question text")
            if not 1 <= len(options) <= 16:
                raise ValueError("invalid option count")
            value_keys = [_clarification_value_key(item.value) for item in options]
            if len(value_keys) != len(set(value_keys)):
                raise ValueError("duplicate option binding")
            return cls(
                question_id=question.question_id,
                slot_id=question.slot_id,
                allowed_values=frozenset(item.value for item in options),
                allowed_evidence_refs=frozenset(
                    item.evidence_ref for item in options
                ),
                max_values=1,
                question_text=question.prompt,
                options=options,
                source_session_id=question.session_id,
                source_request_id=question.request_id,
                source_turn_id=question.turn_id,
                source_state_revision=question.state_revision,
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise OnlineContractError("invalid_pending_clarification", 500) from exc

    def option_for(self, value: Any) -> ClarificationOptionBinding | None:
        wanted = _clarification_value_key(value)
        for option in self.options:
            if _clarification_value_key(option.value) == wanted:
                return option
        return None


@dataclass
class OnlineSessionBinding:
    """Minimal server-side state used to reject stale or cross-session input."""

    session_id: str
    request_id: str
    turn_id: str | int
    state_revision: int
    pending: PendingClarificationBinding | None = None
    consumed_operations: set[tuple[str, str | int, int]] = field(default_factory=set)

    def pending_question_payload(self) -> dict[str, Any]:
        pending = self.pending
        if pending is None or not pending.options or not pending.question_text:
            raise OnlineContractError("invalid_pending_clarification", 500)
        return {
            "question_id": pending.question_id,
            "session_id": self.session_id,
            "request_id": self.request_id,
            "turn_id": self.turn_id,
            "state_revision": self.state_revision,
            "slot_id": pending.slot_id,
            "question_text": pending.question_text,
            "options": [item.to_dict() for item in pending.options],
        }


class OnlineRequestGuard:
    """Bind schema-valid requests to authoritative server session state."""

    def __init__(self, validator: OnlineContractValidator | None = None) -> None:
        self.validator = validator or OnlineContractValidator()
        self._sessions: dict[str, OnlineSessionBinding] = {}
        self._consumed_answer_ids: set[str] = set()
        self._consumed_question_ids: set[str] = set()
        self._lock = RLock()

    def register(self, binding: OnlineSessionBinding) -> None:
        if not isinstance(binding, OnlineSessionBinding):
            raise OnlineContractError("invalid_session_binding", 500)
        pending = binding.pending
        if pending is not None:
            source_values = (
                pending.source_session_id,
                pending.source_request_id,
                pending.source_turn_id,
                pending.source_state_revision,
            )
            if pending.options and any(item is None for item in source_values):
                raise OnlineContractError("invalid_session_binding", 500)
            source_fields = (
                (pending.source_session_id, binding.session_id),
                (pending.source_request_id, binding.request_id),
                (pending.source_turn_id, binding.turn_id),
                (pending.source_state_revision, binding.state_revision),
            )
            if any(
                source is not None and source != actual
                for source, actual in source_fields
            ):
                raise OnlineContractError("invalid_session_binding", 500)
        with self._lock:
            if binding.session_id in self._sessions:
                raise OnlineContractError("session_already_registered", 409)
            self._sessions[binding.session_id] = binding

    def validate_and_consume(
        self, raw_request: Mapping[str, Any]
    ) -> tuple[dict[str, Any], OnlineSessionBinding]:
        request, session, _ = self.apply_atomically(raw_request, lambda *_: None)
        return request, session

    def apply_atomically(
        self,
        raw_request: Mapping[str, Any],
        consumer: Callable[[dict[str, Any], OnlineSessionBinding], _T],
    ) -> tuple[dict[str, Any], OnlineSessionBinding, _T]:
        """Run a consumer against isolated state and commit once on success.

        The registered binding is authoritative and never crosses the consumer
        boundary.  Both the consumer input and the returned session snapshot are
        detached copies, so a retained reference cannot mutate server state
        after this method returns.
        """

        if not callable(consumer):
            raise OnlineContractError("invalid_request_consumer", 500)
        request = self.validator.validate_request(raw_request)
        with self._lock:
            session = self._resolve_session(request)
            operation = str(request["operation"])
            version = str(request["schema_version"])

            if operation == "submit_clarification" and version == "1.0.0":
                raise OnlineContractError("clarification_contract_upgrade_required", 409)

            if version == "2.0.0" and operation == "submit_clarification":
                self._validate_v2_clarification(request, session)
            else:
                self._validate_v1_binding(request, session)

            replay_key = (operation, session.turn_id, session.state_revision)
            if replay_key in session.consumed_operations:
                code = (
                    "clarification_answer_replayed"
                    if operation == "submit_clarification"
                    else "request_replayed"
                )
                raise OnlineContractError(code, 409)
            if operation == "submit_query" and session.pending is not None:
                raise OnlineContractError("clarification_answer_required", 409)

            answer_id = request.get("answer_id")
            question_id = request.get("question_id")
            working_session = deepcopy(session)
            consumer_request = deepcopy(request)
            result = consumer(consumer_request, working_session)

            self._validate_transaction_candidate(session, working_session)
            committed_session = deepcopy(working_session)
            committed_session.consumed_operations.add(replay_key)
            committed_answer_ids = set(self._consumed_answer_ids)
            committed_question_ids = set(self._consumed_question_ids)
            if operation == "submit_clarification":
                committed_answer_ids.add(str(answer_id))
                committed_question_ids.add(str(question_id))

            # All potentially failing validation and copy work completes before
            # these assignments.  The RLock keeps readers from observing a
            # partially committed transaction.
            returned_request = deepcopy(request)
            returned_session = deepcopy(committed_session)
            committed_state = deepcopy(committed_session.__dict__)
            session.__dict__ = committed_state
            self._consumed_answer_ids = committed_answer_ids
            self._consumed_question_ids = committed_question_ids
            return returned_request, returned_session, result

    @staticmethod
    def _validate_transaction_candidate(
        authoritative: OnlineSessionBinding,
        candidate: OnlineSessionBinding,
    ) -> None:
        """Reject consumer changes to guard-owned identity and replay state."""

        if not isinstance(candidate, OnlineSessionBinding):
            raise OnlineContractError("invalid_session_binding", 500)
        if (
            not isinstance(candidate.session_id, str)
            or not candidate.session_id
            or candidate.session_id != authoritative.session_id
            or not isinstance(candidate.request_id, str)
            or not candidate.request_id
            or not (
                isinstance(candidate.turn_id, str) and bool(candidate.turn_id)
                or isinstance(candidate.turn_id, int)
                and not isinstance(candidate.turn_id, bool)
                and candidate.turn_id >= 0
            )
            or not isinstance(candidate.state_revision, int)
            or isinstance(candidate.state_revision, bool)
            or candidate.state_revision < 0
        ):
            raise OnlineContractError("invalid_session_binding", 500)
        if candidate.consumed_operations != authoritative.consumed_operations:
            raise OnlineContractError("invalid_session_binding", 500)

        pending = candidate.pending
        if pending is None:
            return
        if not isinstance(pending, PendingClarificationBinding):
            raise OnlineContractError("invalid_session_binding", 500)
        source_fields = (
            (pending.source_session_id, candidate.session_id),
            (pending.source_request_id, candidate.request_id),
            (pending.source_turn_id, candidate.turn_id),
            (pending.source_state_revision, candidate.state_revision),
        )
        if pending.options and any(source is None for source, _ in source_fields):
            raise OnlineContractError("invalid_session_binding", 500)
        if any(
            source is not None and source != actual
            for source, actual in source_fields
        ):
            raise OnlineContractError("invalid_session_binding", 500)

    def _resolve_session(self, request: Mapping[str, Any]) -> OnlineSessionBinding:
        session = self._sessions.get(str(request["session_id"]))
        if session is not None:
            return session
        if request.get("schema_version") == "2.0.0" and request.get(
            "operation"
        ) == "submit_clarification":
            question_id = request.get("question_id")
            matches = [
                item
                for item in self._sessions.values()
                if item.pending is not None
                and item.pending.question_id == question_id
            ]
            if len(matches) == 1:
                raise OnlineContractError("clarification_session_mismatch", 409)
        raise OnlineContractError("session_not_found", 404)

    @staticmethod
    def _validate_v1_binding(
        request: Mapping[str, Any], session: OnlineSessionBinding
    ) -> None:
        OnlineRequestGuard._require_equal(
            request, session, "request_id", "request_binding_mismatch"
        )
        OnlineRequestGuard._require_equal(
            request, session, "turn_id", "turn_binding_mismatch"
        )
        if request["state_revision"] != session.state_revision:
            raise OnlineContractError("stale_state_revision", 409)

    @staticmethod
    def _require_equal(
        request: Mapping[str, Any],
        session: OnlineSessionBinding,
        field_name: str,
        code: str,
    ) -> None:
        if request[field_name] != getattr(session, field_name):
            raise OnlineContractError(code, 409)

    def _validate_v2_clarification(
        self, request: Mapping[str, Any], session: OnlineSessionBinding
    ) -> None:
        answer_id = str(request["answer_id"])
        question_id = str(request["question_id"])
        if (
            answer_id in self._consumed_answer_ids
            or question_id in self._consumed_question_ids
        ):
            raise OnlineContractError("clarification_answer_replayed", 409)
        if request["request_id"] != session.request_id:
            raise OnlineContractError("clarification_request_mismatch", 409)
        if request["turn_id"] != session.turn_id:
            raise OnlineContractError("clarification_turn_mismatch", 409)
        if request["state_revision"] != session.state_revision:
            raise OnlineContractError("clarification_answer_stale", 409)
        pending = session.pending
        if pending is None:
            raise OnlineContractError("no_pending_clarification", 409)
        if request["question_id"] != pending.question_id:
            raise OnlineContractError("clarification_question_mismatch", 409)
        if request["slot_id"] != pending.slot_id:
            raise OnlineContractError("clarification_cross_slot_update", 409)
        selected = pending.option_for(request["values"][0])
        if selected is None:
            raise OnlineContractError("clarification_value_not_offered", 400)
        if request["evidence_refs"] != [selected.evidence_ref]:
            raise OnlineContractError("invalid_answer_evidence_refs", 400)


__all__ = [
    "DEFAULT_SCHEMA_ROOT",
    "ClarificationOptionBinding",
    "OnlineContractError",
    "OnlineContractValidator",
    "OnlineRequestGuard",
    "OnlineSessionBinding",
    "PendingClarificationBinding",
]
