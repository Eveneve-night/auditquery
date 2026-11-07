import copy
import json
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/'src'))
from tools.evaluation.validate_batch1_c_public import build_report, runtime_input
from backend.agent.domain_pack import DomainPackRegistry, DomainPackError

class PublicAssetsTest(unittest.TestCase):
    def test_real_registry_loads_new_packs_with_frozen_v1_schema(self):
        report=build_report()
        self.assertEqual(3,report['int02']['registry_loaded'])
        self.assertEqual(36,report['case_count'])
        self.assertFalse(report['int02']['migration_target_proven'])

    def test_scoring_fields_never_enter_runtime_input(self):
        case={'input':{'domain_pack_id':'retail_inventory','question':'库存'},
              'expected':'secret','gold':{'question':'bad'},'label':'bad'}
        original=runtime_input(case)
        changed=copy.deepcopy(case)
        changed['input']['expected']='bad'
        changed['gold']='changed'
        self.assertEqual(original,runtime_input(changed))
        self.assertEqual({'domain_pack_id','question'},set(original))

    def test_pack_metadata_has_no_answer_or_question_fields(self):
        def scan(value):
            if isinstance(value,dict):
                self.assertFalse(set(value)&{'expected','gold','label','question','answer','sql'})
                for item in value.values(): scan(item)
            elif isinstance(value,list):
                for item in value: scan(item)
        for domain in ('retail_inventory','equipment_loan'):
            for path in (ROOT/'domain-packs'/domain/'v1').glob('*.json'):
                scan(json.loads(path.read_text(encoding='utf-8')))

    def test_answer_metadata_rejected_by_production_guard(self):
        from backend.agent.domain_pack import _assert_no_answer_fields
        with self.assertRaises(DomainPackError):
            _assert_no_answer_fields({'nested':{'expected':'bad'}})

    def test_new_pack_rules_execute_in_real_required_slot_resolver(self):
        from backend.agent.selective_routing import RequiredSlotResolver
        for domain in ('retail_inventory','equipment_loan'):
            pack=DomainPackRegistry().load(domain,'1.0.1')
            resolution=RequiredSlotResolver(pack).resolve(intent='sql',candidates=(),observations={})
            missing={a.slot_id for a in resolution.assessments if a.required and a.status=='missing'}
            self.assertEqual({'slot.metric','slot.location','slot.time_range'},missing)

if __name__=='__main__': unittest.main()
