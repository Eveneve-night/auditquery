"""Cross-package in-process harness. No adapter means pending, never green.

An explicit --adapter module:function must point at B's real published adapter.
The callable's harness signature is (request, pack); use only the actual B entry,
never an implementation fabricated inside this evaluator.
"""
import argparse
import hashlib
import importlib
import inspect
import json
from pathlib import Path
import sys
import subprocess

C_ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(C_ROOT))
from tools.evaluation.batch1_c_public_requests import build_public_request

def run(root, adapter=None, implementation_root=None):
    # Load A's validator by path; do not accidentally resolve C's tools package.
    import importlib.util
    implementation_root=implementation_root or root
    sys.path.insert(0,str(implementation_root/'src'))
    spec=importlib.util.spec_from_file_location('c_review_A_validator',root/'tools/evaluation/validate_batch1_semantic_contracts.py')
    validator=importlib.util.module_from_spec(spec);spec.loader.exec_module(validator)
    import backend
    backend.__path__.insert(0,str(implementation_root/'src/backend'))
    from backend.agent.domain_pack import DomainPackRegistry
    registry=DomainPackRegistry(C_ROOT/'domain-packs')
    cases=json.loads((C_ROOT/'tests/fixtures/batch1-public/v1/inputs.json').read_text(encoding='utf-8'))
    cases += [{'case_id':'probe.'+name,'input':{'domain_pack_id':'chinook_music_store','question':question}}
              for name,question in [('emoji','😀𠮷歌手 不要超过20万元'),
                                   ('markup','唱片<svg onload=alert(1)>')]]
    call=None
    if adapter:
        module,name=adapter.split(':',1)
        call=getattr(importlib.import_module(module),name)
    rows=[];results=[];attacks=[]
    for case in cases:
        domain=case['input']['domain_pack_id']
        pack=registry.load(domain,'1.0.0' if domain=='chinook_music_store' else '1.0.1')
        request=build_public_request(case,pack)
        validator.validate_request(request,pack)
        row={'case_id':case['case_id'],'request_valid':True,'B_output_valid':None}
        if call:
            output = (call(pack,contract_validator=validator).analyze(request)
                      if inspect.isclass(call) else call(request,pack))
            output=json.loads(json.dumps(output,ensure_ascii=False))
            validator.validate_contract_pair(request,output,pack)
            row['B_output_valid']=True
            results.append({'case_id':case['case_id'],'request':request,'result':output})
        rows.append(row)
    if call:
        seed=results[0]['request']
        domain=cases[0]['input']['domain_pack_id'];pack=registry.load(domain,'1.0.1')
        for field in ('expected','gold','label','required_slots'):
            poisoned=json.loads(json.dumps(seed));poisoned[field]=['poison']
            try:
                if inspect.isclass(call):call(pack,contract_validator=validator).analyze(poisoned)
                else:call(poisoned,pack)
            except validator.SemanticContractError:attacks.append(field)
            else:raise AssertionError('unsafe_request_accepted:'+field)
        poisoned=json.loads(json.dumps(seed))
        poisoned['domain_context']['authorized_catalog']['candidate_ids']=['concept.not_authorized']
        try:
            if inspect.isclass(call):call(pack,contract_validator=validator).analyze(poisoned)
            else:call(poisoned,pack)
        except validator.SemanticContractError:attacks.append('catalog_escape')
        else:raise AssertionError('catalog_escape_accepted')
    hashes={str(p.relative_to(root)):hashlib.sha256(p.read_bytes().replace(b'\r\n',b'\n')).hexdigest()
            for p in [*sorted((root/'configs/schemas/semantics').glob('*.json')),
                      root/'tools/evaluation/validate_batch1_semantic_contracts.py',
                      ]}
    hashes.update({'B/'+str(p.relative_to(implementation_root)):hashlib.sha256(p.read_bytes().replace(b'\r\n',b'\n')).hexdigest()
                   for p in sorted((implementation_root/'src/backend/semantics').glob('*.py'))})
    sufficient=next((x for x in results if x['case_id']=='chinook_music_store.no_rewrite'),None)
    sufficient_ok=(sufficient['result']['typed_decision']['recommended_action']=='accept') if sufficient else None
    versions={name:{'head':subprocess.check_output(['git','-C',str(path),'rev-parse','HEAD'],text=True).strip(),
                    'dirty':bool(subprocess.check_output(['git','-C',str(path),'status','--porcelain'],text=True).strip())}
              for name,path in [('integration',root),('B',implementation_root),('C',C_ROOT)]}
    return {'scope':'public_in_process_contract','split':'prompt_dev','requests_valid':len(rows),
            'public_adapter_status':'executed' if call else 'pending_B_entry',
            'B_outputs_valid':len(results),'manifest_cases':36,'additional_probes':2,
            'adapter':adapter,'rows':rows,'results':results,
            'dependency_hashes':hashes,'model_calls':0,'key_reads':0,'cost_cny':0,
            'source_checkouts':versions,
            'unsafe_request_attacks_rejected':attacks,
            'semantic_exit_checks':{'sufficient_explicit_count_accepted':sufficient_ok},
            'status':('blocked_semantic_behavior' if sufficient_ok is False else
                      ('public_path_checked' if call else 'pending_B_entry')),
            'HTTP':'not_in_scope','business_accuracy_proven':False}

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--integration-root',type=Path,required=True)
    p.add_argument('--adapter');p.add_argument('--implementation-root',type=Path)
    p.add_argument('--report',type=Path,required=True)
    args=p.parse_args();report=run(args.integration_root.resolve(),args.adapter,args.implementation_root)
    args.report.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
    print(json.dumps({k:report[k] for k in ('requests_valid','public_adapter_status','B_outputs_valid')}))
