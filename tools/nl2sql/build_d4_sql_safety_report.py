"""生成 D4-02 单表 NL2SQL 安全执行纵切离线报告。

对 10 条典型单表查询做「安全网关 → 只读执行」纵切，并对一组恶意 SQL 验证拦截，
结果落盘为 ``tests/reports/d4-sql-safety-v1.json``。仅离线：网络 0、模型 API 0。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from backend.nl2sql.readonly_executor import ReadonlyExecutor  # noqa: E402
from backend.nl2sql.sql_safety_gate import validate_candidate  # noqa: E402

SAMPLES_PATH = PROJECT_ROOT / "tests" / "fixtures" / "d4-02" / "single-table-queries.json"
MALICIOUS_PATH = PROJECT_ROOT / "tests" / "fixtures" / "d4-02" / "malicious-sql-samples.json"
REPORT_PATH = PROJECT_ROOT / "tests" / "reports" / "d4-sql-safety-v1.json"


def build_report() -> dict:
    executor = ReadonlyExecutor()
    samples = json.loads(SAMPLES_PATH.read_text(encoding="utf-8"))
    malicious = json.loads(MALICIOUS_PATH.read_text(encoding="utf-8"))

    safe_results = []
    all_approved = True
    for sample in samples:
        decision = validate_candidate(sample["sql"], sample["tables"], sample["params"])
        if not decision.approved:
            all_approved = False
            safe_results.append(
                {"id": sample["id"], "approved": False, "reason": decision.reason}
            )
            continue
        result = executor.execute(decision, sample["params"])
        safe_results.append(
            {
                "id": sample["id"],
                "approved": True,
                "tables": sample["tables"],
                "row_count": result.row_count,
                "expected_rows": sample["expected_rows"],
                "rows_match": result.row_count == sample["expected_rows"],
                "truncated": result.truncated,
                "elapsed_ms": result.elapsed_ms,
            }
        )
    all_approved = all_approved and all(
        item.get("approved") and item.get("rows_match") for item in safe_results
    )

    blocked_results = []
    all_blocked = True
    for sample in malicious:
        decision = validate_candidate(sample["sql"], sample["tables"], sample["params"])
        if decision.approved:
            all_blocked = False
        blocked_results.append(
            {
                "id": sample["id"],
                "category": sample.get("category"),
                "blocked": not decision.approved,
                "reason": decision.reason,
            }
        )

    return {
        "status": "pass" if (all_approved and all_blocked) else "fail",
        "report_type": "d4_sql_safety_offline_build",
        "task_id": "D4-02",
        "scope": "single_table_nl2sql_safety_slice",
        "generated_by": "tools/nl2sql/build_d4_sql_safety_report.py",
        "safe_query_count": len(safe_results),
        "blocked_query_count": len(blocked_results),
        "all_safe_queries_approved_and_correct": all_approved,
        "all_malicious_queries_blocked": all_blocked,
        "safe_queries": safe_results,
        "blocked_queries": blocked_results,
        "network_calls": 0,
        "model_api_calls": 0,
        "estimated_cost_cny": 0.0,
        "not_implemented": [
            "真实模型在线 NL2SQL 生成（授权批次，另行申请）",
            "端到端「意图路由 → NL2SQL 生成 → 安全执行」串联（依赖 D4-01 路由 + 在线模型）",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成 D4-02 SQL 安全执行纵切报告")
    parser.add_argument("--report", default=str(REPORT_PATH))
    args = parser.parse_args(argv)

    report = build_report()
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"报告 -> {report_path}（status={report['status']}）")
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
