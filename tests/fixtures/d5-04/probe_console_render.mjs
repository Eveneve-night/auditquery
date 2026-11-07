"use strict";

/*
 * D5-04 C 侧前端渲染 + 交互探针（离线、无网络）。
 *
 * 用最小 DOM 桩通过动态 import 加载 ES module 化的 app.js（boot 时把路由与交互
 * 函数挂到 window.__d5ConsoleTest），再驱动：
 *   1. 访客页：SQL 成功 / RAG 成功（引用逐字校验）/ 澄清闭环 / 解析失败
 *   2. 管理员登录：未注入口令失败、错误口令失败、正确口令成功
 *   3. 管理员控制台：健康检查、文档队列状态机、Key 保存（泄漏扫描）、
 *      连通测试、审计与用量
 *   4. 攻击回归：伪造 CSRF、无身份访问管理端点、路由守卫退回登录
 * 输出结构化结果供 tests/unit/test_d5_frontend_console.py 断言。
 *
 * 本探针不引用 expected / gold / label，不发起模型或网络调用；
 * 演示口令在运行时随机生成，仓库内不留任何默认口令字面量。
 */

import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const __dirname = dirname(fileURLToPath(import.meta.url));
const PROJECT_ROOT = resolve(__dirname, "..", "..", "..");
const CONSOLE_ROOT = resolve(PROJECT_ROOT, "src", "frontend", "d5-console");

// ---- 最小 DOM 桩 ----
const registry = new Map();

