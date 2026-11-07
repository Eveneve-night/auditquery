"""Predeclared real public experiment; labels enter only after execution."""
import argparse,hashlib,importlib.util,json,statistics,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path[:0]=[str(ROOT/'src'),str(ROOT)]
from backend.document_processing.complexity import process_adaptive,VERSION,normalize
from tools.evaluation.validate_round2_complexity import score
from tools.evaluation.baselines import document_complexity_v1 as old
ASSETS=ROOT/'tests/fixtures/document-complexity/v1'
def digest(p):return hashlib.sha256(p.read_bytes().replace(b'\r\n',b'\n')).hexdigest()
def total(r):return r.get('total_ms',r['elapsed_ms'])
def score_structure(r,label):
    q=label['questions']
    return sum(any(b['page']==x['page'] and normalize(x['quote']) in b['text'] for b in r['blocks']) for x in q)/len(q) if q else 0
def run(a_root=ROOT):
    a_root = Path(a_root).resolve()
    assert digest(Path(old.__file__))=='fb84cc4171f99703c05bb60c4e1f6002b74fbc7eb4e60da19e848fb60a70eae8','v1 drift'
    ap=a_root/'tools/evaluation/round2_metrics.py'
    if a_root == ROOT:
        from tools.evaluation import round2_metrics as metrics
    else:
        spec=importlib.util.spec_from_file_location('A_metrics',ap)
        metrics=importlib.util.module_from_spec(spec);spec.loader.exec_module(metrics)
    manifest=json.loads((ASSETS/'manifest.json').read_text(encoding='utf-8'))
    labels={r['document_id']:r for r in json.loads((ASSETS/'scoring.json').read_text(encoding='utf-8'))}
    names=('uniform_pypdf','uniform_pdfplumber','adaptive','legacy_adaptive_v1');batches=[]
    for batch in range(3):
        rows=[];records=[]
        for number,doc in enumerate(manifest['documents']):
            data=(ASSETS/doc['path']).read_bytes();assert hashlib.sha256(data).hexdigest()==doc['sha256']
            calls={'uniform_pypdf':lambda:old.execute_path(data,'fast'),'uniform_pdfplumber':lambda:old.execute_path(data,'structured'),
                   'adaptive':lambda:process_adaptive(data),'legacy_adaptive_v1':lambda:old.process_adaptive(data)}
            values={name:[] for name in names}
            for repeat in range(5):
                shift=(number+repeat+batch)%4
                for name in names[shift:]+names[:shift]:values[name].append(calls[name]())
            # Only after all real executions do labels enter the scorer.
            label=labels[doc['document_id']];paths={}
            for name,raw in values.items():
                r=sorted(raw,key=total)[2];q=score(r,label);structure=score_structure(r,label)
                samples=[{'total_ms':total(v),'preflight_ms':v.get('preflight_ms',0),'parser_only_ms':v['elapsed_ms'],
                          'success':v['success'],'error_code':v['error_code'],
                          'supported_questions':sum(x['supported'] for x in score(v,label))} for v in raw]
                paths[name]={'strategy':r['strategy'],'success':r['success'],'error_code':r['error_code'],
                             'total_ms':total(r),'preflight_ms':r.get('preflight_ms',0),'parser_only_ms':r['elapsed_ms'],
                             'samples':samples,'questions':q,'structure_score':structure,'features':r.get('features'),
                             'table_row_blocks':sum(b['kind']=='table_row' for b in r['blocks']),
                             'repeat_output_stable':all(v['blocks']==r['blocks'] for v in raw)}
                if name!='legacy_adaptive_v1':
                    records.append({'document_id':doc['document_id'],'document_sha256':doc['sha256'],'arm':name,
                                    'outcome':'succeeded' if r['success'] else 'unsupported' if label['scan_boundary'] else 'failed',
                                    'preflight_ms':r.get('preflight_ms',0),'parse_ms':max(0,round(total(r)-r.get('preflight_ms',0),3)),
                                    'structure_score':structure,'answer_support_score':sum(x['supported'] for x in q)/len(q) if q else 0})
            rows.append({'document_id':doc['document_id'],'sha256':doc['sha256'],'paths':paths})
        summary={}
        for name in names:
            paths=[r['paths'][name] for r in rows]
            summary[name]={'supported_questions':sum(q['supported'] for p in paths for q in p['questions']),
                           'total_questions':sum(len(p['questions']) for p in paths),'success_documents':sum(p['success'] for p in paths),
                           'total_ms':round(sum(p['total_ms'] for p in paths),3),'preflight_ms':round(sum(p['preflight_ms'] for p in paths),3),
                           'sample_failures':sum(not s['success'] for p in paths for s in p['samples']),
                           'all_repeats_stable':all(p['repeat_output_stable'] for p in paths)}
        batches.append({'batch':batch+1,'summary':summary,'rows':rows,'A_records':records,'A_comparison':metrics.compare_document_routes(records)})
        print(json.dumps({'batch':batch+1,'summary':summary}),flush=True)
    quality=all(b['summary']['adaptive']['supported_questions']==32 and b['summary']['adaptive']['success_documents']==30 and b['summary']['adaptive']['all_repeats_stable'] for b in batches)
    reduction=1-statistics.median(b['summary']['adaptive']['total_ms']/b['summary']['legacy_adaptive_v1']['total_ms'] for b in batches)
    benefit=quality and all(b['summary']['adaptive']['total_ms']<=.95*b['summary']['uniform_pdfplumber']['total_ms'] for b in batches)
    sources=[Path(__file__),Path(old.__file__),ROOT/'src/backend/document_processing/complexity.py',ROOT/'src/backend/document_processing/public.py',
             ROOT/'docs/协作/INT08-v2-实验预声明.md',ASSETS/'manifest.json',ASSETS/'scoring.json',ROOT/'tools/evaluation/validate_round2_complexity.py']
    return {'report_version':'INT08-public-prototype.v2','implementation_version':VERSION,'split':'prompt_dev','documents':32,
            'batches':batches,'repeats_per_document_arm':5,'order':'rotate_by_document_repeat_and_batch',
            'criteria':{'quality_restored':quality,'median_latency_reduction_vs_legacy':round(reduction,4),
                        'legacy_overhead_reduction_at_least_20_percent':reduction>=.2,'strategy_benefit_vs_fixed_structure_all_3_batches':benefit},
            'conclusion':'public_sample_strategy_benefit_only' if benefit else 'no_demonstrated_strategy_benefit_vs_fixed_structure',
            'A_scorer_sha256':digest(ap),'A_root_source':str(a_root),
            'sources_utf8_lf_sha256':{p.relative_to(ROOT).as_posix():digest(p) for p in sources},
            'timing_scope':'complete observed wall time; A parse_ms includes route/close overhead after measured preflight',
            'structure_score_semantics':'posthoc same-page contiguous ordered scoring-anchor fraction; not full layout quality',
            'scans':'2 unsupported documents in every arm; both scores zero','frozen_v1_report':'tests/reports/round2-c-complexity-v1.json',
            'not_proven':['hidden_generalization','calibration','OCR','formal_INT08_achievement'],'model_calls':0,'key_reads':0,'cost_cny':0}
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--A-root',type=Path,default=ROOT)
    p.add_argument('--report',type=Path,default=ROOT/'tests/reports/round2-c-complexity-v2.json');a=p.parse_args()
    a.report.write_text(json.dumps(run(a.A_root),ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
