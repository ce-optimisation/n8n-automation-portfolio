import http.client
import json
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from commercial_product.workspace_launcher import launch, stop
from commercial_product.workspace_preflight import preflight


FIXTURE = """
import http.server
import json
import os
from pathlib import Path

workspace_id = os.environ["CLIPENGINE_WORKSPACE_ID"]
data = Path(os.environ["CLIPENGINE_DATA_DIR"])
config = Path(os.environ["CLIPENGINE_ENV_FILE"])
assert "META_APP_SECRET" not in os.environ
assert "PYTHONPATH" not in os.environ
assert "RECORDING_ENABLED" not in os.environ
assert os.environ["CLIPENGINE_HOST"] == "127.0.0.1"
assert "--no-browser" in __import__("sys").argv
data.joinpath("fixture.txt").write_text(workspace_id, encoding="utf-8")

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path != "/healthz":
            self.send_error(404)
            return
        payload = json.dumps({
            "app": "clipengine",
            "worker_alive": True,
            "workspace": workspace_id,
            "fixture": config.read_text(encoding="utf-8"),
            "pid": os.getpid(),
        }).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

http.server.HTTPServer(("127.0.0.1", int(os.environ["CLIPENGINE_PORT"])), Handler).serve_forever()
"""


class WorkspaceLauncherTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.release = self.root / "synthetic_release"
        package = self.release / "clipengine"
        package.mkdir(parents=True)
        (package / "__main__.py").write_text(FIXTURE, encoding="utf-8")
        self.interpreter = Path(getattr(sys, "_base_executable", sys.executable))

    def workspaces(self, ports):
        entries = []
        for number, port in enumerate(ports):
            name = f"local-fixture-{number}"
            root = self.root / name
            (root / "config").mkdir(parents=True)
            (root / "config" / ".env").write_text(f"FIXTURE={name}", encoding="utf-8")
            (root / "data").mkdir()
            entries.append({"id": name, "root": str(root), "port": port})
        descriptor = self.root / "workspaces.json"
        descriptor.write_text(json.dumps({"version": 1, "workspaces": entries}), encoding="utf-8")
        return preflight(descriptor)

    def test_two_isolated_loopback_processes(self):
        with socket.socket() as one, socket.socket() as two:
            one.bind(("127.0.0.1", 0))
            two.bind(("127.0.0.1", 0))
            workspaces = self.workspaces([one.getsockname()[1], two.getsockname()[1]])
        children = []
        with patch.dict(os.environ, {
            "META_APP_SECRET": "must-not-inherit",
            "PYTHONPATH": "must-not-inherit",
            "RECORDING_ENABLED": "must-not-inherit",
            "CLIPENGINE_DATA_DIR": str(self.root / "owner-do-not-use"),
        }):
            for workspace in workspaces:
                child = launch(workspace, self.release, self.interpreter, timeout=8)
                children.append(child)
                self.addCleanup(stop, child)

        self.assertNotEqual(children[0].pid, children[1].pid)
        for workspace, child in zip(workspaces, children, strict=True):
            connection = http.client.HTTPConnection("127.0.0.1", workspace.port, timeout=2)
            try:
                connection.request("GET", "/healthz")
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(json.loads(response.read()), {
                    "app": "clipengine", "worker_alive": True, "workspace": workspace.workspace_id,
                    "fixture": f"FIXTURE={workspace.workspace_id}", "pid": child.pid,
                })
                self.assertEqual((workspace.data_dir / "fixture.txt").read_text(), workspace.workspace_id)
            finally:
                connection.close()
        self.assertFalse((self.root / "owner-do-not-use").exists())
        for child in children:
            stop(child)
            self.assertIsNotNone(child.poll())

    def test_occupied_port_refuses_to_start(self):
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            workspace, = self.workspaces([occupied.getsockname()[1]])
            with self.assertRaisesRegex(ValueError, "unavailable"):
                launch(workspace, self.release, self.interpreter)
            self.assertFalse((workspace.data_dir / "fixture.txt").exists())
