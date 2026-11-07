"""D5 visitor session ownership and fail-closed agent transport."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
from typing import Any, Mapping, Protocol

from jsonschema import Draft202012Validator

from ..api.online_contracts import OnlineContractError, OnlineContractValidator
from .auth import API_VERSION, CONTRACT_VERSION, AuthHttpResponse, AuthService


COOKIE_NAME = "ict_visitor_session"
SESSION_TTL = timedelta(hours=24)
MAX_JSON_BYTES = 8192
OPERATION_LIMIT = 60
OPERATION_WINDOW = timedelta(minutes=5)
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_ERRORS = {
    "invalid_request": (400, "请求格式无效。"),
    "authentication_required": (401, "需要有效身份。"),
    "resource_not_found": (404, "资源不存在。"),
    "operation_replayed": (409, "请求已处理。"),
    "state_conflict": (409, "会话状态已变化。"),
    "rate_limited": (429, "请求过于频繁。"),
    "service_unavailable": (503, "服务暂时不可用。"),
}
_SCHEMA = json.loads((Path(__file__).resolve().parents[3] / "configs" / "schemas" / "d5" /
                      "api-v3.schema.json").read_text(encoding="utf-8"))
_REQUEST_VALIDATORS = {
    key: Draft202012Validator({**_SCHEMA, "$ref": f"#/$defs/{key}"})
    for key in ("session_create_request", "query_request", "clarification_request")
}


class VisitorError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code if code in _ERRORS else "service_unavailable"
        self.http_status, self.safe_message = _ERRORS[self.code]
        super().__init__(self.code)


class AgentPort(Protocol):
    def submit(self, request: Mapping[str, Any]) -> Mapping[str, Any]: ...


@contextmanager
def _prepared_agent(agent: AgentPort, request: Mapping[str, Any]):
    transaction = getattr(agent, "transaction", None)
    if callable(transaction):
        with transaction(request) as prepared:
            yield prepared
    else:
        yield agent.submit(request), lambda: None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class VisitorStore:
    """Share the instance metadata database, but never store raw guest tokens."""

    def __init__(self, auth: AuthService, *, operation_limit: int | None = None,
                 operation_window_seconds: int | None = None) -> None:
        self.auth = auth
        self.operation_limit = self._positive_setting(
            operation_limit, "ICT_QA_RATE_LIMIT", OPERATION_LIMIT)
        seconds = self._positive_setting(
            operation_window_seconds, "ICT_QA_RATE_WINDOW_SECONDS", int(OPERATION_WINDOW.total_seconds()))
        self.operation_window = timedelta(seconds=seconds)
        self._lock = threading.RLock()
        with self.auth.store._lock, self.auth.store._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS visitor_sessions (
                    session_id TEXT PRIMARY KEY,
                    token_hash TEXT NOT NULL UNIQUE,
                    expires_at TEXT NOT NULL,
                    state_revision INTEGER NOT NULL DEFAULT 0,
                    turn_id INTEGER NOT NULL DEFAULT 0,
                    deleted_at TEXT
                );
                CREATE TABLE IF NOT EXISTS visitor_operations (
                    session_id TEXT NOT NULL,
                    operation_id_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    route TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    PRIMARY KEY(session_id, operation_id_hash),
                    FOREIGN KEY(session_id) REFERENCES visitor_sessions(session_id)
                );
                CREATE TABLE IF NOT EXISTS visitor_create_limits (
                    source_hash TEXT PRIMARY KEY,
                    window_started_at TEXT NOT NULL,
                    attempts INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS visitor_operation_limits (
                    dimension TEXT NOT NULL,
                    subject_hash TEXT NOT NULL,
                    window_started_at TEXT NOT NULL,
                    attempts INTEGER NOT NULL,
                    PRIMARY KEY(dimension, subject_hash)
                );
                """
            )

    @staticmethod
    def _positive_setting(value: int | None, env_name: str, default: int) -> int:
        raw = os.environ.get(env_name, str(default)) if value is None else value
        if isinstance(raw, bool) or not re.fullmatch(r"[0-9]+", str(raw)) or int(raw) < 1:
            raise ValueError("invalid visitor rate-limit configuration")
        return int(raw)

    def create(self, source: str) -> tuple[str, str, str]:
        token = secrets.token_urlsafe(48)
        session_id = "guest." + secrets.token_urlsafe(18).replace("-", "_")
        expires = _iso(_now() + SESSION_TTL)
        with self._lock, self.auth.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            source_hash = self.auth._fingerprint("visitor_source", source)
            limit = connection.execute(
                "SELECT window_started_at,attempts FROM visitor_create_limits WHERE source_hash=?",
                (source_hash,),
            ).fetchone()
            now = _now()
            if limit is not None and now - datetime.fromisoformat(limit["window_started_at"].replace("Z", "+00:00")) < timedelta(minutes=5):
                if limit["attempts"] >= 10:
                    connection.rollback()
                    raise VisitorError("rate_limited")
                window, attempts = limit["window_started_at"], limit["attempts"] + 1
            else:
                window, attempts = _iso(now), 1
            connection.execute(
                "INSERT INTO visitor_create_limits VALUES (?,?,?) ON CONFLICT(source_hash) DO UPDATE SET window_started_at=excluded.window_started_at,attempts=excluded.attempts",
                (source_hash, window, attempts),
            )
            connection.execute(
                "INSERT INTO visitor_sessions(session_id,token_hash,expires_at) VALUES (?,?,?)",
                (session_id, _digest(token), expires),
            )
            connection.commit()
        return session_id, token, expires

    def owned(self, session_id: str, token: str | None) -> sqlite3.Row:
        if not token:
            raise VisitorError("authentication_required")
        with self._lock, self.auth.store._connect() as connection:
            row = connection.execute(
                "SELECT * FROM visitor_sessions WHERE token_hash=?", (_digest(token),)
            ).fetchone()
        if row is None or row["deleted_at"] is not None or _now() >= datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00")):
            raise VisitorError("authentication_required")
        if row["session_id"] != session_id:
            raise VisitorError("resource_not_found")
        return row

    def delete(self, session_id: str, token: str | None) -> int:
        self.owned(session_id, token)
        with self._lock, self.auth.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE visitor_sessions SET deleted_at=?,state_revision=state_revision+1 WHERE session_id=? AND token_hash=? AND deleted_at IS NULL",
                (_iso(_now()), session_id, _digest(token or "")),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise VisitorError("state_conflict")
            revision = connection.execute("SELECT state_revision FROM visitor_sessions WHERE session_id=?", (session_id,)).fetchone()[0]
            connection.commit()
        return int(revision)

    def events(self, session_id: str, token: str | None) -> list[dict[str, Any]]:
        self.owned(session_id, token)
        with self._lock, self.auth.store._connect() as connection:
            rows = connection.execute(
                "SELECT status,route,revision FROM visitor_operations WHERE session_id=? ORDER BY revision",
                (session_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def require_operation(self, row: sqlite3.Row, *, revision: int, turn_id: int, operation_id: str) -> None:
        with self._lock, self.auth.store._connect() as connection:
            if connection.execute(
                "SELECT 1 FROM visitor_operations WHERE session_id=? AND operation_id_hash=?",
                (row["session_id"], _digest(operation_id)),
            ).fetchone():
                raise VisitorError("operation_replayed")
        if int(row["state_revision"]) != revision or int(row["turn_id"]) != turn_id:
            raise VisitorError("state_conflict")

    def reserve_operation(self, session_id: str, source: str) -> None:
        """Count accepted attempts, including downstream failures, before any agent work.

        Invalid envelopes/replays are rejected before this reservation. A 429
        changes neither limiter state nor visitor/agent state.
        """
        now = _now()
        subjects = (("session", self.auth._fingerprint("visitor_session_limit", session_id)),
                    ("source", self.auth._fingerprint("visitor_source_limit", source)))
        with self._lock, self.auth.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updates = []
            for dimension, subject_hash in subjects:
                row = connection.execute(
                    "SELECT window_started_at,attempts FROM visitor_operation_limits "
                    "WHERE dimension=? AND subject_hash=?", (dimension, subject_hash),
                ).fetchone()
                if row is not None and now - datetime.fromisoformat(
                    row["window_started_at"].replace("Z", "+00:00")
                ) < self.operation_window:
                    if row["attempts"] >= self.operation_limit:
                        connection.rollback()
                        raise VisitorError("rate_limited")
                    window, attempts = row["window_started_at"], row["attempts"] + 1
                else:
                    window, attempts = _iso(now), 1
                updates.append((dimension, subject_hash, window, attempts))
            connection.executemany(
                "INSERT INTO visitor_operation_limits VALUES (?,?,?,?) "
                "ON CONFLICT(dimension,subject_hash) DO UPDATE SET "
                "window_started_at=excluded.window_started_at,attempts=excluded.attempts",
                updates,
            )
            connection.commit()

    def commit_operation(self, session_id: str, token: str, operation_id: str,
                         *, revision: int, turn_id: int, status: str, route: str) -> int:
        with self._lock, self.auth.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE visitor_sessions SET state_revision=state_revision+1,turn_id=turn_id+1 "
                "WHERE session_id=? AND token_hash=? AND state_revision=? AND turn_id=? AND deleted_at IS NULL",
                (session_id, _digest(token), revision, turn_id),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise VisitorError("state_conflict")
            try:
                connection.execute(
                    "INSERT INTO visitor_operations VALUES (?,?,?,?,?)",
                    (session_id, _digest(operation_id), status, route, revision + 1),
                )
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise VisitorError("operation_replayed") from exc
            connection.commit()
        return revision + 1


class VisitorApplication:
    def __init__(self, store: VisitorStore, agent: AgentPort | None = None) -> None:
        self.store = store
        self.agent = agent
        self._lock = threading.RLock()
        self._validator = OnlineContractValidator()

    @staticmethod
    def _json(status: int, body: Mapping[str, Any], *, cookie: str | None = None) -> AuthHttpResponse:
        headers = {"Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store",
                   "X-Content-Type-Options": "nosniff"}
        if cookie is not None:
            headers["Set-Cookie"] = cookie
        return AuthHttpResponse(status, headers, json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8"))

    @classmethod
    def _error(cls, code: str, request_id: str) -> AuthHttpResponse:
        exc = VisitorError(code)
        return cls._json(exc.http_status, {"api_version": API_VERSION, "contract_version": CONTRACT_VERSION,
                                           "request_id": request_id,
                                           "error": {"code": exc.code, "message": exc.safe_message,
                                                     "retryable": exc.http_status in (429, 503)}})

    @staticmethod
    def _request(body: bytes, content_type: str, keys: set[str], schema_name: str) -> dict[str, Any]:
        if len(body) > MAX_JSON_BYTES or content_type.split(";", 1)[0].strip().casefold() != "application/json":
            raise VisitorError("invalid_request")
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise VisitorError("invalid_request") from exc
        if not isinstance(value, dict) or set(value) != keys:
            raise VisitorError("invalid_request")
        if not _REQUEST_VALIDATORS[schema_name].is_valid(value):
            raise VisitorError("invalid_request")
        if value.get("api_version") != API_VERSION or value.get("contract_version") != CONTRACT_VERSION:
            raise VisitorError("invalid_request")
        if not isinstance(value.get("request_id"), str) or not _ID.fullmatch(value["request_id"]):
            raise VisitorError("invalid_request")
        return value

    def handle(self, method: str, path: str, *, request_id: str, body: bytes = b"{}",
               content_type: str = "application/json", token: str | None = None,
               source: str = "local") -> AuthHttpResponse:
        try:
            if method == "POST" and path == "/api/v3/sessions":
                data = self._request(body, content_type, {"api_version", "contract_version", "request_id"},
                                     "session_create_request")
                sid, raw, expires = self.store.create(source)
                return self._json(200, {"api_version": API_VERSION, "contract_version": CONTRACT_VERSION,
                                        "request_id": data["request_id"], "session_id": sid,
                                        "state_revision": 0, "expires_at": expires},
                                  cookie=f"{COOKIE_NAME}={raw}; Path=/api/v3; HttpOnly; SameSite=Strict; Max-Age=86400")
            if path.startswith("/api/v3/sessions/"):
                suffix = path.removeprefix("/api/v3/sessions/")
                event_route = suffix.endswith("/events")
                sid = suffix[:-7] if event_route else suffix
                if not _ID.fullmatch(sid):
                    raise VisitorError("resource_not_found")
                row = self.store.owned(sid, token)
                if event_route and method == "GET":
                    return self._json(200, {"api_version": API_VERSION, "contract_version": CONTRACT_VERSION,
                                            "request_id": request_id, "status": "ok",
                                            "summary": {"events": self.store.events(sid, token)}})
                if not event_route and method == "GET":
                    return self._json(200, {"api_version": API_VERSION, "contract_version": CONTRACT_VERSION,
                                            "request_id": request_id, "session_id": sid,
                                            "state_revision": row["state_revision"], "expires_at": row["expires_at"]})
                if not event_route and method == "DELETE":
                    revision = self.store.delete(sid, token)
                    return self._json(200, {"api_version": API_VERSION, "contract_version": CONTRACT_VERSION,
                                            "request_id": request_id, "status": "deleted", "resource_revision": revision},
                                      cookie=f"{COOKIE_NAME}=; Path=/api/v3; HttpOnly; SameSite=Strict; Max-Age=0")
                raise VisitorError("resource_not_found")
            if method == "POST" and path in {"/api/v3/query", "/api/v3/clarifications"}:
                clarification = path.endswith("clarifications")
                keys = ({"api_version", "contract_version", "answer_id", "session_id", "request_id",
                         "turn_id", "state_revision", "question_id", "slot_id", "values", "evidence_refs"}
                        if clarification else {"api_version", "contract_version", "session_id", "request_id",
                                               "turn_id", "state_revision", "question"})
                data = self._request(body, content_type, keys,
                                     "clarification_request" if clarification else "query_request")
                sid = data["session_id"]
                if not isinstance(sid, str) or not _ID.fullmatch(sid):
                    raise VisitorError("invalid_request")
                if any(isinstance(data[key], bool) or not isinstance(data[key], int) or data[key] < 0
                       for key in ("turn_id", "state_revision")):
                    raise VisitorError("invalid_request")
                if clarification:
                    for key in ("answer_id", "question_id", "slot_id"):
                        if not isinstance(data[key], str) or not _ID.fullmatch(data[key]):
                            raise VisitorError("invalid_request")
                    if not isinstance(data["values"], list) or len(data["values"]) != 1 or not isinstance(data["evidence_refs"], list) or len(data["evidence_refs"]) != 1 or not isinstance(data["evidence_refs"][0], str) or not _ID.fullmatch(data["evidence_refs"][0]):
                        raise VisitorError("invalid_request")
                elif not isinstance(data["question"], str) or not (1 <= len(data["question"]) <= 2000):
                    raise VisitorError("invalid_request")
                operation_id = data["answer_id"] if clarification else data["request_id"]
                with self._lock:
                    row = self.store.owned(sid, token)
                    self.store.require_operation(row, revision=data["state_revision"],
                                                 turn_id=data["turn_id"], operation_id=operation_id)
                    if self.agent is None:
                        raise VisitorError("service_unavailable")
                    self.store.reserve_operation(sid, source)
                    try:
                        # The adapter must preserve its state until validation and
                        # the persistent visitor turn have both succeeded.
                        with _prepared_agent(self.agent, data) as (submitted, accept):
                            result = dict(submitted)
                            if (result.get("status") not in {"success", "clarification_required", "refused", "error"}
                                    or result.get("route") not in {"sql", "rag", "clarification", "unknown"}
                                    or result.get("schema_version") != "2.0.0"
                                    or result.get("session_id") != sid
                                    or result.get("request_id") != data["request_id"]):
                                raise VisitorError("service_unavailable")
                            sql = result.get("sql")
                            sql_data = sql if isinstance(sql, dict) else {}
                            attempts = sql_data.get("execution_attempts", 0)
                            if isinstance(attempts, bool) or not isinstance(attempts, int):
                                raise VisitorError("service_unavailable")
                            self._validator.validate_response(
                                result,
                                safety_gate_approved=sql_data.get("safety_status") == "approved",
                                readonly_execution_attempts=attempts,
                            )
                            revision = self.store.commit_operation(
                                sid, token or "", operation_id, revision=data["state_revision"],
                                turn_id=data["turn_id"], status=result["status"], route=result["route"],
                            )
                            accept()
                    except (VisitorError, sqlite3.Error):
                        raise
                    except Exception as exc:
                        raise VisitorError("service_unavailable") from exc
                return self._json(200, {"api_version": API_VERSION, "contract_version": CONTRACT_VERSION,
                                        "request_id": data["request_id"], "session_id": sid,
                                        "state_revision": revision, "status": result["status"],
                                        "payload": result, "error": result.get("error") if result["status"] == "error" else None})
            raise VisitorError("resource_not_found")
        except VisitorError as exc:
            return self._error(exc.code, request_id)
        except sqlite3.Error:
            return self._error("service_unavailable", request_id)


__all__ = ["AgentPort", "COOKIE_NAME", "VisitorApplication", "VisitorError", "VisitorStore"]
