"""Map actual maintenance DDL to a new validated pack, not equipment_loan."""
import hashlib,json,sqlite3
from contextlib import closing
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
def build():
    target=ROOT/'domain-packs/equipment_maintenance/v1';target.mkdir(parents=True,exist_ok=True)
    source='source.equipment_maintenance_synthetic';base='schema.equipment_maintenance'
    def obj(oid,kind,parent,name,dtype=None,concepts=()):
        return dict(object_id=oid,object_type=kind,parent_object_id=parent,display_name=name,physical_name=name,
                    data_type=dtype,concept_refs=list(concepts),authorization_level='read_public',source_refs=[source])
    objects=[obj(base,'data_source',None,'equipment_maintenance')];concepts=[];aliases=[];relations=[]
    with closing(sqlite3.connect(':memory:')) as db:
        db.executescript((ROOT/'data/public-business/v1/maintenance.sql').read_text())
        for table in ('asset','work_order','maintenance_event'):
            cid='concept.maintenance.'+table;tid=base+'.'+table
            concepts.append(dict(concept_id=cid,display_name=table,description='Original synthetic maintenance object '+table,parent_concept_ids=[],source_refs=[source]))
            aliases.append(dict(alias_id='alias.maintenance.'+table,surface_form=table,language='en',concept_id=cid,source_ref=source,confidence_class='synthetic'))
            objects.append(obj(tid,'table',base,table,concepts=[cid]))
            for row in db.execute('PRAGMA table_info("'+table+'")'):
                objects.append(obj(tid+'.'+row[1],'column',tid,row[1],row[2],concepts=[cid]))
            for row in db.execute('PRAGMA foreign_key_list("'+table+'")'):
                relations.append(dict(relation_id='relation.maintenance.'+table+'.'+row[3],relation_type='foreign_key',source_object_id=tid+'.'+row[3],target_object_id=base+'.'+row[2]+'.'+row[4],direction='directed',cardinality='many_to_one',source_refs=[source]))
    artifacts={'concepts':concepts,'aliases':aliases,'slots':[],'schema_catalog':objects,'relations':relations,'value_evidence':[],'validation_rules':[]}
    names={'schema_catalog':'schema-catalog.json','value_evidence':'value-evidence.json','validation_rules':'validation-rules.json'};files=[]
    for role,items in artifacts.items():
        path=target/names.get(role,role+'.json');path.write_text(json.dumps(dict(schema_version='1.0.0',artifact_type=role,items=items),ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
        files.append(dict(role=role,path=path.name,sha256=hashlib.sha256(path.read_bytes()).hexdigest(),data_level='L0',send_to_model=True))
    notice='Original project synthetic equipment maintenance metadata, redistribution permitted. No equipment_loan rewrite, customer data, scoring answers or hidden generalization claim.\n'
    (target/'LICENSE-or-SOURCE.md').write_text(notice,encoding='utf-8',newline='\n')
    files.append(dict(role='license_notice',path='LICENSE-or-SOURCE.md',sha256=hashlib.sha256(notice.encode()).hexdigest(),data_level='L0',send_to_model=False))
    manifest=json.loads((ROOT/'domain-packs/order_to_cash/v1.1/manifest.json').read_text(encoding='utf-8'))
    manifest.update(domain_pack_id='equipment_maintenance',domain_id='equipment_maintenance',version='1.0.0',created_at='2026-11-01T00:00:00Z',files=files,
                    source_refs=[dict(source_id=source,uri='repo:domain-packs/equipment_maintenance/v1/LICENSE-or-SOURCE.md',license='MIT',data_level='L0',authorized=True)],
                    compatibility=dict(task_types=['nl2sql'],database_versions=['equipment-maintenance-synthetic.v1']))
    (target/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
if __name__=='__main__':build()
