"""D4-07 A 侧在线公共契约（v1/v2）离线收口校验器。

只做 A 侧公共契约收口，不做业务实现：

- 冻结资产的清单、字节数与 SHA-256；
- `schema_version` / `contract_version` 与文件版本的一致性；
- 机器规则 `clarification-binding.v2.json` 与运行时 `online_contracts.py` 的错误码漂移；
- 公开假样例自身的绑定一致性（六字段回传、单值、单证据）；
- 变异自检：证明上述检查真的会失败，而不是空转通过。

本工具不调用模型、不读取密钥、不读取 validation 或隐藏 test。
"""

from __future__ import annotations

import argparse
import ast
import copy
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

from backend.api.online_contracts import (  # noqa: E402
    OnlineContractError,
    OnlineContractValidator,
)

REPORT_PATH = PROJECT_ROOT / "tests" / "reports" / "d4-online-contract-v2.json"
SCHEMA_ROOT = PROJECT_ROOT / "configs" / "schemas" / "d4-07"
CONTRACT_PATH = (
    PROJECT_ROOT / "configs" / "contracts" / "d4-07" / "clarification-binding.v2.json"
)
EXAMPLES_DIR = PROJECT_ROOT / "configs" / "examples" / "d4-07"
RUNTIME_SOURCE = PROJECT_ROOT / "src" / "backend" / "api" / "online_contracts.py"
V2_CLARIFICATION_METHOD = "_validate_v2_clarification"

EXPECTED_CONTRACT_VERSION = "2.0.0"
EXPECTED_SCHEMA_FILES = (
    "online-request.v1.schema.json",
    "online-response.v1.schema.json",
    "online-request.v2.schema.json",
    "online-response.v2.schema.json",
    "shared-api-definitions.v1.schema.json",
    "shared-api-definitions.v2.schema.json",
)
EXPECTED_BINDING_FIELDS = (
    "session_id",
    "request_id",
    "turn_id",
    "state_revision",
    "question_id",
    "slot_id",
)
# (文件名, 用 request 还是 response 校验器真验)
TRANSPORT_EXAMPLES = (
    ("online-query.v2.valid.json", "request"),
    ("online-clarification-required.v2.valid.json", "response"),
)


class OnlineContractFreezeError(RuntimeError):
    """冻结契约校验失败。"""


def _canonical_text_bytes(path: Path) -> bytes:
    """返回跨平台一致的 UTF-8/LF 表示，避免报告哈希随平台漂移。"""

    text = path.read_text(encoding="utf-8")
    return text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")


def _load_json(path: Path) -> Any:
    return json.loads(_canonical_text_bytes(path).decode("utf-8"))


def _artifact(path: Path) -> dict[str, Any]:
    payload = _canonical_text_bytes(path)
    return {
        "path": path.relative_to(PROJECT_ROOT).as_posix(),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "bytes": len(payload),
    }


def _hash(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _schema_ids_match(schema_paths: list[Path]) -> bool:
    """每个 Schema 的 $id 必须等于其文件名，否则说明文件被改名但引用没跟上。"""

    for path in schema_paths:
        if _load_json(path).get("$id") != path.name:
            return False
    return True


def _runtime_raise_codes(source: str) -> set[str]:
    """从整个运行时文件提取真实 raise 的字符串错误码。

    只认 `raise OnlineContractError("code", ...)` 这类静态字符串字面量错误码，
    不把参数变量、消息文本或无关字符串常量当错误码。这样一旦某个声明错误码
    从真实 raise 中删除（即使字符串仍残留在其他常量里），`error_code_missing_in_runtime`
    仍会正确触发，避免漂移检查空转漏检。
    """

    tree = ast.parse(source)
    codes: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Raise) or node.exc is None:
            continue
        exc = node.exc
        if not isinstance(exc, ast.Call):
            continue
        func = exc.func
        if not (isinstance(func, ast.Name) and func.id == "OnlineContractError"):
            continue
        for arg in exc.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                codes.add(arg.value)
                break
    return codes


