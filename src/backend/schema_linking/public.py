"""Strict schema-linking v1 adapter for A #111; no SQL execution capability."""
from copy import deepcopy
from .linker import SchemaLinker
from ..agent.typed_decision_runtime import CandidateEvidence,EvidenceFeatureAssembler


class PublicSchemaLinker:
    def __init__(self,pack,*,mode='enhanced',contract_validator=None,clarification_slot='slot.schema_target'):
        if contract_validator is None:
            from ..agent import round2_contracts as contract_validator
        self.pack=pack;self.mode=mode;self.contract=contract_validator
        self.clarification_slot=clarification_slot

    def make_request(self,question,request_id,*,max_data_level='L1'):
        request={'request_version':'1.0.0','request_id':request_id,'question':question,
                 'domain_pack_ref':self.contract.pack_reference(self.pack),
                 'catalog_scope':'full_model_visible_read_public_pack','max_data_level':max_data_level,
                 **self.contract.full_catalog(self.pack,max_data_level)}
        self.contract.validate_linking_request(request,self.pack)
        return request

    def locate(self,request):
        request=deepcopy(request)
        catalog=self.contract.validate_linking_request(request,self.pack)
        engine=SchemaLinker(self.pack,max_data_level=request['max_data_level'])
        ids=[key for key in catalog['authorized_object_ids'] if self.pack.resolve(key)['object_type'] in ('table','column')]
        core=engine.locate({'question':request['question'],'authorized_ids':ids,'mode':self.mode})
        def records(values):
            output=[]
            for value in values:
                oid=value['candidate_id']
                refs=sorted({oid,*value['evidence_refs']})
                # Keep the self evidence even when a high-degree graph needs
                # the frozen per-candidate evidence budget.
                refs=sorted({oid,*[x for x in refs if x!=oid][:31]})
                output.append({'object_id':oid,'raw_score':value['raw_score'],'evidence_refs':refs,
                               'raw_features':{'candidate_kind':'schema_object',**value['score_components']}})
            return sorted(output,key=lambda x:(-x['raw_score'],x['object_id']))
        tables=records(core['table_candidates']);columns=records(core['field_candidates'])
        status={'select':'selected','clarify':'clarify','abstain':'abstain'}[core['decision']]
        reason=core['reason_code'];issues=[]
        if status=='clarify':
            context=self.pack.model_context(max_data_level=request['max_data_level'])['artifacts']
            slot=next((x for x in context.get('slots',{'items':[]})['items'] if x['slot_id']==self.clarification_slot),None)
            if slot is None:
                status='abstain';reason='no_declared_schema_clarification_slot'
            else:
                issues=[{'slot_id':self.clarification_slot,'issue':'missing','evidence_refs':[self.clarification_slot]}]
        selected=core['selected_table_ids'][0] if status=='selected' else None
        candidates=tuple(CandidateEvidence(x['object_id'],self.pack.resolve(x['object_id'])['display_name'],
                                          'schema_object',x['raw_score'],tuple(x['evidence_refs']),x['raw_features']) for x in tables)
        chosen=next((x for x in tables if x['object_id']==selected),None)
        chosen_refs=chosen['evidence_refs'] if chosen else []
        rest=sorted({ref for x in tables for ref in x['evidence_refs']}-set(chosen_refs))
        decision_refs=sorted([*chosen_refs,*rest[:64-len(chosen_refs)]])
        decision=EvidenceFeatureAssembler(self.pack).assemble_decision(
            decision_type='choice' if status=='selected' else 'score' if status=='clarify' else 'abstain',
            candidates=candidates,selected_option=selected,
            raw_score=chosen['raw_score'] if chosen else (tables[0]['raw_score'] if status=='clarify' else None),
            evidence_refs=decision_refs,missing_or_conflicting_slots=issues,
            recommended_action={'selected':'accept','clarify':'clarify','abstain':'escalate'}[status],
            abstain_reason='insufficient_evidence' if status=='abstain' else None)
        result={'result_version':'1.0.0','request_id':request['request_id'],'input_sha256':self.contract.canonical_hash(request),
                'domain_pack_ref':self.contract.pack_reference(self.pack),'status':status,'reason_code':reason,
                'table_candidates':tables,'column_candidates':columns,'selected_table_id':selected,
                'selected_column_ids':core['selected_field_ids'] if selected else [],'typed_decision':decision,'sql_execution_authorized':False}
        self.contract.validate_linking_pair(request,result,self.pack)
        return deepcopy(result)
