"""Temporary, configurable event priorities (e.g. a game launch) and the polling-interval policy.

An event only changes HOW OFTEN a brand's sources are looked at (never below a platform-safe floor) and
tags matching items as a review signal. It never touches acquisition permissions, never starts jobs,
never switches other brands off, and expires on its own: after `ends_at` the normal intervals return.
Rules live in the `priority_events` table, so a future launch is a row, not a code change.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone

# Normal polling (seconds) per platform, and the floors that keep us inside platform limits.
BASE_INTERVAL = {"youtube": 20 * 60, "twitch": 5 * 60, "kick": 5 * 60, "rumble": 5 * 60, "podcast": 60 * 60}
FLOOR_INTERVAL = {"youtube": 5 * 60, "twitch": 60, "kick": 60, "rumble": 5 * 60, "podcast": 15 * 60}
LIVE_INTERVAL = {"youtube": 5 * 60, "twitch": 90, "kick": 90, "rumble": 5 * 60, "podcast": 60 * 60}
IMMINENT_WINDOW_S = 30 * 60          # a verified scheduled start within 30 minutes counts as imminent
IMMINENT_GRACE_S = 3 * 3600          # ...and stays interesting for 3 hours after the scheduled time

GTA_VI = dict(
    key="gta_vi_launch", name="GTA VI launch", brand_key="afterlive",
    event_date="2026-11-19", starts_at="2026-11-17T00:00:00+00:00", ends_at="2026-11-26T00:00:00+00:00",
    poll_seconds=300, live_poll_seconds=90,
    match_terms=["gta vi", "gta 6", "gta6", "gtavi", "grand theft auto vi", "grand theft auto 6"])


def parse_ts(v) -> float | None:
    if not v:
        return None
    try:
        d = datetime.fromisoformat(str(v).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()


def seed_events(db) -> int:
    """Insert the built-in events if absent. Never overwrites an owner-edited row."""
    n = 0
    t = time.time()
    for e in (GTA_VI,):
        if not db.one("SELECT 1 FROM priority_events WHERE key=?", (e["key"],)):
            db.execute("INSERT INTO priority_events(key,name,brand_key,starts_at,ends_at,event_date,poll_seconds,"
                       "live_poll_seconds,match_terms_json,enabled,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,1,?,?)",
                       (e["key"], e["name"], e["brand_key"], e["starts_at"], e["ends_at"], e["event_date"],
                        e["poll_seconds"], e["live_poll_seconds"], json.dumps(e["match_terms"]), t, t))
            n += 1
    return n


def active_events(db, now: float, brand_key: str | None = None) -> list[dict]:
    out = []
    for r in db.query("SELECT * FROM priority_events WHERE enabled=1"):
        a, b = parse_ts(r["starts_at"]), parse_ts(r["ends_at"])
        if a is None or b is None or not (a <= now < b):
            continue
        if brand_key and r["brand_key"] != brand_key:
            continue
        out.append(dict(r))
    return out


def tag_item(db, brand_key: str, now: float, *texts: str) -> list[str]:
    """Event keys whose terms appear in the item's own metadata (title, game/category, description).
    A signal for review ordering only; an item is never classified by its creator alone."""
    hay = " ".join(t for t in texts if t).lower()
    tags = []
    for ev in active_events(db, now, brand_key):
        terms = json.loads(ev["match_terms_json"] or "[]")
        if any(t in hay for t in terms):
            tags.append(ev["key"])
    return tags


def interval_for(db, ms, now: float, *, live: bool = False, imminent: bool = False) -> int:
    """Seconds until this source is looked at again."""
    p = ms["platform"]
    secs = LIVE_INTERVAL.get(p, 300) if (live or imminent) else BASE_INTERVAL.get(p, 600)
    for ev in active_events(db, now, ms["brand_key"]):
        secs = min(secs, ev["live_poll_seconds"] if (live or imminent) else ev["poll_seconds"])
    return max(secs, FLOOR_INTERVAL.get(p, 60))


def is_imminent(items, now: float) -> bool:
    for it in items:
        if it.kind == "upcoming":
            t = parse_ts(it.published)
            if t is not None and -IMMINENT_GRACE_S <= t - now <= IMMINENT_WINDOW_S:
                return True
    return False
