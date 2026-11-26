"""Campaign profitability from recorded facts only.

Keeps four money states apart and never converts one into another:
  * estimated  - from versioned assessments (scenarios, not money)
  * pending    - submissions approved by the campaign with no receipt yet (count only)
  * received   - actual receipts, gross and fees, per currency
  * costs      - recorded expenses per currency, plus owner/processing minutes

Owner time is valued only when the caller supplies an hourly rate, and the
valuation is reported separately from cash profit.
"""

from __future__ import annotations

from collections import defaultdict
import sqlite3

COST_KINDS = ("expense", "owner_minutes", "processing_minutes")

COSTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS campaign_costs (
    id INTEGER PRIMARY KEY,
    brief_id INTEGER REFERENCES campaign_briefs(id),
    clip_id INTEGER,
    kind TEXT NOT NULL CHECK (kind IN ('expense','owner_minutes','processing_minutes')),
    currency TEXT CHECK (currency IS NULL OR length(currency)=3),
    amount_minor INTEGER CHECK (amount_minor IS NULL OR amount_minor>=0),
    minutes REAL CHECK (minutes IS NULL OR minutes>=0),
    note TEXT NOT NULL,
    recorded_at REAL NOT NULL,
    CHECK ((kind='expense' AND currency IS NOT NULL AND amount_minor IS NOT NULL AND minutes IS NULL)
        OR (kind<>'expense' AND minutes IS NOT NULL AND amount_minor IS NULL))
);
"""


def validate_cost(kind: str, *, currency: str | None, amount_minor: int | None, minutes: float | None, note: str):
    if kind not in COST_KINDS:
        raise ValueError("Unknown cost kind.")
    if not note or not note.strip() or len(note) > 500:
        raise ValueError("Describe the cost in up to 500 characters.")
    if kind == "expense":
        if not currency or len(currency) != 3 or not currency.isalpha() or amount_minor is None or amount_minor < 0:
            raise ValueError("An expense needs a three-letter currency and a non-negative amount.")
        if minutes is not None:
            raise ValueError("An expense has no minutes.")
    elif minutes is None or minutes < 0 or minutes > 24 * 60 or amount_minor is not None:
        raise ValueError("Time entries need minutes between 0 and 1440 and no amount.")


def report(conn: sqlite3.Connection, *, hourly_rate_minor: int | None = None, rate_currency: str | None = None) -> dict:
    """Per-campaign, per-clip and per-creator facts. ``conn`` rows must be sqlite3.Row."""
    q = conn.execute
    camp: dict = defaultdict(lambda: {"received": defaultdict(lambda: [0, 0]), "expenses": defaultdict(int),
                                      "owner_minutes": 0.0, "processing_minutes": 0.0, "submitted": 0,
                                      "approved_pending": 0, "rejected": 0, "clips": set()})
    clips: dict = defaultdict(lambda: {"received": defaultdict(lambda: [0, 0]), "expenses": defaultdict(int),
                                       "owner_minutes": 0.0, "processing_minutes": 0.0})
    for s in q("SELECT s.id, s.brief_id, s.clip_id, s.status, "
               "(SELECT COUNT(*) FROM campaign_receipts r WHERE r.submission_id=s.id) AS receipts "
               "FROM campaign_submissions s"):
        c = camp[s["brief_id"]]
        c["clips"].add(s["clip_id"])
        c["submitted"] += 1
        if s["status"] == "approved" and not s["receipts"]:
            c["approved_pending"] += 1
        if s["status"] == "rejected":
            c["rejected"] += 1
    for r in q("SELECT s.brief_id, s.clip_id, r.currency, r.gross_minor, r.fee_minor FROM campaign_receipts r "
               "JOIN campaign_submissions s ON s.id=r.submission_id"):
        for bucket in (camp[r["brief_id"]], clips[r["clip_id"]]):
            bucket["received"][r["currency"]][0] += r["gross_minor"]
            bucket["received"][r["currency"]][1] += r["fee_minor"]
    has_costs = q("SELECT 1 FROM sqlite_master WHERE name='campaign_costs'").fetchone()
    for k in (q("SELECT * FROM campaign_costs") if has_costs else []):
        targets = ([camp[k["brief_id"]]] if k["brief_id"] is not None else []) + \
                  ([clips[k["clip_id"]]] if k["clip_id"] is not None else [])
        for t in targets:
            if k["kind"] == "expense":
                t["expenses"][k["currency"]] += k["amount_minor"]
            else:
                t[k["kind"]] += k["minutes"]

    def money(bucket):
        out = {}
        for cur in set(bucket["received"]) | set(bucket["expenses"]):
            gross, fee = bucket["received"].get(cur, (0, 0))
            exp = bucket["expenses"].get(cur, 0)
            out[cur] = {"gross_minor": gross, "fees_minor": fee, "net_receipts_minor": gross - fee,
                        "expenses_minor": exp, "cash_profit_minor": gross - fee - exp}
        time_cost = None
        if hourly_rate_minor and rate_currency:
            time_cost = {"currency": rate_currency,
                         "owner_time_value_minor": round(bucket["owner_minutes"] / 60 * hourly_rate_minor)}
        return {"by_currency": out, "owner_minutes": round(bucket["owner_minutes"], 1),
                "processing_minutes": round(bucket["processing_minutes"], 1), "owner_time_valuation": time_cost}

    campaigns = {}
    for brief_id, c in camp.items():
        if brief_id is None:
            continue
        campaigns[brief_id] = {**money(c), "submitted": c["submitted"], "approved_pending_payment": c["approved_pending"],
                               "rejected": c["rejected"], "clips": len(c["clips"])}
    creators: dict = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    for r in q("SELECT COALESCE(cr.name, 'Unattributed') AS creator, rc.currency, rc.gross_minor, rc.fee_minor "
               "FROM campaign_receipts rc JOIN campaign_submissions s ON s.id=rc.submission_id "
               "LEFT JOIN clips c ON c.id=s.clip_id LEFT JOIN recordings rec ON rec.id=c.recording_id "
               "LEFT JOIN creators cr ON cr.id=rec.creator_id"):
        creators[r["creator"]][r["currency"]][0] += r["gross_minor"]
        creators[r["creator"]][r["currency"]][1] += r["fee_minor"]
    return {"campaigns": campaigns, "clips": {k: money(v) for k, v in clips.items()},
            "creators": {name: {cur: {"gross_minor": g, "net_minor": g - f} for cur, (g, f) in curs.items()}
                         for name, curs in creators.items()},
            "notes": ["Cash figures are actual receipts minus recorded fees and expenses only.",
                      "Approved submissions without a receipt are counted, never valued."]}
