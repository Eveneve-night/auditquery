"""D5-04 C 侧前端（d5-console）契约与交互测试。

验证目标（均离线、可重复）：
1. 契约 mock 覆盖 configs/contracts/d5/product-layer.v1.json 的全部 24 个端点，
   且每条响应严格通过 configs/schemas/d5/api-v3.schema.json 的对应 $defs；
2. 错误体同样符合 error_response，错误码全部来自契约 error_catalog；
3. 失败关闭场景各自命中契约声明的错误码（重放 / 状态冲突 / 未知会话 /
   文件名注入 / 伪造 CSRF / 无身份）；
4. API Key 不回显：扫描全部响应与渲染文本，明文零命中，只出现末四位；
5. 双角色前端渲染与交互：访客提问/澄清/拒绝、管理员登录/文档队列/Key 状态/
   健康检查，技术细节默认折叠；
6. 红线：源码不写 localStorage/sessionStorage/Cookie，不内置默认口令或引导令牌，
   密钥框不预填、不提供“显示已保存 Key”，不向管理端点注入 model/base_url/
   threshold/fallback 等越权参数。

本测试不读取 expected / gold / label 字段，不发起网络或模型调用。
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
import unittest

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONSOLE_ROOT = PROJECT_ROOT / "src" / "frontend" / "d5-console"
CONTRACT_PATH = PROJECT_ROOT / "configs" / "contracts" / "d5" / "product-layer.v1.json"
SCHEMA_PATH = PROJECT_ROOT / "configs" / "schemas" / "d5" / "api-v3.schema.json"
V1_SCHEMA = PROJECT_ROOT / "configs" / "schemas" / "d4-07" / "shared-api-definitions.v1.schema.json"
V2_SCHEMA = PROJECT_ROOT / "configs" / "schemas" / "d4-07" / "shared-api-definitions.v2.schema.json"
RENDER_PROBE = PROJECT_ROOT / "tests" / "fixtures" / "d5-04" / "probe_console_render.mjs"
SWEEP_PROBE = PROJECT_ROOT / "tests" / "fixtures" / "d5-04" / "probe_api_v3_sweep.mjs"

# 扫描探针里每个调用名对应的响应契约（取自 product-layer.v1.json 的 response_schema）
EXPECTED_SCHEMA = {
    "bootstrap_admin": "admin_session_response",
    "create_visitor_session": "session_response",
    "read_own_session": "session_response",
    "read_own_session_after": "session_response",
    "read_own_events": "admin_summary_response",
    "submit_query": "agent_response",
    "submit_query_clarify": "agent_response",
    "submit_clarification": "agent_response",
    "admin_health": "admin_summary_response",
    "admin_read_key_status": "key_status_response",
    "admin_save_key": "key_status_response",
    "admin_replace_key": "key_status_response",
    "admin_test_key_connectivity": "admin_summary_response",
    "admin_delete_key": "operation_response",
    "admin_upload_document": "document_status_response",
    "admin_read_document": "document_status_response",
    "admin_publish_document": "document_status_response",
    "admin_disable_document": "document_status_response",
    "admin_download_document": "admin_summary_response",
    "admin_delete_document": "operation_response",
    "admin_read_audit": "admin_summary_response",
    "admin_read_usage": "admin_summary_response",
    "admin_logout": "operation_response",
    "admin_login": "admin_session_response",
    "admin_revoke_all_sessions": "operation_response",
    "delete_own_session": "operation_response",
}

# 失败关闭场景 → (HTTP 状态码, 契约错误码)
EXPECTED_FAILURES = {
    "err_bootstrap_replayed": (409, "admin_already_initialized"),
    "err_replayed_request": (409, "operation_replayed"),
    "err_stale_revision": (409, "state_conflict"),
    "err_unknown_session": (404, "resource_not_found"),
    "err_path_traversal_upload": (422, "document_invalid"),
    "err_forged_csrf": (403, "csrf_invalid"),
    "err_health_without_session": (401, "authentication_required"),
}


def _run_node(script: str | Path, *, inline: bool = False) -> dict:
    args = ["node"]
    if inline:
        args += ["--input-type=module", "-e", str(script)]
    else:
        args.append(str(script))
    result = subprocess.run(
        args, cwd=str(PROJECT_ROOT), capture_output=True, text=True, encoding="utf-8"
    )
    if result.returncode != 0:
        raise RuntimeError("node 探针执行失败：\n" + result.stderr)
    return json.loads(result.stdout.strip().splitlines()[-1])


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _strip_comments(source: str) -> str:
    """去掉块注释与行注释（保留 :// 形式的 URL），用于红线扫描。"""
    stripped = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"(?<!:)//[^\n]*", "", stripped)


