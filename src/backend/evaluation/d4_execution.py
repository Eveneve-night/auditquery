"""Controlled D4-08 five-arm execution over an injected evaluation adapter.

The adapter owns private case material and deterministic scoring. This module
receives case IDs only, fixes each arm from the frozen protocol, and checks the
adapter's content-free receipts before allowing aggregate calculation. The
default entry is offline; an online adapter needs a separate explicit grant.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import re
from typing import Any, Mapping, Protocol

from .d4_runner import (
    D4EvaluationError, EvaluationCaseOutcome, EvaluationRunMetadata,
    GROUP_IDS, build_evaluation_result,
)


_DIGEST = re.compile(r"^[0-9a-f]{64}$")


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()


def _legacy_crlf_digest(content: bytes) -> str:
    """Hash the frozen D3 loop policy using its recorded CRLF convention."""

    normalized = content.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return hashlib.sha256(normalized.replace(b"\n", b"\r\n")).hexdigest()


@dataclass(frozen=True)
class ExecutionManifest:
    """Content-free identity of the shared cases, catalog, scorer and adapter."""

    case_ids: tuple[str, ...]
    data_version: str
    data_sha256: str
    candidate_catalog_sha256: str
    scorer_sha256: str
    adapter_id: str
    adapter_sha256: str
    split: str
    offline: bool = True
    authorization_id: str | None = None

    def __post_init__(self) -> None:
        if not self.case_ids or len(set(self.case_ids)) != len(self.case_ids):
            raise D4EvaluationError("manifest_case_ids_invalid")
        if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{2,95}", case_id) for case_id in self.case_ids):
            raise D4EvaluationError("manifest_case_ids_invalid")
        for name in ("data_sha256", "candidate_catalog_sha256", "scorer_sha256", "adapter_sha256"):
            if not _DIGEST.fullmatch(getattr(self, name)):
                raise D4EvaluationError(f"{name}_invalid")
        if not self.adapter_id or not self.data_version or self.split not in {"synthetic", "prompt_dev", "validation", "test"}:
            raise D4EvaluationError("manifest_identity_invalid")
        if not self.offline and not self.authorization_id:
            raise D4EvaluationError("online_authorization_required")


@dataclass(frozen=True)
class ArmControls:
    group_id: str
    candidate_catalog_sha256: str
    scorer_sha256: str
    budget_profile_id: str
    budget: Mapping[str, int | float]
    output_constraint: str
    enabled_components: tuple[str, ...]
    disabled_components: tuple[str, ...]
    uses_model: bool
    model_id: str | None

    @property
    def fingerprint(self) -> str:
        return _digest(self.__dict__)


@dataclass(frozen=True)
class ExecutionReceipt:
    outcome: EvaluationCaseOutcome
    control_fingerprint: str
    candidate_catalog_sha256: str
    scorer_sha256: str
    enabled_components: tuple[str, ...]
    disabled_components: tuple[str, ...]
    output_constraint: str
    model_id: str | None


class EvaluationAdapter(Protocol):
    adapter_id: str
    adapter_sha256: str
    offline: bool

    def run_case(self, case_id: str, controls: ArmControls) -> ExecutionReceipt:
        """Execute and score one case without returning question or answer text."""


def execute_matrix(
    manifest: ExecutionManifest,
    metadata: EvaluationRunMetadata,
    adapter: EvaluationAdapter,
    protocol_bytes: bytes,
    policy_bytes: bytes,
    *,
    protocol_sha256: str,
    allow_online: bool = False,
    bootstrap_samples: int = 2000,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run all arms under frozen controls and return aggregate plus audit sidecar."""

    if manifest.adapter_id != adapter.adapter_id or manifest.adapter_sha256 != adapter.adapter_sha256:
        raise D4EvaluationError("adapter_identity_mismatch")
    if manifest.data_version != metadata.data_version:
        raise D4EvaluationError("data_version_mismatch")
    if manifest.offline != adapter.offline:
        raise D4EvaluationError("adapter_mode_mismatch")
    if not manifest.offline and not allow_online:
        raise D4EvaluationError("online_execution_not_authorized")
    if manifest.split == "test":
        if metadata.hidden_case_count != len(manifest.case_ids):
            raise D4EvaluationError("hidden_case_count_mismatch")
    elif metadata.hidden_case_count:
        raise D4EvaluationError("hidden_count_on_non_test_split")
    normalized_protocol = protocol_bytes.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    if not _DIGEST.fullmatch(protocol_sha256) or hashlib.sha256(normalized_protocol).hexdigest() != protocol_sha256:
        raise D4EvaluationError("protocol_sha256_invalid")
    try:
        protocol = json.loads(protocol_bytes)
    except (ValueError, UnicodeDecodeError) as exc:
        raise D4EvaluationError("protocol_json_invalid") from exc
    if protocol.get("protocol_id") != "d4-typed-decision-experiment.v1":
        raise D4EvaluationError("protocol_id_mismatch")
    groups = protocol.get("groups")
    if not isinstance(groups, list) or [group.get("group_id") for group in groups] != list(GROUP_IDS):
        raise D4EvaluationError("protocol_groups_invalid")
    shared = protocol.get("shared_controls", {})
    if _legacy_crlf_digest(policy_bytes) != shared.get("policy_sha256"):
        raise D4EvaluationError("budget_policy_sha256_mismatch")
    try:
        policy_limits = json.loads(policy_bytes)["limits"]
    except (ValueError, UnicodeDecodeError, KeyError, TypeError) as exc:
        raise D4EvaluationError("budget_policy_invalid") from exc
    budget = shared.get("budget", {})
    expected_budget = {"max_model_calls", "max_input_tokens_total", "max_output_tokens_total", "max_wall_time_ms", "hard_cost_cny"}
    if set(budget) != expected_budget or shared.get("case_catalog") != "same_frozen_cases_and_candidate_catalog_for_all_groups" or shared.get("scorer") != "same_deterministic_scorer_for_all_groups":
        raise D4EvaluationError("shared_controls_invalid")
    if any(budget[name] != policy_limits.get(name) for name in expected_budget):
        raise D4EvaluationError("shared_budget_policy_mismatch")
    if not manifest.offline and protocol.get("model_boundary", {}).get("online_models") != ["qwen3.7-flash"]:
        raise D4EvaluationError("model_boundary_invalid")

    results: dict[str, tuple[EvaluationCaseOutcome, ...]] = {}
    fingerprints: dict[str, str] = {}
    for group in groups:
        group_id = group["group_id"]
        if group.get("budget_profile_id") != shared.get("budget_profile_id") or group.get("candidate_catalog") != "shared_frozen_catalog":
            raise D4EvaluationError("group_shared_controls_mismatch")
        controls = ArmControls(
            group_id=group_id,
            candidate_catalog_sha256=manifest.candidate_catalog_sha256,
            scorer_sha256=manifest.scorer_sha256,
            budget_profile_id=shared["budget_profile_id"],
            budget=budget,
            output_constraint=group["output_constraint"],
            enabled_components=tuple(group["enabled_components"]),
            disabled_components=tuple(group["disabled_components"]),
            uses_model=group["uses_model"],
            model_id="qwen3.7-flash" if group["uses_model"] else None,
        )
        if set(controls.enabled_components) & set(controls.disabled_components):
            raise D4EvaluationError("group_components_overlap")
        fingerprints[group_id] = controls.fingerprint
        arm: list[EvaluationCaseOutcome] = []
        for case_id in manifest.case_ids:
            receipt = adapter.run_case(case_id, controls)
            if not isinstance(receipt, ExecutionReceipt) or not isinstance(receipt.outcome, EvaluationCaseOutcome):
                raise D4EvaluationError("adapter_receipt_invalid")
            if receipt.outcome.case_id != case_id:
                raise D4EvaluationError("adapter_case_id_mismatch")
            for observed, expected in (
                (receipt.control_fingerprint, controls.fingerprint),
                (receipt.candidate_catalog_sha256, controls.candidate_catalog_sha256),
                (receipt.scorer_sha256, controls.scorer_sha256),
                (receipt.enabled_components, controls.enabled_components),
                (receipt.disabled_components, controls.disabled_components),
                (receipt.output_constraint, controls.output_constraint),
                (receipt.model_id, controls.model_id if receipt.outcome.model_calls else None),
            ):
                if observed != expected:
                    raise D4EvaluationError(f"adapter_controls_mismatch:{group_id}")
            outcome = receipt.outcome
            if not manifest.offline and controls.uses_model and not outcome.model_calls:
                raise D4EvaluationError("model_usage_missing")
            if (not controls.uses_model and (outcome.model_calls or outcome.total_tokens or outcome.cost_cny)):
                raise D4EvaluationError("rules_only_model_usage")
            if (outcome.model_calls > budget["max_model_calls"] or
                outcome.input_tokens > budget["max_input_tokens_total"] or
                outcome.output_tokens > budget["max_output_tokens_total"] or
                outcome.latency_ms > budget["max_wall_time_ms"] or
                outcome.cost_cny > budget["hard_cost_cny"]):
                raise D4EvaluationError("case_budget_exceeded")
            arm.append(outcome)
        results[group_id] = tuple(arm)
    if manifest.offline or manifest.split != "test" or metadata.hidden_case_count < 120:
        metadata = replace(metadata, evidence_reproducible=False)
    aggregate = build_evaluation_result(results, metadata, protocol_sha256=protocol_sha256, bootstrap_samples=bootstrap_samples)
    audit = {
        "run_id": metadata.run_id,
        "manifest_sha256": _digest(manifest.__dict__),
        "protocol_sha256": protocol_sha256,
        "adapter_id": manifest.adapter_id,
        "adapter_sha256": manifest.adapter_sha256,
        "split": manifest.split,
        "offline": manifest.offline,
        "case_count_per_group": len(manifest.case_ids),
        "candidate_catalog_sha256": manifest.candidate_catalog_sha256,
        "scorer_sha256": manifest.scorer_sha256,
        "budget_profile_id": shared["budget_profile_id"],
        "group_control_fingerprints": fingerprints,
        "aggregate_sha256": _digest(aggregate),
    }
    return aggregate, audit


__all__ = ["ArmControls", "EvaluationAdapter", "ExecutionManifest", "ExecutionReceipt", "execute_matrix"]
