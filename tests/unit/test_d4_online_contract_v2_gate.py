"""D4-07 A 侧在线公共契约 v2 收口校验器的离线测试。"""

from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
for candidate in (PROJECT_ROOT, PROJECT_ROOT / "src"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from backend.api.online_contracts import OnlineContractValidator  # noqa: E402
from tools.evaluation import validate_d4_online_contract_v2 as gate  # noqa: E402


def _contract() -> dict:
    return copy.deepcopy(gate._load_json(gate.CONTRACT_PATH))


def _examples() -> dict:
    return copy.deepcopy(
        gate._load_json(
            gate.EXAMPLES_DIR / "clarification-binding.v2.examples.json"
        )
    )


def _run(contract: dict, examples: dict, runtime_codes: set[str] | None = None) -> dict:
    validator = OnlineContractValidator()
    source = gate._canonical_text_bytes(gate.RUNTIME_SOURCE).decode("utf-8")
    codes = (
        gate._runtime_raise_codes(source) if runtime_codes is None else runtime_codes
    )
    return gate._check_contract(
        contract,
        schema_names={path.name for path in gate.SCHEMA_ROOT.iterdir()},
        runtime_codes=codes,
        examples=examples,
        validator=validator,
    )


class D4OnlineContractV2GateTest(unittest.TestCase):
    def test_frozen_contract_has_no_findings(self) -> None:
        result = _run(_contract(), _examples())
        self.assertEqual(result["errors"], [])

    def test_error_code_drift_is_detected(self) -> None:
        contract = _contract()
        contract["runtime_gates"][0]["error_code"] = "clarification_renamed_by_mistake"
        result = _run(contract, _examples())
        self.assertTrue(
            any(
                item.startswith("error_code_missing_in_runtime:")
                for item in result["errors"]
            )
        )

    def test_example_binding_tamper_is_detected(self) -> None:
        examples = _examples()
        examples["pending_question"]["request_id"] = "request.tampered"
        result = _run(_contract(), examples)
        self.assertIn("example_binding_field_mismatch:request_id", result["errors"])

    def test_example_evidence_mismatch_is_detected(self) -> None:
        examples = _examples()
        examples["accepted_request"]["evidence_refs"] = ["value_evidence.other"]
        result = _run(_contract(), examples)
        self.assertIn("example_evidence_ref_mismatch", result["errors"])

    def test_runtime_mutation_blocked_by_schema_is_detected(self) -> None:
        """运行时 Gate 的样例必须能穿过 Schema，否则声明的错误码不可达。"""

        examples = _examples()
        examples["runtime_rejected_mutations"] = [
            {
                "case_id": "value_emptied",
                "field": "values",
                "replacement": [],
                "error_code": "clarification_value_not_offered",
            }
        ]
        result = _run(_contract(), examples)
        self.assertIn(
            "runtime_mutation_blocked_by_schema:value_emptied", result["errors"]
        )

    def test_transport_example_tamper_is_detected(self) -> None:
        transport = gate._load_transport_examples()
        response_name = "online-clarification-required.v2.valid.json"
        transport[response_name] = copy.deepcopy(transport[response_name])
        transport[response_name]["schema_version"] = "9.9.9"
        validator = OnlineContractValidator()
        errors = gate._check_transport_examples(validator, transport)
        self.assertIn(f"transport_example_invalid:{response_name}", errors)

    def test_schema_id_mismatch_is_detected(self) -> None:
        real = [gate.SCHEMA_ROOT / name for name in gate.EXPECTED_SCHEMA_FILES]
        self.assertTrue(gate._schema_ids_match(real))
        # 伪造一个 $id 与文件名不一致的 Schema，检查必须返回 False
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "renamed.schema.json"
            payload = copy.deepcopy(gate._load_json(real[0]))
            payload["$id"] = "other-name.schema.json"
            fake.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            self.assertFalse(gate._schema_ids_match([fake]))

    def test_mutation_self_check_detects_every_case(self) -> None:
        report = gate.validate_all()
        mutation = report["mutation_self_check"]
        self.assertTrue(mutation["all_detected"])
        self.assertEqual(mutation["detected"], mutation["total"])
        self.assertEqual(mutation["total"], 7)
        self.assertEqual(report["transport_examples"]["all_valid_against_frozen_schema"], True)

    def test_report_is_deterministic_and_offline(self) -> None:
        first = gate.validate_all()
        second = gate.validate_all()
        self.assertEqual(first["stable_sha256"], second["stable_sha256"])
        self.assertEqual(first["status"], "pass")
        boundaries = first["boundaries"]
        self.assertEqual(boundaries["network_calls"], 0)
        self.assertEqual(boundaries["model_api_calls"], 0)
        self.assertEqual(boundaries["credential_files_read"], 0)
        self.assertEqual(boundaries["estimated_cost_cny"], 0.0)
        self.assertEqual(boundaries["validation_cases_read"], 0)
        self.assertEqual(boundaries["hidden_test_cases_read"], 0)
        self.assertFalse(boundaries["answer_derived_fields_used_for_input"])


if __name__ == "__main__":
    unittest.main()
