from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from backend.agent.domain_pack import DomainPackRegistry
from backend.agent.round2_contracts import (
    Round2ContractError, canonical_hash, full_catalog, validate_linking_pair,
    validate_complexity_pair, validate_linking_request,
    validate_complexity_request, validate_schema, _visible_refs,
)
from backend.agent.typed_decision_runtime import CandidateEvidence, EvidenceFeatureAssembler
from tools.evaluation.validate_round2_contracts import (
    build_report, fake_examples, negative_cases, no_strategy_examples,
    no_strategy_negative_cases, REPORT,
)
from tools.evaluation.round2_metrics import ranking_metrics, score_linking, compare_document_routes


class Round2ContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pack = DomainPackRegistry(ROOT / "domain-packs").load("chinook_music_store", "1.0.0")
        cls.examples = fake_examples()

    def test_all_valid_statuses_and_offline_cross_file_refs(self):
        for pair in self.examples["schema_linking"].values():
            validate_linking_pair(pair["request"], pair["result"], self.pack)
        for pair in self.examples["document_complexity"].values():
            validate_complexity_pair(pair["request"], pair["result"])

    def test_sixteen_invalid_mutations_fail_closed(self):
        cases = negative_cases(self.examples)
        self.assertEqual(16, len(cases))
        for name, component, pair in cases:
            with self.subTest(name=name), self.assertRaises(Round2ContractError):
                if component == "schema_linking":
                    validate_linking_pair(pair["request"], pair["result"], self.pack)
                else:
                    validate_complexity_pair(pair["request"], pair["result"])

    def test_full_catalog_contains_distractors_and_rejects_cherry_picking(self):
        catalog = full_catalog(self.pack)
        self.assertIn("schema.chinook.playlisttrack", catalog["authorized_object_ids"])
        self.assertIn("schema.chinook.employee", catalog["authorized_object_ids"])
        pair = deepcopy(self.examples["schema_linking"]["selected"])
        pair["request"]["authorized_object_ids"] = ["schema.chinook.artist"]
        with self.assertRaisesRegex(Round2ContractError, "full_catalog_mismatch"):
            validate_linking_request(pair["request"], self.pack)

    def test_local_only_parent_hides_otherwise_public_descendants(self):
        context = deepcopy(self.pack.model_context())
        for item in context["artifacts"]["schema_catalog"]["items"]:
            if item["object_id"] == "schema.chinook.artist":
                item["authorization_level"] = "local_only"
        class Proxy:
            def model_context(self, **kwargs):
                return context
        ids = full_catalog(Proxy())["authorized_object_ids"]
        self.assertNotIn("schema.chinook.artist", ids)
        self.assertNotIn("schema.chinook.artist.name", ids)

    def test_hidden_endpoint_relations_are_not_allowed_as_evidence(self):
        context = deepcopy(self.pack.model_context())
        for item in context["artifacts"]["schema_catalog"]["items"]:
            if item["object_id"] == "schema.chinook.artist":
                item["authorization_level"] = "local_only"
        pack = self.pack
        class Proxy:
            def model_context(self, **kwargs):
                return context
            def __getattr__(self, name):
                return getattr(pack, name)
        proxy = Proxy()
        visible = _visible_refs(proxy, "L1")
        expected = full_catalog(proxy)["authorized_relation_ids"]
        hidden = {x["relation_id"] for x in pack.artifact_items("relations")} - set(expected)
        self.assertTrue(hidden)
        self.assertFalse(hidden & visible)

    def test_selected_column_parent_must_match_even_when_table_decision_valid(self):
        p = deepcopy(self.examples["schema_linking"]["selected"])
        p["result"]["selected_table_id"] = "schema.chinook.album"
        d = p["result"]["typed_decision"]
        d["selected_option"] = "schema.chinook.album"
        d["raw_score"] = .8
        with self.assertRaisesRegex(Round2ContractError, "selected_column_parent_mismatch"):
            validate_linking_pair(p["request"], p["result"], self.pack)

    def test_tied_candidates_require_stable_id_sort(self):
        p = deepcopy(self.examples["schema_linking"]["selected"])
        p["result"]["table_candidates"][1]["raw_score"] = .9
        with self.assertRaisesRegex(Round2ContractError, "unstable_candidate_order"):
            validate_linking_pair(p["request"], p["result"], self.pack)

    def test_unicode_question_request_binding_cannot_be_reused(self):
        p = deepcopy(self.examples["schema_linking"]["selected"])
        p["request"]["question"] = "😀𠮷查询艺术家"
        with self.assertRaisesRegex(Round2ContractError, "request_binding_mismatch"):
            validate_linking_pair(p["request"], p["result"], self.pack)
        p["result"]["input_sha256"] = canonical_hash(p["request"])
        validate_linking_pair(p["request"], p["result"], self.pack)

    def test_nan_and_calibration_claims_are_not_accepted(self):
        p = deepcopy(self.examples["schema_linking"]["selected"])
        p["result"]["table_candidates"][0]["raw_score"] = float("nan")
        with self.assertRaisesRegex(Round2ContractError, "non_finite_number"):
            validate_linking_pair(p["request"], p["result"], self.pack)
        p = deepcopy(self.examples["document_complexity"]["selected"])
        p["result"]["typed_decision"]["calibration_version"] = "calibration.v1"
        with self.assertRaises(Round2ContractError):
            validate_complexity_pair(p["request"], p["result"])

    def test_complexity_cross_page_evidence_rejected(self):
        p = deepcopy(self.examples["document_complexity"]["selected"])
        p["request"]["evidence_catalog"][0]["page_number"] = 2
        with self.assertRaisesRegex(Round2ContractError, "preflight_evidence_page_mismatch"):
            validate_complexity_pair(p["request"], p["result"])

    def test_complexity_unknown_cannot_dispatch(self):
        p = deepcopy(self.examples["document_complexity"]["unknown"])
        p["result"]["selected_strategy_id"] = "pypdf_text.v1"
        p["result"]["typed_decision"].update(decision_type="choice", selected_option="pypdf_text.v1",
                                            raw_score=.9, recommended_action="accept", abstain_reason=None)
        with self.assertRaisesRegex(Round2ContractError, "unknown_complexity_must_abstain"):
            validate_complexity_pair(p["request"], p["result"])

    def test_zero_capabilities_reach_production_pair_validator_for_both_actions(self):
        for action, pair in no_strategy_examples().items():
            with self.subTest(action=action):
                validate_complexity_request(pair["request"])
                validate_complexity_pair(pair["request"], pair["result"])
                self.assertEqual([], pair["request"]["available_strategies"])
                self.assertEqual([], pair["result"]["typed_decision"]["candidate_options"])
                self.assertIsNone(pair["result"]["selected_strategy_id"])
                self.assertEqual("not_started", pair["result"]["execution_status"])
                self.assertEqual(action, pair["result"]["typed_decision"]["recommended_action"])

    def test_v11_request_is_additive_and_v10_remains_strict(self):
        pair = no_strategy_examples()["escalate"]
        with self.assertRaisesRegex(Round2ContractError, "schema_invalid"):
            validate_schema("document-complexity-request.v1", pair["request"])
        pair["request"]["request_version"] = "1.0.0"
        with self.assertRaisesRegex(Round2ContractError, "schema_invalid"):
            validate_complexity_request(pair["request"])
        selected = deepcopy(self.examples["document_complexity"]["selected"])
        selected["request"]["request_version"] = "1.1.0"
        selected["result"]["input_sha256"] = canonical_hash(selected["request"])
        validate_complexity_pair(selected["request"], selected["result"])
        self.assertEqual("1.0.0", selected["result"]["result_version"])

    def test_zero_capability_mutations_fail_closed_without_offering_a_fake_path(self):
        mutations = no_strategy_negative_cases(no_strategy_examples())
        self.assertEqual(5, len(mutations))
        for name, _, pair in mutations:
            with self.subTest(name=name), self.assertRaises(Round2ContractError):
                validate_complexity_pair(pair["request"], pair["result"])

    def test_zero_capability_request_binding_cannot_reuse_available_path_result(self):
        pair = deepcopy(self.examples["document_complexity"]["unknown"])
        pair["request"].update(request_version="1.1.0", available_strategies=[])
        with self.assertRaisesRegex(Round2ContractError, "request_binding_mismatch"):
            validate_complexity_pair(pair["request"], pair["result"])

    def test_v11_unknown_still_cannot_choose_an_offered_strategy(self):
        pair = deepcopy(self.examples["document_complexity"]["unknown"])
        pair["request"]["request_version"] = "1.1.0"
        pair["result"].update(input_sha256=canonical_hash(pair["request"]), selected_strategy_id="pypdf_text.v1")
        pair["result"]["typed_decision"].update(decision_type="choice", selected_option="pypdf_text.v1",
                                                raw_score=.9, recommended_action="accept", abstain_reason=None)
        with self.assertRaisesRegex(Round2ContractError, "unknown_complexity_must_abstain"):
            validate_complexity_pair(pair["request"], pair["result"])

    def test_unsupported_request_version_and_missing_features_do_not_bypass_validation(self):
        for field, value in (("request_version", "1.2.0"), ("observations", []), ("evidence_catalog", [])):
            pair = no_strategy_examples()["escalate"]
            pair["request"][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(Round2ContractError, "schema_invalid"):
                validate_complexity_request(pair["request"])

    def test_complexity_document_hash_and_extra_fields_rejected(self):
        for field, value in (("document_sha256", "0" * 64), ("model_output", "private")):
            p = deepcopy(self.examples["document_complexity"]["selected"])
            p["result"][field] = value
            with self.assertRaises(Round2ContractError):
                validate_complexity_pair(p["request"], p["result"])

    def test_report_rebuild_is_stable_and_not_actual_engine_quality(self):
        a, b = build_report(), build_report()
        self.assertEqual(a, b)
        self.assertEqual(a, json.loads(REPORT.read_text(encoding="utf-8")))
        self.assertFalse(a["actual_B_30plus_database_verified"])
        self.assertFalse(a["actual_C_30_document_two_path_effect_verified"])
        self.assertEqual(0, a["boundaries"]["model_api_calls"])


class Round2ScorerTests(unittest.TestCase):
    def test_recall_mrr_and_empty_denominators_are_separate(self):
        score = ranking_metrics([["a", "b"], ["c", "a"], []], [["b"], ["a"], []])
        self.assertEqual((2, 1), (score["scored_cases"], score["empty_relevant_cases"]))
        self.assertEqual(0, score["recall_at_1"])
        self.assertEqual(1, score["recall_at_3"])
        self.assertEqual(.5, score["mrr"])
        self.assertIsNone(ranking_metrics([[]], [[]])["mrr"])

    def test_ranking_is_not_final_selection(self):
        pack = DomainPackRegistry(ROOT / "domain-packs").load("chinook_music_store", "1.0.0")
        p = deepcopy(fake_examples()["schema_linking"]["selected"])
        tables = p["result"]["table_candidates"]
        tables[0]["raw_score"], tables[1]["raw_score"] = .8, .9
        tables.sort(key=lambda x: (-x["raw_score"], x["object_id"]))
        candidates = [CandidateEvidence(x["object_id"], pack.resolve(x["object_id"])["display_name"], "schema_object",
                                        x["raw_score"], tuple(x["evidence_refs"]), x["raw_features"]) for x in tables]
        p["result"]["typed_decision"] = EvidenceFeatureAssembler(pack).assemble_decision(
            decision_type="choice", candidates=candidates, selected_option="schema.chinook.artist",
            raw_score=.8, recommended_action="accept")
        labels = {p["request"]["request_id"]: {"table_ids": ["schema.chinook.artist"], "column_ids": ["schema.chinook.artist.name"], "status": "selected"}}
        score = score_linking([p], labels, pack)
        self.assertEqual(0, score["tables"]["recall_at_1"])
        self.assertEqual(.5, score["tables"]["mrr"])
        self.assertEqual(1, score["final_table_accuracy"])

    def test_scoring_id_set_cannot_drop_failures(self):
        p = fake_examples()["schema_linking"]["selected"]
        pack = DomainPackRegistry(ROOT / "domain-packs").load("chinook_music_store", "1.0.0")
        with self.assertRaisesRegex(ValueError, "scoring_case_set_mismatch"):
            score_linking([p], {}, pack)

    def test_document_comparison_includes_preflight_and_preserves_failure_count(self):
        records = [{"document_id": "doc.fake", "document_sha256": "a" * 64, "arm": arm,
                    "outcome": "succeeded" if arm != "adaptive" else "failed", "preflight_ms": 2 if arm == "adaptive" else 0,
                    "parse_ms": 3, "structure_score": 0 if arm == "adaptive" else 1, "answer_support_score": 0}
                   for arm in ("uniform_pypdf", "uniform_pdfplumber", "adaptive")]
        report = compare_document_routes(records)
        self.assertEqual(5, report["arms"]["adaptive"]["mean_total_ms"])
        self.assertEqual(1, report["arms"]["adaptive"]["outcomes"]["failed"])
        records[0]["document_sha256"] = "b" * 64
        with self.assertRaisesRegex(ValueError, "unpaired_document_measurements"):
            compare_document_routes(records)

    def test_identical_pdf_bytes_cannot_be_renamed_to_pad_document_count(self):
        records = [{"document_id": doc, "document_sha256": "a" * 64, "arm": arm,
                    "outcome": "succeeded", "preflight_ms": 0, "parse_ms": 1,
                    "structure_score": 1, "answer_support_score": 1}
                   for doc in ("doc.one", "doc.two")
                   for arm in ("uniform_pypdf", "uniform_pdfplumber", "adaptive")]
        with self.assertRaisesRegex(ValueError, "duplicate_document_bytes"):
            compare_document_routes(records)


if __name__ == "__main__":
    unittest.main()
