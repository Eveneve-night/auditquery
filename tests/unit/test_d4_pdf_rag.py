"""D4-06 PDF RAG 纵切离线测试。

覆盖：
1. manifest ≥10 份文档、每份 PDF 存在且 SHA-256 与 manifest 一致。
2. 双解析器（pdfplumber ↔ pypdf）证据 ID 集合一致性。
3. 「证据 ID → 页码」映射：解析器实测与渲染脚本声称一致，且覆盖多页。
4. 渲染确定性：临时目录重建逐字节一致（hash 一致）。
5. 最小词法检索：命中正确证据、无关查询低分、分闸门拒答。
6. 新文档族不跨 split、渲染/检索不引用 expected/gold/label 答案字段。

仅离线：网络调用 0，模型 API 调用 0。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from src.backend.rag.lexical_retriever import BM25Retriever, retrieve_top_evidence  # noqa: E402
from src.backend.rag.pdf_parser import cross_validate, extract_evidence_pages  # noqa: E402

KNOWLEDGE_DIR = PROJECT_ROOT / "tests" / "fixtures" / "knowledge"
PDF_SOURCE_DIR = KNOWLEDGE_DIR / "pdf"
PDF_DIR = PROJECT_ROOT / "data" / "raw" / "pdf"
MANIFEST_PATH = PROJECT_ROOT / "data" / "manifests" / "pdf-v1.json"
RENDERER = PROJECT_ROOT / "tools" / "rag" / "render_knowledge_pdfs.py"

EVIDENCE_LINE_PATTERN = re.compile(r"^\[([A-Z]+-[0-9]{3})\]\s+", re.MULTILINE)
EVIDENCE_LINE_WITH_BODY = re.compile(r"^\[([A-Z]+-[0-9]{3})\]\s*(.*)$")

# 既有的 4 个金标文档族（v1 冻结资产，不得被新文档族污染 split）。
FROZEN_DOCUMENT_FAMILIES = {"expense_policy", "project_report", "sales_playbook", "security_policy"}


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_manifest() -> dict:
    return _load_json(MANIFEST_PATH)


def _build_evidence_index() -> tuple[BM25Retriever, dict[str, str]]:
    """从 Markdown 源构造证据索引（渲染前的源，与金标证据文本一致）。"""
    evidence: dict[str, str] = {}
    sources = sorted(KNOWLEDGE_DIR.glob("*.md"))
    if PDF_SOURCE_DIR.is_dir():
        sources.extend(sorted(PDF_SOURCE_DIR.glob("*.md")))
    for source in sources:
        for ev_id, body in _parse_evidence(source.read_text(encoding="utf-8")):
            evidence[ev_id] = body
    return BM25Retriever(evidence), evidence


def _parse_evidence(text: str) -> list[tuple[str, str]]:
    result = []
    for line in text.splitlines():
        match = EVIDENCE_LINE_WITH_BODY.match(line.strip())
        if match:
            result.append((match.group(1), match.group(2).strip()))
    return result


class D4PDFRagTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = _load_manifest()
        cls.documents = cls.manifest["documents"]
        cls.by_document = {d["document_id"]: d for d in cls.documents}
        cls.retriever, cls.evidence = _build_evidence_index()

    # 1. manifest 与产物一致性 -------------------------------------------

    def test_manifest_covers_at_least_ten_documents(self) -> None:
        self.assertGreaterEqual(
            len(self.documents), 10, "公开文档夹具至少包含10份文档"
        )

    def test_every_pdf_exists_and_hash_matches(self) -> None:
        for doc in self.documents:
            pdf_path = PDF_DIR / f"{doc['document_id']}.pdf"
            with self.subTest(document=doc["document_id"]):
                self.assertTrue(pdf_path.is_file(), f"PDF 缺失: {pdf_path}")
                self.assertEqual(doc["sha256"], _sha256_file(pdf_path))

    def test_every_document_has_evidence_and_nonempty_pages(self) -> None:
        for doc in self.documents:
            with self.subTest(document=doc["document_id"]):
                self.assertGreaterEqual(len(doc["evidence_pages"]), 1)
                self.assertEqual(doc["evidence_pages"], extract_evidence_pages(PDF_DIR / f"{doc['document_id']}.pdf"))

    # 2. 双解析器交叉验证 ------------------------------------------------

    def test_double_parser_evidence_ids_consistent(self) -> None:
        for doc in self.documents:
            with self.subTest(document=doc["document_id"]):
                _, plumber_ids, pypdf_ids, consistent = cross_validate(PDF_DIR / f"{doc['document_id']}.pdf")
                self.assertTrue(consistent, f"两库证据 ID 集合不一致: {plumber_ids ^ pypdf_ids}")

    # 3. 证据 ID → 页码映射 ----------------------------------------------

    def test_evidence_pages_are_non_trivial(self) -> None:
        """至少一份文档覆盖多页，证明页码映射不是全部落第 1 页的空转。"""
        multi_page = [d for d in self.documents if d["page_count"] > 1]
        self.assertGreaterEqual(len(multi_page), 1, "至少一份文档须为多页")
        distinct_pages = {
            page
            for d in multi_page
            for page in d["evidence_pages"].values()
        }
        self.assertGreater(len(distinct_pages), 1, "证据应分布在多个不同页码")

    def test_evidence_ids_do_not_collide_with_frozen_families(self) -> None:
        """新文档族证据 ID 不得与冻结金标文档族重叠。"""
        for doc in self.documents:
            with self.subTest(document=doc["document_id"]):
                if doc["document_id"] in FROZEN_DOCUMENT_FAMILIES:
                    continue
                for ev_id in doc["evidence_pages"]:
                    self.assertFalse(
                        any(ev_id.startswith(prefix) for prefix in ("EXP", "PROJ", "SALES", "SEC")),
                        f"新文档族证据 ID 撞了冻结前缀: {ev_id}",
                    )

    # 4. 渲染确定性 ------------------------------------------------------

    def test_render_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_manifest = Path(tmp) / "manifest.json"
            completed = subprocess.run(
                [sys.executable, str(RENDERER), "--output-root", tmp, "--manifest", str(tmp_manifest)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            rebuilt = _load_json(tmp_manifest)
            rebuilt_by_id = {d["document_id"]: d for d in rebuilt["documents"]}
            for doc in self.documents:
                with self.subTest(document=doc["document_id"]):
                    self.assertEqual(
                        doc["sha256"],
                        rebuilt_by_id[doc["document_id"]]["sha256"],
                        "重建 hash 不一致，渲染非确定性",
                    )

    # 5. 词法检索 --------------------------------------------------------

    def test_retriever_ranks_correct_evidence(self) -> None:
        ranked = self.retriever.retrieve("月度经营报表的截止时间是什么时候", k=3)
        self.assertEqual("SALES-001", ranked[0][0], f"top-1 应为 SALES-001，实际 {ranked}")

    def test_retriever_ranks_expense_evidence_for_expense_query(self) -> None:
        ranked = self.retriever.retrieve("单笔报销超过5000元需要谁审批", k=3)
        self.assertEqual("EXP-002", ranked[0][0], f"top-1 应为 EXP-002，实际 {ranked}")

    def test_retriever_scores_irrelevant_query_low(self) -> None:
        """无关查询不应给出高置信命中（拒绝不因检索退化而强答）。"""
        ranked = self.retriever.retrieve("公司健身房几点开门可以洗澡吗", k=3)
        top_score = ranked[0][1] if ranked else 0.0
        relevant = self.retriever.retrieve("月度经营报表截止时间", k=1)[0][1]
        self.assertLess(top_score, relevant, "无关查询的 top 分不应高于相关查询")

    def test_retrieve_gate_rejects_insufficient_evidence(self) -> None:
        """分闸门：低分查询应返回空，触发上层拒答。"""
        relevant = self.retriever.retrieve("月度经营报表截止时间", k=1)[0][1]
        hits = retrieve_top_evidence(self.retriever, "健身房几点开门", k=3, min_score=relevant)
        self.assertEqual([], hits, "无关查询不应越过最低分闸门")

    # 6. 防过拟合与安全 ---------------------------------------------------

    def test_renderer_and_retriever_do_not_reference_answer_fields(self) -> None:
        """渲染与检索源码不得实际读取 expected/gold/label 答案字段（docs/23 红线 3）。

        用下标/取值调用模式精确匹配，避免误伤 docstring 中的合规说明文字。
        """
        for path in (RENDERER, PROJECT_ROOT / "src" / "backend" / "rag" / "lexical_retriever.py"):
            source = path.read_text(encoding="utf-8")
            for token in ("expected", "gold", "label"):
                for pattern in (
                    f'["{token}"]',
                    f"['{token}']",
                    f'.get("{token}"',
                    f".get('{token}'",
                    f'["{token}"] =',
                    f"['{token}'] =",
                ):
                    self.assertNotIn(
                        pattern,
                        source,
                        f"{path.name} 实际读取了答案字段 {token}",
                    )

    def test_renderer_is_offline_and_key_free(self) -> None:
        for path in (RENDERER, PROJECT_ROOT / "tools" / "rag" / "build_d4_pdf_rag_report.py"):
            source = path.read_text(encoding="utf-8")
            self.assertNotIn("requests", source)
            self.assertNotIn("urllib", source)
            self.assertNotIn("sk-", source)
            self.assertNotRegex(source, r"[A-Za-z0-9+/]{40,}")


def _sha256_file(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    unittest.main()
