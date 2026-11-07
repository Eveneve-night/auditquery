"""D5-04 双角色控制台真实浏览器冒烟（离线 mock + 本机 Edge）。

用途：证明 src/frontend/d5-console 在真实 DOM 下可渲染、可交互，并产出本地演示
演示用的截图。全程离线：只连本地静态服务，不发外网请求、不调模型。

用法：
    python tools/demo/capture_d5_console.py

产物：tests/reports/d5-04-screenshots/*.png
"""

from __future__ import annotations

import functools
import http.server
import socketserver
import threading
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PAGE_PATH = "/src/frontend/d5-console/index.html"
OUT_DIR = PROJECT_ROOT / "tests" / "reports" / "d5-04-screenshots"
PORT = 8912
BROWSER_CHANNEL = "msedge"


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args, **kwargs) -> None:  # 静默访问日志
        return


def serve_forever() -> socketserver.TCPServer:
    handler = functools.partial(QuietHandler, directory=str(PROJECT_ROOT))
    socketserver.TCPServer.allow_reuse_address = True
    httpd = socketserver.TCPServer(("127.0.0.1", PORT), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    httpd = serve_forever()
    base = f"http://127.0.0.1:{PORT}"
    shots = []

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel=BROWSER_CHANNEL, headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 1000})

            # ---- 访客页：提问 → SQL 成功 ----
            page.goto(f"{base}{PAGE_PATH}#/guest", wait_until="networkidle")
            page.fill("#guest-question", "统计艺术家数量")
            page.click("#guest-submit")
            page.wait_for_selector("#guest-result:not([hidden])", timeout=10000)
            page.wait_for_timeout(300)
            guest_shot = OUT_DIR / "01-guest-sql-success.png"
            page.screenshot(path=str(guest_shot), full_page=True)
            shots.append(guest_shot)

            # ---- 访客页：展开技术细节 ----
            page.click("#guest-tech-toggle")
            page.wait_for_timeout(200)
            tech_shot = OUT_DIR / "02-guest-tech-details.png"
            page.screenshot(path=str(tech_shot), full_page=True)
            shots.append(tech_shot)

            # ---- 管理员登录（演示口令运行时注入，不落盘） ----
            # 用导航栏点击切换路由（哈希跳转在自动化 goto 下不触发 hashchange，
            # 真实浏览器里两种方式都会触发，这里走真实用户路径）
            page.click('.nav-item[data-route="#/admin/login"]')
            page.wait_for_selector("#a-login-user", timeout=10000)
            demo_password = "pw." + str(int(time.time())) + ".demo-only"
            page.evaluate("(pw) => { window.D5_MOCK_ADMIN_PASSWORD = pw; }", demo_password)
            page.fill("#a-login-user", "demo.admin")
            page.fill("#a-login-pass", demo_password)
            page.click("#a-login-submit")
            page.wait_for_selector('[data-view="admin-console"]', timeout=10000)
            page.wait_for_timeout(500)
            admin_shot = OUT_DIR / "03-admin-console.png"
            page.screenshot(path=str(admin_shot), full_page=True)
            shots.append(admin_shot)

            browser.close()
    finally:
        httpd.shutdown()

    for shot in shots:
        print(f"截图已生成：{shot.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
