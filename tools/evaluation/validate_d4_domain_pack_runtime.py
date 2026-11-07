"""Generate the content-safe D4-03 A-side offline runtime report.

Only repository-owned public assets and synthetic component inputs are read.
This command never loads credentials, performs network/model calls, executes
SQL, or reads prompt_dev/validation/test cases.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
from typing import Any, Callable


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(PROJECT_ROOT))

from src.backend.agent import (  # noqa: E402
    CandidateRetriever,
    DomainPackError,
    DomainPackRegistry,
    EvidenceFeatureAssembler,
    canonical_text_sha256,
)


PACK_ROOT = PROJECT_ROOT / "domain-packs" / "chinook" / "v1"
APPROVED_MODEL = "qwen3.7-flash"


class RuntimeValidationError(RuntimeError):
    pass


def _json_hash(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _query_hash(query: str) -> str:
    return hashlib.sha256(query.encode("utf-8")).hexdigest()


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _refresh_hash(pack_path: Path, role: str) -> None:
    manifest = _read_json(pack_path / "manifest.json")
    entry = next(item for item in manifest["files"] if item["role"] == role)
    entry["sha256"] = canonical_text_sha256(pack_path / entry["path"])
    _write_json(pack_path / "manifest.json", manifest)


def _expect_failure(
    name: str,
    mutation: Callable[[Path], None],
    expected_codes: set[str],
) -> dict[str, str]:
    with tempfile.TemporaryDirectory() as temp:
        pack_root = Path(temp) / "domain-packs"
        pack_path = pack_root / "chinook" / "v1"
        shutil.copytree(PACK_ROOT, pack_path)
        mutation(pack_path)
        try:
            DomainPackRegistry(pack_root).load("chinook_music_store", "1.0.0")
        except DomainPackError as exc:
            if exc.code not in expected_codes:
                raise RuntimeValidationError(
                    f"negative probe {name} returned unexpected code {exc.code}"
                ) from exc
            return {"probe": name, "error_code": exc.code}
        raise RuntimeValidationError(f"negative probe {name} did not fail closed")


def _negative_probes() -> list[dict[str, str]]:
    def hash_drift(path: Path) -> None:
        target = path / "aliases.json"
        target.write_text(target.read_text(encoding="utf-8") + " ", encoding="utf-8")

    def missing_file(path: Path) -> None:
        (path / "relations.json").unlink()

    def extra_file(path: Path) -> None:
        (path / "unexpected.json").write_text("{}\n", encoding="utf-8")

    def unknown_field(path: Path) -> None:
        value = _read_json(path / "concepts.json")
        value["items"][0]["unknown_runtime_hint"] = True
        _write_json(path / "concepts.json", value)
        _refresh_hash(path, "concepts")

    def duplicate_id(path: Path) -> None:
        value = _read_json(path / "aliases.json")
        value["items"][0]["alias_id"] = "concept.album"
        _write_json(path / "aliases.json", value)
        _refresh_hash(path, "aliases")

    def dangling_ref(path: Path) -> None:
        value = _read_json(path / "aliases.json")
        value["items"][0]["concept_id"] = "concept.not_present"
        _write_json(path / "aliases.json", value)
        _refresh_hash(path, "aliases")

    def inheritance_cycle(path: Path) -> None:
        value = _read_json(path / "concepts.json")
        root = next(
            item for item in value["items"] if item["concept_id"] == "concept.music_store"
        )
        root["parent_concept_ids"] = ["concept.album"]
        _write_json(path / "concepts.json", value)
        _refresh_hash(path, "concepts")

    def forbidden_answer(path: Path) -> None:
        value = _read_json(path / "concepts.json")
        value["items"][0]["expected_answer"] = "forbidden"
        _write_json(path / "concepts.json", value)
        _refresh_hash(path, "concepts")

    def unauthorized_model_data(path: Path) -> None:
        value = _read_json(path / "manifest.json")
        value["data_level"] = "L2"
        entry = next(item for item in value["files"] if item["role"] == "concepts")
        entry["data_level"] = "L2"
        entry["send_to_model"] = True
        _write_json(path / "manifest.json", value)

    cases = [
        ("hash_drift", hash_drift, {"hash_mismatch"}),
        ("missing_file", missing_file, {"unexpected_file_set"}),
        ("extra_file", extra_file, {"unexpected_file_set"}),
        ("unknown_field", unknown_field, {"schema_validation_failed"}),
        ("duplicate_id", duplicate_id, {"duplicate_id"}),
        ("dangling_reference", dangling_ref, {"dangling_reference"}),
        ("inheritance_cycle", inheritance_cycle, {"inheritance_cycle"}),
        ("forbidden_answer_field", forbidden_answer, {"forbidden_answer_field"}),
        (
            "unauthorized_model_data",
            unauthorized_model_data,
            {"model_data_level_not_allowed"},
        ),
    ]
    return [_expect_failure(name, mutation, codes) for name, mutation, codes in cases]


def _provider_boundary() -> dict[str, Any]:
    paths = sorted((PROJECT_ROOT / "configs" / "providers").glob("*.json"))
    providers = [_read_json(path) for path in paths]
    if len(providers) != 1 or providers[0].get("model") != APPROVED_MODEL:
        raise RuntimeValidationError("single qwen3.7-flash provider boundary changed")
    return {
        "provider_config_count": 1,
        "model": APPROVED_MODEL,
        "second_provider_enabled": False,
        "fallback_enabled": False,
        "jev_api_enabled": False,
    }


def validate_all() -> dict[str, Any]:
    registry = DomainPackRegistry()
    discovered = registry.discover()
    pack = registry.load("chinook_music_store", "1.0.0")
    retriever = CandidateRetriever(pack)
    assembler = EvidenceFeatureAssembler(pack)

    synthetic = [
        ("concept_alias", "artist album", "concept", 5),
        ("schema_relation", "Album ArtistId Artist", "schema_object", 10),
        ("slot_value", "revenue", "slot", 5),
        ("stable_tie", "Name", "schema_object", 7),
        ("no_match", "zzzxqv_not_a_domain_term", "all", 5),
    ]
    scenarios: list[dict[str, Any]] = []
    candidates_by_id: dict[str, tuple] = {}
    for scenario_id, query, kind, top_k in synthetic:
        first = retriever.retrieve(query, candidate_kind=kind, top_k=top_k)
        second = retriever.retrieve(query, candidate_kind=kind, top_k=top_k)
        if first != second:
            raise RuntimeValidationError("candidate retrieval is not deterministic")
        for candidate in first:
            pack.resolve(candidate.candidate_id)
            for evidence_ref in candidate.evidence_refs:
                pack.resolve(evidence_ref)
        summary = [
            {
                "candidate_id": item.candidate_id,
                "raw_score": item.raw_score,
                "evidence_refs": list(item.evidence_refs),
            }
            for item in first
        ]
        candidates_by_id[scenario_id] = first
        scenarios.append(
            {
                "scenario_id": scenario_id,
                "query_sha256": _query_hash(query),
                "candidate_kind": kind,
                "top_k": top_k,
                "candidate_count": len(first),
                "candidate_summary_sha256": _json_hash(summary),
                "stable_repeat": True,
            }
        )
    if scenarios[-1]["candidate_count"] != 0:
        raise RuntimeValidationError("no-match scenario forced a candidate")

    concept_candidates = candidates_by_id["concept_alias"][:2]
    if len(concept_candidates) != 2:
        raise RuntimeValidationError("synthetic concept scenario needs two candidates")
    decisions = [
        assembler.assemble_decision(
            decision_type="boolean",
            candidates=concept_candidates,
            selected_option=concept_candidates[0].candidate_id,
            raw_score=0.5,
            recommended_action="accept",
        ),
        assembler.assemble_decision(
            decision_type="choice",
            candidates=concept_candidates,
            selected_option=concept_candidates[0].candidate_id,
            raw_score=concept_candidates[0].raw_score,
            recommended_action="accept",
        ),
        assembler.assemble_decision(
            decision_type="score",
            candidates=concept_candidates,
            selected_option=None,
            raw_score=0.5,
            recommended_action="escalate",
        ),
        assembler.assemble_decision(
            decision_type="abstain",
            candidates=(),
            selected_option=None,
            raw_score=None,
            recommended_action="reject",
            abstain_reason="insufficient_evidence",
        ),
    ]
    trace = assembler.assemble_trace_event(
        request_id="d4-03-offline-synthetic",
        recorded_at="2026-09-27T00:00:00Z",
        decision=decisions[1],
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

    negative = _negative_probes()
    model_l0 = pack.model_context(max_data_level="L0")
    model_l1 = pack.model_context(max_data_level="L1")
    return {
        "status": "pass",
        "report_type": "d4_domain_pack_runtime_offline_validation",
        "task_id": "D4-03-A",
        "scope": "registry_retrieval_evidence_and_contract_wiring_only",
        "domain_pack": {
            "discovered": [list(item) for item in discovered],
            "domain_pack_id": pack.domain_pack_id,
            "version": pack.version,
            "manifest_sha256": pack.manifest_sha256,
            "artifact_item_counts": {
                role: len(pack.artifact_items(role))
                for role in sorted(pack.artifacts)
            },
            "model_context_l0_roles": sorted(model_l0["artifacts"]),
            "model_context_l1_roles": sorted(model_l1["artifacts"]),
            "l2_l3_model_export_allowed": False,
        },
        "candidate_retrieval": {
            "version": "candidate-retriever.v1",
            "synthetic_scenario_count": len(scenarios),
            "scenarios": scenarios,
            "top_k_hard_max": 50,
            "stable_tie_break": "raw_score_desc_then_candidate_id_asc",
            "forced_candidate_on_no_match": False,
            "query_text_stored": False,
        },
        "typed_contract": {
            "decision_schema_id": "typed-decision.v1",
            "trace_schema_id": "typed-decision-trace-event.v1",
            "decision_types_validated": sorted(
                decision["decision_type"] for decision in decisions
            ),
            "decision_hashes": [_json_hash(item) for item in decisions],
            "trace_sha256": _json_hash(trace),
            "calibration_version": None,
            "evidence_refs_resolved": True,
        },
        "fail_closed_probes": {
            "count": len(negative),
            "results": negative,
        },
        "input_isolation": {
            "prompt_dev_cases_read": 0,
            "validation_cases_read": 0,
            "test_cases_read": 0,
            "expected_gold_label_fields_read": False,
            "validation_historical_exposure_count": 0,
            "validation_current_task_exposure_count": 0,
            "hidden_test_exposure_count": 0,
        },
        "model_boundary": _provider_boundary(),
        "not_implemented": [
            "automatic_required_slots",
            "decision_threshold_routing",
            "empirical_calibration",
            "information_gain_clarification",
            "state_merger",
            "sql_or_rag_execution",
            "business_accuracy_or_recall_evaluation",
        ],
        "network_evaluation_calls": 0,
        "model_api_calls": 0,
        "credential_files_read": 0,
        "sql_executions": 0,
        "estimated_cost_cny": 0.0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="离线校验 D4-03 A 侧领域包运行时。")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    try:
        report = validate_all()
    except (DomainPackError, RuntimeValidationError) as exc:
        parser.error(str(exc))
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
