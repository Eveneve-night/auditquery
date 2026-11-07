"use strict";

/*
 * D5-04 C 侧前端：D5 API v3 离线 mock 传输层（src/frontend/d5-console/mock/index.js）。
 *
 * 覆盖 configs/contracts/d5/product-layer.v1.json 的全部 24 个端点，响应字段与
 * configs/schemas/d5/api-v3.schema.json 一致；错误码与文案取自同一契约的
 * error_catalog（safe_message），不自定义、不泄露内部原因。
 *
 * 问答载荷复用 D4-07 已通过契约校验的 mock（src/frontend/d4-console/mock/index.js），
 * 避免在 D5 里复制一份会漂移的 online_response v2 构件。
 *
 * 安全边界（与契约 security 段一致）：
 * - 不内置任何默认口令或固定引导令牌：管理员口令与引导令牌必须由调用方在运行时
 *   注入（window.D5_MOCK_ADMIN_PASSWORD / window.D5_MOCK_SETUP_TOKEN），否则
 *   登录与引导一律 401。
 * - API Key 只在请求体中接收，mock 只保留末四位，任何响应都不回显明文；
 *   连通测试是独立端点且必须 explicit_confirmation=true。
 * - 审计只写白名单字段（event_type/outcome/error_code/usage_counts 等），
 *   不写问题原文、回答原文、PDF 正文或任何令牌。
 * - 管理写操作校验与会话绑定的 CSRF 令牌；缺失或跨会话一律 403 csrf_invalid，
 *   且校验失败不改变任何状态。
 */

import d4Mock, { sha256Hex } from "../../d4-console/mock/index.js";

const API_VERSION = "3.0";
const CONTRACT_VERSION = "1.0.0";
const BOUNDED_ID = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;
const SHA256 = /^[a-f0-9]{64}$/;
const DOC_STATES = ["quarantined", "validating", "validated", "indexing", "published", "disabled", "failed", "deleted"];
const MAX_FILE_BYTES = 26214400;
const MAX_PAGES = 500;

const state = {
  sessions: new Map(),
  documents: new Map(),
  admin: { authenticated: false, username: null, csrf: null, generation: 0, bootstrapped: false,
           mockPasswordOverride: null },
  key: { configured: false, last_four: null, updated_at: null, revision: 0 },
  audit: [],
  consumedRequestIds: new Set(),
  counters: { session: 0, document: 0, audit: 0, index: 0, revision: 0 },
};

function nowIso(offsetSeconds = 0) {
  return new Date(Date.now() + offsetSeconds * 1000).toISOString();
}

function injected(key) {
  const scope = typeof globalThis !== "undefined" ? globalThis : {};
  if (scope.window && typeof scope.window[key] === "string") return scope.window[key];
  if (typeof scope[key] === "string") return scope[key];
  return null;
}

function nextRevision() {
  state.counters.revision += 1;
  return state.counters.revision;
}

function requestIdOf(body) {
  if (body && typeof body.request_id === "string" && BOUNDED_ID.test(body.request_id)) {
    return body.request_id;
  }
  state.counters.audit += 1;
  return `req.mock.${state.counters.audit}`;
}

function fail(status, code, message, retryable, body) {
  return {
    status,
    json: {
      api_version: API_VERSION,
      contract_version: CONTRACT_VERSION,
      request_id: requestIdOf(body),
      error: { code, message, retryable: Boolean(retryable) },
    },
  };
}

function ok(payload, body) {
  return {
    status: 200,
    json: { api_version: API_VERSION, contract_version: CONTRACT_VERSION, request_id: requestIdOf(body), ...payload },
  };
}

/* ---- 审计（只写白名单字段） ---- */

function audit(eventType, { outcome = "succeeded", errorCode = null, requestId = null, revision = null } = {}) {
  state.counters.audit += 1;
  state.audit.push({
    event_id: `evt.${state.counters.audit}`,
    event_type: eventType,
    occurred_at: nowIso(),
    request_id: requestId || `req.mock.${state.counters.audit}`,
    actor_role: "admin",
    subject_type: "admin_session",
    subject_id_hash: sha256Hex(state.admin.username || "anonymous").slice(0, 32),
    outcome,
    error_code: errorCode,
    source_fingerprint: sha256Hex("d5-console-mock").slice(0, 16),
    resource_revision: revision,
    usage_counts: { model_calls: 0, sql_executions: 0, rag_lookups: 0 },
  });
}

