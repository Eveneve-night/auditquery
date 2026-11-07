"""Actual layout reuse, capability guards and content-free geometric checks."""
import unittest
from unittest.mock import patch
from pathlib import Path
import pdfplumber
from backend.document_processing import complexity as c
ASSETS=Path(__file__).resolve().parents[1]/'fixtures/document-complexity/v1/pdfs'

class LayoutReuseTests(unittest.TestCase):
    def data(self,name):return (ASSETS/(name+'.pdf')).read_bytes()
    def test_single_open_layout_and_actual_page_alignment(self):
        with patch.object(c.pdfplumber,'open',wraps=pdfplumber.open) as opened:
            result=c.process_adaptive(self.data('sparse_table-02'))
        self.assertEqual(1,opened.call_count);self.assertTrue(result['layout_reused'])
        self.assertTrue(result['success']);self.assertEqual({1},{b['page'] for b in result['blocks']})
    def test_zero_or_unsuitable_capabilities_never_parse(self):
        for name,available in [('prose-02',()),('sparse_table-01',('fast',))]:
            with patch.object(c,'execute_path') as fast,patch.object(c,'_parse_reusing_layout') as structured:
                result=c.process_adaptive(self.data(name),available=available)
                fast.assert_not_called();structured.assert_not_called()
            self.assertFalse(result['success']);self.assertEqual('no_available_suitable_strategy',result['error_code'])
    def test_failure_keeps_prediction_and_has_no_fast_fallback(self):
        with patch.object(c,'_parse_reusing_layout',side_effect=ValueError('injected')),patch.object(c,'execute_path') as fast:
            result=c.process_adaptive(self.data('sparse_table-01'));fast.assert_not_called()
        self.assertFalse(result['success']);self.assertEqual('ValueError',result['error_code'])
        self.assertIn('repeated_gapped_rows',result['features']['reason_codes'])
    def test_geometric_alignment_ignores_text_and_content_stream_order(self):
        chars=[{'text':str(row),'top':row*24,'x0':col+row%3,'x1':col+7+row%3,'size':10}
               for row in range(4) for col in (40,210,370)]
        self.assertEqual(4,c._aligned_gapped_rows(chars))
        self.assertEqual(4,c._aligned_gapped_rows([{**v,'text':'字'} for v in reversed(chars)]))
        self.assertEqual(0,c._aligned_gapped_rows([v for v in chars if v['x0']<300]))
