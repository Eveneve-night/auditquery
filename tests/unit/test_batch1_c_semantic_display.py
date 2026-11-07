"""Mock-only browser component tests; no product API or model is simulated."""
import functools
import http.server
from pathlib import Path
import threading
import unittest
import sys
from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[2]

class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self,*args): pass

@unittest.skipUnless(sys.platform == 'win32', 'Microsoft Edge component probe requires Windows')
class SemanticDisplayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server=http.server.ThreadingHTTPServer(('127.0.0.1',0),
            functools.partial(QuietHandler,directory=str(ROOT)))
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True)
        cls.thread.start()
        cls.pw=sync_playwright().start()
        cls.browser=cls.pw.chromium.launch(channel='msedge',headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close();cls.pw.stop();cls.server.shutdown();cls.server.server_close();cls.thread.join()

    def render(self, mutate=''):
        page=self.browser.new_page()
        try:
            page.goto(f'http://127.0.0.1:{self.server.server_port}/')
            return page.evaluate('''async () => {
              const {renderSemanticComparison:render} = await import('/src/frontend/d5-console/semantic-comparison.js');
              const box=document.createElement('section'); document.body.replaceChildren(box);
              const result={result_version:'1.0.0',original_question:'😀歌手<svg onload=alert(1)>',
                rewritten_question:'😀艺术家<svg onload=alert(1)>',
                text_coordinates:{unit:'unicode_code_point',range_semantics:'half_open',normalization:'original_question'},
                edits:[{start:1,end:3,before:'歌手',after:'艺术家',evidence_refs:['concept.artist']}],
                fragments:[{start:3,end:24,text:'<svg onload=alert(1)>',status:'unknown'}]};
              result.fragments[0].end=Array.from(result.original_question).length;
              '''+mutate+'''
              const accepted=render(box,result);
              return {accepted,hidden:box.hidden,text:box.textContent,svg:box.querySelectorAll('svg').length};
            }''')
        finally: page.close()

    def test_mock_v1_codepoint_spans_unknown_and_safe_text(self):
        r=self.render()
        self.assertTrue(r['accepted']);self.assertFalse(r['hidden'])
        self.assertIn('未知：<svg',r['text']);self.assertIn('concept.artist',r['text'])
        self.assertEqual(0,r['svg'])

    def test_old_response_clears_and_hides_optional_component(self):
        r=self.render('delete result.result_version;')
        self.assertFalse(r['accepted']);self.assertTrue(r['hidden']);self.assertEqual('',r['text'])

    def test_invalid_original_offsets_hide_component(self):
        r=self.render('result.edits[0].start=0;')
        self.assertFalse(r['accepted']);self.assertTrue(r['hidden'])

    def test_unexplained_rewrite_is_hidden(self):
        r=self.render("result.rewritten_question='擅自补充2026年';")
        self.assertFalse(r['accepted']);self.assertTrue(r['hidden'])

if __name__=='__main__':unittest.main()
