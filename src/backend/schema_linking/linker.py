"""Internal lexical/alias/relationship baseline, no calibrated probabilities.

Inference accepts only an allowlisted request; scoring assets are never loaded.
The public versioned adapter is separately dependent on A's round2 contract.
"""
from __future__ import annotations
from difflib import SequenceMatcher
import hashlib
import json
import re
import time
import unicodedata
from ..agent.domain_pack import LoadedDomainPack
from ..agent.typed_decision_runtime import CandidateEvidence

VERSION='schema-linker.internal.v2'


def normalize(text):
    return ' '.join(unicodedata.normalize('NFKC',text).casefold().split())


def tokens(text):
    text=normalize(text)
    values=set(re.findall(r'[a-z0-9]+',text))
    for part in re.findall(r'[\u3400-\u9fff]+',text):
        values.update(part[i:i+2] for i in range(len(part)-1))
        if len(part)==1: values.add(part)
    return values


def similarity(query, label):
    terms=tokens(label)
    return len(tokens(query)&terms)/max(1,len(terms))


def phrase_spans(query,phrase):
    phrase=normalize(phrase)
    if not phrase:return []
    pattern=re.escape(phrase)
    if phrase.isascii():pattern=r'(?<![a-z0-9_])'+pattern+r'(?![a-z0-9_])'
    return [m.span() for m in re.finditer(pattern,query)]


