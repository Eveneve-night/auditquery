"""Tests for the B-owned D4-07 online SQL metrics runner."""

from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

from backend.nl2sql.evaluation_metrics import (  # noqa: E402
    SqlEvaluationCase,
    SqlEvaluationError,
    summarize_sql_evaluation,
)
from tools.evaluation.validate_d4_online_sql_metrics import (  # noqa: E402
    main,
    validate_all,
)


def _case(case_id: str, **overrides) -> SqlEvaluationCase:
    values = {
        "case_id": case_id,
        "outcome": "success",
        "error_code": None,
        "retryable": False,
        "safety_status": "approved",
        "execution_attempts": 1,
        "execution_successes": 1,
        "model_calls": 1,
        "input_tokens": 10,
        "output_tokens": 2,
        "estimated_cost_cny": 0.00001,
        "service_elapsed_ms": 1.0,
        "provider_latency_ms": 10.0,
    }
    values.update(overrides)
    return SqlEvaluationCase(**values)


class SqlEvaluationMetricsTest(unittest.TestCase):
    def test_aggregate_counts_usage_and_linear_percentiles(self) -> None:
        summary = summarize_sql_evaluation(
            (
                _case("case.metric-001"),
                _case(
                    "case.metric-002",
                    outcome="refused",
                    safety_status="rejected",
                    execution_attempts=0,
                    execution_successes=0,
                    input_tokens=20,
                    output_tokens=4,
                    estimated_cost_cny=0.00002,
                    service_elapsed_ms=3.0,
                    provider_latency_ms=20.0,
                ),
            )
        )
        self.assertEqual(2, summary["case_count"])
        self.assertEqual(1, summary["outcome_counts"]["success"])
        self.assertEqual(1, summary["outcome_counts"]["refused"])
        self.assertEqual(36, summary["usage"]["total_tokens"])
        self.assertEqual(2.0, summary["service_elapsed_ms"]["p50"])
        self.assertEqual(2.9, summary["service_elapsed_ms"]["p95"])
        self.assertEqual(15.0, summary["provider_latency_ms"]["p50"])
        self.assertEqual(19.5, summary["provider_latency_ms"]["p95"])
        self.assertEqual(0, summary["safety"]["rejected_sql_execution_attempts"])
        self.assertEqual(64, len(summary["stable_sha256"]))

    def test_invalid_or_unsafe_records_fail_closed(self) -> None:
        with self.assertRaisesRegex(SqlEvaluationError, "rejected_sql_was_executed"):
            _case(
                "case.metric-unsafe",
                outcome="refused",
                safety_status="rejected",
                execution_attempts=1,
                execution_successes=0,
            )
        with self.assertRaisesRegex(SqlEvaluationError, "service_elapsed_ms_invalid"):
            _case("case.metric-nan", service_elapsed_ms=float("nan"))
        with self.assertRaisesRegex(SqlEvaluationError, "retryable_forbidden"):
            _case("case.metric-retryable", retryable=True)
        duplicate = _case("case.metric-duplicate")
        with self.assertRaisesRegex(SqlEvaluationError, "evaluation_case_id_duplicate"):
            summarize_sql_evaluation((duplicate, duplicate))

    def test_error_requires_stable_code_and_zero_execution(self) -> None:
        with self.assertRaisesRegex(SqlEvaluationError, "error_code_required"):
            _case(
                "case.metric-error",
                outcome="error",
                error_code=None,
                safety_status="not_applicable",
                execution_attempts=0,
                execution_successes=0,
            )
        result = _case(
            "case.metric-timeout",
            outcome="error",
            error_code="provider_timeout",
            retryable=True,
            safety_status="not_applicable",
            execution_attempts=0,
            execution_successes=0,
            provider_latency_ms=None,
        )
        self.assertEqual("provider_timeout", result.to_dict()["error_code"])


class D4OnlineSqlMetricsRunnerTest(unittest.TestCase):
    def test_public_synthetic_report_is_deterministic_and_content_safe(self) -> None:
        first = validate_all()
        second = validate_all()
        self.assertEqual(first, second)
        self.assertEqual("pass", first["status"])
        self.assertEqual(5, first["metrics"]["case_count"])
        self.assertEqual(1, first["metrics"]["outcome_counts"]["success"])
        self.assertEqual(1, first["metrics"]["outcome_counts"]["refused"])
        self.assertEqual(3, first["metrics"]["outcome_counts"]["error"])
        self.assertEqual(
            0, first["metrics"]["safety"]["rejected_sql_execution_attempts"]
        )
        serialized = json.dumps(first, ensure_ascii=False)
        for forbidden in (
            "TESTONLY",
            '"query":',
            '"candidate_sql":',
            '"rows":',
            "统计艺术家数量",
            "DELETE FROM",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_cli_writes_the_same_machine_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report_path = Path(directory) / "report.json"
            with redirect_stdout(io.StringIO()):
                self.assertEqual(0, main(["--report", str(report_path)]))
            written = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(validate_all(), written)


if __name__ == "__main__":
    unittest.main()
