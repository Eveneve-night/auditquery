import inspect
import unittest
from tools.evaluation import validate_round2_c_contract as contract
from tools.evaluation import validate_round2_complexity_v2 as comparison


class SameRepositoryToolsTest(unittest.TestCase):
    def test_default_pdf_contract_uses_same_repo_without_import_path_mutation(self):
        import backend.agent
        before=list(backend.agent.__path__)
        result=contract.run()
        self.assertEqual(32,result['documents'])
        self.assertEqual('passed',result['zero_capability']['status'])
        self.assertEqual(6,len(result['negative_binding_checks']))
        self.assertEqual(before,list(backend.agent.__path__))

    def test_comparison_root_is_optional_same_repo_not_an_agent_absolute_path(self):
        self.assertEqual(comparison.ROOT,inspect.signature(comparison.run).parameters['a_root'].default)
        self.assertTrue((comparison.ROOT/'tools/evaluation/round2_metrics.py').is_file())
