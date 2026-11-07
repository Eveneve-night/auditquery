"""D4-07-03 C 侧统一测试入口 + 聚合报告。

把 C 侧负责的离线测试汇总成一份可复跑的机器结论：

- 全量离线单元测试（进程内 discover，取精确计数）；
- 前端专项（d4-console 渲染/交互 + 前后端联调）；
- 单模型 / 无 fallback 边界（qwen3.7-flash）。

本工具只汇总 C 侧可机器判定的部分；五轮上下文、断网/超时/429/预算异常
回归均已落在相应单元测试里（`test_d4_frontend_console` 的 multi_round 与
`test_d4_online_sql_service` 的 provider 失败矩阵）。演示截图/视频素材与
B 侧交叉复核已随 PR #62 完成；真实 API 在线逐例仍是另行授权步骤。
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import sys
import unittest
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

REPORT_PATH = PROJECT_ROOT / "tests" / "reports" / "d4-07-c-tests-v1.json"
UNIT_DIR = PROJECT_ROOT / "tests" / "unit"
APPROVED_MODEL = "qwen3.7-flash"
FRONTEND_PATTERNS = (
    "test_d4_frontend_console.py",
    "test_d4_frontend_backend_link.py",
)
REPORT_COMPARE_FIELDS = (
    "status",
    "unit_tests",
    "frontend_suite",
    "model_boundary",
    "boundaries",
    "manual_and_authorized_pending",
    "not_proven",
    "stable_sha256",
)


class CTestsError(RuntimeError):
    pass


def _hash(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _run_suite(suite: unittest.TestSuite) -> dict[str, int]:
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

    loader = unittest.defaultTestLoader
    suite = loader.discover(
        start_dir=str(UNIT_DIR), pattern="test_*.py", top_level_dir=str(UNIT_DIR)
    )
    return _run_suite(suite)


def run_frontend_suite() -> dict[str, int]:
    """运行 C 侧前端专项（渲染/交互 + 前后端联调）。"""

    suite = unittest.TestSuite()
    loader = unittest.defaultTestLoader
    # 用 discover（按文件名模式）而非 loadTestsFromName：tests 不是 Python 包
    # （无 __init__.py），loadTestsFromName 会因 namespace package 行为变化而失败。
    for pattern in FRONTEND_PATTERNS:
        suite.addTests(
            loader.discover(
                start_dir=str(UNIT_DIR),
                pattern=pattern,
                top_level_dir=str(UNIT_DIR),
            )
        )
    return _run_suite(suite)


def model_boundary() -> dict[str, Any]:
    providers = []
    for path in sorted((PROJECT_ROOT / "configs" / "providers").glob("*.json")):
        providers.append(json.loads(path.read_text(encoding="utf-8")))
    models = {item.get("model") for item in providers if isinstance(item, dict)}
    return {
        "provider_count": len(providers),
        "models": sorted(m for m in models if isinstance(m, str)),
        "approved_model": APPROVED_MODEL,
        "single_model_ok": models == {APPROVED_MODEL},
    }


def validate_all(*, run_tests: bool = True) -> dict[str, Any]:
    unit = run_unit_tests() if run_tests else {"tests_run": 0, "failures": 0, "errors": 0, "skipped": 0}
    frontend = run_frontend_suite() if run_tests else {"tests_run": 0, "failures": 0, "errors": 0, "skipped": 0}
    boundary = model_boundary()

    clean = (
        unit["failures"] == 0
        and unit["errors"] == 0
        and frontend["failures"] == 0
        and frontend["errors"] == 0
    )
    status = "pass" if clean and boundary["single_model_ok"] else "fail"

    report: dict[str, Any] = {
        "status": status,
        "report_type": "d4_07_c_tests",
        "task_id": "D4-07-03",
        "unit_tests": unit,
        "frontend_suite": frontend,
        "model_boundary": boundary,
        "boundaries": {
            "network_calls": 0,
            "model_api_calls": 0,
            "credential_files_read": 0,
            "estimated_cost_cny": 0.0,
        },
        "manual_and_authorized_pending": [
            "real_api_online_examples",      # C5：授权批次另行申请
        ],
        "not_proven": [
            "business_accuracy_or_generalization",
            "empirical_calibration",
            "innovation_claim_evidence",
            "online_rag_model_answer",       # 真实模型回答属授权批次
        ],
    }
    report["stable_sha256"] = _hash(
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
    return report


def check_committed() -> int:
    """重建报告并与已提交的 d4-07-c-tests-v1.json 比对，不一致则非零退出。

    用于防止「报告生成器改了，却没有用当前 head 重建并提交报告」导致的
    计数漂移（如提交的仍是全量 410 / 前端 32，而当前 head 实际更多）。
    """
    current = validate_all(run_tests=True)
    if not REPORT_PATH.exists():
        print(f"[FAIL] 缺少已提交报告：{REPORT_PATH}")
        return 1
    committed = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    mismatch = False
    for key in REPORT_COMPARE_FIELDS:
        if current.get(key) != committed.get(key):
            mismatch = True
            print(f"[FAIL] 字段 {key} 与当前 head 重建结果不一致：")
            print(f"  当前: {json.dumps(current.get(key), ensure_ascii=False)}")
            print(f"  已提交: {json.dumps(committed.get(key), ensure_ascii=False)}")
    if not mismatch:
        print("[OK] 已提交报告与当前 head 重建结果一致")
    return 1 if mismatch else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行 D4-07 C 侧统一测试")
    parser.add_argument("--skip-tests", action="store_true", help="跳过测试仅生成结构")
    parser.add_argument("--check", action="store_true", help="重建并与已提交报告比对，不一致则非零退出")
    args = parser.parse_args(argv)
    if args.check:
        return check_committed()
    report = validate_all(run_tests=not args.skip_tests)
    REPORT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "REPORT_PATH",
    "run_unit_tests",
    "run_frontend_suite",
    "model_boundary",
    "validate_all",
    "check_committed",
    "main",
]
