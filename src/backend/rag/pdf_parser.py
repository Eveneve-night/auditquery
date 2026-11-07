"""D4-06 PDF 解析器：pdfplumber 主解析 + pypdf 交叉验证。

- 从 PDF 提取证据 ID（复用 ``[XXX-NNN]`` 前缀约定）；
- 产出「证据 ID → 页码」映射（支撑 M-05 页/片段级定位，Markdown 载体给不了）；
- 双解析器证据 ID 集合一致性校验（不一致即解析缺陷回归用例，见 D4-06 决策 2）。

仅离线：网络调用 0，模型 API 调用 0。
"""

from __future__ import annotations

import io
import re
from pathlib import Path

# 与 tools/evaluation/validate_seed_assets.py、tests/unit/test_grounded_answer_contract.py
# 保持一致的证据 ID 约定：大写字母前缀 + 连字符 + 三位数字。
EVIDENCE_ID_PATTERN = re.compile(r"\[([A-Z]+-[0-9]{3})\]")


def normalize_extracted_text(text: str) -> str:
    """归一化 PDF 提取文本，消除换行/空白差异对行重组的影响。

    提取文本中证据 ID 与正文可能被换行分隔，这里统一为单行、合并连续空白，
    便于后续用全局证据 ID 模式匹配（ID 在正文中唯一，不依赖行首）。
    """
    return re.sub(r"\s+", " ", text).strip()


def extract_evidence_pages(pdf_path: Path) -> dict[str, int]:
    """用 pdfplumber 逐页提取，返回 {证据ID: 首次出现的页码}。"""
    import pdfplumber  # 延迟导入，缺依赖时仅解析器路径报错

    evidence_pages: dict[str, int] = {}
    with pdfplumber.open(str(pdf_path)) as pdf:
        for page_number, page in enumerate(pdf.pages, start=1):
            raw_text = page.extract_text() or ""
            text = normalize_extracted_text(raw_text)
            for evidence_id in EVIDENCE_ID_PATTERN.findall(text):
                evidence_pages.setdefault(evidence_id, page_number)
    return evidence_pages


def extract_ids_with_pypdf(pdf_path: Path) -> set[str]:
    """用 pypdf 逐页提取，返回证据 ID 集合（用于交叉验证）。"""
    from pypdf import PdfReader  # 延迟导入

    ids: set[str] = set()
    with pdf_path.open("rb") as handle:
        reader = PdfReader(handle)
        for page in reader.pages:
            raw_text = page.extract_text() or ""
            ids |= set(EVIDENCE_ID_PATTERN.findall(normalize_extracted_text(raw_text)))
    return ids


def cross_validate(pdf_path: Path) -> tuple[dict[str, int], set[str], set[str], bool]:
    """交叉验证：pdfplumber 主解析 + pypdf 对照。

    返回 (evidence_pages, plumber_ids, pypdf_ids, consistent)。
    ``consistent`` 为 False 表示两库提取的证据 ID 集合不一致，属于解析缺陷。
    """
    evidence_pages = extract_evidence_pages(pdf_path)
    plumber_ids = set(evidence_pages)
    pypdf_ids = extract_ids_with_pypdf(pdf_path)
    consistent = plumber_ids == pypdf_ids
    return evidence_pages, plumber_ids, pypdf_ids, consistent


def extract_page_texts(pdf_bytes: bytes, *, preserve_lines: bool = False) -> list[str]:
    """提取 PDF 每一页的归一化文本，返回 ``[第1页文本, 第2页文本, ...]``。

    用于 D5 产品检索：published 文档是任意上传的 PDF，没有 D4-06 的
    ``[XXX-NNN]`` 证据标记约定，因此以「页」为检索单元。空页返回空串以保持
    页码与 ``citations[].page`` 的一一对齐。``preserve_lines`` 供发布检索区分
    标题与正文；默认保持原有单行归一化接口。仅离线：不调用模型、不连网。
    """
    import pdfplumber  # 延迟导入，缺依赖时仅检索路径报错

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        return [
            ("\n".join(normalize_extracted_text(line) for line in
                       (page.extract_text() or "").splitlines())
             if preserve_lines else normalize_extracted_text(page.extract_text() or ""))
            for page in pdf.pages
        ]
