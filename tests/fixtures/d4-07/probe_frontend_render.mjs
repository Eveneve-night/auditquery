"use strict";

/*
 * D4-07 C 侧前端渲染 + 交互探针（离线、无网络）。
 *
 * 用最小 DOM 桩通过动态 import 加载 ES module 化的 app.js（boot 时把渲染与
 * 请求函数挂到 window.__consoleTest），再驱动：
 *   1. 5 条 submit_query 场景（SQL 成功 / SQL 恶意 / RAG 成功 / RAG 拒答 / 澄清）
 *   2. 澄清提交（submit_clarification 回填 slot）
 *   3. 异常响应（unknown operation 触发 mock 的 error）
 * 输出每个 case 的面板显隐、状态文案、SQL 载荷、澄清绑定、轮次元信息与计数，
 * 供 Python 测试断言前端交互与展示是否正确。
 *
 * 本探针不引用 expected/gold/label，不发起模型或网络调用。
 */

import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const __dirname = dirname(fileURLToPath(import.meta.url));
const PROJECT_ROOT = resolve(__dirname, "..", "..", "..");

// ---- 最小 DOM 桩 ----
function makeNode(tag, className, text) {
  const el = {
    tagName: String(tag).toUpperCase(),
    className: className || "",
    textContent: text !== undefined ? String(text) : "",
    hidden: false,
    disabled: false,
    children: [],
    classList: { add() {}, remove() {} },
    append(child) { this.children.push(child); return this; },
    prepend(child) { this.children.unshift(child); return this; },
    addEventListener() {},
    setAttribute() {},
    scrollIntoView() {},
  };
  Object.defineProperty(el, "firstChild", {
    get() { return this.children[0] || null; },
  });
  el.removeChild = function (child) {
    const idx = this.children.indexOf(child);
    if (idx >= 0) this.children.splice(idx, 1);
    return child;
  };
  return el;
}

const registry = new Map();
function byId(id) {
  if (!registry.has(id)) registry.set(id, makeNode("div"));
  return registry.get(id);
}

const document = {
  getElementById: byId,
  createElement: (tag) => makeNode(tag),
  createTextNode: (text) => ({ nodeType: 3, textContent: String(text) }),
  querySelectorAll: () => [],
};

globalThis.document = document;
globalThis.window = { CONSOLE_DATA_SOURCE: undefined };

// ---- 加载 app.js（boot 会挂 window.__consoleTest） ----
const appUrl = "file:///" + resolve(PROJECT_ROOT, "src", "frontend", "d4-console", "app.js").replace(/\\/g, "/");
await import(appUrl);

const test = globalThis.window.__consoleTest;
if (!test || typeof test.render !== "function") {
  throw new Error("app.js 未暴露 __consoleTest，探针失败");
}

// ---- 加载契约 mock ----
const mockUrl = "file:///" + resolve(PROJECT_ROOT, "src", "frontend", "d4-console", "mock", "index.js").replace(/\\/g, "/");
const mock = (await import(mockUrl)).default;

// ---- 加载引用清单（源文逐字比对自检用）----
const manifestUrl =
  "file:///" +
  resolve(PROJECT_ROOT, "src", "frontend", "d4-console", "rag-manifest.js").replace(/\\/g, "/");
const { isValidCitation, matchesSourceText } = await import(manifestUrl);

function snapshotHidden() {
  const ids = [
    "decision-panel", "candidate-panel", "sql-panel", "citation-panel",
    "trace-panel", "cost-panel", "clarify-panel",
  ];
  const hidden = {};
  for (const id of ids) hidden[id] = test.byId(id).hidden;
  return hidden;
}

function textOf(id) {
  return test.byId(id).textContent;
}

function clarifyTitle() {
  const list = test.byId("clarify-list");
  const first = list.children[0];
  if (!first) return "";
  const title = first.children[0];
  return title ? title.textContent : "";
}

function turnMeta() {
  const list = test.byId("turn-list");
  const first = list.children[0];
  if (!first || !first.children[1]) return "";
  return first.children[1].textContent;
}

function clarifyOptions() {
  const list = test.byId("clarify-list");
  const card = list.children[0];
  if (!card) return [];
  const optsBox = card.children[2];
  if (!optsBox) return [];
  return optsBox.children.map((b) => b.textContent);
}

