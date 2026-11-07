"""Offline validation for the frozen D4-00 evaluation control plane.

The validator reads only repository assets.  It never loads credentials,
constructs model requests, performs network I/O, or executes SQL.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError, ValidationError
from referencing import Registry, Resource


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_ROOT = PROJECT_ROOT / "configs" / "schemas" / "d4"
PROTOCOL_PATH = PROJECT_ROOT / "configs" / "evaluation" / "d4" / "experiment-protocol.v1.json"
DATA_MANIFEST_PATH = PROJECT_ROOT / "configs" / "evaluation" / "d4" / "innovation-eval-manifest.v2.template.json"
EXPOSURE_PATH = PROJECT_ROOT / "configs" / "evaluation" / "d4" / "validation-exposure.v1.json"
DOMAIN_PACK_PATH = PROJECT_ROOT / "domain-packs" / "templates" / "example" / "v1"
DECISION_SAMPLES_PATH = PROJECT_ROOT / "tests" / "fixtures" / "d4-evaluation" / "v1" / "typed-decision-samples.json"
TRACE_SAMPLE_PATH = PROJECT_ROOT / "tests" / "fixtures" / "d4-evaluation" / "v1" / "typed-decision-trace-event.sample.json"
APPROVED_MODEL = "qwen3.7-flash"
EXPECTED_GROUPS = {"B0", "B1", "B2", "P", "P-uncalibrated"}
EXPECTED_DOMAIN_FILES = {
    "concepts": "concepts.json",
    "aliases": "aliases.json",
    "slots": "slots.json",
    "schema_catalog": "schema-catalog.json",
    "relations": "relations.json",
    "value_evidence": "value-evidence.json",
    "validation_rules": "validation-rules.json",
    "license_notice": "LICENSE-or-SOURCE.md",
}
EXPECTED_COMPONENTS = {
    "B0": {
        "enabled": {"fixed_prompt", "llm_semantic_judgment"},
        "disabled": {"typed_decision_contract", "domain_pack", "deterministic_rules", "value_evidence", "evidence_fusion", "empirical_calibration", "selective_routing", "fixed_rule_threshold"},
    },
    "B1": {
        "enabled": {"fixed_prompt", "llm_semantic_judgment", "typed_decision_contract"},
        "disabled": {"domain_pack", "deterministic_rules", "value_evidence", "evidence_fusion", "empirical_calibration", "selective_routing", "fixed_rule_threshold"},
    },
    "B2": {
        "enabled": {"domain_pack", "deterministic_rules", "value_evidence", "evidence_fusion", "selective_routing", "fixed_rule_threshold"},
        "disabled": {"fixed_prompt", "llm_semantic_judgment", "typed_decision_contract", "empirical_calibration"},
    },
    "P": {
        "enabled": {"fixed_prompt", "llm_semantic_judgment", "typed_decision_contract", "domain_pack", "deterministic_rules", "value_evidence", "evidence_fusion", "empirical_calibration", "selective_routing"},
        "disabled": {"fixed_rule_threshold"},
    },
    "P-uncalibrated": {
        "enabled": {"fixed_prompt", "llm_semantic_judgment", "typed_decision_contract", "domain_pack", "deterministic_rules", "value_evidence", "evidence_fusion", "selective_routing"},
        "disabled": {"empirical_calibration", "fixed_rule_threshold"},
    },
}


class D4ValidationError(RuntimeError):
    """Raised for stable, content-safe D4 validation failures."""


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_raw(path: Path) -> str:
    """Hash protected legacy text with its frozen Windows CRLF convention.

    Protected D3 hashes were recorded from a Windows checkout.  Normalizing
    checkout line endings before hashing keeps those frozen values verifiable
    on Linux and GitHub archives without weakening content integrity.
    """
    content = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return hashlib.sha256(content.replace(b"\n", b"\r\n")).hexdigest()


def sha256(path: Path) -> str:
    """Hash a D4 text asset after stable UTF-8/LF canonicalization."""
    content = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return hashlib.sha256(content).hexdigest()


def validate_instance(instance: Any, schema_path: Path) -> None:
    schema = read_json(schema_path)
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(instance)


def _forbidden_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_").replace(" ", "_")
    return (
        "expected" in normalized
        or "gold" in normalized
        or normalized in {"label", "labels", "answer", "answers", "target_answer"}
    )


def assert_no_answer_fields(value: Any, *, location: str = "root") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if _forbidden_key(str(key)):
                raise D4ValidationError(f"forbidden answer-derived field at {location}")
            assert_no_answer_fields(item, location=f"{location}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            assert_no_answer_fields(item, location=f"{location}[{index}]")


def _unique_ids(items: Iterable[Mapping[str, Any]], field: str, artifact: str) -> set[str]:
    values = [item[field] for item in items]
    if len(values) != len(set(values)):
        raise D4ValidationError(f"duplicate {field} in {artifact}")
    return set(values)


def _assert_refs(refs: Iterable[str], known: set[str], location: str) -> None:
    dangling = sorted(set(refs) - known)
    if dangling:
        raise D4ValidationError(f"dangling reference in {location}")


def _assert_acyclic(parents: Mapping[str, Iterable[str]], location: str) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visiting:
            raise D4ValidationError(f"inheritance cycle in {location}")
        if node in visited:
            return
        visiting.add(node)
        for parent in parents.get(node, []):
            visit(parent)
        visiting.remove(node)
        visited.add(node)

    for node in parents:
        visit(node)


def validate_typed_decision(instance: Mapping[str, Any]) -> None:
    """Validate one decision's JSON Schema and cross-field references."""
    schema_path = SCHEMA_ROOT / "typed-decision.v1.schema.json"
    validate_instance(instance, schema_path)
    candidate_ids = [item["candidate_id"] for item in instance["candidate_options"]]
    if len(candidate_ids) != len(set(candidate_ids)):
        raise D4ValidationError("typed decision candidate IDs must be unique")
    selected = instance["selected_option"]
    if selected is not None and selected not in candidate_ids:
        raise D4ValidationError("selected_option must reference a candidate")


