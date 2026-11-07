"""D5-GATE-02 / D4-08-02 A 侧阶段验收与公开 canary 准入控制。

本工具只读取公开仓库资产。它不会读取凭据、validation 或隐藏 test 内容，
也不会调用模型。在线 canary 仅在报告明确为 ready 后由独立入口执行。
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
from typing import Any

from jsonschema import Draft202012Validator


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

CONTROL_PATH = PROJECT_ROOT / "configs" / "evaluation" / "d4" / "public-canary-control.v1.json"
CONTROL_SCHEMA_PATH = PROJECT_ROOT / "configs" / "schemas" / "d4-08" / "public-canary-control.v1.schema.json"
PROTOCOL_PATH = PROJECT_ROOT / "configs" / "evaluation" / "d4" / "experiment-protocol.v1.json"
PUBLIC_TEMPLATE_PATH = PROJECT_ROOT / "configs" / "evaluation" / "d4" / "innovation-eval-manifest.v2.template.json"
REPORT_PATH = PROJECT_ROOT / "tests" / "reports" / "d5-gate-02-stage-acceptance-v1.json"
UNIT_DIR = PROJECT_ROOT / "tests" / "unit"
VALIDATOR_PATH = Path(__file__).resolve()

MERGED_PREREQUISITES = {
    "pr_69_admin_permission_gate": "4414fba07abb8a5d7d729b23cf1ef9e6a195cc6b",
    "pr_70_document_lifecycle": "9878a24babdc4d45d6fa3eda102ada0fc83a2814",
    "pr_72_d4_08_policy_hash_fix": "a5746d7db544ee62eb773773d48912e2df2cd5d2",
    "pr_73_previous_stage_gate": "49d6ca803c2bd02de8ba6fc4019e29ed45f86448",
}

EXPECTED_DEPENDENCIES = {
    "jsonschema": "4.26.0",
    "argon2-cffi": "25.1.0",
    "cryptography": "50.0.1",
    "pdfplumber": "0.11.10",
    "pypdf": "6.19.0",
    "reportlab": "5.0.1",
    "playwright": "1.63.0",
}


class StageGateError(RuntimeError):
    """阶段验收资产不满足严格契约。"""


def _canonical_bytes(path: Path) -> bytes:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")


def _load(path: Path) -> Any:
    return json.loads(_canonical_bytes(path).decode("utf-8"))


def _sha256(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _artifact(path: Path) -> dict[str, Any]:
    payload = _canonical_bytes(path)
    return {
        "path": path.relative_to(PROJECT_ROOT).as_posix(),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "bytes": len(payload),
    }


def _git_is_ancestor(commit: str) -> bool:
    completed = subprocess.run(
        ["git", "merge-base", "--is-ancestor", commit, "HEAD"],
        cwd=PROJECT_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return completed.returncode == 0


def _dependency_versions() -> dict[str, Any]:
    actual: dict[str, str | None] = {}
    for package in EXPECTED_DEPENDENCIES:
        try:
            actual[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            actual[package] = None
    return {
        "expected": EXPECTED_DEPENDENCIES,
        "actual": actual,
        "all_locked_versions_match": actual == EXPECTED_DEPENDENCIES,
    }


def validate_control_plane() -> dict[str, Any]:
    control = _load(CONTROL_PATH)
    schema = _load(CONTROL_SCHEMA_PATH)
    Draft202012Validator.check_schema(schema)
    errors = sorted(
        f"schema:{'.'.join(str(item) for item in error.path) or 'root'}"
        for error in Draft202012Validator(schema).iter_errors(control)
    )

    protocol = _load(PROTOCOL_PATH)
    groups = control.get("groups", [])
    group_ids = [item.get("group_id") for item in groups]
    expected_group_ids = [item.get("group_id") for item in protocol.get("groups", [])]
    call_sum = sum(int(item.get("provider_attempt_cap", 0)) for item in groups)
    b2 = next((item for item in groups if item.get("group_id") == "B2"), {})
    if group_ids != expected_group_ids:
        errors.append("group_order_or_identity_mismatch")
    if call_sum != control.get("batch_limits", {}).get("provider_attempt_cap"):
        errors.append("provider_attempt_sum_mismatch")
    if b2.get("uses_model") is not False or b2.get("provider_attempt_cap") != 0:
        errors.append("b2_must_not_call_provider")
    if control.get("provider_boundary") != {
        "provider_id": "bailian-cn-beijing",
        "region": "cn-beijing",
        "model": "qwen3.7-flash",
        "temperature": 0,
        "enable_thinking": False,
        "second_provider_enabled": False,
        "fallback_enabled": False,
    }:
        errors.append("single_model_boundary_mismatch")

    required_public_files = {
        "public_canary_manifest": PROJECT_ROOT / "configs" / "evaluation" / "d4" / "public-canary-manifest.v1.json",
        "online_adapter_registry": PROJECT_ROOT / "configs" / "evaluation" / "d4" / "evaluation-adapters.v1.json",
    }
    required_formal_files = {
        "formal_v2_data_manifest": PROJECT_ROOT / "configs" / "evaluation" / "d4" / "innovation-eval-manifest.v2.json",
        "hidden_answer_custody": PROJECT_ROOT / "configs" / "evaluation" / "d4" / "hidden-test-custody.v1.json",
        "readonly_hidden_runner_entrypoint": PROJECT_ROOT / "configs" / "evaluation" / "d4" / "hidden-runner-entrypoint.v1.json",
        **required_public_files,
    }
    public_blockers = sorted(f"{name}_missing" for name, path in required_public_files.items() if not path.is_file())
    formal_blockers = sorted(f"{name}_missing" for name, path in required_formal_files.items() if not path.is_file())

    return {
        "status": "pass" if not errors else "fail",
        "errors": sorted(set(errors)),
        "control": _artifact(CONTROL_PATH),
        "schema": _artifact(CONTROL_SCHEMA_PATH),
        "protocol": _artifact(PROTOCOL_PATH),
        "validator": _artifact(VALIDATOR_PATH),
        "group_ids": group_ids,
        "provider_attempt_cap": call_sum,
        "hard_cost_cny": control.get("batch_limits", {}).get("hard_cost_cny"),
        "data_preparation": {
            "state": "ready" if PUBLIC_TEMPLATE_PATH.is_file() and not errors else "blocked",
            "public_template": _artifact(PUBLIC_TEMPLATE_PATH),
        },
        "public_canary": {
            "state": "ready" if not public_blockers and not errors else "blocked",
            "blockers": public_blockers,
        },
        "validation_or_hidden_test": {
            "state": "ready" if not formal_blockers and not errors else "blocked",
            "blockers": formal_blockers,
        },
    }


def _run_suite(pattern: str) -> dict[str, int]:
    suite = unittest.defaultTestLoader.discover(
        start_dir=str(UNIT_DIR), pattern=pattern, top_level_dir=str(UNIT_DIR)
    )
    result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)
    return {
        "tests_run": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "skipped": len(result.skipped),
    }


def _suite_passed(result: dict[str, int]) -> bool:
    return result["tests_run"] > 0 and result["failures"] == 0 and result["errors"] == 0


def _default_test_evidence() -> dict[str, Any]:
    return {
        "unit_tests": _run_suite("test_*.py"),
        "d4_08": _run_suite("test_d4_08*.py"),
        "pr_69_admin_permission": _run_suite("test_d5_admin_consumer_permission_gate.py"),
        "pr_70_document_lifecycle": _run_suite("test_d5_document_lifecycle.py"),
    }


def build_report(test_evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    from tools.evaluation.validate_d4_08_b_runner import validate_all as validate_b_runner
    from tools.evaluation.validate_d4_08_conclusion_gate import validate_all as validate_conclusion
    from tools.evaluation.validate_d4_evaluation_freeze import validate_all as validate_freeze
    from tools.evaluation.validate_d5_product_contracts import validate_all as validate_d5_contracts
    from tools.evaluation.validate_seed_assets import DEFAULT_MANIFEST, validate as validate_seed

    tests = test_evidence if test_evidence is not None else _default_test_evidence()
    control = validate_control_plane()
    dependencies = _dependency_versions()
    merged = {name: _git_is_ancestor(commit) for name, commit in MERGED_PREREQUISITES.items()}
    gates = {
        "d4_00": validate_freeze()["status"],
        "d4_08_conclusion": validate_conclusion()["status"],
        "d4_08_b_runner": validate_b_runner()["status"],
        "d5_contract": validate_d5_contracts()["status"],
    }
    seed = validate_seed(DEFAULT_MANIFEST)
    all_tests_passed = all(_suite_passed(item) for item in tests.values())
    offline_green = (
        all_tests_passed
        and seed.get("status") == "pass"
        and seed.get("total_cases") == 60
        and all(value == "pass" for value in gates.values())
        and all(merged.values())
        and dependencies["all_locked_versions_match"]
        and control["status"] == "pass"
    )

    report: dict[str, Any] = {
        "status": "pass" if offline_green else "fail",
        "report_type": "d5_gate_02_stage_acceptance_and_d4_08_admission",
        "task_ids": ["D5-GATE-02", "D4-08-02"],
        "owner": "A",
        "baseline_main_sha": MERGED_PREREQUISITES["pr_70_document_lifecycle"],
        "merged_prerequisites": merged,
        "dependencies": dependencies,
        "tests": tests,
        "seed_assets": {"status": seed.get("status"), "total_cases": seed.get("total_cases")},
        "offline_gates": gates,
        "control_plane": control,
        "decisions": {
            "d5_05_admitted": offline_green,
            "d4_08_state": (
                "public_canary_ready"
                if offline_green and control["public_canary"]["state"] == "ready"
                else "formal_data_preparation_ready"
                if offline_green and control["data_preparation"]["state"] == "ready"
                else "blocked"
            ),
            "online_canary_executed": False,
        },
        "boundaries": {
            "network_evaluation_calls": 0,
            "model_api_calls": 0,
            "credential_files_read": 0,
            "validation_cases_read": 0,
            "hidden_test_cases_read": 0,
            "estimated_cost_cny": 0.0,
        },
        "not_proven": [
            "product_release_or_clean_machine_deployment",
            "business_accuracy_or_generalization",
            "empirical_calibration",
            "cross_domain_migration",
            "innovation_claim",
        ],
    }
    report["stable_sha256"] = _sha256(
        {key: report[key] for key in report if key != "stable_sha256"}
    )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行 D5-GATE-02 / D4-08-02 A 侧离线验收")
    parser.add_argument("--report", type=Path, default=REPORT_PATH)
    parser.add_argument("--check", action="store_true", help="重建并核对已提交报告，不写文件")
    args = parser.parse_args(argv)
    report = build_report()
    if args.check:
        if not args.report.is_file():
            print(json.dumps({"status": "fail", "error": "committed_report_missing"}, ensure_ascii=False))
            return 1
        committed = json.loads(args.report.read_text(encoding="utf-8"))
        if report != committed:
            print(json.dumps({
                "status": "fail",
                "error": "committed_report_drifted",
                "rebuilt_stable_sha256": report.get("stable_sha256"),
                "committed_stable_sha256": committed.get("stable_sha256"),
            }, ensure_ascii=False, indent=2))
            return 1
        print(json.dumps({"status": "pass", "stable_sha256": report["stable_sha256"]}, ensure_ascii=False))
        return 0
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_report", "validate_control_plane"]
