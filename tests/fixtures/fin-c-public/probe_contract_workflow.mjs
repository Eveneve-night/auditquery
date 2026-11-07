import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { createSourceWorkflow } from '../../../src/frontend/d5-console/data-sources.js';
const [python,root,contractRoot] = process.argv.slice(2);
const valid = async (record,context) => {
  const r=spawnSync(python,[`${root}/tools/evaluation/validate_fin_source_record.py`,'--contract-root',contractRoot],{input:JSON.stringify({record,context}),encoding:'utf8'});
  assert.equal(r.status,0,r.stderr);return JSON.parse(r.stdout).valid;
};
const record={contract_version:'1.0.0',source_id:'source.orders',revision:1,status:'draft',connector:{id:'sqlite',version:'1.0.0'},sqlite_asset_id:'asset.orders',credential_ref:null,binding:null};
const binding={schema_sha256:'a'.repeat(64),authorized_object_ids:['schema.otc.sales_order'],domain_pack:{id:'order_to_cash',version:'1.1.0',manifest_sha256:'b'.repeat(64)},policy:{version:'1.0.0',sha256:'c'.repeat(64)}};
const flow=createSourceWorkflow({getRole:()=> 'admin',validateSource:valid,adapter:{invoke:async(action,{previous})=> {
  const result=structuredClone(previous||record);
  if(previous)result.revision++;
  if(action==='test')result.status='verified';
  if(action==='sync'){result.status='synced';result.binding=binding;}
  if(action==='publish')result.status='published';
  return result;
}}});
assert.equal((await flow.perform('create')).source.status,'draft');
assert.equal((await flow.perform('publish')).error,'invalid_contract');
assert.equal((await flow.perform('test')).source.status,'verified');
assert.equal((await flow.perform('sync')).source.status,'synced');
assert.equal((await flow.perform('publish')).source.status,'published');
assert.equal(await valid({...record,password:'must-not-pass'}),false);
assert.equal(await valid({...record,credential_ref:{source_id:'source.other',secret_id:'credential.other',revision:1,purpose:'data_source'}}),false);
let adapterCalls=0;
const guarded=createSourceWorkflow({getRole:()=> 'admin',validateSource:valid,adapter:{invoke:async(action,{previous,input})=> {
  adapterCalls++; return previous ? {...previous,...input,revision:previous.revision+1} : structuredClone(record);
}}});
await guarded.perform('create');
const safeRef={source_id:'source.orders',secret_id:'credential.orders',revision:1,purpose:'data_source'};
for(const input of [
  {credential_ref:{...safeRef,password:'synthetic-secret'}},
  {credential_ref:{...safeRef,dsn:'synthetic-dsn'}},
  {binding:{...binding,domain_pack:{...binding.domain_pack,password:'synthetic-secret'}}},
  {credential_ref:{...safeRef,source_id:'source.other'}},
  {source_id:{password:'synthetic-secret'}},
  null,
]) {
  const before=adapterCalls;
  assert.equal((await guarded.perform('credential',input)).error,'invalid_contract');
  assert.equal(adapterCalls,before,'invalid input reached adapter');
  assert.equal(guarded.snapshot().source.revision,1);
  assert.ok(!JSON.stringify(guarded.snapshot()).includes('synthetic-secret'));
}
assert.equal((await guarded.perform('credential',{credential_ref:safeRef})).source.revision,2);
console.log(JSON.stringify({source:flow.snapshot().source,real_A_validator:true,adapter:'explicit_offline_fixture',http:false,checks:14,nested_input_attacks_rejected_before_adapter:6,valid_reference_accepted:true}));
