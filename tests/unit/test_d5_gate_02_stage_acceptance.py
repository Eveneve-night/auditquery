from __future__ import annotations

import json
import unittest

from jsonschema import Draft202012Validator

from tools.evaluation.validate_d5_gate_02_stage_acceptance import (
    CONTROL_PATH,
    CONTROL_SCHEMA_PATH,
    build_report,
    validate_control_plane,
)


class D5Gate02StageAcceptanceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.control = json.loads(CONTROL_PATH.read_text(encoding="utf-8"))
        self.schema = json.loads(CONTROL_SCHEMA_PATH.read_text(encoding="utf-8"))

    def test_control_is_strict_draft_2020_12(self) -> None:
        Draft202012Validator.check_schema(self.schema)
        Draft202012Validator(self.schema).validate(self.control)
        self.assertFalse(self.schema["additionalProperties"])

    def test_five_groups_share_one_case_and_four_call_cap(self) -> None:
        groups = self.control["groups"]
        self.assertEqual([item["group_id"] for item in groups], ["B0", "B1", "B2", "P", "P-uncalibrated"])
        self.assertEqual(sum(item["provider_attempt_cap"] for item in groups), 4)
        self.assertEqual(self.control["batch_limits"]["case_count"], 1)
        self.assertEqual(self.control["batch_limits"]["hard_cost_cny"], 0.05)

    def test_b2_never_calls_model(self) -> None:
        b2 = next(item for item in self.control["groups"] if item["group_id"] == "B2")
        self.assertFalse(b2["uses_model"])
        self.assertEqual(b2["provider_attempt_cap"], 0)

    def test_only_bailian_beijing_qwen37_flash_is_allowed(self) -> None:
        boundary = self.control["provider_boundary"]
        self.assertEqual(boundary["provider_id"], "bailian-cn-beijing")
        self.assertEqual(boundary["region"], "cn-beijing")
        self.assertEqual(boundary["model"], "qwen3.7-flash")
        self.assertFalse(boundary["fallback_enabled"])
        self.assertFalse(boundary["second_provider_enabled"])

    def test_auth_failures_stop_and_limits_cannot_be_raised(self) -> None:
        stop = self.control["stop_policy"]
        self.assertTrue({"http_400", "http_401", "http_403"}.issubset(stop["immediate"]))
        self.assertFalse(stop["may_raise_limits"])

    def test_report_redaction_forbids_content_and_key(self) -> None:
        forbidden = set(self.control["redacted_report"]["forbidden_content"])
        self.assertTrue({"prompt_text", "question_text", "model_output_text", "candidate_sql", "evidence_body", "api_key"}.issubset(forbidden))

    def test_readiness_fails_closed_without_adapter_or_data_manifests(self) -> None:
        result = validate_control_plane()
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["data_preparation"]["state"], "ready")
        self.assertEqual(result["public_canary"]["state"], "blocked")
        self.assertIn("online_adapter_registry_missing", result["public_canary"]["blockers"])
        self.assertEqual(result["validation_or_hidden_test"]["state"], "blocked")
        self.assertIn("hidden_answer_custody_missing", result["validation_or_hidden_test"]["blockers"])

    def test_machine_report_separates_d5_admission_from_online_canary(self) -> None:
        passing = {name: {"tests_run": count, "failures": 0, "errors": 0, "skipped": 0} for name, count in {
            "unit_tests": 1,
            "d4_08": 1,
            "pr_69_admin_permission": 1,
            "pr_70_document_lifecycle": 1,
        }.items()}
        report = build_report(test_evidence=passing)
        self.assertEqual(report["status"], "pass")
        self.assertTrue(report["decisions"]["d5_05_admitted"])
        self.assertEqual(report["decisions"]["d4_08_state"], "formal_data_preparation_ready")
        self.assertFalse(report["decisions"]["online_canary_executed"])
        self.assertEqual(report["boundaries"]["model_api_calls"], 0)
        self.assertEqual(report["boundaries"]["credential_files_read"], 0)


if __name__ == "__main__":
    unittest.main()
