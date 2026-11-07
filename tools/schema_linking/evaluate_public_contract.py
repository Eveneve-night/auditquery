"""Actual public v1 pairs scored by A #111, no result fixture substitution."""
import argparse,importlib.util,json,sys,time,hashlib,statistics,subprocess
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT/'src'))
from backend.agent.domain_pack import DomainPackRegistry
from backend.schema_linking.public import PublicSchemaLinker


def load_dependency(root):
    path=Path(root)/'src/backend/agent/round2_contracts.py'
    spec=importlib.util.spec_from_file_location('backend.agent.round2_contracts',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    sys.modules['backend.agent.round2_contracts']=module
    metric_path=Path(root)/'tools/evaluation/round2_metrics.py'
    spec=importlib.util.spec_from_file_location('round2_published_metrics',metric_path)
    metrics=importlib.util.module_from_spec(spec);spec.loader.exec_module(metrics)
    return module,metrics


def build_report(contract_root):
    # The operator explicitly selects this trusted checkout, never a request
    # field. Limit the ownership exception to these read-only Git invocations.
    git=['git','-c','safe.directory='+Path(contract_root).resolve().as_posix(),'-C',str(contract_root)]
    dependency_head=subprocess.check_output([*git,'rev-parse','HEAD'],text=True).strip()
    dependency_dirty=bool(subprocess.check_output([*git,'status','--porcelain','--','src/backend/agent/round2_contracts.py','tools/evaluation/round2_metrics.py','configs/schemas/round2'],text=True).strip())
    if dependency_dirty:raise ValueError('published contract dependency must be a clean snapshot')
    contract,metrics=load_dependency(contract_root)
    pack=DomainPackRegistry(ROOT/'domain-packs').load('order_to_cash','1.1.0')
    assets=ROOT/'tests/fixtures/schema-linking/v1'
    manifest=json.loads((assets/'manifest.json').read_text(encoding='utf-8'))
    for name,expected in manifest['files'].items():
        if hashlib.sha256((assets/name).read_bytes().replace(b'\r\n',b'\n')).hexdigest()!=expected:
            raise ValueError('public development asset hash mismatch')
    inputs=json.loads((assets/'inputs.json').read_text(encoding='utf-8'))
    if manifest['case_count']!=len(inputs) or any(row['split']!='prompt_dev' for row in inputs):
        raise ValueError('only frozen public development inputs allowed')
    # Protocol eligibility is determined before inference, independently of
    # the labels or observed success: v1 requires full catalogs, not subsets.
    eligible=[row for row in inputs if 'authorized_table_ids' not in row['input']]
    scoring=json.loads((assets/'scoring.json').read_text(encoding='utf-8'))
    labels={row['case_id']:row for row in scoring}
    report={'report_version':'round2-b-public-adapter.v2','split':'prompt_dev','domain_pack_ref':contract.pack_reference(pack),
            'contract_dependency':{'pr':111,'head':dependency_head,'request':'schema-linking-request.v1','result':'schema-linking-result.v1',
                                   'source_sha256':{name:hashlib.sha256((Path(contract_root)/name).read_bytes().replace(b'\r\n',b'\n')).hexdigest() for name in ('src/backend/agent/round2_contracts.py','tools/evaluation/round2_metrics.py')}},
            'input_cases':len(inputs),'full_catalog_cases':len(eligible),
            'excluded_subset_cases':[row['case_id'] for row in inputs if row not in eligible],
            'excluded_reason':'public v1 mandates full model-visible catalog; internal subset/permission probes remain separate, not counted as public quality',
            'modes':{},'model_calls':0,'key_reads':0,'cost_cny':0,'sql_execution_authorized':False,
            'latency_scope':'in_process_public_adapter_including_contract_validation_excluding_pack_load',
            'tokens':None,'not_proven':['blind_generalization','formal_business_accuracy','calibration','multi_table_execution']}
    for mode in ('lexical','enhanced'):
        engine=PublicSchemaLinker(pack,mode=mode,contract_validator=contract);pairs=[];timings=[];score={};strata={}
        for row in eligible:
            request=engine.make_request(row['input']['question'],'request.'+row['case_id'])
            started=time.perf_counter();result=engine.locate(request);timings.append((time.perf_counter()-started)*1000)
            pair={'request':request,'result':result};pairs.append(pair)
            # Only AFTER actual production locate/validation do labels enter.
            label=labels[row['case_id']]
            score[request['request_id']]={'table_ids':label['table_ids'],'column_ids':label['field_ids'],
                                         'status':{'select':'selected','clarify':'clarify','abstain':'abstain'}[label['action']]}
            strata.setdefault(row['family'],[]).append(pair)
        report['modes'][mode]={'metrics':metrics.score_linking(pairs,score,pack),
                              'strata':{name:metrics.score_linking(values,{p['request']['request_id']:score[p['request']['request_id']] for p in values},pack) for name,values in strata.items()},
                              'latency_ms':{'p50':statistics.median(timings),'p95':sorted(timings)[min(len(timings)-1,int(len(timings)*.95))]},
                              'request_catalog_objects':len(pairs[0]['request']['authorized_object_ids']),
                              'catalog_sha256':pairs[0]['request']['catalog_sha256'],
                              'cases':[{'request_id':p['request']['request_id'],'input_sha256':p['result']['input_sha256'],
                                        'result_sha256':contract.canonical_hash(p['result']),
                                        'status':p['result']['status'],'selected_table_id':p['result']['selected_table_id'],
                                        'selected_column_ids':p['result']['selected_column_ids'],
                                        'top_table_ids':[x['object_id'] for x in p['result']['table_candidates'][:5]],
                                        'table_correct':p['result']['selected_table_id'] in score[p['request']['request_id']]['table_ids'] if score[p['request']['request_id']]['status']=='selected' else None,
                                        'status_correct':p['result']['status']==score[p['request']['request_id']]['status'],
                                        'columns_exact':set(p['result']['selected_column_ids'])==set(score[p['request']['request_id']]['column_ids']) if score[p['request']['request_id']]['status']=='selected' else None}
                                       for p in pairs]}
    paths=[*sorted((ROOT/'src/backend/schema_linking').glob('*.py')),ROOT/'tools/schema_linking/evaluate_public_contract.py']
    report['source_sha256']={p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes().replace(b'\r\n',b'\n')).hexdigest() for p in paths}
    report['assets_sha256']={name:hashlib.sha256((assets/name).read_bytes().replace(b'\r\n',b'\n')).hexdigest() for name in ('inputs.json','scoring.json','manifest.json')}
    return report

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--contract-root',type=Path,default=ROOT);parser.add_argument('--report',type=Path,required=True);args=parser.parse_args()
    report=build_report(args.contract_root);args.report.parent.mkdir(parents=True,exist_ok=True);args.report.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
    print(json.dumps({mode:row['metrics'] for mode,row in report['modes'].items()},ensure_ascii=False))
