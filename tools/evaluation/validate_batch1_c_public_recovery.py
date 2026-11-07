"""C's independent real B public recovery sequence, synthetic SQL proposal."""
import argparse
import importlib.util
import json
from pathlib import Path
import sys
from copy import deepcopy
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from tools.evaluation.batch1_c_public_requests import build_public_request

def run(a_root,b_root):
    sys.path.insert(0,str(b_root/'src'))
    spec=importlib.util.spec_from_file_location('C_public_recovery_A',a_root/'tools/evaluation/validate_batch1_semantic_contracts.py')
    validator=importlib.util.module_from_spec(spec);spec.loader.exec_module(validator)
    import backend
    backend.__path__.insert(0,str(b_root/'src/backend'))
    from backend.agent.domain_pack import DomainPackRegistry
    from backend.semantics.public import PublicSemanticAdapter
    from backend.semantics.recovery import SemanticRecovery
    from backend.agent.clarification_recovery import SqlProposal
    pack=DomainPackRegistry(ROOT/'domain-packs').load('chinook_music_store','1.0.0')
    adapter=PublicSemanticAdapter(pack,contract_validator=validator)
    bridge=SemanticRecovery(adapter);checks=[]
    def check(name,passed):
        checks.append({'name':name,'passed':bool(passed)})
        if not passed: raise AssertionError(name)
    def request(name,question):
        return build_public_request({'case_id':'public.'+name,'input':{'question':question}},pack)
    r1,r2=request('one','艺术家'),request('two','专辑')
    now='2026-10-23T14:40:00Z'
    bridge.start(r1,recorded_at=now);bridge.start(r2,recorded_at=now)
    sid=r1['session']['session_id'];sid2=r2['session']['session_id']
    q=bridge.pending(sid);other=bridge.pending(sid2)
    check('auto_metric_question',q['slot_id']=='slot.metric')
    option=next(x for x in q['options'] if x['value']=='count')
    answer={k:q[k] for k in ('question_id','session_id','request_id','turn_id','state_revision','slot_id')}
    answer.update(answer_id='answer.c.public',values=['count'],evidence_refs=[option['evidence_ref']])
    def rejected(target,value):
        try:bridge.resume(target,value,recorded_at=now)
        except Exception:return True
        return False
    check('cross_session_rejected',rejected(sid2,answer))
    check('other_session_unchanged',bridge.pending(sid2)==other)
    invalid=deepcopy(answer);invalid['state_revision']+=1
    check('stale_revision_rejected',rejected(sid,invalid))
    check('stale_failure_preserves_pending',bridge.pending(sid)==q)
    bridge.resume(sid,answer,recorded_at=now)
    check('one_answer_restores',bridge.pending(sid) is None)
    check('answer_replay_rejected',rejected(sid,answer))
    class Proposal:
        def propose(self,**kwargs):return SqlProposal('proposal.c.public','SELECT COUNT(*) FROM Artist',('Artist',))
    executed=bridge.execute(sid,Proposal())
    check('real_readonly_sqlite_result',executed.rows==((275,),))
    sufficient=request('sufficient','统计艺术家总数')
    result=bridge.start(sufficient,recorded_at=now)
    check('sufficient_explicit_count_not_questioned',bridge.pending(sufficient['session']['session_id']) is None)
    return {'scope':'real_B_public_v1_to_recovery','checks':checks,'passed':len(checks),
            'proposal':'synthetic','actual_sqlite_executions':1,'model_calls':0,'key_reads':0,'cost_cny':0,
            'new_domains':'semantic_only_no_database_binding','HTTP':False}

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--A-root',type=Path,required=True);p.add_argument('--B-root',type=Path,required=True);p.add_argument('--report',type=Path,required=True)
    args=p.parse_args();r=run(args.A_root,args.B_root)
    args.report.write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
    print(json.dumps({'passed':r['passed']}))
