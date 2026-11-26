"""Convert a researched prospect CSV into the CRM ``import-research`` JSON shape.

Scoring is deliberately conservative and fully deterministic:
  repeatable_source  2 recurring long-form owner, 1 agency/label (varies by client), 0 none
  offer_fit          confidence high=2, medium=1, low=0 (evidence = dated demand URL)
  editorial_fit      1 by default; 0 for gambling/adult/political-extremism segments (advertiser fit review)
  corporate_route    2 confirmed limited company/LLP with a published route, 1 likely corporate, 0 sole
                     trader/partnership or no route (PECR: individuals need consent for cold email)
  editing_gap        1 when dated demand evidence exists; never 2 until short-form posts are counted
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
import re

_RISKY = re.compile(r"\b(?:betting|casino|gambl|adult|onlyfans)\w*", re.I)
_FIRST_URL = re.compile(r"https?://[^\s;,)\"]+")


def _url(text: str) -> str | None:
    m = _FIRST_URL.search(text or "")
    return m.group(0) if m else None


def convert_row(row: dict) -> dict:
    seg = (row.get("segment") or "").strip().lower()
    demand_url = _url(row.get("demand_evidence_url", ""))
    longform = _url(row.get("longform_url", "")) or _url(row.get("website", ""))
    subscriber = (row.get("subscriber_type") or "").lower()
    route = (row.get("contact_route") or "").strip()
    route_ok = bool(route) and "no published" not in route.lower() and "not verified" not in route.lower()
    if "sole trader" in subscriber or "partnership" in subscriber:
        corporate = 0
    elif route_ok and re.search(r"corporate subscriber|\bltd\b|limited|llp|plc", subscriber) and "likely" not in subscriber:
        corporate = 2
    elif route_ok or "likely" in subscriber:
        corporate = 1
    else:
        corporate = 0
    risky = bool(_RISKY.search(" ".join(row.get(k, "") for k in ("name", "segment", "demand_evidence", "shortform_observation"))))
    conf = {"high": 2, "medium": 1}.get((row.get("confidence") or "").strip().lower(), 0)
    repeatable = 2 if seg in {"podcast", "livestreamer", "founder", "business", "media"} else 1 if seg in {"agency", "record_label"} else 0
    criteria = {
        "repeatable_source": {"points": repeatable if longform else 0, "evidence": longform if longform and repeatable else None},
        "offer_fit": {"points": conf if demand_url else 0, "evidence": demand_url if conf and demand_url else None},
        "editorial_fit": {"points": 0 if risky else 1,
                          "evidence": None if risky else (_url(row.get("website", "")) or longform)},
        "corporate_route": {"points": corporate, "evidence": route[:300] if corporate else None},
        "editing_gap": {"points": 1 if demand_url else 0, "evidence": demand_url},
    }
    for entry in criteria.values():
        if not entry["points"]:
            entry["evidence"] = None
        elif not entry["evidence"]:
            entry["points"] = 0
    steps = ["verify entity on Companies House"] if corporate < 2 else []
    steps.append("count recent Shorts/Reels/TikTok posts")
    if risky:
        steps.append("owner decision on advertiser/editorial fit (ASA gambling rules)")
    steps.append("owner approval of specific outreach before any contact")
    return {"organization": row["name"].strip(), "source_url": demand_url or longform or _url(row.get("website", "")),
            "next_action": "; ".join(steps), "criteria": criteria}


def convert(csv_path: Path) -> list[dict]:
    with csv_path.open(encoding="utf-8-sig", newline="") as f:
        return [convert_row(r) for r in csv.DictReader(f) if (r.get("name") or "").strip()]


def main() -> int:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("csv", type=Path)
    cli.add_argument("out", type=Path)
    a = cli.parse_args()
    data = convert(a.csv)
    a.out.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(json.dumps({"prospects": len(data)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
