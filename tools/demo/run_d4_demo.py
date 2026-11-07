"""Run the D4-07 public offline demo on a loopback address only."""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

from backend.api.d4_demo import D4DemoApplication, MAX_REQUEST_BYTES  # noqa: E402


LOOPBACK_HOST = "127.0.0.1"


class _Handler(BaseHTTPRequestHandler):
    app: D4DemoApplication
    server_version = "D4Demo/1.0"
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
                response = self.app.handle(
                    method,
                    self.path,
                    body=b"x" * (MAX_REQUEST_BYTES + 1),
                    content_type=self.headers.get("Content-Type"),
                )
                self._send(response)
                return
            body = self.rfile.read(content_length)
        response = self.app.handle(
            method,
            self.path,
            body=body,
            content_type=self.headers.get("Content-Type"),
        )
        self._send(response)

    def _send(self, response) -> None:
        self.send_response(response.status)
        for key, value in response.headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(response.body)))
        self.end_headers()
        self.wfile.write(response.body)

    def log_message(self, format: str, *args) -> None:
        del format, args


def _build_server(
    host: str, port: int, app: D4DemoApplication | None = None
) -> ThreadingHTTPServer:
    if host != LOOPBACK_HOST:
        raise ValueError("host must be the IPv4 loopback address")
    _Handler.app = app or D4DemoApplication()
    return ThreadingHTTPServer((host, port), _Handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行 D4-07 本机离线演示")
    parser.add_argument("--host", choices=(LOOPBACK_HOST,), default=LOOPBACK_HOST)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    if not 1024 <= args.port <= 65535:
        parser.error("port must be between 1024 and 65535")
    server = _build_server(args.host, args.port)
    print(f"D4-07 离线演示：http://{args.host}:{args.port}")
    print("按 Ctrl+C 停止；服务不会调用模型 API。")
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
