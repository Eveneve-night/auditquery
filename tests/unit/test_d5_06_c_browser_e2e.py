"""D5-06-C 浏览器逐接口 E2E（Playwright + 本机 Microsoft Edge，headless）。

与 ``test_d5_visitor_rag_http_e2e.py``（subprocess + ``http.client`` 模拟浏览器）不同，
本文件用**真实浏览器引擎**驱动前端 UI，证明 D5 双角色控制台在浏览器同源环境下：

- 访客链：live 接线（``window.D5_LIVE_API``）→ 建会话 → RAG 命中出引用 →
  无证据不伪造 → 澄清闭环；
- 管理员链：登录（HttpOnly Cookie + 内存 CSRF）→ 上传 multipart PDF → 发布 →
  访客检索命中（完整闭环）→ Key 只显示末四位；
- 安全负例：登录失败统一错误、未认证访问管理端 100% 阻断、写操作缺 CSRF 被拒。

离线确定性：本地 ``serve --mode online``，只访问 ``127.0.0.1``，0 模型调用、
0 真实 Key、0 网络费用。假主密钥 ``b"a"*32``；API Key 用明显假值
``sk-testonly-...``。

依赖：``playwright``（Python）与本机 Microsoft Edge。二者任一缺失时整模块
``skip``，不拖累无浏览器环境的全量 unittest。
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from backend.product.document_store import DocumentAuditContext
from backend.product.server import ROOT, ProductRuntime

try:
    from playwright.sync_api import sync_playwright

    HAS_PLAYWRIGHT = True
except ImportError:  # pragma: no cover - 环境缺失
    HAS_PLAYWRIGHT = False

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PDF_PATH = PROJECT_ROOT / "data" / "raw" / "pdf" / "sales_playbook.pdf"
# 与 setUpClass 预置文档不同 hash 的 PDF，用于验证「新上传 → quarantined」。
UPLOAD_PDF_PATH = PROJECT_ROOT / "data" / "raw" / "pdf" / "customer_success.pdf"

MASTER_KEY = b"a" * 32
MASTER_KEY_B64 = base64.b64encode(MASTER_KEY).decode("ascii")
ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "ictagent123"  # admin-bootstrap.v2 冻结的初始口令，非秘密
FAKE_API_KEY = "sk-testonly-00000000000000000000000000000000"

# RAG 命中 / 无证据 / SQL 澄清 的稳定问题（已探针验证，见访客链用例）。
RAG_HIT_QUESTION = "毛利率"
NO_EVIDENCE_QUESTION = "区块链共识算法"
SQL_CLARIFY_QUESTION = "艺术家"  # 缺少指标；明确计数已支持直接解析。


def _envelope(request_id: str, **extra):
    return {"api_version": "3.0", "contract_version": "1.0.0", "request_id": request_id, **extra}


def _wait_ready(port: int, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            conn.request("GET", "/health/ready")
            status = conn.getresponse().status
            conn.close()
            if status == 200:
                return True
        except OSError:
            pass
        time.sleep(0.1)
    return False


@unittest.skipUnless(HAS_PLAYWRIGHT, "playwright 未安装，跳过 D5-06-C 浏览器 E2E")
class _BrowserE2EBase(unittest.TestCase):
    """起一个真实 online 服务 + 一个 headless Edge 浏览器，供子类复用。"""

    _server: subprocess.Popen | None = None
    _browser = None
    _playwright = None
    _instance: Path | None = None
    _temp = None
    port = 0

    @classmethod
    def setUpClass(cls):
        if not HAS_PLAYWRIGHT:
            raise unittest.SkipTest("playwright 未安装")
        try:
            cls._temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
            cls._instance = Path(cls._temp.name)

            # 预置一个 published 文档，保证访客链有确定证据来源。
            runtime = ProductRuntime(cls._instance, mode="online", master_key=MASTER_KEY)
            pdf = PDF_PATH.read_bytes()
            upload = runtime.document_app.document_service.upload(
                filename="sales_playbook.pdf", content_type="application/pdf",
                declared_bytes=len(pdf), content_sha256=hashlib.sha256(pdf).hexdigest(),
                file_bytes=pdf, audit=DocumentAuditContext("req-u", "sess", "test"),
            )
            runtime.document_app.document_service.publish(
                upload.document_id, audit=DocumentAuditContext("req-p", "sess", "test"),
            )
            cls.published_document_id = upload.document_id

            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                cls.port = sock.getsockname()[1]

            env = os.environ.copy()
            env["ICT_SECRET_MASTER_KEY_B64"] = MASTER_KEY_B64
            env["PYTHONPATH"] = str(ROOT / "src")
            cls._server = subprocess.Popen(
                [sys.executable, "-m", "backend.product.server", "serve",
                 "--instance", str(cls._instance), "--mode", "online", "--port", str(cls.port)],
                env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            if not _wait_ready(cls.port):
                raise RuntimeError("online 服务未在超时内就绪")

            cls._playwright = sync_playwright().start()
            cls._browser = cls._playwright.chromium.launch(channel="msedge", headless=True)
        except Exception as exc:  # pragma: no cover - 环境缺失（无 Edge 等）
            cls._cleanup_server()
            raise unittest.SkipTest(f"浏览器环境不可用：{exc}") from exc

    @classmethod
    def _cleanup_server(cls):
        if cls._server is not None:
            try:
                cls._server.terminate()
                cls._server.wait(timeout=5)
            except Exception:
                cls._server.kill()
            cls._server = None
        if cls._browser is not None:
            try:
                cls._browser.close()
            except Exception:
                pass
            cls._browser = None
        if cls._playwright is not None:
            try:
                cls._playwright.stop()
            except Exception:
                pass
            cls._playwright = None
        if cls._temp is not None:
            cls._temp.cleanup()
            cls._temp = None

    @classmethod
    def tearDownClass(cls):
        cls._cleanup_server()

    def setUp(self):
        self.context = self._browser.new_context()
        self.page = self.context.new_page()
        self.base = f"http://127.0.0.1:{self.port}"

    def tearDown(self):
        try:
            self.context.close()
        except Exception:
            pass

    # ---- helpers ----

    def open_guest(self):
        self.page.goto(self.base + "/", wait_until="networkidle")
        self.page.wait_for_selector("#guest-question", state="visible", timeout=10_000)

    def ask(self, question: str):
        self.page.fill("#guest-question", question)
        self.page.click("#guest-submit")
        self.page.wait_for_selector("#guest-result", state="visible", timeout=10_000)
        self.page.wait_for_timeout(300)

    def login_admin(self, password: str = ADMIN_PASSWORD):
        self.page.goto(self.base + "/#/admin/settings", wait_until="networkidle")
        self.page.wait_for_selector("#a-login-pass", state="visible", timeout=10_000)
        self.page.fill("#a-login-pass", password)
        self.page.click("#a-login-submit")
        self.page.wait_for_timeout(500)


class GuestChainBrowserTest(_BrowserE2EBase):
    """D5-06-C 完成条件①③：访客链在真实浏览器逐接口走通。"""

    def test_live_badge_and_rag_retrieval(self):
        """首页 online 接线 + RAG 命中出带页码引用（浏览器真实 fetch）。"""
        self.open_guest()
        # 标题与数据源徽标必须显示「在线」，证明 D5_LIVE_API 注入生效。
        self.assertIn("在线", self.page.title())
        badge = self.page.locator("#mode-badge").inner_text()
        self.assertIn("在线模式", badge)

        self.ask(RAG_HIT_QUESTION)
        self.assertEqual("通过", self.page.locator("#guest-result-status").inner_text())
        # RAG 命中：至少一条引用，且回指页码。引用卡片标题为「文档标题 · 第 N 页」；
        # PR #90（面向普通用户清理）后不再展示内部 evidence_id，断言页码显示即可。
        citations = self.page.locator("#guest-citations article")
        self.assertGreater(citations.count(), 0)
        first = citations.first.inner_text()
        self.assertIn("页", first)  # 页码回指

    def test_no_evidence_not_forged(self):
        """无证据问题不得伪造 RAG 答案（离线不编造事实）。"""
        self.open_guest()
        self.ask(NO_EVIDENCE_QUESTION)
        status = self.page.locator("#guest-result-status").inner_text()
        self.assertIn(status, {"已拒绝", "需要补充信息", "错误"})
        # 无证据时不得出现任何引用。
        self.assertEqual(0, self.page.locator("#guest-citations article").count())

    def test_clarification_submit_accepted(self):
        """SQL 缺槽触发澄清，点选项提交被服务端接受（离线不伪造答案）。"""
        self.open_guest()
        self.ask(SQL_CLARIFY_QUESTION)
        self.page.wait_for_selector("#guest-clarify", state="visible", timeout=10_000)
        self.assertGreater(self.page.locator("#guest-clarify-options .clarify-option").count(), 0)
        self.page.locator("#guest-clarify-options .clarify-option").first.click()
        self.page.wait_for_timeout(600)
        # 提交被服务端接受：无错误框、状态不是「错误」（离线 SQL 缺 credential
        # 可能继续要求澄清，这是正确失败关闭，不伪造答案）。
        self.assertTrue(self.page.locator("#guest-error").is_hidden())
        self.assertNotEqual("错误", self.page.locator("#guest-result-status").inner_text())


class AdminChainBrowserTest(_BrowserE2EBase):
    """D5-06-C 完成条件①：管理员链 Cookie/CSRF/登录失败/未认证阻断。"""

    def test_login_success_renders_console(self):
        """默认管理员登录成功进入控制台，各面板可见。"""
        self.login_admin()
        self.page.wait_for_selector('#view[data-view="admin-console"]', state="visible", timeout=10_000)
        # 控制台含健康/文档/Key/审计/用量五个面板。
        for sel in ("#a-health", "#a-docs", "#a-key-status", "#a-audit", "#a-usage"):
            self.assertTrue(self.page.locator(sel).count() > 0, f"缺面板 {sel}")
        # 健康面板渲染服务端实际返回（当前 summary 仅含 mode，契约允许自由对象）。
        self.assertIn("状态 ok", self.page.locator("#a-health").inner_text())

    def test_login_failure_uniform_error(self):
        """错误口令登录失败：统一错误提示，仍停留登录页，不进入控制台。"""
        self.login_admin("wrong-password-not-admin")
        # 登录失败后仍在登录页（有错误提示，无控制台）。
        self.assertTrue(self.page.locator("#a-login-error").count() > 0)
        self.assertEqual(0, self.page.locator('#view[data-view="admin-console"]').count())

    def test_upload_multipart_over_browser(self):
        """管理员浏览器真实 file input 上传 multipart PDF，服务端接收成功。"""
        self.login_admin()
        self.page.wait_for_selector('#view[data-view="admin-console"]', state="visible", timeout=10_000)
        # 浏览器真实 file input 上传（multipart 由 live-transport 构造）；用未上传过的
        # PDF 验证「新上传 → quarantined」完整状态。
        self.page.set_input_files("#a-doc-picker", str(UPLOAD_PDF_PATH))
        self.page.wait_for_timeout(500)
        # 拦截上传响应：multipart 上传被服务端接受（200），并返回 document_id。
        with self.page.expect_response(
            lambda r: "/api/v3/admin/documents" in r.url and r.request.method == "POST",
            timeout=10_000,
        ) as resp_info:
            self.page.click("#a-doc-upload")
        response = resp_info.value
        self.assertEqual(200, response.status)
        body = response.json()
        self.assertIn("document_id", body)
        # upload 同步完成校验链（magic/字节/hash/页数/双解析），最终态为 validated。
        self.assertEqual("validated", body.get("state"))
        self.assertTrue(body["document_id"].startswith("doc."))

    def test_published_document_retrievable_by_guest(self):
        """管理员发布后访客在浏览器检索命中（发布+检索闭环，发布经 API 预置）。"""
        # setUpClass 已通过 document_service 上传并发布 sales_playbook.pdf；此处
        # 用一个无管理员身份的访客上下文验证「published 文档可被访客检索」。
        guest_context = self._browser.new_context()
        try:
            guest_page = guest_context.new_page()
            guest_page.goto(self.base + "/", wait_until="networkidle")
            guest_page.wait_for_selector("#guest-question", state="visible", timeout=10_000)
            guest_page.fill("#guest-question", RAG_HIT_QUESTION)
            guest_page.click("#guest-submit")
            guest_page.wait_for_selector("#guest-result", state="visible", timeout=10_000)
            guest_page.wait_for_timeout(300)
            self.assertEqual("通过", guest_page.locator("#guest-result-status").inner_text())
            self.assertGreater(guest_page.locator("#guest-citations article").count(), 0)
        finally:
            guest_context.close()

    def test_key_status_only_last_four(self):
        """保存 Key 后：状态只显示末四位，输入框清空、不回显明文。"""
        self.login_admin()
        self.page.wait_for_selector('#view[data-view="admin-console"]', state="visible", timeout=10_000)
        self.page.fill("#a-key-input", FAKE_API_KEY)
        self.page.click("#a-key-save")
        self.page.wait_for_timeout(800)
        key_text = self.page.locator("#a-key-status").inner_text()
        # 明文 Key 不得出现在状态面板，只显示末四位。
        self.assertNotIn(FAKE_API_KEY, key_text)
        self.assertIn("末四位", key_text)
        self.assertIn(FAKE_API_KEY[-4:], key_text)
        # 输入框提交后清空。
        self.assertEqual("", self.page.locator("#a-key-input").input_value())

    def test_csrf_missing_on_admin_write(self):
        """登录后写操作缺 CSRF 头 → 403 csrf_invalid（服务端强制）。"""
        self.login_admin()
        self.page.wait_for_selector('#view[data-view="admin-console"]', state="visible", timeout=10_000)
        # 用浏览器 fetch 直发一个带会话 Cookie 但无 x-csrf-token 的写操作。
        result = self.page.evaluate(
            """async () => {
                const r = await fetch('/api/v3/admin/secrets/bailian', {
                    method: 'POST',
                    headers: {'content-type': 'application/json'},
                    body: JSON.stringify({api_version:'3.0', contract_version:'1.0.0',
                        request_id:'csrf-neg', key:'sk-testonly-11111111111111111111'}),
                    credentials: 'same-origin',
                });
                return {status: r.status};
            }"""
        )
        self.assertEqual(403, result["status"])


class AdminSecurityBrowserTest(_BrowserE2EBase):
    """D5-06-C 完成条件①：未认证访问管理端 100% 阻断（IDOR/越权）。"""

    def test_unauthenticated_admin_routes_blocked(self):
        """浏览器同源 fetch 未携带管理会话时，所有管理端点返回 401/403。"""
        self.open_guest()  # 访客页，未登录管理员
        protected = [
            ("GET", "/api/v3/admin/health"),
            ("GET", "/api/v3/admin/documents"),
            ("GET", "/api/v3/admin/secrets/bailian"),
            ("GET", "/api/v3/admin/audit-events"),
            ("GET", "/api/v3/admin/usage-summary"),
            ("POST", "/api/v3/admin/logout"),
            ("POST", "/api/v3/admin/sessions/revoke-all"),
            ("POST", "/api/v3/admin/secrets/bailian"),
            ("DELETE", "/api/v3/admin/secrets/bailian"),
        ]
        results = self.page.evaluate(
            """async (paths) => {
                const out = [];
                for (const [m, p] of paths) {
                    const r = await fetch(p, {method: m, headers: {'content-type':'application/json'},
                        body: m === 'GET' ? undefined : '{}', credentials: 'same-origin'});
                    out.push({m, p, status: r.status});
                }
                return out;
            }""",
            protected,
        )
        for row in results:
            self.assertIn(
                row["status"], (401, 403),
                f"{row['m']} {row['p']} 未认证应被阻断，实际 {row['status']}",
            )

    def test_guest_cannot_touch_other_session(self):
        """访客会话横向越权（用他人 session_id 读会话）→ 404/401（IDOR 阻断）。"""
        self.open_guest()
        # 建会话后拿 session_id，再尝试用一个不存在的他人 session_id 读取。
        self.ask(RAG_HIT_QUESTION)
        result = self.page.evaluate(
            """async () => {
                const r = await fetch('/api/v3/sessions/doc.definitely-not-mine', {
                    credentials: 'same-origin',
                });
                return {status: r.status};
            }"""
        )
        self.assertIn(result["status"], (401, 404))


if __name__ == "__main__":
    unittest.main()
