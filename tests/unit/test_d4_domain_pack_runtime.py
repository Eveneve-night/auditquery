from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from src.backend.agent import (
    CandidateEvidence,
    CandidateRetriever,
    DomainPackError,
    DomainPackRegistry,
    EvidenceFeatureAssembler,
    TypedDecisionRuntimeError,
    canonical_text_sha256,
)
from tools.evaluation.validate_d4_domain_pack_runtime import validate_all


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE_PACK = PROJECT_ROOT / "domain-packs" / "chinook" / "v1"


class MutablePackCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.pack_root = Path(self.temp.name) / "domain-packs"
        self.pack_path = self.pack_root / "chinook" / "v1"
        shutil.copytree(SOURCE_PACK, self.pack_path)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def registry(self) -> DomainPackRegistry:
        return DomainPackRegistry(self.pack_root)

    def read_json(self, name: str) -> dict:
        return json.loads((self.pack_path / name).read_text(encoding="utf-8"))

    def write_json(self, name: str, value: dict) -> None:
        (self.pack_path / name).write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    def refresh_hash(self, role: str) -> None:
        manifest = self.read_json("manifest.json")
        entry = next(item for item in manifest["files"] if item["role"] == role)
        entry["sha256"] = canonical_text_sha256(self.pack_path / entry["path"])
        self.write_json("manifest.json", manifest)

    def assert_error(self, code: str, callback) -> None:
        with self.assertRaises(DomainPackError) as caught:
            callback()
        self.assertEqual(code, caught.exception.code)


