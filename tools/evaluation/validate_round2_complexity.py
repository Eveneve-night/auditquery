"""Real parser/strategy comparison on public development assets, labels isolated."""
import argparse
import hashlib
import json
from pathlib import Path
import statistics
import sys
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'src'))
from backend.document_processing.complexity import execute_path,process_adaptive,normalize,VERSION
from backend.rag.lexical_retriever import BM25Retriever
ASSETS=ROOT/'tests/fixtures/document-complexity/v1'

def score(parsed,label):
    blocks={f'p{b["page"]}.b{i}':b for i,b in enumerate(parsed['blocks']) if b['text']}
    retriever=BM25Retriever({key:b['text'] for key,b in blocks.items()}) if blocks else None
    outcomes=[]
    for question in label['questions']:
        ranked=retriever.retrieve(question['query'],3) if retriever else []
        found=None
        for key,_ in ranked:
            block=blocks[key];start=block['text'].find(normalize(question['quote']))
            if start>=0 and block['page']==question['page']:
                end=start+len(normalize(question['quote']))
                found={'block_id':key,'page':block['page'],'char_start':start,
                       'char_end':end,'snippet':block['text'][start:end]};break
        outcomes.append({'query':question['query'],'supported':found is not None,'source':found})
    return outcomes

def run():
    manifest=json.loads((ASSETS/'manifest.json').read_text(encoding='utf-8'))
    # Labels are used only after parser execution; never passed into prediction.
    scoring={x['document_id']:x for x in json.loads((ASSETS/'scoring.json').read_text(encoding='utf-8'))}
    rows=[]
    for document in manifest['documents']:
        data=(ASSETS/document['path']).read_bytes()
        if hashlib.sha256(data).hexdigest()!=document['sha256']:raise ValueError('pdf_hash_mismatch')
        calls={'uniform_fast':lambda:execute_path(data,'fast'),
               'uniform_structured':lambda:execute_path(data,'structured'),
               'adaptive':lambda:process_adaptive(data)}
        samples={name:[] for name in calls}
        names=list(calls)
        for repeat in range(3):
            for name in names[repeat:]+names[:repeat]:samples[name].append(calls[name]())
        outputs={name:values[-1] for name,values in samples.items()}
        row={'document_id':document['document_id'],'family_id':document['family_id'],'sha256':document['sha256'],'paths':{}}
        for name,parsed in outputs.items():
            matches=score(parsed,scoring[document['document_id']])
            row['paths'][name]={'success':parsed['success'],'error_code':parsed['error_code'],
                'elapsed_ms':round(statistics.median(v.get('total_ms',v.get('elapsed_ms')) for v in samples[name]),3),
                'timing_samples_ms':[v.get('total_ms',v.get('elapsed_ms')) for v in samples[name]],
                'strategy':parsed['strategy'],
                'block_count':len(parsed['blocks']),'questions':matches,
                'table_row_blocks':sum(b['kind']=='table_row' for b in parsed['blocks']),
                'repeat_output_stable':all(v['blocks']==parsed['blocks'] for v in samples[name]),
                'scan_refused':scoring[document['document_id']]['scan_boundary'] and not parsed['success']}
        row['preflight_ms']=outputs['adaptive'].get('preflight_ms')
        row['features']=outputs['adaptive']['features'];rows.append(row)
    summary={}
    for name in ('uniform_fast','uniform_structured','adaptive'):
        values=[row['paths'][name] for row in rows];q=[q for v in values for q in v['questions']]
        summary[name]={'documents':len(values),'text_extracted':sum(v['success'] for v in values),
                       'questions':len(q),'supported_questions':sum(x['supported'] for x in q),
                       'total_ms':round(sum(v['elapsed_ms'] for v in values),3),
                       'median_ms':round(statistics.median(v['elapsed_ms'] for v in values),3),
                       'scan_boundaries_refused':sum(v['scan_refused'] for v in values),
                       'table_row_blocks':sum(v['table_row_blocks'] for v in values)}
    # Paired observed difficulty: support loss of the actual fast parser, never
    # included in preflight features. Report confusion counts, not calibrated confidence.
    tp=fp=fn=tn=0;baseline=dict(tp=0,fp=0,fn=0,tn=0)
    for row in rows:
        if row['features']['unknown']:continue
        difficult=any(not q['supported'] for q in row['paths']['uniform_fast']['questions'])
        predicted=row['features']['strategy']=='structured'
        tp+=predicted and difficult;fp+=predicted and not difficult;fn+=not predicted and difficult;tn+=not predicted and not difficult
        pages_predicted=len(row['features']['pages'])>1
        baseline['tp' if pages_predicted and difficult else 'fp' if pages_predicted else 'fn' if difficult else 'tn']+=1
    benefit=(summary['adaptive']['supported_questions']>=summary['uniform_structured']['supported_questions']
             and summary['adaptive']['total_ms']<summary['uniform_structured']['total_ms'])
    return {'report_version':'INT08-public-prototype.v1','implementation_version':VERSION,
            'observed_pareto_benefit_vs_structured':benefit,
            'conclusion':'prototype_observed_no_strategy_benefit' if not benefit else 'public_sample_benefit_only',
            'split':'prompt_dev','document_count':len(rows),'family_count':manifest['family_count'],
            'summary':summary,'difficulty_confusion':{'tp':tp,'fp':fp,'fn':fn,'tn':tn},'rows':rows,
            'page_count_gt_1_baseline':baseline,
            'source_files':{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes().replace(b'\r\n',b'\n')).hexdigest() for p in [Path(__file__),ROOT/'src/backend/document_processing/complexity.py',ASSETS/'manifest.json',ASSETS/'scoring.json']},
            'adaptive_vs_structured':{'latency_reduction_fraction':round(1-summary['adaptive']['total_ms']/summary['uniform_structured']['total_ms'],4),
                                      'quote_support_delta':summary['adaptive']['supported_questions']-summary['uniform_structured']['supported_questions']},
            'preflight_cost_included_in_adaptive':True,'timing_repeats':3,'timing_order':'rotated_per_document',
            'not_proven':['hidden_generalization','calibrated_complexity','strategy_cost_benefit','OCR','formal_INT08_achievement'],
            'model_calls':0,'key_reads':0,'cost_cny':0}

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--report',type=Path,default=ROOT/'tests/reports/round2-c-complexity-v1.json');a=p.parse_args()
    r=run();a.report.parent.mkdir(parents=True,exist_ok=True);a.report.write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n');print(json.dumps(r['summary']))
