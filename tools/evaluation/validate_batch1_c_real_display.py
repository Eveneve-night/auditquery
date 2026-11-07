"""Browser validation of actual B outputs already checked by A, no mock server."""
import argparse
import functools
import http.server
import json
from pathlib import Path
import threading
from playwright.sync_api import sync_playwright
ROOT=Path(__file__).resolve().parents[2]

class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self,*args):pass

def run(report):
    if report['public_adapter_status']!='executed' or not report['results']:
        raise ValueError('real_B_results_required')
    server=http.server.ThreadingHTTPServer(('127.0.0.1',0),functools.partial(Quiet,directory=str(ROOT)))
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch(channel='msedge',headless=True)
            try:
                page=browser.new_page();page.goto(f'http://127.0.0.1:{server.server_port}/')
                rows=page.evaluate('''async (results) => {
                  const {renderSemanticComparison:render}=await import('/src/frontend/d5-console/semantic-comparison.js');
                  const box=document.createElement('section');document.body.replaceChildren(box);
                  return results.map(item=>{
                    const r=item.result; const accepted=render(box,r);
                    const text=box.textContent;
                    return {case_id:item.case_id,passed:accepted && !box.hidden &&
                      text.includes(r.original_question) && text.includes(r.rewritten_question) &&
                      box.querySelectorAll('svg,script,img').length===0,
                      unknown_displayed:r.fragments.filter(f=>f.status==='unknown').every(f=>text.includes(f.text))};
                  });
                }''',report['results'])
                if not all(x['passed'] and x['unknown_displayed'] for x in rows):
                    raise AssertionError(rows)
                return {'scope':'real_B_output_to_Edge_component','mock':False,'rows':rows,
                        'passed':len(rows),'dependency_hashes':report['dependency_hashes'],
                        'public_HTTP_API':False,'model_calls':0,'key_reads':0,'cost_cny':0}
            finally:browser.close()
    finally:server.shutdown();server.server_close();thread.join()

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--input',type=Path,required=True);p.add_argument('--report',type=Path,required=True)
    args=p.parse_args();result=run(json.loads(args.input.read_text(encoding='utf-8')))
    args.report.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
    print(json.dumps({'passed':result['passed'],'mock':result['mock']}))
