"""D5-05 local single-process product adapter; no implicit model traffic."""

from __future__ import annotations

import argparse
from email import policy
from email.parser import BytesParser
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
from urllib.parse import urlsplit
import uuid

from ..api.d4_demo import D4DemoApplication
from .auth import AuthApplication, AuthError, AuthHttpResponse, AuthService, SqliteAuthStore
from .document_store import DocumentApplication, DocumentService, MAX_FILE_BYTES, SqliteDocumentStore
from .secret_store import SecretApplication, SecretService, SqliteSecretStore, load_master_key
from .visitor import AgentPort, COOKIE_NAME as VISITOR_COOKIE, VisitorApplication, VisitorStore
from ..llm.usage import UsageLedger


MAX_REQUEST_BYTES = MAX_FILE_BYTES + 64 * 1024
ROOT = Path(__file__).resolve().parents[3]
STATIC = ROOT / "src" / "frontend" / "d5-console"
STATIC_FILES = {
    "/": STATIC / "index.html", "/index.html": STATIC / "index.html",
    **{f"/{name}": STATIC / name for name in (
        "app.js", "admin.js", "guest.js", "dom.js", "api-client.js", "styles.css",
        "mock/index.js", "live-transport.js", "citation-validation.js",
    )},
    "/d4-console/rag-manifest.js": ROOT / "src" / "frontend" / "d4-console" / "rag-manifest.js",
    "/d4-console/mock/index.js": ROOT / "src" / "frontend" / "d4-console" / "mock" / "index.js",
}


def _index_payload(payload: bytes, mode: str) -> bytes:
    """Enable the live transport only on an explicitly online product server.

    The checked replacements fail closed if C changes the HTML entry point: an
    online page must never silently fall back to mock or claim zero model calls.
    """
    if mode != "online":
        return payload
    html = payload.decode("utf-8")
    replacements = (
        ('<title>问数智能体控制台</title>',
         '<title>问数智能体控制台 · 在线服务</title>'),
        ('演示模式 · 使用示例数据', '在线模式 · 连接真实服务'),
        ('<script type="module" src="./app.js"></script>',
         '<script>window.D5_LIVE_API = true;</script>\n    '
         '<script type="module" src="./app.js"></script>'),
    )
    for old, new in replacements:
        if html.count(old) != 1:
            raise ProductStartupError("service_unavailable")
        html = html.replace(old, new, 1)
    start = html.find('<footer class="footer">')
    paragraph_start = html.find('<p>', start)
    paragraph_end = html.find('</p>', paragraph_start)
    if start < 0 or paragraph_start < 0 or paragraph_end < 0:
        raise ProductStartupError("service_unavailable")
    paragraph = ('<p>当前页面连接本机在线服务；提交问题后可能调用管理员配置的模型 '
                 '服务并产生费用。管理员页只显示 Key 的配置状态，'
                 '不预填、不回显、不提供“显示已保存 Key”。</p>')
    return (html[:paragraph_start] + paragraph + html[paragraph_end + len('</p>'):]).encode("utf-8")


class ProductStartupError(RuntimeError):
    """Stable startup failure; never embeds paths, keys or database contents."""

    def __init__(self, code: str = "service_unavailable") -> None:
        self.code = code
        super().__init__(code)


def parse_document_multipart(content_type: str, body: bytes) -> tuple[bytes, bytes]:
    """Return strict JSON metadata and PDF bytes; reject ambiguous multipart."""
    match = re.fullmatch(r"multipart/form-data;\s*boundary=([A-Za-z0-9_-]{1,70})", content_type)
    if not match or len(body) > MAX_REQUEST_BYTES:
        raise ValueError("invalid_request")
    boundary = match.group(1).encode("ascii")
    if not body.startswith(b"--" + boundary + b"\r\n") or not body.rstrip(b"\r\n").endswith(b"--" + boundary + b"--"):
        raise ValueError("invalid_request")
    header = b"Content-Type: " + content_type.encode("ascii") + b"\r\nMIME-Version: 1.0\r\n\r\n"
    message = BytesParser(policy=policy.default).parsebytes(header + body)
    if not message.is_multipart() or message.defects:
        raise ValueError("invalid_request")
    parts: dict[str, bytes] = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if name not in {"metadata", "file"} or name in parts or part.defects:
            raise ValueError("invalid_request")
        if part.get("Content-Transfer-Encoding"):
            raise ValueError("invalid_request")
        if name == "metadata" and part.get_content_type() != "application/json":
            raise ValueError("invalid_request")
        if name == "file" and part.get_content_type() != "application/pdf":
            raise ValueError("invalid_request")
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes):
            raise ValueError("invalid_request")
        parts[name] = payload
    if set(parts) != {"metadata", "file"} or len(parts["metadata"]) > 4096:
        raise ValueError("invalid_request")
    if len(parts["file"]) > MAX_FILE_BYTES:
        raise ValueError("upload_too_large")
    return parts["metadata"], parts["file"]


