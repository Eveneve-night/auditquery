"""Run the D4-08 B-side aggregate evaluator on a content-free input file."""

from __future__ import annotations

import argparse
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys
from typing import Sequence

from jsonschema import Draft202012Validator


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from backend.evaluation.d4_runner import (  # noqa: E402
    D4EvaluationError,
    EvaluationRunMetadata,
    build_evaluation_result,
    parse_runner_input,
)
from backend.evaluation.d4_execution import (  # noqa: E402
    EvaluationAdapter, ExecutionManifest, execute_matrix,
)
from tools.evaluation.validate_d4_08_conclusion_gate import (  # noqa: E402
    GATE_PATH,
    compute_conclusion,
)


PROTOCOL_PATH = PROJECT_ROOT / "configs/evaluation/d4/experiment-protocol.v1.json"
POLICY_PATH = PROJECT_ROOT / "configs/policies/agent-loop-limits.v1.json"
RESULT_SCHEMA_PATH = (
    PROJECT_ROOT / "configs/schemas/d4-08/evaluation-runner-result.v1.schema.json"
)


def _canonical_bytes(path: Path) -> bytes:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")


def protocol_sha256() -> str:
    return hashlib.sha256(_canonical_bytes(PROTOCOL_PATH)).hexdigest()


def frozen_conclusion(result: dict[str, object]) -> dict[str, object]:
    gate = json.loads(GATE_PATH.read_text(encoding="utf-8"))
    conclusion = compute_conclusion(gate, result)
    if conclusion["verdict"] == "invalid_result":
        raise D4EvaluationError("conclusion_gate_invalid_result")
    return conclusion


def run(input_path: Path, *, bootstrap_samples: int = 2000) -> dict[str, object]:
    """Legacy outcome-only aggregation; never establishes reproducible execution."""
    raw = json.loads(input_path.read_text(encoding="utf-8"))
    groups, metadata = parse_runner_input(raw)
    metadata = replace(metadata, evidence_reproducible=False)
    result = build_evaluation_result(
        groups,
        metadata,
        protocol_sha256=protocol_sha256(),
        bootstrap_samples=bootstrap_samples,
    )
    schema = json.loads(RESULT_SCHEMA_PATH.read_text(encoding="utf-8"))
    errors = sorted(Draft202012Validator(schema).iter_errors(result), key=lambda item: list(item.path))
    if errors:
        detail = "; ".join(error.message for error in errors[:5])
        raise D4EvaluationError(f"result_schema_invalid:{detail}")
    frozen_conclusion(result)
    return result


def run_controlled(
    manifest: ExecutionManifest,
    metadata: EvaluationRunMetadata,
    adapter: EvaluationAdapter,
    *,
    allow_online: bool = False,
    bootstrap_samples: int = 2000,
) -> tuple[dict[str, object], dict[str, object]]:
    """Five-arm execution API for an audited adapter and external case keeper."""

    protocol_bytes = _canonical_bytes(PROTOCOL_PATH)
    result, audit = execute_matrix(
        manifest, metadata, adapter, protocol_bytes, POLICY_PATH.read_bytes(),
        protocol_sha256=protocol_sha256(), allow_online=allow_online,
        bootstrap_samples=bootstrap_samples,
    )
    schema = json.loads(RESULT_SCHEMA_PATH.read_text(encoding="utf-8"))
    if list(Draft202012Validator(schema).iter_errors(result)):
        raise D4EvaluationError("result_schema_invalid")
    frozen_conclusion(result)
    return result, audit


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="仅聚合 D4-08 脱敏逐例结果；此入口不能证明五组执行控制，不能发布正式结论"
    )
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--bootstrap-samples", type=int, default=2000)
    args = parser.parse_args(argv)
    result = run(args.input, bootstrap_samples=args.bootstrap_samples)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "status": "pass",
                "runner_id": result["runner_id"],
                "run_id": result["run_id"],
                "gate_structure": "accepted",
                "conclusion": "withheld_aggregate_only_not_reproducible",
                "output": str(args.output),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
