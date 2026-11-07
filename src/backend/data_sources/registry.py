"""Authenticated in-process FIN registry; no HTTP or connector execution."""
from copy import deepcopy
import hashlib
from ..product.secret_store import SecretStoreError
from .connectors import ConnectorRegistry


class DataSourceRegistry:
    def __init__(self,storage,auth,*,allow_source,asset_ids=()):
        from . import contracts
        self.storage=storage;self.auth=auth;self.allow_source=allow_source
        self.contract=contracts;self.connectors=ConnectorRegistry()
        self.asset_ids=frozenset(asset_ids)

    def _authorize(self,source,token,csrf,request_id,write=False):
        if not isinstance(request_id,str) or not 1<=len(request_id)<=128:
            raise SecretStoreError('invalid_request')
        principal=self.auth.authorize_admin(token,csrf_token=csrf,require_csrf=write,request_id=request_id)
        if not self.allow_source(principal,source):
            raise SecretStoreError('forbidden')
        return principal

    @staticmethod
    def _operation(principal,request_id):
        return hashlib.sha256(principal.username.encode()).hexdigest()+':'+request_id

    def create(self,record,*,token,csrf,request_id):
        if not isinstance(record,dict):raise SecretStoreError('invalid_request')
        record=deepcopy(record)
        actor=self._authorize(record.get('source_id'),token,csrf,request_id,True)
        self.contract.validate_source(record)
        self.contract.validate_connector(self.connectors.describe(record['connector']))
        if record['sqlite_asset_id'] not in self.asset_ids or record['revision']!=1 or record['status']!='draft' or record['credential_ref'] is not None or record['binding'] is not None:
            raise SecretStoreError('invalid_request')
        return self.storage.change(record['source_id'],0,self._operation(actor,request_id),record,validate=self.contract.validate_source)

    def get(self,source,*,token,request_id):
        self._authorize(source,token,None,request_id)
        return self.storage.get(source)

    def transition(self,source,target,*,expected_revision,token,csrf,request_id):
        actor=self._authorize(source,token,csrf,request_id,True)
        current=self.storage.get(source)
        if target in ('verified','synced','published','draining','disabled'):
            # No verification, schema sync or inflight manager exists yet.
            self.connectors.require(current['connector'],target)
        result={**current,'status':target}
        def validate(value):
            # Called under the same BEGIN IMMEDIATE as CAS/history/audit.
            before={**current,'revision':expected_revision}
            self.contract.validate_transition(before,target,expected_revision=expected_revision,
                                               actor_role='admin',operation_id=request_id)
            self.contract.validate_source(value)
        return self.storage.change(source,expected_revision,self._operation(actor,request_id),result,
                                   deleted=target=='deleted',validate=validate,fingerprint_payload={'target':target})

    def set_credential(self,source,value,*,previous,expected_revision,token,csrf,request_id):
        actor=self._authorize(source,token,csrf,request_id,True)
        current=self.storage.get(source)
        if not isinstance(value,bytes):raise SecretStoreError('invalid_request')
        digest=self.storage.credential_fingerprint(source,'connection',value)
        def prepare(db,result):
            if current['status'] not in ('draft','disabled'):raise SecretStoreError('state_conflict')
            ref=self.storage.put_credential(source,'connection',value,previous=previous,db=db)
            revision=db.execute('SELECT revision FROM source_credentials WHERE ref=?',(ref,)).fetchone()[0]
            result['credential_ref']={'source_id':source,'secret_id':ref,'purpose':'data_source','revision':revision}
            return result
        return self.storage.change(source,expected_revision,self._operation(actor,request_id),current,prepare=prepare,
                                   validate=self.contract.validate_source,
                                   fingerprint_payload={'credential_hmac':digest,'previous':previous})

    def revoke_credential(self,source,*,ref,expected_revision,token,csrf,request_id):
        actor=self._authorize(source,token,csrf,request_id,True)
        current=self.storage.get(source)
        def prepare(db,result):
            if current['status'] not in ('draft','disabled'):raise SecretStoreError('state_conflict')
            row=self.storage._credential(db,ref,source,'connection')
            db.execute('UPDATE source_credentials SET active=0,nonce=NULL,ciphertext=NULL WHERE ref=?',(ref,))
            self.storage._audit(db,'credential_revoked',source,row['revision'])
            result['credential_ref']=None
            return result
        return self.storage.change(source,expected_revision,self._operation(actor,request_id),current,
                                   prepare=prepare,validate=self.contract.validate_source,
                                   fingerprint_payload={'revoke':ref})
