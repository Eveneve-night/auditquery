"use strict";

/*
 * D5-05-C C 侧前端：真实 HTTP 传输层（src/frontend/d5-console/live-transport.js）。
 *
 * 把 D5-04 的「离线契约 mock」切到真实 `/api/v3` 服务。实现与 mock 相同的
 * transport 契约：`transport(method, path, opts) -> {status, json}`（二进制下载
 * 时返回 `{status, blob, contentType}`），由 `api-client.js` 的 `send` 统一归一化错误。
 *
 * 安全边界（与任务卡 D5-05-C、契约 security 段一致）：
 * - 会话 Cookie 由浏览器同源自动携带（credentials=same-origin），客户端不手动
 *   读写 document.cookie，不写 localStorage / sessionStorage。
 * - 写操作只把 CSRF 令牌放进 `x-csrf-token` 请求头，令牌只存在于调用方内存对象。
 * - API Key 只在单次请求体中向服务端提交，客户端不缓存、不回显、不写日志。
 * - multipart 上传字段约定已与 B 的 D5-05-B `parse_document_multipart` 对齐：
 *   `metadata`（document_upload_request 的 JSON，Content-Type=application/json）+
 *   `file`（PDF 字节，Content-Type=application/pdf）。metadata 用 Blob 显式声明
 *   application/json（FormData 字符串默认是 text/plain，会被 B 侧 400 拒绝）。
 */

export function createLiveTransport(options = {}) {
  const baseUrl = options.baseUrl || "";
  const fetchImpl = options.fetchImpl || (typeof fetch === "function" ? fetch : null);
  const FormDataImpl = options.FormDataImpl || (typeof FormData !== "undefined" ? FormData : null);
  const BlobImpl = options.BlobImpl || (typeof Blob !== "undefined" ? Blob : null);

  if (typeof fetchImpl !== "function") {
    throw new Error("createLiveTransport 需要 fetch 实现（浏览器 fetch 或注入的 fetchImpl）");
  }

  return async function transport(method, path, opts = {}) {
    const body = opts.body;
    const csrf = opts.csrf;
    const file = opts.file;
    const raw = Boolean(opts.raw);
    const url = baseUrl + path;
    const headers = {};
    let payload;

    if (file !== undefined && file !== null) {
      if (typeof FormDataImpl !== "function") {
        throw new Error("multipart 上传需要 FormData 实现");
      }
      const form = new FormDataImpl();
      // metadata 必须显式声明 application/json（对齐 B 的 parse_document_multipart）。
      if (typeof BlobImpl === "function") {
        form.append("metadata", new BlobImpl([JSON.stringify(body)], { type: "application/json" }));
      } else {
        form.append("metadata", JSON.stringify(body));
      }
      // file 部分确保 type=application/pdf；浏览器 File 的 type 可能为空串。
      const pdfFile = (file && file.type === "application/pdf")
        ? file
        : (typeof BlobImpl === "function" ? new BlobImpl([file], { type: "application/pdf" }) : file);
      form.append("file", pdfFile, (file && file.name) || "document.pdf");
      payload = form;
      // 不手动设 Content-Type：让 fetch 自动补 multipart boundary。
    } else if (body !== undefined && body !== null) {
      headers["content-type"] = "application/json";
      payload = JSON.stringify(body);
    }

    if (csrf) headers["x-csrf-token"] = csrf;
    // B 服务端以 X-Request-ID 作为本次请求 ID，并要求它与写操作元数据一致。
    if (body && typeof body.request_id === "string" && body.request_id) {
      headers["x-request-id"] = body.request_id;
    }

    const response = await fetchImpl(url, {
      method,
      headers,
      body: payload,
      credentials: "same-origin",
    });

    if (raw) {
      const blob = typeof response.blob === "function" ? await response.blob() : null;
      const contentType = response.headers && typeof response.headers.get === "function"
        ? response.headers.get("content-type")
        : null;
      return { status: response.status, blob, contentType };
    }

    let json = {};
    try {
      json = await response.json();
    } catch (error) {
      // 空 body / 非 JSON：由 api-client 的 send 按 status 归一化。
      json = {};
    }
    return { status: response.status, json };
  };
}

export default { createLiveTransport };