function reset() {
  test.state.turns = [];
  test.state.session = null;
  test.state.pending = null;
  test.state.busy = false;
  registry.clear();
}

function nodeText(node) {
  if (!node) return "";
  if (node.children && node.children.length) {
    return node.children.map(nodeText).join(" ");
  }
  return node.textContent || "";
}

function collect(tag, question) {
  return {
    case: tag,
    question,
    hidden: snapshotHidden(),
    decision_action: textOf("decision-action"),
    result_status: textOf("result-status"),
    sql_code: textOf("sql-code"),
    safety_badge: textOf("safety-badge"),
    clarify_title: clarifyTitle(),
    clarify_options: clarifyOptions(),
    turn_meta: turnMeta(),
    trace_count: test.byId("trace-list").children.length,
    usage_chip_count: test.byId("cost-list").children.length,
    warning_chip_count: test.byId("warning-list").children.length,
    result_message: nodeText(test.byId("result-message")),
    citation_texts: Array.from(test.byId("citation-list").children).map(nodeText),
  };
}

const results = [];

// 1. SQL 成功
reset();
await test.ask("统计艺术家数量");
results.push(collect("sql_success", "统计艺术家数量"));

// 2. SQL 恶意拦截
reset();
await test.ask("删除 Artist 表");
results.push(collect("sql_malicious", "删除 Artist 表"));

// 3. RAG 成功
reset();
await test.ask("月度经营报表的截止时间是什么时候");
results.push(collect("rag_success", "月度经营报表的截止时间是什么时候"));

// 4. RAG 拒答
reset();
await test.ask("公司健身房几点开门可以洗澡吗");
results.push(collect("rag_refused", "公司健身房几点开门可以洗澡吗"));

// 4.5 非法引用（伪造证据 → 失败关闭）
reset();
await test.ask("伪造证据");
results.push(collect("rag_invalid", "伪造证据"));

// 4.6 文档解析失败（原因/受影响页/处理方式）
reset();
await test.ask("这个 PDF 解析失败了");
results.push(collect("rag_parse_error", "这个 PDF 解析失败了"));

// 5. 澄清
reset();
await test.ask("hi");
results.push(collect("clarify", "hi"));

// 6. 澄清提交（选择 count 选项：value + 同一选项的 evidence_ref）
await test.submitClarification("count", "value_evidence.metric_count");
results.push(collect("clarify_submit", "count"));

// 7. 异常响应（unknown operation 触发 mock error）
reset();
const err = await mock.query({
  operation: "unknown",
  session_id: "session.probe",
  request_id: "req.probe.error",
  turn_id: 1,
  state_revision: 0,
});
test.render(test.normalizeResponse(err));
results.push(collect("error", "（unknown operation）"));

// ---- 攻击回归（D4-05 失败关闭）----
const attacks = [];
const attackCases = [
  ["session_id", "session.tampered", "clarification_session_mismatch"],
  ["request_id", "request.tampered", "clarification_request_mismatch"],
  ["turn_id", 99, "clarification_turn_mismatch"],
  ["state_revision", 99, "clarification_answer_stale"],
  ["question_id", "question.tampered", "clarification_question_mismatch"],
  ["slot_id", "slot.tampered", "clarification_cross_slot_update"],
  ["values", ["avg"], "clarification_value_not_offered"],
  ["evidence_refs", ["value_evidence.metric_sum"], "invalid_answer_evidence_refs"],
];
for (let i = 0; i < attackCases.length; i++) {
  const [field, replacement, expect] = attackCases[i];
  const sid = "session.atk." + i;
  const rid = "request.atk." + i;
  const c = await mock.query({
    schema_version: "2.0.0", operation: "submit_query",
    session_id: sid, request_id: rid, turn_id: 1, state_revision: 0, query: "hi",
  });
  const p = c.pending_question;
  const base = {
    schema_version: "2.0.0", operation: "submit_clarification",
    answer_id: "answer.atk." + i,
    session_id: p.session_id, request_id: p.request_id, turn_id: p.turn_id,
    state_revision: p.state_revision, question_id: p.question_id, slot_id: p.slot_id,
    values: ["count"], evidence_refs: ["value_evidence.metric_count"],
  };
  base[field] = replacement;
  const resp = await mock.query(base);
  attacks.push({ field, expect, got: resp.error ? resp.error.code : null, status: resp.status });
}
// 重放攻击：合法消费一次，再重放相同 answer_id
const c2 = await mock.query({
  schema_version: "2.0.0", operation: "submit_query",
  session_id: "session.replay", request_id: "request.replay", turn_id: 1, state_revision: 0, query: "hi",
});
const p2 = c2.pending_question;
const legit = {
  schema_version: "2.0.0", operation: "submit_clarification",
  answer_id: "answer.replay.1",
  session_id: p2.session_id, request_id: p2.request_id, turn_id: p2.turn_id,
  state_revision: p2.state_revision, question_id: p2.question_id, slot_id: p2.slot_id,
  values: ["count"], evidence_refs: ["value_evidence.metric_count"],
};
const firstSubmit = await mock.query(legit);
const replay = await mock.query(legit);
attacks.push({
  field: "answer_id", expect: "clarification_answer_replayed",
  got: replay.error ? replay.error.code : null,
  status: replay.status,
  first_status: firstSubmit.status,
});

