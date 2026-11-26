import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from commercial_product.release_store import ReleaseStore


class ReleaseStoreTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name).resolve()
        store = self.root / "store"
        store.mkdir()
        self.store = ReleaseStore(store)
        self.workspace = self.root / "workspace.sqlite3"
        self.workspace.write_bytes(b"workspace fixture")

    def fixture(self, version):
        bundle = self.root / f"bundle-{version}"
        bundle.mkdir()
        data = f"version {version}".encode()
        (bundle / "version.txt").write_bytes(data)
        manifest = self.root / f"manifest-{version}.json"
        raw = json.dumps({"version": 1, "files": [
            {"path": "version.txt", "sha256": hashlib.sha256(data).hexdigest()},
        ]}).encode()
        manifest.write_bytes(raw)
        return bundle, manifest, hashlib.sha256(raw).hexdigest()

    def test_stage_activate_upgrade_and_rollback(self):
        first = self.store.stage(*self.fixture(1))
        self.assertIsNone(self.store.selection()["current"])
        self.assertEqual((self.store.activate(first) / "version.txt").read_text(), "version 1")
        second = self.store.stage(*self.fixture(2))
        self.store.activate(second)
        self.assertEqual((self.store.current() / "version.txt").read_text(), "version 2")
        self.assertEqual((self.store.rollback() / "version.txt").read_text(), "version 1")
        self.assertEqual(self.store.selection(), {"current": first, "previous": second})
        self.assertEqual(self.workspace.read_bytes(), b"workspace fixture")

    def test_tampered_update_or_rollback_cannot_replace_selection(self):
        first = self.store.stage(*self.fixture(1))
        first_path = self.store.activate(first)
        second = self.store.stage(*self.fixture(2))
        self.store.activate(second)
        (first_path / "version.txt").write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
            self.store.rollback()
        self.assertEqual(self.store.selection(), {"current": second, "previous": first})
        third = self.fixture(3)
        (third[0] / "version.txt").write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
            self.store.stage(*third)
        self.assertFalse((self.store.root / third[2]).exists())

    def test_repeat_stage_is_idempotent_and_activation_preserves_previous(self):
        first_fixture = self.fixture(1)
        first = self.store.stage(*first_fixture)
        self.store.activate(first)
        self.assertEqual(self.store.stage(*first_fixture), first)
        second = self.store.stage(*self.fixture(2))
        self.store.activate(second)
        self.store.activate(second)
        self.assertEqual(self.store.selection(), {"current": second, "previous": first})
