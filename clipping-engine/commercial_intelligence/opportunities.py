"""Rank paid clipping opportunities by expected net contribution, not advertised CPM.

Expected views use a log-normal model (median and spread per account), which is
how short-form view counts behave: most clips earn little, a few earn a lot. The
model applies each campaign's minimum-view threshold, payout cap, remaining
budget, platform fee, deadline, eligibility and approval probabilities, then
subtracts owner review time and processing cost. All assumptions are explicit
inputs and are echoed in the output so a ranking can be audited.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import math
from pathlib import Path

ELIGIBILITY_P = {"yes": 1.0, "unknown": 0.5, "no": 0.0}
RIGHTS_P = {"campaign_licence": 1.0, "unknown": 0.0}  # no rights basis -> no contribution


@dataclass(frozen=True)
class Assumptions:
    median_views: float = 2_000.0       # per clip per platform for a young account
    sigma: float = 1.4                  # log-normal spread
    approval_p: float = 0.6             # brand/marketplace approves the clip
    payout_reliability: float = 0.9     # approved earnings actually paid
    platform_fee: float = 0.10          # marketplace fee on earnings
    clips_per_campaign: int = 6
    review_minutes_per_clip: float = 4.0
    owner_hourly_value: float = 20.0    # in campaign currency
    processing_minutes_per_clip: float = 3.0
    processing_cost_per_hour: float = 0.05  # local electricity estimate
    fx_to_gbp: float = 0.79             # USD->GBP for reporting only


def _phi(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def expected_payout(cpm: float, min_views: int | None, max_payout: float | None, median: float, sigma: float) -> float:
    """E[payout] for views ~ LogNormal(ln median, sigma); zero below threshold, capped above."""
    mu = math.log(max(median, 1.0))
    rate = cpm / 1000.0
    lo = max(min_views or 0, 1)
    hi = (max_payout / rate) if max_payout and rate > 0 else math.inf

    def partial_mean(a, b):  # E[V; a<V<b]
        def z(v):
            return math.inf if v == math.inf else (math.log(v) - mu - sigma ** 2) / sigma
        return math.exp(mu + sigma ** 2 / 2) * (_phi(z(b)) - _phi(z(a)))

    def tail(v):
        return 0.0 if v == math.inf else 1 - _phi((math.log(v) - mu) / sigma)

    if hi <= lo:
        return (max_payout or 0.0) * tail(lo)
    return rate * partial_mean(lo, hi) + (max_payout or 0.0) * tail(hi)


def score(opp: dict, a: Assumptions = Assumptions(), now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    reasons = []
    cpm = opp.get("cpm_usd")
    platforms = [p for p in opp.get("platforms") or [] if p]
    if not cpm or not platforms:
        return {**_base(opp), "expected_net": 0.0, "actionable": False, "reasons": ["rate or platforms not stated"]}
    deadline = opp.get("deadline")
    if deadline:
        try:
            due = datetime.fromisoformat(deadline.replace("Z", "+00:00"))
            if due.tzinfo is None:
                due = due.replace(hour=23, minute=59, tzinfo=timezone.utc)
            if due <= now:
                return {**_base(opp), "expected_net": 0.0, "actionable": False, "reasons": ["deadline passed"]}
        except ValueError:
            reasons.append("deadline unparseable")
    min_views = opp.get("min_payout_views")
    if min_views is None and opp.get("min_payout_usd"):
        min_views = int(opp["min_payout_usd"] / (cpm / 1000))
    per_platform = expected_payout(cpm, min_views, opp.get("max_payout_usd"), a.median_views, a.sigma)
    gross = per_platform * len(platforms) * a.clips_per_campaign
    budget = opp.get("budget_remaining_usd")
    if budget is not None:
        if gross > budget:
            reasons.append(f"capped by remaining budget {budget}")
        gross = min(gross, budget)
    p_elig = ELIGIBILITY_P.get(opp.get("uk_eligible", "unknown"), 0.5)
    p_rights = RIGHTS_P.get(opp.get("rights_basis", "unknown"), 0.0)
    if p_rights == 0:
        reasons.append("no verified rights basis")
    if p_elig < 1:
        reasons.append(f"UK eligibility {opp.get('uk_eligible', 'unknown')}")
    expected_revenue = gross * (1 - a.platform_fee) * a.approval_p * a.payout_reliability * p_elig * p_rights
    clips = a.clips_per_campaign
    cost = clips * (a.review_minutes_per_clip / 60 * a.owner_hourly_value
                    + a.processing_minutes_per_clip / 60 * a.processing_cost_per_hour)
    net = expected_revenue - cost
    return {**_base(opp), "expected_revenue": round(expected_revenue, 2), "expected_cost": round(cost, 2),
            "expected_net": round(net, 2), "expected_net_gbp": round(net * a.fx_to_gbp, 2),
            "per_clip_platform_payout": round(per_platform, 3), "effective_min_views": min_views,
            "actionable": net > 0 and p_rights > 0 and p_elig > 0, "reasons": reasons}


def _base(opp):
    return {k: opp.get(k) for k in ("id", "marketplace", "creator", "brand", "url", "cpm_usd", "platforms")}


def rank(opps: list[dict], a: Assumptions = Assumptions(), now: datetime | None = None) -> list[dict]:
    return sorted((score(o, a, now) for o in opps), key=lambda r: (-r["expected_net"], str(r["id"])))


def main() -> int:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("opportunities", type=Path)
    cli.add_argument("--median-views", type=float)
    cli.add_argument("--assume-rights", action="store_true",
                     help="Scenario only: treat unknown rights as granted (never use for decisions)")
    args = cli.parse_args()
    opps = json.loads(args.opportunities.read_text(encoding="utf-8"))
    if args.assume_rights:
        opps = [{**o, "rights_basis": "campaign_licence"} for o in opps]
    a = Assumptions(**({"median_views": args.median_views} if args.median_views else {}))
    print(json.dumps({"assumptions": asdict(a), "ranking": rank(opps, a)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
