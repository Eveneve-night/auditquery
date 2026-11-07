"use strict";

/*
 * D5-04 C 侧前端：访客页（src/frontend/d5-console/guest.js）。
 *
 * 任务卡要求：访客页保留提问、澄清、SQL/引用/轨迹，默认隐藏不必要技术字段；
 * 支持加载 / 空 / 失败三态与中文提示。
 *
 * 边界：
 * - 会话令牌与会话 ID 只存在于内存，不写 localStorage / sessionStorage / Cookie；
 * - 页面不判定权限，401/403/409 一律按服务端返回展示；
 * - 引用片段的机器校验复用 D4-07 的 rag-manifest.js：证据 ID 存在 + 页码一致
 *   （isValidCitation），再对片段与 PDF 源文做逐字比对（matchesSourceText）。
 *   matchesSourceText 已随 PR #62 合并进 main，语义与 D4-07 前端一致：
 *   true 逐字一致 / false 不一致 / null 源文缺失无法判定。
 */

import { byId, node, clear, setHidden } from "./dom.js";
import { isValidCitation, matchesSourceText } from "../d4-console/rag-manifest.js";
import { citationVerification } from "./citation-validation.js";

const IDS = {
  root: "guest-root",
  session: "guest-session",
  form: "guest-ask-form",
  input: "guest-question",
  submit: "guest-submit",
  status: "guest-status",
  empty: "guest-empty",
  error: "guest-error",
  result: "guest-result",
  resultStatus: "guest-result-status",
  message: "guest-result-message",
  table: "guest-result-table",
  clarify: "guest-clarify",
  clarifyTitle: "guest-clarify-title",
  clarifyOptions: "guest-clarify-options",
  citations: "guest-citations",
  techToggle: "guest-tech-toggle",
  tech: "guest-tech",
  sql: "guest-sql",
  thresholds: "guest-thresholds",
  trace: "guest-trace",
  usage: "guest-usage",
  turns: "guest-turns",
};

function formatNumber(value) {
  return value === null || value === undefined ? "—" : Number(value).toFixed(6).replace(/0+$/, "").replace(/\.$/, "");
}

export function renderGuestShell(root) {
  clear(root);
  // 只打标记，不改容器 id：#view 是路由容器，改写 id 会让后续 getElementById("view") 落空
  root.setAttribute("data-view", IDS.root);

  // 会话栏
  const session = node("div", "guest-bar");
  session.id = IDS.session;
  root.append(session);

  // 错误横幅
  const error = node("div", "error-box");
  error.id = IDS.error;
  setHidden(error, true);
  root.append(error);

  // 消息流：空态 → 历史轮次 → 最新答案 → 澄清卡片
  const thread = node("div", "thread");

  const empty = node("p", "empty-state", "还没有提问。输入一个问题开始，答案会附上证据与技术细节。");
  empty.id = IDS.empty;
  thread.append(empty);

  const turns = node("div", "turn-list");
  turns.id = IDS.turns;
  thread.append(turns);

  const result = node("section", "result-card");
  result.id = IDS.result;
  setHidden(result, true);
  result.append(buildResultBody());
  thread.append(result);

  const clarify = node("section", "clarify-card");
  clarify.id = IDS.clarify;
  setHidden(clarify, true);
  const clarifyTitle = node("div", "clarify-card__title", "需要补充信息");
  clarifyTitle.id = IDS.clarifyTitle;
  const clarifyOptions = node("div", "clarify-card__options");
  clarifyOptions.id = IDS.clarifyOptions;
  clarify.append(clarifyTitle);
  clarify.append(clarifyOptions);
  thread.append(clarify);

  root.append(thread);

  // 底部输入区：提问表单 + 状态
  const composer = node("div", "composer");
  const form = node("form", "ask-form");
  form.id = IDS.form;
  const input = node("input", "ask-form__input");
  input.id = IDS.input;
  input.setAttribute("type", "text");
  input.setAttribute("placeholder", "输入你的问题，例如：统计艺术家数量");
  input.setAttribute("aria-label", "问题输入框");
  const submit = node("button", "ask-form__submit", "提问");
  submit.id = IDS.submit;
  submit.setAttribute("type", "submit");
  form.append(input);
  form.append(submit);
  composer.append(form);

  const status = node("div", "status-pill status-pill--idle", "就绪");
  status.id = IDS.status;
  composer.append(status);

  root.append(composer);

  return IDS;
}

