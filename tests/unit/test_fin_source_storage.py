import concurrent.futures
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from src.backend.data_sources.storage import SourceStorage
from src.backend.product.secret_store import SecretStoreError


class SourceStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'secrets.sqlite3'
        self.store=SourceStorage(self.path,master_key=b'T'*32)
        self.store.change('source.a',0,'create-a',{'state':'draft'})
        self.store.change('source.b',0,'create-b',{'state':'draft'})

    def test_idempotency_before_revision_and_conflicting_reuse(self):
        result=self.store.change('source.a',1,'edit-a',{'state':'disabled'})
        self.assertEqual(result,self.store.change('source.a',1,'edit-a',{'state':'disabled'}))
        with self.assertRaises(SecretStoreError):
            self.store.change('source.a',2,'edit-a',{'state':'draft'})
        self.assertEqual(2,len(self.store.history('source.a')))

    def test_two_instances_cas_has_exactly_one_winner(self):
        other=SourceStorage(self.path,master_key=b'T'*32)
        def write(n):
            try:
                (self.store if n==0 else other).change('source.a',1,'race'+str(n),{'state':'disabled'})
                return True
            except SecretStoreError:return False
        with concurrent.futures.ThreadPoolExecutor() as pool:
            self.assertEqual(1,sum(pool.map(write,range(2))))

    def test_ciphertext_scope_rotation_and_revocation(self):
        ref=self.store.put_credential('source.a','connection',b'fake-password')
        self.assertEqual(b'fake-password',self.store.read_credential(ref,'source.a','connection'))
        for source,purpose in [('source.b','connection'),('source.a','other')]:
            with self.assertRaises(SecretStoreError):self.store.read_credential(ref,source,purpose)
        rotated=self.store.put_credential('source.a','connection',b'new-fake-password',previous=ref)
        with self.assertRaises(SecretStoreError):self.store.read_credential(ref,'source.a','connection')
        self.store.revoke_credential(rotated,'source.a','connection')
        with self.assertRaises(SecretStoreError):self.store.read_credential(rotated,'source.a','connection')
        self.assertNotIn(b'fake-password',self.path.read_bytes())

    def test_tampering_and_wrong_key_fail_without_content(self):
        ref=self.store.put_credential('source.a','connection',b'fake-password')
        with closing(sqlite3.connect(self.path)) as db,db:
            db.execute('UPDATE source_credentials SET source_id=? WHERE ref=?',('source.b',ref))
        for store in (self.store,SourceStorage(self.path,master_key=b'X'*32)):
            with self.assertRaises(SecretStoreError) as error:store.read_credential(ref,'source.b','connection')
            self.assertNotIn('fake-password',str(error.exception))

    def test_soft_delete_blocks_refs_preserves_history_and_does_not_recreate(self):
        ref=self.store.put_credential('source.a','connection',b'fake-password')
        self.store.change('source.a',1,'delete-a',{'state':'deleted'},deleted=True)
        with self.assertRaises(SecretStoreError):self.store.read_credential(ref,'source.a','connection')
        with self.assertRaises(SecretStoreError):self.store.change('source.a',0,'recreate',{'state':'draft'})
        self.assertEqual(2,len(self.store.history('source.a')))

    def test_audit_is_content_free_and_failure_is_atomic(self):
        def fail(_):raise RuntimeError('test rollback')
        with self.assertRaises(RuntimeError):
            self.store.change('source.a',1,'fail',{'state':'disabled'},validate=fail)
        self.assertEqual(1,self.store.get('source.a')['revision'])
        with closing(sqlite3.connect(self.path)) as db,db:
            rows=db.execute('SELECT event,source_hash,revision FROM source_audit').fetchall()
        self.assertNotIn('source.a',json.dumps(rows))

if __name__=='__main__':unittest.main()
