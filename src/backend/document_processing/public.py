"""A-owned document-complexity v1 projection; no HTTP/product envelope change.

The real A validator is injected explicitly until its dependency is merged.
This assessment performs preflight only; selected-path execution is separate.
"""
from .complexity import inspect_structure,select_available

def assess_document(pdf_bytes, document_id, request_id, *, contract_validator,
                    available_strategy_ids=('pypdf_text.v1','pdfplumber_layout.v1')):
    features=inspect_structure(pdf_bytes)
    observations=[];catalog=[];reason_refs={}
    for page in features['pages']:
        ref=f'feature.{document_id}.p{page["page"]}'
        catalog.append({'evidence_id':ref,'page_number':page['page'],'kind':'preflight_observation'})
        observations.append({'page_number':page['page'],'text_characters':page['char_count'],
                             'image_count':page['image_count'],'table_hint_count':int(page['edge_count']>=8 or page['sparse_table_hint']),
                             'layout_hint_count':int(page['column_gap'])+int(page['font_size_variants']>=4),
                             'evidence_refs':[ref]})
        for reason in page['reason_codes'] or ['simple_layout_observed']:
            reason_refs.setdefault(reason,[]).append(ref)
    names={'pypdf_text.v1':'pypdf text','pdfplumber_layout.v1':'pdfplumber layout'}
    if len(available_strategy_ids)!=len(set(available_strategy_ids)) or any(s not in names for s in available_strategy_ids):
        raise ValueError('unsupported_available_strategy')
    strategies=[{'strategy_id':s,'display_name':names[s]} for s in available_strategy_ids]
    request={'request_version':'1.1.0' if not strategies else '1.0.0','request_id':request_id,'phase':'pre_strategy',
             'feature_version':'preflight-observation.v1',
             'document':{'document_id':document_id,'sha256':features['pdf_sha256'],'media_type':'application/pdf',
                         'byte_count':len(pdf_bytes),'page_count':len(features['pages'])},
             'observations':observations,'evidence_catalog':catalog,'available_strategies':strategies}
    contract_validator.validate_complexity_request(request)
    unknown=features['unknown'];score=None if unknown else features['score']
    choice=select_available(features,[{'pypdf_text.v1':'fast','pdfplumber_layout.v1':'structured'}[s] for s in available_strategy_ids])
    selected=None if choice is None else ('pypdf_text.v1' if choice=='fast' else 'pdfplumber_layout.v1')
    abstain=selected is None
    refs=[x['evidence_id'] for x in catalog][:32]
    decision={'decision_type':'abstain' if abstain else 'choice',
              'candidate_options':[{'candidate_id':s['strategy_id'],'display_name':s['display_name'],
                                    'evidence_refs':refs,'raw_features':{'preflight_only':True}}
                                   for s in strategies],
              'selected_option':selected,'raw_score':None if abstain else score,'calibration_version':None,
              'evidence_refs':refs,'missing_or_conflicting_slots':[],
              'recommended_action':'escalate' if abstain else 'accept',
              'abstain_reason':('insufficient_evidence' if unknown else 'policy_restriction') if abstain else None,'contract_version':'1.0.0'}
    result={'result_version':'1.0.0','request_id':request_id,'input_sha256':contract_validator.canonical_hash(request),
            'document_id':document_id,'document_sha256':features['pdf_sha256'],
            'feature_version':'preflight-observation.v1','policy_version':'complexity_policy.v2',
            'assessment_scope':'structural_complexity_not_quality',
            'complexity':'unknown' if unknown else 'complex' if score>=.5 else 'moderate' if score>=.35 else 'simple',
            'complexity_score':score,'score_semantics':'uncalibrated_complexity_score',
            'uncertainty_reason':('unsupported_scan' if 'no_text_layer' in features['reason_codes'] else 'insufficient_features') if unknown else ('policy_restriction' if abstain else None),
            'reasons':[{'reason_code':reason,'evidence_refs':values[:32]} for reason,values in sorted(reason_refs.items())],
            'selected_strategy_id':selected,'typed_decision':decision,'execution_status':'not_started',
            'ocr_authorized':False,'external_processing_authorized':False}
    contract_validator.validate_complexity_pair(request,result)
    return {'request':request,'result':result}