/* ---- 会话 ---- */

function createSession() {
  state.counters.session += 1;
  const sessionId = `sess.${state.counters.session}`;
  const session = { session_id: sessionId, turn_id: 0, state_revision: 0, expires_at: nowIso(86400), pending: null };
  state.sessions.set(sessionId, session);
  return session;
}

function sessionResponse(session, body) {
  return ok(
    {
      session_id: session.session_id,
      state_revision: session.state_revision,
      expires_at: session.expires_at,
    },
    body,
  );
}

function agentResponse(payload, body) {
  return ok(
    {
      session_id: payload.session_id,
      state_revision: payload.state_revision,
      status: payload.status,
      payload,
      error: payload.error || null,
    },
    body,
  );
}

function requireAdmin(csrf, { csrfRequired }) {
  if (!state.admin.authenticated) {
    return fail(401, "authentication_required", "需要有效身份。", false);
  }
  if (csrfRequired && (!csrf || csrf !== state.admin.csrf)) {
    audit("csrf_rejected", { outcome: "denied", errorCode: "csrf_invalid" });
    return fail(403, "csrf_invalid", "请求安全校验失败。", false);
  }
  return null;
}

function issueAdminSession(body, username) {
  state.admin.authenticated = true;
  state.admin.username = username;
  state.admin.generation += 1;
  state.admin.csrf = sha256Hex(`csrf:${username}:${state.admin.generation}:${Date.now()}`);
  return ok(
    {
      authenticated: true,
      csrf_token: state.admin.csrf,
      expires_at: nowIso(8 * 3600),
      ...(body && body.contract_version === "2.0.0" ? {
        contract_version: "2.0.0", must_change_password: false,
        password_change_available: true,
        allowed_actions: ["admin", "change_password", "logout"],
      } : {}),
    },
    body,
  );
}

/* ---- 文档 ---- */

function documentStatus(doc, body) {
  return ok(
    {
      document_id: doc.document_id,
      state: doc.state,
      sha256: doc.sha256,
      bytes: doc.bytes,
      page_count: doc.page_count,
      index_version: doc.index_version,
      failure_code: doc.failure_code,
    },
    body,
  );
}

function seedDocuments() {
  const seeds = [
    {
      filename: "销售分析手册.pdf",
      bytes: 184320,
      page_count: 12,
      state: "published",
      failure_code: null,
    },
    {
      filename: "费用报销制度.pdf",
      bytes: 96256,
      page_count: 8,
      state: "failed",
      failure_code: "bounded_dual_parse_mismatch",
    },
    {
      filename: "人事常见问题.pdf",
      bytes: 65536,
      page_count: 5,
      state: "disabled",
      failure_code: null,
    },
  ];
  for (const seed of seeds) {
    if (!DOC_STATES.includes(seed.state)) {
      throw new Error(`种子文档状态不在契约 8 态之内：${seed.state}`);
    }
    state.counters.document += 1;
    const documentId = `doc.${state.counters.document}`;
    state.documents.set(documentId, {
      document_id: documentId,
      filename: seed.filename,
      sha256: sha256Hex(seed.filename),
      bytes: seed.bytes,
      page_count: seed.page_count,
      state: seed.state,
      index_version: seed.state === "published" || seed.state === "disabled" ? `idx.2026100${state.counters.document}.001` : null,
      failure_code: seed.failure_code,
      revision: nextRevision(),
    });
  }
  state.counters.index = 3;
}

seedDocuments();

/* ---- 端点实现 ---- */

function onCreateSession(body) {
  const session = createSession();
  return sessionResponse(session, body);
}

function onReadSession(sessionId) {
  const session = state.sessions.get(sessionId);
  if (!session) return fail(404, "resource_not_found", "资源不存在。", false);
  return sessionResponse(session, null);
}

