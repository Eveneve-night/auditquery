"""Offline tests for auditable D4-08 five-arm canary wiring."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from backend.evaluation.d4_adapter import ControlledEvaluationAdapter  # noqa: E402
from backend.evaluation.d4_arm_wiring import (  # noqa: E402
    ArmResult, ArmRoute, CanaryRunStopped, FiveArmExecutor, ProviderAttemptLedger,
    _canonical_sha256, run_public_canary,
)
from backend.evaluation.d4_execution import ExecutionManifest  # noqa: E402
from backend.evaluation.d4_runner import (  # noqa: E402
    D4EvaluationError, EvaluationCaseOutcome, GROUP_IDS, SlotCounts,
)
from backend.llm.contracts import (  # noqa: E402
    ModelCallRequest, ModelCallResult, ModelMessage, ModelUsage,
)
from backend.llm.bailian import BailianProvider  # noqa: E402
from backend.llm.errors import ProviderError, ProviderErrorCode  # noqa: E402


PROTOCOL = (ROOT / "configs/evaluation/d4/experiment-protocol.v1.json").read_bytes()
POLICY = (ROOT / "configs/policies/agent-loop-limits.v1.json").read_bytes()
CONTROL = (ROOT / "configs/evaluation/d4/public-canary-control.v1.json").read_bytes()


def _request(group_id: str) -> ModelCallRequest:
    return ModelCallRequest(
        request_id=f"synthetic-{group_id}", task_type="clarification",
        messages=(ModelMessage("user", "synthetic public input"),),
        prompt_version="synthetic-v1", output_schema_id="synthetic-v1",
        max_completion_tokens=16, temperature=0,
    )


class FakeProvider:
    provider_id = "bailian-cn-beijing"
    model = "qwen3.7-flash"

    def __init__(self) -> None:
        self.calls = 0
        self.error: ProviderError | None = None
        self.cost = 0.00001
        self.attempt_count = 1

    def generate_structured(self, request: ModelCallRequest) -> ModelCallResult:
        self.calls += 1
        if self.error:
            raise self.error
        return ModelCallResult(
            request_id=request.request_id, task_type=request.task_type,
            provider_id=self.provider_id, requested_model=self.model,
            returned_model=self.model, provider_request_id=None,
            parsed_output={"action": "clarify"},
            usage=ModelUsage(8, 2, 10), estimated_cost_cny=self.cost,
            latency_ms=1.0, finish_reason="stop", attempt_count=self.attempt_count,
        )


class Calibrator:
    empirically_calibrated = True
    calibration_version = "synthetic-calibration-v1"

    def __call__(self) -> str:
        return "synthetic-score"


def _run_components(case_input, context):
    for component in context.controls.enabled_components:
        if component == "llm_semantic_judgment":
            context.model_call(_request(context.controls.group_id))
        elif component == "empirical_calibration":
            context.apply(component, context.calibrator)
        else:
            context.apply(component, lambda: None)
    return ArmResult(output={"action": "clarify"}, output_constraint=context.controls.output_constraint)


def _b0(case_input, context):
    return _run_components(case_input, context)


def _b1(case_input, context):
    return _run_components(case_input, context)


def _b2(case_input, context):
    return _run_components(case_input, context)


def _p(case_input, context):
    return _run_components(case_input, context)


def _p_uncalibrated(case_input, context):
    return _run_components(case_input, context)


def _routes():
    return {
        group: ArmRoute(group, f"synthetic-{group}", runner,
                        calibrator=Calibrator() if group == "P" else None)
        for group, runner in (
            ("B0", _b0), ("B1", _b1), ("B2", _b2),
            ("P", _p), ("P-uncalibrated", _p_uncalibrated),
        )
    }


class Source:
    candidate_catalog_sha256 = "b" * 64

    def load_input(self, case_id):
        return {"current_request": "synthetic public input", "candidate_options": ["candidate.public"]}


class Scorer:
    scorer_sha256 = "c" * 64

    def score(self, case_id, observation):
        return EvaluationCaseOutcome(
            case_id=case_id, e2e_correct=False, necessary_clarification=True,
            did_clarify=True, post_clarification_success=False,
            confidence=0.5, sufficient_reference_correct=None,
            slot_counts={"slot.metric": SlotCounts(0, 0, 1)},
            schema_target_rank=None, latency_ms=observation.latency_ms,
            model_calls=observation.model_calls,
            input_tokens=observation.input_tokens,
            output_tokens=observation.output_tokens, cost_cny=observation.cost_cny,
            contract_valid=True, evidence_reference_valid=True, trace_complete=True,
        )


def _fixture(provider=None, routes=None):
    provider = provider or FakeProvider()
    ledger = ProviderAttemptLedger(provider)
    executor = FiveArmExecutor(routes or _routes(), ledger)
    manifest = ExecutionManifest(
        case_ids=("public.canary-001",), data_version="synthetic-public-v1",
        data_sha256="a" * 64, candidate_catalog_sha256="b" * 64,
        scorer_sha256="c" * 64, adapter_id="synthetic-five-arm-v1",
        adapter_sha256="d" * 64, split="prompt_dev", offline=True,
    )
    adapter = ControlledEvaluationAdapter(
        adapter_id=manifest.adapter_id, adapter_sha256=manifest.adapter_sha256,
        offline=True, source=Source(), executor=executor, scorer=Scorer(),
    )
    return provider, ledger, executor, manifest, adapter


def _run(manifest, adapter, executor, *, control=CONTROL):
    return run_public_canary(
        manifest, adapter, executor, protocol_bytes=PROTOCOL,
        policy_bytes=POLICY, control_bytes=control,
        protocol_sha256=_canonical_sha256(PROTOCOL),
        control_sha256=_canonical_sha256(control),
    )


class D4ArmWiringTest(unittest.TestCase):
    def test_exact_five_arm_wiring_has_measured_usage_and_no_accuracy_claim(self):
        provider, ledger, executor, manifest, adapter = _fixture()
        audit = _run(manifest, adapter, executor)
        self.assertEqual(4, provider.calls)
        self.assertEqual(("B0", "B1", "B2", "P", "P-uncalibrated"), audit["groups_completed"])
        self.assertEqual(0, audit["group_attempts"]["B2"])
        self.assertEqual(4, audit["provider_attempts"])
        self.assertEqual(32, audit["input_tokens"])
        self.assertEqual(8, audit["output_tokens"])
        self.assertEqual(0.00004, audit["estimated_cost_cny"])
        self.assertEqual("completed", audit["stop_reason"])
        self.assertEqual(32, len(audit["run_id"]))
        self.assertEqual(64, len(audit["case_manifest_sha256"]))
        self.assertFalse(audit["business_claims_allowed"])
        self.assertNotIn("synthetic public input", json.dumps(audit))
        self.assertNotIn("candidate.public", json.dumps(audit))
        self.assertEqual(64, len(audit["group_observations"]["P"]["route_source_sha256"]))
        self.assertEqual(64, len(audit["group_observations"]["P"]["route_module_sha256"]))
        self.assertEqual(64, len(audit["provider_module_sha256"]))

    def test_identity_candidate_binds_routes_provider_and_content_hashes(self):
        provider, _, executor, manifest, adapter = _fixture()
        candidate = executor.identity_candidate(manifest, adapter)

        self.assertEqual("d4-08-five-arm-adapter-candidate.v1", candidate["identity_kind"])
        self.assertEqual(provider.provider_id, candidate["provider_id"])
        self.assertEqual(provider.model, candidate["model_id"])
        self.assertEqual(list(GROUP_IDS), list(candidate["groups"]))
        self.assertEqual(64, len(candidate["identity_sha256"]))
        serialized = json.dumps(candidate, ensure_ascii=False)
        self.assertNotIn("question", serialized)
        self.assertNotIn("expected", serialized)
        self.assertNotIn("prompt", serialized.lower())

        changed = replace(manifest, scorer_sha256="e" * 64)
        changed_adapter = ControlledEvaluationAdapter(
            adapter_id=changed.adapter_id,
            adapter_sha256=changed.adapter_sha256,
            offline=changed.offline,
            source=adapter.source,
            executor=executor,
            scorer=adapter.scorer,
        )
        self.assertNotEqual(
            candidate["identity_sha256"],
            executor.identity_candidate(changed, changed_adapter)["identity_sha256"],
        )

    def test_identity_candidate_rejects_adapter_executor_mismatch(self):
        _, _, executor, manifest, adapter = _fixture()
        _, _, other_executor, _, _ = _fixture()
        adapter.executor = other_executor
        with self.assertRaisesRegex(D4EvaluationError, "adapter_identity_mismatch"):
            executor.identity_candidate(manifest, adapter)

    def test_five_distinct_routes_are_required(self):
        provider = FakeProvider()
        ledger = ProviderAttemptLedger(provider)
        routes = _routes()
        del routes["B2"]
        with self.assertRaisesRegex(D4EvaluationError, "five_arm_routes_incomplete"):
            FiveArmExecutor(routes, ledger)
        routes = _routes()
        routes["B1"] = ArmRoute("B1", "synthetic-B1-same-function", _b0)
        with self.assertRaisesRegex(D4EvaluationError, "five_arm_route_functions_not_unique"):
            FiveArmExecutor(routes, ledger)

    def test_provider_failure_counts_attempt_and_stops_without_content(self):
        provider, ledger, executor, manifest, adapter = _fixture()
        provider.error = ProviderError(
            ProviderErrorCode.AUTHENTICATION, "secret supplier message",
            retryable=False, http_status=401, input_tokens=3, total_tokens=3,
            estimated_cost_cny=0.000003,
        )
        with self.assertRaises(CanaryRunStopped) as failure:
            _run(manifest, adapter, executor)
        self.assertEqual(1, provider.calls)
        snapshot = ledger.snapshot()
        self.assertEqual(1, snapshot["provider_attempts"])
        self.assertEqual(3, snapshot["input_tokens"])
        self.assertEqual("provider_error:authentication_error", snapshot["stop_reason"])
        self.assertNotIn("secret supplier message", json.dumps(snapshot))
        self.assertEqual(snapshot["stop_reason"], failure.exception.audit["stop_reason"])
        self.assertEqual(1, failure.exception.audit["provider_attempts"])
        self.assertNotIn("secret supplier message", json.dumps(failure.exception.audit))

    def test_hard_cost_stops_before_next_group(self):
        provider, ledger, executor, manifest, adapter = _fixture()
        provider.cost = 0.05
        audit = _run(manifest, adapter, executor)
        self.assertEqual(1, provider.calls)
        self.assertEqual(("B0",), audit["groups_completed"])
        self.assertEqual("hard_cost_cap_reached", audit["stop_reason"])

    def test_frozen_token_and_wall_budgets_stop_after_recording_usage(self):
        budget = {"max_input_tokens_total": 8, "max_output_tokens_total": 5000,
                  "max_wall_time_ms": 30000}
        provider = FakeProvider()
        ledger = ProviderAttemptLedger(provider)
        ledger.call("B0", _request("B0"))
        ledger.enforce_budget(budget, 1)
        self.assertEqual("input_token_cap_reached", ledger.snapshot()["stop_reason"])
        self.assertEqual(8, ledger.snapshot()["input_tokens"])
        with self.assertRaisesRegex(D4EvaluationError, "canary_already_stopped"):
            ledger.call("B1", _request("B1"))
        self.assertEqual(1, provider.calls)

        ledger = ProviderAttemptLedger(FakeProvider())
        ledger.enforce_budget(budget, 30000)
        self.assertEqual("wall_time_cap_reached", ledger.snapshot()["stop_reason"])
        self.assertEqual(0, ledger.snapshot()["provider_attempts"])

    def test_retrying_provider_result_fails_closed_and_counts_one_port_attempt(self):
        provider, ledger, executor, manifest, adapter = _fixture()
        provider.attempt_count = 2
        with self.assertRaises(CanaryRunStopped) as failure:
            _run(manifest, adapter, executor)
        self.assertEqual(2, ledger.snapshot()["provider_attempts"])
        self.assertEqual("provider_result_identity_invalid", ledger.snapshot()["stop_reason"])
        self.assertEqual(2, failure.exception.audit["provider_attempts"])

    def test_missing_real_calibration_blocks_p_before_claim(self):
        routes = _routes()
        routes["P"] = ArmRoute("P", "synthetic-P-bad", _p, calibrator=lambda: None)
        provider = FakeProvider()
        with self.assertRaisesRegex(D4EvaluationError, "empirical_calibration_missing"):
            _fixture(provider=provider, routes=routes)
        self.assertEqual(0, provider.calls)

    def test_disabled_or_missing_component_blocks_execution(self):
        def bad_b2(case_input, context):
            context.model_call(_request("B2"))
            return ArmResult({}, context.controls.output_constraint)

        routes = _routes()
        routes["B2"] = ArmRoute("B2", "synthetic-B2-bad", bad_b2)
        provider, ledger, executor, manifest, adapter = _fixture(routes=routes)
        with self.assertRaises(CanaryRunStopped):
            _run(manifest, adapter, executor)
        self.assertEqual(2, provider.calls)
        self.assertEqual(0, ledger.snapshot()["group_attempts"]["B2"])
        self.assertEqual("arm_or_scorer_failed", ledger.snapshot()["stop_reason"])

        def incomplete_b0(case_input, context):
            context.model_call(_request("B0"))
            return ArmResult({}, context.controls.output_constraint)

        routes = _routes()
        routes["B0"] = ArmRoute("B0", "synthetic-B0-incomplete", incomplete_b0)
        provider, ledger, executor, manifest, adapter = _fixture(routes=routes)
        with self.assertRaises(CanaryRunStopped):
            _run(manifest, adapter, executor)
        self.assertEqual(1, provider.calls)
        self.assertEqual("arm_or_scorer_failed", ledger.snapshot()["stop_reason"])

    def test_replayed_request_id_is_stopped_before_second_provider_call(self):
        provider = FakeProvider()
        ledger = ProviderAttemptLedger(provider)
        first = _request("B0")
        ledger.call("B0", first)
        with self.assertRaisesRegex(D4EvaluationError, "request_id_replayed"):
            ledger.call("B1", first)
        self.assertEqual(1, provider.calls)
        self.assertEqual("request_id_replayed", ledger.snapshot()["stop_reason"])

    def test_oversized_request_is_rejected_before_provider_call(self):
        provider = FakeProvider()
        ledger = ProviderAttemptLedger(provider)
        oversized = replace(_request("B0"), max_completion_tokens=1501)
        with self.assertRaisesRegex(D4EvaluationError, "canary_request_budget_invalid"):
            ledger.call("B0", oversized)
        self.assertEqual(0, provider.calls)
        self.assertEqual("request_budget_invalid", ledger.snapshot()["stop_reason"])

    def test_non_public_split_or_changed_control_is_rejected_before_provider(self):
        provider, ledger, executor, manifest, adapter = _fixture()
        with self.assertRaisesRegex(D4EvaluationError, "public_canary_manifest_invalid"):
            _run(replace(manifest, split="validation"), adapter, executor)
        changed = json.loads(CONTROL)
        changed["batch_limits"]["provider_attempt_cap"] = 5
        with self.assertRaisesRegex(D4EvaluationError, "canary_control_invalid"):
            _run(manifest, adapter, executor, control=json.dumps(changed).encode())
        self.assertEqual(0, provider.calls)
        adapter.offline = False
        with self.assertRaisesRegex(D4EvaluationError, "online_provider_adapter_invalid"):
            run_public_canary(
                replace(manifest, offline=False, authorization_id="synthetic-authorization"),
                adapter, executor, protocol_bytes=PROTOCOL, policy_bytes=POLICY,
                control_bytes=CONTROL, protocol_sha256=_canonical_sha256(PROTOCOL),
                control_sha256=_canonical_sha256(CONTROL), allow_online=True,
            )
        self.assertEqual(0, provider.calls)

    def test_real_provider_type_still_cannot_run_without_frozen_adapter_identity(self):
        # No provider constructor, credentials or network call is used here.
        provider = object.__new__(BailianProvider)
        provider.provider_id = "bailian-cn-beijing"
        provider.model = "qwen3.7-flash"
        _, ledger, executor, manifest, adapter = _fixture(provider=provider)
        adapter.offline = False
        with self.assertRaisesRegex(D4EvaluationError, "online_adapter_identity_not_frozen"):
            run_public_canary(
                replace(manifest, offline=False, authorization_id="synthetic-authorization"),
                adapter, executor, protocol_bytes=PROTOCOL, policy_bytes=POLICY,
                control_bytes=CONTROL, protocol_sha256=_canonical_sha256(PROTOCOL),
                control_sha256=_canonical_sha256(CONTROL), allow_online=True,
            )
        self.assertEqual(0, ledger.snapshot()["provider_attempts"])


if __name__ == "__main__":
    unittest.main()