function buildResultBody() {
  const frag = document.createElement("div");
  frag.className = "result-body";

  const head = node("div", "result-head");
  const statusPill = node("span", "status-pill", "—");
  statusPill.id = IDS.resultStatus;
  head.append(statusPill);
  frag.append(head);

  const message = node("p", "result-message", "");
  message.id = IDS.message;
  frag.append(message);

  const table = node("table", "result-table");
  table.id = IDS.table;
  frag.append(table);

  const citations = node("div", "citation-list");
  citations.id = IDS.citations;
  frag.append(citations);

  const toggle = node("button", "tech-toggle", "展开技术细节（SQL / 轨迹 / 用量）");
  toggle.id = IDS.techToggle;
  toggle.setAttribute("type", "button");
  frag.append(toggle);

  // 技术细节默认折叠：访客首屏只见答案与证据
  const tech = node("div", "tech-panel");
  tech.id = IDS.tech;
  setHidden(tech, true);
  tech.append(node("h4", "", "候选 SQL"));
  const sql = node("pre", "code-block", "");
  sql.id = IDS.sql;
  tech.append(sql);
  tech.append(node("h4", "", "判定阈值"));
  const thresholds = node("div", "chip-row");
  thresholds.id = IDS.thresholds;
  tech.append(thresholds);
  tech.append(node("h4", "", "解释轨迹"));
  const trace = node("ol", "timeline");
  trace.id = IDS.trace;
  tech.append(trace);
  tech.append(node("h4", "", "用量"));
  const usage = node("div", "chip-row");
  usage.id = IDS.usage;
  tech.append(usage);
  frag.append(tech);

  toggle.addEventListener("click", () => {
    setHidden(tech, !tech.hidden);
    toggle.textContent = tech.hidden ? "展开技术细节（SQL / 轨迹 / 用量）" : "收起技术细节";
  });

  return frag;
}

// 澄清按钮回调需要 ctx；用模块级引用在每次提问前刷新（不持久化任何令牌）
let ctxOfLastAsk = null;

export function bindGuest(ctx) {
  const form = byId(IDS.form);
  if (!form) return;
  // 每次 renderGuestShell 都会 clear(root) 重建表单，必须重新绑定新元素。
  // 不能用一个模块级「已绑定」标志跳过，否则切换回访客页后表单无 submit 监听。
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const input = byId(IDS.input);
    const question = input ? input.value : "";
    if (input) input.value = "";
    guestAsk(ctx, question);
  });
}

function setStatus(text, busy) {
  const status = byId(IDS.status);
  if (status) {
    status.textContent = text;
    status.className = `status-pill ${busy ? "" : "status-pill--idle"}`;
  }
  const submit = byId(IDS.submit);
  if (submit) submit.disabled = Boolean(busy);
  const input = byId(IDS.input);
  if (input) input.disabled = Boolean(busy);
}

function showError(error) {
  const box = byId(IDS.error);
  if (!box) return;
  clear(box);
  if (!error) {
    setHidden(box, true);
    return;
  }
  setHidden(box, false);
  const code = (error && error.code) || "internal_error";
  const message = (error && error.message) || "操作失败。";
  box.append(node("strong", "", `请求未成功（${code}）`));
  box.append(node("span", "", message));
  box.append(node("span", "", error && error.retryable ? "可以稍后重试。" : "该错误不可重试，请调整输入后重新提交。"));
}

export async function ensureVisitorSession(ctx) {
  const visitor = ctx.state.visitor;
  if (visitor.sessionId) return visitor;
  const response = await ctx.client.createVisitorSession();
  visitor.sessionId = response.session_id;
  visitor.turnId = 0;
  visitor.stateRevision = response.state_revision;
  visitor.expiresAt = response.expires_at;
  renderSessionMeta(ctx);
  return visitor;
}

