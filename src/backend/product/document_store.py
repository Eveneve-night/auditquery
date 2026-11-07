"""D5-03 文档上传与知识库生命周期（后端运行时）。

对齐 ``configs/contracts/d5/product-layer.v1.json`` 冻结的
``pdf_lifecycle``（8 态 + validation_order 8 步 + limits）与
``store_interfaces.DocumentStore`` / ``MetadataStore`` 的文档相关操作。

关键失败关闭语义（契约 DocumentStore.failure_semantics）：
- 任一步校验 / 解析 / 索引 / 发布失败，保留旧活动索引，新对象标记 ``failed``；
- 发布只通过「活动索引指针」原子切换，候选索引与发布目录隔离；
- 服务器生成 document_id，客户端 filename 不作为路径，拒绝路径穿越与覆盖。

审计：``document_state_changed`` / ``index_published`` 与状态变更在同一
SQLite 事务内原子提交（契约「禁止成功状态无审计」）。审计白名单复用
auth 的冻结字段集合，绝不写入 PDF 正文、文件名或原始异常。

仅离线：网络调用 0，模型 API 调用 0，密钥读取 0。
"""

from __future__ import annotations

import concurrent.futures
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import secrets
import sqlite3
from pathlib import Path
from typing import Any, Mapping

from .auth import (
    API_VERSION,
    CONTRACT_VERSION,
    COOKIE_NAME,
    AuthError,
    AuthHttpResponse,
    AuthService,
)

# ---- 冻结限制（product-layer.v1.json pdf_lifecycle.limits）----
MAX_FILE_BYTES = 26214400  # 25 MiB
MAX_PAGES = 500
PARSE_TIMEOUT_SECONDS = 60
PDF_CONTENT_TYPE = "application/pdf"
PDF_EXTENSION = ".pdf"
PDF_MAGIC = b"%PDF-"

DOCUMENT_STATES = {
    "quarantined",
    "validating",
    "validated",
    "indexing",
    "published",
    "disabled",
    "failed",
    "deleted",
}

# 稳定错误码：document 端点声明的错误码 + service_unavailable（契约 error_catalog）。
_SAFE_ERRORS = {
    "invalid_request": (400, "请求格式无效。", False),
    "authentication_required": (401, "需要有效身份。", False),
    "forbidden": (403, "没有执行该操作的权限。", False),
    "csrf_invalid": (403, "请求安全校验失败。", False),
    "resource_not_found": (404, "资源不存在。", False),
    "state_conflict": (409, "资源状态已变化，请刷新后重试。", True),
    "upload_too_large": (413, "文件超过允许大小。", False),
    "unsupported_media_type": (415, "仅接受受支持的 PDF。", False),
    "document_invalid": (422, "文档未通过安全校验。", False),
    "rate_limited": (429, "请求过于频繁，请稍后重试。", True),
    "service_unavailable": (503, "服务暂时不可用。", True),
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class DocumentStoreError(RuntimeError):
    """Stable, content-free document lifecycle failure."""

    def __init__(self, code: str) -> None:
        if code not in _SAFE_ERRORS:
            code = "service_unavailable"
        self.code = code
        self.http_status, self.safe_message, self.retryable = _SAFE_ERRORS[code]
        super().__init__(code)


@dataclass(frozen=True)
class DocumentStatus:
    document_id: str
    state: str
    sha256: str
    bytes: int
    page_count: int | None
    index_version: str | None
    failure_code: str | None

    def response(self, request_id: str) -> dict[str, Any]:
        return {
            "api_version": API_VERSION,
            "contract_version": CONTRACT_VERSION,
            "request_id": request_id,
            "document_id": self.document_id,
            "state": self.state,
            "sha256": self.sha256,
            "bytes": self.bytes,
            "page_count": self.page_count,
            "index_version": self.index_version,
            "failure_code": self.failure_code,
        }


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _bounded_dual_parse(pdf_path: Path, timeout: int) -> int:
    """有界双解析：两个解析器各解析一次并返回页数。

    失败关闭：任一解析器抛错、超时、或两解析器页数不一致，均判
    ``document_invalid``。

    超时语义（诚实边界）：``result(timeout=...)`` 超时后主流程立即失败
    关闭并返回，线程池用 ``shutdown(wait=False)`` 收尾、**不等待未完成的
    解析线程**（若用 ``with`` 退出会隐式 ``shutdown(wait=True)`` 把主流程
    卡到解析线程跑完）。但已启动的解析线程无法被强制杀死（Python 线程
    限制），仍可能在后台占用 CPU；进程级硬超时与资源隔离由 D5-06 安全
    测试补，本 PR 不声称满足冻结的 ``parse_timeout_seconds=60`` 硬上限。
    """

    def _plumber_pages() -> int:
        import pdfplumber  # 延迟导入

        with pdfplumber.open(str(pdf_path)) as pdf:
            return len(pdf.pages)

    def _pypdf_pages() -> int:
        from pypdf import PdfReader  # 延迟导入

        with pdf_path.open("rb") as handle:
            return len(PdfReader(handle).pages)

    pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)
    first = pool.submit(_plumber_pages)
    second = pool.submit(_pypdf_pages)
    try:
        try:
            plumber_pages = first.result(timeout=timeout)
            pypdf_pages = second.result(timeout=timeout)
        except (concurrent.futures.TimeoutError, Exception):
            raise DocumentStoreError("document_invalid") from None
    finally:
        # 不等待未完成线程；进程级硬超时留给 D5-06。
        pool.shutdown(wait=False, cancel_futures=True)

    if plumber_pages != pypdf_pages:
        raise DocumentStoreError("document_invalid")
    if plumber_pages > MAX_PAGES:
        raise DocumentStoreError("document_invalid")
    if plumber_pages < 1:
        raise DocumentStoreError("document_invalid")
    return plumber_pages


