"""Session 21 operational evidence collector (read-only, deterministic).

Replaces repeated manual/AI checks with one command. Prints a concise JSON
summary; writes full detail to --log. It never modifies production: SQLite is
opened read-only, nothing is restarted and no secret values are printed.

  python scripts/s21_ops.py --install %LOCALAPPDATA%\\ClippingEngine \
      --worktree <release worktree> --branch release/clipping-session21-staging \
      --installed-commit 4b933fa9e0f64c0c72b9714f53c37ea3eca20454 --checklist <COMMERCIAL_ACCEPTANCE_CHECKLIST.md>
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import urllib.request


def _git(worktree: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(worktree), *args], capture_output=True, text=True, timeout=60,
                          check=True).stdout.strip()


def health(url: str) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return {"ok": r.status == 200, **json.loads(r.read(4096))}
    except Exception as exc:  # noqa: BLE001 - report, don't raise
        return {"ok": False, "error": type(exc).__name__}


def database(db: Path) -> dict:
    if not db.is_file():
        return {"present": False}
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    try:
        schema = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        jobs = dict(con.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status").fetchall())
        running = con.execute("SELECT id, kind FROM jobs WHERE status='running'").fetchall()
        check = con.execute("PRAGMA quick_check").fetchone()[0]
        return {"present": True, "schema": int(schema[0]) if schema else None, "quick_check": check,
                "jobs": jobs, "running": [list(r) for r in running]}
    finally:
        con.close()


def env_flags(env_file: Path, keys: tuple[str, ...]) -> dict:
    """Report selected non-secret operating flags only."""
    out = {}
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            k, sep, v = line.partition("=")
            if sep and k.strip() in keys:
                out[k.strip()] = v.strip()
    return out


def _blob_sha(data: bytes) -> str:
    data = data.replace(b"\r\n", b"\n")
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def installed_vs_commit(install: Path, worktree: Path, commit: str, subdir: str = "clipping-engine") -> dict:
    """Compare installed runtime files with a commit, normalising line endings."""
    listing = _git(worktree, "ls-tree", "-r", commit, f"{subdir}/clipengine")
    mismatched, missing, checked = [], [], 0
    for line in listing.splitlines():
        meta, path = line.split("\t", 1)
        if not path.endswith((".py", ".html", ".css", ".js")):
            continue
        rel = path[len(subdir) + 1:]
        local = install / rel
        if not local.is_file():
            missing.append(rel)
            continue
        checked += 1
        committed = subprocess.run(["git", "-C", str(worktree), "cat-file", "blob", meta.split()[2]],
                                   capture_output=True, timeout=30, check=True).stdout
        if _blob_sha(local.read_bytes()) != _blob_sha(committed):
            mismatched.append(rel)
    return {"commit": commit, "checked": checked, "mismatched": mismatched, "missing": missing,
            "match": not mismatched and not missing}


def git_state(worktree: Path, branch: str) -> dict:
    head = _git(worktree, "rev-parse", "HEAD")
    remote = _git(worktree, "ls-remote", "origin", f"refs/heads/{branch}").split()
    dirty = [l for l in _git(worktree, "status", "--porcelain").splitlines() if l]
    return {"head": head, "remote": remote[0] if remote else None, "pushed": bool(remote) and remote[0] == head,
            "dirty_files": len(dirty)}


def checklist(path: Path) -> dict:
    rows = [l for l in path.read_text(encoding="utf-8").splitlines() if re.match(r"\| C[0-9]{2} \|", l)]
    statuses: dict[str, int] = {}
    for row in rows:
        cells = [c.strip() for c in row.strip("|").split("|")]
        status = cells[3] if len(cells) > 3 else "UNSPECIFIED"
        key = status.split(" —")[0].split(".")[0].upper() if status.isupper() or "—" in status else "UNCLASSIFIED"
        statuses[key] = statuses.get(key, 0) + 1
    return {"requirements": len(rows), "by_status": statuses}


def main() -> int:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--install", type=Path, required=True)
    cli.add_argument("--worktree", type=Path, required=True)
    cli.add_argument("--branch", required=True)
    cli.add_argument("--installed-commit")
    cli.add_argument("--checklist", type=Path)
    cli.add_argument("--health-url", default="http://127.0.0.1:8765/healthz")
    cli.add_argument("--log", type=Path)
    a = cli.parse_args()
    report = {
        "at": datetime.now(timezone.utc).isoformat(),
        "health": health(a.health_url),
        "database": database(a.install / "data" / "clipengine.sqlite3"),
        "flags": env_flags(a.install / ".env", ("AUTO_DISCOVERED_DOWNLOADS", "RECORDING_ENABLED", "HOST", "PORT")),
        "disk_free_gb": round(shutil.disk_usage(a.install).free / 1e9, 1),
        "backups": sorted(p.name for p in (a.install / "data" / "backups").glob("s2*") if p.is_dir())[-6:],
        "git": git_state(a.worktree, a.branch),
    }
    if a.installed_commit:
        report["installed"] = installed_vs_commit(a.install, a.worktree, a.installed_commit)
    if a.checklist:
        report["checklist"] = checklist(a.checklist)
    exceptions = []
    if not report["health"].get("ok") or not report["health"].get("worker_alive"):
        exceptions.append("engine or worker unhealthy")
    if report["database"].get("quick_check") not in (None, "ok"):
        exceptions.append("database quick_check failed")
    if not report["git"]["pushed"]:
        exceptions.append("staging HEAD not pushed")
    if report.get("installed") and not report["installed"]["match"]:
        exceptions.append("installed runtime differs from recorded commit")
    if report["disk_free_gb"] < 10:
        exceptions.append("low disk space")
    report["exceptions"] = exceptions
    if a.log:
        a.log.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 1 if exceptions else 0


if __name__ == "__main__":
    raise SystemExit(main())
