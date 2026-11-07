"""D5-01 authentication, admin authorization and content-safe audit storage.

This module deliberately has no dependency on a web framework.  A deployment
adapter can map ``AuthError`` to the frozen API-v3 envelope and place the raw
session token in the returned cookie.  Only token hashes and metadata are
persisted.
"""

from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
from pathlib import Path
import re
import secrets
import sqlite3
import threading
from typing import Any, Callable, Mapping

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from argon2.low_level import Type


API_VERSION = "3.0"
CONTRACT_VERSION = "1.0.0"
BOOTSTRAP_TTL = timedelta(minutes=15)
IDLE_TTL = timedelta(minutes=30)
ABSOLUTE_TTL = timedelta(hours=8)
FAILURE_WINDOW = timedelta(minutes=5)
LOCKOUT_TTL = timedelta(minutes=15)
MAX_FAILURES = 5
MAX_BOOTSTRAP_FAILURES = 5
COOKIE_NAME = "ict_admin_session"
BOOTSTRAP_CONTRACT_VERSION = "2.0.0"

_SAFE_ERRORS = {
    "invalid_request": (400, "请求格式无效。", False),
    "authentication_required": (401, "需要有效身份。", False),
    "session_invalid": (401, "会话无效或已过期。", False),
    "forbidden": (403, "没有执行该操作的权限。", False),
    "csrf_invalid": (403, "请求安全校验失败。", False),
    "resource_not_found": (404, "资源不存在。", False),
    "admin_already_initialized": (409, "管理员已初始化。", False),
    "state_conflict": (409, "资源状态已变化，请刷新后重试。", True),
    "rate_limited": (429, "请求过于频繁，请稍后重试。", True),
    "service_unavailable": (503, "服务暂时不可用。", True),
}

_AUDIT_ALLOWED = frozenset(
    {
        "event_id",
        "event_type",
        "occurred_at",
        "request_id",
        "actor_role",
        "subject_type",
        "subject_id_hash",
        "outcome",
        "error_code",
        "source_fingerprint",
        "resource_revision",
        "usage_counts",
    }
)
_AUDIT_FORBIDDEN = frozenset(
    {
        "password",
        "cookie",
        "session_token",
        "csrf_token",
        "api_key",
        "master_key",
        "question_text",
        "answer_text",
        "database_row",
        "document_text",
        "raw_exception",
        "authorization_header",
    }
)


class AuthError(RuntimeError):
    """Stable, content-safe authentication failure."""

    def __init__(self, code: str) -> None:
        if code not in _SAFE_ERRORS:
            code = "service_unavailable"
        self.code = code
        self.http_status, self.safe_message, self.retryable = _SAFE_ERRORS[code]
        super().__init__(code)


@dataclass(frozen=True)
class BootstrapGrant:
    token: str
    expires_at: str


@dataclass(frozen=True)
class SessionCredentials:
    token: str
    csrf_token: str
    expires_at: str
    cookie_header: str

    def response(self, request_id: str, *, contract_version: str = CONTRACT_VERSION) -> dict[str, Any]:
        response = {
            "api_version": API_VERSION,
            "contract_version": contract_version,
            "request_id": request_id,
            "authenticated": True,
            "csrf_token": self.csrf_token,
            "expires_at": self.expires_at,
        }
        if contract_version == BOOTSTRAP_CONTRACT_VERSION:
            response.update(must_change_password=False, password_change_available=True,
                            allowed_actions=["admin", "change_password", "logout"])
        return response


@dataclass(frozen=True)
class AdminPrincipal:
    username: str
    session_id: str
    generation: int


