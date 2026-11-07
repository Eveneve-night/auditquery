"""Actual B linker outputs validated with published A #111, not toy results."""
from pathlib import Path
import importlib.util,os,sys
from copy import deepcopy
import unittest
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT/'src'))
from backend.agent.domain_pack import DomainPackRegistry
from backend.schema_linking.public import PublicSchemaLinker


def dependency():
    root=Path(os.environ.get('ROUND2_A_ROOT',ROOT))
    path=root/'src/backend/agent/round2_contracts.py'
    if not path.is_file():raise unittest.SkipTest('A #111 dependency required; set ROUND2_A_ROOT for isolated integration')
    spec=importlib.util.spec_from_file_location('backend.agent.round2_contracts',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


class PublicLinkingTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.contract=dependency();cls.pack=DomainPackRegistry(ROOT/'domain-packs').load('order_to_cash','1.1.0')
 def adapter(self,**kwargs):return PublicSchemaLinker(self.pack,contract_validator=self.contract,**kwargs)
 def test_real_table_and_field_selection_v1_pair(self):
  engine=self.adapter();request=engine.make_request('历史销售订单的历史订单金额','request.public.history')
  result=engine.locate(request);self.contract.validate_linking_pair(request,result,self.pack)
  self.assertEqual('schema.otc.sales_order_history',result['selected_table_id'])
  self.assertIn('schema.otc.sales_order_history.order_amount',result['selected_column_ids'])
  self.assertFalse(result['sql_execution_authorized'])
  self.assertEqual(228,len(request['authorized_object_ids']))
 def test_clarification_uses_real_versioned_slot_and_unknown_abstains(self):
  engine=self.adapter()
  for query,status in [('订单','clarify'),('sales_ordr','clarify'),('星际基因表达谱','abstain'),('当前销售订单与实收款记录','clarify')]:
   result=engine.locate(engine.make_request(query,'request.public.status'))
   self.assertEqual(status,result['status'])
   if status=='clarify':self.assertEqual('slot.schema_target',result['typed_decision']['missing_or_conflicting_slots'][0]['slot_id'])
 def test_old_frozen_pack_has_no_invented_clarification_slot(self):
  old=DomainPackRegistry(ROOT/'domain-packs').load('order_to_cash','1.0.0')
  engine=PublicSchemaLinker(old,contract_validator=self.contract)
  result=engine.locate(engine.make_request('订单','request.public.old'))
  self.assertEqual('abstain',result['status']);self.assertEqual([],result['typed_decision']['missing_or_conflicting_slots'])
 def test_full_catalog_and_answer_injection_fail_closed(self):
  engine=self.adapter();req=engine.make_request('订单','request.public.input')
  for key in ('expected','gold','label','required_slots','recognized_refs'):
   raw=deepcopy(req);raw[key]=[]
   with self.assertRaises(self.contract.Round2ContractError):engine.locate(raw)
  raw=deepcopy(req);raw['authorized_object_ids'].pop()
  with self.assertRaises(self.contract.Round2ContractError):engine.locate(raw)
 def test_return_boundary_cannot_skip_real_validator(self):
  from unittest.mock import patch
  engine=self.adapter();req=engine.make_request('销售单','request.public.guard')
  with patch.object(self.contract,'validate_linking_pair',side_effect=self.contract.Round2ContractError('rejected')) as guard:
   with self.assertRaises(self.contract.Round2ContractError):engine.locate(req)
   guard.assert_called_once()
 def test_unknown_selected_column_foreign_parent_and_execution_mutations(self):
  engine=self.adapter();req=engine.make_request('历史销售订单的历史订单金额','request.public.negative');result=engine.locate(req)
  for mutation in ('foreign','execution','input','score','relation'):
   r=deepcopy(result)
   if mutation=='foreign':r['selected_column_ids']=['schema.otc.receipt.received_amount']
   elif mutation=='execution':r['sql_execution_authorized']=True
   elif mutation=='input':r['input_sha256']='0'*64
   elif mutation=='score':r['typed_decision']['raw_score']=0
   else:r['table_candidates'][0]['evidence_refs'].append('relation.unavailable')
   with self.assertRaises(self.contract.Round2ContractError):self.contract.validate_linking_pair(req,r,self.pack)

if __name__=='__main__':unittest.main()
