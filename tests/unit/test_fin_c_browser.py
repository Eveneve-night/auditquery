import functools,http.server,json,os,threading,unittest
from pathlib import Path
from playwright.sync_api import sync_playwright
ROOT=Path(__file__).resolve().parents[2]
class SourceBrowserTests(unittest.TestCase):
    def test_actual_preview_and_safe_dom(self):
        class Quiet(http.server.SimpleHTTPRequestHandler):
            def log_message(self,*args):pass
        server=http.server.ThreadingHTTPServer(('127.0.0.1',0),functools.partial(Quiet,directory=str(ROOT)))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with sync_playwright() as pw:
                edge=Path(os.environ.get('PROGRAMFILES(X86)','C:/Program Files (x86)'))/'Microsoft/Edge/Application/msedge.exe'
                browser=pw.chromium.launch(executable_path=str(edge),headless=True,timeout=15000)
                page=browser.new_page();page.set_default_timeout(10000);page.set_default_navigation_timeout(10000)
                errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                page.goto(f'http://127.0.0.1:{server.server_port}/src/frontend/d5-console/data-sources-preview.html')
                page.get_by_text('暂无数据源',exact=True).wait_for()
                self.assertEqual(8,page.locator('button.source-action').count())
                page.get_by_role('button',name='测试连接').click()
                page.get_by_text('尚未接入真实管理服务，此操作不可用。',exact=True).wait_for()
                page.evaluate('''async()=>{const m=await import('./data-sources.js');m.renderSourceWorkflow(document.querySelector('#source-workflow'),{snapshot:()=>({source:{source_id:'<img src=x onerror=alert(1)>',revision:1,status:'draft'},busy:false,error:null})});}''')
                self.assertEqual(0,page.locator('#source-workflow img').count())
                self.assertIn('<img',page.locator('#source-workflow').inner_text())
                self.assertEqual([],errors);browser.close()
        finally:server.shutdown();server.server_close();thread.join()
