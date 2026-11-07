"use strict";

/*
 * D4-07 C 侧前端（d4-console）。
 *
 * 数据源抽象：默认使用契约 mock（src/frontend/d4-console/mock/index.js），
 * 通过 window.CONSOLE_DATA_SOURCE 可替换为 B 侧在线 API 实现，二者实现同一
 * `query(request)` -> Promise<response> 接口，request 满足 online-request.v1、
 * response 满足 online-response.v1（见 configs/schemas/d4-07/）。
 *
 * 请求构建与响应归一化统一走 adapter（./adapter.js），渲染层只消费归一化
 * 视图，不直接依赖某版契约字段名。
 *
 * 本骨架不引用 expected / gold / label 答案字段，不发起真实模型或网络调用。
 */

import {
  newSession,
  buildSubmitQuery,
  buildSubmitClarification,
  normalizeResponse,
} from "./adapter.js";
import { isValidCitation, matchesSourceText } from "./rag-manifest.js";

const byId = (id) => document.getElementById(id);

const state = {
  busy: false,
  turns: [],
  session: null, // { session_id, turn_id, state_revision }
  pending: null, // pending_question 绑定信息（澄清提交时回填）
};

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = String(text);
  return element;
}

function formatScore(value) {
  return value === null || value === undefined ? "—" : Number(value).toFixed(6);
}

function clear(element) {
  while (element.firstChild) element.removeChild(element.firstChild);
}

function setBusy(busy) {
  state.busy = busy;
  byId("ask-submit").disabled = busy;
  byId("question-input").disabled = busy;
  byId("service-state").textContent = busy ? "处理中…" : "就绪";
  byId("service-state").className = `status-pill ${busy ? "" : "status-pill--idle"}`;
}

async function resolveDataSource() {
  if (window.CONSOLE_DATA_SOURCE && window.CONSOLE_DATA_SOURCE.query) {
    return window.CONSOLE_DATA_SOURCE;
  }
  const mod = await import("./mock/index.js");
  return mod.default;
}

function ensureSession() {
  if (!state.session) state.session = newSession();
  return state.session;
}

function renderTurn(text, kind) {
  const list = byId("turn-list");
  const item = node("article", `turn-item turn-item--${kind}`);
  item.append(node("div", "turn-item__q", text));
  item.append(
    node(
      "div",
      "turn-item__meta",
      kind === "user" ? "你 · 第 " + state.turns.length + " 轮" : "系统澄清"
    )
  );
  list.prepend(item);
}

function rememberTurn(role, text) {
  state.turns.push({ role, text });
  renderTurn(text, role);
}

function applyRevision(response) {
  if (response && typeof response.state_revision === "number") {
    state.session.state_revision = response.state_revision;
  }
  if (response && typeof response.turn_id === "number") {
    state.session.turn_id = response.turn_id;
  }
}

async function ask(question) {
  if (state.busy || !question || !question.trim()) return;
  const text = question.trim();
  const session = ensureSession();
  rememberTurn("user", text);
  setBusy(true);
  hideResult();
  try {
    const source = await resolveDataSource();
    const response = await source.query(buildSubmitQuery(session, text));
    applyRevision(response);
    render(normalizeResponse(response));
  } catch (error) {
    renderErrorView({
      code: (error && error.code) || "client_error",
      retryable: true,
      message: (error && error.message) || "请求失败",
    });
  } finally {
    setBusy(false);
  }
}

async function submitClarification(value, evidenceRef) {
  if (state.busy || !state.pending) return;
  const pending = state.pending;
  const text = String(value);
  rememberTurn("user", text);
  setBusy(true);
  hideResult();
  try {
    const source = await resolveDataSource();
    const response = await source.query(buildSubmitClarification(pending, value, evidenceRef));
    applyRevision(response);
    render(normalizeResponse(response));
  } catch (error) {
    renderErrorView({
      code: (error && error.code) || "client_error",
      retryable: true,
      message: (error && error.message) || "请求失败",
    });
  } finally {
    setBusy(false);
  }
}

