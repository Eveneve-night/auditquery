"""ROUND2 contract/scorer smoke only. No real linking, parsing or model run."""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))
from backend.agent.domain_pack import DomainPackRegistry
from backend.agent.typed_decision_runtime import CandidateEvidence, EvidenceFeatureAssembler
from backend.agent.round2_contracts import (
    canonical_hash, full_catalog, pack_reference, validate_linking_pair,
    validate_complexity_pair, Round2ContractError,
)
from tools.evaluation.round2_metrics import score_linking

EXAMPLES = ROOT / "configs/examples/round2/v1/public-contracts.json"
CAPABILITY_EXAMPLES = ROOT / "configs/examples/round2/v1.1/no-strategies.json"
REPORT = ROOT / "tests/reports/round2-a-contracts-v1.json"


def fake_examples():
    """Explicitly fake outputs; never pass these off as B/C implementation."""
    pack = DomainPackRegistry(ROOT / "domain-packs").load("chinook_music_store", "1.0.0")
    reference = pack_reference(pack)
    tables = [CandidateEvidence(oid, pack.resolve(oid)["display_name"], "schema_object", score,
                               (oid,), {"contract_fake": True, "candidate_kind": "schema_object"})
              for oid, score in (("schema.chinook.artist", .9), ("schema.chinook.album", .8))]
    columns = [CandidateEvidence("schema.chinook.artist.name", "艺术家名称", "schema_object", .9,
                                 ("schema.chinook.artist.name",), {"contract_fake": True, "candidate_kind": "schema_object"})]
    linking = {}
    for status in ("selected", "clarify", "abstain", "reject"):
        request = {"request_version": "1.0.0", "request_id": "request.round2." + status,
                   "question": "公开假样例：查询艺术家名称", "domain_pack_ref": reference,
                   "catalog_scope": "full_model_visible_read_public_pack", "max_data_level": "L1",
                   **full_catalog(pack)}
        action = {"selected": "accept", "clarify": "clarify", "abstain": "escalate", "reject": "reject"}[status]
        decision = EvidenceFeatureAssembler(pack).assemble_decision(
            decision_type="choice" if status == "selected" else ("score" if status == "clarify" else "abstain"),
            candidates=tables, selected_option=tables[0].candidate_id if status == "selected" else None,
            raw_score=.9 if status in {"selected", "clarify"} else None,
            recommended_action=action, abstain_reason="insufficient_evidence" if status in {"abstain", "reject"} else None,
            missing_or_conflicting_slots=[{"slot_id": "slot.entity_type", "issue": "missing", "evidence_refs": []}] if status == "clarify" else [])
        def records(values):
            return [{"object_id": x.candidate_id, "raw_score": x.raw_score,
                     "evidence_refs": list(x.evidence_refs), "raw_features": dict(x.raw_features)} for x in values]
        result = {"result_version": "1.0.0", "request_id": request["request_id"], "input_sha256": canonical_hash(request),
                  "domain_pack_ref": reference, "status": status, "reason_code": "public_contract_example",
                  "table_candidates": records(tables), "column_candidates": records(columns),
                  "selected_table_id": tables[0].candidate_id if status == "selected" else None,
                  "selected_column_ids": [columns[0].candidate_id] if status == "selected" else [],
                  "typed_decision": decision, "sql_execution_authorized": False}
        linking[status] = {"request": request, "result": result}
    complexity = {}
    for mode in ("selected", "unknown"):
        request = {"request_version": "1.0.0", "request_id": "request.round2.document." + mode,
                   "phase": "pre_strategy", "feature_version": "preflight-observation.v1",
                   "document": {"document_id": "doc.round2.fake", "sha256": "a" * 64,
                                "media_type": "application/pdf", "byte_count": 1234, "page_count": 2},
                   "observations": [{"page_number": 1, "text_characters": 100 if mode == "selected" else 0,
                                     "image_count": 0 if mode == "selected" else 1,
                                     "table_hint_count": 0, "layout_hint_count": 0, "evidence_refs": ["feature.fake.page1"]}],
                   "evidence_catalog": [{"evidence_id": "feature.fake.page1", "page_number": 1, "kind": "preflight_observation"}],
                   "available_strategies": [{"strategy_id": "pypdf_text.v1", "display_name": "pypdf text"},
                                            {"strategy_id": "pdfplumber_layout.v1", "display_name": "pdfplumber layout"}]}
        decision = {"decision_type": "choice" if mode == "selected" else "abstain",
                    "candidate_options": [{"candidate_id": x["strategy_id"], "display_name": x["display_name"],
                                           "evidence_refs": ["feature.fake.page1"], "raw_features": {"contract_fake": True}}
                                          for x in request["available_strategies"]],
                    "selected_option": "pypdf_text.v1" if mode == "selected" else None,
                    "raw_score": .8 if mode == "selected" else None, "calibration_version": None,
                    "evidence_refs": ["feature.fake.page1"], "missing_or_conflicting_slots": [],
                    "recommended_action": "accept" if mode == "selected" else "escalate",
                    "abstain_reason": None if mode == "selected" else "insufficient_evidence", "contract_version": "1.0.0"}
        result = {"result_version": "1.0.0", "request_id": request["request_id"], "input_sha256": canonical_hash(request),
                  "document_id": "doc.round2.fake", "document_sha256": "a" * 64,
                  "feature_version": "preflight-observation.v1", "policy_version": "complexity_policy.v1",
                  "assessment_scope": "structural_complexity_not_quality", "complexity": "simple" if mode == "selected" else "unknown",
                  "complexity_score": .2 if mode == "selected" else None, "score_semantics": "uncalibrated_complexity_score",
                  "uncertainty_reason": None if mode == "selected" else "unsupported_scan",
                  "reasons": [{"reason_code": "public_fake_preflight", "evidence_refs": ["feature.fake.page1"]}],
                  "selected_strategy_id": "pypdf_text.v1" if mode == "selected" else None,
                  "typed_decision": decision, "execution_status": "not_started", "ocr_authorized": False,
                  "external_processing_authorized": False}
        complexity[mode] = {"request": request, "result": result}
    return {"fixture_version": "1.0.0", "scope": "public_fake_contract_examples_not_engine_predictions",
            "schema_linking": linking, "document_complexity": complexity}


