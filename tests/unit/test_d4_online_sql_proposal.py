"""D4-07-01 B online SQL proposal adapter offline tests."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

from backend.agent.clarification_recovery import build_clarification_answer  # noqa: E402
from backend.agent.intent_routing import (  # noqa: E402
    apply_intent_result,
    create_conversation_state,
)
from backend.agent.selective_routing import build_slot_observations  # noqa: E402
from backend.api.d4_demo import D4DemoService, _intent_result  # noqa: E402
from backend.llm.contracts import ModelCallResult, ModelUsage  # noqa: E402
from backend.nl2sql.online_proposal import (  # noqa: E402
    MODEL_ID,
    PROVIDER_ID,
    OnlineSqlProposalError,
    OnlineSqlProposalPort,
)


def _success_output(sql: str = "SELECT COUNT(*) AS artist_count FROM Artist") -> dict:
    return {
        "schema_version": "1.0.0",
        "task_type": "nl2sql",
        "status": "success",
        "data": {
            "outcome": "sql",
            "sql": sql,
            "tables": ["Artist"],
            "params": [],
            "rationale": "在 Artist 单表统计记录数。",
        },
        "error": None,
        "warnings": [],
    }


class FakeGateway:
    def __init__(
        self,
        output: dict,
        *,
        returned_model: str = MODEL_ID,
        provider_id: str = PROVIDER_ID,
    ) -> None:
        self.output = output
        self.returned_model = returned_model
        self.provider_id = provider_id
        self.requests = []

    def generate_structured(self, request):
        self.requests.append(request)
        return ModelCallResult(
            request_id=request.request_id,
            task_type=request.task_type,
            provider_id=self.provider_id,
            requested_model=MODEL_ID,
            returned_model=self.returned_model,
            provider_request_id="fake-provider-request",
            parsed_output=deepcopy(self.output),
            usage=ModelUsage(100, 20, 120),
            estimated_cost_cny=0.0001,
            latency_ms=12.5,
            finish_reason="stop",
        )


class FailingGateway:
    def generate_structured(self, request):
        del request
        raise RuntimeError("TESTONLY raw provider failure must be hidden")


class OnlineSqlProposalTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.demo = D4DemoService()
        cls.scenario = cls.demo.scenarios["D4-07-SQL-001"]
        cls.state = apply_intent_result(
            create_conversation_state("session.online.sql", cls.scenario["query"]),
            cls.demo.source_catalog,
            _intent_result(),
        )
        cls.complete_observations = build_slot_observations(
            {
                "slot.metric": {
                    "values": ["count"],
                    "evidence_refs": ["evidence.metric_kinds"],
                },
                "slot.entity_type": {
                    "values": ["artist"],
                    "evidence_refs": ["evidence.entity_kinds"],
                },
            }
        )

    def test_builds_frozen_request_and_content_safe_telemetry(self) -> None:
        gateway = FakeGateway(_success_output())
        port = OnlineSqlProposalPort(
            gateway, table_name="Artist", domain_pack=self.demo.recovery.domain_pack
        )
        proposal = port.propose(
            conversation_state=self.state,
            slot_observations=self.complete_observations,
        )
        self.assertEqual(("Artist",), proposal.tables)
        self.assertFalse(hasattr(proposal, "approved"))
        request = gateway.requests[0]
        self.assertEqual("nl2sql", request.task_type)
        self.assertEqual("nl2sql.v1", request.prompt_version)
        self.assertEqual("nl2sql-result.v1", request.output_schema_id)
        payload = json.loads(request.messages[1].content)
        self.assertEqual("Artist", payload["database_schema"]["table"])
        self.assertEqual(["count"], payload["resolved_slots"]["slot.metric"]["values"])
        encoded = json.dumps(port.last_telemetry.to_dict(), ensure_ascii=False)
        self.assertNotIn(proposal.sql, encoded)
        self.assertNotIn("artist", encoded.casefold())

    def test_invalid_schema_model_and_table_drift_fail_closed(self) -> None:
        invalid = _success_output()
        invalid["unexpected"] = True
        wrong_table = _success_output()
        wrong_table["data"]["tables"] = ["Album"]
        cases = (
            (FakeGateway(invalid), "sql_proposal_schema_invalid"),
            (FakeGateway(_success_output(), returned_model="wrong-model"), "sql_proposal_model_contract_mismatch"),
            (FakeGateway(_success_output(), provider_id="second-provider"), "sql_proposal_model_contract_mismatch"),
            (FakeGateway(wrong_table), "sql_proposal_table_mismatch"),
        )
        for gateway, code in cases:
            with self.subTest(code=code):
                port = OnlineSqlProposalPort(
                    gateway,
                    table_name="Artist",
                    domain_pack=self.demo.recovery.domain_pack,
                )
                with self.assertRaisesRegex(OnlineSqlProposalError, code):
                    port.propose(
                        conversation_state=self.state,
                        slot_observations=self.complete_observations,
                    )

    def test_error_envelope_and_gateway_failure_are_bounded(self) -> None:
        declined = {
            "schema_version": "1.0.0",
            "task_type": "nl2sql",
            "status": "error",
            "data": None,
            "error": {
                "code": "INSUFFICIENT_CONTEXT",
                "message": "无法生成单表查询。",
                "retryable": False,
                "field_errors": [],
            },
            "warnings": [],
        }
        for gateway, code in (
            (FakeGateway(declined), "sql_proposal_declined"),
            (FailingGateway(), "sql_proposal_gateway_failed"),
        ):
            with self.subTest(code=code):
                port = OnlineSqlProposalPort(
                    gateway,
                    table_name="Artist",
                    domain_pack=self.demo.recovery.domain_pack,
                )
                with self.assertRaises(OnlineSqlProposalError) as caught:
                    port.propose(
                        conversation_state=self.state,
                        slot_observations=self.complete_observations,
                    )
                self.assertEqual(code, caught.exception.code)
                self.assertNotIn("TESTONLY", str(caught.exception))

    def _accepted_session(self):
        observations = build_slot_observations(self.scenario["initial_observations"])
        session = self.demo.recovery.start(
            conversation_state=self.state,
            slot_observations=observations,
            request_id="req.online.integration",
            recorded_at="2026-09-30T08:00:00Z",
        )
        question = session.pending_question
        self.assertIsNotNone(question)
        reply = self.scenario["structured_replies"][0]
        answer = build_clarification_answer(
            {
                "answer_id": "answer.online.integration.1",
                "question_id": question.question_id,
                "session_id": question.session_id,
                "request_id": question.request_id,
                "turn_id": question.turn_id,
                "state_revision": question.state_revision,
                "slot_id": question.slot_id,
                "values": [reply["value"]],
                "evidence_refs": [reply["evidence_ref"]],
            }
        )
        self.demo.recovery.resume(
            session, answer, recorded_at="2026-09-30T08:00:01Z"
        )
        self.assertEqual("accepted_for_safety_handoff", session.workflow_status)
        return session

    def test_safe_online_proposal_executes_only_after_d4_02_gate(self) -> None:
        session = self._accepted_session()
        port = OnlineSqlProposalPort(
            FakeGateway(_success_output()),
            table_name="Artist",
            domain_pack=self.demo.recovery.domain_pack,
        )
        result = self.demo.recovery.handoff_and_execute(session, proposal_port=port)
        self.assertEqual(275, result.rows[0][0])
        self.assertTrue(session.safety_approved)
        self.assertEqual(1, session.safety_gate_calls)
        self.assertEqual(1, session.local_sqlite_execution_attempts)

    def test_malicious_online_proposal_is_rejected_without_execution(self) -> None:
        session = self._accepted_session()
        port = OnlineSqlProposalPort(
            FakeGateway(_success_output("DELETE FROM Artist")),
            table_name="Artist",
            domain_pack=self.demo.recovery.domain_pack,
        )
        result = self.demo.recovery.handoff_and_execute(session, proposal_port=port)
        self.assertIsNone(result)
        self.assertFalse(session.safety_approved)
        self.assertEqual(1, session.safety_gate_calls)
        self.assertEqual(0, session.local_sqlite_execution_attempts)

    def test_slot_values_require_domain_pack_model_export_permission(self) -> None:
        observations = build_slot_observations(
            {
                "slot.metric": {
                    "values": ["count"],
                    "evidence_refs": ["slot.metric"],
                }
            }
        )
        port = OnlineSqlProposalPort(
            FakeGateway(_success_output()),
            table_name="Artist",
            domain_pack=self.demo.recovery.domain_pack,
        )
        with self.assertRaisesRegex(
            OnlineSqlProposalError, "slot_value_not_authorized_for_model"
        ):
            port.propose(
                conversation_state=self.state,
                slot_observations=observations,
            )


if __name__ == "__main__":
    unittest.main()
