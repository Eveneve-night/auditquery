"""Backup and restore only external encrypted instance state."""

from pathlib import Path
import sys
import tempfile
import unittest

from infra.product_snapshot import backup, restore
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from backend.product.server import ProductRuntime


class ProductSnapshotTests(unittest.TestCase):
    def test_backup_restore_preserves_state_but_not_master_key(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            live, snapshot, restored = (base / name for name in ("live", "snapshot", "restored"))
            runtime = ProductRuntime(live, master_key=b"a" * 32)
            runtime.secret_store.put_if_absent("sk-testonly-0123456789")
            backup(live, snapshot)
            self.assertEqual({p.name for p in snapshot.iterdir()},
                             {"metadata.sqlite3", "secrets.sqlite3", "documents", "snapshot.json"})
            self.assertNotIn(b"a" * 32, (snapshot / "secrets.sqlite3").read_bytes())
            restore(snapshot, restored)
            reopened = ProductRuntime(restored, master_key=b"a" * 32)
            self.assertEqual(reopened.auth.store.read_state()["username"], "admin")
            self.assertTrue(reopened.secret_store.status().configured)

    def test_restore_does_not_overwrite_existing_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            live, snapshot = base / "live", base / "snapshot"
            ProductRuntime(live, master_key=b"a" * 32)
            backup(live, snapshot)
            with self.assertRaises(ValueError):
                restore(snapshot, live)

    def test_restore_rejects_changed_document_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            live, snapshot, restored = base / "live", base / "snapshot", base / "restored"
            ProductRuntime(live, master_key=b"a" * 32)
            (live / "documents" / "quarantine" / "doc.pdf").write_bytes(b"%PDF-test")
            backup(live, snapshot)
            (snapshot / "documents" / "quarantine" / "doc.pdf").write_bytes(b"tampered")
            with self.assertRaisesRegex(ValueError, "snapshot_invalid"):
                restore(snapshot, restored)
            self.assertFalse(restored.exists())

    def test_restore_rejects_missing_index_file(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            live, snapshot, restored = base / "live", base / "snapshot", base / "restored"
            ProductRuntime(live, master_key=b"a" * 32)
            (live / "documents" / "index" / "idx.0123456789ab.json").write_text("{}")
            backup(live, snapshot)
            (snapshot / "documents" / "index" / "idx.0123456789ab.json").unlink()
            with self.assertRaisesRegex(ValueError, "snapshot_invalid"):
                restore(snapshot, restored)
            self.assertFalse(restored.exists())


if __name__ == "__main__":
    unittest.main()