function renderSessionMeta(ctx) {
  const visitor = ctx.state.visitor;
  const box = byId(IDS.session);
  if (!box) return;
  clear(box);
  if (!visitor.sessionId) {
    box.append(node("span", "chip", "会话未创建"));
    return;
  }
  box.append(node("span", "chip", `会话 ${visitor.sessionId}`));
  box.append(node("span", "chip", `轮次 ${visitor.turnId}`));
  box.append(node("span", "chip", `状态版本 ${visitor.stateRevision}`));
  box.append(node("span", "chip", `过期时间 ${String(visitor.expiresAt || "—").replace("T", " ").slice(0, 19)}`));
}

function renderTurns(ctx) {
  const list = byId(IDS.turns);
  if (!list) return;
  clear(list);
  const turns = ctx.state.visitor.turns;
  if (!turns.length) return;
  for (const turn of turns) {
    const item = node("div", `turn-item turn-item--${turn.role}`);
    item.append(node("span", "turn-item__q", turn.text));
    item.append(node("small", "turn-item__meta", turn.role === "user" ? `你 · 第 ${turn.turn} 轮` : "系统"));
    list.append(item);
  }
}

function renderCitations(citations, warnings = []) {
  const box = byId(IDS.citations);
  if (!box) return;
  clear(box);
  if (!citations || !citations.length) {
    setHidden(box, true);
    return;
  }
  setHidden(box, false);
  for (const [index, citation] of citations.entries()) {
    const item = node("article", "citation");
    item.append(node("strong", "", `[${index + 1}] ${citation.document_title} · 第 ${citation.page} 页`));
    item.append(node("small", "", citation.evidence_id));
    // 机器校验：先证据 ID 存在 + 页码一致（isValidCitation），
    // 再片段与 PDF 源文逐字比对（matchesSourceText，PR #62 已合并）。
    const idOk = isValidCitation(citation.evidence_id, citation.page);
    const textOk = idOk ? matchesSourceText(citation.evidence_id, citation.snippet) : null;
    let mark;
    let bad = false;
    if (String(citation.document_id || "").startsWith("doc.")) {
      const verification = citationVerification(citation, warnings);
      mark = verification.label;
      bad = !verification.valid;
    } else if (!idOk) {
      mark = "✗ 证据 ID / 页码校验未通过";
      bad = true;
    } else if (textOk === true) {
      mark = "✓ 已校验 · 证据 ID、页码与源文逐字一致";
    } else if (textOk === false) {
      mark = "✗ 源文逐字校验未通过";
      bad = true;
    } else {
      mark = "— 源文缺失，无法逐字校验";
      bad = true;
    }
    item.append(
      node("small", bad ? "citation__verified citation__verified--bad" : "citation__verified", mark)
    );
    item.append(node("p", "citation__snippet", citation.snippet));
    box.append(item);
  }
}

function renderTech(payload) {
  const sqlBox = byId(IDS.sql);
  if (sqlBox) {
    sqlBox.textContent = payload.sql ? payload.sql.candidate_sql : "本次回答没有生成 SQL。";
  }
  const thresholds = byId(IDS.thresholds);
  if (thresholds) {
    clear(thresholds);
    const values = (payload.decision && payload.decision.thresholds) || {};
    const entries = Object.entries(values).filter(([, v]) => v !== null && v !== undefined);
    if (!entries.length) {
      thresholds.append(node("span", "chip", "无适用阈值"));
    } else {
      for (const [key, value] of entries) {
        thresholds.append(node("span", "chip", `${key} · ${formatNumber(value)}`));
      }
    }
  }
  const trace = byId(IDS.trace);
  if (trace) {
    clear(trace);
    for (const event of payload.trace || []) {
      const item = node("li");
      item.append(node("strong", "", `${event.sequence}. ${event.stage} · ${event.action || "记录"}`));
      item.append(node("small", "", event.reason_code || "无额外理由"));
      trace.append(item);
    }
  }
  const usage = byId(IDS.usage);
  if (usage) {
    clear(usage);
    const u = payload.usage || {};
    for (const [key, value] of [
      ["model_calls", u.model_calls],
      ["input_tokens", u.input_tokens],
      ["output_tokens", u.output_tokens],
      ["estimated_cost_cny", u.estimated_cost_cny],
      ["elapsed_ms", u.elapsed_ms],
    ]) {
      usage.append(node("span", "chip", `${key} · ${value === null || value === undefined ? "—" : value}`));
    }
  }
}