function hideResult() {
  byId("decision-panel").hidden = true;
  byId("candidate-panel").hidden = true;
  byId("sql-panel").hidden = true;
  byId("citation-panel").hidden = true;
  byId("trace-panel").hidden = true;
  byId("cost-panel").hidden = true;
  byId("clarify-panel").hidden = true;
}

function showMainResult() {
  byId("decision-panel").hidden = false;
  byId("candidate-panel").hidden = false;
  byId("trace-panel").hidden = false;
  byId("cost-panel").hidden = false;
}

/** 统一渲染入口：按 status/action 分发到澄清、错误或主结果。 */
function render(view) {
  hideResult();
  const action = view.decision && view.decision.action;
  if (view.status === "clarification_required" || action === "clarify") {
    renderClarification(view);
    return;
  }
  if (view.status === "error") {
    renderErrorView(view.error || { code: "error", retryable: false, message: view.message });
    return;
  }
  renderMainResult(view);
}

function renderMainResult(view) {
  showMainResult();
  state.pending = null; // 非澄清响应，清除陈旧的待澄清绑定
  byId("clarify-panel").hidden = true;
  const status = byId("result-status");
  status.textContent = view.status === "success" ? "通过" : "已拒绝";
  status.className = `status-pill ${view.status === "success" ? "status-pill--ok" : "status-pill--danger"}`;
  byId("result-message").textContent = view.message;

  if (view.decision) {
    byId("decision-action").textContent = view.decision.action;
    byId("decision-reason").textContent = view.decision.reason_code;
    byId("score-kind").textContent = view.decision.score_kind;
    byId("score-value").textContent = formatScore(view.decision.score);
    renderThresholds(view.decision.thresholds);
  }

  renderCandidates(view.candidates);
  renderSql(view.sql);
  renderCitations(view.citations, view.route);
  renderTrace(view.trace);
  renderDisclosure(view.usage, view.error, view.warnings);
}

function renderClarification(view) {
  hideResult();
  const panel = byId("clarify-panel");
  panel.hidden = false;
  const list = byId("clarify-list");
  clear(list);

  const pending = view.pending;
  state.pending = pending || null;

  const card = node("div", "clarify-card");
  if (pending) {
    card.append(node("div", "clarify-card__title", pending.question_text || "需要补充信息"));
  } else {
    card.append(node("div", "clarify-card__title", "需要补充信息"));
  }
  card.append(node("div", "clarify-card__desc", view.message || "请选择以下选项。"));

  // v2：从服务端 options 渲染可选项，提交所选 value + 同一选项的 evidence_ref
  if (pending && pending.options && pending.options.length) {
    const opts = node("div", "clarify-card__options");
    for (const option of pending.options) {
      const btn = node("button", "clarify-option");
      btn.textContent = option.display_name;
      btn.addEventListener("click", () => submitClarification(option.value, option.evidence_ref));
      opts.append(btn);
    }
    card.append(opts);
  }
  list.append(card);

  renderTurn(view.message || "需要补充信息", "clarify");
}

function renderErrorView(error) {
  hideResult();
  showMainResult();
  byId("clarify-panel").hidden = true;
  byId("result-status").textContent = "错误";
  byId("result-status").className = "status-pill status-pill--danger";
  const messageEl = byId("result-message");
  clear(messageEl);
  const msg = (error && (error.message || error.code)) || "unknown_error";
  if (error && error.code === "document_parse_failed") {
    // 文档解析失败：结构化展示原因 / 受影响页 / 处理方式（US-02）
    for (const part of msg.split("。").map((s) => s.trim()).filter(Boolean)) {
      messageEl.append(node("span", "parse-detail", part));
    }
  } else {
    messageEl.textContent = msg;
  }
  byId("decision-action").textContent = "error";
  byId("decision-reason").textContent = "—";
  byId("score-kind").textContent = "—";
  byId("score-value").textContent = "—";
  clear(byId("threshold-list"));
  clear(byId("candidate-list"));
  byId("sql-panel").hidden = true;
  byId("citation-panel").hidden = true;
  clear(byId("trace-list"));
  clear(byId("cost-list"));
  clear(byId("warning-list"));
}