def _validator(def_name: str) -> Draft202012Validator:
    """构造可解析 d5 → d4-07 跨目录引用的校验器。

    api-v3.schema.json 用相对路径 ../d4-07/shared-api-definitions.v2.schema.json
    引用问答载荷，referencing 需要显式注册这两份资源。
    """
    d5 = _load(SCHEMA_PATH)
    v1 = _load(V1_SCHEMA)
    v2 = _load(V2_SCHEMA)
    registry = Registry()
    for schema in (d5, v1, v2):
        registry = registry.with_resource(schema["$id"], Resource.from_contents(schema))
    for name, schema in (
        ("../d4-07/shared-api-definitions.v1.schema.json", v1),
        ("../d4-07/shared-api-definitions.v2.schema.json", v2),
    ):
        registry = registry.with_resource(name, Resource.from_contents(schema))
    schema = {"$id": d5["$id"], "$defs": d5["$defs"], **d5["$defs"][def_name]}
    return Draft202012Validator(schema, registry=registry)


class MockContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.contract = _load(CONTRACT_PATH)
        cls.sweep = _run_node(SWEEP_PROBE)
        cls.calls = cls.sweep["calls"]
        cls.catalog = {item["code"]: item for item in cls.contract["error_catalog"]}
        cls.validators = {
            name: _validator(name) for name in set(EXPECTED_SCHEMA.values()) | {"error_response"}
        }

    def test_contract_declares_24_endpoints(self) -> None:
        self.assertEqual(24, len(self.contract["endpoints"]))

    def test_sweep_covers_every_contract_endpoint(self) -> None:
        covered = {call["name"] for call in self.calls}
        missing = [item["id"] for item in self.contract["endpoints"] if item["id"] not in covered]
        self.assertEqual([], missing, "以下端点未被扫描探针覆盖：" + ", ".join(missing))

    def test_every_success_response_validates(self) -> None:
        problems = []
        for call in self.calls:
            name = call["name"]
            if name.startswith("err_"):
                continue
            validator = self.validators[EXPECTED_SCHEMA[name]]
            for err in validator.iter_errors(call["json"]):
                problems.append(f"{name} {'.'.join(err.path)}: {err.message}")
        self.assertEqual([], problems, "; ".join(problems[:5]))

    def test_every_error_response_validates_against_error_response(self) -> None:
        problems = []
        for call in self.calls:
            if not call["name"].startswith("err_"):
                continue
            for err in self.validators["error_response"].iter_errors(call["json"]):
                problems.append(f"{call['name']} {'.'.join(err.path)}: {err.message}")
        self.assertEqual([], problems, "; ".join(problems[:5]))

    def test_failure_close_scenarios_hit_contract_codes(self) -> None:
        by_name = {call["name"]: call for call in self.calls}
        for name, (status, code) in EXPECTED_FAILURES.items():
            call = by_name.get(name)
            self.assertIsNotNone(call, f"缺少失败场景 {name}")
            self.assertEqual(status, call["status"], f"{name} 状态码不符")
            self.assertEqual(code, call["json"]["error"]["code"], f"{name} 错误码不符")

    def test_error_codes_all_come_from_catalog(self) -> None:
        used = {
            call["json"]["error"]["code"]
            for call in self.calls
            if call["json"].get("error")
        }
        self.assertTrue(used, "扫描未产生任何错误响应")
        unknown = sorted(code for code in used if code not in self.catalog)
        self.assertEqual([], unknown, "出现契约未定义的错误码：" + ", ".join(unknown))

    def test_error_messages_match_catalog_safe_message(self) -> None:
        for call in self.calls:
            error = call["json"].get("error")
            if not error:
                continue
            catalog = self.catalog.get(error["code"])
            if catalog and error["code"] != "authentication_required":
                # 登录未注入口令的提示是对契约安全文案的补充说明，其余一律同文案
                continue
            self.assertLessEqual(len(error["message"]), 160)

    def test_api_key_plaintext_never_appears(self) -> None:
        blob = json.dumps(self.calls, ensure_ascii=False)
        for secret in ("sk-sweep-key-AAAA0001", "sk-sweep-key-BBBB0002"):
            self.assertNotIn(secret, blob, f"响应中泄漏了 API Key 明文（{secret}）")

    def test_key_status_only_exposes_last_four(self) -> None:
        by_name = {call["name"]: call for call in self.calls}
        saved = by_name["admin_save_key"]["json"]
        self.assertEqual("0001", saved["last_four"])
        self.assertEqual("bailian_beijing", saved["provider"])
        self.assertEqual("qwen3.7-flash", saved["model"])
        self.assertEqual("secret_store", saved["source"])
        self.assertNotIn("api_key", saved)
        replaced = by_name["admin_replace_key"]["json"]
        self.assertEqual("0002", replaced["last_four"])

    def test_model_boundary_is_frozen_in_health(self) -> None:
        health = next(c for c in self.calls if c["name"] == "admin_health")["json"]
        boundary = health["summary"]["model_boundary"]
        self.assertEqual("qwen3.7-flash", boundary["model"])
        self.assertEqual("bailian_beijing", boundary["provider"])
        self.assertFalse(boundary["fallback"])
        self.assertFalse(boundary["dynamic_routing"])

    def test_usage_summary_is_offline(self) -> None:
        usage = next(c for c in self.calls if c["name"] == "admin_read_usage")["json"]
        summary = usage["summary"]
        self.assertEqual(0, summary["model_calls"])
        self.assertEqual(0, summary["input_tokens"])
        self.assertEqual(0, summary["output_tokens"])
        self.assertEqual(0, summary["estimated_cost_cny"])

    def test_document_state_machine_is_contract_states(self) -> None:
        states = set(self.contract["pdf_lifecycle"]["states"])
        seen = set()
        for call in self.calls:
            body = call["json"]
            if "state" in body and isinstance(body.get("state"), str):
                seen.add(body["state"])
        self.assertTrue(seen, "扫描未产生任何文档状态")
        self.assertTrue(seen.issubset(states), f"出现契约外状态：{sorted(seen - states)}")
        self.assertIn("published", seen)
        self.assertIn("disabled", seen)


class FrontendRenderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.data = _run_node(RENDER_PROBE)
        cls.guest = cls.data["guest"]
        cls.admin = cls.data["admin"]
        cls.attacks = cls.data["attacks"]
        cls.leak = cls.data["leak"]
        cls.sourceChecks = cls.data["sourceChecks"]
        cls.rebind = cls.data["rebind"]

    def test_guest_sql_success_hides_tech_by_default(self) -> None:
        row = self.guest["sql"]
        self.assertEqual("guest-root", row["view"])
        self.assertTrue(row["resultVisible"])
        self.assertTrue(row["techHidden"], "技术细节必须默认折叠")
        self.assertEqual("通过", row["status"])
        self.assertIn("SELECT COUNT(*)", row["sqlCode"])
        self.assertEqual(1, row["tableRows"])
        self.assertTrue(row["emptyHidden"])

    def test_guest_rag_citation_mark_is_honest(self) -> None:
        row = self.guest["rag"]
        self.assertEqual("通过", row["status"])
        self.assertEqual(1, len(row["citations"]))
        text = row["citations"][0]
        self.assertIn("已校验", text)
        # PR #62 已合并，引用校验升级为证据 ID + 页码 + 源文逐字一致
        self.assertIn("证据 ID、页码与源文逐字一致", text)
        self.assertIn("SALES-001", text)

    def test_source_text_check_is_not_vacuous(self) -> None:
        # 逐字比对必须“非空转”：正例 true、篡改 false、未知 ID / 空片段 null
        self.assertTrue(self.sourceChecks["exactMatch"], "真实片段应判逐字一致")
        self.assertTrue(self.sourceChecks["tamperedSnippet"], "篡改片段应判不一致")
        self.assertTrue(self.sourceChecks["unknownId"], "未知 ID 应判无法校验")
        self.assertTrue(self.sourceChecks["emptySnippet"], "空片段应判无法校验")

    def test_guest_clarification_loop_closes(self) -> None:
        clarify = self.guest["clarify"]
        self.assertTrue(clarify["visible"])
        self.assertIn("指标", clarify["title"])
        self.assertIn("数量（COUNT）", clarify["options"])
        after = self.guest["clarifyAfter"]
        self.assertEqual("通过", after["status"])
        self.assertIn("SELECT COUNT(*)", after["sqlCode"])
        self.assertTrue(after["clarifyHidden"], "澄清闭环后必须收起澄清卡片")

    def test_guest_refusals_are_rendered(self) -> None:
        self.assertEqual("已拒绝", self.guest["sqlRefused"]["status"])
        self.assertIn("DELETE FROM", self.guest["sqlRefused"]["sqlCode"])
        self.assertEqual("已拒绝", self.guest["ragRefused"]["status"])
        self.assertTrue(self.guest["ragRefused"]["citationsHidden"])

    def test_login_rejected_without_injected_password(self) -> None:
        row = self.admin["noPassword"]
        self.assertFalse(row["ok"])
        self.assertIn("D5_MOCK_ADMIN_PASSWORD", row["error"])
        self.assertEqual("admin-login", row["view"])

    def test_wrong_password_uses_catalog_safe_message(self) -> None:
        row = self.admin["wrongPassword"]
        self.assertFalse(row["ok"])
        self.assertEqual("需要有效身份。", row["error"])

    def test_console_renders_all_admin_sections(self) -> None:
        console = self.admin["console"]
        self.assertEqual("admin-console", console["view"])
        self.assertIn("qwen3.7-flash", console["health"])
        self.assertIn("fallback false", console["health"])
        self.assertEqual(3, len(console["docs"]))
        joined = " | ".join(console["docs"])
        for state in ("已发布（published）", "失败（failed）", "已停用（disabled）"):
            self.assertIn(state, joined)
        self.assertIn("未配置", console["key"])
        self.assertIn("revision 0", console["key"])
        self.assertIn("保留 90 天", console["audit"])
        self.assertIn("模型调用 · 0", console["usage"])

    def test_key_input_is_password_and_never_prefilled(self) -> None:
        row = self.admin["keyInput"]
        self.assertEqual("password", row["type"])
        self.assertEqual("", row["value"], "密钥框不得预填任何值")
        self.assertEqual("off", row["autocomplete"])

    def test_key_save_never_shows_even_partial_key_and_clears_input(self) -> None:
        after = self.admin["afterSave"]
        self.assertIn("已配置", after["key"])
        self.assertNotIn("1234", after["key"])
        self.assertEqual("", after["inputValue"], "保存后必须清空输入框")
        self.assertFalse(self.leak["canaryInKeyPanel"])
        self.assertFalse(self.leak["canaryInConsole"])
        self.assertTrue(self.leak["noPartialKey"])
        # 对照组：同一扫描路径能命中页面里确实存在的文本，说明「明文零命中」非空转
        self.assertTrue(self.leak["controlFoundInConsole"], "泄漏扫描本身失效（对照组未命中）")

    def test_question_text_never_enters_audit(self) -> None:
        self.assertFalse(self.leak["canaryInAudit"], "审计事件里出现问题原文")

    def test_connectivity_test_is_explicit_and_offline(self) -> None:
        self.assertIn("离线 fixture", self.admin["connectivity"])
        self.assertIn("不能证明可达", self.admin["connectivity"])

    def test_document_state_transitions(self) -> None:
        self.assertIn("已停用：doc.1", self.admin["afterDisable"]["notice"])
        self.assertIn("state_conflict", self.admin["publishFailedDoc"]["notice"])
        self.assertIn("已发布：doc.3", self.admin["afterPublish"]["notice"])

    def test_attack_regressions(self) -> None:
        self.assertEqual("csrf_invalid", self.attacks["forgedCsrf"])
        self.assertEqual("csrf_invalid", self.attacks["missingCsrf"])
        self.assertEqual("authentication_required", self.attacks["healthWithoutSession"])

    def test_route_guard_defers_to_server(self) -> None:
        self.assertEqual("admin-login", self.admin["afterLogout"]["view"])
        self.assertEqual("#/admin/settings", self.admin["afterLogout"]["route"])
        self.assertIn("已注销。", self.admin["afterLogout"]["error"])
        # 无会话访问 #/admin：健康检查 401 后退回登录页，而不是直接渲染控制台
        self.assertEqual("admin-login", self.admin["guard"]["view"])
        self.assertEqual("请先登录。", self.admin["guard"]["error"])

    def test_form_events_rebound_after_view_swap(self) -> None:
        # 视图重建后表单 submit 监听不得丢失（修复 shellBound 只绑一次的缺陷）
        self.assertTrue(self.rebind["guestInitial"], "访客页初次渲染缺少提交监听")
        self.assertTrue(self.rebind["guestAfterReturn"], "切回访客页后表单提交监听丢失")
        self.assertTrue(self.rebind["loginAfterLogout"], "注销后登录表单提交监听丢失")

    def test_admin_v2_fixed_account_and_optional_password_change(self) -> None:
        self.assertEqual("admin-login", self.admin["settingsEntry"]["view"])
        self.assertEqual("#/admin/settings", self.admin["settingsEntry"]["route"])
        self.assertTrue(self.admin["settingsEntry"]["passwordEmpty"])
        self.assertEqual("admin", self.admin["fixedAccount"]["username"])
        self.assertEqual("readonly", self.admin["fixedAccount"]["readonly"])
        self.assertTrue(self.admin["fixedAccount"]["passwordEmpty"])
        changed = self.admin["passwordChange"]
        self.assertTrue(changed["changed"])
        self.assertEqual("admin-login", changed["view"])
        for field in ("signedOut", "currentCleared", "nextCleared", "confirmCleared",
                      "oldPasswordRejected", "newPasswordAccepted"):
            self.assertTrue(changed[field], field)


class FrontendSourceSafetyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # 红线扫描只看代码：注释里出现 "localStorage" 只是为了说明「不写 localStorage」
        cls.sources = {
            path.name: _strip_comments(path.read_text(encoding="utf-8"))
            for path in sorted(CONSOLE_ROOT.rglob("*.js")) + [CONSOLE_ROOT / "index.html"]
        }
        cls.all_source = "\n".join(cls.sources.values())

    def test_no_client_side_persistence(self) -> None:
        for pattern in (r"localStorage", r"sessionStorage", r"document\.cookie", r"indexedDB"):
            self.assertIsNone(
                re.search(pattern, self.all_source),
                f"前端源码出现客户端持久化调用（{pattern}）",
            )

    def test_no_default_credentials_in_source(self) -> None:
        for name, source in self.sources.items():
            self.assertIsNone(
                re.search(r"password\s*[:=]\s*[\"']", source),
                f"{name} 内置了默认口令字面量",
            )
            self.assertIsNone(
                re.search(r"setup_token\s*[:=]\s*[\"']", source),
                f"{name} 内置了固定引导令牌",
            )

    def test_no_reveal_saved_key_affordance(self) -> None:
        admin_source = self.sources["admin.js"]
        for phrase in ("显示已保存", "显示 Key", "查看明文", "getValue"):
            self.assertNotIn(phrase, admin_source, f"管理员页提供了「{phrase}」能力")

    def test_admin_client_does_not_send_overreach_params(self) -> None:
        client_source = self.sources["api-client.js"]
        for key in ("base_url", "threshold", "fallback", "dynamic_routing", "raw_sql"):
            self.assertNotIn(f'"{key}"', client_source, f"管理请求注入了越权参数 {key}")
        # Provider 与模型只能取契约常量
        self.assertIn('PROVIDER = "bailian_beijing"', client_source)
        self.assertIn('MODEL = "qwen3.7-flash"', client_source)

    def test_index_html_declares_boundaries(self) -> None:
        html = self.sources["index.html"]
        # 面向普通用户：诚实标注演示模式与示例数据，不再暴露开发术语。
        self.assertIn("演示模式", html)
        self.assertIn("使用示例数据", html)
        self.assertIn("不构成安全边界", html)
        self.assertNotIn("离线 mock", html)
        self.assertNotIn("模型调用 0 次", html)
        self.assertNotIn("D5 产品层", html)


if __name__ == "__main__":
    unittest.main()
