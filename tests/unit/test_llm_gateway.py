from __future__ import annotations

from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from backend.agent.loop_guard import LoopGuard, LoopPolicy  # noqa: E402
from backend.llm.contracts import (  # noqa: E402
    ModelCallRequest,
    ModelCallResult,
    ModelMessage,
    ModelUsage,
)
from backend.llm.errors import ProviderError, ProviderErrorCode  # noqa: E402
from backend.llm.gateway import CircuitState, LLMGateway  # noqa: E402


POLICY_PATH = PROJECT_ROOT / "configs" / "policies" / "agent-loop-limits.v1.json"


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def advance(self, seconds: float) -> None:
        self.now += seconds


class SequenceProvider:
    provider_id = "fixture-provider"
    model = "qwen3.7-flash"

    def __init__(self, outcomes: list[ModelCallResult | ProviderError]) -> None:
        self.outcomes = list(outcomes)
        self.requests: list[ModelCallRequest] = []

    def generate_structured(self, request: ModelCallRequest) -> ModelCallResult:
        self.requests.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, ProviderError):
            raise outcome
        return outcome


def request() -> ModelCallRequest:
    return ModelCallRequest(
        request_id="gateway-request-1",
        task_type="intent",
        messages=(ModelMessage("user", "Classify the request."),),
        prompt_version="intent.v1",
        output_schema_id="intent-result.v1",
        max_completion_tokens=128,
    )


def success(
    *, input_tokens: int = 100, output_tokens: int = 20, cost: float = 0.000036
) -> ModelCallResult:
    return ModelCallResult(
        request_id="gateway-request-1",
        task_type="intent",
        provider_id="fixture-provider",
        requested_model="qwen3.7-flash",
        returned_model="qwen3.7-flash",
        provider_request_id="provider-request-1",
        parsed_output={"intent": "query"},
        usage=ModelUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
        ),
        estimated_cost_cny=cost,
        latency_ms=10,
        finish_reason="stop",
    )


def error(
    code: ProviderErrorCode,
    *,
    retryable: bool,
    retry_after: float | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost: float = 0.0,
) -> ProviderError:
    return ProviderError(
        code,
        "fixture failure",
        retryable=retryable,
        retry_after_seconds=retry_after,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        estimated_cost_cny=cost,
    )


