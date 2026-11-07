"""D4-05 clarification, state merge, recovery, and safety offline tests."""

from __future__ import annotations

from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[2]
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
from backend.nl2sql.readonly_executor import (  # noqa: E402
    ExecutionResult,
    ReadonlyExecutor,
)
from backend.nl2sql.sql_safety_gate import SafetyDecision  # noqa: E402


RECORDED_AT = "2026-09-29T01:00:00Z"
VALIDATOR_PATH = (
    PROJECT_ROOT / "tools" / "evaluation" / "validate_d4_clarification_recovery.py"
)
REPORT_PATH = PROJECT_ROOT / "tests" / "reports" / "d4-clarification-recovery-v1.json"
VALIDATOR_SPEC = importlib.util.spec_from_file_location(
    "validate_d4_clarification_recovery", VALIDATOR_PATH
)
if VALIDATOR_SPEC is None or VALIDATOR_SPEC.loader is None:
    raise RuntimeError("无法加载 D4-05 离线校验器")
VALIDATOR_MODULE = importlib.util.module_from_spec(VALIDATOR_SPEC)
sys.modules[VALIDATOR_SPEC.name] = VALIDATOR_MODULE
VALIDATOR_SPEC.loader.exec_module(VALIDATOR_MODULE)
validate_all = VALIDATOR_MODULE.validate_all


def _intent_result() -> dict[str, object]:
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


def _slot_state(**values: object):
    evidence = {
        "metric": "evidence.metric_kinds",
        "entity_type": "evidence.entity_kinds",
        "time_range": "evidence.time_range_years",
        "country": "evidence.country_enum",
        "limit": "evidence.limit_range",
        "grain": "evidence.grain_kinds",
        "sort": "evidence.sort_kinds",
    }
    return build_slot_observations(
        {
            f"slot.{name}": {
                "values": value if isinstance(value, list) else [value],
                "evidence_refs": [evidence[name]],
            }
            for name, value in values.items()
        }
    )


class StaticProposalPort:
    def __init__(self, proposal: SqlProposal) -> None:
        self.proposal = proposal
        self.calls = 0

    def propose(self, *, conversation_state, slot_observations):
        self.calls += 1
        if not conversation_state or not slot_observations:
            raise RuntimeError("TESTONLY invalid port input")
        return self.proposal


class StaticExecutionResultExecutor:
    def __init__(self, elapsed_ms: int) -> None:
        self.elapsed_ms = elapsed_ms

    def execute(self, decision, params):
        if not decision.approved or params:
            raise RuntimeError("TESTONLY invalid execution input")
        return ExecutionResult(
            columns=("artist_count",),
            rows=((275,),),
            row_count=1,
            truncated=False,
            elapsed_ms=self.elapsed_ms,
        )


class D4ClarificationRecoveryBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.pack = DomainPackRegistry().load("chinook_music_store", "1.0.0")
        cls.catalog = load_source_catalog()

    def setUp(self) -> None:
        self.runtime = D4ClarificationRecoveryRuntime(self.pack)

    def routed_state(self, *, query: str = "艺术家", session_id: str = "session.d4-05"):
        return apply_intent_result(
            create_conversation_state(session_id, query),
            self.catalog,
            _intent_result(),
        )

    def start(self, observations, *, query="艺术家", session_id="session.d4-05"):
        return self.runtime.start(
            conversation_state=self.routed_state(query=query, session_id=session_id),
            slot_observations=observations,
            request_id="req.d4-05",
            recorded_at=RECORDED_AT,
        )

    def answer(self, session, value=None, *, answer_id="reply.1", **overrides):
        question = session.pending_question
        assert question is not None
        option = question.options[0]
        payload = {
            "answer_id": answer_id,
            "question_id": question.question_id,
            "session_id": question.session_id,
            "request_id": question.request_id,
            "turn_id": question.turn_id,
            "state_revision": question.state_revision,
            "slot_id": question.slot_id,
            "values": [option.value if value is None else value],
            "evidence_refs": [option.evidence_ref],
        }
        payload.update(overrides)
        return build_clarification_answer(payload)


