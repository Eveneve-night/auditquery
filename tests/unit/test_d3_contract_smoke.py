"""Offline tests for the integrated D3 20-case contract-smoke runner."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from backend.agent.d3_contract_smoke import (  # noqa: E402
    APPROVED_MODEL,
    BatchLimits,
    ContractSmokeError,
    D3ContractSmokeRunner,
    build_case_payload,
)
from backend.llm.contracts import (  # noqa: E402
    ModelCallRequest,
    ModelCallResult,
    ModelUsage,
)
from backend.llm.errors import ProviderError, ProviderErrorCode  # noqa: E402
from backend.llm.gateway import LLMGateway  # noqa: E402
from backend.llm.usage import UsageLedger  # noqa: E402


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _success_envelope(task_type: str, data: dict) -> dict:
    return {
        "schema_version": "1.0.0",
        "task_type": task_type,
        "status": "success",
        "data": data,
        "error": None,
        "warnings": [],
    }


def _mock_output(request: ModelCallRequest) -> dict:
    payload = json.loads(request.messages[-1].content)
    if request.task_type == "intent":
        return _success_envelope(
            "intent",
            {
                "intent": "unknown",
                "needs_clarification": False,
                "required_sources": [],
                "missing_slots": [],
                "reason": "unsupported_task",
                "normalized_query": None,
            },
        )
    if request.task_type == "clarification":
        missing = [
            slot
            for slot in payload["required_slots"]
            if slot not in payload["known_slots"]
            or payload["known_slots"][slot] in (None, "", [], {})
        ]
        if missing:
            data = {
                "should_clarify": True,
                "primary_slot": missing[0],
                "missing_slots": missing,
                "question": "请补充完成当前任务所必需的信息。",
                "reason": "missing_required_slot",
            }
        else:
            data = {
                "should_clarify": False,
                "primary_slot": None,
                "missing_slots": [],
                "question": None,
                "reason": "complete",
            }
        return _success_envelope("clarification", data)
    if request.task_type == "nl2sql":
        return _success_envelope(
            "nl2sql",
            {
                "outcome": "sql",
                "sql": "SELECT COUNT(*) FROM Artist",
                "tables": ["Artist"],
                "params": [],
                "rationale": "生成单表候选查询；尚未执行安全检查。",
            },
        )
    return _success_envelope(
        "grounded_answer",
        {
            "answerable": False,
            "answer": None,
            "evidence_ids": [],
            "citations": [],
            "insufficient_reason": "mock 仅验证契约，不判断业务答案。",
        },
    )


def _mock_result(
    request: ModelCallRequest,
    *,
    input_tokens: int = 10,
    output_tokens: int = 5,
    cost: float = 0.0,
) -> ModelCallResult:
    return ModelCallResult(
        request_id=request.request_id,
        task_type=request.task_type,
        provider_id="offline-mock",
        requested_model=APPROVED_MODEL,
        returned_model=APPROVED_MODEL,
        provider_request_id=None,
        parsed_output=_mock_output(request),
        usage=ModelUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
        ),
        estimated_cost_cny=cost,
        latency_ms=1.0,
        finish_reason="stop",
    )


class _MockProvider:
    provider_id = "offline-mock"
    model = APPROVED_MODEL

    def __init__(self) -> None:
        self.requests: list[ModelCallRequest] = []
        self.usage_ledger = UsageLedger()

    def generate_structured(self, request: ModelCallRequest) -> ModelCallResult:
        self.requests.append(request)
        result = _mock_result(request)
        self.usage_ledger.record_success(result)
        return result


class _SequenceMockProvider(_MockProvider):
    def __init__(
        self,
        outcomes: list[ProviderError | dict[str, int | float] | None],
    ) -> None:
        super().__init__()
        self.outcomes = list(outcomes)

    def generate_structured(self, request: ModelCallRequest) -> ModelCallResult:
        self.requests.append(request)
        outcome = self.outcomes.pop(0) if self.outcomes else None
        if isinstance(outcome, ProviderError):
            self.usage_ledger.record_failure(
                request_id=request.request_id,
                task_type=request.task_type,
                provider_id=self.provider_id,
                requested_model=self.model,
                error=outcome,
                latency_ms=1.0,
            )
            raise outcome
        usage = outcome or {}
        result = _mock_result(
            request,
            input_tokens=int(usage.get("input_tokens", 10)),
            output_tokens=int(usage.get("output_tokens", 5)),
            cost=float(usage.get("cost", 0.0)),
        )
        self.usage_ledger.record_success(result)
        return result


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _AdvancingMockProvider(_MockProvider):
    def __init__(self, clock: _FakeClock, seconds_per_call: float) -> None:
        super().__init__()
        self.clock = clock
        self.seconds_per_call = seconds_per_call

    def generate_structured(self, request: ModelCallRequest) -> ModelCallResult:
        self.clock.advance(self.seconds_per_call)
        return super().generate_structured(request)


class D3ManifestAndPayloadTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.runner = D3ContractSmokeRunner()
        cls.prepared = cls.runner.prepare_cases()

    def test_manifest_prepares_exactly_twenty_prompt_dev_cases(self) -> None:
        self.assertEqual(20, len(self.prepared))
        self.assertEqual(20, len({item.case_id for item in self.prepared}))
        self.assertEqual(
            {"intent": 5, "clarification": 5, "nl2sql": 5, "grounded_answer": 5},
            dict(
                (task_type, sum(item.task_type == task_type for item in self.prepared))
                for task_type in ("intent", "clarification", "nl2sql", "grounded_answer")
            ),
        )
        self.assertEqual(0, self.runner.manifest["validation_exposure_count"])
        self.assertEqual(
            {"accuracy": False, "generalization": False, "end_to_end": False},
            self.runner.manifest["claims"],
        )

    def test_intent_uses_one_fixed_catalog_with_distractors(self) -> None:
        intent_payloads = [item.payload for item in self.prepared if item.task_type == "intent"]
        catalogs = {tuple(payload["available_sources"]) for payload in intent_payloads}
        self.assertEqual(1, len(catalogs))
        catalog = next(iter(catalogs))
        self.assertIn("chinook_sql", catalog)
        self.assertIn("project_report", catalog)
        self.assertIn("security_policy", catalog)
        self.assertGreaterEqual(len(catalog), 6)

    def test_conditional_payload_scopes_are_explicit(self) -> None:
        by_task = {item.task_type: item for item in self.prepared}
        self.assertIn("required_slots", by_task["clarification"].payload)
        self.assertIn("database_schema", by_task["nl2sql"].payload)
        self.assertGreaterEqual(
            len(by_task["nl2sql"].payload["database_schema"]["tables"]), 10
        )
        self.assertIn("evidence", by_task["grounded_answer"].payload)
        self.assertNotIn("document_id", by_task["grounded_answer"].payload)

    def test_answer_like_fields_cannot_enter_payloads(self) -> None:
        sentinel = "DO-NOT-LEAK-D3-ANSWER-SENTINEL"
        manifest_tasks = {task["task_type"]: task for task in self.runner.manifest["tasks"]}
        for task_type, task in manifest_tasks.items():
            records = _load_jsonl(PROJECT_ROOT / task["source_case_path"])
            case = deepcopy(next(record for record in records if record["id"] == task["case_ids"][0]))
            case["expected"] = {"answer": sentinel}
            case["gold"] = sentinel
            case["label"] = sentinel
            selection = _load_json(PROJECT_ROOT / task["selection_path"])
            payload = build_case_payload(
                task_type,
                case,
                selection=selection,
                database_schema=self.runner.database_schema,
                fixture_catalog=self.runner.fixture_catalog,
            )
            with self.subTest(task_type=task_type):
                self.assertNotIn(sentinel, json.dumps(payload, ensure_ascii=False))

    def test_requests_contain_only_system_prompt_and_allowlisted_payload(self) -> None:
        for item in self.prepared:
            with self.subTest(case=item.case_id):
                self.assertEqual(("system", "user"), tuple(m.role for m in item.request.messages))
                self.assertEqual(item.payload, json.loads(item.request.messages[1].content))
                self.assertEqual(APPROVED_MODEL, self.runner.manifest["model"])
                self.assertEqual(0, item.request.temperature)
                self.assertLessEqual(
                    item.request.max_completion_tokens,
                    self.runner.policy.limits["max_output_tokens_total"],
                )

    def test_offline_preflight_is_complete_and_declares_zero_calls(self) -> None:
        report = self.runner.offline_preflight()
        self.assertEqual(20, report["summary"]["prepared_case_count"])
        self.assertEqual(0, report["summary"]["network_calls"])
        self.assertEqual(0, report["summary"]["api_calls"])
        self.assertEqual(0.0, report["summary"]["cost_cny"])
        provenance = report["provenance"]
        self.assertTrue(provenance["artifact_hashes"])
        self.assertTrue(
            all(len(value) == 64 for value in provenance["artifact_hashes"].values())
        )
        self.assertEqual(self.runner.policy.version, provenance["policy"]["version"])
        self.assertEqual("1.0.0", provenance["dataset"]["version"])
        self.assertEqual(
            {"intent", "clarification", "nl2sql", "grounded_answer"},
            set(provenance["contracts"]),
        )
        fixture_hashes = {
            path for path in provenance["artifact_hashes"] if "fixtures/knowledge" in path
        }
        self.assertEqual(4, len(fixture_hashes))
        self.assertEqual(
            self.runner.batch_limits.as_dict(), report["limits"]["batch"]
        )
        self.assertLessEqual(
            self.runner.batch_limits.max_provider_attempts,
            24,
        )
        self.assertLessEqual(self.runner.batch_limits.hard_cost_cny, 1.0)

    def test_manifest_rejects_capability_claims_or_validation_split(self) -> None:
        manifest = deepcopy(self.runner.manifest)
        invalid_variants = []
        claims = deepcopy(manifest)
        claims["claims"]["accuracy"] = True
        invalid_variants.append(claims)
        split = deepcopy(manifest)
        split["source_split"] = "validation"
        invalid_variants.append(split)
        count = deepcopy(manifest)
        count["tasks"][0]["case_ids"].append("INT-007")
        invalid_variants.append(count)
        for index, invalid in enumerate(invalid_variants):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as temp_dir:
                path = Path(temp_dir) / "manifest.json"
                path.write_text(json.dumps(invalid), encoding="utf-8")
                with self.assertRaises(ContractSmokeError):
                    D3ContractSmokeRunner(path)


class D3MockRunTest(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = D3ContractSmokeRunner()
        self.provider = _MockProvider()
        self.gateway = LLMGateway(
            self.provider,
            self.runner.policy,
            sleeper=lambda _seconds: None,
        )

    def test_mock_gateway_records_twenty_contract_valid_cases(self) -> None:
        report = self.runner.run(self.gateway)
        self.assertEqual(20, len(self.provider.requests))
        self.assertEqual(20, report["summary"]["case_count"])
        self.assertEqual(20, report["summary"]["contract_valid_count"])
        self.assertEqual(0, report["summary"]["failed_count"])
        self.assertEqual(20, report["summary"]["model_attempt_count"])
        self.assertEqual(300, report["summary"]["total_tokens"])
        self.assertEqual(0.0, report["summary"]["estimated_cost_cny"])
        self.assertIsNone(report["summary"]["batch_stop_reason"])
        self.assertTrue(
            all(case["usage_source"] == "usage_ledger_delta" for case in report["cases"])
        )
        self.assertEqual(
            {"accuracy": False, "generalization": False, "end_to_end": False},
            report["claims"],
        )
        first_query = self.runner.prepare_cases()[0].payload["query"]
        self.assertNotIn(first_query, json.dumps(report, ensure_ascii=False))

    def test_failed_attempt_then_success_reports_complete_ledger_delta(self) -> None:
        invalid = ProviderError(
            ProviderErrorCode.INVALID_RESPONSE,
            "invalid response containing DO-NOT-PERSIST",
            retryable=False,
            input_tokens=7,
            output_tokens=3,
            total_tokens=10,
            estimated_cost_cny=0.01,
        )
        provider = _SequenceMockProvider(
            [invalid, {"input_tokens": 11, "output_tokens": 4, "cost": 0.02}]
        )
        gateway = LLMGateway(provider, self.runner.policy, sleeper=lambda _seconds: None)

        report = self.runner.run(gateway, case_limit=1)
        case = report["cases"][0]

        self.assertEqual("contract_valid", case["status"])
        self.assertEqual(2, case["attempt_count"])
        self.assertEqual(18, case["input_tokens"])
        self.assertEqual(7, case["output_tokens"])
        self.assertEqual(25, case["total_tokens"])
        self.assertAlmostEqual(0.03, case["estimated_cost_cny"])
        self.assertEqual("usage_ledger_delta", case["usage_source"])
        self.assertEqual(2, report["summary"]["model_attempt_count"])
        self.assertEqual(25, report["summary"]["total_tokens"])
        self.assertNotIn("DO-NOT-PERSIST", json.dumps(report, ensure_ascii=False))

    def test_usage_ledger_is_reported_as_per_run_delta(self) -> None:
        first = self.runner.run(self.gateway, case_limit=1)
        second = self.runner.run(self.gateway, case_limit=1)

        self.assertEqual(15, first["summary"]["total_tokens"])
        self.assertEqual(15, second["summary"]["total_tokens"])
        self.assertEqual(1, first["summary"]["model_attempt_count"])
        self.assertEqual(1, second["summary"]["model_attempt_count"])

    def test_batch_attempt_budget_stops_later_cases(self) -> None:
        self.runner.batch_limits = replace(
            self.runner.batch_limits, max_provider_attempts=2
        )

        report = self.runner.run(self.gateway)

        self.assertEqual(2, len(self.provider.requests))
        self.assertEqual(2, report["summary"]["attempted_case_count"])
        self.assertEqual(18, report["summary"]["not_run_count"])
        self.assertEqual(
            "batch_model_call_limit", report["summary"]["batch_stop_reason"]
        )
        self.assertTrue(
            all(
                case["stop_reason"] == "batch_model_call_limit"
                for case in report["cases"][2:]
            )
        )

    def test_batch_token_and_cost_budgets_apply_across_cases(self) -> None:
        cases = (
            (
                replace(self.runner.batch_limits, max_input_tokens=15),
                _SequenceMockProvider([None, None]),
                "batch_token_limit",
            ),
            (
                replace(self.runner.batch_limits, hard_cost_cny=0.10),
                _SequenceMockProvider(
                    [
                        {"input_tokens": 10, "output_tokens": 5, "cost": 0.06},
                        {"input_tokens": 10, "output_tokens": 5, "cost": 0.06},
                    ]
                ),
                "batch_cost_limit",
            ),
        )
        for limits, provider, expected_reason in cases:
            with self.subTest(stop_reason=expected_reason):
                runner = D3ContractSmokeRunner()
                runner.batch_limits = limits
                gateway = LLMGateway(
                    provider, runner.policy, sleeper=lambda _seconds: None
                )
                report = runner.run(gateway)
                self.assertEqual(2, len(provider.requests))
                self.assertEqual(expected_reason, report["summary"]["batch_stop_reason"])
                self.assertGreater(report["summary"]["not_run_count"], 0)

    def test_batch_wall_time_budget_stops_later_cases(self) -> None:
        clock = _FakeClock()
        runner = D3ContractSmokeRunner()
        runner.batch_limits = replace(runner.batch_limits, max_wall_time_ms=100)
        provider = _AdvancingMockProvider(clock, 0.06)
        gateway = LLMGateway(
            provider,
            runner.policy,
            sleeper=lambda _seconds: None,
            clock=clock,
        )

        report = runner.run(gateway, clock=clock)

        self.assertEqual(2, len(provider.requests))
        self.assertEqual("batch_wall_time_limit", report["summary"]["batch_stop_reason"])
        self.assertEqual(18, report["summary"]["not_run_count"])

    def test_provider_failures_use_stable_safe_error_codes(self) -> None:
        scenarios = (
            (ProviderErrorCode.INVALID_REQUEST, False, "invalid_request"),
            (ProviderErrorCode.AUTHENTICATION, False, "authentication_error"),
            (ProviderErrorCode.AUTHORIZATION, False, "authorization_error"),
            (ProviderErrorCode.RATE_LIMIT, True, "rate_limit"),
            (ProviderErrorCode.TIMEOUT, True, "timeout"),
            (ProviderErrorCode.QUOTA, False, "quota_error"),
        )
        for code, retryable, expected in scenarios:
            failure = ProviderError(
                code,
                "safe category only; secret=DO-NOT-PERSIST",
                retryable=retryable,
            )
            outcomes = [failure, failure, failure] if retryable else [failure]
            provider = _SequenceMockProvider(outcomes)
            gateway = LLMGateway(
                provider, self.runner.policy, sleeper=lambda _seconds: None
            )
            with self.subTest(code=code):
                report = self.runner.run(gateway, case_limit=1)
                case = report["cases"][0]
                self.assertEqual("failed", case["status"])
                self.assertEqual(expected, case["error_code"])
                self.assertNotIn("DO-NOT-PERSIST", json.dumps(report, ensure_ascii=False))

        budget_failure = ProviderError(
            ProviderErrorCode.RATE_LIMIT,
            "budget-triggering response",
            retryable=True,
            input_tokens=30001,
            output_tokens=1,
            total_tokens=30002,
        )
        provider = _SequenceMockProvider([budget_failure])
        gateway = LLMGateway(provider, self.runner.policy, sleeper=lambda _seconds: None)
        report = self.runner.run(gateway, case_limit=1)
        self.assertEqual("policy_limit", report["cases"][0]["error_code"])
        self.assertEqual("token_limit", report["cases"][0]["stop_reason"])

    def test_success_report_has_stable_non_content_output_proof(self) -> None:
        report = self.runner.run(self.gateway, case_limit=1)
        proof = report["cases"][0]["output_validation"]
        request = self.provider.requests[0]
        expected_output = _mock_output(request)
        canonical = json.dumps(
            expected_output,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        expected_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

        self.assertEqual("intent-result.v1", proof["schema_id"])
        self.assertEqual("1.0.0", proof["schema_version"])
        self.assertEqual("passed", proof["json_schema_validation"])
        self.assertEqual("passed", proof["semantic_validation"])
        self.assertEqual(expected_hash, proof["normalized_output_sha256"])
        self.assertEqual(64, len(proof["normalized_output_sha256"]))
        serialized = json.dumps(report, ensure_ascii=False)
        self.assertNotIn(json.dumps(expected_output, ensure_ascii=False), serialized)

    def test_mock_outputs_are_contract_smoke_not_answer_scoring(self) -> None:
        report = self.runner.run(self.gateway)
        self.assertEqual("contract_smoke_only", report["purpose"])
        self.assertIn("contract_valid_rate", report["summary"])
        self.assertNotIn("accuracy", report["summary"])
        self.assertNotIn("generalization", report["summary"])
        self.assertTrue(
            all("evaluation_scope" in case for case in report["cases"])
        )

    def test_case_limit_is_explicit_and_bounded(self) -> None:
        report = self.runner.run(self.gateway, case_limit=3)
        self.assertEqual(3, report["summary"]["case_count"])
        for invalid in (0, 21, True, "3"):
            with self.subTest(case_limit=invalid), self.assertRaises(ContractSmokeError):
                self.runner.run(self.gateway, case_limit=invalid)

    def test_unapproved_gateway_model_fails_before_any_call(self) -> None:
        self.provider.model = "another-model"
        with self.assertRaises(ContractSmokeError):
            self.runner.run(self.gateway)
        self.assertEqual([], self.provider.requests)

    def test_all_task_schemas_reject_extra_root_fields(self) -> None:
        for item in self.runner.prepare_cases():
            invalid = _mock_output(item.request)
            invalid["raw_model_output"] = "forbidden"
            with self.subTest(task=item.task_type), self.assertRaises(Exception):
                self.runner.validate_result(item, invalid)

    def test_nl2sql_rejects_table_outside_input_schema(self) -> None:
        item = next(
            case for case in self.runner.prepare_cases() if case.task_type == "nl2sql"
        )
        invalid = _mock_output(item.request)
        invalid["data"]["tables"] = ["InventedTable"]
        with self.assertRaises(ContractSmokeError):
            self.runner.validate_result(item, invalid)

    def test_grounded_answer_rejects_unknown_or_inexact_citation(self) -> None:
        item = next(
            case
            for case in self.runner.prepare_cases()
            if case.task_type == "grounded_answer"
        )
        evidence_id = item.payload["evidence"][0]["evidence_id"]
        result = _success_envelope(
            "grounded_answer",
            {
                "answerable": True,
                "answer": "mock",
                "evidence_ids": [evidence_id],
                "citations": [
                    {"evidence_id": evidence_id, "quote": "not an exact source quote"}
                ],
                "insufficient_reason": None,
            },
        )
        with self.assertRaises(ContractSmokeError):
            self.runner.validate_result(item, result)

    def test_runner_does_not_execute_candidate_sql(self) -> None:
        report = self.runner.run(self.gateway)
        sql_cases = [case for case in report["cases"] if case["task_type"] == "nl2sql"]
        self.assertEqual(5, len(sql_cases))
        self.assertTrue(all(case["status"] == "contract_valid" for case in sql_cases))
        self.assertTrue(
            all(
                "candidate_sql" not in case
                and "parsed_output" not in case
                and "query" not in case
                for case in sql_cases
            ),
            "sanitized report must not persist candidate SQL",
        )


if __name__ == "__main__":
    unittest.main()
