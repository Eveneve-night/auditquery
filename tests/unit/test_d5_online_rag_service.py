"""D5 OnlineRagService 单元测试：route=rag 响应 + 契约校验 + 证据门。"""

from __future__ import annotations

import sys
import hashlib
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from backend.api.online_contracts import OnlineContractValidator
from backend.api.online_rag_service import OnlineRagService
from backend.rag.evidence_grounding import align_exact_text, grounding_audit_sha256


class _FakeRecord:
    def __init__(self, document_id, state, index_version):
        self.document_id = document_id
        self.state = state
        self.index_version = index_version
        pdf = _sample_pdf()
        self.sha256 = hashlib.sha256(pdf).hexdigest()
        self.bytes = len(pdf)
        from backend.rag.pdf_parser import extract_page_texts
        self.page_count = len(extract_page_texts(pdf))
        self.failure_code = None


class _FakeStore:
    def __init__(self, pdf_bytes, active=None, records=None):
        self._pdf = pdf_bytes
        self._active = active
        self._records = records or []

    def get_active_index(self):
        return self._active

    def list_records(self):
        return self._records

    def read_quarantine(self, document_id):
        return self._pdf


def _sample_pdf() -> bytes:
    return (Path(__file__).resolve().parents[2] / "data" / "raw" / "pdf" /
            "sales_playbook.pdf").read_bytes()


class OnlineRagServiceTest(unittest.TestCase):
    def setUp(self):
        self.validator = OnlineContractValidator()
        self.store = _FakeStore(
            _sample_pdf(), active="idx.1",
            records=[_FakeRecord("doc.abc", "published", "idx.1")],
        )
        self.service = OnlineRagService(self.store)

    def _submit(self, question):
        return self.service.submit({
            "session_id": "s1", "request_id": "r1",
            "turn_id": 0, "state_revision": 0, "question": question,
        })

    def test_success_has_citations_and_passes_contract(self):
        resp = self._submit("销售流程")
        self.assertEqual("rag", resp["route"])
        self.assertEqual("success", resp["status"])
        self.assertTrue(resp["citations"])
        self.assertEqual(1, len(resp["citations"]))
        self.assertIsNone(resp["sql"])
        self.assertIsNone(resp["pending_question"])
        self.assertEqual("accept", resp["decision"]["action"])
        self.assertFalse(resp["decision"]["requires_d4_02_safety_gate"])
        self.assertIn("exact_source_grounding", resp["warnings"])
        self.assertEqual(64, len(resp["trace"][1]["artifact_sha256"]))
        self.assertTrue(resp["message"].endswith("[1]"))
        citations, groundings = self.service.index.retrieve_with_groundings("销售流程")
        self.assertEqual(resp["citations"], citations[:1])
        self.assertEqual(
            grounding_audit_sha256(groundings[:1]),
            resp["trace"][1]["artifact_sha256"],
        )
        # 推进 turn_id / state_revision（visitor 约定 +1）。
        self.assertEqual(1, resp["turn_id"])
        self.assertEqual(1, resp["state_revision"])
        # 响应必须通过冻结契约校验。
        self.validator.validate_response(resp, safety_gate_approved=False,
                                         readonly_execution_attempts=0)

    def test_refused_without_evidence_passes_contract(self):
        resp = self._submit("区块链共识算法")
        self.assertEqual("rag", resp["route"])
        self.assertEqual("refused", resp["status"])
        self.assertEqual([], resp["citations"])
        self.assertEqual("reject", resp["decision"]["action"])
        self.assertEqual("insufficient_evidence", resp["decision"]["reason_code"])
        self.validator.validate_response(resp, safety_gate_approved=False,
                                         readonly_execution_attempts=0)

    def test_refused_when_no_published_document(self):
        service = OnlineRagService(_FakeStore(b"", active=None))
        resp = service.submit({
            "session_id": "s1", "request_id": "r1",
            "turn_id": 0, "state_revision": 0, "question": "任意问题",
        })
        self.assertEqual("refused", resp["status"])
        self.assertEqual([], resp["citations"])
        self.validator.validate_response(resp, safety_gate_approved=False,
                                         readonly_execution_attempts=0)

    def test_citations_reference_real_document_page_evidence(self):
        resp = self._submit("销售流程")
        for citation in resp["citations"]:
            self.assertEqual("doc.abc", citation["document_id"])
            self.assertGreaterEqual(citation["page"], 1)
            self.assertTrue(citation["snippet"])
            self.assertTrue(citation["evidence_id"].startswith("doc.abc.p"))

    def test_unbound_citation_does_not_claim_exact_grounding(self):
        citation = {
            "evidence_id": "doc.abc.p1",
            "document_id": "doc.abc",
            "document_title": "示例文档",
            "page": 1,
            "snippet": "只具有页级绑定的片段。",
            "score": 2.0,
        }
        response = self.service._response(
            {
                "session_id": "s1", "request_id": "r1",
                "turn_id": 0, "state_revision": 0,
            },
            [citation],
            elapsed_ms=0,
        )
        self.assertNotIn("exact_source_grounding", response["warnings"])

    def test_repeated_quote_does_not_claim_unique_exact_grounding(self):
        citation = {
            "evidence_id": "doc.abc.p1",
            "document_id": "doc.abc",
            "document_title": "示例文档",
            "page": 1,
            "snippet": "重复证据",
            "score": 2.0,
        }
        grounding = align_exact_text("重复证据；重复证据", citation["snippet"])
        self.assertIsNotNone(grounding)
        assert grounding is not None
        response = self.service._response(
            {
                "session_id": "s1", "request_id": "r1",
                "turn_id": 0, "state_revision": 0,
            },
            [citation],
            elapsed_ms=0,
            groundings=[
                grounding.to_audit_record(
                    evidence_id=citation["evidence_id"],
                    document_id=citation["document_id"],
                    page=citation["page"],
                )
            ],
        )
        self.assertNotIn("exact_source_grounding", response["warnings"])


if __name__ == "__main__":
    unittest.main()
