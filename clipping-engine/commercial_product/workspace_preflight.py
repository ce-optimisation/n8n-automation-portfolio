"""Validate local customer workspace descriptors without launching the engine."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path


WORKSPACE_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,47}")


@dataclass(frozen=True)
class Workspace:
    workspace_id: str
    root: Path
    env_file: Path
    data_dir: Path
    port: int

    def environment_overrides(self) -> dict[str, str]:
        """Non-secret settings to set explicitly in a clean child environment."""
        return {
            "CLIPENGINE_WORKSPACE_ID": self.workspace_id,
            "CLIPENGINE_ENV_FILE": str(self.env_file),
            "CLIPENGINE_DATA_DIR": str(self.data_dir),
            "CLIPENGINE_HOST": "127.0.0.1",
            "CLIPENGINE_PORT": str(self.port),
        }


def _contained_path(root: Path, path: Path) -> Path:
    resolved = path.resolve()
    if resolved == root or not resolved.is_relative_to(root):
        raise ValueError(f"Workspace path must stay under its root: {path}")
    return resolved


def _workspace(raw: object) -> Workspace:
    if not isinstance(raw, dict) or set(raw) != {"id", "root", "port"}:
        raise ValueError("Each workspace must define exactly id, root and port.")
    workspace_id = raw["id"]
    if not isinstance(workspace_id, str) or not WORKSPACE_ID.fullmatch(workspace_id):
        raise ValueError("Workspace ID must be 1–48 lowercase letters, digits or hyphens and start with a letter or digit.")
    if type(raw["port"]) is not int or not 1024 <= raw["port"] <= 65535:
        raise ValueError(f"Workspace {workspace_id} needs a port between 1024 and 65535.")
    if not isinstance(raw["root"], str) or not Path(raw["root"]).is_absolute():
        raise ValueError(f"Workspace {workspace_id} needs an absolute root path.")
    root = Path(raw["root"]).resolve()
    if root == root.parent or not root.is_dir():
        raise ValueError(f"Workspace {workspace_id} needs an existing dedicated directory.")
    config_dir = _contained_path(root, root / "config")
    data_dir = _contained_path(root, root / "data")
    env_file = _contained_path(config_dir, config_dir / ".env")
    if config_dir.is_relative_to(data_dir) or data_dir.is_relative_to(config_dir):
        raise ValueError(f"Workspace {workspace_id} needs separate config and data directories.")
    if not data_dir.is_dir() or not env_file.is_file():
        raise ValueError(f"Workspace {workspace_id} needs an existing data directory and config/.env file.")
    return Workspace(workspace_id, root, env_file, data_dir, raw["port"])


def preflight(descriptor: Path) -> list[Workspace]:
    raw = json.loads(descriptor.read_text(encoding="utf-8-sig"))
    if not isinstance(raw, dict) or set(raw) != {"version", "workspaces"} or type(raw["version"]) is not int or raw["version"] != 1:
        raise ValueError("Expected a version 1 workspace descriptor.")
    if not isinstance(raw["workspaces"], list) or not raw["workspaces"]:
        raise ValueError("Define at least one workspace.")

    workspaces: list[Workspace] = []
    for entry in raw["workspaces"]:
        workspace = _workspace(entry)
        for prior in workspaces:
            if workspace.workspace_id == prior.workspace_id:
                raise ValueError(f"Duplicate workspace ID: {workspace.workspace_id}")
            if workspace.port == prior.port:
                raise ValueError(f"Duplicate loopback port: {workspace.port}")
            if workspace.root.is_relative_to(prior.root) or prior.root.is_relative_to(workspace.root):
                raise ValueError(f"Workspace roots overlap: {prior.workspace_id} and {workspace.workspace_id}")
            if workspace.env_file.samefile(prior.env_file):
                raise ValueError(f"Workspace config files share one file: {prior.workspace_id} and {workspace.workspace_id}")
        workspaces.append(workspace)
    return workspaces


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("descriptor", type=Path)
    args = parser.parse_args(argv)
    try:
        workspaces = preflight(args.descriptor)
    except (OSError, RuntimeError, ValueError) as error:
        print(f"Workspace preflight failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps([workspace.environment_overrides() for workspace in workspaces], indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
