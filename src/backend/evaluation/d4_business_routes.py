"""Concrete five-arm business orchestration for the D4-08 evaluator.

The component implementations are injected so A/C can review and freeze their
identities independently.  This module defines the real arm ordering and makes
every enabled component pass through :class:`ArmContext`; it never loads case
answers, credentials, prompts or provider configuration.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from ..llm.contracts import ModelCallRequest
from .d4_adapter import _INPUT_FIELDS, _reject_answer_fields
from .d4_arm_wiring import ArmContext, ArmResult, ArmRoute
from .d4_runner import D4EvaluationError


MappingOperation = Callable[[Mapping[str, Any]], Mapping[str, Any]]
PromptOperation = Callable[[Mapping[str, Any], str], ModelCallRequest]


@dataclass(frozen=True)
class BusinessComponents:
    """Reviewed component ports shared by all five experiment arms."""

    fixed_prompt: PromptOperation
    typed_decision_contract: MappingOperation
    domain_pack: MappingOperation
    deterministic_rules: MappingOperation
    value_evidence: MappingOperation
    evidence_fusion: MappingOperation
    empirical_calibration: MappingOperation
    selective_routing: MappingOperation
    fixed_rule_threshold: MappingOperation

    def __post_init__(self) -> None:
        if any(not callable(value) for value in self.__dict__.values()):
            raise D4EvaluationError("business_component_invalid")
        for value in self.__dict__.values():
            if not (
                isinstance(getattr(value, "component_id", None), str)
                and value.component_id.strip()
                and isinstance(getattr(value, "component_version", None), str)
                and value.component_version.strip()
            ):
                raise D4EvaluationError("business_component_identity_missing")
        calibrator = self.empirical_calibration
        if not (
            getattr(calibrator, "empirically_calibrated", False) is True
            and isinstance(getattr(calibrator, "calibration_version", None), str)
            and calibrator.calibration_version.strip()
        ):
            raise D4EvaluationError("empirical_calibration_missing")


class FiveArmBusinessRoutes:
    """Five distinct routes matching ``experiment-protocol.v1`` exactly."""

    def __init__(self, components: BusinessComponents) -> None:
        if not isinstance(components, BusinessComponents):
            raise D4EvaluationError("business_components_invalid")
        self.components = components

    @staticmethod
    def _mapping(value: Any) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise D4EvaluationError("business_component_output_invalid")
        return dict(value)

    def _apply(
        self,
        context: ArmContext,
        component: str,
        operation: MappingOperation,
        state: Mapping[str, Any],
    ) -> dict[str, Any]:
        return self._mapping(context.apply(component, operation, state))

    def _model(
        self,
        case_input: Mapping[str, Any],
        context: ArmContext,
    ) -> dict[str, Any]:
        if not case_input or set(case_input) - _INPUT_FIELDS:
            raise D4EvaluationError("business_model_input_fields_invalid")
        _reject_answer_fields(case_input)
        request = context.apply(
            "fixed_prompt",
            self.components.fixed_prompt,
            case_input,
            context.controls.group_id,
        )
        if not isinstance(request, ModelCallRequest):
            raise D4EvaluationError("business_model_request_invalid")
        return self._mapping(context.model_call(request).parsed_output)

    def b0(self, case_input: Mapping[str, Any], context: ArmContext) -> ArmResult:
        output = self._model(case_input, context)
        return ArmResult(output, context.controls.output_constraint)

    def b1(self, case_input: Mapping[str, Any], context: ArmContext) -> ArmResult:
        output = self._model(case_input, context)
        output = self._apply(
            context, "typed_decision_contract", self.components.typed_decision_contract, output
        )
        return ArmResult(output, context.controls.output_constraint)

    def b2(self, case_input: Mapping[str, Any], context: ArmContext) -> ArmResult:
        state = self._apply(context, "domain_pack", self.components.domain_pack, case_input)
        state = self._apply(
            context, "deterministic_rules", self.components.deterministic_rules, state
        )
        state = self._apply(context, "value_evidence", self.components.value_evidence, state)
        state = self._apply(context, "evidence_fusion", self.components.evidence_fusion, state)
        state = self._apply(
            context, "selective_routing", self.components.selective_routing, state
        )
        state = self._apply(
            context, "fixed_rule_threshold", self.components.fixed_rule_threshold, state
        )
        return ArmResult(state, context.controls.output_constraint)

    def p(self, case_input: Mapping[str, Any], context: ArmContext) -> ArmResult:
        state = self._apply(context, "domain_pack", self.components.domain_pack, case_input)
        state = self._apply(
            context, "deterministic_rules", self.components.deterministic_rules, state
        )
        state = self._apply(context, "value_evidence", self.components.value_evidence, state)
        state = self._apply(context, "evidence_fusion", self.components.evidence_fusion, state)
        model_output = self._model(state, context)
        state = self._apply(
            context,
            "typed_decision_contract",
            self.components.typed_decision_contract,
            model_output,
        )
        state = self._apply(
            context,
            "empirical_calibration",
            self.components.empirical_calibration,
            state,
        )
        state = self._apply(
            context, "selective_routing", self.components.selective_routing, state
        )
        return ArmResult(state, context.controls.output_constraint)

    def p_uncalibrated(
        self, case_input: Mapping[str, Any], context: ArmContext
    ) -> ArmResult:
        state = self._apply(context, "domain_pack", self.components.domain_pack, case_input)
        state = self._apply(
            context, "deterministic_rules", self.components.deterministic_rules, state
        )
        state = self._apply(context, "value_evidence", self.components.value_evidence, state)
        state = self._apply(context, "evidence_fusion", self.components.evidence_fusion, state)
        model_output = self._model(state, context)
        state = self._apply(
            context,
            "typed_decision_contract",
            self.components.typed_decision_contract,
            model_output,
        )
        state = self._apply(
            context, "selective_routing", self.components.selective_routing, state
        )
        return ArmResult(state, context.controls.output_constraint)

    def build(self) -> dict[str, ArmRoute]:
        """Return five separately inspectable routes for identity freezing."""

        c = self.components
        return {
            "B0": ArmRoute(
                "B0", "d4-business-B0.v1", self.b0,
                dependencies=(("fixed_prompt", c.fixed_prompt),),
            ),
            "B1": ArmRoute(
                "B1", "d4-business-B1.v1", self.b1,
                dependencies=(("fixed_prompt", c.fixed_prompt),
                              ("typed_decision_contract", c.typed_decision_contract)),
            ),
            "B2": ArmRoute(
                "B2", "d4-business-B2.v1", self.b2,
                dependencies=(("domain_pack", c.domain_pack),
                              ("deterministic_rules", c.deterministic_rules),
                              ("value_evidence", c.value_evidence),
                              ("evidence_fusion", c.evidence_fusion),
                              ("selective_routing", c.selective_routing),
                              ("fixed_rule_threshold", c.fixed_rule_threshold)),
            ),
            "P": ArmRoute(
                "P",
                "d4-business-P.v1",
                self.p,
                calibrator=self.components.empirical_calibration,
                dependencies=(("fixed_prompt", c.fixed_prompt),
                              ("typed_decision_contract", c.typed_decision_contract),
                              ("domain_pack", c.domain_pack),
                              ("deterministic_rules", c.deterministic_rules),
                              ("value_evidence", c.value_evidence),
                              ("evidence_fusion", c.evidence_fusion),
                              ("empirical_calibration", c.empirical_calibration),
                              ("selective_routing", c.selective_routing)),
            ),
            "P-uncalibrated": ArmRoute(
                "P-uncalibrated",
                "d4-business-P-uncalibrated.v1",
                self.p_uncalibrated,
                dependencies=(("fixed_prompt", c.fixed_prompt),
                              ("typed_decision_contract", c.typed_decision_contract),
                              ("domain_pack", c.domain_pack),
                              ("deterministic_rules", c.deterministic_rules),
                              ("value_evidence", c.value_evidence),
                              ("evidence_fusion", c.evidence_fusion),
                              ("selective_routing", c.selective_routing)),
            ),
        }


__all__ = ["BusinessComponents", "FiveArmBusinessRoutes"]
