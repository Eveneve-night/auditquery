from copy import deepcopy
from pathlib import Path
import unittest

from tools.evaluation.validate_batch1_postmerge import EXCLUDED, compare, sha, source_manifest, verify_receipt, build_report

ROOT = Path(__file__).resolve().parents[2]


class PostmergeInventoryTests(unittest.TestCase):
    def test_input_manifest_covers_all_consumed_categories(self):
        paths = source_manifest(ROOT, "HEAD")
        for path in (
            "src/backend/semantics/public.py", "src/frontend/d5-console/semantic-comparison.js",
            "configs/schemas/semantics/semantic-result.v1.schema.json",
            "domain-packs/retail_inventory/v1/slots.json", "tests/fixtures/batch1-public/v1/inputs.json",
            "tests/fixtures/batch1-public/v1/scoring.json", "tests/unit/test_batch1_b_public.py",
            "tools/evaluation/validate_batch1_c_public_adapter.py", "tests/reports/batch1-c-public-v1.json",
            "infra/start.ps1", "Dockerfile", "requirements-product.txt",
        ):
            self.assertTrue(path in paths, path)
        self.assertFalse(set(paths) & EXCLUDED)

    def test_any_covered_input_change_changes_manifest_digest(self):
        original = {p: {"git_oid": "a" * 40} for p in (
            "src/backend/example.py", "src/frontend/example.js", "tests/fixtures/example.json",
            "tests/unit/test_example.py", "tests/reports/batch1-c-public-v1.json", "start.ps1")}
        for path in original:
            changed = deepcopy(original)
            changed[path]["git_oid"] = "b" * 40
            self.assertNotEqual(sha(original), sha(changed))
            self.assertEqual([path], list(compare(original, changed)))

    def test_old_success_cannot_bind_different_revision(self):
        receipt = {"tested_commit": "a" * 40}
        receipt["stable_sha256"] = sha(receipt)
        with self.assertRaisesRegex(ValueError, "validation_revision_or_digest_mismatch"):
            verify_receipt(receipt, "b" * 40)

    def test_mutated_receipt_digest_rejected_before_counts_used(self):
        receipt = {"tested_commit": "a" * 40}
        receipt["stable_sha256"] = sha(receipt)
        receipt["unit_tests"] = {"run": 999}
        with self.assertRaisesRegex(ValueError, "validation_revision_or_digest_mismatch"):
            verify_receipt(receipt, "a" * 40)

    def test_probes_alone_cannot_claim_current_tree_success(self):
        with self.assertRaisesRegex(ValueError, "probes_without_revision_bound_full_suite"):
            build_report(ROOT, "1eb63d0", "1eb63d0", evidence_dir=Path("not_read_without_receipt"))
