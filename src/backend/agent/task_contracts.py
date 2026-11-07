"""Deterministic payload construction and task-result validation for D3 contracts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
import re
from typing import Any


TASK_TYPES = {"intent", "clarification"}
ROOT_FIELDS = {"schema_version", "task_type", "status", "data", "error", "warnings"}
ERROR_FIELDS = {"code", "message", "retryable", "field_errors"}
FIELD_ERROR_FIELDS = {"path", "code"}
ERROR_CODES = {
    "INVALID_INPUT",
    "INSUFFICIENT_CONTEXT",
    "UNSAFE_REQUEST",
    "SCHEMA_VALIDATION_FAILED",
    "POLICY_LIMIT",
    "INTERNAL_ERROR",
}
INTENTS = {"sql", "rag", "cross_source", "unknown"}
INTENT_REASONS = {
    "routable",
    "ambiguous",
    "outside_available_sources",
    "unsupported_task",
}
CLARIFICATION_REASONS = {
    "missing_required_slot",
    "ambiguous_reference",
    "complete",
}
IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")
SOURCE_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_-]*$")
ERROR_IDENTIFIER = re.compile(r"^[A-Z][A-Z0-9_]*$")


class ContractValidationError(ValueError):
    """Raised when a D3 payload or structured result violates its contract."""


def _require_mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractValidationError(f"{name} must be an object")
    return value


def _require_non_empty_string(value: object, name: str, *, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise ContractValidationError(f"{name} must be a non-empty bounded string")
    return value


def _require_string_list(
    value: object,
    name: str,
    *,
    pattern: re.Pattern[str],
    max_items: int,
) -> list[str]:
    if not isinstance(value, list) or len(value) > max_items:
        raise ContractValidationError(f"{name} must be a bounded array")
    if any(not isinstance(item, str) or not pattern.fullmatch(item) for item in value):
        raise ContractValidationError(f"{name} contains an invalid identifier")
    if len(value) != len(set(value)):
        raise ContractValidationError(f"{name} must contain unique values")
    return value


def _require_exact_fields(value: Mapping[str, Any], fields: set[str], name: str) -> None:
    actual = set(value)
    if actual != fields:
        missing = sorted(fields - actual)
        extra = sorted(actual - fields)
        raise ContractValidationError(f"{name} fields mismatch: missing={missing}, extra={extra}")


def _slot_has_value(value: object) -> bool:
    return value is not None and value != "" and value != [] and value != {}


def build_task_payload(
    case: Mapping[str, Any],
    *,
    payload_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a model payload from approved case fields without reading ``expected``.

    ``payload_context`` may provide reviewed, non-answer context for a smoke candidate.
    Only task-specific allowlisted fields are copied into the returned object.
    """

    task_type = case.get("task_type")
    if task_type not in TASK_TYPES:
        raise ContractValidationError("unsupported task_type")
    input_data = _require_mapping(case.get("input"), "input")
    base_context = _require_mapping(case.get("context"), "context")
    context = dict(base_context)
    if payload_context is not None:
        context.update(_require_mapping(payload_context, "payload_context"))

    query = _require_non_empty_string(input_data.get("query"), "query", max_length=2000)
    if task_type == "intent":
        conversation = input_data.get("conversation")
        if not isinstance(conversation, list):
            raise ContractValidationError("conversation must be an array")
        available_sources = _require_string_list(
            context.get("available_sources"),
            "available_sources",
            pattern=SOURCE_IDENTIFIER,
            max_items=32,
        )
        if not available_sources:
            raise ContractValidationError("available_sources cannot be empty")
        return {
            "query": query,
            "conversation": deepcopy(conversation),
            "available_sources": list(available_sources),
        }

    known_slots = _require_mapping(input_data.get("known_slots"), "known_slots")
    if any(not isinstance(key, str) or not IDENTIFIER.fullmatch(key) for key in known_slots):
        raise ContractValidationError("known_slots contains an invalid identifier")
    required_slots = _require_string_list(
        context.get("required_slots"),
        "required_slots",
        pattern=IDENTIFIER,
        max_items=32,
    )
    if not required_slots:
        raise ContractValidationError("required_slots cannot be empty")
    return {
        "query": query,
        "known_slots": deepcopy(dict(known_slots)),
        "required_slots": list(required_slots),
    }


def _validate_warnings(warnings: object) -> None:
    if not isinstance(warnings, list):
        raise ContractValidationError("warnings must be an array")
    if any(
        not isinstance(item, str) or not item or len(item) > 200 for item in warnings
    ):
        raise ContractValidationError("warnings contains an invalid item")
    if len(warnings) != len(set(warnings)):
        raise ContractValidationError("warnings must contain unique values")


def _validate_error(error: object) -> None:
    value = _require_mapping(error, "error")
    _require_exact_fields(value, ERROR_FIELDS, "error")
    if value["code"] not in ERROR_CODES:
        raise ContractValidationError("error.code is invalid")
    _require_non_empty_string(value["message"], "error.message", max_length=300)
    if not isinstance(value["retryable"], bool):
        raise ContractValidationError("error.retryable must be boolean")
    field_errors = value["field_errors"]
    if not isinstance(field_errors, list):
        raise ContractValidationError("error.field_errors must be an array")
    for index, item in enumerate(field_errors):
        field_error = _require_mapping(item, f"error.field_errors[{index}]")
        _require_exact_fields(field_error, FIELD_ERROR_FIELDS, "field_error")
        _require_non_empty_string(field_error["path"], "field_error.path", max_length=200)
        code = field_error["code"]
        if not isinstance(code, str) or not ERROR_IDENTIFIER.fullmatch(code):
            raise ContractValidationError("field_error.code is invalid")


