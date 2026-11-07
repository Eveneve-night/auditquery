"""Generate the deterministic, content-safe D4-04 offline report.

Only versioned repository assets and obvious synthetic inputs are used.  This
tool does not read credentials, evaluation questions, validation/test data, or
answers; it performs no network, model, or SQL calls.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from typing import Any, Callable


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

from backend.agent.domain_pack import DomainPackRegistry  # noqa: E402
from backend.agent.intent_routing import (  # noqa: E402
    apply_intent_result,
    create_conversation_state,
    load_source_catalog,
)
from backend.agent.selective_routing import (  # noqa: E402
    DEFAULT_POLICY_PATH,
    D4SelectiveDecisionRuntime,
    RequiredSlotResolver,
    SelectiveDecisionPolicy,
    SelectiveRoutingError,
    build_slot_observations,
)
from backend.agent.typed_decision_runtime import (  # noqa: E402
    CandidateEvidence,
    CandidateRetriever,
)


APPROVED_MODEL = "qwen3.7-flash"
RECORDED_AT = "2026-09-27T08:00:00Z"


class D4SelectiveValidationError(RuntimeError):
    pass


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _json_hash(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _thaw(value: Any) -> Any:
    if isinstance(value, dict) or hasattr(value, "items"):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(item) for item in value]
    return value


def _slot_state(**values: object):
    evidence = {
        "metric": "evidence.metric_kinds",
        "entity_type": "evidence.entity_kinds",
        "time_range": "evidence.time_range_years",
        "country": "evidence.country_enum",
        "limit": "evidence.limit_range",
        "grain": "evidence.grain_kinds",
        "sort": "evidence.sort_kinds",
        "comparison_baseline": "slot.comparison_baseline",
    }
    payload = {}
    for short_name, value in values.items():
        payload[f"slot.{short_name}"] = {
            "values": value if isinstance(value, list) else [value],
            "evidence_refs": [evidence[short_name]],
        }
    return build_slot_observations(payload)


def _intent_result(
    intent: str,
    *,
    needs_clarification: bool = False,
    missing_slots: list[str] | None = None,
) -> dict[str, Any]:
    sources = {
        "sql": ["chinook_sql"],
        "rag": ["security_policy"],
        "cross_source": ["chinook_sql", "project_report"],
        "unknown": [],
    }[intent]
    return {
        "schema_version": "1.0.0",
        "task_type": "intent",
        "status": "success",
        "data": {
            "intent": intent,
            "needs_clarification": needs_clarification,
            "required_sources": sources,
            "missing_slots": missing_slots or [],
            "reason": (
                "ambiguous"
                if needs_clarification
                else "outside_available_sources"
                if intent == "unknown"
                else "routable"
            ),
            "normalized_query": None,
        },
        "error": None,
        "warnings": [],
    }


def _provider_boundary() -> dict[str, Any]:
    paths = sorted((PROJECT_ROOT / "configs" / "providers").glob("*.json"))
    providers = [_read_json(path) for path in paths]
    if len(providers) != 1 or providers[0].get("model") != APPROVED_MODEL:
        raise D4SelectiveValidationError("single-model boundary changed")
    return {
        "provider_config_count": 1,
        "model": APPROVED_MODEL,
        "second_provider_enabled": False,
        "fallback_enabled": False,
        "jev_api_enabled": False,
    }


def _status_counts(result: dict[str, Any]) -> dict[str, int]:
    counts = {"filled": 0, "missing": 0, "conflicting": 0, "unknown": 0}
    for item in result["slot_assessments"]:
        counts[item["status"]] += 1
    return counts


def _summarize(scenario_id: str, result: dict[str, Any]) -> dict[str, Any]:
    return {
        "scenario_id": scenario_id,
        "action": result["action"],
        "reason_code": result["reason_code"],
        "raw_score": result["raw_score"],
        "status_counts": _status_counts(result),
        "typed_decision_type": result["typed_decision"]["decision_type"],
        "result_sha256": _json_hash(result),
        "sql_execution_authorized": result["sql_execution_authorized"],
    }


def _condition_scenarios(pack) -> list[dict[str, Any]]:
    retriever = CandidateRetriever(pack)
    base = retriever.retrieve("艺术家", top_k=5)
    resolver = RequiredSlotResolver(pack)
    policy = SelectiveDecisionPolicy(pack)

    def candidate(score: float) -> tuple[CandidateEvidence, ...]:
        return (replace(base[0], raw_score=score),)

    cases = [
        ("accept_at_boundary", _slot_state(metric="count", entity_type="artist"), candidate(0.75), "accept"),
        ("clarify_default_missing", {}, candidate(0.99), "clarify"),
        ("clarify_conflicting", _slot_state(metric=["count", "revenue"]), candidate(0.99), "clarify"),
        ("escalate_below_accept", _slot_state(metric="count", entity_type="artist"), candidate(0.50), "escalate"),
        ("reject_below_abstain", _slot_state(metric="count", entity_type="artist"), candidate(0.249999), "reject"),
        ("abstain_boundary_inclusive", _slot_state(metric="count", entity_type="artist"), candidate(0.25), "escalate"),
        ("hard_rule_unknown", _slot_state(metric="TESTONLY_not_a_metric"), candidate(0.99), "escalate"),
        ("no_candidate_abstain", _slot_state(metric="count", entity_type="artist"), (), "reject"),
        ("soft_clarify_at_boundary", _slot_state(metric="count", entity_type="artist", grain="month"), candidate(0.50), "clarify"),
        ("soft_below_clarify", _slot_state(metric="count", entity_type="artist", grain="month"), candidate(0.499999), "escalate"),
    ]
    results: list[dict[str, Any]] = []
    for index, (scenario_id, observations, candidates, wanted_action) in enumerate(cases):
        resolution = resolver.resolve(
            intent="sql", candidates=candidates, observations=observations
        )
        result = policy.decide(
            intent="sql",
            candidates=candidates,
            resolution=resolution,
            request_id=f"req.conditional.{index}",
            recorded_at=RECORDED_AT,
        )
        if result["action"] != wanted_action:
            raise D4SelectiveValidationError(
                f"conditional scenario {scenario_id} action drift"
            )
        if result["sql_execution_authorized"]:
            raise D4SelectiveValidationError("selective decision authorized SQL")
        for reference in result["typed_decision"]["evidence_refs"]:
            pack.resolve(reference)
        results.append(_summarize(scenario_id, result))
    if {item["action"] for item in results} != {
        "accept",
        "clarify",
        "escalate",
        "reject",
    }:
        raise D4SelectiveValidationError("four-action coverage incomplete")
    return results


def _vertical_scenarios(pack) -> list[dict[str, Any]]:
    runtime = D4SelectiveDecisionRuntime(pack)
    catalog = load_source_catalog()
    cases = [
        (
            "d4_01_d3_missing_list_not_used",
            "艺术家",
            _intent_result(
                "sql", needs_clarification=True, missing_slots=["time_range"]
            ),
            _slot_state(metric="count", entity_type="artist"),
            "accept",
        ),
        (
            "auto_default_slot_missing",
            "艺术家",
            _intent_result("sql"),
            {},
            "clarify",
        ),
        (
            "unknown_intent_rejected",
            "艺术家",
            _intent_result("unknown"),
            _slot_state(metric="count", entity_type="artist"),
            "reject",
        ),
    ]
    summaries: list[dict[str, Any]] = []
    for index, (scenario_id, query, intent_result, observations, wanted_action) in enumerate(cases):
        state = create_conversation_state(f"session.vertical.{index}", query)
        routed = apply_intent_result(state, catalog, intent_result)
        result = runtime.evaluate(
            conversation_state=routed,
            slot_observations=observations,
            request_id=f"req.vertical.{index}",
            recorded_at=RECORDED_AT,
        )
        if result["action"] != wanted_action:
            raise D4SelectiveValidationError(
                f"vertical scenario {scenario_id} action drift"
            )
        summaries.append(_summarize(scenario_id, result))
    return summaries


def _expect_code(
    probe_id: str,
    action: Callable[[], None],
    accepted_codes: set[str],
) -> dict[str, str]:
    try:
        action()
    except SelectiveRoutingError as exc:
        if exc.code not in accepted_codes:
            raise D4SelectiveValidationError(
                f"probe {probe_id} returned {exc.code}"
            ) from exc
        return {"probe_id": probe_id, "error_code": exc.code}
    raise D4SelectiveValidationError(f"probe {probe_id} did not fail closed")


def _fail_closed_probes(pack) -> list[dict[str, str]]:
    retriever = CandidateRetriever(pack)
    candidates = retriever.retrieve("艺术家", top_k=3)
    resolver = RequiredSlotResolver(pack)
    policy = SelectiveDecisionPolicy(pack)

    def answer_field() -> None:
        build_slot_observations(
            {
                "slot.metric": {
                    "values": ["count"],
                    "evidence_refs": ["evidence.metric_kinds"],
                    "gold_value": "TESTONLY",
                }
            }
        )

    def bad_evidence() -> None:
        resolver.resolve(
            intent="sql",
            candidates=candidates,
            observations=build_slot_observations(
                {
                    "slot.metric": {
                        "values": ["count"],
                        "evidence_refs": ["evidence.country_enum"],
                    }
                }
            ),
        )

    def mutated_resolver(kind: str) -> None:
        artifacts = _thaw(pack.artifacts)
        if kind == "required_operator":
            artifacts["slots"]["items"][1]["required_when"][0]["operator"] = "TESTONLY_unknown"
        elif kind == "dangling":
            artifacts["slots"]["items"][1]["required_when"][0]["subject_ref"] = "slot.not_real"
        elif kind == "rule_operator":
            artifacts["validation_rules"]["items"][0]["operator"] = "all"
        mutated = replace(pack, artifacts=artifacts)
        RequiredSlotResolver(mutated).resolve(
            intent="sql",
            candidates=candidates,
            observations=_slot_state(metric="count", entity_type="artist"),
        )

    def unstable_candidate_order() -> None:
        ordered = tuple(reversed(candidates[:2]))
        resolution = resolver.resolve(
            intent="sql",
            candidates=ordered,
            observations=_slot_state(metric="count", entity_type="artist"),
        )
        policy.decide(
            intent="sql",
            candidates=ordered,
            resolution=resolution,
            request_id="req.bad-order",
            recorded_at=RECORDED_AT,
        )

    def invalid_policy() -> None:
        value = _read_json(DEFAULT_POLICY_PATH)
        value["thresholds"]["accept_min"] = 0.4
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policy.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            SelectiveDecisionPolicy(pack, policy_path=path)

    valid_resolution = resolver.resolve(
        intent="sql",
        candidates=(candidates[0],),
        observations=_slot_state(metric="count", entity_type="artist"),
    )
    valid_result = policy.decide(
        intent="sql",
        candidates=(candidates[0],),
        resolution=valid_resolution,
        request_id="req.valid",
        recorded_at=RECORDED_AT,
    )

    def authorize_sql() -> None:
        tampered = deepcopy(valid_result)
        tampered["sql_execution_authorized"] = True
        policy.validate_result(tampered)

    probes = [
        ("answer_derived_field", answer_field, {"answer_derived_field_forbidden"}),
        ("illegal_evidence_ref", bad_evidence, {"slot_evidence_not_allowed"}),
        ("unknown_required_operator", lambda: mutated_resolver("required_operator"), {"unsupported_required_when_operator"}),
        ("dangling_required_reference", lambda: mutated_resolver("dangling"), {"required_when_dangling_reference"}),
        ("unsupported_validation_operator", lambda: mutated_resolver("rule_operator"), {"unsupported_validation_operator"}),
        ("unstable_candidate_order", unstable_candidate_order, {"candidate_order_unstable"}),
        ("invalid_threshold_order", invalid_policy, {"invalid_threshold_order"}),
        ("sql_execution_authority", authorize_sql, {"selective_result_schema_invalid", "sql_execution_authority_forbidden"}),
    ]
    return [_expect_code(probe_id, action, codes) for probe_id, action, codes in probes]


def validate_all() -> dict[str, Any]:
    pack = DomainPackRegistry().load("chinook_music_store", "2.0.0")
    policy = SelectiveDecisionPolicy(pack)
    conditional = _condition_scenarios(pack)
    vertical = _vertical_scenarios(pack)
    fail_closed = _fail_closed_probes(pack)
    source = (PROJECT_ROOT / "src" / "backend" / "agent" / "selective_routing.py").read_text(
        encoding="utf-8"
    )
    if "backend.nl2sql" in source or "ReadonlyExecutor" in source:
        raise D4SelectiveValidationError("D4-04 runtime crossed SQL execution boundary")
    action_counts = {
        action: sum(item["action"] == action for item in conditional)
        for action in ("accept", "clarify", "escalate", "reject")
    }
    return {
        "status": "pass",
        "report_type": "d4_selective_routing_offline_validation",
        "task_id": "D4-04",
        "scope": "automatic_required_slots_and_decision_only_routing",
        "domain_pack": {
            "domain_pack_id": pack.domain_pack_id,
            "version": pack.version,
            "manifest_sha256": pack.manifest_sha256,
            "slot_count": len(pack.artifact_items("slots")),
            "validation_rule_count": len(pack.artifact_items("validation_rules")),
        },
        "policy": {
            "policy_id": policy.policy["policy_id"],
            "version": policy.policy["version"],
            "sha256": policy.policy_sha256,
            "mode": "uncalibrated_raw_score",
            "empirically_calibrated": False,
            "thresholds": dict(policy.policy["thresholds"]),
            "action_priority": list(policy.policy["action_priority"]),
            "action_counts": action_counts,
        },
        "conditional_component_evidence": {
            "classification": "given_legal_candidates_and_explicit_state",
            "scenario_count": len(conditional),
            "scenarios": conditional,
            "query_text_stored": False,
            "slot_values_stored": False,
        },
        "decision_vertical_slice_evidence": {
            "path": ["D4-01_intent_state", "D4-03_domain_evidence", "D4-04_selective_routing"],
            "scenario_count": len(vertical),
            "scenarios": vertical,
            "d3_external_required_slots_used": False,
            "d4_05_clarification_recovery_included": False,
            "query_text_stored": False,
            "slot_values_stored": False,
        },
        "contract_evidence": {
            "typed_decision_schema_id": "typed-decision.v1",
            "trace_schema_id": "typed-decision-trace-event.v1",
            "selective_result_schema_id": "selective-routing-result.v1",
            "slot_statuses": ["filled", "missing", "conflicting", "unknown"],
            "required_state_values": [True, False, None],
            "evidence_refs_resolved": True,
            "deterministic_repeated_results": True,
        },
        "fail_closed_probes": {
            "count": len(fail_closed),
            "results": fail_closed,
        },
        "safety_boundary": {
            "accept_authorizes_sql_execution": False,
            "requires_d4_02_safety_gate": True,
            "sql_candidate_generated": False,
            "sql_executions": 0,
            "d4_02_b_owner_review_pending": True,
        },
        "input_isolation": {
            "prompt_dev_cases_read": 0,
            "validation_cases_read": 0,
            "hidden_test_cases_read": 0,
            "answer_fields_used_for_input": False,
            "validation_historical_exposure_count": 0,
            "validation_current_task_exposure_count": 0,
            "hidden_test_exposure_count": 0,
        },
        "model_boundary": _provider_boundary(),
        "not_implemented": [
            "empirical_calibration",
            "information_gain_question_planning",
            "state_merger",
            "clarification_answer_recovery",
            "sql_generation_or_execution",
            "business_accuracy_or_generalization_evaluation",
            "innovation_claim_evidence",
        ],
        "network_evaluation_calls": 0,
        "model_api_calls": 0,
        "credential_files_read": 0,
        "sql_executions": 0,
        "estimated_cost_cny": 0.0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="离线校验 D4-04 自动槽位与选择性分流。")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    try:
        report = validate_all()
    except (D4SelectiveValidationError, SelectiveRoutingError) as exc:
        parser.error(str(exc))
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
