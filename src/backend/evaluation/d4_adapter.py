"""Content-separated execution adapter for the frozen D4-08 five-arm runner.

The case source supplies only model-visible inputs. The scorer owns labels and
never supplies them to the executor. Concrete source/executor/scorer ports are
injected by the test owner; this module does not load data or call a model.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import math
from typing import Any, Mapping, Protocol

from .d4_execution import ArmControls, ExecutionReceipt
from .d4_runner import D4EvaluationError, EvaluationCaseOutcome


_INPUT_FIELDS = frozenset({
    "current_request", "candidate_options", "evidence_refs", "domain_pack_version",
})
_ANSWER_FIELDS = frozenset({
    "label", "labels", "answer", "answers", "target_answer",
    "reference_answer", "answer_sql", "ground_truth",
})


def _is_answer_field(key: str) -> bool:
    # Match the frozen D4 validator's case/separator normalization and its
    # expected/gold substring rule. Keep this runtime boundary self-contained
    # so a packaged backend does not depend on the offline validation tool.
    normalized = key.casefold().replace("-", "_").replace(" ", "_")
    return "expected" in normalized or "gold" in normalized or normalized in _ANSWER_FIELDS


def _reject_answer_fields(value: Any, depth: int = 0) -> None:
    if depth > 16:
        raise D4EvaluationError("case_input_nesting_invalid")
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise D4EvaluationError("case_input_key_invalid")
            if _is_answer_field(key):
                raise D4EvaluationError("answer_field_in_case_input")
            _reject_answer_fields(item, depth + 1)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_answer_fields(item, depth + 1)


class CaseSource(Protocol):
    candidate_catalog_sha256: str

    def load_input(self, case_id: str) -> Mapping[str, Any]:
        """Return model-visible input only; keep labels in the scorer's store."""


@dataclass(frozen=True)
class ArmObservation:
    """Actual execution output and accounting, retained only until scoring."""

    output: Any
    control_fingerprint: str
    enabled_components: tuple[str, ...]
    disabled_components: tuple[str, ...]
    output_constraint: str
    model_id: str | None
    model_calls: int
    input_tokens: int
    output_tokens: int
    cost_cny: float
    latency_ms: float

    def __post_init__(self) -> None:
        for name in ("model_calls", "input_tokens", "output_tokens"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise D4EvaluationError(f"observation_{name}_invalid")
        for name in ("cost_cny", "latency_ms"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise D4EvaluationError(f"observation_{name}_invalid")


class ArmExecutor(Protocol):
    def execute(self, case_input: Mapping[str, Any], controls: ArmControls) -> ArmObservation:
        """Run one arm and report observed controls and resource usage."""


class OutcomeScorer(Protocol):
    scorer_sha256: str

    def score(self, case_id: str, observation: ArmObservation) -> EvaluationCaseOutcome:
        """Access private labels by case ID; never return them to the executor."""


@dataclass
class ControlledEvaluationAdapter:
    """Bridge real execution/scoring ports to :func:`execute_matrix`.

    The caller must independently audit the concrete ports and their digests.
    Self-reported observations cannot prove that an external runtime truly
    disabled a component or used the declared model.
    """

    adapter_id: str
    adapter_sha256: str
    offline: bool
    source: CaseSource
    executor: ArmExecutor
    scorer: OutcomeScorer

    def run_case(self, case_id: str, controls: ArmControls) -> ExecutionReceipt:
        if self.source.candidate_catalog_sha256 != controls.candidate_catalog_sha256:
            raise D4EvaluationError("case_source_catalog_mismatch")
        if self.scorer.scorer_sha256 != controls.scorer_sha256:
            raise D4EvaluationError("scorer_identity_mismatch")
        try:
            raw_input = self.source.load_input(case_id)
        except Exception:
            raise D4EvaluationError("case_input_load_failed") from None
        if not isinstance(raw_input, Mapping):
            raise D4EvaluationError("case_input_object_required")
        if not raw_input or set(raw_input) - _INPUT_FIELDS:
            raise D4EvaluationError("case_input_fields_invalid")
        _reject_answer_fields(raw_input)
        # Each arm receives its own copy; a mutating executor cannot change the
        # inputs subsequently seen by another arm.
        case_input = deepcopy(dict(raw_input))
        try:
            observation = self.executor.execute(case_input, controls)
        except Exception:
            raise D4EvaluationError("arm_execution_failed") from None
        if not isinstance(observation, ArmObservation):
            raise D4EvaluationError("arm_observation_invalid")
        for observed, expected in (
            (observation.control_fingerprint, controls.fingerprint),
            (observation.enabled_components, controls.enabled_components),
            (observation.disabled_components, controls.disabled_components),
            (observation.output_constraint, controls.output_constraint),
            (observation.model_id, controls.model_id if observation.model_calls else None),
        ):
            if observed != expected:
                raise D4EvaluationError("arm_observed_controls_mismatch")
        try:
            outcome = self.scorer.score(case_id, observation)
        except Exception:
            raise D4EvaluationError("scorer_failed") from None
        if not isinstance(outcome, EvaluationCaseOutcome) or outcome.case_id != case_id:
            raise D4EvaluationError("scorer_outcome_invalid")
        for observed, expected in (
            (outcome.model_calls, observation.model_calls),
            (outcome.input_tokens, observation.input_tokens),
            (outcome.output_tokens, observation.output_tokens),
            (outcome.cost_cny, observation.cost_cny),
            (outcome.latency_ms, observation.latency_ms),
        ):
            if observed != expected:
                raise D4EvaluationError("scorer_resource_mismatch")
        return ExecutionReceipt(
            outcome=outcome,
            control_fingerprint=observation.control_fingerprint,
            candidate_catalog_sha256=self.source.candidate_catalog_sha256,
            scorer_sha256=self.scorer.scorer_sha256,
            enabled_components=observation.enabled_components,
            disabled_components=observation.disabled_components,
            output_constraint=observation.output_constraint,
            model_id=observation.model_id,
        )


__all__ = [
    "ArmExecutor", "ArmObservation", "CaseSource", "ControlledEvaluationAdapter",
    "OutcomeScorer",
]
