"use strict";

/*
 * D5-04 C 侧前端：管理员页（src/frontend/d5-console/admin.js）。
 *
 * 任务卡要求：登录、文档队列、解析状态、发布/停用、Key 状态和健康检查。
 * 红线（契约 security.api_key 与任务卡）：
 * - 密钥输入框不预填、不写 localStorage/sessionStorage、不提供“显示已保存 Key”，
 *   页面只展示 configured / provider / model / source / updated_at / last_four；
 * - CSRF 令牌只存内存，随管理员会话走，页面不判定权限：401/403 一律按服务端
 *   返回展示并退回登录；
 * - 连通测试是独立操作，需要 explicit_confirmation，且不触发任何模型调用。
 */

import { byId, node, clear, setHidden } from "./dom.js";
import { ApiError } from "./api-client.js";

const STATE_TEXT = {
  quarantined: "隔离待检",
  validating: "校验中",
  validated: "校验通过",
  indexing: "索引中",
  published: "已发布",
  disabled: "已停用",
  failed: "失败",
  deleted: "已删除",
};

const IDS = {
  loginRoot: "admin-login",
  loginForm: "a-login-form",
  loginUser: "a-login-user",
  loginPass: "a-login-pass",
  loginSubmit: "a-login-submit",
  loginError: "a-login-error",
  loginNote: "a-login-note",
  consoleRoot: "admin-console",
  notice: "a-notice",
  health: "a-health",
  docs: "a-docs",
  docPicker: "a-doc-picker",
  docMeta: "a-doc-meta",
  docUpload: "a-doc-upload",
  docRefresh: "a-doc-refresh",
  keyStatus: "a-key-status",
  keyInput: "a-key-input",
  keySave: "a-key-save",
  keyReplace: "a-key-replace",
  keyDelete: "a-key-delete",
  keyTest: "a-key-test",
  audit: "a-audit",
  usage: "a-usage",
  logout: "a-logout",
  revoke: "a-revoke",
  settings: "a-settings",
  changeForm: "a-change-form",
  currentPassword: "a-current-password",
  newPassword: "a-new-password",
  confirmPassword: "a-confirm-password",
  changeSubmit: "a-change-submit",
};

let currentCtx = null;

function setNotice(text, kind) {
  const box = byId(IDS.notice);
  if (!box) return;
  clear(box);
  if (!text) {
    setHidden(box, true);
    return;
  }
  setHidden(box, false);
  box.className = `notice ${kind === "bad" ? "notice--bad" : "notice--ok"}`;
  box.textContent = text;
}

async function handleError(ctx, error, fallback) {
  const code = error instanceof ApiError ? error.code : "client_error";
  const message = (error && error.message) || fallback || "操作失败。";
  if (code === "authentication_required" || code === "forbidden" || code === "csrf_invalid") {
    ctx.state.admin = { authenticated: false, csrf: null, username: null };
    if (typeof ctx.navigate === "function") {
      await ctx.navigate("#/admin/settings", { reason: message });
      return code;
    }
  }
  setNotice(`${message}（${code}）`, "bad");
  return code;
}

/* ---- 登录 ---- */

export function renderLogin(ctx, root, reason) {
  currentCtx = ctx;
  clear(root);
  root.setAttribute("data-view", IDS.loginRoot);
  root.append(node("h2", "", "设置 · 管理员登录"));

  const note = node("p", "hint", "管理员账号固定为 admin。页面不持久化密码；离线 mock 需单独注入测试口令。");
  note.id = IDS.loginNote;
  root.append(note);

  const form = node("form", "login-form");
  form.id = IDS.loginForm;
  form.setAttribute("autocomplete", "off");
  const user = node("input", "login-form__input");
  user.id = IDS.loginUser;
  user.setAttribute("type", "text");
  user.value = "admin";
  user.setAttribute("readonly", "readonly");
  user.setAttribute("autocomplete", "off");
  user.setAttribute("aria-label", "管理员用户名");
  const pass = node("input", "login-form__input");
  pass.id = IDS.loginPass;
  pass.setAttribute("type", "password");
  pass.setAttribute("placeholder", "口令");
  pass.setAttribute("autocomplete", "off");
  pass.setAttribute("aria-label", "管理员口令");
  const submit = node("button", "login-form__submit", "登录");
  submit.id = IDS.loginSubmit;
  submit.setAttribute("type", "submit");
  form.append(user);
  form.append(pass);
  form.append(submit);
  root.append(form);

  const error = node("div", "error-box");
  error.id = IDS.loginError;
  setHidden(error, true);
  if (reason) {
    setHidden(error, false);
    error.textContent = reason;
  }
  root.append(error);

  // 每次 renderLogin 都 clear(root) 重建表单，必须重新绑定新元素；不能用模块级
  // 「已绑定」标志跳过，否则注销后再登录表单无 submit 监听。事件触发时实时取
  // 输入值，避免闭包捕获被替换的旧元素。
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const ctx = currentCtx;
    if (ctx) {
      const userInput = byId(IDS.loginUser);
      const passInput = byId(IDS.loginPass);
      adminLoginSubmit(ctx, userInput ? userInput.value : "admin", passInput ? passInput.value : "");
    }
  });
  return IDS;
}

