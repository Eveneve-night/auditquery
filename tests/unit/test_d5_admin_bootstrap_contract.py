"""D5 default administrator bootstrap v2 contract tests."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
import unittest

from jsonschema import Draft202012Validator

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.evaluation import validate_d5_admin_bootstrap_contract as gate  # noqa: E402


def _assets() -> tuple[dict, dict, dict]:
    return (
        copy.deepcopy(gate._load(gate.CONTRACT_PATH)),
        copy.deepcopy(gate._load(gate.CONTRACT_SCHEMA_PATH)),
        copy.deepcopy(gate._load(gate.API_SCHEMA_PATH)),
    )


class D5AdminBootstrapContractTest(unittest.TestCase):
    def test_v2_assets_are_strict_and_valid(self) -> None:
        contract, schema, api_schema = _assets()
        self.assertEqual(gate.check_contract(contract, schema, api_schema), [])
        mutated = copy.deepcopy(contract)
        mutated["unexpected"] = True
        self.assertTrue(gate.check_contract(mutated, schema, api_schema))

    def test_v1_is_retained_and_explicitly_superseded_only_for_bootstrap(self) -> None:
        contract, _, _ = _assets()
        self.assertTrue((PROJECT_ROOT / "configs/contracts/d5/product-layer.v1.json").is_file())
        self.assertEqual(contract["supersedes"]["contract_version"], "1.0.0")
        self.assertIn("roles.bootstrap", contract["supersedes"]["scope"])
        self.assertIn("继续有效", contract["supersedes"]["retained"])

    def test_authorized_initial_identity_and_hash_only_storage_are_frozen(self) -> None:
        contract, schema, api_schema = _assets()
        self.assertEqual(contract["initial_credentials"]["username"], "admin")
        self.assertEqual(contract["initial_credentials"]["create_only_when"], "auth_store_empty")
        self.assertEqual(contract["initial_credentials"]["password_storage"], "argon2id_hash_only")
        contract["initial_credentials"]["initial_password"] = "wrong"
        self.assertIn("authorized_default_password_mismatch", gate.check_contract(contract, schema, api_schema))

    def test_login_response_exposes_optional_password_change(self) -> None:
        _, _, api_schema = _assets()
        payload = {
            "api_version": "3.0", "contract_version": "2.0.0", "request_id": "req.1",
            "authenticated": True, "csrf_token": "x" * 32, "expires_at": "2026-10-13T00:00:00Z",
            "must_change_password": False, "password_change_available": True,
            "allowed_actions": ["admin", "change_password", "logout"]
        }
        validator = Draft202012Validator(api_schema).evolve(schema={"$ref": "#/$defs/admin_session_response"})
        self.assertEqual(list(validator.iter_errors(payload)), [])
        payload["must_change_password"] = True
        self.assertTrue(list(validator.iter_errors(payload)))

    def test_password_change_payload_rejects_unknown_and_multi_value_fields(self) -> None:
        _, _, api_schema = _assets()
        payload = {
            "api_version": "3.0", "contract_version": "2.0.0", "request_id": "req.2",
            "current_password": "old-password", "new_password": "new-password-123"
        }
        validator = Draft202012Validator(api_schema).evolve(schema={"$ref": "#/$defs/password_change_request"})
        self.assertEqual(list(validator.iter_errors(payload)), [])
        payload["admin"] = True
        self.assertTrue(list(validator.iter_errors(payload)))

    def test_password_change_response_requires_revocation_and_cleared_flag(self) -> None:
        _, _, api_schema = _assets()
        payload = {
            "api_version": "3.0", "contract_version": "2.0.0", "request_id": "req.3",
            "status": "password_changed", "must_change_password": False,
            "other_sessions_revoked": True, "resource_revision": 1
        }
        validator = Draft202012Validator(api_schema).evolve(schema={"$ref": "#/$defs/password_change_response"})
        self.assertEqual(list(validator.iter_errors(payload)), [])
        payload["other_sessions_revoked"] = False
        self.assertTrue(list(validator.iter_errors(payload)))

    def test_all_password_sinks_and_non_loopback_gate_are_frozen(self) -> None:
        contract, schema, api_schema = _assets()
        contract["confidentiality"]["forbidden_sinks"].remove("machine_report")
        contract["deployment_guard"]["rules"] = ["允许任意地址。"]
        findings = gate.check_contract(contract, schema, api_schema)
        self.assertIn("password_forbidden_sinks_mismatch", findings)
        self.assertIn("non_loopback_fail_closed_missing", findings)

    def test_machine_report_is_deterministic_and_never_contains_password(self) -> None:
        first = gate.validate_all()
        second = gate.validate_all()
        self.assertEqual(first["status"], "pass")
        self.assertEqual(first["stable_sha256"], second["stable_sha256"])
        self.assertEqual(first["mutation_self_check"]["detected"], first["mutation_self_check"]["total"])
        serialized = json.dumps(first, ensure_ascii=False)
        contract = gate._load(gate.CONTRACT_PATH)
        self.assertNotIn(contract["initial_credentials"]["initial_password"], serialized)

    def test_report_keeps_runtime_pending_and_offline_boundaries(self) -> None:
        report = gate.validate_all()
        self.assertIn("b_default_admin_runtime_and_optional_password_change", report["not_implemented"])
        self.assertIn("c_optional_password_change_ui", report["not_implemented"])
        self.assertEqual(report["boundaries"]["model_api_calls"], 0)
        self.assertEqual(report["boundaries"]["credential_files_read"], 0)


if __name__ == "__main__":
    unittest.main()
