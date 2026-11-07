"""D5 产品检索：从 published 文档构建页级 BM25 索引并检索（确定性、离线）。

与 D4-06 的差别：D4-06 检索的是预写知识夹具（source markdown 里的
``[XXX-NNN]`` 证据标记），而 D5 的 published 文档是管理员任意上传的 PDF，
没有证据标记约定。因此本模块以「页」为检索单元，citation 回指真实的
``document_id / page / snippet``，满足 D5 契约「访客只能检索 published 文档，
引用回指真实 document/page/evidence」。

仅离线：网络调用 0、模型 API 调用 0。
"""

from __future__ import annotations

import threading
import re
import hashlib
from typing import Any

from .evidence_grounding import align_exact_text
from .lexical_retriever import BM25Retriever, tokenize
from .pdf_parser import extract_page_texts

# 默认检索参数：top-k 与最低分闸门（分数低于此值视为证据不足 → 拒答）。
DEFAULT_TOP_K = 10
DEFAULT_MIN_SCORE = 1.0


def _document_title(page_texts: list[str]) -> str:
    for text in page_texts:
        for line in text.splitlines():
            title = re.split(r"\[[A-Z]+-\d{3}\]", line, maxsplit=1)[0].strip()
            if title:
                return title[:128]
    return "已发布文档"


def _matching_snippet(text: str, query_terms: set[str]) -> str:
    # Evidence markers delimit independent assertions in the bundled PDFs.
    # Select a relevant block before cropping so adjacent evidence never leaks
    # into a citation merely because it shares a page.
    blocks = re.split(r"\[[A-Z]+-\d{3}\]\s*", text)
    if len(blocks) > 1:
        blocks = blocks[1:]  # A title alone is not answer evidence.
    else:
        # Ordinary uploads have no fixture markers. Reserve the first nonempty
        # line as a possible page heading, never as answer evidence. If layout
        # cannot distinguish a heading from body (a single-line page), abstain.
        # Physical PDF lines can split a sentence before a trailing exception.
        # Join until an explicit sentence ending; never crop a line prefix into
        # an apparently complete fact. Short independent sentences stay apart.
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        blocks = []
        pending = []
        for line in lines[1:]:
            pending.append(line)
            if re.search(r'[.!?。！？][\"\u201d\u2019)]*$', line):
                blocks.append('\n'.join(pending))
                pending = []
        if pending:
            blocks.append('\n'.join(pending))
    blocks = [block.strip() for block in blocks if block.strip()]
    if not blocks:
        return ""
    block = max(blocks, key=lambda value: len(query_terms.intersection(tokenize(value))))
    if not query_terms.intersection(tokenize(block)):
        return ""
    # A long evidence unit cannot fit the frozen citation/message budget. Do
    # not discard its tail (which can contain negation/exception conditions).
    return block if len(block) <= 240 else ''


