"""D4-07 local Web/API and explainability vertical-slice tests."""

from __future__ import annotations

import copy
import json
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest

from jsonschema import Draft202012Validator


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

from backend.api.d4_demo import (  # noqa: E402
    D4DemoApplication,
    D4DemoError,
    D4DemoService,
    MAX_REQUEST_BYTES,
    _validate_pdf_document,
)
from tools.evaluation.validate_d4_web_api import (  # noqa: E402
    _canonical_text_bytes,
    validate_all,
)
from tools.demo.run_d4_demo import LOOPBACK_HOST, _build_server  # noqa: E402


class D4WebApiBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.service = D4DemoService()
        cls.app = D4DemoApplication(cls.service)

    def run_scenario(self, scenario_id: str, suffix: str = "test") -> dict:
        return self.service.run(
            {
                "schema_version": "1.0.0",
                "request_id": f"req.{suffix}",
                "scenario_id": scenario_id,
            }
        )


class D4DemoServiceTest(D4WebApiBase):
    def test_capabilities_are_explicitly_offline_and_scenario_bounded(self) -> None:
        capabilities = self.service.capabilities()
        self.assertEqual("offline_public_demo", capabilities["mode"])
        self.assertEqual(4, len(capabilities["scenarios"]))
        self.assertFalse(capabilities["boundaries"]["arbitrary_query_accepted"])
        self.assertFalse(capabilities["boundaries"]["arbitrary_sql_accepted"])
        self.assertFalse(capabilities["boundaries"]["online_model_enabled"])

    def test_safe_sql_traverses_decision_safety_gate_and_readonly_execution(self) -> None:
        result = self.run_scenario("D4-07-SQL-001", "sql.safe")
        self.assertEqual("success", result["status"])
        self.assertEqual("accept", result["decision"]["action"])
        self.assertEqual("uncalibrated_raw_score", result["decision"]["score_kind"])
        self.assertFalse(result["decision"]["accept_authorizes_sql_execution"])
        self.assertTrue(result["decision"]["requires_d4_02_safety_gate"])
        self.assertTrue(result["sql"]["safety_approved"])
        self.assertEqual(1, result["sql"]["execution_attempts"])
        self.assertEqual([[275]], result["sql"]["rows"])
        stages = [item["stage"] for item in result["trace"]]
        self.assertIn("initial_decision", stages)
        self.assertIn("clarification_question", stages)
        self.assertIn("state_merge", stages)
        self.assertIn("redecision", stages)
        self.assertIn("safety_gate", stages)
        self.assertIn("sql_execution", stages)

    def test_malicious_sql_is_visible_but_never_executed(self) -> None:
        result = self.run_scenario("D4-07-SQL-002", "sql.reject")
        self.assertEqual("refused", result["status"])
        self.assertFalse(result["sql"]["safety_approved"])
        self.assertEqual(0, result["sql"]["execution_attempts"])
        self.assertEqual([], result["sql"]["rows"])
        self.assertNotIn("sql_execution", [item["stage"] for item in result["trace"]])

    def test_rag_success_exposes_real_document_page_and_evidence(self) -> None:
        result = self.run_scenario("D4-07-RAG-001", "rag.hit")
        self.assertEqual("success", result["status"])
        self.assertEqual("deterministic_bm25", result["decision"]["score_kind"])
        self.assertFalse(result["disclosure"]["model_answer_generated"])
        self.assertIsNone(result["sql"])
        citation = result["citations"][0]
        self.assertEqual("SALES-001", citation["evidence_id"])
        self.assertEqual("sales_playbook", citation["document_id"])
        self.assertEqual(1, citation["page"])
        self.assertTrue(citation["snippet"])

    def test_rag_insufficient_evidence_refuses_answer(self) -> None:
        result = self.run_scenario("D4-07-RAG-002", "rag.refuse")
        self.assertEqual("refused", result["status"])
        self.assertEqual("reject", result["decision"]["action"])
        self.assertEqual("insufficient_evidence", result["decision"]["reason_code"])
        self.assertEqual([], result["citations"])
        self.assertFalse(result["disclosure"]["model_answer_generated"])

    def test_response_is_deterministic_for_the_same_request(self) -> None:
        first = self.run_scenario("D4-07-SQL-001", "stable")
        second = self.run_scenario("D4-07-SQL-001", "stable")
        self.assertEqual(first, second)

    def test_all_candidates_have_resolvable_evidence_references(self) -> None:
        sql = self.run_scenario("D4-07-SQL-001", "refs.sql")
        known_domain_refs = set(self.service.recovery.domain_pack.items_by_id)
        for candidate in sql["candidate_evidence"]:
            self.assertTrue(set(candidate["evidence_refs"]).issubset(known_domain_refs))
        rag = self.run_scenario("D4-07-RAG-001", "refs.rag")
        for candidate in rag["candidate_evidence"]:
            self.assertTrue(set(candidate["evidence_refs"]).issubset(self.service.evidence))

    def test_unknown_and_arbitrary_input_fields_fail_closed(self) -> None:
        invalid_payloads = (
            {
                "schema_version": "1.0.0",
                "request_id": "req.invalid.sql",
                "scenario_id": "D4-07-SQL-001",
                "sql": "SELECT 1",
            },
            {
                "schema_version": "1.0.0",
                "request_id": "req.invalid.query",
                "scenario_id": "D4-07-SQL-001",
                "query": "anything",
            },
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(D4DemoError, "invalid_demo_request"):
                    self.service.run(payload)

    def test_answer_derived_fields_fail_before_schema_validation(self) -> None:
        with self.assertRaisesRegex(D4DemoError, "answer_derived_field_forbidden"):
            self.service.run(
                {
                    "schema_version": "1.0.0",
                    "request_id": "req.invalid.answer",
                    "scenario_id": "D4-07-SQL-001",
                    "expected": "anything",
                }
            )

    def test_unknown_scenario_has_stable_safe_error(self) -> None:
        with self.assertRaises(D4DemoError) as caught:
            self.service.run(
                {
                    "schema_version": "1.0.0",
                    "request_id": "req.unknown",
                    "scenario_id": "D4-07-SQL-999",
                }
            )
        self.assertEqual("unknown_demo_scenario", caught.exception.code)
        self.assertEqual(404, caught.exception.http_status)

    def test_committed_response_schema_rejects_unknown_output_fields(self) -> None:
        schema = json.loads(
            (PROJECT_ROOT / "configs/schemas/d4-07/demo-response.v1.schema.json").read_text(
                encoding="utf-8"
            )
        )
        result = self.run_scenario("D4-07-RAG-001", "schema")
        result["unexpected"] = True
        errors = list(Draft202012Validator(schema).iter_errors(result))
        self.assertTrue(errors)


class D4DemoApplicationTest(D4WebApiBase):
    @staticmethod
    def payload(response) -> dict:
        return json.loads(response.body.decode("utf-8"))

    def test_health_and_capabilities_are_available_without_network_model(self) -> None:
        health = self.app.handle("GET", "/api/v1/health")
        self.assertEqual(200, health.status)
        self.assertEqual(0, self.payload(health)["model_api_calls"])
        capabilities = self.app.handle("GET", "/api/v1/capabilities")
        self.assertEqual(200, capabilities.status)
        self.assertEqual(4, len(self.payload(capabilities)["scenarios"]))

    def test_demo_post_returns_schema_valid_result(self) -> None:
        response = self.app.handle(
            "POST",
            "/api/v1/demo",
            content_type="application/json; charset=utf-8",
            body=json.dumps(
                {
                    "schema_version": "1.0.0",
                    "request_id": "req.http",
                    "scenario_id": "D4-07-RAG-001",
                }
            ).encode("utf-8"),
        )
        self.assertEqual(200, response.status)
        self.assertEqual("success", self.payload(response)["status"])

    def test_wrong_method_content_type_json_and_size_are_stable_errors(self) -> None:
        cases = (
            (self.app.handle("GET", "/api/v1/demo"), 405, "method_not_allowed"),
            (
                self.app.handle("POST", "/api/v1/demo", body=b"{}", content_type="text/plain"),
                415,
                "content_type_required",
            ),
            (
                self.app.handle(
                    "POST", "/api/v1/demo", body=b"{", content_type="application/json"
                ),
                400,
                "invalid_json",
            ),
            (
                self.app.handle(
                    "POST",
                    "/api/v1/demo",
                    body=b"x" * (MAX_REQUEST_BYTES + 1),
                    content_type="application/json",
                ),
                413,
                "request_too_large",
            ),
        )
        for response, expected_status, code in cases:
            with self.subTest(code=code):
                self.assertEqual(expected_status, response.status)
                self.assertEqual(code, self.payload(response)["error"]["code"])

    def test_path_traversal_and_unknown_routes_do_not_serve_files(self) -> None:
        for path in ("/../AGENTS.md", "/assets/../index.html", "/api/v1/unknown"):
            with self.subTest(path=path):
                response = self.app.handle("GET", path)
                self.assertEqual(404, response.status)
                self.assertEqual("route_not_found", self.payload(response)["error"]["code"])

    def test_static_assets_have_security_headers_and_no_remote_dependency(self) -> None:
        for path in ("/", "/assets/styles.css", "/assets/app.js"):
            response = self.app.handle("GET", path)
            self.assertEqual(200, response.status)
            self.assertIn("default-src 'none'", response.headers["Content-Security-Policy"])
            self.assertEqual("nosniff", response.headers["X-Content-Type-Options"])
            self.assertNotIn(b"https://", response.body)
            self.assertNotIn(b"http://", response.body)

    def test_error_response_does_not_echo_request_or_exception(self) -> None:
        marker = "DO_NOT_ECHO_PRIVATE_INPUT"
        response = self.app.handle(
            "POST",
            "/api/v1/demo",
            content_type="application/json",
            body=json.dumps(
                {
                    "schema_version": "1.0.0",
                    "request_id": "req.private",
                    "scenario_id": "D4-07-SQL-001",
                    "query": marker,
                }
            ).encode("utf-8"),
        )
        self.assertEqual(400, response.status)
        self.assertNotIn(marker.encode("utf-8"), response.body)
        self.assertNotIn(b"ValidationError", response.body)

    def test_server_entrypoint_is_loopback_only(self) -> None:
        server = _build_server(LOOPBACK_HOST, 0, self.app)
        try:
            self.assertEqual("127.0.0.1", server.server_address[0])
            self.assertGreater(server.server_address[1], 0)
        finally:
            server.server_close()
        for forbidden_host in ("::1", "0.0.0.0"):
            with self.subTest(host=forbidden_host):
                with self.assertRaisesRegex(ValueError, "IPv4 loopback"):
                    _build_server(forbidden_host, 0, self.app)


class D4WebApiBoundaryTest(unittest.TestCase):
    def test_provider_boundary_remains_single_qwen_flash(self) -> None:
        providers = sorted((PROJECT_ROOT / "configs/providers").glob("*.json"))
        self.assertEqual(1, len(providers))
        config = json.loads(providers[0].read_text(encoding="utf-8"))
        self.assertEqual("qwen3.7-flash", config["model"])

    def test_public_scenarios_do_not_contain_answer_derived_fields(self) -> None:
        value = json.loads(
            (PROJECT_ROOT / "configs/demo/d4-07/public-scenarios.v1.json").read_text(
                encoding="utf-8"
            )
        )

        def visit(item):
            if isinstance(item, dict):
                for key, child in item.items():
                    normalized = key.casefold().replace("-", "_")
                    self.assertNotIn("expected", normalized)
                    self.assertNotIn("gold", normalized)
                    self.assertNotIn(normalized, {"label", "labels", "answer", "answers"})
                    visit(child)
            elif isinstance(item, list):
                for child in item:
                    visit(child)

        visit(value)

    def test_frontend_explains_score_and_thought_boundary(self) -> None:
        index = (PROJECT_ROOT / "src/frontend/d4-demo/index.html").read_text(encoding="utf-8")
        self.assertIn("未校准", index)
        self.assertIn("不展示隐藏思维链", index)
        self.assertIn("模型调用 <strong>0</strong>", index)

    def test_pdf_manifest_artifact_and_page_attacks_fail_closed(self) -> None:
        manifest = json.loads(
            (PROJECT_ROOT / "data/manifests/pdf-v1.json").read_text(encoding="utf-8")
        )
        original = manifest["documents"][0]
        first_evidence_id = next(iter(original["evidence_pages"]))
        wrong_page = 2 if original["evidence_pages"][first_evidence_id] == 1 else 1
        probes = (
            ("missing", {"pdf_path": "data/raw/pdf/not_present.pdf"}, "pdf_artifact_missing"),
            ("sha", {"sha256": "0" * 64}, "pdf_artifact_hash_mismatch"),
            ("bytes", {"bytes": original["bytes"] + 1}, "pdf_artifact_size_mismatch"),
            (
                "page_count",
                {"page_count": original["page_count"] + 1},
                "pdf_page_count_mismatch",
            ),
            (
                "page_mapping",
                {"evidence_pages": {**original["evidence_pages"], first_evidence_id: wrong_page}},
                "rag_evidence_page_mismatch",
            ),
            (
                "page_999",
                {"evidence_pages": {**original["evidence_pages"], first_evidence_id: 999}},
                "invalid_pdf_manifest",
            ),
        )
        for probe_id, changes, expected_code in probes:
            document = copy.deepcopy(original)
            document.update(changes)
            with self.subTest(probe=probe_id):
                with self.assertRaises(D4DemoError) as captured:
                    _validate_pdf_document(PROJECT_ROOT, document)
                self.assertEqual(expected_code, captured.exception.code)

    def test_runtime_records_dual_parser_pdf_integrity(self) -> None:
        service = D4DemoService()
        self.assertGreaterEqual(service.rag_integrity["document_count"], 10)
        self.assertGreaterEqual(service.rag_integrity["evidence_count"], 1)
        self.assertTrue(
            service.rag_integrity["pdf_exists_sha_bytes_page_count_verified"]
        )
        self.assertTrue(service.rag_integrity["dual_parser_evidence_page_verified"])


class D4WebApiMachineReportTest(unittest.TestCase):
    def test_frontend_artifact_hash_input_is_line_ending_independent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lf_path = Path(directory) / "lf.txt"
            crlf_path = Path(directory) / "crlf.txt"
            lf_path.write_bytes("公开演示\n第二行\n".encode("utf-8"))
            crlf_path.write_bytes("公开演示\r\n第二行\r\n".encode("utf-8"))
            self.assertEqual(
                _canonical_text_bytes(lf_path),
                _canonical_text_bytes(crlf_path),
            )

    def test_machine_report_is_deterministic_and_matches_committed_artifact(self) -> None:
        first = validate_all()
        second = validate_all()
        self.assertEqual(first, second)
        committed = json.loads(
            (PROJECT_ROOT / "tests/reports/d4-web-api-v1.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(first, committed)
        digest = hashlib.sha256(
            json.dumps(first, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        self.assertRegex(digest, r"^[0-9a-f]{64}$")

    def test_machine_report_is_content_safe_and_records_zero_exposure(self) -> None:
        report = validate_all()
        serialized = json.dumps(report, ensure_ascii=False)
        for forbidden in (
            "SELECT COUNT",
            "DELETE FROM",
            "月度经营报表的截止时间",
            "公司健身房几点开门",
        ):
            self.assertNotIn(forbidden, serialized)
        self.assertEqual(0, report["model_api_calls"])
        self.assertEqual(0, report["credential_files_read"])
        self.assertEqual(0, report["input_isolation"]["validation_current_task_exposure_count"])
        self.assertEqual(0, report["input_isolation"]["hidden_test_exposure_count"])


if __name__ == "__main__":
    unittest.main()