class SchemaLinker:
    def __init__(self,pack:LoadedDomainPack,*,max_data_level="L1"):
        if not isinstance(pack,LoadedDomainPack):raise TypeError('validated_domain_pack_required')
        self.pack=pack
        context=pack.model_context(max_data_level=max_data_level,roles=('schema_catalog','aliases','relations','concepts'))['artifacts']
        self.exportable={str(x['object_id']) for x in context.get('schema_catalog',{'items':[]})['items']}
        self.exportable_concepts={str(x['concept_id']) for x in context.get('concepts',{'items':[]})['items']}
        self.objects={str(x['object_id']):x for x in pack.artifact_items('schema_catalog')}
        self.tables={key:x for key,x in self.objects.items() if x['object_type']=='table'}
        self.columns={key:x for key,x in self.objects.items() if x['object_type']=='column'}
        self.aliases={key:[] for key in self.tables}
        for key,table in self.tables.items():
            self.aliases[key]=[(table['display_name'],key),(table['physical_name'],key)]
            for alias in context.get('aliases',{'items':[]})['items']:
                if alias['concept_id'] in table['concept_refs']:
                    self.aliases[key].append((alias['surface_form'],alias['alias_id']))
        self.edges=[]
        for relation in context.get('relations',{'items':[]})['items']:
            source=self._table_of(relation['source_object_id']);target=self._table_of(relation['target_object_id'])
            if source and target and source!=target:self.edges.append((source,target,str(relation['relation_id']),str(relation['source_object_id']),str(relation['target_object_id'])))

    def _table_of(self,key):
        item=self.objects.get(key)
        if not item:return None
        return key if item['object_type']=='table' else item['parent_object_id'] if item['object_type']=='column' else None

    def _public_ancestry(self,key):
        while key is not None:
            item=self.objects[key]
            if key not in self.exportable or item['authorization_level']!='read_public':
                return False
            key=item['parent_object_id']
        return True

    def public_catalog_ids(self):
        return sorted(k for k,x in self.objects.items()
                      if x['object_type'] in ('table','column') and self._public_ancestry(k))

    def _field_query(self,query,table_id,column_ids):
        # A table mention identifies the relation, not every similarly named
        # column. Preserve explicit column names (e.g. an alias nested inside
        # an amount name), and remove only the remaining table-name spans.
        field_spans=[span for cid in column_ids if self.columns[cid]['parent_object_id']==table_id
                     for label in (self.columns[cid]['display_name'],self.columns[cid]['physical_name'])
                     for span in phrase_spans(query,label)]
        masked=list(query)
        for label,_ in self.aliases[table_id]:
            for start,end in phrase_spans(query,label):
                if any(a<=start and end<=b and b-a>end-start for a,b in field_spans):
                    continue
                masked[start:end]=' '*(end-start)
        return ''.join(masked)

    def locate(self,request):
        allowed_keys={'question','authorized_ids','recognized_refs','mode'}
        if not isinstance(request,dict) or not {'question','authorized_ids'}<=set(request) or set(request)-allowed_keys:
            raise ValueError('invalid_linking_request_fields')
        question=request['question'];authorized=request['authorized_ids'];mode=request.get('mode','enhanced')
        recognized=request.get('recognized_refs',[])
        if not isinstance(question,str) or not question.strip() or len(question)>8192:raise ValueError('invalid_question')
        if mode not in ('lexical','enhanced'):raise ValueError('invalid_linking_mode')
        if not isinstance(authorized,list) or not authorized or len(authorized)>2000 or any(not isinstance(k,str) for k in authorized) or len(authorized)!=len(set(authorized)):
            raise ValueError('invalid_authorized_catalog')
        if not set(authorized)<=set(self.public_catalog_ids()):raise ValueError('unauthorized_catalog_object')
        visible=set(authorized)
        if any(k in self.columns and self.columns[k]['parent_object_id'] not in visible for k in visible):raise ValueError('column_parent_not_authorized')
        concept_ids={c for k in visible if k in self.tables for c in self.tables[k]['concept_refs']} & self.exportable_concepts
        if not isinstance(recognized,list) or any(not isinstance(c,str) for c in recognized) or not set(recognized)<=concept_ids:
            raise ValueError('unrecognized_or_unauthorized_reference')
        started=time.perf_counter();query=normalize(question)
        # Authorization precedes retrieval. No unavailable object's aliases or
        # relation edges may affect ranking, raw features, or context sizes.
        table_ids=sorted(visible&self.tables.keys());column_ids=sorted(visible&self.columns.keys())
        hits=[]
        for tid in table_ids:
            for surface,ref in self.aliases[tid]:
                for start,end in phrase_spans(query,surface):hits.append((start,end,tid,ref))
        hits=[h for h in hits if not any(t[0]<=h[0] and h[1]<=t[1] and (t[1]-t[0])>(h[1]-h[0]) for t in hits)]
        alias_scores={tid:0.0 for tid in table_ids};alias_refs={tid:set() for tid in table_ids}
        for start,end,tid,ref in hits:
            alias_scores[tid]=max(alias_scores[tid],min(4.0,(end-start)/2.0))
            alias_refs[tid].add(ref)
        anchors={tid for tid in table_ids if alias_scores[tid]>0 or set(self.tables[tid]['concept_refs'])&set(recognized)}
        scores={};reasons={};refs={}
        for tid in table_ids:
            table=self.tables[tid]
            lexical=max(similarity(query,table['display_name']),similarity(query,table['physical_name']))
            colmatch=max((similarity(query,self.columns[k]['display_name']) for k in column_ids if self.columns[k]['parent_object_id']==tid),default=0.0)
            relation_refs={ref for a,b,ref,src,dst in self.edges if a in visible and b in visible and src in visible and dst in visible and (a==tid and b in anchors or b==tid and a in anchors)} if mode=='enhanced' else set()
            fuzzy=max((SequenceMatcher(None,query,normalize(a)).ratio() for a,_ in self.aliases[tid]),default=0.0)
            features={'lexical':lexical,'field_overlap':colmatch*.2,'alias':alias_scores[tid] if mode=='enhanced' else 0.0,
                      'relation_consistency':min(.4,len(relation_refs)*.2),
                      'recognized_concept':.5 if mode=='enhanced' and set(table['concept_refs'])&set(recognized) else 0.0,
                      'fuzzy_suggestion':.5 if mode=='enhanced' and fuzzy>=.85 and alias_scores[tid]==0 else 0.0}
            scores[tid]=sum(features.values());reasons[tid]=features
            refs[tid]={tid,*table['source_refs'],*(alias_refs[tid] if mode=='enhanced' else ()),*relation_refs}
        order=sorted(table_ids,key=lambda k:(-scores[k],k))
        ranked=[]
        for tid in order:
            raw=scores[tid]/(1+scores[tid])
            candidate=CandidateEvidence(tid,self.tables[tid]['display_name'],'schema_object',raw,tuple(sorted(refs[tid])),{'candidate_kind':'schema_object',**reasons[tid]})
            ranked.append(dict(candidate_id=candidate.candidate_id,display_name=candidate.display_name,raw_score=round(raw,6),
                               evidence_refs=list(candidate.evidence_refs),score_components=reasons[tid]))
        field_rank=[]
        field_queries={tid:self._field_query(query,tid,column_ids) for tid in table_ids} if mode=='enhanced' else {}
        for cid in column_ids:
            col=self.columns[cid];parent=col['parent_object_id'];field_query=field_queries.get(parent,query)
            lexical=max(similarity(field_query,col['display_name']),similarity(field_query,col['physical_name']))
            combined=lexical+scores[parent]*.35
            field_rank.append(dict(candidate_id=cid,table_id=parent,display_name=col['display_name'],raw_score=round(combined/(1+combined),6),
                                   evidence_refs=[cid,parent,*col['source_refs']],score_components={'field_lexical':lexical,'parent_context':scores[parent]*.35}))
        field_rank.sort(key=lambda x:(-x['raw_score'],x['candidate_id']))
        top=scores[order[0]] if order else 0;gap=top-(scores[order[1]] if len(order)>1 else 0)
        fuzzy_only=bool(order and reasons[order[0]]['fuzzy_suggestion']>0 and alias_scores[order[0]]==0)
        if mode=='enhanced' and sum(alias_scores[k]>=2 for k in table_ids)>1:
            decision,reason='clarify','multiple_explicit_objects_require_scope'
        elif top<.3:decision,reason='abstain','insufficient_metadata_evidence'
        elif gap<.25 or fuzzy_only:decision,reason='clarify','unconfirmed_spelling' if fuzzy_only else 'ambiguous_catalog_candidates'
        elif mode=='enhanced' and not alias_scores[order[0]] and not (set(self.tables[order[0]]['concept_refs'])&set(recognized)):
            decision,reason='clarify','weak_lexical_match_requires_confirmation'
        else:decision,reason='select','metadata_evidence_selected'
        selected=[order[0]] if decision=='select' else []
        selected_fields=[x['candidate_id'] for x in field_rank if x['table_id'] in selected and x['score_components']['field_lexical']>=.5][:5]
        public_context=[dict(object_id=k,display_name=self.objects[k]['display_name'],physical_name=self.objects[k]['physical_name'],object_type=self.objects[k]['object_type']) for k in sorted(visible)]
        encoded=json.dumps(public_context,ensure_ascii=False,separators=(',',':')).encode()
        return {'implementation_version':VERSION,'mode':mode,'domain_pack_ref':{'domain_pack_id':self.pack.domain_pack_id,'version':self.pack.version,'manifest_sha256':self.pack.manifest_sha256},
                'decision':decision,'reason_code':reason,'selected_table_ids':selected,'selected_field_ids':selected_fields,
                'table_candidates':ranked[:50],'field_candidates':field_rank[:50],
                'context':{'authorized_tables':len(table_ids),'authorized_columns':len(column_ids),'utf8_bytes':len(encoded),'characters':len(encoded.decode()),'token_count':None,'token_count_reason':'no_model_tokenizer_or_model_call'},
                'authorized_catalog_sha256':hashlib.sha256(json.dumps(sorted(visible),separators=(',',':')).encode()).hexdigest(),
                'latency_ms':round((time.perf_counter()-started)*1000,3),'model_calls':0,'sql_execution_authorized':False}
