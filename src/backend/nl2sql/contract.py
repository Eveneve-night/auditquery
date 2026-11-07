"""Versioned D3-02 NL2SQL prompt, schema, and result contract."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

NL2SQL_PROMPT_VERSION = "nl2sql.v1"
NL2SQL_OUTPUT_SCHEMA_ID = "nl2sql-result.v1"
SCHEMA_VALIDATION_FAILED = "SCHEMA_VALIDATION_FAILED"

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_CONTRACT_DIR = _PROJECT_ROOT / "configs" / "prompts" / "nl2sql" / "v1"
_SCHEMA_PATH = _PROJECT_ROOT / "configs" / "schemas" / "tasks" / "nl2sql-result.v1.schema.json"
_PROMPT_CONTRACT_SCHEMA_PATH = _PROJECT_ROOT / "configs" / "schemas" / "prompt-contract.v1.schema.json"


class NL2SQLSchemaValidationError(ValueError):
    """Bounded, stable error raised before domain objects are constructed."""

    code = SCHEMA_VALIDATION_FAILED

    def __init__(self, path: str = "$") -> None:
        self.path = path
        super().__init__(f"{self.code}: invalid structured result at {path}")


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_contract() -> dict[str, Any]:
    return _load_json(_CONTRACT_DIR / "contract.json")


def load_prompt() -> str:
    return (_CONTRACT_DIR / "system.md").read_text(encoding="utf-8")


def load_output_schema() -> dict[str, Any]:
    return _load_json(_SCHEMA_PATH)


def load_smoke_candidates() -> dict[str, Any]:
    return _load_json(_CONTRACT_DIR / "smoke-candidates.json")


def _resolve_ref(root: Mapping[str, Any], reference: str) -> Mapping[str, Any]:
    if not reference.startswith("#/"):
        raise NL2SQLSchemaValidationError("$")
    node: Any = root
    for part in reference[2:].split("/"):
        node = node[part.replace("~1", "/").replace("~0", "~")]
    return node


def _matches_type(value: Any, expected: str) -> bool:
    return {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }[expected]


def _validate_schema(value: Any, schema: Mapping[str, Any], root: Mapping[str, Any], path: str) -> None:
    if "$ref" in schema:
        _validate_schema(value, _resolve_ref(root, schema["$ref"]), root, path)
        return
    if "const" in schema and value != schema["const"]:
        raise NL2SQLSchemaValidationError(path)
    if "enum" in schema and value not in schema["enum"]:
        raise NL2SQLSchemaValidationError(path)

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise NL2SQLSchemaValidationError(path)
        if "maximum" in schema and value > schema["maximum"]:
            raise NL2SQLSchemaValidationError(path)

    schema_type = schema.get("type")
    if schema_type is not None:
        allowed = [schema_type] if isinstance(schema_type, str) else schema_type
        if not any(_matches_type(value, item) for item in allowed):
            raise NL2SQLSchemaValidationError(path)

    if "oneOf" in schema:
        matches = 0
        for option in schema["oneOf"]:
            try:
                _validate_schema(value, option, root, path)
                matches += 1
            except NL2SQLSchemaValidationError:
                pass
        if matches != 1:
            raise NL2SQLSchemaValidationError(path)

    if isinstance(value, dict):
        for name in schema.get("required", []):
            if name not in value:
                raise NL2SQLSchemaValidationError(f"{path}.{name}")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            unknown = set(value) - set(properties)
            if unknown:
                raise NL2SQLSchemaValidationError(f"{path}.{sorted(unknown)[0]}")
        for name, child_schema in properties.items():
            if name in value:
                _validate_schema(value[name], child_schema, root, f"{path}.{name}")

    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", len(value)):
            raise NL2SQLSchemaValidationError(path)
        if schema.get("uniqueItems") and len({json.dumps(x, sort_keys=True) for x in value}) != len(value):
            raise NL2SQLSchemaValidationError(path)
        if "items" in schema:
            for index, item in enumerate(value):
                _validate_schema(item, schema["items"], root, f"{path}[{index}]")

    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", len(value)):
            raise NL2SQLSchemaValidationError(path)
        if "pattern" in schema and re.fullmatch(schema["pattern"], value) is None:
            raise NL2SQLSchemaValidationError(path)

    for rule in schema.get("allOf", []):
        applies = True
        if "if" in rule:
            try:
                _validate_schema(value, rule["if"], root, path)
            except NL2SQLSchemaValidationError:
                applies = False
        if applies and "then" in rule:
            _validate_schema(value, rule["then"], root, path)


def validate_contract() -> None:
    schema = _load_json(_PROMPT_CONTRACT_SCHEMA_PATH)
    _validate_schema(load_contract(), schema, schema, "$")


@dataclass(frozen=True)
class NL2SQLRequest:
    request_id: str
    user_query: str
    database_schema: Mapping[str, Any]
    dialect: str = "sqlite"
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.request_id, str) or not self.request_id.strip():
            raise ValueError("request_id is required")
        if not isinstance(self.user_query, str) or not self.user_query.strip():
            raise ValueError("user_query is required")
        if not isinstance(self.database_schema, Mapping) or not self.database_schema:
            raise ValueError("database_schema is required")
        if self.dialect != "sqlite":
            raise ValueError("only sqlite is supported by nl2sql.v1")
        if not isinstance(self.metadata, Mapping) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in self.metadata.items()
        ):
            raise ValueError("metadata must contain string keys and values")
        object.__setattr__(self, "metadata", dict(self.metadata))


@dataclass(frozen=True)
class NL2SQLResult:
    candidate_sql: str
    tables: tuple[str, ...]
    params: tuple[Any, ...]
    rationale: str
    rejected: bool
    reject_code: str | None
    warnings: tuple[str, ...]
    safety_status: str = "not_validated"
    executable: bool = False


def validate_model_output(raw: Mapping[str, Any]) -> NL2SQLResult:
    """Validate structure only; never approve or execute the candidate SQL."""

    schema = load_output_schema()
    _validate_schema(raw, schema, schema, "$")
    warnings = tuple(raw["warnings"])
    if raw["status"] == "error":
        return NL2SQLResult("", (), (), "", True, raw["error"]["code"], warnings)
    data = raw["data"]
    return NL2SQLResult(
        data["sql"], tuple(data["tables"]), tuple(data["params"]), data["rationale"],
        False, None, warnings,
    )


def candidate_ids(records: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    """Return stable candidate IDs after confirming their declared split."""

    manifest = load_smoke_candidates()
    ids = tuple(manifest["candidate_ids"])
    by_id = {record["id"]: record for record in records}
    for item_id in ids:
        if item_id not in by_id or by_id[item_id].get("split") != manifest["split"]:
            raise ValueError(f"invalid smoke candidate: {item_id}")
    return ids
