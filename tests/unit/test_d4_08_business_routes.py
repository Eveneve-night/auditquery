"""Offline business-route and failure regressions for the D4-08 five arms."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from backend.evaluation.d4_arm_wiring import (  # noqa: E402
    CanaryRunStopped, FiveArmExecutor, ProviderAttemptLedger, _canonical_sha256,
    run_public_canary,
)
from backend.evaluation.d4_adapter import ControlledEvaluationAdapter  # noqa: E402
from backend.evaluation.d4_business_routes import (  # noqa: E402
    BusinessComponents, FiveArmBusinessRoutes,
)
from backend.evaluation.d4_execution import ExecutionManifest  # noqa: E402
from backend.evaluation.d4_runner import (  # noqa: E402
    D4EvaluationError, EvaluationCaseOutcome, SlotCounts,
)
from backend.llm.contracts import (  # noqa: E402
    ModelCallRequest, ModelCallResult, ModelMessage, ModelUsage,
)
from backend.llm.errors import ProviderError, ProviderErrorCode  # noqa: E402


PROTOCOL = (ROOT / "configs/evaluation/d4/experiment-protocol.v1.json").read_bytes()
POLICY = (ROOT / "configs/policies/agent-loop-limits.v1.json").read_bytes()
CONTROL = (ROOT / "configs/evaluation/d4/public-canary-control.v1.json").read_bytes()


class RecordingOperation:
    def __init__(self, name: str, calls: list[str]) -> None:
        self.name = name
        self.component_id = f"offline-review.{name}"
        self.component_version = "1.0.0"
        self.calls = calls
        self.fail = False
        self.invalid = False
        self.inject_forbidden = False

    def __call__(self, value):
        self.calls.append(self.name)
        if self.fail:
            raise RuntimeError("sensitive component detail")
        if self.invalid:
            return "invalid"
        if self.name == "typed_decision_contract" and "decision" not in value:
            raise D4EvaluationError("typed_decision_contract_invalid")
        if self.inject_forbidden:
            return {**value, "expected_answer": "must never reach a prompt"}
        return dict(value)


class Calibrator(RecordingOperation):
    empirically_calibrated = True
    calibration_version = "review-required-calibration-v1"


class Prompt:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls
        self.component_id = "offline-review.fixed-prompt"
        self.component_version = "1.0.0"
        self.invalid = False

    def __call__(self, value, group_id):
        self.calls.append("fixed_prompt")
        if self.invalid:
            return {"not": "a request"}
        return ModelCallRequest(
            request_id=f"offline-{group_id}",
            task_type="typed_decision",
            messages=(ModelMessage("user", json.dumps(value, ensure_ascii=False)),),
            prompt_version="review-required-v1",
            output_schema_id="typed-decision.v1",
            max_completion_tokens=64,
            temperature=0,
        )


class FakeProvider:
    provider_id = "bailian-cn-beijing"
    model = "qwen3.7-flash"

    def __init__(self) -> None:
        self.calls = 0
        self.error: ProviderError | None = None
        self.invalid_payload = False

    def generate_structured(self, request):
        self.calls += 1
        if self.error is not None:
            raise self.error
        payload = {} if self.invalid_payload else {"decision": "clarify"}
        return ModelCallResult(
            request_id=request.request_id,
            task_type=request.task_type,
            provider_id=self.provider_id,
            requested_model=self.model,
            returned_model=self.model,
            provider_request_id="offline-fake",
            parsed_output=payload,
            usage=ModelUsage(7, 2, 9),
            estimated_cost_cny=0.00001,
            latency_ms=1,
            finish_reason="stop",
        )


class Source:
    candidate_catalog_sha256 = "b" * 64

    def load_input(self, case_id):
        return {
            "current_request": "public offline case",
            "candidate_options": ["candidate.public"],
            "evidence_refs": ["evidence.public"],
            "domain_pack_version": "1.0.0",
        }


class Scorer:
    scorer_sha256 = "c" * 64

    def score(self, case_id, observation):
        return EvaluationCaseOutcome(
            case_id=case_id,
            e2e_correct=False,
            necessary_clarification=True,
            did_clarify=True,
            post_clarification_success=False,
            confidence=0.5,
            sufficient_reference_correct=None,
            slot_counts={"slot.metric": SlotCounts(0, 0, 1)},
            schema_target_rank=None,
            latency_ms=observation.latency_ms,
            model_calls=observation.model_calls,
            input_tokens=observation.input_tokens,
            output_tokens=observation.output_tokens,
            cost_cny=observation.cost_cny,
            contract_valid=True,
            evidence_reference_valid=True,
            trace_complete=True,
        )


def fixture():
    calls: list[str] = []
    prompt = Prompt(calls)
    operations = {
        name: RecordingOperation(name, calls)
        for name in (
            "typed_decision_contract", "domain_pack", "deterministic_rules",
            "value_evidence", "evidence_fusion", "selective_routing",
            "fixed_rule_threshold",
        )
    }
    calibrator = Calibrator("empirical_calibration", calls)
    components = BusinessComponents(
        fixed_prompt=prompt,
        empirical_calibration=calibrator,
        **operations,
    )
    provider = FakeProvider()
    ledger = ProviderAttemptLedger(provider)
    routes = FiveArmBusinessRoutes(components)
    executor = FiveArmExecutor(routes.build(), ledger)
    manifest = ExecutionManifest(
        case_ids=("public.canary-001",),
        data_version="public-offline-v1",
        data_sha256="a" * 64,
        candidate_catalog_sha256="b" * 64,
        scorer_sha256="c" * 64,
        adapter_id="five-arm-business-review-candidate-v1",
        adapter_sha256="d" * 64,
        split="prompt_dev",
        offline=True,
    )
    adapter = ControlledEvaluationAdapter(
        adapter_id=manifest.adapter_id,
        adapter_sha256=manifest.adapter_sha256,
        offline=True,
        source=Source(),
        executor=executor,
        scorer=Scorer(),
    )
    return calls, prompt, operations, calibrator, provider, executor, manifest, adapter


def run(executor, manifest, adapter):
    return run_public_canary(
        manifest,
        adapter,
        executor,
        protocol_bytes=PROTOCOL,
        policy_bytes=POLICY,
        control_bytes=CONTROL,
        protocol_sha256=_canonical_sha256(PROTOCOL),
        control_sha256=_canonical_sha256(CONTROL),
    )


class BusinessRouteTest(unittest.TestCase):
    def test_five_real_routes_follow_frozen_components_and_meter_four_calls(self):
        calls, _, _, _, provider, executor, manifest, adapter = fixture()
        audit = run(executor, manifest, adapter)

        self.assertEqual(4, provider.calls)
        self.assertEqual("completed", audit["stop_reason"])
        self.assertEqual(("B0", "B1", "B2", "P", "P-uncalibrated"), audit["groups_completed"])
        self.assertEqual(1, calls.count("empirical_calibration"))
        self.assertEqual(1, calls.count("fixed_rule_threshold"))
        candidate = executor.identity_candidate(manifest, adapter)
        self.assertEqual(5, len(candidate["groups"]))
        self.assertTrue(candidate["component_binding_complete"])
        self.assertEqual(8, len(candidate["groups"]["P"]["dependencies"]))
        self.assertEqual(
            "review-required-calibration-v1",
            candidate["groups"]["P"]["calibration_version"],
        )

    def test_bad_prompt_is_local_failure_before_provider_io(self):
        _, prompt, _, _, provider, executor, manifest, adapter = fixture()
        prompt.invalid = True
        with self.assertRaises(CanaryRunStopped) as stopped:
            run(executor, manifest, adapter)
        self.assertEqual(0, provider.calls)
        self.assertEqual("arm_or_scorer_failed", stopped.exception.audit["stop_reason"])

    def test_malformed_model_payload_is_accounted_and_stops(self):
        _, _, _, _, provider, executor, manifest, adapter = fixture()
        provider.invalid_payload = True
        with self.assertRaises(CanaryRunStopped) as stopped:
            run(executor, manifest, adapter)
        # B0 has no typed-decision contract by design; B1 rejects the same
        # structurally valid JSON object when its required decision is absent.
        self.assertEqual(2, provider.calls)
        self.assertEqual(2, stopped.exception.audit["provider_attempts"])
        self.assertEqual("arm_or_scorer_failed", stopped.exception.audit["stop_reason"])

    def test_component_exception_is_redacted(self):
        _, _, operations, _, provider, executor, manifest, adapter = fixture()
        operations["typed_decision_contract"].fail = True
        with self.assertRaises(CanaryRunStopped) as stopped:
            run(executor, manifest, adapter)
        serialized = json.dumps(stopped.exception.audit)
        self.assertNotIn("sensitive component detail", serialized)
        self.assertEqual(2, provider.calls)

    def test_invalid_component_output_stops_before_later_arms(self):
        _, _, operations, _, provider, executor, manifest, adapter = fixture()
        operations["domain_pack"].invalid = True
        with self.assertRaises(CanaryRunStopped) as stopped:
            run(executor, manifest, adapter)
        self.assertEqual(2, provider.calls)
        self.assertEqual("arm_or_scorer_failed", stopped.exception.audit["stop_reason"])

    def test_component_cannot_inject_answer_fields_into_model_prompt(self):
        _, _, operations, _, provider, executor, manifest, adapter = fixture()
        operations["domain_pack"].inject_forbidden = True
        with self.assertRaises(CanaryRunStopped) as stopped:
            run(executor, manifest, adapter)
        # B0 and B1 complete first; B2 has no model and P is stopped before I/O.
        self.assertEqual(2, provider.calls)
        self.assertEqual("arm_or_scorer_failed", stopped.exception.audit["stop_reason"])
        self.assertNotIn("must never reach a prompt", json.dumps(stopped.exception.audit))

    def test_replacing_registered_prompt_is_rejected_before_provider_call(self):
        calls, _, _, _, provider, executor, manifest, adapter = fixture()
        routes = executor._routes["B0"].run.__self__
        object.__setattr__(routes.components, "fixed_prompt", Prompt(calls))
        with self.assertRaises(CanaryRunStopped) as stopped:
            run(executor, manifest, adapter)
        self.assertEqual(0, provider.calls)
        self.assertEqual("arm_or_scorer_failed", stopped.exception.audit["stop_reason"])

    def test_missing_or_extra_registered_component_is_rejected(self):
        _, _, _, _, provider, executor, manifest, adapter = fixture()
        original = executor._routes["B0"]
        executor._routes["B0"] = replace(original, dependencies=())
        with self.assertRaises(CanaryRunStopped):
            run(executor, manifest, adapter)
        self.assertEqual(0, provider.calls)

        _, _, _, _, provider, executor, manifest, adapter = fixture()
        original = executor._routes["B0"]
        executor._routes["B0"] = replace(
            original,
            dependencies=original.dependencies + (("domain_pack", executor._routes["B2"].dependencies[0][1]),),
        )
        with self.assertRaises(CanaryRunStopped):
            run(executor, manifest, adapter)
        self.assertEqual(0, provider.calls)

    def test_partially_registered_five_arm_set_is_rejected(self):
        _, _, _, _, _, executor, _, _ = fixture()
        routes = dict(executor._routes)
        routes["B1"] = replace(routes["B1"], dependencies=())
        with self.assertRaisesRegex(D4EvaluationError, "business_component_registry_incomplete"):
            FiveArmExecutor(routes, ProviderAttemptLedger(FakeProvider()))

    def test_provider_timeout_rate_limit_and_invalid_response_are_stable(self):
        for code in (
            ProviderErrorCode.TIMEOUT,
            ProviderErrorCode.RATE_LIMIT,
            ProviderErrorCode.INVALID_RESPONSE,
        ):
            with self.subTest(code=code):
                _, _, _, _, provider, executor, manifest, adapter = fixture()
                provider.error = ProviderError(
                    code,
                    "supplier detail must not escape",
                    retryable=True,
                    input_tokens=3,
                    output_tokens=0,
                    total_tokens=3,
                    estimated_cost_cny=0.000003,
                )
                with self.assertRaises(CanaryRunStopped) as stopped:
                    run(executor, manifest, adapter)
                audit = stopped.exception.audit
                self.assertEqual(1, audit["provider_attempts"])
                self.assertEqual((code.value,), audit["stable_error_codes"])
                self.assertEqual(f"provider_error:{code.value}", audit["stop_reason"])
                self.assertNotIn("supplier detail", json.dumps(audit))


if __name__ == "__main__":
    unittest.main()
