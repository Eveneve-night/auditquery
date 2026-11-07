"""In-process Chinook recovery using public semantics and original D4 safety.

Other packs currently support PublicSemanticAdapter only. No database routing
is inferred for newly imported domains. This class is not an HTTP API.
"""
from copy import deepcopy
from pathlib import Path
import re
import threading
from .public import PublicSemanticAdapter
from ..agent.clarification_recovery import D4ClarificationRecoveryRuntime, build_clarification_answer
from ..agent.selective_routing import build_slot_observations
from ..agent.intent_routing import apply_intent_result, create_conversation_state, load_source_catalog
from ..api.online_sql_service import _sql_intent_result


class SemanticRecovery:
    def __init__(self, adapter: PublicSemanticAdapter):
        self.adapter = adapter
        self.runtime = D4ClarificationRecoveryRuntime(adapter.pack)
        self._sessions = {}
        self._lock = threading.RLock()

    def start(self, request, *, recorded_at):
        with self._lock:
            result = self.adapter.analyze(request)
            if self.adapter.pack.domain_pack_id != 'chinook_music_store':
                raise ValueError('semantic_only_domain_no_database_binding')
            if request['session']['turn_index'] != 0:
                raise ValueError('initial_turn_required')
            sid = request['session']['session_id']
            if sid in self._sessions: raise ValueError('session_already_registered')
            if result['typed_decision']['recommended_action'] in ('reject','escalate'):
                raise ValueError('semantic_clarification_required')
            # No execution semantics for negation/filters are invented here.
            if re.search(r'不|非|无|未|没|排除|\d|\b(?:not|without|except)\b',request['question'],re.I):
                raise ValueError('semantic_filters_not_executable')
            from ..agent.explicit_count import explicit_count_values
            if not explicit_count_values(request['question'],self.adapter.pack):
                residual=request['question']
                for f in reversed(result['fragments']):
                    if f['status']=='resolved':
                        residual=residual[:f['start']]+residual[f['end']:]
                if residual.strip(' \t\n?？。.!！'):
                    raise ValueError('semantic_uninterpreted_constraints')
            root=Path(__file__).resolve().parents[3]
            catalog=load_source_catalog(root/'configs/routing/d4-01/source-catalog.v1.json')
            state=apply_intent_result(create_conversation_state(sid,request['question']),catalog,_sql_intent_result())
            raw={x['slot_id']:{'values':x['values'],'evidence_refs':x['evidence_refs']} for x in result['slot_observations']}
            initial_raw = {} if result['typed_decision']['recommended_action']=='accept' else raw
            working=self.runtime.start(conversation_state=state,slot_observations=build_slot_observations(initial_raw),
                                       request_id=request['request_id'],recorded_at=recorded_at)
            if initial_raw != raw:
                for _ in range(len(raw)):
                    q=working.pending_question
                    if q is None or q.slot_id not in raw or len(raw[q.slot_id]['values'])!=1: break
                    value=raw[q.slot_id]['values'][0]
                    option=next((x for x in q.options if x.value==value),None)
                    if option is None: break
                    payload={k:q.to_dict()[k] for k in ('question_id','session_id','request_id','turn_id','state_revision','slot_id')}
                    payload.update(answer_id='auto.semantic.'+str(q.turn_id)+'.'+str(q.state_revision), values=[value],evidence_refs=[option.evidence_ref])
                    self.runtime.resume(working,build_clarification_answer(payload),recorded_at=recorded_at)
            self._sessions[sid]=working
            return {'semantic_result':result,'recovery_result':self.runtime.result(working)}

    def pending(self, session_id):
        with self._lock:
            question=self._sessions[session_id].pending_question
            return deepcopy(question.to_dict()) if question else None

    def resume(self, session_id, answer, *, recorded_at):
        with self._lock:
            working=deepcopy(self._sessions[session_id])
            self.runtime.resume(working,build_clarification_answer(answer),recorded_at=recorded_at)
            result=self.runtime.result(working)
            self._sessions[session_id]=working
            return deepcopy(result)

    def execute(self, session_id, proposal_port):
        with self._lock:
            working=deepcopy(self._sessions[session_id])
            observation=working.observations.get('slot.entity_type')
            if observation is None or len(observation.values)!=1:
                raise ValueError('entity_table_binding_missing')
            value=str(observation.values[0]).replace('_','').casefold()
            tables={str(x['physical_name']) for x in self.adapter.pack.artifact_items('schema_catalog')
                    if x['object_type']=='table' and str(x['physical_name']).casefold()==value}
            if len(tables)!=1: raise ValueError('entity_table_binding_ambiguous')
            table=next(iter(tables))
            class BoundPort:
                def propose(self, **kwargs):
                    proposal=proposal_port.propose(**kwargs)
                    if proposal.tables!=(table,): raise ValueError('entity_table_mismatch')
                    return proposal
            result=self.runtime.handoff_and_execute(working,proposal_port=BoundPort())
            self.runtime.result(working)
            self._sessions[session_id]=working
            return deepcopy(result)
