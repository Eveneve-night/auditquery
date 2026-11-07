"""D4-08 A 侧评测契约与结论 Gate 离线校验器。

A 侧只维护评测契约和结论 Gate，不实现评测运行器：

- 冻结 `conclusion-gate.v1.json`（绑定 D4-00 `experiment-protocol.v1.json`，机器化
  success / hard_failure / degradation 阈值、required 指标、暴露上限与隐藏 test 守卫）；
- 用 `evaluation-runner-result.v1.schema.json` 真验 B 侧评测运行器输出结构；
- `compute_conclusion` 对运行器输出套阈值、产出 verdict（innovation_success /
  degradation / hard_failure / no_claim）与 intermediate_pass；
- 变异自检分两层：契约漂移（阈值/规则/hash/schema_id 被篡改）与结论漂移
  （好的运行器输出被改成违反阈值），证明检测非空转。

本工具不调用模型、不读取密钥、不读取 validation 或隐藏 test 的题目/答案内容。
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

PROJECT_ROOT = Path(__file__).resolve().parents[2]

REPORT_PATH = PROJECT_ROOT / "tests" / "reports" / "d4-08-conclusion-gate-v1.json"
GATE_PATH = PROJECT_ROOT / "configs" / "evaluation" / "d4" / "conclusion-gate.v1.json"
PROTOCOL_PATH = (
    PROJECT_ROOT / "configs" / "evaluation" / "d4" / "experiment-protocol.v1.json"
)
RUNNER_SCHEMA_PATH = (
    PROJECT_ROOT / "configs" / "schemas" / "d4-08" / "evaluation-runner-result.v1.schema.json"
)

EXPECTED_GROUPS = ["B0", "B1", "B2", "P", "P-uncalibrated"]
BASELINE_GROUP_IDS = ["B0", "B1", "B2"]
PROPOSED_GROUP_ID = "P"

# 冻结阈值（与 docs/26 §8、experiment-protocol.v1.json outcome_criteria 一一对应）。
# 校验器是这些值的强制执行点：结论 Gate 契约必须与之一致，否则判漂移。
FROZEN_THRESHOLDS: dict[str, float | int] = {
    "necessary_clarification_recall_min": 0.90,
    "necessary_clarification_recall_hard_min": 0.85,
    "over_clarification_rate_max": 0.10,
    "slot_macro_f1_min": 0.90,
    "post_clarification_success_rate_min": 0.85,
    "high_confidence_error_rate_max": 0.05,
    "sufficient_question_e2e_drop_max_pp": 2.0,
    "quality_gain_pp_min": 5.0,
    "efficiency_gain_pct_min": 30.0,
    "cost_latency_regression_pct_max": 20.0,
    "hidden_test_min_cases": 120,
    "domain_min_count": 3,
    "validation_exposure_max_per_prompt_major": 2,
}

FROZEN_HARD_FAILURE_CODES = [
    "not_better_than_best_baseline",
    "clarification_recall_too_low",
    "over_clarification_too_high",
    "high_confidence_error_too_high",
    "cost_latency_regression",
    "migration_requires_code_or_test_answer",
    "evidence_not_reproducible",
]
FROZEN_SUCCESS_CODES = ["quality_gain", "efficiency_gain"]
FROZEN_DEGRADATION_CODES = [
    "prompt_only_gain",
    "single_schema_only",
    "insufficient_hidden_test",
    "calibration_missing",
    "efficiency_only",
]

# 冻结的 Prompt 作者身份（按能力绑定）：keeper 不得是该能力对应 Prompt 的作者。
# 事实源 docs/20-d3-three-person-task-board.md：
#   D3-01 意图/澄清 Prompt = A(Eveneve-night)；D3-02 NL2SQL Prompt = B(cacgn)；
#   D3-03 RAG(grounded-answer) Prompt = C(stevethomasih520-afk)。
# 交叉保管：SQL 隐藏答案 keeper=C（非 nl2sql 作者）、RAG/UI 隐藏答案 keeper=B（非 rag 作者），
# 故 keeper 只能禁止「该能力作者」，不能把三人全集当作全局禁名单（否则无人可合法保管）。
FROZEN_PROMPT_AUTHORS_BY_CAPABILITY = {
    "clarification": ["Eveneve-night"],
    "nl2sql": ["cacgn"],
    "rag": ["stevethomasih520-afk"],
}


class ConclusionGateError(RuntimeError):
    """结论 Gate 契约/结论校验失败。"""


def _canonical_text_bytes(path: Path) -> bytes:
    text = path.read_text(encoding="utf-8")
    return text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")


def _load_json(path: Path) -> Any:
    return json.loads(_canonical_text_bytes(path).decode("utf-8"))


def _sha256_canonical(path: Path) -> str:
    return hashlib.sha256(_canonical_text_bytes(path)).hexdigest()


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


def _check_gate_consistency(gate: dict[str, Any]) -> list[str]:
    """校验结论 Gate 契约自身的冻结一致性，返回错误码列表。"""

    errors: list[str] = []
    th = gate.get("thresholds", {})

    for key, expected in FROZEN_THRESHOLDS.items():
        if th.get(key) != expected:
            errors.append(f"threshold_drifted:{key}")

    def codes(rules: list[dict[str, Any]], field: str = "code") -> list[str]:
        return [str(r.get(field)) for r in rules]

    hard = codes(gate.get("hard_failure_rules", []))
    success = codes(gate.get("success_rules", []))
    degradation = codes(gate.get("degradation_rules", []))

    if sorted(hard) != sorted(FROZEN_HARD_FAILURE_CODES):
        errors.append("hard_failure_rule_set_drifted")
    if sorted(success) != sorted(FROZEN_SUCCESS_CODES):
        errors.append("success_rule_set_drifted")
    if sorted(degradation) != sorted(FROZEN_DEGRADATION_CODES):
        errors.append("degradation_rule_set_drifted")

    if gate.get("required_groups") != EXPECTED_GROUPS:
        errors.append("required_groups_drifted")
    if gate.get("baseline_group_ids") != BASELINE_GROUP_IDS:
        errors.append("baseline_group_ids_drifted")
    if gate.get("proposed_group_id") != PROPOSED_GROUP_ID:
        errors.append("proposed_group_id_drifted")

    # 协议引用 hash 必须与磁盘上的冻结协议一致。
    protocol_ref = gate.get("protocol_ref", {})
    if protocol_ref.get("path") != "configs/evaluation/d4/experiment-protocol.v1.json":
        errors.append("protocol_ref_path_drifted")
    if protocol_ref.get("sha256") != _sha256_canonical(PROTOCOL_PATH):
        errors.append("protocol_ref_hash_mismatch")

    schema_ref = gate.get("runner_result_schema", {})
    if schema_ref.get("path") != "configs/schemas/d4-08/evaluation-runner-result.v1.schema.json":
        errors.append("runner_schema_path_drifted")
    schema = _load_json(RUNNER_SCHEMA_PATH)
    if schema_ref.get("schema_id") != schema.get("$id"):
        errors.append("runner_schema_id_mismatch")

    # 冻结 Prompt 作者身份必须与 FROZEN_PROMPT_AUTHORS_BY_CAPABILITY 一致（keeper 守卫依赖它）。
    if gate.get("identity", {}).get("prompt_authors_by_capability") != FROZEN_PROMPT_AUTHORS_BY_CAPABILITY:
        errors.append("prompt_authors_drifted")

    # 中间态规则数值必须与 thresholds 一致（防止两处数字漂移不一致）。
    for rule in gate.get("intermediate_pass_rules", []):
        metric = str(rule.get("metric", ""))
        value = rule.get("value")
        key_map = {
            "necessary_clarification_recall": "necessary_clarification_recall_min",
            "over_clarification_rate": "over_clarification_rate_max",
            "slot_macro_f1": "slot_macro_f1_min",
            "post_clarification_success_rate": "post_clarification_success_rate_min",
            "sufficient_question_e2e_accuracy_drop_pp": "sufficient_question_e2e_drop_max_pp",
        }
        if metric in key_map and value != th.get(key_map[metric]):
            errors.append(f"intermediate_rule_inconsistent:{metric}")

    return sorted(set(errors))


def _validate_runner_structure(runner: dict[str, Any]) -> list[str]:
    """用 runner 结果 schema 真验结构，返回错误码列表。"""

    schema = _load_json(RUNNER_SCHEMA_PATH)
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors: list[str] = []
    for err in sorted(validator.iter_errors(runner), key=lambda e: list(e.path)):
        path = ".".join(str(p) for p in err.path) or "root"
        errors.append(f"schema:{path}")
    return errors


def _metric(runner: dict[str, Any], group_id: str, metric: str) -> float | None:
    value = runner.get("groups", {}).get(group_id, {}).get("metrics", {}).get(metric)
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _required_p_metrics_ok(runner: dict[str, Any], gate: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    metrics = runner.get("groups", {}).get(PROPOSED_GROUP_ID, {}).get("metrics", {})
    for metric in gate.get("required_p_metrics", []):
        if metric not in metrics or metrics[metric] is None:
            missing.append(metric)
    return missing


def _best_baseline_accuracy(runner: dict[str, Any]) -> tuple[float, str]:
    best = -1.0
    best_id = ""
    for group_id in BASELINE_GROUP_IDS:
        acc = _metric(runner, group_id, "post_clarification_end_to_end_execution_accuracy")
        if acc is not None and acc > best:
            best = acc
            best_id = group_id
    return best, best_id


def _efficiency_pct_change(runner: dict[str, Any], metric: str, best_id: str) -> float | None:
    p_val = _metric(runner, PROPOSED_GROUP_ID, metric)
    base_val = _metric(runner, best_id, metric)
    if p_val is None or base_val is None or base_val == 0:
        return None
    return (p_val - base_val) / base_val * 100.0


def compute_conclusion(gate: dict[str, Any], runner: dict[str, Any]) -> dict[str, Any]:
    """对运行器输出套冻结阈值，产出结论。"""

    errors: list[str] = list(_validate_runner_structure(runner))
    missing_p_metrics = _required_p_metrics_ok(runner, gate)
    for metric in missing_p_metrics:
        errors.append(f"missing_p_metric:{metric}")

    # 数值类型校验：required 指标必须是 number（排除 bool），非 number 一律 invalid_result，
    # 不允许后续 float() 抛 ValueError。结构校验只约束 schema 已声明的字段；这里兜底拦截
    # compute_conclusion 被直接调用（绕过 schema）时的异常输入。
    p_metrics_raw = runner.get("groups", {}).get(PROPOSED_GROUP_ID, {}).get("metrics", {})
    for metric in gate.get("required_p_metrics", []):
        val = p_metrics_raw.get(metric)
        if val is not None and (isinstance(val, bool) or not isinstance(val, (int, float))):
            errors.append(f"non_numeric_p_metric:{metric}")

    # 协议绑定：运行器必须声明它评测的是同一份冻结协议。
    if runner.get("protocol_id") != "d4-typed-decision-experiment.v1":
        errors.append("runner_protocol_id_mismatch")
    if runner.get("protocol_sha256") != _sha256_canonical(PROTOCOL_PATH):
        errors.append("runner_protocol_hash_mismatch")

    group_ids = list(runner.get("groups", {}).keys())
    if sorted(group_ids) != sorted(EXPECTED_GROUPS):
        errors.append("runner_group_set_mismatch")

    # 结构不完整则无法出结论。
    if errors:
        return {
            "verdict": "invalid_result",
            "intermediate_pass": False,
            "triggered_success": [],
            "triggered_hard_failure": [],
            "triggered_degradation": [],
            "guard_violations": [],
            "errors": sorted(set(errors)),
        }

    th = gate["thresholds"]

    # —— 守卫检查（协议/安全违规，独立于指标）——
    guard_violations: list[str] = []
    exposure = runner.get("exposure", {})
    validation_total = int(exposure.get("validation_historical_count", 0)) + int(
        exposure.get("validation_current_task_count", 0)
    )
    if validation_total > int(th["validation_exposure_max_per_prompt_major"]):
        guard_violations.append("guard:validation_exposure_exceeded")

    hidden = runner.get("hidden_test", {})
    if bool(hidden.get("content_committed", False)):
        guard_violations.append("guard:hidden_test_content_committed")
    authors_by_capability = gate.get("identity", {}).get("prompt_authors_by_capability", {})
    capability = str(hidden.get("capability", ""))
    prompt_authors = set(authors_by_capability.get(capability, []))
    if str(hidden.get("keeper", "")) in prompt_authors:
        guard_violations.append("guard:hidden_test_keeper_is_prompt_author")

    # —— 指标计算 ——
    p_metrics = runner["groups"][PROPOSED_GROUP_ID]["metrics"]
    p_acc = float(p_metrics["post_clarification_end_to_end_execution_accuracy"])
    best_acc, best_id = _best_baseline_accuracy(runner)
    delta_pp = (p_acc - best_acc) * 100.0

    cost_pct = _efficiency_pct_change(runner, "cost_cny", best_id)
    p95_pct = _efficiency_pct_change(runner, "end_to_end_p95_ms", best_id)
    tokens_pct = _efficiency_pct_change(runner, "tokens", best_id)

    # —— 中级任务 3 最低通过 ——
    intermediate_pass = True
    for rule in gate["intermediate_pass_rules"]:
        metric = rule["metric"]
        value = float(p_metrics.get(metric))
        if rule["op"] == ">=":
            ok = value >= float(rule["value"])
        elif rule["op"] == "<=":
            ok = value <= float(rule["value"])
        else:
            ok = True
        if not ok:
            intermediate_pass = False

    # —— 硬失败 ——
    hard_failure: list[str] = []
    if delta_pp <= 0.0:
        hard_failure.append("not_better_than_best_baseline")
    if float(p_metrics.get("necessary_clarification_recall", 1.0)) < float(
        th["necessary_clarification_recall_hard_min"]
    ):
        hard_failure.append("clarification_recall_too_low")
    if float(p_metrics.get("over_clarification_rate", 0.0)) > float(
        th["over_clarification_rate_max"]
    ):
        hard_failure.append("over_clarification_too_high")
    if float(p_metrics.get("high_confidence_error_rate", 0.0)) > float(
        th["high_confidence_error_rate_max"]
    ):
        hard_failure.append("high_confidence_error_too_high")

    regression = (
        cost_pct is not None and cost_pct > float(th["cost_latency_regression_pct_max"])
    ) or (
        p95_pct is not None and p95_pct > float(th["cost_latency_regression_pct_max"])
    )
    if regression and delta_pp < float(th["quality_gain_pp_min"]):
        hard_failure.append("cost_latency_regression")

    migration = runner.get("migration", {})
    if bool(migration.get("new_domain_prompt_or_code_changed", False)) or bool(
        migration.get("new_domain_test_answer_written", False)
    ):
        hard_failure.append("migration_requires_code_or_test_answer")

    if not bool(runner.get("evidence_reproducible", False)):
        hard_failure.append("evidence_not_reproducible")

    # —— 成功 ——
    mcnemar = runner.get("statistics", {}).get("mcnemar", {})
    # 统计证据必须绑定「P 对最佳基线」：comparison 必须是 P_vs_<best_id>、sample_pairs 为正，
    # 且 p_value <= 0.05。任意 comparison（如 B0_vs_B1）不得被当作 P 的质量增益证据。
    expected_comparison = f"P_vs_{best_id}" if best_id else ""
    stat_support = (
        bool(expected_comparison)
        and str(mcnemar.get("comparison", "")) == expected_comparison
        and int(mcnemar.get("sample_pairs", 0)) > 0
        and bool(mcnemar.get("p_value") is not None)
        and float(mcnemar.get("p_value", 1.0)) <= 0.05
    )

    success: list[str] = []
    if delta_pp >= float(th["quality_gain_pp_min"]) and stat_support:
        success.append("quality_gain")

    efficiency_gain = (
        cost_pct is not None and cost_pct <= -float(th["efficiency_gain_pct_min"])
    ) or (
        p95_pct is not None and p95_pct <= -float(th["efficiency_gain_pct_min"])
    ) or (
        tokens_pct is not None and tokens_pct <= -float(th["efficiency_gain_pct_min"])
    )
    if delta_pp >= -float(th["sufficient_question_e2e_drop_max_pp"]) and efficiency_gain:
        success.append("efficiency_gain")

    # —— 降级 ——
    degradation: list[str] = []
    b0_acc = _metric(runner, "B0", "post_clarification_end_to_end_execution_accuracy")
    if b0_acc is not None and p_acc > b0_acc and delta_pp <= 0.0:
        degradation.append("prompt_only_gain")
    if int(migration.get("domain_count", 0)) < int(th["domain_min_count"]):
        degradation.append("insufficient_hidden_test")
    if int(hidden.get("case_count", 0)) < int(th["hidden_test_min_cases"]):
        degradation.append("insufficient_hidden_test")
    if not p_metrics.get("brier_score") or not p_metrics.get("ece"):
        degradation.append("calibration_missing")
    # efficiency_only：有效率收益但未达到 efficiency_gain 成功门槛，且无质量收益。
    has_efficiency_benefit = (
        (cost_pct is not None and cost_pct < 0)
        or (p95_pct is not None and p95_pct < 0)
        or (tokens_pct is not None and tokens_pct < 0)
    )
    if (
        has_efficiency_benefit
        and "efficiency_gain" not in success
        and "quality_gain" not in success
    ):
        degradation.append("efficiency_only")
    # 单一 Schema：仅当 domain_count == 1 且未被 insufficient_hidden_test 覆盖时单独标记。
    if int(migration.get("domain_count", 0)) == 1:
        degradation.append("single_schema_only")

    degradation = sorted(set(degradation))

    # —— 判定 ——
    if guard_violations:
        verdict = "hard_failure"
    elif hard_failure:
        verdict = "hard_failure"
    elif success and intermediate_pass:
        verdict = "innovation_success"
    elif degradation:
        verdict = "degradation"
    else:
        verdict = "no_claim"

    return {
        "verdict": verdict,
        "intermediate_pass": intermediate_pass,
        "delta_pp": round(delta_pp, 4),
        "best_baseline_group": best_id,
        "triggered_success": success,
        "triggered_hard_failure": hard_failure,
        "triggered_degradation": degradation,
        "guard_violations": guard_violations,
        "errors": [],
    }


def _satisfactory_runner() -> dict[str, Any]:
    """一个满足 quality_gain 的合成运行器输出，供结论变异自检与单测复用。"""

    def group(group_id: str, role: str, acc: float) -> dict[str, Any]:
        return {
            "group_id": group_id,
            "role": role,
            "metrics": {
                "post_clarification_end_to_end_execution_accuracy": acc,
                "necessary_clarification_recall": 0.95,
                "over_clarification_rate": 0.05,
                "slot_macro_f1": 0.93,
                "post_clarification_success_rate": 0.90,
                "high_confidence_error_rate": 0.02,
                "sufficient_question_e2e_accuracy_drop_pp": 0.5,
                "end_to_end_p95_ms": 1500,
                "cost_cny": 0.02,
                "tokens": 5000,
                "model_calls": 60,
                "brier_score": 0.05,
                "ece": 0.04,
            },
        }

    return {
        "runner_id": "d4-evaluation-runner.v1",
        "run_id": "synthetic-satisfactory-001",
        "protocol_id": "d4-typed-decision-experiment.v1",
        "protocol_sha256": _sha256_canonical(PROTOCOL_PATH),
        "data_version": "2.0.0",
        "groups": {
            "B0": group("B0", "baseline", 0.68),
            "B1": group("B1", "baseline", 0.70),
            "B2": group("B2", "baseline", 0.66),
            "P": group("P", "proposed", 0.80),
            "P-uncalibrated": group("P-uncalibrated", "diagnostic", 0.76),
        },
        "statistics": {
            "mcnemar": {"comparison": "P_vs_B1", "p_value": 0.01, "sample_pairs": 80},
            "paired_bootstrap_95ci": {
                "comparison": "P_vs_B1",
                "metric": "post_clarification_end_to_end_execution_accuracy",
                "lower": 0.06,
                "upper": 0.14,
            },
        },
        "exposure": {
            "validation_historical_count": 0,
            "validation_current_task_count": 0,
        },
        "hidden_test": {"capability": "nl2sql", "case_count": 120, "content_committed": False, "keeper": "stevethomasih520-afk"},
        "migration": {
            "domain_count": 3,
            "new_domain_prompt_or_code_changed": False,
            "new_domain_test_answer_written": False,
            "business_code_changed_lines": 0,
        },
        "evidence_reproducible": True,
    }


def _contract_mutations() -> list[tuple[str, str]]:
    return [
        ("quality_gain_threshold_loosened", "把 quality_gain_pp_min 从 5.0 放宽为 0.0"),
        ("clarification_recall_hard_loosened", "把必要澄清硬阈值从 0.85 放宽为 0.0"),
        ("drop_hard_failure_rule", "删除一条硬失败规则"),
        ("protocol_ref_hash_tampered", "篡改协议引用 hash"),
        ("runner_schema_id_tampered", "篡改 runner schema_id 引用"),
    ]


def _conclusion_mutations() -> list[tuple[str, str]]:
    return [
        ("p_below_best_baseline", "P 主指标低于最佳基线"),
        ("clarification_recall_violated", "P 必要澄清召回低于 0.85"),
        ("hidden_test_insufficient", "隐藏测试数量少于 120"),
        ("validation_historical_exposure", "validation 历史累计超上限（historical=99, current=0）"),
        ("keeper_is_prompt_author", "nl2sql 能力 keeper 设为该能力 Prompt 作者 cacgn"),
        ("keeper_is_rag_author", "rag 能力 keeper 设为该能力 Prompt 作者 stevethomasih520-afk"),
        ("keeper_is_clarification_author", "clarification 能力 keeper 设为该能力 Prompt 作者 Eveneve-night"),
        ("mcnemar_comparison_misbound", "McNemar comparison 未绑定最佳基线（B0_vs_B1）"),
    ]


def _mutate_contract(name: str, gate: dict[str, Any]) -> dict[str, Any]:
    gate = copy.deepcopy(gate)
    th = gate.setdefault("thresholds", {})
    if name == "quality_gain_threshold_loosened":
        th["quality_gain_pp_min"] = 0.0
        for rule in gate["success_rules"]:
            if rule["code"] == "quality_gain":
                rule["delta_pp"] = 0.0
    elif name == "clarification_recall_hard_loosened":
        th["necessary_clarification_recall_hard_min"] = 0.0
        for rule in gate["hard_failure_rules"]:
            if rule["code"] == "clarification_recall_too_low":
                rule["value"] = 0.0
    elif name == "drop_hard_failure_rule":
        gate["hard_failure_rules"] = [
            r
            for r in gate["hard_failure_rules"]
            if r.get("code") != "not_better_than_best_baseline"
        ]
    elif name == "protocol_ref_hash_tampered":
        gate["protocol_ref"]["sha256"] = "0" * 64
    elif name == "runner_schema_id_tampered":
        gate["runner_result_schema"]["schema_id"] = "tampered"
    else:
        raise ConclusionGateError(f"unknown_contract_mutation:{name}")
    return gate


def _mutate_runner(name: str, runner: dict[str, Any]) -> dict[str, Any]:
    runner = copy.deepcopy(runner)
    if name == "p_below_best_baseline":
        runner["groups"]["P"]["metrics"]["post_clarification_end_to_end_execution_accuracy"] = 0.60
    elif name == "clarification_recall_violated":
        runner["groups"]["P"]["metrics"]["necessary_clarification_recall"] = 0.80
    elif name == "hidden_test_insufficient":
        runner["hidden_test"]["case_count"] = 100
    elif name == "validation_historical_exposure":
        runner["exposure"]["validation_historical_count"] = 99
        runner["exposure"]["validation_current_task_count"] = 0
    elif name == "keeper_is_prompt_author":
        runner["hidden_test"]["capability"] = "nl2sql"
        runner["hidden_test"]["keeper"] = "cacgn"
    elif name == "keeper_is_rag_author":
        runner["hidden_test"]["capability"] = "rag"
        runner["hidden_test"]["keeper"] = "stevethomasih520-afk"
    elif name == "keeper_is_clarification_author":
        runner["hidden_test"]["capability"] = "clarification"
        runner["hidden_test"]["keeper"] = "Eveneve-night"
    elif name == "mcnemar_comparison_misbound":
        runner["statistics"]["mcnemar"]["comparison"] = "B0_vs_B1"
    else:
        raise ConclusionGateError(f"unknown_conclusion_mutation:{name}")
    return runner


def validate_all() -> dict[str, Any]:
    if not GATE_PATH.is_file():
        raise ConclusionGateError("conclusion_gate_missing")
    if not RUNNER_SCHEMA_PATH.is_file():
        raise ConclusionGateError("runner_schema_missing")

    gate = _load_json(GATE_PATH)
    schema = _load_json(RUNNER_SCHEMA_PATH)
    Draft202012Validator.check_schema(schema)

    consistency_errors = _check_gate_consistency(gate)

    # —— 契约变异自检：篡改契约后自洽检查必须检出 ——
    contract_mutation_items = []
    for name, description in _contract_mutations():
        mutated = _mutate_contract(name, gate)
        detected = bool(_check_gate_consistency(mutated))
        contract_mutation_items.append(
            {
                "case_id": name,
                "description": description,
                "detected": detected,
                "sample_errors": _check_gate_consistency(mutated)[:3],
            }
        )

    # —— 结论变异自检：篡改好的运行器输出后结论必须改变 ——
    conclusion_mutation_items = []
    good = _satisfactory_runner()
    good_conclusion = compute_conclusion(gate, good)
    for name, description in _conclusion_mutations():
        mutated = _mutate_runner(name, good)
        mutated_conclusion = compute_conclusion(gate, mutated)
        changed = mutated_conclusion["verdict"] != good_conclusion["verdict"] or bool(
            mutated_conclusion["triggered_hard_failure"]
            or mutated_conclusion["triggered_degradation"]
            or mutated_conclusion["errors"]
        )
        conclusion_mutation_items.append(
            {
                "case_id": name,
                "description": description,
                "detected": changed,
                "mutated_verdict": mutated_conclusion["verdict"],
                "mutated_hard_failure": mutated_conclusion["triggered_hard_failure"],
                "mutated_degradation": mutated_conclusion["triggered_degradation"],
            }
        )

    contract_detected = sum(1 for i in contract_mutation_items if i["detected"])
    conclusion_detected = sum(1 for i in conclusion_mutation_items if i["detected"])
    contract_ok = contract_detected == len(contract_mutation_items)
    conclusion_ok = conclusion_detected == len(conclusion_mutation_items)

    artifacts = [_artifact(GATE_PATH), _artifact(PROTOCOL_PATH), _artifact(RUNNER_SCHEMA_PATH)]

    status = "pass" if (not consistency_errors and contract_ok and conclusion_ok) else "fail"

    report = {
        "status": status,
        "report_type": "d4_08_conclusion_gate_offline_validation",
        "task_id": "D4-08",
        "owner": "A",
        "scope": "evaluation_contract_and_conclusion_gate_freeze",
        "gate": {
            "gate_id": gate.get("gate_id"),
            "version": gate.get("version"),
            "status": gate.get("status"),
            "protocol_sha256": gate.get("protocol_ref", {}).get("sha256"),
            "runner_schema_id": gate.get("runner_result_schema", {}).get("schema_id"),
            "required_p_metrics": gate.get("required_p_metrics"),
            "thresholds": gate.get("thresholds"),
        },
        "artifacts": artifacts,
        "consistency": {
            "errors": consistency_errors,
            "contract_self_consistent": not consistency_errors,
        },
        "satisfactory_runner_conclusion": good_conclusion,
        "mutation_self_check": {
            "contract": {
                "total": len(contract_mutation_items),
                "detected": contract_detected,
                "all_detected": contract_ok,
                "items": contract_mutation_items,
            },
            "conclusion": {
                "total": len(conclusion_mutation_items),
                "detected": conclusion_detected,
                "all_detected": conclusion_ok,
                "items": conclusion_mutation_items,
            },
        },
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
            "b_evaluation_runner_implementation",
            "c_end_to_end_and_report",
            "four_ablations_executed",
            "business_accuracy_or_generalization",
            "empirical_calibration_evidence",
            "cross_domain_migration_evidence",
        ],
    }
    report["stable_sha256"] = _hash(
        {
            "gate": report["gate"],
            "artifacts": report["artifacts"],
            "consistency": report["consistency"],
            "satisfactory_runner_conclusion": report["satisfactory_runner_conclusion"],
            "mutation_self_check": report["mutation_self_check"],
        }
    )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成 D4-08 A 侧结论 Gate 机器报告")
    parser.add_argument("--report", default=str(REPORT_PATH))
    args = parser.parse_args(argv)
    report = validate_all()
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
