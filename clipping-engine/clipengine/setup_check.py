"""Guided verification for the laptop: ``Start-Clipping-Engine.cmd --portfolio-check`` (or
``python -m clipengine --portfolio-check``). Safe to run any time; changes nothing except the identity-verification
records it is asked to create, never touches .env, media or credentials, never prints a secret, needs no administrator rights.

It reports: version/commit, Python and packages, database schema, which platform credentials are present (yes/no only),
verifies identities through the official APIs when keys exist, lists accounts that still need YOUR confirmation, runs one
discovery health pass, links the live-broadcast report, and ends with the short list of what you still have to do.
"""
from __future__ import annotations

import importlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from . import portfolio, portfolio_report
from .config import PROJECT_ROOT
from .db import SCHEMA_VERSION

KEYS = {"youtube": ("youtube_api_key",), "twitch": ("twitch_client_id", "twitch_client_secret"),
        "kick": ("kick_client_id", "kick_client_secret")}
ENV_NAMES = {"youtube_api_key": "YOUTUBE_API_KEY", "twitch_client_id": "TWITCH_CLIENT_ID",
             "twitch_client_secret": "TWITCH_CLIENT_SECRET", "kick_client_id": "KICK_CLIENT_ID",
             "kick_client_secret": "KICK_CLIENT_SECRET"}


def _commit() -> str:
    try:
        r = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            b = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=10)
            return f"{r.stdout.strip()} on {b.stdout.strip()}"
    except (OSError, subprocess.SubprocessError):
        pass
    info = PROJECT_ROOT / "BUILD_INFO.txt"
    return info.read_text(encoding="utf-8", errors="replace").strip()[:80] if info.is_file() else \
        "unknown (this installed copy carries no git information; compare the Portfolio page and files with the repository)"


def credential_status(settings) -> dict:
    """{platform: {'state': 'complete'|'partial'|'missing', 'missing': [ENV NAMES]}} - never the values."""
    out = {}
    for plat, attrs in KEYS.items():
        have = [a for a in attrs if getattr(settings, a, "")]
        miss = [ENV_NAMES[a] for a in attrs if a not in have]
        out[plat] = {"state": "complete" if not miss else "partial" if have else "missing", "missing": miss}
    return out


def schema_status(settings) -> tuple[str, int | None]:
    path = settings.db_path
    if not path.exists():
        return "no database yet (start the engine once)", None
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
        try:
            row = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        finally:
            con.close()
    except sqlite3.Error as e:
        return f"unreadable ({type(e).__name__})", None
    v = int(row[0]) if row else None
    if v is None:
        return "unknown version", None
    return ("current" if v >= SCHEMA_VERSION else f"OLD (v{v}, needs v{SCHEMA_VERSION}: start the engine once to upgrade)"), v


def run(settings, *, do_network: bool = True) -> tuple[list[str], list[str]]:
    """Returns (report lines, owner actions)."""
    L, todo = [], []
    L.append("== Version")
    L.append(f"  Python {sys.version.split()[0]}" + ("" if sys.version_info >= (3, 10) else "  <-- 3.10 or newer is required"))
    L.append(f"  Source: {_commit()}")
    missing = []
    for mod in ("fastapi", "uvicorn", "jinja2", "multipart", "httpx", "faster_whisper"):
        try:
            importlib.import_module(mod)
        except ImportError:
            missing.append(mod)
    L.append("  Packages: " + ("all present" if not missing else "MISSING " + ", ".join(missing) + " (re-run the installer)"))
    if missing:
        todo.append("Re-run Install-Clipping-Engine.cmd to restore the missing Python packages.")
    L.append("== Database")
    st, ver = schema_status(settings)
    L.append(f"  Schema: {st}")
    if ver is None or ver < SCHEMA_VERSION:
        todo.append("Start the engine once (Start-Clipping-Engine.cmd) so the database upgrades, then run this check again.")
    L.append("== Credentials (present or missing only; values are never shown)")
    creds = credential_status(settings)
    for plat, c in creds.items():
        L.append(f"  {plat:8} {c['state'].upper()}" + (f"  add to .env: {', '.join(c['missing'])}" if c["missing"] else ""))
        if c["missing"]:
            todo.append(f"Add {', '.join(c['missing'])} to .env (free developer registration), then restart and re-run this check.")
    ready = ver is not None and ver >= SCHEMA_VERSION
    if ready and do_network and any(c["state"] == "complete" for c in creds.values()):
        from .db import Database
        db = Database(settings.db_path)
        portfolio.seed(db, settings)
        L.append("== Identity verification (official APIs)")
        done = [p for p, c in creds.items() if c["state"] == "complete"] + ["podcast"]
        for ms in db.query("SELECT * FROM monitor_sources WHERE verification<>'VERIFIED' ORDER BY id"):
            if ms["platform"] not in done:
                continue
            r = portfolio.verify_source(settings, db, ms)
            L.append(f"  {ms['uid']:46} {'VERIFIED' if r.get('verified') else 'not confirmed: ' + str(r.get('message') or r.get('code') or 'needs your confirmation')[:70]}")
        from . import discovery
        L.append("== Discovery health pass")
        sup = discovery.DiscoverySupervisor(settings, db, None)
        L.append(f"  {sup.tick()}")
    elif ready:
        L.append("== Identity verification: skipped (no complete credential set yet)")
    L.append("== Status")
    rep = portfolio_report.build(settings)
    if rep.get("error"):
        L.append("  " + rep["error"])
    else:
        L.append("  " + ", ".join(f"{k}={v}" for k, v in sorted(rep["states"].items())))
        confirm = [r for r in rep["sources"] if r["verification"] != "VERIFIED" and r["platform_id"]]
        for r in confirm:
            L.append(f"  needs YOUR confirmation: {r['creator']} / {r['platform']} -> {r['channel_name']} (id {r['platform_id']}); open Portfolio, press 'Confirm official' if correct")
        if confirm:
            todo.append(f"Confirm {len(confirm)} resolved account(s) on the Portfolio page (only if they are the official ones).")
        unresolved = [r for r in rep["sources"] if r["state"] == "NEEDS_VERIFICATION" and not r["platform_id"] and r["requirement"]]
        for r in unresolved[:8]:
            L.append(f"  unresolved: {r['creator']} / {r['platform']}: {str(r['requirement'])[:90]}")
    L.append("== Live broadcast report")
    live = PROJECT_ROOT / "scripts" / "live_report.py"
    L.append("  Run:  .venv\\Scripts\\python.exe scripts\\live_report.py" if live.is_file() else "  scripts\\live_report.py not found in this copy")
    todo.append("Record each platform's written permission (Settings > Source permissions) and each creator's permission "
                "before any recording or download: nothing is acquired until both exist.")
    return L, list(dict.fromkeys(todo))


def main(settings, do_network: bool = True) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    lines, todo = run(settings, do_network=do_network)
    print("\n".join(lines))
    print("\n== Still to do (in order)")
    for i, t in enumerate(todo, 1):
        print(f"  {i}. {t}")
    return 0
