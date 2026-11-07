"""B-owned D4-07 clarification v2 runtime-gate regression tests."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from backend.agent.clarification_recovery import (  # noqa: E402
    ClarificationOption,
    ClarificationQuestion,
)
from backend.api.online_contracts import (  # noqa: E402
    OnlineContractError,
    OnlineContractValidator,
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


def binding(
    *,
    session_id: str = "session.public-001",
    request_id: str = "request.public-001",
) -> OnlineSessionBinding:
    pending = EXAMPLES["pending_question"]
    question = ClarificationQuestion(
        question_id=pending["question_id"],
        session_id=session_id,
        request_id=request_id,
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
        session_id=session_id,
        request_id=request_id,
        turn_id=pending["turn_id"],
        state_revision=pending["state_revision"],
        pending=PendingClarificationBinding.from_question(question),
    )


class OnlineRequestGuardV2Test(unittest.TestCase):
    def setUp(self) -> None:
        self.guard = OnlineRequestGuard()
        self.session = binding()
        self.guard.register(self.session)

    def assert_code(self, expected: str, request: dict) -> None:
        with self.assertRaises(OnlineContractError) as captured:
            self.guard.validate_and_consume(request)
        self.assertEqual(expected, captured.exception.code)

    def test_projects_server_question_to_complete_pending_payload(self) -> None:
        self.assertEqual(
            EXAMPLES["pending_question"], self.session.pending_question_payload()
        )

    def test_projected_question_cannot_be_rebound_to_another_session(self) -> None:
        projected = binding()
        projected.session_id = "session.other"
        guard = OnlineRequestGuard()
        with self.assertRaises(OnlineContractError) as captured:
            guard.register(projected)
        self.assertEqual("invalid_session_binding", captured.exception.code)

    def test_projection_rejects_duplicate_or_missing_server_options(self) -> None:
        session = binding()
        pending = session.pending
        self.assertIsNotNone(pending)
        question = ClarificationQuestion(
            question_id=pending.question_id,
            session_id=session.session_id,
            request_id=session.request_id,
            turn_id=session.turn_id,
            state_revision=session.state_revision,
            slot_id=pending.slot_id,
            issue="missing",
            prompt=pending.question_text,
            options=(),
            evidence_refs=("rule.public_time_required",),
            information_gain={"expected_information_gain": 1.0},
            baseline_slot_id=pending.slot_id,
        )
        with self.assertRaises(OnlineContractError) as captured:
            PendingClarificationBinding.from_question(question)
        self.assertEqual("invalid_pending_clarification", captured.exception.code)

    def test_all_published_runtime_mutations_use_frozen_error_codes(self) -> None:
        base = EXAMPLES["accepted_request"]
        for case in EXAMPLES["runtime_rejected_mutations"]:
            if case["case_id"] == "answer_replayed":
                continue
            with self.subTest(case=case["case_id"]):
                guard = OnlineRequestGuard()
                guard.register(binding())
                request = deepcopy(base)
                request[case["field"]] = case["replacement"]
                with self.assertRaises(OnlineContractError) as captured:
                    guard.validate_and_consume(request)
                self.assertEqual(case["error_code"], captured.exception.code)

    def test_value_and_evidence_must_be_the_same_offered_pair(self) -> None:
        request = deepcopy(EXAMPLES["accepted_request"])
        request["values"] = ["2012"]
        request["evidence_refs"] = ["value_evidence.public_year_2011"]
        self.assert_code("invalid_answer_evidence_refs", request)

    def test_answer_and_question_are_consumed_once(self) -> None:
        accepted = deepcopy(EXAMPLES["accepted_request"])
        self.guard.validate_and_consume(accepted)
        self.assert_code("clarification_answer_replayed", accepted)

    def test_failed_downstream_redecision_does_not_consume_answer(self) -> None:
        accepted = deepcopy(EXAMPLES["accepted_request"])

        def fail_redecision(*_):
            raise RuntimeError("offline redecision failed")

        with self.assertRaisesRegex(RuntimeError, "redecision failed"):
            self.guard.apply_atomically(accepted, fail_redecision)
        request, session, result = self.guard.apply_atomically(
            accepted, lambda *_: "committed"
        )
        self.assertEqual("committed", result)
        self.assertIsNot(session, self.session)
        self.assertEqual(session, self.session)
        self.assertEqual(accepted, request)

    def test_reused_answer_id_or_question_id_is_rejected_across_sessions(self) -> None:
        first = deepcopy(EXAMPLES["accepted_request"])
        self.guard.validate_and_consume(first)

        second_session = binding(
            session_id="session.public-002", request_id="request.public-002"
        )
        self.guard.register(second_session)
        second = deepcopy(first)
        second["session_id"] = second_session.session_id
        second["request_id"] = second_session.request_id
        self.assert_code("clarification_answer_replayed", second)

    def test_v2_query_is_supported_without_pending_question(self) -> None:
        guard = OnlineRequestGuard()
        guard.register(OnlineSessionBinding("session.query", "request.query", 0, 0))
        query = {
            "schema_version": "2.0.0",
            "operation": "submit_query",
            "session_id": "session.query",
            "request_id": "request.query",
            "turn_id": 0,
            "state_revision": 0,
            "query": "统计艺术家数量",
        }
        guard.validate_and_consume(query)
        with self.assertRaises(OnlineContractError) as captured:
            guard.validate_and_consume(query)
        self.assertEqual("request_replayed", captured.exception.code)


class OnlineResponseV2SemanticTest(unittest.TestCase):
    def setUp(self) -> None:
        self.validator = OnlineContractValidator()
        self.value = json.loads(
            (
                PROJECT_ROOT
                / "configs"
                / "examples"
                / "d4-07"
                / "online-clarification-required.v2.valid.json"
            ).read_text(encoding="utf-8")
        )

    def assert_code(self, expected: str, value: dict) -> None:
        with self.assertRaises(OnlineContractError) as captured:
            self.validator.validate_response(value)
        self.assertEqual(expected, captured.exception.code)

    def test_valid_v2_clarification_response_passes_runtime_semantics(self) -> None:
        self.validator.validate_response(self.value)

    def test_pending_question_must_match_response_binding(self) -> None:
        value = deepcopy(self.value)
        value["pending_question"]["request_id"] = "request.other"
        self.assert_code("pending_question_binding_mismatch", value)

    def test_pending_options_require_unique_values(self) -> None:
        value = deepcopy(self.value)
        value["pending_question"]["options"][1]["value"] = value[
            "pending_question"
        ]["options"][0]["value"]
        self.assert_code("pending_question_option_conflict", value)

    def test_distinct_values_may_share_one_domain_evidence_record(self) -> None:
        value = deepcopy(self.value)
        value["pending_question"]["options"][1]["evidence_ref"] = value[
            "pending_question"
        ]["options"][0]["evidence_ref"]
        self.validator.validate_response(value)


if __name__ == "__main__":
    unittest.main()