@dataclass(frozen=True)
class AuthHttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class SqliteAuthStore:
    """Restart-safe single-admin store with serialized security mutations."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._lock = threading.RLock()
        self._init_schema()

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            yield connection
        except BaseException:
            # Every caller uses explicit transactions for security mutations.
            # A close currently rolls them back in sqlite, but doing so here is
            # deliberate and testable rather than relying on driver cleanup.
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def _init_schema(self) -> None:
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS auth_state (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    username TEXT,
                    password_hash TEXT,
                    session_generation INTEGER NOT NULL DEFAULT 0,
                    revision INTEGER NOT NULL DEFAULT 0,
                    fingerprint_key TEXT,
                    bootstrap_token_hash TEXT,
                    bootstrap_expires_at TEXT
                );
                INSERT OR IGNORE INTO auth_state(singleton) VALUES (1);
                CREATE TABLE IF NOT EXISTS admin_sessions (
                    session_id TEXT PRIMARY KEY,
                    token_hash TEXT NOT NULL UNIQUE,
                    csrf_hash TEXT NOT NULL,
                    username TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    absolute_expires_at TEXT NOT NULL,
                    revoked_at TEXT
                );
                CREATE TABLE IF NOT EXISTS login_limits (
                    dimension TEXT NOT NULL,
                    subject_hash TEXT NOT NULL,
                    window_started_at TEXT NOT NULL,
                    failures INTEGER NOT NULL,
                    blocked_until TEXT,
                    PRIMARY KEY (dimension, subject_hash)
                );
                CREATE TABLE IF NOT EXISTS auth_audit (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_json TEXT NOT NULL
                );
                """
            )
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(auth_state)")
            }
            if "fingerprint_key" not in columns:
                connection.execute("ALTER TABLE auth_state ADD COLUMN fingerprint_key TEXT")
            if "default_password_active" not in columns:
                connection.execute("ALTER TABLE auth_state ADD COLUMN default_password_active INTEGER NOT NULL DEFAULT 0")
            connection.execute(
                "UPDATE auth_state SET fingerprint_key=? WHERE singleton=1 AND fingerprint_key IS NULL",
                (secrets.token_hex(32),),
            )

    def read_state(self, connection: sqlite3.Connection | None = None) -> sqlite3.Row:
        if connection is not None:
            row = connection.execute(
                "SELECT * FROM auth_state WHERE singleton = 1"
            ).fetchone()
            assert row is not None
            return row
        with self._connect() as owned:
            return self.read_state(owned)

    def audit_events(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT event_json FROM auth_audit ORDER BY sequence"
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def audit_summary(self, *, limit: int = 20) -> tuple[int, list[dict[str, Any]]]:
        """Return only bounded, already-sanitized audit events for the admin view."""
        if not 1 <= limit <= 20:
            raise ValueError("invalid_audit_limit")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN")
            count = int(connection.execute("SELECT COUNT(*) FROM auth_audit").fetchone()[0])
            rows = connection.execute(
                "SELECT event_json FROM auth_audit ORDER BY sequence DESC LIMIT ?", (limit,)
            ).fetchall()
            connection.commit()
        return count, [json.loads(row[0]) for row in reversed(rows)]

    def persisted_values(self) -> str:
        """Test/support view used to prove raw credentials were not persisted."""
        with self._connect() as connection:
            result: list[str] = []
            for table in ("auth_state", "admin_sessions", "login_limits", "auth_audit"):
                for row in connection.execute(f"SELECT * FROM {table}").fetchall():
                    result.extend("" if value is None else str(value) for value in row)
        return "\n".join(result)


class AuthService:
    """Single-admin D5 authentication service and shared admin-path guard."""

    def __init__(
        self,
        store: SqliteAuthStore,
        *,
        now: Callable[[], datetime] = _utc_now,
        audit_key: bytes | None = None,
    ) -> None:
        self.store = store
        self.now = now
        stored_key = bytes.fromhex(str(store.read_state()["fingerprint_key"]))
        self._audit_key = audit_key or stored_key
        if len(self._audit_key) < 32:
            raise ValueError("audit_key must contain at least 32 bytes")
        self._hasher = PasswordHasher(type=Type.ID)
        self._dummy_hash = self._hasher.hash(secrets.token_urlsafe(32))

    def initialize_default_admin_once(self, password: str, *, request_id: str = "local_init") -> bool:
        """Create the v2 local administrator only on a truly empty store."""
        if not isinstance(password, str) or not 1 <= len(password) <= 256:
            raise AuthError("service_unavailable")
        with self.store._lock, self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = self.store.read_state(connection)
            if state["username"] is not None:
                connection.rollback()
                return False
            if (state["password_hash"] is not None or state["bootstrap_token_hash"] is not None
                    or connection.execute("SELECT 1 FROM admin_sessions LIMIT 1").fetchone()
                    or connection.execute("SELECT 1 FROM auth_audit LIMIT 1").fetchone()):
                connection.rollback()
                raise AuthError("service_unavailable")
            password_hash = self._hasher.hash(password)
            connection.execute(
                "UPDATE auth_state SET username='admin',password_hash=?,default_password_active=1,"
                "revision=revision+1 WHERE singleton=1 AND username IS NULL",
                (password_hash,),
            )
            self._append_audit(connection, event_type="admin_bootstrap", request_id=request_id,
                               outcome="succeeded")
            connection.commit()
            return True

    def default_password_active(self) -> bool:
        return bool(self.store.read_state()["default_password_active"])

    def create_bootstrap_grant(self) -> BootstrapGrant:
        now = self.now()
        raw = secrets.token_urlsafe(48)
        with self.store._lock, self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = self.store.read_state(connection)
            if state["username"] is not None:
                connection.rollback()
                raise AuthError("admin_already_initialized")
            expires = now + BOOTSTRAP_TTL
            connection.execute(
                "UPDATE auth_state SET bootstrap_token_hash=?, bootstrap_expires_at=?, revision=revision+1 WHERE singleton=1",
                (_digest(raw), _iso(expires)),
            )
            connection.commit()
        return BootstrapGrant(raw, _iso(expires))

    def bootstrap_admin(
        self,
        setup_token: str,
        username: str,
        password: str,
        *,
        request_id: str,
        source: str = "unknown",
        tls: bool = False,
    ) -> SessionCredentials:
        self._validate_bootstrap_input(setup_token, username, password)
        now = self.now()
        source_key = self._fingerprint("bootstrap_source", source)
        # Bootstrap has an independent conservative limiter.  Invalid grants
        # are counted before Argon2 work and cannot mutate administrator state.
        with self.store._lock, self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if self._is_blocked(connection, "bootstrap_source", source_key, now):
                self._append_audit(
                    connection,
                    event_type="security_limit_rejected",
                    request_id=request_id,
                    outcome="rejected",
                    error_code="rate_limited",
                    source=source,
                )
                connection.commit()
                raise AuthError("rate_limited")
            state = self.store.read_state(connection)
            try:
                self._require_valid_bootstrap(state, setup_token, now)
            except AuthError as exc:
                self._record_failure(
                    connection,
                    "bootstrap_source",
                    source_key,
                    now,
                    threshold=MAX_BOOTSTRAP_FAILURES,
                )
                self._append_audit(
                    connection,
                    event_type="authorization_denied",
                    request_id=request_id,
                    outcome="rejected",
                    error_code=exc.code,
                    source=source,
                )
                connection.commit()
                raise
            connection.commit()
        password_hash = self._hasher.hash(password)
        with self.store._lock, self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = self.store.read_state(connection)
            if state["username"] is not None:
                connection.rollback()
                raise AuthError("admin_already_initialized")
            try:
                self._require_valid_bootstrap(state, setup_token, now)
            except AuthError:
                connection.rollback()
                raise
            connection.execute(
                "UPDATE auth_state SET username=?, password_hash=?, bootstrap_token_hash=NULL, bootstrap_expires_at=NULL, revision=revision+1 WHERE singleton=1",
                (username.casefold(), password_hash),
            )
            credentials = self._insert_session(connection, username.casefold(), 0, now, tls)
            self._append_audit(
                connection,
                event_type="admin_bootstrap",
                request_id=request_id,
                outcome="succeeded",
                subject_id=credentials.token,
            )
            connection.execute(
                "DELETE FROM login_limits WHERE dimension='bootstrap_source' AND subject_hash=?",
                (source_key,),
            )
            connection.commit()
        return credentials

    def login(
        self,
        username: str,
        password: str,
        *,
        source: str,
        request_id: str,
        tls: bool = False,
    ) -> SessionCredentials:
        if not (1 <= len(username) <= 64 and 1 <= len(password) <= 256):
            raise AuthError("authentication_required")
        now = self.now()
        account_key = self._fingerprint("account", username.casefold())
        source_key = self._fingerprint("source", source)
        with self.store._lock, self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if self._is_blocked(connection, "account", account_key, now) or self._is_blocked(
                connection, "source", source_key, now
            ):
                self._append_audit(
                    connection,
                    event_type="security_limit_rejected",
                    request_id=request_id,
                    outcome="rejected",
                    error_code="rate_limited",
                    source=source,
                )
                connection.commit()
                raise AuthError("rate_limited")
            state = self.store.read_state(connection)
            candidate = state["password_hash"] if state["username"] == username.casefold() else self._dummy_hash
            verified = self._verify_password(candidate or self._dummy_hash, password)
            if not verified or state["username"] != username.casefold():
                self._record_failure(connection, "account", account_key, now)
                self._record_failure(connection, "source", source_key, now)
                self._append_audit(
                    connection,
                    event_type="admin_login_failed",
                    request_id=request_id,
                    outcome="rejected",
                    error_code="authentication_required",
                    source=source,
                )
                connection.commit()
                raise AuthError("authentication_required")
            connection.execute(
                "DELETE FROM login_limits WHERE (dimension='account' AND subject_hash=?) OR (dimension='source' AND subject_hash=?)",
                (account_key, source_key),
            )
            credentials = self._insert_session(
                connection, state["username"], int(state["session_generation"]), now, tls
            )
            self._append_audit(
                connection,
                event_type="admin_login_succeeded",
                request_id=request_id,
                outcome="succeeded",
                subject_id=credentials.token,
                source=source,
            )
            connection.commit()
        return credentials

    def authorize_admin(
        self,
        session_token: str | None,
        *,
        csrf_token: str | None = None,
        require_csrf: bool = False,
        request_id: str,
    ) -> AdminPrincipal:
        if not session_token:
            self._record_authorization_denied(
                request_id=request_id,
                error_code="authentication_required",
            )
            raise AuthError("authentication_required")
        now = self.now()
        with self.store._lock, self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._require_session(
                    connection,
                    session_token,
                    csrf_token=csrf_token,
                    require_csrf=require_csrf,
                    request_id=request_id,
                    now=now,
                )
            except AuthError as exc:
                if exc.code == "csrf_invalid":
                    connection.commit()
                else:
                    self._append_audit(
                        connection,
                        event_type="authorization_denied",
                        request_id=request_id,
                        outcome="rejected",
                        error_code="authentication_required",
                    )
                    connection.commit()
                raise
            connection.execute(
                "UPDATE admin_sessions SET last_seen_at=? WHERE session_id=?",
                (_iso(now), row["session_id"]),
            )
            connection.commit()
            return AdminPrincipal(row["username"], row["session_id"], int(row["generation"]))

    def guard_admin_path(
        self,
        method: str,
        path: str,
        session_token: str | None,
        *,
        csrf_token: str | None = None,
        request_id: str,
    ) -> AdminPrincipal:
        """Apply one backend guard to every frozen ``/api/v3/admin/*`` path."""
        if not path.startswith("/api/v3/admin/"):
            raise ValueError("guard_admin_path only accepts admin paths")
        return self.authorize_admin(
            session_token,
            csrf_token=csrf_token,
            require_csrf=method.upper() in {"POST", "PUT", "PATCH", "DELETE"},
            request_id=request_id,
        )

    def logout(self, token: str, csrf_token: str, *, request_id: str) -> int:
        now = self.now()
        with self.store._lock, self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._require_session(
                    connection,
                    token,
                    csrf_token=csrf_token,
                    require_csrf=True,
                    request_id=request_id,
                    now=now,
                )
            except AuthError as exc:
                self._finish_auth_rejection(connection, exc)
                raise
            cursor = connection.execute(
                "UPDATE admin_sessions SET revoked_at=? WHERE session_id=? AND revoked_at IS NULL",
                (_iso(now), row["session_id"]),
            )
            self._append_audit(
                connection,
                event_type="admin_logout",
                request_id=request_id,
                outcome="succeeded",
                subject_id=row["session_id"],
                resource_revision=cursor.rowcount,
            )
            connection.commit()
            return cursor.rowcount

    def revoke_all(self, token: str, csrf_token: str, *, request_id: str) -> int:
        now = self.now()
        with self.store._lock, self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._require_session(
                    connection,
                    token,
                    csrf_token=csrf_token,
                    require_csrf=True,
                    request_id=request_id,
                    now=now,
                )
            except AuthError as exc:
                self._finish_auth_rejection(connection, exc)
                raise
            state = self.store.read_state(connection)
            generation = int(state["session_generation"]) + 1
            connection.execute(
                "UPDATE auth_state SET session_generation=?, revision=revision+1 WHERE singleton=1",
                (generation,),
            )
            cursor = connection.execute(
                "UPDATE admin_sessions SET revoked_at=? WHERE revoked_at IS NULL",
                (_iso(now),),
            )
            self._append_audit(
                connection,
                event_type="admin_sessions_revoked",
                request_id=request_id,
                outcome="succeeded",
                resource_revision=generation,
            )
            connection.commit()
            return cursor.rowcount

    def change_password(
        self,
        token: str,
        csrf_token: str,
        current_password: str,
        new_password: str,
        *,
        request_id: str,
    ) -> int:
        """Change the admin password and atomically revoke every session."""
        if not (1 <= len(current_password) <= 256 and 12 <= len(new_password) <= 256) or current_password == new_password:
            raise AuthError("invalid_request")
        replacement_hash = self._hasher.hash(new_password)
        now = self.now()
        with self.store._lock, self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._require_session(
                    connection,
                    token,
                    csrf_token=csrf_token,
                    require_csrf=True,
                    request_id=request_id,
                    now=now,
                )
            except AuthError as exc:
                self._finish_auth_rejection(connection, exc)
                raise
            state = self.store.read_state(connection)
            if not self._verify_password(state["password_hash"] or self._dummy_hash, current_password):
                connection.rollback()
                raise AuthError("authentication_required")
            generation = int(state["session_generation"]) + 1
            connection.execute(
                "UPDATE auth_state SET password_hash=?, default_password_active=0, session_generation=?, revision=revision+1 WHERE singleton=1",
                (replacement_hash, generation),
            )
            connection.execute(
                "UPDATE admin_sessions SET revoked_at=? WHERE revoked_at IS NULL",
                (_iso(now),),
            )
            self._append_audit(
                connection,
                event_type="admin_sessions_revoked",
                request_id=request_id,
                outcome="succeeded",
                resource_revision=generation,
            )
            connection.commit()
            return generation

    @staticmethod
    def _validate_bootstrap_input(token: str, username: str, password: str) -> None:
        if not (32 <= len(token) <= 512):
            raise AuthError("invalid_request")
        if not (3 <= len(username) <= 64 and username.strip() == username):
            raise AuthError("invalid_request")
        if not (12 <= len(password) <= 256):
            raise AuthError("invalid_request")

    @staticmethod
    def _require_valid_bootstrap(
        state: sqlite3.Row, setup_token: str, now: datetime
    ) -> None:
        if state["username"] is not None:
            raise AuthError("admin_already_initialized")
        stored = state["bootstrap_token_hash"] or ""
        expires = state["bootstrap_expires_at"]
        if not (
            hmac.compare_digest(stored, _digest(setup_token))
            and expires is not None
            and now < _parse(expires)
        ):
            raise AuthError("authentication_required")

    def _require_session(
        self,
        connection: sqlite3.Connection,
        session_token: str,
        *,
        csrf_token: str | None,
        require_csrf: bool,
        request_id: str,
        now: datetime,
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM admin_sessions WHERE token_hash=?",
            (_digest(session_token),),
        ).fetchone()
        state = self.store.read_state(connection)
        invalid = row is None or row["revoked_at"] is not None
        if row is not None:
            invalid = invalid or (
                int(row["generation"]) != int(state["session_generation"])
                or now >= _parse(row["absolute_expires_at"])
                or now - _parse(row["last_seen_at"]) >= IDLE_TTL
            )
        if invalid:
            raise AuthError("session_invalid")
        if require_csrf and (
            not csrf_token or not hmac.compare_digest(row["csrf_hash"], _digest(csrf_token))
        ):
            self._append_audit(
                connection,
                event_type="csrf_rejected",
                request_id=request_id,
                outcome="rejected",
                error_code="csrf_invalid",
                subject_id=row["session_id"],
            )
            raise AuthError("csrf_invalid")
        return row

    @staticmethod
    def _finish_auth_rejection(connection: sqlite3.Connection, error: AuthError) -> None:
        # CSRF rejection has one allowed side effect: its content-safe audit event.
        # Every other authorization failure leaves storage untouched.
        if error.code == "csrf_invalid":
            connection.commit()
        else:
            connection.rollback()

    def _insert_session(
        self,
        connection: sqlite3.Connection,
        username: str,
        generation: int,
        now: datetime,
        tls: bool,
    ) -> SessionCredentials:
        token = secrets.token_urlsafe(48)
        csrf = secrets.token_urlsafe(48)
        session_id = secrets.token_urlsafe(24)
        expires = now + ABSOLUTE_TTL
        connection.execute(
            "INSERT INTO admin_sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)",
            (
                session_id,
                _digest(token),
                _digest(csrf),
                username,
                generation,
                _iso(now),
                _iso(now),
                _iso(expires),
            ),
        )
        secure = "; Secure" if tls else ""
        cookie = (
            f"{COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Strict; "
            f"Max-Age={int(ABSOLUTE_TTL.total_seconds())}{secure}"
        )
        return SessionCredentials(token, csrf, _iso(expires), cookie)

    def _verify_password(self, encoded: str, password: str) -> bool:
        try:
            return self._hasher.verify(encoded, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False

    def _fingerprint(self, kind: str, value: str) -> str:
        return hmac.new(
            self._audit_key, f"{kind}:{value}".encode("utf-8"), hashlib.sha256
        ).hexdigest()

    def _is_blocked(
        self, connection: sqlite3.Connection, dimension: str, key: str, now: datetime
    ) -> bool:
        row = connection.execute(
            "SELECT blocked_until FROM login_limits WHERE dimension=? AND subject_hash=?",
            (dimension, key),
        ).fetchone()
        return bool(row and row[0] and now < _parse(row[0]))

    def _record_failure(
        self,
        connection: sqlite3.Connection,
        dimension: str,
        key: str,
        now: datetime,
        *,
        threshold: int = MAX_FAILURES,
    ) -> None:
        row = connection.execute(
            "SELECT * FROM login_limits WHERE dimension=? AND subject_hash=?",
            (dimension, key),
        ).fetchone()
        failures = 1
        window = now
        if row and now - _parse(row["window_started_at"]) < FAILURE_WINDOW:
            failures = int(row["failures"]) + 1
            window = _parse(row["window_started_at"])
        blocked = now + LOCKOUT_TTL if failures >= threshold else None
        connection.execute(
            "INSERT INTO login_limits VALUES (?, ?, ?, ?, ?) ON CONFLICT(dimension, subject_hash) DO UPDATE SET window_started_at=excluded.window_started_at, failures=excluded.failures, blocked_until=excluded.blocked_until",
            (dimension, key, _iso(window), failures, _iso(blocked) if blocked else None),
        )

    def _record_authorization_denied(
        self, *, request_id: str, error_code: str
    ) -> None:
        with self.store._lock, self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._append_audit(
                connection,
                event_type="authorization_denied",
                request_id=request_id,
                outcome="rejected",
                error_code=error_code,
            )
            connection.commit()

    def _append_audit(
        self,
        connection: sqlite3.Connection,
        *,
        event_type: str,
        request_id: str,
        outcome: str,
        error_code: str | None = None,
        subject_id: str | None = None,
        source: str | None = None,
        resource_revision: int = 0,
    ) -> None:
        event = {
            "event_id": secrets.token_urlsafe(18),
            "event_type": event_type,
            "occurred_at": _iso(self.now()),
            # A request ID is caller controlled even when its field name is
            # allowlisted.  Persist only a keyed, non-reversible correlation ID.
            "request_id": "req_" + self._fingerprint("request", request_id),
            "actor_role": (
                "anonymous"
                if event_type
                in {"admin_login_failed", "authorization_denied", "security_limit_rejected"}
                else "admin"
            ),
            "subject_type": "admin_session" if subject_id else "auth_state",
            "subject_id_hash": self._fingerprint("subject", subject_id or "auth"),
            "outcome": outcome,
            "error_code": error_code,
            "source_fingerprint": self._fingerprint("source", source) if source else None,
            "resource_revision": resource_revision,
            "usage_counts": {},
        }
        if set(event) != _AUDIT_ALLOWED or set(event) & _AUDIT_FORBIDDEN:
            raise AuthError("service_unavailable")
        connection.execute(
            "INSERT INTO auth_audit(event_json) VALUES (?)",
            (json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")),),
        )


class AuthApplication:
    """Small API-v3 transport adapter for the four D5-01 auth endpoints."""

    MAX_BODY_BYTES = 4096

    def __init__(self, service: AuthService) -> None:
        self.service = service

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
        tls: bool = False,
    ) -> AuthHttpResponse:
        cookies = cookies or {}
        headers = {key.casefold(): value for key, value in (headers or {}).items()}
        response_version = (BOOTSTRAP_CONTRACT_VERSION if path == "/api/v3/admin/password"
                            else CONTRACT_VERSION)
        try:
            if method == "POST" and path == "/api/v3/auth/admin/bootstrap":
                data = self._request(body, content_type)
                self._require_shape(
                    data,
                    {"api_version", "contract_version", "setup_token", "username", "password"},
                )
                self._require_version(data)
                result = self.service.bootstrap_admin(
                    data["setup_token"], data["username"], data["password"],
                    request_id=request_id, source=source, tls=tls,
                )
                return self._session_response(result, request_id)
            if method == "POST" and path == "/api/v3/auth/admin/login":
                data = self._request(body, content_type)
                response_version = data.get("contract_version", CONTRACT_VERSION)
                if response_version == BOOTSTRAP_CONTRACT_VERSION:
                    self._require_v2_shape(data, {"api_version", "contract_version", "request_id",
                                                  "username", "password"})
                    if data["username"] != "admin":
                        raise AuthError("invalid_request")
                    request_id = data["request_id"]
                else:
                    self._require_shape(
                        data, {"api_version", "contract_version", "username", "password"}
                    )
                    self._require_version(data)
                result = self.service.login(
                    data["username"], data["password"], source=source,
                    request_id=request_id, tls=tls,
                )
                return self._session_response(result, request_id, contract_version=response_version)
            if path.startswith("/api/v3/admin/"):
                token = cookies.get(COOKIE_NAME)
                csrf = headers.get("x-csrf-token")
                # All admin paths, including logout/revoke-all, first traverse
                # the same authorization semantics and audit missing/invalid
                # identities before any route-specific operation.
                self.service.guard_admin_path(
                    method, path, token, csrf_token=csrf, request_id=request_id
                )
                if method == "POST" and path == "/api/v3/admin/logout":
                    self._require_empty_request(body, content_type)
                    revision = self.service.logout(token, csrf or "", request_id=request_id)
                    return self._operation_response(request_id, revision, clear_cookie=True, tls=tls)
                if method == "POST" and path == "/api/v3/admin/password":
                    data = self._request(body, content_type)
                    response_version = BOOTSTRAP_CONTRACT_VERSION
                    self._require_v2_shape(data, {"api_version", "contract_version", "request_id",
                                                  "current_password", "new_password"})
                    request_id = data["request_id"]
                    revision = self.service.change_password(
                        token or "", csrf or "", data["current_password"], data["new_password"],
                        request_id=request_id,
                    )
                    secure = "; Secure" if tls else ""
                    return self._json(200, {
                        "api_version": API_VERSION, "contract_version": BOOTSTRAP_CONTRACT_VERSION,
                        "request_id": request_id, "status": "password_changed",
                        "must_change_password": False, "other_sessions_revoked": True,
                        "resource_revision": revision,
                    }, extra={"Set-Cookie": f"{COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0{secure}"})
                if method == "POST" and path == "/api/v3/admin/sessions/revoke-all":
                    self._require_empty_request(body, content_type)
                    revision = self.service.revoke_all(token, csrf or "", request_id=request_id)
                    return self._operation_response(request_id, revision, clear_cookie=True, tls=tls)
                raise AuthError("resource_not_found")
            raise AuthError("resource_not_found")
        except AuthError as exc:
            if exc.code == "session_invalid":
                # Public admin endpoints use the frozen missing/invalid identity
                # shape; the more specific internal code is not exposed.
                exc = AuthError("authentication_required")
            return self._json(
                exc.http_status,
                {
                    "api_version": API_VERSION,
                    "contract_version": response_version,
                    "request_id": request_id,
                    "error": {
                        "code": exc.code,
                        "message": exc.safe_message,
                        "retryable": exc.retryable,
                    },
                },
            )
        except sqlite3.Error:
            exc = AuthError("service_unavailable")
            return self._json(
                exc.http_status,
                {
                    "api_version": API_VERSION,
                    "contract_version": response_version,
                    "request_id": request_id,
                    "error": {
                        "code": exc.code,
                        "message": exc.safe_message,
                        "retryable": exc.retryable,
                    },
                },
            )

    @classmethod
    def _request(cls, body: bytes, content_type: str) -> dict[str, Any]:
        if len(body) > cls.MAX_BODY_BYTES:
            raise AuthError("invalid_request")
        if content_type.split(";", 1)[0].strip().casefold() != "application/json":
            raise AuthError("invalid_request")
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise AuthError("invalid_request") from exc
        if not isinstance(value, dict):
            raise AuthError("invalid_request")
        return value

    @staticmethod
    def _require_shape(value: Mapping[str, Any], keys: set[str]) -> None:
        if set(value) != keys or any(not isinstance(value[key], str) for key in keys):
            raise AuthError("invalid_request")

    @staticmethod
    def _require_version(value: Mapping[str, Any]) -> None:
        if value["api_version"] != API_VERSION or value["contract_version"] != CONTRACT_VERSION:
            raise AuthError("invalid_request")

    @staticmethod
    def _require_v2_shape(value: Mapping[str, Any], keys: set[str]) -> None:
        if (set(value) != keys or any(not isinstance(value[key], str) for key in keys)
                or value["api_version"] != API_VERSION
                or value["contract_version"] != BOOTSTRAP_CONTRACT_VERSION
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value["request_id"])):
            raise AuthError("invalid_request")

    @classmethod
    def _require_empty_request(cls, body: bytes, content_type: str) -> None:
        if cls._request(body, content_type):
            raise AuthError("invalid_request")

    @classmethod
    def _session_response(cls, credentials: SessionCredentials, request_id: str,
                          *, contract_version: str = CONTRACT_VERSION) -> AuthHttpResponse:
        return cls._json(
            200, credentials.response(request_id, contract_version=contract_version),
            extra={"Set-Cookie": credentials.cookie_header}
        )

    @classmethod
    def _operation_response(
        cls, request_id: str, revision: int, *, clear_cookie: bool, tls: bool
    ) -> AuthHttpResponse:
        extra: dict[str, str] = {}
        if clear_cookie:
            secure = "; Secure" if tls else ""
            extra["Set-Cookie"] = (
                f"{COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0{secure}"
            )
        return cls._json(
            200,
            {
                "api_version": API_VERSION,
                "contract_version": CONTRACT_VERSION,
                "request_id": request_id,
                "status": "revoked",
                "resource_revision": revision,
            },
            extra=extra,
        )

    @staticmethod
    def _json(
        status: int, value: Mapping[str, Any], *, extra: Mapping[str, str] | None = None
    ) -> AuthHttpResponse:
        response_headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        }
        response_headers.update(extra or {})
        return AuthHttpResponse(
            status,
            response_headers,
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(),
        )


__all__ = [
    "ABSOLUTE_TTL",
    "AdminPrincipal",
    "AuthApplication",
    "AuthError",
    "AuthHttpResponse",
    "AuthService",
    "BootstrapGrant",
    "COOKIE_NAME",
    "IDLE_TTL",
    "SessionCredentials",
    "SqliteAuthStore",
]
