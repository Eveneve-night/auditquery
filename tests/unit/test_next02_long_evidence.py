import hashlib
import io
from pathlib import Path
import sys
from types import SimpleNamespace
import textwrap
import unittest
from reportlab.pdfgen.canvas import Canvas
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'src'))
from backend.rag.published_retriever import _matching_snippet
from backend.rag.lexical_retriever import tokenize
from backend.api.online_rag_service import OnlineRagService
from backend.api.online_contracts import OnlineContractValidator

LONG='Returns are permitted only when all listed conditions apply, '+('condition '*35)+'except damaged goods are never returnable.'

def service(text,wrapped):
    out=io.BytesIO();c=Canvas(out,pagesize=(612,792));c.setFont('Helvetica',10);c.drawString(40,750,'Policy')
    c.setFont('Helvetica',9 if wrapped else 1.4)
    lines=textwrap.wrap(text,72) if wrapped else [text]
    for n,line in enumerate(lines):c.drawString(40,720-n*14,line)
    c.save();pdf=out.getvalue()
    record=SimpleNamespace(document_id='doc.long',state='published',index_version='index.long',
                           sha256=hashlib.sha256(pdf).hexdigest(),bytes=len(pdf),page_count=1)
    class Store:
        def get_active_index(self):return 'index.long'
        def list_records(self):return [record]
        def read_quarantine(self,_):return pdf
    return OnlineRagService(Store(),min_score=0)

class LongEvidenceTest(unittest.TestCase):
    def test_overlong_single_assertion_is_not_prefix_cropped(self):
        self.assertGreater(len(LONG),240)
        self.assertEqual('',_matching_snippet('Policy\n'+LONG,set(tokenize('Returns'))))

    def test_wrapped_physical_lines_keep_trailing_exception_in_unit(self):
        text='Policy\n'+'\n'.join(textwrap.wrap(LONG,72))
        self.assertEqual('',_matching_snippet(text,set(tokenize('Returns'))))

    def test_real_pdf_to_service_refuses_both_long_layouts(self):
        for wrapped in (False,True):
            with self.subTest(wrapped=wrapped):
                response=service(LONG,wrapped).submit({'question':'Returns','session_id':'s.long','request_id':'r.long','turn_id':0,'state_revision':0})
                self.assertEqual('refused',response['status']);self.assertEqual([],response['citations'])
                OnlineContractValidator().validate_response(response)

    def test_real_pdf_short_evidence_stays_verbatim_with_number(self):
        response=service('Returns are permitted for sealed goods.',True).submit({'question':'Returns','session_id':'s.short','request_id':'r.short','turn_id':0,'state_revision':0})
        self.assertEqual('success',response['status'])
        self.assertEqual('Returns are permitted for sealed goods. [1]',response['message'])
        self.assertEqual('Returns are permitted for sealed goods.',response['citations'][0]['snippet'])
        OnlineContractValidator().validate_response(response)

if __name__=='__main__':unittest.main()
