"""D5-06/07 最终聚合 Gate 的离线失败关闭测试。"""

from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "evaluation" / "validate_d5_06_07_final_gate.py"
SPEC = importlib.util.spec_from_file_location("d5_final_gate", TOOL)
mod = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(mod)

SOURCE = {
    "evaluated_commit_sha": "a" * 40,
    "evaluated_tree_oid": "b" * 40,
    "source_manifest_stable_sha256": "c" * 64,
}


def _d505() -> dict:
    return {
        "status": "pass", "stable_sha256": "d" * 64,
        "gate_decision": {"d5_05_complete": True},
        "offline_regression": {
            "full_unit": {"status": "pass", "tests_run": 1},
            "d4_d5_relevant": {"status": "pass", "tests_run": 1},
            "seed": {"status": "pass", "total_cases": 60},
        },
        "deployment": {"windows_local_online": {"status": "pass"},
                       "trusted_clean_runners": {"status": "pass"}},
        "runtime_persistence": {"ok": True},
        "visitor_product_e2e": {"status": "pass"},
    }


class D50607FinalGateTests(unittest.TestCase):
    def _build(self, *, b="pass", c="pass", c_tests="pass", deliverables="pass"):
        with patch.object(mod.D505, "build_report", return_value=_d505()), \
                patch.object(mod.D505, "_is_ancestor", return_value=True), \
                patch.object(mod, "_validate_b_report", return_value={"status": b}), \
                patch.object(mod, "_validate_c_report", return_value={"status": c}), \
                patch.object(mod, "_run_test_files", return_value={"status": c_tests, "tests_run": 1}), \
                patch.object(mod, "_deliverable_probe", return_value={"status": deliverables}):
            return mod.build_report(evaluated_source=SOURCE)

    def test_all_machine_evidence_produces_review_ready_candidate(self) -> None:
        report = self._build()
        self.assertEqual("pass", report["status"])
        self.assertTrue(report["gate_decision"]["d5_06_complete"])
        self.assertEqual("pass", report["gate_decision"]["d5_07_machine_gate"])
        self.assertEqual(
            "release_candidate_ready_for_non_author_review",
            report["gate_decision"]["d5_07_release_decision"],
        )
        self.assertEqual(["one_non_author_exact_head_final_review"],
                         report["gate_decision"]["external_merge_prerequisites"])

    def test_b_c_and_deliverable_failures_each_block_release(self) -> None:
        for field in ("b", "c", "c_tests", "deliverables"):
            with self.subTest(field=field):
                report = self._build(**{field: "fail"})
                self.assertEqual("blocked", report["status"])
                self.assertEqual("blocked", report["gate_decision"]["d5_07_release_decision"])

    def test_boundaries_are_zero_and_no_accuracy_claim_is_added(self) -> None:
        report = self._build()
        self.assertEqual(0, report["boundaries"]["model_api_calls"])
        self.assertEqual(0, report["boundaries"]["real_credential_files_read"])
        self.assertIn("business_accuracy", report["not_proven"])
        self.assertIn("d4_08_formal_five_group_evaluation", report["not_proven"])

    def test_hash_changes_when_decision_changes(self) -> None:
        report = self._build()
        mutated = deepcopy(report)
        mutated["gate_decision"]["d5_06_complete"] = False
        mutated.pop("stable_sha256")
        self.assertNotEqual(report["stable_sha256"], mod.D505._canonical_hash(mutated))

    def test_final_report_is_excluded_but_release_inputs_are_bound(self) -> None:
        manifest = mod.D505._source_manifest_for_commit("HEAD")
        paths = {item["path"] for item in manifest["files"]}
        self.assertNotIn("tests/reports/d5-06-07-final-gate-v1.json", paths)
        for required in (
            "README.md",
            "tests/reports/d5-06-b-backend-security-v1.json",
            "tests/reports/d5-06-c-machine-report-v1.json",
            "tools/evaluation/validate_d5_06_07_final_gate.py",
        ):
            self.assertIn(required, paths)

    def test_c_report_content_drift_fails_closed(self) -> None:
        committed = {"status": "pass"}
        rebuilt = {"status": "pass", "five_round_http": {}, "windows": {}, "boundaries": {}}
        with patch.object(mod, "_read_json", return_value=committed), \
                patch.object(mod.D506C, "run_machine_report", return_value=rebuilt), \
                patch.object(mod.D506C, "assert_sanitized"):
            result = mod._validate_c_report()
        self.assertEqual("fail", result["status"])
        self.assertFalse(result["rebuilt_matches_committed"])


if __name__ == "__main__":
    unittest.main()
