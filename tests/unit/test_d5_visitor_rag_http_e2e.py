"""D5 访客 RAG 检索的真实 HTTP 五轮纵切（online 模式，同一会话贯穿）。

与 ``test_d5_visitor_rag_e2e.py``（进程内直调 ``runtime.dispatch``）不同，本文件
通过 ``subprocess`` 启动真实的 ``backend.product.server serve`` 进程，用
``http.client`` 经 ``127.0.0.1:<port>`` 发送真实 HTTP 请求，证明访客问答在
**真实 HTTP 服务**上贯穿同一会话连续多轮，并覆盖重放 / 过期 / 串会话 /
无证据不伪造 / 离线失败关闭 / 澄清提交闭环。

离线确定性：不调用模型、不连网、不读取真实 Key（仅用假主密钥
``b"a"*32`` 与本机回环地址）。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from http.client import HTTPConnection

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from backend.product.document_store import DocumentAuditContext
from backend.product.server import ProductRuntime

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PDF_DIR = PROJECT_ROOT / "data" / "raw" / "pdf"

MASTER_KEY = b"a" * 32
MASTER_KEY_B64 = base64.b64encode(MASTER_KEY).decode("ascii")


def _envelope(request_id: str, **extra):
    return {"api_version": "3.0", "contract_version": "1.0.0", "request_id": request_id, **extra}


class VisitorRagHttpE2ETest(unittest.TestCase):
    """真实 HTTP 五轮纵切：online 模式 subprocess + http.client。"""

    @classmethod
    def setUpClass(cls):
        cls._temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.instance = Path(cls._temp.name)
        # 预置 published 文档（直接写实例目录，不走 HTTP admin 上传；admin 上传属 D5-06）。
        runtime = ProductRuntime(cls.instance, mode="online", master_key=MASTER_KEY)
        pdf = (PDF_DIR / "sales_playbook.pdf").read_bytes()
        upload = runtime.document_app.document_service.upload(
            filename="sales_playbook.pdf", content_type="application/pdf",
            declared_bytes=len(pdf), content_sha256=hashlib.sha256(pdf).hexdigest(),
            file_bytes=pdf, audit=DocumentAuditContext("req-upload", "sess-local", "test"),
        )
        cls.document_id = upload.document_id
        runtime.document_app.document_service.publish(
            cls.document_id, audit=DocumentAuditContext("req-publish", "sess-local", "test"),
        )

        # 起真实 server 进程（online 模式）。
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            cls.port = reservation.getsockname()[1]
        env = os.environ.copy()
        env.pop("ICT_MASTER_KEY_FILE", None)
        env["ICT_SECRET_MASTER_KEY_B64"] = MASTER_KEY_B64
        env["PYTHONPATH"] = str(PROJECT_ROOT / "src")
        cls._process = subprocess.Popen(
            [sys.executable, "-m", "backend.product.server", "serve",
             "--instance", str(cls.instance), "--mode", "online", "--port", str(cls.port)],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        for _ in range(100):
            if cls._process.poll() is not None:
                cls._process.terminate()
                raise RuntimeError("product_server_exited")
            try:
                if cls._raw("GET", "/health/ready")[0] == 200:
                    break
            except OSError:
                pass
            time.sleep(0.1)
        else:
            cls._process.terminate()
            raise RuntimeError("product_server_not_ready")

    @classmethod
    def tearDownClass(cls):
        cls._process.terminate()
        cls._process.wait(timeout=10)
        # Windows 上 TerminateProcess 后 sqlite 文件句柄由 OS 异步回收，稍等再清理。
        time.sleep(0.3)
        cls._temp.cleanup()

    def setUp(self):
        # 每用例重置访客限流表，隔离共享 source 桶（同一 server + 同一客户端 IP），
        # 避免限流计数在用例间串扰；限流行为本身由 test_rate_limit_returns_429_over_http 单独验证。
        db = self.instance / "metadata.sqlite3"
        with sqlite3.connect(db, timeout=5) as connection:
            connection.execute("DELETE FROM visitor_create_limits")
            connection.execute("DELETE FROM visitor_operation_limits")
            connection.commit()

    @staticmethod
    def _raw(method, path, body=None, headers=None):
        connection = HTTPConnection("127.0.0.1", VisitorRagHttpE2ETest.port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    @staticmethod
    def _post(path, payload, cookie=None):
        headers = {"Content-Type": "application/json"}
        if cookie:
            headers["Cookie"] = cookie
        status, resp_headers, body = VisitorRagHttpE2ETest._raw(
            "POST", path, json.dumps(payload).encode("utf-8"), headers,
        )
        return status, resp_headers, json.loads(body)

    def _create_session(self):
        status, headers, body = self._post(
            "/api/v3/sessions", _envelope("req.create"),
        )
        self.assertEqual(200, status)
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        return body["session_id"], cookie

    def _query(self, sid, cookie, request_id, turn, revision, question):
        status, _, body = self._post(
            "/api/v3/query",
            _envelope(request_id, session_id=sid, turn_id=turn, state_revision=revision,
                      question=question),
            cookie=cookie,
        )
        return status, body

    # ---- 核心：真实 HTTP 五轮贯穿同一会话 ----

    def test_five_turns_same_session_over_http(self):
        sid, cookie = self._create_session()
        turn, revision = 0, 0

        # 轮 1：RAG 命中 → route=rag / success / citations 回指真实 document/page。
        status, body = self._query(sid, cookie, "req.t1", turn, revision, "销售流程")
        self.assertEqual(200, status)
        self.assertEqual("success", body["status"])
        self.assertEqual("rag", body["payload"]["route"])
        self.assertTrue(body["payload"]["citations"])
        citation = body["payload"]["citations"][0]
        self.assertEqual(self.document_id, citation["document_id"])
        self.assertTrue(citation["evidence_id"].startswith(self.document_id + ".p"))
        revision = body["state_revision"]
        turn += 1
        self.assertEqual(1, revision)

        # 轮 2：未指定指标且无 RAG 证据 → SQL 澄清（明确计数问题不再重复澄清）。
        status, body = self._query(sid, cookie, "req.t2", turn, revision, "艺术家")
        self.assertEqual(200, status)
        self.assertEqual("clarification_required", body["status"])
        self.assertEqual("clarification", body["payload"]["route"])
        self.assertEqual([], body["payload"].get("citations", []))
        pending = body["payload"]["pending_question"]
        self.assertEqual(sid, pending["session_id"])
        revision = body["state_revision"]
        turn += 1
        self.assertEqual(2, revision)

        # 轮 3：澄清提交（metric 槽位）→ 仍澄清（槽位未满），revision 推进。
        option = pending["options"][0]
        status, _, body = self._post(
            "/api/v3/clarifications",
            _envelope("req.t2", answer_id="ans.five.turn3", session_id=sid,
                      turn_id=pending["turn_id"], state_revision=pending["state_revision"],
                      question_id=pending["question_id"], slot_id=pending["slot_id"],
                      values=[option["value"]], evidence_refs=[option["evidence_ref"]]),
            cookie=cookie,
        )
        self.assertEqual(200, status)
        self.assertEqual("clarification_required", body["status"])
        self.assertEqual("clarification", body["payload"]["route"])
        revision = body["state_revision"]
        turn += 1
        self.assertEqual(3, revision)

        # 轮 4：再次 RAG 命中，证明同一会话在澄清后仍能回到 RAG 路由。
        status, body = self._query(sid, cookie, "req.t4", turn, revision, "月度经营报表的截止时间")
        self.assertEqual(200, status)
        self.assertEqual("success", body["status"])
        self.assertEqual("rag", body["payload"]["route"])
        self.assertTrue(body["payload"]["citations"])
        revision = body["state_revision"]
        turn += 1
        self.assertEqual(4, revision)

        # 轮 5：无证据问题 → 非 success，且绝不伪造 citations。
        status, body = self._query(sid, cookie, "req.t5", turn, revision, "区块链共识算法")
        self.assertEqual(200, status)
        self.assertNotEqual("success", body["status"])
        self.assertEqual([], body["payload"].get("citations", []))
        self.assertEqual(5, body["state_revision"])

    # ---- 负例 ----

    def test_replay_returns_409(self):
        sid, cookie = self._create_session()
        status, body = self._query(sid, cookie, "req.rp", 0, 0, "销售流程")
        self.assertEqual(200, status)
        # 同 request_id 同 turn/revision 重放 → 409 operation_replayed。
        status, body = self._query(sid, cookie, "req.rp", 0, 0, "销售流程")
        self.assertEqual(409, status)
        self.assertEqual("operation_replayed", body["error"]["code"])

    def test_cross_session_returns_404(self):
        sid, cookie = self._create_session()
        other_sid, other_cookie = self._create_session()
        # 用他人 cookie 读我的 session → 404（对象所有权校验）。
        status, _, body = self._raw(
            "GET", f"/api/v3/sessions/{sid}", headers={"Cookie": other_cookie},
        )
        self.assertEqual(404, status)
        self.assertEqual("resource_not_found", json.loads(body)["error"]["code"])

    def test_expired_session_returns_401(self):
        sid, cookie = self._create_session()
        db = self.instance / "metadata.sqlite3"
        with sqlite3.connect(db, timeout=5) as connection:
            connection.execute(
                "UPDATE visitor_sessions SET expires_at=? WHERE session_id=?",
                ("2000-01-01T00:00:00Z", sid),
            )
            connection.commit()
        status, body = self._query(sid, cookie, "req.exp", 0, 0, "销售流程")
        self.assertEqual(401, status)
        self.assertEqual("authentication_required", body["error"]["code"])

    def test_no_evidence_not_forged_as_rag(self):
        sid, cookie = self._create_session()
        status, body = self._query(sid, cookie, "req.noev", 0, 0, "区块链共识算法")
        self.assertEqual(200, status)
        payload = body["payload"]
        self.assertNotEqual("success", body["status"])
        self.assertEqual([], payload.get("citations", []))
        self.assertNotEqual("rag", payload.get("route"))

    def test_sql_offline_fails_closed(self):
        # 一个应走 SQL 的问题：离线无 credential → 不返回 success + SQL 执行结果，
        # 也不调用模型（model_calls=0），而是要求澄清或拒绝。
        sid, cookie = self._create_session()
        status, body = self._query(sid, cookie, "req.sql", 0, 0, "统计艺术家总数")
        self.assertEqual(200, status)
        payload = body["payload"]
        self.assertNotEqual("success", body["status"])
        self.assertNotEqual("rag", payload.get("route"))
        self.assertEqual(0, payload["usage"]["model_calls"])
        # 离线失败关闭：不伪造带 citations 的 RAG 答案。
        self.assertEqual([], payload.get("citations", []))

    def test_clarification_roundtrip_and_replay(self):
        sid, cookie = self._create_session()
        _, body = self._query(sid, cookie, "req.cl", 0, 0, "艺术家")
        self.assertEqual("clarification_required", body["status"])
        pending = body["payload"]["pending_question"]
        option = pending["options"][0]
        clarify = _envelope(
            "req.cl", answer_id="ans.clarify", session_id=sid,
            turn_id=pending["turn_id"], state_revision=pending["state_revision"],
            question_id=pending["question_id"], slot_id=pending["slot_id"],
            values=[option["value"]], evidence_refs=[option["evidence_ref"]],
        )
        status, _, body = self._post("/api/v3/clarifications", clarify, cookie=cookie)
        self.assertEqual(200, status)
        # 重放同一 answer_id → 409 operation_replayed。
        status, _, body = self._post("/api/v3/clarifications", clarify, cookie=cookie)
        self.assertEqual(409, status)
        self.assertEqual("operation_replayed", body["error"]["code"])

    def test_rate_limit_returns_429_over_http(self):
        # 访客操作限流：同一会话 60 次合法操作后，第 61 次返回 429，且不推进会话。
        sid, cookie = self._create_session()
        turn, revision = 0, 0
        for number in range(60):
            status, body = self._query(sid, cookie, f"req.rl{number}", turn, revision, "销售流程")
            self.assertEqual(200, status)
            revision = body["state_revision"]
            turn += 1
        status, body = self._query(sid, cookie, "req.rl61", turn, revision, "销售流程")
        self.assertEqual(429, status)
        self.assertEqual("rate_limited", body["error"]["code"])
        self.assertTrue(body["error"]["retryable"])
        # 429 不改变会话 revision（限流发生在 agent 之前，不计入提交）。
        self.assertEqual(60, revision)

    def test_invalid_json_body_returns_400_over_http(self):
        sid, cookie = self._create_session()
        status, headers, raw = self._raw(
            "POST", "/api/v3/query", body=b"{not-valid-json",
            headers={"Content-Type": "application/json", "Cookie": cookie},
        )
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", json.loads(raw)["error"]["code"])


if __name__ == "__main__":
    unittest.main()
