"""Run the D4-07 B-side online SQL API on IPv4 loopback only.

This entry point is intentionally separate from ordinary offline tests.  It
refuses to construct the approved provider unless the caller supplies the
exact real-API confirmation phrase.  Credentials remain external and are
loaded lazily by the existing provider only when a proposal call is reached.
"""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
from typing import Any, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

from backend.agent.loop_guard import LoopPolicy  # noqa: E402
from backend.api.online_sql_service import (  # noqa: E402
    MAX_REQUEST_BYTES,
    OnlineSqlApplication,
    OnlineSqlService,
)
from backend.llm import (  # noqa: E402
    BailianConfig,
    BailianProvider,
    ExternalFileCredentialProvider,
    LLMGateway,
    UsageLedger,
)


LOOPBACK_HOST = "127.0.0.1"
CONFIRMATION_PHRASE = "D4-07-online-qwen3.7-flash"
DEFAULT_PROVIDER_CONFIG = (
    PROJECT_ROOT / "configs" / "providers" / "bailian-qwen3.7-flash.json"
)
DEFAULT_LOOP_POLICY = (
    PROJECT_ROOT / "configs" / "policies" / "agent-loop-limits.v1.json"
)


class OnlineServerSafetyError(RuntimeError):
    """Raised before provider construction when online use is not authorized."""


class _Handler(BaseHTTPRequestHandler):
    app: OnlineSqlApplication
    server_version = "D4OnlineSql/1.0"
    sys_version = ""

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch("PUT")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        body = b""
        if method == "POST":
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                content_length = MAX_REQUEST_BYTES + 1
            if content_length < 0 or content_length > MAX_REQUEST_BYTES:
                body = b"x" * (MAX_REQUEST_BYTES + 1)
            else:
                body = self.rfile.read(content_length)
        response = self.app.handle(
            method,
            self.path,
            content_type=self.headers.get("Content-Type", ""),
            body=body,
        )
        self.send_response(response.status)
        for key, value in response.headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(response.body)))
        self.end_headers()
        self.wfile.write(response.body)

    def log_message(self, format: str, *args) -> None:
        del format, args


def _load_gateway(
    provider_path: Path = DEFAULT_PROVIDER_CONFIG,
    policy_path: Path = DEFAULT_LOOP_POLICY,
) -> LLMGateway:
    raw = json.loads(provider_path.read_text(encoding="utf-8"))
    credential = raw.get("credential")
    if not isinstance(credential, Mapping):
        raise OnlineServerSafetyError("provider credential configuration is missing")
    config = BailianConfig.from_file(provider_path)
    credential_provider = ExternalFileCredentialProvider.from_config(credential)
    ledger = UsageLedger()
    provider = BailianProvider(
        config,
        credential_provider,
        usage_ledger=ledger,
    )
    return LLMGateway(
        provider,
        LoopPolicy.from_file(policy_path),
        usage_ledger=ledger,
    )


def _build_server(
    host: str,
    port: int,
    app: OnlineSqlApplication,
) -> ThreadingHTTPServer:
    if host != LOOPBACK_HOST:
        raise ValueError("host must be the IPv4 loopback address")
    if not isinstance(app, OnlineSqlApplication):
        raise TypeError("app must be OnlineSqlApplication")
    _Handler.app = app
    return ThreadingHTTPServer((host, port), _Handler)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="运行 D4-07 B 侧在线单表 SQL API（需要单独人工授权）"
    )
    parser.add_argument("--host", choices=(LOOPBACK_HOST,), default=LOOPBACK_HOST)
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--table", default="Artist")
    parser.add_argument(
        "--confirm-real-api",
        required=True,
        metavar="PHRASE",
        help=f"必须精确填写 {CONFIRMATION_PHRASE!r}",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.confirm_real_api != CONFIRMATION_PHRASE:
        raise OnlineServerSafetyError("real API confirmation phrase did not match")
    if not 1024 <= args.port <= 65535:
        raise OnlineServerSafetyError("port must be between 1024 and 65535")
    gateway = _load_gateway()
    app = OnlineSqlApplication(
        OnlineSqlService(gateway, table_name=args.table)
    )
    server = _build_server(args.host, args.port, app)
    print(f"D4-07 在线 SQL API：http://{args.host}:{args.port}")
    print("仅允许 qwen3.7-flash；按 Ctrl+C 停止。")
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CONFIRMATION_PHRASE",
    "LOOPBACK_HOST",
    "OnlineServerSafetyError",
    "_build_server",
    "build_parser",
    "main",
]
