"""D4-04 automatic required-slot discovery and selective routing.

This module is deliberately deterministic and offline.  It derives slot
requirements from one validated domain pack, evaluates explicit conversation
state, and records an uncalibrated raw-score routing decision.  ``accept`` is
only a hand-off decision: it never authorizes or executes SQL.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Sequence

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError, ValidationError
from referencing import Registry, Resource

from .domain_pack import (
    DEFAULT_SCHEMA_ROOT,
    DomainPackError,
    LoadedDomainPack,
    canonical_text_sha256,
)
from .intent_routing import validate_conversation_state
from .typed_decision_runtime import (
    CandidateEvidence,
    CandidateRetriever,
    EvidenceFeatureAssembler,
    TypedDecisionRuntimeError,
)


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_D4_04_SCHEMA_ROOT = PROJECT_ROOT / "configs" / "schemas" / "d4-04"
DEFAULT_POLICY_PATH = (
    PROJECT_ROOT
    / "configs"
    / "policies"
    / "d4-04"
    / "selective-decision.v1.json"
)
POLICY_SCHEMA_NAME = "selective-decision-policy.v1.schema.json"
RESULT_SCHEMA_NAME = "selective-routing-result.v1.schema.json"
RESULT_VERSION = "1.0.0"
REQUIRED_SLOTS_SOURCE = "auto_domain_pack"
MODE = "uncalibrated_raw_score"
POLICY_COMPONENT_VERSION = "d4-selective-decision.v1"
CALIBRATOR_INTERFACE_VERSION = "uncalibrated-identity.v1"
SUPPORTED_REQUIRED_WHEN_OPERATORS = frozenset({"equals", "in", "present"})
SUPPORTED_VALIDATION_OPERATORS = frozenset({"in", "conflicts"})
_ALLOWED_ACTIONS = frozenset({"accept", "clarify", "escalate", "reject"})
_EXPECTED_PRIORITY = (
    "policy_restriction",
    "hard_slot_issue",
    "unknown_slot_state",
    "outside_candidate_set",
    "raw_score_threshold",
    "accept",
)
_SCALAR_TYPES = (str, int, float, bool)
_FORBIDDEN_KEYS = {"label", "labels", "answer", "answers", "target_answer"}


class SelectiveRoutingError(RuntimeError):
    """Stable and content-safe D4-04 failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _read_json(path: Path, code: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SelectiveRoutingError(code) from exc
    if not isinstance(value, dict):
        raise SelectiveRoutingError(code)
    return value


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
                raise SelectiveRoutingError("answer_derived_field_forbidden")
            _assert_no_answer_fields(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _assert_no_answer_fields(item)


def _normalize_scalar(value: Any) -> Any:
    if isinstance(value, str):
        return " ".join(value.casefold().split())
    return value


def _equal(left: Any, right: Any) -> bool:
    return _normalize_scalar(left) == _normalize_scalar(right)


def _deduplicate(values: Iterable[Any]) -> tuple[Any, ...]:
    unique: list[Any] = []
    for value in values:
        if not any(_equal(value, existing) for existing in unique):
            unique.append(value)
    return tuple(unique)


@dataclass(frozen=True)
class SlotObservation:
    """Explicit conversation-state values without answer-derived metadata."""

    values: tuple[str | int | float | bool, ...]
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True)
class SlotAssessment:
    """Content-safe status for one domain-pack slot.

    ``required`` is tri-state: ``None`` means the declarative condition could
    not be determined.  Raw values are intentionally omitted from serialization.
    """

    slot_id: str
    required: bool | None
    status: str
    value_count: int
    evidence_refs: tuple[str, ...]
    reason_codes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "slot_id": self.slot_id,
            "required": self.required,
            "status": self.status,
            "value_count": self.value_count,
            "evidence_refs": list(self.evidence_refs),
            "reason_codes": list(self.reason_codes),
        }


@dataclass(frozen=True)
class SlotResolution:
    assessments: tuple[SlotAssessment, ...]
    hard_rule_violations: tuple[str, ...]
    soft_rule_violations: tuple[str, ...]

    def by_id(self) -> dict[str, SlotAssessment]:
        return {item.slot_id: item for item in self.assessments}


