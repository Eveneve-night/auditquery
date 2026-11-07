from dataclasses import replace
from pathlib import Path
import unittest
from src.backend.agent.domain_pack import DomainPackRegistry
from src.backend.semantics.conservative import ConservativeSemantics, normalized_with_offsets

ROOT = Path(__file__).resolve().parents[2]

class ConservativeSemanticsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pack = DomainPackRegistry(ROOT / 'domain-packs').load_path(ROOT / 'domain-packs/chinook/v1')

    def analyze(self, text):
        return ConservativeSemantics(self.pack).analyze(text)

    def test_alias_span_and_no_business_completion(self):
        r = self.analyze('不要统计歌手超过20万元的收入')
        self.assertEqual(r['rewritten_query'], '不要统计艺术家超过20万元的收入')
        for m in r['mentions']:
            self.assertEqual(m['text'], r['original_query'][m['start']:m['end']])
        self.assertFalse(r['sql_execution_authorized'])

    def test_casefold_expansion_and_fullwidth_offsets(self):
        r = self.analyze('Ａｌｂｕｍ 不少于１２张')
        self.assertEqual('专辑 不少于１２张', r['rewritten_query'])
        self.assertEqual('Ａｌｂｕｍ', r['mentions'][0]['text'])
        text, spans = normalized_with_offsets('ß A\t  B')
        self.assertEqual('ss a b', text)
        self.assertEqual((0, 1), spans[1])

    def test_unknown_unchanged_and_no_invented_type(self):
        r = self.analyze('统计量子猫的重量')
        self.assertEqual([], r['mentions'])
        self.assertEqual(r['original_query'], r['rewritten_query'])
        self.assertEqual(r['original_query'], r['uninterpreted'][0]['text'])

    def test_no_rewrite_needed(self):
        r = self.analyze('专辑')
        self.assertEqual([], r['changes'])

    def test_ascii_boundaries(self):
        r = self.analyze('TrackId')
        self.assertEqual([], r['mentions'])

    def test_typo_is_only_suggestion(self):
        r = self.analyze('Albm')
        self.assertEqual('Albm', r['rewritten_query'])
        self.assertTrue(r['requires_clarification'])

    def test_shared_alias_stays_ambiguous(self):
        artifacts = dict(self.pack.artifacts)
        artifacts['aliases'] = {'items': list(self.pack.artifact_items('aliases')) + [
            {'surface_form': '歌手', 'concept_id': 'concept.album', 'alias_id': 'alias.shared'}]}
        r = ConservativeSemantics(replace(self.pack, artifacts=artifacts)).analyze('歌手')
        self.assertEqual('歌手', r['rewritten_query'])
        self.assertEqual('ambiguous', r['mentions'][0]['status'])
        self.assertTrue(r['requires_clarification'])

    def test_invalid_inputs_and_answer_envelopes_rejected(self):
        for value in (None, {}, {'query': '专辑', 'expected': '艺术家'}, '', ' '):
            with self.assertRaises(ValueError):
                self.analyze(value)

    def test_new_concept_metadata_needs_no_type_code(self):
        artifacts = dict(self.pack.artifacts)
        artifacts['concepts'] = {'items': list(self.pack.artifact_items('concepts')) + [
            {'concept_id': 'concept.device', 'display_name': '设备'}]}
        artifacts['aliases'] = {'items': list(self.pack.artifact_items('aliases')) + [
            {'surface_form': '器材', 'concept_id': 'concept.device', 'alias_id': 'alias.device'}]}
        r = ConservativeSemantics(replace(self.pack, artifacts=artifacts)).analyze('不要超过5台器材')
        self.assertEqual('不要超过5台设备', r['rewritten_query'])
        self.assertEqual('concept.device', r['mentions'][0]['concepts'][0])
        # This synthetic fixture is a component test, not validated migration evidence.


class ReplayIsolationTests(unittest.TestCase):
    def test_answer_rows_are_not_runtime_input(self):
        from src.backend.semantics.replay import replay
        with self.assertRaises(ValueError):
            replay(ROOT / 'domain-packs/chinook/v1', [{'query': '专辑', 'expected': '艺术家'}])
        r = replay(ROOT / 'domain-packs/chinook/v1', ['唱片'])
        self.assertEqual(1, r['cases_run'])
        self.assertEqual('prompt_dev', r['split'])
        self.assertEqual(0, r['model_calls'])
        self.assertEqual('专辑', r['results'][0]['rewritten_query'])


class SchemaInventoryTests(unittest.TestCase):
    def test_no_row_values_or_writes_even_with_quoted_table_names(self):
        import tempfile, sqlite3, hashlib
        from contextlib import closing
        from src.backend.semantics.schema_inventory import inspect_sqlite_schema
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'metadata.sqlite3'
            with closing(sqlite3.connect(path)) as connection:
                connection.execute('CREATE TABLE "strange table" (id INTEGER PRIMARY KEY, amount TEXT)')
                connection.execute('INSERT INTO "strange table" VALUES (1, ?)', ('PRIVATE_ROW_DO_NOT_RETURN',))
                connection.commit()
            before = hashlib.sha256(path.read_bytes()).hexdigest()
            result = inspect_sqlite_schema(path)
            self.assertEqual(before, hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertNotIn('PRIVATE_ROW_DO_NOT_RETURN', str(result))
            self.assertEqual('strange table', result['tables'][0]['table'])
            self.assertEqual(0, result['business_rows_read'])
            self.assertFalse(result['execution_authorized'])
            with self.assertRaises(FileNotFoundError):
                inspect_sqlite_schema(path.parent / 'missing.sqlite3')

if __name__ == '__main__':
    unittest.main()
