"""Bounded reliability orchestration around one LLM provider.

This module owns retry, JSON repair, circuit breaking, and LoopGuard
integration. It never selects a fallback model and never reads credentials.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from threading import Lock
import time
from typing import Callable

from backend.agent.loop_guard import LoopGuard, LoopPolicy

from .contracts import LLMProvider, ModelCallRequest, ModelCallResult, ModelMessage
from .errors import ProviderError, ProviderErrorCode
from .usage import UsageLedger


RETRYABLE_CODES = {
    ProviderErrorCode.RATE_LIMIT,
    ProviderErrorCode.TIMEOUT,
    ProviderErrorCode.TRANSIENT,
}
FORCE_OPEN_CODES = {
    ProviderErrorCode.AUTHENTICATION,
    ProviderErrorCode.AUTHORIZATION,
    ProviderErrorCode.CONFIGURATION,
    ProviderErrorCode.QUOTA,
    ProviderErrorCode.MODEL_MISMATCH,
}
JSON_REPAIR_INSTRUCTION = (
    "The previous response did not satisfy the required JSON contract. "
    "Return exactly one valid JSON object and no surrounding commentary."
)


@dataclass(frozen=True)
class ReliabilityConfig:
    max_provider_retries: int
    retry_base_delay_seconds: float
    retry_max_delay_seconds: float
    circuit_failure_threshold: int
    circuit_open_seconds: float

    @classmethod
    def from_policy(cls, policy: LoopPolicy) -> "ReliabilityConfig":
        limits = policy.limits
        config = cls(
            max_provider_retries=int(limits["max_provider_retries_per_call"]),
            retry_base_delay_seconds=float(limits["retry_base_delay_ms"]) / 1000,
            retry_max_delay_seconds=float(limits["retry_max_delay_ms"]) / 1000,
            circuit_failure_threshold=int(limits["circuit_failure_threshold"]),
            circuit_open_seconds=float(limits["circuit_open_seconds"]),
        )
        if (
            config.max_provider_retries < 0
            or config.retry_base_delay_seconds < 0
            or config.retry_max_delay_seconds < config.retry_base_delay_seconds
            or config.circuit_failure_threshold <= 0
            or config.circuit_open_seconds <= 0
        ):
            raise ValueError("Reliability policy values are invalid")
        return config


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Thread-safe circuit breaker for one fixed provider."""

    def __init__(
        self,
        *,
        failure_threshold: int,
        open_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.open_seconds = open_seconds
        self.clock = clock
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._opened_at: float | None = None
        self._half_open_in_flight = False
        self._lock = Lock()

    def before_call(self) -> bool:
        """Return whether this permit is the single half-open probe."""

        with self._lock:
            now = self.clock()
            if self._state == CircuitState.OPEN:
                assert self._opened_at is not None
                if now - self._opened_at < self.open_seconds:
                    raise self._open_error()
                self._state = CircuitState.HALF_OPEN
                self._half_open_in_flight = False
            if self._state == CircuitState.HALF_OPEN:
                if self._half_open_in_flight:
                    raise self._open_error()
                self._half_open_in_flight = True
                return True
            return False

    def cancel_permit(self, was_half_open_probe: bool) -> None:
        if not was_half_open_probe:
            return
        with self._lock:
            self._half_open_in_flight = False

    def record_success(self) -> None:
        with self._lock:
            self._state = CircuitState.CLOSED
            self._consecutive_failures = 0
            self._opened_at = None
            self._half_open_in_flight = False

    def record_failure(self, error: ProviderError) -> None:
        with self._lock:
            force_open = error.code in FORCE_OPEN_CODES
            count_failure = error.retryable and error.code in RETRYABLE_CODES
            if self._state == CircuitState.HALF_OPEN:
                self._open_locked()
            elif force_open:
                self._open_locked()
            elif count_failure:
                self._consecutive_failures += 1
                if self._consecutive_failures >= self.failure_threshold:
                    self._open_locked()

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "state": self._state.value,
                "consecutive_failures": self._consecutive_failures,
                "opened_at": self._opened_at,
                "half_open_in_flight": self._half_open_in_flight,
            }

    def _open_locked(self) -> None:
        self._state = CircuitState.OPEN
        self._opened_at = self.clock()
        self._half_open_in_flight = False

    @staticmethod
    def _open_error() -> ProviderError:
        return ProviderError(
            ProviderErrorCode.CIRCUIT_OPEN,
            "Provider circuit is open; no model request was sent",
            retryable=False,
        )


