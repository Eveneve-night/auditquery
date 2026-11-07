"""D5-03 文档上传与知识库生命周期测试（离线、可重复）。

覆盖（对齐契约 validation_order + 8 态 + 失败关闭）：
1. 状态机：quarantined → validated → published → disabled → deleted / failed；
2. 校验链：扩展名 / Content-Type / magic bytes / 字节数 / hash / 页数 / 双解析；
3. 失败关闭：伪 PDF、字节声明不一致、hash 不一致、超大文件；
4. 幂等去重：相同 hash 重复上传返回已有对象；
5. 状态冲突：publish 非 validated、disable 非 published、delete published；
6. 审计：document_state_changed / index_published 与状态变更同事务落库；
7. 端点鉴权：无会话 401、写操作缺 CSRF 403、资源不存在 404；
8. 响应严格符合 api-v3.schema.json 的 document_status_response。

本测试不读取 expected / gold / label，不发起网络或模型调用。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import tempfile
import time
import unittest
from unittest.mock import patch

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from src.backend.product.auth import AuthError, AuthService, SqliteAuthStore
from src.backend.product.document_store import (
    MAX_FILE_BYTES,
    DocumentApplication,
    DocumentAuditContext,
    DocumentService,
    DocumentStoreError,
    SqliteDocumentStore,
    _bounded_dual_parse,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PDF_DIR = PROJECT_ROOT / "data" / "raw" / "pdf"
SCHEMA_PATH = PROJECT_ROOT / "configs" / "schemas" / "d5" / "api-v3.schema.json"
V1_SCHEMA = PROJECT_ROOT / "configs" / "schemas" / "d4-07" / "shared-api-definitions.v1.schema.json"
V2_SCHEMA = PROJECT_ROOT / "configs" / "schemas" / "d4-07" / "shared-api-definitions.v2.schema.json"


def _document_validator() -> Draft202012Validator:
    d5 = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    v1 = json.loads(V1_SCHEMA.read_text(encoding="utf-8"))
    v2 = json.loads(V2_SCHEMA.read_text(encoding="utf-8"))
    registry = Registry()
    for schema in (d5, v1, v2):
        registry = registry.with_resource(schema["$id"], Resource.from_contents(schema))
    for name, schema in (
        ("../d4-07/shared-api-definitions.v1.schema.json", v1),
        ("../d4-07/shared-api-definitions.v2.schema.json", v2),
    ):
        registry = registry.with_resource(name, Resource.from_contents(schema))
    schema = {"$id": d5["$id"], "$defs": d5["$defs"], **d5["$defs"]["document_status_response"]}
    return Draft202012Validator(schema, registry=registry)


def _read_pdf(name: str = "sales_playbook.pdf") -> bytes:
    return (PDF_DIR / name).read_bytes()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class D5DocumentLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.auth_path = root / "auth.sqlite3"
        self.data_dir = root / "instance"
        self.auth_store = SqliteAuthStore(self.auth_path)
        self.auth = AuthService(self.auth_store, audit_key=b"A" * 32)
        self.store = SqliteDocumentStore(self.auth, self.data_dir)
        self.service = DocumentService(self.store)
        self.app = DocumentApplication(self.auth, self.service)

    def tearDown(self) -> None:
        self.temp.cleanup()

    # ---- 辅助 ----

    def _bootstrap(self) -> tuple[str, str]:
        """创建管理员会话，返回 (session_token, csrf_token)。"""
        grant = self.auth.create_bootstrap_grant()
        credentials = self.auth.bootstrap_admin(
            grant.token,
            "admin",
            "correct horse battery staple",
            request_id="req-bootstrap",
        )
        return credentials.token, credentials.csrf_token

    def _upload_body(self, request_id: str, filename: str, declared: int, sha: str) -> bytes:
        return json.dumps(
            {
                "api_version": "3.0",
                "contract_version": "1.0.0",
                "request_id": request_id,
                "filename": filename,
                "content_type": "application/pdf",
                "declared_bytes": declared,
                "content_sha256": sha,
            }
        ).encode()

    def _upload(
        self,
        token: str,
        csrf: str,
        *,
        content: bytes,
        filename: str = "report.pdf",
        request_id: str = "req-upload",
        declared: int | None = None,
        sha: str | None = None,
    ):
        declared = len(content) if declared is None else declared
        sha = _sha256(content) if sha is None else sha
        return self.app.handle(
            "POST",
            "/api/v3/admin/documents",
            request_id=request_id,
            body=self._upload_body(request_id, filename, declared, sha),
            cookies={"ict_admin_session": token},
            headers={"x-csrf-token": csrf},
            file_bytes=content,
        )

    @staticmethod
    def _json_body(response) -> dict:
        return json.loads(response.body.decode("utf-8"))

    def _audit_events(self) -> list[dict]:
        return self.auth_store.audit_events()

    # ---- 状态机 + 成功路径 ----

    def test_upload_then_publish_then_disable_then_delete(self) -> None:
        token, csrf = self._bootstrap()
        content = _read_pdf("sales_playbook.pdf")

        uploaded = self._json_body(self._upload(token, csrf, content=content))
        self.assertEqual("validated", uploaded["state"])
        self.assertEqual(_sha256(content), uploaded["sha256"])
        self.assertIsNotNone(uploaded["page_count"])
        document_id = uploaded["document_id"]
        index_version = uploaded["index_version"]

        published = self._json_body(
            self.app.handle(
                "POST",
                f"/api/v3/admin/documents/{document_id}/publish",
                request_id="req-publish",
                cookies={"ict_admin_session": token},
                headers={"x-csrf-token": csrf},
            )
        )
        self.assertEqual("published", published["state"])
        self.assertEqual(index_version, self.store.get_active_index())

        disabled = self._json_body(
            self.app.handle(
                "POST",
                f"/api/v3/admin/documents/{document_id}/disable",
                request_id="req-disable",
                cookies={"ict_admin_session": token},
                headers={"x-csrf-token": csrf},
            )
        )
        self.assertEqual("disabled", disabled["state"])

        deleted = self._json_body(
            self.app.handle(
                "DELETE",
                f"/api/v3/admin/documents/{document_id}",
                request_id="req-delete",
                cookies={"ict_admin_session": token},
                headers={"x-csrf-token": csrf},
            )
        )
        self.assertEqual("deleted", deleted["status"])

        read_after_delete = self.app.handle(
            "GET",
            f"/api/v3/admin/documents/{document_id}",
            request_id="req-read-deleted",
            cookies={"ict_admin_session": token},
        )
        self.assertEqual(404, read_after_delete.status)

    def test_read_document_status(self) -> None:
        token, csrf = self._bootstrap()
        content = _read_pdf("sales_playbook.pdf")
        uploaded = self._json_body(self._upload(token, csrf, content=content))
        document_id = uploaded["document_id"]
        read = self._json_body(
            self.app.handle(
                "GET",
                f"/api/v3/admin/documents/{document_id}",
                request_id="req-read",
                cookies={"ict_admin_session": token},
            )
        )
        self.assertEqual(document_id, read["document_id"])
        self.assertEqual("validated", read["state"])

    def test_download_returns_pdf_bytes(self) -> None:
        token, csrf = self._bootstrap()
        content = _read_pdf("sales_playbook.pdf")
        uploaded = self._json_body(self._upload(token, csrf, content=content))
        response = self.app.handle(
            "GET",
            f"/api/v3/admin/documents/{uploaded['document_id']}/download",
            request_id="req-download",
            cookies={"ict_admin_session": token},
        )
        self.assertEqual(200, response.status)
        self.assertEqual(content, response.body)
        self.assertEqual("application/pdf", response.headers["Content-Type"])

    # ---- 幂等去重 ----

    def test_duplicate_hash_returns_existing_document(self) -> None:
        token, csrf = self._bootstrap()
        content = _read_pdf("sales_playbook.pdf")
        first = self._json_body(self._upload(token, csrf, content=content, request_id="req-1"))
        second = self._json_body(self._upload(token, csrf, content=content, request_id="req-2"))
        self.assertEqual(first["document_id"], second["document_id"])

    def test_document_status_response_matches_schema(self) -> None:
        token, csrf = self._bootstrap()
        content = _read_pdf("sales_playbook.pdf")
        validator = _document_validator()
        uploaded = self._json_body(self._upload(token, csrf, content=content))
        self.assertEqual([], [e.message for e in validator.iter_errors(uploaded)])
        document_id = uploaded["document_id"]
        published = self._json_body(
            self.app.handle(
                "POST",
                f"/api/v3/admin/documents/{document_id}/publish",
                request_id="req-publish",
                cookies={"ict_admin_session": token},
                headers={"x-csrf-token": csrf},
            )
        )
        self.assertEqual([], [e.message for e in validator.iter_errors(published)])

    def test_document_id_remains_schema_valid_when_random_suffix_starts_with_underscore(self) -> None:
        token, csrf = self._bootstrap()
        with patch("src.backend.product.document_store.secrets.token_urlsafe", return_value="_edge"):
            uploaded = self._json_body(self._upload(token, csrf, content=_read_pdf()))
        self.assertTrue(uploaded["document_id"].startswith("doc."))
        self.assertEqual([], [e.message for e in _document_validator().iter_errors(uploaded)])

    # ---- 校验链 ----

    def test_non_pdf_extension_rejected(self) -> None:
        token, csrf = self._bootstrap()
        content = _read_pdf("sales_playbook.pdf")
        response = self._upload(token, csrf, content=content, filename="report.txt")
        self.assertEqual(415, response.status)
        self.assertEqual("unsupported_media_type", self._json_body(response)["error"]["code"])

    def test_wrong_content_type_rejected(self) -> None:
        token, csrf = self._bootstrap()
        content = _read_pdf("sales_playbook.pdf")
        response = self.app.handle(
            "POST",
            "/api/v3/admin/documents",
            request_id="req-ct",
            body=json.dumps(
                {
                    "api_version": "3.0",
                    "contract_version": "1.0.0",
                    "request_id": "req-ct",
                    "filename": "report.pdf",
                    "content_type": "text/plain",
                    "declared_bytes": len(content),
                    "content_sha256": _sha256(content),
                }
            ).encode(),
            cookies={"ict_admin_session": token},
            headers={"x-csrf-token": csrf},
            file_bytes=content,
        )
        self.assertEqual(415, response.status)
        self.assertEqual("unsupported_media_type", self._json_body(response)["error"]["code"])

    def test_non_pdf_magic_rejected(self) -> None:
        token, csrf = self._bootstrap()
        fake = b"this is definitely not a pdf file"
        response = self._upload(token, csrf, content=fake)
        self.assertEqual(415, response.status)
        self.assertEqual("unsupported_media_type", self._json_body(response)["error"]["code"])

    def test_fake_pdf_magic_but_unparseable_rejected(self) -> None:
        # %PDF- 开头但不可解析：magic 通过，双解析失败关闭 → document_invalid。
        token, csrf = self._bootstrap()
        fake = b"%PDF-1.4\n" + b"garbage that is not a real pdf" * 10
        response = self._upload(token, csrf, content=fake)
        self.assertEqual(422, response.status)
        self.assertEqual("document_invalid", self._json_body(response)["error"]["code"])

    def test_declared_bytes_mismatch_rejected(self) -> None:
        token, csrf = self._bootstrap()
        content = _read_pdf("sales_playbook.pdf")
        response = self._upload(token, csrf, content=content, declared=len(content) + 10)
        self.assertEqual(422, response.status)
        self.assertEqual("document_invalid", self._json_body(response)["error"]["code"])

    def test_content_sha256_mismatch_rejected(self) -> None:
        token, csrf = self._bootstrap()
        content = _read_pdf("sales_playbook.pdf")
        response = self._upload(token, csrf, content=content, sha="0" * 64)
        self.assertEqual(422, response.status)
        self.assertEqual("document_invalid", self._json_body(response)["error"]["code"])

    def test_oversized_file_rejected(self) -> None:
        token, csrf = self._bootstrap()
        with patch("src.backend.product.document_store.MAX_FILE_BYTES", 64):
            content = b"%PDF-1.4\n" + b"x" * 100
            response = self._upload(token, csrf, content=content)
        self.assertEqual(413, response.status)
        self.assertEqual("upload_too_large", self._json_body(response)["error"]["code"])

    # ---- 路径穿越与文件名注入（隔离语义）----
    #
    # 后端隔离策略（区别于前端 mock 的「拒绝路径片段」）：客户端 filename 只做
    # 扩展名校验后即丢弃，字节落盘路径仅由服务器生成的 document_id
    # （"doc." + secrets.token_urlsafe(16)，字符集 [A-Za-z0-9_-]）决定，不含
    # 路径分隔符 / 保留名 / ".."。因此任何 filename 注入都无法把字节写到
    # quarantine 目录之外。下列用例固化该不变量，证明「穿越被隔离」而非
    # 「穿越被拒收」。

    def _assert_no_escape(self) -> None:
        """断言临时目录内除合法产物外无任何逃逸文件。"""
        for path in Path(self.temp.name).rglob("*"):
            if not path.is_file():
                continue
            if path == self.auth_path:
                continue
            if path.is_relative_to(self.store.quarantine_dir):
                assert path.name.startswith("doc.") and path.name.endswith(".pdf"), path
                continue
            if path.is_relative_to(self.store.index_dir):
                assert path.name.endswith(".json"), path
                continue
            raise AssertionError(f"检测到逃逸文件：{path}")

    def test_relative_path_filename_is_neutralized(self) -> None:
        token, csrf = self._bootstrap()
        content = _read_pdf("sales_playbook.pdf")
        for filename in ("../etc/passwd.pdf", "../../secret.pdf", r"..\\..\\win.pdf"):
            response = self._upload(
                token, csrf, content=content, filename=filename, request_id=f"req-{filename}"
            )
            self.assertEqual(200, response.status, filename)
            body = self._json_body(response)
            self.assertEqual("validated", body["state"], filename)
            self.assertRegex(body["document_id"], r"^doc\.[A-Za-z0-9_-]{16,}$", filename)
        self._assert_no_escape()

    def test_absolute_path_filename_is_neutralized(self) -> None:
        token, csrf = self._bootstrap()
        content = _read_pdf("sales_playbook.pdf")
        for filename in ("/etc/passwd.pdf", "C:\\Windows\\System32\\evil.pdf", "\\\\server\\share\\x.pdf"):
            response = self._upload(token, csrf, content=content, filename=filename)
            self.assertEqual(200, response.status, filename)
            self.assertRegex(
                self._json_body(response)["document_id"], r"^doc\.[A-Za-z0-9_-]{16,}$", filename
            )
        self._assert_no_escape()

    def test_encoded_path_filename_is_neutralized(self) -> None:
        token, csrf = self._bootstrap()
        content = _read_pdf("sales_playbook.pdf")
        for filename in ("..%2f..%2fetc%2fpasswd.pdf", "..%5c..%5cwin.pdf", "%2e%2e%2fetc.pdf"):
            response = self._upload(token, csrf, content=content, filename=filename)
            self.assertEqual(200, response.status, filename)
            self.assertRegex(
                self._json_body(response)["document_id"], r"^doc\.[A-Za-z0-9_-]{16,}$", filename
            )
        self._assert_no_escape()

    def test_windows_reserved_name_filename_is_neutralized(self) -> None:
        token, csrf = self._bootstrap()
        content = _read_pdf("sales_playbook.pdf")
        for filename in ("con.pdf", "nul.pdf", "aux.pdf", "com1.pdf", "lpt1.pdf"):
            response = self._upload(token, csrf, content=content, filename=filename)
            self.assertEqual(200, response.status, filename)
            self.assertRegex(
                self._json_body(response)["document_id"], r"^doc\.[A-Za-z0-9_-]{16,}$", filename
            )
        self._assert_no_escape()

    def test_filename_never_reflected_in_response_or_audit(self) -> None:
        token, csrf = self._bootstrap()
        marker = "../unique-injection-marker.pdf"
        content = _read_pdf("sales_playbook.pdf")
        response = self._upload(token, csrf, content=content, filename=marker)
        body = self._json_body(response)
        # 响应不暴露客户端 filename。
        self.assertNotIn("filename", body)
        self.assertNotIn(marker, response.body.decode("utf-8"))
        # 审计不落文件名。
        for event in self._audit_events():
            self.assertNotIn(marker, json.dumps(event, ensure_ascii=False))

    def test_same_name_different_hash_no_overwrite(self) -> None:
        token, csrf = self._bootstrap()
        ids = set()
        for name in ("sales_playbook.pdf", "expense_policy.pdf", "finance_policy.pdf"):
            response = self._upload(token, csrf, content=_read_pdf(name), filename="report.pdf")
            self.assertEqual(200, response.status)
            body = self._json_body(response)
            self.assertEqual("validated", body["state"])
            ids.add(body["document_id"])
            # 落盘名恒为 document_id.pdf，而非客户端 filename。
            path = self.store._quarantine_path(body["document_id"])
            self.assertEqual(self.store.quarantine_dir, path.parent)
            self.assertTrue(path.is_file())
        self.assertEqual(3, len(ids))
        self._assert_no_escape()

    def test_write_quarantine_refuses_existing_path(self) -> None:
        # 预置同名文件（模拟符号链接/预写攻击目标），写入必须拒绝覆盖。
        did = "doc.testexisting"
        target = self.store._quarantine_path(did)
        target.write_bytes(b"existing")
        with self.assertRaises(DocumentStoreError) as ctx:
            self.store.write_quarantine(did, b"new-bytes")
        self.assertEqual("state_conflict", ctx.exception.code)

    def test_quarantine_path_is_direct_child_for_generated_ids(self) -> None:
        # document_id 的字符集不含分隔符/".."，_quarantine_path 恒为直接子项。
        for did in ("doc." + ("A" * 16), "doc." + ("-" * 16), "doc." + ("_" * 16)):
            path = self.store._quarantine_path(did)
            self.assertEqual(self.store.quarantine_dir, path.parent, did)
            self.assertNotIn("..", path.parts)

    # ---- 状态冲突 ----

    def test_publish_non_validated_conflicts(self) -> None:
        token, csrf = self._bootstrap()
        content = _read_pdf("sales_playbook.pdf")
        uploaded = self._json_body(self._upload(token, csrf, content=content))
        document_id = uploaded["document_id"]
        # 先发布 → published
        self.app.handle(
            "POST",
            f"/api/v3/admin/documents/{document_id}/publish",
            request_id="req-p1",
            cookies={"ict_admin_session": token},
            headers={"x-csrf-token": csrf},
        )
        # 再发布 published → state_conflict
        response = self.app.handle(
            "POST",
            f"/api/v3/admin/documents/{document_id}/publish",
            request_id="req-p2",
            cookies={"ict_admin_session": token},
            headers={"x-csrf-token": csrf},
        )
        self.assertEqual(409, response.status)
        self.assertEqual("state_conflict", self._json_body(response)["error"]["code"])

    def test_delete_published_conflicts(self) -> None:
        token, csrf = self._bootstrap()
        content = _read_pdf("sales_playbook.pdf")
        uploaded = self._json_body(self._upload(token, csrf, content=content))
        document_id = uploaded["document_id"]
        self.app.handle(
            "POST",
            f"/api/v3/admin/documents/{document_id}/publish",
            request_id="req-publish",
            cookies={"ict_admin_session": token},
            headers={"x-csrf-token": csrf},
        )
        response = self.app.handle(
            "DELETE",
            f"/api/v3/admin/documents/{document_id}",
            request_id="req-delete",
            cookies={"ict_admin_session": token},
            headers={"x-csrf-token": csrf},
        )
        self.assertEqual(409, response.status)
        self.assertEqual("state_conflict", self._json_body(response)["error"]["code"])

    # ---- 审计 ----

    def test_upload_and_publish_write_audit(self) -> None:
        token, csrf = self._bootstrap()
        content = _read_pdf("sales_playbook.pdf")
        uploaded = self._json_body(self._upload(token, csrf, content=content))
        document_id = uploaded["document_id"]
        self.app.handle(
            "POST",
            f"/api/v3/admin/documents/{document_id}/publish",
            request_id="req-publish",
            cookies={"ict_admin_session": token},
            headers={"x-csrf-token": csrf},
        )
        event_types = {e["event_type"] for e in self._audit_events()}
        self.assertIn("document_state_changed", event_types)
        self.assertIn("index_published", event_types)
        # 审计字段不包含 PDF 正文 / 文件名 / 原始字节
        for event in self._audit_events():
            serialized = json.dumps(event, ensure_ascii=False)
            self.assertNotIn("截止时间", serialized)
            self.assertNotIn("report.pdf", serialized)

    # ---- 端点鉴权 ----

    def test_upload_requires_session_and_csrf(self) -> None:
        content = _read_pdf("sales_playbook.pdf")
        # 无会话 → 401
        denied = self.app.handle(
            "POST",
            "/api/v3/admin/documents",
            request_id="req-anon",
            body=self._upload_body("req-anon", "report.pdf", len(content), _sha256(content)),
            file_bytes=content,
        )
        self.assertEqual(401, denied.status)

        token, csrf = self._bootstrap()
        # 有会话但缺 CSRF → 403
        no_csrf = self.app.handle(
            "POST",
            "/api/v3/admin/documents",
            request_id="req-nocsrf",
            body=self._upload_body("req-nocsrf", "report.pdf", len(content), _sha256(content)),
            cookies={"ict_admin_session": token},
            file_bytes=content,
        )
        self.assertEqual(403, no_csrf.status)

    def test_read_missing_document_is_404(self) -> None:
        token, _ = self._bootstrap()
        response = self.app.handle(
            "GET",
            "/api/v3/admin/documents/no-such-doc",
            request_id="req-missing",
            cookies={"ict_admin_session": token},
        )
        self.assertEqual(404, response.status)
        self.assertEqual("resource_not_found", self._json_body(response)["error"]["code"])

    # ---- B 复审修复回归 ----

    def test_publish_is_atomic_pointer_and_state_same_transaction(self) -> None:
        """切指针 + validated→published + 审计同一事务；后一步失败整体回滚、旧指针不变。"""
        token, csrf = self._bootstrap()
        content = _read_pdf("sales_playbook.pdf")
        uploaded = self._json_body(self._upload(token, csrf, content=content))
        document_id = uploaded["document_id"]
        self.assertIsNone(self.store.get_active_index())

        original = self.auth._append_audit
        calls = {"n": 0}

        def fail_on_second(connection, *, event_type, **kwargs):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise AuthError("service_unavailable")
            return original(connection, event_type=event_type, **kwargs)

        with patch.object(self.auth, "_append_audit", side_effect=fail_on_second):
            with self.assertRaises(AuthError):
                self.service.publish(
                    document_id,
                    audit=DocumentAuditContext("req-p", "sess", "test"),
                )
        # 事务回滚：活动指针未切、文档仍 validated
        self.assertIsNone(self.store.get_active_index())
        self.assertEqual("validated", self.store.get_record(document_id).state)

    def test_disable_published_clears_active_index(self) -> None:
        """停用当前活动索引指向的 published 文档后，活动指针必须失效。"""
        token, csrf = self._bootstrap()
        content = _read_pdf("sales_playbook.pdf")
        uploaded = self._json_body(self._upload(token, csrf, content=content))
        document_id = uploaded["document_id"]
        index_version = uploaded["index_version"]
        self.app.handle(
            "POST",
            f"/api/v3/admin/documents/{document_id}/publish",
            request_id="req-publish",
            cookies={"ict_admin_session": token},
            headers={"x-csrf-token": csrf},
        )
        self.assertEqual(index_version, self.store.get_active_index())
        self.app.handle(
            "POST",
            f"/api/v3/admin/documents/{document_id}/disable",
            request_id="req-disable",
            cookies={"ict_admin_session": token},
            headers={"x-csrf-token": csrf},
        )
        self.assertIsNone(self.store.get_active_index())

    def test_delete_physical_cleanup_failure_returns_accepted_and_tombstone(self) -> None:
        """字节清理失败不得伪装成完整删除：返回 accepted + 持久化 tombstone 可重试。"""
        token, csrf = self._bootstrap()
        content = _read_pdf("sales_playbook.pdf")
        uploaded = self._json_body(self._upload(token, csrf, content=content))
        document_id = uploaded["document_id"]
        with patch.object(
            self.store,
            "delete_quarantine",
            side_effect=DocumentStoreError("service_unavailable"),
        ):
            response = self.app.handle(
                "DELETE",
                f"/api/v3/admin/documents/{document_id}",
                request_id="req-delete",
                cookies={"ict_admin_session": token},
                headers={"x-csrf-token": csrf},
            )
        self.assertEqual(200, response.status)
        self.assertEqual("accepted", self._json_body(response)["status"])
        # 逻辑删除已生效（不可访问）+ 物理清理任务已持久化
        self.assertIn(document_id, self.store.list_pending_cleanup())
        read = self.app.handle(
            "GET",
            f"/api/v3/admin/documents/{document_id}",
            request_id="req-read-deleted",
            cookies={"ict_admin_session": token},
        )
        self.assertEqual(404, read.status)

    def test_delete_physical_cleanup_success_returns_deleted(self) -> None:
        """字节清理成功才返回完整 deleted，且 tombstone 被移除。"""
        token, csrf = self._bootstrap()
        content = _read_pdf("sales_playbook.pdf")
        uploaded = self._json_body(self._upload(token, csrf, content=content))
        document_id = uploaded["document_id"]
        response = self.app.handle(
            "DELETE",
            f"/api/v3/admin/documents/{document_id}",
            request_id="req-delete",
            cookies={"ict_admin_session": token},
            headers={"x-csrf-token": csrf},
        )
        self.assertEqual(200, response.status)
        self.assertEqual("deleted", self._json_body(response)["status"])
        self.assertNotIn(document_id, self.store.list_pending_cleanup())
        self.assertFalse(self.store._quarantine_path(document_id).exists())

    def test_retry_pending_cleanup_reclaims_bytes_after_restart(self) -> None:
        """重启后可从 tombstone 枚举并重试，敏感字节最终删除。"""
        token, csrf = self._bootstrap()
        content = _read_pdf("sales_playbook.pdf")
        uploaded = self._json_body(self._upload(token, csrf, content=content))
        document_id = uploaded["document_id"]
        with patch.object(
            self.store,
            "delete_quarantine",
            side_effect=DocumentStoreError("service_unavailable"),
        ):
            self.app.handle(
                "DELETE",
                f"/api/v3/admin/documents/{document_id}",
                request_id="req-delete",
                cookies={"ict_admin_session": token},
                headers={"x-csrf-token": csrf},
            )
        # 模拟重启：重新打开 store（共享 auth SQLite，tombstone 表持久化）
        reopened = SqliteDocumentStore(self.auth, self.data_dir)
        self.assertIn(document_id, reopened.list_pending_cleanup())
        # 故障已恢复：重试物理清理
        self.assertEqual([document_id], reopened.retry_pending_cleanup())
        self.assertEqual([], reopened.list_pending_cleanup())
        self.assertFalse(reopened._quarantine_path(document_id).exists())

    def test_repeated_delete_is_not_found(self) -> None:
        """重复删除（对象已 deleted）→ resource_not_found。"""
        token, csrf = self._bootstrap()
        content = _read_pdf("sales_playbook.pdf")
        uploaded = self._json_body(self._upload(token, csrf, content=content))
        document_id = uploaded["document_id"]
        first = self.app.handle(
            "DELETE",
            f"/api/v3/admin/documents/{document_id}",
            request_id="req-delete-1",
            cookies={"ict_admin_session": token},
            headers={"x-csrf-token": csrf},
        )
        self.assertEqual("deleted", self._json_body(first)["status"])
        second = self.app.handle(
            "DELETE",
            f"/api/v3/admin/documents/{document_id}",
            request_id="req-delete-2",
            cookies={"ict_admin_session": token},
            headers={"x-csrf-token": csrf},
        )
        self.assertEqual(404, second.status)
        self.assertEqual("resource_not_found", self._json_body(second)["error"]["code"])

    def test_mark_deleted_is_atomic_on_audit_failure(self) -> None:
        """mark_deleted 内审计写入失败 → 状态/tombstone/字节整体回滚不变。"""
        token, csrf = self._bootstrap()
        content = _read_pdf("sales_playbook.pdf")
        uploaded = self._json_body(self._upload(token, csrf, content=content))
        document_id = uploaded["document_id"]

        def fail_audit(connection, *, event_type, **kwargs):
            raise AuthError("service_unavailable")

        with patch.object(self.auth, "_append_audit", side_effect=fail_audit):
            response = self.app.handle(
                "DELETE",
                f"/api/v3/admin/documents/{document_id}",
                request_id="req-delete",
                cookies={"ict_admin_session": token},
                headers={"x-csrf-token": csrf},
            )
        self.assertEqual(503, response.status)
        # 整体回滚：权威状态仍 validated、无 tombstone、字节仍在
        self.assertEqual("validated", self.store.get_record(document_id).state)
        self.assertEqual([], self.store.list_pending_cleanup())
        self.assertTrue(self.store._quarantine_path(document_id).exists())

    def test_clear_tombstone_failure_retained_and_retryable(self) -> None:
        """字节已删但 clear_tombstone 失败 → tombstone 保留，重试幂等收口。"""
        token, csrf = self._bootstrap()
        content = _read_pdf("sales_playbook.pdf")
        uploaded = self._json_body(self._upload(token, csrf, content=content))
        document_id = uploaded["document_id"]
        with patch.object(
            self.store,
            "clear_tombstone",
            side_effect=DocumentStoreError("service_unavailable"),
        ):
            response = self.app.handle(
                "DELETE",
                f"/api/v3/admin/documents/{document_id}",
                request_id="req-delete",
                cookies={"ict_admin_session": token},
                headers={"x-csrf-token": csrf},
            )
        # clear_tombstone 失败 → 返回 accepted（非 deleted），tombstone 保留，字节已删
        self.assertEqual("accepted", self._json_body(response)["status"])
        self.assertIn(document_id, self.store.list_pending_cleanup())
        self.assertFalse(self.store._quarantine_path(document_id).exists())
        # 故障恢复后重试：幂等收口，字节已删不报错，tombstone 移除
        self.assertEqual([document_id], self.store.retry_pending_cleanup())
        self.assertEqual([], self.store.list_pending_cleanup())

    def test_concurrent_delete_only_one_succeeds(self) -> None:
        """并发删除：只有一个成功，不产生孤儿字节或重复清理任务。"""
        import threading

        token, csrf = self._bootstrap()
        content = _read_pdf("sales_playbook.pdf")
        uploaded = self._json_body(self._upload(token, csrf, content=content))
        document_id = uploaded["document_id"]

        statuses: list[int] = []
        lock = threading.Lock()

        def do_delete() -> None:
            response = self.app.handle(
                "DELETE",
                f"/api/v3/admin/documents/{document_id}",
                request_id="req-delete",
                cookies={"ict_admin_session": token},
                headers={"x-csrf-token": csrf},
            )
            with lock:
                statuses.append(response.status)

        threads = [threading.Thread(target=do_delete) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual([200, 404], sorted(statuses))
        # 无残留 tombstone、无残留字节
        self.assertEqual([], self.store.list_pending_cleanup())
        self.assertFalse(self.store._quarantine_path(document_id).exists())

    def test_audit_event_types_all_within_contract(self) -> None:
        """审计事件类型必须全部落在 A 冻结契约 audit.event_types 内，不得新增契约外类型。"""
        token, csrf = self._bootstrap()
        content = _read_pdf("sales_playbook.pdf")
        uploaded = self._json_body(self._upload(token, csrf, content=content))
        document_id = uploaded["document_id"]
        # 走一遍完整生命周期（含 delete 失败路径），产生尽可能多的审计事件
        with patch.object(
            self.store,
            "delete_quarantine",
            side_effect=DocumentStoreError("service_unavailable"),
        ):
            self.app.handle(
                "DELETE",
                f"/api/v3/admin/documents/{document_id}",
                request_id="req-delete",
                cookies={"ict_admin_session": token},
                headers={"x-csrf-token": csrf},
            )
        contract = json.loads(
            (PROJECT_ROOT / "configs" / "contracts" / "d5" / "product-layer.v1.json").read_text(
                encoding="utf-8"
            )
        )
        allowed = set(contract["audit"]["event_types"])
        used = {e["event_type"] for e in self._audit_events()}
        self.assertTrue(used, "未产生任何审计事件")
        unknown = sorted(t for t in used if t not in allowed)
        self.assertEqual([], unknown, "出现契约外审计事件类型：" + ", ".join(unknown))

    def test_bounded_dual_parse_returns_fast_on_timeout(self) -> None:
        """超时后主流程立即失败关闭，不等待未完成解析线程（进程级硬超时留 D5-06）。"""

        def slow_open(path):
            time.sleep(2.0)
            raise RuntimeError("should not be reached")

        with patch("pdfplumber.open", side_effect=slow_open):
            start = time.monotonic()
            with self.assertRaises(DocumentStoreError) as caught:
                _bounded_dual_parse(PDF_DIR / "sales_playbook.pdf", timeout=0.05)
            elapsed = time.monotonic() - start
        self.assertEqual("document_invalid", caught.exception.code)
        self.assertLess(elapsed, 1.0)  # 证明未等待 2 秒的解析线程


if __name__ == "__main__":
    unittest.main()
