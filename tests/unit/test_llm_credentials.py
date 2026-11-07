from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from backend.llm.credentials import (  # noqa: E402
    Credential,
    CredentialError,
    ExternalFileCredentialProvider,
)


VALID_KEY_A = "sk-aaaaaaaaaaaaaaaaaaaaaaaa"
VALID_KEY_B = "sk-bbbbbbbbbbbbbbbbbbbbbbbb"


class CredentialTest(unittest.TestCase):
    def test_missing_paths_fail_without_reading_files(self) -> None:
        for config in ({"source": "external_file"},
                       {"source": "external_file", "preferred_path": "", "fallback_path": "x"}):
            with self.assertRaises(CredentialError):
                ExternalFileCredentialProvider.from_config(config)

    def test_preferred_text_file_wins_and_repr_is_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            preferred = root / "api key.txt"
            fallback = root / "fallback.csv"
            preferred.write_text(VALID_KEY_A, encoding="utf-8")
            fallback.write_text(f"API Key\n{VALID_KEY_B}\n", encoding="utf-8")

            credential = ExternalFileCredentialProvider(preferred, fallback).load()

            self.assertEqual(VALID_KEY_A, credential.value)
            self.assertEqual(preferred.name, credential.source_name)
            self.assertNotIn(VALID_KEY_A, repr(credential))
            self.assertIn("[REDACTED]", repr(credential))

    def test_invalid_preferred_file_falls_back_to_csv(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            preferred = root / "api key.txt"
            fallback = root / "fallback.csv"
            preferred.write_text("not-a-key", encoding="utf-8")
            fallback.write_text(f"name,api-key\ndefault,{VALID_KEY_B}\n", encoding="utf-8")

            credential = ExternalFileCredentialProvider(preferred, fallback).load()

            self.assertEqual(VALID_KEY_B, credential.value)
            self.assertEqual(fallback.name, credential.source_name)

    def test_loader_does_not_scan_unapproved_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "unapproved-secret.txt").write_text(VALID_KEY_A, encoding="utf-8")
            provider = ExternalFileCredentialProvider(
                root / "missing.txt", root / "missing.csv"
            )

            with self.assertRaises(CredentialError) as context:
                provider.load()

            self.assertNotIn(VALID_KEY_A, str(context.exception))

    def test_credential_dataclass_repr_never_contains_value(self) -> None:
        credential = Credential(VALID_KEY_A, "fixture")
        self.assertNotIn(VALID_KEY_A, repr(credential))


if __name__ == "__main__":
    unittest.main()
