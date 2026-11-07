"""Offline regressions for the submitted local source package (no real API/key)."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from backend.api.online_sql_service import OnlineSqlService
from backend.api.online_contracts import OnlineContractValidator, OnlineContractError
from backend.api.online_rag_service import OnlineRagService
from backend.agent.domain_pack import DomainPackRegistry
from backend.agent.typed_decision_runtime import CandidateRetriever
from backend.llm.contracts import ModelCallResult, ModelUsage
from backend.llm.errors import ProviderError, ProviderErrorCode
from backend.llm.usage import UsageLedger
from backend.product.auth import AuthService, SqliteAuthStore
from backend.product.visitor import VisitorStore, VisitorApplication, VisitorError
from backend.product.visitor_sql import VisitorSqlAdapter, build_approved_sql_adapter
from backend.rag.published_retriever import PublishedDocumentIndex
from backend.rag.published_retriever import _matching_snippet


class FakeGateway:
    def __init__(self, mode="success"):
        self.mode = mode
        self.requests = []
        self.usage_ledger = UsageLedger()

    def generate_structured(self, request):
        self.requests.append(request)
        if self.mode in {"retry_success", "retry_failure"}:
            error = ProviderError(ProviderErrorCode.TIMEOUT, "PRIVATE_PROVIDER_BODY",
                                  retryable=True, input_tokens=17, output_tokens=3,
                                  total_tokens=20, estimated_cost_cny=0.001)
            self.usage_ledger.record_failure(
                request_id=request.request_id, task_type=request.task_type,
                provider_id="bailian-cn-beijing", requested_model="qwen3.7-flash",
                error=error, latency_ms=2)
        if self.mode in {"provider_error", "retry_failure"}:
            error = ProviderError(ProviderErrorCode.TIMEOUT, "PRIVATE_PROVIDER_BODY",
                                  retryable=True, input_tokens=17, output_tokens=3,
                                  total_tokens=20, estimated_cost_cny=0.001)
            self.usage_ledger.record_failure(
                request_id=request.request_id, task_type=request.task_type,
                provider_id="bailian-cn-beijing", requested_model="qwen3.7-flash",
                error=error, latency_ms=2)
            raise error
        payload = json.loads(request.messages[-1].content)
        table = payload["database_schema"]["table"]
        output = {"schema_version": "1.0.0", "task_type": "nl2sql", "status": "success",
                  "data": {"outcome": "sql", "sql": f"SELECT COUNT(*) FROM {table}",
                           "tables": [table], "params": [], "rationale": "单表计数。"},
                  "error": None, "warnings": []}
        if self.mode == "decline":
            output.update(status="error", data=None,
                          error={"code": "INSUFFICIENT_CONTEXT", "message": "拒绝。",
                                 "retryable": False, "field_errors": []})
        elif self.mode == "invalid":
            output = {"PRIVATE_PROVIDER_BODY": "not a task envelope"}
        elif self.mode == "write":
            output["data"]["sql"] = f"DELETE FROM {table}"
        elif self.mode == "wrong_table":
            output["data"].update(sql="SELECT COUNT(*) FROM Artist", tables=["Artist"])
        result = ModelCallResult(request.request_id, "nl2sql", "bailian-cn-beijing",
                                 "qwen3.7-flash", "qwen3.7-flash", None, output,
                                 ModelUsage(100, 20, 120), 0.002, 3, "stop")
        self.usage_ledger.record_success(result)
        return result


def request(question, sid="session.local"):
    return {"schema_version": "2.0.0", "operation": "submit_query",
            "session_id": sid, "request_id": "request.local", "turn_id": 0,
            "state_revision": 0, "query": question}


def service(gateway):
    return OnlineSqlService(gateway, table_name=None, infer_count_slots=True)


class SqlRepairTests(unittest.TestCase):
    def test_three_counts_and_selected_schema_without_repeated_clarification(self):
        db = ROOT / "data/raw/chinook-v1.4.5/Chinook_Sqlite.sqlite"
        before = hashlib.sha256(db.read_bytes()).hexdigest()
        for table, query, expected, column in (
            ("Artist", "数据库里一共有多少位艺术家？", 275, "ArtistId"),
            ("Album", "统计专辑数量", 347, "AlbumId"),
            ("Track", "统计曲目数量", 3503, "TrackId"),
            ("Album", "How many albums are there?", 347, "AlbumId"),
            ("Track", "How many tracks are there?", 3503, "TrackId"),
            ("Track", "Track 曲目数量", 3503, "TrackId"),
        ):
            with self.subTest(table=table, query=query):
                gateway = FakeGateway()
                result = service(gateway).submit(request(query))
                self.assertEqual("success", result["status"])
                self.assertIsNone(result["pending_question"])
                self.assertEqual([[expected]], result["sql"]["rows"])
                self.assertEqual("approved", result["sql"]["safety_status"])
                self.assertEqual(1, result["sql"]["execution_attempts"])
                self.assertEqual(1, len(gateway.requests))
                schema = json.loads(gateway.requests[0].messages[-1].content)["database_schema"]
                self.assertEqual(table, schema["table"])
                self.assertIn(column, schema["columns"])
        self.assertEqual(before, hashlib.sha256(db.read_bytes()).hexdigest())

    def test_distinct_entities_and_non_count_metrics_still_need_clarification(self):
        for query in ("艺术家和专辑数量", "平均曲目单价是多少？", "统计美国客户数量"):
            with self.subTest(query=query):
                gateway = FakeGateway()
                result = service(gateway).submit(request(query))
                self.assertEqual("clarification_required", result["status"])
                self.assertEqual([], gateway.requests)

    def test_declined_invalid_and_provider_failed_calls_keep_usage(self):
        for mode, code, tokens, cost in (
            ("decline", "sql_proposal_declined", 100, 0.002),
            ("invalid", "model_response_invalid", 100, 0.002),
            ("provider_error", "provider_timeout", 17, 0.001),
        ):
            with self.subTest(mode=mode):
                gateway = FakeGateway(mode)
                api = service(gateway)
                failed = api.submit(request("统计专辑数量"))
                self.assertEqual(code, failed["error"]["code"])
                self.assertEqual(1, failed["usage"]["model_calls"])
                self.assertEqual(tokens, failed["usage"]["input_tokens"])
                self.assertEqual(cost, failed["usage"]["estimated_cost_cny"])
                self.assertEqual(tokens, api.health()["input_tokens"])
                self.assertNotIn("PRIVATE_PROVIDER_BODY", json.dumps(failed))
                gateway.mode = "success"
                retried = api.submit(request("统计专辑数量"))
                self.assertEqual("success", retried["status"])
                self.assertEqual(2, api.health()["model_calls"])

    def test_write_and_wrong_table_remain_blocked(self):
        for mode, expected in (("write", "refused"), ("wrong_table", "error")):
            with self.subTest(mode=mode):
                result = service(FakeGateway(mode)).submit(request("统计专辑数量"))
                self.assertEqual(expected, result["status"])
                if mode == "write":
                    self.assertEqual(0, result["sql"]["execution_attempts"])
                    self.assertEqual("rejected", result["sql"]["safety_status"])
                else:
                    self.assertEqual("sql_table_mismatch", result["error"]["code"])

    def test_mixed_language_alias_tie_is_not_distinct_entity_conflict(self):
        result = service(FakeGateway()).submit(request("统计 Track 曲目数量"))
        self.assertEqual("success", result["status"])

    def test_nested_and_ascii_aliases_do_not_supply_other_entity_alias_evidence(self):
        retriever = CandidateRetriever(DomainPackRegistry().load("chinook_music_store", "1.0.0"))
        for query in ("统计播放列表曲目数量", "PlaylistTrack count"):
            with self.subTest(query=query):
                candidates = retriever.retrieve(query, top_k=50, candidate_kind="concept")
                ids = {item.candidate_id for item in candidates}
                self.assertIn("concept.playlisttrack", ids)
                self.assertNotIn("concept.track", ids)
                playlist = next((item for item in candidates if item.candidate_id == "concept.playlist"), None)
                if playlist is not None:
                    self.assertEqual(0, playlist.raw_features["alias_hit_count"])
                    self.assertLess(playlist.raw_score, candidates[0].raw_score)

    def test_stopped_internal_clarify_is_returned_as_refused_escalate(self):
        api = OnlineSqlService(FakeGateway(), table_name=None)
        result = api.submit(request("统计客户数量"))
        for value in ("count", "customer"):
            pending = result["pending_question"]
            option = next(item for item in pending["options"] if item["value"] == value)
            result = api.submit({"schema_version": "2.0.0", "operation": "submit_clarification",
                                 "answer_id": "answer.stop." + value,
                                 **{key: pending[key] for key in ("session_id", "request_id", "turn_id",
                                                                 "state_revision", "question_id", "slot_id")},
                                 "values": [value], "evidence_refs": [option["evidence_ref"]]})
        self.assertEqual("refused", result["status"])
        self.assertEqual("escalate", result["decision"]["action"])

    def test_retry_attempt_usage_is_summed_on_success_and_failure(self):
        for mode, input_tokens, cost in (("retry_success", 117, 0.003),
                                         ("retry_failure", 34, 0.002)):
            with self.subTest(mode=mode):
                api = service(FakeGateway(mode))
                result = api.submit(request("统计专辑数量"))
                self.assertEqual(2, result["usage"]["model_calls"])
                self.assertEqual(input_tokens, result["usage"]["input_tokens"])
                self.assertAlmostEqual(cost, result["usage"]["estimated_cost_cny"])
                self.assertEqual(2, api.health()["model_calls"])

    def test_preflight_failure_with_empty_ledger_does_not_invent_model_call(self):
        class PreflightGateway:
            usage_ledger = UsageLedger()

            def generate_structured(self, _request):
                raise ProviderError(ProviderErrorCode.AUTHENTICATION,
                                    "PRIVATE_PROVIDER_BODY", retryable=False)

        api = service(PreflightGateway())
        result = api.submit(request("统计专辑数量"))
        self.assertEqual("error", result["status"])
        self.assertEqual(0, result["usage"]["model_calls"])
        self.assertEqual(0, api.health()["model_calls"])
        self.assertNotIn("PRIVATE_PROVIDER_BODY", json.dumps(result))

    def test_recorded_configuration_failure_is_not_a_model_call(self):
        class PreflightGateway:
            usage_ledger = UsageLedger()

            def generate_structured(inner, req):
                error = ProviderError(ProviderErrorCode.CONFIGURATION,
                                      "PRIVATE_PROVIDER_BODY", retryable=False)
                inner.usage_ledger.record_failure(
                    request_id=req.request_id, task_type=req.task_type,
                    provider_id="bailian-cn-beijing", requested_model="qwen3.7-flash",
                    error=error, latency_ms=1)
                raise error

        result = service(PreflightGateway()).submit(request("统计专辑数量"))
        self.assertEqual("error", result["status"])
        self.assertEqual(0, result["usage"]["model_calls"])
        self.assertEqual(0, result["usage"]["estimated_cost_cny"])

    def test_failed_clarification_preserves_retry_and_usage(self):
        gateway = FakeGateway("decline")
        api = OnlineSqlService(gateway, table_name=None)
        result = api.submit(request("统计专辑数量"))
        for value in ("count", "album"):
            pending = result["pending_question"]
            option = next(item for item in pending["options"] if item["value"] == value)
            answer = {"schema_version": "2.0.0", "operation": "submit_clarification",
                      "answer_id": "answer." + value,
                      **{key: pending[key] for key in ("session_id", "request_id", "turn_id",
                                                      "state_revision", "question_id", "slot_id")},
                      "values": [value], "evidence_refs": [option["evidence_ref"]]}
            result = api.submit(answer)
        self.assertEqual("sql_proposal_declined", result["error"]["code"])
        self.assertEqual(100, result["usage"]["input_tokens"])
        gateway.mode = "success"
        retried = api.submit(answer)
        self.assertEqual([[347]], retried["sql"]["rows"])
        self.assertEqual(2, api.health()["model_calls"])
        with self.assertRaisesRegex(Exception, "clarification_answer_replayed"):
            api.submit(answer)

    def test_product_adapter_does_not_reuse_previous_table(self):
        gateway = FakeGateway()
        adapter = VisitorSqlAdapter(service(gateway))
        for revision, question, expected in ((0, "统计艺术家数量", 275),
                                              (1, "统计专辑数量", 347),
                                              (2, "统计曲目数量", 3503)):
            result = adapter.submit({"session_id": "guest.local", "request_id": f"request.{revision}",
                                     "turn_id": revision, "state_revision": revision, "question": question})
            self.assertEqual([[expected]], result["sql"]["rows"])
            self.assertEqual(revision + 1, result["state_revision"])

    def test_default_product_builder_selects_album_schema(self):
        gateway = FakeGateway()
        with patch("backend.product.visitor_sql.BailianProvider"), patch(
            "backend.product.visitor_sql.LLMGateway", return_value=gateway
        ):
            adapter = build_approved_sql_adapter(None)
        result = adapter.submit({"session_id": "guest.builder", "request_id": "request.builder",
                                 "turn_id": 0, "state_revision": 0, "question": "统计专辑数量"})
        self.assertEqual([[347]], result["sql"]["rows"])


class RateLimitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.auth = AuthService(SqliteAuthStore(Path(self.tmp.name) / "metadata.sqlite3"))
        self.now = datetime(2026, 10, 18, tzinfo=timezone.utc)

    def tearDown(self):
        self.tmp.cleanup()

    def test_default_60_then_429_and_exact_five_minute_reset(self):
        store = VisitorStore(self.auth)
        with patch("backend.product.visitor._now", return_value=self.now):
            for _ in range(60):
                store.reserve_operation("session.a", "source.a")
            with self.assertRaises(VisitorError) as caught:
                store.reserve_operation("session.a", "source.a")
            self.assertEqual(429, caught.exception.http_status)
        with patch("backend.product.visitor._now", return_value=self.now + timedelta(seconds=299)):
            with self.assertRaises(VisitorError):
                store.reserve_operation("session.a", "source.a")
        with patch("backend.product.visitor._now", return_value=self.now + timedelta(seconds=300)):
            store.reserve_operation("session.a", "source.a")

    def test_configured_limit_and_window_and_cross_session_source_cap(self):
        with patch.dict("os.environ", {"ICT_QA_RATE_LIMIT": "2", "ICT_QA_RATE_WINDOW_SECONDS": "10"}):
            store = VisitorStore(self.auth)
        with patch("backend.product.visitor._now", return_value=self.now):
            store.reserve_operation("session.a", "shared")
            store.reserve_operation("session.b", "shared")
            with self.assertRaises(VisitorError):
                store.reserve_operation("session.c", "shared")
            store.reserve_operation("session.c", "other")
        with patch("backend.product.visitor._now", return_value=self.now + timedelta(seconds=10)):
            store.reserve_operation("session.c", "shared")

    def test_invalid_configuration_fails_at_startup(self):
        for value in ("0", "-1", "abc", "1.5"):
            with self.subTest(value=value), patch.dict("os.environ", {"ICT_QA_RATE_LIMIT": value}):
                with self.assertRaises(ValueError):
                    VisitorStore(self.auth)

    def test_http_61st_request_returns_429_without_agent_or_revision_mutation(self):
        class Agent:
            calls = 0
            def submit(inner, data):
                inner.calls += 1
                return OnlineRagService(None)._response(data, [], elapsed_ms=0)
        agent = Agent()
        store = VisitorStore(self.auth)
        app = VisitorApplication(store, agent)
        with patch("backend.product.visitor._now", return_value=self.now):
            sid, token, _ = store.create("source.http")
            for revision in range(60):
                data = {"api_version": "3.0", "contract_version": "1.0.0",
                        "session_id": sid, "request_id": f"request.http.{revision}",
                        "turn_id": revision, "state_revision": revision, "question": "问题"}
                response = app.handle("POST", "/api/v3/query", request_id=data["request_id"],
                                      body=json.dumps(data).encode(), token=token, source="source.http")
                self.assertEqual(200, response.status)
            data.update(request_id="request.http.60", turn_id=60, state_revision=60)
            response = app.handle("POST", "/api/v3/query", request_id=data["request_id"],
                                  body=json.dumps(data).encode(), token=token, source="source.http")
            self.assertEqual(429, response.status)
            self.assertEqual("rate_limited", json.loads(response.body)["error"]["code"])
            self.assertEqual(60, agent.calls)
            self.assertEqual(60, store.owned(sid, token)["state_revision"])

    def test_failed_downstream_calls_still_consume_quota_and_restart_retains_it(self):
        class FailedAgent:
            def submit(self, _data):
                raise RuntimeError("PRIVATE_PROVIDER_BODY")
        store = VisitorStore(self.auth, operation_limit=2)
        app = VisitorApplication(store, FailedAgent())
        with patch("backend.product.visitor._now", return_value=self.now):
            sid, token, _ = store.create("source.fail")
            data = {"api_version": "3.0", "contract_version": "1.0.0", "session_id": sid,
                    "request_id": "request.fail", "turn_id": 0, "state_revision": 0,
                    "question": "问题"}
            for _ in range(2):
                response = app.handle("POST", "/api/v3/query", request_id=data["request_id"],
                                      body=json.dumps(data).encode(), token=token, source="source.fail")
                self.assertEqual(503, response.status)
            restored = VisitorStore(self.auth, operation_limit=2)
            with self.assertRaises(VisitorError):
                restored.reserve_operation(sid, "source.fail")
            self.assertEqual(0, restored.owned(sid, token)["state_revision"])


class RagRepairTests(unittest.TestCase):
    def setUp(self):
        self.api = OnlineRagService(None)
        self.citation = {"evidence_id": "doc.local.p1", "document_id": "doc.local",
                         "document_title": "差旅制度", "page": 1,
                         "snippet": "报销截止时间为次月第一个工作日18:00。", "score": 2.0}
        self.req = {"question": "报销截止时间？", "session_id": "session.rag",
                    "request_id": "request.rag", "turn_id": 0, "state_revision": 0}

    def test_dynamic_citation_passes_and_wrong_page_id_fails(self):
        response = self.api._response(self.req, [self.citation], elapsed_ms=0)
        OnlineContractValidator().validate_response(response)
        bad = deepcopy(response)
        bad["citations"][0]["page"] = 2
        with self.assertRaises(OnlineContractError):
            OnlineContractValidator().validate_response(bad)

    def test_marker_only_page_cannot_supply_answer_evidence(self):
        self.assertEqual("", _matching_snippet("标题 [EXP-001]", {"标题"}))

    def test_decision_evidence_must_match_actual_citations(self):
        response = self.api._response(self.req, [self.citation], elapsed_ms=0)
        response["decision"]["evidence_refs"] = ["doc.other.p1"]
        with self.assertRaises(OnlineContractError):
            OnlineContractValidator().validate_response(response)

    def test_retrieved_snippet_is_real_and_excludes_adjacent_evidence(self):
        text = "费用管理制度\n[EXP-001] 报销截止时间为次月第一个工作日18:00。\n[SALES-002] 无关销售内容。"
        class Store:
            def get_active_index(self): return "index.1"
            def list_records(self):
                return [SimpleNamespace(state="published", index_version="index.1",
                                        document_id="doc.local", bytes=len(b"fake-pdf"),
                                        page_count=1, sha256=hashlib.sha256(b"fake-pdf").hexdigest())]
            def read_quarantine(self, _document_id): return b"fake-pdf"
        index = PublishedDocumentIndex(Store(), min_score=0)
        with patch("backend.rag.published_retriever.extract_page_texts", return_value=[text]):
            index.rebuild()
            citations = index.retrieve("报销截止时间")
        self.assertEqual(1, len(citations))
        citation = citations[0]
        self.assertEqual("费用管理制度", citation["document_title"])
        self.assertIn(citation["snippet"], text)
        self.assertIn("18:00", citation["snippet"])
        self.assertNotIn("销售", citation["snippet"])
        self.assertEqual("doc.local.p1", citation["evidence_id"])

    def test_bundled_pdf_title_snippet_and_page_are_real(self):
        pdf = (ROOT / "data/raw/pdf/expense_policy.pdf").read_bytes()
        class Store:
            def get_active_index(self): return "index.real"
            def list_records(self):
                return [SimpleNamespace(state="published", index_version="index.real",
                                        document_id="doc.actual", bytes=len(pdf),
                                        page_count=3, sha256=hashlib.sha256(pdf).hexdigest())]
            def read_quarantine(self, _document_id): return pdf
        api = OnlineRagService(Store(), min_score=0)
        response = api.submit({**self.req, "question": "差旅行程结束"})
        OnlineContractValidator().validate_response(response)
        self.assertEqual("success", response["status"])
        self.assertEqual("差旅与报销制度", response["citations"][0]["document_title"])
        self.assertIn("15", response["citations"][0]["snippet"])
        self.assertNotIn("5,000", response["citations"][0]["snippet"])


if __name__ == "__main__":
    unittest.main()
