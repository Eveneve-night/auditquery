"use strict";
import { ApiError, newRequestId } from "./api-client.js";
import { node, clear } from "./dom.js";

export const STEPS = Object.freeze([
  ["create", "新增草稿"], ["credential", "凭据引用"], ["test", "测试连接"],
  ["sync", "同步 Schema"], ["authorize", "授权目录"], ["bind", "绑定领域包"],
  ["publish", "发布"], ["disable", "停用"],
]);
const LABELS = Object.freeze({ forbidden: "未授权，请使用管理员身份。", revision_conflict: "状态已变化，请刷新后重试。",
  authentication_required: "请先登录。", csrf_invalid: "会话校验失败，请重新登录。", state_conflict: "当前状态不允许此操作，请刷新。",
  invalid_request: "请求无效，未执行操作。", service_unavailable: "连接器尚无此能力，未执行操作。",
  not_implemented: "尚未接入真实管理服务，此操作不可用。", source_busy: "仍有在途操作，请稍后重试。",
  invalid_contract: "服务返回无效状态，未更新本地记录。", internal_error: "操作失败，未确认成功。" });

// A is the sole contract owner. The bridge must inject its real validator;
// this UI component neither duplicates a schema nor creates an HTTP protocol.
export function createSourceWorkflow({ adapter, validateSource, getRole }) {
  let source = null, busy = false, error = null;
  function snapshot() { return { source: source && structuredClone(source), busy, error, mode: "offline", httpConnected: false }; }
  async function perform(action, input = {}) {
    error = null;
    if (getRole() !== "admin") { error = "forbidden"; return snapshot(); }
    if (busy) { error = "source_busy"; return snapshot(); }
    if (!STEPS.some(([key]) => key === action)) { error = "not_implemented"; return snapshot(); }
    if ((action === "create" && source) || (action === "publish" && source?.status !== "synced")) { error = "invalid_contract"; return snapshot(); }
    // Never accept passwords/DSNs in this local component. Credentials must
    // be entered through the later authenticated backend capability.
    const allowed = new Set(["source_id", "sqlite_asset_id", "credential_ref", "binding"]);
    if (!input || typeof input !== "object" || Array.isArray(input) || Object.keys(input).some(key => !allowed.has(key))) { error = "invalid_contract"; return snapshot(); }
    busy = true;
    try {
      if (!adapter || typeof adapter.invoke !== "function" || typeof validateSource !== "function") throw new ApiError("not_implemented");
      const previous = source && structuredClone(source);
      if (["source_id", "sqlite_asset_id"].some(key => Object.hasOwn(input, key) && typeof input[key] !== "string")) throw new ApiError("invalid_contract");
      // Validate nested references with the injected production contract before
      // handing any input to the adapter. Creation accepts identifiers only.
      if (Object.hasOwn(input, "credential_ref") || Object.hasOwn(input, "binding")) {
        if (!previous || await validateSource({ ...structuredClone(previous), ...structuredClone(input) }, { action, previous }) === false) throw new ApiError("invalid_contract");
      }
      const result = await adapter.invoke(action, { previous, input: structuredClone(input), operationId: newRequestId("source") });
      if (await validateSource(result, { action, previous }) === false) throw new ApiError("invalid_contract");
      if (!previous && (result.status !== "draft" || result.revision !== 1)) throw new ApiError("invalid_contract");
      if (previous && (result.source_id !== previous.source_id || result.revision !== previous.revision + 1)) throw new ApiError("revision_conflict");
      source = structuredClone(result);
    } catch (failure) {
      error = failure instanceof ApiError && LABELS[failure.code] ? failure.code : "internal_error";
    } finally { busy = false; }
    return snapshot();
  }
  return { snapshot, perform };
}

export function renderSourceWorkflow(root, workflow) {
  clear(root); const state = workflow.snapshot();
  root.appendChild(node("p", "mode-notice", "离线流程组件 · 未接管理HTTP · 不证明数据源已连接或可插拔"));
  root.appendChild(node("p", "status", state.source ? `来源 ${state.source.source_id} · revision ${state.source.revision} · ${state.source.status}` : "暂无数据源"));
  if (state.error) root.appendChild(node("p", "error", LABELS[state.error] || LABELS.internal_error));
  // Only safe source metadata is shown; credential_ref/DSN/password omitted.
  if (state.source?.binding) {
    root.appendChild(node("p", "source-evidence", `Schema ${state.source.binding.schema_sha256} · 领域包 ${state.source.binding.domain_pack.id} · ${state.source.binding.domain_pack.version}`));
  }
  for (const [action, label] of STEPS) {
    const button = node("button", "source-action", label); button.disabled = state.busy;
    button.addEventListener("click", async () => { await workflow.perform(action); renderSourceWorkflow(root, workflow); });
    root.appendChild(button);
  }
}
