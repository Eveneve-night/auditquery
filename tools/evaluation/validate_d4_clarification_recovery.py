"""Generate the deterministic, content-safe D4-05 offline report.

Only public synthetic scenarios and versioned repository assets are read.  The
tool performs no model/network call and reads no credential, validation, or
hidden-test content.  One explicitly injected readonly proposal is executed
against the local Chinook SQLite file and is counted in the report.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Callable, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

from backend.agent.clarification_recovery import (  # noqa: E402
    ClarificationRecoveryError,
    D4ClarificationRecoveryRuntime,
    SqlProposal,
    build_clarification_answer,
)
from backend.agent.domain_pack import DomainPackRegistry  # noqa: E402
from backend.agent.intent_routing import (  # noqa: E402
    apply_intent_result,
    create_conversation_state,
    load_source_catalog,
)
from backend.agent.selective_routing import (  # noqa: E402
    SlotAssessment,
    SlotResolution,
    build_slot_observations,
)
from backend.nl2sql.readonly_executor import ReadonlyExecutor  # noqa: E402
from backend.nl2sql.sql_safety_gate import SafetyDecision, validate_candidate  # noqa: E402


APPROVED_MODEL = "qwen3.7-flash"
RECORDED_AT = "2026-09-29T01:00:00Z"
FIXTURE_PATH = (
    PROJECT_ROOT / "tests" / "fixtures" / "d4-05" / "public-scenarios.v1.json"
)
ATTACK_PATH = (
    PROJECT_ROOT / "tests" / "fixtures" / "d4-02" / "malicious-sql-samples.json"
)


class D4ClarificationValidationError(RuntimeError):
    pass


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _json_hash(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _is_forbidden_key(key: str) -> bool:
    normalized = key.casefold().replace("-", "_").replace(" ", "_")
    return (
        "expected" in normalized
        or "gold" in normalized
        or normalized in {"label", "labels", "answer", "answers", "target_answer"}
    )


def _assert_input_isolated(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if _is_forbidden_key(str(key)):
                raise D4ClarificationValidationError(
                    "public fixture contains answer-derived field"
                )
            _assert_input_isolated(item)
    elif isinstance(value, list):
        for item in value:
            _assert_input_isolated(item)


def _intent_result() -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "task_type": "intent",
        "status": "success",
        "data": {
            "intent": "sql",
            "needs_clarification": False,
            "required_sources": ["chinook_sql"],
            "missing_slots": [],
            "reason": "routable",
            "normalized_query": None,
        },
        "error": None,
        "warnings": [],
    }


def _provider_boundary() -> dict[str, Any]:
    paths = sorted((PROJECT_ROOT / "configs" / "providers").glob("*.json"))
    providers = [_read_json(path) for path in paths]
    if len(providers) != 1 or providers[0].get("model") != APPROVED_MODEL:
        raise D4ClarificationValidationError("single-model boundary changed")
    return {
        "provider_config_count": 1,
        "model": APPROVED_MODEL,
        "second_provider_enabled": False,
        "fallback_enabled": False,
        "jev_api_enabled": False,
    }


@dataclass
class _StaticPort:
    proposal: SqlProposal
    calls: int = 0

    def propose(self, *, conversation_state, slot_observations):
        self.calls += 1
        if not conversation_state or not slot_observations:
            raise D4ClarificationValidationError("offline port input missing")
        return self.proposal


def _run_public_scenarios(runtime: D4ClarificationRecoveryRuntime) -> tuple[list[dict[str, Any]], dict[str, int]]:
    fixture = _read_json(FIXTURE_PATH)
    _assert_input_isolated(fixture)
    if fixture.get("split") != "public_synthetic_contract":
        raise D4ClarificationValidationError("unexpected D4-05 fixture split")
    catalog = load_source_catalog()
    summaries: list[dict[str, Any]] = []
    counters = {
        "questions": 0,
        "safety_gate_calls": 0,
        "local_sqlite_execution_attempts": 0,
        "local_sqlite_execution_successes": 0,
        "safety_rejections": 0,
    }
    for index, scenario in enumerate(fixture["scenarios"]):
        state = apply_intent_result(
            create_conversation_state(
                f"session.public.{index}", str(scenario["query"])
            ),
            catalog,
            _intent_result(),
        )
        observations = build_slot_observations(scenario["initial_observations"])
        session = runtime.start(
            conversation_state=state,
            slot_observations=observations,
            request_id=f"req.public.{index}",
            recorded_at=RECORDED_AT,
        )
        for reply_index, reply in enumerate(scenario["structured_replies"], start=1):
            question = session.pending_question
            if question is None:
                raise D4ClarificationValidationError("reply without pending question")
            structured = build_clarification_answer(
                {
                    "answer_id": f"reply.public.{index}.{reply_index}",
                    "question_id": question.question_id,
                    "session_id": question.session_id,
                    "request_id": question.request_id,
                    "turn_id": question.turn_id,
                    "state_revision": question.state_revision,
                    "slot_id": question.slot_id,
                    "values": [reply["value"]],
                    "evidence_refs": [reply["evidence_ref"]],
                }
            )
            runtime.resume(session, structured, recorded_at=RECORDED_AT)
        proposal = scenario["sql_proposal"]
        if proposal is not None:
            if session.current_decision["action"] != "accept":
                raise D4ClarificationValidationError("proposal reached non-accept state")
            runtime.handoff_and_execute(
                session,
                proposal_port=_StaticPort(
                    SqlProposal(
                        proposal_id=proposal["proposal_id"],
                        sql=proposal["sql"],
                        tables=tuple(proposal["tables"]),
                        params=tuple(proposal["params"]),
                    )
                ),
            )
        result = runtime.result(session)
        counters["questions"] += len(session.questions)
        counters["safety_gate_calls"] += session.safety_gate_calls
        counters["local_sqlite_execution_attempts"] += (
            session.local_sqlite_execution_attempts
        )
        counters["local_sqlite_execution_successes"] += (
            session.local_sqlite_execution_successes
        )
        counters["safety_rejections"] += int(session.safety_approved is False)
        summaries.append(
            {
                "scenario_id": scenario["id"],
                "scenario_kind": scenario["scenario_kind"],
                "question_count": len(session.questions),
                "final_action": session.current_decision["action"],
                "workflow_status": session.workflow_status,
                "safety_gate_calls": session.safety_gate_calls,
                "sqlite_execution_attempts": session.local_sqlite_execution_attempts,
                "result_sha256": _json_hash(result),
            }
        )
    if counters != {
        "questions": 5,
        "safety_gate_calls": 2,
        "local_sqlite_execution_attempts": 1,
        "local_sqlite_execution_successes": 1,
        "safety_rejections": 1,
    }:
        raise D4ClarificationValidationError("public scenario counters drift")
    return summaries, counters


def _information_gain_probe(runtime: D4ClarificationRecoveryRuntime) -> dict[str, Any]:
    candidates = runtime.selective.retriever.retrieve("艺术家", top_k=5)
    resolution = SlotResolution(
        assessments=(
            SlotAssessment(
                "slot.sort",
                True,
                "missing",
                0,
                ("slot.sort", "evidence.sort_kinds"),
                ("value_missing",),
            ),
            SlotAssessment(
                "slot.entity_type",
                True,
                "missing",
                0,
                ("slot.entity_type", "evidence.entity_kinds"),
                ("value_missing",),
            ),
        ),
        hard_rule_violations=(),
        soft_rule_violations=(),
    )
    selective = runtime.selective.policy.decide(
        intent="sql",
        candidates=candidates,
        resolution=resolution,
        request_id="req.info-gain.report",
        recorded_at=RECORDED_AT,
    )
    question = runtime.planner.plan(
        selective_result=selective,
        session_id="session.info-gain.report",
        request_id="req.info-gain.report",
        turn_id=1,
        state_revision=1,
    )
    if question.slot_id == question.baseline_slot_id:
        raise D4ClarificationValidationError("information gain did not beat baseline")
    return {
        "strategy": "expected_information_gain",
        "ablation_baseline": "first_missing",
        "baseline_slot_id": question.baseline_slot_id,
        "selected_slot_id": question.slot_id,
        "selected_features": dict(question.information_gain),
        "selected_differs_from_baseline": True,
    }


def _expect_code(
    probe_id: str,
    action: Callable[[], None],
    accepted_codes: set[str],
) -> dict[str, str]:
    try:
        action()
    except ClarificationRecoveryError as exc:
        if exc.code not in accepted_codes:
            raise D4ClarificationValidationError(
                f"{probe_id} returned unexpected code {exc.code}"
            ) from exc
        return {"probe_id": probe_id, "error_code": exc.code}
    except (TypeError, ValueError) as exc:
        code = "d4_02_execution_revalidation"
        if code not in accepted_codes:
            raise D4ClarificationValidationError(
                f"{probe_id} returned an unsafe exception type"
            ) from exc
        return {"probe_id": probe_id, "error_code": code}
    raise D4ClarificationValidationError(f"{probe_id} did not fail closed")


def _new_missing_session(runtime: D4ClarificationRecoveryRuntime, suffix: str):
    state = apply_intent_result(
        create_conversation_state(f"session.probe.{suffix}", "艺术家"),
        load_source_catalog(),
        _intent_result(),
    )
    return runtime.start(
        conversation_state=state,
        slot_observations=build_slot_observations(
            {
                "slot.metric": {
                    "values": ["count"],
                    "evidence_refs": ["evidence.metric_kinds"],
                }
            }
        ),
        request_id=f"req.probe.{suffix}",
        recorded_at=RECORDED_AT,
    )


def _reply_payload(session, *, value="artist", evidence="evidence.entity_kinds"):
    question = session.pending_question
    return {
        "answer_id": f"reply.{question.question_id}",
        "question_id": question.question_id,
        "session_id": question.session_id,
        "request_id": question.request_id,
        "turn_id": question.turn_id,
        "state_revision": question.state_revision,
        "slot_id": question.slot_id,
        "values": [value],
        "evidence_refs": [evidence],
    }


def _session_mutation_fingerprint(session) -> str:
    return _json_hash(
        {
            "workflow_status": session.workflow_status,
            "pending_question": (
                session.pending_question.to_dict()
                if session.pending_question is not None
                else None
            ),
            "observations": {
                slot_id: {
                    "values": list(observation.values),
                    "evidence_refs": list(observation.evidence_refs),
                }
                for slot_id, observation in sorted(session.observations.items())
            },
            "current_decision": session.current_decision,
            "answer_summaries": session.answer_summaries,
            "merge_events": session.merge_events,
            "decision_traces": session.decision_traces,
            "audit_events": session.audit_events,
            "consumed_question_ids": sorted(session.consumed_question_ids),
            "consumed_answer_ids": sorted(session.consumed_answer_ids),
            "guard": session.guard.snapshot(elapsed_ms=0),
        }
    )


def _fail_closed_probes(runtime: D4ClarificationRecoveryRuntime) -> list[dict[str, str]]:
    def raw_observations() -> None:
        runtime.start(
            conversation_state=_new_missing_session(runtime, "raw-source").conversation_state,
            slot_observations={"slot.metric": {"values": ["count"], "evidence_refs": []}},
            request_id="req.probe.raw",
            recorded_at=RECORDED_AT,
        )

    def stale_reply() -> None:
        session = _new_missing_session(runtime, "stale")
        payload = _reply_payload(session)
        payload["state_revision"] = 999
        runtime.resume(
            session,
            build_clarification_answer(payload),
            recorded_at=RECORDED_AT,
        )

    def cross_slot() -> None:
        session = _new_missing_session(runtime, "cross-slot")
        payload = _reply_payload(session)
        payload["slot_id"] = "slot.country"
        runtime.resume(
            session,
            build_clarification_answer(payload),
            recorded_at=RECORDED_AT,
        )

    def illegal_evidence() -> None:
        session = _new_missing_session(runtime, "evidence")
        payload = _reply_payload(session)
        payload["evidence_refs"] = ["evidence.country_enum"]
        runtime.resume(
            session,
            build_clarification_answer(payload),
            recorded_at=RECORDED_AT,
        )

    def replay() -> None:
        session = _new_missing_session(runtime, "replay")
        reply = build_clarification_answer(_reply_payload(session))
        runtime.resume(session, reply, recorded_at=RECORDED_AT)
        runtime.resume(session, reply, recorded_at=RECORDED_AT)

    def invalid_time_range() -> None:
        state = apply_intent_result(
            create_conversation_state("session.probe.time", "艺术家"),
            load_source_catalog(),
            _intent_result(),
        )
        session = runtime.start(
            conversation_state=state,
            slot_observations=build_slot_observations(
                {
                    "slot.metric": {
                        "values": ["revenue"],
                        "evidence_refs": ["evidence.metric_kinds"],
                    }
                }
            ),
            request_id="req.probe.time",
            recorded_at=RECORDED_AT,
        )
        payload = _reply_payload(
            session, value="2010-2011", evidence="evidence.time_range_years"
        )
        runtime.resume(
            session,
            build_clarification_answer(payload),
            recorded_at=RECORDED_AT,
        )

    def raw_reply() -> None:
        runtime.resume(
            _new_missing_session(runtime, "raw-reply"),
            {},
            recorded_at=RECORDED_AT,
        )

    def forged_decision() -> None:
        ReadonlyExecutor().execute(
            SafetyDecision(
                True,
                execution_sql="SELECT * FROM Artist a JOIN Album b ON a.ArtistId=b.ArtistId",
                tables=("Artist",),
                row_limit=100,
            )
        )

    def pending_decision_tamper() -> None:
        session = _new_missing_session(runtime, "pending-tamper")
        session.current_decision["action"] = "accept"
        port = _StaticPort(
            SqlProposal(
                "proposal.probe.pending-tamper",
                "SELECT COUNT(*) FROM Artist",
                ("Artist",),
            )
        )
        try:
            runtime.handoff_and_execute(session, proposal_port=port)
        except ClarificationRecoveryError:
            if (
                port.calls != 0
                or session.safety_gate_calls != 0
                or session.local_sqlite_execution_attempts != 0
            ):
                raise D4ClarificationValidationError(
                    "pending decision tamper reached execution boundary"
                )
            raise

    def redecision_transaction_rollback() -> None:
        session = _new_missing_session(runtime, "transaction")
        reply = build_clarification_answer(_reply_payload(session))
        before = _session_mutation_fingerprint(session)
        try:
            runtime.resume(session, reply, recorded_at=RECORDED_AT, top_k=0)
        except ClarificationRecoveryError as exc:
            if exc.code != "redecision_failed":
                raise
            if _session_mutation_fingerprint(session) != before:
                raise D4ClarificationValidationError(
                    "failed redecision partially committed session state"
                ) from exc
            runtime.resume(session, reply, recorded_at=RECORDED_AT)
            if session.workflow_status != "accepted_for_safety_handoff":
                raise D4ClarificationValidationError(
                    "rolled-back clarification answer was not retryable"
                ) from exc
            raise

    def accepted_decision_tamper() -> None:
        session = _new_missing_session(runtime, "accepted-tamper")
        runtime.resume(
            session,
            build_clarification_answer(_reply_payload(session)),
            recorded_at=RECORDED_AT,
        )
        session.current_decision["raw_score"] = 0.999999
        port = _StaticPort(
            SqlProposal(
                "proposal.probe.accepted-tamper",
                "SELECT COUNT(*) FROM Artist",
                ("Artist",),
            )
        )
        try:
            runtime.handoff_and_execute(session, proposal_port=port)
        except ClarificationRecoveryError:
            if (
                port.calls != 0
                or session.safety_gate_calls != 0
                or session.local_sqlite_execution_attempts != 0
            ):
                raise D4ClarificationValidationError(
                    "accepted decision tamper reached execution boundary"
                )
            raise

    probes = [
        ("raw_slot_observations", raw_observations, {"slot_observations_not_built"}),
        ("stale_reply", stale_reply, {"clarification_answer_stale"}),
        ("cross_slot_update", cross_slot, {"clarification_cross_slot_update"}),
        ("illegal_evidence", illegal_evidence, {"invalid_answer_evidence_refs"}),
        ("replayed_reply", replay, {"clarification_answer_replayed"}),
        ("time_range_interval", invalid_time_range, {"invalid_time_range_format"}),
        ("raw_reply", raw_reply, {"clarification_answer_not_built"}),
        ("forged_safety_decision", forged_decision, {"d4_02_execution_revalidation"}),
        (
            "pending_decision_tamper",
            pending_decision_tamper,
            {"execution_session_not_ready"},
        ),
        (
            "redecision_transaction_rollback",
            redecision_transaction_rollback,
            {"redecision_failed"},
        ),
        (
            "accepted_decision_tamper",
            accepted_decision_tamper,
            {"execution_decision_mismatch"},
        ),
    ]
    return [_expect_code(probe_id, action, codes) for probe_id, action, codes in probes]


def _attack_set_evidence() -> dict[str, Any]:
    attacks = _read_json(ATTACK_PATH)
    blocked = 0
    for sample in attacks:
        decision = validate_candidate(sample["sql"], sample["tables"], sample["params"])
        if decision.approved:
            raise D4ClarificationValidationError(
                f"D4-02 attack was approved: {sample['id']}"
            )
        blocked += 1
    return {
        "sample_count": len(attacks),
        "blocked_count": blocked,
        "sqlite_execution_attempts": 0,
        "fixture_sha256": _json_hash(attacks),
    }


def validate_all() -> dict[str, Any]:
    pack = DomainPackRegistry().load("chinook_music_store", "1.0.0")
    runtime = D4ClarificationRecoveryRuntime(pack)
    scenarios, counters = _run_public_scenarios(runtime)
    fail_closed = _fail_closed_probes(runtime)
    attack_set = _attack_set_evidence()
    info_gain = _information_gain_probe(runtime)
    return {
        "status": "pass",
        "report_type": "d4_clarification_recovery_offline_validation",
        "task_id": "D4-05",
        "scope": "single_question_state_merge_redecision_and_safe_execution_handoff",
        "domain_pack": {
            "domain_pack_id": pack.domain_pack_id,
            "version": pack.version,
            "manifest_sha256": pack.manifest_sha256,
        },
        "policy": {
            "policy_id": runtime.policy["policy_id"],
            "version": runtime.policy["version"],
            "sha256": runtime.policy_sha256,
            "planner_version": "expected-information-gain.v1",
            "state_merger_version": "strict-state-merger.v1",
            "time_range_representation": "single_year_string",
            "time_range_version": "single-year.v1",
            "max_questions_per_turn": 1,
            "max_clarification_rounds": runtime.loop_policy.limits[
                "max_clarification_rounds"
            ],
        },
        "conditional_component_evidence": {
            "classification": "given_d4_04_clarify_and_public_domain_metadata",
            "information_gain_probe": info_gain,
            "first_missing_retained_as_ablation_baseline": True,
            "one_slot_per_question": True,
        },
        "offline_orchestration_vertical_slice": {
            "classification": "public_synthetic_offline_orchestration",
            "path": [
                "D4-01_intent_state",
                "D4-03_domain_evidence",
                "D4-04_selective_routing",
                "D4-05_question",
                "D4-05_state_merge",
                "D4-04_redecision",
                "D4-02_safety_gate",
                "D4-02_readonly_executor",
            ],
            "scenario_count": len(scenarios),
            "scenarios": scenarios,
            "question_count": counters["questions"],
            "safety_gate_calls": counters["safety_gate_calls"],
            "local_sqlite_execution_attempts": counters[
                "local_sqlite_execution_attempts"
            ],
            "local_sqlite_execution_successes": counters[
                "local_sqlite_execution_successes"
            ],
            "safety_rejections": counters["safety_rejections"],
            "online_sql_generator_used": False,
        },
        "fail_closed_probes": {
            "count": len(fail_closed),
            "results": fail_closed,
        },
        "d4_02_attack_set": attack_set,
        "trace_evidence": {
            "ordered_stages": [
                "initial_decision",
                "clarification_question",
                "structured_answer",
                "state_merge",
                "redecision",
            ],
            "raw_private_reply_stored": False,
            "sql_text_stored_in_report": False,
            "query_text_stored_in_report": False,
            "typed_decision_trace_preserved": True,
        },
        "safety_boundary": {
            "accept_authorizes_sql_execution": False,
            "requires_d4_02_safety_gate": True,
            "handoff_recomputes_d4_04_decision": True,
            "failed_redecision_is_transactional": True,
            "nondeterministic_latency_excluded_from_report_hash": True,
            "malicious_proposal_execution_attempts": 0,
            "forged_safety_decision_rejected": True,
            "d4_02_b_owner_review_pending": True,
        },
        "evidence_classification": {
            "conditional_component_evidence": True,
            "offline_orchestration_evidence": True,
            "business_accuracy_evidence": False,
            "generalization_evidence": False,
            "empirical_calibration_evidence": False,
            "innovation_claim_evidence": False,
        },
        "input_isolation": {
            "fixture_split": "public_synthetic_contract",
            "fixture_sha256": _json_hash(_read_json(FIXTURE_PATH)),
            "prompt_dev_cases_read": 0,
            "validation_cases_read": 0,
            "hidden_test_cases_read": 0,
            "answer_derived_fields_used_for_input": False,
            "validation_historical_exposure_count": 0,
            "validation_current_task_exposure_count": 0,
            "hidden_test_exposure_count": 0,
        },
        "model_boundary": _provider_boundary(),
        "not_implemented": [
            "online_sql_generation",
            "model_based_clarification_question_generation",
            "empirical_calibration",
            "web_ui",
            "hidden_evaluation",
            "business_accuracy_or_generalization_evaluation",
            "innovation_claim_evidence",
        ],
        "network_evaluation_calls": 0,
        "model_api_calls": 0,
        "credential_files_read": 0,
        "estimated_cost_cny": 0.0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="离线校验 D4-05 澄清恢复与安全执行移交。")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    try:
        report = validate_all()
    except (D4ClarificationValidationError, ClarificationRecoveryError) as exc:
        parser.error(str(exc))
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
