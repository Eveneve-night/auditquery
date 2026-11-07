"""Validate the BATCH1 semantic request/result v1 public contract offline.

The validator only reads repository-owned public fixtures and a validated
domain pack.  It never reads credentials, expected/gold/label fields, hidden
tests, databases, or model APIs, and it never authorizes SQL execution.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Iterable, Mapping, Sequence

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError, ValidationError
from referencing import Registry, Resource


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from backend.agent.domain_pack import (  # noqa: E402
    DomainPackError,
    DomainPackRegistry,
    LoadedDomainPack,
)
from backend.agent.selective_routing import (  # noqa: E402
    RequiredSlotResolver,
    SelectiveRoutingError,
    build_slot_observations,
)
from backend.agent.typed_decision_runtime import (  # noqa: E402
    CandidateEvidence,
    EvidenceFeatureAssembler,
    TypedDecisionRuntimeError,
)


SCHEMA_ROOT = PROJECT_ROOT / "configs" / "schemas"
SEMANTIC_SCHEMA_ROOT = SCHEMA_ROOT / "semantics"
REQUEST_SCHEMA_PATH = SEMANTIC_SCHEMA_ROOT / "semantic-request.v1.schema.json"
RESULT_SCHEMA_PATH = SEMANTIC_SCHEMA_ROOT / "semantic-result.v1.schema.json"
TYPED_DECISION_SCHEMA_PATH = SCHEMA_ROOT / "d4" / "typed-decision.v1.schema.json"
EXAMPLE_ROOT = PROJECT_ROOT / "configs" / "examples" / "semantics" / "v1"
VALID_EXAMPLE_PATH = EXAMPLE_ROOT / "semantic-contract.valid.json"
INVALID_EXAMPLE_PATH = EXAMPLE_ROOT / "semantic-contract.invalid-mutations.json"
PACK_ROOT = PROJECT_ROOT / "domain-packs"
PACK_PATH = PACK_ROOT / "chinook" / "v1"
REPORT_PATH = PROJECT_ROOT / "tests" / "reports" / "batch1-semantic-contracts-v1.json"
ALLOWED_CANDIDATE_ROLES = frozenset({"concepts", "schema_catalog", "slots"})
ROLE_TO_KIND = {
    "concepts": "concept",
    "schema_catalog": "schema_object",
    "slots": "slot",
}


class SemanticContractError(RuntimeError):
    """Stable, content-safe public contract validation error."""


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def canonical_sha256(path: Path) -> str:
    content = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return hashlib.sha256(content).hexdigest()


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _forbidden_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_").replace(" ", "_")
    return (
        "expected" in normalized
        or "gold" in normalized
        or normalized in {
            "label",
            "labels",
            "answer",
            "answers",
            "target_answer",
        }
    )


def assert_no_answer_fields(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if _forbidden_key(str(key)):
                raise SemanticContractError("forbidden_answer_field")
            assert_no_answer_fields(item)
    elif isinstance(value, list):
        for item in value:
            assert_no_answer_fields(item)


def _schema_validator(schema_path: Path, *, with_typed_decision: bool = False):
    try:
        schema = read_json(schema_path)
        Draft202012Validator.check_schema(schema)
        registry = Registry()
        if with_typed_decision:
            decision_schema = read_json(TYPED_DECISION_SCHEMA_PATH)
            Draft202012Validator.check_schema(decision_schema)
            registry = registry.with_resource(
                decision_schema["$id"], Resource.from_contents(decision_schema)
            )
        return Draft202012Validator(
            schema, registry=registry, format_checker=FormatChecker()
        )
    except (OSError, UnicodeError, json.JSONDecodeError, SchemaError) as exc:
        raise SemanticContractError("contract_schema_invalid") from exc


def _domain_ref(pack: LoadedDomainPack) -> dict[str, str]:
    return {
        "domain_pack_id": pack.domain_pack_id,
        "version": pack.version,
        "manifest_sha256": pack.manifest_sha256,
    }


def _assert_pack_ref(reference: Mapping[str, Any], pack: LoadedDomainPack) -> None:
    if dict(reference) != _domain_ref(pack):
        raise SemanticContractError("domain_pack_ref_mismatch")


def _unique_by(items: Sequence[Mapping[str, Any]], field: str, code: str) -> None:
    values = [str(item[field]) for item in items]
    if len(values) != len(set(values)):
        raise SemanticContractError(code)


def _assert_pack_refs(pack: LoadedDomainPack, refs: Iterable[str]) -> None:
    try:
        for reference in refs:
            pack.resolve(str(reference))
    except DomainPackError as exc:
        raise SemanticContractError("evidence_ref_not_in_domain_pack") from exc


def _request_observation_map(request: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    observations = request["session"]["slot_observations"]
    _unique_by(observations, "slot_id", "duplicate_slot_observation")
    return {
        str(item["slot_id"]): {
            "values": list(item["values"]),
            "evidence_refs": list(item["evidence_refs"]),
        }
        for item in observations
    }


def _result_observation_map(result: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    observations = result["slot_observations"]
    _unique_by(observations, "slot_id", "duplicate_slot_observation")
    return {
        str(item["slot_id"]): {
            "values": list(item["values"]),
            "evidence_refs": list(item["evidence_refs"]),
        }
        for item in observations
    }


def validate_request(
    request: Mapping[str, Any], pack: LoadedDomainPack
) -> dict[str, Any]:
    assert_no_answer_fields(request)
    try:
        _schema_validator(REQUEST_SCHEMA_PATH).validate(request)
    except ValidationError as exc:
        raise SemanticContractError("request_schema_invalid") from exc
    _assert_pack_ref(request["domain_context"]["domain_pack_ref"], pack)

    catalog = request["domain_context"]["authorized_catalog"]
    context = pack.model_context(max_data_level=str(catalog["max_data_level"]))
    model_visible: set[str] = set()
    for role, artifact in context["artifacts"].items():
        if role not in ALLOWED_CANDIDATE_ROLES:
            continue
        id_field = {
            "concepts": "concept_id",
            "schema_catalog": "object_id",
            "slots": "slot_id",
        }[role]
        model_visible.update(str(item[id_field]) for item in artifact["items"])
    candidate_ids = set(str(item) for item in catalog["candidate_ids"])
    if not candidate_ids <= model_visible:
        raise SemanticContractError("unauthorized_catalog_candidate")

    raw_observations = _request_observation_map(request)
    try:
        observations = build_slot_observations(raw_observations)
        RequiredSlotResolver(pack).resolve(
            intent="unknown", candidates=(), observations=observations
        )
    except SelectiveRoutingError as exc:
        raise SemanticContractError("invalid_session_observation") from exc
    return {
        "authorized_candidate_ids": candidate_ids,
        "session_observations": raw_observations,
    }


def _candidate_objects(
    result: Mapping[str, Any], authorized_ids: set[str], pack: LoadedDomainPack
) -> tuple[CandidateEvidence, ...]:
    records = result["candidates"]
    _unique_by(records, "candidate_id", "duplicate_candidate_id")
    candidates: list[CandidateEvidence] = []
    for record in records:
        candidate_id = str(record["candidate_id"])
        if candidate_id not in authorized_ids:
            raise SemanticContractError("candidate_not_authorized")
        try:
            role = pack.resolve_role(candidate_id)
            pack_item = pack.resolve(candidate_id)
        except DomainPackError as exc:
            raise SemanticContractError("candidate_not_in_domain_pack") from exc
        if role not in ALLOWED_CANDIDATE_ROLES:
            raise SemanticContractError("invalid_candidate_role")
        if str(record["candidate_kind"]) != ROLE_TO_KIND[role]:
            raise SemanticContractError("candidate_kind_mismatch")
        if str(record["display_name"]) != str(pack_item["display_name"]):
            raise SemanticContractError("candidate_display_name_mismatch")
        if candidate_id not in record["evidence_refs"]:
            raise SemanticContractError("candidate_self_evidence_missing")
        _assert_pack_refs(pack, record["evidence_refs"])
        candidates.append(
            CandidateEvidence(
                candidate_id=candidate_id,
                display_name=str(record["display_name"]),
                candidate_kind=str(record["candidate_kind"]),
                raw_score=float(record["raw_score"]),
                evidence_refs=tuple(str(item) for item in record["evidence_refs"]),
                raw_features=dict(record["raw_features"]),
            )
        )
    try:
        EvidenceFeatureAssembler(pack).candidate_options(candidates)
    except TypedDecisionRuntimeError as exc:
        raise SemanticContractError(str(exc)) from exc
    return tuple(candidates)


def _validate_fragments_and_issues(
    result: Mapping[str, Any], candidates: Sequence[CandidateEvidence], pack: LoadedDomainPack
) -> dict[str, Mapping[str, Any]]:
    question = str(result["original_question"])
    candidate_ids = {item.candidate_id for item in candidates}
    fragments = result["fragments"]
    _unique_by(fragments, "fragment_id", "duplicate_fragment_id")
    by_id: dict[str, Mapping[str, Any]] = {}
    previous_end = 0
    for fragment in sorted(fragments, key=lambda item: (item["start"], item["end"])):
        start, end = int(fragment["start"]), int(fragment["end"])
        if start >= end or end > len(question):
            raise SemanticContractError("fragment_position_invalid")
        if start < previous_end:
            raise SemanticContractError("fragment_overlap")
        previous_end = end
        if fragment["text"] != question[start:end]:
            raise SemanticContractError("fragment_text_mismatch")
        fragment_candidates = set(str(item) for item in fragment["candidate_ids"])
        if not fragment_candidates <= candidate_ids:
            raise SemanticContractError("fragment_candidate_not_in_result")
        _assert_pack_refs(pack, fragment["evidence_refs"])
        status = str(fragment["status"])
        normalized_ref = fragment["normalized_ref"]
        if status == "resolved":
            if (
                normalized_ref is None
                or fragment["entity_type"] is None
                or normalized_ref not in fragment_candidates
            ):
                raise SemanticContractError("resolved_fragment_incomplete")
            _assert_pack_refs(pack, [str(normalized_ref)])
        elif status == "ambiguous":
            if normalized_ref is not None or len(fragment_candidates) < 2:
                raise SemanticContractError("ambiguous_fragment_invalid")
        elif (
            normalized_ref is not None
            or fragment["entity_type"] is not None
            or fragment_candidates
        ):
            raise SemanticContractError("unknown_fragment_must_not_resolve")
        by_id[str(fragment["fragment_id"])] = fragment

    issues = result["issues"]
    _unique_by(issues, "issue_id", "duplicate_issue_id")
    issue_links: dict[str, set[str]] = {key: set() for key in by_id}
    for issue in issues:
        issue_candidates = set(str(item) for item in issue["candidate_ids"])
        if not issue_candidates <= candidate_ids:
            raise SemanticContractError("issue_candidate_not_in_result")
        for fragment_ref in issue["fragment_refs"]:
            if fragment_ref not in by_id:
                raise SemanticContractError("issue_fragment_not_in_result")
            issue_links[str(fragment_ref)].add(str(issue["kind"]))
        _assert_pack_refs(pack, issue["evidence_refs"])
    for fragment_id, fragment in by_id.items():
        status = str(fragment["status"])
        if status == "ambiguous" and "ambiguity" not in issue_links[fragment_id]:
            raise SemanticContractError("ambiguous_fragment_issue_missing")
        if status == "unknown" and "unknown" not in issue_links[fragment_id]:
            raise SemanticContractError("unknown_fragment_issue_missing")
    return by_id


def _validate_edits(
    result: Mapping[str, Any],
    fragments: Mapping[str, Mapping[str, Any]],
    candidates: Sequence[CandidateEvidence],
    pack: LoadedDomainPack,
) -> None:
    question = str(result["original_question"])
    candidate_map = {item.candidate_id: item for item in candidates}
    edits = result["edits"]
    _unique_by(edits, "edit_id", "duplicate_edit_id")
    previous_end = 0
    for edit in sorted(edits, key=lambda item: (item["start"], item["end"])):
        start, end = int(edit["start"]), int(edit["end"])
        if start >= end or end > len(question):
            raise SemanticContractError("edit_position_invalid")
        if start < previous_end:
            raise SemanticContractError("edit_overlap")
        previous_end = end
        if edit["before"] != question[start:end]:
            raise SemanticContractError("edit_before_mismatch")
        fragment = fragments.get(str(edit["source_fragment_id"]))
        if (
            fragment is None
            or int(fragment["start"]) != start
            or int(fragment["end"]) != end
            or fragment["status"] != "resolved"
        ):
            raise SemanticContractError("edit_fragment_mismatch")
        normalized_ref = str(fragment["normalized_ref"])
        candidate = candidate_map.get(normalized_ref)
        if candidate is None:
            raise SemanticContractError("edit_candidate_missing")
        pack_item = pack.resolve(normalized_ref)
        if edit["after"] != pack_item["display_name"]:
            raise SemanticContractError("unfounded_edit_replacement")
        _assert_pack_refs(pack, edit["evidence_refs"])
        allowed_evidence = set(fragment["evidence_refs"]) | set(candidate.evidence_refs)
        if not set(edit["evidence_refs"]) <= allowed_evidence:
            raise SemanticContractError("edit_evidence_not_linked")

    rewritten = question
    for edit in reversed(sorted(edits, key=lambda item: item["start"])):
        rewritten = (
            rewritten[: int(edit["start"])]
            + str(edit["after"])
            + rewritten[int(edit["end"]) :]
        )
    if rewritten != result["rewritten_question"]:
        raise SemanticContractError("rewritten_question_not_reconstructable")


def _validate_slot_resolution(
    request_state: Mapping[str, Any],
    result: Mapping[str, Any],
    candidates: Sequence[CandidateEvidence],
    pack: LoadedDomainPack,
) -> None:
    fragment_ids = {str(item["fragment_id"]) for item in result["fragments"]}
    for observation in result["slot_observations"]:
        if not set(observation["fragment_refs"]) <= fragment_ids:
            raise SemanticContractError("slot_fragment_not_in_result")
    result_raw = _result_observation_map(result)
    for slot_id, session_value in request_state.items():
        if result_raw.get(slot_id) != session_value:
            raise SemanticContractError("session_observation_not_preserved")
    try:
        observations = build_slot_observations(result_raw)
        resolution = RequiredSlotResolver(pack).resolve(
            intent=str(result["intent"]),
            candidates=candidates,
            observations=observations,
        )
    except SelectiveRoutingError as exc:
        raise SemanticContractError("required_slot_resolution_invalid") from exc
    rebuilt = {
        "source": "auto_domain_pack",
        "assessments": [item.to_dict() for item in resolution.assessments],
        "hard_rule_violations": list(resolution.hard_rule_violations),
        "soft_rule_violations": list(resolution.soft_rule_violations),
    }
    if rebuilt != result["required_slot_resolution"]:
        raise SemanticContractError("required_slot_resolution_mismatch")


def validate_result(
    request: Mapping[str, Any],
    result: Mapping[str, Any],
    pack: LoadedDomainPack,
    request_state: Mapping[str, Any],
    authorized_ids: set[str],
) -> None:
    assert_no_answer_fields(result)
    try:
        _schema_validator(RESULT_SCHEMA_PATH, with_typed_decision=True).validate(result)
    except ValidationError as exc:
        raise SemanticContractError("result_schema_invalid") from exc
    if result["request_id"] != request["request_id"]:
        raise SemanticContractError("request_id_mismatch")
    if result["original_question"] != request["question"]:
        raise SemanticContractError("original_question_mismatch")
    _assert_pack_ref(result["domain_pack_ref"], pack)

    candidates = _candidate_objects(result, authorized_ids, pack)
    candidate_ids = {item.candidate_id for item in candidates}
    try:
        EvidenceFeatureAssembler(pack).validate_decision(result["typed_decision"])
    except TypedDecisionRuntimeError as exc:
        raise SemanticContractError(str(exc)) from exc
    decision_ids = {
        str(item["candidate_id"])
        for item in result["typed_decision"]["candidate_options"]
    }
    if decision_ids != candidate_ids:
        raise SemanticContractError("typed_decision_candidates_mismatch")

    fragments = _validate_fragments_and_issues(result, candidates, pack)
    _validate_edits(result, fragments, candidates, pack)
    _validate_slot_resolution(request_state, result, candidates, pack)


def validate_contract_pair(
    request: Mapping[str, Any],
    result: Mapping[str, Any],
    pack: LoadedDomainPack,
) -> None:
    state = validate_request(request, pack)
    validate_result(
        request,
        result,
        pack,
        state["session_observations"],
        state["authorized_candidate_ids"],
    )


def _apply_mutation(value: Any, mutation: Mapping[str, Any]) -> None:
    parts = [part.replace("~1", "/").replace("~0", "~") for part in str(mutation["path"]).split("/")[1:]]
    parent = value
    for part in parts[:-1]:
        parent = parent[int(part)] if isinstance(parent, list) else parent[part]
    key = parts[-1]
    operation = mutation["op"]
    if operation == "replace":
        if isinstance(parent, list):
            parent[int(key)] = deepcopy(mutation["value"])
        else:
            parent[key] = deepcopy(mutation["value"])
    elif operation == "add":
        if isinstance(parent, list):
            if key == "-":
                parent.append(deepcopy(mutation["value"]))
            else:
                parent.insert(int(key), deepcopy(mutation["value"]))
        else:
            parent[key] = deepcopy(mutation["value"])
    elif operation == "remove":
        if isinstance(parent, list):
            del parent[int(key)]
        else:
            del parent[key]
    else:
        raise SemanticContractError("invalid_mutation_spec")


def run_invalid_mutations(
    valid_fixture: Mapping[str, Any], pack: LoadedDomainPack
) -> list[dict[str, str]]:
    fixture = read_json(INVALID_EXAMPLE_PATH)
    cases = fixture.get("cases")
    if not isinstance(cases, list) or not cases:
        raise SemanticContractError("invalid_mutation_fixture")
    outcomes: list[dict[str, str]] = []
    for case in cases:
        request = deepcopy(valid_fixture["request"])
        result = deepcopy(valid_fixture["result"])
        target = request if case["target"] == "request" else result
        for mutation in case["mutations"]:
            _apply_mutation(target, mutation)
        try:
            validate_contract_pair(request, result, pack)
        except SemanticContractError as exc:
            actual = str(exc)
        else:
            raise SemanticContractError("invalid_mutation_accepted")
        if actual != case["violation_code"]:
            raise SemanticContractError(
                f"mutation_code_mismatch:{case['case_id']}:{actual}"
            )
        outcomes.append({"case_id": str(case["case_id"]), "violation_code": actual})
    return outcomes


def build_report() -> dict[str, Any]:
    request_schema = read_json(REQUEST_SCHEMA_PATH)
    result_schema = read_json(RESULT_SCHEMA_PATH)
    Draft202012Validator.check_schema(request_schema)
    Draft202012Validator.check_schema(result_schema)

    pack = DomainPackRegistry(PACK_ROOT).load_path(PACK_PATH)
    valid_fixture = read_json(VALID_EXAMPLE_PATH)
    assert_no_answer_fields(valid_fixture)
    validate_contract_pair(valid_fixture["request"], valid_fixture["result"], pack)

    extensible = deepcopy(valid_fixture["result"])
    extensible["fragments"][0]["entity_type"] = "custom.artist_kind.v2"
    validate_contract_pair(valid_fixture["request"], extensible, pack)
    mutations = run_invalid_mutations(valid_fixture, pack)

    report: dict[str, Any] = {
        "report_version": "batch1-semantic-contracts.v1",
        "status": "pass",
        "scope": "public_structure_and_invariant_validation_only",
        "schemas": {
            "request": {
                "id": request_schema["$id"],
                "sha256": canonical_sha256(REQUEST_SCHEMA_PATH),
            },
            "result": {
                "id": result_schema["$id"],
                "sha256": canonical_sha256(RESULT_SCHEMA_PATH),
            },
            "typed_decision_reused": "typed-decision.v1",
        },
        "fixtures": {
            "valid_case_count": 1,
            "valid_fixture_sha256": canonical_sha256(VALID_EXAMPLE_PATH),
            "invalid_case_count": len(mutations),
            "invalid_fixture_sha256": canonical_sha256(INVALID_EXAMPLE_PATH),
            "mutation_outcomes": mutations,
            "extensible_entity_type_case": "pass",
        },
        "invariants": {
            "unicode_code_point_half_open_spans": "pass",
            "non_overlapping_exact_reconstruction_edits": "pass",
            "candidate_selection_and_evidence_resolve_to_domain_pack": "pass",
            "required_slots_recomputed_by_required_slot_resolver": "pass",
            "forbidden_answer_fields_rejected": "pass",
            "sql_execution_authorized": False,
        },
        "boundaries": {
            "split": "public_contract_fixture",
            "validation_exposure_count": 0,
            "hidden_test_cases_read": 0,
            "model_api_calls": 0,
            "real_key_files_read": 0,
            "estimated_cost_cny": 0.0,
        },
        "not_proven": [
            "semantic_extraction_accuracy",
            "faithful_rewrite_accuracy_on_unseen_data",
            "cross_domain_generalization",
            "migration_cost_target",
            "automatic_required_slot_quality",
            "business_answer_accuracy",
            "innovation_claim",
        ],
    }
    report["stable_sha256"] = canonical_hash(report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=REPORT_PATH)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        report = build_report()
    except (SemanticContractError, DomainPackError, SchemaError) as exc:
        print(json.dumps({"status": "fail", "error": str(exc)}, ensure_ascii=False))
        return 1
    if args.check:
        if not args.report.is_file():
            print(json.dumps({"status": "fail", "error": "committed_report_missing"}))
            return 1
        if read_json(args.report) != report:
            print(
                json.dumps(
                    {
                        "status": "fail",
                        "error": "committed_report_drifted",
                        "rebuilt_stable_sha256": report["stable_sha256"],
                    },
                    ensure_ascii=False,
                )
            )
            return 1
        print(json.dumps({"status": "pass", "stable_sha256": report["stable_sha256"]}))
        return 0
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "SemanticContractError",
    "build_report",
    "run_invalid_mutations",
    "validate_contract_pair",
    "validate_request",
    "validate_result",
]
