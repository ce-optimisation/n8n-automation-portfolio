"""Commercial discovery: niche scorecard model and loading of the research dataset into SQLite.

Discovery is deliberately separate from authorisation (owner instruction, 16 Nov 2026): licensing,
written permission or creator consent are NOT scoring criteria. A high score never enables retrieval;
research creators are stored with status 'research' and no source.

Scores are 0-10 per criterion (assessments, with sources and observation dates kept beside them).
Two rankings are produced, because reach is not revenue:
  audience  how much short-form viewership the niche can plausibly generate
  profit    how much sustainable revenue it can produce relative to operating cost and owner time
  overall   0.4 x audience + 0.6 x profit (commercial value deliberately outweighs raw reach)
"""
from __future__ import annotations

import json
import time
from pathlib import Path

AUDIENCE = {   # criterion: weight (inverse criteria are already expressed so that higher = better)
    "demand_growth": 1.0, "shortform_potential": 1.2, "material_supply": 1.0, "clippable_frequency": 1.2,
    "low_competition": 0.8, "proven_shortform": 1.0,
}
PROFIT = {
    "advertiser_value": 1.2, "creator_service_demand": 1.2, "affiliate_leadgen": 1.0, "revenue_beyond_payouts": 1.0,
    "low_policy_risk": 1.0,
    # operational cost of serving the niche (higher = cheaper / easier)
    "low_editing_need": 0.6, "automation_suitability": 0.8, "low_compute": 0.4, "low_oversight": 0.6,
    "owner_synergy": 0.8,
}
CRITERIA_HELP = {
    "demand_growth": "Audience demand and growth", "shortform_potential": "Short-form viewing potential",
    "material_supply": "Frequent long-form/livestream material", "clippable_frequency": "Naturally clippable moments",
    "low_competition": "Competition among clip accounts (10 = little)", "proven_shortform": "Evidence of successful short-form channels",
    "advertiser_value": "Advertiser and sponsor value", "creator_service_demand": "Creator-service / clipping-campaign demand",
    "affiliate_leadgen": "Affiliate and lead-generation opportunity", "revenue_beyond_payouts": "Revenue beyond platform payouts",
    "low_policy_risk": "Monetisation-policy risk (10 = low)", "low_editing_need": "Editing/context needed (10 = little)",
    "automation_suitability": "Suitability for local automated selection", "low_compute": "Processing resources (10 = light)",
    "low_oversight": "Owner oversight needed (10 = little)", "owner_synergy": "Synergy with the owner's businesses",
}


def _wmean(scores: dict, weights: dict) -> float:
    num = sum(float(scores[k]) * w for k, w in weights.items() if k in scores)
    den = sum(w for k, w in weights.items() if k in scores)
    return (num / den) * 10 if den else 0.0


def combine_scores(scores: dict) -> tuple[float, float, float]:
    """(audience 0-100, profit 0-100, overall 0-100)."""
    a = _wmean(scores, AUDIENCE)
    p = _wmean(scores, PROFIT)
    return round(a, 1), round(p, 1), round(0.4 * a + 0.6 * p, 1)


def load_dataset(db, path: Path) -> dict:
    """Load (or refresh) niche scores and research creators from the research JSON. Never touches creators
    the owner has moved beyond research (shortlisted/active/...), never creates sources."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    n_scores = n_creators = 0
    for key, item in (data.get("niches") or {}).items():
        a, p, o = combine_scores(item["scores"])
        db.execute("INSERT OR REPLACE INTO niche_scores(key, scores_json, audience_score, profit_score, overall, rationale, "
                   "sources_json, observed, confidence, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                   (key, json.dumps(item["scores"]), a, p, o, item.get("rationale", ""),
                    json.dumps(item.get("sources", [])), item.get("observed", data.get("observed")),
                    item.get("confidence", ""), time.time()))
        n_scores += 1
    for c in data.get("creators") or []:
        url = c.get("url") or None
        row = db.one("SELECT * FROM creators WHERE url=?", (url,)) if url else \
            db.one("SELECT * FROM creators WHERE name=? AND url IS NULL", (c["name"],))
        fields = dict(name=c["name"], platform=c.get("platform"), niches_json=json.dumps(c.get("niches", [])),
                      platforms_json=json.dumps(c.get("platforms", [])),
                      stats_json=json.dumps({"research": c.get("research", {})}),
                      sources_json=json.dumps(c.get("sources", [])), commercial_score=c.get("score"),
                      notes=c.get("notes", ""),
                      profile_json=json.dumps(c.get("profile", {})))
        if row:
            if row["status"] in ("research",):
                db.update("creators", row["id"], **fields)
        else:
            db.insert("creators", url=url, status="research", permission_json="{}", **fields)
            n_creators += 1
    return {"niche_scores": n_scores, "creators_added": n_creators}