function makeNode(tag, className, text) {
  const el = {
    tagName: String(tag).toUpperCase(),
    className: className || "",
    textContent: text !== undefined && text !== null ? String(text) : "",
    hidden: false,
    disabled: false,
    value: "",
    files: null,
    dataset: {},
    attrs: {},
    _listeners: {},
    children: [],
    classList: { add() {}, remove() {}, toggle() {} },
    append(child) { this.children.push(child); return this; },
    prepend(child) { this.children.unshift(child); return this; },
    addEventListener(type, fn) { this._listeners[type] = fn; },
    setAttribute(key, value) { this.attrs[key] = String(value); },
    getAttribute(key) { return this.attrs[key] === undefined ? null : this.attrs[key]; },
    scrollIntoView() {},
  };
  Object.defineProperty(el, "id", {
    get() { return this._id || ""; },
    set(value) { this._id = value; registry.set(value, this); },
  });
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

function byId(id) {
  if (!registry.has(id)) registry.set(id, makeNode("div", "", ""));
  return registry.get(id);
}

const document = {
  getElementById: byId,
  createElement: (tag) => makeNode(tag),
  createTextNode: (text) => ({ nodeType: 3, textContent: String(text) }),
  querySelectorAll: () => [],
};

const win = {
  location: { hash: "#/guest" },
  addEventListener() {},
  crypto: undefined,
};

globalThis.document = document;
globalThis.window = win;

const appUrl = "file:///" + resolve(CONSOLE_ROOT, "app.js").replace(/\\/g, "/");
await import(appUrl);

const manifestUrl =
  "file:///" + resolve(PROJECT_ROOT, "src", "frontend", "d4-console", "rag-manifest.js").replace(/\\/g, "/");
const { matchesSourceText } = await import(manifestUrl);

const test = globalThis.window.__d5ConsoleTest;
if (!test || typeof test.guestAsk !== "function") {
  throw new Error("app.js 未暴露 __d5ConsoleTest，探针失败");
}

// ---- 辅助 ----
function nodeText(node) {
  if (!node) return "";
  if (node.children && node.children.length) {
    return node.children.map(nodeText).join(" ");
  }
  return node.textContent || "";
}

function textOf(id) {
  return nodeText(byId(id));
}

function childTexts(id) {
  return (byId(id).children || []).map(nodeText);
}

function isHidden(id) {
  return Boolean(byId(id).hidden);
}

function activeView() {
  // 三个视图共用 #view 容器，切换时用 data-view 标记当前视图
  return byId("view").attrs["data-view"] || "";
}

const out = { guest: {}, admin: {}, attacks: {}, leak: {}, sourceChecks: {} };

// ---- 引用逐字比对非空转（正例 / 反例 / 边界，防“恒通过”空转）----
const SRC_OK =
  "[SALES-001] 月度经营报表的截止时间是次月第一个工作日 18:00。截止后到达的数据进入下一次修订版，不静默覆盖已发布报表。";
out.sourceChecks = {
  exactMatch: matchesSourceText("SALES-001", SRC_OK) === true,
  tamperedSnippet:
    matchesSourceText(
      "SALES-001",
      "[SALES-001] 月度经营报表的截止时间是次月第一个休息日 18:00。"
    ) === false,
  unknownId: matchesSourceText("NO-SUCH-EVID", "任意文本") === null,
  emptySnippet: matchesSourceText("SALES-001", "") === null,
};

// ================= 访客流 =================
test.resetAll();
await test.navigate("#/guest");

await test.guestAsk("统计艺术家数量");
out.guest.sql = {
  view: activeView(),
  resultVisible: !isHidden("guest-result"),
  techHidden: isHidden("guest-tech"),
  status: textOf("guest-result-status"),
  message: textOf("guest-result-message"),
  sqlCode: textOf("guest-sql"),
  tableRows: (byId("guest-result-table").children[1] || { children: [] }).children.length,
  emptyHidden: isHidden("guest-empty"),
  session: textOf("guest-session"),
};

await test.guestAsk("月度经营报表的截止时间是什么时候");
out.guest.rag = {
  status: textOf("guest-result-status"),
  citations: childTexts("guest-citations"),
  techHidden: isHidden("guest-tech"),
};

await test.guestAsk("hi");
out.guest.clarify = {
  visible: !isHidden("guest-clarify"),
  title: textOf("guest-clarify-title"),
  options: childTexts("guest-clarify-options"),
  status: textOf("guest-result-status"),
};

await test.guestClarify("count", "value_evidence.metric_count");
out.guest.clarifyAfter = {
  status: textOf("guest-result-status"),
  sqlCode: textOf("guest-sql"),
  clarifyHidden: isHidden("guest-clarify"),
};

// SQL 拒绝（SafetyGate）
await test.guestAsk("删除 Artist 表");
out.guest.sqlRefused = {
  status: textOf("guest-result-status"),
  sqlCode: textOf("guest-sql"),
};

// RAG 拒答（证据未越门槛）
await test.guestAsk("公司健身房几点开门可以洗澡吗");
out.guest.ragRefused = {
  status: textOf("guest-result-status"),
  citationsHidden: isHidden("guest-citations"),
};

// ================= 管理员流 =================
test.resetAll();
await test.navigate("#/admin/settings");
out.admin.settingsEntry = { view: activeView(), route: win.location.hash,
  passwordEmpty: byId("a-login-pass").value === "" };

const noPassword = await test.adminLogin("admin", "whatever-long-enough-password");
out.admin.noPassword = {
  ok: noPassword,
  error: textOf("a-login-error"),
  view: activeView(),
};

const demoPassword = "pw." + Date.now().toString(36) + ".mockonly";
win.D5_MOCK_ADMIN_PASSWORD = demoPassword;

const wrongPassword = await test.adminLogin("admin", demoPassword + "x");
out.admin.wrongPassword = { ok: wrongPassword, error: textOf("a-login-error") };

const loggedIn = await test.adminLogin("admin", demoPassword);
out.admin.login = {
  ok: loggedIn,
  csrfPresent: Boolean(test.state.admin.csrf),
  view: activeView(),
};

await test.navigate("#/admin");
out.admin.console = {
  view: activeView(),
  health: textOf("a-health"),
  docs: childTexts("a-docs"),
  key: textOf("a-key-status"),
  audit: textOf("a-audit"),
  usage: textOf("a-usage"),
  documentIds: test.state.documentIds.slice(),
};

// 密钥输入框：类型 / 初始值 / 不预填
out.admin.keyInput = {
  type: byId("a-key-input").attrs.type || "",
  value: test.keyInputValue(),
  autocomplete: byId("a-key-input").attrs.autocomplete || "",
};

// ---- 访客提问不得进入审计原文 ----
const canaryQuestion = "canary-question-ZQ7X";
await test.ensureVisitorSession();
await test.client.submitQuery({
  sessionId: test.state.visitor.sessionId,
  turnId: 0,
  stateRevision: 0,
  question: canaryQuestion,
});
await test.refreshAudit();
out.leak.canaryInAudit = textOf("a-audit").includes(canaryQuestion);

// ---- Key 保存与泄漏扫描 ----
const canaryKey = "sk-canary-ABCD1234";
test.setKeyInput(canaryKey);
await test.saveKey(canaryKey);
out.admin.afterSave = {
  key: textOf("a-key-status"),
  inputValue: test.keyInputValue(),
  notice: textOf("a-notice"),
};
out.leak.canaryInKeyPanel = textOf("a-key-status").includes(canaryKey);
out.leak.canaryInConsole = nodeText(byId("view")).includes(canaryKey);
out.leak.lastFourOnly = textOf("a-key-status").includes("1234");
// 对照组：同一扫描路径必须能命中页面里确实存在的文本，证明「零命中」不是空转
out.leak.controlFoundInConsole = nodeText(byId("view")).includes("末四位");

await test.testConnectivity();
out.admin.connectivity = textOf("a-notice");

// ---- 文档状态机 ----
const ids = test.state.documentIds.slice();
await test.disableDocument(ids[0]);
out.admin.afterDisable = { notice: textOf("a-notice"), docs: childTexts("a-docs") };

await test.publishDocument(ids[1]); // 失败态文档，应 state_conflict
out.admin.publishFailedDoc = { notice: textOf("a-notice") };

await test.publishDocument(ids[2]);
out.admin.afterPublish = { notice: textOf("a-notice"), docs: childTexts("a-docs") };

// ---- 攻击回归：伪造 CSRF / 无身份访问 ----
out.attacks.forgedCsrf = "no-error";
try {
  await test.client.adminPublishDocument(ids[2], { csrf: "forged-csrf-token-0000000000000000" });
} catch (error) {
  out.attacks.forgedCsrf = error.code;
}

out.attacks.missingCsrf = "no-error";
try {
  await test.client.adminDeleteKey({ csrf: null });
} catch (error) {
  out.attacks.missingCsrf = error.code;
}

// ---- 注销后无身份访问管理端点与路由守卫 ----
await test.adminLogout();
out.admin.afterLogout = { view: activeView(), error: textOf("a-login-error"), route: win.location.hash };

out.attacks.healthWithoutSession = "no-error";
try {
  await test.client.adminHealth();
} catch (error) {
  out.attacks.healthWithoutSession = error.code;
}

await test.navigate("#/admin");
out.admin.guard = { view: activeView(), error: textOf("a-login-error") };

// ---- 事件绑定回归：视图重建后表单监听器不丢失 ----
// 修复前：guest.js / admin.js 用模块级 shellBound 标志只绑一次，切换视图后
// 新表单元素无 submit 监听，导致「切回访客页无法提问」「注销后无法再登录」。
function hasSubmitListener(id) {
  const el = byId(id);
  return Boolean(el && el._listeners && el._listeners.submit);
}
test.resetAll();
await test.navigate("#/guest");
out.rebind = { guestInitial: hasSubmitListener("guest-ask-form") };
await test.navigate("#/admin/login");
await test.navigate("#/guest");
out.rebind.guestAfterReturn = hasSubmitListener("guest-ask-form");
// 管理员登录 → 注销（重新渲染登录页）后，登录表单仍应有提交监听
win.D5_MOCK_ADMIN_PASSWORD = demoPassword;
await test.navigate("#/admin/login");
await test.adminLogin("admin", demoPassword);
await test.navigate("#/admin");
await test.adminLogout();
out.rebind.loginAfterLogout = hasSubmitListener("a-login-form");

// ---- v2 固定 admin / 可选改密回归 ----
await test.navigate("#/admin/login");
out.admin.fixedAccount = {
  username: byId("a-login-user").value,
  readonly: byId("a-login-user").attrs.readonly,
  passwordEmpty: byId("a-login-pass").value === "",
};
await test.adminLogin("admin", demoPassword);
await test.navigate("#/admin/settings");
const replacement = "new." + Date.now().toString(36) + ".mockonly";
byId("a-current-password").value = demoPassword;
byId("a-new-password").value = replacement;
byId("a-confirm-password").value = replacement;
const changed = await test.changePassword();
out.admin.passwordChange = {
  changed,
  view: activeView(),
  signedOut: !test.state.admin.authenticated,
  currentCleared: byId("a-current-password").value === "",
  nextCleared: byId("a-new-password").value === "",
  confirmCleared: byId("a-confirm-password").value === "",
  oldPasswordRejected: !(await test.adminLogin("admin", demoPassword)),
  newPasswordAccepted: await test.adminLogin("admin", replacement),
};

console.log(JSON.stringify(out));
