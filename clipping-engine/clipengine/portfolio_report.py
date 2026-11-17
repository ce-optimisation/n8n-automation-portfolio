"""Read-only portfolio diagnostics: ``python -m clipengine --portfolio [--json]``.

Summarises every brand, creator and source with its REAL operational state, credentials, quota, recent
transitions and the owner's outstanding requirements — so nobody needs to read logs. Opens the database
read-only; never writes, never calls a platform.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from collections import Counter

from . import events, portfolio
from .platforms import ADAPTERS, adapter_for


class _RoDb:
    """Just enough of Database for the read-only report, over a read-only connection."""

    def __init__(self, path):
        self.conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
        self.conn.row_factory = sqlite3.Row

    def query(self, sql, params=()):
        return self.conn.execute(sql, params).fetchall()

    def one(self, sql, params=()):
        r = self.query(sql, params)
        return r[0] if r else None

    def get_kv(self, key, default=None):
        r = self.one("SELECT value FROM meta WHERE key=?", (key,))
        return r["value"] if r else default


def build(settings, db=None) -> dict:
    own = db is None
    if own:
        path = settings.db_path
        if not path.exists():
            return {"error": f"No database at {path}; start the engine once first."}
        db = _RoDb(path)
    try:
        if not db.one("SELECT name FROM sqlite_master WHERE name='monitor_sources'"):
            return {"error": "The database predates the portfolio (schema v6). Start the updated engine once to upgrade it."}
        rows = portfolio.summary(settings, db, persist=False)
        now = time.time()
        creds = {}
        for name in ADAPTERS:
            ad = adapter_for(settings, name)
            creds[name] = "ready" if ad.configured() else "needs_credentials"
        counts = Counter(r["state"] for r in rows)
        brands = db.query("SELECT * FROM brands ORDER BY key")
        ev = db.query("SELECT e.*, m.uid FROM monitor_events e LEFT JOIN monitor_sources m ON m.id=e.monitor_source_id "
                      "ORDER BY e.id DESC LIMIT 15")
        queue = {r["brand_key"]: r["n"] for r in db.query(
            "SELECT brand_key, COUNT(*) n FROM discovered_items WHERE state='queued' GROUP BY brand_key")}
        items = db.one("SELECT COUNT(*) n FROM discovered_items WHERE state<>'baseline'")["n"]
        active = events.active_events(db, now)
        todo = []
        for r in rows:
            if r["requirement"] and r["state"] in ("NEEDS_VERIFICATION", "NEEDS_CREDENTIALS"):
                todo.append(f"{r['creator']} / {r['platform']}: {r['requirement']}")
        for name, st in creds.items():
            if st != "ready":
                todo.append(f"{name}: add the free developer credentials (see .env.example)")
        return {"generated": time.strftime("%Y-%m-%d %H:%M:%S"), "brands": [dict(b) for b in brands],
                "sources": rows, "states": dict(counts), "credentials": creds, "queued_by_brand": queue,
                "discovered_items": items, "active_events": [e["name"] for e in active],
                "recent_events": [dict(e) for e in ev], "owner_actions": sorted(set(todo)),
                "creators": len({r["creator_uid"] for r in rows})}
    finally:
        if own:
            db.conn.close()


def render(rep: dict) -> str:
    if rep.get("error"):
        return rep["error"]
    L = [f"Portfolio status {rep['generated']}: {len(rep['brands'])} brands, {rep['creators']} creators, "
         f"{len(rep['sources'])} sources, {rep['discovered_items']} discovered items",
         "States: " + ", ".join(f"{k}={v}" for k, v in sorted(rep["states"].items())),
         "Credentials: " + ", ".join(f"{k}={v}" for k, v in rep["credentials"].items()),
         "Active priority events: " + (", ".join(rep["active_events"]) or "none"), ""]
    brand = None
    for r in rep["sources"]:
        if r["brand_key"] != brand:
            brand = r["brand_key"]
            L.append(f"== {next((b['name'] for b in rep['brands'] if b['key'] == brand), brand)} "
                     f"(queued: {rep['queued_by_brand'].get(brand, 0)})")
        last = time.strftime("%m-%d %H:%M", time.localtime(r["last_success"])) if r["last_success"] else "never"
        L.append(f"  {r['creator'][:30]:30} {r['platform']:8} {r['state']:19} {r['verification']:18} live={r['live_state']:7} "
                 f"acq={r['acquisition']:19} ok={last}" + (f"  ERR {r['error_code']}" if r["error_code"] else ""))
    if rep["owner_actions"]:
        L += ["", "Needs the owner:"] + [f"  - {a}" for a in rep["owner_actions"]]
    if rep["recent_events"]:
        L += ["", "Recent transitions:"] + [f"  {time.strftime('%m-%d %H:%M', time.localtime(e['ts']))} {e['kind']:10} "
                                          f"{(e['uid'] or '')[:34]:34} {e['message'][:80]}" for e in rep["recent_events"]]
    return "\n".join(L)


def main(settings, as_json: bool = False) -> int:
    try:
        sys.stdout.reconfigure(errors="replace")      # a Windows cp1252 console must never crash on a symbol
    except (AttributeError, ValueError):
        pass
    rep = build(settings)
    print(json.dumps(rep, indent=1, default=str) if as_json else render(rep))
    return 1 if rep.get("error") else 0