export async function adminLoginSubmit(ctx, username, password) {
  currentCtx = ctx;
  const errorBox = byId(IDS.loginError);
  const submit = byId(IDS.loginSubmit);
  if (submit) {
    submit.disabled = true;
    submit.textContent = "登录中…";
  }
  try {
    const response = await ctx.client.adminLogin({ username: "admin", password });
    ctx.state.admin = { authenticated: true, csrf: response.csrf_token, username: "admin" };
    if (typeof ctx.navigate === "function") await ctx.navigate("#/admin");
    return true;
  } catch (error) {
    if (errorBox) {
      setHidden(errorBox, false);
      errorBox.textContent = (error && error.message) || "登录失败。";
    }
    return false;
  } finally {
    const passInput = byId(IDS.loginPass);
    if (passInput) passInput.value = "";
    if (submit) {
      submit.disabled = false;
      submit.textContent = "登录";
    }
  }
}

/* ---- 控制台 ---- */

export function renderConsole(ctx) {
  currentCtx = ctx;
  const root = ctx.root;
  clear(root);
  root.setAttribute("data-view", IDS.consoleRoot);

  const notice = node("div", "notice");
  notice.id = IDS.notice;
  setHidden(notice, true);
  root.append(notice);

  const bar = node("div", "console-bar");
  const logout = node("button", "ghost-button", "注销");
  logout.id = IDS.logout;
  logout.setAttribute("type", "button");
  const revoke = node("button", "ghost-button", "吊销全部会话");
  revoke.id = IDS.revoke;
  revoke.setAttribute("type", "button");
  logout.addEventListener("click", () => adminLogout(ctx));
  revoke.addEventListener("click", () => adminRevokeAll(ctx));
  const settings = node("button", "ghost-button", "设置 / 修改密码");
  settings.id = IDS.settings;
  settings.setAttribute("type", "button");
  settings.addEventListener("click", () => ctx.navigate("#/admin/settings"));
  bar.append(logout);
  bar.append(revoke);
  bar.append(settings);
  root.append(bar);

  root.append(section("健康检查", IDS.health));
  root.append(buildDocumentSection(ctx));
  root.append(buildKeySection(ctx));
  root.append(section("审计事件（脱敏）", IDS.audit));
  root.append(section("用量摘要", IDS.usage));
  return IDS;
}

export function renderSettings(ctx) {
  currentCtx = ctx;
  const root = ctx.root;
  clear(root);
  root.setAttribute("data-view", IDS.settings);
  root.append(node("h2", "", "管理员设置"));
  root.append(node("p", "hint", "修改密码为可选操作；提交成功后所有旧会话失效，请重新登录。页面不持久化密码。"));
  const notice = node("div", "notice");
  notice.id = IDS.notice;
  setHidden(notice, true);
  root.append(notice);
  const form = node("form", "login-form");
  form.id = IDS.changeForm;
  form.setAttribute("autocomplete", "off");
  for (const [id, label] of [
    [IDS.currentPassword, "当前密码"],
    [IDS.newPassword, "新密码（至少 12 位）"],
    [IDS.confirmPassword, "确认新密码"],
  ]) {
    const input = node("input", "login-form__input");
    input.id = id;
    input.setAttribute("type", "password");
    input.setAttribute("placeholder", label);
    input.setAttribute("aria-label", label);
    input.setAttribute("autocomplete", "off");
    form.append(input);
  }
  const submit = node("button", "primary-button", "修改密码");
  submit.id = IDS.changeSubmit;
  submit.setAttribute("type", "submit");
  form.append(submit);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    changePassword(ctx);
  });
  root.append(form);
  const back = node("button", "ghost-button", "返回控制台");
  back.setAttribute("type", "button");
  back.addEventListener("click", () => ctx.navigate("#/admin"));
  root.append(back);
  return IDS;
}

