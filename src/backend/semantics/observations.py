"""Internal enum observations backed by validated pack metadata, not answers.

No numeric/range/default inference. Negated/filtered queries remain unresolved
here: preservation of text alone cannot authorize applying a SQL filter.
"""
import re
from .conservative import ConservativeSemantics, normalized_with_offsets
from ..agent.selective_routing import build_slot_observations


def _key(value):
    return normalized_with_offsets(str(value))[0].replace('_', '').replace(' ', '')


def enum_observations(query, pack):
    analysis = ConservativeSemantics(pack).analyze(query)
    if analysis['requires_clarification'] or re.search(
            r'不|非|无|未|没|排除|\d|\b(?:not|without|except)\b', query, re.I):
        return build_slot_observations({})
    concepts = {c for m in analysis['mentions'] if m['status'] == 'resolved' for c in m['concepts']}
    surfaces = {c: {str(pack.resolve(c)['display_name'])} for c in concepts}
    for item in pack.artifact_items('schema_catalog'):
        if item['object_type'] == 'table' and len(item['concept_refs']) == 1:
            c = str(item['concept_refs'][0])
            if c in surfaces:
                surfaces[c].add(str(item['physical_name']))
    observations = {}
    for slot in pack.artifact_items('slots'):
        matches = {}
        for ref in slot['allowed_value_evidence_refs']:
            evidence = pack.resolve(ref)
            if evidence['evidence_type'] != 'enum':
                continue
            for value in evidence['payload']['values']:
                if any(_key(value) == _key(surface) for words in surfaces.values() for surface in words):
                    matches.setdefault(value, set()).add(ref)
        # Multiple entities or mappings are not silently collapsed.
        if len(matches) == 1:
            value, refs = next(iter(matches.items()))
            observations[str(slot['slot_id'])] = {'values': [value], 'evidence_refs': sorted(refs)}
    return build_slot_observations(observations)
