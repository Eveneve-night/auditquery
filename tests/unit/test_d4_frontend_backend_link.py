"""D4-07 C 侧前端 ↔ B 侧在线 SQL 后端联调测试（进程内桥接，离线）。

验证目标：用前端 adapter 的真实代码（node 执行）生成请求，交给 B 侧
OnlineSqlService 处理，确认两端契约对齐：

1. 前端 `newSession()` 的 `turn_id=0` 与 B 后端 `_submit_query` 的
   `initial_turn_id==0` 一致，首问不再触发 `initial_turn_required`(409)；
2. 前端 `buildSubmitClarification` 六字段从 pending 原样回传，B 后端不返回
   `clarification_*_mismatch`；
3. query → clarification(metric) → clarification(entity_type) → success
   全链路闭环，最终 SQL 通过 SafetyGate 并只读执行。

本测试不发起网络或模型调用（注入 FakeProposalPort），不读 expected/gold/label。
"""

from __future__ import annotations

from copy import deepcopy
import json
import subprocess
from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

from backend.agent.clarification_recovery import SqlProposal  # noqa: E402
from backend.api.online_sql_service import OnlineSqlService  # noqa: E402
from backend.nl2sql.online_proposal import (  # noqa: E402
    MODEL_ID,
    PROVIDER_ID,
    ProposalTelemetry,
)

PROBE = PROJECT_ROOT / "tests" / "fixtures" / "d4-07" / "probe_adapter_link.mjs"


class FakeProposalPort:
    """注入的假 proposal port，不发起真实模型调用。"""

    def __init__(self, sql: str = "SELECT COUNT(*) AS artist_count FROM Artist") -> None:
        self.sql = sql
        self.calls: list[tuple] = []
        self.last_telemetry = None

    def propose(self, *, conversation_state, slot_observations):
        self.calls.append((deepcopy(conversation_state), deepcopy(slot_observations)))
        proposal = SqlProposal(
            proposal_id="proposal.link",
            sql=self.sql,
            tables=("Artist",),
            params=(),
        )
        self.last_telemetry = ProposalTelemetry(
            request_id="request.link.nl2sql",
            provider_id=PROVIDER_ID,
            returned_model=MODEL_ID,
            attempt_count=1,
            input_tokens=100,
            output_tokens=20,
            total_tokens=120,
            estimated_cost_cny=0.0001,
            latency_ms=12.5,
            proposal_sha256="a" * 64,
        )
        return proposal


def _node_adapter(action: str, payload: dict) -> dict:
    result = subprocess.run(
        ["node", str(PROBE), action, json.dumps(payload, ensure_ascii=False)],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        raise RuntimeError(f"node 探针执行失败：\n{result.stderr}")
    return json.loads(result.stdout.strip().splitlines()[-1])


class FrontendBackendLinkTest(unittest.TestCase):
    def setUp(self) -> None:
        self.port = FakeProposalPort()
        self.service = OnlineSqlService(
            proposal_port=self.port,
            table_name="Artist",
            now=lambda: "2026-10-01T08:00:00Z",
        )

    def test_frontend_adapter_links_against_backend_full_flow(self) -> None:
        # 1. 前端 newSession + buildSubmitQuery（真实 JS 代码）
        out = _node_adapter("new-query", {"query": "统计艺术家数量"})
        session, query_req = out["session"], out["req"]

        # 关键：前端 turn_id 起始值必须为 0，与 B 后端 initial_turn_id==0 对齐
        self.assertEqual(0, session["turn_id"])
        self.assertEqual(0, query_req["turn_id"])
        self.assertEqual(0, query_req["state_revision"])
        self.assertEqual("submit_query", query_req["operation"])
        self.assertEqual("2.0.0", query_req["schema_version"])

        # 2. B 后端处理首问：不 409，而是澄清
        response = self.service.submit(query_req)
        self.assertEqual("clarification_required", response["status"])
        self.assertEqual("slot.metric", response["pending_question"]["slot_id"])

        # 3. 多轮澄清：每轮用前端 adapter 从 pending 生成 submit_clarification。
        #    answer_id 由测试侧传入全局递增序号（模拟前端单页面 answerCounter 跨轮递增；
        #    本测试每轮开独立 node 进程，adapter 的模块级计数器会重置，故显式传入）。
        answer_seq = 0
        rounds = 0
        while response["status"] == "clarification_required" and rounds < 5:
            rounds += 1
            answer_seq += 1
            pending = response["pending_question"]
            option = pending["options"][0]
            answer = _node_adapter(
                "clarify",
                {
                    "pending": pending,
                    "value": option["value"],
                    "evidence_ref": option["evidence_ref"],
                    "answer_id": f"answer.link.{answer_seq}",
                },
            )
            # 六字段从 pending 原样回传（前端契约核心），answer_id 新生成且唯一
            for field in (
                "session_id",
                "request_id",
                "turn_id",
                "state_revision",
                "question_id",
                "slot_id",
            ):
                self.assertEqual(pending[field], answer[field], f"{field} 必须原样回传")
            self.assertEqual(f"answer.link.{answer_seq}", answer["answer_id"])
            self.assertEqual([option["value"]], answer["values"])
            self.assertEqual([option["evidence_ref"]], answer["evidence_refs"])
            response = self.service.submit(answer)

        # 4. 全链路闭环：SQL 通过 SafetyGate 并只读执行
        self.assertEqual("success", response["status"])
        self.assertEqual("approved", response["sql"]["safety_status"])
        self.assertEqual([[275]], response["sql"]["rows"])
        self.assertEqual(1, response["sql"]["execution_attempts"])
        self.assertGreaterEqual(rounds, 2)  # metric + entity_type 两轮澄清

    def test_frontend_turn_id_zero_is_required_by_backend(self) -> None:
        # 前端若仍用旧 turn_id=1，B 后端必须拒绝（证明对齐是必要的）
        from backend.api.online_sql_service import OnlineSqlApiError

        out = _node_adapter("new-query", {"query": "统计艺术家数量"})
        query_req = out["req"]
        query_req["turn_id"] = 1  # 模拟旧前端起始值
        with self.assertRaises(OnlineSqlApiError) as ctx:
            self.service.submit(query_req)
        self.assertEqual("initial_turn_required", ctx.exception.code)


if __name__ == "__main__":
    unittest.main()
