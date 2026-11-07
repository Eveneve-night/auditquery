"use strict";

/*
 * D5-04 C 侧前端：D5 API v3 客户端（src/frontend/d5-console/api-client.js）。
 *
 * 契约事实源：
 * - configs/contracts/d5/product-layer.v1.json（24 端点、错误目录、角色与 CSRF 规则）
 * - configs/schemas/d5/api-v3.schema.json（请求/响应字段与常量）
 *
 * 边界（与任务卡 D5-04 一致）：
 * - 客户端只负责构造契约请求、发送、归一化错误；权限一律以服务器返回为准，
 *   路由守卫只改善体验，不构成安全边界。
 * - 客户端不持久化任何东西：会话 ID、CSRF 令牌只存在于内存，不写
 *   localStorage / sessionStorage / document.cookie。
 * - API Key 只在单次请求体中向服务端提交，客户端不缓存、不回显、不写日志。
 * - 不发起真实模型调用；连通测试是独立操作且必须显式确认（explicit_confirmation=true）。
 */

export const API_VERSION = "3.0";
export const CONTRACT_VERSION = "1.0.0";
export const ADMIN_CONTRACT_VERSION = "2.0.0";
export const CSRF_HEADER = "x-csrf-token";
// Legacy fixture exports only; live requests use encrypted model-settings.
export const PROVIDER = "bailian_beijing";
export const MODEL = "qwen3.7-flash";

let seq = 0;

export function newRequestId(prefix = "req") {
  seq += 1;
  return `${prefix}.${seq}.${Date.now().toString(36)}`;
}

export class ApiError extends Error {
  constructor(code, message, retryable, httpStatus) {
    super(message || code);
    this.name = "ApiError";
    this.code = code || "internal_error";
    this.retryable = Boolean(retryable);
    this.httpStatus = httpStatus === undefined ? 500 : httpStatus;
  }
}

