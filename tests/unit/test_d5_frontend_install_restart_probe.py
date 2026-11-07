"""D5-05-C 安装/重启验收探针脚本的单元测试（防空转 + 逻辑自洽）。

验证 `tools/evaluation/validate_d5_05_c_install_restart.py` 的：
- 离线预检（C 前端文件齐全性、B 后端交付如实反映）
- multipart 构造与 B parse_document_multipart 的字段/Content-Type 对齐
- health / static / visitor / multipart 探测在 mock HTTP 下的判定逻辑
- pending/pass/fail 三态编排

变异验证锚点：metadata=application/json、file=application/pdf、x-csrf-token、
STATIC_MANIFEST 含 /live-transport.js。变异上述任一常量，对应断言必须变红。
"""

from __future__ import annotations

import hashlib
import http.cookiejar
import urllib.request
import importlib.util
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TOOL = PROJECT_ROOT / "tools" / "evaluation" / "validate_d5_05_c_install_restart.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("validate_d5_05_c_install_restart", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mod = _load_module()


class TestOfflinePrereq(unittest.TestCase):
    def test_static_manifest_is_complete_on_c_branch(self) -> None:
        offline = mod.offline_static_check()
        self.assertTrue(offline["frontend_files_complete"], f"缺失文件：{offline['static_files_missing']}")
        self.assertEqual(offline["static_files_missing"], [])

    def test_b_server_presence_matches_filesystem(self) -> None:
        offline = mod.offline_static_check()
        self.assertEqual(offline["b_server_present"], mod.B_SERVER.is_file())
        self.assertEqual(offline["b_server"], "src/backend/product/server.py")

    def test_static_manifest_includes_live_transport(self) -> None:
        # 变异锚点：/live-transport.js 是 #78 新增，若从白名单清单漏掉则 C 侧无法验收自己的接线文件。
        urls = {url for url, _local, _ctype in mod.STATIC_MANIFEST}
        self.assertIn("/live-transport.js", urls)
        self.assertIn("/mock/index.js", urls)
        self.assertIn("/d4-console/rag-manifest.js", urls)


class TestMultipartBuild(unittest.TestCase):
    def test_metadata_part_declares_application_json(self) -> None:
        # 变异锚点：metadata Content-Type 必须是 application/json（B 会拒绝 text/plain）。
        fake = b"%PDF-1.4 probe\n%%EOF\n"
        meta = mod._make_metadata("req.t", fake)
        body, content_type = mod.build_multipart(meta, fake, "wbprobe0001")
        self.assertTrue(content_type.startswith("multipart/form-data; boundary="))
        self.assertIn(b"Content-Type: application/json", body)
        self.assertIn(b"Content-Type: application/pdf", body)

    def test_build_multipart_has_exactly_metadata_and_file(self) -> None:
        from email import policy
        from email.parser import BytesParser
        fake = b"%PDF-1.4 probe\n%%EOF\n"
        meta = mod._make_metadata("req.t", fake)
        body, content_type = mod.build_multipart(meta, fake, "wbprobe0001")
        header = b"Content-Type: " + content_type.encode("ascii") + b"\r\nMIME-Version: 1.0\r\n\r\n"
        message = BytesParser(policy=policy.default).parsebytes(header + body)
        self.assertTrue(message.is_multipart())
        names = {part.get_param("name", header="content-disposition") for part in message.iter_parts()}
        self.assertEqual(names, {"metadata", "file"})
        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            if name == "metadata":
                self.assertEqual(part.get_content_type(), "application/json")
                payload = json.loads(part.get_payload(decode=True).decode("utf-8"))
                self.assertEqual(payload["content_sha256"], hashlib.sha256(fake).hexdigest())
                self.assertEqual(payload["declared_bytes"], len(fake))
            else:
                self.assertEqual(part.get_content_type(), "application/pdf")
                self.assertEqual(part.get_payload(decode=True), fake)

    def test_make_metadata_sha256_matches_bytes(self) -> None:
        fake = b"%PDF-1.4\ncontent\n%%EOF\n"
        meta = mod._make_metadata("req.t", fake)
        self.assertEqual(meta["content_sha256"], hashlib.sha256(fake).hexdigest())
        self.assertEqual(meta["content_type"], "application/pdf")
        self.assertEqual(meta["filename"], "probe.pdf")