def negative_cases(examples):
    """Public mutation metadata, not eval questions or runtime rules."""
    cases = []
    def add(name, component, mutate):
        pair = deepcopy(examples[component]["selected"])
        mutate(pair)
        cases.append((name, component, pair))
    add("linking_cherry_picked_catalog", "schema_linking", lambda p: p["request"]["authorized_object_ids"].pop())
    add("linking_label_input", "schema_linking", lambda p: p["request"].update(label="fake"))
    add("linking_catalog_hash", "schema_linking", lambda p: p["request"].update(catalog_sha256="0" * 64))
    add("linking_unauthorized_object", "schema_linking", lambda p: p["result"]["table_candidates"][0].update(object_id="schema.unavailable"))
    add("linking_field_parent", "schema_linking", lambda p: p["result"].update(selected_table_id="schema.chinook.album"))
    add("linking_decision_evidence", "schema_linking", lambda p: p["result"]["typed_decision"].update(evidence_refs=["fake.evidence"]))
    add("linking_execution_authority", "schema_linking", lambda p: p["result"].update(sql_execution_authorized=True))
    add("linking_status_conflict", "schema_linking", lambda p: p["result"].update(status="abstain"))
    add("complexity_posthoc_feature", "document_complexity", lambda p: p["request"].update(actual_parse_failed=True))
    add("complexity_label_input", "document_complexity", lambda p: p["request"]["observations"][0].update(expected_difficulty="high"))
    add("complexity_wrong_page", "document_complexity", lambda p: p["request"]["observations"][0].update(page_number=999))
    add("complexity_unknown_evidence", "document_complexity", lambda p: p["result"]["reasons"][0].update(evidence_refs=["feature.unknown"]))
    add("complexity_unavailable_strategy", "document_complexity", lambda p: p["result"].update(selected_strategy_id="ocr.cloud.v1"))
    add("complexity_fake_execution", "document_complexity", lambda p: p["result"].update(execution_status="succeeded"))
    add("complexity_quality_claim", "document_complexity", lambda p: p["result"].update(assessment_scope="document_quality"))
    add("complexity_calibrated_claim", "document_complexity", lambda p: p["result"]["typed_decision"].update(calibration_version="calibration.v1"))
    return cases


def no_strategy_examples():
    """Fake capability metadata, not a PDF scan or a fabricated parser offer."""
    examples = {}
    for action in ("escalate", "reject"):
        pair = deepcopy(fake_examples()["document_complexity"]["unknown"])
        request, result = pair["request"], pair["result"]
        request.update(request_version="1.1.0", request_id="request.round2.no_strategy." + action,
                       available_strategies=[])
        request["evidence_catalog"].append({"evidence_id": "feature.runtime.capabilities",
                                             "page_number": None, "kind": "document_metadata"})
        result.update(request_id=request["request_id"], input_sha256=canonical_hash(request),
                      uncertainty_reason="policy_restriction",
                      reasons=[{"reason_code": "local_capabilities_unavailable",
                                "evidence_refs": ["feature.runtime.capabilities"]}])
        result["typed_decision"].update(candidate_options=[], recommended_action=action,
                                        abstain_reason="policy_restriction",
                                        evidence_refs=["feature.runtime.capabilities"])
        examples[action] = pair
    return examples


