"""Real PDF preflight -> C projection -> unmodified A validator, in process."""
import argparse
import copy
import hashlib
import importlib
import json
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'src'))

def run(a_root=ROOT):
    a_root = Path(a_root).resolve()
    import backend.agent
    if a_root != ROOT:
        backend.agent.__path__.insert(0,str(a_root/'src/backend/agent'))
    validator=importlib.import_module('backend.agent.round2_contracts')
    from backend.document_processing.public import assess_document
    assets=ROOT/'tests/fixtures/document-complexity/v1'
    rows=[]
    for document in json.loads((assets/'manifest.json').read_text(encoding='utf-8'))['documents']:
        data=(assets/document['path']).read_bytes()
        if hashlib.sha256(data).hexdigest()!=document['sha256']:raise ValueError('pdf_hash_mismatch')
        pair=assess_document(data,'doc.'+document['document_id'],
                             'request.'+document['document_id'],contract_validator=validator)
        rows.append({'document_id':document['document_id'],'contract_valid':True,**pair})
    first=next(row for row in rows if row['result']['selected_strategy_id'])
    rejected=[]
    for name,mutate in (
        ('wrong_pdf_hash',lambda r:r.update(document_sha256='0'*64)),
        ('wrong_request_binding',lambda r:r.update(input_sha256='0'*64)),
        ('external_processing',lambda r:r.update(external_processing_authorized=True)),
        ('OCR_permission',lambda r:r.update(ocr_authorized=True)),
        ('unknown_strategy',lambda r:r.update(selected_strategy_id='paid_model.v1')),
        ('nonexistent_evidence',lambda r:r['reasons'][0].update(evidence_refs=['feature.nonexistent'])),
    ):
        result=copy.deepcopy(first['result']);mutate(result)
        try:validator.validate_complexity_pair(first['request'],result)
        except validator.Round2ContractError:rejected.append({'mutation':name,'rejected':True})
        else:raise AssertionError('invalid_contract_pair_accepted:'+name)
    files=[a_root/'src/backend/agent/round2_contracts.py',*sorted((a_root/'configs/schemas/round2').glob('*.json'))]
    sample=json.loads((assets/'manifest.json').read_text(encoding='utf-8'))['documents'][0]
    try:
        zero=assess_document((assets/sample['path']).read_bytes(),'doc.zero','request.zero',
                             contract_validator=validator,available_strategy_ids=())
        assert zero['request']['available_strategies']==[]
        assert zero['result']['selected_strategy_id'] is None
        assert zero['result']['typed_decision']['candidate_options']==[]
        assert zero['result']['typed_decision']['decision_type']=='abstain'
        assert zero['result']['uncertainty_reason']=='policy_restriction'
        zero_check={'status':'passed','pair':zero}
    except validator.Round2ContractError as exc:
        zero_check={'status':'dependency_rejects_request_1.1.0','error_code':str(exc)}
    return {'scope':'actual_pdf_to_C_assessment_to_A_v1_validator','documents':len(rows),'rows':rows,
            'zero_capability':zero_check,
            'negative_binding_checks':rejected,'all_actual_pdf_hashes_match_manifest':True,
            'A_sources':{str(p.relative_to(a_root)):hashlib.sha256(p.read_bytes().replace(b'\r\n',b'\n')).hexdigest() for p in files},
            'C_sources':{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes().replace(b'\r\n',b'\n')).hexdigest() for p in [ROOT/'src/backend/document_processing/public.py',ROOT/'src/backend/document_processing/complexity.py',Path(__file__)]},
            'model_calls':0,'key_reads':0,'cost_cny':0,'HTTP':False}

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--A-root',type=Path,default=ROOT);p.add_argument('--report',type=Path,required=True);a=p.parse_args()
    r=run(a.A_root);a.report.write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n');print(json.dumps({'documents':r['documents']}))
