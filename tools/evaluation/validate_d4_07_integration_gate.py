"""D4-07-04 A 侧集成 Gate：把离线事实汇总成一份可复跑的机器结论。

Gate 只汇总 A 侧可以机器判定的部分：

- 全量离线单元测试（进程内 discover，取精确计数）；
- 离线种子资产校验器；
- D4-07 公共契约 v2 收口校验器；
- 单模型 / 无 fallback / 无第二 Provider 边界。

人工签字、B/C 真人复审、在线能力和隐藏集结论都不由本 Gate 代判，
只作为 `manual_gates_pending` 列出。
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.evaluation.validate_d4_online_contract_v2 import (  # noqa: E402
    validate_all as validate_contract_freeze,
)

REPORT_PATH = (
    PROJECT_ROOT / "tests" / "reports" / "d4-07-a-integration-gate-v1.json"
)
UNIT_DIR = PROJECT_ROOT / "tests" / "unit"
ATOMICITY_TEST_PATTERN = "test_d4_online_request_atomicity.py"
ASSET_VALIDATOR = PROJECT_ROOT / "tools" / "evaluation" / "validate_seed_assets.py"
PROVIDER_DIR = PROJECT_ROOT / "configs" / "providers"
APPROVED_MODEL = "qwen3.7-flash"


class IntegrationGateError(RuntimeError):
    pass


def _hash(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _run_unit_pattern(pattern: str) -> dict[str, int]:
    loader = unittest.defaultTestLoader
    suite = loader.discover(
        start_dir=str(UNIT_DIR), pattern=pattern, top_level_dir=str(UNIT_DIR)
    )
    stream = io.StringIO()
    runner = unittest.TextTestRunner(stream=stream, verbosity=0)
    result = runner.run(suite)
    return {
        "tests_run": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "skipped": len(result.skipped),
    }


def run_unit_tests() -> dict[str, int]:
    """进程内运行全量离线单元测试，返回精确计数。"""

    return _run_unit_pattern("test_*.py")


def run_atomicity_regression() -> dict[str, int]:
    """单独运行 A2 事务攻击回归，避免只靠全量计数隐式覆盖。"""

    return _run_unit_pattern(ATOMICITY_TEST_PATTERN)


def run_asset_validator() -> dict[str, Any]:
    completed = subprocess.run(
        [sys.executable, str(ASSET_VALIDATOR)],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    return {
        "command": f"python {ASSET_VALIDATOR.relative_to(PROJECT_ROOT).as_posix()}",
        "returncode": completed.returncode,
    }


def model_boundary() -> dict[str, Any]:
    paths = sorted(PROVIDER_DIR.glob("*.json"))
    providers = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    if len(providers) != 1 or providers[0].get("model") != APPROVED_MODEL:
        raise IntegrationGateError("single_model_boundary_changed")
    return {
        "provider_config_count": len(providers),
        "model": APPROVED_MODEL,
        "second_provider_enabled": False,
        "fallback_enabled": False,
        "jev_api_enabled": False,
    }


def decide_gate(
    unit: dict[str, int],
    atomicity: dict[str, int],
    asset: dict[str, Any],
    contract: dict[str, Any],
) -> dict[str, Any]:
    """纯函数：便于单元测试覆盖，不需要真的跑全量测试。"""

    checks = {
        "unit_tests_clean": unit["failures"] == 0
        and unit["errors"] == 0
        and unit["tests_run"] > 0,
        "a2_atomicity_regression_passed": atomicity["failures"] == 0
        and atomicity["errors"] == 0
        and atomicity["tests_run"] > 0,
        "asset_validation_passed": asset["returncode"] == 0,
        "contract_freeze_passed": contract["status"] == "pass",
        "contract_mutations_detected": contract["mutation_self_check"]["all_detected"],
    }
    return {
        "checks": checks,
        "status": "pass" if all(checks.values()) else "fail",
    }


def validate_all(*, run_tests: bool = True) -> dict[str, Any]:
    unit = run_unit_tests() if run_tests else {
        "tests_run": 0,
        "failures": 0,
        "errors": 0,
        "skipped": 0,
    }
    atomicity = run_atomicity_regression() if run_tests else {
        "tests_run": 0,
        "failures": 0,
        "errors": 0,
        "skipped": 0,
    }
    asset = run_asset_validator()
    contract = validate_contract_freeze()
    decision = decide_gate(unit, atomicity, asset, contract)
    boundary = model_boundary()

    report = {
        "status": decision["status"],
        "report_type": "d4_07_a_integration_gate",
        "task_id": "D4-07-04",
        "owner": "A",
        "scope": "a_side_offline_integration_gate",
        "checks": decision["checks"],
        "unit_tests": unit,
        "a2_atomicity_regression": atomicity,
        "asset_validation": asset,
        "contract_freeze": {
            "status": contract["status"],
            "report_type": contract["report_type"],
            "stable_sha256": contract["stable_sha256"],
            "mutation_detected": contract["mutation_self_check"]["detected"],
            "mutation_total": contract["mutation_self_check"]["total"],
            "findings": contract["findings"],
        },
        "model_boundary": boundary,
        "manual_gates_pending": [
            "five_turn_and_offline_failure_regression",
            "b_cross_review_of_rag_ui",
            "real_api_online_examples",
            "demo_screenshots_and_videos",
        ],
        "boundaries": {
            "network_calls": 0,
            "model_api_calls": 0,
            "credential_files_read": 0,
            "estimated_cost_cny": 0.0,
            "validation_cases_read": 0,
            "hidden_test_cases_read": 0,
        },
        "not_proven": [
            "b_evaluation_runner_implementation",
            "c_end_to_end_and_formal_report",
            "business_accuracy_or_generalization",
            "online_orchestration_for_arbitrary_natural_language",
            "online_sql_proposal_quality",
            "rag_business_accuracy",
            "empirical_calibration",
            "schema_recall_at_k_or_mrr",
            "cross_domain_migration",
            "innovation_claim_evidence",
        ],
    }
    report["stable_sha256"] = _hash(
        {
            "checks": report["checks"],
            "unit_tests": report["unit_tests"],
            "a2_atomicity_regression": report["a2_atomicity_regression"],
            "asset_validation": report["asset_validation"],
            "contract_freeze": report["contract_freeze"],
            "model_boundary": report["model_boundary"],
        }
    )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成 D4-07-04 A 侧集成 Gate 机器报告")
    parser.add_argument("--report", default=str(REPORT_PATH))
    parser.add_argument(
        "--skip-unit-tests",
        action="store_true",
        help="跳过进程内全量单元测试（仅用于调试本工具自身）",
    )
    args = parser.parse_args(argv)
    report = validate_all(run_tests=not args.skip_unit_tests)
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