export async function changePassword(ctx) {
  const current = byId(IDS.currentPassword);
  const next = byId(IDS.newPassword);
  const confirm = byId(IDS.confirmPassword);
  const submit = byId(IDS.changeSubmit);
  const currentPassword = current ? current.value : "";
  const newPassword = next ? next.value : "";
  const confirmation = confirm ? confirm.value : "";
  if (!currentPassword || newPassword.length < 12 || newPassword !== confirmation || newPassword === currentPassword) {
    setNotice("请输入当前密码，并确认至少 12 位且不同于当前密码的新密码。", "bad");
    return false;
  }
  if (submit) submit.disabled = true;
  try {
    await ctx.client.adminChangePassword({ currentPassword, newPassword, csrf: ctx.state.admin.csrf });
    ctx.state.admin = { authenticated: false, csrf: null, username: null };
    if (typeof ctx.navigate === "function") await ctx.navigate("#/admin/settings", { reason: "密码已修改，请重新登录。" });
    return true;
  } catch (error) {
    await handleError(ctx, error, "修改密码失败。");
    return false;
  } finally {
    for (const input of [current, next, confirm]) if (input) input.value = "";
    if (submit) submit.disabled = false;
  }
}

function section(title, id) {
  const box = node("section", "panel");
  box.append(node("h3", "", title));
  const body = node("div", "chip-row");
  body.id = id;
  box.append(body);
  return box;
}

function buildDocumentSection(ctx) {
  const box = node("section", "panel");
  box.append(node("h3", "", "文档队列与解析状态"));

  const picker = node("input", "file-input");
  picker.id = IDS.docPicker;
  picker.setAttribute("type", "file");
  picker.setAttribute("accept", "application/pdf");
  const meta = node("div", "chip-row");
  meta.id = IDS.docMeta;
  meta.append(node("span", "chip", "未选择文件"));
  const upload = node("button", "primary-button", "上传并校验");
  upload.id = IDS.docUpload;
  upload.setAttribute("type", "button");
  upload.disabled = true;
  const refresh = node("button", "ghost-button", "刷新队列");
  refresh.id = IDS.docRefresh;
  refresh.setAttribute("type", "button");

  picker.addEventListener("change", async () => {
    const file = picker.files && picker.files[0];
    if (!file) return;
    const digest = await sha256OfFile(file);
    // 保留 File 对象（仅内存引用，不落本地存储），供 multipart 上传提交字节；
    // 字节数/SHA-256 先算好随 metadata 提交，服务端再复核，不绕过服务端校验。
    ctx.state.upload = digest
      ? { filename: file.name, declaredBytes: file.size, contentSha256: digest, file }
      : null;
    clear(meta);
    if (!digest) {
      meta.append(node("span", "chip chip--warn", "当前上下文无法计算 SHA-256（需 https 或 localhost）"));
      upload.disabled = true;
      return;
    }
    meta.append(node("span", "chip", file.name));
    meta.append(node("span", "chip", `${file.size} 字节`));
    meta.append(node("span", "chip", `sha256 ${digest.slice(0, 16)}…`));
    upload.disabled = false;
  });
  upload.addEventListener("click", () => uploadDocument(ctx));
  refresh.addEventListener("click", () => refreshDocuments(ctx));

  box.append(picker);
  box.append(meta);
  box.append(upload);
  box.append(refresh);
  const list = node("div", "doc-list");
  list.id = IDS.docs;
  box.append(list);
  return box;
}

