"""生成 D4-06 离线解析质量报告（机器证据）。

对全部 PDF 执行双解析器交叉验证与「证据 ID → 页码」核对，把结果落盘为
``tests/reports/d4-pdf-rag-v1.json``。仅离线：网络 0、模型 API 0。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = PROJECT_ROOT / "data" / "manifests" / "pdf-v1.json"
REPORT_PATH = PROJECT_ROOT / "tests" / "reports" / "d4-pdf-rag-v1.json"

# 复用 src/backend/rag 下的解析器（通过 sys.path 注入仓库根）。
sys.path.insert(0, str(PROJECT_ROOT))
from src.backend.rag.pdf_parser import cross_validate  # noqa: E402


def build_report(manifest_path: Path, project_root: Path) -> dict:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    documents = manifest["documents"]

    per_document = []
    all_consistent = True
    all_pages_match = True
    for doc in documents:
        pdf_path = project_root / doc["pdf_path"]
        evidence_pages, plumber_ids, pypdf_ids, consistent = cross_validate(pdf_path)
        manifest_pages = doc.get("evidence_pages", {})
        pages_match = evidence_pages == manifest_pages

        all_consistent = all_consistent and consistent
        all_pages_match = all_pages_match and pages_match

        per_document.append(
            {
                "document_id": doc["document_id"],
                "pdf_path": doc["pdf_path"],
                "sha256": doc["sha256"],
                "page_count": doc["page_count"],
                "evidence_pages": evidence_pages,
                "manifest_evidence_pages": manifest_pages,
                "pages_match": pages_match,
                "cross_validation": {
                    "plumber_id_count": len(plumber_ids),
                    "pypdf_id_count": len(pypdf_ids),
                    "consistent": consistent,
                },
            }
        )

    total_ids = sum(len(d["evidence_pages"]) for d in per_document)
    return {
        "status": "pass" if (all_consistent and all_pages_match) else "fail",
        "report_type": "d4_pdf_rag_offline_build",
        "task_id": "D4-06",
        "scope": "pdf_rag_offline_infrastructure",
        "generated_by": "tools/rag/build_d4_pdf_rag_report.py",
        "document_count": len(documents),
        "total_evidence_ids": total_ids,
        "cross_validation_all_consistent": all_consistent,
        "evidence_pages_all_match": all_pages_match,
        "documents": per_document,
        "network_calls": 0,
        "model_api_calls": 0,
        "estimated_cost_cny": 0.0,
        "not_implemented": [
            "真实 API 在线 10+ 逐例（授权批次，另行申请）",
            "检索命中但证据不足的端到端模型拒答（离线仅验证检索分闸门）",
            "在线检索链路产出的引用片段逐字校验（离线清单已校验，在线链路待授权批次）",
        ],
        # 双解析器交叉验证（cross_validate）只比对「证据 ID 集合」是否一致，
        # 不比对片段文本逐字；片段逐字一致性由前端清单单独实现，见下。
        "source_text_check": {
            "cross_validation_scope": "evidence_id_set_only",
            "verbatim_check": "src/frontend/d4-console/rag-manifest.js matchesSourceText()",
            "verbatim_check_scope": "offline_manifest_snippets",
            "verified_by": "tests/unit/test_d4_frontend_console.py::test_source_text_comparison_is_not_vacuous",
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成 D4-06 PDF RAG 离线报告")
    parser.add_argument("--manifest", default=str(MANIFEST_PATH))
    parser.add_argument("--project-root", default=str(PROJECT_ROOT))
    parser.add_argument("--report", default=str(REPORT_PATH))
    args = parser.parse_args(argv)

    report = build_report(Path(args.manifest), Path(args.project_root))
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"报告 -> {report_path}（status={report['status']}）")
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
