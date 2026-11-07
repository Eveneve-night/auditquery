"""Validate the D5 administrator bootstrap v2 contract without runtime access."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONTRACT_PATH = PROJECT_ROOT / "configs" / "contracts" / "d5" / "admin-bootstrap.v2.json"
CONTRACT_SCHEMA_PATH = PROJECT_ROOT / "configs" / "schemas" / "d5" / "admin-bootstrap-contract.v2.schema.json"
API_SCHEMA_PATH = PROJECT_ROOT / "configs" / "schemas" / "d5" / "admin-bootstrap-api.v2.schema.json"
REPORT_PATH = PROJECT_ROOT / "tests" / "reports" / "d5-admin-bootstrap-contract-v2.json"
BASELINE_MAIN_SHA = "89951ba4eac05b874f7afa48d20bdbb60f87b070"
DEFAULT_PASSWORD_SHA256 = "292195d112d7ace728b19a2853dc2af4184521b7538a78336cc0830ad653ae0c"
REQUIRED_ALLOWED_ACTIONS = ["admin", "change_password", "logout"]
REQUIRED_FORBIDDEN_SINKS = {
    "log", "audit", "http_response", "frontend_state", "browser_storage",
    "cookie", "url", "screenshot", "machine_report", "backup_export",
}


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _canonical_bytes(path: Path) -> bytes:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")


def _artifact(path: Path) -> dict[str, Any]:
    payload = _canonical_bytes(path)
    return {
        "path": path.relative_to(PROJECT_ROOT).as_posix(),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "bytes": len(payload),
    }


def _stable_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _contains(value: Any, term: Any) -> bool:
    if value == term:
        return True
    if isinstance(value, str) and isinstance(term, str):
        return term in value
    if isinstance(value, dict):
        return any(_contains(child, term) for child in value.values())
    if isinstance(value, list):
        return any(_contains(child, term) for child in value)
    return False


def check_contract(contract: dict[str, Any], contract_schema: dict[str, Any], api_schema: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for item in Draft202012Validator(contract_schema).iter_errors(contract):
        # Schema messages can echo const values (including the authorized
        # initial password). Reports retain only a stable path/rule code.
        errors.append(f"contract_schema:{item.json_path}:{item.validator}")

    if contract_schema.get("$id") != "admin-bootstrap-contract.v2.schema.json":
        errors.append("contract_schema_id_mismatch")
    if api_schema.get("$id") != contract.get("api", {}).get("schema"):
        errors.append("api_schema_binding_mismatch")
    Draft202012Validator.check_schema(contract_schema)
    Draft202012Validator.check_schema(api_schema)

    credentials = contract.get("initial_credentials", {})
    password = credentials.get("initial_password", "")
    if hashlib.sha256(str(password).encode("utf-8")).hexdigest() != DEFAULT_PASSWORD_SHA256:
        errors.append("authorized_default_password_mismatch")
    if credentials.get("username") != "admin":
        errors.append("fixed_admin_username_mismatch")
    if credentials.get("create_only_when") != "auth_store_empty":
        errors.append("empty_store_guard_missing")
    if credentials.get("password_storage") != "argon2id_hash_only":
        errors.append("argon2id_hash_only_missing")
    if credentials.get("must_change_password") is not False:
        errors.append("forced_password_change_reintroduced")
    if credentials.get("password_change_policy") != "optional":
        errors.append("optional_password_change_policy_missing")

    lifecycle = contract.get("lifecycle", {})
    initialization = lifecycle.get("initialization", [])
    authenticated = lifecycle.get("authenticated_session", [])
    password_change = lifecycle.get("password_change", [])
    recovery = lifecycle.get("restart_and_recovery", [])
    for term, rules, code in (
        ("整体回滚", initialization, "initialization_rollback_missing"),
        ("可选择", authenticated, "optional_password_change_missing"),
        ("其他会话", password_change, "other_session_revocation_missing"),
        ("永久失效", password_change, "default_password_invalidation_missing"),
        ("不得恢复", recovery, "restart_recovery_guard_missing"),
    ):
        if not _contains(rules, term):
            errors.append(code)

    if set(contract.get("confidentiality", {}).get("forbidden_sinks", [])) != REQUIRED_FORBIDDEN_SINKS:
        errors.append("password_forbidden_sinks_mismatch")
    deployment = contract.get("deployment_guard", {})
    if deployment.get("default_bind") != "127.0.0.1" or not _contains(deployment.get("rules", []), "readiness"):
        errors.append("non_loopback_fail_closed_missing")

    defs = api_schema.get("$defs", {})
    required_defs = {"login_request", "admin_session_response", "password_change_request", "password_change_response"}
    if not required_defs.issubset(defs):
        errors.append("api_definitions_incomplete")
    else:
        response = defs["admin_session_response"]
        required = set(response.get("required", []))
        if not {"must_change_password", "password_change_available", "allowed_actions"}.issubset(required):
            errors.append("login_response_change_state_missing")
        response_properties = response.get("properties", {})
        if response_properties.get("must_change_password", {}).get("const") is not False:
            errors.append("login_response_forces_password_change")
        if response_properties.get("password_change_available", {}).get("const") is not True:
            errors.append("optional_password_change_availability_missing")
        if response_properties.get("allowed_actions", {}).get("const") != REQUIRED_ALLOWED_ACTIONS:
            errors.append("authenticated_actions_not_frozen")
        if defs["password_change_response"].get("properties", {}).get("other_sessions_revoked", {}).get("const") is not True:
            errors.append("password_change_revocation_response_missing")

    if contract.get("implementation_status") != "contract_only_b_and_c_runtime_pending":
        errors.append("implementation_status_overclaimed")
    return sorted(set(errors))


def _mutations() -> list[tuple[str, str]]:
    return [
        ("default_password_changed", "修改负责人批准的默认密码"),
        ("empty_store_guard_removed", "允许非空 AuthStore 重置管理员"),
        ("argon2id_removed", "移除 Argon2id-only 存储要求"),
        ("forced_change_reintroduced", "重新引入首次登录强制改密"),
        ("admin_action_removed", "从已认证会话移除普通管理操作"),
        ("session_revocation_removed", "改密后不撤销其他会话"),
        ("restart_reset_allowed", "允许重启恢复默认密码"),
        ("non_loopback_guard_removed", "移除非回环 readiness Gate"),
        ("report_sink_allowed", "允许机器报告保存口令"),
        ("status_overclaimed", "把契约冻结冒充运行时完成"),
    ]


def _apply_mutation(name: str, contract: dict[str, Any], api_schema: dict[str, Any]) -> None:
    if name == "default_password_changed":
        contract["initial_credentials"]["initial_password"] = "changed-value"
    elif name == "empty_store_guard_removed":
        contract["initial_credentials"]["create_only_when"] = "always"
    elif name == "argon2id_removed":
        contract["initial_credentials"]["password_storage"] = "plaintext"
    elif name == "forced_change_reintroduced":
        contract["initial_credentials"]["must_change_password"] = True
    elif name == "admin_action_removed":
        api_schema["$defs"]["admin_session_response"]["properties"]["allowed_actions"]["const"].remove("admin")
    elif name == "session_revocation_removed":
        contract["lifecycle"]["password_change"] = ["改密只更新密码哈希。"]
    elif name == "restart_reset_allowed":
        contract["lifecycle"]["restart_and_recovery"] = ["重启时重新初始化。"]
    elif name == "non_loopback_guard_removed":
        contract["deployment_guard"]["rules"] = ["允许任意监听地址。"]
    elif name == "report_sink_allowed":
        contract["confidentiality"]["forbidden_sinks"].remove("machine_report")
    elif name == "status_overclaimed":
        contract["implementation_status"] = "implemented"
    else:
        raise ValueError(f"unknown_mutation:{name}")


def validate_all() -> dict[str, Any]:
    contract = _load(CONTRACT_PATH)
    contract_schema = _load(CONTRACT_SCHEMA_PATH)
    api_schema = _load(API_SCHEMA_PATH)
    findings = check_contract(contract, contract_schema, api_schema)
    mutation_items: list[dict[str, Any]] = []
    for name, description in _mutations():
        mutated_contract = copy.deepcopy(contract)
        mutated_api = copy.deepcopy(api_schema)
        _apply_mutation(name, mutated_contract, mutated_api)
        mutation_findings = check_contract(mutated_contract, contract_schema, mutated_api)
        mutation_items.append({
            "case_id": name,
            "description": description,
            "detected": bool(mutation_findings),
            "sample_findings": mutation_findings[:3],
        })
    detected = sum(1 for item in mutation_items if item["detected"])
    report: dict[str, Any] = {
        "status": "pass" if not findings and detected == len(mutation_items) else "fail",
        "report_type": "d5_admin_bootstrap_contract_v2_gate",
        "task_id": "D5-AUTH-BASELINE-02",
        "owner": "A",
        "baseline_main_sha": BASELINE_MAIN_SHA,
        "scope": "contract_only_b_and_c_runtime_pending",
        "contract": {
            "contract_id": contract.get("contract_id"),
            "contract_version": contract.get("contract_version"),
            "username": contract.get("initial_credentials", {}).get("username"),
            "default_password_active": True,
            "must_change_password": contract.get("initial_credentials", {}).get("must_change_password"),
            "password_change_policy": contract.get("initial_credentials", {}).get("password_change_policy"),
        },
        "artifacts": [_artifact(path) for path in (CONTRACT_PATH, CONTRACT_SCHEMA_PATH, API_SCHEMA_PATH)],
        "findings": findings,
        "mutation_self_check": {"total": len(mutation_items), "detected": detected, "items": mutation_items},
        "boundaries": {
            "network_calls": 0,
            "model_api_calls": 0,
            "credential_files_read": 0,
            "validation_cases_read": 0,
            "hidden_test_cases_read": 0,
            "estimated_cost_cny": 0.0
        },
        "not_implemented": [
            "b_default_admin_runtime_and_optional_password_change",
            "b_non_loopback_readiness_gate",
            "c_optional_password_change_ui",
            "d5_05_packaging_and_clean_machine_e2e"
        ]
    }
    report["stable_sha256"] = _stable_hash({
        "contract": report["contract"],
        "artifacts": report["artifacts"],
        "findings": report["findings"],
        "mutation_self_check": report["mutation_self_check"],
        "not_implemented": report["not_implemented"],
    })
    serialized = json.dumps(report, ensure_ascii=False)
    if contract["initial_credentials"]["initial_password"] in serialized:
        raise RuntimeError("default_password_leaked_to_machine_report")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成 D5 默认管理员 v2 公共契约离线报告")
    parser.add_argument("--report", default=str(REPORT_PATH))
    args = parser.parse_args(argv)
    report = validate_all()
    path = Path(args.report)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