class DomainPackRegistryTests(MutablePackCase):
    def test_discovers_and_loads_chinook_pack(self) -> None:
        registry = self.registry()
        self.assertEqual((("chinook_music_store", "1.0.0"),), registry.discover())
        pack = registry.load("chinook_music_store")
        self.assertEqual("1.0.0", pack.version)
        self.assertEqual(12, len(pack.artifact_items("concepts")))
        self.assertEqual(76, len(pack.artifact_items("schema_catalog")))
        self.assertEqual(
            "26906fd809480a18274ef3e07558e52b2f55857299685f63facdcdafe2efb3a0",
            pack.manifest_sha256,
        )

    def test_hash_drift_fails_closed(self) -> None:
        path = self.pack_path / "aliases.json"
        path.write_text(path.read_text(encoding="utf-8") + " ", encoding="utf-8")
        self.assert_error(
            "hash_mismatch",
            lambda: self.registry().load("chinook_music_store", "1.0.0"),
        )

    def test_missing_and_extra_files_fail_closed(self) -> None:
        (self.pack_path / "aliases.json").unlink()
        self.assert_error(
            "unexpected_file_set",
            lambda: self.registry().load("chinook_music_store", "1.0.0"),
        )
        shutil.copy2(SOURCE_PACK / "aliases.json", self.pack_path / "aliases.json")
        (self.pack_path / "extra.json").write_text("{}\n", encoding="utf-8")
        self.assert_error(
            "unexpected_file_set",
            lambda: self.registry().load("chinook_music_store", "1.0.0"),
        )

    def test_unknown_field_is_rejected_by_schema(self) -> None:
        concepts = self.read_json("concepts.json")
        concepts["items"][0]["unknown_runtime_hint"] = True
        self.write_json("concepts.json", concepts)
        self.refresh_hash("concepts")
        self.assert_error(
            "schema_validation_failed",
            lambda: self.registry().load("chinook_music_store", "1.0.0"),
        )

    def test_duplicate_id_across_artifacts_is_rejected(self) -> None:
        aliases = self.read_json("aliases.json")
        aliases["items"][0]["alias_id"] = "concept.album"
        self.write_json("aliases.json", aliases)
        self.refresh_hash("aliases")
        self.assert_error(
            "duplicate_id",
            lambda: self.registry().load("chinook_music_store", "1.0.0"),
        )

    def test_dangling_reference_is_rejected(self) -> None:
        aliases = self.read_json("aliases.json")
        aliases["items"][0]["concept_id"] = "concept.not_present"
        self.write_json("aliases.json", aliases)
        self.refresh_hash("aliases")
        self.assert_error(
            "dangling_reference",
            lambda: self.registry().load("chinook_music_store", "1.0.0"),
        )

    def test_concept_inheritance_cycle_is_rejected(self) -> None:
        concepts = self.read_json("concepts.json")
        by_id = {item["concept_id"]: item for item in concepts["items"]}
        by_id["concept.music_store"]["parent_concept_ids"] = ["concept.album"]
        self.write_json("concepts.json", concepts)
        self.refresh_hash("concepts")
        self.assert_error(
            "inheritance_cycle",
            lambda: self.registry().load("chinook_music_store", "1.0.0"),
        )

    def test_schema_parent_type_mismatch_is_rejected(self) -> None:
        catalog = self.read_json("schema-catalog.json")
        album = next(
            item for item in catalog["items"] if item["object_id"] == "schema.chinook.album"
        )
        album["parent_object_id"] = "schema.chinook.artist"
        self.write_json("schema-catalog.json", catalog)
        self.refresh_hash("schema_catalog")
        self.assert_error(
            "schema_parent_type_mismatch",
            lambda: self.registry().load("chinook_music_store", "1.0.0"),
        )

    def test_pack_and_manifest_paths_cannot_escape_trusted_root(self) -> None:
        outside = Path(self.temp.name) / "outside"
        shutil.copytree(SOURCE_PACK, outside)
        self.assert_error("pack_path_escape", lambda: self.registry().load_path(outside))

        manifest = self.read_json("manifest.json")
        manifest["files"][0]["path"] = "../concepts.json"
        self.write_json("manifest.json", manifest)
        self.assert_error(
            "schema_validation_failed",
            lambda: self.registry().load("chinook_music_store", "1.0.0"),
        )

    def test_answer_derived_fields_are_rejected_before_schema_details(self) -> None:
        concepts = self.read_json("concepts.json")
        concepts["items"][0]["expected_answer"] = "forbidden"
        self.write_json("concepts.json", concepts)
        self.refresh_hash("concepts")
        self.assert_error(
            "forbidden_answer_field",
            lambda: self.registry().load("chinook_music_store", "1.0.0"),
        )

    def test_l2_or_l3_content_cannot_be_marked_for_model(self) -> None:
        manifest = self.read_json("manifest.json")
        manifest["data_level"] = "L2"
        concept_entry = next(
            item for item in manifest["files"] if item["role"] == "concepts"
        )
        concept_entry["data_level"] = "L2"
        concept_entry["send_to_model"] = True
        self.write_json("manifest.json", manifest)
        self.assert_error(
            "model_data_level_not_allowed",
            lambda: self.registry().load("chinook_music_store", "1.0.0"),
        )

    def test_model_context_applies_file_and_item_policy(self) -> None:
        manifest = self.read_json("manifest.json")
        alias_entry = next(
            item for item in manifest["files"] if item["role"] == "aliases"
        )
        alias_entry["send_to_model"] = False
        self.write_json("manifest.json", manifest)

        evidence = self.read_json("value-evidence.json")
        evidence["items"][0]["send_to_model"] = False
        self.write_json("value-evidence.json", evidence)
        self.refresh_hash("value_evidence")

        pack = self.registry().load("chinook_music_store", "1.0.0")
        context = pack.model_context(max_data_level="L1")
        self.assertNotIn("aliases", context["artifacts"])
        self.assertNotIn("license_notice", context["artifacts"])
        evidence_ids = {
            item["evidence_id"]
            for item in context["artifacts"]["value_evidence"]["items"]
        }
        self.assertNotIn("evidence.metric_kinds", evidence_ids)
        self.assert_error(
            "model_data_level_not_allowed",
            lambda: pack.model_context(max_data_level="L2"),
        )

    def test_loaded_data_is_immutable(self) -> None:
        pack = self.registry().load("chinook_music_store", "1.0.0")
        with self.assertRaises(TypeError):
            pack.manifest["version"] = "9.9.9"  # type: ignore[index]


class CandidateRetrieverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.pack = DomainPackRegistry().load("chinook_music_store", "1.0.0")
        cls.retriever = CandidateRetriever(cls.pack)

    def test_alias_and_schema_evidence_produce_real_candidates(self) -> None:
        concepts = self.retriever.retrieve(
            "artist album", top_k=5, candidate_kind="concept"
        )
        self.assertIn("concept.artist", {item.candidate_id for item in concepts})
        self.assertIn("concept.album", {item.candidate_id for item in concepts})
        for candidate in concepts:
            self.pack.resolve(candidate.candidate_id)
            for evidence_ref in candidate.evidence_refs:
                self.pack.resolve(evidence_ref)

    def test_specific_alias_and_schema_corroboration_beat_generic_quantity(self) -> None:
        cases = (
            ("统计曲目数量", "concept.track", "schema.chinook.track"),
            ("统计媒体类型数量", "concept.mediatype", "schema.chinook.mediatype"),
            ("统计发票数量", "concept.invoice", "schema.chinook.invoice"),
        )
        for query, expected_id, schema_ref in cases:
            with self.subTest(query=query):
                candidates = self.retriever.retrieve(query, top_k=10)
                self.assertEqual(expected_id, candidates[0].candidate_id)
                self.assertEqual(0.85, candidates[0].raw_score)
                self.assertEqual(
                    1,
                    candidates[0].raw_features["alias_schema_corroboration"],
                )
                self.assertIn(schema_ref, candidates[0].evidence_refs)
                quantity = next(
                    item
                    for item in candidates
                    if item.candidate_id == "schema.chinook.invoiceline.quantity"
                )
                self.assertEqual(0.4, quantity.raw_score)

        media_candidates = self.retriever.retrieve("统计媒体类型数量", top_k=10)
        self.assertNotIn(
            "concept.genre", {item.candidate_id for item in media_candidates}
        )
        distinct_mentions = self.retriever.retrieve(
            "分别统计类型和媒体类型数量", top_k=10
        )
        self.assertIn(
            "concept.genre", {item.candidate_id for item in distinct_mentions}
        )

    def test_schema_relation_features_are_auditable(self) -> None:
        candidates = self.retriever.retrieve(
            "Album ArtistId Artist", top_k=20, candidate_kind="schema_object"
        )
        self.assertTrue(candidates)
        relation_refs = {
            reference
            for candidate in candidates
            for reference in candidate.evidence_refs
            if reference.startswith("relation.")
        }
        self.assertTrue(relation_refs)
        for reference in relation_refs:
            self.assertEqual("relations", self.pack.resolve_role(reference))

    def test_value_evidence_and_rules_are_used_for_slot_candidates(self) -> None:
        candidates = self.retriever.retrieve(
            "revenue", top_k=5, candidate_kind="slot"
        )
        metric = next(item for item in candidates if item.candidate_id == "slot.metric")
        self.assertGreater(metric.raw_features["value_evidence_hit_count"], 0)
        self.assertTrue(any(ref.startswith("evidence.") for ref in metric.evidence_refs))
        self.assertTrue(any(ref.startswith("rule.") for ref in metric.evidence_refs))

    def test_top_k_is_bounded_and_repeatable(self) -> None:
        first = self.retriever.retrieve("Name", top_k=7, candidate_kind="schema_object")
        second = self.retriever.retrieve("Name", top_k=7, candidate_kind="schema_object")
        self.assertEqual(first, second)
        self.assertLessEqual(len(first), 7)
        with self.assertRaises(TypedDecisionRuntimeError) as caught:
            self.retriever.retrieve("Name", top_k=51)
        self.assertEqual("invalid_top_k", caught.exception.code)

    def test_ties_have_stable_candidate_id_order(self) -> None:
        candidates = self.retriever.retrieve(
            "Name", top_k=50, candidate_kind="schema_object"
        )
        by_score: dict[float, list[str]] = defaultdict(list)
        for candidate in candidates:
            by_score[candidate.raw_score].append(candidate.candidate_id)
        tied_groups = [ids for ids in by_score.values() if len(ids) > 1]
        self.assertTrue(tied_groups)
        for ids in tied_groups:
            self.assertEqual(sorted(ids), ids)

    def test_no_match_does_not_force_a_candidate(self) -> None:
        self.assertEqual(
            (),
            self.retriever.retrieve(
                "zzzxqv_not_a_domain_term", top_k=10, candidate_kind="all"
            ),
        )


class EvidenceFeatureAssemblerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.pack = DomainPackRegistry().load("chinook_music_store", "1.0.0")
        cls.retriever = CandidateRetriever(cls.pack)
        cls.assembler = EvidenceFeatureAssembler(cls.pack)
        cls.concepts = cls.retriever.retrieve(
            "artist album", top_k=2, candidate_kind="concept"
        )

    def test_choice_boolean_score_and_abstain_contracts(self) -> None:
        choice = self.assembler.assemble_decision(
            decision_type="choice",
            candidates=self.concepts,
            selected_option=self.concepts[0].candidate_id,
            raw_score=self.concepts[0].raw_score,
            recommended_action="accept",
        )
        boolean = self.assembler.assemble_decision(
            decision_type="boolean",
            candidates=self.concepts,
            selected_option=self.concepts[0].candidate_id,
            raw_score=0.5,
            recommended_action="accept",
        )
        score = self.assembler.assemble_decision(
            decision_type="score",
            candidates=self.concepts,
            selected_option=None,
            raw_score=0.5,
            recommended_action="escalate",
        )
        abstain = self.assembler.assemble_decision(
            decision_type="abstain",
            candidates=(),
            selected_option=None,
            raw_score=None,
            recommended_action="reject",
            abstain_reason="insufficient_evidence",
        )
        self.assertEqual(
            {"choice", "boolean", "score", "abstain"},
            {item["decision_type"] for item in (choice, boolean, score, abstain)},
        )
        self.assertTrue(all(item["calibration_version"] is None for item in (choice, boolean, score, abstain)))

    def test_candidate_and_evidence_must_resolve_to_pack(self) -> None:
        fabricated = CandidateEvidence(
            candidate_id="concept.fabricated",
            display_name="fabricated",
            candidate_kind="concept",
            raw_score=0.1,
            evidence_refs=(),
            raw_features={"candidate_kind": "concept"},
        )
        with self.assertRaises(TypedDecisionRuntimeError) as caught:
            self.assembler.candidate_options((fabricated,))
        self.assertEqual("candidate_not_in_domain_pack", caught.exception.code)

        decision = self.assembler.assemble_decision(
            decision_type="choice",
            candidates=self.concepts,
            selected_option=self.concepts[0].candidate_id,
            raw_score=0.5,
            recommended_action="accept",
        )
        invalid = deepcopy(decision)
        invalid["evidence_refs"].append("evidence.fabricated")
        with self.assertRaises(TypedDecisionRuntimeError) as caught:
            self.assembler.validate_decision(invalid)
        self.assertEqual("evidence_not_in_domain_pack", caught.exception.code)

    def test_candidate_metadata_cannot_spoof_a_real_pack_id(self) -> None:
        real = self.concepts[0]
        spoofed = CandidateEvidence(
            candidate_id=real.candidate_id,
            display_name="spoofed display",
            candidate_kind=real.candidate_kind,
            raw_score=real.raw_score,
            evidence_refs=real.evidence_refs,
            raw_features=real.raw_features,
        )
        with self.assertRaises(TypedDecisionRuntimeError) as caught:
            self.assembler.candidate_options((spoofed,))
        self.assertEqual("candidate_display_name_mismatch", caught.exception.code)

    def test_selected_option_must_be_in_candidates(self) -> None:
        with self.assertRaises(TypedDecisionRuntimeError) as caught:
            self.assembler.assemble_decision(
                decision_type="choice",
                candidates=self.concepts,
                selected_option="concept.customer",
                raw_score=0.4,
                recommended_action="accept",
            )
        self.assertEqual("selected_option_not_in_candidates", caught.exception.code)

    def test_trace_event_is_schema_valid_and_domain_bound(self) -> None:
        decision = self.assembler.assemble_decision(
            decision_type="choice",
            candidates=self.concepts,
            selected_option=self.concepts[0].candidate_id,
            raw_score=0.5,
            recommended_action="accept",
        )
        trace = self.assembler.assemble_trace_event(
            request_id="d4-03-synthetic-001",
            recorded_at="2026-09-27T00:00:00Z",
            decision=decision,
            thresholds={
                "accept_min": 0.0,
                "clarify_min": 0.0,
                "abstain_below": 0.0,
                "calibration_version": None,
            },
            executed_action="accept",
            budget_snapshot={
                "model_calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "estimated_cost_cny": 0.0,
                "elapsed_ms": 0,
            },
        )
        self.assertEqual(self.pack.manifest_sha256, trace["domain_pack_ref"]["manifest_sha256"])
        self.assertIsNone(trace["component_versions"]["calibrator"])

        invalid = deepcopy(trace)
        invalid["executed_action"] = "reject"
        with self.assertRaises(TypedDecisionRuntimeError) as caught:
            self.assembler.validate_trace_event(invalid)
        self.assertEqual("trace_action_mismatch", caught.exception.code)


class RuntimeReportTests(unittest.TestCase):
    def test_machine_report_is_content_safe_and_tracks_boundaries(self) -> None:
        report = validate_all()
        self.assertEqual("pass", report["status"])
        self.assertEqual(9, report["fail_closed_probes"]["count"])
        self.assertEqual(5, report["candidate_retrieval"]["synthetic_scenario_count"])
        self.assertFalse(report["candidate_retrieval"]["query_text_stored"])
        self.assertEqual(0, report["model_api_calls"])
        self.assertEqual(0, report["credential_files_read"])
        self.assertEqual(0, report["input_isolation"]["validation_cases_read"])
        self.assertIn(
            "automatic_required_slots", report["not_implemented"]
        )
        for scenario in report["candidate_retrieval"]["scenarios"]:
            self.assertNotIn("query", scenario)
            self.assertRegex(scenario["query_sha256"], r"^[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
