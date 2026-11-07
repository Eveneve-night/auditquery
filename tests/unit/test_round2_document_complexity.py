"""Real native PDF feature/path tests; all assets are public prompt_dev."""
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'src'));sys.path.insert(0,str(ROOT))
from backend.document_processing.complexity import inspect_structure,execute_path,process_adaptive,MAX_BYTES
from tools.evaluation.validate_round2_complexity import score
ASSETS=ROOT/'tests/fixtures/document-complexity/v1'

class DocumentComplexityTest(unittest.TestCase):
    def data(self,name):return (ASSETS/'pdfs'/f'{name}.pdf').read_bytes()

    def test_manifest_has_original_hashes_families_and_no_blind_claim(self):
        m=json.loads((ASSETS/'manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(32,len(m['documents']))
        self.assertEqual(32,len({d['sha256'] for d in m['documents']}))
        self.assertEqual(6,len({d['family_id'] for d in m['documents']}))
        self.assertEqual(0,m['independent_blind_samples'])
        for d in m['documents']:
            self.assertEqual('prompt_dev',d['split'])
            self.assertEqual(d['sha256'],hashlib.sha256((ASSETS/d['path']).read_bytes()).hexdigest())

    def test_prose_uses_fast_and_features_have_no_outcome_labels(self):
        f=inspect_structure(self.data('prose-02'))
        self.assertEqual('fast',f['strategy']);self.assertFalse(f['calibrated'])
        self.assertNotIn('elapsed_ms',f)
        self.assertEqual(0,f['pages'][0]['edge_count'])
        with self.assertRaises(TypeError):inspect_structure(self.data('prose-02'),expected='poison')

    def test_columns_detected_from_real_character_layout(self):
        f=inspect_structure(self.data('columns-01'))
        self.assertEqual('structured',f['strategy']);self.assertIn('column_gap',f['reason_codes'])

    def test_table_structure_and_real_row_reconstruction(self):
        data=self.data('ruled_table-01')
        self.assertIn('grid_structure',inspect_structure(data)['reason_codes'])
        a,b=execute_path(data,'fast'),execute_path(data,'structured')
        self.assertFalse(any('Stock 301-0 13 units' in x['text'] for x in a['blocks']))
        self.assertTrue(any(x['kind']=='table_row' and 'Stock 301-0 13 units' in x['text'] for x in b['blocks']))

    def test_sparse_table_failure_is_retained_not_relabelled_easy(self):
        data=self.data('sparse_table-01');f=inspect_structure(data)
        self.assertEqual('structured',f['strategy'])
        self.assertIn('repeated_gapped_rows',f['reason_codes'])
        from tools.evaluation.baselines import document_complexity_v1 as old
        self.assertEqual('fast',old.inspect_structure(data)['strategy'])
        labels=json.loads((ASSETS/'scoring.json').read_text(encoding='utf-8'))
        label=next(x for x in labels if x['document_id']=='sparse_table-01')
        self.assertFalse(score(execute_path(data,'fast'),label)[0]['supported'])
        self.assertTrue(score(execute_path(data,'structured'),label)[0]['supported'])
        self.assertFalse(score(old.process_adaptive(data),label)[0]['supported'])
        self.assertTrue(score(process_adaptive(data),label)[0]['supported'])

    def test_image_only_has_unknown_state_and_no_OCR_claim(self):
        data=self.data('scan-boundary-01');r=process_adaptive(data)
        self.assertEqual('manual_review',r['strategy']);self.assertFalse(r['success'])
        self.assertTrue(r['features']['unknown']);self.assertFalse(r['features']['OCR_available'])
        self.assertEqual([],r['blocks'])

    def test_page_and_exact_snippet_mapping_survive_two_page_document(self):
        labels=json.loads((ASSETS/'scoring.json').read_text(encoding='utf-8'))
        label=next(x for x in labels if x['document_id']=='prose-01')
        parsed=execute_path(self.data('prose-01'),'fast');results=score(parsed,label)
        self.assertTrue(all(x['supported'] for x in results))
        self.assertEqual(2,results[1]['source']['page'])
        s=results[1]['source'];block=next(b for b in parsed['blocks'] if b['page']==2)
        self.assertEqual(s['snippet'],block['text'][s['char_start']:s['char_end']])

    def test_invalid_pdf_failures_are_kept_as_manual_branch(self):
        r=process_adaptive(b'%PDF-1.4 invalid')
        self.assertFalse(r['success']);self.assertEqual('manual_review',r['strategy'])
        self.assertTrue(r['error_code'].startswith('preflight_'))
        with self.assertRaises(ValueError):inspect_structure(b'x'*(MAX_BYTES+1))

    def test_prediction_is_separate_from_actual_path_outcome(self):
        data=self.data('prose-02');before=inspect_structure(data)
        with patch('backend.document_processing.complexity.execute_path',return_value={'strategy':'fast','success':False,'blocks':[],'error_code':'injected','elapsed_ms':0}):
            r=process_adaptive(data)
        self.assertEqual(before,r['features']);self.assertFalse(r['success'])
        self.assertGreaterEqual(r['total_ms'],r['preflight_ms'])

if __name__=='__main__':unittest.main()