class TestLiveChecks(unittest.TestCase):
    def test_deep_upload_uses_cookiejar_and_never_expects_response_session_token(self) -> None:
        seen = []

        def fake_http(base, path, **kwargs):
            seen.append((path, kwargs))
            opener = kwargs.get("opener")
            self.assertIsNotNone(opener)
            if path == "/api/v3/auth/admin/login":
                processor = next(h for h in opener.handlers if isinstance(h, urllib.request.HTTPCookieProcessor))
                processor.cookiejar.set_cookie(http.cookiejar.Cookie(
                    version=0, name="ict_admin_session", value="testonly-cookie", port=None,
                    port_specified=False, domain="127.0.0.1", domain_specified=False,
                    domain_initial_dot=False, path="/", path_specified=True, secure=False,
                    expires=None, discard=True, comment=None, comment_url=None,
                    rest={"HttpOnly": None}, rfc2109=False,
                ))
                return 200, {"Set-Cookie": "[redacted]"}, b'{"csrf_token":"testonly-csrf","must_change_password":false}'
            self.assertNotIn("Cookie", kwargs.get("headers", {}))
            self.assertEqual(kwargs["headers"]["x-csrf-token"], "testonly-csrf")
            return 200, {}, b'{"document_id":"doc.test"}'

        with mock.patch.object(mod, "_http", side_effect=fake_http):
            result = mod._deep_upload_probe("http://127.0.0.1:8000", "testonly-password")
        self.assertTrue(result["ok"])
        self.assertFalse(result["must_change_password"])
        self.assertEqual(len(seen), 2)

    def test_health_live_ok(self) -> None:
        with mock.patch.object(mod, "_http", return_value=(200, {"Content-Type": "application/json"},
                                                           b'{"status": "live"}')):
            result = mod._health_live_check("http://127.0.0.1:8000")
        self.assertTrue(result["ok"])
        self.assertEqual(result["http_status"], 200)

    def test_health_ready_unavailable_detected(self) -> None:
        with mock.patch.object(mod, "_http", return_value=(503, {}, b'{"status": "unavailable"}')):
            result = mod._health_ready_check("http://127.0.0.1:8000")
        self.assertFalse(result["ok"])
        self.assertEqual(result["body_status"], "unavailable")

    def test_visitor_home_requires_no_cookie_and_has_badge(self) -> None:
        html = '<html><body><div id="mode-badge"></div></body></html>'
        with mock.patch.object(mod, "_http", return_value=(200, {"Content-Type": "text/html"}, html.encode())):
            result = mod._visitor_home_check("http://127.0.0.1:8000")
        self.assertTrue(result["ok"])
        self.assertTrue(result["has_mode_badge"])
        self.assertTrue(result["no_cookie_sent"])

    def test_static_check_flags_404(self) -> None:
        # 变异锚点：B 未补白名单 → 对应路径 404，static 判定必须失败。
        def fake_http(base, path, **kwargs):
            if path == "/live-transport.js":
                return (404, {}, b"")
            return (200, {"Content-Type": "text/javascript"}, b"console.log(1)")
        with mock.patch.object(mod, "_http", side_effect=fake_http):
            results = mod._static_check("http://127.0.0.1:8000")
        self.assertFalse(results["/live-transport.js"]["ok"])
        self.assertEqual(results["/live-transport.js"]["http_status"], 404)

    def test_multipart_auth_boundary_interpretation(self) -> None:
        # 503 → B 的 catch-all 鉴权 P1 未修；401/403 → 正确边界。
        with mock.patch.object(mod, "_http", return_value=(503, {}, b'{"error":{"code":"service_unavailable"}}')):
            result = mod._multipart_auth_boundary_probe("http://127.0.0.1:8000")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "service_unavailable")
        self.assertIn("catch-all", result["interpretation"])

        with mock.patch.object(mod, "_http", return_value=(401, {}, b'{"error":{"code":"authentication_required"}}')):
            result2 = mod._multipart_auth_boundary_probe("http://127.0.0.1:8000")
        self.assertTrue(result2["ok"])
        self.assertIn("鉴权在解析前", result2["interpretation"])

    def test_deep_upload_sends_request_id_header_matching_metadata(self) -> None:
        # 深验收必须：会话走 CookieJar；上传带 X-Request-ID 与 metadata.request_id 一致
        # （B 的 _require_upload_meta 否则 400 invalid_request）。
        captured: dict = {}

        def fake_http(base, path, **kwargs):
            opener = kwargs.get("opener")
            self.assertIsNotNone(opener)
            if path == "/api/v3/auth/admin/login":
                processor = next(h for h in opener.handlers if isinstance(h, urllib.request.HTTPCookieProcessor))
                processor.cookiejar.set_cookie(http.cookiejar.Cookie(
                    version=0, name="ict_admin_session", value="testonly-cookie", port=None,
                    port_specified=False, domain="127.0.0.1", domain_specified=False,
                    domain_initial_dot=False, path="/", path_specified=True, secure=False,
                    expires=None, discard=True, comment=None, comment_url=None,
                    rest={"HttpOnly": None}, rfc2109=False,
                ))
                return 200, {"Set-Cookie": "[redacted]"}, b'{"csrf_token":"testonly-csrf","must_change_password":false}'
            captured["upload_headers"] = kwargs.get("headers", {})
            captured["upload_body"] = kwargs.get("body", b"")
            self.assertNotIn("Cookie", kwargs.get("headers", {}))
            return 200, {}, b'{"document_id":"doc.abc","state":"validated"}'

        with mock.patch.object(mod, "_http", side_effect=fake_http):
            result = mod._deep_upload_probe("http://127.0.0.1:8000", "pw")
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["document_id_returned"])
        self.assertEqual(captured["upload_headers"]["X-Request-ID"], "req.deep.upload")
        # metadata 里 request_id 必须与 X-Request-ID 头一致（B 的 _require_upload_meta）。
        self.assertIn(b'"request_id": "req.deep.upload"', captured["upload_body"])