class ClarificationPlannerTest(D4ClarificationRecoveryBase):
    def test_planner_only_runs_for_clarify_and_asks_exactly_one_slot(self) -> None:
        session = self.start(_slot_state(metric="count"))
        self.assertEqual("awaiting_clarification", session.workflow_status)
        self.assertEqual(1, len(session.questions))
        self.assertEqual("slot.entity_type", session.pending_question.slot_id)
        self.assertGreater(len(session.pending_question.options), 1)
        with self.assertRaisesRegex(
            ClarificationRecoveryError, "planner_requires_clarify_action"
        ):
            accepted = self.start(_slot_state(metric="count", entity_type="artist"))
            self.runtime.planner.plan(
                selective_result=accepted.current_decision,
                session_id=accepted.session_id,
                request_id=accepted.request_id,
                turn_id=1,
                state_revision=1,
            )

    def test_information_gain_can_beat_first_missing_baseline(self) -> None:
        candidates = self.runtime.selective.retriever.retrieve("艺术家", top_k=5)
        resolution = SlotResolution(
            assessments=(
                SlotAssessment(
                    slot_id="slot.sort",
                    required=True,
                    status="missing",
                    value_count=0,
                    evidence_refs=("slot.sort", "evidence.sort_kinds"),
                    reason_codes=("value_missing",),
                ),
                SlotAssessment(
                    slot_id="slot.entity_type",
                    required=True,
                    status="missing",
                    value_count=0,
                    evidence_refs=("slot.entity_type", "evidence.entity_kinds"),
                    reason_codes=("value_missing",),
                ),
            ),
            hard_rule_violations=(),
            soft_rule_violations=(),
        )
        selective = self.runtime.selective.policy.decide(
            intent="sql",
            candidates=candidates,
            resolution=resolution,
            request_id="req.info-gain",
            recorded_at=RECORDED_AT,
        )
        question = self.runtime.planner.plan(
            selective_result=selective,
            session_id="session.info-gain",
            request_id="req.info-gain",
            turn_id=1,
            state_revision=1,
        )
        self.assertEqual("slot.sort", question.baseline_slot_id)
        self.assertEqual("slot.entity_type", question.slot_id)
        self.assertGreater(
            question.information_gain["downstream_condition_count"], 0
        )

    def test_question_options_and_evidence_are_domain_pack_backed(self) -> None:
        question = self.start(_slot_state(metric="count")).pending_question
        slot = self.pack.resolve(question.slot_id)
        allowed = set(slot["allowed_value_evidence_refs"])
        for option in question.options:
            self.assertIn(option.evidence_ref, allowed)
            evidence = self.pack.resolve(option.evidence_ref)
            self.assertTrue(evidence["send_to_model"])
            self.assertIn(evidence["data_level"], {"L0", "L1"})

    def test_policy_schema_rejects_unknown_fields_and_weight_drift(self) -> None:
        base_policy = json.loads(
            (PROJECT_ROOT / "configs/policies/d4-05/clarification-recovery.v1.json").read_text(
                encoding="utf-8"
            )
        )
        cases = []
        weight_drift = deepcopy(base_policy)
        weight_drift["planner"]["weights"]["candidate_reduction_gain"] = 0.5
        cases.append((weight_drift, "invalid_information_gain_weights"))
        unknown_field = deepcopy(base_policy)
        unknown_field["test_only_unknown"] = True
        cases.append((unknown_field, "invalid_clarification_policy"))
        for policy, code in cases:
            with self.subTest(code=code), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "policy.json"
                path.write_text(json.dumps(policy), encoding="utf-8")
                with self.assertRaisesRegex(ClarificationRecoveryError, code):
                    D4ClarificationRecoveryRuntime(self.pack, policy_path=path)