function renderResultTable(payload) {
  const table = byId(IDS.table);
  if (!table) return;
  clear(table);
  const sql = payload.sql;
  if (!sql || !sql.columns || !sql.columns.length) {
    setHidden(table, true);
    return;
  }
  setHidden(table, false);
  const thead = document.createElement("thead");
  const headerRow = node("tr");
  for (const column of sql.columns) headerRow.append(node("th", "", column));
  thead.append(headerRow);
  table.append(thead);
  const tbody = document.createElement("tbody");
  for (const row of sql.rows || []) {
    const tr = node("tr");
    for (const value of row) tr.append(node("td", "", value === null ? "NULL" : value));
    tbody.append(tr);
  }
  table.append(tbody);
}

function renderClarify(payload) {
  const panel = byId(IDS.clarify);
  const optionsBox = byId(IDS.clarifyOptions);
  const title = byId(IDS.clarifyTitle);
  const pending = payload && payload.pending_question;
  if (!pending) {
    setHidden(panel, true);
    return null;
  }
  setHidden(panel, false);
  if (title) title.textContent = pending.question_text || "需要补充信息";
  if (optionsBox) {
    clear(optionsBox);
    for (const option of pending.options || []) {
      const button = node("button", "clarify-option", option.display_name);
      button.setAttribute("type", "button");
      button.addEventListener("click", () => {
        guestClarify(ctxOfLastAsk, option.value, option.evidence_ref);
      });
      optionsBox.append(button);
    }
  }
  return pending;
}

export async function guestAsk(ctx, question) {
  const text = String(question || "").trim();
  if (!text) return null;
  ctxOfLastAsk = ctx;
  setStatus("处理中…", true);
  showError(null);
  setHidden(byId(IDS.empty), true);
  try {
    const visitor = await ensureVisitorSession(ctx);
    const response = await ctx.client.submitQuery({
      sessionId: visitor.sessionId,
      turnId: visitor.turnId,
      stateRevision: visitor.stateRevision,
      question: text,
    });
    applyResponse(ctx, response, text, "user");
    return response;
  } catch (error) {
    showError(error);
    setStatus("请求失败", false);
    return null;
  } finally {
    setStatus("就绪", false);
  }
}

export async function guestClarify(ctx, value, evidenceRef) {
  const visitor = ctx.state.visitor;
  const pending = visitor.pending;
  if (!pending) return null;
  ctxOfLastAsk = ctx;
  setStatus("处理中…", true);
  showError(null);
  try {
    const response = await ctx.client.submitClarification({
      sessionId: pending.session_id,
      requestId: pending.request_id,
      turnId: pending.turn_id,
      stateRevision: pending.state_revision,
      questionId: pending.question_id,
      slotId: pending.slot_id,
      value,
      evidenceRef,
    });
    applyResponse(ctx, response, String(value), "user");
    return response;
  } catch (error) {
    showError(error);
    setStatus("请求失败", false);
    return null;
  } finally {
    setStatus("就绪", false);
  }
}

function applyResponse(ctx, response, text, role) {
  const visitor = ctx.state.visitor;
  const payload = response.payload;
  visitor.turnId = payload.turn_id;
  visitor.stateRevision = payload.state_revision;
  visitor.pending = payload.pending_question || null;
  visitor.turns.push({ role, text, turn: visitor.turns.filter((t) => t.role === "user").length + 1 });
  renderSessionMeta(ctx);
  renderTurns(ctx);

  const result = byId(IDS.result);
  setHidden(result, false);
  const statusPill = byId(IDS.resultStatus);
  const statusText = { success: "通过", refused: "已拒绝", clarification_required: "需要补充信息", error: "错误" };
  statusPill.textContent = statusText[payload.status] || payload.status;
  statusPill.className = `status-pill ${
    payload.status === "success" ? "status-pill--ok" : payload.status === "refused" || payload.status === "error" ? "status-pill--danger" : ""
  }`;
  byId(IDS.message).textContent = payload.message || "";
  renderResultTable(payload);
  renderCitations(payload.route === "rag" ? payload.citations : [], payload.warnings || []);
  renderTech(payload);
  if (payload.error) {
    showError({ code: payload.error.code, message: payload.error.message, retryable: payload.error.retryable });
  } else {
    showError(null);
  }
  renderClarify(payload);
}
