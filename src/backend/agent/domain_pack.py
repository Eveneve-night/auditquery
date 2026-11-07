"""Strict, offline runtime loading for versioned D4 domain packs.

This module deliberately lives in production code and does not import the
evaluation tools.  A malformed or unauthorized pack always fails closed.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError, ValidationError


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_PACK_ROOT = PROJECT_ROOT / "domain-packs"
DEFAULT_SCHEMA_ROOT = PROJECT_ROOT / "configs" / "schemas" / "d4"
DOMAIN_PACK_MANIFEST_SCHEMA = "domain-pack-manifest.v1.schema.json"
DOMAIN_PACK_CONTENT_SCHEMA = "domain-pack-content.v1.schema.json"
EXPECTED_FILES = {
    "concepts": "concepts.json",
    "aliases": "aliases.json",
    "slots": "slots.json",
    "schema_catalog": "schema-catalog.json",
    "relations": "relations.json",
    "value_evidence": "value-evidence.json",
    "validation_rules": "validation-rules.json",
    "license_notice": "LICENSE-or-SOURCE.md",
}
CONTENT_ROLES = frozenset(EXPECTED_FILES) - {"license_notice"}
DATA_LEVEL_RANK = {"L0": 0, "L1": 1, "L2": 2, "L3": 3}
MODEL_DATA_LEVELS = frozenset({"L0", "L1"})
_ID_FIELDS = {
    "concepts": "concept_id",
    "aliases": "alias_id",
    "slots": "slot_id",
    "schema_catalog": "object_id",
    "relations": "relation_id",
    "value_evidence": "evidence_id",
    "validation_rules": "rule_id",
}


class DomainPackError(RuntimeError):
    """Content-safe domain-pack failure with a stable machine code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def canonical_text_sha256(path: Path) -> str:
    """Return the frozen D4 UTF-8/LF content hash."""

    content = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return hashlib.sha256(content).hexdigest()


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DomainPackError("invalid_json") from exc


def _validate_schema(instance: Any, schema_path: Path) -> None:
    try:
        schema = _read_json(schema_path)
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(
            schema, format_checker=FormatChecker()
        ).validate(instance)
    except (SchemaError, ValidationError) as exc:
        raise DomainPackError("schema_validation_failed") from exc


def _forbidden_key(key: str) -> bool:
    normalized = key.casefold().replace("-", "_").replace(" ", "_")
    return (
        "expected" in normalized
        or "gold" in normalized
        or normalized
        in {
            "label",
            "labels",
            "answer",
            "answers",
            "target_answer",
            "golden_sql",
        }
    )


def _assert_no_answer_fields(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if _forbidden_key(str(key)):
                raise DomainPackError("forbidden_answer_field")
            _assert_no_answer_fields(item)
    elif isinstance(value, list):
        for item in value:
            _assert_no_answer_fields(item)


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _deep_freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_deep_freeze(item) for item in value)
    return value


def _deep_thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _deep_thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_deep_thaw(item) for item in value]
    return value


def _assert_refs(refs: Iterable[str], known: set[str]) -> None:
    if set(refs) - known:
        raise DomainPackError("dangling_reference")


