from __future__ import annotations

import json
from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_ROOT = PROJECT_ROOT / "configs" / "schemas"


class D3ContractSchemaTest(unittest.TestCase):
    def load_schema(self, filename: str) -> dict[str, object]:
        return json.loads((SCHEMA_ROOT / filename).read_text(encoding="utf-8"))

    def test_public_schemas_are_strict_draft_2020_12_json_objects(self) -> None:
        for filename in (
            "prompt-contract.v1.schema.json",
            "task-result-envelope.v1.schema.json",
        ):
            with self.subTest(filename=filename):
                schema = self.load_schema(filename)
                self.assertEqual(
                    "https://json-schema.org/draft/2020-12/schema", schema["$schema"]
                )
                self.assertEqual("object", schema["type"])
                self.assertFalse(schema["additionalProperties"])
                self.assertEqual(len(schema["required"]), len(set(schema["required"])))

    def test_prompt_contract_locks_model_and_generation_mode(self) -> None:
        schema = self.load_schema("prompt-contract.v1.schema.json")
        properties = schema["properties"]
        self.assertEqual("qwen3.7-flash", properties["model"]["const"])
        generation = properties["generation"]
        self.assertFalse(generation["additionalProperties"])
        self.assertEqual(0, generation["properties"]["temperature"]["const"])
        self.assertFalse(
            generation["properties"]["enable_thinking"]["const"]
        )
        self.assertEqual(
            "json_object", generation["properties"]["response_format"]["const"]
        )
        self.assertEqual(
            1500, generation["properties"]["max_completion_tokens"]["maximum"]
        )

    def test_prompt_contract_uses_the_four_existing_task_types(self) -> None:
        schema = self.load_schema("prompt-contract.v1.schema.json")
        task_types = schema["properties"]["task_type"]["enum"]
        self.assertEqual(
            ["intent", "clarification", "nl2sql", "grounded_answer"], task_types
        )

        alignment_rules = schema["allOf"]
        self.assertEqual(4, len(alignment_rules))
        aligned_types = {
            rule["if"]["properties"]["task_type"]["const"]
            for rule in alignment_rules
        }
        self.assertEqual(set(task_types), aligned_types)
        for rule in alignment_rules:
            task_type = rule["if"]["properties"]["task_type"]["const"]
            path_pattern = rule["then"]["properties"]["prompt_path"]["pattern"]
            directory = "grounded-answer" if task_type == "grounded_answer" else task_type
            self.assertIn(f"prompts/{directory}/", path_pattern)

    def test_result_envelope_has_mutually_exclusive_success_and_error_shapes(self) -> None:
        schema = self.load_schema("task-result-envelope.v1.schema.json")
        success_rule, error_rule = schema["allOf"]
        self.assertEqual("success", success_rule["if"]["properties"]["status"]["const"])
        self.assertEqual(
            "object", success_rule["then"]["properties"]["data"]["type"]
        )
        self.assertEqual(
            "null", success_rule["then"]["properties"]["error"]["type"]
        )
        self.assertEqual("error", error_rule["if"]["properties"]["status"]["const"])
        self.assertEqual("null", error_rule["then"]["properties"]["data"]["type"])
        self.assertEqual(
            "#/$defs/task_error",
            error_rule["then"]["properties"]["error"]["$ref"],
        )

    def test_error_shape_is_bounded_and_has_no_raw_content_field(self) -> None:
        schema = self.load_schema("task-result-envelope.v1.schema.json")
        task_error = schema["$defs"]["task_error"]
        self.assertFalse(task_error["additionalProperties"])
        self.assertEqual(
            {"code", "message", "retryable", "field_errors"},
            set(task_error["required"]),
        )
        self.assertEqual(300, task_error["properties"]["message"]["maxLength"])
        forbidden = {"prompt", "response", "provider_error", "secret", "content"}
        self.assertTrue(forbidden.isdisjoint(task_error["properties"]))


if __name__ == "__main__":
    unittest.main()
