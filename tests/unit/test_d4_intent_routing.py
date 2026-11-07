from __future__ import annotations

from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from backend.agent.intent_routing import (  # noqa: E402
    EXPECTED_SOURCE_IDS,
    IntentRoutingError,
    apply_intent_result,
    begin_turn,
    build_intent_payload,
    create_conversation_state,
    load_source_catalog,
    validate_conversation_state,
    validate_routing_state,
)


VALIDATOR_PATH = PROJECT_ROOT / "tools" / "evaluation" / "validate_d4_intent_routing.py"
VALIDATOR_SPEC = importlib.util.spec_from_file_location(
    "validate_d4_intent_routing", VALIDATOR_PATH
)
assert VALIDATOR_SPEC is not None and VALIDATOR_SPEC.loader is not None
VALIDATOR = importlib.util.module_from_spec(VALIDATOR_SPEC)
VALIDATOR_SPEC.loader.exec_module(VALIDATOR)


def success_result(
    *,
    intent: str,
    sources: list[str],
    needs_clarification: bool = False,
    missing_slots: list[str] | None = None,
    reason: str = "routable",
    normalized_query: str | None = None,
) -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "task_type": "intent",
        "status": "success",
        "data": {
            "intent": intent,
            "needs_clarification": needs_clarification,
            "required_sources": sources,
            "missing_slots": missing_slots or [],
            "reason": reason,
            "normalized_query": normalized_query,
        },
        "error": None,
        "warnings": [],
    }


