"""D4-07 backend/frontend shared online API schema tests."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import unittest

from jsonschema import Draft202012Validator
from referencing import Registry, Resource


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_ROOT = PROJECT_ROOT / "configs" / "schemas" / "d4-07"


def _load(name: str) -> dict:
    return json.loads((SCHEMA_ROOT / name).read_text(encoding="utf-8"))


def _validator(wrapper_name: str) -> Draft202012Validator:
    definitions = _load("shared-api-definitions.v1.schema.json")
    registry = Registry().with_resource(
        definitions["$id"], Resource.from_contents(definitions)
    )
    return Draft202012Validator(_load(wrapper_name), registry=registry)


def _decision(action: str = "clarify") -> dict:
    return {
        "contract_version": "1.0.0",
        "action": action,
        "reason_code": "missing_required_slot",
        "score_kind": "uncalibrated_raw_score",
        "raw_score": 0.6,
        "calibration_version": None,
        "thresholds": {
            "accept_min": 0.8,
            "clarify_min": 0.4,
            "abstain_below": 0.4,
            "retrieval_min_score": None,
        },
        "evidence_refs": ["evidence.entity_kinds"],
        "missing_or_conflicting_slots": ["slot.entity"],
        "accept_authorizes_sql_execution": False,
        "requires_d4_02_safety_gate": True,
    }


def _usage() -> dict:
    return {
        "model_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "estimated_cost_cny": 0,
        "elapsed_ms": 12.5,
        "stopped_reason": None,
    }


def _clarification_response() -> dict:
    return {
        "schema_version": "1.0.0",
        "session_id": "session.1",
        "request_id": "request.1",
        "turn_id": "turn.1",
        "state_revision": 1,
        "route": "clarification",
        "status": "clarification_required",
        "message": "需要补充查询实体。",
        "decision": _decision(),
        "pending_question": {
            "question_id": "question.1",
            "session_id": "session.1",
            "request_id": "request.1",
            "turn_id": "turn.1",
            "state_revision": 1,
            "slot_id": "slot.entity",
            "question_text": "你要查询哪类实体？",
        },
        "candidate_evidence": [],
        "sql": None,
        "citations": [],
        "trace": [],
        "usage": _usage(),
        "error": None,
        "warnings": ["uncalibrated_raw_score"],
    }


class D4SharedApiSchemaTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.request_validator = _validator("online-request.v1.schema.json")
        cls.response_validator = _validator("online-response.v1.schema.json")

    def assert_valid(self, validator, value) -> None:
        self.assertEqual([], list(validator.iter_errors(value)))

    def assert_invalid(self, validator, value) -> None:
        self.assertTrue(list(validator.iter_errors(value)))

    def test_all_new_schemas_are_valid_draft_2020_12(self) -> None:
        for name in (
            "shared-api-definitions.v1.schema.json",
            "online-request.v1.schema.json",
            "online-response.v1.schema.json",
        ):
            Draft202012Validator.check_schema(_load(name))

    def test_query_and_clarification_requests_are_strict(self) -> None:
        query = {
            "schema_version": "1.0.0",
            "operation": "submit_query",
            "session_id": "session.1",
            "request_id": "request.1",
            "turn_id": "turn.1",
            "state_revision": 0,
            "query": "统计艺术家数量",
        }
        clarification = {
            "schema_version": "1.0.0",
            "operation": "submit_clarification",
            "session_id": "session.1",
            "request_id": "request.1",
            "turn_id": "turn.1",
            "state_revision": 1,
            "question_id": "question.1",
            "slot_id": "slot.entity",
            "values": ["artist"],
            "evidence_refs": ["evidence.entity_kinds"],
        }
        self.assert_valid(self.request_validator, query)
        self.assert_valid(self.request_validator, clarification)
        for forbidden_key in ("sql", "model", "expected", "gold", "label"):
            with self.subTest(field=forbidden_key):
                invalid = deepcopy(query)
                invalid[forbidden_key] = "forbidden"
                self.assert_invalid(self.request_validator, invalid)

    def test_clarification_response_requires_bound_question(self) -> None:
        response = _clarification_response()
        self.assert_valid(self.response_validator, response)
        missing_question = deepcopy(response)
        missing_question["pending_question"] = None
        self.assert_invalid(self.response_validator, missing_question)
        wrong_action = deepcopy(response)
        wrong_action["decision"]["action"] = "accept"
        self.assert_invalid(self.response_validator, wrong_action)

    def test_error_response_contains_no_decision_sql_or_citations(self) -> None:
        response = _clarification_response()
        response.update(
            {
                "route": "unknown",
                "status": "error",
                "message": "请求无法处理。",
                "decision": None,
                "pending_question": None,
                "citations": [],
                "error": {
                    "code": "invalid_request",
                    "retryable": False,
                    "message": "请求格式无效。",
                },
            }
        )
        self.assert_valid(self.response_validator, response)
        leaked = deepcopy(response)
        leaked["error"]["exception"] = "ValidationError(raw input)"
        self.assert_invalid(self.response_validator, leaked)

    def test_sql_success_never_grants_execution_through_accept(self) -> None:
        response = _clarification_response()
        response.update(
            {
                "route": "sql",
                "status": "success",
                "message": "只读查询已完成。",
                "decision": _decision("accept"),
                "pending_question": None,
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
            }
        )
        self.assert_valid(self.response_validator, response)
        response["decision"]["accept_authorizes_sql_execution"] = True
        self.assert_invalid(self.response_validator, response)

    def test_trace_rejects_raw_content_fields(self) -> None:
        response = _clarification_response()
        response["trace"] = [
            {
                "sequence": 1,
                "stage": "intent_routing",
                "action": "clarify",
                "reason_code": "missing_required_slot",
                "evidence_refs": ["evidence.entity_kinds"],
                "artifact_sha256": "0" * 64,
                "raw_prompt": "forbidden",
            }
        ]
        self.assert_invalid(self.response_validator, response)


if __name__ == "__main__":
    unittest.main()