@dataclass(frozen=True)
class DocumentAuditContext:
    """审计所需的脱敏上下文（不含任何 PDF 正文 / 文件名 / 原始异常）。"""

    request_id: str
    session_id: str
    source: str


class SqliteDocumentStore:
    """文档元数据（共享 auth 的 SQLite）+ 文件字节（实例目录）存储。

    元数据与审计共用 auth 的同一 SQLite 文件，保证「状态变更 + 审计」在
    同一事务原子提交；文件字节写入实例目录下的隔离区与索引目录，重启时
    清理未提交临时文件、读取最后完整的活动索引指针。
    """

    def __init__(self, auth_service: AuthService, data_dir: str | Path) -> None:
        self.auth = auth_service
        self.data_dir = Path(data_dir)
        self.quarantine_dir = self.data_dir / "quarantine"
        self.index_dir = self.data_dir / "index"
        self.quarantine_dir.mkdir(parents=True, exist_ok=True)
        self.index_dir.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self):
        return self.auth.store._connect()

    def _init_schema(self) -> None:
        with self.auth.store._lock, self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS documents (
                    document_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    bytes INTEGER NOT NULL,
                    page_count INTEGER,
                    index_version TEXT,
                    failure_code TEXT,
                    revision INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS active_index (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    index_version TEXT
                );
                CREATE TABLE IF NOT EXISTS document_cleanup (
                    document_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0
                );
                """
            )

    # ---- 文件字节 ----

    def _quarantine_path(self, document_id: str) -> Path:
        # document_id 由服务器生成（secrets.token_urlsafe），不含路径分隔符；
        # 客户端 filename 从不参与路径，杜绝路径穿越。
        return self.quarantine_dir / (document_id + ".pdf")

    def write_quarantine(self, document_id: str, content: bytes) -> None:
        path = self._quarantine_path(document_id)
        if path.exists():
            raise DocumentStoreError("state_conflict")
        try:
            path.write_bytes(content)
        except OSError:
            raise DocumentStoreError("service_unavailable") from None

    def read_quarantine(self, document_id: str) -> bytes:
        path = self._quarantine_path(document_id)
        try:
            return path.read_bytes()
        except OSError:
            raise DocumentStoreError("resource_not_found") from None

    def delete_quarantine(self, document_id: str) -> None:
        path = self._quarantine_path(document_id)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            raise DocumentStoreError("service_unavailable") from None

    # ---- 元数据 ----

    def create_record(
        self,
        document_id: str,
        *,
        sha256: str,
        bytes_count: int,
        audit: DocumentAuditContext | None = None,
    ) -> DocumentStatus:
        now = _utc_now()
        with self.auth.store._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state, sha256 FROM documents WHERE sha256=? AND state NOT IN ('deleted')",
                (sha256,),
            ).fetchone()
            if row is not None:
                # 幂等去重：相同 hash 已存在且未删除，返回已有对象（不重复写字节）。
                connection.rollback()
                existing = connection.execute(
                    "SELECT * FROM documents WHERE sha256=? AND state NOT IN ('deleted')",
                    (sha256,),
                ).fetchone()
                if existing is None:
                    raise DocumentStoreError("service_unavailable")
                return self._row_to_status(existing)
            connection.execute(
                "INSERT INTO documents(document_id, state, sha256, bytes, page_count, "
                "index_version, failure_code, revision, created_at, updated_at) "
                "VALUES (?, 'quarantined', ?, ?, NULL, NULL, NULL, 0, ?, ?)",
                (document_id, sha256, bytes_count, _iso(now), _iso(now)),
            )
            if audit is not None:
                self.auth._append_audit(
                    connection,
                    event_type="document_state_changed",
                    request_id=audit.request_id,
                    outcome="succeeded",
                    subject_id=audit.session_id,
                    source=audit.source,
                )
            connection.commit()
        return DocumentStatus(document_id, "quarantined", sha256, bytes_count, None, None, None)

    def get_record(self, document_id: str) -> DocumentStatus:
        with self.auth.store._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM documents WHERE document_id=?", (document_id,)
            ).fetchone()
        if row is None or row["state"] == "deleted":
            raise DocumentStoreError("resource_not_found")
        return self._row_to_status(row)

    def list_records(self) -> list[DocumentStatus]:
        with self.auth.store._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM documents WHERE state != 'deleted' ORDER BY created_at"
            ).fetchall()
        return [self._row_to_status(row) for row in rows]

    def compare_and_set_state(
        self,
        document_id: str,
        *,
        expected_state: str,
        new_state: str,
        page_count: int | None,
        index_version: str | None,
        failure_code: str | None,
        audit: DocumentAuditContext | None = None,
    ) -> DocumentStatus:
        now = _utc_now()
        with self.auth.store._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM documents WHERE document_id=?", (document_id,)
            ).fetchone()
            if row is None or row["state"] == "deleted":
                raise DocumentStoreError("resource_not_found")
            if row["state"] != expected_state:
                raise DocumentStoreError("state_conflict")
            revision = int(row["revision"]) + 1
            connection.execute(
                "UPDATE documents SET state=?, page_count=?, index_version=?, "
                "failure_code=?, revision=?, updated_at=? WHERE document_id=?",
                (new_state, page_count, index_version, failure_code, revision, _iso(now), document_id),
            )
            if audit is not None:
                self.auth._append_audit(
                    connection,
                    event_type="document_state_changed",
                    request_id=audit.request_id,
                    outcome="succeeded",
                    subject_id=audit.session_id,
                    source=audit.source,
                    resource_revision=revision,
                )
            connection.commit()
        return DocumentStatus(
            document_id, new_state, row["sha256"], int(row["bytes"]),
            page_count, index_version, failure_code,
        )

    def publish_document(
        self, document_id: str, audit: DocumentAuditContext | None = None
    ) -> DocumentStatus:
        """单次原子发布：切活动索引指针 + ``validated→published`` + 审计，同一事务。

        契约 ``pdf_lifecycle.publish`` 要求「单次原子指针切换」。旧实现把
        「切指针」与「改状态」拆成两次事务，后一步写入失败会造成指针已切到
        新版本、文档却仍为 ``validated`` 的撕裂。这里在同一 ``BEGIN IMMEDIATE``
        事务内完成指针切换、状态推进与审计，任一步失败整体回滚、旧指针不变。
        """
        now = _utc_now()
        with self.auth.store._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM documents WHERE document_id=?", (document_id,)
            ).fetchone()
            if row is None or row["state"] == "deleted":
                raise DocumentStoreError("resource_not_found")
            if row["state"] != "validated":
                raise DocumentStoreError("state_conflict")
            if not row["index_version"]:
                raise DocumentStoreError("document_invalid")
            revision = int(row["revision"]) + 1
            connection.execute(
                "INSERT INTO active_index(singleton, index_version) VALUES (1, ?) "
                "ON CONFLICT(singleton) DO UPDATE SET index_version=excluded.index_version",
                (row["index_version"],),
            )
            connection.execute(
                "UPDATE documents SET state='published', revision=?, updated_at=? "
                "WHERE document_id=?",
                (revision, _iso(now), document_id),
            )
            if audit is not None:
                self.auth._append_audit(
                    connection,
                    event_type="index_published",
                    request_id=audit.request_id,
                    outcome="succeeded",
                    subject_id=audit.session_id,
                    source=audit.source,
                    resource_revision=revision,
                )
                self.auth._append_audit(
                    connection,
                    event_type="document_state_changed",
                    request_id=audit.request_id,
                    outcome="succeeded",
                    subject_id=audit.session_id,
                    source=audit.source,
                    resource_revision=revision,
                )
            connection.commit()
        return DocumentStatus(
            document_id, "published", row["sha256"], int(row["bytes"]),
            row["page_count"], row["index_version"], None,
        )

    def disable_document(
        self, document_id: str, audit: DocumentAuditContext | None = None
    ) -> DocumentStatus:
        """停用：``published/validated → disabled``；若它是当前活动索引，同事务失效指针。

        契约「访客只能检索 published 文档」：停用当前活动索引指向的文档后，
        必须同时失效活动指针，避免后续检索仍命中已停用文档。指针失效与状态
        变更、审计在同一事务内提交，失败整体回滚。
        """
        now = _utc_now()
        with self.auth.store._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM documents WHERE document_id=?", (document_id,)
            ).fetchone()
            if row is None or row["state"] == "deleted":
                raise DocumentStoreError("resource_not_found")
            if row["state"] not in {"published", "validated"}:
                raise DocumentStoreError("state_conflict")
            revision = int(row["revision"]) + 1
            connection.execute(
                "UPDATE documents SET state='disabled', revision=?, updated_at=? "
                "WHERE document_id=?",
                (revision, _iso(now), document_id),
            )
            active = connection.execute(
                "SELECT index_version FROM active_index WHERE singleton=1"
            ).fetchone()
            if active is not None and active["index_version"] == row["index_version"]:
                connection.execute(
                    "UPDATE active_index SET index_version=NULL WHERE singleton=1"
                )
            if audit is not None:
                self.auth._append_audit(
                    connection,
                    event_type="document_state_changed",
                    request_id=audit.request_id,
                    outcome="succeeded",
                    subject_id=audit.session_id,
                    source=audit.source,
                    resource_revision=revision,
                )
            connection.commit()
        return DocumentStatus(
            document_id, "disabled", row["sha256"], int(row["bytes"]),
            row["page_count"], row["index_version"], None,
        )

    def get_active_index(self) -> str | None:
        with self.auth.store._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT index_version FROM active_index WHERE singleton=1"
            ).fetchone()
        return None if row is None else row["index_version"]

    @staticmethod
    def _row_to_status(row: sqlite3.Row) -> DocumentStatus:
        return DocumentStatus(
            document_id=row["document_id"],
            state=row["state"],
            sha256=row["sha256"],
            bytes=int(row["bytes"]),
            page_count=row["page_count"],
            index_version=row["index_version"],
            failure_code=row["failure_code"],
        )

    def mark_deleted(
        self,
        document_id: str,
        *,
        expected_state: str,
        audit: DocumentAuditContext | None = None,
    ) -> DocumentStatus:
        """原子逻辑删除：写 ``deleted`` 状态 + 插入清理 tombstone + 审计，同一事务。

        这是两阶段删除的第一阶段。tombstone（document_cleanup 表）按
        ``document_id`` 持久化，是机器可判定的物理清理交接状态：只要 tombstone
        存在，就说明「逻辑已删除、物理字节待回收」；重启后可枚举重试，杜绝
        「元数据已 deleted、字节残留、无恢复记录」的半提交。任一步失败整体回滚，
        不会出现已 deleted 但 tombstone 缺失的中间态。
        """
        now = _utc_now()
        with self.auth.store._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM documents WHERE document_id=?", (document_id,)
            ).fetchone()
            if row is None or row["state"] == "deleted":
                raise DocumentStoreError("resource_not_found")
            if row["state"] != expected_state:
                raise DocumentStoreError("state_conflict")
            revision = int(row["revision"]) + 1
            connection.execute(
                "UPDATE documents SET state='deleted', revision=?, updated_at=? "
                "WHERE document_id=?",
                (revision, _iso(now), document_id),
            )
            connection.execute(
                "INSERT OR IGNORE INTO document_cleanup(document_id, created_at, attempts) "
                "VALUES (?, ?, 0)",
                (document_id, _iso(now)),
            )
            if audit is not None:
                self.auth._append_audit(
                    connection,
                    event_type="document_state_changed",
                    request_id=audit.request_id,
                    outcome="succeeded",
                    subject_id=audit.session_id,
                    source=audit.source,
                    resource_revision=revision,
                )
            connection.commit()
        return DocumentStatus(
            document_id, "deleted", row["sha256"], int(row["bytes"]),
            row["page_count"], row["index_version"], None,
        )

    def clear_tombstone(self, document_id: str) -> None:
        """物理字节删除成功后移除 tombstone（清理任务交接完成）。"""
        with self.auth.store._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM document_cleanup WHERE document_id=?", (document_id,)
            )
            connection.commit()

    def list_pending_cleanup(self) -> list[str]:
        """枚举待物理清理的 document_id（重启后可重试的工作队列，非审计）。"""
        with self.auth.store._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT document_id FROM document_cleanup ORDER BY created_at"
            ).fetchall()
        return [row["document_id"] for row in rows]

    def retry_pending_cleanup(self) -> list[str]:
        """重试所有待物理清理的 tombstone，返回本轮清理完成的 document_id。

        失败的 tombstone 保留，等待下一轮；这是可恢复交接：任何时刻重启都能
        从 document_cleanup 表枚举剩余任务继续回收敏感字节。
        """
        cleaned: list[str] = []
        for document_id in self.list_pending_cleanup():
            try:
                self.delete_quarantine(document_id)
            except DocumentStoreError:
                continue  # 仍失败，保留 tombstone 供下次重试
            self.clear_tombstone(document_id)
            cleaned.append(document_id)
        return cleaned


class DocumentService:
    """文档生命周期编排：校验链 → 双解析 → 候选索引 → 原子发布。"""

    def __init__(self, store: SqliteDocumentStore) -> None:
        self.store = store

    def upload(
        self,
        *,
        filename: str,
        content_type: str,
        declared_bytes: int,
        content_sha256: str,
        file_bytes: bytes,
        audit: DocumentAuditContext,
    ) -> DocumentStatus:
        self._validate_metadata(filename, content_type, declared_bytes, content_sha256, file_bytes)

        # URL-safe randomness may start with '_' or '-', while the frozen API
        # identifier schema requires an alphanumeric first character.
        document_id = "doc." + secrets.token_urlsafe(16)
        status = self.store.create_record(
            document_id,
            sha256=_sha256(file_bytes),
            bytes_count=len(file_bytes),
            audit=audit,
        )
        # 幂等去重命中时 create_record 返回的是已存在对象，无需再解析。
        if status.document_id != document_id:
            return status

        try:
            self.store.write_quarantine(document_id, file_bytes)
            page_count = _bounded_dual_parse(
                self.store._quarantine_path(document_id), PARSE_TIMEOUT_SECONDS
            )
            index_version = self._build_index_candidate(document_id, page_count)
            status = self.store.compare_and_set_state(
                document_id,
                expected_state="quarantined",
                new_state="validated",
                page_count=page_count,
                index_version=index_version,
                failure_code=None,
                audit=audit,
            )
        except DocumentStoreError as exc:
            self.store.compare_and_set_state(
                document_id,
                expected_state="quarantined",
                new_state="failed",
                page_count=None,
                index_version=None,
                failure_code=exc.code,
            )
            raise
        return status

    def publish(self, document_id: str, audit: DocumentAuditContext) -> DocumentStatus:
        return self.store.publish_document(document_id, audit=audit)

    def disable(self, document_id: str, audit: DocumentAuditContext) -> DocumentStatus:
        return self.store.disable_document(document_id, audit=audit)

    def delete(self, document_id: str, audit: DocumentAuditContext) -> tuple[DocumentStatus, bool]:
        """两阶段删除。返回 ``(status, physically_cleaned)``。

        - 阶段 1（逻辑删除，原子）：写 ``deleted`` 状态 + 插入 tombstone + 审计。
        - 阶段 2（物理清理）：删隔离区字节；成功则移除 tombstone，失败则保留
          tombstone 供 ``retry_pending_cleanup`` 重启后重试。

        ``physically_cleaned=False`` 表示「逻辑已删除、物理字节待回收」，
        调用方必须如实反映，不得伪装成完整删除成功。
        """
        record = self.store.get_record(document_id)
        if record.state == "published":
            raise DocumentStoreError("state_conflict")
        status = self.store.mark_deleted(
            document_id, expected_state=record.state, audit=audit
        )
        try:
            self.store.delete_quarantine(document_id)
            self.store.clear_tombstone(document_id)
            return status, True
        except DocumentStoreError:
            # 物理清理失败：tombstone 已持久化，可枚举重试，不伪装成完整删除。
            return status, False

    def read(self, document_id: str) -> DocumentStatus:
        return self.store.get_record(document_id)

    def download(self, document_id: str) -> bytes:
        record = self.store.get_record(document_id)
        if record.state == "deleted":
            raise DocumentStoreError("resource_not_found")
        return self.store.read_quarantine(document_id)

    def list(self) -> list[DocumentStatus]:
        return self.store.list_records()

    # ---- 校验链 ----

    def _validate_metadata(
        self,
        filename: str,
        content_type: str,
        declared_bytes: int,
        content_sha256: str,
        file_bytes: bytes,
    ) -> None:
        # 1. server_generated_id 由服务端生成（见 upload）。
        # 2. extension_and_content_type：扩展名 + Content-Type 双重校验。
        if not isinstance(filename, str) or not filename.lower().endswith(PDF_EXTENSION):
            raise DocumentStoreError("unsupported_media_type")
        if content_type.split(";", 1)[0].strip().casefold() != PDF_CONTENT_TYPE:
            raise DocumentStoreError("unsupported_media_type")
        # 3. pdf_magic_bytes：字节流以 %PDF- 开头。
        if not isinstance(file_bytes, (bytes, bytearray)) or not bytes(file_bytes).startswith(PDF_MAGIC):
            raise DocumentStoreError("unsupported_media_type")
        # 4. byte_limit：声明的字节数与实际字节数、大小上限三重校验。
        if not isinstance(declared_bytes, int) or declared_bytes < 1:
            raise DocumentStoreError("invalid_request")
        if len(file_bytes) != declared_bytes:
            raise DocumentStoreError("document_invalid")
        if len(file_bytes) > MAX_FILE_BYTES:
            raise DocumentStoreError("upload_too_large")
        # 5. sha256_and_dedup：实际 hash 必须与声明一致（去重在 create_record 完成）。
        if not isinstance(content_sha256, str) or _sha256(file_bytes) != content_sha256:
            raise DocumentStoreError("document_invalid")

    def _build_index_candidate(self, document_id: str, page_count: int) -> str:
        """构建候选索引（版本号基于单调时间戳），失败保留旧索引。

        真正的全文索引产物由 D5-05 接 RAG 检索时扩展；此处冻结的是版本号
        与「候选索引不直接生效、需 publish 原子切换」的语义。
        """
        index_version = "idx." + secrets.token_hex(6)
        marker = self.store.index_dir / (index_version + ".json")
        try:
            marker.write_text(
                json.dumps(
                    {"document_id": document_id, "page_count": page_count},
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
        except OSError:
            raise DocumentStoreError("service_unavailable") from None
        return index_version


class DocumentApplication:
    """API-v3 适配器：六个 document 端点（upload/read/publish/disable/delete/download）。"""

    MAX_META_BYTES = 4096

    def __init__(self, auth_service: AuthService, document_service: DocumentService) -> None:
        self.auth_service = auth_service
        self.document_service = document_service

    def handle(
        self,
        method: str,
        path: str,
        *,
        request_id: str,
        content_type: str = "application/json",
        body: bytes = b"{}",
        cookies: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        source: str = "unknown",
        file_bytes: bytes | None = None,
    ) -> AuthHttpResponse:
        cookies = cookies or {}
        headers = {key.casefold(): value for key, value in (headers or {}).items()}
        try:
            token = cookies.get(COOKIE_NAME)
            csrf = headers.get("x-csrf-token")
            principal = self.auth_service.guard_admin_path(
                method, path, token, csrf_token=csrf, request_id=request_id
            )
            audit = DocumentAuditContext(request_id, principal.session_id, source)

            if method == "POST" and path == "/api/v3/admin/documents":
                meta = self._request(body, content_type)
                self._require_upload_meta(meta, request_id)
                status = self.document_service.upload(
                    filename=meta["filename"],
                    content_type=meta["content_type"],
                    declared_bytes=meta["declared_bytes"],
                    content_sha256=meta["content_sha256"],
                    file_bytes=file_bytes or b"",
                    audit=audit,
                )
                return self._json(200, status.response(request_id))

            match = self._match_document_path(path)
            if match is None:
                raise DocumentStoreError("invalid_request")
            document_id = match[0]
            suffix = match[1]

            if suffix == "":
                if method == "GET":
                    return self._json(200, self.document_service.read(document_id).response(request_id))
                if method == "DELETE":
                    self._require_empty(body, content_type)
                    _, physically_cleaned = self.document_service.delete(
                        document_id, audit=audit
                    )
                    # 物理清理未完成时如实返回 accepted（逻辑删除成功、字节待回收），
                    # 不得伪装成完整删除成功。
                    return self._json(
                        200,
                        {
                            "api_version": API_VERSION,
                            "contract_version": CONTRACT_VERSION,
                            "request_id": request_id,
                            "status": "deleted" if physically_cleaned else "accepted",
                            "resource_revision": 0,
                        },
                    )
            elif suffix == "/publish" and method == "POST":
                self._require_empty(body, content_type)
                return self._json(
                    200, self.document_service.publish(document_id, audit=audit).response(request_id)
                )
            elif suffix == "/disable" and method == "POST":
                self._require_empty(body, content_type)
                return self._json(
                    200, self.document_service.disable(document_id, audit=audit).response(request_id)
                )
            elif suffix == "/download" and method == "GET":
                return self._file_response(self.document_service.download(document_id))

            raise DocumentStoreError("invalid_request")
        except AuthError as exc:
            if exc.code == "session_invalid":
                exc = AuthError("authentication_required")
            return self._error(exc, request_id)
        except DocumentStoreError as exc:
            return self._error(exc, request_id)
        except sqlite3.Error:
            return self._error(DocumentStoreError("service_unavailable"), request_id)

    @staticmethod
    def _match_document_path(path: str) -> tuple[str, str] | None:
        prefix = "/api/v3/admin/documents/"
        if not path.startswith(prefix):
            return None
        rest = path[len(prefix):]
        for suffix in ("/publish", "/disable", "/download"):
            if rest.endswith(suffix):
                return rest[: -len(suffix)], suffix
        return rest, ""

    @classmethod
    def _request(cls, body: bytes, content_type: str) -> dict[str, Any]:
        if len(body) > cls.MAX_META_BYTES:
            raise DocumentStoreError("invalid_request")
        if content_type.split(";", 1)[0].strip().casefold() != "application/json":
            raise DocumentStoreError("invalid_request")
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            value = None
        if not isinstance(value, dict):
            raise DocumentStoreError("invalid_request")
        return value

    @classmethod
    def _require_empty(cls, body: bytes, content_type: str) -> None:
        if cls._request(body, content_type):
            raise DocumentStoreError("invalid_request")

    @staticmethod
    def _require_upload_meta(value: Mapping[str, Any], request_id: str) -> None:
        if set(value) != {
            "api_version",
            "contract_version",
            "request_id",
            "filename",
            "content_type",
            "declared_bytes",
            "content_sha256",
        }:
            raise DocumentStoreError("invalid_request")
        if (
            value.get("api_version") != API_VERSION
            or value.get("contract_version") != CONTRACT_VERSION
            or value.get("request_id") != request_id
        ):
            raise DocumentStoreError("invalid_request")

    @classmethod
    def _error(cls, exc: AuthError | DocumentStoreError, request_id: str) -> AuthHttpResponse:
        return cls._json(
            exc.http_status,
            {
                "api_version": API_VERSION,
                "contract_version": CONTRACT_VERSION,
                "request_id": request_id,
                "error": {
                    "code": exc.code,
                    "message": exc.safe_message,
                    "retryable": exc.retryable,
                },
            },
        )

    @staticmethod
    def _json(status: int, value: Mapping[str, Any]) -> AuthHttpResponse:
        return AuthHttpResponse(
            status,
            {"Content-Type": "application/json; charset=utf-8"},
            json.dumps(
                value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8"),
        )

    @staticmethod
    def _file_response(content: bytes) -> AuthHttpResponse:
        return AuthHttpResponse(
            200,
            {"Content-Type": "application/pdf", "Content-Length": str(len(content))},
            content,
        )
