"use strict";

/*
 * D5-04 C 侧 API v3 契约扫描探针（离线、无网络）。
 *
 * 逐个调用 configs/contracts/d5/product-layer.v1.json 声明的 24 个端点，
 * 输出 {名称, 方法, 路径, 状态码, 响应体} 供 tests/unit/test_d5_frontend_console.py
 * 用 configs/schemas/d5/api-v3.schema.json 逐条校验；同时输出若干「失败关闭」
 * 场景（无身份 / 伪造 CSRF / 重放 / 状态冲突 / 未知会话 / 文件名注入），
 * 校验其错误体同样符合契约的 error_response。
 *
 * 引导令牌与演示口令在运行时随机生成，仓库内不留任何默认凭据。
 */

import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import { sha256Hex } from "../../../src/frontend/d4-console/mock/index.js";
import { createMockTransport } from "../../../src/frontend/d5-console/mock/index.js";

const __dirname = dirname(fileURLToPath(import.meta.url));
const PROJECT_ROOT = resolve(__dirname, "..", "..", "..");

globalThis.D5_MOCK_SETUP_TOKEN = "tok." + Date.now().toString(36) + sha256Hex("sweep").slice(0, 24);
globalThis.D5_MOCK_ADMIN_PASSWORD = "pw." + Date.now().toString(36) + ".mockonly";

const transport = createMockTransport();
const out = [];

async function call(name, method, path, options = {}) {
  const result = await transport(method, path, options);
  out.push({ name, method, path, status: result.status, json: result.json });
  return result.json;
}

const envelope = (n) => ({ api_version: "3.0", contract_version: "1.0.0", request_id: `req.sweep.${n}` });

/* ---- 引导（仅一次可用） ---- */
const bootstrap = await call("bootstrap_admin", "POST", "/api/v3/auth/admin/bootstrap", {
  body: {
    ...envelope(1),
    setup_token: globalThis.D5_MOCK_SETUP_TOKEN,
    username: "sweep.admin",
    password: globalThis.D5_MOCK_ADMIN_PASSWORD,
  },
});
const csrf = bootstrap.csrf_token;
await call("err_bootstrap_replayed", "POST", "/api/v3/auth/admin/bootstrap", {
  body: {
    ...envelope(2),
    setup_token: globalThis.D5_MOCK_SETUP_TOKEN,
    username: "sweep.admin",
    password: globalThis.D5_MOCK_ADMIN_PASSWORD,
  },
});

/* ---- 访客端点 ---- */
const session = await call("create_visitor_session", "POST", "/api/v3/sessions", { body: envelope(3) });
const sessionId = session.session_id;
await call("read_own_session", "GET", `/api/v3/sessions/${sessionId}`);
await call("read_own_events", "GET", `/api/v3/sessions/${sessionId}/events`);

const sql = await call("submit_query", "POST", "/api/v3/query", {
  body: { ...envelope(4), session_id: sessionId, turn_id: 0, state_revision: 0, question: "统计艺术家数量" },
});
const turn = sql.payload.turn_id;
const revision = sql.payload.state_revision;

const clarify = await call("submit_query_clarify", "POST", "/api/v3/query", {
  body: { ...envelope(5), session_id: sessionId, turn_id: turn, state_revision: revision, question: "hi" },
});
const pending = clarify.payload.pending_question;
await call("submit_clarification", "POST", "/api/v3/clarifications", {
  body: {
    ...envelope(6),
    answer_id: "answer.sweep.1",
    session_id: pending.session_id,
    request_id: pending.request_id,
    turn_id: pending.turn_id,
    state_revision: pending.state_revision,
    question_id: pending.question_id,
    slot_id: pending.slot_id,
    values: ["count"],
    evidence_refs: ["value_evidence.metric_count"],
  },
});
await call("read_own_session_after", "GET", `/api/v3/sessions/${sessionId}`);

