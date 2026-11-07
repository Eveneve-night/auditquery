import assert from "node:assert/strict";
import { citationVerification } from "../../../src/frontend/d5-console/citation-validation.js";
import { RAG_MANIFEST } from "../../../src/frontend/d4-console/rag-manifest.js";

const dynamic = { document_id: "doc.local", evidence_id: "doc.local.p1", page: 1, snippet: "真实片段" };
assert.deepEqual(citationVerification(dynamic), { valid: true, label: "服务端页级引用" });
assert.deepEqual(citationVerification(dynamic, ["exact_source_grounding"]),
  { valid: true, label: "服务端原文精确对齐" });
assert.equal(citationVerification({ ...dynamic, page: 2 }).valid, false);
assert.equal(citationVerification({ ...dynamic, snippet: "" }).valid, false);
const [evidence_id, entry] = Object.entries(RAG_MANIFEST).find(([, item]) => item.source_text);
assert.deepEqual(citationVerification({ evidence_id, document_id: entry.document_id,
  page: entry.page, snippet: entry.source_text }), { valid: true, label: "原文已核对" });
assert.equal(citationVerification({ evidence_id, page: entry.page, snippet: "伪造片段" }).valid, false);
console.log("citation assertions: 6/6");
