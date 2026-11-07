"""D4-04 automatic slot discovery and selective-routing offline tests."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from backend.agent.domain_pack import DomainPackRegistry  # noqa: E402
from backend.agent.intent_routing import (  # noqa: E402
    apply_intent_result,
    create_conversation_state,
    load_source_catalog,
)
from backend.agent.selective_routing import (  # noqa: E402
    DEFAULT_POLICY_PATH,
    D4SelectiveDecisionRuntime,
    RequiredSlotResolver,
    SelectiveDecisionPolicy,
    SelectiveRoutingError,
    build_slot_observations,
)
from backend.agent.typed_decision_runtime import (  # noqa: E402
    CandidateRetriever,
)


VALIDATOR_PATH = (
    PROJECT_ROOT / "tools" / "evaluation" / "validate_d4_selective_routing.py"
)
REPORT_PATH = PROJECT_ROOT / "tests" / "reports" / "d4-selective-routing-v1.json"
VALIDATOR_SPEC = importlib.util.spec_from_file_location(
    "validate_d4_selective_routing", VALIDATOR_PATH
)
if VALIDATOR_SPEC is None or VALIDATOR_SPEC.loader is None:
    raise RuntimeError("无法加载 D4-04 离线校验器")
VALIDATOR_MODULE = importlib.util.module_from_spec(VALIDATOR_SPEC)
VALIDATOR_SPEC.loader.exec_module(VALIDATOR_MODULE)
validate_all = VALIDATOR_MODULE.validate_all


RECORDED_AT = "2026-09-27T08:00:00Z"


def _thaw(value):
    if isinstance(value, dict) or hasattr(value, "items"):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(item) for item in value]
    return value


def _intent_result(
    *,
    intent: str = "sql",
    needs_clarification: bool = False,
    missing_slots: list[str] | None = None,
) -> dict[str, object]:
    sources = {
        "sql": ["chinook_sql"],
        "rag": ["security_policy"],
        "cross_source": ["chinook_sql", "project_report"],
        "unknown": [],
    }[intent]
    return {
        "schema_version": "1.0.0",
        "task_type": "intent",
        "status": "success",
        "data": {
            "intent": intent,
            "needs_clarification": needs_clarification,
            "required_sources": sources,
            "missing_slots": missing_slots or [],
            "reason": (
                "ambiguous"
                if needs_clarification
                else "outside_available_sources"
                if intent == "unknown"
                else "routable"
            ),
            "normalized_query": None,
        },
        "error": None,
        "warnings": [],
    }


def _slot_state(**values: object):
    evidence = {
        "metric": "evidence.metric_kinds",
        "entity_type": "evidence.entity_kinds",
        "time_range": "evidence.time_range_years",
        "country": "evidence.country_enum",
        "limit": "evidence.limit_range",
        "grain": "evidence.grain_kinds",
        "sort": "evidence.sort_kinds",
        "comparison_baseline": "slot.comparison_baseline",
    }
    payload = {}
    for short_name, value in values.items():
        payload[f"slot.{short_name}"] = {
            "values": value if isinstance(value, list) else [value],
            "evidence_refs": [evidence[short_name]],
        }
    return build_slot_observations(payload)


class D4SelectiveRoutingBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.pack = DomainPackRegistry().load("chinook_music_store", "2.0.0")
        cls.retriever = CandidateRetriever(cls.pack)
        cls.candidates = cls.retriever.retrieve("艺术家", top_k=5)
        cls.resolver = RequiredSlotResolver(cls.pack)
        cls.policy = SelectiveDecisionPolicy(cls.pack)

    def resolve(self, observations, candidates=None, intent="sql"):
        return self.resolver.resolve(
            intent=intent,
            candidates=self.candidates if candidates is None else candidates,
            observations=observations,
        )

    def decide(self, observations, *, score=None, candidates=None, intent="sql"):
        selected = tuple(self.candidates if candidates is None else candidates)
        if score is not None:
            selected = (replace(selected[0], raw_score=score),)
        resolution = self.resolve(observations, selected, intent)
        return self.policy.decide(
            intent=intent,
            candidates=selected,
            resolution=resolution,
            request_id="req.test.d4-04",
            recorded_at=RECORDED_AT,
        )


class SlotObservationBuilderTest(D4SelectiveRoutingBase):
    def test_strict_builder_accepts_only_allowlisted_fields(self) -> None:
        result = _slot_state(metric="count")
        self.assertEqual(("count",), result["slot.metric"].values)
        self.assertEqual(
            ("evidence.metric_kinds",),
            result["slot.metric"].evidence_refs,
        )

    def test_answer_derived_fields_are_rejected(self) -> None:
        for forbidden in ("expected", "gold_sql", "label", "target_answer"):
            with self.subTest(field=forbidden):
                payload = {
                    "slot.metric": {
                        "values": ["count"],
                        "evidence_refs": ["evidence.metric_kinds"],
                        forbidden: "TESTONLY",
                    }
                }
                with self.assertRaisesRegex(
                    SelectiveRoutingError, "answer_derived_field_forbidden"
                ):
                    build_slot_observations(payload)

    def test_unknown_observation_fields_and_slots_fail_closed(self) -> None:
        with self.assertRaisesRegex(
            SelectiveRoutingError, "invalid_slot_observation_fields"
        ):
            build_slot_observations(
                {
                    "slot.metric": {
                        "values": ["count"],
                        "evidence_refs": ["evidence.metric_kinds"],
                        "note": "TESTONLY",
                    }
                }
            )
        with self.assertRaisesRegex(
            SelectiveRoutingError, "unknown_slot_observation"
        ):
            self.resolve(
                build_slot_observations(
                    {
                        "slot.not_real": {
                            "values": ["x"],
                            "evidence_refs": [],
                        }
                    }
                )
            )

    def test_illegal_or_unknown_evidence_reference_fails_closed(self) -> None:
        for reference, code in (
            ("evidence.not_real", "slot_evidence_unknown"),
            ("evidence.country_enum", "slot_evidence_not_allowed"),
        ):
            with self.subTest(reference=reference):
                observations = build_slot_observations(
                    {
                        "slot.metric": {
                            "values": ["count"],
                            "evidence_refs": [reference],
                        }
                    }
                )
                with self.assertRaisesRegex(SelectiveRoutingError, code):
                    self.resolve(observations)


class RequiredSlotResolverTest(D4SelectiveRoutingBase):
    def test_default_required_slot_is_discovered_without_external_list(self) -> None:
        resolution = self.resolve({})
        by_id = resolution.by_id()
        self.assertTrue(by_id["slot.metric"].required)
        self.assertEqual("missing", by_id["slot.metric"].status)
        self.assertIn("required_by_default", by_id["slot.metric"].reason_codes)

    def test_conditional_required_slots_follow_domain_pack_conditions(self) -> None:
        cases = (
            (_slot_state(metric="count"), "slot.entity_type"),
            (_slot_state(metric="revenue"), "slot.time_range"),
            (_slot_state(metric="growth_rate"), "slot.grain"),
            (_slot_state(metric="growth_rate"), "slot.comparison_baseline"),
            (_slot_state(metric="count", entity_type="artist", limit=5), "slot.sort"),
        )
        for observations, slot_id in cases:
            with self.subTest(slot_id=slot_id):
                assessment = self.resolve(observations).by_id()[slot_id]
                self.assertTrue(assessment.required)
                self.assertIn("required_by_condition", assessment.reason_codes)

    def test_ranking_signal_requires_limit_only_for_top_n_queries(self) -> None:
        # 正向：明确排名词（排名/排行/top）触发前N条数槽位。
        for query in ("排名前10的歌曲", "销量排行前10", "Top 10 曲目"):
            with self.subTest(query=query):
                ranking_candidates = self.retriever.retrieve(query, top_k=5)
                positive = self.resolve(
                    _slot_state(metric="unit_price"), candidates=ranking_candidates
                )
                limit = positive.by_id()["slot.limit"]
                self.assertTrue(limit.required)
                self.assertIn("required_by_condition", limit.reason_codes)

        # 负向：标量极值（最高/最低）与普通聚合不触发前N条数槽位。
        for query in ("最高单价是多少", "最低单价是多少", "平均单价是多少"):
            with self.subTest(query=query):
                plain_candidates = self.retriever.retrieve(query, top_k=5)
                negative = self.resolve(
                    _slot_state(metric="unit_price"), candidates=plain_candidates
                )
                limit = negative.by_id()["slot.limit"]
                self.assertFalse(limit.required)
                self.assertIn("not_required", limit.reason_codes)

    def test_geo_filter_signal_requires_country_only_for_geo_queries(self) -> None:
        # 正向：缺失国家/地区的筛选意图（哪个国家/哪些国家）触发国家槽位。
        geo_candidates = self.retriever.retrieve("哪个国家的销量最高", top_k=5)
        positive = self.resolve(
            _slot_state(metric="count", entity_type="customer"),
            candidates=geo_candidates,
        )
        country = positive.by_id()["slot.country"]
        self.assertTrue(country.required)
        self.assertIn("required_by_condition", country.reason_codes)

        # 负向：地域分组（各国家/按国家）与无地域查询不触发国家槽位。
        for query in ("各国家的客户数", "按国家分组的客户数", "客户总数是多少"):
            with self.subTest(query=query):
                plain_candidates = self.retriever.retrieve(query, top_k=5)
                negative = self.resolve(
                    _slot_state(metric="count", entity_type="customer"),
                    candidates=plain_candidates,
                )
                country = negative.by_id()["slot.country"]
                self.assertFalse(country.required)
                self.assertIn("not_required", country.reason_codes)

    def test_candidate_condition_and_intent_are_part_of_requirement_resolution(self) -> None:
        artifacts = _thaw(self.pack.artifacts)
        condition = artifacts["slots"]["items"][3]["required_when"][0]
        condition["subject_ref"] = "concept.artist"
        condition["operator"] = "present"
        condition["values"] = []
        mutated = replace(self.pack, artifacts=artifacts)
        resolver = RequiredSlotResolver(mutated)
        sql_resolution = resolver.resolve(
            intent="sql",
            candidates=self.candidates,
            observations=_slot_state(metric="count", entity_type="artist"),
        )
        self.assertTrue(sql_resolution.by_id()["slot.country"].required)
        rag_resolution = resolver.resolve(
            intent="rag",
            candidates=self.candidates,
            observations={},
        )
        self.assertTrue(
            all(item.required is False for item in rag_resolution.assessments)
        )
        self.assertTrue(
            all("intent_not_applicable" in item.reason_codes for item in rag_resolution.assessments)
        )

    def test_complete_explicit_state_is_filled_and_non_required_is_distinct(self) -> None:
        resolution = self.resolve(
            _slot_state(metric="count", entity_type="artist")
        )
        by_id = resolution.by_id()
        self.assertEqual("filled", by_id["slot.metric"].status)
        self.assertEqual("filled", by_id["slot.entity_type"].status)
        self.assertFalse(by_id["slot.time_range"].required)
        self.assertEqual("missing", by_id["slot.time_range"].status)

    def test_absent_condition_subject_is_indeterminate_not_sufficient(self) -> None:
        resolution = self.resolve({})
        entity = resolution.by_id()["slot.entity_type"]
        self.assertIsNone(entity.required)
        self.assertEqual("unknown", entity.status)
        self.assertIn("requirement_indeterminate", entity.reason_codes)

    def test_multiple_values_are_conflicting(self) -> None:
        resolution = self.resolve(
            _slot_state(metric=["count", "revenue"])
        )
        metric = resolution.by_id()["slot.metric"]
        self.assertEqual("conflicting", metric.status)
        self.assertEqual(2, metric.value_count)

    def test_missing_or_outside_evidence_is_unknown(self) -> None:
        no_evidence = build_slot_observations(
            {"slot.metric": {"values": ["count"], "evidence_refs": []}}
        )
        self.assertEqual(
            "unknown", self.resolve(no_evidence).by_id()["slot.metric"].status
        )
        outside = _slot_state(metric="TESTONLY_not_a_metric")
        resolution = self.resolve(outside)
        self.assertEqual("unknown", resolution.by_id()["slot.metric"].status)
        self.assertIn("rule.metric_must_be_known", resolution.hard_rule_violations)

    def test_unknown_required_when_operator_and_dangling_reference_fail_closed(self) -> None:
        for mutation, code in (
            ("operator", "unsupported_required_when_operator"),
            ("reference", "required_when_dangling_reference"),
        ):
            with self.subTest(mutation=mutation):
                artifacts = _thaw(self.pack.artifacts)
                condition = artifacts["slots"]["items"][1]["required_when"][0]
                if mutation == "operator":
                    condition["operator"] = "TESTONLY_unknown"
                else:
                    condition["subject_ref"] = "slot.not_real"
                mutated = replace(self.pack, artifacts=artifacts)
                resolver = RequiredSlotResolver(mutated)
                with self.assertRaisesRegex(SelectiveRoutingError, code):
                    resolver.resolve(
                        intent="sql",
                        candidates=self.candidates,
                        observations=_slot_state(metric="count"),
                    )

    def test_unknown_validation_operator_fails_closed(self) -> None:
        artifacts = _thaw(self.pack.artifacts)
        artifacts["validation_rules"]["items"][0]["operator"] = "all"
        mutated = replace(self.pack, artifacts=artifacts)
        resolver = RequiredSlotResolver(mutated)
        with self.assertRaisesRegex(
            SelectiveRoutingError, "unsupported_validation_operator"
        ):
            resolver.resolve(
                intent="sql",
                candidates=self.candidates,
                observations=_slot_state(metric="count", entity_type="artist"),
            )


class SelectiveDecisionPolicyTest(D4SelectiveRoutingBase):
    def setUp(self) -> None:
        self.complete = _slot_state(metric="count", entity_type="artist")

    def test_accept_boundary_is_inclusive_and_never_authorizes_sql(self) -> None:
        result = self.decide(self.complete, score=0.75)
        self.assertEqual("accept", result["action"])
        self.assertEqual("accepted_at_raw_score_threshold", result["reason_code"])
        self.assertFalse(result["sql_execution_authorized"])
        self.assertTrue(result["requires_d4_02_safety_gate"])
        self.assertEqual("choice", result["typed_decision"]["decision_type"])

    def test_just_below_accept_escalates(self) -> None:
        result = self.decide(self.complete, score=0.749999)
        self.assertEqual("escalate", result["action"])
        self.assertEqual("raw_score_below_accept", result["reason_code"])

    def test_abstain_boundary_is_strict(self) -> None:
        at_boundary = self.decide(self.complete, score=0.25)
        below = self.decide(self.complete, score=0.249999)
        self.assertEqual("escalate", at_boundary["action"])
        self.assertEqual("reject", below["action"])
        self.assertEqual("raw_score_below_abstain", below["reason_code"])
        self.assertEqual("abstain", below["typed_decision"]["decision_type"])

    def test_missing_and_conflicting_hard_slots_override_score(self) -> None:
        missing = self.decide({}, score=0.99)
        conflicting = self.decide(
            _slot_state(metric=["count", "revenue"]), score=0.99
        )
        self.assertEqual("clarify", missing["action"])
        self.assertEqual("required_slot_missing", missing["reason_code"])
        self.assertEqual("clarify", conflicting["action"])
        self.assertEqual("slot_conflicting", conflicting["reason_code"])
        self.assertTrue(
            missing["typed_decision"]["missing_or_conflicting_slots"]
        )

    def test_unknown_value_and_hard_rule_escalate_fail_closed(self) -> None:
        result = self.decide(
            _slot_state(metric="TESTONLY_not_a_metric"), score=0.99
        )
        self.assertEqual("escalate", result["action"])
        self.assertEqual("hard_rule_failed", result["reason_code"])

    def test_no_candidate_abstains_and_rejects(self) -> None:
        result = self.decide(self.complete, candidates=())
        self.assertEqual("reject", result["action"])
        self.assertEqual("outside_candidate_set", result["reason_code"])
        self.assertEqual("abstain", result["typed_decision"]["decision_type"])
        self.assertEqual(
            "outside_candidate_set", result["typed_decision"]["abstain_reason"]
        )

    def test_unknown_and_unsupported_intents_cover_reject_and_escalate(self) -> None:
        unknown = self.decide(self.complete, intent="unknown")
        rag = self.decide(self.complete, intent="rag")
        self.assertEqual(("reject", "unknown_intent"), (unknown["action"], unknown["reason_code"]))
        self.assertEqual(("escalate", "unsupported_intent"), (rag["action"], rag["reason_code"]))
        self.assertEqual("abstain", rag["typed_decision"]["decision_type"])

    def test_soft_rule_uses_inclusive_clarify_boundary(self) -> None:
        state = _slot_state(
            metric="count", entity_type="artist", grain="month"
        )
        at_boundary = self.decide(state, score=0.5)
        below = self.decide(state, score=0.499999)
        self.assertEqual("clarify", at_boundary["action"])
        self.assertEqual(
            "soft_rule_requires_clarification", at_boundary["reason_code"]
        )
        self.assertEqual("escalate", below["action"])
        self.assertEqual(
            "soft_rule_below_clarify_threshold", below["reason_code"]
        )

    def test_equal_top_scores_escalate_with_stable_reason(self) -> None:
        distinct = self.retriever.retrieve("artist album", top_k=2, candidate_kind="concept")
        candidates = sorted(
            (replace(item, raw_score=0.8) for item in distinct),
            key=lambda item: item.candidate_id,
        )
        self.assertEqual(2, len(candidates))
        result = self.decide(self.complete, candidates=tuple(candidates))
        self.assertEqual("escalate", result["action"])
        self.assertEqual("candidate_score_tie", result["reason_code"])
        self.assertEqual("abstain", result["typed_decision"]["decision_type"])

    def test_same_entity_concept_and_table_tie_is_not_ambiguity(self) -> None:
        candidates = tuple(replace(item, raw_score=0.8) for item in self.candidates[:2])
        self.assertEqual({"concept.artist", "schema.chinook.artist"},
                         {item.candidate_id for item in candidates})
        result = self.decide(self.complete, candidates=candidates)
        self.assertEqual("accept", result["action"])

    def test_all_four_actions_are_reachable(self) -> None:
        actions = {
            self.decide(self.complete, score=0.75)["action"],
            self.decide({}, score=0.9)["action"],
            self.decide(self.complete, score=0.5)["action"],
            self.decide(self.complete, score=0.1)["action"],
        }
        self.assertEqual({"accept", "clarify", "escalate", "reject"}, actions)

    def test_result_is_deterministic_and_all_evidence_refs_resolve(self) -> None:
        first = self.decide(self.complete, score=0.75)
        second = self.decide(self.complete, score=0.75)
        self.assertEqual(first, second)
        refs = first["typed_decision"]["evidence_refs"]
        for reference in refs:
            self.pack.resolve(reference)
        self.assertEqual(
            first["action"], first["trace_event"]["executed_action"]
        )
        self.assertIsNone(
            first["trace_event"]["component_versions"]["calibrator"]
        )

    def test_policy_threshold_order_and_unknown_result_fields_are_rejected(self) -> None:
        policy = json.loads(DEFAULT_POLICY_PATH.read_text(encoding="utf-8"))
        policy["thresholds"]["accept_min"] = 0.4
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policy.json"
            path.write_text(json.dumps(policy), encoding="utf-8")
            with self.assertRaisesRegex(
                SelectiveRoutingError, "invalid_threshold_order"
            ):
                SelectiveDecisionPolicy(self.pack, policy_path=path)

        result = self.decide(self.complete, score=0.75)
        tampered = deepcopy(result)
        tampered["sql"] = "TESTONLY forbidden"
        with self.assertRaisesRegex(
            SelectiveRoutingError, "selective_result_schema_invalid"
        ):
            self.policy.validate_result(tampered)


class D4SelectiveVerticalSliceTest(D4SelectiveRoutingBase):
    def test_d4_01_to_d4_03_to_d4_04_ignores_external_required_slots(self) -> None:
        catalog = load_source_catalog()
        state = create_conversation_state("session.d4-04", "艺术家")
        routed = apply_intent_result(
            state,
            catalog,
            _intent_result(
                needs_clarification=True,
                missing_slots=["time_range"],
            ),
        )
        runtime = D4SelectiveDecisionRuntime(self.pack)
        result = runtime.evaluate(
            conversation_state=routed,
            slot_observations=_slot_state(metric="count", entity_type="artist"),
            request_id="req.vertical.d4-04",
            recorded_at=RECORDED_AT,
        )
        self.assertEqual("auto_domain_pack", result["required_slots_source"])
        self.assertEqual("accept", result["action"])
        by_id = {item["slot_id"]: item for item in result["slot_assessments"]}
        self.assertFalse(by_id["slot.time_range"]["required"])
        self.assertFalse(result["sql_execution_authorized"])

    def test_vertical_result_contains_no_query_or_slot_values(self) -> None:
        catalog = load_source_catalog()
        state = apply_intent_result(
            create_conversation_state("session.redaction", "TESTONLY私密短语 艺术家"),
            catalog,
            _intent_result(),
        )
        result = D4SelectiveDecisionRuntime(self.pack).evaluate(
            conversation_state=state,
            slot_observations=_slot_state(metric="count", entity_type="artist"),
            request_id="req.redaction.d4-04",
            recorded_at=RECORDED_AT,
        )
        encoded = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("TESTONLY私密短语", encoded)
        self.assertNotIn('"values"', encoded)
        for forbidden in ("expected", "gold_sql", "target_answer"):
            self.assertNotIn(forbidden, encoded.casefold())


class D4SelectiveMachineReportTest(unittest.TestCase):
    def test_machine_report_separates_conditional_and_vertical_evidence(self) -> None:
        report = validate_all()
        self.assertEqual("pass", report["status"])
        self.assertEqual(10, report["conditional_component_evidence"]["scenario_count"])
        self.assertEqual(3, report["decision_vertical_slice_evidence"]["scenario_count"])
        self.assertEqual(
            {"accept", "clarify", "escalate", "reject"},
            {
                action
                for action, count in report["policy"]["action_counts"].items()
                if count
            },
        )
        self.assertFalse(
            report["decision_vertical_slice_evidence"][
                "d3_external_required_slots_used"
            ]
        )
        self.assertFalse(
            report["decision_vertical_slice_evidence"][
                "d4_05_clarification_recovery_included"
            ]
        )

    def test_machine_report_is_offline_content_safe_and_uncalibrated(self) -> None:
        report = validate_all()
        self.assertEqual(
            report,
            json.loads(REPORT_PATH.read_text(encoding="utf-8")),
        )
        self.assertEqual("uncalibrated_raw_score", report["policy"]["mode"])
        self.assertFalse(report["policy"]["empirically_calibrated"])
        self.assertEqual(8, report["fail_closed_probes"]["count"])
        self.assertFalse(
            report["safety_boundary"]["accept_authorizes_sql_execution"]
        )
        self.assertTrue(report["safety_boundary"]["requires_d4_02_safety_gate"])
        for key in (
            "network_evaluation_calls",
            "model_api_calls",
            "credential_files_read",
            "sql_executions",
            "estimated_cost_cny",
        ):
            self.assertEqual(0, report[key])
        encoded = json.dumps(report, ensure_ascii=False).casefold()
        self.assertNotIn('"query"', encoded)
        self.assertNotIn('"values"', encoded)


if __name__ == "__main__":
    unittest.main()
