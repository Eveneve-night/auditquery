"""D5-02 authenticated API-key storage with an external master key.

The database contains only AES-256-GCM ciphertext and minimal status metadata.
The master key is supplied explicitly by the deployment environment and is
never generated into, or recovered from, the business database.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import base64
import binascii
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
from typing import Any, Callable, Mapping

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .auth import (
    API_VERSION,
    CONTRACT_VERSION,
    COOKIE_NAME,
    AuthError,
    AuthHttpResponse,
    AuthService,
)


PROVIDER = "bailian_beijing"
MODEL = "qwen3.7-flash"
MASTER_KEY_ENV = "ICT_SECRET_MASTER_KEY_B64"
FORMAT_VERSION = 1
CONNECTIVITY_WINDOW = timedelta(minutes=5)
CONNECTIVITY_LOCKOUT = timedelta(minutes=15)
MAX_CONNECTIVITY_ATTEMPTS = 3
_KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{11,507}[A-Za-z0-9]{4}$")

_SAFE_ERRORS = {
    "invalid_request": (400, "请求格式无效。", False),
    "authentication_required": (401, "需要有效身份。", False),
    "forbidden": (403, "没有执行该操作的权限。", False),
    "csrf_invalid": (403, "请求安全校验失败。", False),
    "secret_not_configured": (409, "API Key 尚未配置。", False),
    "state_conflict": (409, "资源状态已变化，请刷新后重试。", True),
    "rate_limited": (429, "请求过于频繁，请稍后重试。", True),
    "service_unavailable": (503, "服务暂时不可用。", True),
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class SecretStoreError(RuntimeError):
    """Stable, content-free SecretStore failure."""

    def __init__(self, code: str) -> None:
        if code not in _SAFE_ERRORS:
            code = "service_unavailable"
        self.code = code
        self.http_status, self.safe_message, self.retryable = _SAFE_ERRORS[code]
        super().__init__(code)


@dataclass(frozen=True)
class SecretStatus:
    configured: bool
    revision: int
    updated_at: str | None
    last_four: str | None

    def response(self, request_id: str) -> dict[str, Any]:
        return {
            "api_version": API_VERSION,
            "contract_version": CONTRACT_VERSION,
            "request_id": request_id,
            "configured": self.configured,
            "provider": PROVIDER,
            "model": MODEL,
            "source": "secret_store" if self.configured else "not_configured",
            "updated_at": self.updated_at,
            "last_four": self.last_four,
        }


def load_master_key(
    *,
    environ: Mapping[str, str] | None = None,
    docker_secret_path: str | Path | None = None,
) -> bytes:
    """Load one 32-byte key from an explicit environment or Docker secret.

    No default path is scanned. Supplying both sources fails closed so a stale
    environment value cannot silently shadow an intended Docker secret.
    """

    environ = os.environ if environ is None else environ
    encoded = environ.get(MASTER_KEY_ENV)
    if encoded and docker_secret_path is not None:
        raise SecretStoreError("service_unavailable")
    if docker_secret_path is not None:
        try:
            encoded = Path(docker_secret_path).read_text(encoding="ascii").strip()
        except (OSError, UnicodeError):
            encoded = None
    if not encoded:
        raise SecretStoreError("service_unavailable")
    try:
        key = base64.b64decode(encoded, altchars=b"-_", validate=True)
    except (ValueError, binascii.Error):
        key = b""
    if len(key) != 32:
        raise SecretStoreError("service_unavailable")
    return key


class SqliteSecretStore:
    """Restart-safe, revisioned SecretStore backed by authenticated ciphertext."""

    def __init__(self, path: str | Path, *, master_key: bytes | None) -> None:
        if master_key is not None and len(master_key) != 32:
            raise SecretStoreError("service_unavailable")
        self.path = str(path)
        self._master_key = master_key
        self._lock = threading.RLock()
        self._init_schema()

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        try:
            connection.row_factory = sqlite3.Row
            yield connection
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def _init_schema(self) -> None:
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS provider_secrets (
                    provider TEXT PRIMARY KEY,
                    model TEXT NOT NULL,
                    configured INTEGER NOT NULL CHECK(configured IN (0, 1)),
                    revision INTEGER NOT NULL CHECK(revision >= 0),
                    format_version INTEGER NOT NULL,
                    nonce BLOB,
                    ciphertext BLOB,
                    updated_at TEXT,
                    last_four TEXT
                );
                CREATE TABLE IF NOT EXISTS connectivity_limits (
                    source_hash TEXT PRIMARY KEY,
                    window_started_at TEXT NOT NULL,
                    attempts INTEGER NOT NULL,
                    blocked_until TEXT
                );
                """
            )

    def status(self) -> SecretStatus:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM provider_secrets WHERE provider=?",
                (PROVIDER,),
            ).fetchone()
        if row is None:
            return SecretStatus(False, 0, None, None)
        if row["configured"]:
            # Authenticate all persisted metadata before exposing even the
            # redacted status fields.  This also makes a configured store fail
            # closed when its external master key is unavailable.
            self._decrypt_row(row)
        return SecretStatus(
            bool(row["configured"]),
            int(row["revision"]),
            row["updated_at"] if row["configured"] else None,
            row["last_four"] if row["configured"] else None,
        )

    def put_if_absent(
        self, api_key: str, *, now: datetime | None = None,
        audit: SecretAudit | None = None,
    ) -> SecretStatus:
        return self._write(
            api_key, expected_revision=None, require_configured=False, now=now, audit=audit
        )

    def replace(
        self, api_key: str, *, expected_revision: int, now: datetime | None = None,
        audit: SecretAudit | None = None,
    ) -> SecretStatus:
        return self._write(
            api_key,
            expected_revision=expected_revision,
            require_configured=True,
            now=now,
            audit=audit,
        )

    def delete(
        self, *, expected_revision: int, now: datetime | None = None,
        audit: SecretAudit | None = None,
    ) -> SecretStatus:
        timestamp = _iso(now or _utc_now())
        with self._lock, self._connect() as connection:
            if audit is not None:
                audit.attach(connection, self.path)
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT configured, revision FROM provider_secrets WHERE provider=?",
                (PROVIDER,),
            ).fetchone()
            if (
                row is None
                or not row["configured"]
                or int(row["revision"]) != expected_revision
            ):
                raise SecretStoreError("state_conflict")
            revision = expected_revision + 1
            connection.execute(
                "UPDATE provider_secrets SET configured=0, revision=?, nonce=NULL, "
                "ciphertext=NULL, updated_at=?, last_four=NULL WHERE provider=?",
                (revision, timestamp, PROVIDER),
            )
            self._after_mutation(connection)
            if audit is not None:
                audit.append(
                    connection, event_type="secret_changed", outcome="succeeded",
                    resource_revision=revision,
                )
            connection.commit()
        return SecretStatus(False, revision, None, None)

    def get_for_provider_call(self) -> str:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM provider_secrets WHERE provider=?", (PROVIDER,)
            ).fetchone()
        if row is None or not row["configured"]:
            raise SecretStoreError("secret_not_configured")
        return self._decrypt_row(row)

    def _decrypt_row(self, row: sqlite3.Row) -> str:
        cipher = self._cipher()
        if (
            row["format_version"] != FORMAT_VERSION
            or not isinstance(row["nonce"], bytes)
            or not isinstance(row["ciphertext"], bytes)
        ):
            raise SecretStoreError("service_unavailable")
        try:
            if row["model"] != MODEL or not row["updated_at"] or not row["last_four"]:
                raise SecretStoreError("service_unavailable")
            plaintext = cipher.decrypt(
                row["nonce"],
                row["ciphertext"],
                self._aad(
                    int(row["revision"]),
                    updated_at=row["updated_at"],
                    last_four=row["last_four"],
                ),
            ).decode("utf-8")
        except (InvalidTag, UnicodeError, ValueError):
            plaintext = ""
        if not plaintext:
            raise SecretStoreError("service_unavailable")
        self._validate_api_key(plaintext)
        return plaintext

    def startup_check(self) -> SecretStatus:
        return self.status()

    def consume_connectivity_attempt(
        self,
        source_hash: str,
        *,
        now: datetime | None = None,
        audit: SecretAudit | None = None,
    ) -> None:
        current = now or _utc_now()
        with self._lock, self._connect() as connection:
            if audit is not None:
                audit.attach(connection, self.path)
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM connectivity_limits WHERE source_hash=?", (source_hash,)
            ).fetchone()
            if row and row["blocked_until"] and current < _parse(row["blocked_until"]):
                if audit is not None:
                    audit.append(
                        connection, event_type="security_limit_rejected",
                        outcome="rejected", error_code="rate_limited",
                    )
                connection.commit()
                raise SecretStoreError("rate_limited")
            attempts = 1
            window_started = current
            if row and current - _parse(row["window_started_at"]) < CONNECTIVITY_WINDOW:
                attempts = int(row["attempts"]) + 1
                window_started = _parse(row["window_started_at"])
            if attempts > MAX_CONNECTIVITY_ATTEMPTS:
                blocked_until = current + CONNECTIVITY_LOCKOUT
                connection.execute(
                    "INSERT INTO connectivity_limits VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(source_hash) DO UPDATE SET "
                    "window_started_at=excluded.window_started_at, "
                    "attempts=excluded.attempts, blocked_until=excluded.blocked_until",
                    (source_hash, _iso(window_started), attempts, _iso(blocked_until)),
                )
                if audit is not None:
                    audit.append(
                        connection, event_type="security_limit_rejected",
                        outcome="rejected", error_code="rate_limited",
                    )
                connection.commit()
                raise SecretStoreError("rate_limited")
            connection.execute(
                "INSERT INTO connectivity_limits VALUES (?, ?, ?, NULL) "
                "ON CONFLICT(source_hash) DO UPDATE SET "
                "window_started_at=excluded.window_started_at, "
                "attempts=excluded.attempts, blocked_until=NULL",
                (source_hash, _iso(window_started), attempts),
            )
            connection.commit()

    def record_connectivity_outcome(
        self,
        audit: SecretAudit,
        *,
        succeeded: bool,
        usage_counts: Mapping[str, int] | None = None,
    ) -> None:
        with self._lock, self._connect() as connection:
            audit.attach(connection, self.path)
            connection.execute("BEGIN IMMEDIATE")
            audit.append(
                connection, event_type="connectivity_tested",
                outcome="succeeded" if succeeded else "failed",
                error_code=None if succeeded else "service_unavailable",
                usage_counts=usage_counts,
            )
            connection.commit()

    def _write(
        self,
        api_key: str,
        *,
        expected_revision: int | None,
        require_configured: bool,
        now: datetime | None,
        audit: SecretAudit | None,
    ) -> SecretStatus:
        self._validate_api_key(api_key)
        cipher = self._cipher()
        timestamp = _iso(now or _utc_now())
        with self._lock, self._connect() as connection:
            if audit is not None:
                audit.attach(connection, self.path)
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT configured, revision FROM provider_secrets WHERE provider=?",
                (PROVIDER,),
            ).fetchone()
            configured = bool(row and row["configured"])
            current_revision = int(row["revision"]) if row else 0
            if require_configured:
                if not configured or current_revision != expected_revision:
                    raise SecretStoreError("state_conflict")
            elif configured:
                raise SecretStoreError("state_conflict")
            revision = current_revision + 1
            nonce = os.urandom(12)
            ciphertext = cipher.encrypt(
                nonce,
                api_key.encode("utf-8"),
                self._aad(revision, updated_at=timestamp, last_four=api_key[-4:]),
            )
            connection.execute(
                "INSERT INTO provider_secrets VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(provider) DO UPDATE SET model=excluded.model, configured=1, "
                "revision=excluded.revision, format_version=excluded.format_version, "
                "nonce=excluded.nonce, ciphertext=excluded.ciphertext, "
                "updated_at=excluded.updated_at, last_four=excluded.last_four",
                (
                    PROVIDER,
                    MODEL,
                    revision,
                    FORMAT_VERSION,
                    nonce,
                    ciphertext,
                    timestamp,
                    api_key[-4:],
                ),
            )
            self._after_mutation(connection)
            if audit is not None:
                audit.append(
                    connection, event_type="secret_changed", outcome="succeeded",
                    resource_revision=revision,
                )
            connection.commit()
        return SecretStatus(True, revision, timestamp, api_key[-4:])

    def _cipher(self) -> AESGCM:
        if self._master_key is None:
            raise SecretStoreError("service_unavailable")
        return AESGCM(self._master_key)

    @staticmethod
    def _aad(revision: int, *, updated_at: str, last_four: str) -> bytes:
        return json.dumps(
            {
                "format_version": FORMAT_VERSION,
                "last_four": last_four,
                "model": MODEL,
                "provider": PROVIDER,
                "revision": revision,
                "updated_at": updated_at,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")

    @staticmethod
    def _validate_api_key(api_key: str) -> None:
        if not isinstance(api_key, str) or not _KEY_PATTERN.fullmatch(api_key):
            raise SecretStoreError("invalid_request")

    def _after_mutation(self, _connection: sqlite3.Connection) -> None:
        """Fault-injection seam used to prove transaction rollback."""


ConnectivityProbe = Callable[[str], Mapping[str, int]]


@dataclass(frozen=True)
class SecretAudit:
    """Write allowlisted security events into AuthStore in the same SQLite transaction."""

    auth: AuthService
    request_id: str
    session_id: str
    source: str

    def attach(self, connection: sqlite3.Connection, secret_path: str) -> None:
        auth_path = self.auth.store.path
        if Path(auth_path).resolve() != Path(secret_path).resolve():
            # SQLite's cross-file commit is crash-atomic only for file-backed
            # databases outside WAL mode. Refuse unsafe deployment settings.
            if secret_path == ":memory:" or auth_path == ":memory:":
                raise SecretStoreError("service_unavailable")
            if connection.execute("PRAGMA main.journal_mode").fetchone()[0].lower() != "delete":
                raise SecretStoreError("service_unavailable")
            connection.execute("ATTACH DATABASE ? AS auth_audit_db", (auth_path,))
            if connection.execute("PRAGMA auth_audit_db.journal_mode").fetchone()[0].lower() != "delete":
                raise SecretStoreError("service_unavailable")
            connection.execute("PRAGMA main.synchronous=EXTRA")
            connection.execute("PRAGMA auth_audit_db.synchronous=EXTRA")

    def append(
        self,
        connection: sqlite3.Connection,
        *,
        event_type: str,
        outcome: str,
        error_code: str | None = None,
        resource_revision: int = 0,
        usage_counts: Mapping[str, int] | None = None,
    ) -> None:
        if event_type not in {
            "secret_changed", "connectivity_tested", "security_limit_rejected"
        } or outcome not in {"succeeded", "failed", "rejected"}:
            raise SecretStoreError("service_unavailable")
        counts = dict(usage_counts or {})
        if set(counts) - {"requests", "input_tokens", "output_tokens"} or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in counts.values()
        ):
            raise SecretStoreError("service_unavailable")
        event = {
            "event_id": secrets.token_urlsafe(18),
            "event_type": event_type,
            "occurred_at": _iso(self.auth.now()),
            "request_id": "req_" + self.auth._fingerprint("request", self.request_id),
            "actor_role": "admin",
            "subject_type": "admin_session",
            "subject_id_hash": self.auth._fingerprint("subject", self.session_id),
            "outcome": outcome,
            "error_code": error_code,
            "source_fingerprint": self.auth._fingerprint("source", self.source),
            "resource_revision": resource_revision,
            "usage_counts": counts,
        }
        # This is deliberately the frozen D5 allowlist, not a copy of request
        # or provider data.  The alias is present only when databases differ.
        attached_names = {
            row[1] for row in connection.execute("PRAGMA database_list")
        }
        table = (
            "auth_audit_db.auth_audit"
            if "auth_audit_db" in attached_names else "auth_audit"
        )
        connection.execute(
            f"INSERT INTO {table}(event_json) VALUES (?)",
            (json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")),),
        )


class SecretService:
    """Fixed-provider SecretStore orchestration; only connectivity may call out."""

    def __init__(self, store: SqliteSecretStore, *, rate_key: bytes) -> None:
        if len(rate_key) < 32:
            raise ValueError("rate_key must contain at least 32 bytes")
        self.store = store
        self._rate_key = rate_key

    def save(self, api_key: str, *, audit: SecretAudit | None = None) -> SecretStatus:
        return self.store.put_if_absent(api_key, audit=audit)

    def replace(self, api_key: str, *, audit: SecretAudit | None = None) -> SecretStatus:
        return self.store.replace(
            api_key, expected_revision=self.store.status().revision, audit=audit
        )

    def delete(self, *, audit: SecretAudit | None = None) -> SecretStatus:
        return self.store.delete(
            expected_revision=self.store.status().revision, audit=audit
        )

    def status(self) -> SecretStatus:
        return self.store.status()

    def test_connectivity(
        self,
        *,
        explicit_confirmation: bool,
        source: str,
        probe: ConnectivityProbe,
        audit: SecretAudit | None = None,
    ) -> dict[str, Any]:
        if explicit_confirmation is not True:
            raise SecretStoreError("invalid_request")
        source_hash = hmac.new(
            self._rate_key, f"connectivity:{source}".encode("utf-8"), hashlib.sha256
        ).hexdigest()
        self.store.consume_connectivity_attempt(source_hash, audit=audit)
        try:
            api_key = self.store.get_for_provider_call()
        except SecretStoreError:
            if audit is not None:
                self.store.record_connectivity_outcome(audit, succeeded=False)
            # The internal absence code is not in the frozen public catalog.
            raise SecretStoreError("service_unavailable") from None
        failed = False
        try:
            usage = dict(probe(api_key))
        except Exception:
            # Do not chain untrusted provider exceptions: they can contain the
            # complete API key in their message, cause, or traceback.
            failed = True
            usage = {}
        api_key = ""
        if failed:
            if audit is not None:
                self.store.record_connectivity_outcome(audit, succeeded=False)
            raise SecretStoreError("service_unavailable")
        if set(usage) - {"requests", "input_tokens", "output_tokens"} or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in usage.values()
        ):
            if audit is not None:
                self.store.record_connectivity_outcome(audit, succeeded=False)
            raise SecretStoreError("service_unavailable")
        if audit is not None:
            self.store.record_connectivity_outcome(
                audit, succeeded=True, usage_counts=usage
            )
        return {
            "provider": PROVIDER,
            "model": MODEL,
            "reachable": True,
            "usage_counts": usage,
        }


class SecretApplication:
    """API-v3 adapter for the five fixed Bailian SecretStore endpoints."""

    MAX_BODY_BYTES = 2048

    def __init__(
        self,
        auth_service: AuthService,
        secret_service: SecretService,
        *,
        connectivity_probe: ConnectivityProbe,
    ) -> None:
        self.auth_service = auth_service
        self.secret_service = secret_service
        self.connectivity_probe = connectivity_probe

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
    ) -> AuthHttpResponse:
        cookies = cookies or {}
        headers = {key.casefold(): value for key, value in (headers or {}).items()}
        try:
            token = cookies.get(COOKIE_NAME)
            csrf = headers.get("x-csrf-token")
            principal = self.auth_service.guard_admin_path(
                method, path, token, csrf_token=csrf, request_id=request_id
            )
            audit = SecretAudit(
                self.auth_service, request_id, principal.session_id, source
            )
            base = "/api/v3/admin/secrets/bailian"
            if method == "GET" and path == base:
                return self._json(200, self.secret_service.status().response(request_id))
            if method in {"POST", "PUT"} and path == base:
                data = self._request(body, content_type)
                self._require_key_request(data, request_id)
                status = (
                    self.secret_service.save(data["api_key"], audit=audit)
                    if method == "POST"
                    else self.secret_service.replace(data["api_key"], audit=audit)
                )
                return self._json(200, status.response(request_id))
            if method == "DELETE" and path == base:
                self._require_empty(body, content_type)
                status = self.secret_service.delete(audit=audit)
                return self._json(
                    200,
                    {
                        "api_version": API_VERSION,
                        "contract_version": CONTRACT_VERSION,
                        "request_id": request_id,
                        "status": "deleted",
                        "resource_revision": status.revision,
                    },
                )
            if method == "POST" and path == base + "/connectivity-test":
                data = self._request(body, content_type)
                if set(data) != {
                    "api_version",
                    "contract_version",
                    "request_id",
                    "explicit_confirmation",
                }:
                    raise SecretStoreError("invalid_request")
                self._require_version_and_request(data, request_id)
                summary = self.secret_service.test_connectivity(
                    explicit_confirmation=data["explicit_confirmation"],
                    source=source,
                    probe=self.connectivity_probe,
                    audit=audit,
                )
                return self._json(
                    200,
                    {
                        "api_version": API_VERSION,
                        "contract_version": CONTRACT_VERSION,
                        "request_id": request_id,
                        "status": "ok",
                        "summary": summary,
                    },
                )
            raise SecretStoreError("invalid_request")
        except AuthError as exc:
            if exc.code == "session_invalid":
                exc = AuthError("authentication_required")
            return self._error(exc, request_id)
        except SecretStoreError as exc:
            if exc.code == "secret_not_configured":
                exc = SecretStoreError("service_unavailable")
            return self._error(exc, request_id)
        except sqlite3.Error:
            return self._error(SecretStoreError("service_unavailable"), request_id)

    @classmethod
    def _request(cls, body: bytes, content_type: str) -> dict[str, Any]:
        if len(body) > cls.MAX_BODY_BYTES:
            raise SecretStoreError("invalid_request")
        if content_type.split(";", 1)[0].strip().casefold() != "application/json":
            raise SecretStoreError("invalid_request")
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            value = None
        if not isinstance(value, dict):
            raise SecretStoreError("invalid_request")
        return value

    @classmethod
    def _require_empty(cls, body: bytes, content_type: str) -> None:
        if cls._request(body, content_type):
            raise SecretStoreError("invalid_request")

    @classmethod
    def _require_key_request(cls, value: Mapping[str, Any], request_id: str) -> None:
        if set(value) != {
            "api_version",
            "contract_version",
            "request_id",
            "provider",
            "model",
            "api_key",
        }:
            raise SecretStoreError("invalid_request")
        cls._require_version_and_request(value, request_id)
        if value["provider"] != PROVIDER or value["model"] != MODEL:
            raise SecretStoreError("invalid_request")
        SqliteSecretStore._validate_api_key(value["api_key"])

    @staticmethod
    def _require_version_and_request(value: Mapping[str, Any], request_id: str) -> None:
        if (
            value.get("api_version") != API_VERSION
            or value.get("contract_version") != CONTRACT_VERSION
            or value.get("request_id") != request_id
        ):
            raise SecretStoreError("invalid_request")

    @classmethod
    def _error(cls, exc: AuthError | SecretStoreError, request_id: str) -> AuthHttpResponse:
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
