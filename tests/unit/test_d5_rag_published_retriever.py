"""D5 产品检索 PublishedDocumentIndex 单元测试（离线，确定性）。"""

from __future__ import annotations

import sys
import hashlib
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from backend.rag.pdf_parser import extract_page_texts
from backend.rag.published_retriever import PublishedDocumentIndex


class _FakeRecord:
    def __init__(self, document_id, state, index_version, sha256=None):
        self.document_id = document_id
        self.state = state
        self.index_version = index_version
        pdf = _sample_pdf()
        self.sha256 = sha256 or hashlib.sha256(pdf).hexdigest()
        self.bytes = len(pdf)
        from backend.rag.pdf_parser import extract_page_texts
        self.page_count = len(extract_page_texts(pdf))
        self.failure_code = None


class _FakeStore:
    """模拟 SqliteDocumentStore：仅暴露 get_active_index / list_records / read_quarantine。"""

    def __init__(self, records, pdf_bytes, active=None):
        self._records = records
        self._pdf = pdf_bytes
        self._active = active

    def get_active_index(self):
        return self._active

    def list_records(self):
        return self._records

    def read_quarantine(self, document_id):
        return self._pdf


# 一份包含可检索中文文本的假 PDF（用真实 PDF 的字节，确保 pdfplumber 能解析）。
def _sample_pdf() -> bytes:
    return (Path(__file__).resolve().parents[2] / "data" / "raw" / "pdf" /
            "sales_playbook.pdf").read_bytes()


class PublishedDocumentIndexTest(unittest.TestCase):
    def test_rebuild_false_without_active_index(self):
        store = _FakeStore([], b"", active=None)
        index = PublishedDocumentIndex(store)
        self.assertFalse(index.rebuild())
        self.assertIsNone(index.active_document_id)

    def test_rebuild_false_when_active_points_to_disabled(self):
        # active_index 指向的文档已 disabled（不可能发生，但防御：不检索）。
        store = _FakeStore(
            [_FakeRecord("doc.x", "disabled", "idx.1")], b"", active="idx.1"
        )
        index = PublishedDocumentIndex(store)
        self.assertFalse(index.rebuild())

    def test_rebuild_and_retrieve_hits_published_page(self):
        store = _FakeStore(
            [_FakeRecord("doc.abc", "published", "idx.1")],
            _sample_pdf(),
            active="idx.1",
        )
        index = PublishedDocumentIndex(store)
        self.assertTrue(index.rebuild())
        self.assertEqual("doc.abc", index.active_document_id)
        citations = index.retrieve("销售流程")
        self.assertTrue(citations)
        first = citations[0]
        self.assertEqual("doc.abc", first["document_id"])
        self.assertTrue(first["document_title"])
        self.assertNotEqual("doc.abc", first["document_title"])
        self.assertGreaterEqual(first["page"], 1)
        self.assertTrue(first["snippet"])
        self.assertGreaterEqual(first["score"], 1.0)
        # evidence_id 形如 doc.abc.pN，回指真实 document + page。
        self.assertTrue(first["evidence_id"].startswith("doc.abc.p"))
        self.assertTrue(first["evidence_id"].endswith(str(first["page"])))

        grounded_citations, groundings = index.retrieve_with_groundings("销售流程")
        self.assertEqual(citations, grounded_citations)
        self.assertEqual(len(grounded_citations), len(groundings))
        # 与 PublishedDocumentIndex.rebuild 保持同一规范化坐标系：PR #99
        # 明确保留标题/正文行边界，char_start/end 只针对该页文本。
        page_texts = extract_page_texts(_sample_pdf(), preserve_lines=True)
        for citation, grounding in zip(grounded_citations, groundings):
            self.assertEqual("exact", grounding["alignment"])
            self.assertEqual(citation["evidence_id"], grounding["evidence_id"])
            source = page_texts[citation["page"] - 1]
            self.assertEqual(
                citation["snippet"],
                source[grounding["char_start"]:grounding["char_end"]],
            )

    def test_retrieve_empty_when_no_evidence(self):
        store = _FakeStore(
            [_FakeRecord("doc.abc", "published", "idx.1")],
            _sample_pdf(),
            active="idx.1",
        )
        index = PublishedDocumentIndex(store)
        index.rebuild()
        self.assertEqual([], index.retrieve("区块链共识算法"))


if __name__ == "__main__":
    unittest.main()
