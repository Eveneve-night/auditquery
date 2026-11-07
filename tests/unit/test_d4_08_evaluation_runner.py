"""Tests for the D4-08 B-side aggregate evaluation runner."""

from __future__ import annotations

from contextlib import redirect_stdout
import copy
from dataclasses import replace
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

from jsonschema import Draft202012Validator


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

from backend.evaluation.d4_runner import (  # noqa: E402
    D4EvaluationError,
    EvaluationCaseOutcome,
    EvaluationRunMetadata,
    SlotCounts,
    build_evaluation_result,
    parse_runner_input,
)
from backend.evaluation.d4_execution import (  # noqa: E402
    ExecutionManifest, ExecutionReceipt, execute_matrix,
)
from backend.evaluation.d4_adapter import (  # noqa: E402
    ArmObservation, ControlledEvaluationAdapter, _is_answer_field,
)
from tools.evaluation.validate_d4_evaluation_freeze import (  # noqa: E402
    _forbidden_key as frozen_forbidden_key,
)
from tools.evaluation.run_d4_08_evaluation import (  # noqa: E402
    main,
    protocol_sha256,
    run,
)
from tools.evaluation.validate_d4_08_b_runner import (  # noqa: E402
    validate_all as validate_runner_selfcheck,
)


def _metadata() -> EvaluationRunMetadata:
    return EvaluationRunMetadata(
        run_id="synthetic-runner-selfcheck-001",
        data_version="synthetic-v1",
        validation_historical_count=0,
        validation_current_task_count=0,
        hidden_capability="nl2sql",
        hidden_case_count=40,
        hidden_content_committed=False,
        hidden_keeper="stevethomasih520-afk",
        domain_count=3,
        new_domain_prompt_or_code_changed=False,
        new_domain_test_answer_written=False,
        business_code_changed_lines=0,
        evidence_reproducible=True,
    )


def _case(index: int, *, correct: bool, latency: float, rank: int = 1) -> EvaluationCaseOutcome:
    necessary = index < 20
    return EvaluationCaseOutcome(
        case_id=f"synthetic.case-{index:03d}",
        e2e_correct=correct,
        necessary_clarification=necessary,
        did_clarify=necessary,
        post_clarification_success=correct if necessary else None,
        confidence=0.9 if correct else 0.2,
        sufficient_reference_correct=None if necessary else True,
        slot_counts={"slot.metric": SlotCounts(1 if correct else 0, 0, 0 if correct else 1)},
        schema_target_rank=rank,
        latency_ms=latency,
        model_calls=1,
        input_tokens=10,
        output_tokens=2,
        cost_cny=0.00001,
        contract_valid=True,
        evidence_reference_valid=True,
        trace_complete=True,
    )


def _groups() -> dict[str, tuple[EvaluationCaseOutcome, ...]]:
    correct_counts = {"B0": 24, "B1": 28, "B2": 22, "P": 38, "P-uncalibrated": 34}
    latencies = {"B0": 20.0, "B1": 18.0, "B2": 4.0, "P": 12.0, "P-uncalibrated": 11.0}
    result = {}
    for group_id, count in correct_counts.items():
        # Errors are kept in the necessary half so sufficient-question drop is 0.
        wrong = set(range(40 - count)) if count < 40 else set()
        result[group_id] = tuple(
            _case(index, correct=index not in wrong, latency=latencies[group_id], rank=1 if index % 4 else 2)
            for index in range(40)
        )
    return result


