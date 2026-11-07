"""D5-05-C C 侧前端真实接线（live transport + multipart）离线测试。

验证目标（均离线、可重复，不发网络/模型调用）：
1. createLiveTransport 用 mock fetch 构造真实 HTTP 请求：method/url/JSON body、
   `x-csrf-token` 头、credentials=same-origin（同源 Cookie 由浏览器自动携带）；
2. 文档上传走受控 multipart/FormData：metadata（document_upload_request JSON）+
   file（PDF 字节）两字段，不手动设 Content-Type，写操作仍带 CSRF 头；
3. 二进制下载（raw）返回 {status, blob, contentType}；非 2xx 仍归一化 {status, json}
   交给 api-client 抛 ApiError；空 body 不抛错；
4. api-client 的 upload 透传 file、download 透传 raw；
5. 源码红线：live-transport.js 不写 localStorage/sessionStorage/document.cookie。
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
import unittest
from jsonschema import Draft202012Validator

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONSOLE_ROOT = PROJECT_ROOT / "src" / "frontend" / "d5-console"
LIVE_TRANSPORT = CONSOLE_ROOT / "live-transport.js"
API_CLIENT = CONSOLE_ROOT / "api-client.js"
LIVE_PROBE = PROJECT_ROOT / "tests" / "fixtures" / "d5-05" / "probe_live_transport.mjs"


def _run_node(script: str, *, inline: bool = False) -> dict:
    args = ["node"]
    if inline:
        args += ["--input-type=module", "-e", script]
    else:
        args.append(str(script))
    result = subprocess.run(
        args, cwd=str(PROJECT_ROOT), capture_output=True, text=True, encoding="utf-8"
    )
    if result.returncode != 0:
        raise RuntimeError("node 探针执行失败：\n" + result.stderr)
    return json.loads(result.stdout.strip().splitlines()[-1])


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _strip_comments(source: str) -> str:
    """去掉块注释与行注释（保留 :// 形式的 URL），用于红线扫描。"""
    stripped = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"(?<!:)//[^\n]*", "", stripped)


class LiveTransportProbeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.data = _run_node(LIVE_PROBE)

    def test_json_request_uses_contract_shape(self) -> None:
        jr = self.data["jsonRequest"]
        self.assertTrue(jr["ok"])
        self.assertEqual("POST", jr["method"])
        self.assertEqual("/api/v3/query", jr["url"])
        self.assertEqual("application/json", jr["contentType"])
        self.assertEqual("same-origin", jr["credentials"])

    def test_write_carries_csrf_header(self) -> None:
        self.assertEqual("tok-123", self.data["jsonRequest"]["csrfHeader"])
        self.assertEqual("tok-admin", self.data["multipart"]["csrfHeader"])

    def test_request_id_header_matches_body_for_query_upload_and_key(self) -> None:
        self.assertEqual("req.test.query", self.data["jsonRequest"]["requestIdHeader"])
        self.assertEqual("req.test.upload", self.data["multipart"]["requestIdHeader"])
        self.assertEqual(self.data["keySave"]["bodyRequestId"], self.data["keySave"]["requestIdHeader"])

    def test_multipart_upload_has_metadata_and_file(self) -> None:
        mp = self.data["multipart"]
        self.assertTrue(mp["ok"])
        self.assertTrue(mp["isFormData"])
        names = {p["name"] for p in mp["parts"]}
        self.assertEqual({"metadata", "file"}, names)
        self.assertTrue(mp["metadataIsJson"])
        self.assertTrue(mp["contentTypeNotManuallySet"], "multipart 不应手动设 Content-Type")

    def test_multipart_content_types_align_with_d5_05_b_contract(self) -> None:
        # 对齐 B 的 parse_document_multipart：metadata 必须 application/json，
        # file 必须 application/pdf（否则 B 侧 400 拒绝）。
        mp = self.data["multipart"]
        self.assertEqual("application/json", mp["metadataType"])
        self.assertEqual("application/pdf", mp["fileType"])

    def test_download_raw_returns_blob_and_content_type(self) -> None:
        self.assertTrue(self.data["download"]["ok"])

    def test_error_response_is_normalized_for_client(self) -> None:
        er = self.data["errorNormalize"]
        self.assertTrue(er["ok"])
        self.assertEqual("csrf_invalid", er["code"])

    def test_empty_body_does_not_throw(self) -> None:
        self.assertTrue(self.data["emptyBody"]["ok"])

    def test_non_function_fetch_throws_explicitly(self) -> None:
        self.assertTrue(self.data["noFetchThrows"]["ok"])


