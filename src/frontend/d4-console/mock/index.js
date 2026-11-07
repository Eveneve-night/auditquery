"use strict";

import { isValidCitation } from "../rag-manifest.js";

/*
 * D4-07 C 侧契约 mock 数据源（模拟在线后端，对齐 v2 澄清绑定契约）。
 *
 * 每个响应严格满足共享在线契约 configs/schemas/d4-07/online-response.v2.schema.json：
 * - schema_version=2.0.0，turn_id/state_revision 为非负整数
 * - pending_question 下发 options（value/display_name/evidence_ref）
 * - usage 全 0（离线，不声明模型/业务准确率/泛化/创新）
 * - trace 的 artifact_sha256 为真实 SHA-256（FIPS 180-4），可被内容摘要独立复核
 * - 不引用 expected / gold / label 答案字段
 *
 * 请求入口 query(request) 接收 online-request.v2 的 submit_query /
 * submit_clarification；澄清回答按 D4-05 失败关闭：六字段篡改、重放、
 * 空证据、非法值均拒绝（错误码与 clarification-binding.v2.json 一致）。
 */

/* ---- 真实 SHA-256（FIPS 180-4，纯 JS，浏览器与 node 通用） ---- */
const K = [
  0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1,
  0x923f82a4, 0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3,
  0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786,
  0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
  0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147,
  0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
  0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
  0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
  0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a,
  0x5b9cca4f, 0x682e6ff3, 0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
  0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
];

function rotr(x, n) {
  return (x >>> n) | (x << (32 - n));
}

export function sha256Hex(text) {
  const data = new TextEncoder().encode(String(text));
  const bitLen = data.length * 8;
  const paddedLen = Math.ceil((data.length + 9) / 64) * 64;
  const buf = new ArrayBuffer(paddedLen);
  const bytes = new Uint8Array(buf);
  bytes.set(data);
  bytes[data.length] = 0x80;
  const dv = new DataView(buf);
  dv.setUint32(paddedLen - 8, Math.floor(bitLen / 0x100000000));
  dv.setUint32(paddedLen - 4, bitLen >>> 0);

  let h0 = 0x6a09e667, h1 = 0xbb67ae85, h2 = 0x3c6ef372, h3 = 0xa54ff53a;
  let h4 = 0x510e527f, h5 = 0x9b05688c, h6 = 0x1f83d9ab, h7 = 0x5be0cd19;

  const w = new Uint32Array(64);
  for (let i = 0; i < paddedLen; i += 64) {
    for (let j = 0; j < 16; j++) w[j] = dv.getUint32(i + j * 4);
    for (let j = 16; j < 64; j++) {
      const s0 = rotr(w[j - 15], 7) ^ rotr(w[j - 15], 18) ^ (w[j - 15] >>> 3);
      const s1 = rotr(w[j - 2], 17) ^ rotr(w[j - 2], 19) ^ (w[j - 2] >>> 10);
      w[j] = (w[j - 16] + s0 + w[j - 7] + s1) >>> 0;
    }
    let a = h0, b = h1, c = h2, d = h3, e = h4, f = h5, g = h6, h = h7;
    for (let j = 0; j < 64; j++) {
      const S1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
      const ch = (e & f) ^ (~e & g);
      const temp1 = (h + S1 + ch + K[j] + w[j]) >>> 0;
      const S0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
      const maj = (a & b) ^ (a & c) ^ (b & c);
      const temp2 = (S0 + maj) >>> 0;
      h = g; g = f; f = e; e = (d + temp1) >>> 0;
      d = c; c = b; b = a; a = (temp1 + temp2) >>> 0;
    }
    h0 = (h0 + a) >>> 0; h1 = (h1 + b) >>> 0; h2 = (h2 + c) >>> 0;
    h3 = (h3 + d) >>> 0; h4 = (h4 + e) >>> 0; h5 = (h5 + f) >>> 0;
    h6 = (h6 + g) >>> 0; h7 = (h7 + h) >>> 0;
  }

  return [h0, h1, h2, h3, h4, h5, h6, h7]
    .map((x) => x.toString(16).padStart(8, "0"))
    .join("");
}

/* ---- 服务端会话状态模拟（失败关闭用） ---- */
const server = {
  pending: null, // 最近一次 clarify 产生的 pending_question
  consumed_answer_ids: new Set(),
  consumed_question_ids: new Set(),
};

let questionCounter = 0;
function nextQuestionId() {
  questionCounter += 1;
  return "question.web." + questionCounter;
}

/* ---- online-response.v2 构件 ---- */