class PublishedDocumentIndex:
    """按需重建的 published 文档页级索引。

    设计要点：
    - 每次 ``rebuild`` 都从 ``document_store`` 读取当前活动索引（``active_index``）
      指向的那份 published 文档，重新解析 PDF、重建 BM25——检索路径无持久化
      状态，杜绝「已 disable/delete 的文档仍可被检索」的陈旧索引问题。
    - 检索是纯确定性的 BM25 词法匹配；分数低于 ``min_score`` 视为无证据。
    """

    def __init__(
        self,
        document_store: Any,
        *,
        top_k: int = DEFAULT_TOP_K,
        min_score: float = DEFAULT_MIN_SCORE,
    ) -> None:
        self._store = document_store
        self._top_k = top_k
        self._min_score = min_score
        self._lock = threading.RLock()
        self._retriever: BM25Retriever | None = None
        self._pages: list[dict[str, Any]] = []
        self._document_id: str | None = None
        self._document_sha256: str | None = None
        self._index_version: str | None = None

    # ---- 活动 published 文档 ----

    def _active_published(self) -> Any:
        """返回当前活动索引指向的 published 文档记录；无则 None。"""
        active = self._store.get_active_index()
        if active is None:
            return None
        for record in self._store.list_records():
            if record.state == "published" and record.index_version == active:
                return record
        return None

    # ---- 索引重建 ----

    def rebuild(self) -> bool:
        """从当前 published 文档重建页级索引；返回是否有可检索内容。"""
        with self._lock:
            self._retriever = None
            self._pages = []
            self._document_id = None
            self._document_sha256 = None
            self._index_version = None
            record = self._active_published()
            if record is None:
                return False
            pdf_bytes = self._store.read_quarantine(record.document_id)
            if (hashlib.sha256(pdf_bytes).hexdigest() != record.sha256
                    or len(pdf_bytes) != record.bytes):
                raise ValueError("published_pdf_integrity_mismatch")
            page_texts = extract_page_texts(pdf_bytes, preserve_lines=True)
            if len(page_texts) != record.page_count:
                raise ValueError("published_pdf_page_count_mismatch")
            title = _document_title(page_texts)
            evidence_map: dict[str, str] = {}
            for index, text in enumerate(page_texts, start=1):
                if not text:
                    # 空页不参与索引，但仍保持页码与真实页一一对应。
                    continue
                evidence_id = f"{record.document_id}.p{index}"
                evidence_map[evidence_id] = text
                self._pages.append(
                    {
                        "evidence_id": evidence_id,
                        "document_id": record.document_id,
                        "document_title": title,
                        "page": index,
                        "text": text,
                    }
                )
            if not evidence_map:
                # 整份 PDF 无任何可提取文本 → 无可检索内容。
                self._pages = []
                self._retriever = None
                return False
            self._retriever = BM25Retriever(evidence_map)
            self._document_id = record.document_id
            self._document_sha256 = record.sha256
            self._index_version = record.index_version
            return True

    # ---- 检索 ----

    @property
    def active_document_id(self) -> str | None:
        return self._document_id

    @property
    def active_index_version(self) -> str | None:
        return self._index_version

    def retrieve(self, query: str) -> list[dict[str, Any]]:
        """检索相关页，返回 citations（按分数降序，仅保留 >= min_score）。

        无 published 文档、无证据、或分数低于阈值时返回空列表，由上层判定拒答。
        """
        citations, _ = self.retrieve_with_groundings(query)
        return citations

    def retrieve_with_groundings(
        self,
        query: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """返回兼容 v2 的 citations 及服务端精确对齐审计记录。

        审计记录中的字符区间只针对规范化页文本；冻结的 online-response.v2
        citation 尚未包含坐标字段，因此本方法把记录单独返回给解释轨迹绑定，
        不向 v2 citation 静默添加字段。
        """
        with self._lock:
            if self._retriever is None:
                return [], []
            ranked = self._retriever.retrieve(query, self._top_k)
            query_terms = set(tokenize(query))
            by_id = {page["evidence_id"]: page for page in self._pages}
            citations: list[dict[str, Any]] = []
            groundings: list[dict[str, Any]] = []
            for evidence_id, score in ranked:
                if score < self._min_score:
                    continue
                page = by_id.get(evidence_id)
                if page is None:
                    continue
                text = page["text"]
                snippet = _matching_snippet(text, query_terms)
                grounding = align_exact_text(text, snippet)
                if grounding is None:
                    continue
                citations.append(
                    {
                        "evidence_id": evidence_id,
                        "document_id": page["document_id"],
                        "document_title": page["document_title"],
                        "page": page["page"],
                        "snippet": snippet,
                        "score": round(score, 6),
                    }
                )
                groundings.append(
                    grounding.to_audit_record(
                        evidence_id=evidence_id,
                        document_id=page["document_id"],
                        page=page["page"],
                    )
                )
            return citations, groundings


__all__ = [
    "PublishedDocumentIndex",
    "DEFAULT_TOP_K",
    "DEFAULT_MIN_SCORE",
]
