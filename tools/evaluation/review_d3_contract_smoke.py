"""Offline, content-safe D3-06 review of a D3 contract smoke report.

The reviewer consumes only the already-sanitized machine report and repository
artifacts.  It never loads credentials, constructs prompts, or calls a model.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SOURCE_REPORT = (
    PROJECT_ROOT
    / "tests"
    / "reports"
    / "d3-contract-smoke-qwen3.7-flash-v1.json"
)
DEFAULT_MANIFEST = PROJECT_ROOT / "tests" / "goldens" / "d3-smoke" / "v1" / "manifest.json"
DEFAULT_GATE_REPORT = PROJECT_ROOT / "tests" / "reports" / "d3-contract-smoke-gate-v1.json"
EXPECTED_SOURCE_SHA256 = "21fc24b1fb6f85360a2b422c77f63955ce85372e228f9e5c821ea789037feaff"
APPROVED_MODEL = "qwen3.7-flash"
HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{16,}=*", re.IGNORECASE),
    re.compile(
        r"\b(?:DASHSCOPE_API_KEY|OPENAI_API_KEY)\s*[:=]\s*[^,\s\"']+",
        re.IGNORECASE,
    ),
)
FORBIDDEN_CASE_FIELDS = frozenset(
    {
        "prompt",
        "messages",
        "question",
        "raw_response",
        "response_content",
        "candidate_sql",
        "sql",
        "answer",
        "evidence",
        "evidence_body",
        "evidence_text",
    }
)
REQUIRED_CASE_FIELDS = frozenset(
    {
        "case_id",
        "task_type",
        "evaluation_scope",
        "prompt_version",
        "output_schema_id",
        "status",
        "provider_id",
        "requested_model",
        "returned_model",
        "attempt_count",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "estimated_cost_cny",
        "latency_ms",
        "usage_source",
        "finish_reason",
        "warnings",
        "error_code",
        "stop_reason",
        "output_validation",
    }
)


class GateReviewError(RuntimeError):
    """Raised when the source report cannot be reviewed safely."""


def _load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise GateReviewError(f"expected a JSON object: {path}")
    return value


def _sha256(path: Path) -> str:
    """Hash legacy D3 text artifacts with the frozen CRLF convention.

    The D3 evidence was produced on Windows and its recorded hashes therefore
    cover CRLF bytes.  Git stores the same text as LF, so hashing raw checkout
    bytes makes verification depend on the runner platform.  Canonicalizing
    text line endings back to the frozen convention preserves the original
    evidence while still detecting every content change.
    """
    content = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return hashlib.sha256(content.replace(b"\n", b"\r\n")).hexdigest()


def _decimal(value: Any) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise GateReviewError("numeric report field has an invalid type")
    return Decimal(str(value))


def _linear_percentile(values: Sequence[float], percentile: float) -> float:
    """Hyndman-Fan type 7 percentile: interpolate at (n - 1) * p."""

    if not values:
        raise GateReviewError("cannot calculate a percentile from an empty sample")
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _forbidden_case_field_count(value: Any) -> int:
    if isinstance(value, Mapping):
        return sum(
            (1 if str(key).lower() in FORBIDDEN_CASE_FIELDS else 0)
            + _forbidden_case_field_count(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return sum(_forbidden_case_field_count(item) for item in value)
    return 0


def _secret_pattern_count(raw_text: str) -> int:
    return sum(len(pattern.findall(raw_text)) for pattern in SECRET_PATTERNS)


def _manifest_cases(manifest: Mapping[str, Any]) -> tuple[list[str], dict[str, Mapping[str, Any]]]:
    case_ids: list[str] = []
    metadata: dict[str, Mapping[str, Any]] = {}
    tasks = manifest.get("tasks")
    if not isinstance(tasks, list):
        raise GateReviewError("manifest tasks must be an array")
    for task in tasks:
        if not isinstance(task, Mapping) or not isinstance(task.get("case_ids"), list):
            raise GateReviewError("manifest task is invalid")
        for case_id in task["case_ids"]:
            if not isinstance(case_id, str) or case_id in metadata:
                raise GateReviewError("manifest case IDs must be unique strings")
            case_ids.append(case_id)
            metadata[case_id] = task
    return case_ids, metadata


def _artifact_hashes_match(report: Mapping[str, Any], project_root: Path) -> bool:
    try:
        artifact_hashes = report["provenance"]["artifact_hashes"]
    except (KeyError, TypeError):
        return False
    if not isinstance(artifact_hashes, Mapping) or not artifact_hashes:
        return False
    root = project_root.resolve()
    for relative_path, expected_hash in artifact_hashes.items():
        if not isinstance(relative_path, str) or not isinstance(expected_hash, str):
            return False
        candidate = (root / relative_path).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            return False
        if not candidate.is_file() or _sha256(candidate) != expected_hash.lower():
            return False
    return True


def review_report(
    source_path: Path = DEFAULT_SOURCE_REPORT,
    *,
    manifest_path: Path = DEFAULT_MANIFEST,
    project_root: Path = PROJECT_ROOT,
    expected_source_sha256: str = EXPECTED_SOURCE_SHA256,
    generated_at: str | None = None,
) -> dict[str, Any]:
    source_path = source_path.resolve()
    manifest_path = manifest_path.resolve()
    report = _load_object(source_path)
    manifest = _load_object(manifest_path)
    raw_text = source_path.read_text(encoding="utf-8")
    source_sha256 = _sha256(source_path)

    cases = report.get("cases")
    summary = report.get("summary")
    if not isinstance(cases, list) or not all(isinstance(item, Mapping) for item in cases):
        raise GateReviewError("report cases must be an array of objects")
    if not isinstance(summary, Mapping):
        raise GateReviewError("report summary must be an object")

    manifest_ids, manifest_metadata = _manifest_cases(manifest)
    case_ids = [item.get("case_id") for item in cases]
    attempted = [item for item in cases if item.get("status") != "not_run"]
    valid = [item for item in cases if item.get("status") == "contract_valid"]
    failed = [item for item in cases if item.get("status") == "failed"]
    not_run = [item for item in cases if item.get("status") == "not_run"]

    attempts = sum(int(item.get("attempt_count", -1)) for item in cases)
    retries = sum(max(0, int(item.get("attempt_count", 0)) - 1) for item in attempted)
    input_tokens = sum(int(item.get("input_tokens", -1)) for item in cases)
    output_tokens = sum(int(item.get("output_tokens", -1)) for item in cases)
    total_tokens = sum(int(item.get("total_tokens", -1)) for item in cases)
    estimated_cost = sum(
        (_decimal(item.get("estimated_cost_cny", "NaN")) for item in cases),
        Decimal("0"),
    )
    latencies = [float(item["latency_ms"]) for item in attempted]

    per_case_fields_complete = all(
        REQUIRED_CASE_FIELDS.issubset(item.keys()) for item in cases
    )
    per_case_token_math_valid = all(
        item.get("total_tokens") == item.get("input_tokens", -1) + item.get("output_tokens", -1)
        for item in cases
    )
    output_validation_complete = all(
        isinstance(item.get("output_validation"), Mapping)
        and item["output_validation"].get("schema_id") == item.get("output_schema_id")
        and item["output_validation"].get("schema_version") == "1.0.0"
        and item["output_validation"].get("json_schema_validation") == "passed"
        and item["output_validation"].get("semantic_validation") == "passed"
        and isinstance(item["output_validation"].get("normalized_output_sha256"), str)
        and HASH_PATTERN.fullmatch(
            item["output_validation"]["normalized_output_sha256"]
        )
        is not None
        for item in valid
    )
    manifest_case_metadata_matches = all(
        isinstance(case_id, str)
        and case_id in manifest_metadata
        and item.get("task_type") == manifest_metadata[case_id].get("task_type")
        and item.get("evaluation_scope")
        == manifest_metadata[case_id].get("evaluation_scope")
        for case_id, item in zip(case_ids, cases)
    )

    execution_safety = report.get("execution_safety")
    execution_safety_valid = isinstance(execution_safety, Mapping) and all(
        (
            execution_safety.get("human_authorization_required") is True,
            execution_safety.get("explicit_confirmation_received") is True,
            execution_safety.get("credential_persisted") is False,
            execution_safety.get("raw_prompt_persisted") is False,
            execution_safety.get("raw_response_persisted") is False,
            execution_safety.get("candidate_sql_executed") is False,
        )
    )

    limits = report.get("limits", {}).get("batch", {})
    within_batch_budget = isinstance(limits, Mapping) and all(
        (
            attempts <= int(limits.get("max_provider_attempts", -1)),
            input_tokens <= int(limits.get("max_input_tokens", -1)),
            output_tokens <= int(limits.get("max_output_tokens", -1)),
            estimated_cost <= _decimal(limits.get("hard_cost_cny", "NaN")),
            int(summary.get("elapsed_ms", -1)) <= int(limits.get("max_wall_time_ms", -1)),
        )
    )

    aggregate_reconciled = all(
        (
            summary.get("planned_case_count") == len(manifest_ids),
            summary.get("case_count") == len(cases),
            summary.get("attempted_case_count") == len(attempted),
            summary.get("contract_valid_count") == len(valid),
            summary.get("failed_count") == len(failed),
            summary.get("not_run_count") == len(not_run),
            summary.get("model_attempt_count") == attempts,
            summary.get("input_tokens") == input_tokens,
            summary.get("output_tokens") == output_tokens,
            summary.get("total_tokens") == total_tokens,
            _decimal(summary.get("estimated_cost_cny", "NaN")) == estimated_cost,
        )
    )

    claims = report.get("claims")
    scope_boundaries_valid = (
        report.get("purpose") == "contract_smoke_only"
        and report.get("source_split") == "prompt_dev"
        and report.get("validation_exposure_count") == 0
        and claims
        == {"accuracy": False, "generalization": False, "end_to_end": False}
    )
    fixed_model_valid = (
        report.get("model") == APPROVED_MODEL
        and manifest.get("model") == APPROVED_MODEL
        and all(
            item.get("requested_model") == APPROVED_MODEL
            and item.get("returned_model") == APPROVED_MODEL
            for item in attempted
        )
    )
    complete_contract_smoke = (
        len(cases) == 20
        and len(valid) == 20
        and not failed
        and not not_run
        and summary.get("batch_stop_reason") is None
    )

    forbidden_content_field_count = _forbidden_case_field_count(cases)
    suspicious_secret_pattern_count = _secret_pattern_count(raw_text)
    try:
        source_display_path = source_path.relative_to(project_root.resolve()).as_posix()
    except ValueError:
        source_display_path = source_path.name
    checks = {
        "source_sha256_verified": source_sha256 == expected_source_sha256.lower(),
        "report_and_manifest_identity_match": (
            report.get("manifest_id") == manifest.get("manifest_id")
            and report.get("manifest_version") == manifest.get("version")
        ),
        "manifest_case_ids_match_in_order": case_ids == manifest_ids,
        "manifest_case_metadata_match": manifest_case_metadata_matches,
        "repository_artifact_hashes_verified": _artifact_hashes_match(
            report, project_root
        ),
        "scope_and_claim_boundaries_valid": scope_boundaries_valid,
        "fixed_model_qwen3_7_flash": fixed_model_valid,
        "all_20_contracts_valid": complete_contract_smoke,
        "aggregate_reconciled_from_cases": aggregate_reconciled,
        "per_case_required_fields_complete": per_case_fields_complete,
        "per_case_token_math_valid": per_case_token_math_valid,
        "schema_semantic_and_output_hash_complete": output_validation_complete,
        "batch_budget_respected": within_batch_budget,
        "execution_safety_flags_valid": execution_safety_valid,
        "forbidden_content_fields_absent": forbidden_content_field_count == 0,
        "suspicious_secret_patterns_absent": suspicious_secret_pattern_count == 0,
    }
    passed = all(checks.values())

    return {
        "report_type": "d3_contract_smoke_gate_review",
        "version": "1.0.0",
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "review_actor_type": "independent_machine_review",
        "review_scope": "sanitized_report_and_repository_artifacts_only",
        "api_calls_during_review": 0,
        "source_report": {
            "path": source_display_path,
            "sha256": source_sha256,
            "expected_sha256": expected_source_sha256.lower(),
            "sha256_verified": checks["source_sha256_verified"],
        },
        "review_tool": {
            "path": "tools/evaluation/review_d3_contract_smoke.py",
            "sha256": _sha256(Path(__file__).resolve()),
        },
        "manifest": {
            "path": manifest_path.relative_to(project_root.resolve()).as_posix(),
            "sha256": _sha256(manifest_path),
        },
        "metrics": {
            "case_count": len(cases),
            "task_counts": dict(sorted(Counter(item["task_type"] for item in cases).items())),
            "contract_valid_count": len(valid),
            "failed_count": len(failed),
            "not_run_count": len(not_run),
            "provider_attempt_count": attempts,
            "provider_retry_count": retries,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "estimated_cost_cny": float(estimated_cost),
            "batch_stop_reason": summary.get("batch_stop_reason"),
            "provider_latency_ms": {
                "definition": "per-case sum of provider-attempt latency from UsageLedger; excludes not_run cases",
                "sample_count": len(latencies),
                "percentile_method": "linear_interpolation_at_(n_minus_1)_times_p",
                "p50": round(_linear_percentile(latencies, 0.50), 3),
                "p95": round(_linear_percentile(latencies, 0.95), 3),
                "max": round(max(latencies), 3),
            },
            "batch_wall_clock_ms": {
                "definition": "single monotonic clock around the complete smoke batch",
                "value": int(summary["elapsed_ms"]),
            },
            "validation_exposure_count": report.get("validation_exposure_count"),
            "forbidden_content_field_count": forbidden_content_field_count,
            "suspicious_secret_pattern_count": suspicious_secret_pattern_count,
            "candidate_sql_executed": execution_safety.get("candidate_sql_executed")
            if isinstance(execution_safety, Mapping)
            else None,
        },
        "checks": checks,
        "gate": {
            "status": "pass" if passed else "fail",
            "code": "contract_smoke_gate_pass" if passed else "contract_smoke_gate_fail",
            "scope": "api_and_structured_contract_smoke_stability_only",
            "does_not_prove": [
                "accuracy",
                "generalization",
                "end_to_end_business_capability",
                "innovation",
            ],
        },
        "review_completion": {
            "independent_machine_review": "completed",
            "human_b_signoff": "pending",
            "human_c_signoff": "pending",
            "final_d3_stage_closure": "pending_human_signoff",
        },
    }


def write_report(report: Mapping[str, Any], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="离线复核 D3-05 脱敏报告并生成 D3-06 机器 Gate 报告。"
    )
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE_REPORT)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output", type=Path, default=DEFAULT_GATE_REPORT)
    parser.add_argument(
        "--expected-source-sha256", default=EXPECTED_SOURCE_SHA256
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = review_report(
            args.source,
            manifest_path=args.manifest,
            expected_source_sha256=args.expected_source_sha256,
        )
        write_report(report, args.output)
    except (
        GateReviewError,
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        InvalidOperation,
        KeyError,
        TypeError,
        ValueError,
    ) as exc:
        print(
            json.dumps(
                {"status": "failed", "error_code": type(exc).__name__},
                ensure_ascii=False,
            )
        )
        return 2
    print(
        json.dumps(
            {
                "status": report["gate"]["status"],
                "code": report["gate"]["code"],
                "output": str(args.output.resolve()),
                "api_calls": 0,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if report["gate"]["status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
