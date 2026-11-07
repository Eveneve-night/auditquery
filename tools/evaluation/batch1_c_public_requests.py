"""Build semantic-request.v1 from public questions and authorized metadata only."""
import json

def build_public_request(case, pack):
    # Never inspect scoring/review fields, and never accept caller required slots.
    question = case['input']['question']
    context = pack.model_context(max_data_level='L1')
    ids=[]
    for role, field in [('concepts','concept_id'),('slots','slot_id'),('schema_catalog','object_id')]:
        ids.extend(item[field] for item in context['artifacts'][role]['items'])
    if not 0 < len(set(ids)) <= 200:
        raise ValueError('authorized_catalog_size_unsupported')
    request = {
        'request_version':'1.0.0','request_id':'request.c.'+case['case_id'],
        'question':question,
        'domain_context':{
            'domain_pack_ref':{'domain_pack_id':pack.domain_pack_id,'version':pack.version,
                               'manifest_sha256':pack.manifest_sha256},
            'authorized_catalog':{'candidate_ids':sorted(set(ids)),'max_data_level':'L1'}},
        'session':{'session_id':'session.c.'+case['case_id'],'turn_index':0,'slot_observations':[]}}
    # Exercise the same serialization boundary a consumer uses.
    return json.loads(json.dumps(request,ensure_ascii=False))
