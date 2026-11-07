"""D5-05 B startup, persistence and fail-closed transport checks."""

import base64
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from jsonschema import Draft202012Validator

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from backend.product.server import (ProductRuntime, ProductStartupError, ROOT, STATIC,
                                    UnreadyRuntime, _index_payload, parse_document_multipart, serve)
from backend.llm.usage import UsageRecord


class ProductServerTests(unittest.TestCase):
    def test_admin_observability_is_authenticated_bounded_and_honest(self):
        contract = json.loads((ROOT / "configs" / "contracts" / "d5" /
                               "admin-bootstrap.v2.json").read_text(encoding="utf-8"))
        schema = json.loads((ROOT / "configs" / "schemas" / "d5" /
                             "api-v3.schema.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            runtime = ProductRuntime(directory, master_key=b"a" * 32)
            login = runtime.dispatch("POST", "/api/v3/auth/admin/login", body=json.dumps({
                "api_version": "3.0", "contract_version": "2.0.0", "request_id": "req.login",
                "username": "admin", "password": contract["initial_credentials"]["initial_password"],
            }).encode())
            self.assertEqual(login.status, 200)
            cookie = login.headers["Set-Cookie"].split(";", 1)[0]
            for path in ("/api/v3/admin/health", "/api/v3/admin/audit-events",
                         "/api/v3/admin/usage-summary"):
                with self.subTest(path=path):
                    self.assertEqual(runtime.dispatch("GET", path).status, 401)
                    response = runtime.dispatch("GET", path, cookie_header=cookie)
                    self.assertEqual(response.status, 200)
                    Draft202012Validator(schema).evolve(
                        schema={"$ref": "#/$defs/admin_summary_response"}
                    ).validate(json.loads(response.body))
                    self.assertNotIn(contract["initial_credentials"]["initial_password"],
                                     response.body.decode())
            health = json.loads(runtime.dispatch("GET", "/api/v3/admin/health",
                                                 cookie_header=cookie).body)["summary"]
            self.assertEqual(health["sessions"]["active"], 0)
            self.assertEqual(health["documents"], {"total": 0, "published": 0, "ids": []})
            self.assertEqual(health["model_boundary"]["model"], "qwen3.7-flash")
            self.assertFalse(health["model_boundary"]["fallback"])
            self.assertFalse(health["secret"]["configured"])
            audit = json.loads(runtime.dispatch("GET", "/api/v3/admin/audit-events",
                                                cookie_header=cookie).body)["summary"]
            self.assertGreaterEqual(audit["count"], 1)
            self.assertLessEqual(len(audit["events"]), 20)
            self.assertEqual(audit["retention_days"], 90)
            usage = json.loads(runtime.dispatch("GET", "/api/v3/admin/usage-summary",
                                                cookie_header=cookie).body)["summary"]
            self.assertEqual(usage["model_calls"], 0)
            self.assertEqual(usage["window"], "mixed")
            self.assertEqual(usage["model_usage_window"], "process_lifetime")
            self.assertEqual(usage["session_document_window"], "instance_lifetime")

    def test_injected_online_agent_does_not_claim_zero_model_usage(self):
        contract = json.loads((ROOT / "configs" / "contracts" / "d5" /
                               "admin-bootstrap.v2.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            runtime = ProductRuntime(directory, mode="online", master_key=b"a" * 32,
                                     agent_port=object())
            login = runtime.dispatch("POST", "/api/v3/auth/admin/login", body=json.dumps({
                "api_version": "3.0", "contract_version": "2.0.0", "request_id": "req.login",
                "username": "admin", "password": contract["initial_credentials"]["initial_password"],
            }).encode())
            cookie = login.headers["Set-Cookie"].split(";", 1)[0]
            usage = json.loads(runtime.dispatch("GET", "/api/v3/admin/usage-summary",
                                                cookie_header=cookie).body)["summary"]
            self.assertIs(usage["usage_available"], False)
            self.assertNotIn("model_calls", usage)

    def test_online_usage_reads_only_process_ledger_without_provider_call(self):
        contract = json.loads((ROOT / "configs" / "contracts" / "d5" /
                               "admin-bootstrap.v2.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            runtime = ProductRuntime(directory, mode="online", master_key=b"a" * 32)
            self.assertIsNotNone(runtime._usage_ledger)
            runtime._usage_ledger.add(UsageRecord(
                recorded_at="2026-10-15T00:00:00Z", request_id="req.test",
                task_type="nl2sql", provider_id="bailian", requested_model="qwen3.7-flash",
                returned_model="qwen3.7-flash", provider_request_id=None,
                status="success", error_code=None, input_tokens=7, output_tokens=3,
                total_tokens=10, estimated_cost_cny=0.001, latency_ms=10,
            ))
            login = runtime.dispatch("POST", "/api/v3/auth/admin/login", body=json.dumps({
                "api_version": "3.0", "contract_version": "2.0.0", "request_id": "req.login",
                "username": "admin", "password": contract["initial_credentials"]["initial_password"],
            }).encode())
            cookie = login.headers["Set-Cookie"].split(";", 1)[0]
            usage = json.loads(runtime.dispatch("GET", "/api/v3/admin/usage-summary",
                                                cookie_header=cookie).body)["summary"]
            self.assertEqual(usage["model_calls"], 1)
            self.assertEqual(usage["input_tokens"], 7)
            self.assertEqual(usage["output_tokens"], 3)
            self.assertEqual(usage["estimated_cost_cny"], 0.001)

    def test_online_index_is_live_and_offline_index_is_mock(self):
        original = (STATIC / "index.html").read_bytes()
        self.assertEqual(_index_payload(original, "offline"), original)
        live = _index_payload(original, "online").decode("utf-8")
        self.assertIn("window.D5_LIVE_API = true;", live)
        self.assertLess(live.index("window.D5_LIVE_API = true;"), live.index('src="./app.js"'))
        self.assertIn("在线模式 · 连接真实服务", live)
        self.assertIn("提交问题后可能调用管理员配置的模型", live)
        self.assertNotIn("唯一允许的模型", live)
        self.assertNotIn("演示模式", live)
        self.assertNotIn("模型调用 0 次", live)
        with self.assertRaises(ProductStartupError):
            _index_payload(original.replace(b'src="./app.js"', b'src="./changed.js"'), "online")

    def test_online_http_serves_live_console_without_model_call(self):
        with tempfile.TemporaryDirectory() as directory:
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
            env = os.environ.copy()
            env.pop("ICT_MASTER_KEY_FILE", None)
            env["ICT_SECRET_MASTER_KEY_B64"] = base64.b64encode(b"a" * 32).decode("ascii")
            env["PYTHONPATH"] = str(ROOT / "src")
            process = subprocess.Popen(
                [sys.executable, "-m", "backend.product.server", "serve", "--instance", directory,
                 "--mode", "online", "--port", str(port)],
                env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            try:
                for _ in range(50):
                    if process.poll() is not None:
                        self.fail("online_product_server_exited")
                    try:
                        conn = HTTPConnection("127.0.0.1", port, timeout=2)
                        conn.request("GET", "/health/ready")
                        ready = conn.getresponse().status
                        conn.close()
                        if ready == 200:
                            break
                    except OSError:
                        pass
                    time.sleep(0.1)
                else:
                    self.fail("online_product_server_not_ready")
                for path in ("/", "/index.html"):
                    conn = HTTPConnection("127.0.0.1", port, timeout=2)
                    try:
                        conn.request("GET", path)
                        response = conn.getresponse()
                        self.assertEqual(response.status, 200)
                        self.assertEqual(response.getheader("Cache-Control"), "no-store")
                        self.assertIn(b"window.D5_LIVE_API = true;", response.read())
                    finally:
                        conn.close()
                conn = HTTPConnection("127.0.0.1", port, timeout=2)
                try:
                    conn.request("GET", "/d4-console/mock/index.js")
                    self.assertEqual(conn.getresponse().status, 200)
                finally:
                    conn.close()
            finally:
                process.terminate()
                process.wait(timeout=5)

    def test_actual_bind_address_controls_default_password_readiness(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = ProductRuntime(directory, master_key=b"a" * 32)
            self.assertTrue(runtime.ready())
            with patch.dict(os.environ, {"ICT_DOCKER_BIND": "1"}), patch(
                "backend.product.server.ThreadingHTTPServer"
            ) as server:
                serve(runtime, host="0.0.0.0", port=0)
                server.assert_called_once()
            self.assertFalse(runtime.ready())

    def test_v2_default_admin_optional_change_and_nonloopback_gate(self):
        contract = json.loads((ROOT / "configs" / "contracts" / "d5" /
                               "admin-bootstrap.v2.json").read_text(encoding="utf-8"))
        password = contract["initial_credentials"]["initial_password"]
        schema = json.loads((ROOT / "configs" / "schemas" / "d5" /
                             "admin-bootstrap-api.v2.schema.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            runtime = ProductRuntime(directory, master_key=b"a" * 32)
            state = runtime.auth.store.read_state()
            self.assertEqual(state["username"], "admin")
            self.assertTrue(state["password_hash"].startswith("$argon2id$"))
            self.assertTrue(runtime.auth.default_password_active())
            self.assertNotIn(password, runtime.auth.store.persisted_values())
            login_body = {"api_version": "3.0", "contract_version": "2.0.0",
                          "request_id": "req.login", "username": "admin", "password": password}
            login = runtime.dispatch("POST", "/api/v3/auth/admin/login",
                                     body=json.dumps(login_body).encode())
            self.assertEqual(login.status, 200)
            payload = json.loads(login.body)
            Draft202012Validator(schema).evolve(schema={"$ref": "#/$defs/admin_session_response"}).validate(payload)
            self.assertEqual(payload["allowed_actions"], ["admin", "change_password", "logout"])
            cookie = login.headers["Set-Cookie"].split(";", 1)[0]
            self.assertEqual(runtime.dispatch("GET", "/api/v3/admin/health", cookie_header=cookie).status, 200)
            outside = ProductRuntime(directory, master_key=b"a" * 32, bind_host="0.0.0.0")
            self.assertFalse(outside.ready())
            change = {"api_version": "3.0", "contract_version": "2.0.0", "request_id": "req.change",
                      "current_password": password, "new_password": "testonly-new-password-123"}
            changed = runtime.dispatch("POST", "/api/v3/admin/password", body=json.dumps(change).encode(),
                                       cookie_header=cookie, csrf=payload["csrf_token"])
            self.assertEqual(changed.status, 200)
            Draft202012Validator(schema).evolve(schema={"$ref": "#/$defs/password_change_response"}).validate(
                json.loads(changed.body))
            self.assertFalse(runtime.auth.default_password_active())
            self.assertTrue(outside.ready())
            self.assertEqual(runtime.dispatch("GET", "/api/v3/admin/health", cookie_header=cookie).status, 401)
            self.assertEqual(runtime.dispatch("POST", "/api/v3/auth/admin/login",
                                              body=json.dumps(login_body).encode()).status, 401)
            login_body["password"] = "testonly-new-password-123"
            self.assertEqual(runtime.dispatch("POST", "/api/v3/auth/admin/login",
                                              body=json.dumps(login_body).encode()).status, 200)
            restarted = ProductRuntime(directory, master_key=b"a" * 32)
            self.assertFalse(restarted.auth.default_password_active())
            self.assertEqual(restarted.auth.store.read_state()["username"], "admin")

    def test_every_admin_stub_authenticates_before_responding(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = ProductRuntime(directory, master_key=b"a" * 32)
            for method, path in (("GET", "/api/v3/admin/audit-events"),
                                 ("GET", "/api/v3/admin/usage-summary"),
                                 ("POST", "/api/v3/admin/password"),
                                 ("GET", "/api/v3/admin/unknown"),
                                 ("POST", "/api/v3/admin/documents"),
                                 ("GET", "/api/v3/admin/secrets/bailian")):
                with self.subTest(path=path):
                    response = runtime.dispatch(method, path)
                    self.assertEqual(response.status, 401)
                    self.assertEqual(json.loads(response.body)["error"]["code"], "authentication_required")

    def test_real_http_process_restart_keeps_guest_session(self):
        with tempfile.TemporaryDirectory() as directory:
            ProductRuntime(directory, master_key=b"a" * 32)
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
            env = os.environ.copy()
            env.pop("ICT_MASTER_KEY_FILE", None)
            env["ICT_SECRET_MASTER_KEY_B64"] = base64.b64encode(b"a" * 32).decode("ascii")
            env["PYTHONPATH"] = str(ROOT / "src")

            def request(method, path, body=None, headers=None):
                connection = HTTPConnection("127.0.0.1", port, timeout=2)
                try:
                    connection.request(method, path, body=body, headers=headers or {})
                    response = connection.getresponse()
                    return response.status, dict(response.getheaders()), response.read()
                finally:
                    connection.close()

            def start():
                process = subprocess.Popen(
                    [sys.executable, "-m", "backend.product.server", "serve", "--instance", directory,
                     "--mode", "offline", "--port", str(port)],
                    env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                for _ in range(50):
                    if process.poll() is not None:
                        self.fail("product_server_exited")
                    try:
                        if request("GET", "/health/ready")[0] == 200:
                            return process
                    except OSError:
                        pass
                    time.sleep(0.1)
                process.terminate()
                self.fail("product_server_not_ready")

            first = start()
            try:
                self.assertEqual(request("GET", "/")[0], 200)
                self.assertEqual(request("GET", "/mock/index.js")[0], 200)
                self.assertEqual(request("GET", "/d4-console/rag-manifest.js")[0], 200)
                self.assertEqual(request("GET", "/d4-console/mock/index.js")[0], 200)
                self.assertEqual(request("GET", "/api/v3/admin/health")[0], 401)
                payload = json.dumps({"api_version": "3.0", "contract_version": "1.0.0",
                                      "request_id": "req.create"}).encode("utf-8")
                status, headers, body = request("POST", "/api/v3/sessions", payload,
                                                {"Content-Type": "application/json"})
                self.assertEqual(status, 200)
                session_id = json.loads(body)["session_id"]
                guest_cookie = headers["Set-Cookie"].split(";", 1)[0]
            finally:
                first.terminate()
                first.wait(timeout=5)
            second = start()
            try:
                self.assertEqual(request("GET", "/api/v3/sessions/" + session_id,
                                         headers={"Cookie": guest_cookie})[0], 200)
                self.assertEqual(request("GET", "/api/v3/sessions/" + session_id)[0], 401)
            finally:
                second.terminate()
                second.wait(timeout=5)

    def test_unready_surface_exposes_only_health(self):
        self.assertEqual(UnreadyRuntime.dispatch("GET", "/health/live").status, 200)
        self.assertEqual(UnreadyRuntime.dispatch("GET", "/health/ready").status, 503)
        self.assertEqual(UnreadyRuntime.dispatch("POST", "/api/v3/sessions").status, 503)

    def test_restart_preserves_admin_and_secret_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = ProductRuntime(directory, master_key=b"a" * 32)
            runtime.secret_store.put_if_absent("sk-testonly-0123456789")
            document_id = "doc.restart"
            version = "idx.0123456789ab"
            (Path(directory) / "documents" / "index" / (version + ".json")).write_text("{}")
            runtime.documents.create_record(document_id, sha256="a" * 64, bytes_count=9)
            runtime.documents.compare_and_set_state(
                document_id, expected_state="quarantined", new_state="validated",
                page_count=1, index_version=version, failure_code=None,
            )
            runtime.documents.publish_document(document_id)
            restarted = ProductRuntime(directory, master_key=b"a" * 32)
            self.assertTrue(restarted.ready())
            self.assertEqual(restarted.auth.store.read_state()["username"], "admin")
            self.assertTrue(restarted.secret_store.status().configured)
            self.assertEqual(restarted.documents.get_active_index(), version)
            self.assertEqual(restarted.documents.get_record(document_id).state, "published")
            self.assertNotIn("sk-testonly-0123456789", (Path(directory) / "secrets.sqlite3").read_bytes().decode("latin1"))

    def test_wrong_or_missing_master_key_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = ProductRuntime(directory, master_key=b"a" * 32)
            runtime.secret_store.put_if_absent("sk-testonly-0123456789")
            with self.assertRaises(ProductStartupError):
                ProductRuntime(directory, master_key=b"b" * 32)

    def test_corrupt_storage_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            ProductRuntime(directory, master_key=b"a" * 32)
            (Path(directory) / "metadata.sqlite3").write_bytes(b"not-sqlite")
            with self.assertRaises(ProductStartupError) as raised:
                ProductRuntime(directory, master_key=b"a" * 32)
            self.assertEqual(raised.exception.code, "storage_corrupt")

    def test_missing_document_state_or_active_index_fails_readiness(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = ProductRuntime(directory, master_key=b"a" * 32)
            self.assertTrue(runtime.ready())
            index = Path(directory) / "documents" / "index"
            index.rmdir()
            self.assertFalse(runtime.ready())
            self.assertEqual(runtime.dispatch("GET", "/health/ready").status, 503)
            index.mkdir()
            marker = index / "idx.0123456789ab.json"
            marker.write_text("{}", encoding="utf-8")
            with runtime.auth.store._connect() as connection:
                connection.execute("INSERT INTO active_index(singleton,index_version) VALUES (1,?)",
                                   ("idx.0123456789ab",))
            self.assertTrue(runtime.ready())
            marker.unlink()
            self.assertFalse(runtime.ready())

    def test_health_and_offline_answer_are_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = ProductRuntime(directory, master_key=b"a" * 32)
            self.assertEqual(runtime.dispatch("GET", "/health/live").status, 200)
            self.assertEqual(runtime.dispatch("GET", "/health/ready").status, 200)
            created = runtime.dispatch("POST", "/api/v3/sessions", body=json.dumps({
                "api_version": "3.0", "contract_version": "1.0.0", "request_id": "req.create"
            }).encode())
            sid = json.loads(created.body)["session_id"]
            response = runtime.dispatch("POST", "/api/v3/query", body=json.dumps({
                "api_version": "3.0", "contract_version": "1.0.0", "request_id": "req.query",
                "session_id": sid, "turn_id": 0, "state_revision": 0, "question": "统计艺术家数量"
            }).encode(), cookie_header=created.headers["Set-Cookie"].split(";", 1)[0])
            self.assertEqual(response.status, 503)
            self.assertEqual(json.loads(response.body)["error"]["code"], "service_unavailable")
            response = runtime.dispatch("GET", "/api/v3/admin/health")
            self.assertEqual(response.status, 401)

    def test_offline_demo_is_separate_and_discloses_zero_model_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = ProductRuntime(directory, master_key=b"a" * 32)
            page = runtime.dispatch("GET", "/demo")
            self.assertEqual(page.status, 200)
            self.assertIn(b"OFFLINE PUBLIC DEMO", page.body)
            self.assertEqual(runtime.dispatch("GET", "/assets/app.js").status, 200)
            health = runtime.dispatch("GET", "/api/v1/health")
            self.assertEqual(json.loads(health.body)["model_api_calls"], 0)

    def test_instance_inside_checkout_is_rejected(self):
        with self.assertRaises(ProductStartupError) as raised:
            ProductRuntime(ROOT / "instance", master_key=b"a" * 32)
        self.assertEqual(raised.exception.code, "invalid_instance_directory")

    def test_master_key_file_inside_checkout_is_rejected_before_read(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"ICT_MASTER_KEY_FILE": str(ROOT / ".env.example")}):
                with self.assertRaises(ProductStartupError) as raised:
                    ProductRuntime(directory)
            self.assertEqual(raised.exception.code, "service_unavailable")

    def test_online_mode_wires_internal_rag_first_agent(self):
        # C 接线：online 模式不再 fail-closed，而是内部构造 RAG 优先 agent
        # （RagFirstAgent 包装 OnlineRagService + VisitorSqlAdapter）。
        from backend.product.visitor_rag import RagFirstAgent
        with tempfile.TemporaryDirectory() as directory:
            runtime = ProductRuntime(directory, mode="online", master_key=b"a" * 32)
        self.assertIsInstance(runtime.visitor_app.agent, RagFirstAgent)
        self.assertIsNotNone(runtime.visitor_app.agent.sql)
        self.assertIsNotNone(runtime.visitor_app.agent.rag)


    def test_multipart_requires_exact_metadata_and_pdf_parts(self):
        boundary = b"test-boundary"
        body = (b"--" + boundary + b"\r\nContent-Disposition: form-data; name=\"metadata\"\r\n"
                b"Content-Type: application/json\r\n\r\n{}\r\n--" + boundary +
                b"\r\nContent-Disposition: form-data; name=\"file\"; filename=\"sample.pdf\"\r\n"
                b"Content-Type: application/pdf\r\n\r\n%PDF-fake\r\n--" + boundary + b"--\r\n")
        self.assertEqual(parse_document_multipart("multipart/form-data; boundary=test-boundary", body),
                         (b"{}", b"%PDF-fake"))
        with self.assertRaises(ValueError):
            parse_document_multipart("multipart/form-data; boundary=wrong", body)
        with self.assertRaises(ValueError):
            parse_document_multipart("multipart/form-data; boundary=test-boundary", body + b"garbage")

    def test_multipart_requires_authorization_before_parse(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = ProductRuntime(directory, master_key=b"a" * 32)
            response = runtime.dispatch("POST", "/api/v3/admin/documents", body=b"malformed",
                                        content_type="multipart/form-data; boundary=x")
            self.assertEqual(response.status, 401)


if __name__ == "__main__":
    unittest.main()
