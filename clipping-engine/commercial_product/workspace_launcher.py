"""Opt-in foreground launcher for a validated local workspace."""

from __future__ import annotations

import argparse
import http.client
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from commercial_product.release_store import ReleaseStore
from commercial_product.workspace_preflight import Workspace, preflight


SYSTEM_ENV = frozenset({
    "APPDATA", "COMSPEC", "HOMEDRIVE", "HOMEPATH", "LANG", "LOCALAPPDATA",
    "PROGRAMDATA", "SYSTEMDRIVE", "SYSTEMROOT", "TEMP", "TMP", "USERPROFILE", "WINDIR",
})


def child_environment(workspace: Workspace, interpreter: Path, tools_dir: Path | None) -> dict[str, str]:
    env = {name: value for name, value in os.environ.items() if name.upper() in SYSTEM_ENV}
    system_root = next((value for name, value in env.items() if name.upper() == "SYSTEMROOT"), None)
    paths = [str(interpreter.parent)]
    if tools_dir is not None:
        paths.append(str(tools_dir))
    if system_root is not None:
        paths.append(str(Path(system_root) / "System32"))
    env["PATH"] = os.pathsep.join(paths)
    env.update(workspace.environment_overrides())
    return env


def _require_free_port(port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        if os.name == "nt":
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError as error:
            raise ValueError(f"Loopback port {port} is unavailable.") from error


def _ready(port: int) -> bool:
    client = http.client.HTTPConnection("127.0.0.1", port, timeout=0.5)
    try:
        client.request("GET", "/healthz")
        response = client.getresponse()
        if response.status != 200:
            return False
        body = json.loads(response.read(4096))
        return isinstance(body, dict) and body.get("app") == "clipengine" and body.get("worker_alive") is True
    except (OSError, ValueError, http.client.HTTPException):
        return False
    finally:
        client.close()


def stop(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def launch(
    workspace: Workspace,
    release_root: Path,
    interpreter: Path,
    *,
    tools_dir: Path | None = None,
    timeout: float = 30,
) -> subprocess.Popen:
    """Start the supplied trusted release and wait for loopback worker readiness."""
    release_root = release_root.resolve(strict=True)
    # Keep the configured executable name so Windows virtual environments retain
    # their own pyvenv.cfg and site-packages.
    interpreter = interpreter.absolute()
    if not interpreter.is_file() or not (release_root / "clipengine" / "__main__.py").is_file():
        raise ValueError("Supply a Python executable and a trusted Clipping Engine release directory.")
    if tools_dir is not None:
        tools_dir = tools_dir.resolve(strict=True)
        if not tools_dir.is_dir():
            raise ValueError("The optional tools path must be a directory.")
    _require_free_port(workspace.port)
    process = subprocess.Popen(
        [str(interpreter), "-B", "-E", "-s", "-m", "clipengine", "--no-browser"],
        cwd=release_root,
        env=child_environment(workspace, interpreter, tools_dir),
        stdin=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"Workspace process exited before readiness (code {process.returncode}).")
            if _ready(workspace.port) and process.poll() is None:
                return process
            time.sleep(0.1)
        raise TimeoutError(f"Workspace did not report worker readiness on port {workspace.port}.")
    except BaseException:
        stop(process)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("descriptor", type=Path)
    parser.add_argument("workspace_id")
    release = parser.add_mutually_exclusive_group(required=True)
    release.add_argument("--release-root", type=Path)
    release.add_argument("--release-store", type=Path)
    parser.add_argument("--python", required=True, type=Path)
    parser.add_argument("--tools-dir", type=Path)
    args = parser.parse_args(argv)
    try:
        workspace = next((entry for entry in preflight(args.descriptor) if entry.workspace_id == args.workspace_id), None)
        if workspace is None:
            raise ValueError("The requested workspace ID is not in the descriptor.")
        release_root = ReleaseStore(args.release_store).current() if args.release_store else args.release_root
        process = launch(workspace, release_root, args.python, tools_dir=args.tools_dir)
    except (OSError, ValueError, RuntimeError) as error:
        print(f"Workspace launch failed: {error}", file=sys.stderr)
        return 1
    print(f"{workspace.workspace_id} ready at http://127.0.0.1:{workspace.port}", flush=True)
    try:
        return process.wait()
    except KeyboardInterrupt:
        stop(process)
        return 130


if __name__ == "__main__":
    sys.exit(main())
