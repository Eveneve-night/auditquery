from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from backend.agent.loop_guard import LoopGuard, LoopPolicy  # noqa: E402


POLICY_PATH = PROJECT_ROOT / "configs" / "policies" / "agent-loop-limits.v1.json"
ATTACKS_PATH = PROJECT_ROOT / "tests" / "fixtures" / "product" / "loop-attacks.jsonl"


class LoopGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = LoopPolicy.from_file(POLICY_PATH)

    def test_normal_sequence_is_allowed(self) -> None:
        guard = LoopGuard(self.policy)
        self.assertTrue(guard.record("state_transition", action="route").allowed)
        self.assertTrue(guard.record("model_call", action="intent", input_tokens=500).allowed)
        self.assertTrue(
            guard.record(
                "tool_call",
                action="schema_search",
                tool_name="search_schema",
                arguments={"q": "customer"},
            ).allowed
        )

    def test_soft_cost_warns_but_does_not_stop(self) -> None:
        decision = LoopGuard(self.policy).record(
            "model_call", action="answer", estimated_cost_cny=0.31
        )
        self.assertTrue(decision.allowed)
        self.assertEqual("soft_cost_limit_exceeded", decision.warning)

    def test_hard_limits(self) -> None:
        cases = [
            ("wall_time_limit", {"elapsed_ms": 30001}),
            ("token_limit", {"input_tokens": 30001}),
            ("token_limit", {"output_tokens": 5001}),
            ("hard_cost_limit", {"estimated_cost_cny": 1.01}),
            ("security_violation", {"security_violation": True}),
        ]
        for reason, kwargs in cases:
            with self.subTest(reason=reason, kwargs=kwargs):
                decision = LoopGuard(self.policy).record(
                    "model_call", action="one", **kwargs
                )
                self.assertFalse(decision.allowed)
                self.assertEqual(reason, decision.stop_reason)

    def test_provider_retry_is_scoped_per_call(self) -> None:
        guard = LoopGuard(self.policy)
        for scope in ("call-a", "call-b"):
            self.assertTrue(
                guard.record("provider_retry", action=f"{scope}-1", scope_id=scope).allowed
            )
            self.assertTrue(
                guard.record("provider_retry", action=f"{scope}-2", scope_id=scope).allowed
            )
        decision = guard.record("provider_retry", action="call-a-3", scope_id="call-a")
        self.assertFalse(decision.allowed)
        self.assertEqual("specialized_attempt_limit", decision.stop_reason)

    def test_stopped_guard_does_not_mutate(self) -> None:
        guard = LoopGuard(self.policy)
        first = guard.record("model_call", security_violation=True)
        snapshot = first.snapshot
        second = guard.record("model_call", input_tokens=999)
        self.assertFalse(second.allowed)
        self.assertEqual(snapshot["counts"], second.snapshot["counts"])
        self.assertEqual(snapshot["input_tokens_total"], second.snapshot["input_tokens_total"])

    def test_usage_can_be_added_without_counting_another_model_call(self) -> None:
        guard = LoopGuard(self.policy)
        self.assertTrue(guard.record("model_call").allowed)
        decision = guard.record_usage(
            input_tokens=120,
            output_tokens=30,
            estimated_cost_cny=0.01,
        )
        self.assertTrue(decision.allowed)
        self.assertEqual(1, decision.snapshot["counts"]["model_call"])
        self.assertEqual(120, decision.snapshot["input_tokens_total"])

    def test_usage_hard_limit_and_manual_termination_stop_the_guard(self) -> None:
        guard = LoopGuard(self.policy)
        decision = guard.record_usage(estimated_cost_cny=1.01)
        self.assertFalse(decision.allowed)
        self.assertEqual("hard_cost_limit", decision.stop_reason)

        other = LoopGuard(self.policy)
        terminated = other.terminate("wall_time_limit")
        self.assertFalse(terminated.allowed)
        self.assertEqual("wall_time_limit", other.snapshot()["stopped_reason"])

    def test_invalid_event_and_negative_usage_are_rejected(self) -> None:
        guard = LoopGuard(self.policy)
        with self.assertRaises(ValueError):
            guard.record("unknown")
        with self.assertRaises(ValueError):
            guard.record("model_call", input_tokens=-1)
        with self.assertRaises(ValueError):
            guard.record("tool_call", arguments={})
        self.assertEqual({}, guard.snapshot()["counts"])

    def test_attack_corpus(self) -> None:
        cases = [json.loads(line) for line in ATTACKS_PATH.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(12, len(cases))
        for case in cases:
            with self.subTest(case=case["id"]):
                guard = LoopGuard(self.policy)
                decision = None
                events = case.get("events")
                if events is None:
                    repeat = case["repeat"]
                    events = []
                    for index in range(1, repeat["count"] + 1):
                        event = {
                            "event_type": repeat["event_type"],
                            "action": repeat.get("action_template", "{index}").format(index=index),
                        }
                        if "scope_id" in repeat:
                            event["scope_id"] = repeat["scope_id"]
                        if "tool_name_template" in repeat:
                            event["tool_name"] = repeat["tool_name_template"].format(index=index)
                        if "arguments_template" in repeat:
                            event["arguments"] = {
                                key: value.format(index=index) if isinstance(value, str) else value
                                for key, value in repeat["arguments_template"].items()
                            }
                        events.append(event)
                for event in events:
                    event = dict(event)
                    event_type = event.pop("event_type")
                    decision = guard.record(event_type, **event)
                    if not decision.allowed:
                        break
                self.assertIsNotNone(decision)
                self.assertFalse(decision.allowed)
                self.assertEqual(case["expected_stop_reason"], decision.stop_reason)


if __name__ == "__main__":
    unittest.main()