// 失败关闭：同一 request_id 重放、过期 revision（会话仍然有效）
await call("err_replayed_request", "POST", "/api/v3/query", {
  body: { ...envelope(4), session_id: sessionId, turn_id: 0, state_revision: 0, question: "统计艺术家数量" },
});
await call("err_stale_revision", "POST", "/api/v3/query", {
  body: { ...envelope(7), session_id: sessionId, turn_id: 0, state_revision: 99, question: "统计艺术家数量" },
});
await call("err_unknown_session", "GET", "/api/v3/sessions/sess.does.not.exist");

/* ---- 管理端点 ---- */
await call("admin_health", "GET", "/api/v3/admin/health");
await call("admin_read_key_status", "GET", "/api/v3/admin/secrets/bailian");
await call("admin_save_key", "POST", "/api/v3/admin/secrets/bailian", {
  csrf,
  body: { ...envelope(8), provider: "bailian_beijing", model: "qwen3.7-flash", api_key: "sk-sweep-key-AAAA0001" },
});
await call("admin_replace_key", "PUT", "/api/v3/admin/secrets/bailian", {
  csrf,
  body: { ...envelope(9), provider: "bailian_beijing", model: "qwen3.7-flash", api_key: "sk-sweep-key-BBBB0002" },
});
await call("admin_test_key_connectivity", "POST", "/api/v3/admin/secrets/bailian/connectivity-test", {
  csrf,
  body: { ...envelope(10), explicit_confirmation: true },
});
await call("admin_delete_key", "DELETE", "/api/v3/admin/secrets/bailian", { csrf });

const uploaded = await call("admin_upload_document", "POST", "/api/v3/admin/documents", {
  csrf,
  body: {
    ...envelope(11),
    filename: "销售分析手册.pdf",
    content_type: "application/pdf",
    declared_bytes: 184320,
    content_sha256: sha256Hex("sweep-document"),
  },
});
const documentId = uploaded.document_id;
await call("err_path_traversal_upload", "POST", "/api/v3/admin/documents", {
  csrf,
  body: {
    ...envelope(12),
    filename: "../../etc/passwd.pdf",
    content_type: "application/pdf",
    declared_bytes: 1024,
    content_sha256: sha256Hex("sweep-evil"),
  },
});
await call("admin_read_document", "GET", `/api/v3/admin/documents/${documentId}`);
await call("admin_publish_document", "POST", `/api/v3/admin/documents/${documentId}/publish`, { csrf, body: {} });
await call("admin_disable_document", "POST", `/api/v3/admin/documents/${documentId}/disable`, { csrf, body: {} });
await call("admin_download_document", "GET", `/api/v3/admin/documents/${documentId}/download`);
await call("admin_delete_document", "DELETE", `/api/v3/admin/documents/${documentId}`, { csrf });
await call("admin_read_audit", "GET", "/api/v3/admin/audit-events");
await call("admin_read_usage", "GET", "/api/v3/admin/usage-summary");

// 失败关闭：伪造 CSRF（会话仍有效）
await call("err_forged_csrf", "POST", "/api/v3/admin/logout", {
  csrf: "forged-csrf-token-0000000000000000000000000000000000",
  body: {},
});
await call("admin_logout", "POST", "/api/v3/admin/logout", { csrf, body: {} });

const login = await call("admin_login", "POST", "/api/v3/auth/admin/login", {
  body: { ...envelope(13), username: "sweep.admin", password: globalThis.D5_MOCK_ADMIN_PASSWORD },
});
await call("admin_revoke_all_sessions", "POST", "/api/v3/admin/sessions/revoke-all", {
  csrf: login.csrf_token,
  body: {},
});
await call("err_health_without_session", "GET", "/api/v3/admin/health");
await call("delete_own_session", "DELETE", `/api/v3/sessions/${sessionId}`);

console.log(JSON.stringify({ calls: out, endpoint_count: 24 }));
