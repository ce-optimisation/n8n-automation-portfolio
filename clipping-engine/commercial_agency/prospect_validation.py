"""Deterministic pre-send validation of researched prospects (Session 21).

A successful run is NOT proof of commercial eligibility: it only applies explicit rules to the recorded evidence and
says exactly what still blocks each prospect. Rules:
  * excluded: do-not-contact basis, or the opportunity is closed / filled / removed;
  * corporate-subscriber marketing needs a verified ACTIVE company; otherwise hold;
  * possible sole traders / individuals: reply to their own posting only (PECR: no cold email);
  * US recipients: CAN-SPAM needs a physical postal address in the message;
  * caption-required work: client captions only with the accuracy-review workflow (owner policy);
  * evidence older than MAX_AGE_DAYS must be rechecked before sending;
  * every send needs cleared portfolio examples and the owner's specific approval.
"""
from __future__ import annotations

import argparse
import csv
from datetime import date, datetime
import json
from pathlib import Path
import re

MAX_AGE_DAYS = 3
_CLOSED = re.compile(r"\b(closed|filled|removed|expired|404|no longer accepting)\b", re.I)
_INDIVIDUAL = re.compile(r"sole trader|individual|unincorporated|possible individual", re.I)
_ACTIVE = re.compile(r"^\s*active\b", re.I)
_CAPTIONS = re.compile(r"caption|subtit", re.I)


def _norm(name: str) -> str:
    return re.sub(r"\b(ltd|limited|llc|inc|plc|llp)\b|[^a-z0-9]", "", name.lower())


def _date(text: str) -> date | None:
    m = re.search(r"(20\d\d)-(\d\d)-(\d\d)", text or "")
    return date(*map(int, m.groups())) if m else None


def validate_row(row: dict, *, today: date, cleared_collections: set[str]) -> dict:
    basis = (row.get("contact_basis") or "").strip().lower()
    status = row.get("opportunity_status") or ""
    entity = row.get("entity_status") or ""
    blockers, notes = [], []
    if basis.startswith("do-not-contact") or (_CLOSED.search(status) and not re.search(r"\bopen\b", status, re.I)):
        return {"name": row["name"], "decision": "excluded", "blockers": [basis or status], "notes": []}
    if basis.startswith("hold"):
        blockers.append("held: " + basis)
    route = "respond-to-posting" if basis.startswith("respond-to-posting") else \
        "corporate-marketing" if "corporate-subscriber" in basis else "unclear"
    if route == "unclear":
        blockers.append("no recorded contact basis")
    individual = bool(_INDIVIDUAL.search(entity + " " + basis))
    if route == "corporate-marketing" and (individual or not _ACTIVE.search(entity)):
        blockers.append("corporate marketing needs a verified active company (Companies House)")
    if individual:
        notes.append("possible individual subscriber: reply to their own posting only")
    if (row.get("country") or "").upper().startswith("US"):
        notes.append("US recipient: CAN-SPAM requires a physical postal address in the email")
    if _CAPTIONS.search(row.get("caption_requirement") or "") and not re.search(r"not stated|n/a|unverified",
                                                                              row.get("caption_requirement") or "", re.I):
        notes.append("client requires captions: use the reviewed-caption workflow; never deliver unchecked captions")
    checked = _date(row.get("status_checked_at") or "")
    if not checked or (today - checked).days > MAX_AGE_DAYS:
        blockers.append(f"recheck opportunity status (last checked {checked or 'unknown'})")
    cat = (row.get("portfolio_category") or "").strip().lower()
    if not any(c in cat or cat in c for c in cleared_collections if c) if cat else True:
        blockers.append(f"no cleared portfolio examples for '{cat or 'unspecified'}' yet")
    blockers.append("owner approval of the specific message")
    if "account" in (row.get("route") or "").lower() or re.search(r"YT Jobs|Twine|Upwork|Contra", row.get("route") or ""):
        notes.append("route needs a platform account in the owner's name")
    decision = "ready_after_owner_approval" if blockers == ["owner approval of the specific message"] else "blocked"
    return {"name": row["name"], "decision": decision, "route": route, "blockers": blockers, "notes": notes,
            "status": status, "category": cat}


def validate(csv_path: Path, *, today: date | None = None, cleared_collections: set[str] | None = None) -> dict:
    today = today or date.today()
    with csv_path.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    seen, results = {}, []
    for row in rows:
        key = _norm(row.get("name", ""))
        r = validate_row(row, today=today, cleared_collections=cleared_collections or set())
        if key in seen:
            r = {**r, "decision": "excluded", "blockers": [f"duplicate of {seen[key]}"]}
        seen.setdefault(key, row["name"])
        results.append(r)
    summary = {}
    for r in results:
        summary[r["decision"]] = summary.get(r["decision"], 0) + 1
    return {"validated_at": datetime.now().isoformat(timespec="seconds"), "summary": summary, "prospects": results}


def main() -> int:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("csv", type=Path)
    cli.add_argument("--cleared", default="", help="Comma-separated cleared portfolio collections")
    cli.add_argument("--out", type=Path)
    a = cli.parse_args()
    report = validate(a.csv, cleared_collections={c.strip().lower() for c in a.cleared.split(",") if c.strip()})
    if a.out:
        a.out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report["summary"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
