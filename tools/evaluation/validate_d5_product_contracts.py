"""D5-00 独立部署产品层公共契约的离线冻结校验器。

本工具只验证版本化 Schema、权限矩阵、数据分类、Store 语义、PDF 生命周期
和威胁映射，不实现认证、密钥、上传、RAG、前端或部署运行时。
它不访问网络、模型、密钥、validation 或隐藏 test。
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONTRACT_PATH = PROJECT_ROOT / "configs" / "contracts" / "d5" / "product-layer.v1.json"
CONTRACT_SCHEMA_PATH = PROJECT_ROOT / "configs" / "schemas" / "d5" / "product-layer-contract.v1.schema.json"
API_SCHEMA_PATH = PROJECT_ROOT / "configs" / "schemas" / "d5" / "api-v3.schema.json"
EXAMPLES_PATH = PROJECT_ROOT / "configs" / "examples" / "d5" / "api-v3.examples.json"
REPORT_PATH = PROJECT_ROOT / "tests" / "reports" / "d5-product-contracts-v1.json"
D4_RESPONSE_DEFINITIONS_PATH = PROJECT_ROOT / "configs" / "schemas" / "d4-07" / "shared-api-definitions.v2.schema.json"
EXPECTED_D4_RESPONSE_REF = "../d4-07/shared-api-definitions.v2.schema.json#/$defs/online_response"
BASELINE_MAIN_SHA = "25e6d31060d175a24242897d5512f8b43e105ea1"

REQUIRED_ROLES = {"visitor", "admin", "bootstrap"}
REQUIRED_STORES = {"AuthStore", "MetadataStore", "DocumentStore", "SecretStore"}
REQUIRED_DATA_CLASSES = {"public", "internal", "sensitive", "secret"}
REQUIRED_ERRORS = {
    "authentication_required": 401,
    "forbidden": 403,
    "csrf_invalid": 403,
    "resource_not_found": 404,
    "operation_replayed": 409,
    "state_conflict": 409,
    "rate_limited": 429,
}
REQUIRED_AUDIT_FORBIDDEN = {
    "password",
    "cookie",
    "session_token",
    "csrf_token",
    "api_key",
    "master_key",
    "question_text",
    "answer_text",
    "database_row",
}
REQUIRED_PDF_ORDER = [
    "server_generated_id",
    "extension_and_content_type",
    "pdf_magic_bytes",
    "byte_limit",
    "sha256_and_dedup",
    "page_limit",
    "bounded_dual_parse",
    "index_candidate_validation",
]
BANNED_ANSWER_KEYS = {"expected", "gold", "label", "gold_sql", "expected_sql", "answer_derived"}
BANNED_ADMIN_PATH_TERMS = {"arbitrary-sql", "database-file", "model-config", "base-url", "safety-threshold"}


class D5ContractError(RuntimeError):
    """D5-00 冻结资产或语义无效。"""


def _canonical_bytes(path: Path) -> bytes:
    text = path.read_text(encoding="utf-8")
    return text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")


def _load(path: Path) -> Any:
    return json.loads(_canonical_bytes(path).decode("utf-8"))


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


def _duplicate_values(values: list[str]) -> set[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return duplicates


def _banned_keys(value: Any, path: str = "$") -> list[str]:
    findings: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if key.lower() in BANNED_ANSWER_KEYS:
                findings.append(child_path)
            findings.extend(_banned_keys(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            findings.extend(_banned_keys(child, f"{path}[{index}]"))
    return findings


def _schema_ref_exists(api_schema: dict[str, Any], ref: str | None) -> bool:
    if ref is None:
        return True
    prefix = "api-v3.schema.json#/$defs/"
    return ref.startswith(prefix) and ref[len(prefix) :] in api_schema.get("$defs", {})


def _validate_payload(api_schema: dict[str, Any], ref: str, payload: Any) -> list[str]:
    name = ref.rsplit("/", 1)[-1]
    if name not in api_schema.get("$defs", {}):
        return [f"schema_ref_not_found:{ref}"]
    root_validator = Draft202012Validator(api_schema)
    validator = root_validator.evolve(schema={"$ref": f"#/$defs/{name}"})
    return [f"{item.json_path}:{item.message}" for item in validator.iter_errors(payload)]


def check_contract(
    contract: dict[str, Any],
    contract_schema: dict[str, Any],
    api_schema: dict[str, Any],
    examples: dict[str, Any],
) -> list[str]:
    errors: list[str] = []

    for item in Draft202012Validator(contract_schema).iter_errors(contract):
        errors.append(f"contract_schema:{item.json_path}:{item.message}")

    if api_schema.get("$id") != "api-v3.schema.json":
        errors.append("api_schema_id_mismatch")
    if contract_schema.get("$id") != "product-layer-contract.v1.schema.json":
        errors.append("contract_schema_id_mismatch")
    if contract.get("api_schema") != api_schema.get("$id"):
        errors.append("api_schema_binding_mismatch")
    agent_payload = (
        api_schema.get("$defs", {})
        .get("agent_response", {})
        .get("properties", {})
        .get("payload", {})
    )
    refs = [item.get("$ref") for item in agent_payload.get("oneOf", [])]
    if EXPECTED_D4_RESPONSE_REF not in refs:
        errors.append("d4_v2_response_binding_missing")
    elif not D4_RESPONSE_DEFINITIONS_PATH.is_file():
        errors.append("d4_v2_response_schema_missing")
    else:
        d4_definitions = _load(D4_RESPONSE_DEFINITIONS_PATH)
        if "online_response" not in d4_definitions.get("$defs", {}):
            errors.append("d4_v2_response_ref_unresolved")

    roles = [item.get("id", "") for item in contract.get("roles", [])]
    if set(roles) != REQUIRED_ROLES:
        errors.append("role_set_mismatch")
    if _duplicate_values(roles):
        errors.append("role_id_duplicated")

    catalog = contract.get("error_catalog", [])
    codes = [item.get("code", "") for item in catalog]
    if _duplicate_values(codes):
        errors.append("error_code_duplicated")
    status_by_code = {item.get("code"): item.get("http_status") for item in catalog}
    for code, status in REQUIRED_ERRORS.items():
        if status_by_code.get(code) != status:
            errors.append(f"stable_error_mismatch:{code}")

    endpoints = contract.get("endpoints", [])
    endpoint_ids = [item.get("id", "") for item in endpoints]
    route_keys = [f"{item.get('method')} {item.get('path')}" for item in endpoints]
    if _duplicate_values(endpoint_ids):
        errors.append("endpoint_id_duplicated")
    if _duplicate_values(route_keys):
        errors.append("endpoint_route_duplicated")
    for endpoint in endpoints:
        endpoint_id = endpoint.get("id", "missing")
        path = endpoint.get("path", "")
        method = endpoint.get("method", "")
        endpoint_errors = set(endpoint.get("errors", []))
        for code in endpoint_errors:
            if code not in status_by_code:
                errors.append(f"endpoint_error_unknown:{endpoint_id}:{code}")
        for field in ("request_schema", "response_schema"):
            if not _schema_ref_exists(api_schema, endpoint.get(field)):
                errors.append(f"endpoint_schema_ref_unknown:{endpoint_id}:{field}")
        if path.startswith("/api/v3/admin/"):
            if endpoint.get("roles") != ["admin"] or endpoint.get("auth") != "admin_session":
                errors.append(f"admin_auth_not_enforced:{endpoint_id}")
            if not {"authentication_required", "forbidden"}.issubset(endpoint_errors):
                errors.append(f"admin_401_403_missing:{endpoint_id}")
            if method in {"POST", "PUT", "DELETE"} and endpoint.get("csrf") != "required":
                errors.append(f"admin_csrf_not_required:{endpoint_id}")
        if endpoint.get("ownership") == "own_session":
            expected_auth = (
                "anonymous"
                if endpoint_id == "create_visitor_session"
                else "visitor_session"
            )
            if endpoint.get("auth") != expected_auth:
                errors.append(f"visitor_session_auth_missing:{endpoint_id}")
            if "{session_id}" in path and "resource_not_found" not in endpoint_errors:
                errors.append(f"idor_uniform_not_found_missing:{endpoint_id}")
        lowered = path.lower()
        if any(term in lowered for term in BANNED_ADMIN_PATH_TERMS):
            errors.append(f"forbidden_admin_capability:{endpoint_id}")

    model = contract.get("model_boundary", {})
    if model != {
        "provider": "bailian_beijing",
        "model": "qwen3.7-flash",
        "fallback": False,
        "dynamic_routing": False,
        "admin_mutable": False,
    }:
        errors.append("single_model_boundary_changed")

    audit = contract.get("audit", {})
    forbidden = set(audit.get("forbidden_fields", []))
    allowed = set(audit.get("allowed_fields", []))
    if not REQUIRED_AUDIT_FORBIDDEN.issubset(forbidden):
        errors.append("audit_forbidden_fields_incomplete")
    if forbidden & allowed:
        errors.append("audit_allowlist_forbidden_overlap")

    data_classes = [item.get("id", "") for item in contract.get("data_classes", [])]
    if set(data_classes) != REQUIRED_DATA_CLASSES or _duplicate_values(data_classes):
        errors.append("data_class_set_mismatch")

    stores = contract.get("store_interfaces", [])
    store_ids = [item.get("id", "") for item in stores]
    if set(store_ids) != REQUIRED_STORES or _duplicate_values(store_ids):
        errors.append("store_interface_set_mismatch")
    for store in stores:
        combined = " ".join(
            str(store.get(field, ""))
            for field in ("transaction", "concurrency", "restart_recovery", "failure_semantics")
        )
        if not all(str(store.get(field, "")).strip() for field in ("transaction", "concurrency", "restart_recovery", "failure_semantics")):
            errors.append(f"store_semantics_incomplete:{store.get('id', 'missing')}")
        store_id = store.get("id")
        if store_id == "AuthStore" and not all(
            term in combined
            for term in ("单事务", "失败不得消费令牌", "会话世代", "并发初始化")
        ):
            errors.append("auth_store_atomicity_incomplete")
        if store_id == "DocumentStore" and not all(
            term in combined for term in ("原子指针", "旧索引", "拒绝覆盖")
        ):
            errors.append("document_store_atomicity_incomplete")
        if store_id == "SecretStore" and not all(term in combined for term in ("原子", "revision", "主密钥", "失败关闭")):
            errors.append("secret_store_atomicity_incomplete")

    pdf = contract.get("pdf_lifecycle", {})
    if pdf.get("validation_order") != REQUIRED_PDF_ORDER:
        errors.append("pdf_validation_order_mismatch")
    if "原子" not in str(pdf.get("publish", "")) or "旧索引" not in str(pdf.get("failure", "")):
        errors.append("pdf_atomic_publish_incomplete")
    limits = pdf.get("limits", {})
    if not all(isinstance(limits.get(key), int) and limits[key] > 0 for key in ("max_file_bytes", "max_pages", "parse_timeout_seconds")):
        errors.append("pdf_limits_invalid")

    threats = contract.get("threats", [])
    threat_ids = [item.get("id", "") for item in threats]
    if set(threat_ids) != {f"T-D5-{index:02d}" for index in range(1, 13)} or _duplicate_values(threat_ids):
        errors.append("threat_set_mismatch")
    owners = {item.get("test_owner") for item in threats}
    if not {"A", "B", "C", "B+C"}.issubset(owners):
        errors.append("threat_test_ownership_incomplete")

    key_status = api_schema.get("$defs", {}).get("key_status_response", {})
    if "api_key" in key_status.get("properties", {}):
        errors.append("api_key_plaintext_response_exposed")
    key_request = api_schema.get("$defs", {}).get("key_write_request", {})
    if "api_key" not in key_request.get("properties", {}):
        errors.append("api_key_write_field_missing")

    if _banned_keys({"contract": contract, "api_schema": api_schema, "examples": examples}):
        errors.append("answer_derived_field_present")

    if examples.get("example_set_version") != "1.0.0":
        errors.append("example_version_mismatch")
    for example in examples.get("examples", []):
        ref = example.get("schema_ref", "")
        payload_errors = _validate_payload(api_schema, ref, example.get("payload"))
        if payload_errors:
            errors.append(f"example_invalid:{example.get('id', 'missing')}")
    return sorted(set(errors))


def _mutations() -> list[tuple[str, str]]:
    return [
        ("admin_auth_removed", "移除管理端点的服务端管理员会话要求"),
        ("admin_csrf_removed", "移除管理写操作 CSRF 要求"),
        ("key_plaintext_exposed", "把 API Key 明文加入状态响应"),
        ("model_made_mutable", "允许管理员修改固定模型"),
        ("pdf_magic_removed", "跳过 PDF magic bytes 校验"),
        ("audit_password_allowed", "允许密码进入审计字段"),
        ("store_atomicity_removed", "删除 SecretStore 原子替换语义"),
        ("auth_atomicity_removed", "删除会话初始化失败回滚语义"),
        ("document_atomicity_removed", "删除文档索引原子发布语义"),
        ("endpoint_schema_dangling", "制造悬空公共 Schema 引用"),
        ("answer_field_added", "向公共样例加入 expected 答案派生字段"),
    ]


def _apply_mutation(name: str, contract: dict[str, Any], api_schema: dict[str, Any], examples: dict[str, Any]) -> None:
    if name == "admin_auth_removed":
        contract["endpoints"][8]["auth"] = "visitor_session"
    elif name == "admin_csrf_removed":
        contract["endpoints"][8]["csrf"] = "not_applicable"
    elif name == "key_plaintext_exposed":
        api_schema["$defs"]["key_status_response"]["properties"]["api_key"] = {"type": "string"}
    elif name == "model_made_mutable":
        contract["model_boundary"]["admin_mutable"] = True
    elif name == "pdf_magic_removed":
        contract["pdf_lifecycle"]["validation_order"].remove("pdf_magic_bytes")
    elif name == "audit_password_allowed":
        contract["audit"]["forbidden_fields"].remove("password")
        contract["audit"]["allowed_fields"].append("password")
    elif name == "store_atomicity_removed":
        secret = next(item for item in contract["store_interfaces"] if item["id"] == "SecretStore")
        secret["transaction"] = "写入后再更新元数据。"
    elif name == "auth_atomicity_removed":
        auth = next(item for item in contract["store_interfaces"] if item["id"] == "AuthStore")
        auth["transaction"] = "初始化分两步写入。"
    elif name == "document_atomicity_removed":
        document = next(item for item in contract["store_interfaces"] if item["id"] == "DocumentStore")
        document["transaction"] = "直接覆盖当前索引。"
    elif name == "endpoint_schema_dangling":
        contract["endpoints"][0]["response_schema"] = "api-v3.schema.json#/$defs/missing"
    elif name == "answer_field_added":
        examples["examples"][0]["payload"]["expected"] = "leak"
    else:
        raise D5ContractError(f"unknown_mutation:{name}")


def validate_all() -> dict[str, Any]:
    contract = _load(CONTRACT_PATH)
    contract_schema = _load(CONTRACT_SCHEMA_PATH)
    api_schema = _load(API_SCHEMA_PATH)
    examples = _load(EXAMPLES_PATH)
    findings = check_contract(contract, contract_schema, api_schema, examples)

    mutation_items: list[dict[str, Any]] = []
    for name, description in _mutations():
        mutated_contract = copy.deepcopy(contract)
        mutated_api = copy.deepcopy(api_schema)
        mutated_examples = copy.deepcopy(examples)
        _apply_mutation(name, mutated_contract, mutated_api, mutated_examples)
        mutation_findings = check_contract(mutated_contract, contract_schema, mutated_api, mutated_examples)
        mutation_items.append({
            "case_id": name,
            "description": description,
            "detected": bool(mutation_findings),
            "sample_findings": mutation_findings[:3],
        })
    detected = sum(1 for item in mutation_items if item["detected"])
    all_detected = detected == len(mutation_items)

    artifacts = [_artifact(path) for path in (CONTRACT_PATH, CONTRACT_SCHEMA_PATH, API_SCHEMA_PATH, EXAMPLES_PATH)]
    report: dict[str, Any] = {
        "status": "pass" if not findings and all_detected else "fail",
        "report_type": "d5_product_contract_freeze_gate",
        "task_id": "D5-00",
        "owner": "A",
        "baseline_main_sha": BASELINE_MAIN_SHA,
        "baseline_scope": "contract_only_not_runtime_implementation",
        "contract": {
            "contract_id": contract.get("contract_id"),
            "contract_version": contract.get("contract_version"),
            "api_version": contract.get("api_version"),
            "endpoint_count": len(contract.get("endpoints", [])),
            "error_code_count": len(contract.get("error_catalog", [])),
            "threat_count": len(contract.get("threats", [])),
            "store_interface_count": len(contract.get("store_interfaces", [])),
        },
        "artifacts": artifacts,
        "findings": findings,
        "mutation_self_check": {"total": len(mutation_items), "detected": detected, "all_detected": all_detected, "items": mutation_items},
        "boundaries": {
            "network_calls": 0,
            "model_api_calls": 0,
            "credential_files_read": 0,
            "estimated_cost_cny": 0.0,
            "validation_cases_read": 0,
            "hidden_test_cases_read": 0,
            "sql_executions": 0,
        },
        "not_implemented": [
            "b_auth_rbac_audit_runtime",
            "b_secret_store_runtime",
            "c_pdf_upload_and_index_runtime",
            "c_dual_role_frontend",
            "independent_packaging_and_restart_recovery",
            "security_e2e_and_product_release_gate",
            "business_accuracy_or_product_availability",
        ],
        "required_non_author_reviews": {
            "B": ["admin_auth_session_csrf_rate_limit_semantics", "store_atomicity_and_secret_boundary", "stable_error_mapping"],
            "C": ["pdf_lifecycle_and_upload_limits", "frontend_consumable_schema_and_redaction", "threat_test_ownership"]
        },
    }
    report["stable_sha256"] = _stable_hash({
        "contract": report["contract"],
        "artifacts": report["artifacts"],
        "findings": report["findings"],
        "mutation_self_check": report["mutation_self_check"],
        "not_implemented": report["not_implemented"],
    })
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成 D5-00 产品层公共契约冻结报告")
    parser.add_argument("--report", default=str(REPORT_PATH))
    args = parser.parse_args(argv)
    report = validate_all()
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