def no_strategy_negative_cases(examples):
    cases = []
    def add(name, mutate):
        pair = deepcopy(examples["escalate"])
        mutate(pair)
        cases.append((name, "document_complexity", pair))
    add("no_strategy_phantom_candidates", lambda p: p["result"]["typed_decision"].update(
        candidate_options=fake_examples()["document_complexity"]["unknown"]["result"]["typed_decision"]["candidate_options"]))
    add("no_strategy_phantom_selection", lambda p: p["result"].update(selected_strategy_id="pypdf_text.v1"))
    add("no_strategy_missing_evidence", lambda p: p["result"]["typed_decision"].update(evidence_refs=[]))
    add("no_strategy_unknown_evidence", lambda p: p["result"]["reasons"][0].update(evidence_refs=["feature.not_present"]))
    add("no_strategy_accept_action", lambda p: p["result"]["typed_decision"].update(recommended_action="accept"))
    return cases


def build_report():
    examples = json.loads(EXAMPLES.read_text(encoding="utf-8"))
    if examples != fake_examples():
        raise Round2ContractError("public_example_drifted")
    capabilities = json.loads(CAPABILITY_EXAMPLES.read_text(encoding="utf-8"))
    if capabilities != no_strategy_examples():
        raise Round2ContractError("capability_example_drifted")
    pack = DomainPackRegistry(ROOT / "domain-packs").load("chinook_music_store", "1.0.0")
    for pair in examples["schema_linking"].values():
        validate_linking_pair(pair["request"], pair["result"], pack)
    for pair in examples["document_complexity"].values():
        validate_complexity_pair(pair["request"], pair["result"])
    for pair in capabilities.values():
        validate_complexity_pair(pair["request"], pair["result"])
    rejected = []
    for name, component, pair in negative_cases(examples) + no_strategy_negative_cases(capabilities):
        try:
            if component == "schema_linking":
                validate_linking_pair(pair["request"], pair["result"], pack)
            else:
                validate_complexity_pair(pair["request"], pair["result"])
        except Round2ContractError as exc:
            rejected.append({"case_id": name, "code": str(exc)})
        else:
            raise Round2ContractError("negative_example_accepted")
    pair = examples["schema_linking"]["selected"]
    toy_score = score_linking([pair], {pair["request"]["request_id"]: {
        "table_ids": ["schema.chinook.artist"], "column_ids": ["schema.chinook.artist.name"], "status": "selected"}}, pack)
    inputs = [*sorted((ROOT / "configs/schemas/round2").glob("*.json")), EXAMPLES, CAPABILITY_EXAMPLES,
              ROOT / "configs/evaluation/round2/public-development.v1.json",
              ROOT / "src/backend/agent/round2_contracts.py", ROOT / "tools/evaluation/round2_metrics.py", Path(__file__)]
    report = {"report_version": "1.0.0", "scope": "public_fake_contract_and_scorer_smoke",
              "valid_linking_examples": 4, "valid_complexity_examples": 4, "negative_examples_rejected": rejected,
              "complexity_request_versions": ["1.0.0", "1.1.0"], "complexity_result_version": "1.0.0",
              "zero_capability_actions_verified": sorted(capabilities),
              "toy_linking_score_not_actual_quality": toy_score,
              "source_manifest": {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes().replace(b"\r\n", b"\n")).hexdigest() for p in inputs},
              "actual_B_30plus_database_verified": False, "actual_C_30_document_two_path_effect_verified": False,
              "boundaries": {"model_api_calls": 0, "real_key_reads": 0, "formal_validation_reads": 0, "hidden_test_reads": 0, "cost_cny": 0},
              "not_proven": ["INT04_quality", "INT08_difficulty_prediction", "strategy_downstream_benefit", "business_accuracy", "generalization", "innovation"]}
    report["stable_sha256"] = canonical_hash(report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--emit-examples", action="store_true")
    parser.add_argument("--refresh-capability-examples", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--report", type=Path, default=REPORT)
    args = parser.parse_args()
    if args.emit_examples:
        print(json.dumps(fake_examples(), ensure_ascii=False, indent=2))
        return 0
    if args.refresh_capability_examples:
        CAPABILITY_EXAMPLES.parent.mkdir(parents=True, exist_ok=True)
        CAPABILITY_EXAMPLES.write_text(json.dumps(no_strategy_examples(), ensure_ascii=False, indent=2) + "\n",
                                       encoding="utf-8", newline="\n")
    report = build_report()
    if args.check:
        if json.loads(args.report.read_text(encoding="utf-8")) != report:
            raise Round2ContractError("contract_report_drifted")
    else:
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"status": "pass", "negative_cases": len(report["negative_examples_rejected"]), "stable_sha256": report["stable_sha256"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
