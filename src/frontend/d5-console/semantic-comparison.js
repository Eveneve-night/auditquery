"use strict";

import { node, clear } from "./dom.js";

// semantic-result.v1, A contract commit 06d1edf. This optional component does
// not add fields to online-response.v2 or infer a backend transport envelope.
export function renderSemanticComparison(container, result) {
  clear(container);
  container.hidden = true;
  if (!result || result.result_version !== "1.0.0" ||
      typeof result.original_question !== "string" ||
      typeof result.rewritten_question !== "string" ||
      !Array.isArray(result.edits) || !Array.isArray(result.fragments) ||
      result.original_question.length > 16384 || result.rewritten_question.length > 16384 ||
      result.edits.length > 128 || result.fragments.length > 128) return false;
  const original = Array.from(result.original_question);
  const coords = result.text_coordinates;
  if (!coords || coords.unit !== "unicode_code_point" ||
      coords.range_semantics !== "half_open" || coords.normalization !== "original_question") return false;
  const span = (item, text) => Number.isInteger(item.start) && Number.isInteger(item.end) &&
    item.start >= 0 && item.end > item.start && item.end <= original.length &&
    original.slice(item.start, item.end).join("") === text;
  if (result.edits.some(e => !e || !span(e, e.before) || typeof e.after !== "string" ||
      !Array.isArray(e.evidence_refs)) ||
      result.fragments.some(f => !f || !span(f, f.text))) return false;
  const sorted = [...result.edits].sort((a, b) => a.start - b.start);
  if (sorted.some((e, i) => i && e.start < sorted[i - 1].end)) return false;
  let rebuilt = [...original];
  for (const edit of sorted.reverse()) rebuilt.splice(edit.start, edit.end - edit.start, ...Array.from(edit.after));
  if (rebuilt.join("") !== result.rewritten_question) return false;
  container.append(node("h3", "", "问题规范化对照"));
  container.append(node("p", "", `原问题：${result.original_question}`));
  container.append(node("p", "", `标准化问题：${result.rewritten_question}`));
  container.append(node("small", "", "展示服务端语义结果；保留未知信息，不代表已执行查询。"));
  const edits = node("ul", "semantic-edits");
  for (const edit of result.edits) {
    edits.append(node("li", "", `${edit.before} → ${edit.after}；原文位置 [${edit.start}, ${edit.end})；证据：${edit.evidence_refs.join("、")}`));
  }
  if (!result.edits.length) edits.append(node("li", "", "无修改项"));
  container.append(edits);
  const unresolved = result.fragments.filter(f => f.status === "unknown" || f.status === "ambiguous");
  const fragments = node("ul", "semantic-unknown");
  for (const item of unresolved) {
    fragments.append(node("li", "", `${item.status === "unknown" ? "未知" : "有歧义"}：${item.text}；原文位置 [${item.start}, ${item.end})`));
  }
  if (unresolved.length) container.append(fragments);
  container.hidden = false;
  return true;
}
