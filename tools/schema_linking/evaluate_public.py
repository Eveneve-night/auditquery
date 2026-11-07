"""Replay only public prompt_dev inputs; labels enter exclusively after locate."""
from pathlib import Path
import hashlib,json,statistics,sys,time
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'src'))
from backend.agent.domain_pack import DomainPackRegistry
from backend.schema_linking.linker import SchemaLinker
ASSETS=ROOT/'tests/fixtures/schema-linking/v1'


def runtime_input(row,engine,mode):
    raw=row['input']
    if not isinstance(raw,dict) or set(raw)-{'question','authorized_table_ids'}:raise ValueError('invalid_runtime_input_fields')
    ids=engine.public_catalog_ids()
    if 'authorized_table_ids' in raw:
        tables=set(raw['authorized_table_ids'])
        if not tables<=set(engine.tables):raise ValueError('unauthorized_table_set')
        ids=[key for key in ids if key in tables or key in engine.columns and engine.columns[key]['parent_object_id'] in tables]
    return {'question':raw['question'],'authorized_ids':ids,'mode':mode}


def rank_metrics(rows,key,target):
    ranks=[];recalls={k:[] for k in (1,3,5)}
    for result,label in rows:
        expected=set(label[target])
        if not expected:continue
        ids=[x['candidate_id'] for x in result[key]]
        first=min((ids.index(x)+1 for x in expected if x in ids),default=None)
        ranks.append(1/first if first else 0)
        for k in recalls:recalls[k].append(len(set(ids[:k])&expected)/len(expected))
    return {'evaluated_cases':len(ranks),'mrr':statistics.mean(ranks) if ranks else None,
            **{'recall_at_'+str(k):statistics.mean(v) if v else None for k,v in recalls.items()}}


def summarize(rows):
    latency=sorted(r['latency_ms'] for r,_ in rows)
    good=sum(r['decision']==s['action'] and (s['action']!='select' or set(r['selected_table_ids'])==set(s['table_ids'])) for r,s in rows)
    exact=sum(r['decision']==s['action'] and (s['action']!='select' or set(r['selected_table_ids'])==set(s['table_ids']) and set(r['selected_field_ids'])==set(s['field_ids'])) for r,s in rows)
    positives=[(r,s) for r,s in rows if s['action']=='select']
    negatives=[(r,s) for r,s in rows if s['action']!='select']
    return {'cases':len(rows),'table':rank_metrics(rows,'table_candidates','table_ids'),
            'field':rank_metrics(rows,'field_candidates','field_ids'),
            'final_table_or_action_accuracy':good/len(rows),'final_exact_table_field_or_action_accuracy':exact/len(rows),
            'status_accuracy':sum(r['decision']==s['action'] for r,s in rows)/len(rows),
            'positive_cases':len(positives),
            'final_table_accuracy':sum(r['decision']=='select' and set(r['selected_table_ids'])==set(s['table_ids']) for r,s in positives)/len(positives) if positives else None,
            'final_table_and_fields_exact':sum(r['decision']=='select' and set(r['selected_table_ids'])==set(s['table_ids']) and set(r['selected_field_ids'])==set(s['field_ids']) for r,s in positives)/len(positives) if positives else None,
            'selection_coverage':sum(r['decision']=='select' for r,_ in rows)/len(rows),
            'nonselected_reference_cases':len(negatives),
            'false_selection_rate':sum(r['decision']=='select' for r,_ in negatives)/len(negatives) if negatives else None,
            'action_counts':{k:sum(r['decision']==k for r,_ in rows) for k in ('select','clarify','abstain')},
            'latency_scope':'metadata_scoring_after_request_validation_excludes_pack_loading',
            'latency_ms':{'p50':statistics.median(latency),'p95':latency[min(len(latency)-1,int(len(latency)*.95))]},
            'context_utf8_bytes':{'min':min(r['context']['utf8_bytes'] for r,_ in rows),'max':max(r['context']['utf8_bytes'] for r,_ in rows)},
            'tokens':None,'tokens_reason':'no_model_or_tokenizer; context bytes measured instead'}


