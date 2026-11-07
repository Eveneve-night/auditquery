"use strict";

import { isValidCitation, matchesSourceText } from "../d4-console/rag-manifest.js";

export function citationVerification(citation, warnings = []) {
  if (String(citation.document_id || "").startsWith("doc.")) {
    const bound = Number.isInteger(citation.page) && citation.page > 0 &&
      citation.evidence_id === `${citation.document_id}.p${citation.page}` &&
      typeof citation.snippet === "string" && citation.snippet.trim().length > 0;
    // The browser has no copy of an arbitrary uploaded PDF. Do not claim a
    // local comparison. exact_source_grounding means the service proved the
    // quote is an exact substring of its normalized page text and bound the
    // private char interval/hash audit record into the response trace.
    const exact = Array.isArray(warnings) && warnings.includes("exact_source_grounding");
    return {
      valid: bound,
      label: bound ? (exact ? "服务端原文精确对齐" : "服务端页级引用") : "引用待核对",
    };
  }
  const verified = isValidCitation(citation.evidence_id, citation.page) &&
    matchesSourceText(citation.evidence_id, citation.snippet) === true;
  return { valid: verified, label: verified ? "原文已核对" : "引用待核对" };
}
