import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from zipfile import ZipFile

from commercial_agency import handoff


class HandoffTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        for client in ("fixture-one", "fixture-two"):
            handoff.onboard(self.root, client, f"fixture agreement {client}")
        (self.root / "fixture-one" / "inbox" / "clip.mp4").write_bytes(b"fixture-one clip")
        (self.root / "fixture-two" / "inbox" / "clip.mp4").write_bytes(b"fixture-two clip")
        (self.root / "fixture-two" / "inbox" / "other.srt").write_bytes(b"fixture-two captions")

    def package(self, client_id="fixture-one", files=None):
        return handoff.package(self.root, client_id, "test-delivery", ["clip.mp4"] if files is None else files,
                               "fixture source permission", "fixture final approval")

    def test_both_client_workspaces_use_their_own_asset_bytes(self):
        for client, payload in (("fixture-one", b"fixture-one clip"), ("fixture-two", b"fixture-two clip")):
            manifest = self.package(client)
            self.assertEqual(manifest["client_id"], client)
            self.assertEqual(manifest["assets"][0]["bytes"], len(payload))
            self.assertEqual(manifest["assets"][0]["sha256"], hashlib.sha256(payload).hexdigest())
            self.assertEqual(handoff.verify(self.root, client, "test-delivery"), manifest)

    def test_missing_asset_and_cross_workspace_paths_block_package(self):
        with self.assertRaises(FileNotFoundError):
            self.package(files=["other.srt"])
        for value in ("../fixture-two/inbox/other.srt", r"..\fixture-two\inbox\other.srt", "clip.mp4:stream"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.package(files=[value])
        self.assertFalse((self.root / "fixture-one" / "exports" / "test-delivery.zip").exists())

    def test_package_cannot_overwrite_existing_delivery(self):
        first = self.package()
        asset = self.root / "fixture-one" / "inbox" / "clip.mp4"
        asset.write_bytes(b"changed fixture")
        with self.assertRaises(FileExistsError):
            self.package()
        self.assertEqual(handoff.verify(self.root, "fixture-one", "test-delivery"), first)

    def test_verify_rejects_foreign_or_modified_artifacts(self):
        original = self.package()
        exports_one = self.root / "fixture-one" / "exports" / "test-delivery.zip"
        exports_two = self.root / "fixture-two" / "exports" / "test-delivery.zip"
        shutil.copyfile(exports_one, exports_two)
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            handoff.verify(self.root, "fixture-two", "test-delivery")
        with ZipFile(exports_one, "w") as archive:
            archive.writestr("manifest.json", json.dumps(original))
            archive.writestr("clip.mp4", b"changed")
        with self.assertRaisesRegex(ValueError, "does not match"):
            handoff.verify(self.root, "fixture-one", "test-delivery")

    def test_empty_rights_reference_blocks_package(self):
        with self.assertRaisesRegex(ValueError, "rights reference"):
            handoff.package(self.root, "fixture-one", "test-delivery", ["clip.mp4"], "", "approved")

    def test_cli_package_and_verify(self):
        cli = [sys.executable, "-m", "commercial_agency.handoff", "--root", str(self.root)]
        args = ["package", "fixture-one", "test-delivery", "--rights-reference", "fixture source",
                "--approval-reference", "fixture approval", "clip.mp4"]
        sealed = subprocess.run([*cli, *args], capture_output=True, text=True, check=True)
        verified = subprocess.run([*cli, "verify", "fixture-one", "test-delivery"],
                                  capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(sealed.stdout), json.loads(verified.stdout))


if __name__ == "__main__":
    unittest.main()
