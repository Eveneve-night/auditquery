#!/usr/bin/env python3
"""Generate a scoped, offline D5-06-B security and latency report.

This is component evidence, not a business-accuracy or final D5 Gate report.
No provider credential is loaded and no model request is made.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import platform
import statistics
import sys
import tempfile
import time
import tracemalloc
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from backend.product.server import ProductRuntime  # noqa: E402
from backend.nl2sql.readonly_executor import ReadonlyExecutor  # noqa: E402

REPORT = ROOT / "tests" / "reports" / "d5-06-b-backend-security-v1.json"
TEST_DIR = ROOT / "tests" / "unit"
TEST_FILES = (
    "test_d5_product_server.py",
    "test_d5_secret_store.py",
    "test_d5_admin_consumer_permission_gate.py",
    "test_d4_sql_safety.py",
    "test_d4_online_sql_service.py",
)
FORBIDDEN_REPORT_MARKERS = ("sk-", "ictagent123", "ict_admin_session", "csrf_token",
                            "Set-Cookie", "SELECT ", "INSERT ")


def percentile(values: list[float], fraction: float) -> float:
    """Linear interpolation, with a defined result for a single sample."""
    if not values or not 0 <= fraction <= 1:
        raise ValueError("invalid_percentile_input")
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def assert_offline_environment() -> None:
    """Fail before any test can discover an external credential source."""
    if os.environ.get("ICT_MASTER_KEY_FILE") or os.environ.get("ICT_SECRET_MASTER_KEY_B64"):
        raise RuntimeError("external_secret_environment_not_allowed")


def assert_sanitized(report_text: str) -> None:
    lowered = report_text.casefold()
    if any(marker.casefold() in lowered for marker in FORBIDDEN_REPORT_MARKERS):
        raise ValueError("sensitive_report_content")


def run_unit_matrix() -> dict[str, object]:
    # Discover by filename: a third-party site-packages/tests package must not
    # shadow this repository's tests/ namespace directory.
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for filename in TEST_FILES:
        if not (TEST_DIR / filename).is_file():
            raise RuntimeError("unit_matrix_test_file_missing")
        discovered = loader.discover(str(TEST_DIR), pattern=filename, top_level_dir=str(TEST_DIR))
        if discovered.countTestCases() == 0:
            raise RuntimeError("unit_matrix_test_file_empty")
        suite.addTests(discovered)
    count = suite.countTestCases()
    result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)
    return {
        "cases": count,
        "run": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "skipped": len(result.skipped),
        "passed": result.wasSuccessful() and result.testsRun == count,
    }


def benchmark_admin_reads(samples: int = 100) -> dict[str, object]:
    if samples < 2:
        raise ValueError("insufficient_samples")
    contract = json.loads((ROOT / "configs/contracts/d5/admin-bootstrap.v2.json").read_text("utf-8"))
    with tempfile.TemporaryDirectory(prefix="d5-06-b-") as instance:
        runtime = ProductRuntime(instance, master_key=b"a" * 32)
        login = runtime.dispatch("POST", "/api/v3/auth/admin/login", body=json.dumps({
            "api_version": "3.0", "contract_version": "2.0.0", "request_id": "req.b.login",
            "username": "admin", "password": contract["initial_credentials"]["initial_password"],
        }).encode("utf-8"))
        if login.status != 200:
            raise RuntimeError("admin_login_failed")
        cookie = login.headers["Set-Cookie"].split(";", 1)[0]
        paths = ("/api/v3/admin/health", "/api/v3/admin/audit-events",
                 "/api/v3/admin/usage-summary")
        latencies: list[float] = []
        tracemalloc.start()
        try:
            for index in range(samples):
                path = paths[index % len(paths)]
                if runtime.dispatch("GET", path).status != 401:
                    raise RuntimeError("anonymous_admin_access")
                started = time.perf_counter()
                response = runtime.dispatch("GET", path, cookie_header=cookie)
                latencies.append((time.perf_counter() - started) * 1000)
                if response.status != 200:
                    raise RuntimeError("authenticated_admin_read_failed")
            _, peak_bytes = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
    return {
        "scope": "local_in_process_admin_reads_only",
        "platform": platform.system().lower(),
        "concurrency": 1,
        "network_transport": "none",
        "samples": samples,
        "p50_ms": round(statistics.median(latencies), 3),
        "p95_ms": round(percentile(latencies, 0.95), 3),
        "max_ms": round(max(latencies), 3),
        "python_tracemalloc_peak_bytes": peak_bytes,
        "anonymous_status": 401,
        "authenticated_status": 200,
        "errors": 0,
    }


def resource_limits() -> dict[str, object]:
    """Read enforced defaults and frozen policy without loosening either."""
    executor = ReadonlyExecutor()
    policy = json.loads((ROOT / "configs/policies/agent-loop-limits.v1.json").read_text("utf-8"))
    limits = policy["limits"]
    return {
        "sql_max_rows": executor.max_rows,
        "sql_max_vm_steps": executor.max_vm_steps,
        "sql_max_elapsed_ms": executor.max_elapsed_ms,
        "loop_max_model_calls": limits["max_model_calls"],
        "loop_max_wall_time_ms": limits["max_wall_time_ms"],
        "loop_max_input_tokens_total": limits["max_input_tokens_total"],
        "loop_max_output_tokens_total": limits["max_output_tokens_total"],
        "loop_hard_cost_cny": limits["hard_cost_cny"],
        "policy_version": policy["version"],
    }


def build_report(samples: int = 100) -> dict[str, object]:
    assert_offline_environment()
    matrix = run_unit_matrix()
    performance = benchmark_admin_reads(samples)
    return {
        "task_id": "D5-06-B",
        "evidence_kind": "offline_component_security_and_performance",
        "status": "pass" if matrix["passed"] else "fail",
        "unit_matrix": matrix,
        "performance": performance,
        "resource_limits": resource_limits(),
        "boundaries": {
            "real_model_api_calls": 0,
            "real_key_read": False,
            "business_accuracy_measured": False,
            "final_gate": False,
        },
    }


def main() -> int:
    report = build_report()
    serialized = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    assert_sanitized(serialized)
    REPORT.write_text(serialized, encoding="utf-8")
    print(json.dumps({"status": report["status"], "unit_matrix": report["unit_matrix"],
                      "performance": report["performance"]}, ensure_ascii=False))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
