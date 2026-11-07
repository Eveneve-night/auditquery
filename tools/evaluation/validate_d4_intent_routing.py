"""Offline evidence generator for D4-01 fixed routing and explicit state.

The tool uses only synthetic requests and parsed results.  It never reads
credentials, calls a model or network, executes SQL, or opens validation/test
case content.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

from jsonschema import Draft202012Validator


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from backend.agent.intent_routing import (  # noqa: E402
    DEFAULT_CATALOG_PATH,
    EXPECTED_SOURCE_IDS,
    IntentRoutingError,
    apply_intent_result,
    begin_turn,
    build_intent_payload,
    create_conversation_state,
    load_source_catalog,
    validate_conversation_state,
)


SCHEMA_ROOT = PROJECT_ROOT / "configs" / "schemas" / "d4-01"
EXPOSURE_PATH = (
    PROJECT_ROOT / "configs" / "evaluation" / "d4" / "validation-exposure.v1.json"
)
APPROVED_MODEL = "qwen3.7-flash"


class D401ValidationError(RuntimeError):
    """Stable D4-01 offline validation failure."""


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise D401ValidationError("JSON_OBJECT_REQUIRED")
    return value


def _sha256_utf8_lf(path: Path) -> str:
    content = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return hashlib.sha256(content).hexdigest()


def _object_sha256(value: Any) -> str:
    content = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def _result(
    intent: str,
    sources: list[str],
    *,
    needs_clarification: bool = False,
    missing_slots: list[str] | None = None,
    reason: str = "routable",
) -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "task_type": "intent",
        "status": "success",
        "data": {
            "intent": intent,
            "needs_clarification": needs_clarification,
            "required_sources": sources,
            "missing_slots": missing_slots or [],
            "reason": reason,
            "normalized_query": None,
        },
        "error": None,
        "warnings": [],
    }


def _validate_provider_boundary() -> dict[str, Any]:
    paths = sorted((PROJECT_ROOT / "configs" / "providers").glob("*.json"))
    providers = [_read_json(path) for path in paths]
    if len(providers) != 1 or providers[0].get("model") != APPROVED_MODEL:
        raise D401ValidationError("MODEL_BOUNDARY_DRIFT")
    return {
        "provider_config_count": 1,
        "model": APPROVED_MODEL,
        "second_provider_enabled": False,
        "fallback_enabled": False,
        "jev_api_enabled": False,
    }


def validate_all() -> dict[str, Any]:
    schema_paths = sorted(SCHEMA_ROOT.glob("*.json"))
    if len(schema_paths) != 2:
        raise D401ValidationError("D4_01_SCHEMA_SET_DRIFT")
    for path in schema_paths:
        Draft202012Validator.check_schema(_read_json(path))

    catalog = load_source_catalog()
    catalog_ids = [source["source_id"] for source in catalog["sources"]]
    if tuple(catalog_ids) != EXPECTED_SOURCE_IDS:
        raise D401ValidationError("SOURCE_CATALOG_SET_DRIFT")

    exposure = _read_json(EXPOSURE_PATH)
    if (
        exposure.get("historical_exposure_count") != 0
        or exposure.get("current_task_exposure_count") != 0
        or exposure.get("entries") != []
    ):
        raise D401ValidationError("VALIDATION_EXPOSURE_NOT_ZERO")

    scenarios = [
        ("sql", _result("sql", ["chinook_sql"])),
        ("rag", _result("rag", ["security_policy"])),
        (
            "cross",
            _result("cross_source", ["chinook_sql", "project_report"]),
        ),
        (
            "unknown",
            _result("unknown", [], reason="outside_available_sources"),
        ),
        (
            "clarify",
            _result(
                "sql",
                ["chinook_sql"],
                needs_clarification=True,
                missing_slots=["time_range"],
                reason="ambiguous",
            ),
        ),
    ]
    payload_catalogs: list[list[str]] = []
    phases: Counter[str] = Counter()
    state_hashes: list[str] = []
    for scenario_id, result in scenarios:
        state = create_conversation_state(
            f"testonly-{scenario_id}",
            f"TESTONLY synthetic request {scenario_id}",
        )
        payload = build_intent_payload(state, catalog)
        payload_catalogs.append(payload["available_sources"])
        updated = apply_intent_result(state, catalog, result)
        validate_conversation_state(updated)
        phases[updated["phase"]] += 1
        state_hashes.append(_object_sha256(updated))
        history_text = json.dumps(updated["history"], ensure_ascii=False)
        if "synthetic request" in history_text:
            raise D401ValidationError("HISTORY_CONTENT_LEAK")

    if any(item != catalog_ids for item in payload_catalogs):
        raise D401ValidationError("PER_REQUEST_CATALOG_DRIFT")

    first = create_conversation_state("testonly-multiturn", "TESTONLY first")
    routed = apply_intent_result(first, catalog, _result("sql", ["chinook_sql"]))
    next_turn = begin_turn(routed, catalog, "TESTONLY second")
    if next_turn["phase"] != "awaiting_intent" or len(next_turn["history"]) != 1:
        raise D401ValidationError("MULTITURN_STATE_FAILURE")

    return {
        "status": "pass",
        "report_type": "d4_intent_routing_offline_validation",
        "task_id": "D4-01",
        "scope": "fixed_catalog_and_explicit_state_contract_only",
        "schemas": {
            "count": 2,
            "source_catalog": {
                "id": "source-catalog.v1",
                "sha256": _sha256_utf8_lf(
                    SCHEMA_ROOT / "source-catalog.v1.schema.json"
                ),
            },
            "conversation_state": {
                "id": "conversation-state.v1",
                "sha256": _sha256_utf8_lf(
                    SCHEMA_ROOT / "conversation-state.v1.schema.json"
                ),
            },
        },
        "source_catalog": {
            "catalog_id": catalog["catalog_id"],
            "version": catalog["version"],
            "sha256": _sha256_utf8_lf(DEFAULT_CATALOG_PATH),
            "source_count": len(catalog_ids),
            "route_kind_counts": dict(
                sorted(Counter(item["route_kind"] for item in catalog["sources"]).items())
            ),
            "same_catalog_for_every_request": True,
            "resource_hashes_verified": len(catalog_ids),
        },
        "state_transitions": {
            "synthetic_scenario_count": len(scenarios),
            "phase_counts": dict(sorted(phases.items())),
            "normalized_state_sha256": state_hashes,
            "multiturn_reset_verified": True,
            "history_contains_request_content": False,
        },
        "input_isolation": {
            "payload_allowlist": ["query", "conversation", "available_sources"],
            "answer_derived_fields_read": False,
            "prompt_dev_cases_read": 0,
            "validation_cases_read": 0,
            "test_cases_read": 0,
            "validation_historical_exposure_count": 0,
            "validation_current_task_exposure_count": 0,
        },
        "model_boundary": _validate_provider_boundary(),
        "not_implemented": [
            "automatic_required_slots",
            "clarification_question_generation",
            "domain_pack_runtime",
            "empirical_calibration",
            "sql_or_rag_execution",
            "business_accuracy_evaluation",
        ],
        "network_calls": 0,
        "model_api_calls": 0,
        "sql_executions": 0,
        "estimated_cost_cny": 0.0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="离线校验 D4-01 固定路由与显式会话状态。")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    try:
        report = validate_all()
    except (
        D401ValidationError,
        IntentRoutingError,
        OSError,
        KeyError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        print(
            json.dumps(
                {"status": "fail", "error_code": type(exc).__name__},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
