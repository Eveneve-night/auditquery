"""FIN-DS01 v1 pure invariants, not a connector, credential store or executor."""
from functools import lru_cache
import json
from pathlib import Path
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from referencing import Registry, Resource

ROOT = Path(__file__).resolve().parents[3]
VERSION = "1.0.0"


class DataSourceContractError(ValueError):
    """Stable code only; never raw input/DSN/credential or exception content."""


@lru_cache(maxsize=1)
def _validators():
    schemas = [json.loads(p.read_text(encoding="utf-8")) for p in
               sorted((ROOT / "configs/schemas/data-sources").glob("*.json"))]
    registry = Registry().with_resources((s["$id"], Resource.from_contents(s)) for s in schemas)
    for schema in schemas:
        Draft202012Validator.check_schema(schema)
    return {s["$id"]: Draft202012Validator(s, registry=registry) for s in schemas}


def _validate(name, value):
    try:
        _validators()[name].validate(value)
    except (ValidationError, TypeError, ValueError):
        raise DataSourceContractError("invalid_contract") from None


def validate_source(source):
    _validate("data-source-record.v1", source)
    secret = source["credential_ref"]
    if secret is not None and secret["source_id"] != source["source_id"]:
        raise DataSourceContractError("credential_scope_mismatch")
    if source["status"] in {"synced", "published", "draining"} and source["binding"] is None:
        raise DataSourceContractError("binding_required")
    return None


def validate_connector(descriptor):
    _validate("connector-descriptor.v1", descriptor)
    capabilities = descriptor["capabilities"]
    # This batch has no network connector or multi-table permission.
    if capabilities["network"] != "unsupported" or capabilities["multi_table"] != "unsupported":
        raise DataSourceContractError("capability_not_authorized")
    if capabilities["tls"] != "not_applicable":
        raise DataSourceContractError("capability_not_applicable")


def validate_snapshot(snapshot, source, *, authorized_source_ids, descriptor):
    _validate("query-source-snapshot.v1", snapshot)
    validate_source(source)
    validate_connector(descriptor)
    # Authorization precedes existence/version details: callers return generic 404.
    if source["source_id"] not in authorized_source_ids or snapshot["source_id"] != source["source_id"]:
        raise DataSourceContractError("source_not_found")
    if source["status"] != "published":
        raise DataSourceContractError("source_not_available")
    if descriptor["connector"] != source["connector"]:
        raise DataSourceContractError("connector_mismatch")
    if any(descriptor["capabilities"][key] != "supported" for key in ("readonly", "single_table", "timeout")):
        raise DataSourceContractError("required_capability_unavailable")
    if (snapshot["source_revision"] != source["revision"] or
        snapshot["connector"] != source["connector"] or snapshot["binding"] != source["binding"]):
        raise DataSourceContractError("stale_source_binding")
    # Success authorizes only further existing safety checks, never SQL execution.


TRANSITIONS = {
    "draft": {"verified", "deleted"},
    "verified": {"synced", "draft", "deleted"},
    "synced": {"published", "draft", "deleted"},
    "published": {"draining"},
    "draining": {"disabled"},
    "disabled": {"draft", "deleted"},
    "deleted": set(),
}


def validate_transition(source, target, *, expected_revision, actor_role, operation_id, inflight=0):
    """Validate a proposed command; B must atomically CAS/persist/audit/replay."""
    if actor_role != "admin":
        raise DataSourceContractError("forbidden")
    validate_source(source)
    if (type(expected_revision) is not int or expected_revision != source["revision"]):
        raise DataSourceContractError("revision_conflict")
    if not isinstance(operation_id, str) or not operation_id or len(operation_id) > 128:
        raise DataSourceContractError("invalid_operation_id")
    if not isinstance(target, str) or target not in TRANSITIONS[source["status"]]:
        raise DataSourceContractError("invalid_transition")
    if type(inflight) is not int or inflight < 0:
        raise DataSourceContractError("invalid_inflight_count")
    if source["status"] == "draining" and inflight:
        raise DataSourceContractError("source_busy")
    if target == "published" and source["binding"] is None:
        raise DataSourceContractError("binding_required")
