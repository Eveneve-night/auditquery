import copy
from pathlib import Path
import sys
import unittest
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'src'))
from backend.agent.domain_pack import DomainPackRegistry
from tools.evaluation.batch1_c_public_requests import build_public_request

class PublicRequestTest(unittest.TestCase):
    def test_scoring_poison_and_external_required_slots_cannot_enter_request(self):
        pack=DomainPackRegistry().load('retail_inventory','1.0.1')
        case={'case_id':'retail.alias','input':{'question':'😀不要改12件货品'},'expected':'poison','required_slots':['bad']}
        before=build_public_request(case,pack)
        changed=copy.deepcopy(case);changed['expected']='other';changed['input']['label']='bad'
        self.assertEqual(before,build_public_request(changed,pack))
        self.assertEqual(case['input']['question'],before['question'])
        self.assertEqual([],before['session']['slot_observations'])
        self.assertNotIn('required_slots',before)

    def test_catalog_contains_full_authorized_metadata_not_only_question_hit(self):
        pack=DomainPackRegistry().load('equipment_loan','1.0.1')
        request=build_public_request({'case_id':'equipment.unknown','input':{'question':'未知'}},pack)
        ids=request['domain_context']['authorized_catalog']['candidate_ids']
        self.assertIn('concept.equipment_loan.available',ids)
        self.assertIn('concept.equipment_loan.total',ids)
        self.assertIn('slot.time_range',ids)
        self.assertEqual(pack.manifest_sha256,request['domain_context']['domain_pack_ref']['manifest_sha256'])
