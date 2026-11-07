"use strict";

/*
 * D4-07 C 侧前端 adapter ↔ B 侧在线 SQL 后端联调探针（进程内桥接，离线）。
 *
 * 本探针只负责「用前端 adapter 的真实代码」生成请求，不做网络/模型调用：
 *   - new-query <payload>：newSession() + buildSubmitQuery()，输出 { session, req }
 *   - clarify <payload>  ：buildSubmitClarification(pending, value, evidence_ref)
 *
 * payload 为单行 JSON 字符串（argv[3]）。B 后端处理由 Python 测试侧负责。
 */

import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const __dirname = dirname(fileURLToPath(import.meta.url));
const adapterUrl =
  "file:///" +
  resolve(__dirname, "../../../src/frontend/d4-console/adapter.js").replace(
    /\\/g,
    "/"
  );

const adapter = await import(adapterUrl);

const action = process.argv[2];
const payload = JSON.parse(process.argv[3] || "{}");

if (action === "new-query") {
  const session = adapter.newSession();
  const req = adapter.buildSubmitQuery(session, payload.query || "统计艺术家数量");
  console.log(JSON.stringify({ session, req }));
} else if (action === "clarify") {
  const req = adapter.buildSubmitClarification(
    payload.pending,
    payload.value,
    payload.evidence_ref
  );
  // 测试侧可覆盖 answer_id，模拟前端单页面跨轮递增（本探针每次调用是独立进程，
  // adapter 的模块级 answerCounter 会重置，故由测试侧传入全局递增序号）。
  if (payload.answer_id) req.answer_id = payload.answer_id;
  console.log(JSON.stringify(req));
} else {
  console.log(JSON.stringify({ error: "unknown_action", action }));
}
