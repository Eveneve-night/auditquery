"""Versioned encrypted model configuration, separate from data-source credentials."""
import json
import os
from dataclasses import replace
from ..llm.openai_compatible import ServiceConfig, OpenAICompatibleProvider, invalid
from ..llm.credentials import _validate_key
from ..llm.contracts import ModelCallRequest, ModelMessage
from ..llm.usage import UsageLedger
from .secret_store import SecretStoreError


class ModelSettings:
    AAD=b'auditquery:model-settings:1.0.0'

    def __init__(self,store):
        self.store=store
        with store._lock, store._connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS model_settings (id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL, nonce BLOB NOT NULL, ciphertext BLOB NOT NULL)')

    def _read(self,db):
        row=db.execute('SELECT * FROM model_settings WHERE id=1').fetchone()
        if row:
            try:
                data=json.loads(self.store._cipher().decrypt(row['nonce'],row['ciphertext'],self.AAD+str(row['revision']).encode()).decode())
                return row['revision'],data
            except Exception: raise SecretStoreError('service_unavailable') from None
        # Read-only migration of legacy ciphertext with its original AAD.
        legacy=db.execute('SELECT * FROM provider_secrets WHERE provider=?',('bailian_beijing',)).fetchone()
        key=self.store._decrypt_row(legacy) if legacy and legacy['configured'] else ''
        config=dict(version='1.0.0',protocol='openai-chat-completions',provider_id='bailian_beijing',
                    model='qwen3.7-flash',base_url='https://dashscope.aliyuncs.com/compatible-mode/v1',
                    local=False,auth_mode='bearer',input_per_million_tokens=0.2,output_per_million_tokens=0.8)
        return 0,dict(config=config,key=key,enabled=bool(key))

    def snapshot(self):
        with self.store._lock,self.store._connect() as db:
            revision,data=self._read(db)
        config=ServiceConfig.from_dict(data['config'])
        return revision,config,data['key'],data['enabled']

    def status(self):
        revision,config,key,enabled=self.snapshot()
        return dict(version='1.0.0',revision=revision,config=config.public(),configured=bool(enabled),
                    credential_configured=bool(key),pricing_status='unknown' if None in
                    (config.input_per_million_tokens,config.output_per_million_tokens) else 'configured')

    def save(self,config,key,expected_revision,*,audit=None,enabled=True):
        config=ServiceConfig.from_dict(config)
        if not isinstance(key,str): raise invalid()
        if enabled and config.auth_mode=='bearer': _validate_key(key)
        elif key: raise invalid()
        if isinstance(expected_revision,bool) or not isinstance(expected_revision,int) or expected_revision<0: raise invalid()
        with self.store._lock,self.store._connect() as db:
            if audit: audit.attach(db,self.store.path)
            db.execute('BEGIN IMMEDIATE')
            revision,_=self._read(db)
            if revision!=expected_revision: raise SecretStoreError('state_conflict')
            revision+=1; nonce=os.urandom(12)
            payload=json.dumps(dict(config=config.public(),key=key,enabled=enabled),sort_keys=True).encode()
            ciphertext=self.store._cipher().encrypt(nonce,payload,self.AAD+str(revision).encode())
            db.execute('INSERT OR REPLACE INTO model_settings VALUES (1,?,?,?)',(revision,nonce,ciphertext))
            # Commit the migrated settings and invalidate the old model-key slot
            # atomically. Database/source credentials live in different tables.
            db.execute("UPDATE provider_secrets SET configured=0, revision=revision+1, nonce=NULL, ciphertext=NULL, last_four=NULL WHERE provider='bailian_beijing' AND configured=1")
            self.store._after_mutation(db)
            if audit: audit.append(db,event_type='secret_changed',outcome='succeeded',resource_revision=revision)
            db.commit()
        return self.status()

    def revoke(self,expected_revision,*,audit=None):
        _,config,_,_=self.snapshot()
        # Tombstone remains so an old legacy key cannot be resurrected.
        return self.save(config.public(),'',expected_revision,audit=audit,enabled=False)


class ConfiguredProvider:
    def __init__(self,settings,ledger,*,transport_factory=None):
        self.settings=settings; self.usage_ledger=ledger; self.transport_factory=transport_factory

    @property
    def provider_id(self): return self.settings.snapshot()[1].provider_id

    @property
    def model(self): return self.settings.snapshot()[1].model

    def accepts_result(self,result):
        revision,config,_,enabled=self.settings.snapshot()
        return (enabled and result.configuration_revision==revision and result.provider_id==config.provider_id
                and result.requested_model==config.model and result.returned_model==config.model)

    def generate_structured(self,request):
        revision,config,key,enabled=self.settings.snapshot()
        if not enabled: raise invalid()
        provider=OpenAICompatibleProvider(config,key,usage_ledger=self.usage_ledger,
            transport=self.transport_factory(config) if self.transport_factory else None)
        return replace(provider.generate_structured(request),configuration_revision=revision)


def probe_settings(settings):
    from pathlib import Path
    from ..agent.loop_guard import LoopGuard, LoopPolicy
    ledger=UsageLedger()
    provider=ConfiguredProvider(settings,ledger)
    policy=LoopPolicy.from_file(Path(__file__).resolve().parents[3]/'configs/policies/agent-loop-limits.v1.json')
    guard=LoopGuard(policy)
    if not guard.record('model_call',action='connectivity').allowed: raise invalid()
    try:
        result=provider.generate_structured(ModelCallRequest('connectivity','connectivity',
            (ModelMessage('user','Return a JSON object with ok true.'),),'1','connectivity.v1',32))
    finally:
        summary=ledger.summary()
        decision=guard.record_usage(input_tokens=summary['input_tokens'],output_tokens=summary['output_tokens'],
                                    estimated_cost_cny=summary['estimated_cost_cny'])
    if not decision.allowed: raise invalid()
    summary=ledger.summary()
    return dict(version='1.0.0',provider_id=result.provider_id,model=result.requested_model,
                configuration_revision=result.configuration_revision,
                **{k:summary[k] for k in ('calls','input_tokens','output_tokens','estimated_cost_cny')})