def validate_typed_decisions() -> dict[str, Any]:
    schema_path = SCHEMA_ROOT / "typed-decision.v1.schema.json"
    fixture = read_json(DECISION_SAMPLES_PATH)
    if fixture.get("schema_id") != "typed-decision.v1":
        raise D4ValidationError("typed decision fixture schema_id mismatch")
    samples = fixture.get("samples")
    if not isinstance(samples, list):
        raise D4ValidationError("typed decision samples must be an array")
    decision_types: set[str] = set()
    for sample in samples:
        validate_typed_decision(sample)
        decision_types.add(sample["decision_type"])
    if decision_types != {"boolean", "choice", "score", "abstain"}:
        raise D4ValidationError("typed decision fixture must cover all four types")
    return {
        "schema_id": "typed-decision.v1",
        "schema_sha256": sha256(schema_path),
        "sample_count": len(samples),
        "decision_types": sorted(decision_types),
    }


def validate_trace_schema() -> dict[str, Any]:
    decision_schema = read_json(SCHEMA_ROOT / "typed-decision.v1.schema.json")
    trace_path = SCHEMA_ROOT / "typed-decision-trace-event.v1.schema.json"
    trace_schema = read_json(trace_path)
    registry = Registry().with_resource(
        decision_schema["$id"], Resource.from_contents(decision_schema)
    )
    Draft202012Validator.check_schema(trace_schema)
    sample = read_json(TRACE_SAMPLE_PATH)
    Draft202012Validator(
        trace_schema,
        registry=registry,
        format_checker=FormatChecker(),
    ).validate(sample)
    if sample["executed_action"] != sample["decision"]["recommended_action"]:
        raise D4ValidationError("trace executed_action must match the decision")
    if sample["domain_pack_ref"]["manifest_sha256"] != sha256(DOMAIN_PACK_PATH / "manifest.json"):
        raise D4ValidationError("trace domain pack hash mismatch")
    return {
        "schema_id": trace_schema["$id"],
        "schema_sha256": sha256(trace_path),
        "sample_count": 1,
    }


