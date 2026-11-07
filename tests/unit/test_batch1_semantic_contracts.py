from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re
import unittest

from tools.evaluation.validate_batch1_semantic_contracts import (
    DomainPackRegistry,
    INVALID_EXAMPLE_PATH,
    REPORT_PATH,
    REQUEST_SCHEMA_PATH,
    RESULT_SCHEMA_PATH,
    SemanticContractError,
    VALID_EXAMPLE_PATH,
    build_report,
    run_invalid_mutations,
    validate_contract_pair,
)


ROOT = Path(__file__).resolve().parents[2]


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


class Batch1SemanticContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = read_json(VALID_EXAMPLE_PATH)
        cls.pack = DomainPackRegistry(ROOT / "domain-packs").load_path(
            ROOT / "domain-packs" / "chinook" / "v1"
        )

    def test_public_valid_pair_passes(self):
        validate_contract_pair(
            self.fixture["request"], self.fixture["result"], self.pack
        )

    def test_entity_type_is_extensible_and_not_an_enum(self):
        result = deepcopy(self.fixture["result"])
        result["fragments"][0]["entity_type"] = "new_domain.device_asset.v7"
        validate_contract_pair(self.fixture["request"], result, self.pack)
        schema = read_json(RESULT_SCHEMA_PATH)
        entity_schema = schema["$defs"]["fragment"]["properties"]["entity_type"]
        self.assertNotIn("enum", json.dumps(entity_schema))

    def test_external_required_slots_are_not_a_request_field(self):
        request = deepcopy(self.fixture["request"])
        request["required_slots"] = ["slot.time_range"]
        with self.assertRaisesRegex(
            SemanticContractError, "request_schema_invalid"
        ):
            validate_contract_pair(request, self.fixture["result"], self.pack)
        request_schema = read_json(REQUEST_SCHEMA_PATH)
        self.assertNotIn("required_slots", request_schema["properties"])

    def test_answer_bearing_field_is_rejected_before_use(self):
        request = deepcopy(self.fixture["request"])
        request["context"] = {"gold_sql": "SELECT 1"}
        with self.assertRaisesRegex(
            SemanticContractError, "forbidden_answer_field"
        ):
            validate_contract_pair(request, self.fixture["result"], self.pack)

    def test_all_public_invalid_mutations_fail_with_stable_codes(self):
        outcomes = run_invalid_mutations(self.fixture, self.pack)
        self.assertEqual(14, len(outcomes))
        self.assertEqual(
            len(outcomes), len({item["case_id"] for item in outcomes})
        )
        covered = {item["violation_code"] for item in outcomes}
        self.assertTrue(
            {
                "fragment_position_invalid",
                "edit_overlap",
                "unfounded_edit_replacement",
                "required_slot_resolution_mismatch",
                "evidence_ref_not_in_domain_pack",
                "forbidden_answer_field",
            }
            <= covered
        )

    def test_result_uses_unicode_half_open_coordinates(self):
        result = self.fixture["result"]
        self.assertEqual("unicode_code_point", result["text_coordinates"]["unit"])
        self.assertEqual("half_open", result["text_coordinates"]["range_semantics"])
        for fragment in result["fragments"]:
            self.assertEqual(
                fragment["text"],
                result["original_question"][fragment["start"] : fragment["end"]],
            )

    def test_report_is_deterministic_and_committed_copy_is_current(self):
        first = build_report()
        second = build_report()
        self.assertEqual(first, second)
        self.assertEqual(first, read_json(REPORT_PATH))
        self.assertEqual("pass", first["status"])
        self.assertEqual(0, first["boundaries"]["model_api_calls"])
        self.assertEqual(0, first["boundaries"]["hidden_test_cases_read"])

    def test_invalid_fixture_is_public_mutation_metadata_only(self):
        fixture = read_json(INVALID_EXAMPLE_PATH)
        self.assertEqual(
            "semantic-contract-invalid-mutations.v1", fixture["fixture_version"]
        )
        self.assertTrue(all("violation_code" in case for case in fixture["cases"]))
        self.assertFalse(any("expected" in case for case in fixture["cases"]))

    def test_task_document_local_links_resolve(self):
        document = ROOT / "docs" / "tasks" / "BATCH1-A-公共语义契约v1.md"
        text = document.read_text(encoding="utf-8")
        links = [item for item in re.findall(r"\[[^]]+\]\(([^)]+)\)", text) if "://" not in item]
        self.assertGreaterEqual(len(links), 8)
        for link in links:
            self.assertTrue((document.parent / link).resolve().is_file(), link)


if __name__ == "__main__":
    unittest.main()