function buildKeySection(ctx) {
  const box = node("section", "panel");
  box.append(node("h3", "", "模型服务（OpenAI 兼容 Chat Completions）"));
  for (const [key, label] of Object.entries({provider_id:"服务商标识",model:"模型 ID",base_url:"Base URL（不含 /chat/completions）",local:"本地模式 true/false",auth_mode:"认证 bearer/none",input_per_million_tokens:"输入单价 CNY/百万 token",output_per_million_tokens:"输出单价 CNY/百万 token"})) {
    const field = node("input", ""); field.id = `model-${key}`;
    field.setAttribute("aria-label", label); field.setAttribute("placeholder",label);
    box.append(field);
  }
  box.append(node("p", "", "远程服务必须 HTTPS；本地 HTTP/无认证须明确开启。单价留空为未知，发送前拒绝；保存不调用模型。"));

  const status = node("div", "chip-row");
  status.id = IDS.keyStatus;
  box.append(status);

  // 不预填、不回显、不提供“显示已保存 Key”
  const input = node("input", "key-input");
  input.id = IDS.keyInput;
  input.setAttribute("type", "password");
  input.setAttribute("placeholder", "仅在保存 / 替换时输入，提交后不留痕");
  input.setAttribute("aria-label", "API Key");
  input.setAttribute("autocomplete", "off");
  box.append(input);

  const save = node("button", "primary-button", "保存 Key");
  save.id = IDS.keySave;
  const replace = node("button", "ghost-button", "替换 Key");
  replace.id = IDS.keyReplace;
  const remove = node("button", "ghost-button", "删除 Key");
  remove.id = IDS.keyDelete;
  const test = node("button", "ghost-button", "连通测试（显式确认）");
  test.id = IDS.keyTest;
  for (const button of [save, replace, remove, test]) button.setAttribute("type", "button");
  save.addEventListener("click", () => saveKey(ctx, { replace: false }));
  replace.addEventListener("click", () => saveKey(ctx, { replace: true }));
  remove.addEventListener("click", () => deleteKey(ctx));
  test.addEventListener("click", () => testConnectivity(ctx));
  box.append(save);
  box.append(replace);
  box.append(remove);
  box.append(test);
  return box;
}

