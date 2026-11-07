"""D4-07 clarification binding v2 public contract tests.

The local reference gate below verifies the published examples and error-code
mapping only.  It is not the B-owned server state gate and must not be wired
into production request handling.
"""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import unittest

from jsonschema import Draft202012Validator
from referencing import Registry, Resource


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_ROOT = PROJECT_ROOT / "configs" / "schemas" / "d4-07"
EXAMPLE_ROOT = PROJECT_ROOT / "configs" / "examples" / "d4-07"
CONTRACT_PATH = (
    PROJECT_ROOT
    / "configs"
    / "contracts"
    / "d4-07"
    / "clarification-binding.v2.json"
)


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _validator(wrapper_name: str) -> Draft202012Validator:
    registry: Registry = Registry()
    for name in (
        "shared-api-definitions.v1.schema.json",
        "shared-api-definitions.v2.schema.json",
    ):
        schema = _load(SCHEMA_ROOT / name)
        registry = registry.with_resource(
            schema["$id"], Resource.from_contents(schema)
        )
    return Draft202012Validator(_load(SCHEMA_ROOT / wrapper_name), registry=registry)


def _reference_runtime_gate(
    *,
    pending: dict,
    request: dict,
    contract: dict,
    consumed_answer_ids: set[str] | None = None,
    consumed_question_ids: set[str] | None = None,
) -> str | None:
    """Materialize the documented B runtime rules for public example checks."""

    consumed_answer_ids = consumed_answer_ids or set()
    consumed_question_ids = consumed_question_ids or set()
    if (
        request["answer_id"] in consumed_answer_ids
        or request["question_id"] in consumed_question_ids
    ):
        return "clarification_answer_replayed"

    errors = {
        gate["field"]: gate["error_code"]
        for gate in contract["runtime_gates"]
        if gate["rule_id"].startswith("binding.")
    }
    for field in contract["immutable_echo_fields"]:
        if request[field] != pending[field]:
            return errors[field]

    selected = next(
        (
            option
            for option in pending["options"]
            if option["value"] == request["values"][0]
        ),
        None,
    )
    if selected is None:
        return "clarification_value_not_offered"
    if request["evidence_refs"] != [selected["evidence_ref"]]:
        return "invalid_answer_evidence_refs"
    return None


class D4ClarificationBindingContractV2Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.request_v1 = _validator("online-request.v1.schema.json")
        cls.request_v2 = _validator("online-request.v2.schema.json")
        cls.response_v2 = _validator("online-response.v2.schema.json")
        cls.contract = _load(CONTRACT_PATH)
        cls.examples = _load(
            EXAMPLE_ROOT / "clarification-binding.v2.examples.json"
        )

    def assert_valid(self, validator: Draft202012Validator, value: dict) -> None:
        self.assertEqual([], list(validator.iter_errors(value)))

    def assert_invalid(self, validator: Draft202012Validator, value: dict) -> None:
        self.assertTrue(list(validator.iter_errors(value)))

    def test_v2_schemas_and_cross_file_refs_resolve(self) -> None:
        for name in (
            "shared-api-definitions.v2.schema.json",
            "online-request.v2.schema.json",
            "online-response.v2.schema.json",
        ):
            Draft202012Validator.check_schema(_load(SCHEMA_ROOT / name))
        self.assert_valid(
            self.request_v2,
            _load(EXAMPLE_ROOT / "online-query.v2.valid.json"),
        )
        self.assert_valid(
            self.response_v2,
            _load(EXAMPLE_ROOT / "online-clarification-required.v2.valid.json"),
        )

    def test_v1_stays_frozen_and_version_boundaries_are_strict(self) -> None:
        v1 = {
            "schema_version": "1.0.0",
            "operation": "submit_clarification",
            "session_id": "session.legacy",
            "request_id": "request.legacy",
            "turn_id": "turn.legacy",
            "state_revision": 1,
            "question_id": "question.legacy",
            "slot_id": "slot.entity",
            "values": ["artist"],
            "evidence_refs": [],
        }
        self.assert_valid(self.request_v1, v1)
        self.assert_invalid(self.request_v2, v1)
        v2 = self.examples["accepted_request"]
        self.assert_valid(self.request_v2, v2)
        self.assert_invalid(self.request_v1, v2)

    def test_v2_carries_complete_binding_offer_and_replay_identifier(self) -> None:
        pending = self.examples["pending_question"]
        request = self.examples["accepted_request"]
        self.assertEqual(
            self.contract["immutable_echo_fields"],
            [
                "session_id",
                "request_id",
                "turn_id",
                "state_revision",
                "question_id",
                "slot_id",
            ],
        )
        for field in self.contract["immutable_echo_fields"]:
            self.assertEqual(pending[field], request[field])
        self.assertIn("answer_id", request)
        self.assertEqual(1, len(request["values"]))
        self.assertEqual(1, len(request["evidence_refs"]))
        self.assertEqual(
            request["evidence_refs"][0], pending["options"][0]["evidence_ref"]
        )
        self.assert_valid(self.request_v2, request)
        self.assertIsNone(
            _reference_runtime_gate(
                pending=pending,
                request=request,
                contract=self.contract,
            )
        )

    def test_schema_rejects_empty_evidence_multiple_values_and_unknown_fields(self) -> None:
        base = self.examples["accepted_request"]
        for case in self.examples["schema_rejected_mutations"]:
            with self.subTest(case=case["case_id"]):
                request = deepcopy(base)
                request[case["field"]] = case["replacement"]
                self.assert_invalid(self.request_v2, request)

    def test_schema_rejects_answer_derived_fields(self) -> None:
        for field in ("expected", "gold", "label", "gold_sql"):
            with self.subTest(field=field):
                request = deepcopy(self.examples["accepted_request"])
                request[field] = "forbidden"
                self.assert_invalid(self.request_v2, request)

    def test_binding_and_selection_attacks_need_the_documented_runtime_gate(self) -> None:
        pending = self.examples["pending_question"]
        base = self.examples["accepted_request"]
        for case in self.examples["runtime_rejected_mutations"]:
            with self.subTest(case=case["case_id"]):
                request = deepcopy(base)
                request[case["field"]] = case["replacement"]
                self.assert_valid(self.request_v2, request)
                consumed = (
                    {request["answer_id"]}
                    if case["case_id"] == "answer_replayed"
                    else set()
                )
                self.assertEqual(
                    case["error_code"],
                    _reference_runtime_gate(
                        pending=pending,
                        request=request,
                        contract=self.contract,
                        consumed_answer_ids=consumed,
                    ),
                )

    def test_runtime_rules_are_explicitly_not_claimed_as_schema_proof(self) -> None:
        self.assertEqual("B", self.contract["runtime_gate_owner"])
        self.assertTrue(self.contract["runtime_gates"])
        self.assertTrue(
            all(not gate["schema_enforced"] for gate in self.contract["runtime_gates"])
        )
        self.assertEqual(
            {
                "clarification_session_mismatch",
                "clarification_request_mismatch",
                "clarification_turn_mismatch",
                "clarification_answer_stale",
                "clarification_question_mismatch",
                "clarification_cross_slot_update",
                "clarification_value_not_offered",
                "invalid_answer_evidence_refs",
                "clarification_answer_replayed",
            },
            {gate["error_code"] for gate in self.contract["runtime_gates"]},
        )


if __name__ == "__main__":
    unittest.main()