def validate_domain_pack(pack_path: Path = DOMAIN_PACK_PATH) -> dict[str, Any]:
    manifest = read_json(pack_path / "manifest.json")
    validate_instance(manifest, SCHEMA_ROOT / "domain-pack-manifest.v1.schema.json")
    assert_no_answer_fields(manifest, location="domain_pack.manifest")

    entries = manifest["files"]
    roles = [entry["role"] for entry in entries]
    paths = [entry["path"] for entry in entries]
    if len(roles) != len(set(roles)) or len(paths) != len(set(paths)):
        raise D4ValidationError("domain pack roles and paths must be unique")
    if {entry["role"]: entry["path"] for entry in entries} != EXPECTED_DOMAIN_FILES:
        raise D4ValidationError("domain pack file set is not the frozen v1 set")

    artifacts: dict[str, dict[str, Any]] = {}
    root = pack_path.resolve()
    for entry in entries:
        file_path = (root / entry["path"]).resolve()
        try:
            file_path.relative_to(root)
        except ValueError as exc:
            raise D4ValidationError("domain pack path escapes pack root") from exc
        if not file_path.is_file() or sha256(file_path) != entry["sha256"]:
            raise D4ValidationError(f"domain pack hash mismatch for {entry['role']}")
        if entry["data_level"] in {"L2", "L3"} and entry["send_to_model"]:
            raise D4ValidationError("L2/L3 domain pack files cannot be sent to the model")
        if entry["role"] == "license_notice":
            continue
        artifact = read_json(file_path)
        validate_instance(artifact, SCHEMA_ROOT / "domain-pack-content.v1.schema.json")
        if artifact["artifact_type"] != entry["role"]:
            raise D4ValidationError("domain pack role does not match artifact_type")
        assert_no_answer_fields(artifact, location=f"domain_pack.{entry['role']}")
        artifacts[entry["role"]] = artifact

    source_ids = {item["source_id"] for item in manifest["source_refs"]}
    concepts = artifacts["concepts"]["items"]
    concept_ids = _unique_ids(concepts, "concept_id", "concepts")
    concept_parents = {item["concept_id"]: item["parent_concept_ids"] for item in concepts}
    for item in concepts:
        _assert_refs(item["parent_concept_ids"], concept_ids, item["concept_id"])
        _assert_refs(item["source_refs"], source_ids, item["concept_id"])
    _assert_acyclic(concept_parents, "concepts")

    aliases = artifacts["aliases"]["items"]
    _unique_ids(aliases, "alias_id", "aliases")
    for item in aliases:
        _assert_refs([item["concept_id"]], concept_ids, item["alias_id"])
        _assert_refs([item["source_ref"]], source_ids, item["alias_id"])

    slots = artifacts["slots"]["items"]
    slot_ids = _unique_ids(slots, "slot_id", "slots")
    schema_objects = artifacts["schema_catalog"]["items"]
    object_ids = _unique_ids(schema_objects, "object_id", "schema_catalog")
    evidence = artifacts["value_evidence"]["items"]
    evidence_ids = _unique_ids(evidence, "evidence_id", "value_evidence")
    known_condition_subjects = concept_ids | slot_ids | object_ids
    for item in slots:
        _assert_refs(item["conflict_slot_ids"], slot_ids, item["slot_id"])
        _assert_refs(item["allowed_value_evidence_refs"], evidence_ids, item["slot_id"])
        _assert_refs(item["source_refs"], source_ids, item["slot_id"])
        for condition in item["required_when"]:
            _assert_refs([condition["subject_ref"]], known_condition_subjects, condition["condition_id"])

    object_parents: dict[str, list[str]] = {}
    for item in schema_objects:
        parent = item["parent_object_id"]
        object_parents[item["object_id"]] = [] if parent is None else [parent]
        if parent is not None:
            _assert_refs([parent], object_ids, item["object_id"])
        _assert_refs(item["concept_refs"], concept_ids, item["object_id"])
        _assert_refs(item["source_refs"], source_ids, item["object_id"])
    _assert_acyclic(object_parents, "schema_catalog")

    relations = artifacts["relations"]["items"]
    _unique_ids(relations, "relation_id", "relations")
    for item in relations:
        _assert_refs([item["source_object_id"], item["target_object_id"]], object_ids, item["relation_id"])
        _assert_refs(item["source_refs"], source_ids, item["relation_id"])

    for item in evidence:
        _assert_refs([item["slot_id"]], slot_ids, item["evidence_id"])
        _assert_refs([item["source_ref"]], source_ids, item["evidence_id"])
        if item["data_level"] in {"L2", "L3"} and item["send_to_model"]:
            raise D4ValidationError("L2/L3 value evidence cannot be sent to the model")

    rules = artifacts["validation_rules"]["items"]
    _unique_ids(rules, "rule_id", "validation_rules")
    known_inputs = concept_ids | slot_ids | object_ids
    for item in rules:
        _assert_refs(item["input_refs"], known_inputs, item["rule_id"])
        _assert_refs(item["evidence_refs"], evidence_ids, item["rule_id"])
        _assert_refs(item["source_refs"], source_ids, item["rule_id"])

    return {
        "domain_pack_id": manifest["domain_pack_id"],
        "version": manifest["version"],
        "manifest_sha256": sha256(pack_path / "manifest.json"),
        "file_count": len(entries),
        "item_counts": {role: len(value["items"]) for role, value in sorted(artifacts.items())},
        "dangling_reference_count": 0,
        "inheritance_cycle_count": 0,
        "hash_mismatch_count": 0,
        "forbidden_answer_field_count": 0,
    }


