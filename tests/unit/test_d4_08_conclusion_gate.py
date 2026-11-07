"""D4-08 A 侧评测契约与结论 Gate 校验器的离线测试。"""

from __future__ import annotations

import copy
from pathlib import Path
import sys
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.evaluation import validate_d4_08_conclusion_gate as gate  # noqa: E402


def _gate() -> dict:
    return copy.deepcopy(gate._load_json(gate.GATE_PATH))


def _runner() -> dict:
    return copy.deepcopy(gate._satisfactory_runner())


class D4ConclusionGateConsistencyTest(unittest.TestCase):
    def test_frozen_gate_is_self_consistent(self) -> None:
        errors = gate._check_gate_consistency(_gate())
        self.assertEqual(errors, [])

    def test_thresholds_match_frozen_values(self) -> None:
        gate_obj = _gate()
        self.assertEqual(
            gate_obj["thresholds"], {k: v for k, v in gate.FROZEN_THRESHOLDS.items()}
        )

    def test_protocol_ref_hash_binds_frozen_protocol(self) -> None:
        gate_obj = _gate()
        self.assertEqual(
            gate_obj["protocol_ref"]["sha256"],
            gate._sha256_canonical(gate.PROTOCOL_PATH),
        )


class D4ConclusionEngineTest(unittest.TestCase):
    def test_satisfactory_runner_yields_quality_gain(self) -> None:
        conclusion = gate.compute_conclusion(_gate(), _runner())
        self.assertEqual(conclusion["verdict"], "innovation_success")
        self.assertTrue(conclusion["intermediate_pass"])
        self.assertIn("quality_gain", conclusion["triggered_success"])
        self.assertEqual(conclusion["triggered_hard_failure"], [])

    def test_p_below_best_baseline_is_hard_failure(self) -> None:
        runner = _runner()
        runner["groups"]["P"]["metrics"][
            "post_clarification_end_to_end_execution_accuracy"
        ] = 0.60
        conclusion = gate.compute_conclusion(_gate(), runner)
        self.assertEqual(conclusion["verdict"], "hard_failure")
        self.assertIn(
            "not_better_than_best_baseline",
            conclusion["triggered_hard_failure"],
        )

    def test_clarification_recall_violation_is_hard_failure(self) -> None:
        runner = _runner()
        runner["groups"]["P"]["metrics"]["necessary_clarification_recall"] = 0.80
        conclusion = gate.compute_conclusion(_gate(), runner)
        self.assertIn(
            "clarification_recall_too_low", conclusion["triggered_hard_failure"]
        )

    def test_over_clarification_violation_is_hard_failure(self) -> None:
        runner = _runner()
        runner["groups"]["P"]["metrics"]["over_clarification_rate"] = 0.25
        conclusion = gate.compute_conclusion(_gate(), runner)
        self.assertIn(
            "over_clarification_too_high", conclusion["triggered_hard_failure"]
        )

    def test_migration_code_change_is_hard_failure(self) -> None:
        runner = _runner()
        runner["migration"]["new_domain_prompt_or_code_changed"] = True
        conclusion = gate.compute_conclusion(_gate(), runner)
        self.assertIn(
            "migration_requires_code_or_test_answer",
            conclusion["triggered_hard_failure"],
        )

    def test_evidence_not_reproducible_is_hard_failure(self) -> None:
        runner = _runner()
        runner["evidence_reproducible"] = False
        conclusion = gate.compute_conclusion(_gate(), runner)
        self.assertIn(
            "evidence_not_reproducible", conclusion["triggered_hard_failure"]
        )

    def test_insufficient_hidden_test_is_degradation(self) -> None:
        runner = _runner()
        runner["hidden_test"]["case_count"] = 100
        conclusion = gate.compute_conclusion(_gate(), runner)
        self.assertIn("insufficient_hidden_test", conclusion["triggered_degradation"])

    def test_exposure_exceeded_is_guard_violation(self) -> None:
        runner = _runner()
        runner["exposure"]["validation_current_task_count"] = 3
        conclusion = gate.compute_conclusion(_gate(), runner)
        self.assertEqual(conclusion["verdict"], "hard_failure")
        self.assertIn(
            "guard:validation_exposure_exceeded", conclusion["guard_violations"]
        )

    def test_invalid_structure_yields_invalid_result(self) -> None:
        runner = _runner()
        del runner["groups"]["P"]
        conclusion = gate.compute_conclusion(_gate(), runner)
        self.assertEqual(conclusion["verdict"], "invalid_result")
        self.assertTrue(conclusion["errors"])

    def test_validation_historical_exposure_is_counted(self) -> None:
        # historical=99 但 current=0：累计仍超上限 2，必须触发守卫（不能只查 current）。
        runner = _runner()
        runner["exposure"]["validation_historical_count"] = 99
        runner["exposure"]["validation_current_task_count"] = 0
        conclusion = gate.compute_conclusion(_gate(), runner)
        self.assertEqual(conclusion["verdict"], "hard_failure")
        self.assertIn(
            "guard:validation_exposure_exceeded", conclusion["guard_violations"]
        )

    def test_non_numeric_required_metric_yields_invalid_result(self) -> None:
        # 字符串指标不得让 compute_conclusion 抛 ValueError，应返回 invalid_result。
        runner = _runner()
        runner["groups"]["P"]["metrics"]["slot_macro_f1"] = "high"
        conclusion = gate.compute_conclusion(_gate(), runner)
        self.assertEqual(conclusion["verdict"], "invalid_result")
        self.assertTrue(
            any(
                e.startswith("non_numeric_p_metric") or e.startswith("schema")
                for e in conclusion["errors"]
            )
        )

    def test_mcnemar_comparison_must_bind_best_baseline(self) -> None:
        # comparison 错配（B0_vs_B1 而非 P_vs_B1）不得被当作 P 的质量增益证据。
        runner = _runner()
        runner["statistics"]["mcnemar"]["comparison"] = "B0_vs_B1"
        conclusion = gate.compute_conclusion(_gate(), runner)
        self.assertNotIn("quality_gain", conclusion["triggered_success"])
        self.assertNotEqual(conclusion["verdict"], "innovation_success")

    def test_keeper_is_real_prompt_author_is_guard_violation(self) -> None:
        # nl2sql 能力下 keeper = 该能力 Prompt 作者 cacgn 必须触发守卫（不能只拦字面 "prompt_author"）。
        runner = _runner()
        runner["hidden_test"]["capability"] = "nl2sql"
        runner["hidden_test"]["keeper"] = "cacgn"
        conclusion = gate.compute_conclusion(_gate(), runner)
        self.assertEqual(conclusion["verdict"], "hard_failure")
        self.assertIn(
            "guard:hidden_test_keeper_is_prompt_author", conclusion["guard_violations"]
        )

    def test_keeper_is_rag_author_is_guard_violation(self) -> None:
        # rag 能力下 keeper = 该能力 Prompt 作者 stevethomasih520-afk 必须触发守卫。
        runner = _runner()
        runner["hidden_test"]["capability"] = "rag"
        runner["hidden_test"]["keeper"] = "stevethomasih520-afk"
        conclusion = gate.compute_conclusion(_gate(), runner)
        self.assertEqual(conclusion["verdict"], "hard_failure")
        self.assertIn(
            "guard:hidden_test_keeper_is_prompt_author", conclusion["guard_violations"]
        )

    def test_keeper_is_clarification_author_is_guard_violation(self) -> None:
        # clarification 能力下 keeper = 该能力 Prompt 作者 Eveneve-night 必须触发守卫。
        runner = _runner()
        runner["hidden_test"]["capability"] = "clarification"
        runner["hidden_test"]["keeper"] = "Eveneve-night"
        conclusion = gate.compute_conclusion(_gate(), runner)
        self.assertEqual(conclusion["verdict"], "hard_failure")
        self.assertIn(
            "guard:hidden_test_keeper_is_prompt_author", conclusion["guard_violations"]
        )

    def test_keeper_cross_role_is_allowed(self) -> None:
        # 交叉保管合法：nl2sql 能力 keeper = C（stevethomasih520-afk，非 nl2sql 作者）不触发守卫。
        runner = _runner()  # 满意样例：capability=nl2sql, keeper=stevethomasih520-afk
        conclusion = gate.compute_conclusion(_gate(), runner)
        self.assertEqual(conclusion["verdict"], "innovation_success")
        self.assertNotIn(
            "guard:hidden_test_keeper_is_prompt_author", conclusion["guard_violations"]
        )


