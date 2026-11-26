"""Customer workspace export, verification, restore and deletion.

* Export: consistent online SQLite snapshots (backup API), media, logs and
  preferences into a ZIP with a SHA-256 manifest. Credentials, tokens, secrets
  and downloadable models are never exported.
* Restore: verifies every hash, refuses unsafe paths, writes into an empty
  directory and runs ``PRAGMA integrity_check`` on each database.
* Delete: removes a validated customer workspace's data directory, refusing the
  owner's production installation, and proves nothing remains.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import tempfile
import zipfile

FORMAT = "clipengine-workspace-export/1"
_DB_SUFFIXES = {".sqlite", ".sqlite3", ".db"}
_SKIP_DIRS = {"secrets", "models", "__pycache__", ".tmp", "tmp"}
_SKIP_NAME_PARTS = ("credential", "token", "secret", "password", "keyring", "cookie")
_SKIP_SUFFIXES = {".env", ".key", ".pem", ".p12", ".pfx", "-wal", "-shm", "-journal"}


def _protected_roots() -> list[Path]:
    roots = []
    if local := os.environ.get("LOCALAPPDATA"):
        roots.append(Path(local) / "ClippingEngine")
    return [r.resolve() for r in roots]


def excluded(rel: PurePosixPath) -> str | None:
    """Reason a path is excluded from export, or None."""
    lower = [p.lower() for p in rel.parts]
    if any(p in _SKIP_DIRS for p in lower[:-1]):
        return "excluded directory"
    name = lower[-1]
    if any(part in name for part in _SKIP_NAME_PARTS):
        return "credential material"
    if any(name.endswith(s) for s in _SKIP_SUFFIXES) or name.startswith(".env"):
        return "secret or transient file"
    return None


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def export(data_dir: Path, out_zip: Path, *, workspace_id: str) -> dict:
    data_dir = Path(data_dir).resolve()
    if not data_dir.is_dir():
        raise ValueError("Workspace data directory does not exist.")
    if out_zip.resolve().is_relative_to(data_dir):
        raise ValueError("Write the export outside the workspace being exported.")
    manifest = {"format": FORMAT, "workspace_id": workspace_id,
                "created_at": datetime.now(timezone.utc).isoformat(), "files": [], "excluded": []}
    with tempfile.TemporaryDirectory(prefix=".ws-export-") as scratch, \
            zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        for path in sorted(p for p in data_dir.rglob("*") if p.is_file()):
            rel = PurePosixPath(path.relative_to(data_dir).as_posix())
            if path.is_symlink():
                manifest["excluded"].append({"path": str(rel), "reason": "symbolic link"})
                continue
            if reason := excluded(rel):
                manifest["excluded"].append({"path": str(rel), "reason": reason})
                continue
            source = path
            if path.suffix.lower() in _DB_SUFFIXES:
                source = Path(scratch) / f"snap-{len(manifest['files'])}.db"
                src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
                dst = sqlite3.connect(source)
                with dst:
                    src.backup(dst)
                src.close(); dst.close()
            zf.write(source, f"data/{rel}")
            manifest["files"].append({"path": str(rel), "sha256": _sha256(source), "size": source.stat().st_size,
                                      "database": path.suffix.lower() in _DB_SUFFIXES})
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
    return manifest


def _safe_member(name: str) -> PurePosixPath:
    p = PurePosixPath(name)
    if p.is_absolute() or ".." in p.parts or ":" in name or "\\" in name or not p.parts or p.parts[0] != "data":
        raise ValueError(f"Unsafe archive member {name!r}")
    return PurePosixPath(*p.parts[1:])


def verify(zip_path: Path) -> dict:
    with zipfile.ZipFile(zip_path) as zf:
        manifest = json.loads(zf.read("manifest.json"))
        if manifest.get("format") != FORMAT:
            raise ValueError("Unsupported export format.")
        listed = {f["path"]: f for f in manifest["files"]}
        members = {str(_safe_member(n)): n for n in zf.namelist() if n != "manifest.json"}
        if set(members) != set(listed):
            raise ValueError("Archive contents do not match the manifest.")
        for rel, name in members.items():
            h = hashlib.sha256()
            with zf.open(name) as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            if h.hexdigest() != listed[rel]["sha256"]:
                raise ValueError(f"Hash mismatch for {rel}")
            if excluded(PurePosixPath(rel)):
                raise ValueError(f"Archive contains excluded material: {rel}")
    return manifest


def restore(zip_path: Path, target: Path) -> dict:
    manifest = verify(zip_path)
    target = Path(target)
    if target.exists() and any(target.iterdir()):
        raise ValueError("Restore target must be empty.")
    target.mkdir(parents=True, exist_ok=True)
    root = target.resolve()
    with zipfile.ZipFile(zip_path) as zf:
        for name in zf.namelist():
            if name == "manifest.json":
                continue
            dest = (root / Path(*_safe_member(name).parts)).resolve()
            if not dest.is_relative_to(root):
                raise ValueError("Path escapes restore target.")
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(name) as src, dest.open("wb") as out:
                shutil.copyfileobj(src, out)
    for f in manifest["files"]:
        if f["database"]:
            con = sqlite3.connect(root / Path(*PurePosixPath(f["path"]).parts))
            ok = con.execute("PRAGMA integrity_check").fetchone()[0]
            con.close()
            if ok != "ok":
                raise ValueError(f"Integrity check failed for {f['path']}: {ok}")
    return manifest


def delete(data_dir: Path, *, workspace_id: str, confirm: str) -> dict:
    """Delete a customer workspace's data. ``confirm`` must equal ``delete <workspace_id>``."""
    data_dir = Path(data_dir).resolve()
    if confirm != f"delete {workspace_id}":
        raise ValueError("Confirmation text does not match.")
    for root in _protected_roots():
        if data_dir == root or data_dir.is_relative_to(root) or root.is_relative_to(data_dir):
            raise ValueError("Refusing to delete the owner's production installation.")
    if data_dir.name != "data" or data_dir.parent.name != workspace_id:
        raise ValueError("Path is not a customer workspace data directory for this workspace ID.")
    count = sum(1 for p in data_dir.rglob("*") if p.is_file())
    shutil.rmtree(data_dir)
    if data_dir.exists():
        raise RuntimeError("Workspace data still present after deletion.")
    return {"workspace_id": workspace_id, "deleted_files": count,
            "deleted_at": datetime.now(timezone.utc).isoformat(),
            "note": "Credential-store entries are removed with the workspace credential namespace tool."}


def main() -> int:
    cli = argparse.ArgumentParser(description=__doc__)
    sub = cli.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export"); e.add_argument("data_dir", type=Path); e.add_argument("out", type=Path); e.add_argument("--workspace-id", required=True)
    v = sub.add_parser("verify"); v.add_argument("zip", type=Path)
    r = sub.add_parser("restore"); r.add_argument("zip", type=Path); r.add_argument("target", type=Path)
    d = sub.add_parser("delete"); d.add_argument("data_dir", type=Path); d.add_argument("--workspace-id", required=True); d.add_argument("--confirm", required=True)
    a = cli.parse_args()
    out = {"export": lambda: export(a.data_dir, a.out, workspace_id=a.workspace_id), "verify": lambda: verify(a.zip),
           "restore": lambda: restore(a.zip, a.target),
           "delete": lambda: delete(a.data_dir, workspace_id=a.workspace_id, confirm=a.confirm)}[a.cmd]()
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