function usage() {
  return {
    model_calls: 0,
    input_tokens: 0,
    output_tokens: 0,
    estimated_cost_cny: 0,
    elapsed_ms: 0,
    stopped_reason: null,
  };
}

function thresholds() {
  return {
    accept_min: 0.75,
    clarify_min: 0.5,
    abstain_below: 0.25,
    retrieval_min_score: 5.0,
  };
}

function decision(action, reasonCode, scoreKind, rawScore, missingSlots) {
  return {
    contract_version: "1.0.0",
    action,
    reason_code: reasonCode,
    score_kind: scoreKind,
    raw_score: rawScore,
    calibration_version: null,
    thresholds: thresholds(),
    evidence_refs: [],
    missing_or_conflicting_slots: missingSlots || [],
    accept_authorizes_sql_execution: false,
    requires_d4_02_safety_gate: true,
  };
}

function traceEvent(sequence, stage, action, reasonCode, artifact) {
  return {
    sequence,
    stage,
    action,
    reason_code: reasonCode,
    evidence_refs: [],
    artifact_sha256: sha256Hex(artifact),
  };
}

function sqlResult(proposalId, candidateSql, tables, safetyStatus, attempts, columns, rows, rowCount) {
  return {
    proposal_id: proposalId,
    candidate_sql: candidateSql,
    tables,
    safety_status: safetyStatus,
    execution_attempts: attempts,
    columns,
    rows,
    row_count: rowCount,
    truncated: false,
  };
}

function base(request, route, status) {
  // 终态（success/refused）默认推进到下一轮：turn_id/state_revision 各 +1；
  // clarification_required / error 由调用方覆盖回原值（不推进）。
  return {
    schema_version: "2.0.0",
    session_id: request.session_id,
    request_id: request.request_id,
    turn_id: request.turn_id + 1,
    state_revision: request.state_revision + 1,
    route,
    status,
    message: "",
    decision: null,
    pending_question: null,
    candidate_evidence: [],
    sql: null,
    citations: [],
    trace: [],
    usage: usage(),
    error: null,
    warnings: [],
  };
}

/* ---- 六类场景 ---- */

const ARTIST_SQL = "SELECT COUNT(*) AS artist_count FROM Artist";

function sqlSuccess(request) {
  const r = base(request, "sql", "success");
  r.message = "安全只读统计：艺术家总数已返回。";
  r.decision = decision("accept", "metric_and_entity_resolved", "uncalibrated_raw_score", 0.92);
  r.candidate_evidence = [
    {
      candidate_id: "candidate.metric.count",
      display_name: "计数（COUNT）",
      candidate_kind: "metric",
      score: 0.95,
      evidence_refs: ["evidence.metric_kinds"],
    },
    {
      candidate_id: "candidate.entity.artist",
      display_name: "艺术家（Artist）",
      candidate_kind: "entity",
      score: 0.91,
      evidence_refs: ["evidence.entity_kinds"],
    },
  ];
  r.sql = sqlResult("proposal.demo.sql.001", ARTIST_SQL, ["Artist"], "approved", 1, ["artist_count"], [[275]], 1);
  r.trace = [
    traceEvent(1, "intent_routing", "route_to_sql", "metric_query", "route_to_sql"),
    traceEvent(2, "selective_routing", "accept", "metric_and_entity_resolved", "accept:metric_and_entity_resolved"),
    traceEvent(3, "sql_safety_gate", "approve", "single_table_readonly", ARTIST_SQL),
    traceEvent(4, "readonly_execution", "executed", "rows_returned", "rows:275"),
  ];
  r.warnings = ["uncalibrated_raw_score"];
  return r;
}

function sqlMalicious(request) {
  const r = base(request, "sql", "refused");
  r.message = "写操作被安全门拦截，未执行。";
  r.decision = decision("reject", "write_operation_blocked", "uncalibrated_raw_score", 0.1);
  r.sql = sqlResult("proposal.demo.sql.002", "DELETE FROM Artist", ["Artist"], "rejected", 0, [], [], 0);
  r.trace = [
    traceEvent(1, "intent_routing", "route_to_sql", "metric_query", "route_to_sql"),
    traceEvent(2, "sql_safety_gate", "deny", "write_operation_blocked", "DELETE FROM Artist"),
  ];
  r.warnings = ["uncalibrated_raw_score", "safety_gate_denied"];
  return r;
}

