"""Build the B-owned D4-07 online SQL metrics report without network access.

The public synthetic scenarios use the real OnlineSqlService, D4-05 recovery,
D4-02 SafetyGate, and local SQLite executor.  Model behavior and timing are
injected deterministically so the report proves orchestration and aggregation,
not model quality or production performance.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

from backend.agent.clarification_recovery import SqlProposal  # noqa: E402
from backend.api.online_sql_service import OnlineSqlService  # noqa: E402
from backend.llm.errors import ProviderError, ProviderErrorCode  # noqa: E402
from backend.nl2sql.evaluation_metrics import (  # noqa: E402
    SqlEvaluationCase,
    summarize_sql_evaluation,
)
from backend.nl2sql.online_proposal import (  # noqa: E402
    MODEL_ID,
    PROVIDER_ID,
    ProposalTelemetry,
)


class D4OnlineSqlMetricsError(RuntimeError):
    """Raised when a public synthetic scenario violates its expected boundary."""


class _SteppedTimer:
    def __init__(self, step_seconds: float) -> None:
        self._step = step_seconds
        self._value = 0.0

    def __call__(self) -> float:
        current = self._value
        self._value += self._step
        return current


class _FixtureProposalPort:
    def __init__(
        self,
        *,
        sql: str,
        input_tokens: int,
        output_tokens: int,
        estimated_cost_cny: float,
        provider_latency_ms: float,
        failure: ProviderError | None = None,
    ) -> None:
        self.sql = sql
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.estimated_cost_cny = estimated_cost_cny
        self.provider_latency_ms = provider_latency_ms
        self.failure = failure
        self.calls = 0
        self.last_telemetry: ProposalTelemetry | None = None

    def propose(self, *, conversation_state, slot_observations) -> SqlProposal:
        del conversation_state, slot_observations
        self.calls += 1
        self.last_telemetry = None
        if self.failure is not None:
            raise self.failure
        self.last_telemetry = ProposalTelemetry(
            request_id=f"request.public-metric-{self.calls}.nl2sql",
            provider_id=PROVIDER_ID,
            returned_model=MODEL_ID,
            attempt_count=1,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            total_tokens=self.input_tokens + self.output_tokens,
            estimated_cost_cny=self.estimated_cost_cny,
            latency_ms=self.provider_latency_ms,
            proposal_sha256=(f"{self.calls:x}" * 64)[:64],
        )
        return SqlProposal(
            proposal_id=f"proposal.public-metric-{self.calls}",
            sql=self.sql,
            tables=("Artist",),
            params=(),
        )


def _query_request(case_id: str) -> dict[str, Any]:
    suffix = case_id.lower()
    return {
        "schema_version": "2.0.0",
        "operation": "submit_query",
        "session_id": f"session.{suffix}",
        "request_id": f"request.{suffix}",
        "turn_id": 0,
        "state_revision": 0,
        "query": "统计艺术家数量",
    }


def _answer_request(
    response: dict[str, Any], *, value: str, answer_id: str
) -> dict[str, Any]:
    pending = response["pending_question"]
    option = next(item for item in pending["options"] if item["value"] == value)
    return {
        "schema_version": "2.0.0",
        "operation": "submit_clarification",
        "answer_id": answer_id,
        "session_id": pending["session_id"],
        "request_id": pending["request_id"],
        "turn_id": pending["turn_id"],
        "state_revision": pending["state_revision"],
        "question_id": pending["question_id"],
        "slot_id": pending["slot_id"],
        "values": [option["value"]],
        "evidence_refs": [option["evidence_ref"]],
    }


def _run_case(
    *,
    case_id: str,
    sql: str,
    timer_step_ms: float,
    input_tokens: int = 0,
    output_tokens: int = 0,
    estimated_cost_cny: float = 0.0,
    provider_latency_ms: float = 0.0,
    failure: ProviderError | None = None,
) -> tuple[SqlEvaluationCase, dict[str, Any]]:
    port = _FixtureProposalPort(
        sql=sql,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        estimated_cost_cny=estimated_cost_cny,
        provider_latency_ms=provider_latency_ms,
        failure=failure,
    )
    service = OnlineSqlService(
        proposal_port=port,
        table_name="Artist",
        now=lambda: "2026-10-03T08:00:00Z",
        timer=_SteppedTimer(timer_step_ms / 1000.0),
    )
    first = service.submit(_query_request(case_id))
    second = service.submit(
        _answer_request(
            first,
            value="count",
            answer_id=f"answer.{case_id.lower()}.1",
        )
    )
    final = service.submit(
        _answer_request(
            second,
            value="artist",
            answer_id=f"answer.{case_id.lower()}.2",
        )
    )
    sql_payload = final.get("sql") or {}
    usage = final["usage"]
    error = final.get("error")
    safety_status = str(sql_payload.get("safety_status", "not_applicable"))
    attempts = int(sql_payload.get("execution_attempts", 0))
    result = SqlEvaluationCase(
        case_id=case_id,
        outcome=str(final["status"]),
        error_code=None if error is None else str(error["code"]),
        retryable=False if error is None else bool(error["retryable"]),
        safety_status=safety_status,
        execution_attempts=attempts,
        execution_successes=(
            1 if final["status"] == "success" and attempts == 1 else 0
        ),
        model_calls=int(usage["model_calls"]),
        input_tokens=int(usage["input_tokens"]),
        output_tokens=int(usage["output_tokens"]),
        estimated_cost_cny=float(usage["estimated_cost_cny"]),
        service_elapsed_ms=float(usage["elapsed_ms"]),
        provider_latency_ms=(
            None
            if port.last_telemetry is None
            else float(port.last_telemetry.latency_ms)
        ),
    )
    return result, deepcopy(final)


def validate_all() -> dict[str, Any]:
    definitions = (
        {
            "case_id": "D4-07-B-METRIC-001",
            "sql": "SELECT COUNT(*) AS artist_count FROM Artist",
            "timer_step_ms": 1.0,
            "input_tokens": 100,
            "output_tokens": 20,
            "estimated_cost_cny": 0.0001,
            "provider_latency_ms": 12.5,
            "expected": ("success", None, "approved", 1),
        },
        {
            "case_id": "D4-07-B-METRIC-002",
            "sql": "DELETE FROM Artist",
            "timer_step_ms": 2.0,
            "input_tokens": 110,
            "output_tokens": 25,
            "estimated_cost_cny": 0.0002,
            "provider_latency_ms": 17.5,
            "expected": ("refused", None, "rejected", 0),
        },
        {
            "case_id": "D4-07-B-METRIC-003",
            "sql": "SELECT COUNT(*) FROM Artist",
            "timer_step_ms": 3.0,
            "failure": ProviderError(
                ProviderErrorCode.TIMEOUT,
                "TESTONLY provider detail must not escape",
                retryable=True,
            ),
            "expected": ("error", "provider_timeout", "not_applicable", 0),
        },
        {
            "case_id": "D4-07-B-METRIC-004",
            "sql": "SELECT COUNT(*) FROM Artist",
            "timer_step_ms": 4.0,
            "failure": ProviderError(
                ProviderErrorCode.RATE_LIMIT,
                "TESTONLY provider detail must not escape",
                retryable=True,
            ),
            "expected": (
                "error",
                "provider_rate_limited",
                "not_applicable",
                0,
            ),
        },
        {
            "case_id": "D4-07-B-METRIC-005",
            "sql": "SELECT COUNT(*) FROM Artist",
            "timer_step_ms": 5.0,
            "failure": ProviderError(
                ProviderErrorCode.INVALID_RESPONSE,
                "TESTONLY provider detail must not escape",
                retryable=False,
            ),
            "expected": (
                "error",
                "model_response_invalid",
                "not_applicable",
                0,
            ),
        },
    )
    cases: list[SqlEvaluationCase] = []
    for definition in definitions:
        parameters = {key: value for key, value in definition.items() if key != "expected"}
        result, response = _run_case(**parameters)
        expected = definition["expected"]
        actual = (
            result.outcome,
            result.error_code,
            result.safety_status,
            result.execution_attempts,
        )
        if actual != expected:
            raise D4OnlineSqlMetricsError(
                f"scenario_expectation_failed:{result.case_id}:{actual!r}"
            )
        serialized = json.dumps(response, ensure_ascii=False)
        if "TESTONLY" in serialized:
            raise D4OnlineSqlMetricsError(
                f"provider_content_leaked:{result.case_id}"
            )
        cases.append(result)

    metrics = summarize_sql_evaluation(cases)
    safety = metrics["safety"]
    if not isinstance(safety, dict) or safety["rejected_sql_execution_attempts"] != 0:
        raise D4OnlineSqlMetricsError("rejected_sql_execution_detected")
    report = {
        "status": "pass",
        "report_type": "d4_online_sql_metrics_offline_validation",
        "task_id": "D4-07-01B",
        "owner": "B",
        "scope": "public_synthetic_online_sql_orchestration_and_metrics",
        "metrics": metrics,
        "input_isolation": {
            "fixture_split": "public_synthetic_contract",
            "prompt_dev_cases_read": 0,
            "validation_cases_read": 0,
            "hidden_test_cases_read": 0,
            "answer_derived_fields_used_for_input": False,
        },
        "content_safety": {
            "query_text_stored_in_report": False,
            "prompt_text_stored_in_report": False,
            "candidate_sql_stored_in_report": False,
            "sql_params_stored_in_report": False,
            "result_rows_stored_in_report": False,
            "model_raw_output_stored_in_report": False,
            "provider_error_detail_stored_in_report": False,
        },
        "model_boundary": {
            "provider_id": PROVIDER_ID,
            "model": MODEL_ID,
            "injected_proposal_port": True,
            "second_provider_enabled": False,
            "fallback_enabled": False,
        },
        "boundaries": {
            "network_calls": 0,
            "model_api_calls": 0,
            "credential_files_read": 0,
            "actual_token_cost_cny": 0.0,
        },
        "evidence_classification": {
            "offline_orchestration_evidence": True,
            "metrics_aggregation_evidence": True,
            "production_performance_evidence": False,
            "online_model_quality_evidence": False,
            "business_accuracy_evidence": False,
            "generalization_evidence": False,
        },
        "stable_sha256": metrics["stable_sha256"],
    }
    rendered = json.dumps(report, ensure_ascii=False, sort_keys=True)
    forbidden = ("TESTONLY", "candidate_sql\"", "\"query\":", "\"rows\":")
    if any(marker in rendered for marker in forbidden):
        raise D4OnlineSqlMetricsError("report_content_boundary_violated")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="离线生成 D4-07 B 侧在线 SQL 脱敏指标报告。"
    )
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    report = validate_all()
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered, encoding="utf-8", newline="\n")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