class LLMGatewayTest(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = LoopPolicy.from_file(POLICY_PATH)
        self.clock = FakeClock()

    def gateway(self, provider: SequenceProvider, policy: LoopPolicy | None = None) -> LLMGateway:
        return LLMGateway(
            provider,
            policy or self.policy,
            sleeper=self.clock.sleep,
            clock=self.clock,
        )

    def guard(self, policy: LoopPolicy | None = None) -> LoopGuard:
        return LoopGuard(policy or self.policy, clock=self.clock)

    def test_rate_limit_retries_once_then_succeeds(self) -> None:
        provider = SequenceProvider(
            [
                error(
                    ProviderErrorCode.RATE_LIMIT,
                    retryable=True,
                    retry_after=0.75,
                ),
                success(),
            ]
        )
        guard = self.guard()

        result = self.gateway(provider).generate_structured(request(), guard=guard)

        self.assertEqual(2, result.attempt_count)
        self.assertEqual(("provider_retry_applied",), result.warnings)
        self.assertEqual([0.75], self.clock.sleeps)
        self.assertEqual(2, len(provider.requests))
        self.assertEqual(2, guard.snapshot()["counts"]["model_call"])
        self.assertEqual(1, guard.snapshot()["counts"]["provider_retry"])

    def test_400_401_and_403_stop_without_retry(self) -> None:
        for code in (
            ProviderErrorCode.INVALID_REQUEST,
            ProviderErrorCode.AUTHENTICATION,
            ProviderErrorCode.AUTHORIZATION,
        ):
            with self.subTest(code=code):
                provider = SequenceProvider([error(code, retryable=False)])
                gateway = self.gateway(provider)
                with self.assertRaises(ProviderError) as context:
                    gateway.generate_structured(request(), guard=self.guard())
                self.assertEqual(code, context.exception.code)
                self.assertEqual(1, len(provider.requests))
                self.assertEqual([], self.clock.sleeps)

    def test_network_and_nonretryable_5xx_do_not_retry(self) -> None:
        for provider_error in (
            error(ProviderErrorCode.NETWORK, retryable=True),
            error(ProviderErrorCode.TRANSIENT, retryable=False),
        ):
            with self.subTest(code=provider_error.code, retryable=provider_error.retryable):
                provider = SequenceProvider([provider_error])
                with self.assertRaises(ProviderError):
                    self.gateway(provider).generate_structured(
                        request(), guard=self.guard()
                    )
                self.assertEqual(1, len(provider.requests))

    def test_retry_exhaustion_is_three_total_attempts(self) -> None:
        provider_error = error(ProviderErrorCode.TRANSIENT, retryable=True)
        provider = SequenceProvider([provider_error, provider_error, provider_error, success()])
        guard = self.guard()

        with self.assertRaises(ProviderError) as context:
            self.gateway(provider).generate_structured(request(), guard=guard)

        self.assertEqual(ProviderErrorCode.TRANSIENT, context.exception.code)
        self.assertEqual(3, len(provider.requests))
        self.assertEqual(2, guard.snapshot()["counts"]["provider_retry"])

    def test_invalid_json_gets_exactly_one_repair_attempt(self) -> None:
        provider = SequenceProvider(
            [error(ProviderErrorCode.INVALID_RESPONSE, retryable=False), success()]
        )
        guard = self.guard()

        result = self.gateway(provider).generate_structured(request(), guard=guard)

        self.assertEqual(2, result.attempt_count)
        self.assertIn("json_repair_applied", result.warnings)
        self.assertEqual(1, guard.snapshot()["counts"]["json_repair"])
        self.assertEqual("system", provider.requests[1].messages[0].role)
        self.assertIn("valid JSON object", provider.requests[1].messages[0].content)

    def test_failed_attempt_usage_is_counted_before_repair_succeeds(self) -> None:
        provider = SequenceProvider(
            [
                error(
                    ProviderErrorCode.INVALID_RESPONSE,
                    retryable=False,
                    input_tokens=40,
                    output_tokens=5,
                    cost=0.10,
                ),
                success(input_tokens=100, output_tokens=20, cost=0.20),
            ]
        )
        guard = self.guard()

        result = self.gateway(provider).generate_structured(request(), guard=guard)

        snapshot = guard.snapshot()
        self.assertEqual(2, result.attempt_count)
        self.assertEqual(140, snapshot["input_tokens_total"])
        self.assertEqual(25, snapshot["output_tokens_total"])
        self.assertAlmostEqual(0.30, snapshot["estimated_cost_cny"])

    def test_failed_response_usage_can_stop_retry_on_hard_limit(self) -> None:
        provider = SequenceProvider(
            [
                error(
                    ProviderErrorCode.RATE_LIMIT,
                    retryable=True,
                    input_tokens=30001,
                    output_tokens=1,
                ),
                success(),
            ]
        )
        guard = self.guard()

        with self.assertRaises(ProviderError) as context:
            self.gateway(provider).generate_structured(request(), guard=guard)

        self.assertEqual(ProviderErrorCode.POLICY_LIMIT, context.exception.code)
        self.assertEqual("token_limit", guard.snapshot()["stopped_reason"])
        self.assertEqual(1, len(provider.requests))
        self.assertEqual([], self.clock.sleeps)

    def test_failed_response_at_exact_limit_does_not_start_retry(self) -> None:
        provider = SequenceProvider(
            [
                error(
                    ProviderErrorCode.RATE_LIMIT,
                    retryable=True,
                    input_tokens=30000,
                ),
                success(),
            ]
        )
        guard = self.guard()

        with self.assertRaises(ProviderError) as context:
            self.gateway(provider).generate_structured(request(), guard=guard)

        self.assertEqual(ProviderErrorCode.POLICY_LIMIT, context.exception.code)
        self.assertEqual("token_limit", guard.snapshot()["stopped_reason"])
        self.assertEqual(1, len(provider.requests))

    def test_second_invalid_json_is_not_repaired_again(self) -> None:
        invalid = error(ProviderErrorCode.INVALID_RESPONSE, retryable=False)
        provider = SequenceProvider([invalid, invalid, success()])
        guard = self.guard()

        with self.assertRaises(ProviderError) as context:
            self.gateway(provider).generate_structured(request(), guard=guard)

        self.assertEqual(ProviderErrorCode.INVALID_RESPONSE, context.exception.code)
        self.assertEqual(2, len(provider.requests))
        self.assertEqual(1, guard.snapshot()["counts"]["json_repair"])

    def test_authentication_failure_opens_then_half_open_probe_recovers(self) -> None:
        provider = SequenceProvider(
            [error(ProviderErrorCode.AUTHENTICATION, retryable=False), success()]
        )
        gateway = self.gateway(provider)

        with self.assertRaises(ProviderError):
            gateway.generate_structured(request(), guard=self.guard())
        self.assertEqual(CircuitState.OPEN.value, gateway.circuit.snapshot()["state"])

        with self.assertRaises(ProviderError) as context:
            gateway.generate_structured(request(), guard=self.guard())
        self.assertEqual(ProviderErrorCode.CIRCUIT_OPEN, context.exception.code)
        self.assertEqual(1, len(provider.requests))

        self.clock.advance(31)
        result = gateway.generate_structured(request(), guard=self.guard())
        self.assertEqual({"intent": "query"}, result.parsed_output)
        self.assertEqual(CircuitState.CLOSED.value, gateway.circuit.snapshot()["state"])
        self.assertEqual(2, len(provider.requests))

    def test_usage_over_hard_token_limit_stops_after_successful_call(self) -> None:
        provider = SequenceProvider([success(input_tokens=30001, output_tokens=1)])
        guard = self.guard()

        with self.assertRaises(ProviderError) as context:
            self.gateway(provider).generate_structured(request(), guard=guard)

        self.assertEqual(ProviderErrorCode.POLICY_LIMIT, context.exception.code)
        self.assertEqual("token_limit", guard.snapshot()["stopped_reason"])
        self.assertEqual(1, len(provider.requests))

    def test_guard_may_narrow_but_cannot_loosen_gateway_limits(self) -> None:
        narrower_limits = dict(self.policy.limits)
        narrower_limits["max_model_calls"] = 1
        narrower = LoopPolicy(
            policy_id=self.policy.policy_id,
            version=self.policy.version,
            limits=narrower_limits,
            termination_precedence=self.policy.termination_precedence,
        )
        provider = SequenceProvider([success()])
        result = self.gateway(provider).generate_structured(
            request(), guard=self.guard(narrower)
        )
        self.assertEqual(1, result.attempt_count)

        looser_limits = dict(self.policy.limits)
        looser_limits["max_model_calls"] = 5
        looser = LoopPolicy(
            policy_id=self.policy.policy_id,
            version=self.policy.version,
            limits=looser_limits,
            termination_precedence=self.policy.termination_precedence,
        )
        blocked_provider = SequenceProvider([success()])
        with self.assertRaises(ProviderError) as context:
            self.gateway(blocked_provider).generate_structured(
                request(), guard=self.guard(looser)
            )
        self.assertEqual(ProviderErrorCode.CONFIGURATION, context.exception.code)
        self.assertEqual([], blocked_provider.requests)

    def test_retry_sleep_cannot_exceed_wall_time_budget(self) -> None:
        limits = dict(self.policy.limits)
        limits["max_wall_time_ms"] = 1000
        policy = LoopPolicy(
            policy_id=self.policy.policy_id,
            version=self.policy.version,
            limits=limits,
            termination_precedence=self.policy.termination_precedence,
        )
        provider = SequenceProvider(
            [
                error(
                    ProviderErrorCode.RATE_LIMIT,
                    retryable=True,
                    retry_after=5,
                )
            ]
        )
        guard = self.guard(policy)

        with self.assertRaises(ProviderError) as context:
            self.gateway(provider, policy).generate_structured(request(), guard=guard)

        self.assertEqual(ProviderErrorCode.POLICY_LIMIT, context.exception.code)
        self.assertEqual("wall_time_limit", guard.snapshot()["stopped_reason"])
        self.assertEqual([], self.clock.sleeps)


if __name__ == "__main__":
    unittest.main()
