"""Content-safe metrics for B-owned D4-07 online SQL evaluations.

The module aggregates already-sanitized case outcomes.  It deliberately has no
field for a user query, prompt, SQL text, parameters, result rows, or model raw
output, so those values cannot accidentally enter the committed report.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import re
from typing import Iterable


_CASE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,95}$")
_OUTCOMES = frozenset({"success", "refused", "error", "clarification_required"})
_SAFETY_STATES = frozenset({"approved", "rejected", "not_applicable"})


class SqlEvaluationError(ValueError):
    """Raised when a sanitized evaluation record is internally inconsistent."""


def _non_negative_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SqlEvaluationError(f"{name}_invalid")
    return value


def _non_negative_number(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SqlEvaluationError(f"{name}_invalid")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise SqlEvaluationError(f"{name}_invalid")
    return number


def _linear_percentile(values: Iterable[float], quantile: float) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        raise SqlEvaluationError("percentile_requires_values")
    if not 0 <= quantile <= 1:
        raise SqlEvaluationError("percentile_quantile_invalid")
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * weight


@dataclass(frozen=True)
class SqlEvaluationCase:
    """One sanitized, content-free online SQL evaluation outcome."""

    case_id: str
    outcome: str
    error_code: str | None
    retryable: bool
    safety_status: str
    execution_attempts: int
    execution_successes: int
    model_calls: int
    input_tokens: int
    output_tokens: int
    estimated_cost_cny: float
    service_elapsed_ms: float
    provider_latency_ms: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.case_id, str) or not _CASE_ID_RE.fullmatch(self.case_id):
            raise SqlEvaluationError("case_id_invalid")
        if self.outcome not in _OUTCOMES:
            raise SqlEvaluationError("outcome_invalid")
        if self.safety_status not in _SAFETY_STATES:
            raise SqlEvaluationError("safety_status_invalid")
        if not isinstance(self.retryable, bool):
            raise SqlEvaluationError("retryable_invalid")
        if self.outcome == "error":
            if not isinstance(self.error_code, str) or not self.error_code:
                raise SqlEvaluationError("error_code_required")
        elif self.error_code is not None:
            raise SqlEvaluationError("error_code_forbidden")
        elif self.retryable:
            raise SqlEvaluationError("retryable_forbidden")
        for name in (
            "execution_attempts",
            "execution_successes",
            "model_calls",
            "input_tokens",
            "output_tokens",
        ):
            _non_negative_int(name, getattr(self, name))
        if self.execution_successes > self.execution_attempts:
            raise SqlEvaluationError("execution_successes_exceed_attempts")
        if self.safety_status == "rejected" and self.execution_attempts != 0:
            raise SqlEvaluationError("rejected_sql_was_executed")
        if self.outcome == "error" and self.execution_attempts != 0:
            raise SqlEvaluationError("error_response_was_executed")
        if self.outcome == "clarification_required" and self.execution_attempts != 0:
            raise SqlEvaluationError("clarification_response_was_executed")
        if self.outcome == "success" and self.safety_status != "approved":
            raise SqlEvaluationError("success_requires_approved_sql")
        _non_negative_number("estimated_cost_cny", self.estimated_cost_cny)
        _non_negative_number("service_elapsed_ms", self.service_elapsed_ms)
        if self.provider_latency_ms is not None:
            _non_negative_number("provider_latency_ms", self.provider_latency_ms)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def to_dict(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "outcome": self.outcome,
            "error_code": self.error_code,
            "retryable": self.retryable,
            "safety_status": self.safety_status,
            "execution_attempts": self.execution_attempts,
            "execution_successes": self.execution_successes,
            "model_calls": self.model_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "estimated_cost_cny": round(float(self.estimated_cost_cny), 10),
            "service_elapsed_ms": round(float(self.service_elapsed_ms), 3),
            "provider_latency_ms": (
                None
                if self.provider_latency_ms is None
                else round(float(self.provider_latency_ms), 3)
            ),
        }


def summarize_sql_evaluation(
    cases: Iterable[SqlEvaluationCase],
) -> dict[str, object]:
    """Aggregate sanitized cases into deterministic B-side machine metrics."""

    ordered = sorted(tuple(cases), key=lambda item: item.case_id)
    if not ordered:
        raise SqlEvaluationError("evaluation_cases_empty")
    case_ids = [item.case_id for item in ordered]
    if len(set(case_ids)) != len(case_ids):
        raise SqlEvaluationError("evaluation_case_id_duplicate")

    outcome_counts = {outcome: 0 for outcome in sorted(_OUTCOMES)}
    error_counts: dict[str, int] = {}
    for item in ordered:
        outcome_counts[item.outcome] += 1
        if item.error_code is not None:
            error_counts[item.error_code] = error_counts.get(item.error_code, 0) + 1

    service_latencies = [item.service_elapsed_ms for item in ordered]
    provider_latencies = [
        item.provider_latency_ms
        for item in ordered
        if item.provider_latency_ms is not None
    ]
    public_cases = [item.to_dict() for item in ordered]
    summary: dict[str, object] = {
        "case_count": len(ordered),
        "cases": public_cases,
        "outcome_counts": outcome_counts,
        "error_counts": dict(sorted(error_counts.items())),
        "safety": {
            "approved_count": sum(item.safety_status == "approved" for item in ordered),
            "rejected_count": sum(item.safety_status == "rejected" for item in ordered),
            "sqlite_execution_attempts": sum(item.execution_attempts for item in ordered),
            "sqlite_execution_successes": sum(item.execution_successes for item in ordered),
            "rejected_sql_execution_attempts": sum(
                item.execution_attempts
                for item in ordered
                if item.safety_status == "rejected"
            ),
        },
        "usage": {
            "definition": "sanitized response telemetry from injected offline proposal attempts",
            "injected": True,
            "model_calls": sum(item.model_calls for item in ordered),
            "input_tokens": sum(item.input_tokens for item in ordered),
            "output_tokens": sum(item.output_tokens for item in ordered),
            "total_tokens": sum(item.total_tokens for item in ordered),
            "estimated_cost_cny": round(
                sum(float(item.estimated_cost_cny) for item in ordered), 10
            ),
        },
        "service_elapsed_ms": {
            "definition": "deterministic injected offline orchestration elapsed time",
            "sample_count": len(service_latencies),
            "p50": round(_linear_percentile(service_latencies, 0.50), 3),
            "p95": round(_linear_percentile(service_latencies, 0.95), 3),
        },
        "provider_latency_ms": {
            "definition": "sanitized injected proposal telemetry; not production performance",
            "sample_count": len(provider_latencies),
            "p50": (
                None
                if not provider_latencies
                else round(_linear_percentile(provider_latencies, 0.50), 3)
            ),
            "p95": (
                None
                if not provider_latencies
                else round(_linear_percentile(provider_latencies, 0.95), 3)
            ),
        },
    }
    encoded = json.dumps(
        summary, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    summary["stable_sha256"] = hashlib.sha256(encoded).hexdigest()
    return summary


__all__ = [
    "SqlEvaluationCase",
    "SqlEvaluationError",
    "summarize_sql_evaluation",
]
