from __future__ import annotations

from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from jsonschema import ValidationError


PROJECT_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = PROJECT_ROOT / "tools" / "evaluation" / "validate_d4_evaluation_freeze.py"
SPEC = importlib.util.spec_from_file_location("d4_evaluation_freeze", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class D4EvaluationFreezeTest(unittest.TestCase):
    def _temporary_pack(self) -> tuple[tempfile.TemporaryDirectory[str], Path]:
        directory = tempfile.TemporaryDirectory()
        target = Path(directory.name) / "pack"
        shutil.copytree(MODULE.DOMAIN_PACK_PATH, target)
        return directory, target

    @staticmethod
    def _read(path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _write(path: Path, value: dict) -> None:
        path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def _refresh_manifest_hash(self, pack: Path, role: str) -> None:
        manifest_path = pack / "manifest.json"
        manifest = self._read(manifest_path)
        entry = next(item for item in manifest["files"] if item["role"] == role)
        entry["sha256"] = MODULE.sha256(pack / entry["path"])
        self._write(manifest_path, manifest)

    def test_full_offline_validation_passes_with_zero_external_activity(self) -> None:
        report = MODULE.validate_all()
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["schema_count"], 6)
        self.assertEqual(report["network_calls"], 0)
        self.assertEqual(report["model_api_calls"], 0)
        self.assertEqual(report["estimated_cost_cny"], 0.0)
        self.assertFalse(report["hidden_test_content_committed"])
        self.assertFalse(report["runtime_implemented"])
        self.assertFalse(report["innovation_proven"])
        self.assertFalse(report["generalization_proven"])

    def test_d4_text_hash_is_stable_across_lf_and_crlf(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lf_path = Path(directory) / "lf.json"
            crlf_path = Path(directory) / "crlf.json"
            lf_path.write_bytes(b'{"value":"TESTONLY"}\n')
            crlf_path.write_bytes(b'{"value":"TESTONLY"}\r\n')
            self.assertEqual(MODULE.sha256(lf_path), MODULE.sha256(crlf_path))
            self.assertEqual(MODULE.sha256_raw(lf_path), MODULE.sha256_raw(crlf_path))

    def test_typed_decision_schema_rejects_unknown_field(self) -> None:
        fixture = MODULE.read_json(MODULE.DECISION_SAMPLES_PATH)
        sample = deepcopy(fixture["samples"][0])
        sample["unexpected_field"] = "must fail closed"
        with self.assertRaises(ValidationError):
            MODULE.validate_instance(
                sample,
                MODULE.SCHEMA_ROOT / "typed-decision.v1.schema.json",
            )

    def test_typed_decision_rejects_selection_outside_candidates(self) -> None:
        fixture = MODULE.read_json(MODULE.DECISION_SAMPLES_PATH)
        sample = deepcopy(fixture["samples"][0])
        sample["selected_option"] = "candidate.missing"
        with self.assertRaisesRegex(MODULE.D4ValidationError, "reference a candidate"):
            MODULE.validate_typed_decision(sample)

    def test_answer_derived_fields_are_rejected_at_any_depth(self) -> None:
        for field in ("expected_intent", "gold_sql", "label", "target_answer"):
            with self.subTest(field=field):
                with self.assertRaises(MODULE.D4ValidationError):
                    MODULE.assert_no_answer_fields({"nested": [{field: "TESTONLY"}]})

    def test_domain_pack_hash_mismatch_is_rejected(self) -> None:
        directory, pack = self._temporary_pack()
        try:
            aliases = self._read(pack / "aliases.json")
            aliases["items"][0]["alias"] = "TESTONLY changed alias"
            self._write(pack / "aliases.json", aliases)
            with self.assertRaisesRegex(MODULE.D4ValidationError, "hash mismatch"):
                MODULE.validate_domain_pack(pack)
        finally:
            directory.cleanup()

    def test_domain_pack_schema_rejects_unknown_fields(self) -> None:
        manifest = MODULE.read_json(MODULE.DOMAIN_PACK_PATH / "manifest.json")
        manifest["unknown_field"] = "must fail closed"
        with self.assertRaises(ValidationError):
            MODULE.validate_instance(
                manifest,
                MODULE.SCHEMA_ROOT / "domain-pack-manifest.v1.schema.json",
            )

    def test_domain_pack_dangling_reference_is_rejected(self) -> None:
        directory, pack = self._temporary_pack()
        try:
            aliases = self._read(pack / "aliases.json")
            aliases["items"][0]["concept_id"] = "concept.missing"
            self._write(pack / "aliases.json", aliases)
            self._refresh_manifest_hash(pack, "aliases")
            with self.assertRaisesRegex(MODULE.D4ValidationError, "dangling reference"):
                MODULE.validate_domain_pack(pack)
        finally:
            directory.cleanup()

    def test_domain_pack_inheritance_cycle_is_rejected(self) -> None:
        directory, pack = self._temporary_pack()
        try:
            concepts = self._read(pack / "concepts.json")
            first, second = concepts["items"]
            first["parent_concept_ids"] = [second["concept_id"]]
            second["parent_concept_ids"] = [first["concept_id"]]
            self._write(pack / "concepts.json", concepts)
            self._refresh_manifest_hash(pack, "concepts")
            with self.assertRaisesRegex(MODULE.D4ValidationError, "inheritance cycle"):
                MODULE.validate_domain_pack(pack)
        finally:
            directory.cleanup()

    def test_protocol_freezes_groups_gates_exposure_and_model_boundary(self) -> None:
        report = MODULE.validate_experiment_protocol()
        self.assertEqual(
            report["group_ids"],
            ["B0", "B1", "B2", "P", "P-uncalibrated"],
        )
        self.assertEqual(report["competition_candidate_gate"], 180)
        self.assertEqual(report["innovation_claim_gate"], 240)
        self.assertEqual(report["validation_historical_exposure_count"], 0)
        self.assertEqual(report["validation_current_task_exposure_count"], 0)
        self.assertEqual(report["protected_d3_asset_count"], 14)
        boundary = report["provider_boundary"]
        self.assertEqual(boundary["provider_config_count"], 1)
        self.assertEqual(boundary["model"], "qwen3.7-flash")
        self.assertFalse(boundary["fallback_enabled"])
        self.assertFalse(boundary["jev_api_enabled"])

    def test_protocol_rejects_answer_fields_in_model_input_allowlist(self) -> None:
        protocol = MODULE.read_json(MODULE.PROTOCOL_PATH)
        for field in ("expected", "gold", "label"):
            with self.subTest(field=field):
                candidate = deepcopy(protocol)
                candidate["shared_controls"]["model_input_policy"]["allowlist"].append(field)
                with self.assertRaises(ValidationError):
                    MODULE.validate_instance(
                        candidate,
                        MODULE.SCHEMA_ROOT / "experiment-protocol.v1.schema.json",
                    )

    def test_public_data_manifest_contains_no_cases_or_answers(self) -> None:
        manifest = MODULE.read_json(MODULE.DATA_MANIFEST_PATH)
        serialized = json.dumps(manifest, ensure_ascii=False).lower()
        self.assertFalse(manifest["contains_case_content"])
        self.assertNotIn("cases", manifest)
        self.assertNotIn("question", serialized)
        self.assertNotIn("gold_sql", serialized)
        MODULE.assert_no_answer_fields(manifest)


if __name__ == "__main__":
    unittest.main()
