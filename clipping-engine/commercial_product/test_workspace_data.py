import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
import zipfile

from commercial_product import workspace_data as wd


class WorkspaceDataTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.data = base / "ws-a" / "data"
        (self.data / "clips").mkdir(parents=True)
        (self.data / "secrets").mkdir()
        (self.data / "models").mkdir()
        con = sqlite3.connect(self.data / "clipengine.sqlite3")
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("CREATE TABLE clips (id INTEGER PRIMARY KEY, title TEXT)")
        con.execute("INSERT INTO clips (title) VALUES ('customer clip')")
        con.commit()
        self.live = con  # keep open: export must snapshot a live WAL database
        (self.data / "clips" / "c1.mp4").write_bytes(b"\x00\x01media")
        (self.data / "secrets" / "youtube.json").write_text("{}")
        (self.data / "models" / "big.bin").write_bytes(b"0" * 10)
        (self.data / "oauth_tokens.json").write_text("{}")
        (self.data / "prefs.json").write_text('{"captions": "anton"}')
        self.out = base / "export.zip"

    def tearDown(self):
        self.live.close()
        self.tmp.cleanup()

    def test_export_excludes_credentials_and_round_trips(self):
        m = wd.export(self.data, self.out, workspace_id="ws-a")
        paths = {f["path"] for f in m["files"]}
        self.assertEqual(paths, {"clipengine.sqlite3", "clips/c1.mp4", "prefs.json"})
        self.assertTrue({"secrets/youtube.json", "models/big.bin", "oauth_tokens.json"}
                        <= {e["path"] for e in m["excluded"]})
        target = Path(self.tmp.name) / "restored"
        wd.restore(self.out, target)
        con = sqlite3.connect(target / "clipengine.sqlite3")
        self.assertEqual(con.execute("SELECT title FROM clips").fetchone()[0], "customer clip")
        con.close()
        self.assertEqual((target / "clips" / "c1.mp4").read_bytes(), b"\x00\x01media")

    def test_tampering_and_traversal_are_rejected(self):
        wd.export(self.data, self.out, workspace_id="ws-a")
        tampered = Path(self.tmp.name) / "t.zip"
        with zipfile.ZipFile(self.out) as src, zipfile.ZipFile(tampered, "w") as dst:
            for n in src.namelist():
                dst.writestr(n, b"evil" if n == "data/prefs.json" else src.read(n))
        with self.assertRaises(ValueError):
            wd.verify(tampered)
        evil = Path(self.tmp.name) / "e.zip"
        with zipfile.ZipFile(evil, "w") as z:
            z.writestr("manifest.json", json.dumps({"format": wd.FORMAT, "files": []}))
            z.writestr("data/../../escape.txt", "x")
        with self.assertRaises(ValueError):
            wd.restore(evil, Path(self.tmp.name) / "r2")
        self.assertFalse((Path(self.tmp.name) / "escape.txt").exists())

    def test_restore_requires_empty_target_and_export_outside_workspace(self):
        wd.export(self.data, self.out, workspace_id="ws-a")
        with self.assertRaises(ValueError):
            wd.restore(self.out, self.data)
        with self.assertRaises(ValueError):
            wd.export(self.data, self.data / "inside.zip", workspace_id="ws-a")

    def test_delete_is_guarded_and_complete(self):
        with self.assertRaises(ValueError):
            wd.delete(self.data, workspace_id="ws-a", confirm="yes")
        with self.assertRaises(ValueError):
            wd.delete(self.data, workspace_id="ws-b", confirm="delete ws-b")
        prod = Path(os.environ.get("LOCALAPPDATA", self.tmp.name)) / "ClippingEngine" / "data"
        with self.assertRaises(ValueError):
            wd.delete(prod, workspace_id="ClippingEngine", confirm="delete ClippingEngine")
        self.live.close()
        result = wd.delete(self.data, workspace_id="ws-a", confirm="delete ws-a")
        self.assertEqual(result["deleted_files"], 6)
        self.assertFalse(self.data.exists())
        self.live = sqlite3.connect(":memory:")


if __name__ == "__main__":
    unittest.main()