class D4IntentRoutingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = load_source_catalog()

    def test_fixed_catalog_references_only_existing_verified_resources(self) -> None:
        self.assertEqual("d4-fixed-source-catalog", self.catalog["catalog_id"])
        self.assertEqual("1.0.0", self.catalog["version"])
        self.assertEqual(
            EXPECTED_SOURCE_IDS,
            tuple(item["source_id"] for item in self.catalog["sources"]),
        )
        self.assertEqual(
            {"sql", "rag"},
            {item["route_kind"] for item in self.catalog["sources"]},
        )
        serialized = json.dumps(self.catalog, ensure_ascii=False).lower()
        for forbidden in ("expected", "gold_sql", '"label"', "target_answer"):
            self.assertNotIn(forbidden, serialized)

    def test_machine_report_is_content_free_and_preserves_capability_boundary(self) -> None:
        report = VALIDATOR.validate_all()
        self.assertEqual("pass", report["status"])
        self.assertEqual("D4-01", report["task_id"])
        self.assertEqual(5, report["source_catalog"]["source_count"])
        self.assertEqual(5, report["state_transitions"]["synthetic_scenario_count"])
        self.assertTrue(report["source_catalog"]["same_catalog_for_every_request"])
        self.assertFalse(report["input_isolation"]["answer_derived_fields_read"])
        self.assertEqual(0, report["input_isolation"]["validation_cases_read"])
        self.assertEqual(0, report["network_calls"])
        self.assertEqual(0, report["model_api_calls"])
        self.assertEqual(0, report["sql_executions"])
        self.assertIn("automatic_required_slots", report["not_implemented"])
        serialized = json.dumps(report, ensure_ascii=False).lower()
        self.assertNotIn("synthetic request", serialized)
        self.assertNotIn("testonly", serialized)
        self.assertNotIn("cases", report)
        self.assertNotIn("raw_output", serialized)
        self.assertNotIn("model_output", serialized)

    def test_all_requests_receive_the_same_catalog_without_answer_fields(self) -> None:
        first = create_conversation_state("session-001", "TESTONLY first request")
        second = create_conversation_state(
            "session-002",
            "TESTONLY second request",
            conversation=[{"role": "user", "content": "TESTONLY earlier turn"}],
        )
        first_payload = build_intent_payload(first, self.catalog)
        second_payload = build_intent_payload(second, self.catalog)
        self.assertEqual(
            list(EXPECTED_SOURCE_IDS), first_payload["available_sources"]
        )
        self.assertEqual(
            first_payload["available_sources"], second_payload["available_sources"]
        )
        self.assertEqual(
            {"query", "conversation", "available_sources"}, set(first_payload)
        )
        self.assertNotIn("expected", json.dumps(first_payload).lower())

    def test_valid_results_cover_each_route_phase(self) -> None:
        cases = [
            (
                "sql",
                success_result(intent="sql", sources=["chinook_sql"]),
                "routed",
            ),
            (
                "rag",
                success_result(intent="rag", sources=["security_policy"]),
                "routed",
            ),
            (
                "cross",
                success_result(
                    intent="cross_source",
                    sources=["chinook_sql", "project_report"],
                ),
                "routed",
            ),
            (
                "unknown",
                success_result(
                    intent="unknown",
                    sources=[],
                    reason="outside_available_sources",
                ),
                "rejected",
            ),
            (
                "clarify",
                success_result(
                    intent="sql",
                    sources=["chinook_sql"],
                    needs_clarification=True,
                    missing_slots=["time_range"],
                    reason="ambiguous",
                ),
                "needs_clarification",
            ),
        ]
        for name, result, expected_phase in cases:
            with self.subTest(name=name):
                state = create_conversation_state(f"session-{name}", "TESTONLY request")
                updated = apply_intent_result(state, self.catalog, result)
                validate_conversation_state(updated)
                self.assertEqual(expected_phase, updated["phase"])
                self.assertEqual(1, updated["revision"])
                self.assertEqual(expected_phase, updated["history"][-1]["phase"])
                self.assertNotIn("query", updated["history"][-1])
                self.assertEqual("awaiting_intent", state["phase"])

    def test_new_turn_preserves_route_history_without_query_text(self) -> None:
        state = create_conversation_state("session-history", "TESTONLY sensitive first")
        routed = apply_intent_result(
            state,
            self.catalog,
            success_result(intent="sql", sources=["chinook_sql"]),
        )
        next_state = begin_turn(
            routed,
            self.catalog,
            "TESTONLY sensitive second",
            conversation=[{"role": "assistant", "content": "TESTONLY prior reply"}],
        )
        self.assertEqual("awaiting_intent", next_state["phase"])
        self.assertEqual(2, next_state["revision"])
        self.assertEqual(1, len(next_state["history"]))
        history_text = json.dumps(next_state["history"], ensure_ascii=False)
        self.assertNotIn("sensitive first", history_text)
        self.assertNotIn("sensitive second", history_text)

    def test_state_schema_rejects_unknown_or_answer_derived_fields(self) -> None:
        state = create_conversation_state("session-strict", "TESTONLY request")
        for field in ("unexpected", "expected", "gold_sql", "label"):
            with self.subTest(field=field):
                invalid = deepcopy(state)
                invalid[field] = "TESTONLY forbidden"
                with self.assertRaisesRegex(
                    IntentRoutingError, "INVALID_CONVERSATION_STATE"
                ):
                    validate_conversation_state(invalid)

    def test_out_of_catalog_and_route_kind_mismatch_fail_closed(self) -> None:
        state = create_conversation_state("session-invalid-route", "TESTONLY request")
        with self.assertRaisesRegex(IntentRoutingError, "INVALID_INTENT_RESULT"):
            apply_intent_result(
                state,
                self.catalog,
                success_result(intent="sql", sources=["not_in_catalog"]),
            )
        with self.assertRaisesRegex(IntentRoutingError, "ROUTE_KIND_MISMATCH"):
            apply_intent_result(
                state,
                self.catalog,
                success_result(intent="sql", sources=["security_policy"]),
            )
        with self.assertRaisesRegex(IntentRoutingError, "ROUTE_KIND_MISMATCH"):
            apply_intent_result(
                state,
                self.catalog,
                success_result(
                    intent="cross_source",
                    sources=["security_policy", "project_report"],
                ),
            )
        self.assertEqual("awaiting_intent", state["phase"])

    def test_error_result_and_invalid_transitions_leave_state_unchanged(self) -> None:
        state = create_conversation_state("session-errors", "TESTONLY request")
        original = deepcopy(state)
        error_result = {
            "schema_version": "1.0.0",
            "task_type": "intent",
            "status": "error",
            "data": None,
            "error": {
                "code": "INTERNAL_ERROR",
                "message": "TESTONLY safe error",
                "retryable": False,
                "field_errors": [],
            },
            "warnings": [],
        }
        with self.assertRaisesRegex(IntentRoutingError, "INTENT_RESULT_ERROR"):
            apply_intent_result(state, self.catalog, error_result)
        self.assertEqual(original, state)
        with self.assertRaisesRegex(IntentRoutingError, "INVALID_STATE_TRANSITION"):
            begin_turn(state, self.catalog, "TESTONLY premature next turn")

        routed = apply_intent_result(
            state,
            self.catalog,
            success_result(intent="sql", sources=["chinook_sql"]),
        )
        with self.assertRaisesRegex(IntentRoutingError, "INVALID_STATE_TRANSITION"):
            apply_intent_result(
                routed,
                self.catalog,
                success_result(intent="sql", sources=["chinook_sql"]),
            )

    def test_catalog_hash_drift_and_unknown_fields_are_rejected(self) -> None:
        for mutation, expected_code in (
            ("hash", "SOURCE_RESOURCE_HASH_MISMATCH"),
            ("missing", "SOURCE_RESOURCE_MISSING"),
            ("unknown", "INVALID_SOURCE_CATALOG"),
            ("duplicate", "SOURCE_CATALOG_SET_DRIFT"),
        ):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                candidate = deepcopy(self.catalog)
                if mutation == "hash":
                    candidate["sources"][0]["resource_refs"][0]["sha256"] = "0" * 64
                elif mutation == "missing":
                    candidate["sources"][0]["resource_refs"][0]["path"] = (
                        "tests/fixtures/knowledge/not_found.md"
                    )
                elif mutation == "unknown":
                    candidate["sources"][0]["unknown"] = "TESTONLY"
                else:
                    candidate["sources"][1]["source_id"] = "chinook_sql"
                path = Path(directory) / "catalog.json"
                path.write_text(
                    json.dumps(candidate, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(IntentRoutingError, expected_code):
                    load_source_catalog(path)

    def test_state_history_tampering_is_rejected(self) -> None:
        state = create_conversation_state("session-tamper", "TESTONLY request")
        routed = apply_intent_result(
            state,
            self.catalog,
            success_result(intent="rag", sources=["expense_policy"]),
        )
        tampered = deepcopy(routed)
        tampered["history"][-1]["selected_source_ids"] = ["security_policy"]
        with self.assertRaisesRegex(IntentRoutingError, "INVALID_STATE_HISTORY"):
            validate_conversation_state(tampered)

        out_of_catalog = deepcopy(routed)
        out_of_catalog["route"]["selected_source_ids"] = ["not_in_catalog"]
        out_of_catalog["history"][-1]["selected_source_ids"] = ["not_in_catalog"]
        with self.assertRaisesRegex(IntentRoutingError, "STATE_SOURCE_OUT_OF_CATALOG"):
            validate_routing_state(out_of_catalog, self.catalog)


if __name__ == "__main__":
    unittest.main()
