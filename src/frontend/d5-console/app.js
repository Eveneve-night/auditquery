"use strict";

/*
 * D5-04 C 侧前端：双角色控制台骨架（src/frontend/d5-console/app.js）。
 *
 * 路由（hash）：#/guest 访客页 · #/admin/login 管理员登录 · #/admin 管理员控制台。
 * 事实源：configs/contracts/d5/product-layer.v1.json（24 端点）、
 *         configs/schemas/d5/api-v3.schema.json（字段与常量）。
 *
 * 安全边界：
 * - 路由守卫只改善体验，不构成安全边界：进入 #/admin 仍以 GET /admin/health
 *   的服务端结果为准，401/403 一律退回登录页。
 * - 不含任何持久化：会话 ID、CSRF 令牌只存在于内存对象。
 * - 默认离线 mock（src/frontend/d5-console/mock/index.js）；接线真实后端时
 *   注入 window.D5_API_TRANSPORT(method, path, {body, csrf}) 即可，页面代码不变。
 */

import { createClient, ApiError } from "./api-client.js";
import { createMockTransport, resetMockState } from "./mock/index.js";
import { createLiveTransport } from "./live-transport.js";
import * as guest from "./guest.js";
import * as admin from "./admin.js";
import { byId } from "./dom.js";

const ROUTE_GUEST = "#/guest";
const ROUTE_LOGIN = "#/admin/login";
const ROUTE_ADMIN = "#/admin";
const ROUTE_SETTINGS = "#/admin/settings";

const state = {
  route: ROUTE_GUEST,
  visitor: {
    sessionId: null,
    turnId: 0,
    stateRevision: 0,
    expiresAt: null,
    pending: null,
    turns: [],
  },
  admin: { authenticated: false, csrf: null, username: null },
  documents: [],
  documentIds: [],
  upload: null,
};

// 数据源模式：injected=外部注入 transport；live=真实后端 fetch；mock=离线契约 mock。
function transportMode() {
  const scope = typeof window !== "undefined" ? window : {};
  if (scope.D5_API_TRANSPORT && typeof scope.D5_API_TRANSPORT === "function") return "injected";
  if (scope.D5_LIVE_API === true || typeof scope.D5_LIVE_BASE_URL === "string") return "live";
  return "mock";
}

function resolveTransport() {
  const scope = typeof window !== "undefined" ? window : {};
  const mode = transportMode();
  if (mode === "injected") {
    return scope.D5_API_TRANSPORT;
  }
  if (mode === "live") {
    return createLiveTransport({ baseUrl: scope.D5_LIVE_BASE_URL || "" });
  }
  return createMockTransport();
}

const DATA_SOURCE_LABEL = {
  injected: "在线模式 · 连接真实服务",
  live: "在线模式 · 连接真实服务",
  mock: "演示模式 · 使用示例数据",
};

const ENV_LABEL = {
  injected: "在线服务",
  live: "在线服务",
  mock: "演示环境",
};

const client = createClient({ transport: resolveTransport() });

function viewRoot() {
  return byId("view");
}

const ctx = {
  client,
  state,
  get root() {
    return viewRoot();
  },
  navigate,
};

async function renderRoute(hash, options = {}) {
  const root = viewRoot();
  if (!root) return null;
  state.route = hash;
  if (hash === ROUTE_LOGIN) {
    admin.renderLogin(ctx, root, options.reason);
    return "admin-login";
  }
  if (hash === ROUTE_ADMIN) {
    if (!state.admin.authenticated) {
      await navigate(ROUTE_SETTINGS, { reason: "请先登录。" });
      return "admin-login";
    }
    admin.renderConsole(ctx);
    // 权限以服务端为准：健康检查返回 401/403 时由 admin 模块退回登录页
    await admin.refreshConsole(ctx);
    return "admin";
  }
  if (hash === ROUTE_SETTINGS) {
    if (!state.admin.authenticated) {
      admin.renderLogin(ctx, root, options.reason);
      return "admin-login";
    }
    admin.renderSettings(ctx);
    // Settings is not a privilege boundary; the server still validates each request.
    try {
      await client.adminHealth();
      return "admin-settings";
    } catch (error) {
      if (error instanceof ApiError && [401, 403].includes(error.httpStatus)) {
        state.admin = { authenticated: false, csrf: null, username: null };
        await navigate(ROUTE_SETTINGS, { reason: error.message });
        return "admin-login";
      }
      return "admin-settings";
    }
  }
  guest.renderGuestShell(root);
  guest.bindGuest(ctx);
  return "guest";
}

