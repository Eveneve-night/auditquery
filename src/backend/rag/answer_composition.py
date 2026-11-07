"""NEXT-02：把页级证据组装成「逐事实编号引用」的确定性摘录回答。

本模块离线运行，不调用模型、不连网、不读取 Key。它只负责三件事：

1. 逐事实引用：每条用于回答的证据独立编号 ``[1] [2] ...``，正文顺序与
   ``citations`` 顺序严格一致，使访客能按编号回溯到具体页。
2. 长度预算内的完整事实：``online_response.message`` 硬上限 256 字符
   （引自 shared-api-definitions.v2）。本模块**绝不把单条事实截断成半句**——
   放不下整条事实就不放，避免产生无法与原文逐字比对的部分引用。
3. 标题 / 同名误命中防护：若命中片段与文档标题实质重复，说明该页只有标题
   被词法匹配到，不构成答案证据，必须排除；排除后若无剩余证据则触发拒答。

契约边界：``online_response`` 与 ``citation`` 在冻结 schema 中均为
``additionalProperties: false``，因此本模块不新增任何字段，模式标记只通过
``warnings`` 字符串表达（schema 限制 ``^[a-z][a-z0-9_]{1,63}$``）。
"""

from __future__ import annotations

import re
from typing import Any

# online_response.message 的 maxLength（shared-api-definitions.v2.schema.json）。
MAX_MESSAGE_CHARS = 256

# warnings 中的回答模式标记。模型模式当前**未接线**，仅作为显式区分保留，
# 真实模型调用需人类对本批任务另行授权（AGENTS.md 第 5 节）。
ANSWER_MODE_EXTRACTIVE = "answer_mode_extractive"
ANSWER_MODE_MODEL = "answer_mode_model"

_PUNCTUATION = re.compile(r"[\s　,.;:!?，。；：！？、\-—_/\\|()\[\]{}<>\"'`~*#]+")


def _normalize_for_compare(text: str) -> str:
    """归一化用于比较：去标点与空白并折叠大小写。"""
    return _PUNCTUATION.sub("", text).casefold()


def is_title_only_evidence(snippet: str, document_title: str) -> bool:
    """判断命中片段是否只是文档标题的重复（标题/同名误命中）。

    标题行本身不是答案证据：它只能说明文档主题，不能回答具体问题。若片段
    归一化后与标题相同，或片段是标题的截断前缀，都判为误命中。
    """
    if not isinstance(snippet, str) or not isinstance(document_title, str):
        return False
    probe = _normalize_for_compare(snippet)
    title = _normalize_for_compare(document_title)
    if not probe or not title:
        return False
    if probe == title:
        return True
    # 片段被截断成标题前缀时同样不构成独立事实。
    return len(probe) >= 8 and title.startswith(probe)


def select_answer_facts(
    citations: list[dict[str, Any]],
    *,
    max_chars: int = MAX_MESSAGE_CHARS,
) -> list[dict[str, Any]]:
    """挑选能完整放进正文预算的证据，调用方需已按分数降序传入。

    返回参与回答的 citation 子集；排除空片段与标题误命中；任何一条事实都
    不会被截断。第一条恒尝试保留（单条 snippet 上限 240，加编号后仍合规）。
    """
    selected: list[dict[str, Any]] = []
    used = 0
    for citation in citations:
        if not isinstance(citation, dict):
            continue
        snippet = str(citation.get("snippet", "")).strip()
        if not snippet:
            continue
        if is_title_only_evidence(snippet, str(citation.get("document_title", ""))):
            continue
        piece = f"{snippet} [{len(selected) + 1}]"
        cost = len(piece) + (1 if selected else 0)
        if used + cost > max_chars:
            # 不做半句截断：本条放不下就整体放弃，后续一并停止。
            break
        selected.append(citation)
        used += cost
    return selected


def compose_extractive_message(facts: list[dict[str, Any]]) -> str:
    """把选定事实拼成逐事实编号引用的正文。

    无可用事实时返回拒答措辞，调用方据此给出 ``status=refused``。
    """
    if not facts:
        return "检索证据不足，系统已拒绝生成答案。"
    return " ".join(
        f"{str(fact.get('snippet', '')).strip()} [{index}]"
        for index, fact in enumerate(facts, start=1)
    )


def is_message_within_limit(message: str, *, max_chars: int = MAX_MESSAGE_CHARS) -> bool:
    """校验正文未越过契约上限（供调用方在返回前自检）。"""
    return isinstance(message, str) and 0 < len(message) <= max_chars


__all__ = [
    "ANSWER_MODE_EXTRACTIVE",
    "ANSWER_MODE_MODEL",
    "MAX_MESSAGE_CHARS",
    "compose_extractive_message",
    "is_message_within_limit",
    "is_title_only_evidence",
    "select_answer_facts",
]