// ---- 多轮状态推进回归 ----
// 回归 1：连续两个普通查询，第二次用服务端最新 turn_id/state_revision
reset();
await test.ask("统计艺术家数量");
const q1 = { turn: test.state.session.turn_id, rev: test.state.session.state_revision };
await test.ask("统计艺术家数量");
const q2 = { turn: test.state.session.turn_id, rev: test.state.session.state_revision };

// 回归 2：5 轮 query/clarification/answer 状态推进（无重放/过期/串轮）
reset();
const rounds = [];
for (let i = 0; i < 5; i++) {
  await test.ask("hi");
  const clarify = { turn: test.state.session.turn_id, rev: test.state.session.state_revision };
  await test.submitClarification("count", "value_evidence.metric_count");
  const after = { turn: test.state.session.turn_id, rev: test.state.session.state_revision };
  rounds.push({
    round: i + 1,
    clarify_turn: clarify.turn,
    clarify_rev: clarify.rev,
    after_turn: after.turn,
    after_rev: after.rev,
    sql_ok: textOf("sql-code").includes("SELECT COUNT(*)"),
  });
}

const regressions = { consecutive_queries: { q1, q2 }, multi_round: rounds };

// ---- 源文逐字比对自检（正例 + 反例，证明校验非空转）----
// 取 mock 的 rag 成功响应里的真实 snippet 作正例，再对它做四种破坏作反例。
const ragReq = {
  schema_version: "2.0.0",
  operation: "submit_query",
  session_id: "session.probe",
  request_id: "req.probe.source",
  turn_id: 0,
  state_revision: 0,
  query: "月度经营报表的截止时间是什么时候",
};
const ragResp = await mock.query(ragReq);
const srcSnippet = ragResp.citations[0].snippet;
const srcId = ragResp.citations[0].evidence_id;

const source_checks = {
  evidence_id: srcId,
  positive_original: matchesSourceText(srcId, srcSnippet),
  // 反例 1：改事实（次月第一个工作日 → 每月最后一个工作日）
  negative_wrong_fact: matchesSourceText(
    srcId,
    srcSnippet.replace("次月第一个工作日", "每月最后一个工作日")
  ),
  // 反例 2：追加编造内容
  negative_appended_fabrication: matchesSourceText(
    srcId,
    srcSnippet + "逾期未交计入下月并影响部门考核。"
  ),
  // 反例 3：改一个字（不静默覆盖 → 静默覆盖）
  negative_single_char: matchesSourceText(
    srcId,
    srcSnippet.replace("不静默覆盖", "静默覆盖")
  ),
  // 反例 4：片段取自另一条证据的源文
  negative_cross_evidence: matchesSourceText(srcId, "销售分析的北极星指标是毛利率"),
  // 边界：未知证据 ID 不可判定为一致（应为 null）
  boundary_unknown_id: matchesSourceText("NOPE-999", srcSnippet),
  // 边界：空片段不可判定（应为 null）
  boundary_empty_snippet: matchesSourceText(srcId, ""),
  // 页码校验仍独立生效
  page_check_ok: isValidCitation(srcId, ragResp.citations[0].page),
  page_check_wrong_page: isValidCitation(srcId, 99),
};

console.log(JSON.stringify({ results, attacks, regressions, source_checks }));