def _runtime_check_order(source: str) -> list[str]:
    """按源码顺序列出 v2 澄清校验抛出的稳定错误码。"""

    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == V2_CLARIFICATION_METHOD:
            order: list[str] = []
            for child in ast.walk(node):
                if not isinstance(child, ast.Raise) or child.exc is None:
                    continue
                exc = child.exc
                if not isinstance(exc, ast.Call):
                    continue
                for arg in exc.args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        order.append(arg.value)
            return order
    raise OnlineContractFreezeError("v2_clarification_method_not_found")


def _check_contract(
    contract: dict[str, Any],
    *,
    schema_names: set[str],
    runtime_codes: set[str],
    examples: dict[str, Any],
    validator: OnlineContractValidator,
) -> dict[str, Any]:
    errors: list[str] = []

    version = str(contract.get("contract_version", ""))
    if version != EXPECTED_CONTRACT_VERSION:
        errors.append(f"contract_version_mismatch:{version or 'missing'}")

    for key in ("request_schema", "response_schema"):
        name = str(contract.get(key, ""))
        if name not in schema_names:
            errors.append(f"{key}_not_found:{name or 'missing'}")

    echo_fields = [str(item) for item in contract.get("immutable_echo_fields", [])]
    client_fields = [str(item) for item in contract.get("client_generated_fields", [])]
    if set(echo_fields) & set(client_fields):
        errors.append("echo_and_client_fields_overlap")
    for field in EXPECTED_BINDING_FIELDS:
        if field not in echo_fields:
            errors.append(f"immutable_echo_field_missing:{field}")

    selection = contract.get("selection_contract") or {}
    if selection.get("value_count") != 1:
        errors.append(f"selection_value_count_invalid:{selection.get('value_count')}")
    if selection.get("evidence_ref_count") != 1:
        errors.append(
            f"selection_evidence_ref_count_invalid:{selection.get('evidence_ref_count')}"
        )

    gates = contract.get("runtime_gates") or []
    if not gates:
        errors.append("runtime_gates_empty")
    declared_codes: list[str] = []
    gate_order: list[str] = []
    for gate in gates:
        code = str(gate.get("error_code", ""))
        rule_id = str(gate.get("rule_id", ""))
        if not code or not rule_id:
            errors.append("runtime_gate_field_incomplete")
            continue
        gate_order.append(rule_id)
        declared_codes.append(code)
        if code not in runtime_codes:
            errors.append(f"error_code_missing_in_runtime:{code}")
    if len(set(declared_codes)) != len(declared_codes):
        errors.append("runtime_gate_error_code_duplicated")

    example_errors = _check_examples(examples, set(declared_codes), validator)
    errors.extend(example_errors)

    return {
        "errors": sorted(set(errors)),
        "declared_error_codes": declared_codes,
        "declared_gate_order": gate_order,
        "gate_count": len(gates),
    }


def _mutate(mapping: dict[str, Any], field: str, replacement: Any) -> dict[str, Any]:
    mutated = copy.deepcopy(mapping)
    mutated[field] = replacement
    return mutated


def _schema_rejects(validator: OnlineContractValidator, payload: Any) -> bool:
    try:
        validator.validate_request(payload)
    except OnlineContractError:
        return True
    return False


def _response_rejects(validator: OnlineContractValidator, payload: Any) -> bool:
    try:
        validator.validate_response(payload)
    except OnlineContractError:
        return True
    return False


def _load_transport_examples() -> dict[str, Any]:
    return {
        filename: _load_json(EXAMPLES_DIR / filename)
        for filename, _ in TRANSPORT_EXAMPLES
    }


