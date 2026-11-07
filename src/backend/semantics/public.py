"""In-process semantic v1 adapter. Requires A #104 as an installed dependency.

The production boundary calls A's unchanged validator both before inference
and before returning a result. tools/evaluation is a deliberate temporary
runtime dependency; no permissive validator or HTTP contract is invented here.
"""
from copy import deepcopy
from dataclasses import asdict
import hashlib
from .conservative import ConservativeSemantics
from .observations import enum_observations
from ..agent.selective_routing import RequiredSlotResolver, build_slot_observations
from ..agent.typed_decision_runtime import CandidateEvidence, CandidateRetriever, EvidenceFeatureAssembler


class PublicSemanticAdapter:
    def __init__(self, pack, *, contract_validator=None):
        if contract_validator is None:
            from tools.evaluation import validate_batch1_semantic_contracts as contract_validator
        self.pack = pack
        self.validator = contract_validator

    def analyze(self, request):
        request = deepcopy(request)
        authorized = self.validator.validate_request(request, self.pack)['authorized_candidate_ids']
        query = request['question']
        internal = ConservativeSemantics(self.pack).analyze(query)
        candidate_map = {x.candidate_id: x for x in CandidateRetriever(self.pack).retrieve(query, top_k=50)
                         if x.candidate_id in authorized}
        for mention in internal['mentions']:
            for concept in mention['concepts']:
                if concept in authorized:
                    refs = tuple(sorted({concept, *mention['evidence_refs']}))
                    candidate_map[concept] = CandidateEvidence(concept, self.pack.resolve(concept)['display_name'],
                                                              'concept', 1.0, refs, {'exact_alias': True, 'candidate_kind': 'concept'})
        candidates = tuple(sorted(candidate_map.values(), key=lambda x: (-x.raw_score, x.candidate_id)))[:50]
        ids = {x.candidate_id for x in candidates}
        fragments, issues, edits = [], [], []

        def fragment(start, end, mention=None):
            fid = 'fragment.' + hashlib.sha256(f'{start}:{end}:{query[start:end]}'.encode()).hexdigest()[:20]
            concepts = sorted(set(mention['concepts']) & ids) if mention else []
            # Filtering the authorized catalog must never turn an original
            # ambiguity into a unique resolution.
            resolved = bool(mention and mention['status'] == 'resolved' and len(concepts) == 1)
            status = 'resolved' if resolved else ('ambiguous' if len(concepts) >= 2 else 'unknown')
            if status == 'unknown': concepts = []
            refs = sorted(set(mention['evidence_refs'])) if mention and status != 'unknown' else []
            normalized = concepts[0] if resolved else None
            f = dict(fragment_id=fid, start=start, end=end, text=query[start:end], status=status,
                     entity_type=normalized, normalized_ref=normalized,
                     normalized_value=self.pack.resolve(normalized)['display_name'] if resolved else None,
                     candidate_ids=concepts, evidence_refs=refs)
            fragments.append(f)
            if status != 'resolved':
                issues.append(dict(issue_id='issue.'+fid.split('.')[1], kind='ambiguity' if status == 'ambiguous' else 'unknown',
                                   fragment_refs=[fid], candidate_ids=concepts, evidence_refs=refs,
                                   reason_code='ambiguous_alias' if status == 'ambiguous' else 'uninterpreted_text'))
            if resolved:
                edit = next((e for e in internal['changes'] if (e['start'], e['end']) == (start, end)), None)
                if edit:
                    edits.append(dict(edit_id='edit.'+fid.split('.')[1], source_fragment_id=fid,
                                      start=start, end=end, before=query[start:end], after=edit['after'],
                                      kind=edit['kind'], evidence_refs=refs))
            return f

        cursor = 0
        mentions = internal['mentions']
        for mention in mentions:
            start, end = mention['start'], mention['end']
            if start < cursor: continue
            # Crossing spans cannot fit the nonoverlap public contract: keep
            # their union unknown rather than choosing a competing entity.
            crossing = [m for m in mentions if m['start'] < end and start < m['end'] and m is not mention]
            if crossing:
                end = max(end, *(m['end'] for m in crossing)); mention = None
            while cursor < start:
                stop = min(cursor+1024, start); fragment(cursor, stop); cursor = stop
            fragment(start, end, mention); cursor = end
        while cursor < len(query):
            stop = min(cursor+1024, len(query)); fragment(cursor, stop); cursor = stop
        for suggestion in internal['suggestions']:
            linked = next((f for f in fragments if f['start'] <= suggestion['start'] and suggestion['end'] <= f['end']), None)
            if linked:
                issues.append(dict(issue_id='issue.spelling.'+str(suggestion['start']), kind='unconfirmed_spelling',
                                   fragment_refs=[linked['fragment_id']], candidate_ids=[], evidence_refs=[],
                                   reason_code='spelling_requires_confirmation'))
        raw = {x['slot_id']: {'values': list(x['values']), 'evidence_refs': list(x['evidence_refs'])}
               for x in request['session']['slot_observations']}
        automatic = enum_observations(query, self.pack) if all(c in ids for m in mentions for c in m['concepts']) else {}
        # Reuse the existing whole-query count recognizer for the already
        # supported Chinook runtime; other domains rely on their metadata.
        if self.pack.domain_pack_id == 'chinook_music_store' and all(c in ids for m in mentions for c in m['concepts']):
            from ..agent.explicit_count import explicit_count_values
            from ..agent.selective_routing import SlotObservation
            for slot_id, value in explicit_count_values(query, self.pack).items():
                if slot_id not in authorized:
                    continue
                refs = [r for r in self.pack.resolve(slot_id)['allowed_value_evidence_refs']
                        if self.pack.resolve(r)['evidence_type'] == 'enum'
                        and value in self.pack.resolve(r)['payload']['values']]
                if refs:
                    automatic[slot_id] = SlotObservation((value,), tuple(refs))
        for key, observation in automatic.items():
            if key not in raw:
                raw[key] = {'values': list(observation.values), 'evidence_refs': list(observation.evidence_refs)}
            elif raw[key]['values'] != list(observation.values) and fragments:
                issues.append(dict(issue_id='issue.session.'+key, kind='ambiguity',
                                   fragment_refs=[fragments[0]['fragment_id']], candidate_ids=[], evidence_refs=[],
                                   reason_code='session_observation_conflict'))
        observations = build_slot_observations(raw)
        intent = 'sql' if any(f['status'] == 'resolved' for f in fragments) else 'unknown'
        resolution = RequiredSlotResolver(self.pack).resolve(intent=intent, candidates=candidates, observations=observations)
        missing = [dict(slot_id=x.slot_id, issue='conflicting' if x.status == 'conflicting' else 'missing',
                        evidence_refs=list(x.evidence_refs)[:16]) for x in resolution.assessments
                   if x.status == 'conflicting' or (x.required is not False and x.status != 'filled')]
        unsafe = bool(resolution.hard_rule_violations) or not candidates or any(i['kind'] in ('ambiguity','unconfirmed_spelling') for i in issues)
        assembler = EvidenceFeatureAssembler(self.pack)
        decision = assembler.assemble_decision(decision_type='abstain' if unsafe else 'score', candidates=candidates,
                                               raw_score=None if unsafe else max(x.raw_score for x in candidates),
                                               recommended_action='escalate' if unsafe else ('clarify' if missing else 'accept'),
                                               abstain_reason='insufficient_evidence' if unsafe else None,
                                               missing_or_conflicting_slots=missing)
        result = dict(result_version='1.0.0', request_id=request['request_id'], original_question=query,
                      rewritten_question=query, text_coordinates={'unit':'unicode_code_point','range_semantics':'half_open','normalization':'original_question'},
                      domain_pack_ref=dict(request['domain_context']['domain_pack_ref']), intent=intent,
                      fragments=fragments, candidates=[dict(asdict(x), evidence_refs=list(x.evidence_refs)) for x in candidates], issues=issues, edits=edits,
                      slot_observations=[dict(slot_id=k, **v, fragment_refs=([f['fragment_id'] for f in fragments if f['status']=='resolved'][:16] if k in automatic and k not in {x['slot_id'] for x in request['session']['slot_observations']} else [])) for k,v in raw.items()],
                      required_slot_resolution={'source':'auto_domain_pack','assessments':[x.to_dict() for x in resolution.assessments],
                                                'hard_rule_violations':list(resolution.hard_rule_violations),'soft_rule_violations':list(resolution.soft_rule_violations)},
                      typed_decision=decision, sql_execution_authorized=False)
        for edit in reversed(edits):
            result['rewritten_question'] = result['rewritten_question'][:edit['start']] + edit['after'] + result['rewritten_question'][edit['end']:]
        self.validator.validate_contract_pair(request, result, self.pack)
        return deepcopy(result)