function onDeleteSession(sessionId, body) {
  const session = state.sessions.get(sessionId);
  if (!session) return fail(404, "resource_not_found", "资源不存在。", false);
  state.sessions.delete(sessionId);
  return ok({ status: "deleted", resource_revision: nextRevision() }, body);
}

function onReadSessionEvents(sessionId, body) {
  const session = state.sessions.get(sessionId);
  if (!session) return fail(404, "resource_not_found", "资源不存在。", false);
  // 只回稳定 ID 与计数，不回问题/回答原文
  return ok(
    {
      status: "ok",
      summary: {
        session_id: session.session_id,
        state_revision: session.state_revision,
        turn_id: session.turn_id,
        event_count: 0,
        events: [],
        note: "脱敏事件流：不返回问题或回答原文。",
      },
    },
    body,
  );
}

async function onQuery(body) {
  const session = state.sessions.get(body ? body.session_id : null);
  if (!session) return fail(404, "resource_not_found", "资源不存在。", false, body);
  if (!BOUNDED_ID.test(String(body.request_id || ""))) {
    return fail(400, "invalid_request", "请求格式无效。", false, body);
  }
  if (state.consumedRequestIds.has(body.request_id)) {
    return fail(409, "operation_replayed", "操作已处理。", false, body);
  }
  if (body.turn_id !== session.turn_id || body.state_revision !== session.state_revision) {
    return fail(409, "state_conflict", "资源状态已变化，请刷新后重试。", true, body);
  }
  state.consumedRequestIds.add(body.request_id);
  const payload = await d4Mock.query({
    schema_version: "2.0.0",
    operation: "submit_query",
    session_id: session.session_id,
    request_id: body.request_id,
    turn_id: body.turn_id,
    state_revision: body.state_revision,
    query: body.question,
  });
  session.turn_id = payload.turn_id;
  session.state_revision = payload.state_revision;
  session.pending = payload.pending_question || null;
  return agentResponse(payload, body);
}

async function onClarification(body) {
  const session = state.sessions.get(body ? body.session_id : null);
  if (!session) return fail(404, "resource_not_found", "资源不存在。", false, body);
  if (state.consumedRequestIds.has(body.answer_id)) {
    return fail(409, "operation_replayed", "操作已处理。", false, body);
  }
  const pending = session.pending;
  if (!pending) return fail(409, "state_conflict", "资源状态已变化，请刷新后重试。", true, body);
  const binding = ["session_id", "request_id", "turn_id", "state_revision", "question_id", "slot_id"];
  for (const field of binding) {
    if (body[field] !== pending[field]) {
      return fail(409, "state_conflict", "资源状态已变化，请刷新后重试。", true, body);
    }
  }
  state.consumedRequestIds.add(body.answer_id);
  const payload = await d4Mock.query({
    schema_version: "2.0.0",
    operation: "submit_clarification",
    answer_id: body.answer_id,
    session_id: body.session_id,
    request_id: body.request_id,
    turn_id: body.turn_id,
    state_revision: body.state_revision,
    question_id: body.question_id,
    slot_id: body.slot_id,
    values: body.values,
    evidence_refs: body.evidence_refs,
  });
  session.turn_id = payload.turn_id;
  session.state_revision = payload.state_revision;
  session.pending = payload.pending_question || null;
  return agentResponse(payload, body);
}

function onBootstrap(body) {
  if (state.admin.bootstrapped) {
    return fail(409, "admin_already_initialized", "管理员已初始化。", false, body);
  }
  const setupToken = injected("D5_MOCK_SETUP_TOKEN");
  const password = String((body && body.password) || "");
  const username = String((body && body.username) || "");
  if (!setupToken || body.setup_token !== setupToken) {
    audit("admin_bootstrap", { outcome: "denied", errorCode: "authentication_required", requestId: requestIdOf(body) });
    return fail(401, "authentication_required", "需要有效身份。", false, body);
  }
  if (username.length < 3 || username.length > 64 || password.length < 12 || password.length > 256) {
    return fail(400, "invalid_request", "请求格式无效。", false, body);
  }
  state.admin.bootstrapped = true;
  audit("admin_bootstrap", { outcome: "succeeded", requestId: requestIdOf(body), revision: nextRevision() });
  return issueAdminSession(body, username);
}

