"""Offline public fake contract evidence only, no connector/secret/model operation."""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'src'))
from backend.data_sources.contracts import validate_source, validate_snapshot, validate_transition, DataSourceContractError

def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def build_report():
    example=ROOT/'configs/examples/data-sources/v1/public.json'
    e=json.loads(example.read_text(encoding='utf-8'))
    validate_snapshot(e['snapshot'],e['source'],authorized_source_ids=['source.demo'],descriptor=e['descriptor'])
    rejected=[]
    mutations=[
        ('path_escape',lambda x:x['source'].update(sqlite_asset_id='../outside')),
        ('dsn',lambda x:x['source'].update(dsn='sqlite:///fake')),
        ('gold',lambda x:x['source'].update(gold={})),
        ('unknown_connector',lambda x:x['source']['connector'].update(id='postgresql')),
        ('credential_scope',lambda x:x['source'].update(credential_ref={'secret_id':'fake.ref','source_id':'other','purpose':'data_source','revision':1})),
        ('binding_missing',lambda x:x['source'].update(binding=None)),
        ('stale_revision',lambda x:x['snapshot'].update(source_revision=2)),
        ('stale_schema',lambda x:x['snapshot']['binding'].update(schema_sha256='d'*64)),
        ('draining',lambda x:x['source'].update(status='draining')),
        ('zero_readonly',lambda x:x['descriptor']['capabilities'].update(readonly='unsupported')),
        ('network',lambda x:x['descriptor']['capabilities'].update(network='supported')),
        ('join',lambda x:x['descriptor']['capabilities'].update(multi_table='supported')),
    ]
    for name,mutate in mutations:
        x=deepcopy(e);mutate(x)
        try:validate_snapshot(x['snapshot'],x['source'],authorized_source_ids=['source.demo'],descriptor=x['descriptor'])
        except DataSourceContractError as exc:rejected.append({'id':name,'code':str(exc)})
        else:raise AssertionError('mutation_accepted')
    source=deepcopy(e['source']);source.update(status='draft',binding=None)
    validate_transition(source,'verified',expected_revision=3,actor_role='admin',operation_id='op.test')
    for target,role in [('published','admin'),('verified','visitor')]:
        try:validate_transition(source,target,expected_revision=3,actor_role=role,operation_id='op.test')
        except DataSourceContractError as exc:rejected.append({'id':target+'_'+role,'code':str(exc)})
        else:raise AssertionError('invalid_transition_accepted')
    paths=[example,*sorted((ROOT/'configs/schemas/data-sources').glob('*.json')),
           ROOT/'src/backend/data_sources/contracts.py',Path(__file__)]
    report={'version':'1.0.0','scope':'public_fake_contract_not_runtime','positive_snapshot':1,'negative_checks':rejected,
            'sources_utf8_lf':{p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes().replace(b'\r\n',b'\n')).hexdigest() for p in paths},
            'model_api_calls':0,'real_key_reads':0,'cost_cny':0,
            'not_proven':['registry_atomicity','credentials_storage','connector_execution','UI_integration','business_accuracy']}
    report['stable_sha256']=digest(report)
    return report

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--check',action='store_true')
    p.add_argument('--report',type=Path,default=ROOT/'tests/reports/fin-ds01-contracts-v1.json')
    args=p.parse_args();r=build_report()
    if args.check:assert json.loads(args.report.read_text(encoding='utf-8'))==r,'report_drift'
    else:args.report.write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
    print(json.dumps({'negative_checks':len(r['negative_checks']),'stable_sha256':r['stable_sha256']}))