async function sha256OfFile(file) {
  const subtle = (typeof window !== "undefined" && window.crypto && window.crypto.subtle) || null;
  if (!subtle || typeof file.arrayBuffer !== "function") return null;
  const buffer = await file.arrayBuffer();
  const digest = await subtle.digest("SHA-256", buffer);
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

/* ---- 数据刷新 ---- */

export async function refreshHealth(ctx) {
  const box = byId(IDS.health);
  if (!box) return null;
  clear(box);
  try {
    const response = await ctx.client.adminHealth();
    const summary = response.summary || {};
    ctx.state.documentIds = (summary.documents && summary.documents.ids) || [];
    box.append(node("span", "chip chip--ok", `状态 ${response.status}`));
    box.append(node("span", "chip", `API ${summary.api_version} · 契约 ${summary.contract_version}`));
    box.append(node("span", "chip", `会话 ${summary.sessions && summary.sessions.active}`));
    box.append(node("span", "chip", `文档 ${summary.documents && summary.documents.total}（已发布 ${summary.documents && summary.documents.published}）`));
    box.append(node("span", "chip", `模型 ${summary.model_boundary && summary.model_boundary.model}`));
    box.append(node("span", "chip", `fallback ${String(summary.model_boundary && summary.model_boundary.fallback)}`));
    return response;
  } catch (error) {
    await handleError(ctx, error, "健康检查失败。");
    return null;
  }
}

export async function refreshDocuments(ctx) {
  const list = byId(IDS.docs);
  if (!list) return null;
  clear(list);
  const documents = ctx.state.documents || [];
  if (!documents.length) {
    list.append(node("p", "empty-state", "队列为空：上传一份 PDF 开始。"));
    return null;
  }
  for (const doc of documents) {
    const row = node("article", "doc-row");
    row.append(node("strong", "", doc.document_id));
    row.append(node("span", "chip", `${STATE_TEXT[doc.state] || doc.state}（${doc.state}）`));
    row.append(node("small", "", `${doc.bytes} 字节 · ${doc.page_count === null ? "页数待解析" : doc.page_count + " 页"}`));
    row.append(node("small", "", `sha256 ${String(doc.sha256).slice(0, 12)}…`));
    row.append(node("small", "", doc.index_version ? `索引 ${doc.index_version}` : "未进入活动索引"));
    if (doc.failure_code) row.append(node("small", "chip chip--warn", `失败码 ${doc.failure_code}`));

    const publish = node("button", "ghost-button", "发布");
    publish.disabled = !(doc.state === "validated" || doc.state === "disabled");
    publish.addEventListener("click", () => publishDocument(ctx, doc.document_id));
    const disable = node("button", "ghost-button", "停用");
    disable.disabled = doc.state !== "published";
    disable.addEventListener("click", () => disableDocument(ctx, doc.document_id));
    const remove = node("button", "ghost-button", "删除");
    remove.disabled = doc.state === "deleted";
    remove.addEventListener("click", () => deleteDocument(ctx, doc.document_id));
    row.append(publish);
    row.append(disable);
    row.append(remove);
    list.append(row);
  }
  return documents;
}

export async function loadDocuments(ctx) {
  // admin_summary_response 只给摘要；文档队列在真实后端由列表端点提供，
  // 离线 mock 场景下用已发布/失败/停用三类种子文档演示状态机。
  const ids = ctx.state.documentIds || [];
  const documents = [];
  for (const id of ids) {
    try {
      const response = await ctx.client.adminReadDocument(id);
      documents.push(response);
    } catch (error) {
      await handleError(ctx, error, "读取文档状态失败。");
      return null;
    }
  }
  ctx.state.documents = documents;
  return refreshDocuments(ctx);
}

export async function refreshKey(ctx) {
  const box = byId(IDS.keyStatus);
  if (!box) return null;
  clear(box);
  try {
    const response = await ctx.client.adminReadKeyStatus();
    ctx.state.modelSettings = response;
    for (const [key,value] of Object.entries(response.config || {})) {
      const field = byId(`model-${key}`); if (field) field.value = value === null ? "" : String(value);
    }
    box.append(node("span", response.configured ? "chip chip--ok" : "chip chip--warn", response.configured ? "已配置" : "未配置"));
    box.append(node("span", "chip", `Provider ${response.config?.provider_id} / ${response.config?.model}`));
    box.append(node("span", "chip", `价格 ${response.pricing_status} · revision ${response.revision}`));
    return response;
  } catch (error) {
    await handleError(ctx, error, "读取 Key 状态失败。");
    return null;
  }
}

export async function refreshAudit(ctx) {
  const box = byId(IDS.audit);
  if (!box) return null;
  clear(box);
  try {
    const response = await ctx.client.adminReadAudit();
    const summary = response.summary || {};
    box.append(node("span", "chip", `事件数 ${summary.count}`));
    box.append(node("span", "chip", `保留 ${summary.retention_days} 天`));
    const events = summary.events || [];
    const list = node("div", "audit-list");
    for (const event of events.slice(-8)) {
      const row = node("div", "audit-row");
      row.append(node("small", "", `${String(event.occurred_at).replace("T", " ").slice(0, 19)} · ${event.event_type} · ${event.outcome}`));
      if (event.error_code) row.append(node("small", "chip chip--warn", event.error_code));
      list.append(row);
    }
    box.append(list);
    return response;
  } catch (error) {
    await handleError(ctx, error, "读取审计事件失败。");
    return null;
  }
}

export async function refreshUsage(ctx) {
  const box = byId(IDS.usage);
  if (!box) return null;
  clear(box);
  try {
    const response = await ctx.client.adminReadUsage();
    const summary = response.summary || {};
    for (const [label, key] of [
      ["会话创建数", "sessions_created"],
      ["文档总数", "documents_total"],
      ["模型调用", "model_calls"],
      ["输入 token", "input_tokens"],
      ["输出 token", "output_tokens"],
      ["费用（元）", "estimated_cost_cny"],
    ]) {
      box.append(node("span", "chip", `${label} · ${summary[key] === undefined ? "—" : summary[key]}`));
    }
    return response;
  } catch (error) {
    await handleError(ctx, error, "读取用量摘要失败。");
    return null;
  }
}

export async function refreshConsole(ctx) {
  setNotice("");
  const health = await refreshHealth(ctx);
  if (!health) return false;
  await loadDocuments(ctx);
  await refreshKey(ctx);
  await refreshAudit(ctx);
  await refreshUsage(ctx);
  return true;
}

/* ---- 写操作 ---- */

export async function uploadDocument(ctx) {
  const meta = ctx.state.upload;
  if (!meta) {
    setNotice("请先选择要上传的 PDF。", "bad");
    return null;
  }
  try {
    const response = await ctx.client.adminUploadDocument({ ...meta, csrf: ctx.state.admin.csrf });
    setNotice(`已上传：${meta.filename}（${response.state}），等待校验后发布`, "ok");
    ctx.state.upload = null;
    await loadDocuments(ctx);
    await refreshAudit(ctx);
    return response;
  } catch (error) {
    await handleError(ctx, error, "上传失败。");
    return null;
  }
}

export async function publishDocument(ctx, documentId) {
  try {
    const response = await ctx.client.adminPublishDocument(documentId, { csrf: ctx.state.admin.csrf });
    setNotice(`已发布：${documentId}（索引 ${response.index_version}）`, "ok");
    await loadDocuments(ctx);
    await refreshAudit(ctx);
    return response;
  } catch (error) {
    await handleError(ctx, error, "发布失败。");
    return null;
  }
}

export async function disableDocument(ctx, documentId) {
  try {
    const response = await ctx.client.adminDisableDocument(documentId, { csrf: ctx.state.admin.csrf });
    setNotice(`已停用：${documentId}（已退出活动索引）`, "ok");
    await loadDocuments(ctx);
    await refreshAudit(ctx);
    return response;
  } catch (error) {
    await handleError(ctx, error, "停用失败。");
    return null;
  }
}

export async function deleteDocument(ctx, documentId) {
  try {
    const response = await ctx.client.adminDeleteDocument(documentId, { csrf: ctx.state.admin.csrf });
    setNotice(`已删除：${documentId}（版本 ${response.resource_revision}）`, "ok");
    await loadDocuments(ctx);
    await refreshAudit(ctx);
    return response;
  } catch (error) {
    await handleError(ctx, error, "删除失败。");
    return null;
  }
}

export async function saveKey(ctx, { replace }) {
  const input = byId(IDS.keyInput);
  const apiKey = input ? String(input.value || "") : "";
  const config = {version:"1.0.0", protocol:"openai-chat-completions"};
  for (const key of ["provider_id","model","base_url","local","auth_mode","input_per_million_tokens","output_per_million_tokens"]) {
    const value = byId(`model-${key}`)?.value || "";
    config[key] = key === "local" ? value === "true" : key.endsWith("_tokens") ? (value === "" ? null : Number(value)) : value;
  }
  try {
    const method = replace ? ctx.client.adminReplaceKey : ctx.client.adminSaveKey;
    const response = await method.call(ctx.client, { apiKey, config, expectedRevision: ctx.state.modelSettings?.revision, csrf: ctx.state.admin.csrf });
    // 提交后立即清空输入框：明文不留痕
    if (input) input.value = "";
    setNotice("模型配置已保存；未调用模型。", "ok");
    await refreshKey(ctx);
    await refreshAudit(ctx);
    return response;
  } catch (error) {
    await handleError(ctx, error, "保存 Key 失败。");
    return null;
  } finally {
    if (input) input.value = "";
  }
}

export async function deleteKey(ctx) {
  try {
    await ctx.client.adminDeleteKey({ csrf: ctx.state.admin.csrf, expectedRevision: ctx.state.modelSettings?.revision });
    setNotice("Key 已删除，模型调用将失败关闭。", "ok");
    await refreshKey(ctx);
    await refreshAudit(ctx);
    return true;
  } catch (error) {
    await handleError(ctx, error, "删除 Key 失败。");
    return null;
  }
}

export async function testConnectivity(ctx) {
  try {
    const response = await ctx.client.adminTestKeyConnectivity({ csrf: ctx.state.admin.csrf });
    const summary = response.summary || {};
    setNotice(response.fixture ? "离线 fixture：未调用模型，不能证明可达。" : `连通测试完成；调用 ${response.calls} 次。`, "ok");
    await refreshAudit(ctx);
    return response;
  } catch (error) {
    await handleError(ctx, error, "连通测试失败。");
    return null;
  }
}

export async function adminLogout(ctx) {
  try {
    await ctx.client.adminLogout({ csrf: ctx.state.admin.csrf });
    ctx.state.admin = { authenticated: false, csrf: null, username: null };
    if (typeof ctx.navigate === "function") await ctx.navigate("#/admin/settings", { reason: "已注销。" });
    return true;
  } catch (error) {
    await handleError(ctx, error, "注销失败。");
    return null;
  }
}

export async function adminRevokeAll(ctx) {
  try {
    await ctx.client.adminRevokeAllSessions({ csrf: ctx.state.admin.csrf });
    ctx.state.admin = { authenticated: false, csrf: null, username: null };
    if (typeof ctx.navigate === "function") await ctx.navigate("#/admin/settings", { reason: "已吊销全部会话，请重新登录。" });
    return true;
  } catch (error) {
    await handleError(ctx, error, "吊销会话失败。");
    return null;
  }
}