class StructuredAnswerAndMergeTest(D4ClarificationRecoveryBase):
    def test_answer_builder_rejects_empty_multi_value_and_answer_fields(self) -> None:
        session = self.start(_slot_state(metric="count"))
        question = session.pending_question
        base = {
            "answer_id": "reply.builder",
            "question_id": question.question_id,
            "session_id": question.session_id,
            "request_id": question.request_id,
            "turn_id": question.turn_id,
            "state_revision": question.state_revision,
            "slot_id": question.slot_id,
            "values": ["artist"],
            "evidence_refs": ["evidence.entity_kinds"],
        }
        for values, code in (([], "empty_clarification_answer"), (["artist", "album"], "multiple_answer_values")):
            with self.subTest(code=code):
                raw = dict(base, values=values)
                with self.assertRaisesRegex(ClarificationRecoveryError, code):
                    build_clarification_answer(raw)
        leaked = dict(base, gold_sql="TESTONLY")
        with self.assertRaisesRegex(
            ClarificationRecoveryError, "answer_derived_field_forbidden"
        ):
            build_clarification_answer(leaked)

    def test_runtime_requires_built_observations_and_built_answer(self) -> None:
        with self.assertRaisesRegex(
            ClarificationRecoveryError, "slot_observations_not_built"
        ):
            self.start(
                {
                    "slot.metric": {
                        "values": ["count"],
                        "evidence_refs": ["evidence.metric_kinds"],
                    }
                }
            )
        session = self.start(_slot_state(metric="count"))
        with self.assertRaisesRegex(
            ClarificationRecoveryError, "clarification_answer_not_built"
        ):
            self.runtime.resume(session, {}, recorded_at=RECORDED_AT)

    def test_session_turn_revision_question_and_slot_binding(self) -> None:
        fields = (
            ("session_id", "session.other", "clarification_session_mismatch"),
            ("request_id", "req.other", "clarification_request_mismatch"),
            ("question_id", "clarify.deadbeefdeadbeefdeadbeef", "clarification_question_mismatch"),
            ("turn_id", 99, "clarification_turn_mismatch"),
            ("state_revision", 99, "clarification_answer_stale"),
            ("slot_id", "slot.country", "clarification_cross_slot_update"),
        )
        for field, value, code in fields:
            with self.subTest(field=field):
                session = self.start(_slot_state(metric="count"), session_id=f"session.{field}")
                answer = self.answer(session, **{field: value})
                with self.assertRaisesRegex(ClarificationRecoveryError, code):
                    self.runtime.resume(session, answer, recorded_at=RECORDED_AT)

    def test_illegal_value_and_evidence_fail_closed(self) -> None:
        session = self.start(_slot_state(metric="count"))
        with self.assertRaisesRegex(
            ClarificationRecoveryError, "clarification_value_not_offered"
        ):
            self.runtime.resume(
                session,
                self.answer(session, "TESTONLY_not_offered"),
                recorded_at=RECORDED_AT,
            )
        session = self.start(_slot_state(metric="count"), session_id="session.bad-ref")
        with self.assertRaisesRegex(
            ClarificationRecoveryError, "invalid_answer_evidence_refs"
        ):
            self.runtime.resume(
                session,
                self.answer(session, evidence_refs=["evidence.country_enum"]),
                recorded_at=RECORDED_AT,
            )

    def test_replay_is_rejected_after_successful_merge(self) -> None:
        session = self.start(_slot_state(metric="count"))
        answer = self.answer(session, "artist")
        self.runtime.resume(session, answer, recorded_at=RECORDED_AT)
        with self.assertRaisesRegex(
            ClarificationRecoveryError, "clarification_answer_replayed"
        ):
            self.runtime.resume(session, answer, recorded_at=RECORDED_AT)

    def test_redecision_failure_rolls_back_and_same_answer_can_retry(self) -> None:
        session = self.start(_slot_state(metric="count"))
        answer = self.answer(session, "artist")
        question = session.pending_question
        before = {
            "workflow_status": session.workflow_status,
            "pending_question": question.to_dict(),
            "observations": deepcopy(session.observations),
            "answer_summaries": deepcopy(session.answer_summaries),
            "merge_events": deepcopy(session.merge_events),
            "decision_traces": deepcopy(session.decision_traces),
            "audit_events": deepcopy(session.audit_events),
            "consumed_question_ids": set(session.consumed_question_ids),
            "consumed_answer_ids": set(session.consumed_answer_ids),
            "guard": session.guard.snapshot(elapsed_ms=0),
        }
        with self.assertRaisesRegex(ClarificationRecoveryError, "redecision_failed"):
            self.runtime.resume(
                session,
                answer,
                recorded_at=RECORDED_AT,
                top_k=0,
            )
        self.assertEqual(before["workflow_status"], session.workflow_status)
        self.assertEqual(
            before["pending_question"], session.pending_question.to_dict()
        )
        self.assertEqual(before["observations"], session.observations)
        self.assertEqual(before["answer_summaries"], session.answer_summaries)
        self.assertEqual(before["merge_events"], session.merge_events)
        self.assertEqual(before["decision_traces"], session.decision_traces)
        self.assertEqual(before["audit_events"], session.audit_events)
        self.assertEqual(
            before["consumed_question_ids"], session.consumed_question_ids
        )
        self.assertEqual(before["consumed_answer_ids"], session.consumed_answer_ids)
        self.assertEqual(before["guard"], session.guard.snapshot(elapsed_ms=0))

        self.runtime.resume(session, answer, recorded_at=RECORDED_AT)
        self.assertEqual("accepted_for_safety_handoff", session.workflow_status)

    def test_time_range_v1_accepts_single_year_only(self) -> None:
        session = self.start(_slot_state(metric="revenue"))
        self.assertEqual("slot.time_range", session.pending_question.slot_id)
        self.assertEqual(
            ["2009", "2010", "2011", "2012", "2013"],
            [item.value for item in session.pending_question.options],
        )
        with self.assertRaisesRegex(
            ClarificationRecoveryError, "invalid_time_range_format"
        ):
            self.runtime.resume(
                session,
                self.answer(session, "2010-2011"),
                recorded_at=RECORDED_AT,
            )

    def test_no_progress_soft_conflict_fails_closed(self) -> None:
        session = self.start(
            _slot_state(metric="count", entity_type="artist", grain="month")
        )
        self.assertEqual("slot.grain", session.pending_question.slot_id)
        with self.assertRaisesRegex(
            ClarificationRecoveryError, "clarification_answer_no_progress"
        ):
            self.runtime.resume(
                session,
                self.answer(session, "year"),
                recorded_at=RECORDED_AT,
            )