def _database_ok(path: Path) -> bool:
    try:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=rw", uri=True)
        try:
            return connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        finally:
            connection.close()
    except sqlite3.Error:
        return False


class ProductRuntime:
    def __init__(self, instance: str | Path, *, mode: str = "offline", master_key: bytes | None = None,
                 agent_port: AgentPort | None = None, bind_host: str = "127.0.0.1",
                 deployment_mode: str = "local") -> None:
        if mode not in {"offline", "online"}:
            raise ProductStartupError("invalid_request")
        # online 模式不再因 agent_port 为空而 fail-closed：C 侧已接线 RAG 优先
        # agent（见下方 _build_online_agent）。仍允许外部注入 agent_port 供测试。
        self.mode = mode
        self.bind_host = bind_host
        self.deployment_mode = deployment_mode
        self.instance = Path(instance).expanduser().resolve()
        if self.instance == ROOT or ROOT in self.instance.parents:
            raise ProductStartupError("invalid_instance_directory")
        try:
            self.instance.mkdir(parents=True, exist_ok=True)
            if not self.instance.is_dir():
                raise OSError()
            probe = self.instance / ".write-check"
            probe.write_bytes(b"")
            probe.unlink()
            if master_key is None:
                secret_path = os.environ.get("ICT_MASTER_KEY_FILE")
                if secret_path:
                    resolved_secret = Path(secret_path).expanduser().resolve()
                    if resolved_secret == ROOT or ROOT in resolved_secret.parents:
                        raise ProductStartupError("service_unavailable")
                master_key = load_master_key(docker_secret_path=secret_path)
            auth_db = self.instance / "metadata.sqlite3"
            secret_db = self.instance / "secrets.sqlite3"
            for path in (auth_db, secret_db):
                if path.exists() and not _database_ok(path):
                    raise ProductStartupError("storage_corrupt")
            self.auth = AuthService(SqliteAuthStore(auth_db))
            self.secret_store = SqliteSecretStore(secret_db, master_key=master_key)
            self.secret_store.startup_check()
            from .model_settings import ModelSettings
            self.model_settings = ModelSettings(self.secret_store)
            self.model_settings.status()  # Authenticate persisted settings at startup.
            bootstrap = json.loads((ROOT / "configs" / "contracts" / "d5" /
                                    "admin-bootstrap.v2.json").read_text(encoding="utf-8"))
            identity = bootstrap["initial_credentials"]
            if (bootstrap["contract_version"] != "2.0.0" or identity["username"] != "admin"
                    or identity["must_change_password"] is not False):
                raise ProductStartupError("service_unavailable")
            self.auth.initialize_default_admin_once(identity["initial_password"])
            self.documents = SqliteDocumentStore(self.auth, self.instance / "documents")
            self._usage_ledger: UsageLedger | None = None
            if mode == "online" and agent_port is None:
                agent_port = self._build_online_agent()
            self.visitor_app = VisitorApplication(VisitorStore(self.auth), agent=agent_port)
            self.auth_app = AuthApplication(self.auth)
            self.document_app = DocumentApplication(self.auth, DocumentService(self.documents))
            self.secret_app = SecretApplication(
                self.auth, SecretService(self.secret_store, rate_key=master_key),
                connectivity_probe=self._disabled_probe,
            )
            self._offline_demo: D4DemoApplication | None = None
            self._offline_demo_lock = threading.RLock()
        except ProductStartupError:
            raise
        except (OSError, sqlite3.Error, ValueError, RuntimeError) as exc:
            raise ProductStartupError() from exc

    def _model_settings_request(self, method, path, common):
        from .secret_store import SecretAudit, SecretStoreError
        from .model_settings import probe_settings
        from ..llm.errors import ProviderError
        from ..llm.credentials import CredentialError
        rid=common['request_id']
        try:
            principal=self.auth.guard_admin_path(method,path,common['cookies'].get('ict_admin_session'),
                csrf_token=common['headers'].get('x-csrf-token'),request_id=rid)
            audit=SecretAudit(self.auth,rid,principal.session_id,common['source'])
            if method=='GET' and path=='/api/v3/admin/model-settings':
                result=self.model_settings.status()
            else:
                if len(common['body'])>8192 or common['content_type'].split(';')[0]!='application/json':
                    return self._error(rid,'invalid_request',400)
                data=json.loads(common['body'])
                if not isinstance(data,dict): return self._error(rid,'invalid_request',400)
                if method=='PUT' and path=='/api/v3/admin/model-settings' and set(data)=={'config','api_key','expected_revision'}:
                    result=self.model_settings.save(data['config'],data['api_key'],data['expected_revision'],audit=audit)
                elif method=='DELETE' and path=='/api/v3/admin/model-settings' and set(data)=={'expected_revision'}:
                    result=self.model_settings.revoke(data['expected_revision'],audit=audit)
                elif (method=='POST' and path=='/api/v3/admin/model-settings/test'
                      and set(data)=={'explicit_confirmation'} and data['explicit_confirmation'] is True):
                    if self.mode!='online':
                        return self._error(rid,'online_mode_required',409,message='离线模式不进行模型连通测试；请明确启动 online 模式。')
                    import hashlib
                    self.secret_store.consume_connectivity_attempt(hashlib.sha256(common['source'].encode()).hexdigest(),audit=audit)
                    try:
                        result=probe_settings(self.model_settings)
                    except Exception:
                        self.secret_store.record_connectivity_outcome(audit,succeeded=False)
                        raise
                    self.secret_store.record_connectivity_outcome(audit,succeeded=True,
                        usage_counts={'requests':result['calls'],'input_tokens':result['input_tokens'],
                                      'output_tokens':result['output_tokens']})
                else: return self._error(rid,'invalid_request',400)
            return AuthHttpResponse(200,{'Content-Type':'application/json'},json.dumps(result).encode())
        except AuthError as exc: return self._error(rid,exc.code,exc.http_status)
        except (ValueError,TypeError,CredentialError): return self._error(rid,'invalid_request',400)
        except ProviderError as exc:
            return self._error(rid,'model_'+exc.code.value,400,
                message={'configuration_error':'模型配置无效或单价未知，请检查配置。',
                         'authentication_error':'模型服务认证失败，请检查 Key。',
                         'authorization_error':'模型服务拒绝授权。',
                         'network_error':'模型连接失败，请检查地址与网络。',
                         'model_mismatch':'服务返回的模型与配置不一致。'}.get(exc.code.value,'模型测试未通过，请检查协议、限额及服务状态。'))
        except SecretStoreError as exc: return self._error(rid,exc.code,exc.http_status)
        except (OSError,sqlite3.Error,RuntimeError): return self._error(rid,'service_unavailable',503)

    @staticmethod
    def _disabled_probe(*_args, **_kwargs):
        raise ProductStartupError("service_unavailable")

    def _build_online_agent(self) -> AgentPort:
        """构造 online 访客 agent：RAG 优先、无证据回退 SQL（C 侧接线）。

        SQL 路径仍由 ``build_approved_sql_adapter`` 基于 SecretStore credential
        构造，安全门（D4-02 SafetyGate / ReadonlyExecutor）不变；本方法仅构造、
        不调用模型、不读取真实 Key。
        """
        from .visitor_rag import build_rag_first_agent
        from .visitor_sql import build_approved_sql_adapter

        sql_adapter = build_approved_sql_adapter(self.secret_store)
        self._usage_ledger = sql_adapter.usage_ledger
        return build_rag_first_agent(self.documents, sql_adapter)

    def _admin_summary(self) -> dict:
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with self.auth.store._lock, self.auth.store._connect() as connection:
            active_sessions = int(connection.execute(
                "SELECT COUNT(*) FROM visitor_sessions WHERE deleted_at IS NULL AND expires_at > ?", (now,)
            ).fetchone()[0])
        records = self.documents.list_records()
        model_status = self.model_settings.status()
        return {
            "mode": self.mode,
            "api_version": "3.0", "contract_version": "1.0.0",
            "sessions": {"active": active_sessions},
            "documents": {
                "total": len(records),
                "published": sum(item.state == "published" for item in records),
                "ids": [item.document_id for item in records],
            },
            "model_boundary": {"provider": self.model_settings.status()['config']['provider_id'],
                               "model": self.model_settings.status()['config']['model'],
                               "fallback": False, "dynamic_routing": False},
            "secret": {"configured": model_status['credential_configured'],
                       "source": "secret_store" if model_status['credential_configured'] else "not_configured"},
        }

    def _admin_usage(self) -> dict:
        with self.auth.store._lock, self.auth.store._connect() as connection:
            sessions_created = int(connection.execute("SELECT COUNT(*) FROM visitor_sessions").fetchone()[0])
        summary = {"window": "mixed", "model_usage_window": "process_lifetime",
                   "session_document_window": "instance_lifetime",
                   "sessions_created": sessions_created,
                   "documents_total": len(self.documents.list_records())}
        if self.mode == "offline":
            summary.update(model_calls=0, input_tokens=0, output_tokens=0, estimated_cost_cny=0)
        elif self._usage_ledger is not None:
            usage = self._usage_ledger.summary()
            summary.update(model_calls=usage["calls"], input_tokens=usage["input_tokens"],
                           output_tokens=usage["output_tokens"],
                           estimated_cost_cny=usage["estimated_cost_cny"])
        else:
            # Injected agents have no authoritative ledger. Never claim zero usage.
            summary["usage_available"] = False
        return summary

    def ready(self) -> bool:
        return (
            _database_ok(self.instance / "metadata.sqlite3")
            and _database_ok(self.instance / "secrets.sqlite3")
            and (not self.auth.default_password_active() or
                 (self.bind_host == "127.0.0.1" and self.deployment_mode == "local"))
            and self._secret_ready()
            and self._documents_ready()
        )

    def _documents_ready(self) -> bool:
        try:
            root = self.instance / "documents"
            for directory in (root, root / "quarantine", root / "index"):
                if not directory.is_dir() or directory.is_symlink():
                    return False
            active = self.documents.get_active_index()
            if active is not None:
                if not re.fullmatch(r"idx\.[0-9a-f]{12}", active):
                    return False
                marker = root / "index" / (active + ".json")
                if not marker.is_file() or marker.is_symlink():
                    return False
            return True
        except (OSError, sqlite3.Error, ValueError):
            return False

    def _secret_ready(self) -> bool:
        try:
            self.secret_store.startup_check()
            self.model_settings.status()
            return True
        except (OSError, sqlite3.Error, RuntimeError):
            return False

    def dispatch(
        self, method: str, path: str, *, body: bytes = b"{}", content_type: str = "application/json",
        request_id: str | None = None, cookie_header: str = "", csrf: str = "", source: str = "local",
    ) -> AuthHttpResponse:
        rid = request_id or "req_" + uuid.uuid4().hex
        if path == "/health/live" and method == "GET":
            return self._json(200, {"status": "live"})
        if path == "/health/ready" and method == "GET":
            ready = self.ready()
            return self._json(200 if ready else 503, {"status": "ready" if ready else "unavailable"})
        if not self.ready():
            return self._error(rid, "service_unavailable", 503)
        if self.mode == "offline" and (path == "/demo" or path.startswith("/assets/") or
                                       path.startswith("/api/v1/")):
            demo_path = "/" if path == "/demo" else path
            try:
                with self._offline_demo_lock:
                    if self._offline_demo is None:
                        self._offline_demo = D4DemoApplication()
                    response = self._offline_demo.handle(method, demo_path, body=body,
                                                         content_type=content_type)
            except (OSError, ValueError, RuntimeError):
                return self._error(rid, "service_unavailable", 503)
            return AuthHttpResponse(response.status, response.headers, response.body)
        parsed_cookie = cookies.SimpleCookie()
        try:
            parsed_cookie.load(cookie_header)
        except cookies.CookieError:
            return self._error(rid, "invalid_request", 400)
        cookie_values = {key: value.value for key, value in parsed_cookie.items()}
        headers = {"x-csrf-token": csrf}
        common = dict(request_id=rid, content_type=content_type, body=body,
                      cookies=cookie_values, headers=headers, source=source)
        if path.startswith("/api/v3/admin/"):
            try:
                self.auth.guard_admin_path(method, path, cookie_values.get("ict_admin_session"),
                                           csrf_token=csrf, request_id=rid)
            except AuthError as exc:
                return self._error(rid, "authentication_required" if exc.code == "session_invalid" else exc.code,
                                   exc.http_status)
        if path.startswith("/api/v3/auth/") or path in {
            "/api/v3/admin/logout", "/api/v3/admin/sessions/revoke-all", "/api/v3/admin/password"
        }:
            return self.auth_app.handle(method, path, **common)
        if path.startswith("/api/v3/admin/secrets/"):
            return self.secret_app.handle(method, path, **common)
        if path.startswith("/api/v3/admin/model-settings"):
            return self._model_settings_request(method, path, common)
        if path.startswith("/api/v3/admin/documents"):
            if path == "/api/v3/admin/documents" and method == "POST" and content_type.startswith("multipart/"):
                try:
                    metadata, file_bytes = parse_document_multipart(content_type, body)
                except ValueError as exc:
                    code = str(exc)
                    return self._error(rid, code, 413 if code == "upload_too_large" else 400)
                common["content_type"] = "application/json"
                common["body"] = metadata
                return self.document_app.handle(method, path, file_bytes=file_bytes, **common)
            return self.document_app.handle(method, path, **common)
        if path == "/api/v3/admin/health" and method == "GET":
            try:
                summary = self._admin_summary()
            except (OSError, sqlite3.Error, ValueError, RuntimeError):
                return self._error(rid, "service_unavailable", 503)
            return self._json(200, {"api_version": "3.0", "contract_version": "1.0.0",
                                    "request_id": rid, "status": "ok", "summary": summary})
        if path == "/api/v3/admin/audit-events" and method == "GET":
            try:
                count, events = self.auth.store.audit_summary()
            except (OSError, sqlite3.Error, ValueError, RuntimeError):
                return self._error(rid, "service_unavailable", 503)
            return self._json(200, {"api_version": "3.0", "contract_version": "1.0.0",
                                    "request_id": rid, "status": "ok",
                                    "summary": {"count": count, "retention_days": 90,
                                                "append_only": True, "events": events}})
        if path == "/api/v3/admin/usage-summary" and method == "GET":
            try:
                summary = self._admin_usage()
            except (OSError, sqlite3.Error, ValueError, RuntimeError):
                return self._error(rid, "service_unavailable", 503)
            return self._json(200, {"api_version": "3.0", "contract_version": "1.0.0",
                                    "request_id": rid, "status": "ok", "summary": summary})
        if path == "/api/v3/sessions" or path.startswith("/api/v3/sessions/") or path in {
            "/api/v3/query", "/api/v3/clarifications"
        }:
            return self.visitor_app.handle(method, path, request_id=rid, body=body,
                                           content_type=content_type, token=cookie_values.get(VISITOR_COOKIE),
                                           source=source)
        if path.startswith("/api/v3/"):
            # A stub is intentionally explicit: offline demo is never passed off as an online answer.
            return self._error(rid, "service_unavailable", 503)
        return self._error(rid, "resource_not_found", 404)

    @staticmethod
    def _json(status: int, value: dict) -> AuthHttpResponse:
        return AuthHttpResponse(status, {"Content-Type": "application/json; charset=utf-8",
                                         "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
                                json.dumps(value, ensure_ascii=False).encode("utf-8"))

    @classmethod
    def _error(cls, rid: str, code: str, status: int, *, message: str | None = None) -> AuthHttpResponse:
        return cls._json(status, {"api_version": "3.0", "contract_version": "1.0.0", "request_id": rid,
                                  "error": {"code": code, "message": message or ("服务暂时不可用。" if status == 503 else "请求无效。"),
                                            "retryable": status == 503}})


class UnreadyRuntime:
    """Health-only fail-closed surface for missing config or damaged storage."""

    @staticmethod
    def dispatch(method: str, path: str, **_kwargs) -> AuthHttpResponse:
        if method == "GET" and path == "/health/live":
            return ProductRuntime._json(200, {"status": "live"})
        if method == "GET" and path == "/health/ready":
            return ProductRuntime._json(503, {"status": "unavailable"})
        return ProductRuntime._error("req_" + uuid.uuid4().hex, "service_unavailable", 503)


def serve(runtime: ProductRuntime | UnreadyRuntime, host: str = "127.0.0.1", port: int = 8000) -> None:
    if host != "127.0.0.1" and not (host == "0.0.0.0" and os.environ.get("ICT_DOCKER_BIND") == "1"):
        raise ProductStartupError("invalid_bind_address")
    if isinstance(runtime, ProductRuntime):
        # The readiness gate must evaluate the address actually exposed by this server.
        runtime.bind_host = host

    class Handler(BaseHTTPRequestHandler):
        def _handle(self) -> None:
            path = urlsplit(self.path).path
            if isinstance(runtime, ProductRuntime) and runtime.ready() and self.command == "GET" and path in STATIC_FILES and STATIC_FILES[path].is_file():
                name = path
                payload = STATIC_FILES[path].read_bytes()
                if path in {"/", "/index.html"}:
                    try:
                        payload = _index_payload(payload, runtime.mode)
                    except ProductStartupError:
                        response = ProductRuntime._error("req_" + uuid.uuid4().hex, "service_unavailable", 503)
                        self.send_response(response.status)
                        for key, value in response.headers.items():
                            self.send_header(key, value)
                        self.send_header("Content-Length", str(len(response.body)))
                        self.end_headers()
                        self.wfile.write(response.body)
                        return
                self.send_response(200)
                self.send_header("Content-Type", "text/css; charset=utf-8" if name.endswith(".css") else
                                 "text/javascript; charset=utf-8" if name.endswith(".js") else "text/html; charset=utf-8")
                self.send_header("X-Content-Type-Options", "nosniff")
                if path in {"/", "/index.html"}:
                    self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(payload)
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                size = -1
            if size < 0 or size > MAX_REQUEST_BYTES or self.headers.get("Transfer-Encoding"):
                response = ProductRuntime._error("req_" + uuid.uuid4().hex, "invalid_request", 400)
            else:
                body = self.rfile.read(size) if size else b"{}"
                response = runtime.dispatch(self.command, path, body=body,
                    content_type=self.headers.get("Content-Type", "application/json"),
                    request_id=self.headers.get("X-Request-ID"),
                    cookie_header=self.headers.get("Cookie", ""), csrf=self.headers.get("X-CSRF-Token", ""),
                    source=self.client_address[0])
            self.send_response(response.status)
            for key, value in response.headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(response.body)))
            self.end_headers()
            self.wfile.write(response.body)

        do_GET = do_POST = do_PUT = do_DELETE = _handle

        def log_message(self, _format: str, *_args) -> None:
            # URL and headers may contain private values; no access log by default.
            return

    with ThreadingHTTPServer((host, port), Handler) as server:
        server.serve_forever()


