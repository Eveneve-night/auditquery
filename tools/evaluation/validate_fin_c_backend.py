"""C independent in-process review, real B modules and A validator; no HTTP."""
import argparse,hashlib,json,sys,tempfile
from pathlib import Path
CONTRACT_FILES=(
    'configs/schemas/data-sources/common.v1.schema.json',
    'configs/schemas/data-sources/connector.v1.schema.json',
    'configs/schemas/data-sources/snapshot.v1.schema.json',
    'configs/schemas/data-sources/source.v1.schema.json',
    'src/backend/data_sources/contracts.py',
)
def run(backend_root,contract_root):
    sys.path.insert(0,str(backend_root))
    from src.backend.data_sources import __path__ as package_path
    package_path.append(str(contract_root/'src/backend/data_sources'))
    from src.backend.data_sources import contracts as a
    from src.backend.data_sources.registry import DataSourceRegistry
    from src.backend.data_sources.storage import SourceStorage
    from src.backend.product.auth import AuthService,SqliteAuthStore,AuthError
    from src.backend.product.secret_store import SecretStoreError
    checks=[]
    def expect_error(name,call):
        try:call()
        except (SecretStoreError,AuthError,a.DataSourceContractError) as e:
            assert 'c-test-only-secret' not in str(e);checks.append({'name':name,'rejected':True,'code':str(e)})
        else:raise AssertionError(name)
    with tempfile.TemporaryDirectory() as temporary:
        root=Path(temporary);auth=AuthService(SqliteAuthStore(root/'auth.sqlite3'),audit_key=b'C'*32)
        grant=auth.create_bootstrap_grant();session=auth.bootstrap_admin(grant.token,'admin','synthetic review password',request_id='bootstrap')
        store=SourceStorage(root/'metadata.sqlite3',master_key=b'D'*32)
        registry=DataSourceRegistry(store,auth,allow_source=lambda actor,source:source=='source.orders',asset_ids=['asset.orders'])
        record={'contract_version':'1.0.0','source_id':'source.orders','revision':1,'status':'draft','connector':{'id':'sqlite','version':'1.0.0'},'sqlite_asset_id':'asset.orders','credential_ref':None,'binding':None}
        args={'token':session.token,'csrf':session.csrf_token,'request_id':'create'}
        expect_error('missing_auth',lambda:registry.create(record,**{**args,'token':None}))
        expect_error('wrong_csrf',lambda:registry.create(record,**{**args,'csrf':'incorrect'}))
        expect_error('unauthorized_source',lambda:registry.get('source.hidden',token=session.token,request_id='hidden'))
        expect_error('unknown_connector',lambda:registry.create({**record,'connector':{'id':'module.any','version':'1.0.0'}},**args))
        expect_error('uncontrolled_asset',lambda:registry.create({**record,'sqlite_asset_id':'asset.unknown'},**args))
        first=registry.create(record,**args);a.validate_source(first)
        assert registry.create(record,**args)==first and len(store.history('source.orders'))==1
        checks.append({'name':'create_idempotency_and_real_A_schema','passed':True})
        for target in ('verified','synced','published'):
            expect_error('zero_capability_no_fake_'+target,lambda:registry.transition('source.orders',target,expected_revision=1,**{**args,'request_id':target}))
        assert store.get('source.orders')['status']=='draft'
        descriptor=registry.connectors.describe(record['connector']);a.validate_connector(descriptor)
        assert descriptor['capabilities']['readonly']=='unsupported'
        secret=b'c-test-only-secret'
        saved=registry.set_credential('source.orders',secret,previous=None,expected_revision=1,**{**args,'request_id':'credential'})
        a.validate_source(saved);ref=saved['credential_ref']['secret_id']
        assert secret.decode() not in json.dumps(saved)
        assert store.read_credential(ref,'source.orders','connection')==secret
        expect_error('credential_cross_source',lambda:store.read_credential(ref,'source.hidden','connection'))
        expect_error('credential_wrong_purpose',lambda:store.read_credential(ref,'source.orders','other'))
        expect_error('stale_revision',lambda:registry.set_credential('source.orders',secret,previous=ref,expected_revision=1,**{**args,'request_id':'stale'}))
        changed=registry.set_credential('source.orders',b'replacement fake',previous=ref,expected_revision=2,**{**args,'request_id':'rotate'})
        expect_error('old_reference_after_rotation',lambda:store.read_credential(ref,'source.orders','connection'))
        current_ref=changed['credential_ref']['secret_id']
        deleted=registry.transition('source.orders','deleted',expected_revision=3,**{**args,'request_id':'delete'})
        a.validate_source(deleted)
        expect_error('credential_after_delete',lambda:store.read_credential(current_ref,'source.orders','connection'))
        assert registry.set_credential('source.orders',secret,previous=None,expected_revision=1,**{**args,'request_id':'credential'})==saved
        expect_error('historical_replay_cannot_restore_secret',lambda:store.read_credential(ref,'source.orders','connection'))
        assert secret.decode() not in json.dumps(store.history('source.orders'))
        checks.append({'name':'history_redaction_and_no_false_success','passed':True})
    return {'scope':'C_independent_B_production_in_process_with_A_validator','checks':checks,'HTTP':False,
            'connector_execution_implemented':False,'model_calls':0,'real_key_reads':0,'cost_cny':0,
            'B_sources':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (backend_root/'src/backend/data_sources').glob('*.py')},
            'source_hash_normalization':{'A_sources':'utf8_lf','B_sources':'raw_bytes'},
            'A_sources':{name:hashlib.sha256((contract_root/name).read_text(encoding='utf-8').replace('\r\n','\n').encode('utf-8')).hexdigest() for name in CONTRACT_FILES}}
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--backend-root',type=Path,required=True);p.add_argument('--contract-root',type=Path,required=True);p.add_argument('--report',type=Path,required=True);x=p.parse_args()
    r=run(x.backend_root,x.contract_root);x.report.write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n',encoding='utf-8');print(json.dumps({'checks':len(r['checks'])}))
