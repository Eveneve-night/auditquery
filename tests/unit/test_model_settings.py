import json
from pathlib import Path
import sys
import tempfile
import unittest
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'src'))
from backend.llm.openai_compatible import ServiceConfig, OpenAICompatibleProvider, PinnedTransport
from backend.llm.credentials import CredentialError, _validate_key
from backend.llm.bailian import HttpResponse
from backend.llm.contracts import ModelCallRequest, ModelMessage
from backend.llm.errors import ProviderError
from backend.llm.usage import UsageLedger
from backend.product.model_settings import ModelSettings, ConfiguredProvider
from backend.product.secret_store import SqliteSecretStore, SecretStoreError
from backend.product.server import ProductRuntime


def config(**kw):
    return dict(dict(version='1.0.0',protocol='openai-chat-completions',provider_id='other-provider',
        model='model-two',base_url='https://models.example/v1',local=False,auth_mode='bearer',
        input_per_million_tokens=1.0,output_per_million_tokens=2.0),**kw)


class FakeTransport:
    def __init__(self,model): self.model=model; self.calls=[]
    def send(self,**kw):
        self.calls.append(kw)
        return HttpResponse(200,{},json.dumps(dict(model=self.model,usage=dict(prompt_tokens=10,
            completion_tokens=5,total_tokens=15),choices=[dict(message=dict(content='{"ok":true}'))])).encode())


REQUEST=ModelCallRequest('req.public','unit',(ModelMessage('user','Synthetic public example'),),'1','unit',32)


class ModelSettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.path=Path(self.tmp.name)/'secrets.db'
        self.store=SqliteSecretStore(self.path,master_key=b'x'*32); self.settings=ModelSettings(self.store)

    def tearDown(self): self.tmp.cleanup()

    def test_two_configs_use_exact_model_endpoint_and_opaque_key(self):
        for c,key in [(config(),'token-without-vendor-prefix'),(config(provider_id='local',model='local-model',base_url='http://127.0.0.1:8001/v1',local=True),'opaque+/=')]:
            revision=self.settings.status()['revision']; self.settings.save(c,key,revision)
            fake=FakeTransport(c['model']); ledger=UsageLedger()
            provider=ConfiguredProvider(self.settings,ledger,transport_factory=lambda _:fake)
            result=provider.generate_structured(REQUEST)
            self.assertEqual(result.requested_model,c['model']); self.assertEqual(result.provider_id,c['provider_id'])
            self.assertEqual(fake.calls[0]['url'],c['base_url']+'/chat/completions')
            self.assertEqual(fake.calls[0]['headers']['Authorization'],'Bearer '+key)
            self.assertNotIn('enable_thinking',json.loads(fake.calls[0]['body']))
            self.assertAlmostEqual(result.estimated_cost_cny,0.00002)
            self.assertNotIn(key,json.dumps(self.settings.status()))

    def test_restart_rotation_revoke_no_legacy_resurrection(self):
        self.store.put_if_absent('sk-testonly-legacy-credential-1111')
        self.assertEqual(self.settings.snapshot()[2],'sk-testonly-legacy-credential-1111')
        with patch.object(self.store,'_after_mutation',side_effect=RuntimeError('synthetic-migration-failure')):
            with self.assertRaises(RuntimeError): self.settings.save(config(),'new-opaque-key',0)
        self.assertEqual(self.settings.status()['revision'],0)
        self.assertEqual(self.store.get_for_provider_call(),'sk-testonly-legacy-credential-1111')
        self.settings.save(config(),'new-opaque-key',0)
        other=ModelSettings(SqliteSecretStore(self.path,master_key=b'x'*32))
        self.assertEqual(other.snapshot()[2],'new-opaque-key')
        other.save(config(model='replacement'),'rotated',1)
        self.assertEqual(self.settings.snapshot()[2],'rotated')
        other.revoke(2)
        with self.assertRaises(ProviderError): ConfiguredProvider(other,UsageLedger()).generate_structured(REQUEST)
        self.assertFalse(other.status()['configured'])
        self.assertNotIn(b'new-opaque-key',self.path.read_bytes())

    def test_aad_tamper_and_fault_rollback(self):
        self.settings.save(config(),'opaque',0)
        with patch.object(self.store,'_after_mutation',side_effect=RuntimeError('synthetic')):
            with self.assertRaises(RuntimeError): self.settings.save(config(),'replacement',1)
        self.assertEqual(self.settings.snapshot()[2],'opaque')
        with self.store._connect() as db: db.execute('UPDATE model_settings SET revision=9')
        with self.assertRaises(SecretStoreError): self.settings.status()

    def test_stale_revision_fails(self):
        self.settings.save(config(),'opaque',0)
        with self.assertRaises(SecretStoreError): self.settings.save(config(),'changed',0)
        self.assertEqual(self.settings.snapshot()[2],'opaque')

    def test_unsafe_keys_rejected(self):
        for key in ('','a\nb','a\rb','a\x00b','has space','a'*4097,'é'):
            with self.subTest(key=repr(key)):
                with self.assertRaises(CredentialError): _validate_key(key)
        self.assertEqual(_validate_key('opaque+/='),'opaque+/=')

    def test_ssrf_and_unknown_fields_rejected(self):
        for changes in [dict(base_url='file:///etc/passwd'),dict(base_url='http://example.com/v1'),
            dict(base_url='https://user:pass@example.com/v1'),dict(base_url='https://169.254.169.254/v1'),
            dict(base_url='https://127.0.0.1/v1'),dict(base_url='https://example.com/v1?key=secret'),
            dict(base_url='https://224.0.0.1/v1'),
            dict(base_url='https://@example.com/v1'),dict(base_url='https://example.com/v1#'),
            dict(base_url='https://example.com/v1#frag'),dict(auth_mode='none'),dict(protocol='anthropic'),
            dict(local=True,base_url='http://192.168.1.1/v1'),dict(gold='answer')]:
            with self.subTest(changes=changes):
                with self.assertRaises(ProviderError): ServiceConfig.from_dict(config(**changes))

    def test_dns_private_address_fails_before_connection(self):
        c=ServiceConfig.from_dict(config())
        with patch('socket.getaddrinfo',return_value=[(2,1,6,'',('10.0.0.1',443))]),patch('socket.create_connection') as dial:
            with self.assertRaises(ProviderError): PinnedTransport(c).send(url=c.endpoint,headers={},body=b'',timeout_seconds=1,max_response_bytes=100)
            dial.assert_not_called()

    def test_unknown_price_does_not_send_or_claim_free(self):
        c=config(input_per_million_tokens=None); self.settings.save(c,'opaque',0)
        fake=FakeTransport(c['model'])
        with self.assertRaises(ProviderError):
            ConfiguredProvider(self.settings,UsageLedger(),transport_factory=lambda _:fake).generate_structured(REQUEST)
        self.assertEqual(fake.calls,[]); self.assertEqual(self.settings.status()['pricing_status'],'unknown')

    def test_explicit_local_no_auth(self):
        c=ServiceConfig.from_dict(config(local=True,base_url='http://localhost:8080/v1',auth_mode='none'))
        f=FakeTransport(c.model); OpenAICompatibleProvider(c,'',transport=f).generate_structured(REQUEST)
        self.assertNotIn('Authorization',f.calls[0]['headers'])

    def test_admin_auth_csrf_save_and_no_echo(self):
        runtime=ProductRuntime(self.tmp.name+'/instance',master_key=b'z'*32)
        path='/api/v3/admin/model-settings'
        self.assertEqual(runtime.dispatch('GET',path).status,401)
        bootstrap=json.loads((Path(__file__).resolve().parents[2]/'configs/contracts/d5/admin-bootstrap.v2.json').read_text(encoding='utf-8'))
        login=runtime.dispatch('POST','/api/v3/auth/admin/login',body=json.dumps(dict(api_version='3.0',contract_version='2.0.0',request_id='login',username='admin',password=bootstrap['initial_credentials']['initial_password'])).encode())
        cookie=login.headers['Set-Cookie'].split(';')[0]; data=json.loads(login.body)
        payload=json.dumps(dict(config=config(),api_key='opaque+/=',expected_revision=0)).encode()
        self.assertEqual(runtime.dispatch('PUT',path,body=payload,cookie_header=cookie).status,403)
        response=runtime.dispatch('PUT',path,body=payload,cookie_header=cookie,csrf=data['csrf_token'])
        self.assertEqual(response.status,200,response.body)
        self.assertNotIn(b'opaque',response.body)
        self.assertEqual(json.loads(runtime.dispatch('GET',path,cookie_header=cookie).body)['config']['model'],'model-two')
        with patch('backend.product.model_settings.probe_settings') as probe:
            self.assertEqual(runtime.dispatch('POST',path+'/test',body=b'{"explicit_confirmation":true}',cookie_header=cookie,csrf=data['csrf_token']).status,409)
            probe.assert_not_called()

    def test_real_loopback_http_switch_and_redirect_no_forward(self):
        seen=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_POST(self):
                payload=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                seen.append((self.path,self.headers.get('Authorization'),payload['model']))
                if self.path.startswith('/redirect'):
                    self.send_response(302); self.send_header('Location','https://forbidden.example/steal'); self.end_headers(); return
                data=json.dumps(dict(model=payload['model'],usage=dict(prompt_tokens=1,completion_tokens=1,total_tokens=2),
                    choices=[dict(message=dict(content='{"ok":true}'))])).encode()
                self.send_response(200); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data)
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        try:
            for index,model in enumerate(('first-model','second-model')):
                self.settings.save(config(provider_id='service'+str(index),model=model,local=True,
                    base_url=f'http://127.0.0.1:{server.server_port}/v1'),'opaque-'+str(index),index)
                result=ConfiguredProvider(self.settings,UsageLedger()).generate_structured(REQUEST)
                self.assertEqual(result.returned_model,model)
            c=ServiceConfig.from_dict(config(local=True,base_url=f'http://127.0.0.1:{server.server_port}/redirect'))
            with self.assertRaises(ProviderError): OpenAICompatibleProvider(c,'opaque').generate_structured(REQUEST)
            self.assertEqual(len(seen),3)
            self.assertEqual([v[2] for v in seen[:2]],['first-model','second-model'])
            self.assertEqual([v[1] for v in seen[:2]],['Bearer opaque-0','Bearer opaque-1'])
        finally: server.shutdown(); server.server_close(); thread.join()

    def test_sql_assembly_uses_configured_model_and_existing_safety_gate(self):
        from backend.product.visitor_sql import build_approved_sql_adapter
        self.settings.save(config(),'opaque',0)
        output=dict(schema_version='1.0.0',task_type='nl2sql',status='success',
            data=dict(outcome='sql',sql='SELECT COUNT(*) FROM Artist',tables=['Artist'],params=[],rationale='Synthetic count proposal'),error=None,warnings=[])
        def send(_transport,**kw):
            self.assertEqual(kw['url'],'https://models.example/v1/chat/completions')
            self.assertEqual(json.loads(kw['body'])['model'],'model-two')
            return HttpResponse(200,{},json.dumps(dict(model='model-two',usage=dict(prompt_tokens=10,completion_tokens=5,total_tokens=15),
                choices=[dict(message=dict(content=json.dumps(output)))])).encode())
        with patch.object(PinnedTransport,'send',send):
            adapter=build_approved_sql_adapter(self.store)
            response=adapter.service.submit(dict(schema_version='2.0.0',operation='submit_query',session_id='session.config-test',
                request_id='request.config-test',turn_id=0,state_revision=0,query='统计艺术家数量'))
        self.assertEqual(response['status'],'success',response)
        self.assertEqual(response['sql']['safety_status'],'approved')
        self.assertEqual(response['sql']['rows'],[[275]])
        self.assertEqual(response['sql']['execution_attempts'],1)
        self.assertEqual(adapter.usage_ledger.records()[0].requested_model,'model-two')

    def test_remote_error_body_and_ids_cannot_leak(self):
        c=ServiceConfig.from_dict(config()); fake=FakeTransport(c.model)
        fake.send=lambda **kw: HttpResponse(401,{'x-request-id':'opaque'},b'{"message":"opaque and confidential SQL","request_id":"opaque"}')
        with self.assertRaises(ProviderError) as caught:
            OpenAICompatibleProvider(c,'opaque',transport=fake).generate_structured(REQUEST)
        self.assertEqual(caught.exception.code.value,'authentication_error')
        self.assertNotIn('opaque',str(caught.exception)); self.assertNotIn('SQL',str(caught.exception))

    def test_reflected_key_in_success_is_rejected_but_billable_usage_kept(self):
        c=ServiceConfig.from_dict(config()); ledger=UsageLedger()
        class Reflection(FakeTransport):
            def send(self,**kw):
                response=super().send(**kw)
                data=json.loads(response.body); data['choices'][0]['message']['content']='{"answer":"opaque"}'
                return HttpResponse(200,{},json.dumps(data).encode())
        with self.assertRaises(ProviderError) as caught:
            OpenAICompatibleProvider(c,'opaque',transport=Reflection(c.model),usage_ledger=ledger).generate_structured(REQUEST)
        self.assertNotIn('opaque',str(caught.exception))
        self.assertEqual(ledger.summary()['total_tokens'],15)
        self.assertGreater(ledger.summary()['estimated_cost_cny'],0)

    def test_machine_schema_and_runtime_mutations(self):
        from jsonschema import Draft202012Validator
        schema=json.loads((Path(__file__).resolve().parents[2]/'configs/schemas/model-services/service.v1.schema.json').read_text(encoding='utf-8'))
        Draft202012Validator.check_schema(schema)
        validator=Draft202012Validator(schema); validator.validate(config())
        self.assertTrue(list(validator.iter_errors(config(gold='leak'))))
        self.assertTrue(list(validator.iter_errors(config(auth_mode='none'))))

    def test_revision_change_invalidates_inflight_result_even_same_model(self):
        self.settings.save(config(),'opaque',0)
        fake=FakeTransport('model-two'); provider=ConfiguredProvider(self.settings,UsageLedger(),transport_factory=lambda _:fake)
        result=provider.generate_structured(REQUEST)
        self.assertTrue(provider.accepts_result(result))
        self.settings.save(config(base_url='https://replacement.example/v1'),'new-opaque',1)
        self.assertFalse(provider.accepts_result(result))

    def test_backup_restore_keeps_encrypted_settings_not_master_key(self):
        from infra.product_snapshot import backup, restore
        instance=Path(self.tmp.name)/'source-instance'
        runtime=ProductRuntime(instance,master_key=b'z'*32)
        runtime.model_settings.save(config(),'synthetic-backup-token',0)
        archive=Path(self.tmp.name)/'backup'; target=Path(self.tmp.name)/'restored'
        backup(instance,archive); restore(archive,target)
        restored=ModelSettings(SqliteSecretStore(target/'secrets.sqlite3',master_key=b'z'*32))
        self.assertEqual(restored.snapshot()[2],'synthetic-backup-token')
        for path in archive.rglob('*'):
            if path.is_file():
                self.assertNotIn(b'synthetic-backup-token',path.read_bytes())
                self.assertNotIn(b'z'*32,path.read_bytes())


if __name__=='__main__': unittest.main()
