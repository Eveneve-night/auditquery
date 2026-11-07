// D5-05-C 前端 live transport 离线探针。
// 用 mock fetch / mock FormData 验证 createLiveTransport 的真实 HTTP 行为，
// 不发起真实网络请求。输出单行 JSON 供 tests/unit 断言。

import { createLiveTransport } from "../../../src/frontend/d5-console/live-transport.js";

class MockFormData {
  constructor() {
    this.parts = [];
  }
  append(name, value, filename) {
    this.parts.push({
      name,
      value,
      filename: filename || null,
      type: value && typeof value.type === "string" ? value.type : null,
    });
  }
}

function mockResponse(status, { json, blob = null, contentType = null } = {}) {
  return {
    status,
    async json() {
      if (json === undefined) throw new Error("no json");
      return json;
    },
    async blob() {
      return blob;
    },
    headers: {
      get(name) {
        return name.toLowerCase() === "content-type" ? contentType : null;
      },
    },
  };
}

async function run() {
  const out = {};

  // 1. JSON 请求 + CSRF 头 + 同源凭证
  {
    const calls = [];
    const fetchImpl = async (url, init) => {
      calls.push({ url, init });
      return mockResponse(200, { json: { ok: true } });
    };
    const transport = createLiveTransport({ baseUrl: "", fetchImpl, FormDataImpl: MockFormData });
    const result = await transport("POST", "/api/v3/query", {
      body: { a: 1, request_id: "req.test.query" },
      csrf: "tok-123",
    });
    out.jsonRequest = {
      ok: result.status === 200 && result.json.ok === true,
      url: calls[0].url,
      method: calls[0].init.method,
      contentType: calls[0].init.headers["content-type"],
      body: calls[0].init.body,
      csrfHeader: calls[0].init.headers["x-csrf-token"],
      requestIdHeader: calls[0].init.headers["x-request-id"] || null,
      credentials: calls[0].init.credentials,
    };
  }

  // 2. multipart 上传：metadata JSON + file 字节，不手动设 content-type
  {
    const calls = [];
    const fetchImpl = async (url, init) => {
      calls.push({ url, init });
      return mockResponse(200, { json: { document_id: "doc.1" } });
    };
    const transport = createLiveTransport({ baseUrl: "", fetchImpl, FormDataImpl: MockFormData });
    const file = { name: "a.pdf", size: 1234 };
    const result = await transport("POST", "/api/v3/admin/documents", {
      body: { api_version: "3.0", request_id: "req.test.upload", filename: "a.pdf", declared_bytes: 1234, content_sha256: "a".repeat(64) },
      csrf: "tok-admin",
      file,
    });
    const form = calls[0].init.body;
    const metaPart = form.parts.find((p) => p.name === "metadata");
    const filePart = form.parts.find((p) => p.name === "file");
    const metaText = metaPart && metaPart.value && typeof metaPart.value.text === "function"
      ? await metaPart.value.text()
      : String(metaPart ? metaPart.value : "");
    let metaJson = null;
    try {
      metaJson = JSON.parse(metaText);
    } catch {
      metaJson = null;
    }
    out.multipart = {
      ok: result.status === 200,
      isFormData: form instanceof MockFormData,
      parts: form.parts.map((p) => ({ name: p.name, filename: p.filename, type: p.type })),
      csrfHeader: calls[0].init.headers["x-csrf-token"],
      requestIdHeader: calls[0].init.headers["x-request-id"] || null,
      contentTypeNotManuallySet: calls[0].init.headers["content-type"] === undefined,
      metadataType: metaPart ? metaPart.type : null,
      fileType: filePart ? filePart.type : null,
      metadataIsJson: Boolean(metaJson) && metaJson.filename === "a.pdf" && metaJson.content_sha256 === "a".repeat(64),
    };
  }

  // 3. Key 写入同样要求请求头与 JSON body 的 request_id 一致。
  {
    const calls = [];
    const fetchImpl = async (url, init) => {
      calls.push({ url, init });
      return mockResponse(200, { json: { status: "ok" } });
    };
    const transport = createLiveTransport({ fetchImpl, FormDataImpl: MockFormData });
    await transport("POST", "/api/v3/admin/secrets/bailian", {
      body: { api_version: "3.0", contract_version: "1.0.0", request_id: "req.test.key" },
      csrf: "tok-admin",
    });
    out.keySave = {
      requestIdHeader: calls[0].init.headers["x-request-id"] || null,
      bodyRequestId: JSON.parse(calls[0].init.body).request_id,
    };
  }

  // 4. 二进制下载：raw 返回 blob + contentType
  {
    const fetchImpl = async () =>
      mockResponse(200, { blob: "PDFBYTES", contentType: "application/pdf" });
    const transport = createLiveTransport({ baseUrl: "", fetchImpl, FormDataImpl: MockFormData });
    const result = await transport("GET", "/api/v3/admin/documents/doc.1/download", { raw: true });
    out.download = {
      ok: result.status === 200 && result.blob === "PDFBYTES" && result.contentType === "application/pdf",
    };
  }

  // 4. 错误归一化：非 2xx 仍返回 {status, json} 交给 send 抛 ApiError
  {
    const fetchImpl = async () =>
      mockResponse(403, { json: { error: { code: "csrf_invalid", message: "CSRF 校验失败。", retryable: false } } });
    const transport = createLiveTransport({ baseUrl: "", fetchImpl, FormDataImpl: MockFormData });
    const result = await transport("POST", "/api/v3/admin/logout", { body: {}, csrf: "bad" });
    out.errorNormalize = {
      ok: result.status === 403 && result.json.error.code === "csrf_invalid",
      code: result.json.error.code,
    };
  }

  // 5. 非 JSON 响应体（空 body）不抛错，归一化为 {}
  {
    const fetchImpl = async () => mockResponse(204, {});
    const transport = createLiveTransport({ baseUrl: "", fetchImpl, FormDataImpl: MockFormData });
    const result = await transport("DELETE", "/api/v3/admin/secrets/bailian", { csrf: "tok" });
    out.emptyBody = { ok: result.status === 204, jsonIsObject: typeof result.json === "object" && result.json !== null };
  }

  // 6. 传入非函数的 fetch 实现时明确报错（不静默降级到 mock）
  {
    let threw = false;
    try {
      createLiveTransport({ fetchImpl: "not-a-function", FormDataImpl: MockFormData });
    } catch {
      threw = true;
    }
    out.noFetchThrows = { ok: threw };
  }

  console.log(JSON.stringify(out));
}

run().catch((error) => {
  console.log(JSON.stringify({ probeError: String(error && error.stack ? error.stack : error) }));
  process.exit(1);
});
