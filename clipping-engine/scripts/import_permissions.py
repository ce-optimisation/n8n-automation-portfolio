"""Record the owner's written permissions in bulk (Session 13).

Two spreadsheets (CSV, open in Excel) are filled in BY THE OWNER:
  * platform_permissions.csv  - one row per platform host (rumble.com, youtube.com, twitch.tv, kick.com)
  * creator_permissions.csv   - one row per creator

Rows with a blank ``granted_by`` or ``reference`` are skipped (never guessed). The default is a DRY RUN that shows what would
change; ``--apply`` writes after a verified database backup and ONLY once the owner types the attestation sentence
themselves. This tool never invents a grantor, date or reference.

    python scripts/import_permissions.py --make-templates        # writes the two CSVs (prefilled with names/ids only)
    python scripts/import_permissions.py                          # dry run
    python scripts/import_permissions.py --apply                  # real write (asks for the attestation)
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from clipengine.config import get_settings          # noqa: E402
from clipengine.db import Database                  # noqa: E402

ATTEST = "I HOLD THESE PERMISSIONS IN WRITING"
PLATFORM_HOSTS = ["rumble.com", "youtube.com", "twitch.tv", "kick.com"]
P_COLS = ["domain", "granted_by", "granted_on", "reference", "scope"]
C_COLS = ["id", "name", "brand", "platforms_on_roster", "granted_by", "reference", "permitted_uses", "expires", "evidence"]


def paths(settings):
    d = settings.data_dir / "reports" / "session13"
    d.mkdir(parents=True, exist_ok=True)
    return d / "platform_permissions.csv", d / "creator_permissions.csv"


def make_templates(settings, db):
    pp, cp = paths(settings)
    have = {r["domain"]: r for r in db.query("SELECT * FROM source_permissions")}
    with pp.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(P_COLS)
        for h in PLATFORM_HOSTS:
            r = have.get(h)
            w.writerow([h, r["granted_by"] if r else "", r["granted_on"] if r else "", r["reference"] if r else "",
                        r["scope"] if r else ""])
    plats: dict[int, list[str]] = {}
    for m in db.query("SELECT creator_id, platform FROM monitor_sources"):
        plats.setdefault(m["creator_id"], []).append(m["platform"])
    with cp.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(C_COLS)
        for c in db.query("SELECT * FROM creators WHERE id IN (SELECT DISTINCT creator_id FROM monitor_sources) "
                          "ORDER BY brand_key, name"):
            perm = json.loads(c["permission_json"] or "{}")
            w.writerow([c["id"], c["name"], c["brand_key"], "/".join(sorted(set(plats.get(c["id"], [])))),
                        perm.get("granted_by", ""), perm.get("reference", ""), perm.get("permitted_uses", ""),
                        perm.get("expires", ""), perm.get("evidence", "")])
    print(f"Templates written:\n  {pp}\n  {cp}")


def read(p: Path):
    if not p.is_file():
        return []
    with p.open(newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def plan(settings, db):
    pp, cp = paths(settings)
    prow, crow, skipped = [], [], []
    for r in read(pp):
        dom = (r.get("domain") or "").strip().lower()
        if not dom:
            continue
        if (r.get("granted_by") or "").strip() and (r.get("reference") or "").strip():
            prow.append({k: (r.get(k) or "").strip() for k in P_COLS})
        else:
            skipped.append(f"platform {dom}: needs granted_by and reference")
    today = time.strftime("%Y-%m-%d")
    for r in read(cp):
        try:
            cid = int(r["id"])
        except (TypeError, ValueError):
            continue
        gb, ref = (r.get("granted_by") or "").strip(), (r.get("reference") or "").strip()
        if not (gb and ref):
            skipped.append(f"creator {r.get('name')}: needs granted_by and reference")
            continue
        exp = (r.get("expires") or "").strip()
        if exp and exp[:10] < today:
            skipped.append(f"creator {r.get('name')}: expiry {exp} is in the past")
            continue
        crow.append({"id": cid, "name": r.get("name"), "granted_by": gb, "reference": ref,
                     "permitted_uses": (r.get("permitted_uses") or "").strip(), "expires": exp,
                     "evidence": (r.get("evidence") or "").strip()})
    return prow, crow, skipped


def backup(settings):
    dst = settings.data_dir / "backups" / f"pre-permissions-{time.strftime('%Y%m%d-%H%M%S')}.sqlite3"
    src = sqlite3.connect(f"file:{settings.db_path}?mode=ro", uri=True)
    out = sqlite3.connect(dst)
    src.backup(out)
    ok = out.execute("pragma integrity_check").fetchone()[0]
    out.close()
    src.close()
    if ok != "ok":
        raise SystemExit("backup failed its integrity check; nothing was written")
    return dst


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--make-templates", action="store_true")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    settings = get_settings()
    db = Database(settings.db_path)
    if a.make_templates:
        make_templates(settings, db)
        return 0
    prow, crow, skipped = plan(settings, db)
    print(f"Platforms to record : {len(prow)}  {[r['domain'] for r in prow]}")
    print(f"Creators to record  : {len(crow)}  {[r['name'] for r in crow]}")
    for s in skipped:
        print("  skipped -", s)
    if not a.apply:
        print("\nDRY RUN - nothing written. Re-run with --apply after checking the above.")
        return 0
    if not (prow or crow):
        print("Nothing to apply.")
        return 0
    jobs = db.one("SELECT COUNT(*) n FROM jobs WHERE status IN ('queued','running')")["n"]
    if jobs:
        raise SystemExit(f"{jobs} job(s) are active; try again when the engine is idle.")
    print(f"\nTo record these as written permissions you hold, type exactly:\n  {ATTEST}")
    if input("> ").strip() != ATTEST:
        print("Attestation not given; nothing written.")
        return 1
    print("Backup:", backup(settings).name)
    now = time.time()
    for r in prow:
        db.execute("INSERT OR REPLACE INTO source_permissions(domain, granted_by, granted_on, reference, scope, created_at, "
                   "updated_at) VALUES (?,?,?,?,?,?,?)",
                   (r["domain"], r["granted_by"], r["granted_on"], r["reference"], r["scope"], now, now))
    for r in crow:
        c = db.one("SELECT * FROM creators WHERE id=?", (r["id"],))
        if not c:
            continue
        perm = json.loads(c["permission_json"] or "{}")
        perm.update({k: r[k] for k in ("granted_by", "reference", "permitted_uses", "expires", "evidence") if r[k] or k in perm})
        db.update("creators", r["id"], permission_json=json.dumps(perm), status="active")
    from clipengine import portfolio
    portfolio.reconcile(settings, db)
    print(f"Recorded {len(prow)} platform and {len(crow)} creator permission(s); source states recomputed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
