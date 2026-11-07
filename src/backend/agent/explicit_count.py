"""Conservative recognition of complete, unfiltered entity-count questions."""
from __future__ import annotations

import re
from .domain_pack import LoadedDomainPack
from .typed_decision_runtime import CandidateRetriever, _normalize, _phrase_spans


def explicit_count_values(query: str, pack: LoadedDomainPack) -> dict[str, str]:
    """Only whole recognized templates may skip a user clarification.

    Values still pass the existing offered-option/evidence binding and merger.
    Filters, mixed metrics, multiple entities and instructions stay unresolved.
    """
    normalized = _normalize(query).strip(" ?？。.!！")
    hits = CandidateRetriever(pack)._concept_alias_hits(normalized)
    table_options: dict[str, set[str]] = {}
    for item in pack.artifact_items("schema_catalog"):
        if item["object_type"] == "table":
            for concept in item["concept_refs"]:
                table_options.setdefault(str(concept), set()).add(str(item["physical_name"]))
    tables = {concept: next(iter(options)) for concept, options in table_options.items()
              if len(options) == 1}
    if any(concept in hits and len(options) != 1 for concept, options in table_options.items()):
        return {}
    entity_hits = {concept: aliases for concept, aliases in hits.items() if concept in tables}
    if len(entity_hits) != 1:
        return {}
    concept, alias_ids = next(iter(entity_hits.items()))
    enum = next(item for item in pack.artifact_items("value_evidence")
                if item["evidence_id"] == "evidence.entity_kinds")["payload"]["values"]
    entity = next((str(value) for value in enum
                   if str(value).replace("_", "").casefold() == tables[concept].casefold()), None)
    if entity is None:
        return {}
    spans = sorted({span for alias in pack.artifact_items("aliases")
                    if alias["alias_id"] in alias_ids
                    for span in _phrase_spans(normalized, str(alias["surface_form"]))}, reverse=True)
    masked = normalized
    for start, end in spans:
        masked = masked[:start] + "ENTITY" + masked[end:]
    masked = re.sub(r"ENTITY(?:\s*ENTITY)+", "ENTITY", masked)
    templates = (
        r"(?:请|请问)?(?:统计|查询|查看)?\s*ENTITY\s*(?:的)?(?:总数量|数量|总数|个数)",
        r"(?:请问)?(?:数据库里|数据库中)?(?:一共|总共)?(?:有)?多少(?:位|个|张|首|种|条)?\s*ENTITY",
        r"(?:请问)?(?:数据库里|数据库中)?\s*ENTITY\s*(?:一共|总共)?(?:有)?多少(?:位|个|张|首|种|条)?",
        r"(?:how many\s+ENTITY(?:\s+are there(?:\s+in (?:the )?database)?)?|count\s+(?:all\s+)?ENTITY|ENTITY\s+count)",
    )
    if not any(re.fullmatch(template, masked) for template in templates):
        return {}
    return {"slot.metric": "count", "slot.entity_type": entity}