function renderThresholds(thresholds) {
  const list = byId("threshold-list");
  clear(list);
  if (!thresholds) {
    list.append(node("span", "chip", "无适用阈值"));
    return;
  }
  let any = false;
  for (const [key, value] of Object.entries(thresholds)) {
    if (value === null || value === undefined) continue;
    any = true;
    const chip = node("span", "chip");
    chip.append(node("strong", "", key));
    chip.append(document.createTextNode(` · ${formatScore(value)}`));
    list.append(chip);
  }
  if (!any) list.append(node("span", "chip", "无适用阈值"));
}

function renderCandidates(candidates) {
  const list = byId("candidate-list");
  clear(list);
  if (!candidates || !candidates.length) {
    list.append(node("p", "result-message", "没有候选证据。"));
    return;
  }
  for (const candidate of candidates) {
    const item = node("article", "stack-item");
    const top = node("div", "stack-item__top");
    top.append(node("strong", "", candidate.display_name));
    top.append(node("small", "", formatScore(candidate.score)));
    item.append(top);
    item.append(node("small", "", `${candidate.candidate_kind} · ${candidate.candidate_id}`));
    item.append(node("div", "refs", (candidate.evidence_refs || []).join(" · ") || "无引用"));
    list.append(item);
  }
}

function renderSql(sql) {
  const panel = byId("sql-panel");
  panel.hidden = !sql;
  if (!sql) return;
  byId("sql-code").textContent = sql.candidate_sql;
  const badge = byId("safety-badge");
  const ok = sql.safety_ok;
  badge.textContent = ok ? "SafetyGate 通过" : "SafetyGate 拒绝";
  badge.className = `status-pill ${ok ? "" : "status-pill--danger"}`;
  const meta = byId("sql-meta");
  clear(meta);
  for (const value of [
    `表：${(sql.tables || []).join(", ")}`,
    `执行尝试：${sql.execution_attempts}`,
    `结果行：${sql.row_count}`,
  ]) meta.append(node("span", "chip", value));
  const table = byId("result-table");
  clear(table);
  if (!sql.columns || !sql.columns.length) return;
  const thead = node("thead");
  const headerRow = node("tr");
  for (const column of sql.columns) headerRow.append(node("th", "", column));
  thead.append(headerRow);
  table.append(thead);
  const tbody = node("tbody");
  for (const row of sql.rows || []) {
    const tr = node("tr");
    for (const value of row) tr.append(node("td", "", value === null ? "NULL" : value));
    tbody.append(tr);
  }
  table.append(tbody);
}

function renderCitations(citations, route) {
  const panel = byId("citation-panel");
  panel.hidden = route !== "rag";
  if (route !== "rag") return;
  const list = byId("citation-list");
  clear(list);
  if (!citations || !citations.length) {
    list.append(node("p", "result-message", "没有证据越过固定检索门槛。"));
    return;
  }
  for (const citation of citations) {
    const item = node("article", "citation");
    const top = node("div", "citation__top");
    top.append(node("strong", "", `${citation.document_title} · 第 ${citation.page} 页`));
    top.append(node("small", "", formatScore(citation.score)));
    item.append(top);
    const meta = citation.pdf_path
      ? `${citation.evidence_id} · ${citation.pdf_path}`
      : `${citation.evidence_id} · ${citation.document_id}`;
    item.append(node("small", "", meta));
    // 两级机器校验，语义严格区分：
    // idOk  = 证据 ID 存在 + 页码一致（不比对文本）
    // textOk = 引用片段与 PDF 源文逐字一致（true 一致 / false 不一致 / null 无法判定）
    const idOk = isValidCitation(citation.evidence_id, citation.page);
    const textOk = idOk ? matchesSourceText(citation.evidence_id, citation.snippet) : null;
    let markText;
    let markBad = false;
    if (!idOk) {
      markText = "✗ 证据 ID / 页码校验未通过";
      markBad = true;
    } else if (textOk === true) {
      markText = "✓ 已校验 · 证据 ID、页码与源文逐字一致";
    } else if (textOk === false) {
      markText = "✗ 源文逐字校验未通过（片段与 PDF 原文不一致）";
      markBad = true;
    } else {
      markText = "— 源文缺失，无法逐字校验";
      markBad = true;
    }
    item.append(
      node(
        "small",
        markBad ? "citation__verified citation__verified--bad" : "citation__verified",
        markText
      )
    );
    // 引用上下文：点击展开 / 收起完整片段
    const snippet = node("p", "citation__snippet", citation.snippet);
    snippet.title = "点击查看 / 收起完整上下文";
    item.append(snippet);
    item.addEventListener("click", () => snippet.classList.toggle("is-expanded"));
    list.append(item);
  }
}