function ragSuccess(request) {
  const r = base(request, "rag", "success");
  r.message = "已定位制度证据并给出页码引用（未生成模型答案）。";
  r.decision = decision("accept", "evidence_above_threshold", "deterministic_bm25", 7.8);
  r.candidate_evidence = [
    {
      candidate_id: "candidate.evidence.sales_001",
      display_name: "月度经营报表截止时间",
      candidate_kind: "evidence",
      score: 7.8,
      evidence_refs: ["SALES-001"],
    },
  ];
  r.citations = [
    {
      evidence_id: "SALES-001",
      document_id: "sales_playbook",
      document_title: "销售分析手册",
      page: 1,
      snippet:
        "[SALES-001] 月度经营报表的截止时间是次月第一个工作日 18:00。截止后到达的数据进入下一次修订版，不静默覆盖已发布报表。",
      score: 7.8,
    },
  ];
  // 引用合法性校验（模拟后端 citation_validation）：evidence_id 必须命中 D4-06
  // manifest 且页码一致，否则失败关闭。
  if (!r.citations.every((c) => isValidCitation(c.evidence_id, c.page))) {
    return ragInvalidCitation(request);
  }
  r.trace = [
    traceEvent(1, "intent_routing", "route_to_rag", "document_question", "route_to_rag"),
    traceEvent(2, "lexical_retrieval", "top_k_retrieved", "evidence_above_threshold", "bm25:top1"),
    traceEvent(3, "citation_validation", "page_resolved", "evidence_to_page_verified", "sales_playbook:page1"),
  ];
  r.warnings = ["uncalibrated_raw_score", "no_model_answer_generated"];
  return r;
}

function ragInvalidCitation(request) {
  // 非法引用失败关闭：检索命中但 citation 未通过合法性校验（证据 ID 不在
  // D4-06 manifest 或页码不一致），后端拒绝作答。
  const r = base(request, "rag", "refused");
  r.message = "检索命中的引用未通过合法性校验（证据 ID 不存在或页码不一致），已失败关闭。";
  r.decision = decision("reject", "invalid_citation_reference", "deterministic_bm25", 0.0);
  r.trace = [
    traceEvent(1, "intent_routing", "route_to_rag", "document_question", "route_to_rag"),
    traceEvent(2, "lexical_retrieval", "top_k_retrieved", "evidence_above_threshold", "bm25:top1"),
    traceEvent(3, "citation_validation", "reject", "invalid_citation_reference", "citation:invalid"),
  ];
  r.warnings = ["uncalibrated_raw_score", "invalid_citation_rejected"];
  return r;
}

function ragParseError(request) {
  // 文档解析失败：双解析器无法一致识别证据 ID，无法建立「ID → 页码」映射。
  // 按 US-02 验收，展示原因、受影响页与可采取的处理方式。
  const r = base(request, "rag", "error");
  r.turn_id = request.turn_id; // 失败不推进轮次
  r.state_revision = request.state_revision;
  r.message = "知识文档解析失败，无法建立证据页码映射。";
  r.decision = null;
  r.error = {
    code: "document_parse_failed",
    retryable: false,
    message:
      "原因：PDF 内容损坏，pdfplumber 与 pypdf 双解析器对证据 ID 的识别结果不一致。" +
      "受影响页：第 2 页（EXP-003 附近）。" +
      "处理方式：重新生成可检索文本 PDF，或替换为纯文本版本文档后重试。",
  };
  r.trace = [
    traceEvent(1, "intent_routing", "route_to_rag", "document_question", "route_to_rag"),
    traceEvent(2, "document_parse", "parse_failed", "double_parser_mismatch", "expense_policy:page2"),
  ];
  r.warnings = ["document_parse_issue", "no_model_answer_generated"];
  return r;
}

function ragRefused(request) {
  const r = base(request, "rag", "refused");
  r.message = "没有证据越过固定检索门槛，拒绝作答。";
  r.decision = decision("reject", "evidence_below_threshold", "deterministic_bm25", 1.2);
  r.trace = [
    traceEvent(1, "intent_routing", "route_to_rag", "document_question", "route_to_rag"),
    traceEvent(2, "lexical_retrieval", "abstain", "evidence_below_threshold", "below_threshold"),
  ];
  r.warnings = ["evidence_below_threshold"];
  return r;
}