def _raw_payload() -> dict[str, object]:
    def serialize(case: EvaluationCaseOutcome) -> dict[str, object]:
        return {
            "case_id": case.case_id,
            "e2e_correct": case.e2e_correct,
            "necessary_clarification": case.necessary_clarification,
            "did_clarify": case.did_clarify,
            "post_clarification_success": case.post_clarification_success,
            "confidence": case.confidence,
            "sufficient_reference_correct": case.sufficient_reference_correct,
            "slot_counts": {
                name: {"tp": item.true_positive, "fp": item.false_positive, "fn": item.false_negative}
                for name, item in case.slot_counts.items()
            },
            "schema_target_rank": case.schema_target_rank,
            "latency_ms": case.latency_ms,
            "model_calls": case.model_calls,
            "input_tokens": case.input_tokens,
            "output_tokens": case.output_tokens,
            "cost_cny": case.cost_cny,
            "contract_valid": case.contract_valid,
            "evidence_reference_valid": case.evidence_reference_valid,
            "trace_complete": case.trace_complete,
        }

    metadata = _metadata()
    return {
        "run_id": metadata.run_id,
        "data_version": metadata.data_version,
        "groups": {
            group_id: [serialize(case) for case in cases]
            for group_id, cases in _groups().items()
        },
        "exposure": {
            "validation_historical_count": metadata.validation_historical_count,
            "validation_current_task_count": metadata.validation_current_task_count,
        },
        "hidden_test": {
            "capability": metadata.hidden_capability,
            "case_count": metadata.hidden_case_count,
            "content_committed": metadata.hidden_content_committed,
            "keeper": metadata.hidden_keeper,
        },
        "migration": {
            "domain_count": metadata.domain_count,
            "new_domain_prompt_or_code_changed": metadata.new_domain_prompt_or_code_changed,
            "new_domain_test_answer_written": metadata.new_domain_test_answer_written,
            "business_code_changed_lines": metadata.business_code_changed_lines,
        },
        "evidence_reproducible": metadata.evidence_reproducible,
    }


