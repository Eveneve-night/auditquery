"""D5-04 双角色前端演示截图生成：用本机 Edge（Chromium 内核）headless 生成 3 张截图。

离线：运行时只访问 127.0.0.1 静态页（内置线程 HTTP 服务），mock 驱动，
不发起模型/网络调用、不读取真实 API Key。

可复现前置（依赖已登记在 requirements-dev.txt）：
  - pip install -r requirements-dev.txt
  - 浏览器走 ``channel="msedge"``（本机 Edge），无需 ``playwright install`` 下载 Chromium。
  - 本脚本不录屏，无需 Playwright FFmpeg 工具。

与 D4-07 演示脚本（capture_demo_screenshots.py）的差异：
  - D5 页面为 ES module + hash 路由（#/guest / #/admin/login / #/admin），
    由脚本内置静态服务并保证 ``.js`` 的 MIME 为 ``text/javascript``。
  - 管理员流需先注入演示口令 ``window.D5_MOCK_ADMIN_PASSWORD``（add_init_script
    在页面脚本执行前注入）；口令运行时随机生成，仓库内不留默认口令字面量
    （与 tests/fixtures/d5-04/probe_console_render.mjs 口径一致）。
"""

from __future__ import annotations

import json
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import secrets
import sys
import threading

from playwright.sync_api import sync_playwright

PROJECT_ROOT = Path(__file__).resolve().parents[2]
# 服务根目录为 src/frontend：guest.js 会跨目录 import ../d4-console/rag-manifest.js，
# 只 serve d5-console 会导致该模块 404、视图不渲染。
FRONTEND_DIR = PROJECT_ROOT / "src" / "frontend"
OUT = PROJECT_ROOT / "deliverables" / "d5-demo-screenshots"
PORT = 8900
BASE_URL = f"http://127.0.0.1:{PORT}/d5-console/index.html"


class _ConsoleHandler(SimpleHTTPRequestHandler):
    """静态服务 d5-console，固定 .js/.mjs 的 MIME 以支持 ES module 加载。"""

    extensions_map = {
        **SimpleHTTPRequestHandler.extensions_map,
        ".js": "text/javascript",
        ".mjs": "text/javascript",
        ".css": "text/css",
        ".html": "text/html",
    }

    def log_message(self, *args):  # 静默访问日志
        pass


def start_server() -> ThreadingHTTPServer:
    handler = partial(_ConsoleHandler, directory=str(FRONTEND_DIR))
    server = ThreadingHTTPServer(("127.0.0.1", PORT), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def capture(browser) -> int:
    demo_password = "pw." + secrets.token_hex(8) + ".mockonly"
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    page.add_init_script(
        f"window.D5_MOCK_ADMIN_PASSWORD = {json.dumps(demo_password)};"
    )

    # 场景 1：访客 SQL 提问成功
    page.goto(BASE_URL + "#/guest", wait_until="networkidle")
    page.fill("#guest-question", "统计艺术家数量")
    page.click("#guest-submit")
    page.wait_for_timeout(700)
    page.screenshot(path=str(OUT / "01-guest-sql-success.png"), full_page=True)

    # 场景 2：访客技术细节展开（SQL/轨迹/阈值/用量默认折叠）
    page.click("#guest-tech-toggle")
    page.wait_for_timeout(300)
    page.screenshot(path=str(OUT / "02-guest-tech-details.png"), full_page=True)

    # 场景 3：管理员登录后进入控制台（口令由 add_init_script 注入）
    page.goto(BASE_URL + "#/admin/login", wait_until="networkidle")
    page.fill("#a-login-user", "admin")
    page.fill("#a-login-pass", demo_password)
    page.click("#a-login-submit")
    page.wait_for_timeout(700)  # 登录成功后前端跳转 #/admin
    page.screenshot(path=str(OUT / "03-admin-console.png"), full_page=True)

    page.close()
    return 0


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    server = start_server()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="msedge", headless=True)
            capture(browser)
            browser.close()
    finally:
        server.shutdown()

    produced = sorted(OUT.glob("*.png"))
    print(f"共生成 {len(produced)} 张截图：")
    for path in produced:
        print(f"  {path.name}  ({path.stat().st_size} bytes)")
    return 0 if len(produced) == 3 else 1


if __name__ == "__main__":
    sys.exit(main())