function renderTrace(trace) {
  const list = byId("trace-list");
  clear(list);
  for (const event of trace || []) {
    const item = node("li");
    item.append(node("span", "timeline__index", event.sequence));
    const body = node("div", "timeline__body");
    body.append(node("strong", "", `${event.stage} · ${event.action || "记录"}`));
    body.append(node("small", "", event.reason_code || "无额外理由"));
    body.append(node("small", "", `artifact ${String(event.artifact_sha256 || "").slice(0, 16)}…`));
    if (event.evidence_refs && event.evidence_refs.length) {
      body.append(node("div", "refs", event.evidence_refs.join(" · ")));
    }
    item.append(body);
    list.append(item);
  }
}

function renderDisclosure(usage, error, warnings) {
  const cost = byId("cost-list");
  clear(cost);
  if (usage) {
    for (const [key, value] of [
      ["model_calls", usage.model_calls],
      ["input_tokens", usage.input_tokens],
      ["output_tokens", usage.output_tokens],
      ["estimated_cost_cny", usage.estimated_cost_cny],
      ["elapsed_ms", usage.elapsed_ms],
      ["stopped_reason", usage.stopped_reason],
    ]) {
      const chip = node("span", "chip");
      chip.append(node("strong", "", key));
      chip.append(document.createTextNode(` · ${value === null || value === undefined ? "—" : value}`));
      cost.append(chip);
    }
  }
  if (error) {
    const chip = node("span", "chip chip--danger");
    chip.append(node("strong", "", "error"));
    chip.append(document.createTextNode(` · ${error.code}${error.retryable ? "（可重试）" : ""}`));
    cost.append(chip);
  }
  const warn = byId("warning-list");
  clear(warn);
  for (const w of warnings || []) {
    warn.append(node("span", "chip chip--warn", w));
  }
}

function bind() {
  byId("ask-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const input = byId("question-input");
    ask(input.value);
    input.value = "";
  });

  // 侧栏导航：点击平滑滚动定位 + 激活态切换（不干预表单与数据流）
  document.querySelectorAll(".nav-item").forEach((item) => {
    item.addEventListener("click", () => {
      document.querySelectorAll(".nav-item").forEach((n) => n.classList.remove("is-active"));
      item.classList.add("is-active");
      const target = document.getElementById(item.dataset.target);
      if (target) target.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  });
}

function boot() {
  bind();
  setBusy(false);
  byId("conn-mode").textContent =
    window.CONSOLE_DATA_SOURCE && window.CONSOLE_DATA_SOURCE.query ? "数据源：自定义" : "数据源：mock";

  // 离线探针钩子：暴露渲染与请求函数供 tests/fixtures/d4-07/probe_frontend_render.mjs
  // 断言，不暴露任何 expected / gold / label 答案字段。
  if (typeof window !== "undefined") {
    window.__consoleTest = {
      render,
      renderClarification,
      renderErrorView,
      ask,
      submitClarification,
      newSession,
      buildSubmitQuery,
      buildSubmitClarification,
      normalizeResponse,
      formatScore,
      byId,
      state,
    };
  }
}

boot();
