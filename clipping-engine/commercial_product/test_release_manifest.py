import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from commercial_product.release_manifest import verify


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


class ReleaseManifestTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.parent = Path(scratch.name).resolve()
        self.root = self.parent / "release"
        (self.root / "clipengine").mkdir(parents=True)
        self.runtime = self.root / "clipengine" / "version.py"
        self.runtime.write_bytes(b"version = 21\n")
        (self.root / "README.md").write_bytes(b"Synthetic bundle\n")
        self.manifest = self.parent / "manifest.json"
        self.files = [
            {"path": "clipengine/version.py", "sha256": digest(self.runtime.read_bytes())},
            {"path": "README.md", "sha256": digest((self.root / "README.md").read_bytes())},
        ]

    def write_manifest(self, files=None):
        raw = json.dumps({"version": 1, "files": self.files if files is None else files}).encode()
        self.manifest.write_bytes(raw)
        return digest(raw)

    def test_exact_bundle_verifies_without_modification(self):
        pin = self.write_manifest()
        before = self.runtime.stat().st_mtime_ns
        self.assertEqual(verify(self.root, self.manifest, pin), 2)
        self.assertEqual(self.runtime.stat().st_mtime_ns, before)

    def test_content_and_manifest_tampering_fail(self):
        pin = self.write_manifest()
        self.runtime.write_bytes(b"version = 22\n")
        with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
            verify(self.root, self.manifest, pin)
        self.manifest.write_bytes(self.manifest.read_bytes() + b"\n")
        with self.assertRaisesRegex(ValueError, "trusted digest"):
            verify(self.root, self.manifest, pin)

    def test_unlisted_and_missing_files_fail(self):
        pin = self.write_manifest()
        extra = self.root / "extra.exe"
        extra.write_bytes(b"unlisted")
        with self.assertRaisesRegex(ValueError, "Unlisted: .*extra.exe"):
            verify(self.root, self.manifest, pin)
        extra.unlink()
        self.runtime.unlink()
        with self.assertRaisesRegex(ValueError, "Missing: .*version.py"):
            verify(self.root, self.manifest, pin)

    def test_path_escapes_and_windows_aliases_fail(self):
        clean = self.files[0]
        for invalid in ("../outside", "clipengine/../outside", "clipengine//version.py", "/absolute",
                        "C:/absolute", "clipengine/version.py:stream", "clipengine/version.py.",
                        "clipengine/CON.txt", "clipengine\\version.py"):
            with self.subTest(invalid=invalid):
                pin = self.write_manifest([{**clean, "path": invalid}])
                with self.assertRaisesRegex(ValueError, "Invalid relative"):
                    verify(self.root, self.manifest, pin)
        pin = self.write_manifest(self.files + [{**self.files[1], "path": "readme.md"}])
        with self.assertRaisesRegex(ValueError, "case-aliased"):
            verify(self.root, self.manifest, pin)

    def test_hard_link_to_file_outside_root_fails(self):
        pin = self.write_manifest()
        os.link(self.runtime, self.parent / "alias.py")
        with self.assertRaisesRegex(ValueError, "unaliased file"):
            verify(self.root, self.manifest, pin)