def _check_transport_examples(
    validator: OnlineContractValidator, transport: dict[str, Any]
) -> list[str]:
    """用冻结 Schema 真验公开传输样例：请求样例必须过请求 Schema，响应样例必须过响应 Schema。"""

    errors: list[str] = []
    for filename, kind in TRANSPORT_EXAMPLES:
        payload = transport.get(filename)
        if payload is None:
            errors.append(f"transport_example_missing:{filename}")
            continue
        rejected = (
            _schema_rejects(validator, payload)
            if kind == "request"
            else _response_rejects(validator, payload)
        )
        if rejected:
            errors.append(f"transport_example_invalid:{filename}")
    return errors


def _check_examples(
    examples: dict[str, Any],
    declared_codes: set[str],
    validator: OnlineContractValidator,
) -> list[str]:
    errors: list[str] = []
    if str(examples.get("example_set_version", "")) == "":
        errors.append("example_set_version_missing")

    pending = examples.get("pending_question") or {}
    accepted = examples.get("accepted_request") or {}
    for field in EXPECTED_BINDING_FIELDS:
        if field not in pending or field not in accepted:
            errors.append(f"example_binding_field_missing:{field}")
            continue
        if pending[field] != accepted[field]:
            errors.append(f"example_binding_field_mismatch:{field}")

    options = pending.get("options") or []
    if len(options) < 2:
        errors.append("example_option_count_invalid")
    values = accepted.get("values")
    evidence_refs = accepted.get("evidence_refs")
    if not isinstance(values, list) or len(values) != 1:
        errors.append("example_value_count_invalid")
    if not isinstance(evidence_refs, list) or len(evidence_refs) != 1:
        errors.append("example_evidence_ref_count_invalid")
    if isinstance(values, list) and len(values) == 1:
        selected = next(
            (item for item in options if item.get("value") == values[0]), None
        )
        if selected is None:
            errors.append("example_value_not_offered")
        elif isinstance(evidence_refs, list) and evidence_refs != [
            selected.get("evidence_ref")
        ]:
            errors.append("example_evidence_ref_mismatch")

    for mutation in examples.get("runtime_rejected_mutations", []):
        code = str(mutation.get("error_code", ""))
        if code not in declared_codes:
            errors.append(f"example_code_not_declared:{code or 'missing'}")

    # Schema 层的变异必须真的被冻结 Schema 拒绝；运行时层的变异必须能穿过
    # Schema（否则声明的运行时错误码永远不可达，说明契约与 Schema 已经漂移）。
    if not _schema_rejects(validator, accepted):
        for mutation in examples.get("schema_rejected_mutations", []):
            case_id = str(mutation.get("case_id", ""))
            field = str(mutation.get("field", ""))
            if not field:
                errors.append("schema_mutation_field_missing")
                continue
            if not _schema_rejects(
                validator, _mutate(accepted, field, mutation.get("replacement"))
            ):
                errors.append(f"schema_mutation_not_rejected:{case_id or field}")
        for mutation in examples.get("runtime_rejected_mutations", []):
            case_id = str(mutation.get("case_id", ""))
            field = str(mutation.get("field", ""))
            if not field:
                errors.append("runtime_mutation_field_missing")
                continue
            if _schema_rejects(
                validator, _mutate(accepted, field, mutation.get("replacement"))
            ):
                errors.append(
                    f"runtime_mutation_blocked_by_schema:{case_id or field}"
                )
    else:
        errors.append("example_accepted_request_schema_invalid")
    return errors


def _mutations() -> list[tuple[str, str]]:
    """变异自检用例：每一条都必须被检查函数检出。"""

    return [
        ("drop_runtime_gate", "删除一条机器规则"),
        ("contract_version_downgraded", "把契约版本改回 1.0.0"),
        ("evidence_ref_count_loosened", "把单证据约束放宽为 2"),
        ("example_evidence_tampered", "篡改公开样例的证据引用"),
        ("runtime_codes_emptied", "运行时错误码集合置空（模拟实现改名）"),
        (
            "runtime_raise_dropped",
            "删除真实 raise 的澄清重放错误码（模拟实现改名但字符串常量残留）",
        ),
        ("transport_response_version_tampered", "篡改公开响应样例的 schema_version"),
    ]


