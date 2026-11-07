"use strict";

/*
 * D4-07 C 侧前端适配层（adapter）。
 *
 * 职责一：请求构建 —— 把前端会话状态转成 online-request.v2 合规的
 *   submit_query / submit_clarification 请求（见
 *   configs/schemas/d4-07/online-request.v2.schema.json）。
 *
 * 职责二：响应归一化 —— 把 online-response.v2 响应映射为渲染层统一视图，
 *   屏蔽 raw_score / safety_status / usage / pending_question(options) 等字段差异，
 *   使渲染层不直接依赖某一版契约字段名。
 *
 * v2 澄清绑定语义（D4-07 澄清绑定契约 v2）：
 * - submit_clarification 只生成 answer_id，session_id/request_id/turn_id/
 *   state_revision/question_id/slot_id 六字段必须从 pending_question 原样回传；
 * - values / evidence_refs 恰好各一个，分别来自所选 option 的 value 与 evidence_ref。
 *
 * 本模块不引用 expected / gold / label 答案字段，不发起模型或网络调用。
 */

let requestCounter = 0;
let answerCounter = 0;

function nextRequestId() {
  requestCounter += 1;
  return "console.web." + requestCounter;
}

function nextAnswerId() {
  answerCounter += 1;
  return "answer.web." + answerCounter;
}

function nextSessionId() {
  const rand = Math.random().toString(36).slice(2, 10);
  return "session.web." + Date.now() + "." + rand;
}

/** 新建会话状态：session_id / turn_id(整数) / state_revision(整数)。

 * turn_id 从 0 起（与 B 侧在线 SQL 后端 `_submit_query` 的 initial_turn_id==0 对齐，
 * v2 契约 turn_id 为 >= 0）。 */
export function newSession() {
  return {
    session_id: nextSessionId(),
    turn_id: 0,
    state_revision: 0,
  };
}

/** 构造 submit_query 请求（online-request.v2）。 */
export function buildSubmitQuery(session, query) {
  return {
    schema_version: "2.0.0",
    operation: "submit_query",
    session_id: session.session_id,
    request_id: nextRequestId(),
    turn_id: session.turn_id,
    state_revision: session.state_revision,
    query,
  };
}

/**
 * 构造 submit_clarification 请求（online-request.v2）。
 *
 * 只生成 answer_id；六个绑定字段从 pending 原样回传，不得重建 request_id。
 * values / evidence_refs 恰好各一个，来自所选 option 的 value 与 evidence_ref。
 *
 * @param {object} pending  后端返回的 pending_question（不可变绑定源）
 * @param {*}       value    用户所选 option 的 value
 * @param {string}  evidenceRef 所选 option 的 evidence_ref
 */
export function buildSubmitClarification(pending, value, evidenceRef) {
  return {
    schema_version: "2.0.0",
    operation: "submit_clarification",
    answer_id: nextAnswerId(),
    session_id: pending.session_id,
    request_id: pending.request_id,
    turn_id: pending.turn_id,
    state_revision: pending.state_revision,
    question_id: pending.question_id,
    slot_id: pending.slot_id,
    values: [value],
    evidence_refs: [evidenceRef],
  };
}

/**
 * 归一化 online-response.v2 为渲染层视图。
 *
 * 字段映射（online → 视图）：
 *   decision.raw_score            → decision.score
 *   sql.safety_status             → sql.safety_status + sql.safety_ok
 *   usage                         → usage（token/费用/时延）
 *   pending_question（含 options）→ pending（结构化澄清绑定 + 可选值）
 *   error                         → error
 */
export function normalizeResponse(response) {
  if (!response) {
    return {
      route: "unknown",
      status: "error",
      message: "",
      decision: null,
      candidates: [],
      sql: null,
      citations: [],
      trace: [],
      usage: null,
      pending: null,
      error: { code: "empty_response", retryable: true, message: "空响应" },
      warnings: [],
    };
  }

  const decision = response.decision || null;
  const sql = response.sql || null;

  return {
    route: response.route || "unknown",
    status: response.status || "error",
    message: response.message || "",
    decision: decision
      ? {
          action: decision.action,
          reason_code: decision.reason_code,
          score_kind: decision.score_kind,
          score: decision.raw_score,
          thresholds: decision.thresholds || null,
          missing_slots: decision.missing_or_conflicting_slots || [],
        }
      : null,
    candidates: response.candidate_evidence || [],
    sql: sql
      ? {
          candidate_sql: sql.candidate_sql,
          safety_status: sql.safety_status,
          safety_ok: sql.safety_status === "approved",
          tables: sql.tables || [],
          execution_attempts: sql.execution_attempts,
          columns: sql.columns || [],
          rows: sql.rows || [],
          row_count: sql.row_count,
          truncated: sql.truncated,
        }
      : null,
    citations: response.citations || [],
    trace: response.trace || [],
    usage: response.usage || null,
    pending: response.pending_question || null,
    error: response.error || null,
    warnings: response.warnings || [],
  };
}
