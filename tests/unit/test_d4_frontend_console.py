"""D4-07 C 侧前端（d4-console）契约与交互测试（对齐 v2 澄清绑定契约）。

验证目标（均离线、可重复）：
1. 契约 mock 产出的每一条响应都严格通过共享在线契约
   configs/schemas/d4-07/online-response.v2.schema.json；
2. 前端 adapter 构建的 submit_query / submit_clarification 请求严格通过
   online-request.v2.schema.json，且 submit_clarification 只生成 answer_id、
   六绑定字段从 pending 原样回传、单值单证据；
3. mock 覆盖六类路由，且澄清提交（submit_clarification）可闭环；
4. mock 实现 D4-05 失败关闭：六字段篡改、重放、空证据、非法值均拒绝，
   错误码与 clarification-binding.v2.json 一致；
5. trace 的 artifact_sha256 为真实 SHA-256，与 Python hashlib 独立复算一致；
6. 前端源码（app.js / adapter.js / mock/index.js）不引用 expected/gold/label。

本测试不读取任何 expected / gold / label 字段，不发起网络或模型调用。
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
import unittest

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_ROOT = PROJECT_ROOT / "src" / "frontend" / "d4-console"
SCHEMA_ROOT = PROJECT_ROOT / "configs" / "schemas" / "d4-07"
MOCK_PATH = FRONTEND_ROOT / "mock" / "index.js"
ADAPTER_PATH = FRONTEND_ROOT / "adapter.js"
APP_JS_PATH = FRONTEND_ROOT / "app.js"
INDEX_HTML_PATH = FRONTEND_ROOT / "index.html"
RENDER_PROBE_PATH = PROJECT_ROOT / "tests" / "fixtures" / "d4-07" / "probe_frontend_render.mjs"

ARTIST_SQL = "SELECT COUNT(*) AS artist_count FROM Artist"


def _node() -> str:
    return "node"


def _run_node(script: str, *, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [_node(), "--input-type=module", "-e", script],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _file_url(path: Path) -> str:
    return "file:///" + str(path.resolve()).replace("\\", "/")


def _load_schema(name: str) -> dict:
    return json.loads((SCHEMA_ROOT / name).read_text(encoding="utf-8"))


def _online_validator(wrapper_name: str) -> Draft202012Validator:
    """加载 v1 + v2 的 shared-api-definitions（v2 复用 v1 的 decision/sql/citation 等）。"""
    registry: Registry = Registry()
    for name in (
        "shared-api-definitions.v1.schema.json",
        "shared-api-definitions.v2.schema.json",
    ):
        schema = _load_schema(name)
        registry = registry.with_resource(
            schema["$id"], Resource.from_contents(schema)
        )
    return Draft202012Validator(_load_schema(wrapper_name), registry=registry)


def _run_mock_probe() -> list[dict]:
    mock_url = _file_url(MOCK_PATH)
    questions = [
        "统计艺术家数量",
        "删除 Artist 表",
        "月度经营报表的截止时间是什么时候",
        "公司健身房几点开门可以洗澡吗",
        "hi",
    ]
    lines = ['import mock from "' + mock_url + '";', "const out = [];"]
    for idx, q in enumerate(questions):
        lines.append(
            'out.push(mock.query({ schema_version: "2.0.0", operation: "submit_query", '
            'session_id: "session.probe.' + str(idx) + '", request_id: "req.probe.' + str(idx) + '", '
            'turn_id: 1, state_revision: 0, query: "' + q + '" }));'
        )
    lines.append(
        'out.push(mock.query({ schema_version: "2.0.0", operation: "unknown", '
        'session_id: "session.probe.x", request_id: "req.probe.x", turn_id: 1, '
        'state_revision: 0 }));'
    )
    lines.append(
        "Promise.all(out).then((rs) => { console.log(JSON.stringify(rs)); });"
    )
    result = _run_node("\n".join(lines), cwd=PROJECT_ROOT)
    if result.returncode != 0:
        raise RuntimeError("mock 探针执行失败：\n" + result.stderr)
    return json.loads(result.stdout.strip().splitlines()[-1])


def _run_adapter_probe() -> dict:
    adapter_url = _file_url(ADAPTER_PATH)
    lines = [
        'import * as adapter from "' + adapter_url + '";',
        'const session = adapter.newSession();',
        'const query = adapter.buildSubmitQuery(session, "统计艺术家数量");',
        'const pending = { question_id: "question.web.1", session_id: "session.x", '
        'request_id: "request.x", turn_id: 1, state_revision: 2, slot_id: "slot.metric" };',
        'const clarification = adapter.buildSubmitClarification(pending, "count", '
        '"value_evidence.metric_count");',
        "console.log(JSON.stringify({ session, query, clarification }));",
    ]
    result = _run_node("\n".join(lines), cwd=PROJECT_ROOT)
    if result.returncode != 0:
        raise RuntimeError("adapter 探针执行失败：\n" + result.stderr)
    return json.loads(result.stdout.strip().splitlines()[-1])


def _run_render_probe() -> dict:
    result = subprocess.run(
        [_node(), str(RENDER_PROBE_PATH)],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        raise RuntimeError("渲染探针执行失败：\n" + result.stderr)
    return json.loads(result.stdout.strip().splitlines()[-1])


class MockOnlineContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.response_validator = _online_validator("online-response.v2.schema.json")
        cls.responses = _run_mock_probe()

    def test_mock_probe_returns_six_routes(self) -> None:
        self.assertEqual(6, len(self.responses))

    def test_every_mock_response_validates_against_v2_schema(self) -> None:
        errors = []
        for idx, response in enumerate(self.responses):
            for err in self.response_validator.iter_errors(response):
                errors.append(f"response[{idx}] {'.'.join(err.path)}: {err.message}")
        self.assertEqual([], errors, "存在未通过 v2 契约的响应：" + "; ".join(errors[:5]))

    def test_mock_covers_route_and_status_matrix(self) -> None:
        seen = {(r["route"], r["status"]) for r in self.responses}
        self.assertIn(("sql", "success"), seen)
        self.assertIn(("sql", "refused"), seen)
        self.assertIn(("rag", "success"), seen)
        self.assertIn(("rag", "refused"), seen)
        self.assertIn(("clarification", "clarification_required"), seen)
        self.assertIn(("unknown", "error"), seen)

    def test_usage_is_strictly_offline(self) -> None:
        for response in self.responses:
            u = response["usage"]
            self.assertEqual(0, u["model_calls"])
            self.assertEqual(0, u["input_tokens"])
            self.assertEqual(0, u["output_tokens"])
            self.assertEqual(0, u["estimated_cost_cny"])

    def test_rag_citation_carries_page_and_evidence_id(self) -> None:
        rag_ok = [r for r in self.responses if r["route"] == "rag" and r["status"] == "success"]
        self.assertTrue(rag_ok, "缺少 RAG 成功样例")
        for citation in rag_ok[0]["citations"]:
            self.assertIn("page", citation)
            self.assertIn("evidence_id", citation)
            self.assertIn("document_id", citation)
            self.assertGreaterEqual(citation["page"], 1)

    def test_sql_refusal_keeps_safety_rejected_and_zero_attempts(self) -> None:
        sql_refused = [r for r in self.responses if r["route"] == "sql" and r["status"] == "refused"]
        self.assertTrue(sql_refused, "缺少 SQL 拒绝样例")
        for r in sql_refused:
            if r["sql"] is not None:
                self.assertEqual("rejected", r["sql"]["safety_status"])
                self.assertEqual(0, r["sql"]["execution_attempts"])

    def test_clarification_carries_options_and_binding(self) -> None:
        clarify = [r for r in self.responses if r["status"] == "clarification_required"]
        self.assertTrue(clarify, "缺少澄清样例")
        pending = clarify[0]["pending_question"]
        self.assertIsNotNone(pending)
        self.assertEqual("slot.metric", pending["slot_id"])
        self.assertIn("options", pending)
        self.assertGreaterEqual(len(pending["options"]), 1)
        for opt in pending["options"]:
            self.assertIn("value", opt)
            self.assertIn("display_name", opt)
            self.assertIn("evidence_ref", opt)


class AdapterContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.request_validator = _online_validator("online-request.v2.schema.json")
        cls.probe = _run_adapter_probe()

    def test_submit_query_request_validates(self) -> None:
        self.assertEqual([], list(self.request_validator.iter_errors(self.probe["query"])))

    def test_submit_clarification_request_validates(self) -> None:
        self.assertEqual([], list(self.request_validator.iter_errors(self.probe["clarification"])))

    def test_turn_id_and_revision_are_integers(self) -> None:
        self.assertIsInstance(self.probe["session"]["turn_id"], int)
        self.assertIsInstance(self.probe["session"]["state_revision"], int)

    def test_submit_clarification_echoes_pending_binding_and_generates_answer_id(self) -> None:
        c = self.probe["clarification"]
        pending = {
            "question_id": "question.web.1",
            "session_id": "session.x",
            "request_id": "request.x",
            "turn_id": 1,
            "state_revision": 2,
            "slot_id": "slot.metric",
        }
        # 六字段原样回传（尤其 request_id 不得重建）
        for field in ("session_id", "request_id", "turn_id", "state_revision", "question_id", "slot_id"):
            self.assertEqual(pending[field], c[field], f"{field} 必须从 pending 原样回传")
        # 客户端只生成 answer_id
        self.assertIn("answer_id", c)
        self.assertTrue(c["answer_id"].startswith("answer."))
        # 单值单证据
        self.assertEqual(["count"], c["values"])
        self.assertEqual(["value_evidence.metric_count"], c["evidence_refs"])


class Sha256IntegrityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.responses = _run_mock_probe()

    def test_sql_artifact_is_real_sha256_of_candidate_sql(self) -> None:
        sql_ok = [r for r in self.responses if r["route"] == "sql" and r["status"] == "success"]
        self.assertTrue(sql_ok, "缺少 SQL 成功样例")
        r = sql_ok[0]
        expected = hashlib.sha256(ARTIST_SQL.encode("utf-8")).hexdigest()
        gate_events = [e for e in r["trace"] if e["stage"] == "sql_safety_gate"]
        self.assertTrue(gate_events, "缺少 sql_safety_gate 轨迹事件")
        self.assertEqual(expected, gate_events[0]["artifact_sha256"])
        self.assertEqual(64, len(gate_events[0]["artifact_sha256"]))

    def test_no_trace_artifact_is_zero_padded(self) -> None:
        for r in self.responses:
            for event in r["trace"]:
                self.assertNotEqual("0" * 64, event["artifact_sha256"])
                self.assertRegex(event["artifact_sha256"], r"^[0-9a-f]{64}$")


class FrontendRenderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.data = _run_render_probe()
        cls.results = cls.data["results"]
        cls.attacks = cls.data["attacks"]
        cls.regressions = cls.data["regressions"]
        cls.source_checks = cls.data["source_checks"]

    def _by_case(self, name: str) -> dict:
        for r in self.results:
            if r["case"] == name:
                return r
        self.fail(f"未找到 case {name!r} 的渲染结果")

    def test_render_probe_returns_seven_rows(self) -> None:
        self.assertEqual(9, len(self.results))

    def test_sql_success_shows_sql_panel_and_hides_citation(self) -> None:
        r = self._by_case("sql_success")
        hidden = r["hidden"]
        self.assertFalse(hidden["sql-panel"])
        self.assertTrue(hidden["citation-panel"])
        self.assertEqual("accept", r["decision_action"])
        self.assertEqual("通过", r["result_status"])
        self.assertIn("SELECT COUNT(*)", r["sql_code"])
        self.assertEqual(4, r["trace_count"])

    def test_sql_malicious_is_rejected(self) -> None:
        r = self._by_case("sql_malicious")
        self.assertFalse(r["hidden"]["sql-panel"])
        self.assertEqual("reject", r["decision_action"])
        self.assertEqual("已拒绝", r["result_status"])
        self.assertIn("DELETE FROM", r["sql_code"])

    def test_rag_success_shows_citation_and_hides_sql(self) -> None:
        r = self._by_case("rag_success")
        self.assertTrue(r["hidden"]["sql-panel"])
        self.assertFalse(r["hidden"]["citation-panel"])
        self.assertEqual("accept", r["decision_action"])

    def test_rag_invalid_citation_is_refused(self) -> None:
        r = self._by_case("rag_invalid")
        self.assertEqual("reject", r["decision_action"])
        self.assertEqual("已拒绝", r["result_status"])

    def test_rag_parse_error_shows_reason_page_and_action(self) -> None:
        r = self._by_case("rag_parse_error")
        self.assertEqual("error", r["decision_action"])
        self.assertEqual("错误", r["result_status"])
        self.assertIn("原因", r["result_message"])
        self.assertIn("受影响页", r["result_message"])
        self.assertIn("处理方式", r["result_message"])

    def test_rag_citation_has_checked_mark_and_source_text(self) -> None:
        r = self._by_case("rag_success")
        self.assertEqual(1, len(r["citation_texts"]))
        text = r["citation_texts"][0]
        # 界面文案：ID/页码 + 源文逐字两级校验都通过才声称「源文逐字一致」
        self.assertIn("已校验", text)
        self.assertIn("源文逐字一致", text)
        self.assertIn("SALES-001", text)
        self.assertIn("第 1 页", text)
        # 源文准确：snippet 与 PDF 原文一致，不含编造内容
        self.assertIn("销售分析手册", text)
        self.assertIn("次月第一个工作日 18:00", text)
        self.assertNotIn("每月最后一个工作日", text)
        self.assertNotIn("部门考核", text)

    def test_source_text_comparison_accepts_original(self) -> None:
        """正例：mock 的 snippet 与 PDF 源文逐字一致。"""
        checks = self.source_checks
        self.assertEqual("SALES-001", checks["evidence_id"])
        self.assertTrue(checks["positive_original"])

    def test_source_text_comparison_is_not_vacuous(self) -> None:
        """反例：改事实 / 追加编造 / 改一字 / 串证据，四种破坏都必须被检出。

        这条是「校验非空转」的证明——如果 matchesSourceText 恒返回 true，
        这四个断言会立刻变红。
        """
        checks = self.source_checks
        self.assertFalse(checks["negative_wrong_fact"], "改事实未被检出")
        self.assertFalse(checks["negative_appended_fabrication"], "追加编造未被检出")
        self.assertFalse(checks["negative_single_char"], "改一字未被检出")
        self.assertFalse(checks["negative_cross_evidence"], "串证据文本未被检出")

    def test_source_text_comparison_boundaries_are_unknown_not_true(self) -> None:
        """边界：未知证据 ID、空片段都不可判定为「一致」（返回 null）。"""
        checks = self.source_checks
        self.assertIsNone(checks["boundary_unknown_id"])
        self.assertIsNone(checks["boundary_empty_snippet"])
        # 页码校验仍独立于文本校验生效
        self.assertTrue(checks["page_check_ok"])
        self.assertFalse(checks["page_check_wrong_page"])

    def test_every_manifest_entry_has_source_text_from_pdf(self) -> None:
        """清单 44 条证据每一条都带 PDF 源文正文，且能被逐字比对照回。"""
        manifest_url = _file_url(FRONTEND_ROOT / "rag-manifest.js")
        script = (
            'import { RAG_MANIFEST, matchesSourceText } from "' + manifest_url + '";'
            "const ids = Object.keys(RAG_MANIFEST);"
            "const missing = ids.filter((id) => !RAG_MANIFEST[id].source_text);"
            "const selfMatch = ids.filter((id) => matchesSourceText(id, RAG_MANIFEST[id].source_text) !== true);"
            "console.log(JSON.stringify({count: ids.length, missing, selfMatch}));"
        )
        result = _run_node(script, cwd=PROJECT_ROOT)
        if result.returncode != 0:
            raise RuntimeError("rag-manifest 探针执行失败：\n" + result.stderr)
        data = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(44, data["count"])
        self.assertEqual([], data["missing"], "存在没有源文正文的证据条目")
        self.assertEqual([], data["selfMatch"], "源文正文无法与自身逐字比对通过")

    def test_rag_manifest_citation_validation(self) -> None:
        manifest_url = _file_url(FRONTEND_ROOT / "rag-manifest.js")
        script = (
            'import { isValidCitation, RAG_MANIFEST } from "' + manifest_url + '";'
            "console.log(JSON.stringify({"
            "entry_count: Object.keys(RAG_MANIFEST).length,"
            'sales_001_p1: isValidCitation("SALES-001", 1),'
            'sales_001_p2: isValidCitation("SALES-001", 2),'
            'fake_999: isValidCitation("FAKE-999", 1),'
            'exp_003_p2: isValidCitation("EXP-003", 2),'
            'exp_003_p1: isValidCitation("EXP-003", 1),'
            "}));"
        )
        result = _run_node(script, cwd=PROJECT_ROOT)
        if result.returncode != 0:
            raise RuntimeError("rag-manifest 探针执行失败：\n" + result.stderr)
        data = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(44, data["entry_count"])
        self.assertTrue(data["sales_001_p1"])
        self.assertFalse(data["sales_001_p2"])
        self.assertFalse(data["fake_999"])
        self.assertTrue(data["exp_003_p2"])
        self.assertFalse(data["exp_003_p1"])

    def test_clarify_renders_options(self) -> None:
        r = self._by_case("clarify")
        self.assertFalse(r["hidden"]["clarify-panel"])
        self.assertIn("指标", r["clarify_title"])
        self.assertIn("数量（COUNT）", r["clarify_options"])
        self.assertIn("求和（SUM）", r["clarify_options"])

    def test_clarify_submit_closes_loop_with_sql_success(self) -> None:
        r = self._by_case("clarify_submit")
        self.assertFalse(r["hidden"]["sql-panel"])
        self.assertEqual("accept", r["decision_action"])
        self.assertIn("SELECT COUNT(*)", r["sql_code"])

    def test_first_turn_is_round_one_not_zero(self) -> None:
        r = self._by_case("sql_success")
        self.assertIn("第 1 轮", r["turn_meta"])

    def test_clarify_submit_advances_turn(self) -> None:
        r = self._by_case("clarify_submit")
        self.assertIn("第 2 轮", r["turn_meta"])

    def test_error_response_renders_error_state(self) -> None:
        r = self._by_case("error")
        self.assertEqual("error", r["decision_action"])
        self.assertEqual("错误", r["result_status"])

    def test_attack_regression_rejects_all_tamperings(self) -> None:
        self.assertEqual(9, len(self.attacks))
        for attack in self.attacks:
            self.assertEqual(
                attack["expect"], attack["got"],
                f"{attack['field']} 篡改应拒绝为 {attack['expect']}，实得 {attack['got']}",
            )
            self.assertEqual("error", attack["status"])

    def test_consecutive_queries_use_latest_turn_and_revision(self) -> None:
        q1 = self.regressions["consecutive_queries"]["q1"]
        q2 = self.regressions["consecutive_queries"]["q2"]
        # 第一次查询后推进到 turn=1/rev=1，第二次用该最新值并再推进到 turn=2/rev=2
        self.assertEqual((1, 1), (q1["turn"], q1["rev"]))
        self.assertEqual((2, 2), (q2["turn"], q2["rev"]))

    def test_five_round_progression_without_replay_or_stale(self) -> None:
        rounds = self.regressions["multi_round"]
        self.assertEqual(5, len(rounds))
        for r in rounds:
            # clarify 不推进，answer 推进 turn/rev 各 +1
            self.assertEqual(r["clarify_turn"] + 1, r["after_turn"], f"第 {r['round']} 轮 answer 应推进 turn_id")
            self.assertEqual(r["clarify_rev"] + 1, r["after_rev"], f"第 {r['round']} 轮 answer 应推进 state_revision")
            self.assertTrue(r["sql_ok"], f"第 {r['round']} 轮澄清后应回到 SQL 成功")
        # 5 轮后从 turn=0/rev=0 推进到 turn=5/rev=5
        self.assertEqual((5, 5), (rounds[-1]["after_turn"], rounds[-1]["after_rev"]))


class FrontendSourceSafetyTest(unittest.TestCase):
    def test_frontend_sources_do_not_reference_answer_fields(self) -> None:
        patterns = [
            r"\.\s*expected\b",
            r"\[\s*['\"]expected['\"]\s*\]",
            r"\.\s*gold\b",
            r"\[\s*['\"]gold['\"]\s*\]",
            r"\.\s*label\b",
            r"\[\s*['\"]label['\"]\s*\]",
        ]
        for path in (APP_JS_PATH, ADAPTER_PATH, MOCK_PATH):
            source = path.read_text(encoding="utf-8")
            for pattern in patterns:
                self.assertIsNone(
                    re.search(pattern, source),
                    f"{path.name} 以字段访问方式引用了答案字段（{pattern}）",
                )

    def test_index_html_declares_offline_boundaries(self) -> None:
        html = INDEX_HTML_PATH.read_text(encoding="utf-8")
        self.assertIn("契约 MOCK", html)
        self.assertIn("模型调用", html)

    def test_mock_usage_block_is_literal_offline(self) -> None:
        source = MOCK_PATH.read_text(encoding="utf-8")
        self.assertIn("model_calls: 0", source)


if __name__ == "__main__":
    unittest.main()
