"""Offline tests for the D3-02 NL2SQL task contract."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from backend.nl2sql.contract import (  # noqa: E402
    NL2SQL_OUTPUT_SCHEMA_ID,
    NL2SQL_PROMPT_VERSION,
    NL2SQLRequest,
    NL2SQLSchemaValidationError,
    candidate_ids,
    load_contract,
    load_output_schema,
    load_prompt,
    load_smoke_candidates,
    validate_contract,
    validate_model_output,
)


def success_result() -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "task_type": "nl2sql",
        "status": "success",
        "data": {
            "outcome": "sql",
            "sql": "SELECT COUNT(*) FROM Customer WHERE Country = ?",
            "tables": ["Customer"],
            "params": ["USA"],
            "rationale": "Filter one table and count rows.",
        },
        "error": None,
        "warnings": [],
    }


def error_result() -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "task_type": "nl2sql",
        "status": "error",
        "data": None,
        "error": {
            "code": "INSUFFICIENT_CONTEXT",
            "message": "The request requires more than one table.",
            "retryable": False,
            "field_errors": [],
        },
        "warnings": [],
    }


class TestPromptAndSchema(unittest.TestCase):
    def test_public_contract_is_valid(self):
        validate_contract()
        contract = load_contract()
        self.assertEqual(contract["model"], "qwen3.7-flash")
        self.assertEqual(contract["output_schema_id"], NL2SQL_OUTPUT_SCHEMA_ID)

    def test_contract_validator_enforces_provider_token_bounds(self):
        contract = load_contract()
        for invalid_limit in (0, 1501):
            with self.subTest(max_completion_tokens=invalid_limit):
                changed = json.loads(json.dumps(contract))
                changed["generation"]["max_completion_tokens"] = invalid_limit
                with patch("backend.nl2sql.contract.load_contract", return_value=changed):
                    with self.assertRaises(NL2SQLSchemaValidationError):
                        validate_contract()

    def test_prompt_and_schema_versions_are_stable(self):
        self.assertEqual(NL2SQL_PROMPT_VERSION, "nl2sql.v1")
        self.assertEqual(NL2SQL_OUTPUT_SCHEMA_ID, "nl2sql-result.v1")
        self.assertEqual(load_output_schema()["$id"], NL2SQL_OUTPUT_SCHEMA_ID)

    def test_prompt_allows_required_aggregate_functions(self):
        prompt = load_prompt()
        for function in ("COUNT", "SUM", "AVG", "MIN", "MAX", "ROUND"):
            self.assertIn(function, prompt)
        self.assertIn("params", prompt)
        self.assertIn("?", prompt)
        self.assertIn("不执行 SQL", prompt)

    def test_old_parallel_prompt_and_schema_are_removed(self):
        module = PROJECT_ROOT / "src" / "backend" / "nl2sql"
        self.assertFalse((module / "prompts" / "nl2sql_v1.md").exists())
        self.assertFalse((module / "schemas" / "nl2sql_output_v1.json").exists())


class TestNL2SQLRequest(unittest.TestCase):
    def schema(self):
        return {"tables": {"Artist": {"columns": {"ArtistId": {"type": "INTEGER"}}}}}

    def test_valid_request(self):
        request = NL2SQLRequest("req-1", "有多少位艺术家？", self.schema())
        self.assertEqual(request.dialect, "sqlite")

    def test_required_input_is_fail_closed(self):
        for kwargs in (
            {"request_id": "", "user_query": "q", "database_schema": self.schema()},
            {"request_id": "r", "user_query": "", "database_schema": self.schema()},
            {"request_id": "r", "user_query": "q", "database_schema": {}},
            {"request_id": "r", "user_query": "q", "database_schema": self.schema(), "dialect": "postgres"},
            {"request_id": 1, "user_query": "q", "database_schema": self.schema()},
            {"request_id": "r", "user_query": 1, "database_schema": self.schema()},
            {"request_id": "r", "user_query": "q", "database_schema": ["Artist"]},
            {"request_id": "r", "user_query": "q", "database_schema": self.schema(), "metadata": {"attempt": 1}},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                NL2SQLRequest(**kwargs)


class TestStrictModelOutputValidation(unittest.TestCase):
    def assert_schema_failure(self, raw):
        with self.assertRaises(NL2SQLSchemaValidationError) as caught:
            validate_model_output(raw)
        self.assertEqual(caught.exception.code, "SCHEMA_VALIDATION_FAILED")
        self.assertNotIn("SELECT", str(caught.exception))

    def test_valid_candidate_remains_non_executable(self):
        result = validate_model_output(success_result())
        self.assertFalse(result.rejected)
        self.assertEqual(result.tables, ("Customer",))
        self.assertEqual(result.params, ("USA",))
        self.assertEqual(result.safety_status, "not_validated")
        self.assertFalse(result.executable)

    def test_valid_error_is_rejected(self):
        result = validate_model_output(error_result())
        self.assertTrue(result.rejected)
        self.assertEqual(result.reject_code, "INSUFFICIENT_CONTEXT")
        self.assertFalse(result.executable)

    def test_unknown_status_fails(self):
        raw = success_result()
        raw["status"] = "maybe"
        self.assert_schema_failure(raw)

    def test_error_missing_required_reason_fails(self):
        raw = error_result()
        del raw["error"]["message"]
        self.assert_schema_failure(raw)

    def test_wrong_tables_type_fails(self):
        raw = success_result()
        raw["data"]["tables"] = "Customer"
        self.assert_schema_failure(raw)

    def test_wrong_params_type_fails(self):
        raw = success_result()
        raw["data"]["params"] = {"country": "USA"}
        self.assert_schema_failure(raw)

    def test_additional_field_fails(self):
        raw = success_result()
        raw["data"]["raw_model_output"] = "not allowed"
        self.assert_schema_failure(raw)

    def test_status_data_error_must_be_mutually_exclusive(self):
        raw = success_result()
        raw["error"] = error_result()["error"]
        self.assert_schema_failure(raw)

    def test_multiple_tables_fail_closed(self):
        raw = success_result()
        raw["data"]["tables"] = ["Customer", "Invoice"]
        self.assert_schema_failure(raw)

    def test_candidate_sql_is_not_string_scanned_as_safe(self):
        for sql in ("SELECT CreatedAt FROM T", "SELECT load_extension(?)", "DELETE FROM Artist"):
            raw = success_result()
            raw["data"]["sql"] = sql
            result = validate_model_output(raw)
            self.assertEqual(result.safety_status, "not_validated")
            self.assertFalse(result.executable)


class TestSmokeCandidates(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = PROJECT_ROOT / "tests" / "goldens" / "model-eval" / "v1" / "nl2sql.jsonl"
        cls.records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]

    def test_manifest_is_machine_readable_and_has_five_unique_ids(self):
        manifest = load_smoke_candidates()
        self.assertEqual(manifest["purpose"], "contract_smoke_only")
        self.assertEqual(manifest["split"], "prompt_dev")
        self.assertEqual(len(manifest["candidate_ids"]), 5)
        self.assertEqual(len(set(manifest["candidate_ids"])), 5)

    def test_candidate_ids_exist_and_match_declared_split(self):
        self.assertEqual(
            candidate_ids(self.records),
            ("SQL-001", "SQL-004", "SQL-005", "SQL-007", "SQL-010"),
        )

    def test_manifest_contains_no_gold_or_expected_answers(self):
        serialized = json.dumps(load_smoke_candidates(), ensure_ascii=False).lower()
        self.assertNotIn("gold", serialized)
        self.assertNotIn("expected", serialized)
        self.assertNotIn("select ", serialized)

    def test_candidate_prompt_shapes_are_supported_without_reading_answers(self):
        by_id = {record["id"]: record for record in self.records}
        required_tags = {
            "SQL-001": {"count"},
            "SQL-004": {"top_n", "sort"},
            "SQL-005": {"average", "unit_conversion"},
            "SQL-007": {"filter", "parameterized"},
            "SQL-010": {"sum"},
        }
        for item_id, tags in required_tags.items():
            self.assertTrue(tags.issubset(set(by_id[item_id]["tags"])))
            self.assertEqual(by_id[item_id]["context"]["scope"], "single_table")


if __name__ == "__main__":
    unittest.main()
