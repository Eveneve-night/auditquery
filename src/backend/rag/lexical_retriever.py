"""D4-06 最小词法检索器（零依赖 BM25，字符 bigram + ASCII token）。

设计卡决策 5：只用 ``input.query`` 与证据文本做打分，不得读取 ``expected/*``；
不引入嵌入模型与向量库（避免新增供应商变量）。

中文按字符 bigram 切词，无需外部分词器；英文/数字按连续 token 切分，
大小写归一。打分用 BM25，返回 top-k 证据 ID 与分数，供上层判断是否拒答。
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from typing import Mapping, Sequence

ASCII_TOKEN = re.compile(r"[A-Za-z0-9]+")
CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")

K1 = 1.5
B = 0.75


def tokenize(text: str) -> list[str]:
    """切词：ASCII 连续 token（小写）+ 中文字符 bigram。"""
    tokens: list[str] = []
    for tok in ASCII_TOKEN.findall(text):
        tokens.append(tok.lower())
    for run in CJK_RUN.findall(text):
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
    return tokens


class BM25Retriever:
    """对证据文本集合做 BM25 词法检索。"""

    def __init__(self, evidence: Mapping[str, str]) -> None:
        if not evidence:
            raise ValueError("evidence 不能为空")
        self.evidence = dict(evidence)
        self._doc_ids = list(self.evidence)
        self._tokenized: dict[str, list[str]] = {
            doc_id: tokenize(text) for doc_id, text in self.evidence.items()
        }
        self._doc_len: dict[str, int] = {
            doc_id: len(tokens) for doc_id, tokens in self._tokenized.items()
        }
        self._avgdl = sum(self._doc_len.values()) / len(self._doc_len)
        # term -> 出现该 term 的文档数（文档频率）
        self._df: dict[str, int] = defaultdict(int)
        for tokens in self._tokenized.values():
            for term in set(tokens):
                self._df[term] += 1
        self._n = len(self._doc_ids)

    def _idf(self, term: str) -> float:
        df = self._df.get(term, 0)
        return math.log((self._n - df + 0.5) / (df + 0.5) + 1.0)

    def _score(self, doc_id: str, query_terms: list[str]) -> float:
        tokens = self._tokenized[doc_id]
        length = self._doc_len[doc_id]
        freq: dict[str, int] = defaultdict(int)
        for term in tokens:
            freq[term] += 1
        total = 0.0
        for term in query_terms:
            f = freq.get(term, 0)
            if f == 0:
                continue
            denom = f + K1 * (1 - B + B * length / self._avgdl)
            total += self._idf(term) * (f * (K1 + 1)) / denom
        return total

    def retrieve(self, query: str, k: int) -> list[tuple[str, float]]:
        """返回 top-k（证据ID, 分数），按分数降序；分数 0 的条目也会返回以保证确定性。"""
        if k <= 0:
            raise ValueError("k 必须为正整数")
        query_terms = tokenize(query)
        if not query_terms:
            return []
        scored = [(doc_id, self._score(doc_id, query_terms)) for doc_id in self._doc_ids]
        scored.sort(key=lambda item: (-item[1], item[0]))
        return scored[:k]


def build_evidence_index(
    evidence_texts: Mapping[str, str],
) -> BM25Retriever:
    """便捷构造：从 {证据ID: 证据文本} 建索引。"""
    return BM25Retriever(evidence_texts)


def retrieve_top_evidence(
    retriever: BM25Retriever,
    query: str,
    k: int,
    min_score: float,
) -> Sequence[str]:
    """检索并应用最低分闸门：低于阈值视为证据不足，返回空（供上层拒答）。

    这是离线可测的「检索命中但证据不足 → 拒答」判定边界；实际问答仍由
    grounded-answer 契约 + 模型在有据证据上完成。
    """
    ranked = retriever.retrieve(query, k)
    return [doc_id for doc_id, score in ranked if score >= min_score]