class D4EvaluationRunnerTest(unittest.TestCase):
    def test_controlled_adapter_runs_and_scores_five_arms_without_input_labels(self) -> None:
        protocol_bytes = (PROJECT_ROOT / "configs/evaluation/d4/experiment-protocol.v1.json").read_bytes()
        policy_bytes = (PROJECT_ROOT / "configs/policies/agent-loop-limits.v1.json").read_bytes()
        case_ids = tuple(case.case_id for case in _groups()["B0"])
        manifest = ExecutionManifest(
            case_ids=case_ids, data_version="synthetic-v1", data_sha256="a" * 64,
            candidate_catalog_sha256="b" * 64, scorer_sha256="c" * 64,
            adapter_id="controlled-synthetic-v1", adapter_sha256="d" * 64,
            split="synthetic",
        )

        class Source:
            candidate_catalog_sha256 = manifest.candidate_catalog_sha256
            inject_answer = False
            inject_nested_answer = False
            inject_nested_field = None

            def load_input(self, case_id):
                value = {"current_request": case_id, "candidate_options": ["table.sample"]}
                if self.inject_answer:
                    value["expected"] = "hidden answer"
                if self.inject_nested_answer:
                    value["candidate_options"] = [{"candidate_id": "table.sample", "gold_sql": "hidden"}]
                if self.inject_nested_field is not None:
                    value["current_request"] = {
                        "turns": [{"input": {"metadata": {self.inject_nested_field: "hidden"}}}],
                    }
                return value

        class Executor:
            def __init__(self):
                self.calls = []
                self.controls = {}
                self.bad_controls = False
                self.bad_usage = False

            def execute(self, case_input, controls):
                self.calls.append((controls.group_id, case_input.copy()))
                self.controls[controls.group_id] = controls
                self.assert_no_labels(case_input)
                model_calls = int(controls.uses_model)
                return ArmObservation(
                    output={"selected": "table.sample"},
                    control_fingerprint="bad" if self.bad_controls else controls.fingerprint,
                    enabled_components=controls.enabled_components,
                    disabled_components=controls.disabled_components,
                    output_constraint=controls.output_constraint,
                    model_id=controls.model_id if model_calls else None,
                    model_calls=model_calls,
                    input_tokens=10 * model_calls,
                    output_tokens=2 * model_calls,
                    cost_cny=0.00001 * model_calls,
                    latency_ms=5.0,
                )

            @staticmethod
            def assert_no_labels(value):
                assert "expected" not in value and "gold" not in value and "label" not in value

        class Scorer:
            scorer_sha256 = manifest.scorer_sha256

            def score(self, case_id, observation):
                index = case_ids.index(case_id)
                scored = _case(index, correct=index % 2 == 0, latency=observation.latency_ms)
                return replace(
                    scored,
                    model_calls=observation.model_calls + int(executor.bad_usage),
                    input_tokens=observation.input_tokens,
                    output_tokens=observation.output_tokens,
                    cost_cny=observation.cost_cny,
                )

        source = Source()
        executor = Executor()
        scorer = Scorer()
        adapter = ControlledEvaluationAdapter(
            adapter_id=manifest.adapter_id, adapter_sha256=manifest.adapter_sha256,
            offline=True, source=source, executor=executor, scorer=scorer,
        )
        metadata = replace(_metadata(), hidden_case_count=0)
        aggregate, audit = execute_matrix(
            manifest, metadata, adapter, protocol_bytes, policy_bytes,
            protocol_sha256=protocol_sha256(), bootstrap_samples=100,
        )
        self.assertEqual(5 * len(case_ids), len(executor.calls))
        self.assertEqual(set(_groups()), {group for group, _ in executor.calls})
        self.assertEqual(0, aggregate["groups"]["B2"]["metrics"]["model_calls"])
        self.assertFalse(aggregate["evidence_reproducible"])
        self.assertNotIn("table.sample", json.dumps(aggregate))
        self.assertEqual(len(case_ids), audit["case_count_per_group"])

        executor.bad_controls = True
        with self.assertRaisesRegex(D4EvaluationError, "arm_observed_controls_mismatch"):
            adapter.run_case(case_ids[0], executor.controls["B0"])
        executor.bad_controls = False
        executor.bad_usage = True
        with self.assertRaisesRegex(D4EvaluationError, "scorer_resource_mismatch"):
            adapter.run_case(case_ids[0], executor.controls["B0"])
        executor.bad_usage = False
        source.candidate_catalog_sha256 = "e" * 64
        with self.assertRaisesRegex(D4EvaluationError, "case_source_catalog_mismatch"):
            adapter.run_case(case_ids[0], executor.controls["B0"])
        source.candidate_catalog_sha256 = manifest.candidate_catalog_sha256
        source.inject_answer = True
        with self.assertRaisesRegex(D4EvaluationError, "case_input_fields_invalid"):
            adapter.run_case(case_ids[0], executor.controls["B0"])
        source.inject_answer = False
        source.inject_nested_answer = True
        with self.assertRaisesRegex(D4EvaluationError, "answer_field_in_case_input"):
            adapter.run_case(case_ids[0], executor.controls["B0"])
        source.inject_nested_answer = False
        for field in (
            "target_answer", "labels", "answers", "reference_answer", "answer_sql",
            "TARGET-ANSWER", "LaBeLs", "Reference Answer", "ANSWER-SQL",
            "expectedIntent", "GOLD SQL", "ground truth",
        ):
            with self.subTest(field=field):
                source.inject_nested_field = field
                calls_before = len(executor.calls)
                with self.assertRaisesRegex(D4EvaluationError, "answer_field_in_case_input"):
                    adapter.run_case(case_ids[0], executor.controls["B0"])
                self.assertEqual(calls_before, len(executor.calls))

    def test_answer_field_boundary_covers_frozen_d4_predicate(self) -> None:
        for field in (
            "expected", "EXPECTED_INTENT", "my-expected-answer", "gold", "GOLD SQL",
            "label", "LABELS", "answer", "ANSWERS", "TARGET ANSWER",
        ):
            with self.subTest(field=field):
                self.assertTrue(frozen_forbidden_key(field))
                self.assertTrue(_is_answer_field(field))
        self.assertFalse(_is_answer_field("candidate_id"))

    def test_observation_rejects_invalid_accounting(self) -> None:
        fields = dict(
            output=None, control_fingerprint="0" * 64, enabled_components=(),
            disabled_components=(), output_constraint="fixed_baseline_output",
            model_id=None, model_calls=0, input_tokens=0, output_tokens=0,
            cost_cny=0.0, latency_ms=0.0,
        )
        with self.assertRaisesRegex(D4EvaluationError, "observation_model_calls_invalid"):
            ArmObservation(**(fields | {"model_calls": True}))
        with self.assertRaisesRegex(D4EvaluationError, "observation_latency_ms_invalid"):
            ArmObservation(**(fields | {"latency_ms": float("nan")}))

    def test_every_frozen_metric_is_present_or_explicitly_unavailable(self) -> None:
        protocol = json.loads((PROJECT_ROOT / "configs/evaluation/d4/experiment-protocol.v1.json").read_text(encoding="utf-8"))
        result = build_evaluation_result(_groups(), _metadata(), protocol_sha256=protocol_sha256(), bootstrap_samples=100)
        metrics = result["groups"]["P"]["metrics"]
        for name in [protocol["metrics"]["primary"], *protocol["metrics"]["secondary"], *protocol["metrics"]["calibration"]]:
            self.assertIn(name, metrics)
            if metrics[name] is None:
                self.assertIn(name, metrics["metric_unavailable"])
        self.assertEqual("source_observation_missing", metrics["metric_unavailable"]["entity_f1"])
        self.assertEqual(0.95, metrics["post_clarification_end_to_end_execution_accuracy"])
        self.assertEqual(1.0, metrics["coverage_risk"][0]["coverage"])
        self.assertEqual(10, len(metrics["reliability_diagram"]))

    def test_optional_metric_observations_are_calculated_and_bad_fields_rejected(self) -> None:
        groups = {
            group_id: tuple(replace(case,
                entity_counts=SlotCounts(1, 0, 0),
                first_question_key_slot_hit=True if case.necessary_clarification else None,
                clarification_rounds=1 if case.did_clarify else 0,
            ) for case in cases)
            for group_id, cases in _groups().items()
        }
        metadata = replace(_metadata(), new_domain_setup_started_ms=1000,
                           new_domain_setup_finished_ms=13500,
                           domain_pack_entry_ids=tuple(f"entry-{index}" for index in range(40)))
        metrics = build_evaluation_result(groups, metadata, protocol_sha256=protocol_sha256(), bootstrap_samples=100)["groups"]["P"]["metrics"]
        self.assertEqual(1.0, metrics["entity_f1"])
        self.assertEqual(1.0, metrics["first_question_key_slot_hit_rate"])
        self.assertEqual(0.5, metrics["average_clarification_rounds"])
        self.assertEqual(12.5, metrics["new_domain_setup_time"])
        self.assertEqual(40, metrics["domain_pack_entry_count"])
        self.assertEqual({}, metrics["metric_unavailable"])
        raw = _raw_payload()
        raw["groups"]["P"][0]["entity_counts"] = {"tp": 1, "fp": 0, "fn": 0, "answer": "bad"}
        with self.assertRaisesRegex(D4EvaluationError, "entity_counts_fields_invalid"):
            parse_runner_input(raw)
        with self.assertRaisesRegex(D4EvaluationError, "new_domain_setup_interval_invalid"):
            replace(_metadata(), new_domain_setup_started_ms=2000, new_domain_setup_finished_ms=1000)
        with self.assertRaisesRegex(D4EvaluationError, "domain_pack_entry_ids_invalid"):
            replace(_metadata(), domain_pack_entry_ids=("entry-1", "entry-1"))

    def test_execution_adapter_enforces_frozen_arms_controls_and_budget(self) -> None:
        protocol_bytes = (PROJECT_ROOT / "configs/evaluation/d4/experiment-protocol.v1.json").read_bytes()
        policy_bytes = (PROJECT_ROOT / "configs/policies/agent-loop-limits.v1.json").read_bytes()
        digest = lambda letter: letter * 64
        manifest = ExecutionManifest(
            case_ids=tuple(case.case_id for case in _groups()["B0"]),
            data_version="synthetic-v1",
            data_sha256=digest("a"), candidate_catalog_sha256=digest("b"),
            scorer_sha256=digest("c"), adapter_id="synthetic-adapter-v1",
            adapter_sha256=digest("d"), split="synthetic",
        )

        class Adapter:
            adapter_id = manifest.adapter_id
            adapter_sha256 = manifest.adapter_sha256
            offline = True
            bad = False
            over_budget = False

            def run_case(self, case_id, controls):
                outcome = next(case for case in _groups()[controls.group_id] if case.case_id == case_id)
                if controls.group_id == "B2":
                    outcome = replace(outcome, model_calls=0, input_tokens=0, output_tokens=0, cost_cny=0)
                if self.over_budget:
                    outcome = replace(outcome, model_calls=5)
                return ExecutionReceipt(
                    outcome=outcome,
                    control_fingerprint="wrong" if self.bad else controls.fingerprint,
                    candidate_catalog_sha256=controls.candidate_catalog_sha256,
                    scorer_sha256=controls.scorer_sha256,
                    enabled_components=controls.enabled_components,
                    disabled_components=controls.disabled_components,
                    output_constraint=controls.output_constraint,
                    model_id=controls.model_id if outcome.model_calls else None,
                )

        adapter = Adapter()
        controlled_metadata = replace(_metadata(), hidden_case_count=0)
        aggregate, audit = execute_matrix(manifest, controlled_metadata, adapter, protocol_bytes, policy_bytes,
                                          protocol_sha256=protocol_sha256(), bootstrap_samples=100)
        self.assertEqual(40, audit["case_count_per_group"])
        self.assertEqual(0, aggregate["groups"]["B2"]["metrics"]["model_calls"])
        self.assertEqual(set(_groups()), set(audit["group_control_fingerprints"]))
        crlf_policy = policy_bytes.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
        crlf_aggregate, crlf_audit = execute_matrix(
            manifest, controlled_metadata, adapter, protocol_bytes, crlf_policy,
            protocol_sha256=protocol_sha256(), bootstrap_samples=100,
        )
        self.assertEqual(aggregate, crlf_aggregate)
        self.assertEqual(audit, crlf_audit)
        adapter.bad = True
        with self.assertRaisesRegex(D4EvaluationError, "adapter_controls_mismatch"):
            execute_matrix(manifest, controlled_metadata, adapter, protocol_bytes, policy_bytes, protocol_sha256=protocol_sha256())
        adapter.bad = False
        adapter.over_budget = True
        with self.assertRaisesRegex(D4EvaluationError, "case_budget_exceeded"):
            execute_matrix(manifest, controlled_metadata, adapter, protocol_bytes, policy_bytes, protocol_sha256=protocol_sha256())
        with self.assertRaisesRegex(D4EvaluationError, "protocol_sha256_invalid"):
            execute_matrix(manifest, controlled_metadata, adapter, protocol_bytes, policy_bytes, protocol_sha256=digest("0"))
        with self.assertRaisesRegex(D4EvaluationError, "data_version_mismatch"):
            execute_matrix(replace(manifest, data_version="other-v1"), controlled_metadata,
                           adapter, protocol_bytes, policy_bytes, protocol_sha256=protocol_sha256())
        with self.assertRaisesRegex(D4EvaluationError, "budget_policy_sha256_mismatch"):
            execute_matrix(manifest, controlled_metadata, adapter, protocol_bytes, b"{}", protocol_sha256=protocol_sha256())
        adapter.offline = False
        with self.assertRaisesRegex(D4EvaluationError, "online_execution_not_authorized"):
            execute_matrix(replace(manifest, offline=False, authorization_id="human-approval"),
                           controlled_metadata, adapter, protocol_bytes, policy_bytes,
                           protocol_sha256=protocol_sha256())
        adapter.offline = True
        with self.assertRaisesRegex(D4EvaluationError, "hidden_count_on_non_test_split"):
            execute_matrix(manifest, _metadata(), adapter, protocol_bytes, policy_bytes,
                           protocol_sha256=protocol_sha256())
    def test_builds_schema_valid_deterministic_aggregate(self) -> None:
        first = build_evaluation_result(
            _groups(), _metadata(), protocol_sha256=protocol_sha256(), bootstrap_samples=200
        )
        second = build_evaluation_result(
            _groups(), _metadata(), protocol_sha256=protocol_sha256(), bootstrap_samples=200
        )
        self.assertEqual(first, second)
        schema = json.loads(
            (PROJECT_ROOT / "configs/schemas/d4-08/evaluation-runner-result.v1.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual([], list(Draft202012Validator(schema).iter_errors(first)))
        self.assertEqual("P_vs_B1", first["statistics"]["mcnemar"]["comparison"])
        self.assertEqual(40, first["statistics"]["mcnemar"]["sample_pairs"])
        self.assertLessEqual(first["statistics"]["mcnemar"]["p_value"], 0.05)

    def test_computes_sql_performance_usage_and_clarification_metrics(self) -> None:
        result = build_evaluation_result(
            _groups(), _metadata(), protocol_sha256=protocol_sha256(), bootstrap_samples=100
        )
        proposed = result["groups"]["P"]["metrics"]
        self.assertEqual(0.95, proposed["post_clarification_end_to_end_execution_accuracy"])
        self.assertEqual(1.0, proposed["necessary_clarification_recall"])
        self.assertEqual(0.0, proposed["over_clarification_rate"])
        self.assertEqual(0.9, proposed["post_clarification_success_rate"])
        self.assertEqual(0.75, proposed["schema_recall_at_1"])
        self.assertEqual(1.0, proposed["schema_recall_at_3"])
        self.assertEqual(0.875, proposed["schema_mrr"])
        self.assertEqual(12.0, proposed["end_to_end_p95_ms"])
        self.assertEqual(480, proposed["tokens"])
        self.assertEqual(40, proposed["model_calls"])
        self.assertEqual(0.0004, proposed["cost_cny"])

    def test_output_contains_no_case_ids_or_content_fields(self) -> None:
        result = build_evaluation_result(
            _groups(), _metadata(), protocol_sha256=protocol_sha256(), bootstrap_samples=100
        )
        serialized = json.dumps(result, ensure_ascii=False)
        for forbidden in (
            "synthetic.case-",
            '"query"',
            '"prompt"',
            '"sql"',
            '"answer"',
            '"raw_output"',
        ):
            self.assertNotIn(forbidden, serialized)

    def test_rejects_unpaired_or_duplicate_case_ids(self) -> None:
        groups = _groups()
        groups["P"] = groups["P"][:-1]
        with self.assertRaisesRegex(D4EvaluationError, "paired_case_ids_mismatch"):
            build_evaluation_result(
                groups, _metadata(), protocol_sha256=protocol_sha256(), bootstrap_samples=100
            )
        groups = _groups()
        groups["B0"] = groups["B0"] + (groups["B0"][0],)
        with self.assertRaisesRegex(D4EvaluationError, "duplicate_case_id"):
            build_evaluation_result(
                groups, _metadata(), protocol_sha256=protocol_sha256(), bootstrap_samples=100
            )

    def test_rejects_group_specific_scoring_labels(self) -> None:
        raw = _raw_payload()
        raw["groups"]["P"][0]["necessary_clarification"] = False
        raw["groups"]["P"][0]["post_clarification_success"] = None
        raw["groups"]["P"][0]["sufficient_reference_correct"] = True
        groups, metadata = parse_runner_input(raw)
        with self.assertRaisesRegex(D4EvaluationError, "paired_clarification_label_mismatch"):
            build_evaluation_result(groups, metadata, protocol_sha256=protocol_sha256())

        raw = _raw_payload()
        raw["groups"]["P"][20]["sufficient_reference_correct"] = False
        groups, metadata = parse_runner_input(raw)
        with self.assertRaisesRegex(D4EvaluationError, "paired_reference_label_mismatch"):
            build_evaluation_result(groups, metadata, protocol_sha256=protocol_sha256())

    def test_hidden_case_count_cannot_exceed_supplied_cases(self) -> None:
        raw = _raw_payload()
        raw["hidden_test"]["case_count"] = 120
        groups, metadata = parse_runner_input(raw)
        with self.assertRaisesRegex(D4EvaluationError, "hidden_case_count_exceeds_input_cases"):
            build_evaluation_result(groups, metadata, protocol_sha256=protocol_sha256())

    def test_success_cannot_follow_missing_clarification(self) -> None:
        raw = _raw_payload()
        raw["groups"]["P"][0]["did_clarify"] = False
        raw["groups"]["P"][0]["post_clarification_success"] = True
        with self.assertRaisesRegex(D4EvaluationError, "post_clarification_success_without_clarification"):
            parse_runner_input(raw)

    def test_rejects_missing_group_and_invalid_protocol_hash(self) -> None:
        groups = _groups()
        del groups["B2"]
        with self.assertRaisesRegex(D4EvaluationError, "group_set_mismatch"):
            build_evaluation_result(groups, _metadata(), protocol_sha256=protocol_sha256())
        with self.assertRaisesRegex(D4EvaluationError, "protocol_sha256_invalid"):
            build_evaluation_result(_groups(), _metadata(), protocol_sha256="bad")

    def test_parse_rejects_content_or_unknown_fields(self) -> None:
        raw = _raw_payload()
        raw["groups"]["P"][0]["query"] = "forbidden content"
        with self.assertRaisesRegex(D4EvaluationError, "case_unknown_fields:query"):
            parse_runner_input(raw)
        raw = _raw_payload()
        raw["answer"] = "forbidden"
        with self.assertRaisesRegex(D4EvaluationError, "runner_input_unknown_fields:answer"):
            parse_runner_input(raw)

    def test_invalid_boolean_number_and_conditional_fields_fail_closed(self) -> None:
        raw = _raw_payload()
        raw["groups"]["P"][0]["e2e_correct"] = 1
        with self.assertRaisesRegex(D4EvaluationError, "e2e_correct_invalid"):
            parse_runner_input(raw)
        raw = _raw_payload()
        raw["groups"]["P"][0]["confidence"] = float("nan")
        with self.assertRaisesRegex(D4EvaluationError, "confidence_invalid"):
            parse_runner_input(raw)
        raw = _raw_payload()
        raw["groups"]["P"][0]["post_clarification_success"] = None
        with self.assertRaisesRegex(D4EvaluationError, "post_clarification_success_required"):
            parse_runner_input(raw)

    def test_cli_reads_external_input_and_writes_only_aggregate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "outcomes.json"
            output_path = Path(directory) / "aggregate.json"
            input_path.write_text(json.dumps(_raw_payload()), encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                self.assertEqual(
                    0,
                    main(
                        [
                            "--input",
                            str(input_path),
                            "--output",
                            str(output_path),
                            "--bootstrap-samples",
                            "100",
                        ]
                    ),
                )
            written = json.loads(output_path.read_text(encoding="utf-8"))
            direct = run(input_path, bootstrap_samples=100)
        self.assertEqual(direct, written)
        self.assertNotIn("synthetic.case-", json.dumps(written))
        self.assertFalse(written["evidence_reproducible"])

    def test_parser_does_not_mutate_input(self) -> None:
        raw = _raw_payload()
        before = copy.deepcopy(raw)
        parse_runner_input(raw)
        self.assertEqual(before, raw)

    def test_machine_selfcheck_is_explicitly_synthetic_and_offline(self) -> None:
        report = validate_runner_selfcheck()
        self.assertEqual("pass", report["status"])
        self.assertEqual("synthetic_mechanics_selfcheck_only", report["evidence_kind"])
        self.assertFalse(report["business_claims_allowed"])
        self.assertTrue(all(report["checks"].values()))
        self.assertTrue(all(item["detected"] for item in report["mutation_checks"]))
        self.assertTrue(report["synthetic_fixture"]["gate_verdict_withheld_for_synthetic_data"])
        self.assertNotIn("innovation_success", json.dumps(report))
        self.assertEqual(
            {
                "network_calls": 0,
                "model_api_calls": 0,
                "real_key_reads": 0,
                "validation_case_reads": 0,
                "hidden_test_case_reads": 0,
                "estimated_cost_cny": 0.0,
            },
            report["boundaries"],
        )


if __name__ == "__main__":
    unittest.main()
