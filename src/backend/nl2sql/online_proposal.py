"""D4-07 B-side online SQL proposal adapter.

The adapter is deliberately separated from execution.  It sends one bounded
NL2SQL request through an injected reliability gateway, validates the frozen
NL2SQL v1 result, and returns a ``SqlProposal``.  D4-05 remains the only owner
of the SafetyGate/ReadonlyExecutor handoff; a successful model result never
grants execution authority.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Mapping, Protocol, runtime_checkable

from ..agent.clarification_recovery import SqlProposal
from ..agent.domain_pack import LoadedDomainPack
from ..agent.intent_routing import validate_conversation_state
from ..agent.selective_routing import SlotObservation
from ..llm.contracts import ModelCallRequest, ModelCallResult, ModelMessage
from ..llm.errors import ProviderError
from ..llm.usage import UsageLedger
from .contract import (
    NL2SQL_OUTPUT_SCHEMA_ID,
    NL2SQL_PROMPT_VERSION,
    load_contract,
    load_prompt,
    validate_model_output,
)
from .schema_catalog import single_table_catalog


MODEL_ID = "qwen3.7-flash"
PROVIDER_ID = "bailian-cn-beijing"
TASK_TYPE = "nl2sql"
MAX_QUERY_CHARS = 2000
MAX_SLOT_COUNT = 32


class OnlineSqlProposalError(RuntimeError):
    """Stable content-safe failure raised by the online proposal boundary."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@runtime_checkable
class StructuredGateway(Protocol):
    def generate_structured(self, request: ModelCallRequest) -> ModelCallResult: ...


@dataclass(frozen=True)
class ProposalTelemetry:
    request_id: str
    provider_id: str
    returned_model: str
    attempt_count: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    estimated_cost_cny: float
    latency_ms: float
    proposal_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "provider_id": self.provider_id,
            "returned_model": self.returned_model,
            "attempt_count": self.attempt_count,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "estimated_cost_cny": self.estimated_cost_cny,
            "latency_ms": self.latency_ms,
            "proposal_sha256": self.proposal_sha256,
        }


