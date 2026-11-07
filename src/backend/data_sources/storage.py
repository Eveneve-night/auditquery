"""Internal transactional persistence, not the public FIN-DS01 protocol.

Reuse the existing SecretStore connection/rollback and authenticated encryption
primitive. No key loading, network, path selection or credentials in exports.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from contextlib import contextmanager, nullcontext
import hmac
from cryptography.exceptions import InvalidTag
from ..product.secret_store import SqliteSecretStore, SecretStoreError


def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)


class SourceStorage(SqliteSecretStore):
    def _init_schema(self):
        super()._init_schema()
        with self._connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS sources (
                    source_id TEXT PRIMARY KEY, revision INTEGER NOT NULL,
                    deleted INTEGER NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS source_history (
                    source_id TEXT NOT NULL,revision INTEGER NOT NULL,payload TEXT NOT NULL,
                    PRIMARY KEY(source_id,revision));
                CREATE TABLE IF NOT EXISTS source_receipts (
                    source_id TEXT NOT NULL,request_id TEXT NOT NULL,fingerprint TEXT NOT NULL,
                    response TEXT NOT NULL,PRIMARY KEY(source_id,request_id));
                CREATE TABLE IF NOT EXISTS source_credentials (
                    ref TEXT PRIMARY KEY,source_id TEXT NOT NULL,purpose TEXT NOT NULL,
                    revision INTEGER NOT NULL,active INTEGER NOT NULL,nonce BLOB,ciphertext BLOB);
                CREATE TABLE IF NOT EXISTS source_audit (
                    event TEXT NOT NULL,source_hash TEXT NOT NULL,revision INTEGER NOT NULL);
            ''')

    @contextmanager
    def transaction(self):
        try:
            with self._lock,self._connect() as db:
                db.execute('BEGIN IMMEDIATE')
                yield db
                db.commit()
        except sqlite3.Error:
            raise SecretStoreError('service_unavailable') from None

    @staticmethod
    def _audit(db,event,source,revision):
        db.execute('INSERT INTO source_audit VALUES (?,?,?)',
                   (event,hashlib.sha256(source.encode()).hexdigest(),revision))

    def get(self,source):
        with self.transaction() as db:
            row=db.execute('SELECT * FROM sources WHERE source_id=?',(source,)).fetchone()
        if row is None:raise SecretStoreError('invalid_request')
        return json.loads(row['payload'])

    def history(self,source):
        with self.transaction() as db:
            return [json.loads(r['payload']) for r in db.execute(
                'SELECT payload FROM source_history WHERE source_id=? ORDER BY revision',(source,))]

    def change(self,source,expected,request_id,payload,*,deleted=False,validate=None,prepare=None,fingerprint_payload=None):
        """Trusted caller supplies validated metadata; no external request accepted here."""
        if type(expected) is not int or expected<0 or not isinstance(request_id,str) or not request_id:
            raise SecretStoreError('invalid_request')
        fingerprint=hashlib.sha256(canonical([expected,payload if fingerprint_payload is None else fingerprint_payload,deleted]).encode()).hexdigest()
        with self.transaction() as db:
            receipt=db.execute('SELECT * FROM source_receipts WHERE source_id=? AND request_id=?',
                               (source,request_id)).fetchone()
            if receipt:
                if receipt['fingerprint']!=fingerprint:raise SecretStoreError('state_conflict')
                return json.loads(receipt['response'])
            old=db.execute('SELECT * FROM sources WHERE source_id=?',(source,)).fetchone()
            if (old and (old['deleted'] or old['revision']!=expected)) or (not old and expected!=0):
                raise SecretStoreError('state_conflict')
            result={**payload,'source_id':source,'revision':expected+1}
            if prepare:result=prepare(db,result)
            if validate:validate(result)
            encoded=canonical(result)
            db.execute('INSERT INTO sources VALUES (?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET revision=excluded.revision,deleted=excluded.deleted,payload=excluded.payload',
                       (source,expected+1,int(deleted),encoded))
            db.execute('INSERT INTO source_history VALUES (?,?,?)',(source,expected+1,encoded))
            db.execute('INSERT INTO source_receipts VALUES (?,?,?,?)',(source,request_id,fingerprint,encoded))
            if deleted:db.execute('UPDATE source_credentials SET active=0,nonce=NULL,ciphertext=NULL WHERE source_id=?',(source,))
            self._audit(db,'source_changed',source,expected+1)
            return result

    @staticmethod
    def _live_source(db,source):
        row=db.execute('SELECT deleted FROM sources WHERE source_id=?',(source,)).fetchone()
        if row is None or row['deleted']:raise SecretStoreError('invalid_request')

    @staticmethod
    def _credential(db,ref,source,purpose):
        SourceStorage._live_source(db,source)
        row=db.execute('SELECT * FROM source_credentials WHERE ref=?',(ref,)).fetchone()
        if row is None or not row['active'] or row['source_id']!=source or row['purpose']!=purpose:
            raise SecretStoreError('secret_not_configured')
        return row

    @staticmethod
    def _scope(ref,source,purpose,revision):
        return canonical(['source-credential.v1',ref,source,purpose,revision]).encode()

    def credential_fingerprint(self,source,purpose,value):
        self._cipher()  # Missing master key fails closed, with no key loading.
        return hmac.new(self._master_key,self._scope('',source,purpose,0)+value,hashlib.sha256).hexdigest()

    def put_credential(self,source,purpose,value,*,previous=None,db=None):
        if purpose!='connection' or not isinstance(value,bytes) or not 1<=len(value)<=8192:
            raise SecretStoreError('invalid_request')
        with (self.transaction() if db is None else nullcontext(db)) as db:
            self._live_source(db,source)
            revision=db.execute('SELECT COALESCE(MAX(revision),0)+1 FROM source_credentials WHERE source_id=? AND purpose=?',(source,purpose)).fetchone()[0]
            if previous:
                row=self._credential(db,previous,source,purpose)
                db.execute('UPDATE source_credentials SET active=0,nonce=NULL,ciphertext=NULL WHERE ref=?',(previous,))
            elif db.execute('SELECT 1 FROM source_credentials WHERE source_id=? AND purpose=? AND active=1',(source,purpose)).fetchone():
                raise SecretStoreError('state_conflict')
            ref='credential.'+secrets.token_hex(16);nonce=secrets.token_bytes(12)
            encrypted=self._cipher().encrypt(nonce,value,self._scope(ref,source,purpose,revision))
            db.execute('INSERT INTO source_credentials VALUES (?,?,?,?,1,?,?)',(ref,source,purpose,revision,nonce,encrypted))
            self._audit(db,'credential_rotated',source,revision)
            return ref

    def read_credential(self,ref,source,purpose):
        # A trusted connector call only; never expose through admin/export APIs.
        with self.transaction() as db:
            row=self._credential(db,ref,source,purpose)
            try:return self._cipher().decrypt(row['nonce'],row['ciphertext'],self._scope(ref,source,purpose,row['revision']))
            except (InvalidTag,ValueError,TypeError):raise SecretStoreError('service_unavailable') from None

    def revoke_credential(self,ref,source,purpose):
        with self.transaction() as db:
            row=self._credential(db,ref,source,purpose)
            db.execute('UPDATE source_credentials SET active=0,nonce=NULL,ciphertext=NULL WHERE ref=?',(ref,))
            self._audit(db,'credential_revoked',source,row['revision'])