def build_slot_observations(
    raw_state: Mapping[str, Any],
) -> dict[str, SlotObservation]:
    """Build observations from a strict allowlist; reject answer-bearing keys."""

    if not isinstance(raw_state, Mapping):
        raise SelectiveRoutingError("invalid_slot_observations")
    _assert_no_answer_fields(raw_state)
    observations: dict[str, SlotObservation] = {}
    for slot_id, payload in raw_state.items():
        if not isinstance(slot_id, str) or not isinstance(payload, Mapping):
            raise SelectiveRoutingError("invalid_slot_observations")
        if set(payload) != {"values", "evidence_refs"}:
            raise SelectiveRoutingError("invalid_slot_observation_fields")
        values = payload["values"]
        evidence_refs = payload["evidence_refs"]
        if (
            not isinstance(values, list)
            or len(values) > 16
            or any(
                not isinstance(value, _SCALAR_TYPES)
                or isinstance(value, (dict, list, tuple))
                for value in values
            )
        ):
            raise SelectiveRoutingError("invalid_slot_values")
        if (
            not isinstance(evidence_refs, list)
            or len(evidence_refs) > 32
            or any(not isinstance(ref, str) for ref in evidence_refs)
            or len(evidence_refs) != len(set(evidence_refs))
        ):
            raise SelectiveRoutingError("invalid_slot_evidence_refs")
        observations[slot_id] = SlotObservation(
            values=tuple(values),
            evidence_refs=tuple(sorted(evidence_refs)),
        )
    return observations


class IdentityUncalibratedCalibrator:
    """Versioned interface that preserves raw score without calibration claims."""

    interface_version = CALIBRATOR_INTERFACE_VERSION
    empirically_calibrated = False

    def transform(self, raw_score: float) -> float:
        if isinstance(raw_score, bool) or not isinstance(raw_score, (int, float)):
            raise SelectiveRoutingError("invalid_raw_score")
        value = float(raw_score)
        if not 0 <= value <= 1:
            raise SelectiveRoutingError("invalid_raw_score")
        return value


