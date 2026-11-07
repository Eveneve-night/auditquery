"""The container probe must check application state, not only a volume marker."""

from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from tools.deployment.d5_05_container_probe import seed, verify


class D5ContainerProbeTests(unittest.TestCase):
    def test_seed_and_reopen_verify_published_document_index_and_fake_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            instance = Path(directory)
            seed(instance, ROOT, master_key=b"t" * 32)
            verify(instance, ROOT, master_key=b"t" * 32)

    def test_verify_fails_on_empty_instance(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(AssertionError):
                verify(Path(directory), ROOT, master_key=b"t" * 32)


if __name__ == "__main__":
    unittest.main()