def main() -> int:
    parser = argparse.ArgumentParser(description="D5 local product service")
    parser.add_argument("command", choices=("serve", "change-password-local", "check", "admin-exists"))
    parser.add_argument("--instance", required=True)
    parser.add_argument("--mode", choices=("offline", "online"), default="offline")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    try:
        try:
            runtime: ProductRuntime | UnreadyRuntime = ProductRuntime(
                args.instance, mode=args.mode, bind_host=args.host,
                deployment_mode=os.environ.get("ICT_DEPLOYMENT_MODE", "local"),
            )
        except ProductStartupError as exc:
            if args.command != "serve" or exc.code not in {"service_unavailable", "storage_corrupt"}:
                raise
            runtime = UnreadyRuntime()
        if args.command == "check":
            return 0 if runtime.ready() else 1
        if args.command == "admin-exists":
            return 0 if runtime.auth.store.read_state()["username"] else 2
        if args.command == "change-password-local":
            from getpass import getpass
            current = getpass("当前管理员密码: ")
            replacement = getpass("新管理员密码（至少12位）: ")
            session = runtime.auth.login("admin", current, source="local_cli", request_id="local_change")
            runtime.auth.change_password(session.token, session.csrf_token, current, replacement,
                                         request_id="local_change")
            print("管理员密码已修改；其他会话已撤销。")
            return 0
        serve(runtime, host=args.host, port=args.port)
        return 0
    except AuthError as exc:
        print(f"操作失败：{exc.code}")
        return 1
    except ProductStartupError as exc:
        print(f"启动失败：{exc.code}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
