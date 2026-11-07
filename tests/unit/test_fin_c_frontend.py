import json,shutil,subprocess,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
class SourceComponentTests(unittest.TestCase):
    def test_async_component_guards_and_honest_mode(self):
        node=shutil.which('node')
        self.assertIsNotNone(node,'Node runtime required for actual frontend component checks')
        result=subprocess.run([node,str(ROOT/'tests/fixtures/fin-c-public/probe_workflow.mjs')],cwd=ROOT,capture_output=True,text=True)
        self.assertEqual(0,result.returncode,result.stderr)
        self.assertEqual(10,json.loads(result.stdout)['component_checks'])
