"""D4-07-04 集成 Gate 的离线测试（只覆盖纯函数与边界，不递归跑全量测试）。"""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
for candidate in (PROJECT_ROOT, PROJECT_ROOT / "src"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from tools.evaluation import validate_d4_07_integration_gate as gate  # noqa: E402


CLEAN_UNIT = {"tests_run": 353, "failures": 0, "errors": 0, "skipped": 0}
CLEAN_ATOMICITY = {"tests_run": 5, "failures": 0, "errors": 0, "skipped": 0}
CLEAN_ASSET = {"command": "python tools/evaluation/validate_seed_assets.py", "returncode": 0}
CLEAN_CONTRACT = {
    "status": "pass",
    "mutation_self_check": {"all_detected": True, "detected": 5, "total": 5},
}


class D4IntegrationGateTest(unittest.TestCase):
    def test_gate_passes_when_all_checks_clean(self) -> None:
        decision = gate.decide_gate(
            CLEAN_UNIT, CLEAN_ATOMICITY, CLEAN_ASSET, CLEAN_CONTRACT
        )
        self.assertEqual(decision["status"], "pass")
        self.assertTrue(all(decision["checks"].values()))

    def test_gate_fails_on_unit_test_failure(self) -> None:
        unit = dict(CLEAN_UNIT, failures=1)
        decision = gate.decide_gate(
            unit, CLEAN_ATOMICITY, CLEAN_ASSET, CLEAN_CONTRACT
        )
        self.assertEqual(decision["status"], "fail")
        self.assertFalse(decision["checks"]["unit_tests_clean"])

    def test_gate_fails_when_no_test_ran(self) -> None:
        """0 个用例也按失败处理，避免空转通过。"""

        unit = dict(CLEAN_UNIT, tests_run=0)
        decision = gate.decide_gate(
            unit, CLEAN_ATOMICITY, CLEAN_ASSET, CLEAN_CONTRACT
        )
        self.assertEqual(decision["status"], "fail")

    def test_gate_fails_on_atomicity_regression_failure(self) -> None:
        atomicity = dict(CLEAN_ATOMICITY, failures=1)
        decision = gate.decide_gate(
            CLEAN_UNIT, atomicity, CLEAN_ASSET, CLEAN_CONTRACT
        )
        self.assertEqual(decision["status"], "fail")
        self.assertFalse(decision["checks"]["a2_atomicity_regression_passed"])

    def test_gate_fails_when_no_atomicity_test_ran(self) -> None:
        atomicity = dict(CLEAN_ATOMICITY, tests_run=0)
        decision = gate.decide_gate(
            CLEAN_UNIT, atomicity, CLEAN_ASSET, CLEAN_CONTRACT
        )
        self.assertEqual(decision["status"], "fail")

    def test_gate_fails_on_asset_validator_failure(self) -> None:
        asset = dict(CLEAN_ASSET, returncode=1)
        decision = gate.decide_gate(
            CLEAN_UNIT, CLEAN_ATOMICITY, asset, CLEAN_CONTRACT
        )
        self.assertFalse(decision["checks"]["asset_validation_passed"])

    def test_gate_fails_when_contract_mutation_not_detected(self) -> None:
        """变异自检没检出 = 检测空转，必须判失败。"""

        contract = {
            "status": "pass",
            "mutation_self_check": {"all_detected": False, "detected": 4, "total": 5},
        }
        decision = gate.decide_gate(
            CLEAN_UNIT, CLEAN_ATOMICITY, CLEAN_ASSET, contract
        )
        self.assertEqual(decision["status"], "fail")
        self.assertFalse(decision["checks"]["contract_mutations_detected"])

    def test_single_model_boundary_is_enforced(self) -> None:
        boundary = gate.model_boundary()
        self.assertEqual(boundary["provider_config_count"], 1)
        self.assertEqual(boundary["model"], "qwen3.7-flash")
        self.assertFalse(boundary["second_provider_enabled"])
        self.assertFalse(boundary["fallback_enabled"])
        self.assertFalse(boundary["jev_api_enabled"])


if __name__ == "__main__":
    unittest.main()
