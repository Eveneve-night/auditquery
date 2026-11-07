"""从 D4-06 的 10 份 PDF 原文导出前端 RAG 引用清单（rag-manifest.js）。

产出三份事实（全部来自 PDF 原文，无自拟）：
  1. evidence_id -> 页码（来自 D4-06 报告 evidence_pages）
  2. document_id -> 标题（来自各 PDF 首页首行）
  3. evidence_id -> 源文正文（来自 PDF 该页中 [EVID] 标记之后、下一个标记之前的文本）

第 3 项使前端可以做「引用片段与源文逐字一致」的机器校验，而不再只是
「证据 ID 存在 + 页码一致」。

用法：python tools/rag/build_frontend_manifest.py
离线：只读本地 PDF 与报告，不发起网络或模型调用。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from pypdf import PdfReader  # noqa: E402

REPORT = PROJECT_ROOT / "tests" / "reports" / "d4-pdf-rag-v1.json"
PDF_DIR = PROJECT_ROOT / "data" / "raw" / "pdf"
OUTPUT = PROJECT_ROOT / "src" / "frontend" / "d4-console" / "rag-manifest.js"

# PDF 正文形如 "[SALES-001] 月度经营报表的截止时间是……"，证据 ID 用方括号标记。
EVID_RE = re.compile(r"\[([A-Z]+-\d+)\]\s*(.*?)(?=\[[A-Z]+-\d+\]|\Z)", re.DOTALL)

HEADER = '''"use strict";

/*
 * D4-07 C 侧 RAG 引用合法性清单（离线，由 tools/rag/build_frontend_manifest.py
 * 从 D4-06 的 10 份 PDF 原文自动导出，请勿手改）。
 *
 * 数据来源：
 *  - page / document_id：tests/reports/d4-pdf-rag-v1.json 的 evidence_pages
 *    （D4-06 双解析器交叉验证后的页码，44 条证据）。
 *  - title：各 PDF 首页首行标题（非自拟）。
 *  - source_text：该 PDF 页中 [EVID] 标记之后、下一个标记之前的正文原文。
 *
 * 两级校验，语义严格区分：
 *  - isValidCitation()：只校验「证据 ID 存在 + 页码一致」，不比对文本。
 *  - matchesSourceText()：校验「引用片段与 PDF 源文逐字一致」（两侧都移除
 *    空白后做包含判断，因此 PDF 换行不影响，但任何一字之差都会不匹配）。
 *
 * 注意：D4-06 的双解析器交叉验证（cross_validate）只比对「证据 ID 集合」
 * 是否一致，并不比对片段文本逐字；片段逐字一致性由本模块的
 * matchesSourceText() 单独保证。
 *
 * 本模块不引用 expected/gold/label，不发起网络或模型调用。
 */

export const RAG_MANIFEST = '''

TAIL = ''';

/** 校验一条 citation 是否合法：evidence_id 存在且页码一致（不比对文本）。 */
export function isValidCitation(evidenceId, page) {
  const entry = RAG_MANIFEST[evidenceId];
  return entry != null && entry.page === page;
}

/** 取 evidence_id 对应的文档信息（不存在返回 null）。 */
export function lookupEvidence(evidenceId) {
  return RAG_MANIFEST[evidenceId] || null;
}

/** 归一化：移除所有空白，并去掉开头的 [EVID] / 【EVID】 标记。 */
function normalizeForCompare(text) {
  return String(text == null ? "" : text)
    .replace(/^[\\u3010\\u005b][^\\u3011\\u005d]*[\\u3011\\u005d]\\s*/, "")
    .replace(/\\s+/g, "");
}

/**
 * 校验引用片段与 PDF 源文是否逐字一致。
 *
 * 返回 true / false / null：
 *  - true：片段（去掉空白与开头 ID 标记后）是源文正文的子串，即逐字一致；
 *  - false：证据 ID 存在，但片段文本与源文不一致（含多字、少字、改字）；
 *  - null：清单里没有该证据的源文，无法校验（不可判定为一致）。
 */
export function matchesSourceText(evidenceId, snippet) {
  const entry = RAG_MANIFEST[evidenceId];
  if (entry == null || entry.source_text == null) return null;
  const source = normalizeForCompare(entry.source_text);
  const quoted = normalizeForCompare(snippet);
  if (!quoted) return null;
  return source.includes(quoted);
}
'''


def _page_texts(pdf_path: Path) -> list[str]:
    reader = PdfReader(str(pdf_path))
    return [(page.extract_text() or "") for page in reader.pages]


def build() -> dict:
    report = json.loads(REPORT.read_text(encoding="utf-8"))
    manifest: dict[str, dict] = {}
    missing: list[str] = []

    for doc in report["documents"]:
        doc_id = doc["document_id"]
        pdf_path = PDF_DIR / f"{doc_id}.pdf"
        pages = _page_texts(pdf_path)
        title = (pages[0] or "").strip().splitlines()[0].strip()

        for evid, page in doc["evidence_pages"].items():
            text = pages[page - 1] if 1 <= page <= len(pages) else ""
            match = None
            for m in EVID_RE.finditer(text):
                if m.group(1) == evid:
                    match = m
                    break
            body = match.group(2).strip() if match else None
            if not body:
                missing.append(f"{evid}@{doc_id}#p{page}")
                continue
            manifest[evid] = {
                "document_id": doc_id,
                "page": page,
                "title": title,
                "source_text": body,
            }

    if missing:
        raise SystemExit(f"以下证据在 PDF 原文中找不到正文，失败关闭：{missing}")
    return manifest


def main() -> None:
    manifest = build()
    body = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True)
    # 显式 LF：Windows 上 Path.write_text 默认会把 '\n' 转成 CRLF，
    # 导致重建产物与提交文件字节漂移（13060 -> 13385 bytes）。固定 LF 保证跨平台一致。
    with OUTPUT.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(HEADER + body + TAIL)
    print(f"已生成 {OUTPUT.relative_to(PROJECT_ROOT)}，共 {len(manifest)} 条证据（含源文正文）")


if __name__ == "__main__":
    main()