function clarify(request) {
  const r = base(request, "clarification", "clarification_required");
  r.turn_id = request.turn_id; // 澄清等待补充，不推进轮次
  r.state_revision = request.state_revision;
  r.message = "问题缺少可执行的指标信息，请选择要统计的指标。";
  r.decision = decision("clarify", "missing_required_slot", "uncalibrated_raw_score", 0.4, ["slot.metric"]);
  const pending = {
    question_id: nextQuestionId(),
    session_id: request.session_id,
    request_id: request.request_id,
    turn_id: request.turn_id,
    state_revision: request.state_revision,
    slot_id: "slot.metric",
    question_text: "你要统计哪项指标？",
    options: [
      { value: "count", display_name: "数量（COUNT）", evidence_ref: "value_evidence.metric_count" },
      { value: "sum", display_name: "求和（SUM）", evidence_ref: "value_evidence.metric_sum" },
    ],
  };
  r.pending_question = pending;
  server.pending = pending;
  r.trace = [
    traceEvent(1, "selective_routing", "clarify", "missing_required_slot", "clarify:slot.metric"),
  ];
  r.warnings = ["uncalibrated_raw_score"];
  return r;
}

function errorResponse(request) {
  const r = base(request, "unknown", "error");
  r.turn_id = request.turn_id; // 失败不推进轮次
  r.state_revision = request.state_revision;
  r.message = "请求无法处理。";
  r.error = { code: "invalid_request", retryable: false, message: "请求格式无效。" };
  r.warnings = [];
  return r;
}

/* ---- 路由 ---- */

function routeQuery(query) {
  if (/删除|写入|修改|update|delete|insert|drop/i.test(query)) return sqlMalicious;
  if (/伪造|假证据|非法引用|篡改/i.test(query)) return ragInvalidCitation;
  if (/解析失败|损坏|乱码|无法解析|pdf.*坏/i.test(query)) return ragParseError;
  if (/健身房|洗澡|开门/i.test(query)) return ragRefused;
  if (/截止时间|报表|报销|制度|手册|政策|证据/i.test(query)) return ragSuccess;
  if (/艺术家|统计|多少|数量|count/i.test(query)) return sqlSuccess;
  if (/^.{0,6}$/.test(query)) return clarify;
  return ragSuccess;
}

function refuse(request, code) {
  const r = base(request, "clarification", "error");
  r.message = "澄清回答校验失败：" + code;
  r.error = { code, retryable: false, message: "澄清绑定校验失败。" };
  return r;
}

function respondClarification(request) {
  // 重放：answer_id 或 question_id 已消费则拒绝（先于 no_pending，与 D4-05 一致）
  if (
    server.consumed_answer_ids.has(request.answer_id) ||
    server.consumed_question_ids.has(request.question_id)
  ) {
    return refuse(request, "clarification_answer_replayed");
  }
  const pending = server.pending;
  if (!pending) {
    return refuse(request, "no_pending_clarification");
  }
  // 六字段原样绑定（与 D4-05 StateMerger 一致）
  if (request.session_id !== pending.session_id) return refuse(request, "clarification_session_mismatch");
  if (request.request_id !== pending.request_id) return refuse(request, "clarification_request_mismatch");
  if (request.turn_id !== pending.turn_id) return refuse(request, "clarification_turn_mismatch");
  if (request.state_revision !== pending.state_revision) return refuse(request, "clarification_answer_stale");
  if (request.question_id !== pending.question_id) return refuse(request, "clarification_question_mismatch");
  if (request.slot_id !== pending.slot_id) return refuse(request, "clarification_cross_slot_update");
  // 值白名单：恰好一个值，且必须来自服务端 options
  const values = request.values || [];
  if (values.length !== 1) return refuse(request, "clarification_value_not_offered");
  const selected = pending.options.find((o) => o.value === values[0]);
  if (!selected) return refuse(request, "clarification_value_not_offered");
  // 证据：恰好一条，且必须等于所选 option 的 evidence_ref
  const refs = request.evidence_refs || [];
  if (refs.length !== 1 || refs[0] !== selected.evidence_ref) {
    return refuse(request, "invalid_answer_evidence_refs");
  }
  // 通过：原子消费 + 清除 pending，返回 SQL 成功（turn_id/state_revision 已在 base 中推进）
  server.consumed_answer_ids.add(request.answer_id);
  server.consumed_question_ids.add(request.question_id);
  server.pending = null;
  return sqlSuccess(request);
}

function route(request) {
  const op = String(request.operation || "");
  if (op === "submit_clarification") return respondClarification(request);
  if (op === "submit_query") {
    const query = String(request.query || "").trim();
    return routeQuery(query)(request);
  }
  return errorResponse(request);
}

export default {
  name: "契约 mock（在线契约）",
  mode: "offline_mock",
  sha256Hex,
  query(request) {
    return Promise.resolve(route(request));
  },
};
