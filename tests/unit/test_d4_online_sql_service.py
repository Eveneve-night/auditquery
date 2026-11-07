"""D4-07-01 B online SQL session/API orchestration tests."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

from backend.agent.clarification_recovery import SqlProposal  # noqa: E402
from backend.api.online_sql_service import (  # noqa: E402
    InMemoryDiagnosticSink,
    OnlineSqlApplication,
    OnlineSqlService,
)
from backend.llm.errors import ProviderError, ProviderErrorCode  # noqa: E402
from backend.nl2sql.online_proposal import (  # noqa: E402
    MODEL_ID,
    PROVIDER_ID,
    ProposalTelemetry,
)
from tools.demo.run_d4_online_sql import (  # noqa: E402
    LOOPBACK_HOST,
    OnlineServerSafetyError,
    _build_server,
    main as online_server_main,
)


class FakeProposalPort:
    def __init__(
        self,
        sql: str = "SELECT COUNT(*) AS artist_count FROM Artist",
        tables: tuple[str, ...] = ("Artist",),
    ) -> None:
        self.sql = sql
        self.tables = tables
        self.calls = []
        self.failure: ProviderError | None = None
        self.last_telemetry = None

    def propose(self, *, conversation_state, slot_observations):
        self.calls.append((deepcopy(conversation_state), deepcopy(slot_observations)))
        self.last_telemetry = None
        if self.failure is not None:
            raise self.failure
        proposal = SqlProposal(
            proposal_id="proposal.test-online-sql",
            sql=self.sql,
            tables=self.tables,
            params=(),
        )
        self.last_telemetry = ProposalTelemetry(
            request_id="request.test.nl2sql",
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


def query_request(
    session_id: str = "session.online-001",
    query: str = "统计艺术家数量",
) -> dict:
    return {
        "schema_version": "2.0.0",
        "operation": "submit_query",
        "session_id": session_id,
        "request_id": "request.online-001",
        "turn_id": 0,
        "state_revision": 0,
        "query": query,
    }


def answer_request(response: dict, value: str, answer_id: str) -> dict:
    pending = response["pending_question"]
    option = next(item for item in pending["options"] if item["value"] == value)
    return {
        "schema_version": "2.0.0",
        "operation": "submit_clarification",
        "answer_id": answer_id,
        "session_id": pending["session_id"],
        "request_id": pending["request_id"],
        "turn_id": pending["turn_id"],
        "state_revision": pending["state_revision"],
        "question_id": pending["question_id"],
        "slot_id": pending["slot_id"],
        "values": [option["value"]],
        "evidence_refs": [option["evidence_ref"]],
    }


class OnlineSqlServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.port = FakeProposalPort()
        self.service = OnlineSqlService(
            proposal_port=self.port,
            table_name="Artist",
            now=lambda: "2026-10-01T08:00:00Z",
        )

    def complete(self) -> tuple[dict, dict, dict]:
        first = self.service.submit(query_request())
        second = self.service.submit(
            answer_request(first, "count", "answer.online-001.1")
        )
        third = self.service.submit(
            answer_request(second, "artist", "answer.online-001.2")
        )
        return first, second, third

    def test_query_clarification_and_safe_execution_form_one_valid_flow(self) -> None:
        first, second, result = self.complete()
        self.assertEqual("clarification_required", first["status"])
        self.assertEqual("slot.metric", first["pending_question"]["slot_id"])
        self.assertGreater(len(first["pending_question"]["options"]), 1)
        self.assertEqual(
            1,
            len(
                {
                    item["evidence_ref"]
                    for item in first["pending_question"]["options"]
                }
            ),
        )
        self.assertEqual("slot.entity_type", second["pending_question"]["slot_id"])
        self.assertEqual("success", result["status"])
        self.assertEqual("approved", result["sql"]["safety_status"])
        self.assertEqual([[275]], result["sql"]["rows"])
        self.assertEqual(1, result["sql"]["execution_attempts"])
        self.assertEqual(1, result["usage"]["model_calls"])
        self.assertEqual(100, result["usage"]["input_tokens"])
        self.assertEqual(result, self.service.state("session.online-001"))
        self.assertEqual(1, self.service.health()["model_calls"])
        self.assertEqual(1, len(self.port.calls))
        sent_state, sent_slots = self.port.calls[0]
        self.assertEqual("统计艺术家数量", sent_state["current_request"]["query"])
        self.assertEqual({"slot.metric", "slot.entity_type"}, set(sent_slots))

    def test_track_and_media_type_aliases_reach_existing_safe_execution_chain(self) -> None:
        cases = (
            ("track", "统计曲目数量", "Track", 3503),
            ("media_type", "统计媒体类型数量", "MediaType", 5),
        )
        for entity, query, table, expected_count in cases:
            with self.subTest(entity=entity):
                port = FakeProposalPort(
                    f"SELECT COUNT(*) AS item_count FROM {table}",
                    (table,),
                )
                service = OnlineSqlService(
                    proposal_port=port,
                    table_name=table,
                    now=lambda: "2026-10-01T08:00:00Z",
                )
                session_id = f"session.alias-{entity}"
                first = service.submit(query_request(session_id, query))
                second = service.submit(
                    answer_request(first, "count", f"answer.alias-{entity}.1")
                )
                result = service.submit(
                    answer_request(second, entity, f"answer.alias-{entity}.2")
                )

                self.assertEqual(0.85, first["decision"]["raw_score"])
                self.assertEqual("success", result["status"])
                self.assertEqual("accept", result["decision"]["action"])
                self.assertEqual([[expected_count]], result["sql"]["rows"])
                self.assertEqual("approved", result["sql"]["safety_status"])
                self.assertEqual(1, result["sql"]["execution_attempts"])
                self.assertEqual(1, len(port.calls))

    def test_loop_guard_stop_is_publicly_refused_and_escalated(self) -> None:
        for entity, query in (
            ("customer", "统计客户数量"),
            ("invoice", "统计发票数量"),
        ):
            with self.subTest(entity=entity):
                port = FakeProposalPort()
                service = OnlineSqlService(
                    proposal_port=port,
                    table_name="Artist",
                    now=lambda: "2026-10-01T08:00:00Z",
                )
                session_id = f"session.no-progress-{entity}"
                first = service.submit(query_request(session_id, query))
                second = service.submit(
                    answer_request(first, "count", f"answer.no-progress-{entity}.1")
                )
                final_answer = answer_request(
                    second, entity, f"answer.no-progress-{entity}.2"
                )
                result = service.submit(final_answer)

                self.assertEqual("refused", result["status"])
                self.assertEqual("escalate", result["decision"]["action"])
                self.assertEqual(
                    "specialized_attempt_limit",
                    result["decision"]["reason_code"],
                )
                self.assertIsNone(result["pending_question"])
                self.assertIsNone(result["error"])
                self.assertIsNone(result["sql"])
                self.assertEqual([], port.calls)
                self.assertEqual(0, result["usage"]["model_calls"])
                self.assertEqual("workflow_stopped", result["trace"][-1]["stage"])
                self.assertEqual("escalate", result["trace"][-1]["action"])
                with self.assertRaisesRegex(
                    Exception, "clarification_answer_replayed"
                ):
                    service.submit(final_answer)

    def test_clarification_no_progress_stop_has_consistent_public_contract(self) -> None:
        service = OnlineSqlService(
            proposal_port=FakeProposalPort(),
            table_name="Artist",
            now=lambda: "2026-10-01T08:00:00Z",
        )
        first = service.submit(
            query_request("session.no-progress-contract", "统计美国客户数量")
        )
        session = deepcopy(
            service._sessions["session.no-progress-contract"].recovery
        )
        service.recovery._stop(session, "clarification_no_progress")
        binding = service._binding_for(
            session,
            fallback_turn=first["turn_id"],
            fallback_revision=first["state_revision"],
        )

        response = service._response_for(
            session,
            binding=binding,
            elapsed_ms=0.0,
        )
        service._validate_response(response, session)

        self.assertEqual("refused", response["status"])
        self.assertEqual("escalate", response["decision"]["action"])
        self.assertEqual(
            "clarification_no_progress", response["decision"]["reason_code"]
        )
        self.assertIsNone(response["pending_question"])
        self.assertIsNone(response["error"])
        self.assertIsNone(response["sql"])

    def test_write_proposal_is_refused_without_database_execution(self) -> None:
        self.port.sql = "DELETE FROM Artist"
        _, _, result = self.complete()
        self.assertEqual("refused", result["status"])
        self.assertEqual("reject", result["decision"]["action"])
        self.assertEqual("rejected", result["sql"]["safety_status"])
        self.assertEqual(0, result["sql"]["execution_attempts"])
        self.assertEqual([], result["sql"]["rows"])
        self.assertNotIn(
            "sql_execution", [item["stage"] for item in result["trace"]]
        )

    def test_rate_limit_is_safe_and_same_answer_can_retry(self) -> None:
        first = self.service.submit(query_request())
        second = self.service.submit(
            answer_request(first, "count", "answer.online-001.1")
        )
        final_answer = answer_request(second, "artist", "answer.online-001.2")
        self.port.failure = ProviderError(
            ProviderErrorCode.RATE_LIMIT,
            "TESTONLY raw provider details must not escape",
            retryable=True,
        )
        failed = self.service.submit(final_answer)
        self.assertEqual("error", failed["status"])
        self.assertEqual("provider_rate_limited", failed["error"]["code"])
        self.assertTrue(failed["error"]["retryable"])
        self.assertNotIn("TESTONLY", json.dumps(failed, ensure_ascii=False))
        self.port.failure = None
        retried = self.service.submit(final_answer)
        self.assertEqual("success", retried["status"])

    def test_provider_failures_have_stable_content_safe_envelopes(self) -> None:
        cases = (
            (ProviderErrorCode.TIMEOUT, "provider_timeout", True, 1),
            (ProviderErrorCode.NETWORK, "provider_unavailable", True, 1),
            (ProviderErrorCode.POLICY_LIMIT, "policy_limit", False, 0),
            (ProviderErrorCode.INVALID_RESPONSE, "model_response_invalid", False, 1),
        )
        for index, (provider_code, api_code, retryable, model_calls) in enumerate(
            cases, start=1
        ):
            with self.subTest(provider_code=provider_code):
                port = FakeProposalPort()
                service = OnlineSqlService(
                    proposal_port=port,
                    table_name="Artist",
                    now=lambda: "2026-10-01T08:00:00Z",
                )
                session_id = f"session.failure-{index}"
                request = query_request(session_id)
                request["request_id"] = f"request.failure-{index}"
                first = service.submit(request)
                second = service.submit(
                    answer_request(first, "count", f"answer.failure-{index}.1")
                )
                final_answer = answer_request(
                    second, "artist", f"answer.failure-{index}.2"
                )
                port.failure = ProviderError(
                    provider_code,
                    "TESTONLY raw provider response and secret marker",
                    retryable=retryable,
                )
                result = service.submit(final_answer)
                self.assertEqual(api_code, result["error"]["code"])
                self.assertEqual(retryable, result["error"]["retryable"])
                self.assertEqual(model_calls, result["usage"]["model_calls"])
                self.assertNotIn("TESTONLY", json.dumps(result, ensure_ascii=False))

    def test_unknown_query_failure_emits_only_safe_diagnostic_fields(self) -> None:
        marker = "TESTONLY private question, SQL, row, key, and exception detail"
        with patch.object(
            self.service.recovery,
            "start",
            side_effect=RuntimeError(marker),
        ):
            result = self.service.submit(query_request())

        self.assertEqual("online_sql_runtime_failed", result["error"]["code"])
        self.assertFalse(result["error"]["retryable"])
        self.assertNotIn(marker, json.dumps(result, ensure_ascii=False))
        self.assertNotIn("diagnostic", json.dumps(result, ensure_ascii=False))
        self.assertIsInstance(self.service.diagnostic_sink, InMemoryDiagnosticSink)
        events = self.service.diagnostic_sink.snapshot()
        self.assertEqual(1, len(events))
        event = events[0]
        self.assertEqual("online_sql.unexpected_exception.v1", event.event_id)
        self.assertEqual("online_sql_service", event.component)
        self.assertEqual("submit_query", event.operation)
        self.assertEqual("RuntimeError", event.exception_class)
        self.assertIsNone(event.error_code)
        self.assertEqual(
            {
                "event_id",
                "component",
                "operation",
                "exception_class",
                "error_code",
                "session_id_digest",
                "request_id_digest",
            },
            set(event.__dict__),
        )
        self.assertRegex(event.session_id_digest, r"^[0-9a-f]{64}$")
        self.assertRegex(event.request_id_digest, r"^[0-9a-f]{64}$")
        self.assertNotIn("session.online-001", repr(event))
        self.assertNotIn("request.online-001", repr(event))
        self.assertNotIn(marker, repr(event))

    def test_unknown_clarification_failure_is_observable_and_retryable(self) -> None:
        first = self.service.submit(query_request())
        second = self.service.submit(
            answer_request(first, "count", "answer.online-001.1")
        )
        final_answer = answer_request(second, "artist", "answer.online-001.2")
        marker = "TESTONLY private provider body must not enter diagnostics"
        with patch.object(
            self.service.recovery,
            "handoff_and_execute",
            side_effect=LookupError(marker),
        ):
            failed = self.service.submit(final_answer)

        self.assertEqual("online_sql_runtime_failed", failed["error"]["code"])
        event = self.service.diagnostic_sink.snapshot()[-1]
        self.assertEqual("submit_clarification", event.operation)
        self.assertEqual("LookupError", event.exception_class)
        self.assertNotIn(marker, repr(event))
        retried = self.service.submit(final_answer)
        self.assertEqual("success", retried["status"])

    def test_known_recovery_error_records_domain_code_not_exception_text(self) -> None:
        first = self.service.submit(query_request())
        second = self.service.submit(
            answer_request(first, "count", "answer.online-001.1")
        )
        final_answer = answer_request(second, "artist", "answer.online-001.2")
        marker = "TESTONLY sensitive provider response"
        self.port.failure = ProviderError(
            ProviderErrorCode.RATE_LIMIT,
            marker,
            retryable=True,
        )
        failed = self.service.submit(final_answer)

        self.assertEqual("provider_rate_limited", failed["error"]["code"])
        event = self.service.diagnostic_sink.snapshot()[-1]
        self.assertEqual("online_sql.domain_error.v1", event.event_id)
        self.assertEqual("ClarificationRecoveryError", event.exception_class)
        self.assertEqual("sql_proposal_failed", event.error_code)
        self.assertNotIn(marker, repr(event))

    def test_diagnostic_sink_failure_keeps_original_public_error(self) -> None:
        marker = "TESTONLY sink failure detail"

        class FailingSink:
            def emit(self, _event) -> None:
                raise RuntimeError(marker)

        service = OnlineSqlService(
            proposal_port=FakeProposalPort(),
            table_name="Artist",
            now=lambda: "2026-10-01T08:00:00Z",
            diagnostic_sink=FailingSink(),
        )
        with patch.object(
            service.recovery,
            "start",
            side_effect=LookupError("TESTONLY original exception detail"),
        ):
            result = service.submit(query_request())

        self.assertEqual("online_sql_runtime_failed", result["error"]["code"])
        self.assertNotIn(marker, json.dumps(result, ensure_ascii=False))

    def test_replay_and_cross_session_answer_fail_closed(self) -> None:
        first = self.service.submit(query_request())
        answer = answer_request(first, "count", "answer.online-001.1")
        self.service.submit(answer)
        with self.assertRaisesRegex(Exception, "clarification_answer_replayed"):
            self.service.submit(answer)

        other = query_request("session.online-002")
        other["request_id"] = "request.online-002"
        other_response = self.service.submit(other)
        tampered = answer_request(
            other_response, "count", "answer.online-002.cross-session"
        )
        tampered["request_id"] = "request.online-001"
        with self.assertRaisesRegex(Exception, "clarification_request_mismatch"):
            self.service.submit(tampered)


class OnlineSqlApplicationTest(unittest.TestCase):
    def setUp(self) -> None:
        service = OnlineSqlService(
            proposal_port=FakeProposalPort(),
            table_name="Artist",
            now=lambda: "2026-10-01T08:00:00Z",
        )
        self.app = OnlineSqlApplication(service)

    @staticmethod
    def payload(response) -> dict:
        return json.loads(response.body.decode("utf-8"))

    def test_health_query_and_state_endpoints(self) -> None:
        health = self.app.handle("GET", "/api/v2/health")
        self.assertEqual(200, health.status)
        self.assertEqual(MODEL_ID, self.payload(health)["model"])
        request = query_request()
        response = self.app.handle(
            "POST",
            "/api/v2/query",
            content_type="application/json; charset=utf-8",
            body=json.dumps(request, ensure_ascii=False).encode("utf-8"),
        )
        self.assertEqual(200, response.status)
        state = self.app.handle("GET", "/api/v2/sessions/session.online-001")
        self.assertEqual(self.payload(response), self.payload(state))

    def test_transport_errors_are_bounded_and_do_not_echo_input(self) -> None:
        marker = "DO_NOT_ECHO_PRIVATE_QUERY"
        response = self.app.handle(
            "POST",
            "/api/v2/query",
            content_type="text/plain",
            body=marker.encode("utf-8"),
        )
        self.assertEqual(415, response.status)
        self.assertNotIn(marker.encode("utf-8"), response.body)
        mismatch = query_request()
        mismatch["operation"] = "submit_clarification"
        response = self.app.handle(
            "POST",
            "/api/v2/query",
            content_type="application/json",
            body=json.dumps(mismatch).encode("utf-8"),
        )
        self.assertEqual(400, response.status)
        self.assertEqual("operation_route_mismatch", self.payload(response)["error"]["code"])

    def test_server_is_loopback_only(self) -> None:
        server = _build_server(LOOPBACK_HOST, 0, self.app)
        try:
            self.assertEqual(LOOPBACK_HOST, server.server_address[0])
        finally:
            server.server_close()
        for host in ("0.0.0.0", "::1"):
            with self.subTest(host=host):
                with self.assertRaisesRegex(ValueError, "IPv4 loopback"):
                    _build_server(host, 0, self.app)

    def test_wrong_online_confirmation_stops_before_provider_loading(self) -> None:
        with patch("tools.demo.run_d4_online_sql._load_gateway") as load_gateway:
            with self.assertRaisesRegex(
                OnlineServerSafetyError, "confirmation phrase"
            ):
                online_server_main(["--confirm-real-api", "not-authorized"])
        load_gateway.assert_not_called()


if __name__ == "__main__":
    unittest.main()
