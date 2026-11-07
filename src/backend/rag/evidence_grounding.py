"""RAG 引文到规范化页文本的确定性精确对齐。

本模块只处理 ``pdf_parser.normalize_extracted_text`` 产出的页文本坐标，不能把
字符区间解释成原始 PDF 字节偏移或页面 ``bbox``。它不调用模型、不访问网络，
用于在 citation 离开服务端前证明 ``quote == page_text[start:end]``。
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _overlapping_occurrence_count(source: str, quote: str) -> int:
    count = 0
    offset = 0
    while True:
        offset = source.find(quote, offset)
        if offset < 0:
            return count
        count += 1
        offset += 1


@dataclass(frozen=True)
class ExactTextGrounding:
    """一条引文在规范化页文本中的精确、半开字符区间。"""

    char_start: int
    char_end: int
    normalized_text_sha256: str
    quote_sha256: str
    occurrence_count: int
    alignment: str = "exact"

    def to_audit_record(
        self,
        *,
        evidence_id: str,
        document_id: str,
        page: int,
    ) -> dict[str, Any]:
        """返回不含原文正文的可审计记录。"""
        return {
            "alignment": self.alignment,
            "char_end": self.char_end,
            "char_start": self.char_start,
            "document_id": document_id,
            "evidence_id": evidence_id,
            "normalized_text_sha256": self.normalized_text_sha256,
            "occurrence_count": self.occurrence_count,
            "page": page,
            "quote_sha256": self.quote_sha256,
        }


def align_exact_text(
    normalized_page_text: str,
    quote: str,
) -> ExactTextGrounding | None:
    """把引文精确对齐到规范化页文本；找不到时返回 ``None``。

    重复引文按最早出现位置确定性选择，同时记录出现次数，供后续评测识别
    “虽然逐字命中、但位置有歧义”的样本。第一阶段不做模糊对齐，避免 OCR
    近似匹配被误写成已经核验的答案来源。
    """
    if not isinstance(normalized_page_text, str) or not isinstance(quote, str):
        return None
    if not normalized_page_text or not quote:
        return None
    start = normalized_page_text.find(quote)
    if start < 0:
        return None
    end = start + len(quote)
    return ExactTextGrounding(
        char_start=start,
        char_end=end,
        normalized_text_sha256=_sha256_text(normalized_page_text),
        quote_sha256=_sha256_text(quote),
        occurrence_count=_overlapping_occurrence_count(normalized_page_text, quote),
    )


def grounding_audit_sha256(records: list[dict[str, Any]]) -> str:
    """对对齐审计记录生成稳定摘要，供解释轨迹绑定。"""
    encoded = json.dumps(
        records,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "ExactTextGrounding",
    "align_exact_text",
    "grounding_audit_sha256",
]