class RequiredSlotResolver:
    """Derive required slots and validate explicit state against a domain pack."""

    def __init__(self, domain_pack: LoadedDomainPack) -> None:
        self.domain_pack = domain_pack
        self._slots = tuple(domain_pack.artifact_items("slots"))
        self._rules = tuple(domain_pack.artifact_items("validation_rules"))
        self._evidence = {
            str(item["evidence_id"]): item
            for item in domain_pack.artifact_items("value_evidence")
        }
        self._slot_map = {str(item["slot_id"]): item for item in self._slots}

    def resolve(
        self,
        *,
        intent: str,
        candidates: Sequence[CandidateEvidence],
        observations: Mapping[str, SlotObservation],
    ) -> SlotResolution:
        if intent not in {"sql", "rag", "cross_source", "unknown"}:
            raise SelectiveRoutingError("invalid_intent")
        _assert_no_answer_fields(observations)
        unknown_slots = set(observations) - set(self._slot_map)
        if unknown_slots:
            raise SelectiveRoutingError("unknown_slot_observation")
        self._validate_candidates(candidates)
        self._validate_observations(observations)

        assessments: dict[str, SlotAssessment] = {}
        for slot in sorted(self._slots, key=lambda item: str(item["slot_id"])):
            slot_id = str(slot["slot_id"])
            observation = observations.get(slot_id, SlotObservation((), ()))
            required, requirement_refs, requirement_reasons = self._required_state(
                slot, intent, observations, candidates
            )
            status, value_reasons = self._value_state(
                slot, observation, required
            )
            refs = {
                slot_id,
                *slot["source_refs"],
                *observation.evidence_refs,
                *requirement_refs,
            }
            assessments[slot_id] = SlotAssessment(
                slot_id=slot_id,
                required=required,
                status=status,
                value_count=len(_deduplicate(observation.values)),
                evidence_refs=tuple(sorted(refs)),
                reason_codes=tuple(sorted({*requirement_reasons, *value_reasons})),
            )

        self._apply_declared_conflicts(assessments, observations)
        hard, soft = self._apply_validation_rules(assessments, observations)
        return SlotResolution(
            assessments=tuple(assessments[key] for key in sorted(assessments)),
            hard_rule_violations=tuple(sorted(hard)),
            soft_rule_violations=tuple(sorted(soft)),
        )

    def _validate_candidates(self, candidates: Sequence[CandidateEvidence]) -> None:
        seen: set[str] = set()
        for candidate in candidates:
            if candidate.candidate_id in seen:
                raise SelectiveRoutingError("duplicate_candidate_id")
            seen.add(candidate.candidate_id)
            try:
                self.domain_pack.resolve(candidate.candidate_id)
                for reference in candidate.evidence_refs:
                    self.domain_pack.resolve(reference)
            except DomainPackError as exc:
                raise SelectiveRoutingError("candidate_evidence_unknown") from exc

    def _validate_observations(
        self, observations: Mapping[str, SlotObservation]
    ) -> None:
        for slot_id, observation in observations.items():
            slot = self._slot_map[slot_id]
            allowed_refs = {slot_id, *slot["allowed_value_evidence_refs"]}
            for reference in observation.evidence_refs:
                try:
                    role = self.domain_pack.resolve_role(reference)
                except DomainPackError as exc:
                    raise SelectiveRoutingError("slot_evidence_unknown") from exc
                if reference not in allowed_refs or role not in {"slots", "value_evidence"}:
                    raise SelectiveRoutingError("slot_evidence_not_allowed")

    def _required_state(
        self,
        slot: Mapping[str, Any],
        intent: str,
        observations: Mapping[str, SlotObservation],
        candidates: Sequence[CandidateEvidence],
    ) -> tuple[bool | None, set[str], set[str]]:
        refs = set(str(ref) for ref in slot["source_refs"])
        reasons: set[str] = set()
        if intent != "sql":
            reasons.add("intent_not_applicable")
            return False, refs, reasons
        if slot["required_by_default"]:
            reasons.add("required_by_default")
            return True, refs, reasons

        results: list[bool | None] = []
        for condition in slot["required_when"]:
            condition_id = str(condition["condition_id"])
            refs.add(condition_id)
            result, condition_refs = self._evaluate_condition(
                condition, observations, candidates
            )
            refs.update(condition_refs)
            results.append(result)
        if any(result is True for result in results):
            reasons.add("required_by_condition")
            return True, refs, reasons
        if any(result is None for result in results):
            reasons.add("requirement_indeterminate")
            return None, refs, reasons
        reasons.add("not_required")
        return False, refs, reasons

    def _evaluate_condition(
        self,
        condition: Mapping[str, Any],
        observations: Mapping[str, SlotObservation],
        candidates: Sequence[CandidateEvidence],
    ) -> tuple[bool | None, set[str]]:
        operator = str(condition["operator"])
        if operator not in SUPPORTED_REQUIRED_WHEN_OPERATORS:
            raise SelectiveRoutingError("unsupported_required_when_operator")
        subject_ref = str(condition["subject_ref"])
        try:
            role = self.domain_pack.resolve_role(subject_ref)
        except DomainPackError as exc:
            raise SelectiveRoutingError("required_when_dangling_reference") from exc
        refs = {subject_ref}

        if role == "slots":
            observation = observations.get(subject_ref, SlotObservation((), ()))
            values = _deduplicate(observation.values)
            refs.update(observation.evidence_refs)
            if operator == "present":
                return bool(values), refs
            if not values or len(values) != 1:
                return None, refs
            current = values[0]
        elif role in {"concepts", "schema_catalog"}:
            matching = [item for item in candidates if item.candidate_id == subject_ref]
            if not matching:
                return (False if operator == "present" else None), refs
            refs.update(matching[0].evidence_refs)
            if operator == "present":
                return True, refs
            current = subject_ref
        else:
            raise SelectiveRoutingError("unsupported_required_when_subject")

        condition_values = tuple(condition["values"])
        if operator == "equals":
            if len(condition_values) != 1:
                raise SelectiveRoutingError("invalid_required_when_values")
            return _equal(current, condition_values[0]), refs
        if operator == "in":
            if not condition_values:
                raise SelectiveRoutingError("invalid_required_when_values")
            return any(_equal(current, value) for value in condition_values), refs
        raise SelectiveRoutingError("unsupported_required_when_operator")

    def _value_state(
        self,
        slot: Mapping[str, Any],
        observation: SlotObservation,
        required: bool | None,
    ) -> tuple[str, set[str]]:
        values = _deduplicate(observation.values)
        if not values:
            if required is None:
                return "unknown", {"requirement_indeterminate"}
            return "missing", {"value_missing"}
        if len(values) > 1:
            return "conflicting", {"multiple_values"}
        value = values[0]
        if not self._value_type_matches(str(slot["value_type"]), value):
            return "unknown", {"invalid_value_type"}

        allowed = tuple(str(ref) for ref in slot["allowed_value_evidence_refs"])
        if allowed:
            supplied = set(observation.evidence_refs) & set(allowed)
            if not supplied:
                return "unknown", {"value_evidence_missing"}
            if not any(self._matches_evidence(value, self._evidence[ref]) for ref in supplied):
                return "unknown", {"value_outside_evidence"}
        return "filled", {"value_filled"}

    @staticmethod
    def _value_type_matches(value_type: str, value: Any) -> bool:
        if value_type in {"string", "date", "datetime", "enum"}:
            return isinstance(value, str) and bool(value.strip())
        if value_type == "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        if value_type == "number":
            return isinstance(value, (int, float)) and not isinstance(value, bool)
        if value_type == "boolean":
            return isinstance(value, bool)
        raise SelectiveRoutingError("unsupported_slot_value_type")

    @staticmethod
    def _matches_evidence(value: Any, evidence: Mapping[str, Any]) -> bool:
        evidence_type = str(evidence["evidence_type"])
        payload = evidence["payload"]
        if evidence_type in {"enum", "dictionary"}:
            values = payload.get("values")
            if not isinstance(values, (list, tuple)):
                raise SelectiveRoutingError("invalid_value_evidence")
            return any(_equal(value, candidate) for candidate in values)
        if evidence_type == "range":
            numeric: float
            try:
                numeric = float(value)
            except (TypeError, ValueError) as exc:
                raise SelectiveRoutingError("invalid_range_value") from exc
            minimum = payload.get("minimum")
            maximum = payload.get("maximum")
            if minimum is None and maximum is None:
                raise SelectiveRoutingError("invalid_value_evidence")
            return (minimum is None or numeric >= float(minimum)) and (
                maximum is None or numeric <= float(maximum)
            )
        if evidence_type == "regex":
            pattern = payload.get("pattern")
            if not isinstance(pattern, str) or not isinstance(value, str):
                raise SelectiveRoutingError("invalid_value_evidence")
            try:
                return re.fullmatch(pattern, value) is not None
            except re.error as exc:
                raise SelectiveRoutingError("invalid_value_evidence") from exc
        raise SelectiveRoutingError("unsupported_value_evidence_type")

    def _apply_declared_conflicts(
        self,
        assessments: dict[str, SlotAssessment],
        observations: Mapping[str, SlotObservation],
    ) -> None:
        for slot in self._slots:
            slot_id = str(slot["slot_id"])
            if not _deduplicate(observations.get(slot_id, SlotObservation((), ())).values):
                continue
            for conflict_id in slot["conflict_slot_ids"]:
                other = observations.get(str(conflict_id), SlotObservation((), ()))
                if _deduplicate(other.values):
                    current = assessments[slot_id]
                    assessments[slot_id] = replace(
                        current,
                        status="conflicting",
                        evidence_refs=tuple(sorted({*current.evidence_refs, str(conflict_id)})),
                        reason_codes=tuple(sorted({*current.reason_codes, "conflict_slot_present"})),
                    )

    def _apply_validation_rules(
        self,
        assessments: dict[str, SlotAssessment],
        observations: Mapping[str, SlotObservation],
    ) -> tuple[set[str], set[str]]:
        hard: set[str] = set()
        soft: set[str] = set()
        for rule in self._rules:
            operator = str(rule["operator"])
            if operator not in SUPPORTED_VALIDATION_OPERATORS:
                raise SelectiveRoutingError("unsupported_validation_operator")
            input_refs = tuple(str(ref) for ref in rule["input_refs"])
            if any(ref not in self._slot_map for ref in input_refs):
                raise SelectiveRoutingError("unsupported_validation_rule_input")
            violated = False
            affected: tuple[str, ...] = input_refs
            if operator == "in":
                evidence_refs = tuple(str(ref) for ref in rule["evidence_refs"])
                if not evidence_refs or any(ref not in self._evidence for ref in evidence_refs):
                    raise SelectiveRoutingError("validation_rule_evidence_invalid")
                for slot_id in input_refs:
                    values = _deduplicate(
                        observations.get(slot_id, SlotObservation((), ())).values
                    )
                    if values and not all(
                        any(
                            self._matches_evidence(value, self._evidence[evidence_ref])
                            for evidence_ref in evidence_refs
                        )
                        for value in values
                    ):
                        violated = True
                        break
            elif operator == "conflicts":
                if len(input_refs) < 2:
                    raise SelectiveRoutingError("validation_rule_inputs_invalid")
                first_present = bool(
                    _deduplicate(
                        observations.get(input_refs[0], SlotObservation((), ())).values
                    )
                )
                dependencies_present = all(
                    bool(
                        _deduplicate(
                            observations.get(ref, SlotObservation((), ())).values
                        )
                    )
                    for ref in input_refs[1:]
                )
                violated = first_present and not dependencies_present
                affected = (input_refs[0],)

            if not violated:
                continue
            rule_id = str(rule["rule_id"])
            target = hard if rule["rule_kind"] == "hard" else soft
            target.add(rule_id)
            for slot_id in affected:
                current = assessments[slot_id]
                status = "unknown" if rule["rule_kind"] == "hard" else "conflicting"
                assessments[slot_id] = replace(
                    current,
                    status=status,
                    evidence_refs=tuple(
                        sorted(
                            {
                                *current.evidence_refs,
                                rule_id,
                                *rule["evidence_refs"],
                                *rule["source_refs"],
                            }
                        )
                    ),
                    reason_codes=tuple(
                        sorted({*current.reason_codes, str(rule["reason_code"])})
                    ),
                )
        return hard, soft


