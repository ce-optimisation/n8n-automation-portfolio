import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from contextlib import closing

from workspace_preferences import DATABASE, SCHEMA, export_preferences, restore_preferences


class PreferencesTest(unittest.TestCase):
    def test_restore_uses_an_allowlist_and_leaves_source_unchanged(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / DATABASE
            with closing(sqlite3.connect(source)) as db, db:
                db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
                db.executemany("INSERT INTO meta VALUES (?, ?)", [
                    ("caption_style", '{"enabled": true, "size": 54, "font": "private-path"}'),
                    ("title_style", '{"variant": "afm_v2", "centre_frac": 0.18}'),
                    ("access_token", "secret"),
                    ("recording.enabled", "1"),
                ])
                db.execute("CREATE TABLE clips (title TEXT)")
                db.execute("INSERT INTO clips VALUES ('private clip')")
            (root / ".env").write_text("TOKEN=secret", encoding="utf-8")
            (root / "private.mp4").write_bytes(b"private media")
            original = source.read_bytes()

            bundle = root / "preferences.json"
            digest = export_preferences(source, bundle)
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(json.loads(bundle.read_bytes()), {
                "schema": SCHEMA,
                "styles": {
                    "caption_style": {"enabled": True, "size": 54},
                    "title_style": {"variant": "afm_v2", "centre_frac": 0.18},
                },
            })
            destination = root / "new"
            restore_preferences(bundle, destination, digest)
            with closing(sqlite3.connect(destination / "data" / DATABASE)) as db:
                self.assertEqual(db.execute("PRAGMA integrity_check").fetchone(), ("ok",))
                self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall(),
                                 [("meta",)])
                restored = dict(db.execute("SELECT key, value FROM meta"))
            self.assertEqual(restored.pop("recording.enabled"), "0")
            self.assertEqual(restored.pop("delivery.enabled"), "0")
            self.assertEqual({key: json.loads(value) for key, value in restored.items()},
                             json.loads(bundle.read_bytes())["styles"])
            self.assertEqual(list((destination / "data").iterdir()), [destination / "data" / DATABASE])
            self.assertNotIn("secret", (destination / "config" / ".env").read_text())

    def test_rejects_tampering_and_unknown_fields_before_writing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bundle = root / "preferences.json"
            destination = root / "new"
            payload = json.dumps({"schema": SCHEMA, "styles": {"caption_style": {"token": "secret"}}}).encode()
            bundle.write_bytes(payload)
            with self.assertRaisesRegex(ValueError, "digest"):
                restore_preferences(bundle, destination, "0" * 64)
            with self.assertRaisesRegex(ValueError, "field"):
                restore_preferences(bundle, destination, hashlib.sha256(payload).hexdigest())
            self.assertFalse(destination.exists())

    def test_refuses_to_overwrite_existing_destination(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bundle = root / "preferences.json"
            payload = json.dumps({"schema": SCHEMA, "styles": {}}).encode()
            bundle.write_bytes(payload)
            with self.assertRaises(FileExistsError):
                restore_preferences(bundle, root, hashlib.sha256(payload).hexdigest())
            self.assertEqual(list(root.iterdir()), [bundle])