class ApiClientWiringTest(unittest.TestCase):
    def test_admin_login_and_change_use_frozen_v2_schema(self) -> None:
        script = """
import { createClient } from "./src/frontend/d5-console/api-client.js";
const calls = [];
const client = createClient({ transport: async (method, path, opts) => {
  calls.push({ method, path, opts });
  return { status: 200, json: { authenticated: true, csrf_token: "x".repeat(32) } };
} });
await client.adminLogin({ username: "admin", password: "testonly-password" });
await client.adminChangePassword({ currentPassword: "testonly-password",
  newPassword: "testonly-new-password", csrf: "testonly-csrf" });
console.log(JSON.stringify(calls));
"""
        calls = _run_node(script, inline=True)
        schema = json.loads((PROJECT_ROOT / "configs" / "schemas" / "d5" /
                             "admin-bootstrap-api.v2.schema.json").read_text(encoding="utf-8"))
        self.assertEqual([item["path"] for item in calls],
                         ["/api/v3/auth/admin/login", "/api/v3/admin/password"])
        for item, kind in zip(calls, ("login_request", "password_change_request")):
            Draft202012Validator(schema).evolve(schema={"$ref": f"#/$defs/{kind}"}).validate(item["opts"]["body"])
        self.assertEqual(calls[1]["opts"]["csrf"], "testonly-csrf")

    def test_upload_passes_file_and_download_passes_raw(self) -> None:
        script = """
import { createClient } from "./src/frontend/d5-console/api-client.js";
const calls = [];
const transport = async (method, path, opts) => {
  calls.push({ method, path, opts });
  return { status: 200, json: { ok: true } };
};
const client = createClient({ transport });
await client.adminUploadDocument({
  filename: "a.pdf", declaredBytes: 1, contentSha256: "a".repeat(64), csrf: "t", file: { name: "a.pdf" },
});
await client.adminDownloadDocument("doc.1");
console.log(JSON.stringify({
  uploadHasFile: Boolean(calls[0].opts.file),
  uploadCsrf: calls[0].opts.csrf,
  uploadBodyFilename: calls[0].opts.body.filename,
  uploadBodyHasSha: calls[0].opts.body.content_sha256 === "a".repeat(64),
  downloadRaw: calls[1].opts.raw === true,
  downloadMethod: calls[1].method,
  downloadPath: calls[1].path,
}));
"""
        data = _run_node(script, inline=True)
        self.assertTrue(data["uploadHasFile"])
        self.assertEqual("t", data["uploadCsrf"])
        self.assertEqual("a.pdf", data["uploadBodyFilename"])
        self.assertTrue(data["uploadBodyHasSha"])
        self.assertTrue(data["downloadRaw"])
        self.assertEqual("GET", data["downloadMethod"])
        self.assertEqual("/api/v3/admin/documents/doc.1/download", data["downloadPath"])


class LiveTransportSourceSafetyTest(unittest.TestCase):
    def test_no_client_side_persistence(self) -> None:
        source = _strip_comments(_read(LIVE_TRANSPORT))
        for forbidden in ("localStorage", "sessionStorage", "document.cookie", "indexedDB"):
            self.assertNotIn(forbidden, source, f"live-transport 不应写 {forbidden}")

    def test_no_default_credentials_or_key_logging(self) -> None:
        source = _strip_comments(_read(LIVE_TRANSPORT))
        # 不内置口令/引导令牌，不打印 body 或 Key
        self.assertNotIn("console.log", source)
        self.assertNotIn("console.error", source)
        self.assertNotIn("api_key", source)


if __name__ == "__main__":
    unittest.main()
