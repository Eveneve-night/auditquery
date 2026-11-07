from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re
import sys
import unittest

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from referencing import Registry, Resource


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from backend.agent.task_contracts import (  # noqa: E402
    ContractValidationError,
    build_task_payload,
    validate_task_result,
)


PROMPT_SCHEMA_PATH = PROJECT_ROOT / "configs" / "schemas" / "prompt-contract.v1.schema.json"
ENVELOPE_SCHEMA_PATH = PROJECT_ROOT / "configs" / "schemas" / "task-result-envelope.v1.schema.json"
CANDIDATES_PATH = (
    PROJECT_ROOT
    / "tests"
    / "goldens"
    / "d3-smoke-candidates"
    / "v1"
    / "d3-01-intent-clarification.json"
)
SAMPLES_PATH = (
    PROJECT_ROOT
    / "tests"
    / "fixtures"
    / "d3-contracts"
    / "v1"
    / "task-result-samples.json"
)


def load_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_jsonl(path: Path) -> dict[str, dict[str, object]]:
    return {
        item["id"]: item
        for item in (
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    }


class D3IntentClarificationContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.prompt_schema = load_json(PROMPT_SCHEMA_PATH)
        self.envelope_schema = load_json(ENVELOPE_SCHEMA_PATH)
        envelope_resource = Resource.from_contents(self.envelope_schema)
        self.registry = Registry().with_resource(
            self.envelope_schema["$id"], envelope_resource
        )

    def schema_validator(self, task_type: str) -> Draft202012Validator:
        filename = (
            "intent-result.v1.schema.json"
            if task_type == "intent"
            else "clarification-result.v1.schema.json"
        )
        schema = load_json(PROJECT_ROOT / "configs" / "schemas" / "tasks" / filename)
        Draft202012Validator.check_schema(schema)
        return Draft202012Validator(schema, registry=self.registry)

    def assert_contract_matches_public_schema(self, task: str) -> dict[str, object]:
        contract_path = PROJECT_ROOT / "configs" / "prompts" / task / "v1" / "contract.json"
        contract = load_json(contract_path)
        public_required = set(self.prompt_schema["required"])
        self.assertTrue(public_required.issubset(contract))
        self.assertTrue(set(contract).issubset(self.prompt_schema["properties"]))

        for key in (
            "contract_id",
            "contract_version",
            "prompt_version",
            "prompt_path",
            "output_schema_id",
            "output_schema_path",
        ):
            pattern = self.prompt_schema["properties"][key]["pattern"]
            self.assertIsNotNone(re.fullmatch(pattern, contract[key]), key)

        self.assertEqual("qwen3.7-flash", contract["model"])
        generation = contract["generation"]
        self.assertEqual(0, generation["temperature"])
        self.assertFalse(generation["enable_thinking"])
        self.assertEqual("json_object", generation["response_format"])
        self.assertLessEqual(generation["max_completion_tokens"], 1500)
        self.assertTrue((PROJECT_ROOT / contract["prompt_path"]).is_file())
        self.assertTrue((PROJECT_ROOT / contract["output_schema_path"]).is_file())
        return contract

    def test_contracts_match_public_metadata_and_are_cross_linked(self) -> None:
        cases = (("intent", "intent"), ("clarification", "clarification"))
        for directory, task_type in cases:
            with self.subTest(task_type=task_type):
                contract = self.assert_contract_matches_public_schema(directory)
                self.assertEqual(task_type, contract["task_type"])
                result_schema = load_json(PROJECT_ROOT / contract["output_schema_path"])
                self.assertEqual(contract["output_schema_id"], result_schema["$id"])
                self.assertEqual(
                    "task-result-envelope.v1",
                    result_schema["allOf"][0]["$ref"],
                )

    def test_task_schemas_are_strict_and_encode_required_invariants(self) -> None:
        intent = load_json(
            PROJECT_ROOT / "configs" / "schemas" / "tasks" / "intent-result.v1.schema.json"
        )
        intent_data = intent["$defs"]["intent_data"]
        self.assertFalse(intent_data["additionalProperties"])
        self.assertEqual(
            {"sql", "rag", "cross_source", "unknown"},
            set(intent_data["properties"]["intent"]["enum"]),
        )
        self.assertEqual(6, len(intent_data["allOf"]))

        clarification = load_json(
            PROJECT_ROOT
            / "configs"
            / "schemas"
            / "tasks"
            / "clarification-result.v1.schema.json"
        )
        clarification_data = clarification["$defs"]["clarification_data"]
        self.assertFalse(clarification_data["additionalProperties"])
        decision = clarification_data["allOf"][0]
        self.assertEqual(1, decision["then"]["properties"]["missing_slots"]["minItems"])
        self.assertEqual(0, decision["else"]["properties"]["missing_slots"]["maxItems"])
        self.assertEqual("null", decision["else"]["properties"]["question"]["type"])

    def test_prompts_enforce_scope_and_json_only_output(self) -> None:
        intent_prompt = (
            PROJECT_ROOT / "configs" / "prompts" / "intent" / "v1" / "system.md"
        ).read_text(encoding="utf-8")
        clarification_prompt = (
            PROJECT_ROOT / "configs" / "prompts" / "clarification" / "v1" / "system.md"
        ).read_text(encoding="utf-8")

        self.assertIn("只返回一个符合 `intent-result.v1` 的 JSON 对象", intent_prompt)
        self.assertIn("不生成或执行 SQL", intent_prompt)
        self.assertIn("不得因为可选细节缺失而过度澄清", intent_prompt)
        self.assertIn('"schema_version": "1.0.0"', intent_prompt)
        self.assertIn('"required_sources": ["source_id"]', intent_prompt)
        self.assertIn(
            "只返回一个符合 `clarification-result.v1` 的 JSON 对象",
            clarification_prompt,
        )
        self.assertIn("每轮最多提出一个", clarification_prompt)
        self.assertIn("不得为了可选信息继续追问", clarification_prompt)
        self.assertIn('"primary_slot": "slot_name"', clarification_prompt)
        self.assertIn('"primary_slot": null', clarification_prompt)
        for case_id in ("INT-001", "CLR-001"):
            self.assertNotIn(case_id, intent_prompt)
            self.assertNotIn(case_id, clarification_prompt)

    def test_candidate_lists_use_exactly_five_prompt_dev_cases_per_task(self) -> None:
        candidates = load_json(CANDIDATES_PATH)
        self.assertEqual("prompt_dev", candidates["source_split"])
        selections = candidates["selections"]
        self.assertEqual({"intent", "clarification"}, set(selections))

        selected_cases: dict[str, list[dict[str, object]]] = {}
        for task_type, selection in selections.items():
            with self.subTest(task_type=task_type):
                case_ids = selection["case_ids"]
                self.assertEqual(5, len(case_ids))
                self.assertEqual(5, len(set(case_ids)))
                source = load_jsonl(PROJECT_ROOT / selection["source_path"])
                cases = [source[case_id] for case_id in case_ids]
                self.assertTrue(all(case["split"] == "prompt_dev" for case in cases))
                self.assertTrue(all(case["task_type"] == task_type for case in cases))
                contexts = selection["payload_context"]
                self.assertEqual(set(case_ids), set(contexts))
                payloads = [
                    build_task_payload(case, payload_context=contexts[case["id"]])
                    for case in cases
                ]
                expected_keys = (
                    {"query", "conversation", "available_sources"}
                    if task_type == "intent"
                    else {"query", "known_slots", "required_slots"}
                )
                self.assertTrue(all(set(payload) == expected_keys for payload in payloads))
                selected_cases[task_type] = cases

        intent_labels = {
            case["expected"]["intent"] for case in selected_cases["intent"]
        }
        self.assertEqual({"sql", "rag", "cross_source", "unknown"}, intent_labels)
        clarification_labels = {
            case["expected"]["should_clarify"]
            for case in selected_cases["clarification"]
        }
        self.assertEqual({True, False}, clarification_labels)

    def test_intent_candidates_use_one_fixed_catalog_with_distractors(self) -> None:
        candidates = load_json(CANDIDATES_PATH)
        selection = candidates["selections"]["intent"]
        catalog = selection["source_catalog"]
        self.assertEqual(6, len(catalog))
        self.assertEqual(len(catalog), len(set(catalog)))
        self.assertGreaterEqual(len(catalog), 4, "固定目录必须包含合理干扰项")
        for case_id in selection["case_ids"]:
            self.assertEqual(
                catalog,
                selection["payload_context"][case_id]["available_sources"],
                f"{case_id} 不得使用按金标答案人工挑选的数据源目录",
            )

    def test_payload_builder_never_reads_expected_answers(self) -> None:
        case = {
            "task_type": "intent",
            "input": {"query": "统计客户总数。", "conversation": []},
            "context": {"available_sources": ["chinook_sql"]},
            "expected": {"sentinel": "EXPECTED_ANSWER_MUST_NOT_LEAK"},
        }

        payload = build_task_payload(
            case,
            payload_context={
                "available_sources": ["chinook_sql"],
                "ignored_expected": "EXPECTED_ANSWER_MUST_NOT_LEAK",
            },
        )

        self.assertEqual(
            {"query", "conversation", "available_sources"}, set(payload)
        )
        self.assertNotIn("EXPECTED_ANSWER_MUST_NOT_LEAK", json.dumps(payload))

    def test_deterministic_validator_accepts_success_and_error_samples(self) -> None:
        samples = load_json(SAMPLES_PATH)
        self.assertEqual(4, len(samples))
        for name, sample in samples.items():
            with self.subTest(sample=name):
                self.schema_validator(sample["task_type"]).validate(sample["result"])
                validate_task_result(
                    sample["task_type"], sample["payload"], sample["result"]
                )

    def test_draft_2020_12_validator_rejects_schema_violations(self) -> None:
        samples = load_json(SAMPLES_PATH)
        intent_sample = samples["intent_success"]
        intent_validator = self.schema_validator("intent")

        invalid_intent_results = []
        false_ambiguous = deepcopy(intent_sample["result"])
        false_ambiguous["data"]["reason"] = "ambiguous"
        invalid_intent_results.append(false_ambiguous)
        unknown_routable = deepcopy(intent_sample["result"])
        unknown_routable["data"].update(
            {"intent": "unknown", "required_sources": [], "reason": "routable"}
        )
        invalid_intent_results.append(unknown_routable)
        extra_field = deepcopy(intent_sample["result"])
        extra_field["data"]["explanation"] = "not allowed"
        invalid_intent_results.append(extra_field)
        missing_field = deepcopy(intent_sample["result"])
        del missing_field["data"]["normalized_query"]
        invalid_intent_results.append(missing_field)

        for result in invalid_intent_results:
            with self.subTest(result=result["data"]):
                with self.assertRaises(ValidationError):
                    intent_validator.validate(result)

        clarification_sample = samples["clarification_required"]
        clarification_validator = self.schema_validator("clarification")
        invalid_clarification = deepcopy(clarification_sample["result"])
        invalid_clarification["data"].update(
            {
                "should_clarify": False,
                "primary_slot": "metric",
                "missing_slots": ["metric"],
                "question": "请补充指标。",
                "reason": "complete",
            }
        )
        with self.assertRaises(ValidationError):
            clarification_validator.validate(invalid_clarification)

    def test_intent_validator_rejects_schema_and_cross_field_violations(self) -> None:
        sample = load_json(SAMPLES_PATH)["intent_success"]
        invalid_results: list[tuple[str, dict[str, object]]] = []

        false_ambiguous = deepcopy(sample["result"])
        false_ambiguous["data"]["reason"] = "ambiguous"
        invalid_results.append(("known intent", false_ambiguous))

        unknown_routable = deepcopy(sample["result"])
        unknown_routable["data"].update(
            {"intent": "unknown", "required_sources": [], "reason": "routable"}
        )
        invalid_results.append(("unknown intent", unknown_routable))

        unavailable_source = deepcopy(sample["result"])
        unavailable_source["data"]["required_sources"] = ["unlisted_source"]
        invalid_results.append(("subset", unavailable_source))

        extra_field = deepcopy(sample["result"])
        extra_field["data"]["explanation"] = "not allowed"
        invalid_results.append(("fields mismatch", extra_field))

        missing_field = deepcopy(sample["result"])
        del missing_field["data"]["normalized_query"]
        invalid_results.append(("fields mismatch", missing_field))

        for message, result in invalid_results:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ContractValidationError, message):
                    validate_task_result("intent", sample["payload"], result)

    def test_clarification_validator_rejects_semantic_violations(self) -> None:
        sample = load_json(SAMPLES_PATH)["clarification_required"]

        primary_not_missing = deepcopy(sample["result"])
        primary_not_missing["data"]["primary_slot"] = "other_slot"
        with self.assertRaisesRegex(ContractValidationError, "primary_slot"):
            validate_task_result(
                "clarification", sample["payload"], primary_not_missing
            )

        missing_does_not_match = deepcopy(sample["result"])
        missing_does_not_match["data"]["missing_slots"] = ["metric"]
        with self.assertRaisesRegex(ContractValidationError, "reported missing slots"):
            validate_task_result(
                "clarification", sample["payload"], missing_does_not_match
            )

        complete = load_json(SAMPLES_PATH)["clarification_complete"]
        skipped_clarification = deepcopy(complete["result"])
        incomplete_payload = deepcopy(complete["payload"])
        del incomplete_payload["known_slots"]["country"]
        with self.assertRaisesRegex(ContractValidationError, "cannot skip"):
            validate_task_result(
                "clarification", incomplete_payload, skipped_clarification
            )

    def test_error_and_root_shapes_reject_extra_or_missing_fields(self) -> None:
        sample = load_json(SAMPLES_PATH)["intent_error"]
        error_with_content = deepcopy(sample["result"])
        error_with_content["error"]["content"] = "raw provider response"
        with self.assertRaisesRegex(ContractValidationError, "fields mismatch"):
            validate_task_result("intent", sample["payload"], error_with_content)

        extra_root = deepcopy(sample["result"])
        extra_root["trace"] = []
        with self.assertRaisesRegex(ContractValidationError, "fields mismatch"):
            validate_task_result("intent", sample["payload"], extra_root)

    def test_public_error_envelope_does_not_allow_content_payloads(self) -> None:
        error_properties = self.envelope_schema["$defs"]["task_error"]["properties"]
        forbidden = {"prompt", "response", "provider_error", "secret", "content"}
        self.assertTrue(forbidden.isdisjoint(error_properties))


if __name__ == "__main__":
    unittest.main()
