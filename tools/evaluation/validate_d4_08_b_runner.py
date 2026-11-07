"""Generate the D4-08-01 B-side runner self-check report.

All outcomes below are synthetic mechanics fixtures.  They are not evaluation
evidence and cannot support accuracy, migration, or innovation claims.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
from dataclasses import replace
import sys
from typing import Any, Sequence

from jsonschema import Draft202012Validator


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from backend.evaluation.d4_runner import (  # noqa: E402
    D4EvaluationError,
    EvaluationCaseOutcome,
    EvaluationRunMetadata,
    SlotCounts,
    build_evaluation_result,
    parse_runner_input,
)
from backend.evaluation.d4_execution import (  # noqa: E402
    ExecutionManifest, ExecutionReceipt,
)
from tools.evaluation.run_d4_08_evaluation import protocol_sha256, run_controlled  # noqa: E402
from tools.evaluation.validate_d4_08_conclusion_gate import (  # noqa: E402
    GATE_PATH,
    compute_conclusion,
)


REPORT_PATH = PROJECT_ROOT / "tests/reports/d4-08-b-evaluation-runner-v1.json"
RESULT_SCHEMA_PATH = PROJECT_ROOT / "configs/schemas/d4-08/evaluation-runner-result.v1.schema.json"
RUNNER_PATH = PROJECT_ROOT / "src/backend/evaluation/d4_runner.py"
CLI_PATH = PROJECT_ROOT / "tools/evaluation/run_d4_08_evaluation.py"
EXECUTION_PATH = PROJECT_ROOT / "src/backend/evaluation/d4_execution.py"


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _artifact(path: Path) -> dict[str, Any]:
    payload = path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
    return {
        "path": path.relative_to(PROJECT_ROOT).as_posix(),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "bytes": len(payload),
    }


def _case(index: int, correct: bool, latency: float) -> EvaluationCaseOutcome:
    necessary = index < 20
    return EvaluationCaseOutcome(
        case_id=f"selfcheck.case-{index:03d}",
        e2e_correct=correct,
        necessary_clarification=necessary,
        did_clarify=necessary,
        post_clarification_success=correct if necessary else None,
        confidence=0.9 if correct else 0.2,
        sufficient_reference_correct=None if necessary else True,
        slot_counts={"slot.metric": SlotCounts(int(correct), 0, int(not correct))},
        schema_target_rank=1 if index % 4 else 2,
        latency_ms=latency,
        model_calls=1,
        input_tokens=10,
        output_tokens=2,
        cost_cny=0.00001,
        contract_valid=True,
        evidence_reference_valid=True,
        trace_complete=True,
    )


def _fixture() -> tuple[dict[str, tuple[EvaluationCaseOutcome, ...]], EvaluationRunMetadata]:
    correct_counts = {"B0": 24, "B1": 28, "B2": 22, "P": 38, "P-uncalibrated": 34}
    latencies = {"B0": 20.0, "B1": 18.0, "B2": 4.0, "P": 12.0, "P-uncalibrated": 11.0}
    groups = {
        group_id: tuple(
            _case(index, index >= 40 - count, latencies[group_id])
            for index in range(40)
        )
        for group_id, count in correct_counts.items()
    }
    metadata = EvaluationRunMetadata(
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
    return groups, metadata


def _mutation_checks(groups: dict[str, tuple[EvaluationCaseOutcome, ...]], metadata: EvaluationRunMetadata) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []

    def detected(name: str, action: Any) -> None:
        try:
            action()
        except D4EvaluationError as error:
            checks.append({"mutation": name, "detected": True, "error_code": str(error).split(":", 1)[0]})
        else:
            checks.append({"mutation": name, "detected": False, "error_code": None})

    mismatched = dict(groups)
    mismatched["P"] = mismatched["P"][:-1]
    detected(
        "paired_case_removed",
        lambda: build_evaluation_result(mismatched, metadata, protocol_sha256=protocol_sha256()),
    )
    missing_group = dict(groups)
    del missing_group["B2"]
    detected(
        "required_group_removed",
        lambda: build_evaluation_result(missing_group, metadata, protocol_sha256=protocol_sha256()),
    )
    detected(
        "protocol_hash_tampered",
        lambda: build_evaluation_result(groups, metadata, protocol_sha256="0" * 63),
    )
    detected("content_field_injected", lambda: parse_runner_input({"query": "forbidden"}))
    return checks


def validate_all() -> dict[str, Any]:
    groups, metadata = _fixture()
    controlled_metadata = replace(metadata, hidden_case_count=0)
    manifest = ExecutionManifest(
        case_ids=tuple(case.case_id for case in groups["B0"]),
        data_version="synthetic-v1",
        data_sha256="a" * 64, candidate_catalog_sha256="b" * 64,
        scorer_sha256="c" * 64, adapter_id="synthetic-controlled-v1",
        adapter_sha256="d" * 64, split="synthetic",
    )

    class SyntheticAdapter:
        adapter_id = manifest.adapter_id
        adapter_sha256 = manifest.adapter_sha256
        offline = True
        mutate_controls = False

        def run_case(self, case_id: str, controls: Any) -> ExecutionReceipt:
            case = next(item for item in groups[controls.group_id] if item.case_id == case_id)
            if controls.group_id == "B2":
                case = replace(case, model_calls=0, input_tokens=0, output_tokens=0, cost_cny=0)
            return ExecutionReceipt(
                outcome=case,
                control_fingerprint="invalid" if self.mutate_controls else controls.fingerprint,
                candidate_catalog_sha256=controls.candidate_catalog_sha256,
                scorer_sha256=controls.scorer_sha256,
                enabled_components=controls.enabled_components,
                disabled_components=controls.disabled_components,
                output_constraint=controls.output_constraint,
                model_id=controls.model_id if case.model_calls else None,
            )

    adapter = SyntheticAdapter()
    controlled_result, control_audit = run_controlled(
        manifest, controlled_metadata, adapter, bootstrap_samples=500
    )
    adapter.mutate_controls = True
    try:
        run_controlled(manifest, controlled_metadata, adapter, bootstrap_samples=500)
    except D4EvaluationError as error:
        control_mutation = {"mutation": "adapter_control_tampered", "detected": True,
                            "error_code": str(error).split(":", 1)[0]}
    else:
        control_mutation = {"mutation": "adapter_control_tampered", "detected": False,
                            "error_code": None}
    result = build_evaluation_result(
        groups,
        metadata,
        protocol_sha256=protocol_sha256(),
        bootstrap_samples=500,
    )
    schema_errors = list(Draft202012Validator(_load_json(RESULT_SCHEMA_PATH)).iter_errors(result))
    conclusion = compute_conclusion(_load_json(GATE_PATH), result)
    mutations = _mutation_checks(groups, metadata) + [control_mutation]
    serialized = json.dumps(result, ensure_ascii=False, sort_keys=True)
    forbidden_markers = ('"query"', '"prompt"', '"sql"', '"answer"', '"raw_output"', "selfcheck.case-")
    content_safe = all(marker not in serialized for marker in forbidden_markers)
    checks = {
        "result_schema_valid": not schema_errors,
        "all_five_groups_present": set(result["groups"]) == {"B0", "B1", "B2", "P", "P-uncalibrated"},
        "statistics_bound_to_best_baseline": result["statistics"]["mcnemar"]["comparison"] == "P_vs_B1",
        "deterministic_statistics": result == build_evaluation_result(
            groups,
            metadata,
            protocol_sha256=protocol_sha256(),
            bootstrap_samples=500,
        ),
        "aggregate_is_content_safe": content_safe,
        "all_mutations_detected": all(item["detected"] for item in mutations),
        "synthetic_fixture_accepted_by_gate": not conclusion["errors"],
        "controlled_execution_matches_aggregate": controlled_result["groups"]["P"]["metrics"] == result["groups"]["P"]["metrics"],
        "shared_controls_audited": len(control_audit["group_control_fingerprints"]) == 5,
        "all_protocol_metrics_present": all(
            name in result["groups"]["P"]["metrics"]
            for name in (
                [_load_json(PROJECT_ROOT / "configs/evaluation/d4/experiment-protocol.v1.json")["metrics"]["primary"]]
                + _load_json(PROJECT_ROOT / "configs/evaluation/d4/experiment-protocol.v1.json")["metrics"]["secondary"]
                + _load_json(PROJECT_ROOT / "configs/evaluation/d4/experiment-protocol.v1.json")["metrics"]["calibration"]
            )
        ),
    }
    return {
        "status": "pass" if all(checks.values()) else "fail",
        "task_id": "D4-08-01",
        "owner": "B",
        "evidence_kind": "synthetic_mechanics_selfcheck_only",
        "business_claims_allowed": False,
        "checks": checks,
        "mutation_checks": mutations,
        "synthetic_fixture": {
            "case_count_per_group": 40,
            "gate_structure_errors": conclusion["errors"],
            "gate_verdict_withheld_for_synthetic_data": True,
            "result_sha256": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
            "controlled_manifest_sha256": control_audit["manifest_sha256"],
            "controlled_aggregate_sha256": control_audit["aggregate_sha256"],
            "group_control_fingerprints": control_audit["group_control_fingerprints"],
        },
        "artifacts": [_artifact(path) for path in (RUNNER_PATH, EXECUTION_PATH, CLI_PATH, RESULT_SCHEMA_PATH)],
        "boundaries": {
            "network_calls": 0,
            "model_api_calls": 0,
            "real_key_reads": 0,
            "validation_case_reads": 0,
            "hidden_test_case_reads": 0,
            "estimated_cost_cny": 0.0,
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成 D4-08-01 B 侧运行器合成自检报告")
    parser.add_argument("--report", type=Path, default=REPORT_PATH)
    args = parser.parse_args(argv)
    report = validate_all()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "report": str(args.report)}, ensure_ascii=False))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