function currentHash() {
  const scope = typeof window !== "undefined" ? window : {};
  const hash = scope.location && typeof scope.location.hash === "string" ? scope.location.hash : "";
  return hash || ROUTE_GUEST;
}

function navigate(hash, options = {}) {
  const scope = typeof window !== "undefined" ? window : {};
  if (scope.location && typeof scope.location.hash === "string" && scope.location.hash !== hash) {
    scope.location.hash = hash;
  }
  return renderRoute(hash, options);
}

function bindNav() {
  const scope = typeof window !== "undefined" ? window : {};
  if (typeof scope.addEventListener === "function") {
    scope.addEventListener("hashchange", () => {
      const hash = currentHash();
      if (hash !== state.route) renderRoute(hash);
    });
  }
  const links = typeof document !== "undefined" && document.querySelectorAll
    ? document.querySelectorAll("[data-route]")
    : [];
  for (const link of links) {
    link.addEventListener("click", (event) => {
      event.preventDefault();
      navigate(link.getAttribute("data-route") || ROUTE_GUEST);
    });
  }
}

function resetAll() {
  resetMockState();
  state.visitor = { sessionId: null, turnId: 0, stateRevision: 0, expiresAt: null, pending: null, turns: [] };
  state.admin = { authenticated: false, csrf: null, username: null };
  state.documents = [];
  state.documentIds = [];
  state.upload = null;
}

function boot() {
  bindNav();
  const mode = transportMode();
  const badge = byId("mode-badge");
  if (badge) {
    badge.textContent = DATA_SOURCE_LABEL[mode] || DATA_SOURCE_LABEL.mock;
  }
  const env = byId("env-label");
  if (env) {
    env.textContent = ENV_LABEL[mode] || ENV_LABEL.mock;
  }
  renderRoute(currentHash());

  if (typeof window !== "undefined") {
    // 离线探针钩子：供 tests/fixtures/d5-04/probe_console_render.mjs 断言
    window.__d5ConsoleTest = {
      navigate,
      renderRoute,
      resetAll,
      state,
      client,
      ctx,
      ApiError,
      byId,
      guestAsk: (question) => guest.guestAsk(ctx, question),
      guestClarify: (value, evidenceRef) => guest.guestClarify(ctx, value, evidenceRef),
      ensureVisitorSession: () => guest.ensureVisitorSession(ctx),
      renderGuestShell: (root) => guest.renderGuestShell(root || viewRoot()),
      adminLogin: (username, password) => admin.adminLoginSubmit(ctx, username, password),
      renderConsole: () => admin.renderConsole(ctx),
      refreshConsole: () => admin.refreshConsole(ctx),
      loadDocuments: () => admin.loadDocuments(ctx),
      refreshKey: () => admin.refreshKey(ctx),
      refreshAudit: () => admin.refreshAudit(ctx),
      refreshDocuments: () => admin.refreshDocuments(ctx),
      uploadMeta: (meta) => {
        state.upload = meta;
      },
      uploadDocument: () => admin.uploadDocument(ctx),
      publishDocument: (id) => admin.publishDocument(ctx, id),
      disableDocument: (id) => admin.disableDocument(ctx, id),
      deleteDocument: (id) => admin.deleteDocument(ctx, id),
      saveKey: (apiKey) => admin.saveKey(ctx, { replace: false }),
      replaceKey: (apiKey) => admin.saveKey(ctx, { replace: true }),
      deleteKey: () => admin.deleteKey(ctx),
      testConnectivity: () => admin.testConnectivity(ctx),
      adminLogout: () => admin.adminLogout(ctx),
      adminRevokeAll: () => admin.adminRevokeAll(ctx),
      changePassword: () => admin.changePassword(ctx),
      setKeyInput: (value) => {
        const input = byId("a-key-input");
        if (input) input.value = value;
        return input;
      },
      keyInputValue: () => {
        const input = byId("a-key-input");
        return input ? input.value : null;
      },
      textOf: (id) => {
        const element = byId(id);
        return element ? element.textContent : null;
      },
    };
  }
}

boot();
