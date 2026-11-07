"""D5 visitor ownership, replay, restart and D4 SQL adapter (offline fakes)."""

import json
from pathlib import Path
import sys
import tempfile
import unittest
from jsonschema import Draft202012Validator

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from backend.api.online_sql_service import OnlineSqlService
from backend.agent.clarification_recovery import SqlProposal
from backend.llm.errors import ProviderError, ProviderErrorCode
from backend.nl2sql.online_proposal import MODEL_ID, PROVIDER_ID, ProposalTelemetry
from backend.product.server import ProductRuntime
from backend.product.visitor_sql import VisitorSqlAdapter


def _request(request_id: str, **extra):
    return {"api_version": "3.0", "contract_version": "1.0.0", "request_id": request_id, **extra}


def _send(runtime, method, path, data=None, cookie=""):
    return runtime.dispatch(method, path, body=json.dumps(data).encode() if data is not None else b"{}",
                            cookie_header=cookie)


class _FakeProposal:
    def __init__(self):
        self.last_telemetry = None

    def propose(self, *, conversation_state, slot_observations):
        self.last_telemetry = ProposalTelemetry(
            request_id="request.fake", provider_id=PROVIDER_ID, returned_model=MODEL_ID,
            attempt_count=0, input_tokens=0, output_tokens=0, total_tokens=0,
            estimated_cost_cny=0, latency_ms=0, proposal_sha256="a" * 64,
        )
        return SqlProposal("proposal.fake", "SELECT COUNT(*) FROM Artist", ("Artist",), ())


