"""D4-07-A2 authoritative session transaction and alias-isolation tests."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import sys
from threading import Event
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from backend.agent.clarification_recovery import (  # noqa: E402
    ClarificationOption,
    ClarificationQuestion,
)
from backend.api.online_contracts import (  # noqa: E402
    OnlineContractError,
    OnlineRequestGuard,
    OnlineSessionBinding,
    PendingClarificationBinding,
)


EXAMPLES = json.loads(
    (
        PROJECT_ROOT
        / "configs"
        / "examples"
        / "d4-07"
        / "clarification-binding.v2.examples.json"
    ).read_text(encoding="utf-8")
)


def _query_request() -> dict:
    return {
        "schema_version": "2.0.0",
        "operation": "submit_query",
        "session_id": "session.atomic-query",
        "request_id": "request.atomic-query",
        "turn_id": 0,
        "state_revision": 0,
        "query": "统计公开示例记录数",
    }


def _clarification_session() -> OnlineSessionBinding:
    pending = EXAMPLES["pending_question"]
    question = ClarificationQuestion(
        question_id=pending["question_id"],
        session_id=pending["session_id"],
        request_id=pending["request_id"],
        turn_id=pending["turn_id"],
        state_revision=pending["state_revision"],
        slot_id=pending["slot_id"],
        issue="missing",
        prompt=pending["question_text"],
        options=tuple(
            ClarificationOption(
                value=item["value"],
                display_name=item["display_name"],
                evidence_ref=item["evidence_ref"],
            )
            for item in pending["options"]
        ),
        evidence_refs=("rule.public_time_required",),
        information_gain={"expected_information_gain": 1.0},
        baseline_slot_id=pending["slot_id"],
    )
    return OnlineSessionBinding(
        session_id=pending["session_id"],
        request_id=pending["request_id"],
        turn_id=pending["turn_id"],
        state_revision=pending["state_revision"],
        pending=PendingClarificationBinding.from_question(question),
    )


def _state(guard: OnlineRequestGuard, session: OnlineSessionBinding) -> tuple:
    return deepcopy(
        (
            session.session_id,
            session.request_id,
            session.turn_id,
            session.state_revision,
            session.pending,
            session.consumed_operations,
            guard._consumed_answer_ids,
            guard._consumed_question_ids,
        )
    )


class _ThirdDeepcopyFailsStr(str):
    """合法 str 子类：模拟提交后生成返回快照时复制失败。"""

    calls = 0
    sentinel: RuntimeError

    def __deepcopy__(self, _memo):
        type(self).calls += 1
        if type(self).calls == 3:
            raise type(self).sentinel
        return type(self)(self)


class OnlineRequestAtomicityTest(unittest.TestCase):
    def test_query_mutate_then_raise_rolls_back_and_retries(self) -> None:
        guard = OnlineRequestGuard()
        authoritative = OnlineSessionBinding(
            "session.atomic-query", "request.atomic-query", 0, 0
        )
        guard.register(authoritative)
        request = _query_request()
        before = _state(guard, authoritative)
        sentinel = RuntimeError("query consumer failed after mutation")

        def fail_after_mutation(_request, working):
            working.state_revision = 99
            working.pending = PendingClarificationBinding("question.fake", "slot.fake")
            working.consumed_operations.add(("forged", 99, 99))
            raise sentinel

        with self.assertRaises(RuntimeError) as captured:
            guard.apply_atomically(request, fail_after_mutation)
        self.assertIs(sentinel, captured.exception)
        self.assertEqual(before, _state(guard, authoritative))

        _, returned, result = guard.apply_atomically(
            request, lambda *_: "retry-success"
        )
        self.assertEqual("retry-success", result)
        self.assertIsNot(authoritative, returned)
        with self.assertRaises(OnlineContractError) as replayed:
            guard.validate_and_consume(request)
        self.assertEqual("request_replayed", replayed.exception.code)

    def test_clarification_failure_preserves_every_authoritative_field(self) -> None:
        guard = OnlineRequestGuard()
        authoritative = _clarification_session()
        guard.register(authoritative)
        request = deepcopy(EXAMPLES["accepted_request"])
        before = _state(guard, authoritative)
        sentinel = LookupError("clarification consumer failed after mutation")

        def fail_after_mutation(_request, working):
            working.session_id = "session.polluted"
            working.request_id = "request.polluted"
            working.turn_id = 999
            working.state_revision = 999
            working.pending = None
            working.consumed_operations.add(("forged", 999, 999))
            raise sentinel

        with self.assertRaises(LookupError) as captured:
            guard.apply_atomically(request, fail_after_mutation)
        self.assertIs(sentinel, captured.exception)
        self.assertEqual(before, _state(guard, authoritative))

        def commit_expected_changes(_request, working):
            working.pending = None
            working.state_revision += 1
            return "clarification-committed"

        _, returned, result = guard.apply_atomically(request, commit_expected_changes)
        self.assertEqual("clarification-committed", result)
        self.assertIsNone(authoritative.pending)
        self.assertEqual(before[3] + 1, authoritative.state_revision)
        self.assertEqual(authoritative, returned)
        self.assertIsNot(authoritative, returned)
        self.assertEqual(1, len(authoritative.consumed_operations))
        self.assertEqual({request["answer_id"]}, guard._consumed_answer_ids)
        self.assertEqual({request["question_id"]}, guard._consumed_question_ids)
        with self.assertRaises(OnlineContractError) as replayed:
            guard.validate_and_consume(request)
        self.assertEqual("clarification_answer_replayed", replayed.exception.code)

    def test_working_result_and_returned_snapshot_cannot_mutate_authority(self) -> None:
        guard = OnlineRequestGuard()
        authoritative = _clarification_session()
        guard.register(authoritative)
        request = deepcopy(EXAMPLES["accepted_request"])
        original_text = authoritative.pending.question_text
        original_label = authoritative.pending.options[0].display_name

        def leak_working_copy(_request, working):
            return {
                "working": working,
                "pending": working.pending,
                "options": working.pending.options,
            }

        _, returned, result = guard.apply_atomically(request, leak_working_copy)
        leaked = result["working"]
        leaked.state_revision = 900
        leaked.consumed_operations.clear()
        object.__setattr__(result["pending"], "question_text", "polluted")
        object.__setattr__(result["options"][0], "display_name", "polluted")
        returned.state_revision = 901
        returned.consumed_operations.clear()
        object.__setattr__(returned.pending, "question_text", "returned-polluted")

        self.assertNotEqual(900, authoritative.state_revision)
        self.assertNotEqual(901, authoritative.state_revision)
        self.assertEqual(1, len(authoritative.consumed_operations))
        self.assertEqual(original_text, authoritative.pending.question_text)
        self.assertEqual(original_label, authoritative.pending.options[0].display_name)

    def test_success_rejects_consumer_changes_to_guard_owned_state(self) -> None:
        guard = OnlineRequestGuard()
        authoritative = OnlineSessionBinding(
            "session.atomic-query", "request.atomic-query", 0, 0
        )
        guard.register(authoritative)
        before = _state(guard, authoritative)

        def forge_replay_state(_request, working):
            working.consumed_operations.add(("forged", 7, 7))
            return "must-not-commit"

        with self.assertRaises(OnlineContractError) as captured:
            guard.apply_atomically(_query_request(), forge_replay_state)
        self.assertEqual("invalid_session_binding", captured.exception.code)
        self.assertEqual(before, _state(guard, authoritative))

    def test_return_snapshot_copy_failure_does_not_commit(self) -> None:
        guard = OnlineRequestGuard()
        authoritative = OnlineSessionBinding(
            "session.atomic-query", "request.atomic-query", 0, 0
        )
        guard.register(authoritative)
        request = _query_request()
        before = _state(guard, authoritative)
        sentinel = RuntimeError("return snapshot copy failed")
        _ThirdDeepcopyFailsStr.calls = 0
        _ThirdDeepcopyFailsStr.sentinel = sentinel

        def fail_while_preparing_return(_request, working):
            working.request_id = _ThirdDeepcopyFailsStr("request.next")
            return "must-not-commit"

        with self.assertRaises(RuntimeError) as captured:
            guard.apply_atomically(request, fail_while_preparing_return)
        self.assertIs(sentinel, captured.exception)
        self.assertEqual(3, _ThirdDeepcopyFailsStr.calls)
        self.assertEqual(before, _state(guard, authoritative))

        _, _, result = guard.apply_atomically(request, lambda *_: "retry-success")
        self.assertEqual("retry-success", result)

    def test_rlock_serializes_same_request_to_one_success(self) -> None:
        guard = OnlineRequestGuard()
        guard.register(
            OnlineSessionBinding(
                "session.atomic-query", "request.atomic-query", 0, 0
            )
        )
        request = _query_request()
        entered = Event()
        release = Event()

        def first_consumer(*_):
            entered.set()
            self.assertTrue(release.wait(timeout=5))
            return "first-success"

        def second_call():
            return guard.apply_atomically(request, lambda *_: "second-success")

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(guard.apply_atomically, request, first_consumer)
            self.assertTrue(entered.wait(timeout=5))
            second = pool.submit(second_call)
            release.set()
            self.assertEqual("first-success", first.result(timeout=5)[2])
            with self.assertRaises(OnlineContractError) as captured:
                second.result(timeout=5)
        self.assertEqual("request_replayed", captured.exception.code)


if __name__ == "__main__":
    unittest.main()
