"""D5 在线 RAG 服务：把 published 文档检索包装成 visitor 的 ``route=rag`` 响应。

确定性、离线：不调用模型、不连网、不读取 Key。检索走
``src/backend/rag/published_retriever.PublishedDocumentIndex``（页级 BM25），
响应结构与 D4-07 ``online-response.v2`` 冻结契约对齐，供 ``visitor`` 的
``OnlineContractValidator`` 校验。

证据门：检索到的页分数低于 ``min_score``、或当前无 published 文档时，
返回 ``status=refused`` 且不带 citations，符合「RAG 无有效证据必须拒答」红线。
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Mapping

from ..rag.answer_composition import (
    ANSWER_MODE_EXTRACTIVE,
    compose_extractive_message,
    select_answer_facts,
)
from ..rag.evidence_grounding import grounding_audit_sha256
from ..rag.published_retriever import (
    DEFAULT_MIN_SCORE,
    DEFAULT_TOP_K,
    PublishedDocumentIndex,
)

# 契约冻结常量（对齐 shared-api-definitions.v1 的 decision.contract_version）。
DECISION_CONTRACT_VERSION = "1.0.0"


def _canonical_sha256(value: Any) -> str:
    """对结构化对象做确定性的 SHA-256（排序键、紧凑分隔符）。"""
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _grounded_answer(citations: list[dict[str, Any]]) -> str:
    """把页级证据整理成逐事实编号引用的可读回答，不引入无证据事实。

    NEXT-02：正文中的 ``[n]`` 与 ``citations`` 顺序严格一致，使每条事实都能
    按编号回溯到具体页；单条事实不会被截断成半句。无可用证据时返回拒答措辞，
    调用方据此给出 ``status=refused``。
    """
    return compose_extractive_message(citations)


def _groundings_match_citations(
    citations: list[dict[str, Any]],
    groundings: list[dict[str, Any]],
) -> bool:
    """确认私有对齐记录逐项覆盖即将返回的 citation。"""
    if not citations or len(citations) != len(groundings):
        return False
    for citation, grounding in zip(citations, groundings):
        if grounding.get("alignment") != "exact":
            return False
        if any(
            grounding.get(field) != citation.get(field)
            for field in ("evidence_id", "document_id", "page")
        ):
            return False
        start = grounding.get("char_start")
        end = grounding.get("char_end")
        snippet = citation.get("snippet")
        if (
            not isinstance(start, int)
            or not isinstance(end, int)
            or start < 0
            or end <= start
            or not isinstance(snippet, str)
            or end - start != len(snippet)
        ):
            return False
        occurrence_count = grounding.get("occurrence_count")
        # 页内重复文本虽可确定性选择首处，但尚不能证明该字符位置是唯一来源。
        # 继续保留页级 citation；只有唯一逐字命中才能对外标记“精确对齐”。
        if not isinstance(occurrence_count, int) or occurrence_count != 1:
            return False
        for hash_field in ("normalized_text_sha256", "quote_sha256"):
            value = grounding.get(hash_field)
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(ch not in "0123456789abcdef" for ch in value)
            ):
                return False
        if grounding["quote_sha256"] != hashlib.sha256(
            snippet.encode("utf-8")
        ).hexdigest():
            return False
    return True


class OnlineRagService:
    """访客问答的 RAG 路由：检索 published 文档，产出 route=rag 响应。"""

    def __init__(
        self,
        document_store: Any,
        *,
        top_k: int = DEFAULT_TOP_K,
        min_score: float = DEFAULT_MIN_SCORE,
    ) -> None:
        self.index = PublishedDocumentIndex(
            document_store, top_k=top_k, min_score=min_score
        )
        self.min_score = min_score
        self.top_k = top_k

    # ---- AgentPort 兼容入口 ----

    def submit(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """按 visitor 的 query request 检索并返回 route=rag 响应。

        输入 ``request`` 含 ``question``（访客问题）以及 ``session_id`` /
        ``request_id`` / ``turn_id`` / ``state_revision``；返回的 ``turn_id`` /
        ``state_revision`` 已按 visitor 约定推进（+1）。
        """
        started = time.monotonic()
        question = str(request["question"])
        self.index.rebuild()
        retrieved, retrieved_groundings = self.index.retrieve_with_groundings(question)
        # NEXT-02 逐事实引用：按分数顺序挑选能完整放进 256 字正文预算的证据，
        # 并剔除只命中标题的伪证据。正文编号与 citations 顺序严格一致；未参与
        # 回答的召回页不能仅因分数过门就冒充答案依据。
        facts = select_answer_facts(retrieved)
        kept_groundings: list[dict[str, Any]] = []
        cursor = 0
        for citation, grounding in zip(retrieved, retrieved_groundings):
            if cursor < len(facts) and citation is facts[cursor]:
                kept_groundings.append(grounding)
                cursor += 1
        citations = facts
        groundings = kept_groundings
        elapsed_ms = round(max(0.0, (time.monotonic() - started) * 1000.0), 3)
        return self._response(
            request,
            citations,
            elapsed_ms=elapsed_ms,
            groundings=groundings,
        )

    # ---- 响应构造 ----

    def _response(
        self,
        request: Mapping[str, Any],
        citations: list[dict[str, Any]],
        *,
        elapsed_ms: float,
        groundings: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        has_evidence = bool(citations)
        status = "success" if has_evidence else "refused"
        action = "accept" if has_evidence else "reject"
        reason_code = "evidence_retrieved" if has_evidence else "insufficient_evidence"
        top_score = max((c["score"] for c in citations), default=None)
        evidence_refs = [c["evidence_id"] for c in citations]
        exact_grounding = _groundings_match_citations(citations, groundings or [])
        grounding_sha256 = (
            grounding_audit_sha256(groundings or []) if exact_grounding else None
        )
        message = _grounded_answer(citations)
        return {
            "schema_version": "2.0.0",
            "session_id": str(request["session_id"]),
            "request_id": str(request["request_id"]),
            "turn_id": int(request["turn_id"]) + 1,
            "state_revision": int(request["state_revision"]) + 1,
            "route": "rag",
            "status": status,
            "message": message,
            "decision": {
                "contract_version": DECISION_CONTRACT_VERSION,
                "action": action,
                "reason_code": reason_code,
                "score_kind": "deterministic_bm25",
                "raw_score": top_score,
                "calibration_version": None,
                "thresholds": {
                    "accept_min": None,
                    "clarify_min": None,
                    "abstain_below": None,
                    "retrieval_min_score": self.min_score,
                },
                "evidence_refs": evidence_refs,
                "missing_or_conflicting_slots": [],
                "accept_authorizes_sql_execution": False,
                "requires_d4_02_safety_gate": False,
            },
            "pending_question": None,
            "candidate_evidence": [
                {
                    "candidate_id": c["evidence_id"],
                    "display_name": f"第{c['page']}页",
                    "candidate_kind": "published_page",
                    "score": c["score"],
                    "evidence_refs": [c["evidence_id"]],
                }
                for c in citations
            ],
            "sql": None,
            "citations": citations,
            "trace": [
                {
                    "sequence": 1,
                    "stage": "rag_retrieval",
                    "action": "retrieve",
                    "reason_code": "deterministic_bm25",
                    "evidence_refs": evidence_refs,
                    "artifact_sha256": _canonical_sha256(
                        {"evidence_refs": evidence_refs, "top_k": self.top_k}
                    ),
                },
                {
                    "sequence": 2,
                    "stage": "evidence_gate",
                    "action": action,
                    "reason_code": reason_code,
                    "evidence_refs": evidence_refs,
                    "artifact_sha256": (
                        grounding_sha256
                        if grounding_sha256 is not None
                        else _canonical_sha256(
                            {
                                "accepted_evidence_ids": evidence_refs,
                                "min_score": self.min_score,
                            }
                        )
                    ),
                },
            ],
            "usage": {
                "model_calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "estimated_cost_cny": 0,
                "elapsed_ms": elapsed_ms,
                "stopped_reason": None,
            },
            "error": None,
            # NEXT-02：显式标注回答模式。schema 要求 warnings 元素匹配
            # ^[a-z][a-z0-9_]{1,63}$ 且 uniqueItems，故模式写成单一标记。
            # 模型模式（ANSWER_MODE_MODEL）当前未接线，真实调用需人类授权。
            "warnings": (
                [
                    "offline_deterministic_rag",
                    ANSWER_MODE_EXTRACTIVE,
                    "deterministic_extractive_answer",
                    "exact_source_grounding",
                ]
                if exact_grounding
                else (
                    [
                        "offline_deterministic_rag",
                        ANSWER_MODE_EXTRACTIVE,
                        "deterministic_extractive_answer",
                    ]
                    if has_evidence
                    else ["offline_deterministic_rag", "insufficient_evidence"]
                )
            ),
        }


__all__ = ["OnlineRagService"]