class VisitorTests(unittest.TestCase):
    def test_query_and_clarification_limit_is_persistent_and_precedes_agent(self):
        class FailingAgent:
            calls = 0

            def submit(self, _request):
                self.calls += 1
                raise RuntimeError("offline_test_failure")

        with tempfile.TemporaryDirectory() as directory:
            agent = FailingAgent()
            runtime = ProductRuntime(directory, mode="online", master_key=b"a" * 32, agent_port=agent)
            created = _send(runtime, "POST", "/api/v3/sessions", _request("req.create"))
            sid = json.loads(created.body)["session_id"]
            cookie = created.headers["Set-Cookie"].split(";", 1)[0]
            query = _request("req.query", session_id=sid, turn_id=0, state_revision=0, question="统计")
            clarification = _request("req.query", answer_id="answer.1", session_id=sid,
                                     turn_id=0, state_revision=0, question_id="question.1",
                                     slot_id="slot.1", values=["Artist"], evidence_refs=["evidence.1"])
            for number in range(60):
                path, data = (("/api/v3/query", query) if number % 2 == 0
                              else ("/api/v3/clarifications", clarification))
                self.assertEqual(_send(runtime, "POST", path, data, cookie).status, 503)
            self.assertEqual(agent.calls, 60)
            reopened = ProductRuntime(directory, mode="online", master_key=b"a" * 32, agent_port=agent)
            limited = _send(reopened, "POST", "/api/v3/query", query, cookie)
            self.assertEqual(limited.status, 429)
            self.assertEqual(agent.calls, 60)
            self.assertEqual(json.loads(_send(reopened, "GET", "/api/v3/sessions/" + sid,
                                             cookie=cookie).body)["state_revision"], 0)

    def test_failed_outer_validation_rolls_back_sql_pending_and_allows_retry(self):
        class OneBadReply:
            def __init__(self):
                self.inner = OnlineSqlService(proposal_port=_FakeProposal())
                self._sessions = self.inner._sessions
                self._lock = self.inner._lock
                self.guard = self.inner.guard
                self.bad = True

            def submit(self, request):
                response = self.inner.submit(request)
                if request["operation"] == "submit_clarification" and self.bad:
                    self.bad = False
                    response["schema_version"] = "invalid"
                return response

        with tempfile.TemporaryDirectory() as directory:
            adapter = VisitorSqlAdapter(OneBadReply())
            runtime = ProductRuntime(directory, mode="online", master_key=b"a" * 32,
                                     agent_port=adapter)
            created = _send(runtime, "POST", "/api/v3/sessions", _request("req.create"))
            cookie = created.headers["Set-Cookie"].split(";", 1)[0]
            sid = json.loads(created.body)["session_id"]
            query = _request("req.query", session_id=sid, turn_id=0, state_revision=0,
                             question="统计艺术家数量")
            first = json.loads(_send(runtime, "POST", "/api/v3/query", query, cookie).body)
            self.assertEqual(first["status"], "clarification_required")
            pending = first["payload"]["pending_question"]
            choice = pending["options"][0]
            clarification = _request(pending["request_id"], answer_id="answer.1", session_id=sid,
                                     turn_id=pending["turn_id"], state_revision=pending["state_revision"],
                                     question_id=pending["question_id"], slot_id=pending["slot_id"],
                                     values=[choice["value"]], evidence_refs=[choice["evidence_ref"]])
            before = json.loads(json.dumps(adapter._pending[sid]))
            self.assertEqual(_send(runtime, "POST", "/api/v3/clarifications", clarification, cookie).status, 503)
            self.assertEqual(adapter._pending[sid], before)
            self.assertEqual(json.loads(_send(runtime, "GET", "/api/v3/sessions/" + sid,
                                             cookie=cookie).body)["state_revision"], 1)
            self.assertEqual(len(runtime.visitor_app.store.events(sid, cookie.split("=", 1)[1])), 1)
            retried = _send(runtime, "POST", "/api/v3/clarifications", clarification, cookie)
            self.assertEqual(retried.status, 200)
            self.assertEqual(json.loads(retried.body)["state_revision"], 2)

    def test_invalid_agent_payload_does_not_advance_session(self):
        class BadAgent:
            def submit(self, request):
                return {"schema_version": "2.0.0", "session_id": request["session_id"],
                        "request_id": request["request_id"], "status": "success", "route": "sql"}

        with tempfile.TemporaryDirectory() as directory:
            runtime = ProductRuntime(directory, mode="online", master_key=b"a" * 32, agent_port=BadAgent())
            created = _send(runtime, "POST", "/api/v3/sessions", _request("req.create"))
            sid = json.loads(created.body)["session_id"]
            cookie = created.headers["Set-Cookie"].split(";", 1)[0]
            query = _request("req.query", session_id=sid, turn_id=0, state_revision=0, question="x")
            self.assertEqual(_send(runtime, "POST", "/api/v3/query", query, cookie).status, 503)
            state = _send(runtime, "GET", "/api/v3/sessions/" + sid, cookie=cookie)
            self.assertEqual(json.loads(state.body)["state_revision"], 0)

    def test_public_session_and_event_envelopes_match_frozen_schema(self):
        schema = json.loads((Path(__file__).resolve().parents[2] / "configs" / "schemas" / "d5" /
                             "api-v3.schema.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            runtime = ProductRuntime(directory, master_key=b"a" * 32)
            created = _send(runtime, "POST", "/api/v3/sessions", _request("req.create"))
            sid = json.loads(created.body)["session_id"]
            cookie = created.headers["Set-Cookie"].split(";", 1)[0]
            event_reply = _send(runtime, "GET", "/api/v3/sessions/" + sid + "/events", cookie=cookie)
            for name, response in (("session_response", created), ("admin_summary_response", event_reply)):
                Draft202012Validator({**schema, "$ref": f"#/$defs/{name}"}).validate(json.loads(response.body))

    def test_ownership_cookie_restart_delete_and_no_token_storage(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = ProductRuntime(directory, master_key=b"a" * 32)
            created = _send(runtime, "POST", "/api/v3/sessions", _request("req.create"))
            self.assertEqual(created.status, 200)
            self.assertIn("HttpOnly", created.headers["Set-Cookie"])
            cookie = created.headers["Set-Cookie"].split(";", 1)[0]
            sid = json.loads(created.body)["session_id"]
            self.assertNotIn(cookie.split("=", 1)[1], (Path(directory) / "metadata.sqlite3").read_bytes().decode("latin1"))
            other = _send(runtime, "POST", "/api/v3/sessions", _request("req.other"))
            other_sid = json.loads(other.body)["session_id"]
            self.assertEqual(_send(runtime, "GET", "/api/v3/sessions/" + other_sid, cookie=cookie).status, 404)
            self.assertEqual(_send(runtime, "GET", "/api/v3/sessions/" + sid).status, 401)
            reopened = ProductRuntime(directory, master_key=b"a" * 32)
            self.assertEqual(_send(reopened, "GET", "/api/v3/sessions/" + sid, cookie=cookie).status, 200)
            self.assertEqual(_send(reopened, "DELETE", "/api/v3/sessions/" + sid, cookie=cookie).status, 200)
            self.assertEqual(_send(reopened, "GET", "/api/v3/sessions/" + sid, cookie=cookie).status, 401)

    def test_online_sql_adapter_uses_d4_gate_and_rejects_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            service = OnlineSqlService(proposal_port=_FakeProposal())
            runtime = ProductRuntime(directory, mode="online", master_key=b"a" * 32,
                                     agent_port=VisitorSqlAdapter(service))
            created = _send(runtime, "POST", "/api/v3/sessions", _request("req.create"))
            cookie = created.headers["Set-Cookie"].split(";", 1)[0]
            sid = json.loads(created.body)["session_id"]
            query = _request("req.query", session_id=sid, turn_id=0, state_revision=0,
                             question="统计艺术家数量")
            first = _send(runtime, "POST", "/api/v3/query", query, cookie)
            self.assertEqual(first.status, 200)
            body = json.loads(first.body)
            self.assertEqual(body["status"], "clarification_required")
            self.assertEqual(body["state_revision"], 1)
            self.assertEqual(_send(runtime, "POST", "/api/v3/query", query, cookie).status, 409)
            self.assertEqual(len(runtime.visitor_app.store.events(sid, cookie.split("=", 1)[1])), 1)
            pending = body["payload"]["pending_question"]
            choice = pending["options"][0]
            clarification = _request(pending["request_id"], answer_id="answer.1", session_id=sid,
                                     turn_id=pending["turn_id"], state_revision=pending["state_revision"],
                                     question_id=pending["question_id"], slot_id=pending["slot_id"],
                                     values=[choice["value"]], evidence_refs=[choice["evidence_ref"]])
            second = _send(runtime, "POST", "/api/v3/clarifications", clarification, cookie)
            self.assertEqual(second.status, 200)
            self.assertEqual(json.loads(second.body)["state_revision"], 2)
            self.assertEqual(_send(runtime, "POST", "/api/v3/clarifications", clarification, cookie).status, 409)

    def test_create_session_is_rate_limited_per_source(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = ProductRuntime(directory, master_key=b"a" * 32)
            for number in range(10):
                self.assertEqual(_send(runtime, "POST", "/api/v3/sessions", _request(f"req.{number}")).status, 200)
            self.assertEqual(_send(runtime, "POST", "/api/v3/sessions", _request("req.11")).status, 429)

    def test_invalid_schema_never_reaches_agent(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = ProductRuntime(directory, master_key=b"a" * 32)
            created = _send(runtime, "POST", "/api/v3/sessions", _request("req.create"))
            sid = json.loads(created.body)["session_id"]
            cookie = created.headers["Set-Cookie"].split(";", 1)[0]
            bad = _request("req.bad", session_id=sid, turn_id=True, state_revision=0, question="x")
            self.assertEqual(_send(runtime, "POST", "/api/v3/query", bad, cookie).status, 400)

    def test_provider_failures_surface_as_stable_error_codes(self):
        """断网/超时/429/预算/无效 JSON（模型侧）经 D5 visitor 落为稳定错误码。

        D4 OnlineSqlService 已把 ProviderError 映射为稳定错误码；本用例证明
        这些失败穿过 VisitorSqlAdapter → VisitorApplication 产品层后，对外仍是
        status=error + 冻结错误码 + retryable 语义，且原始 provider 细节不外泄。
        """
        cases = (
            (ProviderErrorCode.TIMEOUT, "provider_timeout", True),
            (ProviderErrorCode.NETWORK, "provider_unavailable", True),
            (ProviderErrorCode.RATE_LIMIT, "provider_rate_limited", True),
            (ProviderErrorCode.POLICY_LIMIT, "policy_limit", False),
            (ProviderErrorCode.INVALID_RESPONSE, "model_response_invalid", False),
        )

        class FailingProposalPort:
            def __init__(self, failure):
                self.failure = failure
                self.last_telemetry = None
                self.calls = 0

            def propose(self, *, conversation_state, slot_observations):
                self.calls += 1
                raise self.failure

        for provider_code, api_code, retryable in cases:
            with self.subTest(provider_code=provider_code):
                with tempfile.TemporaryDirectory() as directory:
                    service = OnlineSqlService(
                        proposal_port=FailingProposalPort(
                            ProviderError(provider_code, "TESTONLY-raw-secret", retryable=retryable)
                        )
                    )
                    runtime = ProductRuntime(directory, mode="online", master_key=b"a" * 32,
                                             agent_port=VisitorSqlAdapter(service))
                    created = _send(runtime, "POST", "/api/v3/sessions", _request("req.create"))
                    cookie = created.headers["Set-Cookie"].split(";", 1)[0]
                    sid = json.loads(created.body)["session_id"]
                    query = _request("req.query", session_id=sid, turn_id=0, state_revision=0,
                                     question="统计艺术家数量")
                    first = json.loads(_send(runtime, "POST", "/api/v3/query", query, cookie).body)
                    self.assertEqual("clarification_required", first["status"])
                    # 两轮澄清补齐 metric / entity_type，最终触发模型调用 → provider 失败。
                    for step, answer_id in enumerate(("answer.1", "answer.2"), start=1):
                        pending = first["payload"]["pending_question"]
                        option = pending["options"][0]
                        clarification = _request(
                            pending["request_id"], answer_id=answer_id, session_id=sid,
                            turn_id=pending["turn_id"], state_revision=pending["state_revision"],
                            question_id=pending["question_id"], slot_id=pending["slot_id"],
                            values=[option["value"]], evidence_refs=[option["evidence_ref"]],
                        )
                        reply = _send(runtime, "POST", "/api/v3/clarifications", clarification, cookie)
                        self.assertEqual(200, reply.status)
                        first = json.loads(reply.body)
                    self.assertEqual("error", first["status"])
                    self.assertEqual(api_code, first["error"]["code"])
                    self.assertEqual(retryable, first["error"]["retryable"])
                    self.assertNotIn("TESTONLY", json.dumps(first, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