def _apply_mutation(
    name: str,
    contract: dict[str, Any],
    examples: dict[str, Any],
    schema_names: set[str],
    runtime_codes: set[str],
    validator: OnlineContractValidator,
    transport: dict[str, Any],
) -> dict[str, Any]:
    contract = copy.deepcopy(contract)
    examples = copy.deepcopy(examples)
    codes = set(runtime_codes)

    if name == "drop_runtime_gate":
        contract["runtime_gates"] = [
            gate
            for gate in contract.get("runtime_gates", [])
            if gate.get("error_code") != "clarification_answer_replayed"
        ]
    elif name == "contract_version_downgraded":
        contract["contract_version"] = "1.0.0"
    elif name == "evidence_ref_count_loosened":
        contract.setdefault("selection_contract", {})["evidence_ref_count"] = 2
    elif name == "example_evidence_tampered":
        examples["accepted_request"]["evidence_refs"] = [
            "value_evidence.public_year_2012"
        ]
    elif name == "runtime_codes_emptied":
        codes = set()
    elif name == "runtime_raise_dropped":
        codes.discard("clarification_answer_replayed")
    elif name == "transport_response_version_tampered":
        response_name = "online-clarification-required.v2.valid.json"
        transport[response_name] = copy.deepcopy(transport[response_name])
        transport[response_name]["schema_version"] = "9.9.9"
    else:
        raise OnlineContractFreezeError(f"unknown_mutation:{name}")

    result = _check_contract(
        contract,
        schema_names=schema_names,
        runtime_codes=codes,
        examples=examples,
        validator=validator,
    )
    transport_errors = _check_transport_examples(validator, transport)
    result["errors"] = sorted(set(result["errors"] + transport_errors))
    return result


