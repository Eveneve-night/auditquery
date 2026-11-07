"""Independent public recovery probes using B's real enum_observations.

The proposal is explicitly synthetic; recovery, SafetyGate and SQLite are real.
No claim that the new semantic public HTTP adapter already exists.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[2]

def run(implementation_root):
    sys.path.insert(0,str(Path(implementation_root).resolve()))
    sys.path.insert(0,str(Path(implementation_root).resolve()/'src'))
    from src.backend.agent.domain_pack import DomainPackRegistry
    from src.backend.semantics.observations import enum_observations
    from src.backend.agent.selective_routing import RequiredSlotResolver
    from src.backend.agent.clarification_recovery import (
        D4ClarificationRecoveryRuntime, build_clarification_answer, SqlProposal,
        ClarificationRecoveryError)
    from src.backend.agent.intent_routing import (create_conversation_state,
        apply_intent_result, load_source_catalog)
    from src.backend.api.online_sql_service import _sql_intent_result
    pack=DomainPackRegistry(ROOT/'domain-packs').load('chinook_music_store','1.0.0')
    runtime=D4ClarificationRecoveryRuntime(pack)
    catalog=load_source_catalog(ROOT/'configs/routing/d4-01/source-catalog.v1.json')
    rows=[]
    def check(name,ok):
        rows.append({'name':name,'passed':bool(ok)})
        if not ok: raise AssertionError(name)
    def start(sid):
        state=apply_intent_result(create_conversation_state(sid,'艺术家'),catalog,_sql_intent_result())
        return runtime.start(conversation_state=state,slot_observations=enum_observations('艺术家',pack),
            request_id='request.c.'+sid,recorded_at='2026-10-23T13:40:00Z')
    a,b=start('session.c.one'),start('session.c.two')
    pending=a.pending_question.to_dict()
    check('auto_missing_metric_not_entity',pending['slot_id']=='slot.metric')
    option=next(x for x in pending['options'] if x['value']=='count')
    value={k:pending[k] for k in ('question_id','session_id','request_id','turn_id','state_revision','slot_id')}
    value.update(answer_id='answer.c.one',values=['count'],evidence_refs=[option['evidence_ref']])
    answer=build_clarification_answer(value)
    try: runtime.resume(b,answer,recorded_at='2026-10-23T13:41:00Z')
    except ClarificationRecoveryError: rejected=True
    else: rejected=False
    check('cross_session_answer_rejected',rejected)
    check('other_session_pending_unchanged',b.pending_question is not None)
    runtime.resume(a,answer,recorded_at='2026-10-23T13:41:00Z')
    check('one_answer_recovers_without_entity_reconfirmation',a.pending_question is None)
    try: runtime.resume(a,answer,recorded_at='2026-10-23T13:42:00Z')
    except ClarificationRecoveryError: rejected=True
    else: rejected=False
    check('replayed_answer_rejected',rejected)
    class SyntheticProposal:
        def propose(self,**kwargs):
            return SqlProposal('proposal.c.offline','SELECT COUNT(*) FROM Artist',('Artist',))
    result=runtime.handoff_and_execute(a,proposal_port=SyntheticProposal())
    check('actual_safety_gate_and_readonly_sqlite',result is not None and a.safety_approved
          and a.safety_gate_calls==1 and a.local_sqlite_execution_successes==1)
    domains={}
    for domain in ('retail_inventory','equipment_loan'):
        dp=DomainPackRegistry(ROOT/'domain-packs').load(domain,'1.0.1')
        resolution=RequiredSlotResolver(dp).resolve(intent='sql',candidates=(),observations={})
        assessments=resolution.assessments
        missing=sorted(x.slot_id for x in assessments if x.required and x.status=='missing')
        check(domain+'_auto_required_dimensions',set(missing)=={'slot.metric','slot.location','slot.time_range'})
        domains[domain]={'auto_missing':missing,'public_API_recovery':'pending_B_adapter',
                         'database_execution':'not_connected'}
    module=Path(implementation_root)/'src/backend/semantics/observations.py'
    return {'scope':'public_internal_recovery_only','split':'prompt_dev',
            'B_observations_sha256':hashlib.sha256(module.read_bytes().replace(b'\r\n',b'\n')).hexdigest(),
            'checks':rows,'passed':sum(r['passed'] for r in rows),'total':len(rows),
            'domains':domains,'proposal_kind':'synthetic_no_model',
            'model_calls':0,'key_reads':0,'cost_cny':0,
            'not_proven':['new_semantic_public_HTTP','new_domain_execution','business_accuracy']}

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--implementation-root',type=Path,required=True)
    parser.add_argument('--report',type=Path,required=True)
    args=parser.parse_args()
    report=run(args.implementation_root)
    args.report.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
    print(json.dumps({'passed':report['passed'],'total':report['total']}))
