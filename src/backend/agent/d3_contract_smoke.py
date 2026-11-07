"""D3 contract-smoke manifest, payload construction, and bounded runner.

The default workflow is offline.  Real model access is possible only when a
caller explicitly constructs and injects an ``LLMGateway``; this module never
loads credentials or creates a network client.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import time
from typing import Any, Callable, Protocol

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JSONSchemaValidationError
from referencing import Registry, Resource

from backend.agent.loop_guard import LoopGuard, LoopPolicy
from backend.agent.task_contracts import (
    ContractValidationError,
    build_task_payload,
    validate_task_result,
)
from backend.llm.contracts import ModelCallRequest, ModelCallResult, ModelMessage
from backend.llm.errors import ProviderError
from backend.llm.usage import UsageLedger
from backend.nl2sql.contract import validate_model_output


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_MANIFEST_PATH = (
    PROJECT_ROOT / "tests" / "goldens" / "d3-smoke" / "v1" / "manifest.json"
)
APPROVED_MODEL = "qwen3.7-flash"
EXPECTED_TASKS = ("intent", "clarification", "nl2sql", "grounded_answer")
EXPECTED_SCOPES = {
    "intent": "fixed_catalog_conditional_routing",
    "clarification": "given_required_slots_conditional_check",
    "nl2sql": "candidate_sql_contract_only",
    "grounded_answer": "given_evidence_conditional_answer",
}
EVIDENCE_LINE = re.compile(r"^\[([A-Z]+-[0-9]{3})\]\s+(.+)$")
SAFE_FINISH_REASONS = {"stop", "length", "content_filter", "tool_calls"}
SAFE_WARNINGS = {
    "provider_retry_applied",
    "json_repair_applied",
    "soft_cost_limit_exceeded",
}


class ContractSmokeError(ValueError):
    """Raised when D3 smoke configuration or output fails closed."""


class GatewayLike(Protocol):
    provider: Any

    def generate_structured(
        self, request: ModelCallRequest, *, guard: LoopGuard
    ) -> ModelCallResult: ...


@dataclass(frozen=True)
class PreparedCase:
    case_id: str
    task_type: str
    evaluation_scope: str
    payload: dict[str, Any]
    request: ModelCallRequest
    result_schema: dict[str, Any]


@dataclass(frozen=True)
class BatchLimits:
    max_provider_attempts: int
    max_input_tokens: int
    max_output_tokens: int
    hard_cost_cny: float
    max_wall_time_ms: int

    @classmethod
    def from_mapping(cls, value: object) -> "BatchLimits":
        if not isinstance(value, Mapping):
            raise ContractSmokeError("batch_limits must be an object")
        _require_exact_fields(
            value,
            {
                "max_provider_attempts",
                "max_input_tokens",
                "max_output_tokens",
                "hard_cost_cny",
                "max_wall_time_ms",
            },
            "batch_limits",
        )
        integer_fields = (
            "max_provider_attempts",
            "max_input_tokens",
            "max_output_tokens",
            "max_wall_time_ms",
        )
        if any(
            not isinstance(value[field], int)
            or isinstance(value[field], bool)
            or value[field] <= 0
            for field in integer_fields
        ):
            raise ContractSmokeError("batch integer limits must be positive")
        cost = value["hard_cost_cny"]
        if (
            not isinstance(cost, (int, float))
            or isinstance(cost, bool)
            or cost <= 0
        ):
            raise ContractSmokeError("batch hard_cost_cny must be positive")
        return cls(
            max_provider_attempts=value["max_provider_attempts"],
            max_input_tokens=value["max_input_tokens"],
            max_output_tokens=value["max_output_tokens"],
            hard_cost_cny=float(cost),
            max_wall_time_ms=value["max_wall_time_ms"],
        )

    def as_dict(self) -> dict[str, int | float]:
        return {
            "max_provider_attempts": self.max_provider_attempts,
            "max_input_tokens": self.max_input_tokens,
            "max_output_tokens": self.max_output_tokens,
            "hard_cost_cny": self.hard_cost_cny,
            "max_wall_time_ms": self.max_wall_time_ms,
        }


@dataclass
class _BatchUsage:
    provider_attempts: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_cny: float = 0.0

    def add(self, usage: Mapping[str, Any]) -> None:
        self.provider_attempts += int(usage["attempts"])
        self.input_tokens += int(usage["input_tokens"])
        self.output_tokens += int(usage["output_tokens"])
        self.total_tokens += int(usage["total_tokens"])
        self.estimated_cost_cny = round(
            self.estimated_cost_cny + float(usage["estimated_cost_cny"]), 8
        )


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ContractSmokeError(f"JSON root must be an object: {path.name}")
    return value


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ContractSmokeError(
                f"JSONL record must be an object: {path.name}:{line_number}"
            )
        records.append(value)
    return records


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _require_exact_fields(
    value: Mapping[str, Any], expected: set[str], name: str
) -> None:
    actual = set(value)
    if actual != expected:
        raise ContractSmokeError(
            f"{name} fields mismatch: missing={sorted(expected - actual)}, "
            f"extra={sorted(actual - expected)}"
        )


def _repo_path(value: object, *, directory: bool = False) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ContractSmokeError("repository path must be a non-empty string")
    path = (PROJECT_ROOT / value).resolve()
    try:
        path.relative_to(PROJECT_ROOT.resolve())
    except ValueError as exc:
        raise ContractSmokeError("path escapes the repository") from exc
    exists = path.is_dir() if directory else path.is_file()
    if not exists:
        kind = "directory" if directory else "file"
        raise ContractSmokeError(f"repository {kind} does not exist: {value}")
    return path


def _candidate_ids(selection: Mapping[str, Any], task_type: str) -> list[str]:
    if task_type in {"intent", "clarification"}:
        selections = selection.get("selections")
        if not isinstance(selections, Mapping):
            raise ContractSmokeError("intent/clarification selection is invalid")
        task_selection = selections.get(task_type)
        if not isinstance(task_selection, Mapping):
            raise ContractSmokeError(f"missing selection for {task_type}")
        value = task_selection.get("case_ids")
    elif task_type == "nl2sql":
        value = selection.get("candidate_ids")
    else:
        value = selection.get("selected_case_ids")
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ContractSmokeError(f"candidate IDs are invalid for {task_type}")
    return value


def _payload_context(
    selection: Mapping[str, Any], task_type: str, case_id: str
) -> Mapping[str, Any] | None:
    if task_type not in {"intent", "clarification"}:
        return None
    selections = selection["selections"]
    task_selection = selections[task_type]
    contexts = task_selection.get("payload_context")
    if not isinstance(contexts, Mapping):
        raise ContractSmokeError(f"payload_context is missing for {task_type}")
    context = contexts.get(case_id)
    if not isinstance(context, Mapping):
        raise ContractSmokeError(f"payload_context is missing for {case_id}")
    return context


def _build_intent_or_clarification_payload(
    task_type: str,
    case: Mapping[str, Any],
    selection: Mapping[str, Any],
) -> dict[str, Any]:
    case_id = case.get("id")
    if not isinstance(case_id, str):
        raise ContractSmokeError("case id is invalid")
    return build_task_payload(
        case,
        payload_context=_payload_context(selection, task_type, case_id),
    )


def _build_nl2sql_payload(
    case: Mapping[str, Any], database_schema: Mapping[str, Any]
) -> dict[str, Any]:
    input_data = case.get("input")
    context = case.get("context")
    if not isinstance(input_data, Mapping) or not isinstance(context, Mapping):
        raise ContractSmokeError("nl2sql input/context must be objects")
    query = input_data.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ContractSmokeError("nl2sql query is required")
    if context.get("dialect") != "sqlite" or context.get("scope") != "single_table":
        raise ContractSmokeError("nl2sql case is outside the frozen v1 scope")
    if context.get("database") != database_schema.get("schema_id"):
        raise ContractSmokeError("nl2sql database does not match the frozen schema")
    return {
        "query": query,
        "database_schema": deepcopy(dict(database_schema)),
        "dialect": "sqlite",
        "scope": "single_table",
    }


def _fixture_catalog(fixture_dir: Path) -> dict[str, dict[str, str]]:
    catalog: dict[str, dict[str, str]] = {}
    for path in sorted(fixture_dir.glob("*.md")):
        evidence: dict[str, str] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            match = EVIDENCE_LINE.fullmatch(line.strip())
            if match:
                evidence[match.group(1)] = match.group(2)
        if evidence:
            catalog[path.stem] = evidence
    return catalog


def _build_grounded_answer_payload(
    case: Mapping[str, Any], fixture_catalog: Mapping[str, Mapping[str, str]]
) -> dict[str, Any]:
    input_data = case.get("input")
    context = case.get("context")
    if not isinstance(input_data, Mapping) or not isinstance(context, Mapping):
        raise ContractSmokeError("grounded_answer input/context must be objects")
    query = input_data.get("query")
    document_id = context.get("document_id")
    evidence_ids = context.get("evidence_ids")
    if not isinstance(query, str) or not query.strip():
        raise ContractSmokeError("grounded_answer query is required")
    if not isinstance(document_id, str) or document_id not in fixture_catalog:
        raise ContractSmokeError("grounded_answer document is not an approved fixture")
    if (
        not isinstance(evidence_ids, list)
        or not evidence_ids
        or any(not isinstance(item, str) for item in evidence_ids)
        or len(evidence_ids) != len(set(evidence_ids))
    ):
        raise ContractSmokeError("grounded_answer evidence_ids are invalid")
    document_evidence = fixture_catalog[document_id]
    evidence: list[dict[str, str]] = []
    for evidence_id in evidence_ids:
        if evidence_id not in document_evidence:
            raise ContractSmokeError(f"fixture evidence is missing: {evidence_id}")
        evidence.append(
            {"evidence_id": evidence_id, "text": document_evidence[evidence_id]}
        )
    return {"query": query, "evidence": evidence}


def build_case_payload(
    task_type: str,
    case: Mapping[str, Any],
    *,
    selection: Mapping[str, Any],
    database_schema: Mapping[str, Any],
    fixture_catalog: Mapping[str, Mapping[str, str]],
) -> dict[str, Any]:
    """Build one allowlisted payload without consulting answer-like fields."""

    if case.get("task_type") != task_type:
        raise ContractSmokeError("case task_type does not match manifest task")
    if task_type in {"intent", "clarification"}:
        return _build_intent_or_clarification_payload(task_type, case, selection)
    if task_type == "nl2sql":
        return _build_nl2sql_payload(case, database_schema)
    if task_type == "grounded_answer":
        return _build_grounded_answer_payload(case, fixture_catalog)
    raise ContractSmokeError(f"unsupported smoke task: {task_type}")


class D3ContractSmokeRunner:
    """Prepare and optionally execute the fixed 20-case D3 contract smoke."""

    def __init__(self, manifest_path: str | Path = DEFAULT_MANIFEST_PATH) -> None:
        self.manifest_path = Path(manifest_path).resolve()
        self.manifest = _load_json(self.manifest_path)
        self._validate_manifest()
        self.policy_path = _repo_path(self.manifest["policy_path"])
        self.policy = LoopPolicy.from_file(self.policy_path)
        self.batch_limits = BatchLimits.from_mapping(self.manifest["batch_limits"])
        self.database_schema_path = _repo_path(self.manifest["database_schema_path"])
        self.database_schema = _load_json(self.database_schema_path)
        self.fixture_dir = _repo_path(
            self.manifest["knowledge_fixture_dir"], directory=True
        )
        self.fixture_catalog = _fixture_catalog(self.fixture_dir)
        self._prepared_cases: tuple[PreparedCase, ...] | None = None

    def _validate_manifest(self) -> None:
        _require_exact_fields(
            self.manifest,
            {
                "manifest_id",
                "version",
                "purpose",
                "model",
                "source_split",
                "validation_exposure_count",
                "claims",
                "batch_limits",
                "policy_path",
                "dataset_manifest_path",
                "database_schema_path",
                "knowledge_fixture_dir",
                "tasks",
            },
            "manifest",
        )
        if self.manifest["purpose"] != "contract_smoke_only":
            raise ContractSmokeError("manifest purpose must be contract_smoke_only")
        if self.manifest["model"] != APPROVED_MODEL:
            raise ContractSmokeError("manifest model is not approved")
        if self.manifest["source_split"] != "prompt_dev":
            raise ContractSmokeError("D3 smoke may use only prompt_dev")
        if self.manifest["validation_exposure_count"] != 0:
            raise ContractSmokeError("D3-04 validation exposure must remain zero")
        if self.manifest["claims"] != {
            "accuracy": False,
            "generalization": False,
            "end_to_end": False,
        }:
            raise ContractSmokeError("D3 contract smoke cannot make capability claims")
        BatchLimits.from_mapping(self.manifest["batch_limits"])
        _repo_path(self.manifest["policy_path"])
        _repo_path(self.manifest["dataset_manifest_path"])
        _repo_path(self.manifest["database_schema_path"])
        _repo_path(self.manifest["knowledge_fixture_dir"], directory=True)

        tasks = self.manifest["tasks"]
        if not isinstance(tasks, list) or len(tasks) != len(EXPECTED_TASKS):
            raise ContractSmokeError("manifest must contain exactly four task groups")
        task_types: list[str] = []
        all_ids: list[str] = []
        for task in tasks:
            if not isinstance(task, Mapping):
                raise ContractSmokeError("manifest task must be an object")
            _require_exact_fields(
                task,
                {
                    "task_type",
                    "evaluation_scope",
                    "source_case_path",
                    "selection_path",
                    "contract_path",
                    "case_ids",
                },
                "manifest task",
            )
            task_type = task["task_type"]
            if task_type not in EXPECTED_TASKS:
                raise ContractSmokeError(f"unexpected task type: {task_type}")
            if task["evaluation_scope"] != EXPECTED_SCOPES[task_type]:
                raise ContractSmokeError(f"incorrect evaluation scope for {task_type}")
            case_ids = task["case_ids"]
            if (
                not isinstance(case_ids, list)
                or len(case_ids) != 5
                or any(not isinstance(item, str) for item in case_ids)
                or len(case_ids) != len(set(case_ids))
            ):
                raise ContractSmokeError(f"{task_type} must contain five unique IDs")
            for path_key in ("source_case_path", "selection_path", "contract_path"):
                _repo_path(task[path_key])
            task_types.append(task_type)
            all_ids.extend(case_ids)
        if tuple(task_types) != EXPECTED_TASKS:
            raise ContractSmokeError("task groups must use the frozen order")
        if len(all_ids) != 20 or len(all_ids) != len(set(all_ids)):
            raise ContractSmokeError("manifest must contain 20 unique case IDs")

    def prepare_cases(self) -> tuple[PreparedCase, ...]:
        if self._prepared_cases is not None:
            return self._prepared_cases
        prompt_contract_schema = _load_json(
            PROJECT_ROOT / "configs" / "schemas" / "prompt-contract.v1.schema.json"
        )
        prepared: list[PreparedCase] = []
        for task in self.manifest["tasks"]:
            task_type = task["task_type"]
            source_path = _repo_path(task["source_case_path"])
            selection_path = _repo_path(task["selection_path"])
            contract_path = _repo_path(task["contract_path"])
            selection = _load_json(selection_path)
            if _candidate_ids(selection, task_type) != task["case_ids"]:
                raise ContractSmokeError(
                    f"manifest IDs do not match reviewed selection for {task_type}"
                )

            contract = _load_json(contract_path)
            Draft202012Validator(prompt_contract_schema).validate(contract)
            if contract.get("model") != APPROVED_MODEL:
                raise ContractSmokeError(f"unapproved model in {contract_path.name}")
            if contract.get("task_type") != task_type:
                raise ContractSmokeError("contract task_type does not match manifest")
            generation = contract["generation"]
            if (
                generation.get("temperature") != 0
                or generation.get("enable_thinking") is not False
                or generation.get("response_format") != "json_object"
                or generation.get("max_completion_tokens", 0)
                > int(self.policy.limits["max_output_tokens_total"])
            ):
                raise ContractSmokeError("contract generation settings exceed D3 policy")

            prompt_path = _repo_path(contract["prompt_path"])
            schema_path = _repo_path(contract["output_schema_path"])
            prompt = prompt_path.read_text(encoding="utf-8")
            result_schema = _load_json(schema_path)
            Draft202012Validator.check_schema(result_schema)
            if result_schema.get("$id") != contract["output_schema_id"]:
                raise ContractSmokeError("contract output schema ID does not match file")

            records = _load_jsonl(source_path)
            by_id = {record.get("id"): record for record in records}
            for case_id in task["case_ids"]:
                case = by_id.get(case_id)
                if not isinstance(case, Mapping):
                    raise ContractSmokeError(f"case does not exist: {case_id}")
                if case.get("split") != "prompt_dev":
                    raise ContractSmokeError(f"non-prompt_dev case is forbidden: {case_id}")
                payload = build_case_payload(
                    task_type,
                    case,
                    selection=selection,
                    database_schema=self.database_schema,
                    fixture_catalog=self.fixture_catalog,
                )
                request = ModelCallRequest(
                    request_id=f"d3-smoke-{case_id.lower()}",
                    task_type=task_type,
                    messages=(
                        ModelMessage("system", prompt),
                        ModelMessage("user", _canonical_json(payload)),
                    ),
                    prompt_version=contract["prompt_version"],
                    output_schema_id=contract["output_schema_id"],
                    max_completion_tokens=generation["max_completion_tokens"],
                    temperature=0,
                    metadata={
                        "manifest_id": self.manifest["manifest_id"],
                        "case_id": case_id,
                        "evaluation_scope": task["evaluation_scope"],
                        "source_split": "prompt_dev",
                    },
                )
                prepared.append(
                    PreparedCase(
                        case_id=case_id,
                        task_type=task_type,
                        evaluation_scope=task["evaluation_scope"],
                        payload=payload,
                        request=request,
                        result_schema=result_schema,
                    )
                )
        if len(prepared) != 20:
            raise ContractSmokeError("preflight did not prepare exactly 20 cases")
        self._prepared_cases = tuple(prepared)
        return self._prepared_cases

    def _artifact_hashes(self) -> dict[str, str]:
        paths = {
            self.manifest_path,
            self.policy_path,
            self.database_schema_path,
            _repo_path(self.manifest["dataset_manifest_path"]),
        }
        for task in self.manifest["tasks"]:
            contract_path = _repo_path(task["contract_path"])
            contract = _load_json(contract_path)
            paths.update(
                {
                    _repo_path(task["source_case_path"]),
                    _repo_path(task["selection_path"]),
                    contract_path,
                    _repo_path(contract["prompt_path"]),
                    _repo_path(contract["output_schema_path"]),
                }
            )
        paths.update(self.fixture_dir.glob("*.md"))
        return {
            path.relative_to(PROJECT_ROOT).as_posix(): _sha256(path)
            for path in sorted(paths, key=lambda item: item.as_posix())
        }

    def _provenance(self) -> dict[str, Any]:
        dataset = _load_json(_repo_path(self.manifest["dataset_manifest_path"]))
        contracts: dict[str, dict[str, str]] = {}
        for task in self.manifest["tasks"]:
            contract = _load_json(_repo_path(task["contract_path"]))
            contracts[task["task_type"]] = {
                "contract_id": contract["contract_id"],
                "contract_version": contract["contract_version"],
                "prompt_version": contract["prompt_version"],
                "output_schema_id": contract["output_schema_id"],
            }
        return {
            "dataset": {
                "dataset_id": dataset["dataset_id"],
                "version": dataset["version"],
            },
            "contracts": contracts,
            "policy": {
                "policy_id": self.policy.policy_id,
                "version": self.policy.version,
            },
            "artifact_hashes": self._artifact_hashes(),
        }

    def offline_preflight(self) -> dict[str, Any]:
        prepared = self.prepare_cases()
        return {
            "report_type": "d3_contract_smoke_offline_preflight",
            "purpose": "contract_smoke_only",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "manifest_id": self.manifest["manifest_id"],
            "manifest_version": self.manifest["version"],
            "model": APPROVED_MODEL,
            "source_split": "prompt_dev",
            "validation_exposure_count": 0,
            "claims": deepcopy(self.manifest["claims"]),
            "provenance": self._provenance(),
            "limits": {
                "per_case_policy": {
                    "policy_id": self.policy.policy_id,
                    "version": self.policy.version,
                    "limits": deepcopy(self.policy.limits),
                },
                "batch": self.batch_limits.as_dict(),
            },
            "summary": {
                "prepared_case_count": len(prepared),
                "task_counts": dict(Counter(item.task_type for item in prepared)),
                "network_calls": 0,
                "api_calls": 0,
                "cost_cny": 0.0,
            },
            "cases": [
                {
                    "case_id": item.case_id,
                    "task_type": item.task_type,
                    "evaluation_scope": item.evaluation_scope,
                    "status": "prepared",
                    "prompt_version": item.request.prompt_version,
                    "output_schema_id": item.request.output_schema_id,
                }
                for item in prepared
            ],
        }

    @staticmethod
    def _elapsed_ms(started_at: float, clock: Callable[[], float]) -> int:
        return max(0, int((clock() - started_at) * 1000))

    def _batch_stop_reason(
        self,
        usage: _BatchUsage,
        *,
        started_at: float,
        clock: Callable[[], float],
    ) -> str | None:
        limits = self.batch_limits
        if usage.estimated_cost_cny >= limits.hard_cost_cny:
            return "batch_cost_limit"
        if self._elapsed_ms(started_at, clock) >= limits.max_wall_time_ms:
            return "batch_wall_time_limit"
        if (
            usage.input_tokens >= limits.max_input_tokens
            or usage.output_tokens >= limits.max_output_tokens
        ):
            return "batch_token_limit"
        if usage.provider_attempts >= limits.max_provider_attempts:
            return "batch_model_call_limit"
        return None

    def _remaining_case_policy(
        self,
        usage: _BatchUsage,
        *,
        started_at: float,
        clock: Callable[[], float],
    ) -> LoopPolicy:
        limits = dict(self.policy.limits)
        remaining_attempts = self.batch_limits.max_provider_attempts - usage.provider_attempts
        remaining_input = self.batch_limits.max_input_tokens - usage.input_tokens
        remaining_output = self.batch_limits.max_output_tokens - usage.output_tokens
        remaining_cost = self.batch_limits.hard_cost_cny - usage.estimated_cost_cny
        remaining_wall = self.batch_limits.max_wall_time_ms - self._elapsed_ms(
            started_at, clock
        )
        if min(remaining_attempts, remaining_input, remaining_output, remaining_wall) <= 0:
            raise ContractSmokeError("batch budget is already exhausted")
        if remaining_cost <= 0:
            raise ContractSmokeError("batch cost budget is already exhausted")
        limits["max_model_calls"] = min(
            int(limits["max_model_calls"]), remaining_attempts
        )
        limits["max_input_tokens_total"] = min(
            int(limits["max_input_tokens_total"]), remaining_input
        )
        limits["max_output_tokens_total"] = min(
            int(limits["max_output_tokens_total"]), remaining_output
        )
        limits["hard_cost_cny"] = min(
            float(limits["hard_cost_cny"]), remaining_cost
        )
        limits["max_wall_time_ms"] = min(
            int(limits["max_wall_time_ms"]), remaining_wall
        )
        return LoopPolicy(
            policy_id=self.policy.policy_id,
            version=self.policy.version,
            limits=limits,
            termination_precedence=self.policy.termination_precedence,
        )

    @staticmethod
    def _ledger_cursor(gateway: GatewayLike) -> int | None:
        ledger = getattr(gateway, "usage_ledger", None)
        return ledger.cursor() if isinstance(ledger, UsageLedger) else None

    @staticmethod
    def _request_usage(
        gateway: GatewayLike,
        *,
        ledger_cursor: int | None,
        request_id: str,
        guard: LoopGuard,
        result: ModelCallResult | None,
        error: Exception | None,
    ) -> dict[str, Any]:
        ledger = getattr(gateway, "usage_ledger", None)
        if isinstance(ledger, UsageLedger) and ledger_cursor is not None:
            summary = ledger.summary(
                request_id=request_id, start_index=ledger_cursor
            )
            if summary["attempts"]:
                return {
                    "attempts": summary["attempts"],
                    "input_tokens": summary["input_tokens"],
                    "output_tokens": summary["output_tokens"],
                    "total_tokens": summary["total_tokens"],
                    "estimated_cost_cny": summary["estimated_cost_cny"],
                    "latency_ms": summary["latency_total_ms"],
                    "usage_source": "usage_ledger_delta",
                }

        snapshot = guard.snapshot()
        attempts = int(snapshot["counts"].get("model_call", 0))
        if result is not None:
            attempts = result.attempt_count
        elif (
            isinstance(error, ProviderError)
            and error.code.value == "policy_limit"
            and snapshot["stopped_reason"] == "model_call_limit"
        ):
            attempts = min(attempts, int(guard.policy.limits["max_model_calls"]))
        input_tokens = int(snapshot["input_tokens_total"])
        output_tokens = int(snapshot["output_tokens_total"])
        return {
            "attempts": attempts,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "estimated_cost_cny": float(snapshot["estimated_cost_cny"]),
            "latency_ms": (
                result.latency_ms if result is not None else snapshot["elapsed_ms"]
            ),
            "usage_source": "loop_guard_snapshot",
        }

    @staticmethod
    def _stable_error_code(error: Exception) -> str:
        if isinstance(error, ProviderError):
            return error.code.value
        if isinstance(error, JSONSchemaValidationError):
            return "schema_validation_error"
        if isinstance(error, (ContractSmokeError, ContractValidationError, ValueError)):
            return "contract_validation_error"
        return "internal_error"

    @staticmethod
    def _output_validation(
        result: ModelCallResult, *, schema_id: str
    ) -> dict[str, str]:
        parsed_output = result.parsed_output
        schema_version = parsed_output.get("schema_version")
        if not isinstance(schema_version, str):
            raise ContractSmokeError("validated output is missing schema_version")
        digest = hashlib.sha256(
            _canonical_json(parsed_output).encode("utf-8")
        ).hexdigest()
        return {
            "schema_id": schema_id,
            "schema_version": schema_version,
            "json_schema_validation": "passed",
            "semantic_validation": "passed",
            "normalized_output_sha256": digest,
        }

    def run(
        self,
        gateway: GatewayLike,
        *,
        case_limit: int | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> dict[str, Any]:
        """Run through an explicitly injected gateway and return a sanitized report."""

        provider = getattr(gateway, "provider", None)
        if getattr(provider, "model", None) != APPROVED_MODEL:
            raise ContractSmokeError("gateway must be fixed to qwen3.7-flash")
        prepared = self.prepare_cases()
        if case_limit is None:
            selected = prepared
        else:
            if not isinstance(case_limit, int) or isinstance(case_limit, bool):
                raise ContractSmokeError("case_limit must be an integer")
            if not 1 <= case_limit <= len(prepared):
                raise ContractSmokeError("case_limit must be within the manifest size")
            selected = prepared[:case_limit]

        batch_started = clock()
        batch_usage = _BatchUsage()
        batch_stop_reason: str | None = None
        case_reports: list[dict[str, Any]] = []
        for item in selected:
            batch_stop_reason = self._batch_stop_reason(
                batch_usage, started_at=batch_started, clock=clock
            )
            if batch_stop_reason is not None:
                break
            case_policy = self._remaining_case_policy(
                batch_usage, started_at=batch_started, clock=clock
            )
            guard = LoopGuard(case_policy, clock=clock)
            ledger_cursor = self._ledger_cursor(gateway)
            result: ModelCallResult | None = None
            failure: Exception | None = None
            case_report: dict[str, Any] = {
                "case_id": item.case_id,
                "task_type": item.task_type,
                "evaluation_scope": item.evaluation_scope,
                "prompt_version": item.request.prompt_version,
                "output_schema_id": item.request.output_schema_id,
            }
            try:
                result = gateway.generate_structured(item.request, guard=guard)
                if (
                    result.request_id != item.request.request_id
                    or result.task_type != item.task_type
                ):
                    raise ContractSmokeError("provider result identity does not match request")
                if (
                    result.requested_model != APPROVED_MODEL
                    or result.returned_model != APPROVED_MODEL
                ):
                    raise ContractSmokeError("provider returned an unapproved model")
                self.validate_result(item, result.parsed_output)
            except Exception as exc:  # sanitized per-case failure; continue the batch
                failure = exc

            usage = self._request_usage(
                gateway,
                ledger_cursor=ledger_cursor,
                request_id=item.request.request_id,
                guard=guard,
                result=result,
                error=failure,
            )
            batch_usage.add(usage)
            snapshot = guard.snapshot()
            if failure is None:
                assert result is not None
                case_report.update(
                    {
                        "status": "contract_valid",
                        "provider_id": result.provider_id,
                        "requested_model": result.requested_model,
                        "returned_model": result.returned_model,
                        "attempt_count": usage["attempts"],
                        "input_tokens": usage["input_tokens"],
                        "output_tokens": usage["output_tokens"],
                        "total_tokens": usage["total_tokens"],
                        "estimated_cost_cny": usage["estimated_cost_cny"],
                        "latency_ms": usage["latency_ms"],
                        "usage_source": usage["usage_source"],
                        "finish_reason": (
                            result.finish_reason
                            if result.finish_reason in SAFE_FINISH_REASONS
                            else None
                        ),
                        "warnings": [
                            warning
                            for warning in result.warnings
                            if warning in SAFE_WARNINGS
                        ],
                        "error_code": None,
                        "stop_reason": snapshot["stopped_reason"],
                        "output_validation": self._output_validation(
                            result, schema_id=item.request.output_schema_id
                        ),
                    }
                )
            else:
                case_report.update(
                    {
                        "status": "failed",
                        "provider_id": getattr(provider, "provider_id", "unknown"),
                        "requested_model": APPROVED_MODEL,
                        "returned_model": None,
                        "attempt_count": usage["attempts"],
                        "input_tokens": usage["input_tokens"],
                        "output_tokens": usage["output_tokens"],
                        "total_tokens": usage["total_tokens"],
                        "estimated_cost_cny": usage["estimated_cost_cny"],
                        "latency_ms": usage["latency_ms"],
                        "usage_source": usage["usage_source"],
                        "finish_reason": None,
                        "warnings": [],
                        "error_code": self._stable_error_code(failure),
                        "stop_reason": snapshot["stopped_reason"],
                        "output_validation": None,
                    }
                )
            case_reports.append(case_report)

            batch_stop_reason = self._batch_stop_reason(
                batch_usage, started_at=batch_started, clock=clock
            )
            if batch_stop_reason is not None:
                break

        for item in selected[len(case_reports) :]:
            case_reports.append(
                {
                    "case_id": item.case_id,
                    "task_type": item.task_type,
                    "evaluation_scope": item.evaluation_scope,
                    "prompt_version": item.request.prompt_version,
                    "output_schema_id": item.request.output_schema_id,
                    "status": "not_run",
                    "provider_id": getattr(provider, "provider_id", "unknown"),
                    "requested_model": APPROVED_MODEL,
                    "returned_model": None,
                    "attempt_count": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "total_tokens": 0,
                    "estimated_cost_cny": 0.0,
                    "latency_ms": 0,
                    "usage_source": "not_run",
                    "finish_reason": None,
                    "warnings": [],
                    "error_code": "batch_budget_exhausted",
                    "stop_reason": batch_stop_reason,
                    "output_validation": None,
                }
            )

        valid_count = sum(
            item["status"] == "contract_valid" for item in case_reports
        )
        failed_count = sum(item["status"] == "failed" for item in case_reports)
        not_run_count = sum(item["status"] == "not_run" for item in case_reports)
        attempted_count = len(case_reports) - not_run_count
        batch_elapsed_ms = self._elapsed_ms(batch_started, clock)
        return {
            "report_type": "d3_contract_smoke_run",
            "purpose": "contract_smoke_only",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "manifest_id": self.manifest["manifest_id"],
            "manifest_version": self.manifest["version"],
            "model": APPROVED_MODEL,
            "source_split": "prompt_dev",
            "validation_exposure_count": 0,
            "claims": deepcopy(self.manifest["claims"]),
            "provenance": self._provenance(),
            "limits": {
                "per_case_policy": {
                    "policy_id": self.policy.policy_id,
                    "version": self.policy.version,
                    "limits": deepcopy(self.policy.limits),
                },
                "batch": self.batch_limits.as_dict(),
            },
            "summary": {
                "planned_case_count": len(selected),
                "case_count": len(case_reports),
                "attempted_case_count": attempted_count,
                "not_run_count": not_run_count,
                "contract_valid_count": valid_count,
                "failed_count": failed_count,
                "contract_valid_rate": (
                    valid_count / attempted_count if attempted_count else 0.0
                ),
                "model_attempt_count": batch_usage.provider_attempts,
                "input_tokens": batch_usage.input_tokens,
                "output_tokens": batch_usage.output_tokens,
                "total_tokens": batch_usage.total_tokens,
                "estimated_cost_cny": batch_usage.estimated_cost_cny,
                "elapsed_ms": batch_elapsed_ms,
                "batch_stop_reason": batch_stop_reason,
            },
            "cases": case_reports,
        }

    @staticmethod
    def validate_result(item: PreparedCase, result: Mapping[str, Any]) -> None:
        if not isinstance(result, Mapping):
            raise ContractSmokeError("model result must be an object")
        schema = item.result_schema
        if item.task_type in {"intent", "clarification"}:
            envelope = _load_json(
                PROJECT_ROOT / "configs" / "schemas" / "task-result-envelope.v1.schema.json"
            )
            registry = Registry().with_resource(
                envelope["$id"], Resource.from_contents(envelope)
            )
            Draft202012Validator(schema, registry=registry).validate(result)
            validate_task_result(item.task_type, item.payload, result)
            return
        Draft202012Validator(schema).validate(result)
        if item.task_type == "nl2sql":
            validate_model_output(result)
            if result["status"] == "success":
                available_tables = set(item.payload["database_schema"]["tables"])
                if not set(result["data"]["tables"]).issubset(available_tables):
                    raise ContractSmokeError(
                        "nl2sql result references a table outside the input schema"
                    )
            return
        D3ContractSmokeRunner._validate_grounded_answer_semantics(
            item.payload, result
        )

    @staticmethod
    def _validate_grounded_answer_semantics(
        payload: Mapping[str, Any], result: Mapping[str, Any]
    ) -> None:
        if result["status"] == "error":
            return
        data = result["data"]
        if not data["answerable"]:
            return
        evidence_by_id = {
            evidence["evidence_id"]: evidence["text"]
            for evidence in payload["evidence"]
        }
        result_ids = set(data["evidence_ids"])
        citation_ids = {citation["evidence_id"] for citation in data["citations"]}
        if not result_ids.issubset(evidence_by_id):
            raise ContractSmokeError("result references evidence outside the input")
        if result_ids != citation_ids:
            raise ContractSmokeError("evidence_ids and citation IDs must match")
        for citation in data["citations"]:
            source = evidence_by_id.get(citation["evidence_id"])
            if source is None or citation["quote"] not in source:
                raise ContractSmokeError("citation quote is not an exact source substring")


def write_report(report: Mapping[str, Any], path: str | Path) -> None:
    """Write a sanitized runner report chosen by the caller."""

    Path(path).write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


__all__ = [
    "APPROVED_MODEL",
    "BatchLimits",
    "ContractSmokeError",
    "D3ContractSmokeRunner",
    "DEFAULT_MANIFEST_PATH",
    "PreparedCase",
    "build_case_payload",
    "write_report",
]
