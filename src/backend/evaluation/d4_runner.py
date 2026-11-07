"""D4-08 B-side deterministic evaluation runner.

The runner consumes *outcomes*, never questions, prompts, SQL, result rows, model
output, or hidden answers.  It verifies that all five frozen experiment groups
contain the same case IDs, computes metrics from those paired outcomes, and emits
only the aggregate object accepted by ``evaluation-runner-result.v1``.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
import random
import re
from typing import Any, Iterable, Mapping, Sequence


GROUP_ROLES = {
    "B0": "baseline",
    "B1": "baseline",
    "B2": "baseline",
    "P": "proposed",
    "P-uncalibrated": "diagnostic",
}
GROUP_IDS = tuple(GROUP_ROLES)
PROTOCOL_ID = "d4-typed-decision-experiment.v1"
RUNNER_ID = "d4-evaluation-runner.v1"
_CASE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,95}$")
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,127}$")
_CAPABILITIES = frozenset({"nl2sql", "rag", "clarification"})
_CASE_FIELDS = frozenset(
    {
        "case_id",
        "e2e_correct",
        "necessary_clarification",
        "did_clarify",
        "post_clarification_success",
        "confidence",
        "sufficient_reference_correct",
        "slot_counts",
        "schema_target_rank",
        "latency_ms",
        "model_calls",
        "input_tokens",
        "output_tokens",
        "cost_cny",
        "contract_valid",
        "evidence_reference_valid",
        "trace_complete",
        "abstained",
        "abstain_was_correct",
        "entity_counts",
        "first_question_key_slot_hit",
        "clarification_rounds",
    }
)
_TOP_LEVEL_FIELDS = frozenset(
    {
        "run_id",
        "data_version",
        "groups",
        "exposure",
        "hidden_test",
        "migration",
        "evidence_reproducible",
    }
)


class D4EvaluationError(ValueError):
    """Raised when evaluation input is unsafe, incomplete, or inconsistent."""


def _strict_keys(value: Mapping[str, Any], allowed: frozenset[str], code: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise D4EvaluationError(f"{code}:{','.join(unknown)}")


def _boolean(name: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise D4EvaluationError(f"{name}_invalid")
    return value


def _non_negative_int(name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise D4EvaluationError(f"{name}_invalid")
    return value


def _finite_number(name: str, value: Any, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise D4EvaluationError(f"{name}_invalid")
    number = float(value)
    if not math.isfinite(number) or number < minimum:
        raise D4EvaluationError(f"{name}_invalid")
    return number


def _rate(numerator: int | float, denominator: int, code: str) -> float:
    if denominator <= 0:
        raise D4EvaluationError(code)
    return float(numerator) / denominator


def _linear_percentile(values: Sequence[float], quantile: float) -> float:
    if not values:
        raise D4EvaluationError("percentile_requires_values")
    ordered = sorted(float(item) for item in values)
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


@dataclass(frozen=True)
class SlotCounts:
    true_positive: int
    false_positive: int
    false_negative: int

    def __post_init__(self) -> None:
        for field in ("true_positive", "false_positive", "false_negative"):
            _non_negative_int(field, getattr(self, field))


@dataclass(frozen=True)
class EvaluationCaseOutcome:
    """Content-free outcome for one case in one experiment group."""

    case_id: str
    e2e_correct: bool
    necessary_clarification: bool
    did_clarify: bool
    post_clarification_success: bool | None
    confidence: float
    sufficient_reference_correct: bool | None
    slot_counts: Mapping[str, SlotCounts]
    schema_target_rank: int | None
    latency_ms: float
    model_calls: int
    input_tokens: int
    output_tokens: int
    cost_cny: float
    contract_valid: bool
    evidence_reference_valid: bool
    trace_complete: bool
    abstained: bool = False
    abstain_was_correct: bool | None = None
    entity_counts: SlotCounts | None = None
    first_question_key_slot_hit: bool | None = None
    clarification_rounds: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.case_id, str) or not _CASE_ID_RE.fullmatch(self.case_id):
            raise D4EvaluationError("case_id_invalid")
        for field in (
            "e2e_correct",
            "necessary_clarification",
            "did_clarify",
            "contract_valid",
            "evidence_reference_valid",
            "trace_complete",
            "abstained",
        ):
            _boolean(field, getattr(self, field))
        if self.necessary_clarification:
            if not isinstance(self.post_clarification_success, bool):
                raise D4EvaluationError("post_clarification_success_required")
            if not self.did_clarify and self.post_clarification_success:
                raise D4EvaluationError("post_clarification_success_without_clarification")
            if self.sufficient_reference_correct is not None:
                raise D4EvaluationError("sufficient_reference_correct_forbidden")
        else:
            if self.post_clarification_success is not None:
                raise D4EvaluationError("post_clarification_success_forbidden")
            if not isinstance(self.sufficient_reference_correct, bool):
                raise D4EvaluationError("sufficient_reference_correct_required")
        if self.abstained != isinstance(self.abstain_was_correct, bool):
            raise D4EvaluationError("abstain_correctness_inconsistent")
        confidence = _finite_number("confidence", self.confidence)
        if confidence > 1.0:
            raise D4EvaluationError("confidence_invalid")
        _finite_number("latency_ms", self.latency_ms)
        _finite_number("cost_cny", self.cost_cny)
        for field in ("model_calls", "input_tokens", "output_tokens"):
            _non_negative_int(field, getattr(self, field))
        if self.schema_target_rank is not None:
            _non_negative_int("schema_target_rank", self.schema_target_rank)
        if not isinstance(self.slot_counts, Mapping) or not self.slot_counts:
            raise D4EvaluationError("slot_counts_required")
        for slot, counts in self.slot_counts.items():
            if not isinstance(slot, str) or not re.fullmatch(r"[a-z][a-z0-9_.-]{1,63}", slot):
                raise D4EvaluationError("slot_name_invalid")
            if not isinstance(counts, SlotCounts):
                raise D4EvaluationError("slot_counts_invalid")
        if self.entity_counts is not None and not isinstance(self.entity_counts, SlotCounts):
            raise D4EvaluationError("entity_counts_invalid")
        if self.first_question_key_slot_hit is not None:
            _boolean("first_question_key_slot_hit", self.first_question_key_slot_hit)
            if not self.necessary_clarification:
                raise D4EvaluationError("first_question_key_slot_hit_forbidden")
        if self.clarification_rounds is not None:
            _non_negative_int("clarification_rounds", self.clarification_rounds)
            if not self.did_clarify and self.clarification_rounds:
                raise D4EvaluationError("clarification_rounds_without_clarification")

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True)
class EvaluationRunMetadata:
    run_id: str
    data_version: str
    validation_historical_count: int
    validation_current_task_count: int
    hidden_capability: str
    hidden_case_count: int
    hidden_content_committed: bool
    hidden_keeper: str
    domain_count: int
    new_domain_prompt_or_code_changed: bool
    new_domain_test_answer_written: bool
    business_code_changed_lines: int
    evidence_reproducible: bool
    new_domain_setup_started_ms: int | None = None
    new_domain_setup_finished_ms: int | None = None
    domain_pack_entry_ids: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not _RUN_ID_RE.fullmatch(self.run_id):
            raise D4EvaluationError("run_id_invalid")
        if not isinstance(self.data_version, str) or not self.data_version.strip():
            raise D4EvaluationError("data_version_invalid")
        for field in (
            "validation_historical_count",
            "validation_current_task_count",
            "hidden_case_count",
            "domain_count",
            "business_code_changed_lines",
        ):
            _non_negative_int(field, getattr(self, field))
        if self.hidden_capability not in _CAPABILITIES:
            raise D4EvaluationError("hidden_capability_invalid")
        if not isinstance(self.hidden_keeper, str) or not self.hidden_keeper.strip():
            raise D4EvaluationError("hidden_keeper_invalid")
        for field in (
            "hidden_content_committed",
            "new_domain_prompt_or_code_changed",
            "new_domain_test_answer_written",
            "evidence_reproducible",
        ):
            _boolean(field, getattr(self, field))
        if (self.new_domain_setup_started_ms is None) != (self.new_domain_setup_finished_ms is None):
            raise D4EvaluationError("new_domain_setup_interval_incomplete")
        if self.new_domain_setup_started_ms is not None:
            _non_negative_int("new_domain_setup_started_ms", self.new_domain_setup_started_ms)
            _non_negative_int("new_domain_setup_finished_ms", self.new_domain_setup_finished_ms)
            if self.new_domain_setup_finished_ms < self.new_domain_setup_started_ms:
                raise D4EvaluationError("new_domain_setup_interval_invalid")
        if self.domain_pack_entry_ids is not None:
            if not isinstance(self.domain_pack_entry_ids, tuple):
                raise D4EvaluationError("domain_pack_entry_ids_invalid")
            if any(not isinstance(item, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{1,127}", item) for item in self.domain_pack_entry_ids):
                raise D4EvaluationError("domain_pack_entry_ids_invalid")
            if len(set(self.domain_pack_entry_ids)) != len(self.domain_pack_entry_ids):
                raise D4EvaluationError("domain_pack_entry_ids_invalid")


def _slot_macro_f1(cases: Sequence[EvaluationCaseOutcome]) -> float:
    totals: dict[str, list[int]] = {}
    for case in cases:
        for slot, counts in case.slot_counts.items():
            current = totals.setdefault(slot, [0, 0, 0])
            current[0] += counts.true_positive
            current[1] += counts.false_positive
            current[2] += counts.false_negative
    scores: list[float] = []
    for true_positive, false_positive, false_negative in totals.values():
        denominator = (2 * true_positive) + false_positive + false_negative
        scores.append(1.0 if denominator == 0 else (2 * true_positive) / denominator)
    return sum(scores) / len(scores)


def _ece(cases: Sequence[EvaluationCaseOutcome], bins: int = 10) -> float:
    total = len(cases)
    error = 0.0
    for index in range(bins):
        lower = index / bins
        upper = (index + 1) / bins
        members = [
            case
            for case in cases
            if case.confidence >= lower
            and (case.confidence < upper or (index == bins - 1 and case.confidence <= 1.0))
        ]
        if members:
            accuracy = sum(case.e2e_correct for case in members) / len(members)
            average_confidence = sum(case.confidence for case in members) / len(members)
            error += (len(members) / total) * abs(accuracy - average_confidence)
    return error


def _f1(counts: SlotCounts) -> float:
    denominator = 2 * counts.true_positive + counts.false_positive + counts.false_negative
    return 1.0 if denominator == 0 else 2 * counts.true_positive / denominator


def _reliability_diagram(cases: Sequence[EvaluationCaseOutcome]) -> list[dict[str, Any]]:
    points = []
    for index in range(10):
        members = [
            case for case in cases
            if index / 10 <= case.confidence < (index + 1) / 10
            or (index == 9 and case.confidence == 1.0)
        ]
        points.append({
            "bin": index,
            "count": len(members),
            "mean_confidence": round(sum(case.confidence for case in members) / len(members), 10) if members else None,
            "accuracy": round(sum(case.e2e_correct for case in members) / len(members), 10) if members else None,
        })
    return points


def _coverage_risk(cases: Sequence[EvaluationCaseOutcome]) -> list[dict[str, Any]]:
    points = []
    for threshold in sorted({0.0, *(case.confidence for case in cases)}):
        covered = [case for case in cases if not case.abstained and case.confidence >= threshold]
        points.append({
            "threshold": threshold,
            "coverage": round(len(covered) / len(cases), 10),
            "risk": round(sum(not case.e2e_correct for case in covered) / len(covered), 10) if covered else None,
        })
    return points


def _group_metrics(cases: Sequence[EvaluationCaseOutcome]) -> dict[str, Any]:
    total = len(cases)
    necessary = [case for case in cases if case.necessary_clarification]
    sufficient = [case for case in cases if not case.necessary_clarification]
    schema_cases = [case for case in cases if case.schema_target_rank is not None]
    high_confidence = [case for case in cases if case.confidence >= 0.8]
    abstained = [case for case in cases if case.abstained]
    reference_accuracy = _rate(
        sum(bool(case.sufficient_reference_correct) for case in sufficient),
        len(sufficient),
        "sufficient_cases_required",
    )
    sufficient_accuracy = _rate(
        sum(case.e2e_correct for case in sufficient),
        len(sufficient),
        "sufficient_cases_required",
    )
    slot_totals = SlotCounts(
        sum(count.true_positive for case in cases for count in case.slot_counts.values()),
        sum(count.false_positive for case in cases for count in case.slot_counts.values()),
        sum(count.false_negative for case in cases for count in case.slot_counts.values()),
    )
    metrics: dict[str, Any] = {
        "post_clarification_end_to_end_execution_accuracy": _rate(
            sum(case.e2e_correct for case in cases), total, "evaluation_cases_empty"
        ),
        "necessary_clarification_recall": _rate(
            sum(case.did_clarify for case in necessary),
            len(necessary),
            "necessary_clarification_cases_required",
        ),
        "over_clarification_rate": _rate(
            sum(case.did_clarify for case in sufficient),
            len(sufficient),
            "sufficient_cases_required",
        ),
        "slot_macro_f1": _slot_macro_f1(cases),
        "slot_micro_f1": _f1(slot_totals),
        "post_clarification_success_rate": _rate(
            sum(bool(case.post_clarification_success) for case in necessary),
            len(necessary),
            "necessary_clarification_cases_required",
        ),
        "high_confidence_error_rate": (
            0.0
            if not high_confidence
            else sum(not case.e2e_correct for case in high_confidence) / len(high_confidence)
        ),
        "sufficient_question_e2e_accuracy_drop_pp": max(
            0.0, (reference_accuracy - sufficient_accuracy) * 100.0
        ),
        "end_to_end_p50_ms": _linear_percentile(
            [case.latency_ms for case in cases], 0.50
        ),
        "end_to_end_p95_ms": _linear_percentile(
            [case.latency_ms for case in cases], 0.95
        ),
        "cost_cny": sum(case.cost_cny for case in cases),
        "tokens": sum(case.total_tokens for case in cases),
        "model_calls": sum(case.model_calls for case in cases),
        "brier_score": sum(
            (case.confidence - float(case.e2e_correct)) ** 2 for case in cases
        )
        / total,
        "ece": _ece(cases),
        "reliability_diagram": _reliability_diagram(cases),
        "coverage_risk": _coverage_risk(cases),
        "contract_valid_rate": sum(case.contract_valid for case in cases) / total,
        "evidence_reference_valid_rate": sum(
            case.evidence_reference_valid for case in cases
        )
        / total,
        "trace_completeness_rate": sum(case.trace_complete for case in cases) / total,
        "coverage": sum(not case.abstained for case in cases) / total,
        "abstain_precision": (
            1.0
            if not abstained
            else sum(bool(case.abstain_was_correct) for case in abstained) / len(abstained)
        ),
    }
    if schema_cases:
        for k in (1, 3, 5):
            metrics[f"schema_recall_at_{k}"] = sum(
                0 < int(case.schema_target_rank or 0) <= k for case in schema_cases
            ) / len(schema_cases)
        metrics["schema_mrr"] = sum(
            0.0 if not case.schema_target_rank else 1.0 / case.schema_target_rank
            for case in schema_cases
        ) / len(schema_cases)
    unavailable: dict[str, str] = {}
    optional = {
        "entity_f1": (
            _f1(SlotCounts(
                sum(case.entity_counts.true_positive for case in cases if case.entity_counts),
                sum(case.entity_counts.false_positive for case in cases if case.entity_counts),
                sum(case.entity_counts.false_negative for case in cases if case.entity_counts),
            )) if all(case.entity_counts is not None for case in cases) else None
        ),
        "first_question_key_slot_hit_rate": (
            sum(bool(case.first_question_key_slot_hit) for case in necessary if case.did_clarify)
            / sum(case.did_clarify for case in necessary)
            if any(case.did_clarify for case in necessary)
            and all(case.first_question_key_slot_hit is not None for case in necessary if case.did_clarify)
            else None
        ),
        "average_clarification_rounds": (
            sum(case.clarification_rounds for case in cases if case.clarification_rounds is not None) / total
            if all(case.clarification_rounds is not None for case in cases) else None
        ),
    }
    for name, value in optional.items():
        metrics[name] = value
        if value is None:
            unavailable[name] = "source_observation_missing"
    for name in ("schema_recall_at_1", "schema_recall_at_3", "schema_recall_at_5", "schema_mrr"):
        if name not in metrics:
            metrics[name] = None
            unavailable[name] = "no_schema_cases"
    metrics["end_to_end_p50"] = metrics["end_to_end_p50_ms"]
    metrics["end_to_end_p95"] = metrics["end_to_end_p95_ms"]
    metrics["metric_unavailable"] = unavailable
    return {name: round(value, 10) if isinstance(value, float) else value for name, value in metrics.items()}


def _mcnemar_exact(
    proposed: Sequence[EvaluationCaseOutcome], baseline: Sequence[EvaluationCaseOutcome]
) -> float:
    proposed_only = sum(
        p.e2e_correct and not b.e2e_correct for p, b in zip(proposed, baseline)
    )
    baseline_only = sum(
        b.e2e_correct and not p.e2e_correct for p, b in zip(proposed, baseline)
    )
    discordant = proposed_only + baseline_only
    if discordant == 0:
        return 1.0
    tail = sum(math.comb(discordant, k) for k in range(min(proposed_only, baseline_only) + 1))
    return min(1.0, 2.0 * tail / (2**discordant))


def _paired_bootstrap_ci(
    proposed: Sequence[EvaluationCaseOutcome],
    baseline: Sequence[EvaluationCaseOutcome],
    *,
    seed_material: str,
    samples: int,
) -> tuple[float, float]:
    if samples < 100:
        raise D4EvaluationError("bootstrap_samples_too_small")
    differences = [float(p.e2e_correct) - float(b.e2e_correct) for p, b in zip(proposed, baseline)]
    seed = int.from_bytes(hashlib.sha256(seed_material.encode("utf-8")).digest()[:8], "big")
    generator = random.Random(seed)
    estimates = [
        sum(differences[generator.randrange(len(differences))] for _ in differences)
        / len(differences)
        for _ in range(samples)
    ]
    return _linear_percentile(estimates, 0.025), _linear_percentile(estimates, 0.975)


def build_evaluation_result(
    groups: Mapping[str, Iterable[EvaluationCaseOutcome]],
    metadata: EvaluationRunMetadata,
    *,
    protocol_sha256: str,
    bootstrap_samples: int = 2000,
) -> dict[str, Any]:
    """Build a schema-ready aggregate from five strictly paired groups."""

    if not re.fullmatch(r"[0-9a-f]{64}", protocol_sha256):
        raise D4EvaluationError("protocol_sha256_invalid")
    if set(groups) != set(GROUP_IDS):
        raise D4EvaluationError("group_set_mismatch")
    ordered: dict[str, tuple[EvaluationCaseOutcome, ...]] = {}
    expected_ids: tuple[str, ...] | None = None
    for group_id in GROUP_IDS:
        cases = tuple(sorted(groups[group_id], key=lambda case: case.case_id))
        if not cases:
            raise D4EvaluationError(f"group_empty:{group_id}")
        case_ids = tuple(case.case_id for case in cases)
        if len(set(case_ids)) != len(case_ids):
            raise D4EvaluationError(f"duplicate_case_id:{group_id}")
        if expected_ids is None:
            expected_ids = case_ids
        elif case_ids != expected_ids:
            raise D4EvaluationError(f"paired_case_ids_mismatch:{group_id}")
        ordered[group_id] = cases

    # The scorer's labels are shared by every arm. A group-specific label or
    # sufficient-question reference would invalidate paired comparisons.
    reference = ordered[GROUP_IDS[0]]
    for group_id in GROUP_IDS[1:]:
        for base_case, case in zip(reference, ordered[group_id]):
            if case.necessary_clarification != base_case.necessary_clarification:
                raise D4EvaluationError(f"paired_clarification_label_mismatch:{group_id}")
            if case.sufficient_reference_correct != base_case.sufficient_reference_correct:
                raise D4EvaluationError(f"paired_reference_label_mismatch:{group_id}")
    if metadata.hidden_case_count > len(reference):
        raise D4EvaluationError("hidden_case_count_exceeds_input_cases")

    metrics = {group_id: _group_metrics(ordered[group_id]) for group_id in GROUP_IDS}
    for group_metrics in metrics.values():
        group_metrics["new_domain_setup_time"] = (
            (metadata.new_domain_setup_finished_ms - metadata.new_domain_setup_started_ms) / 1000
            if metadata.new_domain_setup_started_ms is not None else None
        )
        group_metrics["domain_pack_entry_count"] = (
            len(metadata.domain_pack_entry_ids) if metadata.domain_pack_entry_ids is not None else None
        )
        group_metrics["business_code_changed_lines"] = metadata.business_code_changed_lines
        for name in ("new_domain_setup_time", "domain_pack_entry_count"):
            if group_metrics[name] is None:
                group_metrics["metric_unavailable"][name] = "migration_observation_missing"
    best_baseline = max(
        ("B0", "B1", "B2"),
        key=lambda group_id: metrics[group_id][
            "post_clarification_end_to_end_execution_accuracy"
        ],
    )
    proposed = ordered["P"]
    baseline = ordered[best_baseline]
    lower, upper = _paired_bootstrap_ci(
        proposed,
        baseline,
        seed_material=f"{protocol_sha256}:{metadata.run_id}:P_vs_{best_baseline}",
        samples=bootstrap_samples,
    )
    return {
        "runner_id": RUNNER_ID,
        "run_id": metadata.run_id,
        "protocol_id": PROTOCOL_ID,
        "protocol_sha256": protocol_sha256,
        "data_version": metadata.data_version,
        "groups": {
            group_id: {
                "group_id": group_id,
                "role": GROUP_ROLES[group_id],
                "metrics": metrics[group_id],
            }
            for group_id in GROUP_IDS
        },
        "statistics": {
            "mcnemar": {
                "comparison": f"P_vs_{best_baseline}",
                "p_value": round(_mcnemar_exact(proposed, baseline), 10),
                "sample_pairs": len(proposed),
            },
            "paired_bootstrap_95ci": {
                "comparison": f"P_vs_{best_baseline}",
                "metric": "post_clarification_end_to_end_execution_accuracy",
                "lower": round(lower, 10),
                "upper": round(upper, 10),
            },
        },
        "exposure": {
            "validation_historical_count": metadata.validation_historical_count,
            "validation_current_task_count": metadata.validation_current_task_count,
        },
        "hidden_test": {
            "capability": metadata.hidden_capability,
            "case_count": metadata.hidden_case_count,
            "content_committed": metadata.hidden_content_committed,
            "keeper": metadata.hidden_keeper,
        },
        "migration": {
            "domain_count": metadata.domain_count,
            "new_domain_prompt_or_code_changed": metadata.new_domain_prompt_or_code_changed,
            "new_domain_test_answer_written": metadata.new_domain_test_answer_written,
            "business_code_changed_lines": metadata.business_code_changed_lines,
        },
        "evidence_reproducible": metadata.evidence_reproducible,
    }


def _parse_case(value: Any) -> EvaluationCaseOutcome:
    if not isinstance(value, Mapping):
        raise D4EvaluationError("case_object_required")
    _strict_keys(value, _CASE_FIELDS, "case_unknown_fields")
    missing = sorted(_CASE_FIELDS - {
        "abstained", "abstain_was_correct", "entity_counts",
        "first_question_key_slot_hit", "clarification_rounds",
    } - set(value))
    if missing:
        raise D4EvaluationError(f"case_missing_fields:{','.join(missing)}")
    raw_slots = value["slot_counts"]
    if not isinstance(raw_slots, Mapping):
        raise D4EvaluationError("slot_counts_invalid")
    slots: dict[str, SlotCounts] = {}
    for name, raw_counts in raw_slots.items():
        if not isinstance(raw_counts, Mapping):
            raise D4EvaluationError("slot_counts_invalid")
        _strict_keys(raw_counts, frozenset({"tp", "fp", "fn"}), "slot_counts_unknown_fields")
        if set(raw_counts) != {"tp", "fp", "fn"}:
            raise D4EvaluationError("slot_counts_fields_invalid")
        slots[str(name)] = SlotCounts(raw_counts["tp"], raw_counts["fp"], raw_counts["fn"])
    raw_entity = value.get("entity_counts")
    if raw_entity is not None:
        if not isinstance(raw_entity, Mapping) or set(raw_entity) != {"tp", "fp", "fn"}:
            raise D4EvaluationError("entity_counts_fields_invalid")
        entity_counts = SlotCounts(raw_entity["tp"], raw_entity["fp"], raw_entity["fn"])
    else:
        entity_counts = None
    return EvaluationCaseOutcome(
        case_id=value["case_id"],
        e2e_correct=value["e2e_correct"],
        necessary_clarification=value["necessary_clarification"],
        did_clarify=value["did_clarify"],
        post_clarification_success=value["post_clarification_success"],
        confidence=value["confidence"],
        sufficient_reference_correct=value["sufficient_reference_correct"],
        slot_counts=slots,
        schema_target_rank=value["schema_target_rank"],
        latency_ms=value["latency_ms"],
        model_calls=value["model_calls"],
        input_tokens=value["input_tokens"],
        output_tokens=value["output_tokens"],
        cost_cny=value["cost_cny"],
        contract_valid=value["contract_valid"],
        evidence_reference_valid=value["evidence_reference_valid"],
        trace_complete=value["trace_complete"],
        abstained=value.get("abstained", False),
        abstain_was_correct=value.get("abstain_was_correct"),
        entity_counts=entity_counts,
        first_question_key_slot_hit=value.get("first_question_key_slot_hit"),
        clarification_rounds=value.get("clarification_rounds"),
    )


def parse_runner_input(value: Any) -> tuple[dict[str, tuple[EvaluationCaseOutcome, ...]], EvaluationRunMetadata]:
    """Parse a strict content-free JSON object for the CLI adapter."""

    if not isinstance(value, Mapping):
        raise D4EvaluationError("runner_input_object_required")
    _strict_keys(value, _TOP_LEVEL_FIELDS, "runner_input_unknown_fields")
    missing = sorted(_TOP_LEVEL_FIELDS - set(value))
    if missing:
        raise D4EvaluationError(f"runner_input_missing_fields:{','.join(missing)}")
    raw_groups = value["groups"]
    if not isinstance(raw_groups, Mapping) or set(raw_groups) != set(GROUP_IDS):
        raise D4EvaluationError("group_set_mismatch")
    groups = {
        group_id: tuple(_parse_case(item) for item in raw_groups[group_id])
        for group_id in GROUP_IDS
    }
    exposure = value["exposure"]
    hidden = value["hidden_test"]
    migration = value["migration"]
    for name, item, fields in (
        (
            "exposure",
            exposure,
            frozenset({"validation_historical_count", "validation_current_task_count"}),
        ),
        (
            "hidden_test",
            hidden,
            frozenset({"capability", "case_count", "content_committed", "keeper"}),
        ),
        (
            "migration",
            migration,
            frozenset(
                {
                    "domain_count",
                    "new_domain_prompt_or_code_changed",
                    "new_domain_test_answer_written",
                    "business_code_changed_lines",
                    "new_domain_setup_started_ms",
                    "new_domain_setup_finished_ms",
                    "domain_pack_entry_ids",
                }
            ),
        ),
    ):
        required = fields - {"new_domain_setup_started_ms", "new_domain_setup_finished_ms", "domain_pack_entry_ids"}
        if not isinstance(item, Mapping) or not required <= set(item) or not set(item) <= fields:
            raise D4EvaluationError(f"{name}_fields_invalid")
    metadata = EvaluationRunMetadata(
        run_id=value["run_id"],
        data_version=value["data_version"],
        validation_historical_count=exposure["validation_historical_count"],
        validation_current_task_count=exposure["validation_current_task_count"],
        hidden_capability=hidden["capability"],
        hidden_case_count=hidden["case_count"],
        hidden_content_committed=hidden["content_committed"],
        hidden_keeper=hidden["keeper"],
        domain_count=migration["domain_count"],
        new_domain_prompt_or_code_changed=migration["new_domain_prompt_or_code_changed"],
        new_domain_test_answer_written=migration["new_domain_test_answer_written"],
        business_code_changed_lines=migration["business_code_changed_lines"],
        evidence_reproducible=value["evidence_reproducible"],
        new_domain_setup_started_ms=migration.get("new_domain_setup_started_ms"),
        new_domain_setup_finished_ms=migration.get("new_domain_setup_finished_ms"),
        domain_pack_entry_ids=(tuple(migration["domain_pack_entry_ids"])
                               if isinstance(migration.get("domain_pack_entry_ids"), list) else migration.get("domain_pack_entry_ids")),
    )
    return groups, metadata


__all__ = [
    "D4EvaluationError",
    "EvaluationCaseOutcome",
    "EvaluationRunMetadata",
    "GROUP_IDS",
    "SlotCounts",
    "build_evaluation_result",
    "parse_runner_input",
]