def _validate_intent_data(data: object, payload: Mapping[str, Any]) -> None:
    value = _require_mapping(data, "data")
    fields = {
        "intent",
        "needs_clarification",
        "required_sources",
        "missing_slots",
        "reason",
        "normalized_query",
    }
    _require_exact_fields(value, fields, "intent data")
    intent = value["intent"]
    if intent not in INTENTS:
        raise ContractValidationError("data.intent is invalid")
    needs_clarification = value["needs_clarification"]
    if not isinstance(needs_clarification, bool):
        raise ContractValidationError("data.needs_clarification must be boolean")
    required_sources = _require_string_list(
        value["required_sources"],
        "data.required_sources",
        pattern=SOURCE_IDENTIFIER,
        max_items=4,
    )
    available_sources = set(
        _require_string_list(
            payload.get("available_sources"),
            "payload.available_sources",
            pattern=SOURCE_IDENTIFIER,
            max_items=32,
        )
    )
    if not set(required_sources).issubset(available_sources):
        raise ContractValidationError("required_sources must be a subset of available_sources")
    missing_slots = _require_string_list(
        value["missing_slots"],
        "data.missing_slots",
        pattern=IDENTIFIER,
        max_items=8,
    )
    reason = value["reason"]
    if reason not in INTENT_REASONS:
        raise ContractValidationError("data.reason is invalid")
    normalized_query = value["normalized_query"]
    if normalized_query is not None:
        _require_non_empty_string(normalized_query, "data.normalized_query", max_length=500)

    if needs_clarification:
        if not missing_slots or reason != "ambiguous":
            raise ContractValidationError("clarification requires missing slots and ambiguous reason")
    elif missing_slots:
        raise ContractValidationError("missing_slots must be empty when clarification is false")

    if intent in {"sql", "rag"} and not required_sources:
        raise ContractValidationError("sql/rag intent requires at least one source")
    if intent == "cross_source" and len(required_sources) < 2:
        raise ContractValidationError("cross_source intent requires at least two sources")
    if intent == "unknown" and required_sources:
        raise ContractValidationError("unknown intent cannot require a source")
    if not needs_clarification and intent != "unknown" and reason != "routable":
        raise ContractValidationError("known intent without clarification must be routable")
    if not needs_clarification and intent == "unknown" and reason not in {
        "outside_available_sources",
        "unsupported_task",
    }:
        raise ContractValidationError("unknown intent requires an unknown-route reason")


def _validate_clarification_data(data: object, payload: Mapping[str, Any]) -> None:
    value = _require_mapping(data, "data")
    fields = {"should_clarify", "primary_slot", "missing_slots", "question", "reason"}
    _require_exact_fields(value, fields, "clarification data")
    should_clarify = value["should_clarify"]
    if not isinstance(should_clarify, bool):
        raise ContractValidationError("data.should_clarify must be boolean")
    missing_slots = _require_string_list(
        value["missing_slots"],
        "data.missing_slots",
        pattern=IDENTIFIER,
        max_items=8,
    )
    required_slots = set(
        _require_string_list(
            payload.get("required_slots"),
            "payload.required_slots",
            pattern=IDENTIFIER,
            max_items=32,
        )
    )
    known_slots = _require_mapping(payload.get("known_slots"), "payload.known_slots")
    deterministic_missing = {
        slot for slot in required_slots if slot not in known_slots or not _slot_has_value(known_slots[slot])
    }
    reason = value["reason"]
    if reason not in CLARIFICATION_REASONS:
        raise ContractValidationError("data.reason is invalid")

    if should_clarify:
        primary_slot = value["primary_slot"]
        if not isinstance(primary_slot, str) or primary_slot not in missing_slots:
            raise ContractValidationError("primary_slot must be one of missing_slots")
        if not missing_slots or not set(missing_slots).issubset(required_slots):
            raise ContractValidationError("missing_slots must be a non-empty subset of required_slots")
        _require_non_empty_string(value["question"], "data.question", max_length=200)
        if reason == "complete":
            raise ContractValidationError("clarification cannot use complete reason")
        if deterministic_missing:
            if set(missing_slots) != deterministic_missing or reason != "missing_required_slot":
                raise ContractValidationError("reported missing slots must match required input slots")
    else:
        if value["primary_slot"] is not None or missing_slots or value["question"] is not None:
            raise ContractValidationError("complete input cannot contain clarification fields")
        if reason != "complete":
            raise ContractValidationError("complete input must use complete reason")
        if deterministic_missing:
            raise ContractValidationError("required missing slots cannot skip clarification")


def validate_task_result(
    task_type: str,
    payload: Mapping[str, Any],
    result: Mapping[str, Any],
) -> None:
    """Validate a parsed model result against the D3 envelope and input semantics."""

    if task_type not in TASK_TYPES:
        raise ContractValidationError("unsupported task_type")
    value = _require_mapping(result, "result")
    _require_exact_fields(value, ROOT_FIELDS, "result")
    if value["schema_version"] != "1.0.0":
        raise ContractValidationError("schema_version must be 1.0.0")
    if value["task_type"] != task_type:
        raise ContractValidationError("task_type does not match requested task")
    _validate_warnings(value["warnings"])

    status = value["status"]
    if status == "error":
        if value["data"] is not None:
            raise ContractValidationError("error result data must be null")
        _validate_error(value["error"])
        return
    if status != "success":
        raise ContractValidationError("status must be success or error")
    if value["error"] is not None:
        raise ContractValidationError("success result error must be null")

    if task_type == "intent":
        _validate_intent_data(value["data"], payload)
    else:
        _validate_clarification_data(value["data"], payload)


__all__ = ["ContractValidationError", "build_task_payload", "validate_task_result"]
