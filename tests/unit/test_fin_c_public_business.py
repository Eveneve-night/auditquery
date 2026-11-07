import importlib.util,json,sqlite3,tempfile,unittest
from contextlib import closing
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('public_builder',ROOT/'tools/data_assets/build_public_business.py')
b=importlib.util.module_from_spec(spec);spec.loader.exec_module(b)
class PublicBusinessTests(unittest.TestCase):
    def test_real_databases_reproduce_and_refuse_writes(self):
        with tempfile.TemporaryDirectory() as a,tempfile.TemporaryDirectory() as c:
            first=b.build(Path(a));second=b.build(Path(c))
            self.assertEqual(first,second);self.assertEqual(39,len(first[0]['table_rows']))
            self.assertTrue(all(x['readonly_write_rejected'] for x in first))
            with closing(sqlite3.connect((Path(a)/'orders.sqlite3').as_uri()+'?mode=ro',uri=True)) as db:
                self.assertEqual(200,db.execute('SELECT SUM(order_amount) FROM sales_order').fetchone()[0])
                # Offline data correctness reference only, no product executor permission.
                self.assertEqual(0,db.execute('SELECT count(*) FROM sales_order_line l JOIN product p ON p.id=l.product_id WHERE l.line_amount != l.quantity*p.unit_price').fetchone()[0])
            with closing(sqlite3.connect((Path(a)/'maintenance.sqlite3').as_uri()+'?mode=ro',uri=True)) as db:
                self.assertEqual(1,db.execute("SELECT count(*) FROM work_order WHERE status='open'").fetchone()[0])
    def test_existing_output_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as p:
            b.build(Path(p))
            with self.assertRaises(FileExistsError):b.build(Path(p))
    def test_runtime_assets_have_no_scoring_fields(self):
        for path in b.ASSETS.glob('*-seed.json'):
            text=path.read_text(encoding='utf-8')
            self.assertNotIn('expected',text);self.assertNotIn('gold',text)
        self.assertEqual('prompt_dev',json.loads((b.ASSETS/'manifest.json').read_text())['split'])
    def test_text_checkout_line_endings_do_not_change_assets(self):
        original=b.ASSETS
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);checkout=root/'assets';checkout.mkdir()
            for p in original.iterdir():
                if p.is_file(): (checkout/p.name).write_bytes(p.read_bytes().replace(b'\r\n',b'\n').replace(b'\n',b'\r\n'))
            try:
                b.ASSETS=checkout
                self.assertEqual(2,len(b.build(root/'db')))
            finally:b.ASSETS=original
