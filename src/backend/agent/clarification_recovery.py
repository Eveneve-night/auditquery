"""D4-05 deterministic clarification, state merge, and safe recovery.

The module is deliberately offline.  It never calls a model, reads a secret,
or treats a selective-routing ``accept`` as SQL execution authority.  A SQL
proposal can only enter through an explicit injected port and is always sent
through the existing D4-02 safety gate before the readonly executor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError, ValidationError
from referencing import Registry, Resource

from ..nl2sql.readonly_executor import ExecutionResult, ReadonlyExecutor
from ..nl2sql.sql_safety_gate import SafetyDecision, validate_candidate

from .domain_pack import LoadedDomainPack, canonical_text_sha256
from .intent_routing import validate_conversation_state
from .loop_guard import LoopGuard, LoopPolicy
from .selective_routing import (
    D4SelectiveDecisionRuntime,
    RequiredSlotResolver,
    SelectiveRoutingError,
    SlotObservation,
    build_slot_observations,
)
from .typed_decision_runtime import CandidateEvidence


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_POLICY_PATH = (
    PROJECT_ROOT
    / "configs"
    / "policies"
    / "d4-05"
    / "clarification-recovery.v1.json"
)
DEFAULT_SCHEMA_ROOT = PROJECT_ROOT / "configs" / "schemas" / "d4-05"
DEFAULT_D4_SCHEMA_ROOT = PROJECT_ROOT / "configs" / "schemas" / "d4"
DEFAULT_LOOP_POLICY_PATH = (
    PROJECT_ROOT / "configs" / "policies" / "agent-loop-limits.v1.json"
)
POLICY_SCHEMA_NAME = "clarification-recovery-policy.v1.schema.json"
RESULT_SCHEMA_NAME = "clarification-recovery-result.v1.schema.json"
RESULT_VERSION = "1.0.0"
PLANNER_VERSION = "expected-information-gain.v1"
STATE_MERGER_VERSION = "strict-state-merger.v1"
_SCALAR_TYPES = (str, int, float, bool)
_FORBIDDEN_KEYS = {"label", "labels", "answer", "answers", "target_answer"}
_ALLOWED_QUESTION_ISSUES = {"missing", "conflicting"}


class ClarificationRecoveryError(RuntimeError):
    """Stable, content-safe D4-05 failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _read_json(path: Path, code: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ClarificationRecoveryError(code) from exc
    if not isinstance(value, dict):
        raise ClarificationRecoveryError(code)
    return value


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _forbidden_key(key: str) -> bool:
    normalized = key.casefold().replace("-", "_").replace(" ", "_")
    return (
        "expected" in normalized
        or "gold" in normalized
        or normalized in _FORBIDDEN_KEYS
    )


def _assert_no_answer_fields(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if _forbidden_key(str(key)):
                raise ClarificationRecoveryError("answer_derived_field_forbidden")
            _assert_no_answer_fields(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _assert_no_answer_fields(item)


def _equal(left: Any, right: Any) -> bool:
    if isinstance(left, str) and isinstance(right, str):
        return " ".join(left.casefold().split()) == " ".join(
            right.casefold().split()
        )
    return left == right


def _ensure_built_observations(
    observations: Mapping[str, SlotObservation],
) -> dict[str, SlotObservation]:
    if not isinstance(observations, Mapping):
        raise ClarificationRecoveryError("slot_observations_not_built")
    result: dict[str, SlotObservation] = {}
    for slot_id, observation in observations.items():
        if not isinstance(slot_id, str) or not isinstance(
            observation, SlotObservation
        ):
            raise ClarificationRecoveryError("slot_observations_not_built")
        result[slot_id] = observation
    return result


@dataclass(frozen=True)
class ClarificationOption:
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
class ClarificationQuestion:
    question_id: str
    session_id: str
    request_id: str
    turn_id: int
    state_revision: int
    slot_id: str
    issue: str
    prompt: str
    options: tuple[ClarificationOption, ...]
    evidence_refs: tuple[str, ...]
    information_gain: Mapping[str, float | int]
    baseline_slot_id: str
    planner_version: str = PLANNER_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "question_id": self.question_id,
            "session_id": self.session_id,
            "request_id": self.request_id,
            "turn_id": self.turn_id,
            "state_revision": self.state_revision,
            "slot_id": self.slot_id,
            "issue": self.issue,
            "prompt": self.prompt,
            "options": [item.to_dict() for item in self.options],
            "evidence_refs": list(self.evidence_refs),
            "information_gain": dict(self.information_gain),
            "baseline_slot_id": self.baseline_slot_id,
            "planner_version": self.planner_version,
        }


@dataclass(frozen=True)
class ClarificationAnswer:
    answer_id: str
    question_id: str
    session_id: str
    request_id: str
    turn_id: int
    state_revision: int
    slot_id: str
    values: tuple[str | int | float | bool, ...]
    evidence_refs: tuple[str, ...]


def build_clarification_answer(raw: Mapping[str, Any]) -> ClarificationAnswer:
    """Construct a strict structured answer; free-form private text is rejected."""

    if not isinstance(raw, Mapping):
        raise ClarificationRecoveryError("invalid_clarification_answer")
    _assert_no_answer_fields(raw)
    required = {
        "answer_id",
        "question_id",
        "session_id",
        "request_id",
        "turn_id",
        "state_revision",
        "slot_id",
        "values",
        "evidence_refs",
    }
    if set(raw) != required:
        raise ClarificationRecoveryError("invalid_clarification_answer_fields")
    identifiers = (
        raw["answer_id"],
        raw["question_id"],
        raw["session_id"],
        raw["request_id"],
        raw["slot_id"],
    )
    if any(not isinstance(item, str) or not item.strip() for item in identifiers):
        raise ClarificationRecoveryError("invalid_clarification_answer")
    if any(
        not isinstance(raw[name], int) or isinstance(raw[name], bool) or raw[name] < 0
        for name in ("turn_id", "state_revision")
    ):
        raise ClarificationRecoveryError("invalid_clarification_answer")
    values = raw["values"]
    if not isinstance(values, list) or not values:
        raise ClarificationRecoveryError("empty_clarification_answer")
    if len(values) != 1:
        raise ClarificationRecoveryError("multiple_answer_values")
    if any(
        not isinstance(value, _SCALAR_TYPES)
        or isinstance(value, (dict, list, tuple))
        for value in values
    ):
        raise ClarificationRecoveryError("invalid_clarification_answer_value")
    evidence_refs = raw["evidence_refs"]
    if (
        not isinstance(evidence_refs, list)
        or not evidence_refs
        or len(evidence_refs) != len(set(evidence_refs))
        or any(not isinstance(item, str) or not item for item in evidence_refs)
    ):
        raise ClarificationRecoveryError("invalid_answer_evidence_refs")
    return ClarificationAnswer(
        answer_id=str(raw["answer_id"]),
        question_id=str(raw["question_id"]),
        session_id=str(raw["session_id"]),
        request_id=str(raw["request_id"]),
        turn_id=int(raw["turn_id"]),
        state_revision=int(raw["state_revision"]),
        slot_id=str(raw["slot_id"]),
        values=tuple(values),
        evidence_refs=tuple(sorted(evidence_refs)),
    )


@dataclass(frozen=True)
class SqlProposal:
    """Offline-injectable SQL candidate; this object never authorizes execution."""

    proposal_id: str
    sql: str
    tables: tuple[str, ...]
    params: tuple[Any, ...] = ()

    def __post_init__(self) -> None:
        if not self.proposal_id or not self.sql or not self.tables:
            raise ClarificationRecoveryError("invalid_sql_proposal")


@runtime_checkable
class SqlProposalPort(Protocol):
    """Port for a future SQL generator; D4-05 tests inject an offline stub."""

    def propose(
        self,
        *,
        conversation_state: Mapping[str, Any],
        slot_observations: Mapping[str, SlotObservation],
    ) -> SqlProposal: ...


class ClarificationPlanner:
    """Choose exactly one domain-pack-backed issue by expected information gain."""

    def __init__(
        self,
        domain_pack: LoadedDomainPack,
        *,
        policy: Mapping[str, Any],
        policy_sha256: str,
    ) -> None:
        self.domain_pack = domain_pack
        self.policy = policy
        self.policy_sha256 = policy_sha256
        self._slots = {
            str(item["slot_id"]): item
            for item in domain_pack.artifact_items("slots")
        }
        self._evidence = {
            str(item["evidence_id"]): item
            for item in domain_pack.artifact_items("value_evidence")
        }
        self._rules = tuple(domain_pack.artifact_items("validation_rules"))

    def plan(
        self,
        *,
        selective_result: Mapping[str, Any],
        session_id: str,
        request_id: str,
        turn_id: int,
        state_revision: int,
    ) -> ClarificationQuestion:
        if selective_result.get("action") != "clarify":
            raise ClarificationRecoveryError("planner_requires_clarify_action")
        issues = selective_result.get("typed_decision", {}).get(
            "missing_or_conflicting_slots"
        )
        if not isinstance(issues, list) or not issues:
            raise ClarificationRecoveryError("clarification_issue_missing")
        candidates: list[tuple[float, str, Mapping[str, Any], tuple[ClarificationOption, ...], dict[str, float | int]]] = []
        for issue in issues:
            if not isinstance(issue, Mapping):
                raise ClarificationRecoveryError("invalid_clarification_issue")
            slot_id = issue.get("slot_id")
            issue_kind = issue.get("issue")
            if slot_id not in self._slots or issue_kind not in _ALLOWED_QUESTION_ISSUES:
                raise ClarificationRecoveryError("invalid_clarification_issue")
            for reference in issue.get("evidence_refs", []):
                try:
                    self.domain_pack.resolve(str(reference))
                except Exception as exc:
                    raise ClarificationRecoveryError(
                        "clarification_evidence_unknown"
                    ) from exc
            try:
                options = self._options_for_slot(str(slot_id))
            except ClarificationRecoveryError as exc:
                if exc.code == "clarification_options_unavailable":
                    continue
                raise
            features = self._information_gain_features(
                str(slot_id), str(issue_kind), options
            )
            candidates.append(
                (float(features["weighted_score"]), str(slot_id), issue, options, features)
            )
        if not candidates:
            raise ClarificationRecoveryError("clarification_options_unavailable")
        baseline_slot_id = str(issues[0]["slot_id"])
        candidates.sort(key=lambda item: (-item[0], item[1]))
        _, slot_id, issue, options, features = candidates[0]
        slot = self._slots[slot_id]
        issue_word = "缺失" if issue["issue"] == "missing" else "冲突"
        prompt = f"{slot['display_name']}信息{issue_word}，请选择一个合法值。"
        seed = {
            "session_id": session_id,
            "request_id": request_id,
            "turn_id": turn_id,
            "state_revision": state_revision,
            "slot_id": slot_id,
            "policy_sha256": self.policy_sha256,
        }
        question_id = f"clarify.{_canonical_sha256(seed)[:24]}"
        refs = {
            slot_id,
            *slot["source_refs"],
            *(item.evidence_ref for item in options),
            *issue.get("evidence_refs", []),
        }
        return ClarificationQuestion(
            question_id=question_id,
            session_id=session_id,
            request_id=request_id,
            turn_id=turn_id,
            state_revision=state_revision,
            slot_id=slot_id,
            issue=str(issue["issue"]),
            prompt=prompt,
            options=options,
            evidence_refs=tuple(sorted(str(item) for item in refs)),
            information_gain=features,
            baseline_slot_id=baseline_slot_id,
        )

    def _options_for_slot(self, slot_id: str) -> tuple[ClarificationOption, ...]:
        slot = self._slots[slot_id]
        options: list[ClarificationOption] = []
        for reference in slot["allowed_value_evidence_refs"]:
            evidence = self._evidence.get(str(reference))
            if evidence is None:
                raise ClarificationRecoveryError("clarification_evidence_unknown")
            if evidence["send_to_model"] is not True or evidence["data_level"] not in {
                "L0",
                "L1",
            }:
                raise ClarificationRecoveryError("clarification_evidence_not_authorized")
            evidence_type = str(evidence["evidence_type"])
            payload = evidence["payload"]
            values: Sequence[Any]
            if evidence_type in {"enum", "dictionary"}:
                values = payload.get("values", [])
            elif evidence_type == "range":
                values = self._range_options(slot_id, payload)
            else:
                raise ClarificationRecoveryError("clarification_options_unavailable")
            for value in values:
                if not isinstance(value, _SCALAR_TYPES):
                    raise ClarificationRecoveryError("clarification_options_invalid")
                options.append(
                    ClarificationOption(
                        value=value,
                        display_name=str(value),
                        evidence_ref=str(reference),
                    )
                )
        unique: list[ClarificationOption] = []
        for option in options:
            if not any(_equal(option.value, item.value) for item in unique):
                unique.append(option)
        if not unique or len(unique) > 64:
            raise ClarificationRecoveryError("clarification_options_unavailable")
        return tuple(unique)

    def _range_options(
        self, slot_id: str, payload: Mapping[str, Any]
    ) -> tuple[str | int | float, ...]:
        minimum = payload.get("minimum")
        maximum = payload.get("maximum")
        if slot_id == "slot.time_range":
            config = self.policy["state_merge"]["time_range"]
            if minimum != config["minimum_year"] or maximum != config["maximum_year"]:
                raise ClarificationRecoveryError("time_range_policy_mismatch")
            return tuple(
                str(year)
                for year in range(config["minimum_year"], config["maximum_year"] + 1)
            )
        if isinstance(minimum, int) and isinstance(maximum, int):
            if maximum < minimum or maximum - minimum > 31:
                raise ClarificationRecoveryError("clarification_options_unavailable")
            return tuple(range(minimum, maximum + 1))
        if isinstance(minimum, int) and maximum is None:
            # A small, deterministic suggestion set derived only from the
            # declared lower bound; no test answer or database value is used.
            return (minimum, minimum + 4, minimum + 9)
        raise ClarificationRecoveryError("clarification_options_unavailable")

    def _information_gain_features(
        self,
        slot_id: str,
        issue: str,
        options: Sequence[ClarificationOption],
    ) -> dict[str, float | int]:
        option_count = len(options)
        candidate_reduction = 1.0 - (1.0 / option_count)
        downstream_count = sum(
            1
            for slot in self._slots.values()
            for condition in slot["required_when"]
            if condition["subject_ref"] == slot_id
        ) + sum(
            1 for rule in self._rules if slot_id in set(rule["input_refs"])
        )
        downstream_impact = min(downstream_count / 4.0, 1.0)
        legal_information = min(math.log2(option_count + 1) / 4.0, 1.0)
        if issue == "conflicting":
            candidate_reduction = min(1.0, candidate_reduction + 0.1)
        weights = self.policy["planner"]["weights"]
        weighted = (
            weights["candidate_reduction_gain"] * candidate_reduction
            + weights["downstream_condition_impact"] * downstream_impact
            + weights["legal_value_information"] * legal_information
        )
        return {
            "candidate_reduction_gain": round(candidate_reduction, 6),
            "downstream_condition_count": downstream_count,
            "downstream_condition_impact": round(downstream_impact, 6),
            "legal_value_count": option_count,
            "legal_value_information": round(legal_information, 6),
            "weighted_score": round(weighted, 6),
        }


class StateMerger:
    """Merge one strictly built reply into only its pending slot."""

    version = STATE_MERGER_VERSION

    def __init__(
        self,
        domain_pack: LoadedDomainPack,
        *,
        policy: Mapping[str, Any],
    ) -> None:
        self.domain_pack = domain_pack
        self.policy = policy
        self.resolver = RequiredSlotResolver(domain_pack)

    def merge(
        self,
        *,
        question: ClarificationQuestion | None,
        answer: ClarificationAnswer,
        observations: Mapping[str, SlotObservation],
        intent: str,
        candidates: Sequence[CandidateEvidence],
        consumed_question_ids: set[str],
        consumed_answer_ids: set[str],
    ) -> tuple[dict[str, SlotObservation], dict[str, Any]]:
        current = _ensure_built_observations(observations)
        if answer.answer_id in consumed_answer_ids or answer.question_id in consumed_question_ids:
            raise ClarificationRecoveryError("clarification_answer_replayed")
        if question is None:
            raise ClarificationRecoveryError("no_pending_clarification")
        if answer.session_id != question.session_id:
            raise ClarificationRecoveryError("clarification_session_mismatch")
        if answer.request_id != question.request_id:
            raise ClarificationRecoveryError("clarification_request_mismatch")
        if answer.question_id != question.question_id:
            raise ClarificationRecoveryError("clarification_question_mismatch")
        if answer.turn_id != question.turn_id:
            raise ClarificationRecoveryError("clarification_turn_mismatch")
        if answer.state_revision != question.state_revision:
            raise ClarificationRecoveryError("clarification_answer_stale")
        if answer.slot_id != question.slot_id:
            raise ClarificationRecoveryError("clarification_cross_slot_update")

        if question.slot_id == "slot.time_range":
            pattern = self.policy["state_merge"]["time_range"]["pattern"]
            if not isinstance(answer.values[0], str) or re.fullmatch(
                pattern, answer.values[0]
            ) is None:
                raise ClarificationRecoveryError("invalid_time_range_format")
        selected = next(
            (
                option
                for option in question.options
                if _equal(option.value, answer.values[0])
            ),
            None,
        )
        if selected is None:
            raise ClarificationRecoveryError("clarification_value_not_offered")
        if set(answer.evidence_refs) != {selected.evidence_ref}:
            raise ClarificationRecoveryError("invalid_answer_evidence_refs")
        try:
            built = build_slot_observations(
                {
                    question.slot_id: {
                        "values": list(answer.values),
                        "evidence_refs": list(answer.evidence_refs),
                    }
                }
            )
            merged = dict(current)
            merged[question.slot_id] = built[question.slot_id]
            resolution = self.resolver.resolve(
                intent=intent,
                candidates=candidates,
                observations=merged,
            )
        except SelectiveRoutingError as exc:
            raise ClarificationRecoveryError("clarification_answer_invalid") from exc
        assessment = resolution.by_id()[question.slot_id]
        if assessment.status != "filled":
            raise ClarificationRecoveryError("clarification_answer_no_progress")
        answer_digest = _canonical_sha256(
            {
                "question_id": answer.question_id,
                "slot_id": answer.slot_id,
                "values": list(answer.values),
                "evidence_refs": list(answer.evidence_refs),
            }
        )
        event = {
            "slot_id": question.slot_id,
            "issue_before": question.issue,
            "status_after": assessment.status,
            "value_count": assessment.value_count,
            "evidence_refs": list(assessment.evidence_refs),
            "answer_sha256": answer_digest,
            "state_merger_version": self.version,
        }
        return merged, event


@dataclass
class RecoverySession:
    session_id: str
    request_id: str
    conversation_state: Mapping[str, Any]
    observations: dict[str, SlotObservation]
    candidates: tuple[CandidateEvidence, ...]
    current_decision: dict[str, Any]
    guard: LoopGuard
    decision_recorded_at: str
    decision_top_k: int
    pending_question: ClarificationQuestion | None = None
    questions: list[ClarificationQuestion] = field(default_factory=list)
    answer_summaries: list[dict[str, Any]] = field(default_factory=list)
    merge_events: list[dict[str, Any]] = field(default_factory=list)
    decision_traces: list[dict[str, Any]] = field(default_factory=list)
    audit_events: list[dict[str, Any]] = field(default_factory=list)
    consumed_question_ids: set[str] = field(default_factory=set)
    consumed_answer_ids: set[str] = field(default_factory=set)
    workflow_status: str = "initialized"
    stop_reason: str | None = None
    safety_gate_calls: int = 0
    local_sqlite_execution_attempts: int = 0
    local_sqlite_execution_successes: int = 0
    safety_approved: bool | None = None


class D4ClarificationRecoveryRuntime:
    """D4-01 -> D4-03 -> D4-04 -> D4-05 offline orchestration."""

    def __init__(
        self,
        domain_pack: LoadedDomainPack,
        *,
        policy_path: Path | str = DEFAULT_POLICY_PATH,
        schema_root: Path | str = DEFAULT_SCHEMA_ROOT,
        loop_policy_path: Path | str = DEFAULT_LOOP_POLICY_PATH,
    ) -> None:
        self.domain_pack = domain_pack
        self.policy_path = Path(policy_path)
        self.schema_root = Path(schema_root)
        self.policy = _read_json(self.policy_path, "invalid_clarification_policy")
        self._validate_policy()
        self.policy_sha256 = canonical_text_sha256(self.policy_path)
        self.loop_policy = LoopPolicy.from_file(loop_policy_path)
        self.selective = D4SelectiveDecisionRuntime(domain_pack)
        self.planner = ClarificationPlanner(
            domain_pack,
            policy=self.policy,
            policy_sha256=self.policy_sha256,
        )
        self.merger = StateMerger(domain_pack, policy=self.policy)
        self._result_validator = self._build_result_validator()

    def _validate_policy(self) -> None:
        schema = _read_json(
            self.schema_root / POLICY_SCHEMA_NAME,
            "invalid_clarification_policy_schema",
        )
        try:
            Draft202012Validator.check_schema(schema)
            Draft202012Validator(
                schema, format_checker=FormatChecker()
            ).validate(self.policy)
        except (SchemaError, ValidationError) as exc:
            raise ClarificationRecoveryError("invalid_clarification_policy") from exc
        total = sum(self.policy["planner"]["weights"].values())
        if abs(total - 1.0) > 1e-9:
            raise ClarificationRecoveryError("invalid_information_gain_weights")

    def _build_result_validator(self) -> Draft202012Validator:
        result_schema = _read_json(
            self.schema_root / RESULT_SCHEMA_NAME,
            "invalid_clarification_result_schema",
        )
        decision_schema = _read_json(
            DEFAULT_D4_SCHEMA_ROOT / "typed-decision.v1.schema.json",
            "invalid_clarification_result_schema",
        )
        trace_schema = _read_json(
            DEFAULT_D4_SCHEMA_ROOT / "typed-decision-trace-event.v1.schema.json",
            "invalid_clarification_result_schema",
        )
        try:
            for schema in (result_schema, decision_schema, trace_schema):
                Draft202012Validator.check_schema(schema)
            registry = Registry().with_resource(
                decision_schema["$id"], Resource.from_contents(decision_schema)
            ).with_resource(
                trace_schema["$id"], Resource.from_contents(trace_schema)
            )
            return Draft202012Validator(
                result_schema,
                registry=registry,
                format_checker=FormatChecker(),
            )
        except SchemaError as exc:
            raise ClarificationRecoveryError(
                "invalid_clarification_result_schema"
            ) from exc

    def start(
        self,
        *,
        conversation_state: Mapping[str, Any],
        slot_observations: Mapping[str, SlotObservation],
        request_id: str,
        recorded_at: str,
        top_k: int = 5,
    ) -> RecoverySession:
        observations = _ensure_built_observations(slot_observations)
        try:
            validate_conversation_state(conversation_state)
        except Exception as exc:
            raise ClarificationRecoveryError("invalid_conversation_state") from exc
        session_id = str(conversation_state["session_id"])
        query = str(conversation_state["current_request"]["query"])
        try:
            candidates = self.selective.retriever.retrieve(query, top_k=top_k)
            decision = self.selective.evaluate(
                conversation_state=conversation_state,
                slot_observations=observations,
                request_id=request_id,
                recorded_at=recorded_at,
                top_k=top_k,
            )
        except SelectiveRoutingError as exc:
            raise ClarificationRecoveryError("initial_selective_decision_failed") from exc
        except Exception as exc:
            raise ClarificationRecoveryError("initial_selective_decision_failed") from exc
        guard = LoopGuard(self.loop_policy)
        guard_decision = guard.record(
            "state_transition", action="initial_decision", elapsed_ms=0
        )
        if not guard_decision.allowed:
            raise ClarificationRecoveryError(guard_decision.stop_reason or "loop_guard_stopped")
        session = RecoverySession(
            session_id=session_id,
            request_id=request_id,
            conversation_state=dict(conversation_state),
            observations=observations,
            candidates=tuple(candidates),
            current_decision=decision,
            guard=guard,
            decision_recorded_at=recorded_at,
            decision_top_k=top_k,
            decision_traces=[decision["trace_event"]],
        )
        self._append_audit(
            session,
            event_type="initial_decision",
            action=decision["action"],
            reason_code=decision["reason_code"],
            artifact=decision["trace_event"],
        )
        self._after_decision(session, recorded_at=recorded_at)
        return session

    def resume(
        self,
        session: RecoverySession,
        answer: ClarificationAnswer,
        *,
        recorded_at: str,
        top_k: int = 5,
    ) -> RecoverySession:
        if not isinstance(answer, ClarificationAnswer):
            raise ClarificationRecoveryError("clarification_answer_not_built")
        previous_issue_signature = self._issue_signature(session.current_decision)
        try:
            merged, merge_event = self.merger.merge(
                question=session.pending_question,
                answer=answer,
                observations=session.observations,
                intent=str(session.conversation_state["route"]["intent"]),
                candidates=session.candidates,
                consumed_question_ids=session.consumed_question_ids,
                consumed_answer_ids=session.consumed_answer_ids,
            )
        except ClarificationRecoveryError:
            raise
        except Exception as exc:
            raise ClarificationRecoveryError("state_merge_failed") from exc
        assert session.pending_question is not None
        question = session.pending_question
        try:
            decision = self.selective.evaluate(
                conversation_state=session.conversation_state,
                slot_observations=merged,
                request_id=session.request_id,
                recorded_at=recorded_at,
                top_k=top_k,
            )
            candidates = self.selective.retriever.retrieve(
                str(session.conversation_state["current_request"]["query"]),
                top_k=top_k,
            )
        except SelectiveRoutingError as exc:
            raise ClarificationRecoveryError("redecision_failed") from exc
        except Exception as exc:
            raise ClarificationRecoveryError("redecision_failed") from exc

        # The merge and all consumption/audit mutations are committed only
        # after D4-04 redecision succeeds.  A caller may retry the same valid
        # answer after a transient/invalid redecision request without landing
        # in an unrecoverable half-committed state.
        guard_decision = session.guard.record(
            "state_transition", action="state_merge", elapsed_ms=0
        )
        if not guard_decision.allowed:
            self._stop(session, guard_decision.stop_reason or "loop_guard_stopped")
            return session
        answer_summary = {
            "answer_id": answer.answer_id,
            "question_id": answer.question_id,
            "slot_id": answer.slot_id,
            "value_count": len(answer.values),
            "evidence_refs": list(answer.evidence_refs),
            "answer_sha256": merge_event["answer_sha256"],
        }
        session.consumed_question_ids.add(question.question_id)
        session.consumed_answer_ids.add(answer.answer_id)
        session.answer_summaries.append(answer_summary)
        session.merge_events.append(merge_event)
        session.observations = merged
        session.pending_question = None
        self._append_audit(
            session,
            event_type="structured_answer",
            slot_id=answer.slot_id,
            evidence_refs=answer.evidence_refs,
            value_count=len(answer.values),
            artifact=answer_summary,
        )
        self._append_audit(
            session,
            event_type="state_merge",
            slot_id=answer.slot_id,
            reason_code="slot_filled",
            evidence_refs=merge_event["evidence_refs"],
            value_count=merge_event["value_count"],
            artifact=merge_event,
        )
        session.candidates = tuple(candidates)
        session.current_decision = decision
        session.decision_recorded_at = recorded_at
        session.decision_top_k = top_k
        session.decision_traces.append(decision["trace_event"])
        self._append_audit(
            session,
            event_type="redecision",
            action=decision["action"],
            reason_code=decision["reason_code"],
            artifact=decision["trace_event"],
        )
        if decision["action"] == "clarify" and self._issue_signature(
            decision
        ) == previous_issue_signature:
            self._stop(session, "clarification_no_progress")
            return session
        self._after_decision(session, recorded_at=recorded_at)
        return session

    def handoff_and_execute(
        self,
        session: RecoverySession,
        *,
        proposal_port: SqlProposalPort,
        executor: ReadonlyExecutor | None = None,
    ) -> ExecutionResult | None:
        self._validate_execution_handoff(session)
        if not isinstance(proposal_port, SqlProposalPort):
            raise ClarificationRecoveryError("invalid_sql_proposal_port")
        try:
            proposal = proposal_port.propose(
                conversation_state=session.conversation_state,
                slot_observations=session.observations,
            )
        except Exception as exc:
            raise ClarificationRecoveryError("sql_proposal_failed") from exc
        if not isinstance(proposal, SqlProposal):
            raise ClarificationRecoveryError("invalid_sql_proposal")
        session.safety_gate_calls += 1
        try:
            safety = validate_candidate(
                proposal.sql,
                list(proposal.tables),
                list(proposal.params),
            )
        except Exception as exc:
            self._stop(session, "sql_safety_gate_failed")
            raise ClarificationRecoveryError("sql_safety_gate_failed") from exc
        session.safety_approved = safety.approved
        safety_reason = (
            "safety_gate_approved"
            if safety.approved
            else self._safety_rejection_code(safety)
        )
        self._append_audit(
            session,
            event_type="safety_gate",
            action="accept" if safety.approved else "reject",
            reason_code=safety_reason,
            artifact={
                "proposal_id": proposal.proposal_id,
                "approved": safety.approved,
                "tables": list(proposal.tables),
            },
        )
        if not safety.approved:
            session.workflow_status = "safety_rejected"
            return None
        guard_decision = session.guard.record(
            "sql_execution", action="readonly_sql_execution", elapsed_ms=0
        )
        if not guard_decision.allowed:
            self._stop(session, guard_decision.stop_reason or "loop_guard_stopped")
            return None
        session.local_sqlite_execution_attempts += 1
        active_executor = executor or ReadonlyExecutor()
        try:
            result = active_executor.execute(safety, proposal.params)
        except Exception as exc:
            self._stop(session, "readonly_execution_failed")
            raise ClarificationRecoveryError("readonly_execution_failed") from exc
        session.local_sqlite_execution_successes += 1
        session.workflow_status = "executed"
        self._append_audit(
            session,
            event_type="sql_execution",
            action="accept",
            reason_code="readonly_execution_completed",
            value_count=result.row_count,
            artifact={
                "columns": list(result.columns),
                "row_count": result.row_count,
                "truncated": result.truncated,
            },
        )
        return result

    def _validate_execution_handoff(self, session: RecoverySession) -> None:
        """Recompute D4-04 and reject mutable-session authorization drift."""

        if not isinstance(session, RecoverySession):
            raise ClarificationRecoveryError("execution_session_invalid")
        if (
            session.workflow_status != "accepted_for_safety_handoff"
            or session.pending_question is not None
            or not session.answer_summaries
            or not session.merge_events
            or not session.audit_events
            or session.audit_events[-1]["event_type"] != "redecision"
        ):
            raise ClarificationRecoveryError("execution_session_not_ready")
        try:
            recomputed = self.selective.evaluate(
                conversation_state=session.conversation_state,
                slot_observations=session.observations,
                request_id=session.request_id,
                recorded_at=session.decision_recorded_at,
                top_k=session.decision_top_k,
            )
        except Exception as exc:
            raise ClarificationRecoveryError("execution_redecision_failed") from exc
        if recomputed["action"] != "accept":
            raise ClarificationRecoveryError("execution_redecision_not_accept")
        if _canonical_sha256(recomputed) != _canonical_sha256(
            session.current_decision
        ):
            raise ClarificationRecoveryError("execution_decision_mismatch")
        if recomputed["sql_execution_authorized"] is not False:
            raise ClarificationRecoveryError("accept_execution_authority_forbidden")
        if recomputed["requires_d4_02_safety_gate"] is not True:
            raise ClarificationRecoveryError("safety_gate_required")

    def result(self, session: RecoverySession) -> dict[str, Any]:
        result = {
            "result_version": RESULT_VERSION,
            "policy_ref": {
                "policy_id": self.policy["policy_id"],
                "version": self.policy["version"],
                "sha256": self.policy_sha256,
            },
            "domain_pack_ref": {
                "domain_pack_id": self.domain_pack.domain_pack_id,
                "version": self.domain_pack.version,
                "manifest_sha256": self.domain_pack.manifest_sha256,
            },
            "session_id": session.session_id,
            "request_id": session.request_id,
            "workflow_status": session.workflow_status,
            "current_action": session.current_decision["action"],
            "stop_reason": session.stop_reason,
            "pending_question": (
                session.pending_question.to_dict()
                if session.pending_question is not None
                else None
            ),
            "questions": [item.to_dict() for item in session.questions],
            "answer_summaries": list(session.answer_summaries),
            "merge_events": list(session.merge_events),
            "decision_traces": list(session.decision_traces),
            "audit_events": list(session.audit_events),
            "safety": {
                "accept_authorizes_sql_execution": False,
                "requires_d4_02_safety_gate": True,
                "safety_gate_calls": session.safety_gate_calls,
                "safety_approved": session.safety_approved,
                "local_sqlite_execution_attempts": session.local_sqlite_execution_attempts,
                "local_sqlite_execution_successes": session.local_sqlite_execution_successes,
            },
            "data_exposure": {
                "model_api_calls": 0,
                "network_evaluation_calls": 0,
                "credential_files_read": 0,
                "validation_exposures": 0,
                "hidden_test_exposures": 0,
            },
        }
        self.validate_result(result)
        return result

    def validate_result(self, result: Mapping[str, Any]) -> None:
        try:
            self._result_validator.validate(result)
        except ValidationError as exc:
            raise ClarificationRecoveryError(
                "clarification_result_schema_invalid"
            ) from exc
        if result["safety"]["accept_authorizes_sql_execution"] is not False:
            raise ClarificationRecoveryError("accept_execution_authority_forbidden")
        if result["data_exposure"] != {
            "model_api_calls": 0,
            "network_evaluation_calls": 0,
            "credential_files_read": 0,
            "validation_exposures": 0,
            "hidden_test_exposures": 0,
        }:
            raise ClarificationRecoveryError("data_exposure_boundary_violated")

    def _after_decision(
        self, session: RecoverySession, *, recorded_at: str
    ) -> None:
        action = session.current_decision["action"]
        if action == "clarify":
            turn_id = len(session.questions) + 1
            guard_decision = session.guard.record(
                "clarification",
                action="clarification_question",
                scope_id=session.session_id,
                elapsed_ms=0,
            )
            if not guard_decision.allowed:
                self._stop(
                    session, guard_decision.stop_reason or "loop_guard_stopped"
                )
                return
            try:
                question = self.planner.plan(
                    selective_result=session.current_decision,
                    session_id=session.session_id,
                    request_id=session.request_id,
                    turn_id=turn_id,
                    state_revision=int(session.conversation_state["revision"]),
                )
            except ClarificationRecoveryError:
                raise
            except Exception as exc:
                raise ClarificationRecoveryError("clarification_planning_failed") from exc
            session.pending_question = question
            session.questions.append(question)
            session.workflow_status = "awaiting_clarification"
            self._append_audit(
                session,
                event_type="clarification_question",
                slot_id=question.slot_id,
                action="clarify",
                reason_code=question.issue,
                evidence_refs=question.evidence_refs,
                artifact=question.to_dict(),
            )
        elif action == "accept":
            session.workflow_status = "accepted_for_safety_handoff"
        elif action == "escalate":
            session.workflow_status = "escalated"
        else:
            session.workflow_status = "rejected"

    @staticmethod
    def _issue_signature(decision: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
        issues = decision.get("typed_decision", {}).get(
            "missing_or_conflicting_slots", []
        )
        return tuple(
            sorted((str(item["slot_id"]), str(item["issue"])) for item in issues)
        )

    @staticmethod
    def _safety_rejection_code(decision: SafetyDecision) -> str:
        reason = decision.reason or ""
        categories = (
            ("JOIN", "join_forbidden"),
            ("子查询", "subquery_forbidden"),
            ("写操作", "write_forbidden"),
            ("多语句", "multiple_statements_forbidden"),
            ("声明", "table_declaration_invalid"),
            ("LIMIT", "row_limit_invalid"),
            ("参数", "parameter_binding_invalid"),
            ("未知表", "unknown_table"),
        )
        for marker, code in categories:
            if marker in reason:
                return code
        return "sql_safety_rejected"

    def _append_audit(
        self,
        session: RecoverySession,
        *,
        event_type: str,
        slot_id: str | None = None,
        action: str | None = None,
        reason_code: str | None = None,
        evidence_refs: Sequence[str] = (),
        value_count: int | None = None,
        artifact: Any = None,
    ) -> None:
        session.audit_events.append(
            {
                "sequence": len(session.audit_events) + 1,
                "event_type": event_type,
                "slot_id": slot_id,
                "action": action,
                "reason_code": reason_code,
                "evidence_refs": sorted(set(evidence_refs)),
                "value_count": value_count,
                "artifact_sha256": _canonical_sha256(artifact),
            }
        )

    def _stop(self, session: RecoverySession, reason: str) -> None:
        session.guard.terminate(reason, elapsed_ms=0)
        session.stop_reason = reason
        session.workflow_status = "stopped"
        session.pending_question = None
        self._append_audit(
            session,
            event_type="workflow_stopped",
            action="escalate",
            reason_code=reason,
            artifact={"reason": reason},
        )


__all__ = [
    "ClarificationAnswer",
    "ClarificationOption",
    "ClarificationPlanner",
    "ClarificationQuestion",
    "ClarificationRecoveryError",
    "D4ClarificationRecoveryRuntime",
    "DEFAULT_POLICY_PATH",
    "RecoverySession",
    "SqlProposal",
    "SqlProposalPort",
    "StateMerger",
    "build_clarification_answer",
]