function onLogin(body) {
  const expected = state.admin.mockPasswordOverride || injected("D5_MOCK_ADMIN_PASSWORD");
  const username = String((body && body.username) || "");
  const password = String((body && body.password) || "");
  const requestId = requestIdOf(body);
  if (!expected) {
    return fail(401, "authentication_required", "离线 mock 不内置默认口令：请先注入 window.D5_MOCK_ADMIN_PASSWORD。", false, body);
  }
  // 未知账号与错误口令返回同形响应，避免账号枚举
  if ((body.contract_version === "2.0.0" ? username !== "admin" : username.length < 3) || password !== expected) {
    audit("admin_login_failed", { outcome: "denied", errorCode: "authentication_required", requestId });
    return fail(401, "authentication_required", "需要有效身份。", false, body);
  }
  audit("admin_login_succeeded", { outcome: "succeeded", requestId, revision: nextRevision() });
  return issueAdminSession(body, username);
}

function onChangePassword(body, csrf) {
  const denied = requireAdmin(csrf, { csrfRequired: true });
  if (denied) return denied;
  const expected = state.admin.mockPasswordOverride || injected("D5_MOCK_ADMIN_PASSWORD");
  const current = String((body && body.current_password) || "");
  const next = String((body && body.new_password) || "");
  if (current !== expected) return fail(401, "authentication_required", "需要有效身份。", false, body);
  if (next.length < 12 || next.length > 256 || next === current) {
    return fail(400, "invalid_request", "请求格式无效。", false, body);
  }
  state.admin.mockPasswordOverride = next;
  state.admin.authenticated = false;
  state.admin.csrf = null;
  return ok({ contract_version: "2.0.0", status: "password_changed", must_change_password: false,
              other_sessions_revoked: true, resource_revision: nextRevision() }, body);
}

function onLogout(body, csrf) {
  const denied = requireAdmin(csrf, { csrfRequired: true });
  if (denied) return denied;
  state.admin.authenticated = false;
  state.admin.csrf = null;
  audit("admin_logout", { requestId: requestIdOf(body), revision: nextRevision() });
  return ok({ status: "completed", resource_revision: nextRevision() }, body);
}

function onRevokeAll(body, csrf) {
  const denied = requireAdmin(csrf, { csrfRequired: true });
  if (denied) return denied;
  state.admin.authenticated = false;
  state.admin.csrf = null;
  audit("admin_sessions_revoked", { requestId: requestIdOf(body), revision: nextRevision() });
  return ok({ status: "revoked", resource_revision: nextRevision() }, body);
}

function onHealth(body) {
  const denied = requireAdmin(null, { csrfRequired: false });
  if (denied) return denied;
  const published = [...state.documents.values()].filter((d) => d.state === "published").length;
  return ok(
    {
      status: "ok",
      summary: {
        service: "ict8-agent",
        api_version: API_VERSION,
        contract_version: CONTRACT_VERSION,
        checked_at: nowIso(),
        sessions: { active: state.sessions.size },
        // summary 为自由对象：契约未定义「文档列表」端点，队列 ID 由健康检查下发，
        // 不新增端点、不改 Schema（configs/schemas/d5/api-v3.schema.json）。
        documents: {
          total: state.documents.size,
          published,
          ids: [...state.documents.values()].filter((d) => d.state !== "deleted").map((d) => d.document_id),
        },
        model_boundary: { provider: "bailian_beijing", model: "qwen3.7-flash", fallback: false, dynamic_routing: false },
        secret: { configured: state.key.configured, source: state.key.configured ? "secret_store" : "not_configured" },
      },
    },
    body,
  );
}

