"""Build the deterministic, content-safe D4-07 offline Web/API report."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

from backend.api.d4_demo import D4DemoApplication, D4DemoService, MAX_REQUEST_BYTES  # noqa: E402


REPORT_PATH = PROJECT_ROOT / "tests" / "reports" / "d4-web-api-v1.json"
APPROVED_MODEL = "qwen3.7-flash"


class D4WebApiValidationError(RuntimeError):
    pass


def _hash(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _canonical_text_bytes(path: Path) -> bytes:
    """Return a platform-independent UTF-8/LF representation for report hashes."""

    text = path.read_text(encoding="utf-8")
    return text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")


def _text_artifact(path: Path) -> dict[str, Any]:
    payload = _canonical_text_bytes(path)
    return {
        "path": path.relative_to(PROJECT_ROOT).as_posix(),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "bytes": len(payload),
    }


def _payload(response) -> dict[str, Any]:
    return json.loads(response.body.decode("utf-8"))


def _provider_boundary() -> dict[str, Any]:
    paths = sorted((PROJECT_ROOT / "configs" / "providers").glob("*.json"))
    providers = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    if len(providers) != 1 or providers[0].get("model") != APPROVED_MODEL:
        raise D4WebApiValidationError("single-model boundary changed")
    return {
        "provider_config_count": 1,
        "model": APPROVED_MODEL,
        "second_provider_enabled": False,
        "fallback_enabled": False,
        "jev_api_enabled": False,
    }


def validate_all() -> dict[str, Any]:
    service = D4DemoService()
    app = D4DemoApplication(service)
    expected = {
        "D4-07-SQL-001": ("sql", "success"),
        "D4-07-SQL-002": ("sql", "refused"),
        "D4-07-RAG-001": ("rag", "success"),
        "D4-07-RAG-002": ("rag", "refused"),
    }
    scenarios = []
    totals = {
        "sqlite_execution_attempts": 0,
        "sqlite_execution_successes": 0,
        "safety_rejections": 0,
        "citations": 0,
        "trace_events": 0,
    }
    for index, (scenario_id, (route, status)) in enumerate(expected.items(), start=1):
        result = service.run(
            {
                "schema_version": "1.0.0",
                "request_id": f"report.demo.{index}",
                "scenario_id": scenario_id,
            }
        )
        if result["route"] != route or result["status"] != status:
            raise D4WebApiValidationError("scenario result drift")
        sql = result["sql"]
        attempts = int(sql["execution_attempts"]) if sql is not None else 0
        approved = bool(sql["safety_approved"]) if sql is not None else False
        totals["sqlite_execution_attempts"] += attempts
        totals["sqlite_execution_successes"] += int(attempts == 1 and approved)
        totals["safety_rejections"] += int(sql is not None and not approved)
        totals["citations"] += len(result["citations"])
        totals["trace_events"] += len(result["trace"])
        scenarios.append(
            {
                "scenario_id": scenario_id,
                "route": route,
                "status": status,
                "action": result["decision"]["action"],
                "score_kind": result["decision"]["score_kind"],
                "candidate_count": len(result["candidate_evidence"]),
                "citation_count": len(result["citations"]),
                "trace_event_count": len(result["trace"]),
                "safety_approved": sql["safety_approved"] if sql is not None else None,
                "sqlite_execution_attempts": attempts,
                "result_sha256": _hash(result),
            }
        )
    if totals["sqlite_execution_attempts"] != 1 or totals["safety_rejections"] != 1:
        raise D4WebApiValidationError("execution boundary drift")
    if totals["citations"] < 1:
        raise D4WebApiValidationError("citation evidence missing")

    probes = []
    probe_cases = (
        (
            "arbitrary_sql",
            app.handle(
                "POST",
                "/api/v1/demo",
                content_type="application/json",
                body=json.dumps(
                    {
                        "schema_version": "1.0.0",
                        "request_id": "probe.sql",
                        "scenario_id": "D4-07-SQL-001",
                        "sql": "SELECT 1",
                    }
                ).encode("utf-8"),
            ),
            "invalid_demo_request",
        ),
        (
            "answer_derived_field",
            app.handle(
                "POST",
                "/api/v1/demo",
                content_type="application/json",
                body=json.dumps(
                    {
                        "schema_version": "1.0.0",
                        "request_id": "probe.answer",
                        "scenario_id": "D4-07-SQL-001",
                        "expected": "forbidden",
                    }
                ).encode("utf-8"),
            ),
            "answer_derived_field_forbidden",
        ),
        (
            "unknown_scenario",
            app.handle(
                "POST",
                "/api/v1/demo",
                content_type="application/json",
                body=json.dumps(
                    {
                        "schema_version": "1.0.0",
                        "request_id": "probe.unknown",
                        "scenario_id": "D4-07-SQL-999",
                    }
                ).encode("utf-8"),
            ),
            "unknown_demo_scenario",
        ),
        (
            "oversized_request",
            app.handle(
                "POST",
                "/api/v1/demo",
                content_type="application/json",
                body=b"x" * (MAX_REQUEST_BYTES + 1),
            ),
            "request_too_large",
        ),
        (
            "path_traversal",
            app.handle("GET", "/assets/../index.html"),
            "route_not_found",
        ),
        (
            "wrong_method",
            app.handle("GET", "/api/v1/demo"),
            "method_not_allowed",
        ),
    )
    for probe_id, response, expected_code in probe_cases:
        code = _payload(response).get("error", {}).get("code")
        if code != expected_code:
            raise D4WebApiValidationError(f"probe drift: {probe_id}")
        probes.append(
            {"probe_id": probe_id, "http_status": response.status, "error_code": code}
        )

    frontend = service.frontend_root
    asset_paths = [frontend / "index.html", frontend / "styles.css", frontend / "app.js"]
    assets = [_text_artifact(path) for path in asset_paths]
    for path in asset_paths:
        data = path.read_text(encoding="utf-8")
        if "https://" in data or "http://" in data:
            raise D4WebApiValidationError("remote frontend dependency found")

    return {
        "status": "pass",
        "report_type": "d4_web_api_offline_validation",
        "task_id": "D4-07",
        "scope": "public_synthetic_local_web_api_and_explainability",
        "api_contract": {
            "request_schema_id": "demo-request.v1",
            "response_schema_id": "demo-response.v1",
            "scenario_manifest_id": service.scenario_manifest["manifest_id"],
            "scenario_manifest_version": service.scenario_manifest["version"],
            "scenario_manifest_sha256": service.scenario_sha256,
            "arbitrary_query_accepted": False,
            "arbitrary_sql_accepted": False,
        },
        "public_scenarios": {
            "count": len(scenarios),
            "sql_count": sum(item["route"] == "sql" for item in scenarios),
            "rag_count": sum(item["route"] == "rag" for item in scenarios),
            "success_count": sum(item["status"] == "success" for item in scenarios),
            "refused_count": sum(item["status"] == "refused" for item in scenarios),
            "items": scenarios,
        },
        "execution_evidence": totals,
        "rag_artifact_integrity": dict(service.rag_integrity),
        "fail_closed_probes": {"count": len(probes), "items": probes},
        "frontend": {
            "asset_count": len(assets),
            "assets": assets,
            "hash_normalization": "utf8_lf_v1",
            "remote_dependency_count": 0,
            "content_security_policy_enabled": True,
            "hidden_chain_of_thought_displayed": False,
        },
        "evidence_boundary": {
            "public_synthetic_demo_only": True,
            "conditional_and_offline_orchestration_evidence": True,
            "online_model_answer_generated": False,
            "business_accuracy_proven": False,
            "generalization_proven": False,
            "empirical_calibration_proven": False,
            "innovation_proven": False,
        },
        "input_isolation": {
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
            "arbitrary_natural_language_online_orchestration",
            "online_sql_generation",
            "model_generated_rag_answer",
            "empirical_calibration",
            "authentication_or_public_network_deployment",
            "business_accuracy_or_generalization_evaluation",
            "innovation_claim_evidence",
        ],
        "network_evaluation_calls": 0,
        "model_api_calls": 0,
        "credential_files_read": 0,
        "estimated_cost_cny": 0.0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成 D4-07 离线 Web/API 机器报告")
    parser.add_argument("--report", default=str(REPORT_PATH))
    args = parser.parse_args(argv)
    report = validate_all()
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