def _proposal_sha256(sql: str, tables: tuple[str, ...], params: tuple[Any, ...]) -> str:
    encoded = json.dumps(
        {"sql": sql, "tables": list(tables), "params": list(params)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _slot_payload(
    observations: Mapping[str, SlotObservation],
    *,
    model_evidence: Mapping[str, str],
) -> dict[str, Any]:
    if len(observations) > MAX_SLOT_COUNT:
        raise OnlineSqlProposalError("too_many_slot_observations")
    payload: dict[str, Any] = {}
    for slot_id in sorted(observations):
        observation = observations[slot_id]
        if not isinstance(slot_id, str) or not isinstance(observation, SlotObservation):
            raise OnlineSqlProposalError("invalid_slot_observations")
        if not observation.evidence_refs or any(
            model_evidence.get(reference) != slot_id
            for reference in observation.evidence_refs
        ):
            raise OnlineSqlProposalError("slot_value_not_authorized_for_model")
        payload[slot_id] = {
            "values": list(observation.values),
            "evidence_refs": list(observation.evidence_refs),
        }
    return payload


class OnlineSqlProposalPort:
    """Generate one strictly validated proposal for one preselected table."""

    def __init__(
        self,
        gateway: StructuredGateway,
        *,
        table_name: str | None,
        domain_pack: LoadedDomainPack,
    ) -> None:
        if not isinstance(gateway, StructuredGateway):
            raise TypeError("gateway must implement generate_structured")
        if table_name is not None and (not isinstance(table_name, str) or not table_name):
            raise ValueError("table_name is required")
        try:
            self.catalog = single_table_catalog(table_name) if table_name else None
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("unknown table_name") from exc
        self.gateway = gateway
        self.table_name = table_name
        if not isinstance(domain_pack, LoadedDomainPack):
            raise TypeError("domain_pack must be a LoadedDomainPack")
        self.domain_pack = domain_pack
        self.model_evidence = {
            str(item["evidence_id"]): str(item["slot_id"])
            for item in domain_pack.artifact_items("value_evidence")
            if item.get("send_to_model") is True
            and item.get("data_level") in {"L0", "L1"}
        }
        self.last_telemetry: ProposalTelemetry | None = None

    def _selected_table(self, observations: Mapping[str, SlotObservation]) -> str:
        entity = observations.get("slot.entity_type")
        if (not isinstance(entity, SlotObservation) or len(entity.values) != 1
                or not isinstance(entity.values[0], str)):
            raise OnlineSqlProposalError("sql_proposal_table_mismatch")
        normalized = entity.values[0].replace("_", "").casefold()
        tables = {
            str(item["physical_name"])
            for item in self.domain_pack.artifact_items("schema_catalog")
            if item["object_type"] == "table"
            and str(item["physical_name"]).casefold() == normalized
        }
        if len(tables) != 1:
            raise OnlineSqlProposalError("sql_proposal_table_mismatch")
        selected = tables.pop()
        if self.table_name is not None and self.table_name != selected:
            raise OnlineSqlProposalError("sql_proposal_table_mismatch")
        return selected

    def _capture_usage(self, request_id: str, cursor: int, *, result=None, error=None) -> None:
        ledger = getattr(self.gateway, "usage_ledger", None)
        summary = ledger.summary(request_id=request_id, start_index=cursor) if isinstance(ledger, UsageLedger) else {}
        if summary.get("calls", 0) or (isinstance(ledger, UsageLedger) and result is None):
            # Provider bookkeeping also records local credential/configuration
            # failures. Keep their usage, but do not label them model calls.
            calls = sum(
                record.error_code not in {"configuration_error", "circuit_open", "policy_limit"}
                for record in ledger.records()[cursor:]
                if record.request_id == request_id
            )
            input_tokens, output_tokens, total_tokens = (
                int(summary[key]) for key in ("input_tokens", "output_tokens", "total_tokens")
            )
            cost, latency = float(summary["estimated_cost_cny"]), float(summary["latency_total_ms"])
        elif result is not None:
            calls = result.attempt_count
            input_tokens, output_tokens, total_tokens = (
                result.usage.input_tokens, result.usage.output_tokens, result.usage.total_tokens
            )
            cost, latency = result.estimated_cost_cny, result.latency_ms
        else:
            current = error
            while current is not None and not isinstance(current, ProviderError):
                current = current.__cause__
            if current is None:
                return
            # A gateway may stop before contacting the provider. The ledger,
            # when present, remains authoritative about actual attempts.
            calls = 0 if current.code.value in {"circuit_open", "policy_limit", "configuration_error"} else 1
            input_tokens, output_tokens, total_tokens = current.input_tokens, current.output_tokens, current.total_tokens
            cost, latency = current.estimated_cost_cny, 0.0
        self.last_telemetry = ProposalTelemetry(
            request_id, PROVIDER_ID, MODEL_ID, calls, input_tokens, output_tokens,
            total_tokens, cost, latency, "",
        )

    def propose(
        self,
        *,
        conversation_state: Mapping[str, Any],
        slot_observations: Mapping[str, SlotObservation],
    ) -> SqlProposal:
        # Publish an explicit zero-attempt receipt for local validation failures.
        # Otherwise the API's generic proposal error fallback can invent a call
        # before the gateway has been reached (e.g. mismatched entity/table).
        self.last_telemetry = ProposalTelemetry(
            "", PROVIDER_ID, MODEL_ID, 0, 0, 0, 0, 0.0, 0.0, ""
        )
        try:
            validate_conversation_state(conversation_state)
        except Exception as exc:
            raise OnlineSqlProposalError("invalid_conversation_state") from exc
        route = conversation_state.get("route")
        if not isinstance(route, Mapping) or route.get("intent") != "sql":
            raise OnlineSqlProposalError("sql_route_required")
        current_request = conversation_state.get("current_request")
        if not isinstance(current_request, Mapping):
            raise OnlineSqlProposalError("invalid_conversation_state")
        query = current_request.get("query")
        if not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY_CHARS:
            raise OnlineSqlProposalError("invalid_query")
        request_id = current_request.get("request_id")
        if not isinstance(request_id, str) or not request_id.strip():
            request_id = str(conversation_state.get("session_id", "request"))

        contract = load_contract()
        resolved_slots = _slot_payload(
            slot_observations, model_evidence=self.model_evidence
        )
        table_name = self._selected_table(slot_observations)
        catalog = self.catalog if self.catalog is not None else single_table_catalog(table_name)
        payload = {
            "query": query,
            "dialect": "sqlite",
            "database_schema": {
                "table": table_name,
                "description": catalog["description"],
                "columns": catalog["columns"],
            },
            "resolved_slots": resolved_slots,
        }
        model_request = ModelCallRequest(
            request_id=f"{request_id}.nl2sql",
            task_type=TASK_TYPE,
            messages=(
                ModelMessage("system", load_prompt()),
                ModelMessage(
                    "user",
                    json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                ),
            ),
            prompt_version=NL2SQL_PROMPT_VERSION,
            output_schema_id=NL2SQL_OUTPUT_SCHEMA_ID,
            max_completion_tokens=int(contract["generation"]["max_completion_tokens"]),
            temperature=float(contract["generation"]["temperature"]),
            metadata={"component": "d4-07-online-sql-proposal"},
        )
        ledger = getattr(self.gateway, "usage_ledger", None)
        cursor = ledger.cursor() if isinstance(ledger, UsageLedger) else 0
        try:
            result = self.gateway.generate_structured(model_request)
        except Exception as exc:
            self._capture_usage(model_request.request_id, cursor, error=exc)
            raise OnlineSqlProposalError("sql_proposal_gateway_failed") from exc
        # Account for returned/billable responses before all downstream model,
        # schema, declined-result and table validation branches.
        self._capture_usage(model_request.request_id, cursor, result=result)
        if (
            result.request_id != model_request.request_id
            or result.task_type != TASK_TYPE
            or result.provider_id != PROVIDER_ID
            or result.requested_model != MODEL_ID
            or result.returned_model != MODEL_ID
        ):
            raise OnlineSqlProposalError("sql_proposal_model_contract_mismatch")
        try:
            validated = validate_model_output(result.parsed_output)
        except Exception as exc:
            raise OnlineSqlProposalError("sql_proposal_schema_invalid") from exc
        if validated.rejected:
            raise OnlineSqlProposalError("sql_proposal_declined")
        if validated.tables != (table_name,):
            raise OnlineSqlProposalError("sql_proposal_table_mismatch")

        proposal_hash = _proposal_sha256(
            validated.candidate_sql, validated.tables, validated.params
        )
        proposal = SqlProposal(
            proposal_id=f"proposal.{proposal_hash[:24]}",
            sql=validated.candidate_sql,
            tables=validated.tables,
            params=validated.params,
        )
        measured = self.last_telemetry
        self.last_telemetry = ProposalTelemetry(
            request_id=result.request_id,
            provider_id=result.provider_id,
            returned_model=result.returned_model,
            attempt_count=measured.attempt_count,
            input_tokens=measured.input_tokens,
            output_tokens=measured.output_tokens,
            total_tokens=measured.total_tokens,
            estimated_cost_cny=measured.estimated_cost_cny,
            latency_ms=measured.latency_ms,
            proposal_sha256=proposal_hash,
        )
        return proposal


__all__ = [
    "MODEL_ID",
    "PROVIDER_ID",
    "OnlineSqlProposalError",
    "OnlineSqlProposalPort",
    "ProposalTelemetry",
    "StructuredGateway",
]