def _assert_acyclic(parents: Mapping[str, Iterable[str]]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visiting:
            raise DomainPackError("inheritance_cycle")
        if node in visited:
            return
        visiting.add(node)
        for parent in parents.get(node, ()):  # references are checked separately
            visit(parent)
        visiting.remove(node)
        visited.add(node)

    for candidate in parents:
        visit(candidate)


def _semantic_version(value: str) -> tuple[int, int, int]:
    try:
        major, minor, patch = value.split(".")
        return int(major), int(minor), int(patch)
    except (AttributeError, TypeError, ValueError) as exc:
        raise DomainPackError("invalid_version") from exc


@dataclass(frozen=True)
class LoadedDomainPack:
    """An immutable, fully validated domain-pack view."""

    pack_path: Path
    manifest: Mapping[str, Any]
    artifacts: Mapping[str, Mapping[str, Any]]
    items_by_id: Mapping[str, Mapping[str, Any]]
    item_roles: Mapping[str, str]
    manifest_sha256: str

    @property
    def domain_pack_id(self) -> str:
        return str(self.manifest["domain_pack_id"])

    @property
    def version(self) -> str:
        return str(self.manifest["version"])

    def artifact_items(self, role: str) -> tuple[Mapping[str, Any], ...]:
        artifact = self.artifacts.get(role)
        if artifact is None:
            raise DomainPackError("unknown_artifact_role")
        return tuple(artifact["items"])

    def resolve(self, reference_id: str) -> Mapping[str, Any]:
        try:
            return self.items_by_id[reference_id]
        except KeyError as exc:
            raise DomainPackError("unknown_reference") from exc

    def resolve_role(self, reference_id: str) -> str:
        try:
            return self.item_roles[reference_id]
        except KeyError as exc:
            raise DomainPackError("unknown_reference") from exc

    def model_context(
        self,
        *,
        max_data_level: str = "L1",
        roles: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        """Return only explicitly model-authorized L0/L1 JSON artifacts.

        L2/L3 are intentionally unsupported even when a caller asks for them;
        broadening that boundary requires a separate reviewed contract version.
        """

        if max_data_level not in MODEL_DATA_LEVELS:
            raise DomainPackError("model_data_level_not_allowed")
        requested = CONTENT_ROLES if roles is None else frozenset(roles)
        if not requested <= CONTENT_ROLES:
            raise DomainPackError("unknown_artifact_role")

        artifacts: dict[str, Any] = {}
        for entry in self.manifest["files"]:
            role = str(entry["role"])
            if role not in requested or not entry["send_to_model"]:
                continue
            if DATA_LEVEL_RANK[str(entry["data_level"])] > DATA_LEVEL_RANK[max_data_level]:
                continue
            artifact = _deep_thaw(self.artifacts[role])
            if role == "value_evidence":
                artifact["items"] = [
                    item
                    for item in artifact["items"]
                    if item["send_to_model"]
                    and item["data_level"] in MODEL_DATA_LEVELS
                    and DATA_LEVEL_RANK[item["data_level"]]
                    <= DATA_LEVEL_RANK[max_data_level]
                ]
            artifacts[role] = artifact
        return {
            "domain_pack_id": self.domain_pack_id,
            "version": self.version,
            "manifest_sha256": self.manifest_sha256,
            "max_data_level": max_data_level,
            "artifacts": artifacts,
        }


class DomainPackRegistry:
    """Discover and strictly load versioned packs beneath one trusted root."""

    def __init__(
        self,
        pack_root: Path | str = DEFAULT_PACK_ROOT,
        schema_root: Path | str = DEFAULT_SCHEMA_ROOT,
    ) -> None:
        self.pack_root = Path(pack_root).resolve()
        self.schema_root = Path(schema_root).resolve()
        self._locations: dict[tuple[str, str], Path] = {}

    def discover(self) -> tuple[tuple[str, str], ...]:
        if not self.pack_root.is_dir():
            raise DomainPackError("pack_root_missing")
        locations: dict[tuple[str, str], Path] = {}
        for manifest_path in sorted(self.pack_root.glob("*/*/manifest.json")):
            relative = manifest_path.relative_to(self.pack_root)
            if relative.parts[0] == "templates":
                continue
            pack_path = manifest_path.parent.resolve()
            try:
                pack_path.relative_to(self.pack_root)
            except ValueError as exc:
                raise DomainPackError("pack_path_escape") from exc
            manifest = _read_json(manifest_path)
            _assert_no_answer_fields(manifest)
            _validate_schema(
                manifest, self.schema_root / DOMAIN_PACK_MANIFEST_SCHEMA
            )
            key = (str(manifest["domain_pack_id"]), str(manifest["version"]))
            if key in locations:
                raise DomainPackError("duplicate_pack_version")
            locations[key] = pack_path
        self._locations = locations
        return tuple(sorted(locations))

    def load(
        self, domain_pack_id: str, version: str | None = None
    ) -> LoadedDomainPack:
        if not self._locations:
            self.discover()
        matching = [key for key in self._locations if key[0] == domain_pack_id]
        if not matching:
            raise DomainPackError("pack_not_found")
        selected_version = version
        if selected_version is None:
            selected_version = max(
                (item[1] for item in matching), key=_semantic_version
            )
        try:
            pack_path = self._locations[(domain_pack_id, selected_version)]
        except KeyError as exc:
            raise DomainPackError("pack_version_not_found") from exc
        return self.load_path(pack_path)

    def load_path(self, pack_path: Path | str) -> LoadedDomainPack:
        root = Path(pack_path).resolve()
        try:
            root.relative_to(self.pack_root)
        except ValueError as exc:
            raise DomainPackError("pack_path_escape") from exc
        if not root.is_dir():
            raise DomainPackError("pack_not_found")

        manifest_path = root / "manifest.json"
        if not manifest_path.is_file():
            raise DomainPackError("manifest_missing")
        manifest = _read_json(manifest_path)
        _assert_no_answer_fields(manifest)
        _validate_schema(manifest, self.schema_root / DOMAIN_PACK_MANIFEST_SCHEMA)

        entries = manifest["files"]
        roles = [str(entry["role"]) for entry in entries]
        paths = [str(entry["path"]) for entry in entries]
        if len(roles) != len(set(roles)) or len(paths) != len(set(paths)):
            raise DomainPackError("duplicate_file_entry")
        if {entry["role"]: entry["path"] for entry in entries} != EXPECTED_FILES:
            raise DomainPackError("unexpected_file_set")
        expected_names = {"manifest.json", *EXPECTED_FILES.values()}
        actual_names = {entry.name for entry in root.iterdir()}
        if actual_names != expected_names:
            raise DomainPackError("unexpected_file_set")

        manifest_level = DATA_LEVEL_RANK[str(manifest["data_level"])]
        artifacts: dict[str, Mapping[str, Any]] = {}
        for entry in entries:
            role = str(entry["role"])
            file_path = (root / str(entry["path"])).resolve()
            try:
                file_path.relative_to(root)
            except ValueError as exc:
                raise DomainPackError("file_path_escape") from exc
            if not file_path.is_file():
                raise DomainPackError("pack_file_missing")
            if canonical_text_sha256(file_path) != entry["sha256"]:
                raise DomainPackError("hash_mismatch")
            level = str(entry["data_level"])
            if DATA_LEVEL_RANK[level] > manifest_level:
                raise DomainPackError("data_level_exceeds_manifest")
            if entry["send_to_model"] and level not in MODEL_DATA_LEVELS:
                raise DomainPackError("model_data_level_not_allowed")
            if role == "license_notice":
                continue
            artifact = _read_json(file_path)
            _assert_no_answer_fields(artifact)
            _validate_schema(artifact, self.schema_root / DOMAIN_PACK_CONTENT_SCHEMA)
            if artifact["artifact_type"] != role:
                raise DomainPackError("artifact_role_mismatch")
            artifacts[role] = artifact

        item_map, item_roles = self._validate_semantics(manifest, artifacts)
        return LoadedDomainPack(
            pack_path=root,
            manifest=_deep_freeze(manifest),
            artifacts=_deep_freeze(artifacts),
            items_by_id=_deep_freeze(item_map),
            item_roles=MappingProxyType(dict(item_roles)),
            manifest_sha256=canonical_text_sha256(manifest_path),
        )

    @staticmethod
    def _validate_semantics(
        manifest: Mapping[str, Any], artifacts: Mapping[str, Mapping[str, Any]]
    ) -> tuple[dict[str, Mapping[str, Any]], dict[str, str]]:
        source_ids: set[str] = set()
        item_map: dict[str, Mapping[str, Any]] = {}
        item_roles: dict[str, str] = {}

        def register(item_id: str, item: Mapping[str, Any], role: str) -> None:
            if item_id in item_map or item_id in source_ids:
                raise DomainPackError("duplicate_id")
            item_map[item_id] = item
            item_roles[item_id] = role

        for source in manifest["source_refs"]:
            source_id = str(source["source_id"])
            if source_id in source_ids:
                raise DomainPackError("duplicate_id")
            source_ids.add(source_id)

        for role, id_field in _ID_FIELDS.items():
            for item in artifacts[role]["items"]:
                register(str(item[id_field]), item, role)
                if role == "slots":
                    for condition in item["required_when"]:
                        register(str(condition["condition_id"]), condition, "conditions")

        concepts = artifacts["concepts"]["items"]
        concept_ids = {str(item["concept_id"]) for item in concepts}
        concept_parents = {
            str(item["concept_id"]): tuple(item["parent_concept_ids"])
            for item in concepts
        }
        for item in concepts:
            _assert_refs(item["parent_concept_ids"], concept_ids)
            _assert_refs(item["source_refs"], source_ids)
        _assert_acyclic(concept_parents)

        for item in artifacts["aliases"]["items"]:
            _assert_refs((item["concept_id"],), concept_ids)
            _assert_refs((item["source_ref"],), source_ids)

        slots = artifacts["slots"]["items"]
        slot_ids = {str(item["slot_id"]) for item in slots}
        schema_objects = artifacts["schema_catalog"]["items"]
        object_ids = {str(item["object_id"]) for item in schema_objects}
        evidence = artifacts["value_evidence"]["items"]
        evidence_ids = {str(item["evidence_id"]) for item in evidence}
        condition_subjects = concept_ids | slot_ids | object_ids
        for item in slots:
            _assert_refs(item["conflict_slot_ids"], slot_ids)
            _assert_refs(item["allowed_value_evidence_refs"], evidence_ids)
            _assert_refs(item["source_refs"], source_ids)
            for condition in item["required_when"]:
                _assert_refs((condition["subject_ref"],), condition_subjects)

        objects_by_id = {str(item["object_id"]): item for item in schema_objects}
        object_parents: dict[str, tuple[str, ...]] = {}
        for item in schema_objects:
            object_id = str(item["object_id"])
            parent = item["parent_object_id"]
            object_parents[object_id] = () if parent is None else (str(parent),)
            if parent is not None:
                _assert_refs((parent,), object_ids)
            _assert_refs(item["concept_refs"], concept_ids)
            _assert_refs(item["source_refs"], source_ids)
            object_type = item["object_type"]
            if object_type == "data_source" and parent is not None:
                raise DomainPackError("schema_parent_type_mismatch")
            if object_type == "table":
                if parent is None or objects_by_id[str(parent)]["object_type"] != "data_source":
                    raise DomainPackError("schema_parent_type_mismatch")
            if object_type == "column":
                if parent is None or objects_by_id[str(parent)]["object_type"] != "table":
                    raise DomainPackError("schema_parent_type_mismatch")
        _assert_acyclic(object_parents)

        for item in artifacts["relations"]["items"]:
            _assert_refs(
                (item["source_object_id"], item["target_object_id"]), object_ids
            )
            _assert_refs(item["source_refs"], source_ids)

        file_entries = {entry["role"]: entry for entry in manifest["files"]}
        evidence_file_level = DATA_LEVEL_RANK[
            str(file_entries["value_evidence"]["data_level"])
        ]
        for item in evidence:
            _assert_refs((item["slot_id"],), slot_ids)
            _assert_refs((item["source_ref"],), source_ids)
            item_level = str(item["data_level"])
            if DATA_LEVEL_RANK[item_level] > evidence_file_level:
                raise DomainPackError("data_level_exceeds_file")
            if item["send_to_model"] and item_level not in MODEL_DATA_LEVELS:
                raise DomainPackError("model_data_level_not_allowed")

        known_rule_inputs = concept_ids | slot_ids | object_ids
        for item in artifacts["validation_rules"]["items"]:
            _assert_refs(item["input_refs"], known_rule_inputs)
            _assert_refs(item["evidence_refs"], evidence_ids)
            _assert_refs(item["source_refs"], source_ids)

        for source in manifest["source_refs"]:
            source_id = str(source["source_id"])
            item_map[source_id] = source
            item_roles[source_id] = "source_refs"
        return item_map, item_roles
