"""Deterministic, coordinated production deployment of a verified staging commit.

  python scripts/s21_deploy.py preflight --install %LOCALAPPDATA%\\ClippingEngine --worktree <repo worktree>
  python scripts/s21_deploy.py deploy    --install ... --worktree ... --executor claude-code
  python scripts/s21_deploy.py rollback  --install ... --backup <backup folder>

Coordination: a deployment takes data/DEPLOY.lock (executor, commit, time). Another executor that finds the lock
must not deploy or restart the engine. Every deploy appends to data/RELEASE_LOG.jsonl.
Safety gates (all must pass): engine healthy, no running job, no live capture recording/finalising, no queued or
running delivery/cloud upload in progress, >= MIN_FREE_GB free, clean worktree whose HEAD is on the remote branch.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request

RUNTIME = ("clipengine", "commercial_intelligence", "commercial_agency", "commercial_product", "scripts")
FILES = ("requirements.txt",)
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "data", ".pytest_cache")
MIN_FREE_GB = 1.5
HEALTH = "http://127.0.0.1:8765/healthz"


def _git(wt: Path, *a) -> str:
    return subprocess.run(["git", "-C", str(wt), *a], capture_output=True, text=True, check=True, timeout=60).stdout.strip()


def health() -> dict:
    try:
        with urllib.request.urlopen(HEALTH, timeout=10) as r:
            return json.loads(r.read(4096))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": type(exc).__name__}


def _ro(db: Path):
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)


def preflight(install: Path, wt: Path, branch: str) -> dict:
    db = install / "data" / "clipengine.sqlite3"
    con = _ro(db)
    try:
        running = con.execute("SELECT id, kind FROM jobs WHERE status='running'").fetchall()
        live = con.execute("SELECT id, state FROM source_items WHERE state IN ('recording','finalizing')").fetchall()
        busy = con.execute("SELECT id, kind, status FROM jobs WHERE kind IN ('deliver_clip','cloud_upload') "
                           "AND status IN ('running','queued')").fetchall()
        schema = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        check = con.execute("PRAGMA quick_check").fetchone()[0]
    finally:
        con.close()
    head = _git(wt, "rev-parse", "HEAD")
    remote = (_git(wt, "ls-remote", "origin", f"refs/heads/{branch}").split() or [None])[0]
    dirty = [l for l in _git(wt, "status", "--porcelain", "--untracked-files=no").splitlines() if l]
    free = shutil.disk_usage(install).free / 1e9
    lock = install / "data" / "DEPLOY.lock"
    gates = {
        "engine_healthy": bool(health().get("ok")),
        "no_running_jobs": not running,
        "no_live_capture": not live,
        "no_pending_delivery_or_upload": not busy,
        "disk_free": free >= MIN_FREE_GB,
        "database_ok": check == "ok",
        "worktree_clean": not dirty,
        "head_pushed": head == remote,
        "no_other_deploy_lock": not lock.exists(),
    }
    return {"ok": all(gates.values()), "gates": gates, "head": head, "schema": int(schema[0]) if schema else None,
            "free_gb": round(free, 2), "running": running, "live": live, "busy": busy,
            "lock": lock.read_text(encoding="utf-8") if lock.exists() else None}


def _hash_tree(root: Path) -> dict:
    out = {}
    for name in RUNTIME:
        base = root / name
        for p in sorted(base.rglob("*")) if base.is_dir() else []:
            if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc" and "data" not in p.relative_to(root).parts[1:2]:
                out[p.relative_to(root).as_posix()] = hashlib.sha256(p.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
    for name in FILES:
        if (root / name).is_file():
            out[name] = hashlib.sha256((root / name).read_bytes().replace(b"\r\n", b"\n")).hexdigest()
    return out


def _engine_pid() -> int | None:
    out = subprocess.run(["powershell", "-NoProfile", "-Command",
                          "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
                          "? { $_.CommandLine -match 'clipengine --no-browser' -and $_.ExecutablePath -match 'Python3' } | "
                          "Select -First 1 -Expand ProcessId"], capture_output=True, text=True, timeout=60).stdout.strip()
    return int(out) if out.isdigit() else None


def restart_and_wait(timeout: float = 180) -> bool:
    pid = _engine_pid()
    if pid:
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, timeout=30)
    end = time.time() + timeout
    time.sleep(5)
    while time.time() < end:
        h = health()
        if h.get("ok") and h.get("worker_alive"):
            return True
        time.sleep(3)
    return False


def deploy(install: Path, wt: Path, branch: str, executor: str) -> dict:
    pre = preflight(install, wt, branch)
    if not pre["ok"]:
        return {"deployed": False, "reason": "preflight failed", "preflight": pre}
    src = wt / "clipping-engine"
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    lock = install / "data" / "DEPLOY.lock"
    lock.write_text(json.dumps({"executor": executor, "commit": pre["head"], "at": ts}), encoding="utf-8")
    backup = install / "data" / "backups" / f"s21-release-{ts}"
    try:
        backup.mkdir(parents=True)
        s, d = _ro(install / "data" / "clipengine.sqlite3"), sqlite3.connect(backup / "clipengine.sqlite3")
        s.backup(d); s.close()
        if d.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("backup integrity check failed")
        d.close()
        for name in RUNTIME:
            if (install / name).exists():
                shutil.copytree(install / name, backup / "runtime" / name, ignore=IGNORE)
        for name in FILES:
            if (install / name).exists():
                shutil.copy2(install / name, backup / "runtime" / name)
        (backup / "ROLLBACK.txt").write_text(
            f"Release {pre['head']} by {executor}. Rollback: python scripts/s21_deploy.py rollback --install <install> "
            f"--backup {backup}\nRestores runtime files; restores the database only with --restore-db (schema was "
            f"{pre['schema']} before this release).\n", encoding="utf-8")
        for name in RUNTIME:
            if (src / name).is_dir():
                shutil.copytree(src / name, install / name, dirs_exist_ok=True, ignore=IGNORE)
        for name in FILES:
            shutil.copy2(src / name, install / name)
        pip = subprocess.run([str(install / ".venv" / "Scripts" / "python.exe"), "-m", "pip", "install", "-q",
                              "--disable-pip-version-check", "-r", str(install / "requirements.txt")],
                             capture_output=True, text=True, timeout=900)
        if pip.returncode != 0:
            raise RuntimeError("dependency install failed: " + pip.stderr[-400:])
        mismatch = sorted(k for k, v in _hash_tree(src).items() if _hash_tree(install).get(k) != v)
        if mismatch:
            raise RuntimeError(f"installed files differ from staging: {mismatch[:5]}")
        healthy = restart_and_wait()
        post = verify(install)
        record = {"at": ts, "executor": executor, "commit": pre["head"], "schema_before": pre["schema"],
                  "schema_after": post["schema"], "backup": str(backup), "healthy": healthy, "post": post}
        with (install / "data" / "RELEASE_LOG.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
        return {"deployed": healthy and post["ok"], **record}
    finally:
        lock.unlink(missing_ok=True)


def verify(install: Path) -> dict:
    con = _ro(install / "data" / "clipengine.sqlite3")
    try:
        schema = int(con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0])
        check = con.execute("PRAGMA quick_check").fetchone()[0]
        rec = con.execute("SELECT value FROM meta WHERE key='recording.enabled'").fetchone()
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()
    env = (install / ".env").read_text(encoding="utf-8", errors="replace") if (install / ".env").exists() else ""
    h = health()
    checks = {"healthy": bool(h.get("ok") and h.get("worker_alive")), "quick_check": check == "ok",
              "recording_preserved": bool(rec) and rec[0] == "1",
              "vod_hold_preserved": "AUTO_DISCOVERED_DOWNLOADS=0" in env,
              "new_tables": {"clip_favourites", "campaign_costs", "favourite_events"} <= tables}
    return {"ok": all(checks.values()), "schema": schema, "checks": checks}


def rollback(install: Path, backup: Path, restore_db: bool) -> dict:
    lock = install / "data" / "DEPLOY.lock"
    if lock.exists():
        return {"rolled_back": False, "reason": "another deploy holds the lock", "lock": lock.read_text()}
    for name in RUNTIME:
        if (backup / "runtime" / name).is_dir():
            shutil.rmtree(install / name, ignore_errors=True)
            shutil.copytree(backup / "runtime" / name, install / name)
    for name in FILES:
        if (backup / "runtime" / name).is_file():
            shutil.copy2(backup / "runtime" / name, install / name)
    if restore_db:
        pid = _engine_pid()
        if pid:
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, timeout=30)
            time.sleep(3)
        shutil.copy2(backup / "clipengine.sqlite3", install / "data" / "clipengine.sqlite3")
    return {"rolled_back": True, "healthy": restart_and_wait(), "post": verify(install)}


def main() -> int:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("command", choices=("preflight", "deploy", "verify", "rollback"))
    cli.add_argument("--install", type=Path, required=True)
    cli.add_argument("--worktree", type=Path, default=Path(__file__).resolve().parents[2])
    cli.add_argument("--branch", default="release/clipping-session21-staging")
    cli.add_argument("--executor", default="claude-code")
    cli.add_argument("--backup", type=Path)
    cli.add_argument("--restore-db", action="store_true")
    a = cli.parse_args()
    if a.command == "preflight":
        out = preflight(a.install, a.worktree, a.branch)
    elif a.command == "deploy":
        out = deploy(a.install, a.worktree, a.branch, a.executor)
    elif a.command == "verify":
        out = verify(a.install)
    else:
        out = rollback(a.install, a.backup, a.restore_db)
    print(json.dumps(out, indent=2, default=str))
    return 0 if out.get("ok", out.get("deployed", out.get("rolled_back"))) else 1


if __name__ == "__main__":
    raise SystemExit(main())
