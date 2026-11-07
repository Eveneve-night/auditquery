from __future__ import annotations

import argparse
from contextlib import redirect_stderr
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = PROJECT_ROOT / "tools" / "evaluation" / "run_d3_contract_smoke_online.py"
SPEC = importlib.util.spec_from_file_location("d3_online_smoke", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class D3OnlineSmokeSafetyTest(unittest.TestCase):
    def test_parser_requires_explicit_case_limit_and_confirmation(self) -> None:
        parser = MODULE.build_parser()
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args([])
        args = parser.parse_args(
            [
                "--case-limit",
                "20",
                "--confirm-real-api",
                MODULE.CONFIRMATION_PHRASE,
            ]
        )
        self.assertEqual(args.case_limit, 20)

    def test_wrong_confirmation_stops_before_reading_any_file(self) -> None:
        args = argparse.Namespace(
            confirm_real_api="wrong",
            output=Path("missing-report.json"),
            manifest=Path("missing-manifest.json"),
            provider_config=Path("missing-provider.json"),
            case_limit=20,
            overwrite=False,
        )
        with self.assertRaises(MODULE.OnlineSmokeSafetyError):
            MODULE.execute_online_smoke(args)

    def test_existing_report_requires_explicit_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "existing.json"
            output.write_text("{}", encoding="utf-8")
            args = argparse.Namespace(
                confirm_real_api=MODULE.CONFIRMATION_PHRASE,
                output=output,
                manifest=Path("missing-manifest.json"),
                provider_config=Path("missing-provider.json"),
                case_limit=20,
                overwrite=False,
            )
            with self.assertRaises(MODULE.OnlineSmokeSafetyError):
                MODULE.execute_online_smoke(args)

    def test_public_summary_does_not_copy_cases_or_content(self) -> None:
        report = {
            "report_type": "d3_contract_smoke_run",
            "purpose": "contract_smoke_only",
            "model": "qwen3.7-flash",
            "source_split": "prompt_dev",
            "summary": {
                "planned_case_count": 1,
                "attempted_case_count": 1,
                "not_run_count": 0,
                "contract_valid_count": 1,
                "failed_count": 0,
                "model_attempt_count": 1,
                "input_tokens": 10,
                "output_tokens": 5,
                "total_tokens": 15,
                "estimated_cost_cny": 0.01,
                "elapsed_ms": 100,
                "batch_stop_reason": None,
            },
            "cases": [{"raw_response": "must-not-print"}],
        }
        summary = MODULE.public_summary(report, Path("report.json"))
        self.assertNotIn("cases", summary)
        self.assertNotIn("must-not-print", repr(summary))


if __name__ == "__main__":
    unittest.main()