class SelectiveDecisionPolicy:
    """Strict v1 policy over slot status and uncalibrated candidate score."""

    def __init__(
        self,
        domain_pack: LoadedDomainPack,
        *,
        policy_path: Path | str = DEFAULT_POLICY_PATH,
        schema_root: Path | str = DEFAULT_D4_04_SCHEMA_ROOT,
        d4_schema_root: Path | str = DEFAULT_SCHEMA_ROOT,
    ) -> None:
        self.domain_pack = domain_pack
        self.policy_path = Path(policy_path)
        self.schema_root = Path(schema_root)
        self.d4_schema_root = Path(d4_schema_root)
        self.policy = _read_json(self.policy_path, "invalid_selective_policy")
        self._validate_policy()
        self.policy_sha256 = canonical_text_sha256(self.policy_path)
        self.calibrator = IdentityUncalibratedCalibrator()
        self.assembler = EvidenceFeatureAssembler(
            domain_pack, schema_root=self.d4_schema_root
        )
        self._result_validator = self._build_result_validator()

    @property
    def thresholds(self) -> Mapping[str, float]:
        return self.policy["thresholds"]

    def _validate_policy(self) -> None:
        schema = _read_json(
            self.schema_root / POLICY_SCHEMA_NAME,
            "invalid_selective_policy_schema",
        )
        try:
            Draft202012Validator.check_schema(schema)
            Draft202012Validator(
                schema, format_checker=FormatChecker()
            ).validate(self.policy)
        except (SchemaError, ValidationError) as exc:
            raise SelectiveRoutingError("invalid_selective_policy") from exc
        if frozenset(self.policy["allowed_actions"]) != _ALLOWED_ACTIONS:
            raise SelectiveRoutingError("invalid_policy_actions")
        if tuple(self.policy["action_priority"]) != _EXPECTED_PRIORITY:
            raise SelectiveRoutingError("invalid_action_priority")
        thresholds = self.policy["thresholds"]
        if not (
            0
            <= thresholds["abstain_below"]
            < thresholds["clarify_min"]
            < thresholds["accept_min"]
            <= 1
        ):
            raise SelectiveRoutingError("invalid_threshold_order")

    def _build_result_validator(self) -> Draft202012Validator:
        result_schema = _read_json(
            self.schema_root / RESULT_SCHEMA_NAME,
            "invalid_selective_result_schema",
        )
        decision_schema = _read_json(
            self.d4_schema_root / "typed-decision.v1.schema.json",
            "invalid_selective_result_schema",
        )
        trace_schema = _read_json(
            self.d4_schema_root / "typed-decision-trace-event.v1.schema.json",
            "invalid_selective_result_schema",
        )
        try:
            for schema in (result_schema, decision_schema, trace_schema):
                Draft202012Validator.check_schema(schema)
            registry = Registry()
            for schema in (decision_schema, trace_schema):
                registry = registry.with_resource(
                    schema["$id"], Resource.from_contents(schema)
                )
            return Draft202012Validator(
                result_schema,
                registry=registry,
                format_checker=FormatChecker(),
            )
        except SchemaError as exc:
            raise SelectiveRoutingError("invalid_selective_result_schema") from exc

    def decide(
        self,
        *,
        intent: str,
        candidates: Sequence[CandidateEvidence],
        resolution: SlotResolution,
        request_id: str,
        recorded_at: str,
        budget_snapshot: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        candidates = tuple(candidates)
        self._validate_candidate_order(candidates)
        raw_score = (
            self.calibrator.transform(candidates[0].raw_score)
            if candidates
            else None
        )
        action, reason_code = self._choose_action(
            intent=intent,
            candidates=candidates,
            resolution=resolution,
            raw_score=raw_score,
        )
        if action == "clarify" and raw_score is None:
            # The frozen score decision requires a number.  Zero is the
            # deterministic raw-score representation of an empty candidate set;
            # it is not a calibrated probability.
            raw_score = 0.0
        decision = self._assemble_decision(
            action=action,
            reason_code=reason_code,
            candidates=candidates,
            resolution=resolution,
            raw_score=raw_score,
        )
        trace = self.assembler.assemble_trace_event(
            request_id=request_id,
            recorded_at=recorded_at,
            decision=decision,
            thresholds={
                "accept_min": self.thresholds["accept_min"],
                "clarify_min": self.thresholds["clarify_min"],
                "abstain_below": self.thresholds["abstain_below"],
                "calibration_version": None,
            },
            executed_action=action,
            selective_policy_version=POLICY_COMPONENT_VERSION,
            budget_snapshot=dict(
                budget_snapshot
                or {
                    "model_calls": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "estimated_cost_cny": 0,
                    "elapsed_ms": 0,
                }
            ),
        )
        result = {
            "result_version": RESULT_VERSION,
            "mode": MODE,
            "required_slots_source": REQUIRED_SLOTS_SOURCE,
            "intent": intent,
            "policy_ref": {
                "policy_id": self.policy["policy_id"],
                "version": self.policy["version"],
                "sha256": self.policy_sha256,
            },
            "raw_score": raw_score,
            "slot_assessments": [
                item.to_dict() for item in resolution.assessments
            ],
            "action": action,
            "reason_code": reason_code,
            "typed_decision": decision,
            "trace_event": trace,
            "requires_d4_02_safety_gate": True,
            "sql_execution_authorized": False,
        }
        self.validate_result(result)
        return result

    def _validate_candidate_order(
        self, candidates: Sequence[CandidateEvidence]
    ) -> None:
        expected = sorted(
            candidates, key=lambda item: (-item.raw_score, item.candidate_id)
        )
        if list(candidates) != expected:
            raise SelectiveRoutingError("candidate_order_unstable")
        ids = [item.candidate_id for item in candidates]
        if len(ids) != len(set(ids)):
            raise SelectiveRoutingError("duplicate_candidate_id")

    def _choose_action(
        self,
        *,
        intent: str,
        candidates: Sequence[CandidateEvidence],
        resolution: SlotResolution,
        raw_score: float | None,
    ) -> tuple[str, str]:
        supported_intents = set(self.policy["supported_intents"])
        if intent == "unknown":
            return "reject", "unknown_intent"
        if intent not in {"sql", "rag", "cross_source"}:
            return "reject", "policy_restriction"
        if intent not in supported_intents:
            return "escalate", "unsupported_intent"

        assessments = resolution.assessments
        explicit_conflicting = [
            item
            for item in assessments
            if item.status == "conflicting"
            and set(item.reason_codes) & {"multiple_values", "conflict_slot_present"}
        ]
        if explicit_conflicting:
            return "clarify", "slot_conflicting"
        if resolution.hard_rule_violations:
            return "escalate", "hard_rule_failed"
        missing = [
            item
            for item in assessments
            if item.required is True and item.status == "missing"
        ]
        if missing:
            return "clarify", "required_slot_missing"
        unknown = [
            item
            for item in assessments
            if item.required is None
            or (item.required is True and item.status == "unknown")
        ]
        if unknown:
            return "escalate", "slot_state_unknown"
        if resolution.soft_rule_violations:
            if raw_score is not None and raw_score >= self.thresholds["clarify_min"]:
                return "clarify", "soft_rule_requires_clarification"
            return "escalate", "soft_rule_below_clarify_threshold"
        if not candidates:
            return "reject", "outside_candidate_set"
        assert raw_score is not None
        if raw_score < self.thresholds["abstain_below"]:
            return "reject", "raw_score_below_abstain"
        tied_meanings = {
            self._candidate_meaning(candidate)
            for candidate in candidates
            if candidate.raw_score == raw_score
        }
        if len(tied_meanings) > 1:
            return "escalate", "candidate_score_tie"
        if raw_score < self.thresholds["accept_min"]:
            return "escalate", "raw_score_below_accept"
        return "accept", "accepted_at_raw_score_threshold"

    def _candidate_meaning(self, candidate: CandidateEvidence) -> str:
        item = self.domain_pack.resolve(candidate.candidate_id)
        if candidate.candidate_kind == "schema_object" and item["object_type"] == "table":
            concepts = item["concept_refs"]
            if len(concepts) == 1:
                return str(concepts[0])
        return candidate.candidate_id

    def _assemble_decision(
        self,
        *,
        action: str,
        reason_code: str,
        candidates: Sequence[CandidateEvidence],
        resolution: SlotResolution,
        raw_score: float | None,
    ) -> dict[str, Any]:
        relevant = [
            item
            for item in resolution.assessments
            if item.required is True
            or item.required is None
            or item.status in {"filled", "conflicting", "unknown"}
        ]
        evidence_refs = {
            reference
            for item in relevant
            for reference in item.evidence_refs
        }
        evidence_refs.update(
            reference
            for candidate in candidates
            for reference in candidate.evidence_refs
        )
        issues = []
        if action == "clarify":
            for item in resolution.assessments:
                if item.status == "conflicting":
                    issues.append(
                        {
                            "slot_id": item.slot_id,
                            "issue": "conflicting",
                            "evidence_refs": list(item.evidence_refs),
                        }
                    )
                elif item.required is True and item.status == "missing":
                    issues.append(
                        {
                            "slot_id": item.slot_id,
                            "issue": "missing",
                            "evidence_refs": list(item.evidence_refs),
                        }
                    )

        if (not candidates and action != "clarify") or reason_code in {
            "unknown_intent",
            "unsupported_intent",
            "policy_restriction",
            "outside_candidate_set",
            "raw_score_below_abstain",
            "candidate_score_tie",
        }:
            abstain_reason = {
                "outside_candidate_set": "outside_candidate_set",
                "unknown_intent": "policy_restriction",
                "unsupported_intent": "policy_restriction",
                "policy_restriction": "policy_restriction",
                "raw_score_below_abstain": "insufficient_evidence",
                "candidate_score_tie": "conflicting_evidence",
            }.get(reason_code, "insufficient_evidence")
            return self.assembler.assemble_decision(
                decision_type="abstain",
                candidates=(),
                selected_option=None,
                raw_score=None,
                evidence_refs=sorted(evidence_refs),
                missing_or_conflicting_slots=(),
                recommended_action=action,
                abstain_reason=abstain_reason,
            )
        if action == "accept":
            return self.assembler.assemble_decision(
                decision_type="choice",
                candidates=candidates,
                selected_option=candidates[0].candidate_id,
                raw_score=raw_score,
                evidence_refs=sorted(evidence_refs),
                recommended_action=action,
            )
        return self.assembler.assemble_decision(
            decision_type="score",
            candidates=candidates,
            selected_option=None,
            raw_score=raw_score,
            evidence_refs=sorted(evidence_refs),
            missing_or_conflicting_slots=issues,
            recommended_action=action,
        )

    def validate_result(self, result: Mapping[str, Any]) -> None:
        try:
            self._result_validator.validate(result)
        except ValidationError as exc:
            raise SelectiveRoutingError("selective_result_schema_invalid") from exc
        if result["action"] != result["typed_decision"]["recommended_action"]:
            raise SelectiveRoutingError("result_action_mismatch")
        if result["action"] != result["trace_event"]["executed_action"]:
            raise SelectiveRoutingError("trace_action_mismatch")
        if (
            result["typed_decision"]["decision_type"] != "abstain"
            and result["raw_score"] != result["typed_decision"]["raw_score"]
        ):
            raise SelectiveRoutingError("result_score_mismatch")
        if result["sql_execution_authorized"] is not False:
            raise SelectiveRoutingError("sql_execution_authority_forbidden")


class D4SelectiveDecisionRuntime:
    """Offline D4-01 -> D4-03 -> D4-04 decision-only vertical slice."""

    def __init__(
        self,
        domain_pack: LoadedDomainPack,
        *,
        policy_path: Path | str = DEFAULT_POLICY_PATH,
    ) -> None:
        self.domain_pack = domain_pack
        self.retriever = CandidateRetriever(domain_pack)
        self.resolver = RequiredSlotResolver(domain_pack)
        self.policy = SelectiveDecisionPolicy(
            domain_pack, policy_path=policy_path
        )

    def evaluate(
        self,
        *,
        conversation_state: Mapping[str, Any],
        slot_observations: Mapping[str, SlotObservation],
        request_id: str,
        recorded_at: str,
        top_k: int = 5,
    ) -> dict[str, Any]:
        """Evaluate one state without using D3 externally supplied missing slots."""

        try:
            validate_conversation_state(conversation_state)
        except Exception as exc:
            raise SelectiveRoutingError("invalid_conversation_state") from exc
        intent = conversation_state["route"]["intent"]
        if intent is None:
            raise SelectiveRoutingError("intent_not_available")
        query = conversation_state["current_request"]["query"]
        candidates = self.retriever.retrieve(query, top_k=top_k)
        resolution = self.resolver.resolve(
            intent=str(intent),
            candidates=candidates,
            observations=slot_observations,
        )
        return self.policy.decide(
            intent=str(intent),
            candidates=candidates,
            resolution=resolution,
            request_id=request_id,
            recorded_at=recorded_at,
        )


__all__ = [
    "CALIBRATOR_INTERFACE_VERSION",
    "D4SelectiveDecisionRuntime",
    "DEFAULT_POLICY_PATH",
    "IdentityUncalibratedCalibrator",
    "RequiredSlotResolver",
    "SelectiveDecisionPolicy",
    "SelectiveRoutingError",
    "SlotAssessment",
    "SlotObservation",
    "SlotResolution",
    "build_slot_observations",
]
