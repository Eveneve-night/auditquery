"""D4-07 C 侧机器报告的一致性回归（对应 A 侧复审 P1-1）。

防止「报告生成器改了，却没有用当前 head 重建并提交报告」导致的计数漂移：
已提交的 ``tests/reports/d4-07-c-tests-v1.json`` 必须与当前 head 的重建结果一致。

四条校验（都不含自指字段，避免生成时振荡）：
1. stable_sha256 自洽：用提交报告自身字段重算 hash，必须等于已提交值
   （抓到「改了计数却没同步 hash」的半成品报告）。
2. frontend 计数与运行时一致：run_frontend_suite() 只发现 console + backend_link
   两个文件（不含本文件，无递归），其精确计数必须等于已提交的 frontend_suite。
3. model_boundary 单模型约束：无 fallback（qwen3.7-flash），纯静态可查。
4. pending/not_proven 与生成器一致，且已经完成的演示素材和 B 复审不再
   留在 pending，真实在线与业务结论边界继续保留。

全量 unit_tests 精确计数的一致，由 ``validate_d4_07_c_tests.py --check`` 以
subprocess 形式重建比对（避免在测试内递归重跑全量 suite）。
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.evaluation import validate_d4_07_c_tests as validate  # noqa: E402


def _load() -> dict:
    return json.loads(validate.REPORT_PATH.read_text(encoding="utf-8"))


class CommittedReportConsistencyTest(unittest.TestCase):
    def test_committed_stable_hash_is_self_consistent(self) -> None:
        report = _load()
        recomputed = validate._hash(
            {
                key: report[key]
                for key in (
                    "status",
                    "unit_tests",
                    "frontend_suite",
                    "model_boundary",
                    "boundaries",
                    "manual_and_authorized_pending",
                    "not_proven",
                )
            }
        )
        self.assertEqual(recomputed, report["stable_sha256"])

    def test_committed_frontend_count_matches_runtime(self) -> None:
        report = _load()
        runtime = validate.run_frontend_suite()
        self.assertEqual(runtime["tests_run"], report["frontend_suite"]["tests_run"])

    def test_committed_model_boundary_single_model(self) -> None:
        report = _load()
        boundary = report["model_boundary"]
        self.assertTrue(boundary["single_model_ok"])
        self.assertEqual(boundary["models"], ["qwen3.7-flash"])
        self.assertEqual(boundary["provider_count"], 1)

    def test_committed_pending_and_not_proven_match_current_facts(self) -> None:
        report = _load()
        current = validate.validate_all(run_tests=False)
        self.assertEqual(
            report["manual_and_authorized_pending"],
            current["manual_and_authorized_pending"],
        )
        self.assertEqual(report["not_proven"], current["not_proven"])
        self.assertEqual(
            report["manual_and_authorized_pending"], ["real_api_online_examples"]
        )
        self.assertNotIn(
            "demo_screenshots_and_videos",
            report["manual_and_authorized_pending"],
        )
        self.assertNotIn(
            "b_cross_review_of_rag_ui",
            report["manual_and_authorized_pending"],
        )
        self.assertIn("online_rag_model_answer", report["not_proven"])
        self.assertIn("business_accuracy_or_generalization", report["not_proven"])
        self.assertIn("empirical_calibration", report["not_proven"])
        self.assertIn("innovation_claim_evidence", report["not_proven"])


if __name__ == "__main__":
    unittest.main()