class RecoveryAndSafetyTest(D4ClarificationRecoveryBase):
    def accepted_session(self):
        session = self.start(_slot_state(metric="count"))
        self.runtime.resume(
            session,
            self.answer(session, "artist"),
            recorded_at="2026-09-29T01:00:01Z",
        )
        self.assertEqual("accepted_for_safety_handoff", session.workflow_status)
        return session

    def test_merge_then_redecision_accepts_but_does_not_execute(self) -> None:
        session = self.accepted_session()
        self.assertEqual("accept", session.current_decision["action"])
        self.assertFalse(session.current_decision["sql_execution_authorized"])
        self.assertEqual(0, session.safety_gate_calls)
        self.assertEqual(0, session.local_sqlite_execution_attempts)
        self.assertEqual(
            [
                "initial_decision",
                "clarification_question",
                "structured_answer",
                "state_merge",
                "redecision",
            ],
            [item["event_type"] for item in session.audit_events],
        )

    def test_merge_can_require_one_more_question(self) -> None:
        session = self.start(_slot_state(metric="count"))
        self.runtime.resume(
            session,
            self.answer(session, "customer"),
            recorded_at=RECORDED_AT,
        )
        self.assertEqual("awaiting_clarification", session.workflow_status)
        self.assertEqual("slot.country", session.pending_question.slot_id)
        self.assertEqual(2, len(session.questions))

    def test_existing_loop_guard_stops_third_clarification(self) -> None:
        session = self.start(_slot_state(metric="growth_rate"))
        first = self.answer(session, answer_id="reply.growth.1")
        self.runtime.resume(session, first, recorded_at=RECORDED_AT)
        self.assertIsNotNone(session.pending_question)
        second = self.answer(session, answer_id="reply.growth.2")
        self.runtime.resume(session, second, recorded_at=RECORDED_AT)
        self.assertEqual("stopped", session.workflow_status)
        self.assertEqual("specialized_attempt_limit", session.stop_reason)
        self.assertIsNone(session.pending_question)

    def test_malicious_sql_is_rejected_without_execution(self) -> None:
        session = self.accepted_session()
        port = StaticProposalPort(
            SqlProposal(
                "proposal.malicious",
                "DELETE FROM Artist",
                ("Artist",),
            )
        )
        result = self.runtime.handoff_and_execute(session, proposal_port=port)
        self.assertIsNone(result)
        self.assertEqual("safety_rejected", session.workflow_status)
        self.assertEqual(1, session.safety_gate_calls)
        self.assertEqual(0, session.local_sqlite_execution_attempts)

    def test_malformed_proposal_fails_closed_with_stable_error(self) -> None:
        session = self.accepted_session()
        port = StaticProposalPort(
            SqlProposal(
                "proposal.malformed",
                "SELECT COUNT(*) FROM Artist",
                ("Artist",),
            )
        )
        with patch(
            "backend.agent.clarification_recovery.validate_candidate",
            side_effect=TypeError("TESTONLY unsafe boundary failure"),
        ):
            with self.assertRaisesRegex(
                ClarificationRecoveryError, "sql_safety_gate_failed"
            ):
                self.runtime.handoff_and_execute(session, proposal_port=port)
        self.assertEqual("stopped", session.workflow_status)
        self.assertEqual("sql_safety_gate_failed", session.stop_reason)
        self.assertEqual(0, session.local_sqlite_execution_attempts)

    def test_readonly_proposal_passes_gate_and_executes_once(self) -> None:
        session = self.accepted_session()
        port = StaticProposalPort(
            SqlProposal(
                "proposal.readonly",
                "SELECT COUNT(*) AS artist_count FROM Artist",
                ("Artist",),
            )
        )
        result = self.runtime.handoff_and_execute(session, proposal_port=port)
        self.assertEqual(1, port.calls)
        self.assertEqual(((275,),), result.rows)
        self.assertEqual("executed", session.workflow_status)
        self.assertEqual(1, session.safety_gate_calls)
        self.assertEqual(1, session.local_sqlite_execution_attempts)
        self.assertEqual(1, session.local_sqlite_execution_successes)
        self.assertEqual("sql_execution", session.audit_events[-1]["event_type"])

    def test_execution_elapsed_time_does_not_change_deterministic_result(self) -> None:
        results = []
        for elapsed_ms in (0, 999):
            session = self.accepted_session()
            port = StaticProposalPort(
                SqlProposal(
                    "proposal.stable-timing",
                    "SELECT COUNT(*) AS artist_count FROM Artist",
                    ("Artist",),
                )
            )
            self.runtime.handoff_and_execute(
                session,
                proposal_port=port,
                executor=StaticExecutionResultExecutor(elapsed_ms),
            )
            results.append(self.runtime.result(session))
        self.assertEqual(results[0], results[1])

    def test_non_accept_cannot_reach_safety_gate(self) -> None:
        session = self.start(_slot_state(metric="count"))
        port = StaticProposalPort(
            SqlProposal("proposal.blocked", "SELECT COUNT(*) FROM Artist", ("Artist",))
        )
        with self.assertRaisesRegex(
            ClarificationRecoveryError, "execution_session_not_ready"
        ):
            self.runtime.handoff_and_execute(session, proposal_port=port)
        self.assertEqual(0, port.calls)

    def test_mutating_pending_decision_cannot_bypass_clarification(self) -> None:
        session = self.start(_slot_state(metric="count"))
        session.current_decision["action"] = "accept"
        port = StaticProposalPort(
            SqlProposal(
                "proposal.pending-tamper",
                "SELECT COUNT(*) FROM Artist",
                ("Artist",),
            )
        )
        with self.assertRaisesRegex(
            ClarificationRecoveryError, "execution_session_not_ready"
        ):
            self.runtime.handoff_and_execute(session, proposal_port=port)
        self.assertEqual(0, port.calls)
        self.assertEqual(0, session.safety_gate_calls)
        self.assertEqual(0, session.local_sqlite_execution_attempts)

    def test_accepted_decision_tamper_is_detected_by_recomputation(self) -> None:
        session = self.accepted_session()
        session.current_decision["raw_score"] = 0.999999
        port = StaticProposalPort(
            SqlProposal(
                "proposal.accepted-tamper",
                "SELECT COUNT(*) FROM Artist",
                ("Artist",),
            )
        )
        with self.assertRaisesRegex(
            ClarificationRecoveryError, "execution_decision_mismatch"
        ):
            self.runtime.handoff_and_execute(session, proposal_port=port)
        self.assertEqual(0, port.calls)
        self.assertEqual(0, session.safety_gate_calls)
        self.assertEqual(0, session.local_sqlite_execution_attempts)

    def test_forged_safety_decision_is_rejected_by_existing_executor(self) -> None:
        forged = SafetyDecision(
            approved=True,
            execution_sql="SELECT * FROM Artist a JOIN Album b ON a.ArtistId=b.ArtistId",
            tables=("Artist",),
            row_limit=100,
        )
        with self.assertRaisesRegex(ValueError, "revalidation"):
            ReadonlyExecutor().execute(forged)

    def test_result_schema_trace_and_redaction(self) -> None:
        state = self.routed_state(
            query="TESTONLY_PRIVATE_PHRASE 艺术家",
            session_id="session.redaction.d4-05",
        )
        session = self.runtime.start(
            conversation_state=state,
            slot_observations=_slot_state(metric="count"),
            request_id="req.redaction.d4-05",
            recorded_at=RECORDED_AT,
        )
        self.runtime.resume(
            session,
            self.answer(session, "artist"),
            recorded_at=RECORDED_AT,
        )
        result = self.runtime.result(session)
        encoded = json.dumps(result, ensure_ascii=False).casefold()
        self.assertNotIn("testonly_private_phrase", encoded)
        self.assertNotIn('"values"', encoded)
        for forbidden in ('"expected"', '"gold_sql"', '"target_answer"'):
            self.assertNotIn(forbidden, encoded)
        self.assertEqual(2, len(result["decision_traces"]))
        tampered = deepcopy(result)
        tampered["unexpected"] = True
        with self.assertRaisesRegex(
            ClarificationRecoveryError, "clarification_result_schema_invalid"
        ):
            self.runtime.validate_result(tampered)