function onUploadDocument(body, csrf) {
  const denied = requireAdmin(csrf, { csrfRequired: true });
  if (denied) return denied;
  const filename = String((body && body.filename) || "");
  const bytes = Number(body && body.declared_bytes);
  // 路径穿越与客户端文件名注入：服务端生成 ID，且拒绝任何路径片段
  if (/[\\/]|^~$|^\.\.?$|[:*?"<>|]/.test(filename) || filename.length > 255 || filename.length < 1) {
    audit("security_limit_rejected", { outcome: "denied", errorCode: "document_invalid", requestId: requestIdOf(body) });
    return fail(422, "document_invalid", "文档未通过安全校验。", false, body);
  }
  if (body.content_type !== "application/pdf") {
    return fail(415, "unsupported_media_type", "仅接受受支持的 PDF。", false, body);
  }
  if (!Number.isInteger(bytes) || bytes < 1 || bytes > MAX_FILE_BYTES) {
    return fail(413, "upload_too_large", "文件超过允许大小。", false, body);
  }
  if (!SHA256.test(String((body && body.content_sha256) || ""))) {
    return fail(400, "invalid_request", "请求格式无效。", false, body);
  }
  state.counters.document += 1;
  const documentId = `doc.${state.counters.document}`;
  const doc = {
    document_id: documentId,
    filename,
    sha256: body.content_sha256,
    bytes,
    page_count: null,
    state: "validating",
    index_version: null,
    failure_code: null,
    revision: nextRevision(),
  };
  state.documents.set(documentId, doc);
  audit("document_state_changed", { requestId: requestIdOf(body), revision: doc.revision });
  return documentStatus(doc, body);
}

function onReadDocument(documentId, body) {
  const denied = requireAdmin(null, { csrfRequired: false });
  if (denied) return denied;
  const doc = state.documents.get(documentId);
  if (!doc || doc.state === "deleted") return fail(404, "resource_not_found", "资源不存在。", false, body);
  // 模拟异步校验：validating → validated（双解析得出页数），仅用于离线演示
  if (doc.state === "validating") {
    doc.state = "validated";
    doc.page_count = Math.min(MAX_PAGES, Math.max(1, Math.round(doc.bytes / 15360)));
    doc.revision = nextRevision();
  }
  return documentStatus(doc, body);
}

function onPublishDocument(documentId, body, csrf) {
  const denied = requireAdmin(csrf, { csrfRequired: true });
  if (denied) return denied;
  const doc = state.documents.get(documentId);
  if (!doc || doc.state === "deleted") return fail(404, "resource_not_found", "资源不存在。", false, body);
  if (doc.state !== "validated" && doc.state !== "disabled") {
    return fail(409, "state_conflict", "资源状态已变化，请刷新后重试。", true, body);
  }
  state.counters.index += 1;
  doc.state = "published";
  doc.index_version = `idx.mock.${state.counters.index}`;
  doc.failure_code = null;
  doc.revision = nextRevision();
  audit("index_published", { requestId: requestIdOf(body), revision: doc.revision });
  return documentStatus(doc, body);
}

function onDisableDocument(documentId, body, csrf) {
  const denied = requireAdmin(csrf, { csrfRequired: true });
  if (denied) return denied;
  const doc = state.documents.get(documentId);
  if (!doc || doc.state === "deleted") return fail(404, "resource_not_found", "资源不存在。", false, body);
  if (doc.state !== "published") {
    return fail(409, "state_conflict", "资源状态已变化，请刷新后重试。", true, body);
  }
  doc.state = "disabled";
  doc.revision = nextRevision();
  audit("document_state_changed", { requestId: requestIdOf(body), revision: doc.revision });
  return documentStatus(doc, body);
}

function onDeleteDocument(documentId, body, csrf) {
  const denied = requireAdmin(csrf, { csrfRequired: true });
  if (denied) return denied;
  const doc = state.documents.get(documentId);
  if (!doc || doc.state === "deleted") return fail(404, "resource_not_found", "资源不存在。", false, body);
  doc.state = "deleted";
  doc.index_version = null;
  doc.revision = nextRevision();
  audit("document_state_changed", { requestId: requestIdOf(body), revision: doc.revision });
  return ok({ status: "deleted", resource_revision: doc.revision }, body);
}

function onDownloadDocument(documentId, body) {
  const denied = requireAdmin(null, { csrfRequired: false });
  if (denied) return denied;
  const doc = state.documents.get(documentId);
  if (!doc || doc.state === "deleted") return fail(404, "resource_not_found", "资源不存在。", false, body);
  return ok(
    {
      status: "ok",
      summary: {
        document_id: doc.document_id,
        filename: doc.filename,
        bytes: doc.bytes,
        sha256: doc.sha256,
        note: "离线 mock 不返回文档正文；真实部署下为受控下载。",
      },
    },
    body,
  );
}

function keyStatus(body) {
  return ok(
    {
      configured: state.key.configured,
      provider: "bailian_beijing",
      model: "qwen3.7-flash",
      source: state.key.configured ? "secret_store" : "not_configured",
      updated_at: state.key.updated_at,
      last_four: state.key.last_four,
    },
    body,
  );
}

function onReadKey(body) {
  const denied = requireAdmin(null, { csrfRequired: false });
  if (denied) return denied;
  return keyStatus(body);
}

function onSaveKey(body, csrf, { replace }) {
  const denied = requireAdmin(csrf, { csrfRequired: true });
  if (denied) return denied;
  const apiKey = String((body && body.api_key) || "");
  if (apiKey.length < 16 || apiKey.length > 512) {
    return fail(400, "invalid_request", "请求格式无效。", false, body);
  }
  if (replace && !state.key.configured) {
    return fail(409, "state_conflict", "资源状态已变化，请刷新后重试。", true, body);
  }
  if (!replace && state.key.configured) {
    return fail(409, "state_conflict", "资源状态已变化，请刷新后重试。", true, body);
  }
  // 只保留末四位，明文不落任何状态、不进响应、不进审计
  state.key.configured = true;
  state.key.last_four = apiKey.slice(-4);
  state.key.updated_at = nowIso();
  state.key.revision += 1;
  audit("secret_changed", { requestId: requestIdOf(body), revision: state.key.revision });
  return keyStatus(body);
}

function onDeleteKey(body, csrf) {
  const denied = requireAdmin(csrf, { csrfRequired: true });
  if (denied) return denied;
  if (!state.key.configured) {
    return fail(409, "state_conflict", "资源状态已变化，请刷新后重试。", true, body);
  }
  state.key.configured = false;
  state.key.last_four = null;
  state.key.updated_at = nowIso();
  state.key.revision += 1;
  audit("secret_changed", { requestId: requestIdOf(body), revision: state.key.revision });
  return ok({ status: "deleted", resource_revision: state.key.revision }, body);
}

function onConnectivityTest(body, csrf) {
  const denied = requireAdmin(csrf, { csrfRequired: true });
  if (denied) return denied;
  if (body.explicit_confirmation !== true) {
    return fail(400, "invalid_request", "请求格式无效。", false, body);
  }
  if (!state.key.configured) {
    audit("connectivity_tested", { outcome: "denied", errorCode: "service_unavailable", requestId: requestIdOf(body) });
    return fail(503, "service_unavailable", "服务暂时不可用。", true, body);
  }
  audit("connectivity_tested", { requestId: requestIdOf(body), revision: state.key.revision });
  return ok(
    {
      status: "ok",
      summary: {
        provider: "bailian_beijing",
        model: "qwen3.7-flash",
        reachable: true,
        elapsed_ms: 0,
        checked_at: nowIso(),
        note: "离线 mock：连通测试为独立操作，不触发任何模型调用。",
      },
    },
    body,
  );
}

function onReadAudit(body) {
  const denied = requireAdmin(null, { csrfRequired: false });
  if (denied) return denied;
  return ok(
    {
      status: "ok",
      summary: {
        count: state.audit.length,
        retention_days: 90,
        append_only: true,
        events: state.audit.slice(-20),
      },
    },
    body,
  );
}

function onReadUsage(body) {
  const denied = requireAdmin(null, { csrfRequired: false });
  if (denied) return denied;
  return ok(
    {
      status: "ok",
      summary: {
        window: "offline_mock",
        sessions_created: state.counters.session,
        documents_total: state.documents.size,
        model_calls: 0,
        input_tokens: 0,
        output_tokens: 0,
        estimated_cost_cny: 0,
      },
    },
    body,
  );
}

/* ---- 路由 ---- */

function split(path) {
  return String(path || "").split("?")[0].replace(/\/+$/, "");
}

export function createMockTransport() {
  return async function transport(method, path, options = {}) {
    const body = options.body || null;
    const csrf = options.csrf || null;
    const target = split(path);

    if (method === "POST" && target === "/api/v3/sessions") return onCreateSession(body);
    if (method === "POST" && target === "/api/v3/query") return onQuery(body);
    if (method === "POST" && target === "/api/v3/clarifications") return onClarification(body);
    if (method === "POST" && target === "/api/v3/auth/admin/bootstrap") return onBootstrap(body);
    if (method === "POST" && target === "/api/v3/auth/admin/login") return onLogin(body);
    if (method === "POST" && target === "/api/v3/admin/password") return onChangePassword(body, csrf);
    if (method === "POST" && target === "/api/v3/admin/logout") return onLogout(body, csrf);
    if (method === "POST" && target === "/api/v3/admin/sessions/revoke-all") return onRevokeAll(body, csrf);
    if (method === "GET" && target === "/api/v3/admin/health") return onHealth(body);
    if (method === "POST" && target === "/api/v3/admin/documents") return onUploadDocument(body, csrf);
    if (method === "GET" && target === "/api/v3/admin/audit-events") return onReadAudit(body);
    if (method === "GET" && target === "/api/v3/admin/usage-summary") return onReadUsage(body);

    if (target === "/api/v3/admin/documents") {
      return fail(405, "invalid_request", "请求格式无效。", false, body);
    }
    if (target === "/api/v3/admin/secrets/bailian") {
      if (method === "GET") return onReadKey(body);
      if (method === "POST") return onSaveKey(body, csrf, { replace: false });
      if (method === "PUT") return onSaveKey(body, csrf, { replace: true });
      if (method === "DELETE") return onDeleteKey(body, csrf);
    }
    if (method === "POST" && target === "/api/v3/admin/secrets/bailian/connectivity-test") {
      return onConnectivityTest(body, csrf);
    }

    let match = /^\/api\/v3\/sessions\/([^/]+)\/events$/.exec(target);
    if (match) {
      const sessionId = decodeURIComponent(match[1]);
      if (method === "GET") return onReadSessionEvents(sessionId, body);
    }
    match = /^\/api\/v3\/sessions\/([^/]+)$/.exec(target);
    if (match) {
      const sessionId = decodeURIComponent(match[1]);
      if (method === "GET") return onReadSession(sessionId);
      if (method === "DELETE") return onDeleteSession(sessionId, body);
    }
    match = /^\/api\/v3\/admin\/documents\/([^/]+)\/download$/.exec(target);
    if (match && method === "GET") return onDownloadDocument(decodeURIComponent(match[1]), body);
    match = /^\/api\/v3\/admin\/documents\/([^/]+)\/publish$/.exec(target);
    if (match && method === "POST") return onPublishDocument(decodeURIComponent(match[1]), body, csrf);
    match = /^\/api\/v3\/admin\/documents\/([^/]+)\/disable$/.exec(target);
    if (match && method === "POST") return onDisableDocument(decodeURIComponent(match[1]), body, csrf);
    match = /^\/api\/v3\/admin\/documents\/([^/]+)$/.exec(target);
    if (match) {
      const documentId = decodeURIComponent(match[1]);
      if (method === "GET") return onReadDocument(documentId, body);
      if (method === "DELETE") return onDeleteDocument(documentId, body, csrf);
    }

    return fail(404, "resource_not_found", "资源不存在。", false, body);
  };
}

export function resetMockState() {
  state.sessions.clear();
  state.documents.clear();
  state.audit.length = 0;
  state.consumedRequestIds.clear();
  state.admin = { authenticated: false, username: null, csrf: null, generation: 0, bootstrapped: false,
                  mockPasswordOverride: null };
  state.key = { configured: false, last_four: null, updated_at: null, revision: 0 };
  state.counters = { session: 0, document: 0, audit: 0, index: 0, revision: 0 };
  seedDocuments();
}

export default { createMockTransport, resetMockState };
