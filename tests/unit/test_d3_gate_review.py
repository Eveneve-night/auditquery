from __future__ import annotations

from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = PROJECT_ROOT / "tools" / "evaluation" / "review_d3_contract_smoke.py"
SOURCE_REPORT = (
    PROJECT_ROOT
    / "tests"
    / "reports"
    / "d3-contract-smoke-qwen3.7-flash-v1.json"
)
MANIFEST = PROJECT_ROOT / "tests" / "goldens" / "d3-smoke" / "v1" / "manifest.json"
SPEC = importlib.util.spec_from_file_location("d3_gate_review", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class D3GateReviewTest(unittest.TestCase):
    def _source(self) -> dict:
        return json.loads(SOURCE_REPORT.read_text(encoding="utf-8"))

    def _review_temp_source(self, value: dict, *, expected_hash: str | None = None) -> dict:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.json"
            source.write_text(
                json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            digest = MODULE._sha256(source)
            return MODULE.review_report(
                source,
                manifest_path=MANIFEST,
                project_root=PROJECT_ROOT,
                expected_source_sha256=expected_hash or digest,
                generated_at="2026-09-25T00:00:00+00:00",
            )

    def test_real_report_recomputes_metrics_and_passes_machine_gate(self) -> None:
        report = MODULE.review_report(
            SOURCE_REPORT,
            manifest_path=MANIFEST,
            project_root=PROJECT_ROOT,
            generated_at="2026-09-25T00:00:00+00:00",
        )
        metrics = report["metrics"]
        self.assertEqual(report["gate"]["code"], "contract_smoke_gate_pass")
        self.assertEqual(metrics["case_count"], 20)
        self.assertEqual(metrics["provider_attempt_count"], 20)
        self.assertEqual(metrics["provider_retry_count"], 0)
        self.assertEqual(metrics["input_tokens"], 24720)
        self.assertEqual(metrics["output_tokens"], 2673)
        self.assertEqual(metrics["total_tokens"], 27393)
        self.assertEqual(metrics["estimated_cost_cny"], 0.0070824)
        self.assertEqual(metrics["provider_latency_ms"]["p50"], 2996.433)
        self.assertEqual(metrics["provider_latency_ms"]["p95"], 4148.139)
        self.assertEqual(metrics["batch_wall_clock_ms"]["value"], 60322)
        self.assertEqual(report["api_calls_during_review"], 0)
        self.assertEqual(report["review_completion"]["human_b_signoff"], "pending")
        self.assertEqual(report["review_completion"]["human_c_signoff"], "pending")

    def test_frozen_text_hash_is_stable_across_lf_and_crlf(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lf_path = Path(directory) / "lf.json"
            crlf_path = Path(directory) / "crlf.json"
            lf_path.write_bytes(b'{"value":"TESTONLY"}\n')
            crlf_path.write_bytes(b'{"value":"TESTONLY"}\r\n')
            self.assertEqual(MODULE._sha256(lf_path), MODULE._sha256(crlf_path))

    def test_summary_tampering_fails_reconciliation(self) -> None:
        source = self._source()
        source["summary"]["total_tokens"] += 1
        report = self._review_temp_source(source)
        self.assertFalse(report["checks"]["aggregate_reconciled_from_cases"])
        self.assertEqual(report["gate"]["code"], "contract_smoke_gate_fail")

    def test_content_or_secret_material_fails_redaction_checks(self) -> None:
        source = self._source()
        source["cases"][0]["candidate_sql"] = "SELECT secret FROM hidden"
        source["cases"][0]["warnings"] = [
            "Bearer TESTONLY-credential-sentinel-123456"
        ]
        report = self._review_temp_source(source)
        self.assertEqual(report["metrics"]["forbidden_content_field_count"], 1)
        self.assertGreater(report["metrics"]["suspicious_secret_pattern_count"], 0)
        self.assertFalse(report["checks"]["forbidden_content_fields_absent"])
        self.assertFalse(report["checks"]["suspicious_secret_patterns_absent"])
        self.assertEqual(report["gate"]["status"], "fail")

    def test_source_hash_mismatch_fails_gate(self) -> None:
        report = self._review_temp_source(
            deepcopy(self._source()), expected_hash="0" * 64
        )
        self.assertFalse(report["checks"]["source_sha256_verified"])
        self.assertEqual(report["gate"]["status"], "fail")

    def test_gate_report_contains_no_case_content(self) -> None:
        report = MODULE.review_report(
            SOURCE_REPORT,
            manifest_path=MANIFEST,
            project_root=PROJECT_ROOT,
            generated_at="2026-09-25T00:00:00+00:00",
        )
        serialized = json.dumps(report, ensure_ascii=False)
        self.assertNotIn("cases", report)
        self.assertNotIn("SELECT secret FROM hidden", serialized)
        self.assertNotIn("raw_response", serialized)
        self.assertNotIn("question", serialized)


if __name__ == "__main__":
    unittest.main()
