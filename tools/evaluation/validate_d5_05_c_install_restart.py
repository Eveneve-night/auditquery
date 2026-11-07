#!/usr/bin/env python3
"""D5-05-C C 侧前端安装/重启端到端验收探针（自包含、可复跑、诚实分层）。

定位：B 的 D5-05-B 单体服务交付后，本脚本对「运行中的服务」做 C 侧前端可观测面的
验收（静态资源托管、访客首页免登录、multipart 上传格式对齐、鉴权边界探测）。
本脚本**不碰 B 的主启动文件**（server.py / start.ps1 / compose.yaml / Dockerfile），
也不代替 B 侧安全 P1（catch-all 鉴权 / 问答澄清限流 / 澄清 pending 半提交）
与 v2 管理员服务端安全 Gate 的独立验收。

两种模式：
- 默认（离线预检）：不连网，校验 C 前端文件齐全 + B 后端是否交付，输出脱敏报告；
  用于 B 交付前确认探针本身就绪、不空转。
- `--live <base-url>`：连网探针（需 B 服务已在跑），覆盖存活 / 静态资源 / 访客首页 /
  multipart 鉴权边界，可选 `--deep-upload` 从安全终端提示读取密码后走真实上传深验收。

不读取真实 Key、不调用模型。输出脱敏报告到
tests/reports/d5-05-c-install-restart-v1.json。
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import http.cookiejar
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONSOLE_ROOT = PROJECT_ROOT / "src" / "frontend" / "d5-console"
D4_CONSOLE_ROOT = PROJECT_ROOT / "src" / "frontend" / "d4-console"
B_SERVER = PROJECT_ROOT / "src" / "backend" / "product" / "server.py"
REPORT_PATH = PROJECT_ROOT / "tests" / "reports" / "d5-05-c-install-restart-v1.json"

# 静态资源清单：(URL 路径, 本地相对文件, 期望 Content-Type 前缀)。
# 前 8 项是 B 当前白名单；后 3 项是 #78 联调前 B 需补进白名单的路径（C 侧交叉复核 P1-1）。
STATIC_MANIFEST: list[tuple[str, Path, str]] = [
    ("/", CONSOLE_ROOT / "index.html", "text/html"),
    ("/index.html", CONSOLE_ROOT / "index.html", "text/html"),
    ("/app.js", CONSOLE_ROOT / "app.js", "text/javascript"),
    ("/admin.js", CONSOLE_ROOT / "admin.js", "text/javascript"),
    ("/guest.js", CONSOLE_ROOT / "guest.js", "text/javascript"),
    ("/dom.js", CONSOLE_ROOT / "dom.js", "text/javascript"),
    ("/api-client.js", CONSOLE_ROOT / "api-client.js", "text/javascript"),
    ("/styles.css", CONSOLE_ROOT / "styles.css", "text/css"),
    # 待 B 补白名单（#78 联调硬阻塞项）：
    ("/live-transport.js", CONSOLE_ROOT / "live-transport.js", "text/javascript"),
    ("/mock/index.js", CONSOLE_ROOT / "mock" / "index.js", "text/javascript"),
    ("/d4-console/rag-manifest.js", D4_CONSOLE_ROOT / "rag-manifest.js", "text/javascript"),
]

# multipart 对齐 B 的 parse_document_multipart：metadata=application/json、file=application/pdf。
MULTIPART_METADATA_FIELDS = ("api_version", "contract_version", "request_id",
                             "filename", "content_type", "declared_bytes", "content_sha256")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def offline_static_check() -> dict:
    """离线预检：C 前端文件是否齐全 + B 后端是否交付（不连网）。"""
    missing: list[str] = []
    present: list[str] = []
    for _url, local, _ctype in STATIC_MANIFEST:
        (present if local.is_file() else missing).append(str(local.relative_to(PROJECT_ROOT)))
    return {
        "b_server_present": B_SERVER.is_file(),
        "b_server": "src/backend/product/server.py",
        "static_files_present": len(present),
        "static_files_missing": missing,
        "frontend_files_complete": not missing,
    }


def _http(base: str, path: str, *, method: str = "GET", headers: dict | None = None,
          body: bytes | None = None, timeout: int = 5,
          opener=None) -> tuple[int, dict, bytes]:
    """标准库 HTTP 请求，返回 (status, headers_dict, body_bytes)。"""
    url = base.rstrip("/") + path
    request = urllib.request.Request(url, data=body, method=method)
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with (opener.open(request, timeout=timeout) if opener is not None else
              urllib.request.urlopen(request, timeout=timeout)) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()
    except (urllib.error.URLError, TimeoutError, OSError):
        return -1, {}, b""


def _content_type(headers: dict) -> str:
    for key, value in headers.items():
        if key.lower() == "content-type":
            return value.split(";", 1)[0].strip()
    return ""


def _health_live_check(base: str) -> dict:
    status, headers, body = _http(base, "/health/live")
    ok = status == 200
    parsed: dict = {}
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        parsed = {}
    return {
        "ok": ok and parsed.get("status") == "live",
        "http_status": status,
        "body_status": parsed.get("status"),
    }


def _health_ready_check(base: str) -> dict:
    status, _headers, body = _http(base, "/health/ready")
    parsed: dict = {}
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        parsed = {}
    ready = parsed.get("status") == "ready"
    return {
        "ok": (status == 200 and ready),
        "http_status": status,
        "body_status": parsed.get("status"),
        "expect_503_if_unavailable": True,
    }


def _static_check(base: str) -> dict:
    results: dict[str, dict] = {}
    for url, _local, expected_prefix in STATIC_MANIFEST:
        status, headers, body = _http(base, url)
        actual_type = _content_type(headers)
        # 对 / 与 /index.html 允许 text/html；js/css 按前缀判定（浏览器允许 application/javascript）。
        if url in {"/", "/index.html"}:
            type_ok = actual_type.startswith("text/html")
        else:
            type_ok = actual_type.startswith(expected_prefix) or \
                (expected_prefix == "text/javascript" and actual_type.startswith("application/javascript"))
        results[url] = {
            "ok": status == 200 and bool(body) and type_ok,
            "http_status": status,
            "content_type": actual_type,
            "bytes": len(body),
        }
    return results


def _visitor_home_check(base: str) -> dict:
    # 访客首页免登录：GET / 不带任何 Cookie 应返回 200 HTML，且含 C 侧数据源标记。
    status, _headers, body = _http(base, "/")
    text = body.decode("utf-8", errors="replace")
    has_mode_badge = 'id="mode-badge"' in text
    return {
        "ok": status == 200 and has_mode_badge,
        "http_status": status,
        "has_mode_badge": has_mode_badge,
        "no_cookie_sent": True,
    }


def build_multipart(metadata: dict, file_bytes: bytes, boundary: str) -> tuple[bytes, str]:
    """构造对齐 B parse_document_multipart 的 multipart body。"""
    meta_json = json.dumps(metadata, ensure_ascii=False).encode("utf-8")
    b = boundary.encode("ascii")
    body = b""
    body += b"--" + b + b"\r\n"
    body += b'Content-Disposition: form-data; name="metadata"\r\n'
    body += b"Content-Type: application/json\r\n\r\n"
    body += meta_json + b"\r\n"
    body += b"--" + b + b"\r\n"
    body += b'Content-Disposition: form-data; name="file"; filename="probe.pdf"\r\n'
    body += b"Content-Type: application/pdf\r\n\r\n"
    body += file_bytes + b"\r\n"
    body += b"--" + b + b"--\r\n"
    return body, f"multipart/form-data; boundary={boundary}"


def _make_metadata(request_id: str, file_bytes: bytes) -> dict:
    return {
        "api_version": "3.0",
        "contract_version": "1.0.0",
        "request_id": request_id,
        "filename": "probe.pdf",
        "content_type": "application/pdf",
        "declared_bytes": len(file_bytes),
        "content_sha256": _sha256(file_bytes),
    }


def _multipart_auth_boundary_probe(base: str) -> dict:
    """无凭据上传探测：multipart 先鉴权后解析的边界。

    带正确格式的 multipart（metadata=application/json + file=application/pdf）但不带
    登录 Cookie：期望 401/403（鉴权在解析前）。若得到 503 说明 B 的 catch-all 鉴权 P1
    未修；若得到 400/413 说明格式未被接受（含 B 白名单/解析问题）。此探测能同时揭示
    B 的 P1 状态与 C 侧 multipart 格式是否对齐，但不证明完整上传成功。
    """
    fake_pdf = b"%PDF-1.4\n% probe placeholder, not a real document\n%%EOF\n"
    metadata = _make_metadata("req.install.restart.probe", fake_pdf)
    body, content_type = build_multipart(metadata, fake_pdf, "wbprobe0001")
    status, _headers, resp_body = _http(
        base, "/api/v3/admin/documents", method="POST",
        headers={"Content-Type": content_type, "x-csrf-token": ""}, body=body,
    )
    code = ""
    try:
        code = json.loads(resp_body.decode("utf-8")).get("error", {}).get("code", "")
    except (ValueError, UnicodeDecodeError):
        code = ""
    return {
        "ok": status in {401, 403},
        "http_status": status,
        "error_code": code,
        "interpretation": (
            "401/403=鉴权在解析前(正确边界)" if status in {401, 403}
            else "503=B 的 catch-all 鉴权 P1 未修" if status == 503
            else "400/413=multipart 格式未对齐或被拒，需查 metadata/file 类型"
        ),
    }


def _deep_upload_probe(base: str, admin_password: str) -> dict:
    """可选深验收：登录 CookieJar 自动保管 HttpOnly Cookie，仅提取 CSRF。

    契约要点（2026-10-13 联调实证，与 B 的 D5-05-B 逐项对齐）：
    - 登录契约 contract_version=2.0.0，上传 metadata contract_version=1.0.0（两者分开）；
    - 会话令牌由 http.cookiejar 自动保管（HttpOnly，应用代码不接触），不从响应正文读 session_token；
    - 上传必须带 X-Request-ID 头且与 metadata.request_id 一致，否则 400 invalid_request（B 的 _require_upload_meta）。
    """
    username = "admin"
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    login_status, _login_headers, login_body = _http(
        base, "/api/v3/auth/admin/login", method="POST",
        headers={"Content-Type": "application/json"},
        body=json.dumps({"api_version": "3.0", "contract_version": "2.0.0",
                         "request_id": "req.deep.login", "username": username,
                         "password": admin_password}).encode("utf-8"), opener=opener,
    )
    if login_status != 200:
        return {
            "ok": False,
            "skipped_reason": "login_failed",
            "login_http_status": login_status,
            "note": "管理员登录失败；请核对当前密码及服务状态。",
        }
    try:
        login_json = json.loads(login_body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {"ok": False, "skipped_reason": "login_bad_json", "login_http_status": login_status}
    csrf_token = login_json.get("csrf_token")
    if not any(cookie.name == "ict_admin_session" for cookie in jar) or not isinstance(csrf_token, str):
        return {"ok": False, "skipped_reason": "missing_cookie_or_csrf"}
    # Use an authorized repository fixture: a fake PDF would only test rejection.
    pdf_fixture = PROJECT_ROOT / "data" / "raw" / "pdf" / "sales_playbook.pdf"
    if not pdf_fixture.is_file():
        return {"ok": False, "skipped_reason": "pdf_fixture_missing"}
    pdf_bytes = pdf_fixture.read_bytes()
    request_id = "req.deep.upload"
    metadata = _make_metadata(request_id, pdf_bytes)
    body, content_type = build_multipart(metadata, pdf_bytes, "wbdeep0002")
    up_status, _up_headers, up_body = _http(
        base, "/api/v3/admin/documents", method="POST",
        headers={"Content-Type": content_type, "x-csrf-token": csrf_token,
                 "X-Request-ID": request_id},
        body=body, opener=opener,
    )
    doc_id = ""
    try:
        doc_id = json.loads(up_body.decode("utf-8")).get("document_id", "")
    except (ValueError, UnicodeDecodeError):
        doc_id = ""
    return {
        "ok": up_status in {200, 201} and bool(doc_id),
        "http_status": up_status,
        "document_id_returned": bool(doc_id),
        "must_change_password": login_json.get("must_change_password"),
    }


def validate_all(args: argparse.Namespace) -> dict:
    offline = offline_static_check()

    report: dict = {
        "status": "pending",
        "task_id": "D5-05-C",
        "owner": "C",
        "evidence_kind": "offline_prereq_check_only" if not args.live else "live_frontend_observable_probe",
        "business_claims_allowed": False,
        "offline_prereq": offline,
        "boundaries": {
            "b_side_scope": "本脚本不碰 B 主启动文件；B 的安全修复由独立测试验收。",
            "real_api_key": "未读取；连通测试仅 mock/失败关闭。",
            "model_api": "0 调用，费用 0 元。",
            "install_restart": "Docker/Windows 真实启动由 B 的 start.ps1 / compose.yaml 驱动；本脚本只探针运行中服务。",
        },
    }

    if not offline["b_server_present"]:
        report["status"] = "pending"
        report["pending_reason"] = "B 的 D5-05-B server.py 尚未交付（PR #79 未合并或未落地），live 探针无法执行。"
        return report

    if not args.live:
        report["status"] = "pending"
        report["pending_reason"] = "B 后端已交付但未指定 --live，离线预检通过；用 --live http://127.0.0.1:8000 跑连网探针。"
        return report

    live: dict = {
        "health_live": _health_live_check(args.live),
        "health_ready": _health_ready_check(args.live),
        "static": _static_check(args.live),
        "visitor_home": _visitor_home_check(args.live),
        "multipart_auth_boundary": _multipart_auth_boundary_probe(args.live),
    }
    if getattr(args, "admin_password", None) is not None:
        live["deep_upload"] = _deep_upload_probe(args.live, args.admin_password)

    report["live"] = live
    report["static_summary"] = {
        "total": len(STATIC_MANIFEST),
        "ok": sum(1 for r in live["static"].values() if r["ok"]),
    }
    # pass 仅当 C 侧可观测面全部通过；deep_upload 若存在也必须 ok。
    mandatory = [live["health_live"]["ok"], live["health_ready"]["ok"],
                 live["visitor_home"]["ok"], live["multipart_auth_boundary"]["ok"],
                 all(r["ok"] for r in live["static"].values())]
    if "deep_upload" in live:
        mandatory.append(live["deep_upload"]["ok"])
    report["status"] = "pass" if all(mandatory) else "fail"
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="D5-05-C 前端安装/重启端到端验收探针")
    parser.add_argument("--live", metavar="URL", help="连网探针基地址，如 http://127.0.0.1:8000")
    parser.add_argument("--deep-upload", action="store_true", help="从安全终端提示读取密码并做上传深验收")
    args = parser.parse_args()
    if args.deep_upload:
        if not args.live:
            parser.error("--deep-upload 需要 --live")
        args.admin_password = getpass.getpass("管理员密码（不会写入报告或命令行）：")
    else:
        args.admin_password = None
    report = validate_all(args)
    args.admin_password = None
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] in {"pass", "pending"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
