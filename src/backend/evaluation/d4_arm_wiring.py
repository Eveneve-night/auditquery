"""Audited, fail-closed wiring for the one-case D4-08 public canary.

Concrete five-arm business routes and the independent scorer are injected.
This module never loads credentials, constructs prompts, or reads case labels.
It meters the *provider port*, not a route's self-reported usage.  A route's
source digest and component trace still require non-author review before a
formal adapter identity can be frozen.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import inspect
import json
import math
from pathlib import Path
from time import monotonic
from typing import Any, Callable, Mapping
from uuid import uuid4

from ..llm.bailian import BailianProvider
from ..llm.contracts import LLMProvider, ModelCallRequest, ModelCallResult
from ..llm.errors import ProviderError
from .d4_adapter import ArmObservation, ControlledEvaluationAdapter
from .d4_execution import ArmControls, ExecutionManifest, _legacy_crlf_digest
from .d4_runner import D4EvaluationError, GROUP_IDS


_MODEL_GROUPS = frozenset({"B0", "B1", "P", "P-uncalibrated"})
_PROVIDER_ID = "bailian-cn-beijing"
_MODEL_ID = "qwen3.7-flash"


class CanaryRunStopped(D4EvaluationError):
    """Fail-closed run with a content-free audit available to the caller."""

    def __init__(self, audit: Mapping[str, Any]) -> None:
        self.audit = dict(audit)
        super().__init__("public_canary_stopped")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_sha256(value: bytes) -> str:
    return _sha256(value.replace(b"\r\n", b"\n").replace(b"\r", b"\n"))


def _source_sha256(value: Any) -> str:
    try:
        source = inspect.getsource(value)
    except (OSError, TypeError):
        raise D4EvaluationError("arm_route_source_unavailable") from None
    return _sha256(source.replace("\r\n", "\n").encode("utf-8"))


def _module_sha256(value: Any) -> str:
    path = inspect.getsourcefile(value)
    if path is None:
        raise D4EvaluationError("arm_route_module_unavailable")
    try:
        return _canonical_sha256(Path(path).read_bytes())
    except OSError:
        raise D4EvaluationError("arm_route_module_unavailable") from None


@dataclass(frozen=True)
class ArmResult:
    output: Any
    output_constraint: str


@dataclass(frozen=True)
class ArmRoute:
    group_id: str
    route_id: str
    run: Callable[[Mapping[str, Any], "ArmContext"], ArmResult]
    calibrator: Callable[..., Any] | None = None
    dependencies: tuple[tuple[str, Callable[..., Any]], ...] = ()

    @property
    def source_sha256(self) -> str:
        return _source_sha256(self.run)


def _component_identity(name: str, operation: Callable[..., Any]) -> dict[str, str]:
    component_id = getattr(operation, "component_id", None)
    component_version = getattr(operation, "component_version", None)
    if (
        not isinstance(name, str) or not name.strip()
        or not callable(operation)
        or not isinstance(component_id, str) or not component_id.strip()
        or not isinstance(component_version, str) or not component_version.strip()
    ):
        raise D4EvaluationError("business_component_identity_missing")
    target = operation if inspect.isfunction(operation) or inspect.ismethod(operation) else type(operation)
    return {
        "component": name,
        "component_id": component_id,
        "component_version": component_version,
        "source_sha256": _source_sha256(target),
        "module_sha256": _module_sha256(target),
    }


class ProviderAttemptLedger:
    """One-case, four-attempt hard boundary with content-free accounting."""

    def __init__(self, provider: LLMProvider, *, max_attempts: int = 4, hard_cost_cny: float = 0.05) -> None:
        if provider.provider_id != _PROVIDER_ID or provider.model != _MODEL_ID:
            raise D4EvaluationError("canary_provider_identity_invalid")
        if max_attempts != 4 or hard_cost_cny != 0.05:
            raise D4EvaluationError("canary_budget_invalid")
        self._provider = provider
        self._attempts = {group_id: 0 for group_id in GROUP_IDS}
        self._input_tokens = {group_id: 0 for group_id in GROUP_IDS}
        self._output_tokens = {group_id: 0 for group_id in GROUP_IDS}
        self._cost_cny = {group_id: 0.0 for group_id in GROUP_IDS}
        self._errors: list[str] = []
        self._stop_reason: str | None = None
        self._usage_complete = True
        self._request_ids: set[str] = set()
        self.provider_module_sha256 = _module_sha256(type(provider))

    def _stop(self, reason: str) -> None:
        if self._stop_reason is None:
            self._stop_reason = reason

    def stop_local_failure(self) -> None:
        self._stop("arm_or_scorer_failed")

    def enforce_budget(self, budget: Mapping[str, int | float], elapsed_ms: float) -> None:
        if self._stop_reason is not None:
            return
        if elapsed_ms >= budget["max_wall_time_ms"]:
            self._stop("wall_time_cap_reached")
        elif sum(self._input_tokens.values()) >= budget["max_input_tokens_total"]:
            self._stop("input_token_cap_reached")
        elif sum(self._output_tokens.values()) >= budget["max_output_tokens_total"]:
            self._stop("output_token_cap_reached")

    def call(self, group_id: str, request: ModelCallRequest) -> ModelCallResult:
        if group_id not in _MODEL_GROUPS:
            raise D4EvaluationError("rules_only_provider_forbidden")
        if self._stop_reason is not None:
            raise D4EvaluationError("canary_already_stopped")
        if not isinstance(request, ModelCallRequest) or request.temperature != 0:
            self._stop("request_contract_invalid")
            raise D4EvaluationError("canary_request_invalid")
        # Stricter than the shared policy: keep the single canary request well
        # below the 30k-token input/5k-token output task budget. Actual billed
        # cost is still observed only after the Provider response.
        if (request.max_completion_tokens > 1500
                or sum(len(message.content.encode("utf-8")) for message in request.messages) > 20000):
            self._stop("request_budget_invalid")
            raise D4EvaluationError("canary_request_budget_invalid")
        if request.request_id in self._request_ids:
            self._stop("request_id_replayed")
            raise D4EvaluationError("request_id_replayed")
        if self._attempts[group_id] >= 1 or sum(self._attempts.values()) >= 4:
            self._stop("provider_attempt_cap_reached")
            raise D4EvaluationError("provider_attempt_cap_reached")
        if sum(self._cost_cny.values()) >= 0.05:
            self._stop("hard_cost_cap_reached")
            raise D4EvaluationError("hard_cost_cap_reached")
        # Reserve the attempt before I/O. Even a timeout or invalid response is
        # an attempt; this port never retries or silently changes provider.
        self._attempts[group_id] += 1
        self._request_ids.add(request.request_id)
        try:
            result = self._provider.generate_structured(request)
        except ProviderError as error:
            if (not math.isfinite(error.estimated_cost_cny) or error.estimated_cost_cny < 0
                    or any(isinstance(value, bool) or not isinstance(value, int) or value < 0
                           for value in (error.input_tokens, error.output_tokens))):
                self._usage_complete = False
                self._stop("provider_usage_invalid")
                raise D4EvaluationError("provider_usage_invalid") from None
            self._input_tokens[group_id] += error.input_tokens
            self._output_tokens[group_id] += error.output_tokens
            self._cost_cny[group_id] += error.estimated_cost_cny
            self._errors.append(error.code.value)
            self._stop(f"provider_error:{error.code.value}")
            raise D4EvaluationError("canary_provider_failed") from None
        except Exception:
            self._errors.append("unexpected_provider_error")
            self._usage_complete = False
            self._stop("unexpected_provider_error")
            raise D4EvaluationError("canary_provider_failed") from None
        if not isinstance(result, ModelCallResult):
            self._usage_complete = False
            self._stop("provider_result_invalid")
            raise D4EvaluationError("provider_result_invalid")
        if (any(isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in (result.usage.input_tokens, result.usage.output_tokens))
                or not math.isfinite(result.estimated_cost_cny) or result.estimated_cost_cny < 0
                or not math.isfinite(result.latency_ms) or result.latency_ms < 0):
            self._usage_complete = False
            self._stop("provider_usage_invalid")
            raise D4EvaluationError("provider_usage_invalid")
        # Account even when the response identity/attempt count is invalid.
        if isinstance(result.attempt_count, int) and not isinstance(result.attempt_count, bool) and result.attempt_count > 1:
            self._attempts[group_id] += result.attempt_count - 1
        self._input_tokens[group_id] += result.usage.input_tokens
        self._output_tokens[group_id] += result.usage.output_tokens
        self._cost_cny[group_id] += result.estimated_cost_cny
        if (type(result.attempt_count) is not int or result.attempt_count != 1 or result.provider_id != _PROVIDER_ID
                or result.requested_model != _MODEL_ID or result.returned_model != _MODEL_ID
                or result.request_id != request.request_id):
            self._stop("provider_result_identity_invalid")
            raise D4EvaluationError("provider_result_identity_invalid")
        if sum(self._cost_cny.values()) >= 0.05:
            self._stop("hard_cost_cap_reached")
        return result

    def group_usage(self, group_id: str) -> tuple[int, int, int, float]:
        return (self._attempts[group_id], self._input_tokens[group_id],
                self._output_tokens[group_id], self._cost_cny[group_id])

    def snapshot(self) -> dict[str, Any]:
        return {
            "provider_attempts": sum(self._attempts.values()),
            "group_attempts": dict(self._attempts),
            "input_tokens": sum(self._input_tokens.values()),
            "output_tokens": sum(self._output_tokens.values()),
            "estimated_cost_cny": round(sum(self._cost_cny.values()), 8),
            "usage_complete": self._usage_complete,
            "stop_reason": self._stop_reason or "completed",
            "stable_error_codes": tuple(self._errors),
        }


class ArmContext:
    def __init__(
        self,
        controls: ArmControls,
        ledger: ProviderAttemptLedger,
        calibrator: Callable[..., Any] | None,
        dependencies: Mapping[str, Callable[..., Any]] | None = None,
    ) -> None:
        self.controls = controls
        self._ledger = ledger
        self.calibrator = calibrator
        self._dependencies = dict(dependencies) if dependencies is not None else None
        self._applied: set[str] = set()

    @property
    def applied_components(self) -> frozenset[str]:
        return frozenset(self._applied)

    def apply(self, component: str, operation: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        if component not in self.controls.enabled_components or component == "llm_semantic_judgment":
            raise D4EvaluationError("disabled_component_invoked")
        if not callable(operation):
            raise D4EvaluationError("component_operation_invalid")
        if self._dependencies is not None and self._dependencies.get(component) is not operation:
            raise D4EvaluationError("business_component_identity_mismatch")
        if component == "empirical_calibration" and operation is not self.calibrator:
            raise D4EvaluationError("empirical_calibration_missing")
        value = operation(*args, **kwargs)
        self._applied.add(component)
        return value

    def model_call(self, request: ModelCallRequest) -> ModelCallResult:
        if not self.controls.uses_model or "llm_semantic_judgment" not in self.controls.enabled_components:
            raise D4EvaluationError("rules_only_provider_forbidden")
        result = self._ledger.call(self.controls.group_id, request)
        self._applied.add("llm_semantic_judgment")
        return result


class FiveArmExecutor:
    """Dispatch exact frozen groups; compute usage from the guarded provider port."""

    def __init__(self, routes: Mapping[str, ArmRoute], ledger: ProviderAttemptLedger) -> None:
        if set(routes) != set(GROUP_IDS) or any(routes[group].group_id != group for group in GROUP_IDS):
            raise D4EvaluationError("five_arm_routes_incomplete")
        if len({routes[group].route_id for group in GROUP_IDS}) != 5:
            raise D4EvaluationError("five_arm_route_ids_not_unique")
        if len({id(routes[group].run) for group in GROUP_IDS}) != 5:
            raise D4EvaluationError("five_arm_route_functions_not_unique")
        calibrator = routes["P"].calibrator
        if not (
            callable(calibrator) and getattr(calibrator, "empirically_calibrated", False) is True
            and isinstance(getattr(calibrator, "calibration_version", None), str)
            and calibrator.calibration_version.strip()
        ):
            raise D4EvaluationError("empirical_calibration_missing")
        if any(routes[group].calibrator is not None for group in GROUP_IDS if group != "P"):
            raise D4EvaluationError("calibration_on_non_proposed_arm")
        self._component_binding_required = any(route.dependencies for route in routes.values())
        if self._component_binding_required and not all(route.dependencies for route in routes.values()):
            raise D4EvaluationError("business_component_registry_incomplete")
        for route in routes.values():
            names = [name for name, _ in route.dependencies]
            if len(names) != len(set(names)):
                raise D4EvaluationError("business_component_identity_duplicate")
        self._routes = dict(routes)
        self.ledger = ledger
        # Fail before any model call if a route has no inspectable source.
        self.route_identities = {
            group: {
                "route_id": routes[group].route_id,
                "source_sha256": routes[group].source_sha256,
                "module_sha256": _module_sha256(routes[group].run),
                "calibration_version": (
                    routes[group].calibrator.calibration_version if group == "P" else None
                ),
                "calibrator_source_sha256": (
                    _source_sha256(type(routes[group].calibrator))
                    if group == "P" else None
                ),
                "calibrator_module_sha256": (
                    _module_sha256(type(routes[group].calibrator))
                    if group == "P" else None
                ),
                "dependencies": tuple(
                    _component_identity(name, operation)
                    for name, operation in routes[group].dependencies
                ),
            }
            for group in GROUP_IDS
        }

    def identity_candidate(
        self,
        manifest: ExecutionManifest,
        adapter: ControlledEvaluationAdapter,
    ) -> dict[str, Any]:
        """Build the content-free identity record that non-authors review.

        This is deliberately a candidate, not an approval or a formal
        manifest. A/C can reproduce and freeze its digest without receiving
        case text, expected answers, prompts, provider responses or secrets.
        """

        if (
            adapter.executor is not self
            or manifest.adapter_id != adapter.adapter_id
            or manifest.adapter_sha256 != adapter.adapter_sha256
            or manifest.offline != adapter.offline
        ):
            raise D4EvaluationError("adapter_identity_mismatch")
        routes = {
            group_id: {
                "route_id": self.route_identities[group_id]["route_id"],
                "route_source_sha256": self.route_identities[group_id]["source_sha256"],
                "route_module_sha256": self.route_identities[group_id]["module_sha256"],
                "calibration_version": self.route_identities[group_id]["calibration_version"],
                "calibrator_source_sha256": self.route_identities[group_id]["calibrator_source_sha256"],
                "calibrator_module_sha256": self.route_identities[group_id]["calibrator_module_sha256"],
                "dependencies": self.route_identities[group_id]["dependencies"],
            }
            for group_id in GROUP_IDS
        }
        candidate = {
            "identity_kind": "d4-08-five-arm-adapter-candidate.v1",
            "adapter_id": manifest.adapter_id,
            "adapter_sha256": manifest.adapter_sha256,
            "offline": manifest.offline,
            "data_version": manifest.data_version,
            "data_sha256": manifest.data_sha256,
            "candidate_catalog_sha256": manifest.candidate_catalog_sha256,
            "scorer_sha256": manifest.scorer_sha256,
            "provider_id": _PROVIDER_ID,
            "model_id": _MODEL_ID,
            "provider_module_sha256": self.ledger.provider_module_sha256,
            "groups": routes,
            "component_binding_complete": self._component_binding_required,
        }
        candidate["identity_sha256"] = _sha256(json.dumps(
            candidate, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
        ).encode("utf-8"))
        return candidate

    def execute(self, case_input: Mapping[str, Any], controls: ArmControls) -> ArmObservation:
        if controls.group_id not in self._routes:
            raise D4EvaluationError("arm_group_unknown")
        route = self._routes[controls.group_id]
        dependencies = dict(route.dependencies) if self._component_binding_required else None
        if dependencies is not None and set(dependencies) != (
            set(controls.enabled_components) - {"llm_semantic_judgment"}
        ):
            self.ledger.stop_local_failure()
            raise D4EvaluationError("business_component_set_mismatch")
        before = self.ledger.group_usage(controls.group_id)
        context = ArmContext(controls, self.ledger, route.calibrator, dependencies)
        started = monotonic()
        try:
            result = route.run(case_input, context)
        except D4EvaluationError:
            self.ledger.stop_local_failure()
            raise
        except Exception:
            self.ledger.stop_local_failure()
            raise D4EvaluationError("arm_route_failed") from None
        if not isinstance(result, ArmResult) or result.output_constraint != controls.output_constraint:
            self.ledger.stop_local_failure()
            raise D4EvaluationError("arm_output_constraint_mismatch")
        if context.applied_components != frozenset(controls.enabled_components):
            self.ledger.stop_local_failure()
            raise D4EvaluationError("arm_component_trace_mismatch")
        after = self.ledger.group_usage(controls.group_id)
        calls, input_tokens, output_tokens = (after[i] - before[i] for i in range(3))
        cost = after[3] - before[3]
        if controls.uses_model != (calls == 1):
            self.ledger.stop_local_failure()
            raise D4EvaluationError("arm_model_attempt_mismatch")
        return ArmObservation(
            output=result.output,
            control_fingerprint=controls.fingerprint,
            enabled_components=tuple(controls.enabled_components),
            disabled_components=tuple(controls.disabled_components),
            output_constraint=controls.output_constraint,
            model_id=_MODEL_ID if calls else None,
            model_calls=calls,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_cny=round(cost, 8),
            latency_ms=(monotonic() - started) * 1000,
        )


def run_public_canary(
    manifest: ExecutionManifest,
    adapter: ControlledEvaluationAdapter,
    executor: FiveArmExecutor,
    *,
    protocol_bytes: bytes,
    policy_bytes: bytes,
    control_bytes: bytes,
    protocol_sha256: str,
    control_sha256: str,
    allow_online: bool = False,
) -> dict[str, Any]:
    """Run one public case, returning an audit only (never accuracy metrics)."""

    if len(manifest.case_ids) != 1 or manifest.split != "prompt_dev":
        raise D4EvaluationError("public_canary_manifest_invalid")
    if not manifest.offline and (not allow_online or not manifest.authorization_id):
        raise D4EvaluationError("online_execution_not_authorized")
    if not manifest.offline and type(executor.ledger._provider) is not BailianProvider:
        raise D4EvaluationError("online_provider_adapter_invalid")
    # An authorization alone cannot attest that the injected arm functions,
    # empirical calibrator, source and scorer are the independently reviewed
    # implementations. A's frozen adapter identity manifest does not exist
    # yet, so this provisional wiring must never launch an online canary.
    if not manifest.offline:
        raise D4EvaluationError("online_adapter_identity_not_frozen")
    if (adapter.executor is not executor or manifest.offline != adapter.offline
            or manifest.adapter_id != adapter.adapter_id or manifest.adapter_sha256 != adapter.adapter_sha256):
        raise D4EvaluationError("adapter_identity_mismatch")
    if executor.ledger.snapshot()["provider_attempts"]:
        raise D4EvaluationError("canary_ledger_not_fresh")
    if _canonical_sha256(protocol_bytes) != protocol_sha256 or _canonical_sha256(control_bytes) != control_sha256:
        raise D4EvaluationError("canary_contract_hash_mismatch")
    try:
        protocol = json.loads(protocol_bytes)
        control = json.loads(control_bytes)
        policy = json.loads(policy_bytes)
    except (ValueError, UnicodeDecodeError):
        raise D4EvaluationError("canary_contract_invalid") from None
    if (_legacy_crlf_digest(policy_bytes) != protocol.get("shared_controls", {}).get("policy_sha256")
            or protocol.get("protocol_id") != "d4-typed-decision-experiment.v1"
            or protocol.get("model_boundary", {}).get("online_models") != [_MODEL_ID]
            or protocol.get("model_boundary", {}).get("provider_ids") != [_PROVIDER_ID]
            or control.get("control_id") != "d4-08-public-canary-control.v1"
            or control.get("batch_limits") != {
                "case_count": 1, "provider_attempt_cap": 4, "hard_cost_cny": 0.05,
                "validation_case_reads": 0, "hidden_test_case_reads": 0,
            }
            or control.get("provider_boundary", {}).get("model") != _MODEL_ID
            or control.get("provider_boundary", {}).get("provider_id") != _PROVIDER_ID
            or control.get("provider_boundary", {}).get("temperature") != 0
            or control.get("provider_boundary", {}).get("enable_thinking") is not False
            or policy.get("limits", {}).get("max_model_calls") != 4
            or any(protocol.get("shared_controls", {}).get("budget", {}).get(key) != policy.get("limits", {}).get(key)
                   for key in ("max_model_calls", "max_input_tokens_total", "max_output_tokens_total",
                               "max_wall_time_ms", "hard_cost_cny"))):
        raise D4EvaluationError("canary_control_invalid")
    groups = protocol.get("groups")
    caps = control.get("groups")
    if (not isinstance(groups, list) or not isinstance(caps, list)
            or not all(isinstance(group, dict) for group in groups)
            or not all(isinstance(cap, dict) for cap in caps)
            or [group.get("group_id") for group in groups] != list(GROUP_IDS)
            or [group.get("group_id") for group in caps] != list(GROUP_IDS)
            or any(cap.get("provider_attempt_cap") != int(cap["group_id"] in _MODEL_GROUPS) for cap in caps)):
        raise D4EvaluationError("canary_groups_invalid")
    shared = protocol["shared_controls"]
    run_id = uuid4().hex
    case_manifest_sha256 = _sha256(json.dumps(
        {"case_ids": manifest.case_ids, "data_version": manifest.data_version,
         "data_sha256": manifest.data_sha256, "split": manifest.split},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8"))
    started = monotonic()
    observations: dict[str, dict[str, Any]] = {}

    def audit() -> dict[str, Any]:
        return {
            "evidence_kind": "public_canary_wiring_only",
            "business_claims_allowed": False,
            "run_id": run_id,
            "case_manifest_sha256": case_manifest_sha256,
            "data_sha256": manifest.data_sha256,
            "adapter_id": manifest.adapter_id,
            "adapter_sha256": manifest.adapter_sha256,
            "provider_module_sha256": executor.ledger.provider_module_sha256,
            "protocol_sha256": protocol_sha256,
            "control_sha256": control_sha256,
            "groups_completed": tuple(observations),
            "group_observations": dict(observations),
            "elapsed_ms": round((monotonic() - started) * 1000, 3),
            **executor.ledger.snapshot(),
        }

    for group in groups:
        executor.ledger.enforce_budget(shared["budget"], (monotonic() - started) * 1000)
        if executor.ledger.snapshot()["stop_reason"] != "completed":
            break
        controls = ArmControls(
            group_id=group["group_id"],
            candidate_catalog_sha256=manifest.candidate_catalog_sha256,
            scorer_sha256=manifest.scorer_sha256,
            budget_profile_id=shared["budget_profile_id"],
            budget=shared["budget"],
            output_constraint=group["output_constraint"],
            enabled_components=tuple(group["enabled_components"]),
            disabled_components=tuple(group["disabled_components"]),
            uses_model=group["uses_model"],
            model_id=_MODEL_ID if group["uses_model"] else None,
        )
        try:
            receipt = adapter.run_case(manifest.case_ids[0], controls)
        except D4EvaluationError:
            executor.ledger.stop_local_failure()
            raise CanaryRunStopped(audit()) from None
        observations[group["group_id"]] = {
            "control_fingerprint": receipt.control_fingerprint,
            "route_id": executor.route_identities[group["group_id"]]["route_id"],
            "route_source_sha256": executor.route_identities[group["group_id"]]["source_sha256"],
            "route_module_sha256": executor.route_identities[group["group_id"]]["module_sha256"],
            "calibration_version": executor.route_identities[group["group_id"]]["calibration_version"],
            "calibrator_source_sha256": executor.route_identities[group["group_id"]]["calibrator_source_sha256"],
            "calibrator_module_sha256": executor.route_identities[group["group_id"]]["calibrator_module_sha256"],
        }
        executor.ledger.enforce_budget(shared["budget"], (monotonic() - started) * 1000)
    return audit()


__all__ = ["ArmContext", "ArmResult", "ArmRoute", "CanaryRunStopped", "FiveArmExecutor", "ProviderAttemptLedger", "run_public_canary"]
