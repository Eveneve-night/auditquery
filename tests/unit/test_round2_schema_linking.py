import json,sqlite3
from pathlib import Path
from dataclasses import replace
import unittest
from src.backend.agent.domain_pack import DomainPackRegistry
from src.backend.schema_linking.linker import SchemaLinker
from tools.schema_linking.evaluate_public import runtime_input
ROOT=Path(__file__).resolve().parents[2]

class LinkingTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.pack=DomainPackRegistry().load('order_to_cash','1.0.0');cls.engine=SchemaLinker(cls.pack)
 def request(self,q):return {'question':q,'authorized_ids':self.engine.public_catalog_ids()}
 def test_real_single_business_ddl_has_39_related_tables_no_rows(self):
  with sqlite3.connect(':memory:') as db:
   db.executescript((ROOT/'data/schema-linking/order_to_cash/v1/schema.sql').read_text())
   names=[x[0] for x in db.execute("select name from sqlite_master where type='table'")]
   self.assertEqual(39,len(names))
   for name in names:self.assertEqual(0,db.execute('select count(*) from "'+name+'"').fetchone()[0])
   for source,target,_,_,_ in self.engine.edges:self.assertIn(source,self.engine.tables);self.assertIn(target,self.engine.tables)
   reached={next(iter(self.engine.tables))}
   while True:
    new=reached|{b for a,b,*_ in self.engine.edges if a in reached}|{a for a,b,*_ in self.engine.edges if b in reached}
    if new==reached:break
    reached=new
   self.assertEqual(set(self.engine.tables),reached)
 def test_history_and_aggregate_grains_are_distinct(self):
  for q,table in [('历史销售订单','sales_order_history'),('销售订单月汇总','sales_order_monthly_summary'),('销售订单明细','sales_order_line')]:
   r=self.engine.locate(self.request(q));self.assertEqual(['schema.otc.'+table],r['selected_table_ids']);self.assertFalse(r['sql_execution_authorized'])
 def test_receivable_and_actual_receipt_fields(self):
  a=self.engine.locate(self.request('应收账款的应收金额'));b=self.engine.locate(self.request('实收款记录的实收金额'))
  self.assertIn('schema.otc.receivable.receivable_amount',a['selected_field_ids']);self.assertIn('schema.otc.receipt.received_amount',b['selected_field_ids'])
 def test_unknown_ambiguous_multiple_and_spelling_are_not_forced(self):
  for q,action in [('星际基因表达谱','abstain'),('订单','clarify'),('sales_ordr','clarify'),('当前销售订单与实收款记录','clarify')]:
   r=self.engine.locate(self.request(q));self.assertEqual(action,r['decision']);self.assertEqual([],r['selected_table_ids'])
 def test_answer_fields_and_unknown_mode_fail_closed(self):
  for key in ('expected','gold','label','required_slots'):
   raw=self.request('销售单');raw[key]=['schema.otc.sales_order']
   with self.assertRaises(ValueError):self.engine.locate(raw)
  raw=self.request('销售单');raw['mode']='paid_model'
  with self.assertRaises(ValueError):self.engine.locate(raw)
 def test_permissions_apply_to_fields_relations_and_candidate_features(self):
  allowed=['schema.otc.receivable','schema.otc.receivable.receivable_amount']
  r=self.engine.locate({'question':'实收款记录的实收金额','authorized_ids':allowed})
  self.assertTrue({x['candidate_id'] for x in r['table_candidates']+r['field_candidates']}<=set(allowed))
  self.assertTrue(all(not x['score_components']['relation_consistency'] for x in r['table_candidates']))
  with self.assertRaises(ValueError):self.engine.locate({'question':'金额','authorized_ids':['schema.otc.receivable.receivable_amount']})
  with self.assertRaises(ValueError):self.engine.locate({'question':'金额','authorized_ids':['schema.fake']})
 def test_hidden_catalog_changes_do_not_change_authorized_ranking(self):
  artifacts=dict(self.pack.artifacts);aliases=list(self.pack.artifact_items('aliases'))
  aliases.append({'alias_id':'alias.hidden','surface_form':'神秘金额','concept_id':'concept.otc.receipt'})
  artifacts['aliases']={'items':aliases}
  engine=SchemaLinker(replace(self.pack,artifacts=artifacts));allowed=['schema.otc.receivable','schema.otc.receivable.receivable_amount']
  raw={'question':'神秘金额','authorized_ids':allowed}
  a=self.engine.locate(raw);b=engine.locate(raw)
  for key in ('table_candidates','field_candidates','context','decision'):self.assertEqual(a[key],b[key])
 def test_catalog_permutation_and_unicode_preserve_ranking(self):
  a=self.request('😀历史销售订单');b=dict(a,authorized_ids=list(reversed(a['authorized_ids'])))
  x=self.engine.locate(a);y=self.engine.locate(b)
  self.assertEqual(x['table_candidates'],y['table_candidates']);self.assertEqual(x['selected_table_ids'],y['selected_table_ids'])
 def test_relation_evidence_resolves_and_only_has_authorized_endpoints(self):
  r=self.engine.locate(self.request('销售订单明细和商品档案'))
  for candidate in r['table_candidates']:
   for ref in candidate['evidence_refs']:self.pack.resolve(ref)
  self.assertTrue(any(c['score_components']['relation_consistency']>0 for c in r['table_candidates']))
 def test_labels_do_not_enter_runtime_payload(self):
  row={'input':{'question':'销售单'},'expected':['schema.otc.receipt'],'gold':'private'}
  payload=runtime_input(row,self.engine,'enhanced')
  self.assertEqual({'question','authorized_ids','mode'},set(payload));self.assertNotIn('expected',json.dumps(payload))
  self.assertEqual(self.engine.public_catalog_ids(),payload['authorized_ids'])

class FileAuthorizationTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):cls.pack=DomainPackRegistry().load('order_to_cash','1.0.0')
 def test_non_exportable_alias_file_cannot_affect_candidate_features(self):
  manifest=json.loads(json.dumps(dict(self.pack.manifest),default=lambda x:dict(x) if hasattr(x,'items') else list(x)))
  for entry in manifest['files']:
   if entry['role']=='aliases':entry['send_to_model']=False
  engine=SchemaLinker(replace(self.pack,manifest=manifest))
  result=engine.locate({'question':'SKU','authorized_ids':engine.public_catalog_ids()})
  self.assertEqual('abstain',result['decision'])
  self.assertTrue(all(not any(ref.startswith('alias.') for ref in row['evidence_refs']) for row in result['table_candidates']))

class SourceAuthorizationTests(unittest.TestCase):
 def test_parent_data_source_denial_is_not_overridden_by_public_child(self):
  pack=DomainPackRegistry().load('order_to_cash','1.0.0')
  artifacts=dict(pack.artifacts)
  items=[dict(x) for x in pack.artifact_items('schema_catalog')]
  next(x for x in items if x['object_type']=='data_source')['authorization_level']='local_only'
  artifacts['schema_catalog']={'items':items}
  engine=SchemaLinker(replace(pack,artifacts=artifacts))
  self.assertEqual([],engine.public_catalog_ids())
  with self.assertRaises(ValueError):engine.locate({'question':'订单','authorized_ids':['schema.otc.sales_order']})

class FieldIntentTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.pack=DomainPackRegistry().load('order_to_cash','1.0.0');cls.engine=SchemaLinker(cls.pack)
 def locate(self,question,engine=None,mode='enhanced'):
  engine=engine or self.engine
  return engine.locate({'question':question,'authorized_ids':engine.public_catalog_ids(),'mode':mode})
 def test_shared_table_words_do_not_select_unrequested_identifiers(self):
  for question,field in [('历史订单明细的历史订购数量','schema.otc.sales_order_line_history.quantity'),
                         ('供应商付款的实付金额','schema.otc.payment.paid_amount')]:
   self.assertEqual([field],self.locate(question)['selected_field_ids'])
 def test_table_only_alias_does_not_invent_an_amount_request(self):
  result=self.locate('每日订单汇总')
  self.assertEqual(['schema.otc.sales_order_daily_summary'],result['selected_table_ids'])
  self.assertEqual([],result['selected_field_ids'])
 def test_explicit_columns_and_alias_nested_in_field_remain_selectable(self):
  for question,fields in [('当前销售订单的订单金额和客户编号',{'schema.otc.sales_order.order_amount','schema.otc.sales_order.customer_id'}),
                          ('供应商付款的供应商编号和实付金额',{'schema.otc.payment.supplier_id','schema.otc.payment.paid_amount'})]:
   self.assertEqual(fields,set(self.locate(question)['selected_field_ids']))
 def test_renamed_metadata_is_used_without_business_specific_rules(self):
  artifacts=dict(self.pack.artifacts);items=[dict(x) for x in self.pack.artifact_items('schema_catalog')]
  names={'schema.otc.payment':'服务费用清单','schema.otc.payment.supplier_id':'服务费用编号','schema.otc.payment.paid_amount':'结算金额'}
  for item in items:
   if item['object_id'] in names:item['display_name']=names[item['object_id']]
  artifacts['schema_catalog']={'items':items};engine=SchemaLinker(replace(self.pack,artifacts=artifacts))
  self.assertEqual(['schema.otc.payment.paid_amount'],self.locate('服务费用清单的结算金额',engine)['selected_field_ids'])
 def test_lexical_baseline_keeps_original_field_selection(self):
  query='供应商付款的实付金额'
  self.assertEqual({'schema.otc.payment.supplier_id','schema.otc.payment.paid_amount'},set(self.locate(query,mode='lexical')['selected_field_ids']))

if __name__=='__main__':unittest.main()