export function createClient(options = {}) {
  const transport = options.transport;
  if (typeof transport !== "function") {
    throw new Error("createClient 需要 transport(method, path, opts)");
  }

  async function send(method, path, opts = {}) {
    const result = await transport(method, path, opts);
    const json = (result && result.json) || {};
    const status = result && typeof result.status === "number" ? result.status : 200;
    if (status >= 400) {
      const error = json.error || {};
      throw new ApiError(
        error.code || "internal_error",
        error.message || "操作失败。",
        error.retryable,
        status,
      );
    }
    // 二进制下载（raw）：返回 transport 原始结果 {status, blob, contentType}。
    if (opts.raw) {
      return result;
    }
    return json;
  }

  function envelope() {
    return { api_version: API_VERSION, contract_version: CONTRACT_VERSION, request_id: newRequestId() };
  }

  function adminEnvelope() {
    return { api_version: API_VERSION, contract_version: ADMIN_CONTRACT_VERSION, request_id: newRequestId() };
  }

  return {
    transport,

    /* ---- 访客端点（6） ---- */
    createVisitorSession() {
      return send("POST", "/api/v3/sessions", { body: envelope() });
    },
    readOwnSession(sessionId) {
      return send("GET", `/api/v3/sessions/${encodeURIComponent(sessionId)}`);
    },
    deleteOwnSession(sessionId) {
      return send("DELETE", `/api/v3/sessions/${encodeURIComponent(sessionId)}`);
    },
    submitQuery({ sessionId, turnId, stateRevision, question }) {
      return send("POST", "/api/v3/query", {
        body: {
          ...envelope(),
          session_id: sessionId,
          turn_id: turnId,
          state_revision: stateRevision,
          question,
        },
      });
    },
    submitClarification({ sessionId, requestId, turnId, stateRevision, questionId, slotId, value, evidenceRef }) {
      return send("POST", "/api/v3/clarifications", {
        body: {
          ...envelope(),
          answer_id: newRequestId("answer"),
          session_id: sessionId,
          request_id: requestId,
          turn_id: turnId,
          state_revision: stateRevision,
          question_id: questionId,
          slot_id: slotId,
          values: [value],
          evidence_refs: [evidenceRef],
        },
      });
    },
    readOwnEvents(sessionId) {
      return send("GET", `/api/v3/sessions/${encodeURIComponent(sessionId)}/events`);
    },

    /* ---- 引导与登录（2） ---- */
    bootstrapAdmin({ setupToken, username, password }) {
      return send("POST", "/api/v3/auth/admin/bootstrap", {
        body: { ...envelope(), setup_token: setupToken, username, password },
      });
    },
    adminLogin({ username, password }) {
      return send("POST", "/api/v3/auth/admin/login", {
        body: { ...adminEnvelope(), username, password },
      });
    },

    adminChangePassword({ currentPassword, newPassword, csrf }) {
      return send("POST", "/api/v3/admin/password", {
        csrf,
        body: { ...adminEnvelope(), current_password: currentPassword, new_password: newPassword },
      });
    },

    /* ---- 管理端点（16） ---- */
    adminLogout({ csrf }) {
      return send("POST", "/api/v3/admin/logout", { csrf, body: {} });
    },
    adminRevokeAllSessions({ csrf }) {
      return send("POST", "/api/v3/admin/sessions/revoke-all", { csrf, body: {} });
    },
    adminHealth() {
      return send("GET", "/api/v3/admin/health");
    },
    adminUploadDocument({ filename, declaredBytes, contentSha256, csrf, file }) {
      // file 为 File/Blob 时走受控 multipart/FormData（字段约定：metadata JSON + file 字节）；
      // 不传 file 时保持 JSON 元数据提交（离线 mock / 无字节场景）。
      return send("POST", "/api/v3/admin/documents", {
        csrf,
        file,
        body: {
          ...envelope(),
          filename,
          content_type: "application/pdf",
          declared_bytes: declaredBytes,
          content_sha256: contentSha256,
        },
      });
    },
    adminReadDocument(documentId) {
      return send("GET", `/api/v3/admin/documents/${encodeURIComponent(documentId)}`);
    },
    adminPublishDocument(documentId, { csrf }) {
      return send("POST", `/api/v3/admin/documents/${encodeURIComponent(documentId)}/publish`, { csrf, body: {} });
    },
    adminDisableDocument(documentId, { csrf }) {
      return send("POST", `/api/v3/admin/documents/${encodeURIComponent(documentId)}/disable`, { csrf, body: {} });
    },
    adminDeleteDocument(documentId, { csrf }) {
      return send("DELETE", `/api/v3/admin/documents/${encodeURIComponent(documentId)}`, { csrf });
    },
    adminDownloadDocument(documentId) {
      return send("GET", `/api/v3/admin/documents/${encodeURIComponent(documentId)}/download`, { raw: true });
    },
    adminReadKeyStatus() {
      return send("GET", "/api/v3/admin/model-settings");
    },
    adminSaveKey({ apiKey, csrf, config, expectedRevision }) {
      return send("PUT", "/api/v3/admin/model-settings", {
        csrf,
        body: { config, api_key: apiKey, expected_revision: expectedRevision },
      });
    },
    adminReplaceKey({ apiKey, csrf, config, expectedRevision }) {
      return send("PUT", "/api/v3/admin/model-settings", {
        csrf,
        body: { config, api_key: apiKey, expected_revision: expectedRevision },
      });
    },
    adminDeleteKey({ csrf, expectedRevision }) {
      return send("DELETE", "/api/v3/admin/model-settings", { csrf, body: { expected_revision: expectedRevision } });
    },
    adminTestKeyConnectivity({ csrf }) {
      return send("POST", "/api/v3/admin/model-settings/test", {
        csrf,
        body: { explicit_confirmation: true },
      });
    },
    adminReadAudit() {
      return send("GET", "/api/v3/admin/audit-events");
    },
    adminReadUsage() {
      return send("GET", "/api/v3/admin/usage-summary");
    },
  };
}