def validate_all() -> dict[str, Any]:
    if not CONTRACT_PATH.is_file():
        raise OnlineContractFreezeError("binding_contract_missing")

    schema_paths = []
    for name in EXPECTED_SCHEMA_FILES:
        path = SCHEMA_ROOT / name
        if not path.is_file():
            raise OnlineContractFreezeError(f"schema_missing:{name}")
        schema_paths.append(path)
    schema_names = {path.name for path in schema_paths}

    examples_path = EXAMPLES_DIR / "clarification-binding.v2.examples.json"
    contract = _load_json(CONTRACT_PATH)
    examples = _load_json(examples_path)
    transport = _load_transport_examples()

    runtime_source = _canonical_text_bytes(RUNTIME_SOURCE).decode("utf-8")
    runtime_codes = _runtime_raise_codes(runtime_source)
    runtime_order = _runtime_check_order(runtime_source)

    validator = OnlineContractValidator()
    result = _check_contract(
        contract,
        schema_names=schema_names,
        runtime_codes=runtime_codes,
        examples=examples,
        validator=validator,
    )
    transport_errors = _check_transport_examples(validator, transport)
    schema_ids_ok = _schema_ids_match(schema_paths)
    all_errors = sorted(set(result["errors"] + transport_errors))
    if not schema_ids_ok:
        all_errors.append("schema_id_mismatch")
    all_errors = sorted(set(all_errors))

    declared_set = set(result["declared_error_codes"])
    unregistered_runtime_codes = [
        code for code in runtime_order if code not in declared_set
    ]

    mutation_items = []
    for name, description in _mutations():
        mutated = _apply_mutation(
            name,
            contract,
            examples,
            schema_names,
            runtime_codes,
            validator,
            transport,
        )
        detected = bool(mutated["errors"])
        mutation_items.append(
            {
                "case_id": name,
                "description": description,
                "detected": detected,
                "error_count": len(mutated["errors"]),
                "sample_errors": mutated["errors"][:3],
            }
        )
    detected_count = sum(1 for item in mutation_items if item["detected"])
    mutation_ok = detected_count == len(mutation_items)

    artifacts = [_artifact(CONTRACT_PATH), _artifact(examples_path)]
    artifacts.extend(_artifact(path) for path in schema_paths)
    artifacts.extend(
        _artifact(EXAMPLES_DIR / filename) for filename, _ in TRANSPORT_EXAMPLES
    )

    status = "pass" if not all_errors and mutation_ok else "fail"

    report = {
        "status": status,
        "report_type": "d4_online_contract_v2_freeze_gate",
        "task_id": "D4-07-00",
        "owner": "A",
        "scope": "public_contract_freeze_offline_verification",
        "contract": {
            "contract_id": contract.get("contract_id"),
            "contract_version": contract.get("contract_version"),
            "request_schema": contract.get("request_schema"),
            "response_schema": contract.get("response_schema"),
            "gate_count": result["gate_count"],
            "declared_error_codes": result["declared_error_codes"],
            "declared_gate_order": result["declared_gate_order"],
        },
        "artifacts": artifacts,
        "version_consistency": {
            "expected_contract_version": EXPECTED_CONTRACT_VERSION,
            "schema_count": len(schema_paths),
            "schema_ids_match_filenames": schema_ids_ok,
            "referenced_request_schema_resolves": contract.get("request_schema")
            in schema_names,
            "referenced_response_schema_resolves": contract.get("response_schema")
            in schema_names,
        },
        "error_code_drift": {
            "declared_code_count": len(result["declared_error_codes"]),
            "missing_in_runtime": [
                item
                for item in result["errors"]
                if item.startswith("error_code_missing_in_runtime:")
            ],
            "runtime_check_order": runtime_order,
            "declared_order": result["declared_gate_order"],
            "unregistered_runtime_codes": unregistered_runtime_codes,
            "order_matches_declared": runtime_order == result["declared_error_codes"],
            "note": (
                "运行时检查顺序与声明顺序不一致时不判失败：两者均为失败关闭，"
                "但错误码优先级变化需要 C 侧前端显式适配。"
                "unregistered_runtime_codes 是运行时抛出但未登记为机器规则的错误码，"
                "只作登记，不判失败。"
            ),
        },
        "example_consistency": {
            "example_set_version": examples.get("example_set_version"),
            "runtime_rejected_mutation_count": len(
                examples.get("runtime_rejected_mutations", [])
            ),
            "schema_rejected_mutation_count": len(
                examples.get("schema_rejected_mutations", [])
            ),
        },
        "transport_examples": {
            "files": [filename for filename, _ in TRANSPORT_EXAMPLES],
            "all_valid_against_frozen_schema": not transport_errors,
            "errors": transport_errors,
        },
        "mutation_self_check": {
            "total": len(mutation_items),
            "detected": detected_count,
            "all_detected": mutation_ok,
            "items": mutation_items,
        },
        "findings": all_errors,
        "boundaries": {
            "network_calls": 0,
            "model_api_calls": 0,
            "credential_files_read": 0,
            "estimated_cost_cny": 0.0,
            "validation_cases_read": 0,
            "hidden_test_cases_read": 0,
            "answer_derived_fields_used_for_input": False,
        },
        "not_proven": [
            "b_runtime_state_semantics",
            "c_frontend_v2_consumption",
            "five_turn_conversation_regression",
            "online_sql_proposal_quality",
            "business_accuracy_or_generalization",
            "empirical_calibration",
            "innovation_claim_evidence",
        ],
    }
    report["stable_sha256"] = _hash(
        {
            "contract": report["contract"],
            "artifacts": report["artifacts"],
            "findings": report["findings"],
            "mutation_self_check": report["mutation_self_check"],
            "runtime_check_order": report["error_code_drift"]["runtime_check_order"],
            "transport_examples": report["transport_examples"],
        }
    )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成 D4-07 在线公共契约 v2 收口机器报告")
    parser.add_argument("--report", default=str(REPORT_PATH))
    args = parser.parse_args(argv)
    report = validate_all()
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
