"""Fixed-catalog intent routing and explicit conversation state for D4-01.

This module performs no model, network, credential, SQL, or RAG calls.  It
builds the allowlisted intent payload and applies an already parsed
``intent-result.v1`` object through deterministic, fail-closed transitions.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError, ValidationError

from .task_contracts import (
    ContractValidationError,
    build_task_payload,
    validate_task_result,
)


PROJECT_ROOT = Path(__file__).resolve().parents[3]
SCHEMA_ROOT = PROJECT_ROOT / "configs" / "schemas" / "d4-01"
DEFAULT_CATALOG_PATH = (
    PROJECT_ROOT / "configs" / "routing" / "d4-01" / "source-catalog.v1.json"
)
CATALOG_SCHEMA_PATH = SCHEMA_ROOT / "source-catalog.v1.schema.json"
STATE_SCHEMA_PATH = SCHEMA_ROOT / "conversation-state.v1.schema.json"
EXPECTED_SOURCE_IDS = (
    "chinook_sql",
    "expense_policy",
    "project_report",
    "sales_playbook",
    "security_policy",
)
TERMINAL_PHASES = {"routed", "needs_clarification", "rejected"}
FORBIDDEN_ANSWER_KEYS = {"label", "labels", "answer", "answers", "target_answer"}


class IntentRoutingError(RuntimeError):
    """Stable, content-safe D4-01 routing failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise IntentRoutingError("INVALID_JSON_OBJECT")
    return value


def _sha256_utf8_lf(path: Path) -> str:
    content = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return hashlib.sha256(content).hexdigest()


def _validate_schema(instance: Mapping[str, Any], schema_path: Path, code: str) -> None:
    try:
        schema = _read_json(schema_path)
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(instance)
    except (SchemaError, ValidationError, OSError, ValueError, json.JSONDecodeError) as exc:
        raise IntentRoutingError(code) from exc


def _is_forbidden_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_").replace(" ", "_")
    return "expected" in normalized or "gold" in normalized or normalized in FORBIDDEN_ANSWER_KEYS


