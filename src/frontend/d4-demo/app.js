"use strict";

const byId = (id) => document.getElementById(id);
const state = { counter: 0, buttons: [] };

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = String(text);
  return element;
}

function formatScore(value) {
  return value === null || value === undefined ? "—" : Number(value).toFixed(6);
}

function setBusy(busy) {
  for (const button of state.buttons) button.disabled = busy;
  byId("service-state").textContent = busy ? "正在运行…" : "本机离线服务就绪";
}

function clear(element) {
  while (element.firstChild) element.removeChild(element.firstChild);
}

function renderScenarios(capabilities) {
  const grid = byId("scenario-grid");
  clear(grid);
  state.buttons = [];
  for (const scenario of capabilities.scenarios) {
    const button = node("button", "scenario-card");
    button.type = "button";
    button.dataset.scenarioId = scenario.scenario_id;
    button.append(node("span", "scenario-card__route", scenario.route.toUpperCase()));
    button.append(node("strong", "", scenario.title));
    button.append(node("small", "", scenario.scenario_id));
    button.addEventListener("click", () => runScenario(scenario.scenario_id));
    grid.append(button);
    state.buttons.push(button);
  }
  byId("manifest-ref").textContent = `${capabilities.scenario_manifest.id} · ${capabilities.scenario_manifest.version}`;
}

async function runScenario(scenarioId) {
  setBusy(true);
  state.counter += 1;
  let failed = false;
  try {
    const response = await fetch("/api/v1/demo", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        schema_version: "1.0.0",
        request_id: `demo.web.${state.counter}`,
        scenario_id: scenarioId,
      }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error?.code || "demo_request_failed");
    renderResult(payload);
  } catch (error) {
    failed = true;
    byId("service-state").textContent = `失败：${error.message}`;
    byId("service-state").className = "status-pill status-pill--danger";
  } finally {
    for (const button of state.buttons) button.disabled = false;
    if (!failed) {
      byId("service-state").textContent = "本机离线服务就绪";
      byId("service-state").className = "status-pill";
    }
  }
}

function renderResult(result) {
  const shell = byId("result-shell");
  shell.hidden = false;
  const status = byId("result-status");
  status.textContent = result.status === "success" ? "通过" : "已拒绝";
  status.className = `status-pill ${result.status === "success" ? "" : "status-pill--danger"}`;
  byId("result-message").textContent = result.message;
  byId("decision-action").textContent = result.decision.action;
  byId("decision-reason").textContent = result.decision.reason_code;
  byId("score-kind").textContent = result.decision.score_kind;
  byId("score-value").textContent = formatScore(result.decision.score);
  renderThresholds(result.decision.thresholds);
  renderCandidates(result.candidate_evidence);
  renderSql(result.sql);
  renderCitations(result.citations, result.route);
  renderTrace(result.trace);
  shell.scrollIntoView({ behavior: "smooth", block: "start" });
}

function renderThresholds(thresholds) {
  const list = byId("threshold-list");
  clear(list);
  for (const [key, value] of Object.entries(thresholds)) {
    if (value === null) continue;
    const chip = node("span", "chip");
    chip.append(node("strong", "", key));
    chip.append(document.createTextNode(` · ${formatScore(value)}`));
    list.append(chip);
  }
  if (!list.childNodes.length) list.append(node("span", "chip", "无适用阈值"));
}

function renderCandidates(candidates) {
  const list = byId("candidate-list");
  clear(list);
  if (!candidates.length) {
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
    item.append(node("div", "refs", candidate.evidence_refs.join(" · ") || "无引用"));
    list.append(item);
  }
}

function renderSql(sql) {
  const panel = byId("sql-panel");
  panel.hidden = !sql;
  if (!sql) return;
  byId("sql-code").textContent = sql.candidate_sql;
  const badge = byId("safety-badge");
  badge.textContent = sql.safety_approved ? "SafetyGate 通过" : "SafetyGate 拒绝";
  badge.className = `status-pill ${sql.safety_approved ? "" : "status-pill--danger"}`;
  const meta = byId("sql-meta");
  clear(meta);
  for (const value of [
    `表：${sql.tables.join(", ")}`,
    `执行尝试：${sql.execution_attempts}`,
    `结果行：${sql.row_count}`,
  ]) meta.append(node("span", "chip", value));
  const table = byId("result-table");
  clear(table);
  if (!sql.columns.length) return;
  const thead = node("thead");
  const headerRow = node("tr");
  for (const column of sql.columns) headerRow.append(node("th", "", column));
  thead.append(headerRow);
  table.append(thead);
  const tbody = node("tbody");
  for (const row of sql.rows) {
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
  if (!citations.length) {
    list.append(node("p", "result-message", "没有证据越过固定检索门槛。"));
    return;
  }
  for (const citation of citations) {
    const item = node("article", "citation");
    const top = node("div", "citation__top");
    top.append(node("strong", "", `${citation.document_title} · 第 ${citation.page} 页`));
    top.append(node("small", "", formatScore(citation.score)));
    item.append(top);
    item.append(node("small", "", `${citation.evidence_id} · ${citation.pdf_path}`));
    item.append(node("p", "", citation.snippet));
    list.append(item);
  }
}

function renderTrace(trace) {
  const list = byId("trace-list");
  clear(list);
  for (const event of trace) {
    const item = node("li");
    item.append(node("span", "timeline__index", event.sequence));
    const body = node("div", "timeline__body");
    body.append(node("strong", "", `${event.stage} · ${event.action || "记录"}`));
    body.append(node("small", "", event.reason_code || "无额外理由"));
    body.append(node("small", "", `artifact ${event.artifact_sha256.slice(0, 16)}…`));
    if (event.evidence_refs.length) body.append(node("div", "refs", event.evidence_refs.join(" · ")));
    item.append(body);
    list.append(item);
  }
}

async function boot() {
  try {
    const response = await fetch("/api/v1/capabilities", { headers: { Accept: "application/json" } });
    if (!response.ok) throw new Error("capabilities_unavailable");
    renderScenarios(await response.json());
    byId("service-state").textContent = "本机离线服务就绪";
  } catch (error) {
    byId("service-state").textContent = `无法连接：${error.message}`;
    byId("service-state").className = "status-pill status-pill--danger";
  }
}

boot();
