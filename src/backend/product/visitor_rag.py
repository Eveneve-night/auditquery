"""D5 访客问答的 RAG 优先 dispatcher（AgentPort 实现）。

路由决策：访客 query 先做确定性的 published 文档检索，命中证据则走
``route=rag``（带 citations），否则回退到既有 SQL 工作流（``VisitorSqlAdapter``）。
clarification 恒走 SQL（RAG 不产生澄清）。

这是 C 侧的运行时接线：把 C 的 RAG 检索接到 B 的 visitor agent 端口上。
SQL / SecretStore / SafetyGate 权限边界仍由 B 的 ``VisitorSqlAdapter`` 内部
保证，本 dispatcher 不绕过、不重建任何安全门。
"""

from __future__ import annotations

from contextlib import contextmanager
import threading
from typing import Any, Mapping

from ..api.online_rag_service import OnlineRagService
from .visitor_sql import VisitorSqlAdapter


class RagFirstAgent:
    """RAG 优先、无证据回退 SQL 的访客 agent。

    ``transaction`` 语义与 ``VisitorSqlAdapter`` 一致：RAG 命中是无状态的
    确定性检索（无 pending 半提交风险），直接产出；未命中则委托 SQL 的
    transaction，保留澄清 pending 的失败回滚保护（对应 D5 半提交 P1）。
    """

    def __init__(self, rag_service: OnlineRagService, sql_adapter: VisitorSqlAdapter) -> None:
        self.rag = rag_service
        self.sql = sql_adapter
        self._lock = threading.RLock()

    def _rag_hits(self, question: str) -> bool:
        """RAG 是否命中：先重建索引（读当前 published 文档）再检索。"""
        with self._lock:
            self.rag.index.rebuild()
            return bool(self.rag.index.retrieve(question))

    def submit(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        """直接提交（防御性 fallback；正常由 visitor 走 ``transaction``）。"""
        if "question" in request and self._rag_hits(str(request["question"])):
            return self.rag.submit(request)
        return self.sql.submit(request)

    @contextmanager
    def transaction(self, request: Mapping[str, Any]):
        if "question" in request and self._rag_hits(str(request["question"])):
            result = self.rag.submit(request)

            def accept() -> None:
                # RAG 检索无持久化 pending 状态，无需回滚。
                return None

            yield result, accept
            return
        with self.sql.transaction(request) as (submitted, accept):
            yield submitted, accept


def build_rag_first_agent(
    document_store: Any,
    sql_adapter: VisitorSqlAdapter,
    *,
    top_k: int | None = None,
    min_score: float | None = None,
) -> RagFirstAgent:
    """构造 RAG 优先 agent（不调用模型、不连网）。"""
    kwargs: dict[str, Any] = {}
    if top_k is not None:
        kwargs["top_k"] = top_k
    if min_score is not None:
        kwargs["min_score"] = min_score
    rag_service = OnlineRagService(document_store, **kwargs)
    return RagFirstAgent(rag_service, sql_adapter)


__all__ = ["RagFirstAgent", "build_rag_first_agent"]