def build_report():
    pack=DomainPackRegistry(ROOT/'domain-packs').load('order_to_cash','1.0.0');engine=SchemaLinker(pack)
    inputs=json.loads((ASSETS/'inputs.json').read_text(encoding='utf-8'))
    scores={x['case_id']:x for x in json.loads((ASSETS/'scoring.json').read_text(encoding='utf-8'))}
    outputs={mode:[] for mode in ('lexical','enhanced')};by_family={mode:{} for mode in outputs};rows=[]
    for row in inputs:
        if row['split']!='prompt_dev':raise ValueError('only_public_development_allowed')
        for mode in outputs:
            result=engine.locate(runtime_input(row,engine,mode))
            # Labels are only looked up AFTER runtime result exists.
            label=scores[row['case_id']];outputs[mode].append((result,label))
            by_family[mode].setdefault(row['family'],[]).append((result,label))
            rows.append({'case_id':row['case_id'],'family':row['family'],'mode':mode,
                         'table_or_action_correct':result['decision']==label['action'] and (label['action']!='select' or set(result['selected_table_ids'])==set(label['table_ids'])),
                         'exact_selection_correct':result['decision']==label['action'] and (label['action']!='select' or set(result['selected_table_ids'])==set(label['table_ids']) and set(result['selected_field_ids'])==set(label['field_ids'])),
                         'decision':result['decision'],'selected_table_ids':result['selected_table_ids'],
                         'selected_field_ids':result['selected_field_ids'],'latency_ms':result['latency_ms'],
                         'top_table_ids':[x['candidate_id'] for x in result['table_candidates'][:5]],
                         'top_field_ids':[x['candidate_id'] for x in result['field_candidates'][:5]],
                         'sql_execution_authorized':result['sql_execution_authorized']})
    manifest=json.loads((ASSETS/'manifest.json').read_text(encoding='utf-8'))
    assert manifest['case_count']==len(inputs)
    for name,expected in manifest['files'].items():
        assert hashlib.sha256((ASSETS/name).read_bytes().replace(b'\r\n',b'\n')).hexdigest()==expected
    source={p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes().replace(b'\r\n',b'\n')).hexdigest() for p in sorted((ROOT/'src/backend/schema_linking').glob('*.py'))}
    for relative in ('tools/schema_linking/evaluate_public.py','tools/schema_linking/build_order_to_cash.py'):
        source[relative]=hashlib.sha256((ROOT/relative).read_bytes().replace(b'\r\n',b'\n')).hexdigest()
    return {'report_version':'round2-b-internal-development.v2','split':'prompt_dev','hidden_or_validation_exposures':0,
            'scope':'public_metadata_linking_not_execution_or_generalization','tables':len(engine.tables),'columns':len(engine.columns),'foreign_key_edges':len(engine.edges),
            'domain_pack_manifest_sha256':pack.manifest_sha256,'schema_ddl_sha256':hashlib.sha256((ROOT/'data/schema-linking/order_to_cash/v1/schema.sql').read_bytes().replace(b'\r\n',b'\n')).hexdigest(),'source_sha256':source,
            'assets':{name:hashlib.sha256((ASSETS/name).read_bytes().replace(b'\r\n',b'\n')).hexdigest() for name in ('inputs.json','scoring.json')},
            'modes':{mode:summarize(value) for mode,value in outputs.items()},
            'strata':{mode:{family:summarize(value) for family,value in groups.items()} for mode,groups in by_family.items()},'rows':rows,
            'public_contract':'internal_diagnostic_only_use_PublicSchemaLinker_for_public_v1','model_calls':0,'key_reads':0,'cost_cny':0,
            'sql_execution_authorized':False,'not_proven':['blind_generalization','business_accuracy','calibration','multi_table_execution','formal_target_attainment']}

if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--report',type=Path,required=True);args=parser.parse_args()
    result=build_report();args.report.parent.mkdir(parents=True,exist_ok=True);args.report.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result['modes'],ensure_ascii=False))