def _validate_provider_boundary(protocol: Mapping[str, Any]) -> dict[str, Any]:
    provider_paths = sorted((PROJECT_ROOT / "configs" / "providers").glob("*.json"))
    providers = [read_json(path) for path in provider_paths]
    if len(providers) != 1:
        raise D4ValidationError("exactly one provider configuration is allowed")
    provider = providers[0]
    if provider.get("model") != APPROVED_MODEL:
        raise D4ValidationError("provider model is not qwen3.7-flash")
    if provider.get("provider_id") != "bailian-cn-beijing":
        raise D4ValidationError("unexpected provider ID")
    boundary = protocol["model_boundary"]
    if boundary["online_models"] != [APPROVED_MODEL] or boundary["provider_ids"] != [provider["provider_id"]]:
        raise D4ValidationError("experiment model boundary does not match provider config")
    if boundary["second_provider_enabled"] or boundary["fallback_enabled"] or boundary["jev_api_enabled"]:
        raise D4ValidationError("second provider, fallback, and Jev API must remain disabled")
    return {
        "provider_config_count": 1,
        "provider_id": provider["provider_id"],
        "model": provider["model"],
        "fallback_enabled": False,
        "jev_api_enabled": False,
    }


def validate_experiment_protocol() -> dict[str, Any]:
    protocol = read_json(PROTOCOL_PATH)
    validate_instance(protocol, SCHEMA_ROOT / "experiment-protocol.v1.schema.json")

    policy_path = PROJECT_ROOT / protocol["shared_controls"]["policy_path"]
    policy = read_json(policy_path)
    if sha256_raw(policy_path) != protocol["shared_controls"]["policy_sha256"]:
        raise D4ValidationError("loop policy hash mismatch")
    budget = protocol["shared_controls"]["budget"]
    for key, value in budget.items():
        if policy["limits"][key] != value:
            raise D4ValidationError("experiment budget must match the frozen LoopGuard policy")

    groups = {item["group_id"]: item for item in protocol["groups"]}
    if set(groups) != EXPECTED_GROUPS:
        raise D4ValidationError("experiment group set drifted")
    for group_id, expected in EXPECTED_COMPONENTS.items():
        group = groups[group_id]
        enabled = set(group["enabled_components"])
        disabled = set(group["disabled_components"])
        if enabled != expected["enabled"] or disabled != expected["disabled"]:
            raise D4ValidationError(f"component matrix drifted for {group_id}")
        if enabled & disabled:
            raise D4ValidationError(f"component enabled and disabled in {group_id}")
    if groups["B2"]["uses_model"] or any(not groups[item]["uses_model"] for item in EXPECTED_GROUPS - {"B2"}):
        raise D4ValidationError("only B2 may disable model use")

    data_manifest = read_json(DATA_MANIFEST_PATH)
    validate_instance(data_manifest, SCHEMA_ROOT / "innovation-eval-manifest.v2.schema.json")
    assert_no_answer_fields(data_manifest, location="innovation_eval_manifest")
    data_ref = protocol["data_manifest"]
    if sha256(PROJECT_ROOT / data_ref["path"]) != data_ref["sha256"]:
        raise D4ValidationError("innovation data manifest hash mismatch")
    if data_manifest["split_plan"]["total"] != 240:
        raise D4ValidationError("innovation gate must remain 240 cases")
    gates = protocol["data_gates"]
    if gates["competition_candidate"]["minimum_total_cases"] != 180 or gates["innovation_claim"]["minimum_total_cases"] != 240:
        raise D4ValidationError("180-case and 240-case gates must remain distinct")

    exposure = read_json(EXPOSURE_PATH)
    exposure_ref = protocol["validation_exposure"]
    if sha256(EXPOSURE_PATH) != exposure_ref["register_sha256"]:
        raise D4ValidationError("validation exposure register hash mismatch")
    if exposure["historical_exposure_count"] != 0 or exposure["current_task_exposure_count"] != 0 or exposure["entries"]:
        raise D4ValidationError("D4-00 must not expose validation data")
    if exposure["maximum_exposures"] != exposure_ref["maximum_per_prompt_major"]:
        raise D4ValidationError("validation exposure limit mismatch")

    for relative_path, expected_hash in protocol["protected_d3_assets"].items():
        path = (PROJECT_ROOT / relative_path).resolve()
        try:
            path.relative_to(PROJECT_ROOT.resolve())
        except ValueError as exc:
            raise D4ValidationError("protected D3 asset path escapes repository") from exc
        if not path.is_file() or sha256_raw(path) != expected_hash:
            raise D4ValidationError("protected D3 asset changed without a version bump")

    return {
        "protocol_id": protocol["protocol_id"],
        "version": protocol["version"],
        "protocol_sha256": sha256(PROTOCOL_PATH),
        "group_count": len(groups),
        "group_ids": sorted(groups),
        "competition_candidate_gate": 180,
        "innovation_claim_gate": 240,
        "validation_historical_exposure_count": 0,
        "validation_current_task_exposure_count": 0,
        "protected_d3_asset_count": len(protocol["protected_d3_assets"]),
        "provider_boundary": _validate_provider_boundary(protocol),
    }


def validate_all() -> dict[str, Any]:
    schema_paths = sorted(SCHEMA_ROOT.glob("*.json"))
    for path in schema_paths:
        Draft202012Validator.check_schema(read_json(path))
    typed = validate_typed_decisions()
    trace = validate_trace_schema()
    domain_pack = validate_domain_pack()
    protocol = validate_experiment_protocol()
    return {
        "status": "pass",
        "report_type": "d4_evaluation_freeze_offline_validation",
        "task_id": "D4-00",
        "schema_count": len(schema_paths),
        "typed_decision": typed,
        "trace_event": trace,
        "domain_pack": domain_pack,
        "experiment_protocol": protocol,
        "hidden_test_content_committed": False,
        "runtime_implemented": False,
        "innovation_proven": False,
        "generalization_proven": False,
        "network_calls": 0,
        "model_api_calls": 0,
        "estimated_cost_cny": 0.0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="离线校验 D4-00 冻结契约和实验控制面。")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    try:
        report = validate_all()
    except (
        D4ValidationError,
        SchemaError,
        ValidationError,
        OSError,
        KeyError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        print(json.dumps({"status": "fail", "error_code": type(exc).__name__}, ensure_ascii=False, indent=2))
        return 1
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
