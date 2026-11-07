"""把知识夹具 Markdown（既有 4 份 + 新增 6 份新文档族）渲染成 PDF（reportlab，BSD-3）。

D4-06 决策 4 实现：确定性渲染、物理保留 ``[XXX-NNN]`` 证据行、可编程分页，
产物登记 SHA-256（与 Chinook 同款 manifest 纪律）。

渲染规则（确定性，重建逐字节一致）：
- 页面 A4，边距 2cm，标题 16pt 加粗，正文 12pt；
- 每条证据渲染为一个独立段落，段落间空一行；
- 分页策略：每页最多 ``PAGE_SIZE`` 条证据，超过则强制分页（制造非平凡多页与页边界）；
- 字体用 reportlab 内置 ``STSong-Light``（CID），零外部字体依赖；
- 固定创建时间与 Producer，保证 PDF 字节级可复现。

仅离线：网络调用 0，模型 API 调用 0，不读取任何密钥。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

# 可复现构建：固定时间戳（2000-01-01T00:00:00Z），消除 PDF /CreationDate /ModDate 差异。
# reportlab 的 TimeStamp 在检测到 SOURCE_DATE_EPOCH 时采用固定时间。
os.environ.setdefault("SOURCE_DATE_EPOCH", "946684800")

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer

PROJECT_ROOT = Path(__file__).resolve().parents[2]
KNOWLEDGE_DIR = PROJECT_ROOT / "tests" / "fixtures" / "knowledge"
PDF_SOURCE_DIR = KNOWLEDGE_DIR / "pdf"
MANIFEST_PATH = PROJECT_ROOT / "data" / "manifests" / "pdf-v1.json"

EVIDENCE_LINE_PATTERN = re.compile(r"^\[([A-Z]+-[0-9]{3})\]\s*(.*)$")
TITLE_PATTERN = re.compile(r"^#\s+(.*)$")

# 确定性元数据（与 D4-03 领域包同款固定时间戳纪律）。
FIXED_CREATED_AT = "2026-09-26T14:00:00+08:00"
FIXED_PRODUCER = "tools/rag/render_knowledge_pdfs.py (deterministic)"
FONT_NAME = "STSong-Light"
PAGE_SIZE = 2  # 每页证据条数

# 内置中文字体注册（reportlab CID 字体，无外部文件依赖）。
pdfmetrics.registerFont(UnicodeCIDFont(FONT_NAME))

TITLE_STYLE = ParagraphStyle(
    "title", fontName=FONT_NAME, fontSize=16, leading=22, spaceAfter=10
)
BODY_STYLE = ParagraphStyle(
    "body", fontName=FONT_NAME, fontSize=12, leading=20, spaceAfter=6
)


def _escape(text: str) -> str:
    """转义 XML 特殊字符，避免 Paragraph 把正文当标记解析。"""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def parse_markdown(text: str) -> tuple[str, list[tuple[str, str]]]:
    """解析夹具 Markdown：返回（标题, [(证据ID, 正文), ...]）。"""
    title = ""
    evidence: list[tuple[str, str]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        title_match = TITLE_PATTERN.match(line)
        if title_match:
            title = title_match.group(1).strip()
            continue
        ev_match = EVIDENCE_LINE_PATTERN.match(line)
        if ev_match:
            evidence.append((ev_match.group(1), ev_match.group(2).strip()))
    return title, evidence


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def render_one(source: Path, output: Path) -> tuple[int, dict[str, int]]:
    """渲染单个 Markdown 夹具为 PDF，返回（页数, {证据ID: 页码}）。"""
    text = source.read_text(encoding="utf-8")
    title, evidence = parse_markdown(text)
    if not evidence:
        raise ValueError(f"{source.name} 未解析出任何证据行")

    doc = SimpleDocTemplate(
        str(output),
        pagesize=A4,
        leftMargin=2 * cm,
        rightMargin=2 * cm,
        topMargin=2 * cm,
        bottomMargin=2 * cm,
        title=title or source.stem,
        author="AuditQuery synthetic fixtures",
        subject="自造知识夹具 PDF",
        creator=FIXED_PRODUCER,
    )

    flowables: list[Any] = []
    if title:
        flowables.append(Paragraph(_escape(title), TITLE_STYLE))
        flowables.append(Spacer(1, 6))

    evidence_pages: dict[str, int] = {}
    page_index = 1
    for index, (evidence_id, body) in enumerate(evidence, start=1):
        if index > 1 and (index - 1) % PAGE_SIZE == 0:
            flowables.append(PageBreak())
            page_index += 1
        evidence_pages[evidence_id] = page_index
        flowables.append(Paragraph(f"[{evidence_id}] {_escape(body)}", BODY_STYLE))

    doc.build(flowables)

    page_count = _count_pages(output)
    return page_count, evidence_pages


def _count_pages(pdf_path: Path) -> int:
    """用 pypdf 读取页数（渲染后立即校验产物可打开）。"""
    from pypdf import PdfReader  # 延迟导入，避免渲染脚本强依赖 pypdf 导入顺序

    with pdf_path.open("rb") as handle:
        return len(PdfReader(handle).pages)


def build_manifest(records: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "manifest_id": "pdf-v1",
        "version": "1.0.0",
        "created_at": FIXED_CREATED_AT,
        "hash_algorithm": "sha256",
        "renderer": "tools/rag/render_knowledge_pdfs.py",
        "font": FONT_NAME,
        "license": "internal-synthetic（自造夹具，无第三方许可负担）",
        "page_size": "A4",
        "documents": records,
    }


def collect_sources() -> list[Path]:
    """收集待渲染的 Markdown 源：既有 4 份（根目录）+ 新增 6 份（pdf 子目录）。

    新文档族放 ``tests/fixtures/knowledge/pdf/`` 子目录，避开 D3 契约冒烟
    对根目录 ``glob("*.md")`` 的数量断言（4 份冻结夹具不被污染）。
    """
    sources = sorted(KNOWLEDGE_DIR.glob("*.md"))
    if PDF_SOURCE_DIR.is_dir():
        sources.extend(sorted(PDF_SOURCE_DIR.glob("*.md")))
    return sources


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="渲染知识夹具 Markdown 为 PDF")
    parser.add_argument(
        "--output-root",
        default=str(PROJECT_ROOT),
        help="输出根目录（默认仓库根，便于测试在临时目录重建）",
    )
    parser.add_argument(
        "--manifest",
        default=str(MANIFEST_PATH),
        help="manifest 输出路径（默认为 data/manifests/pdf-v1.json）",
    )
    args = parser.parse_args(argv)

    output_root = Path(args.output_root)
    pdf_dir = output_root / "data" / "raw" / "pdf"
    pdf_dir.mkdir(parents=True, exist_ok=True)

    sources = collect_sources()
    if not sources:
        print("未找到任何夹具 Markdown", file=sys.stderr)
        return 1

    records: list[dict[str, Any]] = []
    for source in sources:
        output = pdf_dir / f"{source.stem}.pdf"
        page_count, evidence_pages = render_one(source, output)
        records.append(
            {
                "document_id": source.stem,
                "title": parse_markdown(source.read_text(encoding="utf-8"))[0],
                "source_markdown": str(source.relative_to(PROJECT_ROOT)),
                "pdf_path": str(output.relative_to(output_root)),
                "sha256": sha256_file(output),
                "bytes": output.stat().st_size,
                "page_count": page_count,
                "evidence_pages": evidence_pages,
            }
        )

    manifest = build_manifest(records)
    manifest_path = Path(args.manifest)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    print(f"渲染完成：{len(records)} 份 PDF -> {pdf_dir}")
    print(f"manifest -> {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
