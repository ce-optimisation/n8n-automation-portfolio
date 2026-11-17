"""Import and reconcile the owner-supplied source roster (research/portfolio_sources_*.txt).

The roster holds CANDIDATE identities, not proof. This utility only fills in or updates the registry:
  * validates 6 brands / 18 creators / 27 sources and refuses to add anyone (no new creators, ever);
  * maps each line to its existing source record, never duplicating;
  * fills missing candidate URLs/handles (an owner-supplied Rumble URL goes through `owner_set`, so it is locked
    and re-verification is required); never overwrites a locked value and never re-enables a disabled source;
  * reports conflicts (a different URL than the one on file) instead of silently replacing them;
  * leaves every identity NEEDS_VERIFICATION until an official API / engine evidence confirms it.

    python scripts/portfolio_admin.py import-roster [file]
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlparse

from . import portfolio
from .portfolio import BRANDS, CREATORS, _norm

DEFAULT_FILE = Path(__file__).resolve().parent.parent / "research" / "portfolio_sources_2026-11-17.txt"
PLATFORMS = {"rumble": "rumble", "youtube": "youtube", "twitch": "twitch", "kick": "kick", "podcast": "podcast"}
ALIASES = {"steven bartlett diary of a ceo": "steven-bartlett", "modern wisdom chris williamson": "modern-wisdom",
           "all in podcast": "all-in-podcast", "joe rogan experience": "joe-rogan-experience"}
LINE = re.compile(r"^-\s+(?P<name>.+?)\s+\[(?P<plat>[A-Za-z]+)\]:\s*(?P<val>.+?)\s*$")


def parse(text: str) -> list[dict]:
    rows, brand, last = [], None, None
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.startswith("## "):
            brand = line[3:].strip()
            last = None
            continue
        m = LINE.match(line)
        if m and brand:
            last = {"brand": brand, "name": m["name"].strip(), "platform": m["plat"].lower(), "value": m["val"].strip(),
                    "evidence": ""}
            rows.append(last)
        elif last is not None and line.strip().startswith("Evidence/status:"):
            last["evidence"] = line.split(":", 1)[1].strip()
    return rows


def _uid_for(name: str) -> str | None:
    n = _norm(name)
    if n in ALIASES:
        return ALIASES[n]
    for c in CREATORS:
        if _norm(c["name"]) == n or _norm(c["name"].split("—")[0]) == _norm(name.split("/")[0]):
            return c["uid"]
    return None


def _handle(platform: str, url: str) -> str:
    path = [x for x in urlparse(url).path.split("/") if x]
    if not path:
        return ""
    if platform == "youtube":
        return path[0] if path[0].startswith("@") else ""
    return path[-1] if platform in ("twitch", "kick") else ""


def validate(rows: list[dict]) -> list[str]:
    problems = []
    brands = {r["brand"] for r in rows}
    if brands != {b["name"] for b in BRANDS.values()}:
        problems.append(f"Brands in the roster do not match the six approved brands: {sorted(brands)}")
    creators = {_uid_for(r["name"]) for r in rows}
    if None in creators:
        problems.append("Unrecognised creator(s) (no one may be added): " +
                        ", ".join(sorted({r['name'] for r in rows if _uid_for(r['name']) is None})))
    if len(creators - {None}) != 18:
        problems.append(f"Expected 18 approved creators, roster maps to {len(creators - {None})}")
    if len(rows) != 27:
        problems.append(f"Expected 27 source records, roster has {len(rows)}")
    return problems


def reconcile(settings, db, rows: list[dict] | None = None, file: Path | None = None) -> dict:
    """Apply the roster. Idempotent. Returns {'summary', 'problems', 'rows': [...]}."""
    if rows is None:
        rows = parse(Path(file or DEFAULT_FILE).read_text(encoding="utf-8"))
    problems = validate(rows)
    report = []
    if problems:
        return {"problems": problems, "rows": [], "summary": {"applied": 0, "refused": len(rows)}}
    counts = {"match": 0, "filled": 0, "conflict": 0, "missing_record": 0, "notes": 0}
    for r in rows:
        uid = _uid_for(r["name"])
        ptype = "podcast_feed" if r["platform"] == "podcast" else "channel"
        ms = db.one("SELECT * FROM monitor_sources WHERE uid=?", (f"{uid}/{r['platform']}/{ptype}",))
        item = {"creator": r["name"], "platform": r["platform"], "value": r["value"], "result": "", "detail": ""}
        if not ms:
            item.update(result="missing_record", detail="No such source in the registry (the importer never adds sources).")
            counts["missing_record"] += 1
            report.append(item)
            continue
        locked = set(json.loads(ms["owner_locked_json"] or "[]"))
        if r["platform"] == "podcast":
            names = {_norm(n) for c in CREATORS if c["uid"] == uid for s in c["sources"] if s["platform"] == "podcast" for n in s["names"]}
            want = _norm(r["value"].split(":", 1)[-1])
            item.update(result="match" if want in names else "conflict",
                        detail="feed to be discovered by exact title" if want in names else
                        f"roster title '{r['value']}' differs from the registry's expected title")
        else:
            url = r["value"]
            if r["platform"] == "rumble":
                cur = ms["canonical_url"]
                if not cur:
                    portfolio.owner_set(db, ms["id"], canonical_url=url)
                    item.update(result="filled", detail="Rumble URL recorded (locked); needs engine verification")
                elif cur.rstrip("/").lower() == url.rstrip("/").lower():
                    item.update(result="match")
                else:
                    item.update(result="conflict", detail=f"registry has {cur}; roster says {url}; nothing changed")
            else:
                h = _handle(r["platform"], url)
                cur = ms["candidate_handle"] or ""
                if not h:
                    item.update(result="conflict", detail="could not read a handle from the roster URL")
                elif cur.lower() == h.lower():
                    item.update(result="match")
                elif not cur and "candidate_handle" not in locked:
                    portfolio.owner_set(db, ms["id"], candidate_handle=h)
                    item.update(result="filled", detail=f"candidate handle {h} recorded")
                else:
                    item.update(result="conflict", detail=f"registry candidate {cur or '(none)'}; roster {h}"
                                + ("; value is locked" if "candidate_handle" in locked else "") + "; nothing changed")
        if r["evidence"] and not (ms["notes"] or "").strip():
            db.update("monitor_sources", ms["id"], notes=f"Roster evidence (not verification): {r['evidence']}"[:500])
            counts["notes"] += 1
        counts[item["result"]] += 1
        report.append(item)
    fresh = db.query("SELECT verification, COUNT(*) n FROM monitor_sources GROUP BY verification")
    return {"problems": [], "rows": report, "summary": {**counts, "verification": {x["verification"]: x["n"] for x in fresh}}}


def render(res: dict) -> str:
    if res["problems"]:
        return "Roster refused:\n  " + "\n  ".join(res["problems"])
    out = [f"{r['creator'][:34]:34} {r['platform']:8} {r['result']:14} {r['detail']}" for r in res["rows"]
           if r["result"] != "match"]
    return "\n".join(out + [f"summary: {res['summary']}"])
