"""D5-05 trusted Actions receipt contract tests."""

from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "evaluation" / "import_d5_05_actions_receipt.py"
RECEIPT = ROOT / "tests" / "reports" / "d5-05-trusted-actions-evidence-v1.json"
SPEC = importlib.util.spec_from_file_location("d5_actions_receipt", TOOL)
mod = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(mod)


class D505ActionsReceiptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.receipt = json.loads(RECEIPT.read_text(encoding="utf-8"))

    def _resign(self, receipt: dict[str, object]) -> dict[str, object]:
        receipt["stable_sha256"] = mod._canonical_hash(receipt)
        return receipt

    def test_committed_receipt_is_valid_and_bound_to_main_push(self) -> None:
        mod.validate_receipt(
            self.receipt,
            expected_head_sha="4bc170cd895dff040058d71888f946379267f67a",
        )
        self.assertEqual(
            {"d5-05-clean-ubuntu", "d5-05-clean-windows"},
            {job["job"] for job in self.receipt["jobs"]},
        )

    def test_tampered_head_is_rejected_even_when_resigned(self) -> None:
        changed = deepcopy(self.receipt)
        changed["source"]["head_sha"] = "0" * 40
        self._resign(changed)
        with self.assertRaisesRegex(mod.ReceiptError, "receipt_head_sha_mismatch"):
            mod.validate_receipt(
                changed,
                expected_head_sha="4bc170cd895dff040058d71888f946379267f67a",
            )

    def test_missing_docker_check_is_rejected_even_when_resigned(self) -> None:
        changed = deepcopy(self.receipt)
        ubuntu = next(job for job in changed["jobs"] if job["job"] == "d5-05-clean-ubuntu")
        del ubuntu["checks"]["docker_build"]
        self._resign(changed)
        with self.assertRaisesRegex(mod.ReceiptError, "invalid_receipt_checks"):
            mod.validate_receipt(changed)

    def test_self_asserted_artifact_cannot_be_promoted(self) -> None:
        changed = deepcopy(self.receipt)
        changed["verification"]["self_asserted_artifact_is_sufficient"] = True
        self._resign(changed)
        with self.assertRaisesRegex(mod.ReceiptError, "invalid_receipt_verification"):
            mod.validate_receipt(changed)

    def test_duplicate_job_is_rejected(self) -> None:
        changed = deepcopy(self.receipt)
        changed["jobs"][1] = deepcopy(changed["jobs"][0])
        self._resign(changed)
        with self.assertRaisesRegex(mod.ReceiptError, "invalid_receipt_job_identity"):
            mod.validate_receipt(changed)

    def test_runner_os_and_artifact_url_are_bound(self) -> None:
        changed = deepcopy(self.receipt)
        changed["jobs"][0]["runner"]["os"] = "Windows"
        self._resign(changed)
        with self.assertRaisesRegex(mod.ReceiptError, "invalid_receipt_runner_os"):
            mod.validate_receipt(changed)

        changed = deepcopy(self.receipt)
        changed["jobs"][0]["artifact"]["api_url"] = "https://example.invalid/artifact"
        self._resign(changed)
        with self.assertRaisesRegex(mod.ReceiptError, "invalid_receipt_artifact_url"):
            mod.validate_receipt(changed)

    def test_unknown_content_field_is_rejected(self) -> None:
        changed = deepcopy(self.receipt)
        changed["prompt"] = "forbidden"
        self._resign(changed)
        with self.assertRaisesRegex(mod.ReceiptError, "unknown_receipt_field"):
            mod.validate_receipt(changed)


if __name__ == "__main__":
    unittest.main()
