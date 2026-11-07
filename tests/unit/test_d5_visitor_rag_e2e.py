"""D5 访客 RAG 检索五轮 E2E：published 文档经真实 /api/v3 产品路由检索。

覆盖 A 任务卡「D5-05-C 修复 访客检索与五轮纵切」的核心验收：
revision/turn 推进、重放、串会话、无证据拒答、disabled 文档不进检索、离线失败关闭。
离线确定性：不调用模型、不连网。
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from backend.product.document_store import DocumentAuditContext
from backend.product.server import ProductRuntime

PDF_DIR = Path(__file__).resolve().parents[2] / "data" / "raw" / "pdf"


def _request(request_id: str, **extra):
    return {"api_version": "3.0", "contract_version": "1.0.0", "request_id": request_id, **extra}


def _send(runtime, method, path, data=None, cookie=""):
    return runtime.dispatch(
        method, path,
        body=json.dumps(data).encode() if data is not None else b"{}",
        cookie_header=cookie,
    )


def _query(request_id, sid, turn, revision, question):
    return _request(request_id, session_id=sid, turn_id=turn, state_revision=revision,
                    question=question)


class VisitorRagE2ETest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runtime = ProductRuntime(self.temp.name, mode="online", master_key=b"a" * 32)
        pdf = (PDF_DIR / "sales_playbook.pdf").read_bytes()
        audit = DocumentAuditContext("req-upload", "sess-local", "test")
        status = self.runtime.document_app.document_service.upload(
            filename="sales_playbook.pdf", content_type="application/pdf",
            declared_bytes=len(pdf), content_sha256=hashlib.sha256(pdf).hexdigest(),
            file_bytes=pdf, audit=audit,
        )
        self.document_id = status.document_id
        self.runtime.document_app.document_service.publish(
            self.document_id, audit=DocumentAuditContext("req-publish", "sess-local", "test")
        )
        created = _send(self.runtime, "POST", "/api/v3/sessions", _request("req.create"))
        self.assertEqual(200, created.status)
        self.sid = json.loads(created.body)["session_id"]
        self.cookie = created.headers["Set-Cookie"].split(";", 1)[0]

    def tearDown(self):
        self.temp.cleanup()

    def _ask(self, request_id, turn, revision, question):
        response = _send(self.runtime, "POST", "/api/v3/query",
                         _query(request_id, self.sid, turn, revision, question), self.cookie)
        return response.status, json.loads(response.body)

    # ---- 五轮纵切 ----

    def test_turn1_rag_hit_citations_reference_real_document_page(self):
        status, body = self._ask("req.t1", 0, 0, "月度经营报表的截止时间")
        self.assertEqual(200, status)
        self.assertEqual("success", body["status"])
        payload = body["payload"]
        self.assertEqual("rag", payload["route"])
        self.assertTrue(payload["citations"])
        citation = payload["citations"][0]
        # 引用回指真实 document/page/evidence（上传发布的那份文档）。
        self.assertEqual(self.document_id, citation["document_id"])
        self.assertGreaterEqual(citation["page"], 1)
        self.assertTrue(citation["snippet"])
        self.assertTrue(citation["evidence_id"].startswith(self.document_id + ".p"))
        self.assertEqual(1, body["state_revision"])

    def test_turn2_no_evidence_not_answered_as_rag(self):
        status, body = self._ask("req.t2", 0, 0, "区块链共识算法")
        self.assertEqual(200, status)
        payload = body["payload"]
        # 无 RAG 证据：不得伪造 RAG 答案（无 citations、非 success）。
        # RAG refused 或回退 SQL（离线 fail-closed）均可，但绝无伪造 citations。
        self.assertNotEqual("success", body["status"])
        self.assertEqual([], payload.get("citations", []))
        self.assertEqual(1, body["state_revision"])

    def test_turn3_replay_detected(self):
        # 同一 request_id 在相同 turn/revision 下重放 → 409 operation_replayed。
        self._ask("req.t3", 0, 0, "销售流程")
        status, body = self._ask("req.t3", 0, 0, "销售流程")
        self.assertEqual(409, status)
        self.assertEqual("operation_replayed", body["error"]["code"])

    def test_turn4_cross_session_is_not_found(self):
        other = _send(self.runtime, "POST", "/api/v3/sessions", _request("req.other"))
        other_sid = json.loads(other.body)["session_id"]
        other_cookie = other.headers["Set-Cookie"].split(";", 1)[0]
        # 用他人 cookie 访问我的 session → 404（对象所有权校验）。
        status, body = self._ask("req.t4", 0, 0, "销售流程")
        self.assertEqual(200, status)  # 先正常推进一次，确认自身会话可用
        probe = _send(self.runtime, "GET", f"/api/v3/sessions/{self.sid}", cookie=other_cookie)
        self.assertEqual(404, probe.status)
        # 顺带确认他人 session 对我也 404（对称）。
        probe2 = _send(self.runtime, "GET", f"/api/v3/sessions/{other_sid}", cookie=self.cookie)
        self.assertEqual(404, probe2.status)

    def test_turn5_disabled_document_not_retrievable(self):
        # 先正常命中一次（确认 published 可检索）。
        status, body = self._ask("req.t5", 0, 0, "销售流程")
        self.assertEqual("success", body["status"])
        # 停用文档 → 活动索引失效，新会话检索不到 → 无证据。
        self.runtime.document_app.document_service.disable(
            self.document_id, audit=DocumentAuditContext("req-disable", "sess-local", "test")
        )
        created = _send(self.runtime, "POST", "/api/v3/sessions", _request("req.create2"))
        sid2 = json.loads(created.body)["session_id"]
        cookie2 = created.headers["Set-Cookie"].split(";", 1)[0]
        resp = _send(self.runtime, "POST", "/api/v3/query",
                     _query("req.t5b", sid2, 0, 0, "销售流程"), cookie2)
        body2 = json.loads(resp.body)
        # 停用后无 published 文档可检索：RAG 无证据 → 回退 SQL（离线 fail-closed）
        # 或 refused；绝不再返回指向已停用文档的 citations。
        if body2["payload"].get("route") == "rag":
            self.assertEqual([], body2["payload"]["citations"])
        else:
            self.assertNotIn(
                self.document_id,
                [c["document_id"] for c in body2["payload"].get("citations", [])],
            )

    def test_sql_fallback_fails_closed_offline(self):
        # 一个与 PDF 无关、应走 SQL 的问题：离线无 credential → fail-closed，
        # 不得静默伪造成功，也不得把 SQL 问题强路由成 RAG 答案。
        status, body = self._ask("req.sql", 0, 0, "统计艺术家总数")
        self.assertEqual(200, status)
        payload = body["payload"]
        self.assertNotEqual("success", body["status"])
        # SQL 路由离线失败关闭（route=unknown/status=error 或 refused），
        # 不会返回伪造的 RAG citations。
        if payload.get("route") == "rag":
            self.fail("SQL 问题被错误强路由为 RAG")


if __name__ == "__main__":
    unittest.main()