class LLMGateway:
    """Reliability boundary for one provider and one approved model."""

    def __init__(
        self,
        provider: LLMProvider,
        policy: LoopPolicy,
        *,
        usage_ledger: UsageLedger | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.provider = provider
        self.policy = policy
        self.config = ReliabilityConfig.from_policy(policy)
        inherited_ledger = getattr(provider, "usage_ledger", None)
        self.usage_ledger = usage_ledger or (
            inherited_ledger if isinstance(inherited_ledger, UsageLedger) else None
        )
        self.sleeper = sleeper
        self.clock = clock
        self.circuit = CircuitBreaker(
            failure_threshold=self.config.circuit_failure_threshold,
            open_seconds=self.config.circuit_open_seconds,
            clock=clock,
        )

    def generate_structured(
        self,
        request: ModelCallRequest,
        *,
        guard: LoopGuard | None = None,
    ) -> ModelCallResult:
        guard = guard or LoopGuard(self.policy, clock=self.clock)
        self._validate_guard(guard)
        gateway_started = self.clock()
        current_request = request
        attempt_count = 0
        retry_count = 0
        repair_used = False
        usage_warning: str | None = None

        while True:
            half_open_probe = self.circuit.before_call()
            call_decision = guard.record(
                "model_call", action=f"model_call:{request.task_type}"
            )
            if not call_decision.allowed:
                self.circuit.cancel_permit(half_open_probe)
                raise self._policy_error(call_decision.stop_reason)
            attempt_count += 1

            try:
                result = self.provider.generate_structured(current_request)
            except ProviderError as error:
                self.circuit.record_failure(error)
                failure_usage = guard.record_usage(
                    input_tokens=error.input_tokens,
                    output_tokens=error.output_tokens,
                    estimated_cost_cny=error.estimated_cost_cny,
                )
                usage_warning = failure_usage.warning or usage_warning
                if not failure_usage.allowed:
                    raise self._policy_error(failure_usage.stop_reason) from error
                reached_reason = self._failure_usage_limit_reached(guard)
                if reached_reason is not None:
                    guard.terminate(reached_reason)
                    raise self._policy_error(reached_reason) from error
                if error.code == ProviderErrorCode.INVALID_RESPONSE and not repair_used:
                    repair_decision = guard.record(
                        "json_repair",
                        action="json_repair",
                        scope_id=request.request_id,
                    )
                    if not repair_decision.allowed:
                        raise self._policy_error(repair_decision.stop_reason) from error
                    repair_used = True
                    current_request = self._repair_request(request)
                    continue
                if not self._can_retry(error, retry_count):
                    raise
                retry_decision = guard.record(
                    "provider_retry",
                    action="provider_retry",
                    scope_id=request.request_id,
                )
                if not retry_decision.allowed:
                    raise self._policy_error(retry_decision.stop_reason) from error
                retry_count += 1
                delay_seconds = self._retry_delay(error, retry_count)
                self._sleep_within_wall_limit(guard, delay_seconds)
                continue
            except Exception as exc:
                error = ProviderError(
                    ProviderErrorCode.INVALID_RESPONSE,
                    "Provider violated the model-call contract",
                    retryable=False,
                )
                self.circuit.record_failure(error)
                raise error from exc

            self.circuit.record_success()
            usage_decision = guard.record_usage(
                input_tokens=result.usage.input_tokens,
                output_tokens=result.usage.output_tokens,
                estimated_cost_cny=result.estimated_cost_cny,
            )
            if not usage_decision.allowed:
                raise self._policy_error(usage_decision.stop_reason)
            warnings = list(result.warnings)
            if retry_count:
                warnings.append("provider_retry_applied")
            if repair_used:
                warnings.append("json_repair_applied")
            if usage_decision.warning:
                usage_warning = usage_decision.warning
            if usage_warning:
                warnings.append(usage_warning)
            elapsed_ms = max(
                result.latency_ms, (self.clock() - gateway_started) * 1000
            )
            return replace(
                result,
                attempt_count=attempt_count,
                latency_ms=elapsed_ms,
                warnings=tuple(dict.fromkeys(warnings)),
            )

    def task_usage(
        self, request_id: str, *, start_index: int = 0
    ) -> dict[str, object]:
        if self.usage_ledger is None:
            return {}
        return self.usage_ledger.summary(
            request_id=request_id, start_index=start_index
        )

    def _validate_guard(self, guard: LoopGuard) -> None:
        if (
            guard.policy.policy_id != self.policy.policy_id
            or guard.policy.version != self.policy.version
            or guard.policy.termination_precedence
            != self.policy.termination_precedence
        ):
            raise ProviderError(
                ProviderErrorCode.CONFIGURATION,
                "LoopGuard policy does not match the gateway policy",
                retryable=False,
            )
        narrowable_limits = {
            "max_model_calls",
            "max_input_tokens_total",
            "max_output_tokens_total",
            "max_wall_time_ms",
            "hard_cost_cny",
        }
        if set(guard.policy.limits) != set(self.policy.limits):
            raise ProviderError(
                ProviderErrorCode.CONFIGURATION,
                "LoopGuard policy limits do not match the gateway policy",
                retryable=False,
            )
        for name, gateway_limit in self.policy.limits.items():
            guard_limit = guard.policy.limits[name]
            if name in narrowable_limits:
                if float(guard_limit) <= 0 or float(guard_limit) > float(gateway_limit):
                    raise ProviderError(
                        ProviderErrorCode.CONFIGURATION,
                        "LoopGuard cannot loosen gateway hard limits",
                        retryable=False,
                    )
            elif guard_limit != gateway_limit:
                raise ProviderError(
                    ProviderErrorCode.CONFIGURATION,
                    "LoopGuard may only narrow approved hard limits",
                    retryable=False,
                )

    def _can_retry(self, error: ProviderError, retry_count: int) -> bool:
        return (
            error.retryable
            and error.code in RETRYABLE_CODES
            and retry_count < self.config.max_provider_retries
        )

    def _retry_delay(self, error: ProviderError, retry_count: int) -> float:
        if error.retry_after_seconds is not None:
            requested = error.retry_after_seconds
        else:
            requested = self.config.retry_base_delay_seconds * (2 ** (retry_count - 1))
        return min(requested, self.config.retry_max_delay_seconds)

    def _sleep_within_wall_limit(
        self, guard: LoopGuard, delay_seconds: float
    ) -> None:
        snapshot = guard.snapshot()
        remaining_ms = int(guard.policy.limits["max_wall_time_ms"]) - int(
            snapshot["elapsed_ms"]
        )
        delay_ms = int(delay_seconds * 1000)
        if delay_ms >= remaining_ms:
            guard.terminate("wall_time_limit")
            raise self._policy_error("wall_time_limit")
        self.sleeper(delay_seconds)

    @staticmethod
    def _failure_usage_limit_reached(guard: LoopGuard) -> str | None:
        """Stop retries when a failed response exactly consumes a hard limit."""

        snapshot = guard.snapshot()
        limits = guard.policy.limits
        if guard.estimated_cost_cny >= float(limits["hard_cost_cny"]):
            return "hard_cost_limit"
        if int(snapshot["elapsed_ms"]) >= int(limits["max_wall_time_ms"]):
            return "wall_time_limit"
        if (
            guard.input_tokens_total
            >= int(limits["max_input_tokens_total"])
            or guard.output_tokens_total
            >= int(limits["max_output_tokens_total"])
        ):
            return "token_limit"
        return None

    @staticmethod
    def _repair_request(request: ModelCallRequest) -> ModelCallRequest:
        instruction = ModelMessage("system", JSON_REPAIR_INSTRUCTION)
        return replace(request, messages=(instruction, *request.messages))

    @staticmethod
    def _policy_error(reason: str | None) -> ProviderError:
        return ProviderError(
            ProviderErrorCode.POLICY_LIMIT,
            f"Loop policy stopped the request: {reason or 'unknown_limit'}",
            retryable=False,
        )


__all__ = [
    "CircuitBreaker",
    "CircuitState",
    "LLMGateway",
    "ReliabilityConfig",
]
