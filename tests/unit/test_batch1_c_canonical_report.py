"""Portable report measurements using actual validated copied domain packs."""
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT),str(ROOT/'src')]
from backend.agent.domain_pack import DomainPackRegistry, DomainPackError
from tools.evaluation import validate_batch1_c_public as report

class CanonicalReportTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name)

    def checkout(self,name,newline):
        root=self.base/name
        shutil.copytree(ROOT/'domain-packs',root/'domain-packs')
        shutil.copytree(ROOT/'tests/fixtures/batch1-public',root/'tests/fixtures/batch1-public')
        evidence=root/report.RECOVERY_REPORT;evidence.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(ROOT/report.RECOVERY_REPORT,evidence)
        for path in root.rglob('*'):
            if path.is_file():
                path.write_bytes(report.canonical_utf8_lf(path).replace(b'\n',newline))
        return root

    def build(self,root):
        with patch.object(report,'ROOT',root),patch.object(report,'ASSETS',root/'tests/fixtures/batch1-public/v1'):
            return report.build_report(ROOT)

    def test_entire_report_equal_for_lf_and_crlf_actual_checkouts(self):
        lf,crlf=self.checkout('lf',b'\n'),self.checkout('crlf',b'\r\n')
        original=(lf/'domain-packs/retail_inventory/v1/concepts.json').read_bytes()
        converted=(crlf/'domain-packs/retail_inventory/v1/concepts.json').read_bytes()
        self.assertNotEqual(len(original),len(converted))
        a,b=self.build(lf),self.build(crlf)
        self.assertEqual(a,b)
        self.assertEqual('2.0.0',a['report_version'])
        self.assertEqual(36,a['int01']['executed'])
        for metadata in a['domain_packs'].values():
            self.assertNotIn('configuration_bytes',metadata)
            self.assertEqual(9,metadata['configuration_file_count'])
            names=[x['path'] for x in metadata['configuration_files']]
            self.assertEqual(sorted(names),names)
            self.assertEqual(metadata['configuration_canonical_utf8_lf_bytes'],
                             sum(x['canonical_utf8_lf_bytes'] for x in metadata['configuration_files']))

    def test_real_content_change_changes_size_and_binding(self):
        root=self.checkout('changed',b'\n');before=self.build(root)
        pack=root/'domain-packs/retail_inventory/v1'
        notice=pack/'LICENSE-or-SOURCE.md'
        notice.write_text(notice.read_text(encoding='utf-8')+'\nNew synthetic source notice.\n',encoding='utf-8')
        manifest=json.loads((pack/'manifest.json').read_text(encoding='utf-8'))
        # Only a temporary test copy is rehashed, never frozen repo metadata.
        for item in manifest['files']:
            if item['path']==notice.name:item['sha256']=report.sha(notice)
        (pack/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        after=self.build(root)
        a,b=before['domain_packs']['retail_inventory'],after['domain_packs']['retail_inventory']
        self.assertGreater(b['configuration_canonical_utf8_lf_bytes'],a['configuration_canonical_utf8_lf_bytes'])
        self.assertNotEqual(a['configuration_content_sha256'],b['configuration_content_sha256'])
        self.assertNotEqual(a['manifest_sha256'],b['manifest_sha256'])
        self.assertNotEqual(before['stable_sha256'],after['stable_sha256'])

    def test_equal_length_content_change_still_changes_hash(self):
        root=self.checkout('same_size',b'\n')
        registry=DomainPackRegistry(root/'domain-packs');pack=registry.load('retail_inventory','1.0.1')
        before=report.configuration_metrics(pack)
        notice=pack.pack_path/'LICENSE-or-SOURCE.md'
        content=notice.read_text(encoding='utf-8');self.assertIn('合成',content)
        notice.write_text(content.replace('合成','示例',1),encoding='utf-8')
        after=report.configuration_metrics(pack)
        self.assertEqual(before['configuration_canonical_utf8_lf_bytes'],after['configuration_canonical_utf8_lf_bytes'])
        self.assertNotEqual(before['configuration_content_sha256'],after['configuration_content_sha256'])
        with self.assertRaisesRegex(DomainPackError,'hash_mismatch'):registry.load('retail_inventory','1.0.1')

    def test_unlisted_files_not_measured_and_registry_still_rejects(self):
        root=self.checkout('extra',b'\n');registry=DomainPackRegistry(root/'domain-packs')
        pack=registry.load('equipment_loan','1.0.1');before=report.configuration_metrics(pack)
        (pack.pack_path/'scratch.tmp').write_text('not configuration',encoding='utf-8')
        self.assertEqual(before,report.configuration_metrics(pack))
        with self.assertRaisesRegex(DomainPackError,'unexpected_file_set'):registry.load('equipment_loan','1.0.1')

    def test_check_reports_real_drift(self):
        from contextlib import redirect_stdout
        import io
        target=self.base/'report.json'
        generated=report.build_report(ROOT)
        target.write_text(json.dumps(generated,ensure_ascii=False),encoding='utf-8')
        with patch.object(sys,'argv',['validator','--check','--report',str(target)]),redirect_stdout(io.StringIO()):
            self.assertEqual(0,report.main())
        generated['case_count']+=1
        target.write_text(json.dumps(generated,ensure_ascii=False),encoding='utf-8')
        with patch.object(sys,'argv',['validator','--check','--report',str(target)]),redirect_stdout(io.StringIO()):
            self.assertEqual(1,report.main())

if __name__=='__main__':unittest.main()