def _assert_no_answer_fields(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if _is_forbidden_key(str(key)):
                raise IntentRoutingError("ANSWER_DERIVED_FIELD_FORBIDDEN")
            _assert_no_answer_fields(item)
    elif isinstance(value, list):
        for item in value:
            _assert_no_answer_fields(item)


def validate_source_catalog(catalog: Mapping[str, Any]) -> None:
    """Validate one in-memory v1 catalog, including referenced asset hashes."""
    _validate_schema(catalog, CATALOG_SCHEMA_PATH, "INVALID_SOURCE_CATALOG")
    _assert_no_answer_fields(catalog)

    source_ids = [source["source_id"] for source in catalog["sources"]]
    if tuple(source_ids) != EXPECTED_SOURCE_IDS or len(source_ids) != len(set(source_ids)):
        raise IntentRoutingError("SOURCE_CATALOG_SET_DRIFT")
    route_kinds = {source["route_kind"] for source in catalog["sources"]}
    if route_kinds != {"sql", "rag"}:
        raise IntentRoutingError("SOURCE_CATALOG_ROUTE_KIND_DRIFT")

    root = PROJECT_ROOT.resolve()
    seen_paths: set[str] = set()
    for source in catalog["sources"]:
        for reference in source["resource_refs"]:
            relative_path = reference["path"]
            if relative_path in seen_paths:
                raise IntentRoutingError("SOURCE_RESOURCE_DUPLICATE")
            seen_paths.add(relative_path)
            resource_path = (root / relative_path).resolve()
            try:
                resource_path.relative_to(root)
            except ValueError as exc:
                raise IntentRoutingError("SOURCE_RESOURCE_PATH_ESCAPE") from exc
            if not resource_path.is_file():
                raise IntentRoutingError("SOURCE_RESOURCE_MISSING")
            if _sha256_utf8_lf(resource_path) != reference["sha256"]:
                raise IntentRoutingError("SOURCE_RESOURCE_HASH_MISMATCH")


def load_source_catalog(path: Path = DEFAULT_CATALOG_PATH) -> dict[str, Any]:
    """Load and verify the immutable v1 source catalog and referenced assets."""
    try:
        catalog = _read_json(path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise IntentRoutingError("INVALID_SOURCE_CATALOG") from exc
    validate_source_catalog(catalog)
    return deepcopy(catalog)


def source_ids(catalog: Mapping[str, Any]) -> tuple[str, ...]:
    validate_source_catalog(catalog)
    return tuple(source["source_id"] for source in catalog["sources"])


def _empty_route() -> dict[str, Any]:
    return {
        "intent": None,
        "selected_source_ids": [],
        "needs_clarification": False,
        "missing_slots": [],
        "reason": None,
        "normalized_query": None,
    }


def _validated_messages(conversation: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    if isinstance(conversation, (str, bytes)):
        raise IntentRoutingError("INVALID_CONVERSATION")
    messages: list[dict[str, str]] = []
    for item in conversation:
        if not isinstance(item, Mapping) or set(item) != {"role", "content"}:
            raise IntentRoutingError("INVALID_CONVERSATION")
        role = item["role"]
        content = item["content"]
        if role not in {"user", "assistant"} or not isinstance(content, str) or not content:
            raise IntentRoutingError("INVALID_CONVERSATION")
        messages.append({"role": role, "content": content})
    return messages


def validate_conversation_state(state: Mapping[str, Any]) -> None:
    _validate_schema(state, STATE_SCHEMA_PATH, "INVALID_CONVERSATION_STATE")
    revisions = [item["revision"] for item in state["history"]]
    if revisions != sorted(set(revisions)) or any(item > state["revision"] for item in revisions):
        raise IntentRoutingError("INVALID_STATE_HISTORY")
    if state["phase"] in TERMINAL_PHASES:
        if not state["history"] or state["history"][-1]["revision"] != state["revision"]:
            raise IntentRoutingError("INVALID_STATE_HISTORY")
        route = state["route"]
        expected = {
            "revision": state["revision"],
            "phase": state["phase"],
            "intent": route["intent"],
            "selected_source_ids": route["selected_source_ids"],
            "needs_clarification": route["needs_clarification"],
            "missing_slots": route["missing_slots"],
            "reason": route["reason"],
        }
        if state["history"][-1] != expected:
            raise IntentRoutingError("INVALID_STATE_HISTORY")
    elif revisions and revisions[-1] >= state["revision"]:
        raise IntentRoutingError("INVALID_STATE_HISTORY")


def create_conversation_state(
    session_id: str,
    query: str,
    *,
    conversation: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    state = {
        "state_version": "1.0.0",
        "session_id": session_id,
        "revision": 0,
        "phase": "awaiting_intent",
        "current_request": {
            "query": query,
            "conversation": _validated_messages(conversation),
        },
        "route": _empty_route(),
        "history": [],
    }
    validate_conversation_state(state)
    return state


def begin_turn(
    state: Mapping[str, Any],
    catalog: Mapping[str, Any],
    query: str,
    *,
    conversation: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Start a new routing turn without retaining previous query text in history."""
    validate_routing_state(state, catalog)
    if state["phase"] not in TERMINAL_PHASES:
        raise IntentRoutingError("INVALID_STATE_TRANSITION")
    updated = deepcopy(dict(state))
    updated["revision"] += 1
    updated["phase"] = "awaiting_intent"
    updated["current_request"] = {
        "query": query,
        "conversation": _validated_messages(conversation),
    }
    updated["route"] = _empty_route()
    validate_conversation_state(updated)
    return updated


def build_intent_payload(
    state: Mapping[str, Any],
    catalog: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the D3 intent-v1 payload from state and the fixed catalog only."""
    validate_routing_state(state, catalog)
    if state["phase"] != "awaiting_intent":
        raise IntentRoutingError("INVALID_STATE_TRANSITION")
    catalog_ids = source_ids(catalog)
    if catalog_ids != EXPECTED_SOURCE_IDS:
        raise IntentRoutingError("SOURCE_CATALOG_SET_DRIFT")
    case = {
        "task_type": "intent",
        "input": deepcopy(state["current_request"]),
        "context": {"available_sources": list(catalog_ids)},
    }
    try:
        return build_task_payload(case)
    except ContractValidationError as exc:
        raise IntentRoutingError("INVALID_INTENT_PAYLOAD") from exc


def _validate_route_kinds(catalog: Mapping[str, Any], data: Mapping[str, Any]) -> None:
    kinds = {source["source_id"]: source["route_kind"] for source in catalog["sources"]}
    selected = data["required_sources"]
    intent = data["intent"]
    selected_kinds = {kinds[source_id] for source_id in selected}
    if intent == "sql" and selected_kinds != {"sql"}:
        raise IntentRoutingError("ROUTE_KIND_MISMATCH")
    if intent == "rag" and selected_kinds != {"rag"}:
        raise IntentRoutingError("ROUTE_KIND_MISMATCH")
    if intent == "cross_source" and selected_kinds != {"sql", "rag"}:
        raise IntentRoutingError("ROUTE_KIND_MISMATCH")
    if intent == "unknown" and selected:
        raise IntentRoutingError("ROUTE_KIND_MISMATCH")


def validate_routing_state(
    state: Mapping[str, Any],
    catalog: Mapping[str, Any],
) -> None:
    """Validate state structure and every persisted source against the catalog."""
    validate_conversation_state(state)
    catalog_set = set(source_ids(catalog))
    route_records: list[Mapping[str, Any]] = []
    if state["route"]["intent"] is not None:
        route_records.append(state["route"])
    route_records.extend(state["history"])
    for record in route_records:
        selected = record["selected_source_ids"]
        if not set(selected).issubset(catalog_set):
            raise IntentRoutingError("STATE_SOURCE_OUT_OF_CATALOG")
        _validate_route_kinds(
            catalog,
            {"intent": record["intent"], "required_sources": selected},
        )


def apply_intent_result(
    state: Mapping[str, Any],
    catalog: Mapping[str, Any],
    result: Mapping[str, Any],
) -> dict[str, Any]:
    """Apply a validated intent result; the input state remains unchanged on failure."""
    validate_routing_state(state, catalog)
    if state["phase"] != "awaiting_intent":
        raise IntentRoutingError("INVALID_STATE_TRANSITION")
    payload = build_intent_payload(state, catalog)
    try:
        validate_task_result("intent", payload, result)
    except ContractValidationError as exc:
        raise IntentRoutingError("INVALID_INTENT_RESULT") from exc
    if result["status"] == "error":
        raise IntentRoutingError("INTENT_RESULT_ERROR")

    data = result["data"]
    _validate_route_kinds(catalog, data)
    if data["needs_clarification"]:
        if data["intent"] == "unknown":
            raise IntentRoutingError("INVALID_INTENT_RESULT")
        phase = "needs_clarification"
    elif data["intent"] == "unknown":
        phase = "rejected"
    else:
        phase = "routed"

    updated = deepcopy(dict(state))
    updated["revision"] += 1
    updated["phase"] = phase
    updated["route"] = {
        "intent": data["intent"],
        "selected_source_ids": list(data["required_sources"]),
        "needs_clarification": data["needs_clarification"],
        "missing_slots": list(data["missing_slots"]),
        "reason": data["reason"],
        "normalized_query": data["normalized_query"],
    }
    updated["history"].append(
        {
            "revision": updated["revision"],
            "phase": phase,
            "intent": data["intent"],
            "selected_source_ids": list(data["required_sources"]),
            "needs_clarification": data["needs_clarification"],
            "missing_slots": list(data["missing_slots"]),
            "reason": data["reason"],
        }
    )
    validate_routing_state(updated, catalog)
    return updated


__all__ = [
    "DEFAULT_CATALOG_PATH",
    "EXPECTED_SOURCE_IDS",
    "IntentRoutingError",
    "apply_intent_result",
    "begin_turn",
    "build_intent_payload",
    "create_conversation_state",
    "load_source_catalog",
    "source_ids",
    "validate_conversation_state",
    "validate_routing_state",
    "validate_source_catalog",
]
