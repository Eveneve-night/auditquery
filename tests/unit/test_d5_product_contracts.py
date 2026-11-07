"""D5-00 产品层公共契约、权限矩阵和威胁模型离线测试。"""

from __future__ import annotations

import copy
from pathlib import Path
import sys
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.evaluation import validate_d5_product_contracts as gate  # noqa: E402


def _assets() -> tuple[dict, dict, dict, dict]:
    return (
        copy.deepcopy(gate._load(gate.CONTRACT_PATH)),
        copy.deepcopy(gate._load(gate.CONTRACT_SCHEMA_PATH)),
        copy.deepcopy(gate._load(gate.API_SCHEMA_PATH)),
        copy.deepcopy(gate._load(gate.EXAMPLES_PATH)),
    )


def _errors(contract: dict, contract_schema: dict, api_schema: dict, examples: dict) -> list[str]:
    return gate.check_contract(contract, contract_schema, api_schema, examples)


class D5ProductContractsTest(unittest.TestCase):
    def test_frozen_assets_are_valid(self) -> None:
        self.assertEqual(_errors(*_assets()), [])

    def test_all_admin_routes_require_admin_session_and_401_403(self) -> None:
        contract, _, _, _ = _assets()
        protected = [item for item in contract["endpoints"] if item["path"].startswith("/api/v3/admin/")]
        self.assertGreater(len(protected), 10)
        for endpoint in protected:
            self.assertEqual(endpoint["roles"], ["admin"])
            self.assertEqual(endpoint["auth"], "admin_session")
            self.assertIn("authentication_required", endpoint["errors"])
            self.assertIn("forbidden", endpoint["errors"])

    def test_visitor_session_creation_is_anonymous_but_followups_are_bound(self) -> None:
        contract, _, _, _ = _assets()
        create = next(
            item
            for item in contract["endpoints"]
            if item["id"] == "create_visitor_session"
        )
        read = next(
            item
            for item in contract["endpoints"]
            if item["id"] == "read_own_session"
        )
        self.assertEqual(create["auth"], "anonymous")
        self.assertEqual(read["auth"], "visitor_session")
        self.assertEqual(read["ownership"], "own_session")

    def test_admin_auth_mutation_is_rejected(self) -> None:
        assets = list(_assets())
        endpoint = next(item for item in assets[0]["endpoints"] if item["id"] == "admin_health")
        endpoint["auth"] = "visitor_session"
        self.assertIn("admin_auth_not_enforced:admin_health", _errors(*assets))

    def test_admin_write_without_csrf_is_rejected(self) -> None:
        assets = list(_assets())
        endpoint = next(item for item in assets[0]["endpoints"] if item["id"] == "admin_delete_key")
        endpoint["csrf"] = "not_applicable"
        self.assertIn("admin_csrf_not_required:admin_delete_key", _errors(*assets))

    def test_unknown_endpoint_error_and_dangling_schema_are_rejected(self) -> None:
        assets = list(_assets())
        endpoint = assets[0]["endpoints"][0]
        endpoint["errors"].append("not_registered")
        endpoint["response_schema"] = "api-v3.schema.json#/$defs/not_registered"
        findings = _errors(*assets)
        self.assertIn("endpoint_error_unknown:create_visitor_session:not_registered", findings)
        self.assertIn("endpoint_schema_ref_unknown:create_visitor_session:response_schema", findings)

    def test_idor_uses_uniform_not_found(self) -> None:
        assets = list(_assets())
        endpoint = next(item for item in assets[0]["endpoints"] if item["id"] == "read_own_session")
        endpoint["errors"].remove("resource_not_found")
        self.assertIn("idor_uniform_not_found_missing:read_own_session", _errors(*assets))

    def test_key_never_appears_in_response(self) -> None:
        assets = list(_assets())
        assets[2]["$defs"]["key_status_response"]["properties"]["api_key"] = {"type": "string"}
        self.assertIn("api_key_plaintext_response_exposed", _errors(*assets))

    def test_model_provider_and_fallback_are_immutable(self) -> None:
        assets = list(_assets())
        assets[0]["model_boundary"]["admin_mutable"] = True
        assets[0]["model_boundary"]["fallback"] = True
        self.assertIn("single_model_boundary_changed", _errors(*assets))

    def test_audit_forbidden_content_cannot_be_allowed(self) -> None:
        assets = list(_assets())
        assets[0]["audit"]["allowed_fields"].append("question_text")
        self.assertIn("audit_allowlist_forbidden_overlap", _errors(*assets))

    def test_store_set_and_secret_atomicity_are_frozen(self) -> None:
        assets = list(_assets())
        secret = next(item for item in assets[0]["store_interfaces"] if item["id"] == "SecretStore")
        secret["transaction"] = "先删除旧值再尝试写新值。"
        self.assertIn("secret_store_atomicity_incomplete", _errors(*assets))

    def test_auth_and_document_atomicity_are_frozen(self) -> None:
        assets = list(_assets())
        auth = next(
            item
            for item in assets[0]["store_interfaces"]
            if item["id"] == "AuthStore"
        )
        document = next(
            item
            for item in assets[0]["store_interfaces"]
            if item["id"] == "DocumentStore"
        )
        auth["transaction"] = "分步写入初始化状态。"
        document["transaction"] = "直接覆盖活动索引。"
        findings = _errors(*assets)
        self.assertIn("auth_store_atomicity_incomplete", findings)
        self.assertIn("document_store_atomicity_incomplete", findings)

    def test_pdf_magic_order_and_old_index_fallback_are_frozen(self) -> None:
        assets = list(_assets())
        assets[0]["pdf_lifecycle"]["validation_order"].remove("pdf_magic_bytes")
        assets[0]["pdf_lifecycle"]["failure"] = "失败后返回错误。"
        findings = _errors(*assets)
        self.assertIn("pdf_validation_order_mismatch", findings)
        self.assertIn("pdf_atomic_publish_incomplete", findings)

    def test_answer_derived_fields_are_rejected(self) -> None:
        assets = list(_assets())
        assets[3]["examples"][0]["payload"]["gold_sql"] = "SELECT 1"
        self.assertIn("answer_derived_field_present", _errors(*assets))

    def test_examples_reject_unknown_fields(self) -> None:
        _, _, api_schema, examples = _assets()
        query = next(item for item in examples["examples"] if item["id"] == "visitor_query_request")
        query["payload"]["sql"] = "SELECT 1"
        findings = gate._validate_payload(api_schema, query["schema_ref"], query["payload"])
        self.assertTrue(findings)

    def test_v3_response_reuses_frozen_d4_v2_payload(self) -> None:
        assets = list(_assets())
        payload = assets[2]["$defs"]["agent_response"]["properties"]["payload"]
        refs = [item.get("$ref") for item in payload["oneOf"]]
        self.assertIn(gate.EXPECTED_D4_RESPONSE_REF, refs)
        payload["oneOf"] = [{"type": "object"}, {"type": "null"}]
        self.assertIn("d4_v2_response_binding_missing", _errors(*assets))

    def test_mutation_self_check_and_report_are_deterministic(self) -> None:
        first = gate.validate_all()
        second = gate.validate_all()
        self.assertEqual(first["status"], "pass")
        self.assertEqual(first["baseline_main_sha"], gate.BASELINE_MAIN_SHA)
        self.assertEqual(first["stable_sha256"], second["stable_sha256"])
        self.assertEqual(first["mutation_self_check"]["detected"], first["mutation_self_check"]["total"])
        self.assertEqual(first["mutation_self_check"]["total"], 11)

    def test_report_boundary_is_offline_and_contract_only(self) -> None:
        report = gate.validate_all()
        self.assertEqual(report["boundaries"], {
            "network_calls": 0,
            "model_api_calls": 0,
            "credential_files_read": 0,
            "estimated_cost_cny": 0.0,
            "validation_cases_read": 0,
            "hidden_test_cases_read": 0,
            "sql_executions": 0,
        })
        self.assertIn("b_auth_rbac_audit_runtime", report["not_implemented"])
        self.assertIn("c_pdf_upload_and_index_runtime", report["not_implemented"])


if __name__ == "__main__":
    unittest.main()
