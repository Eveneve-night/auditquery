"""Real readonly DB/pack mapping and reference checks, not NL2SQL accuracy."""
import argparse,hashlib,json,sqlite3,sys
from contextlib import closing
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path[:0]=[str(ROOT),str(ROOT/'src')]
from tools.data_assets.build_public_business import build
from backend.agent.domain_pack import DomainPackRegistry
def run(output):
    databases=build(output.resolve());mapping={};observed={}
    for domain,pack_id,version in [('orders','order_to_cash','1.1.0'),('maintenance','equipment_maintenance','1.0.0')]:
        pack=DomainPackRegistry(ROOT/'domain-packs').load(pack_id,version)
        tables={o['object_id']:o for o in pack.artifact_items('schema_catalog') if o['object_type']=='table'}
        with closing(sqlite3.connect((output.resolve()/(domain+'.sqlite3')).as_uri()+'?mode=ro',uri=True)) as db:
            for tid,table in tables.items():
                actual={r[1] for r in db.execute('PRAGMA table_info("'+table['physical_name']+'")')}
                declared={o['physical_name'] for o in pack.artifact_items('schema_catalog') if o['object_type']=='column' and o['parent_object_id']==tid}
                assert actual==declared,(tid,actual,declared)
            # Runtime probes are defined independently of the scoring file.
            if domain=='orders':observed['orders.total']=[list(r) for r in db.execute('SELECT SUM(order_amount) FROM sales_order')]
            else:observed['maintenance.open']=[list(r) for r in db.execute("SELECT COUNT(*) FROM work_order WHERE status='open'")]
        mapping[domain]={'domain_pack_id':pack_id,'version':version,'manifest_sha256':pack.manifest_sha256,
                         'tables':{tid:t['physical_name'] for tid,t in tables.items()}}
    # Labels enter only after all actual readonly probe results are available.
    scoring=json.loads((ROOT/'tests/fixtures/fin-c-public/v1/scoring.json').read_text(encoding='utf-8'))
    checked=[{'case_id':s['case_id'],'matched':observed[s['case_id']]==s['result']} for s in scoring if s['case_id'] in observed]
    assert all(c['matched'] for c in checked)
    return {'scope':'public_synthetic_DB_readonly_and_metadata_mapping_not_NL_accuracy','split':'prompt_dev',
            'databases':databases,'mapping':mapping,'reference_probes':checked,'ambiguous_and_unknown_NL_cases_executed':False,
            'origin':'original_project_synthetic','configuration_effort_hours':None,'migration_zero_code_proven':False,
            'source_hashes':{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted((ROOT/'data/public-business/v1').glob('*')) if p.is_file()},
            'model_calls':0,'key_reads':0,'cost_cny':0}
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);p.add_argument('--report',type=Path,required=True);a=p.parse_args()
    result=run(a.output);a.report.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8');print(json.dumps({'databases':len(result['databases']),'reference_probes':result['reference_probes']}))
