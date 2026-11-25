import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from commercial_product.workspace_preflight import main, preflight


class WorkspacePreflightTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.directory = Path(scratch.name)
        self.descriptor = self.directory / "workspaces.json"
        self.first = self.workspace("workspace-one", 8766)
        self.second = self.workspace("workspace-two", 8767)

    def workspace(self, identifier, port, root=None):
        root = self.directory / identifier if root is None else root
        (root / "config").mkdir(parents=True)
        (root / "data").mkdir()
        (root / "config" / ".env").write_text("DASHBOARD_PASSWORD=synthetic-secret\n", encoding="utf-8")
        return {"id": identifier, "root": str(root), "port": port}

    def write(self, *entries):
        self.descriptor.write_text(json.dumps({"version": 1, "workspaces": entries}), encoding="utf-8")

    def test_two_dedicated_roots_and_ports_generate_nonsecret_overrides(self):
        self.write(self.first, self.second)
        observed = preflight(self.descriptor)
        self.assertNotEqual(observed[0].data_dir, observed[1].data_dir)
        self.assertNotEqual(observed[0].env_file, observed[1].env_file)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(main([str(self.descriptor)]), 0)
        self.assertNotIn("synthetic-secret", stdout.getvalue())
        first, second = json.loads(stdout.getvalue())
        self.assertEqual(first["CLIPENGINE_HOST"], "127.0.0.1")
        self.assertEqual(first["CLIPENGINE_PORT"], "8766")
        self.assertEqual(second["CLIPENGINE_PORT"], "8767")
        self.assertEqual(first["CLIPENGINE_WORKSPACE_ID"], "workspace-one")
        self.assertEqual(second["CLIPENGINE_WORKSPACE_ID"], "workspace-two")

    def test_duplicate_id_port_and_nested_roots_fail(self):
        nested = self.workspace("nested", 8768, Path(self.first["root"]) / "nested")
        for second, expected in (
            ({**self.second, "id": self.first["id"]}, "Duplicate workspace ID"),
            ({**self.second, "port": self.first["port"]}, "Duplicate loopback port"),
            (nested, "Workspace roots overlap"),
            ({**self.second, "root": self.first["root"]}, "Workspace roots overlap"),
        ):
            with self.subTest(expected=expected):
                self.write(self.first, second)
                with self.assertRaisesRegex(ValueError, expected):
                    preflight(self.descriptor)

    def test_shared_config_hard_link_fails(self):
        first_env = Path(self.first["root"]) / "config" / ".env"
        second_env = Path(self.second["root"]) / "config" / ".env"
        second_env.unlink()
        os.link(first_env, second_env)
        self.write(self.first, self.second)
        with self.assertRaisesRegex(ValueError, "config files share one file"):
            preflight(self.descriptor)

    def test_invalid_descriptor_fails(self):
        for change in ({"port": True}, {"port": 70000}, {"id": "../outside"}, {"root": "."}, {"host": "0.0.0.0"}):
            with self.subTest(change=change):
                self.write({**self.first, **change})
                with self.assertRaises(ValueError):
                    preflight(self.descriptor)

    def test_missing_config_fails_without_creating_it(self):
        env = Path(self.first["root"]) / "config" / ".env"
        env.unlink()
        self.write(self.first)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main([str(self.descriptor)]), 1)
        self.assertFalse(env.exists())