class TestOrchestration(unittest.TestCase):
    def test_pending_when_b_server_absent(self) -> None:
        with mock.patch.object(mod, "offline_static_check",
                               return_value={"b_server_present": False, "b_server": "x",
                                             "static_files_present": 0, "static_files_missing": [],
                                             "frontend_files_complete": True}):
            report = mod.validate_all(SimpleNamespace(live=None, admin_password=None))
        self.assertEqual(report["status"], "pending")
        self.assertIn("pending_reason", report)

    def test_pending_when_offline_only(self) -> None:
        with mock.patch.object(mod, "offline_static_check",
                               return_value={"b_server_present": True, "b_server": "x",
                                             "static_files_present": 11, "static_files_missing": [],
                                             "frontend_files_complete": True}):
            report = mod.validate_all(SimpleNamespace(live=None, admin_password=None))
        self.assertEqual(report["status"], "pending")

    def test_pass_when_all_live_checks_ok(self) -> None:
        offline = {"b_server_present": True, "b_server": "x", "static_files_present": 11,
                   "static_files_missing": [], "frontend_files_complete": True}
        good_static = {url: {"ok": True, "http_status": 200, "content_type": "text/javascript", "bytes": 10}
                       for url, _l, _c in mod.STATIC_MANIFEST}
        with mock.patch.object(mod, "offline_static_check", return_value=offline), \
             mock.patch.object(mod, "_health_live_check", return_value={"ok": True}), \
             mock.patch.object(mod, "_health_ready_check", return_value={"ok": True}), \
             mock.patch.object(mod, "_static_check", return_value=good_static), \
             mock.patch.object(mod, "_visitor_home_check", return_value={"ok": True}), \
             mock.patch.object(mod, "_multipart_auth_boundary_probe", return_value={"ok": True}):
            report = mod.validate_all(SimpleNamespace(live="http://127.0.0.1:8000", admin_password=None))
        self.assertEqual(report["status"], "pass")

    def test_fail_when_static_broken(self) -> None:
        offline = {"b_server_present": True, "b_server": "x", "static_files_present": 11,
                   "static_files_missing": [], "frontend_files_complete": True}
        bad_static = {url: {"ok": url != "/app.js", "http_status": 200, "content_type": "x", "bytes": 1}
                      for url, _l, _c in mod.STATIC_MANIFEST}
        with mock.patch.object(mod, "offline_static_check", return_value=offline), \
             mock.patch.object(mod, "_health_live_check", return_value={"ok": True}), \
             mock.patch.object(mod, "_health_ready_check", return_value={"ok": True}), \
             mock.patch.object(mod, "_static_check", return_value=bad_static), \
             mock.patch.object(mod, "_visitor_home_check", return_value={"ok": True}), \
             mock.patch.object(mod, "_multipart_auth_boundary_probe", return_value={"ok": True}):
            report = mod.validate_all(SimpleNamespace(live="http://127.0.0.1:8000", admin_password=None))
        self.assertEqual(report["status"], "fail")


if __name__ == "__main__":
    unittest.main(verbosity=2)
