"""Deterministic offline regression coverage for the two-scale SQL runner."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "evaluation"))

import validate_d5_08_b_two_scale_sql as runner  # noqa: E402


class TwoScaleSQLTest(unittest.TestCase):
    def test_percentile_and_invalid_inputs(self) -> None:
        self.assertEqual(2.5, runner.percentile([1, 3], 0.75))
        with self.assertRaises(ValueError):
            runner.percentile([], 0.95)
        with self.assertRaises(ValueError):
            runner.build_report(samples=1)

    def test_report_covers_both_verified_scales_and_guards(self) -> None:
        report = runner.build_report(samples=2, warmup=0)
        self.assertEqual("pass", report["status"])
        self.assertEqual([1_000, 10_000], [item["artist_rows_verified"] for item in report["scales"]])
        self.assertEqual(0, report["boundaries"]["real_model_api_calls"])
        for item in report["scales"]:
            self.assertEqual(100, item["rows_returned_each_run"])
            self.assertTrue(item["limit_injected_each_run"])
            self.assertTrue(all(item["guards"].values()))
            self.assertGreaterEqual(item["p95_ms"], item["p50_ms"])
            self.assertGreater(item["python_tracemalloc_peak_bytes"], 0)

    def test_source_database_unchanged(self) -> None:
        before = runner.digest(runner.DEFAULT_DB_PATH)
        runner.build_report(samples=2, warmup=0)
        self.assertEqual(before, runner.digest(runner.DEFAULT_DB_PATH))


if __name__ == "__main__":
    unittest.main()
