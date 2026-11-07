"""ROUND2 public invariants, not a linker, parser, classifier or executor."""
from __future__ import annotations

from functools import lru_cache
import hashlib
import json
import math
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from referencing import Registry, Resource

from .typed_decision_runtime import CandidateEvidence, EvidenceFeatureAssembler, TypedDecisionRuntimeError
from .domain_pack import DomainPackError

ROOT = Path(__file__).resolve().parents[3]
SCHEMAS = ROOT / "configs/schemas"


class Round2ContractError(ValueError):
    """Stable code only; never include raw input, exception text or PDF text."""


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _clean(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise Round2ContractError("not_json_compatible")
            name = str(key).lower().replace("-", "_")
            if "expected" in name or "gold" in name or name in {"label", "labels", "answer", "answers"}:
                raise Round2ContractError("forbidden_scoring_field")
            _clean(item)
    elif isinstance(value, list):
        for item in value:
            _clean(item)
    elif isinstance(value, float) and not math.isfinite(value):
        raise Round2ContractError("non_finite_number")
    elif value is not None and not isinstance(value, (str, int, bool, float)):
        raise Round2ContractError("not_json_compatible")


@lru_cache(maxsize=1)
def _validators():
    paths = [*sorted((SCHEMAS / "round2").glob("*.json")),
             SCHEMAS / "d4/typed-decision.v1.schema.json",
             SCHEMAS / "semantics/semantic-request.v1.schema.json"]
    resources = [json.loads(p.read_text(encoding="utf-8")) for p in paths]
    registry = Registry().with_resources((s["$id"], Resource.from_contents(s)) for s in resources)
    for schema in resources:
        Draft202012Validator.check_schema(schema)
    return {s["$id"]: Draft202012Validator(s, registry=registry) for s in resources}


def validate_schema(schema_id, value):
    _clean(value)
    try:
        _validators()[schema_id].validate(value)
    except ValidationError as exc:
        raise Round2ContractError("schema_invalid") from exc


def _unique(items, key):
    ids = [x[key] for x in items]
    if len(ids) != len(set(ids)):
        raise Round2ContractError("duplicate_id")


def pack_reference(pack):
    return {"domain_pack_id": pack.domain_pack_id, "version": pack.version,
            "manifest_sha256": pack.manifest_sha256}


def full_catalog(pack, max_data_level="L1"):
    """Full public, model-visible catalog; no question/scoring access."""
    context = pack.model_context(max_data_level=max_data_level)["artifacts"]
    objects = {x["object_id"]: x for x in context.get("schema_catalog", {}).get("items", [])
               if x["authorization_level"] == "read_public"}
    while any(x["parent_object_id"] is not None and x["parent_object_id"] not in objects for x in objects.values()):
        objects = {k: x for k, x in objects.items() if x["parent_object_id"] is None or x["parent_object_id"] in objects}
    relations = {x["relation_id"]: x for x in context.get("relations", {}).get("items", [])
                 if x["source_object_id"] in objects and x["target_object_id"] in objects}
    if not objects:
        raise Round2ContractError("authorized_catalog_empty")
    payload = {"objects": [objects[k] for k in sorted(objects)],
               "relations": [relations[k] for k in sorted(relations)]}
    return {"authorized_object_ids": sorted(objects), "authorized_relation_ids": sorted(relations),
            "catalog_sha256": canonical_hash(payload)}


def _visible_refs(pack, level):
    ids = set()
    fields = {"concepts": "concept_id", "aliases": "alias_id", "slots": "slot_id",
              "schema_catalog": "object_id", "relations": "relation_id",
              "value_evidence": "evidence_id", "validation_rules": "rule_id"}
    for role, artifact in pack.model_context(max_data_level=level)["artifacts"].items():
        ids.update(x[fields[role]] for x in artifact["items"])
    # Object authorization is stronger than artifact-level model visibility.
    catalog = full_catalog(pack, level)
    public = set(catalog["authorized_object_ids"])
    ids -= {x["object_id"] for x in pack.artifact_items("schema_catalog") if x["object_id"] not in public}
    ids -= {x["relation_id"] for x in pack.artifact_items("relations")
            if x["relation_id"] not in catalog["authorized_relation_ids"]}
    ids.update(x["source_id"] for x in pack.manifest["source_refs"]
               if x["authorized"] and x["data_level"] in ({"L0"} if level == "L0" else {"L0", "L1"}))
    return ids


def _refs(refs, allowed):
    if not set(refs) <= allowed:
        raise Round2ContractError("evidence_not_authorized")


def _bind(request, result):
    if request["request_id"] != result["request_id"] or result["input_sha256"] != canonical_hash(request):
        raise Round2ContractError("request_binding_mismatch")


def validate_linking_request(request, pack):
    validate_schema("schema-linking-request.v1", request)
    if request["domain_pack_ref"] != pack_reference(pack):
        raise Round2ContractError("domain_pack_mismatch")
    catalog = full_catalog(pack, request["max_data_level"])
    if (request["authorized_object_ids"] != catalog["authorized_object_ids"] or
        request["authorized_relation_ids"] != catalog["authorized_relation_ids"] or
        request["catalog_sha256"] != catalog["catalog_sha256"]):
        raise Round2ContractError("full_catalog_mismatch")
    return catalog


def validate_linking_pair(request, result, pack):
    catalog = validate_linking_request(request, pack)
    validate_schema("schema-linking-result.v1", result)
    _bind(request, result)
    if result["domain_pack_ref"] != pack_reference(pack):
        raise Round2ContractError("domain_pack_mismatch")
    allowed = _visible_refs(pack, request["max_data_level"])
    assembler = EvidenceFeatureAssembler(pack)
    table_records = ()
    for field, kind in (("table_candidates", "table"), ("column_candidates", "column")):
        records = result[field]
        _unique(records, "object_id")
        if records != sorted(records, key=lambda x: (-x["raw_score"], x["object_id"])):
            raise Round2ContractError("unstable_candidate_order")
        candidates = []
        for record in records:
            oid = record["object_id"]
            if oid not in catalog["authorized_object_ids"] or pack.resolve(oid)["object_type"] != kind:
                raise Round2ContractError("candidate_not_authorized_or_wrong_kind")
            if record["raw_features"].get("candidate_kind") != "schema_object":
                raise Round2ContractError("candidate_kind_mismatch")
            _refs(record["evidence_refs"], allowed)
            candidates.append(CandidateEvidence(oid, pack.resolve(oid)["display_name"], "schema_object",
                                                record["raw_score"], tuple(record["evidence_refs"]), record["raw_features"]))
        try:
            options = assembler.candidate_options(candidates)
        except TypedDecisionRuntimeError as exc:
            raise Round2ContractError("candidate_evidence_invalid") from exc
        if kind == "table":
            table_records = records
            if options != result["typed_decision"]["candidate_options"]:
                raise Round2ContractError("decision_candidates_mismatch")
    decision = result["typed_decision"]
    try:
        assembler.validate_decision(decision)
    except TypedDecisionRuntimeError as exc:
        raise Round2ContractError("typed_decision_invalid") from exc
    _refs(decision["evidence_refs"], allowed)
    for issue in decision["missing_or_conflicting_slots"]:
        try:
            role = pack.resolve_role(issue["slot_id"])
        except DomainPackError as exc:
            raise Round2ContractError("invalid_clarification_slot") from exc
        if role != "slots":
            raise Round2ContractError("invalid_clarification_slot")
        _refs(issue["evidence_refs"], allowed)
    if decision["calibration_version"] is not None:
        raise Round2ContractError("calibration_not_established")
    selected = result["selected_table_id"]
    columns = result["selected_column_ids"]
    if result["status"] == "selected":
        if (decision["decision_type"] != "choice" or decision["recommended_action"] != "accept" or
            selected != decision["selected_option"] or selected not in {x["object_id"] for x in table_records}):
            raise Round2ContractError("selection_decision_mismatch")
        record = next(x for x in table_records if x["object_id"] == selected)
        if decision["raw_score"] != record["raw_score"]:
            raise Round2ContractError("selected_score_mismatch")
        if not set(record["evidence_refs"]) <= set(decision["evidence_refs"]):
            raise Round2ContractError("selection_evidence_missing")
        if not set(columns) <= {x["object_id"] for x in result["column_candidates"]} or any(
            pack.resolve(c)["parent_object_id"] != selected for c in columns
        ):
            raise Round2ContractError("selected_column_parent_mismatch")
    else:
        expected = {"clarify": ("score", "clarify"), "abstain": ("abstain", "escalate"), "reject": ("abstain", "reject")}
        if selected is not None or columns or (decision["decision_type"], decision["recommended_action"]) != expected[result["status"]]:
            raise Round2ContractError("nonselection_decision_mismatch")


def validate_complexity_request(request):
    # 1.1 adds an explicit zero-capability input; never relax frozen 1.0.
    schema_id = ("document-complexity-request.v1.1"
                 if isinstance(request, dict) and request.get("request_version") == "1.1.0"
                 else "document-complexity-request.v1")
    validate_schema(schema_id, request)
    _unique(request["evidence_catalog"], "evidence_id")
    _unique(request["observations"], "page_number")
    _unique(request["available_strategies"], "strategy_id")
    pages = request["document"]["page_count"]
    catalog = {x["evidence_id"]: x for x in request["evidence_catalog"]}
    if any(x["page_number"] is not None and x["page_number"] > pages for x in catalog.values()):
        raise Round2ContractError("preflight_page_out_of_range")
    for item in request["observations"]:
        if item["page_number"] > pages:
            raise Round2ContractError("preflight_page_out_of_range")
        _refs(item["evidence_refs"], set(catalog))
        if any(catalog[r]["page_number"] not in (None, item["page_number"]) for r in item["evidence_refs"]):
            raise Round2ContractError("preflight_evidence_page_mismatch")


def validate_complexity_pair(request, result):
    validate_complexity_request(request)
    validate_schema("document-complexity-result.v1", result)
    _bind(request, result)
    document = request["document"]
    if result["document_id"] != document["document_id"] or result["document_sha256"] != document["sha256"]:
        raise Round2ContractError("document_binding_mismatch")
    catalog = {x["evidence_id"] for x in request["evidence_catalog"]}
    for reason in result["reasons"]:
        _refs(reason["evidence_refs"], catalog)
    decision = result["typed_decision"]
    _unique(decision["candidate_options"], "candidate_id")
    strategies = {x["strategy_id"]: x for x in request["available_strategies"]}
    option_ids = {x["candidate_id"] for x in decision["candidate_options"]}
    if option_ids != set(strategies):
        raise Round2ContractError("strategy_candidates_mismatch")
    for option in decision["candidate_options"]:
        if option["display_name"] != strategies[option["candidate_id"]]["display_name"]:
            raise Round2ContractError("strategy_display_name_mismatch")
        _refs(option["evidence_refs"], catalog)
        if not option["evidence_refs"]:
            raise Round2ContractError("strategy_evidence_missing")
    _refs(decision["evidence_refs"], catalog)
    if not decision["evidence_refs"]:
        raise Round2ContractError("strategy_evidence_missing")
    if decision["missing_or_conflicting_slots"] or decision["calibration_version"] is not None:
        raise Round2ContractError("unsupported_document_decision_metadata")
    chosen = result["selected_strategy_id"]
    if chosen is None:
        if (decision["decision_type"] != "abstain" or decision["recommended_action"] not in {"escalate", "reject"}):
            raise Round2ContractError("strategy_abstention_mismatch")
    elif (chosen not in strategies or decision["decision_type"] != "choice" or
          decision["selected_option"] != chosen or decision["recommended_action"] != "accept"):
        raise Round2ContractError("strategy_selection_mismatch")
    if result["complexity"] == "unknown":
        if result["complexity_score"] is not None or result["uncertainty_reason"] is None or chosen is not None:
            raise Round2ContractError("unknown_complexity_must_abstain")
    elif result["complexity_score"] is None:
        raise Round2ContractError("complexity_score_missing")
    if result["uncertainty_reason"] in {"unsupported_scan", "resource_limit", "policy_restriction"} and chosen is not None:
        raise Round2ContractError("unsupported_processing_must_abstain")