class D4MutationSelfCheckTest(unittest.TestCase):
    def test_contract_mutations_all_detected(self) -> None:
        for name, _ in gate._contract_mutations():
            mutated = gate._mutate_contract(name, _gate())
            self.assertTrue(
                gate._check_gate_consistency(mutated), f"contract mutation {name} undetected"
            )

    def test_conclusion_mutations_all_detected(self) -> None:
        gate_obj = _gate()
        good = _runner()
        good_conclusion = gate.compute_conclusion(gate_obj, good)
        for name, _ in gate._conclusion_mutations():
            mutated = gate._mutate_runner(name, _runner())
            changed = gate.compute_conclusion(gate_obj, mutated)
            detected = changed["verdict"] != good_conclusion["verdict"] or bool(
                changed["triggered_hard_failure"]
                or changed["triggered_degradation"]
                or changed["errors"]
            )
            self.assertTrue(detected, f"conclusion mutation {name} undetected")

    def test_full_report_passes_and_offline(self) -> None:
        report = gate.validate_all()
        self.assertEqual(report["status"], "pass")
        self.assertTrue(report["mutation_self_check"]["contract"]["all_detected"])
        self.assertTrue(report["mutation_self_check"]["conclusion"]["all_detected"])
        self.assertEqual(report["boundaries"]["network_calls"], 0)
        self.assertEqual(report["boundaries"]["model_api_calls"], 0)
        self.assertEqual(report["boundaries"]["credential_files_read"], 0)
        self.assertEqual(report["boundaries"]["estimated_cost_cny"], 0.0)
        self.assertEqual(report["boundaries"]["validation_cases_read"], 0)
        self.assertEqual(report["boundaries"]["hidden_test_cases_read"], 0)
        self.assertFalse(report["boundaries"]["answer_derived_fields_used_for_input"])
        self.assertTrue(len(report["stable_sha256"]) == 64)


if __name__ == "__main__":
    unittest.main()
