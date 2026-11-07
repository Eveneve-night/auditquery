"""D5 RagFirstAgent dispatcher 单元测试：RAG 优先、无证据回退 SQL、澄清走 SQL。"""

from __future__ import annotations

import sys
import hashlib
import unittest
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from backend.api.online_rag_service import OnlineRagService
from backend.product.visitor_rag import RagFirstAgent


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
    def __init__(self, pdf_bytes, active=None):
        self._pdf = pdf_bytes
        self._active = active
        self._records = [_FakeRecord("doc.abc", "published", "idx.1")] if active else []

    def get_active_index(self):
        return self._active

    def list_records(self):
        return self._records

    def read_quarantine(self, document_id):
        return self._pdf


class _FakeSqlAdapter:
    """最小 SQL adapter：记录是否被调用，返回 route=sql 占位响应。"""

    def __init__(self):
        self.submit_calls = 0
        self.transaction_calls = 0

    def submit(self, request):
        self.submit_calls += 1
        return {
            "schema_version": "2.0.0", "session_id": request["session_id"],
            "request_id": request["request_id"],
            "turn_id": int(request["turn_id"]) + 1,
            "state_revision": int(request["state_revision"]) + 1,
            "route": "sql", "status": "refused", "message": "sql fallback",
            "decision": None, "pending_question": None, "candidate_evidence": [],
            "sql": None, "citations": [], "trace": [],
            "usage": {"model_calls": 0, "input_tokens": 0, "output_tokens": 0,
                      "estimated_cost_cny": 0, "elapsed_ms": 0.0, "stopped_reason": None},
            "error": None, "warnings": [],
        }

    @contextmanager
    def transaction(self, request):
        self.transaction_calls += 1
        result = self.submit(request)
        yield result, lambda: None


def _sample_pdf() -> bytes:
    return (Path(__file__).resolve().parents[2] / "data" / "raw" / "pdf" /
            "sales_playbook.pdf").read_bytes()


def _query(question, session_id="s1"):
    return {"session_id": session_id, "request_id": "r1",
            "turn_id": 0, "state_revision": 0, "question": question}


def _clarification():
    return {"session_id": "s1", "request_id": "r1", "answer_id": "a1",
            "turn_id": 1, "state_revision": 1, "question_id": "q1",
            "slot_id": "sl1", "values": ["x"], "evidence_refs": ["e1"]}


class RagFirstAgentTest(unittest.TestCase):
    def setUp(self):
        store = _FakeStore(_sample_pdf(), active="idx.1")
        self.rag = OnlineRagService(store)
        self.sql = _FakeSqlAdapter()
        self.agent = RagFirstAgent(self.rag, self.sql)

    def test_rag_hit_returns_rag_route(self):
        resp = self.agent.submit(_query("销售流程"))
        self.assertEqual("rag", resp["route"])
        self.assertTrue(resp["citations"])
        self.assertEqual(0, self.sql.submit_calls)

    def test_no_evidence_falls_back_to_sql(self):
        resp = self.agent.submit(_query("区块链共识算法"))
        self.assertEqual("sql", resp["route"])
        self.assertEqual(1, self.sql.submit_calls)

    def test_clarification_always_goes_to_sql(self):
        resp = self.agent.submit(_clarification())
        self.assertEqual("sql", resp["route"])
        self.assertEqual(1, self.sql.submit_calls)

    def test_transaction_rag_hit_yields_rag_without_sql(self):
        with self.agent.transaction(_query("销售流程")) as (resp, accept):
            self.assertEqual("rag", resp["route"])
            accept()
        self.assertEqual(0, self.sql.submit_calls)
        self.assertEqual(0, self.sql.transaction_calls)

    def test_transaction_delegates_to_sql_when_no_evidence(self):
        with self.agent.transaction(_query("区块链共识算法")) as (resp, accept):
            self.assertEqual("sql", resp["route"])
            accept()
        self.assertEqual(1, self.sql.transaction_calls)

    def test_transaction_clarification_delegates_to_sql(self):
        with self.agent.transaction(_clarification()) as (resp, accept):
            self.assertEqual("sql", resp["route"])
            accept()
        self.assertEqual(1, self.sql.transaction_calls)


if __name__ == "__main__":
    unittest.main()
