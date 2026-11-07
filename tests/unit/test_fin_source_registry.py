from pathlib import Path
import os
import tempfile
import unittest
from copy import deepcopy
from src.backend.data_sources import __path__ as package_path

# Isolated integration only. Production registry has no external path loader.
dependency=Path(os.environ.get('FIN_CONTRACT_ROOT',Path(__file__).resolve().parents[2]))
package_path.append(str(dependency/'src/backend/data_sources'))
from src.backend.data_sources.registry import DataSourceRegistry
from src.backend.data_sources.storage import SourceStorage
from src.backend.product.auth import AuthService,SqliteAuthStore,AuthError
from src.backend.product.secret_store import SecretStoreError


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name)
        self.auth=AuthService(SqliteAuthStore(root/'auth.db'),audit_key=b'A'*32)
        grant=self.auth.create_bootstrap_grant()
        self.login=self.auth.bootstrap_admin(grant.token,'admin','test only strong password',request_id='bootstrap')
        self.store=SourceStorage(root/'secrets.db',master_key=b'T'*32)
        self.registry=DataSourceRegistry(self.store,self.auth,allow_source=lambda actor,source:source=='source.a',asset_ids=['asset.test'])
        self.record={'contract_version':'1.0.0','source_id':'source.a','revision':1,'status':'draft',
                     'connector':{'id':'sqlite','version':'1.0.0'},'sqlite_asset_id':'asset.test','credential_ref':None,'binding':None}
        self.args={'token':self.login.token,'csrf':self.login.csrf_token,'request_id':'create'}

    def test_create_replay_conflict_and_path_module_rejection(self):
        result=self.registry.create(self.record,**self.args)
        self.assertEqual(result,self.registry.create(self.record,**self.args))
        for change in ({'sqlite_asset_id':'../escape'},{'connector':{'id':'os.system','version':'1.0.0'}},{'status':'verified'},{'password':'fake'}):
            with self.assertRaises(ValueError if 'password' in change or 'connector' in change or 'sqlite_asset_id' in change else SecretStoreError):
                self.registry.create({**self.record,**change},**self.args)

    def test_auth_csrf_and_source_acl_precede_storage(self):
        for args in ({**self.args,'token':None},{**self.args,'csrf':'wrong'}):
            with self.assertRaises(AuthError):self.registry.create(self.record,**args)
        with self.assertRaises(SecretStoreError):self.registry.create({**self.record,'source_id':'source.b'},**self.args)
        with self.assertRaises(SecretStoreError):self.store.get('source.a')

    def test_no_adapter_never_becomes_verified_synced_or_published(self):
        self.registry.create(self.record,**self.args)
        for target in ('verified','synced','published','draining','disabled'):
            with self.assertRaisesRegex(ValueError,'required_capability_unavailable'):self.registry.transition('source.a',target,expected_revision=1,**{**self.args,'request_id':target})
        self.assertEqual('draft',self.store.get('source.a')['status'])

    def test_atomic_credentials_rotation_revocation_and_replay(self):
        self.registry.create(self.record,**self.args)
        args={**self.args,'request_id':'credential'}
        result=self.registry.set_credential('source.a',b'fake-password',previous=None,expected_revision=1,**args)
        self.assertEqual(result,self.registry.set_credential('source.a',b'fake-password',previous=None,expected_revision=1,**args))
        ref=result['credential_ref']['secret_id']
        self.assertEqual(2,result['revision'])
        self.assertNotIn('fake-password',str(result))
        result=self.registry.revoke_credential('source.a',ref=ref,expected_revision=2,**{**args,'request_id':'revoke'})
        self.assertIsNone(result['credential_ref'])
        with self.assertRaises(SecretStoreError):self.store.read_credential(ref,'source.a','connection')

    def test_soft_delete_replay_history_and_stale_cas(self):
        self.registry.create(self.record,**self.args)
        args={**self.args,'request_id':'delete'}
        first=self.registry.transition('source.a','deleted',expected_revision=1,**args)
        self.assertEqual(first,self.registry.transition('source.a','deleted',expected_revision=1,**args))
        self.assertEqual(2,len(self.store.history('source.a')))

    def test_credential_replay_after_delete_cannot_restore_revoked_secret(self):
        self.registry.create(self.record,**self.args)
        args={**self.args,'request_id':'credential'}
        first=self.registry.set_credential('source.a',b'fake-password',previous=None,expected_revision=1,**args)
        self.registry.transition('source.a','deleted',expected_revision=2,**{**args,'request_id':'delete'})
        self.assertEqual(first,self.registry.set_credential('source.a',b'fake-password',previous=None,expected_revision=1,**args))
        with self.assertRaises(SecretStoreError):self.store.read_credential(first['credential_ref']['secret_id'],'source.a','connection')

if __name__=='__main__':unittest.main()
