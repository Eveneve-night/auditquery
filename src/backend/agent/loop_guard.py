"""Deterministic limits that stop repeated agent/model/tool loops.

This module is deliberately provider-neutral and uses only the Python standard
library. It never reads API credentials and performs no network calls.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import time
from typing import Any, Callable


EVENT_LIMITS: dict[str, tuple[str, str]] = {
    "state_transition": ("max_state_transitions", "state_transition_limit"),
    "model_call": ("max_model_calls", "model_call_limit"),
    "tool_call": ("max_tool_calls", "tool_call_limit"),
    "clarification": ("max_clarification_rounds", "specialized_attempt_limit"),
    "json_repair": ("max_json_repair_attempts", "specialized_attempt_limit"),
    "provider_retry": ("max_provider_retries_per_call", "specialized_attempt_limit"),
    "provider_fallback": ("max_provider_fallbacks", "specialized_attempt_limit"),
    "sql_generation": ("max_sql_generation_attempts", "specialized_attempt_limit"),
    "sql_execution": ("max_sql_execution_attempts", "specialized_attempt_limit"),
    "retrieval_round": ("max_retrieval_rounds", "specialized_attempt_limit"),
}


@dataclass(frozen=True)
class LoopPolicy:
    policy_id: str
    version: str
    limits: dict[str, int | float]
    termination_precedence: tuple[str, ...]

    @classmethod
    def from_file(cls, path: str | Path) -> "LoopPolicy":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(
            policy_id=payload["policy_id"],
            version=payload["version"],
            limits=payload["limits"],
            termination_precedence=tuple(payload["termination_precedence"]),
        )


@dataclass(frozen=True)
class GuardDecision:
    allowed: bool
    stop_reason: str | None = None
    warning: str | None = None
    snapshot: dict[str, Any] = field(default_factory=dict)


class LoopGuard:
    """Tracks one agent request and stops it when the policy is exceeded."""

    def __init__(
        self,
        policy: LoopPolicy,
        *,
        start_time: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.policy = policy
        self.clock = clock
        self.started_at = clock() if start_time is None else start_time
        self.counts: dict[str, int] = {}
        self.scoped_counts: dict[str, int] = {}
        self.tool_signatures: dict[str, int] = {}
        self.last_action: str | None = None
        self.consecutive_action_count = 0
        self.input_tokens_total = 0
        self.output_tokens_total = 0
        self.estimated_cost_cny = 0.0
        self.stopped_reason: str | None = None

    @staticmethod
    def tool_signature(tool_name: str, arguments: Any) -> str:
        canonical = json.dumps(
            arguments,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return f"{tool_name}:{digest}"

    def record(
        self,
        event_type: str,
        *,
        action: str | None = None,
        tool_name: str | None = None,
        arguments: Any = None,
        scope_id: str | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        estimated_cost_cny: float = 0.0,
        elapsed_ms: int | None = None,
        security_violation: bool = False,
    ) -> GuardDecision:
        if self.stopped_reason is not None:
            return GuardDecision(
                allowed=False,
                stop_reason=self.stopped_reason,
                snapshot=self.snapshot(elapsed_ms=elapsed_ms),
            )
        if event_type not in EVENT_LIMITS:
            raise ValueError(f"Unsupported loop-guard event: {event_type}")
        if min(input_tokens, output_tokens) < 0 or estimated_cost_cny < 0:
            raise ValueError("Token and cost increments must be non-negative")
        if event_type == "tool_call" and not tool_name:
            raise ValueError("tool_name is required for tool_call")

        self.counts[event_type] = self.counts.get(event_type, 0) + 1
        scoped_key = f"{event_type}:{scope_id or 'global'}"
        self.scoped_counts[scoped_key] = self.scoped_counts.get(scoped_key, 0) + 1
        self.input_tokens_total += input_tokens
        self.output_tokens_total += output_tokens
        self.estimated_cost_cny += estimated_cost_cny

        current_action = action or event_type
        if current_action == self.last_action:
            self.consecutive_action_count += 1
        else:
            self.last_action = current_action
            self.consecutive_action_count = 1

        if event_type == "tool_call":
            assert tool_name is not None
            signature = self.tool_signature(tool_name, arguments)
            self.tool_signatures[signature] = self.tool_signatures.get(signature, 0) + 1

        violations = self._violations(
            event_type=event_type,
            scoped_key=scoped_key,
            elapsed_ms=elapsed_ms,
            security_violation=security_violation,
        )
        stop_reason = self._select_reason(violations)
        warning = None
        if self.estimated_cost_cny > float(self.policy.limits["soft_cost_cny"]):
            warning = "soft_cost_limit_exceeded"
        if stop_reason is not None:
            self.stopped_reason = stop_reason
        return GuardDecision(
            allowed=stop_reason is None,
            stop_reason=stop_reason,
            warning=warning,
            snapshot=self.snapshot(elapsed_ms=elapsed_ms),
        )

    def _violations(
        self,
        *,
        event_type: str,
        scoped_key: str,
        elapsed_ms: int | None,
        security_violation: bool,
    ) -> set[str]:
        limits = self.policy.limits
        violations = self._global_violations(
            elapsed_ms=elapsed_ms, security_violation=security_violation
        )
        if any(
            count > int(limits["max_identical_tool_signature_total"])
            for count in self.tool_signatures.values()
        ):
            violations.add("identical_tool_loop")
        if self.consecutive_action_count > int(limits["max_consecutive_same_action"]):
            violations.add("consecutive_action_loop")

        limit_key, reason = EVENT_LIMITS[event_type]
        count = (
            self.scoped_counts[scoped_key]
            if event_type == "provider_retry"
            else self.counts[event_type]
        )
        if count > int(limits[limit_key]):
            violations.add(reason)
        return violations

    def _global_violations(
        self, *, elapsed_ms: int | None, security_violation: bool = False
    ) -> set[str]:
        limits = self.policy.limits
        violations: set[str] = set()
        if security_violation:
            violations.add("security_violation")
        if self.estimated_cost_cny > float(limits["hard_cost_cny"]):
            violations.add("hard_cost_limit")
        effective_elapsed = self._elapsed_ms() if elapsed_ms is None else elapsed_ms
        if effective_elapsed > int(limits["max_wall_time_ms"]):
            violations.add("wall_time_limit")
        if (
            self.input_tokens_total > int(limits["max_input_tokens_total"])
            or self.output_tokens_total > int(limits["max_output_tokens_total"])
        ):
            violations.add("token_limit")
        return violations

    def record_usage(
        self,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        estimated_cost_cny: float = 0.0,
        elapsed_ms: int | None = None,
    ) -> GuardDecision:
        """Add usage without counting a second model call."""

        if self.stopped_reason is not None:
            return GuardDecision(
                allowed=False,
                stop_reason=self.stopped_reason,
                snapshot=self.snapshot(elapsed_ms=elapsed_ms),
            )
        if min(input_tokens, output_tokens) < 0 or estimated_cost_cny < 0:
            raise ValueError("Token and cost increments must be non-negative")
        self.input_tokens_total += input_tokens
        self.output_tokens_total += output_tokens
        self.estimated_cost_cny += estimated_cost_cny
        stop_reason = self._select_reason(
            self._global_violations(elapsed_ms=elapsed_ms)
        )
        warning = None
        if self.estimated_cost_cny > float(self.policy.limits["soft_cost_cny"]):
            warning = "soft_cost_limit_exceeded"
        if stop_reason is not None:
            self.stopped_reason = stop_reason
        return GuardDecision(
            allowed=stop_reason is None,
            stop_reason=stop_reason,
            warning=warning,
            snapshot=self.snapshot(elapsed_ms=elapsed_ms),
        )

    def terminate(
        self, reason: str, *, elapsed_ms: int | None = None
    ) -> GuardDecision:
        """Stop the current task without fabricating another counted event."""

        if not reason.strip():
            raise ValueError("A stop reason is required")
        if self.stopped_reason is None:
            self.stopped_reason = reason
        return GuardDecision(
            allowed=False,
            stop_reason=self.stopped_reason,
            snapshot=self.snapshot(elapsed_ms=elapsed_ms),
        )

    def _select_reason(self, violations: set[str]) -> str | None:
        for reason in self.policy.termination_precedence:
            if reason in violations:
                return reason
        return sorted(violations)[0] if violations else None

    def _elapsed_ms(self) -> int:
        return int((self.clock() - self.started_at) * 1000)

    def snapshot(self, *, elapsed_ms: int | None = None) -> dict[str, Any]:
        return {
            "policy_id": self.policy.policy_id,
            "policy_version": self.policy.version,
            "counts": dict(self.counts),
            "scoped_counts": dict(self.scoped_counts),
            "input_tokens_total": self.input_tokens_total,
            "output_tokens_total": self.output_tokens_total,
            "estimated_cost_cny": round(self.estimated_cost_cny, 6),
            "elapsed_ms": self._elapsed_ms() if elapsed_ms is None else elapsed_ms,
            "stopped_reason": self.stopped_reason,
        }
