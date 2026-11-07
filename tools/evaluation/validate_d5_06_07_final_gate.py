#!/usr/bin/env python3
"""重建 D5-06 聚合证据并给出 D5-07 产品发布候选机器结论。

本工具只运行离线/localhost 检查并消费仓库内脱敏机器产物。它不读取真实
API Key、不调用模型、不读取正式 validation 或隐藏 test。最终发布仍须一名
非作者对精确 PR head 终审；机器 Gate 通过不能代替该治理条件。
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))


def _load_tool(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, PROJECT_ROOT / "tools" / "evaluation" / filename)
    if spec is None or spec.loader is None:
        raise RuntimeError("gate_tool_load_failed")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


D505 = _load_tool("d5_05_postmerge_gate", "validate_d5_05_postmerge_gate.py")
D506B = _load_tool("d5_06_b_backend_security", "validate_d5_06_b_backend_security.py")
D506C = _load_tool("d5_06_c_machine_report", "validate_d5_06_c_machine_report.py")

REPORT_PATH = PROJECT_ROOT / "tests" / "reports" / "d5-06-07-final-gate-v1.json"
B_REPORT_PATH = PROJECT_ROOT / "tests" / "reports" / "d5-06-b-backend-security-v1.json"
C_REPORT_PATH = PROJECT_ROOT / "tests" / "reports" / "d5-06-c-machine-report-v1.json"
PREREQUISITES = {
    "pr_91_full_source_binding": "aaec9cd965ddbb2d8e5c7b9d280f7799182a3ab6",
    "pr_92_accelerated_review_rule": "27579757ac425570c8dcf01552f5106908b141b9",
    "pr_93_b_backend_security": "2098085f207425c95297412305f93ffcd4ea26f4",
    "pr_89_c_browser_e2e": "684bb42b4bb020275f7eca2a7e5d12e187b82f94",
}
C_TEST_FILES = (
    "test_d5_06_c_browser_e2e.py",
    "test_d5_06_c_machine_report.py",
    "test_d5_document_lifecycle.py",
    "test_d5_visitor.py",
    "test_d5_visitor_rag_http_e2e.py",
)
RELEASE_FILES = (
    "README.md",
    "Dockerfile",
    "compose.yaml",
    "requirements-product.txt",
    "infra/start.ps1",
    "configs/contracts/d5/product-layer.v1.json",
    "configs/contracts/d5/admin-bootstrap.v2.json",
    "deliverables/设计报告.docx",
    "deliverables/设计报告.pdf",
    "deliverables/技术实现说明书.docx",
    "deliverables/技术实现说明书.pdf",
    "deliverables/demo-video/demo_flow.webm",
)


class FinalGateError(RuntimeError):
    """稳定且不包含内容的最终 Gate 错误。"""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FinalGateError("machine_report_invalid") from exc
    if not isinstance(value, dict):
        raise FinalGateError("machine_report_invalid")
    return value


def _run_test_files(filenames: tuple[str, ...]) -> dict[str, Any]:
    total = 0
    for filename in filenames:
        path = PROJECT_ROOT / "tests" / "unit" / filename
        if not path.is_file():
            return {"status": "fail", "tests_run": total, "failures": 1,
                    "error_code": "release_test_file_missing"}
        completed = subprocess.run(
            [sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests/unit", "-p", filename],
            cwd=PROJECT_ROOT, capture_output=True, text=True, encoding="utf-8", check=False,
        )
        import re
        match = re.search(r"Ran\s+(\d+)\s+tests?", completed.stdout + "\n" + completed.stderr)
        if completed.returncode != 0 or match is None:
            return {"status": "fail", "tests_run": total, "failures": 1,
                    "error_code": "release_test_suite_failed"}
        total += int(match.group(1))
    return {"status": "pass", "tests_run": total, "failures": 0}


def _validate_b_report() -> dict[str, Any]:
    committed = _read_json(B_REPORT_PATH)
    matrix = D506B.run_unit_matrix()
    expected_limits = D506B.resource_limits()
    performance = committed.get("performance")
    boundaries = committed.get("boundaries")
    valid = (
        committed.get("task_id") == "D5-06-B"
        and committed.get("status") == "pass"
        and matrix.get("passed") is True
        and matrix.get("run") == matrix.get("cases")
        and committed.get("resource_limits") == expected_limits
        and isinstance(performance, dict)
        and performance.get("errors") == 0
        and performance.get("samples") == 100
        and boundaries == {
            "business_accuracy_measured": False,
            "final_gate": False,
            "real_key_read": False,
            "real_model_api_calls": 0,
        }
    )
    return {
        "status": "pass" if valid else "fail",
        "tests_run": matrix.get("run", 0),
        "failures": matrix.get("failures", 0),
        "errors": matrix.get("errors", 0),
        "committed_report_sha256": _sha256(B_REPORT_PATH),
        "resource_limits_match_runtime": committed.get("resource_limits") == expected_limits,
        "performance_scope": performance.get("scope") if isinstance(performance, dict) else None,
    }


def _validate_c_report() -> dict[str, Any]:
    committed = _read_json(C_REPORT_PATH)
    rebuilt = D506C.run_machine_report()
    encoded = json.dumps(rebuilt, ensure_ascii=False, sort_keys=True)
    D506C.assert_sanitized(encoded)
    rebuilt["sanitization"] = {
        "passed": True,
        "markers_checked": len(D506C._FORBIDDEN_MARKERS),
    }
    matches = rebuilt == committed
    five_round = rebuilt.get("five_round_http", {})
    windows = rebuilt.get("windows", {})
    valid = (
        matches
        and rebuilt.get("status") == "pass"
        and five_round.get("rounds_validated") is True
        and five_round.get("replay_blocked") is True
        and five_round.get("post_publish_retrieval_ok") is True
        and windows.get("restart", {}).get("state_preserved") is True
        and windows.get("backup", {}).get("master_key_included") is False
        and windows.get("restore", {}).get("ok") is True
        and rebuilt.get("boundaries") == {
            "external_network": False,
            "loopback_only": True,
            "model_api_calls": 0,
            "real_key_read": False,
        }
    )
    return {
        "status": "pass" if valid else "fail",
        "rebuilt_matches_committed": matches,
        "committed_report_sha256": _sha256(C_REPORT_PATH),
        "rounds_recorded": len(five_round.get("rounds", [])),
        "rounds_validated": five_round.get("rounds_validated", False),
        "clarification_replay_blocked": five_round.get("replay_blocked", False),
        "post_publish_retrieval": five_round.get("post_publish_retrieval_ok", False),
    }


def _deliverable_probe() -> dict[str, Any]:
    missing = [relative for relative in RELEASE_FILES if not (PROJECT_ROOT / relative).is_file()]
    pdf_count = len(list((PROJECT_ROOT / "data" / "raw" / "pdf").glob("*.pdf")))
    screenshots = len(list((PROJECT_ROOT / "deliverables" / "d5-demo-screenshots").glob("*.png")))
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    readme_ready = all(marker in readme for marker in ("docker compose", "start.ps1", "/health/ready"))
    return {
        "status": "pass" if not missing and pdf_count >= 10 and screenshots >= 3 and readme_ready else "fail",
        "required_files_present": not missing,
        "missing_file_count": len(missing),
        "knowledge_pdf_count": pdf_count,
        "demo_screenshot_count": screenshots,
        "readme_startup_and_health_documented": readme_ready,
        "competition_submission_checklist_complete": False,
    }


def build_report(*, python312: Path | None = None, evaluated_commit: str | None = None,
                 evaluated_source: dict[str, Any] | None = None) -> dict[str, Any]:
    source = evaluated_source or D505._verify_evaluated_source(evaluated_commit)
    d505 = D505.build_report(
        run_tests=True, python312=python312, evaluated_commit=evaluated_commit, evaluated_source=source
    )
    b = _validate_b_report()
    c = _validate_c_report()
    c_tests = _run_test_files(C_TEST_FILES)
    deliverables = _deliverable_probe()
    prerequisites = {name: D505._is_ancestor(commit) for name, commit in PREREQUISITES.items()}

    d506_blockers: list[str] = []
    if d505.get("status") != "pass" or not d505.get("gate_decision", {}).get("d5_05_complete"):
        d506_blockers.append("d5_05_current_source_gate_not_passed")
    if b.get("status") != "pass":
        d506_blockers.append("d5_06_b_machine_evidence_not_passed")
    if c.get("status") != "pass":
        d506_blockers.append("d5_06_c_machine_evidence_not_passed")
    if c_tests.get("status") != "pass":
        d506_blockers.append("d5_06_c_release_suite_not_passed")
    if not all(prerequisites.values()):
        d506_blockers.append("merged_prerequisite_missing")

    release_blockers = list(d506_blockers)
    if deliverables.get("status") != "pass":
        release_blockers.append("product_deliverable_baseline_incomplete")
    d506_complete = not d506_blockers
    machine_release_passed = not release_blockers
    report: dict[str, Any] = {
        "report_schema_version": "d5-06-07-final-gate.v1",
        "gate_id": "D5-GATE-FINAL",
        "owner": "A",
        "status": "pass" if machine_release_passed else "blocked",
        "evaluated_source": source,
        "merged_prerequisites": prerequisites,
        "d5_05_current_source_gate": {
            "status": d505.get("status"),
            "stable_sha256": d505.get("stable_sha256"),
            "full_unit": d505.get("offline_regression", {}).get("full_unit"),
            "d4_d5_relevant": d505.get("offline_regression", {}).get("d4_d5_relevant"),
            "seed": d505.get("offline_regression", {}).get("seed"),
            "windows_local_online": d505.get("deployment", {}).get("windows_local_online"),
            "trusted_clean_runners": d505.get("deployment", {}).get("trusted_clean_runners"),
            "runtime_persistence": d505.get("runtime_persistence"),
            "visitor_product_e2e": d505.get("visitor_product_e2e"),
        },
        "d5_06_b": b,
        "d5_06_c": c,
        "d5_06_c_release_suite": c_tests,
        "product_deliverables": deliverables,
        "gate_decision": {
            "d5_06_complete": d506_complete,
            "d5_06_blockers": d506_blockers,
            "d5_07_machine_gate": "pass" if machine_release_passed else "blocked",
            "d5_07_release_decision": (
                "release_candidate_ready_for_non_author_review" if machine_release_passed else "blocked"
            ),
            "release_blockers": release_blockers,
            "external_merge_prerequisites": ["one_non_author_exact_head_final_review"],
        },
        "boundaries": {
            "model": "qwen3.7-flash",
            "provider": "bailian_beijing",
            "model_api_calls": 0,
            "network_evaluation_calls": 0,
            "real_credential_files_read": 0,
            "formal_validation_cases_read": 0,
            "hidden_test_cases_read": 0,
            "estimated_cost_cny": 0.0,
            "localhost_http_only": True,
        },
        "not_proven": [
            "real_model_online_behavior",
            "business_accuracy",
            "generalization",
            "empirical_calibration",
            "innovation_claim",
            "d4_08_formal_five_group_evaluation",
            "competition_submission_package_complete",
        ],
    }
    report["stable_sha256"] = D505._canonical_hash(report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行 D5-06/07 最终聚合机器 Gate")
    parser.add_argument("--report", type=Path, default=REPORT_PATH)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--python312", type=Path)
    parser.add_argument("--evaluated-commit")
    args = parser.parse_args(argv)
    if args.check and args.evaluated_commit:
        print(json.dumps({"status": "fail", "error": "evaluated_commit_forbidden_in_check"}))
        return 2
    committed: dict[str, Any] | None = None
    selected_commit = args.evaluated_commit
    try:
        if args.check:
            committed = _read_json(args.report)
            source = committed.get("evaluated_source")
            if not isinstance(source, dict) or not isinstance(source.get("evaluated_commit_sha"), str):
                raise FinalGateError("committed_source_binding_missing")
            selected_commit = source["evaluated_commit_sha"]
        source_binding = D505._verify_evaluated_source(selected_commit)
        report = build_report(
            python312=args.python312, evaluated_commit=selected_commit, evaluated_source=source_binding
        )
    except (FinalGateError, D505.GateProbeError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "fail", "error": str(exc)}, ensure_ascii=False))
        return 1
    if args.check:
        if committed != report:
            print(json.dumps({"status": "fail", "error": "committed_report_drifted",
                              "rebuilt_stable_sha256": report["stable_sha256"]}, ensure_ascii=False))
            return 1
        print(json.dumps({"status": "pass", "stable_sha256": report["stable_sha256"]}))
        return 0
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] in {"pass", "blocked"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
