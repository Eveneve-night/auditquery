"""D4-07 online contract registry and state-binding regression tests."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from backend.api.online_contracts import (  # noqa: E402
    OnlineContractError,
    OnlineContractValidator,
    OnlineRequestGuard,
    OnlineSessionBinding,
    PendingClarificationBinding,
)


def query_request(**changes):
    value = {
        "schema_version": "1.0.0",
        "operation": "submit_query",
        "session_id": "session.1",
        "request_id": "request.1",
        "turn_id": "turn.1",
        "state_revision": 0,
        "query": "统计艺术家数量",
    }
    value.update(changes)
    return value


def clarification_request(**changes):
    value = {
        "schema_version": "1.0.0",
        "operation": "submit_clarification",
        "session_id": "session.1",
        "request_id": "request.1",
        "turn_id": "turn.2",
        "state_revision": 1,
        "question_id": "question.1",
        "slot_id": "slot.entity",
        "values": ["artist"],
        "evidence_refs": ["evidence.entity_kinds"],
    }
    value.update(changes)
    return value


def decision(action="accept"):
    return {
        "contract_version": "1.0.0",
        "action": action,
        "reason_code": "query_resolved",
        "score_kind": "uncalibrated_raw_score",
        "raw_score": 0.9,
        "calibration_version": None,
        "thresholds": {
            "accept_min": 0.8,
            "clarify_min": 0.4,
            "abstain_below": 0.4,
            "retrieval_min_score": None,
        },
        "evidence_refs": ["evidence.entity_kinds"],
        "missing_or_conflicting_slots": [],
        "accept_authorizes_sql_execution": False,
        "requires_d4_02_safety_gate": True,
    }


def response(**changes):
    value = {
        "schema_version": "1.0.0",
        "session_id": "session.1",
        "request_id": "request.1",
        "turn_id": "turn.2",
        "state_revision": 2,
        "route": "sql",
        "status": "success",
        "message": "只读查询已完成。",
        "decision": decision(),
        "pending_question": None,
        "candidate_evidence": [],
        "sql": {
            "proposal_id": "proposal.1",
            "candidate_sql": "SELECT COUNT(*) AS artist_count FROM Artist",
            "tables": ["Artist"],
            "safety_status": "approved",
            "execution_attempts": 1,
            "columns": ["artist_count"],
            "rows": [[275]],
            "row_count": 1,
            "truncated": False,
        },
        "citations": [],
        "trace": [],
        "usage": {
            "model_calls": 1,
            "input_tokens": 50,
            "output_tokens": 20,
            "estimated_cost_cny": 0.001,
            "elapsed_ms": 25,
            "stopped_reason": None,
        },
        "error": None,
        "warnings": ["uncalibrated_raw_score"],
    }
    value.update(changes)
    return value


class OnlineRequestGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.guard = OnlineRequestGuard()

    def assert_code(self, expected, request):
        with self.assertRaises(OnlineContractError) as captured:
            self.guard.validate_and_consume(request)
        self.assertEqual(expected, captured.exception.code)

    def test_query_is_schema_validated_bound_and_replay_protected(self):
        self.guard.register(OnlineSessionBinding("session.1", "request.1", "turn.1", 0))
        accepted, _ = self.guard.validate_and_consume(query_request())
        self.assertEqual("submit_query", accepted["operation"])
        self.assert_code("request_replayed", query_request())

    def test_unknown_fields_and_answer_derived_fields_fail_closed(self):
        self.guard.register(OnlineSessionBinding("session.1", "request.1", "turn.1", 0))
        for field in ("sql", "model", "expected", "gold", "label"):
            with self.subTest(field=field):
                self.assert_code("invalid_online_request", query_request(**{field: "x"}))

    def test_cross_session_request_turn_and_stale_revision_are_rejected(self):
        self.guard.register(OnlineSessionBinding("session.1", "request.1", "turn.1", 3))
        probes = (
            ("session_not_found", {"session_id": "session.2", "state_revision": 3}),
            ("request_binding_mismatch", {"request_id": "request.2", "state_revision": 3}),
            ("turn_binding_mismatch", {"turn_id": "turn.2", "state_revision": 3}),
            ("stale_state_revision", {"state_revision": 2}),
        )
        for code, changes in probes:
            with self.subTest(code=code):
                self.assert_code(code, query_request(**changes))

    def test_v1_clarification_requires_contract_upgrade(self):
        pending = PendingClarificationBinding(
            question_id="question.1",
            slot_id="slot.entity",
            allowed_values=frozenset({"artist", "album"}),
            allowed_evidence_refs=frozenset({"evidence.entity_kinds"}),
        )
        self.guard.register(
            OnlineSessionBinding("session.1", "request.1", "turn.2", 1, pending)
        )
        self.assert_code(
            "clarification_contract_upgrade_required", clarification_request()
        )

    def test_committed_consumer_examples_match_the_concrete_request_schema(self):
        examples = json.loads(
            (
                PROJECT_ROOT / "configs/schemas/d4-07/online-examples.v1.json"
            ).read_text(encoding="utf-8")
        )
        validator = OnlineContractValidator()
        self.assertEqual("1.0.0", examples["schema_version"])
        self.assertEqual(2, len(examples["valid_requests"]))
        for request in examples["valid_requests"]:
            validator.validate_request(request)
        self.assertEqual(
            {
                "stale_state_revision",
                "session_not_found",
                "request_replayed",
                "slot_binding_mismatch",
                "multiple_answer_values",
            },
            {item["expected_error_code"] for item in examples["invalid_cases"]},
        )


class OnlineResponseSemanticTest(unittest.TestCase):
    def setUp(self) -> None:
        self.validator = OnlineContractValidator()

    def assert_code(self, expected, value, **proof):
        with self.assertRaises(OnlineContractError) as captured:
            self.validator.validate_response(value, **proof)
        self.assertEqual(expected, captured.exception.code)

    def test_approved_sql_requires_server_safety_and_execution_proof(self):
        value = response()
        self.validator.validate_response(
            value, safety_gate_approved=True, readonly_execution_attempts=1
        )
        self.assert_code(
            "safety_approval_unproven", value, readonly_execution_attempts=1
        )
        self.assert_code(
            "execution_attempt_count_mismatch", value, safety_gate_approved=True
        )

    def test_row_count_and_route_semantics_are_enforced(self):
        bad_count = response()
        bad_count["sql"]["row_count"] = 0
        self.assert_code(
            "sql_row_count_mismatch",
            bad_count,
            safety_gate_approved=True,
            readonly_execution_attempts=1,
        )
        bad_route = response(route="rag")
        self.assert_code(
            "sql_result_route_mismatch",
            bad_route,
            safety_gate_approved=True,
            readonly_execution_attempts=1,
        )

    def test_rag_success_requires_citation(self):
        rag = response(route="rag", sql=None)
        rag["decision"]["requires_d4_02_safety_gate"] = False
        self.assert_code("rag_success_requires_citation", rag)

    def test_success_and_refusal_require_matching_decision_actions(self):
        success = response()
        success["decision"]["action"] = "reject"
        self.assert_code(
            "success_decision_mismatch",
            success,
            safety_gate_approved=True,
            readonly_execution_attempts=1,
        )
        refused = response(status="refused", sql=None)
        self.assert_code("refused_decision_mismatch", refused)

    def test_sql_success_requires_an_approved_result(self):
        value = response(sql=None)
        self.assert_code("sql_success_requires_result", value)

    def test_refused_sql_can_show_rejected_candidate_but_never_results(self):
        refused = response(status="refused")
        refused["decision"]["action"] = "reject"
        refused["sql"].update(
            {
                "safety_status": "rejected",
                "execution_attempts": 0,
                "columns": [],
                "rows": [],
                "row_count": 0,
            }
        )
        self.validator.validate_response(refused)
        leaked = deepcopy(refused)
        leaked["sql"]["rows"] = [[275]]
        leaked["sql"]["row_count"] = 1
        self.assert_code("unapproved_sql_result_forbidden", leaked)

    def test_refused_response_cannot_expose_citations(self):
        refused = response(status="refused", route="rag", sql=None)
        refused["decision"]["action"] = "reject"
        refused["decision"]["requires_d4_02_safety_gate"] = False
        refused["citations"] = [
            {
                "evidence_id": "evidence.1",
                "document_id": "document.1",
                "document_title": "公开文档",
                "page": 1,
                "snippet": "脱敏引用片段。",
                "score": 1.0,
            }
        ]
        self.assert_code("refused_citations_forbidden", refused)

    def test_schema_failure_is_content_safe(self):
        invalid = response()
        invalid["raw_prompt"] = "do not leak"
        self.assert_code("invalid_online_response", invalid)


if __name__ == "__main__":
    unittest.main()