class D4ClarificationMachineReportTest(unittest.TestCase):
    def test_machine_report_is_deterministic_and_matches_committed_artifact(self) -> None:
        report = validate_all()
        self.assertEqual("pass", report["status"])
        self.assertEqual(
            report,
            json.loads(REPORT_PATH.read_text(encoding="utf-8")),
        )
        vertical = report["offline_orchestration_vertical_slice"]
        self.assertEqual(4, vertical["scenario_count"])
        self.assertEqual(1, vertical["local_sqlite_execution_attempts"])
        self.assertEqual(1, vertical["local_sqlite_execution_successes"])
        self.assertEqual(11, report["fail_closed_probes"]["count"])
        self.assertEqual(36, report["d4_02_attack_set"]["blocked_count"])
        self.assertTrue(
            report["safety_boundary"]["handoff_recomputes_d4_04_decision"]
        )
        self.assertTrue(
            report["safety_boundary"]["failed_redecision_is_transactional"]
        )
        self.assertTrue(
            report["safety_boundary"][
                "nondeterministic_latency_excluded_from_report_hash"
            ]
        )

    def test_machine_report_separates_evidence_and_records_zero_exposure(self) -> None:
        report = validate_all()
        classes = report["evidence_classification"]
        self.assertTrue(classes["conditional_component_evidence"])
        self.assertTrue(classes["offline_orchestration_evidence"])
        self.assertFalse(classes["business_accuracy_evidence"])
        self.assertFalse(classes["generalization_evidence"])
        self.assertFalse(classes["innovation_claim_evidence"])
        self.assertTrue(
            report["conditional_component_evidence"][
                "information_gain_probe"
            ]["selected_differs_from_baseline"]
        )
        for key in (
            "network_evaluation_calls",
            "model_api_calls",
            "credential_files_read",
            "estimated_cost_cny",
        ):
            self.assertEqual(0, report[key])
        isolation = report["input_isolation"]
        self.assertEqual(0, isolation["validation_current_task_exposure_count"])
        self.assertEqual(0, isolation["hidden_test_exposure_count"])
        self.assertFalse(isolation["answer_derived_fields_used_for_input"])
        encoded = json.dumps(report, ensure_ascii=False).casefold()
        self.assertNotIn("select count", encoded)
        self.assertNotIn("delete from", encoded)


if __name__ == "__main__":
    unittest.main()
