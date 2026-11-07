"""Deterministic D4-03 candidate evidence and typed-contract wiring.

The scores produced here are reproducible lexical feature scores.  They are
never calibration probabilities and this module does not choose thresholds or
actions on a caller's behalf.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError, ValidationError
from referencing import Registry, Resource

from .domain_pack import DEFAULT_SCHEMA_ROOT, DomainPackError, LoadedDomainPack


CANDIDATE_RETRIEVER_VERSION = "candidate-retriever.v1"
EVIDENCE_ASSEMBLER_VERSION = "evidence-feature-assembler.v1"
TYPED_DECISION_CONTRACT_VERSION = "1.0.0"
TRACE_VERSION = "1.0.0"
MAX_TOP_K = 50
_CANDIDATE_ROLES = frozenset({"concepts", "schema_catalog", "slots"})
_CANDIDATE_KINDS = frozenset({"all", "concept", "schema_object", "slot"})
_ROLE_TO_KIND = {
    "concepts": "concept",
    "schema_catalog": "schema_object",
    "slots": "slot",
}


class TypedDecisionRuntimeError(RuntimeError):
    """Content-safe candidate or typed-decision failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _normalize(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _phrase_hit(query: str, phrase: str) -> bool:
    return bool(_phrase_spans(query, phrase))


def _phrase_spans(query: str, phrase: str) -> tuple[tuple[int, int], ...]:
    normalized = _normalize(phrase)
    if not normalized:
        return ()
    if normalized.isascii() and normalized.replace(" ", "").isalnum():
        # Complete tokens avoid matching Track inside PlaylistTrack/TrackId;
        # plural entity names still resolve to their curated table alias.
        pattern = rf"(?<![a-z0-9_]){re.escape(normalized)}(?:s|es)?(?![a-z0-9_])"
        return tuple(match.span() for match in re.finditer(pattern, query))
    return tuple(
        (match.start(), match.start() + len(normalized))
        for match in re.finditer(f"(?={re.escape(normalized)})", query)
    )


def _score(features: Mapping[str, int | float]) -> float:
    weighted = (
        0.40 * float(features.get("display_name_hit", 0))
        + 0.35 * min(float(features.get("alias_hit_count", 0)), 2.0)
        + 0.40 * float(features.get("alias_schema_corroboration", 0))
        + 0.30 * float(features.get("physical_name_hit", 0))
        + 0.15 * min(float(features.get("value_evidence_hit_count", 0)), 2.0)
        + 0.10 * min(float(features.get("schema_link_count", 0)), 2.0)
        + 0.05 * min(float(features.get("relation_hit_count", 0)), 2.0)
    )
    return round(min(weighted, 1.0), 6)


@dataclass(frozen=True)
class CandidateEvidence:
    candidate_id: str
    display_name: str
    candidate_kind: str
    raw_score: float
    evidence_refs: tuple[str, ...]
    raw_features: Mapping[str, int | float | str | bool | None]


class CandidateRetriever:
    """Bounded lexical/alias retrieval over one validated domain pack."""

    def __init__(self, domain_pack: LoadedDomainPack) -> None:
        self.domain_pack = domain_pack
        self._concepts = tuple(domain_pack.artifact_items("concepts"))
        self._aliases = tuple(domain_pack.artifact_items("aliases"))
        self._slots = tuple(domain_pack.artifact_items("slots"))
        self._objects = tuple(domain_pack.artifact_items("schema_catalog"))
        self._relations = tuple(domain_pack.artifact_items("relations"))
        self._evidence = tuple(domain_pack.artifact_items("value_evidence"))
        self._rules = tuple(domain_pack.artifact_items("validation_rules"))

    def retrieve(
        self,
        query: str,
        *,
        top_k: int = 5,
        candidate_kind: str = "all",
    ) -> tuple[CandidateEvidence, ...]:
        if not isinstance(query, str) or not query.strip():
            raise TypedDecisionRuntimeError("query_required")
        if not isinstance(top_k, int) or isinstance(top_k, bool) or not 1 <= top_k <= MAX_TOP_K:
            raise TypedDecisionRuntimeError("invalid_top_k")
        if candidate_kind not in _CANDIDATE_KINDS:
            raise TypedDecisionRuntimeError("invalid_candidate_kind")

        normalized_query = _normalize(query)
        concept_alias_hits = self._concept_alias_hits(normalized_query)
        direct_object_hits = self._direct_object_hits(normalized_query)
        candidates: list[CandidateEvidence] = []
        if candidate_kind in {"all", "concept"}:
            candidates.extend(
                self._concept_candidates(
                    normalized_query, concept_alias_hits, direct_object_hits
                )
            )
        if candidate_kind in {"all", "schema_object"}:
            candidates.extend(
                self._schema_candidates(
                    normalized_query, concept_alias_hits, direct_object_hits
                )
            )
        if candidate_kind in {"all", "slot"}:
            candidates.extend(self._slot_candidates(normalized_query))
        candidates.sort(key=lambda item: (-item.raw_score, item.candidate_id))
        return tuple(candidates[:top_k])

    def _concept_alias_hits(self, query: str) -> dict[str, tuple[str, ...]]:
        matched: list[tuple[str, str, str, tuple[tuple[int, int], ...]]] = []
        for alias in self._aliases:
            surface = _normalize(str(alias["surface_form"]))
            spans = _phrase_spans(query, surface)
            if spans:
                matched.append(
                    (
                        str(alias["concept_id"]),
                        str(alias["alias_id"]),
                        surface,
                        spans,
                    )
                )

        # Prefer the most specific overlapping alias.  For example, the
        # curated alias "媒体类型" must not also activate the generic "类型"
        # alias from another concept merely because it is a substring.
        specific = [
            item
            for item in matched
            if any(
                not any(
                    item[2] != other[2]
                    and other_start <= start
                    and end <= other_end
                    for other in matched
                    for other_start, other_end in other[3]
                )
                for start, end in item[3]
            )
        ]
        hits: dict[str, list[str]] = {}
        for concept_id, alias_id, _surface, _spans in specific:
            hits.setdefault(concept_id, []).append(alias_id)
        return {key: tuple(sorted(value)) for key, value in hits.items()}

    def _direct_object_hits(self, query: str) -> set[str]:
        return {
            str(item["object_id"])
            for item in self._objects
            if _phrase_hit(query, str(item["display_name"]))
            or _phrase_hit(query, str(item["physical_name"]))
        }

    def _concept_candidates(
        self,
        query: str,
        alias_hits: Mapping[str, tuple[str, ...]],
        object_hits: set[str],
    ) -> list[CandidateEvidence]:
        objects_by_concept: dict[str, list[Mapping[str, Any]]] = {}
        for item in self._objects:
            for concept_id in item["concept_refs"]:
                objects_by_concept.setdefault(str(concept_id), []).append(item)
        result: list[CandidateEvidence] = []
        for concept in self._concepts:
            concept_id = str(concept["concept_id"])
            display_hit = int(_phrase_hit(query, str(concept["display_name"])))
            aliases = alias_hits.get(concept_id, ())
            linked_objects = objects_by_concept.get(concept_id, [])
            direct_matches = {
                str(item["object_id"])
                for item in linked_objects
                if str(item["object_id"]) in object_hits
            }
            alias_schema_matches = {
                str(item["object_id"])
                for item in linked_objects
                if aliases
                and not display_hit
                and str(item["object_type"]) == "table"
            }
            matched_objects = sorted(direct_matches | alias_schema_matches)
            if not display_hit and not aliases and not matched_objects:
                continue
            features: dict[str, int | float | str | bool | None] = {
                "candidate_kind": "concept",
                "display_name_hit": display_hit,
                "alias_hit_count": len(aliases),
                "alias_schema_corroboration": int(bool(alias_schema_matches)),
                "physical_name_hit": 0,
                "schema_link_count": len(matched_objects),
                "relation_hit_count": 0,
                "value_evidence_hit_count": 0,
            }
            evidence_refs = tuple(
                sorted({concept_id, *aliases, *matched_objects})
            )
            result.append(
                CandidateEvidence(
                    candidate_id=concept_id,
                    display_name=str(concept["display_name"]),
                    candidate_kind="concept",
                    raw_score=_score(features),
                    evidence_refs=evidence_refs,
                    raw_features=features,
                )
            )
        return result

    def _schema_candidates(
        self,
        query: str,
        alias_hits: Mapping[str, tuple[str, ...]],
        object_hits: set[str],
    ) -> list[CandidateEvidence]:
        relation_refs: dict[str, list[str]] = {}
        for relation in self._relations:
            source = str(relation["source_object_id"])
            target = str(relation["target_object_id"])
            if source in object_hits and target in object_hits:
                relation_refs.setdefault(source, []).append(str(relation["relation_id"]))
                relation_refs.setdefault(target, []).append(str(relation["relation_id"]))
        result: list[CandidateEvidence] = []
        for item in self._objects:
            object_id = str(item["object_id"])
            display_hit = int(_phrase_hit(query, str(item["display_name"])))
            physical_hit = int(_phrase_hit(query, str(item["physical_name"])))
            aliases = tuple(
                sorted(
                    alias_id
                    for concept_id in item["concept_refs"]
                    for alias_id in alias_hits.get(str(concept_id), ())
                )
            )
            parent_id = item["parent_object_id"]
            parent_hit = int(parent_id is not None and str(parent_id) in object_hits)
            relations = tuple(sorted(relation_refs.get(object_id, ())))
            if not display_hit and not physical_hit and not aliases and not parent_hit:
                continue
            features: dict[str, int | float | str | bool | None] = {
                "candidate_kind": "schema_object",
                "object_type": str(item["object_type"]),
                "display_name_hit": display_hit,
                "alias_hit_count": len(aliases),
                "physical_name_hit": physical_hit,
                "schema_link_count": parent_hit,
                "relation_hit_count": len(relations),
                "value_evidence_hit_count": 0,
            }
            refs = {object_id, *aliases, *relations}
            refs.update(str(value) for value in item["concept_refs"])
            if parent_hit:
                refs.add(str(parent_id))
            # Resolve now so a later assembler cannot inherit an invalid ID.
            for reference in refs:
                self.domain_pack.resolve(reference)
            result.append(
                CandidateEvidence(
                    candidate_id=object_id,
                    display_name=str(item["display_name"]),
                    candidate_kind="schema_object",
                    raw_score=_score(features),
                    evidence_refs=tuple(sorted(refs)),
                    raw_features=features,
                )
            )
        return result

    def _slot_candidates(self, query: str) -> list[CandidateEvidence]:
        evidence_by_slot: dict[str, list[Mapping[str, Any]]] = {}
        for item in self._evidence:
            evidence_by_slot.setdefault(str(item["slot_id"]), []).append(item)
        rules_by_slot: dict[str, list[str]] = {}
        for rule in self._rules:
            for input_ref in rule["input_refs"]:
                if str(input_ref).startswith("slot."):
                    rules_by_slot.setdefault(str(input_ref), []).append(
                        str(rule["rule_id"])
                    )

        result: list[CandidateEvidence] = []
        for slot in self._slots:
            slot_id = str(slot["slot_id"])
            display_hit = int(_phrase_hit(query, str(slot["display_name"])))
            physical_hit = int(_phrase_hit(query, slot_id.removeprefix("slot.")))
            matched_evidence: list[str] = []
            for evidence in evidence_by_slot.get(slot_id, []):
                payload = evidence["payload"]
                values = payload.get("values", ())
                if any(
                    isinstance(value, str) and _phrase_hit(query, value)
                    for value in values
                ):
                    matched_evidence.append(str(evidence["evidence_id"]))
            if not display_hit and not physical_hit and not matched_evidence:
                continue
            rules = tuple(sorted(rules_by_slot.get(slot_id, ())))
            features: dict[str, int | float | str | bool | None] = {
                "candidate_kind": "slot",
                "display_name_hit": display_hit,
                "alias_hit_count": 0,
                "physical_name_hit": physical_hit,
                "schema_link_count": 0,
                "relation_hit_count": 0,
                "value_evidence_hit_count": len(matched_evidence),
                "validation_rule_count": len(rules),
            }
            refs = tuple(sorted({slot_id, *matched_evidence, *rules}))
            result.append(
                CandidateEvidence(
                    candidate_id=slot_id,
                    display_name=str(slot["display_name"]),
                    candidate_kind="slot",
                    raw_score=_score(features),
                    evidence_refs=refs,
                    raw_features=features,
                )
            )
        return result


class EvidenceFeatureAssembler:
    """Build and validate frozen TypedDecision/TraceEvent objects.

    The caller supplies a decision type, selection and action.  This class
    only checks the contract and evidence lineage; D4-04 remains responsible
    for required-slot discovery, thresholds and selective routing.
    """

    def __init__(
        self,
        domain_pack: LoadedDomainPack,
        *,
        schema_root: Path | str = DEFAULT_SCHEMA_ROOT,
    ) -> None:
        self.domain_pack = domain_pack
        self.schema_root = schema_root
        self._decision_schema = self._load_schema("typed-decision.v1.schema.json")
        self._trace_schema = self._load_schema(
            "typed-decision-trace-event.v1.schema.json"
        )
        self._decision_validator = Draft202012Validator(
            self._decision_schema, format_checker=FormatChecker()
        )
        registry = Registry().with_resource(
            self._decision_schema["$id"],
            Resource.from_contents(self._decision_schema),
        )
        self._trace_validator = Draft202012Validator(
            self._trace_schema,
            registry=registry,
            format_checker=FormatChecker(),
        )

    def _load_schema(self, name: str) -> Mapping[str, Any]:
        import json

        try:
            schema = json.loads((Path(self.schema_root) / name).read_text(encoding="utf-8"))
            Draft202012Validator.check_schema(schema)
            return schema
        except (OSError, UnicodeError, json.JSONDecodeError, SchemaError) as exc:
            raise TypedDecisionRuntimeError("contract_schema_invalid") from exc

    def candidate_options(
        self, candidates: Sequence[CandidateEvidence]
    ) -> list[dict[str, Any]]:
        ids = [candidate.candidate_id for candidate in candidates]
        if len(ids) != len(set(ids)):
            raise TypedDecisionRuntimeError("duplicate_candidate_id")
        options: list[dict[str, Any]] = []
        for candidate in candidates:
            try:
                role = self.domain_pack.resolve_role(candidate.candidate_id)
                pack_item = self.domain_pack.resolve(candidate.candidate_id)
            except DomainPackError as exc:
                raise TypedDecisionRuntimeError("candidate_not_in_domain_pack") from exc
            if role not in _CANDIDATE_ROLES:
                raise TypedDecisionRuntimeError("invalid_candidate_role")
            if candidate.candidate_kind != _ROLE_TO_KIND[role]:
                raise TypedDecisionRuntimeError("candidate_kind_mismatch")
            if candidate.display_name != pack_item["display_name"]:
                raise TypedDecisionRuntimeError("candidate_display_name_mismatch")
            if not 0 <= candidate.raw_score <= 1:
                raise TypedDecisionRuntimeError("candidate_raw_score_invalid")
            if candidate.candidate_id not in candidate.evidence_refs:
                raise TypedDecisionRuntimeError("candidate_self_evidence_missing")
            for reference in candidate.evidence_refs:
                try:
                    self.domain_pack.resolve(reference)
                except DomainPackError as exc:
                    raise TypedDecisionRuntimeError("evidence_not_in_domain_pack") from exc
            features = dict(candidate.raw_features)
            features["lexical_raw_score"] = candidate.raw_score
            options.append(
                {
                    "candidate_id": candidate.candidate_id,
                    "display_name": candidate.display_name,
                    "evidence_refs": list(candidate.evidence_refs),
                    "raw_features": features,
                }
            )
        return options

    def assemble_decision(
        self,
        *,
        decision_type: str,
        candidates: Sequence[CandidateEvidence] = (),
        selected_option: str | None = None,
        raw_score: float | None = None,
        evidence_refs: Iterable[str] | None = None,
        missing_or_conflicting_slots: Sequence[Mapping[str, Any]] = (),
        recommended_action: str,
        abstain_reason: str | None = None,
    ) -> dict[str, Any]:
        options = self.candidate_options(candidates)
        combined_refs = (
            sorted(
                {
                    reference
                    for candidate in candidates
                    for reference in candidate.evidence_refs
                }
            )
            if evidence_refs is None
            else sorted(set(evidence_refs))
        )
        decision = {
            "decision_type": decision_type,
            "candidate_options": options,
            "selected_option": selected_option,
            "raw_score": raw_score,
            "calibration_version": None,
            "evidence_refs": combined_refs,
            "missing_or_conflicting_slots": [
                dict(item) for item in missing_or_conflicting_slots
            ],
            "recommended_action": recommended_action,
            "abstain_reason": abstain_reason,
            "contract_version": TYPED_DECISION_CONTRACT_VERSION,
        }
        self.validate_decision(decision)
        return decision

    def validate_decision(self, decision: Mapping[str, Any]) -> None:
        try:
            self._decision_validator.validate(decision)
        except ValidationError as exc:
            raise TypedDecisionRuntimeError("typed_decision_schema_invalid") from exc
        candidate_ids = [
            str(item["candidate_id"]) for item in decision["candidate_options"]
        ]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise TypedDecisionRuntimeError("duplicate_candidate_id")
        selected = decision["selected_option"]
        if selected is not None and selected not in candidate_ids:
            raise TypedDecisionRuntimeError("selected_option_not_in_candidates")
        for candidate in decision["candidate_options"]:
            try:
                role = self.domain_pack.resolve_role(str(candidate["candidate_id"]))
                pack_item = self.domain_pack.resolve(str(candidate["candidate_id"]))
            except DomainPackError as exc:
                raise TypedDecisionRuntimeError("candidate_not_in_domain_pack") from exc
            if role not in _CANDIDATE_ROLES:
                raise TypedDecisionRuntimeError("invalid_candidate_role")
            if candidate["display_name"] != pack_item["display_name"]:
                raise TypedDecisionRuntimeError("candidate_display_name_mismatch")
            if candidate["raw_features"].get("candidate_kind") != _ROLE_TO_KIND[role]:
                raise TypedDecisionRuntimeError("candidate_kind_mismatch")
            if candidate["candidate_id"] not in candidate["evidence_refs"]:
                raise TypedDecisionRuntimeError("candidate_self_evidence_missing")
            self._assert_evidence_refs(candidate["evidence_refs"])
        self._assert_evidence_refs(decision["evidence_refs"])
        for issue in decision["missing_or_conflicting_slots"]:
            try:
                if self.domain_pack.resolve_role(str(issue["slot_id"])) != "slots":
                    raise TypedDecisionRuntimeError("slot_not_in_domain_pack")
            except DomainPackError as exc:
                raise TypedDecisionRuntimeError("slot_not_in_domain_pack") from exc
            self._assert_evidence_refs(issue["evidence_refs"])
        if decision["calibration_version"] is not None:
            raise TypedDecisionRuntimeError("calibration_not_available")

    def _assert_evidence_refs(self, refs: Iterable[str]) -> None:
        for reference in refs:
            try:
                self.domain_pack.resolve(str(reference))
            except DomainPackError as exc:
                raise TypedDecisionRuntimeError("evidence_not_in_domain_pack") from exc

    def assemble_trace_event(
        self,
        *,
        request_id: str,
        recorded_at: str,
        decision: Mapping[str, Any],
        thresholds: Mapping[str, Any],
        executed_action: str,
        budget_snapshot: Mapping[str, Any],
        selective_policy_version: str = "not-implemented.d4-03",
    ) -> dict[str, Any]:
        """Assemble a trace from caller-supplied action metadata.

        Threshold values are recorded only because the frozen trace schema
        requires them.  This method does not evaluate them or select an action.
        """

        self.validate_decision(decision)
        trace = {
            "event_type": "typed_decision",
            "trace_version": TRACE_VERSION,
            "request_id": request_id,
            "recorded_at": recorded_at,
            "domain_pack_ref": {
                "domain_pack_id": self.domain_pack.domain_pack_id,
                "version": self.domain_pack.version,
                "manifest_sha256": self.domain_pack.manifest_sha256,
            },
            "decision": dict(decision),
            "thresholds": dict(thresholds),
            "executed_action": executed_action,
            "component_versions": {
                "candidate_retriever": CANDIDATE_RETRIEVER_VERSION,
                "evidence_assembler": EVIDENCE_ASSEMBLER_VERSION,
                "calibrator": None,
                "selective_policy": selective_policy_version,
            },
            "budget_snapshot": dict(budget_snapshot),
        }
        self.validate_trace_event(trace)
        return trace

    def validate_trace_event(self, trace: Mapping[str, Any]) -> None:
        try:
            self._trace_validator.validate(trace)
        except ValidationError as exc:
            raise TypedDecisionRuntimeError("trace_event_schema_invalid") from exc
        reference = trace["domain_pack_ref"]
        if (
            reference["domain_pack_id"] != self.domain_pack.domain_pack_id
            or reference["version"] != self.domain_pack.version
            or reference["manifest_sha256"] != self.domain_pack.manifest_sha256
        ):
            raise TypedDecisionRuntimeError("trace_domain_pack_mismatch")
        self.validate_decision(trace["decision"])
        if trace["executed_action"] != trace["decision"]["recommended_action"]:
            raise TypedDecisionRuntimeError("trace_action_mismatch")
        if trace["component_versions"]["calibrator"] is not None:
            raise TypedDecisionRuntimeError("calibration_not_available")
