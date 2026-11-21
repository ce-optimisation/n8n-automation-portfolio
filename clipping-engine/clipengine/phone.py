"""Phone alerts and the one-tap review queue (Session 13).

Alerts use ntfy (https://ntfy.sh, free). The alert text is deliberately GENERIC ("3 new clips ready"): no clip titles, creator
names or transcript text ever leave this computer. The topic name is the only secret needed to read the feed, so it is long and
random and is shown only on the local /phone page. Tapping an alert opens the private dashboard (``DASHBOARD_URL``), which needs
the dashboard password.

Who is shown on the phone (``phone_queue``): rendered clips awaiting review that are SAFE (no safety flag), whose creator has a
written permission on record (so the rights basis is known), and whose brand is known. Anything else is reviewed on the desktop
page, where flags and rights are handled explicitly.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

import httpx

from .publishing.autodeliver import derive_rights
from .publishing.delivery import clip_brand

log = logging.getLogger("clipengine.phone")

KEY_LAST_ID = "push.last_clip_id"
KEY_LAST_AT = "push.last_at"


def configured(settings) -> bool:
    return bool(settings.ntfy_topic)


def send(settings, title: str, message: str, click: str | None = None, *, client: httpx.Client | None = None) -> dict:
    """POST one notification to ntfy. Never raises; returns {'ok': bool, 'detail': str}."""
    if not configured(settings):
        return {"ok": False, "detail": "NTFY_TOPIC is not set."}
    headers = {"Title": title, "Tags": "clapper", "Priority": "default"}
    if click:
        headers["Click"] = click
    own = client is None
    client = client or httpx.Client(timeout=15)
    try:
        r = client.post(f"{settings.ntfy_server}/{settings.ntfy_topic}", content=message.encode("utf-8"), headers=headers)
        return {"ok": r.status_code == 200, "detail": f"HTTP {r.status_code}"}
    except httpx.HTTPError as e:
        return {"ok": False, "detail": f"network problem ({type(e).__name__})"}
    finally:
        if own:
            client.close()


def phone_queue(db) -> list:
    """Clips the owner can approve with one tap, in a BALANCED order (Session 15): the brands take turns, so one busy brand
    (Afterlive makes about 70% of the clips) cannot bury the others. Brands go in order of how long their oldest clip has
    waited; within a brand the highest-scoring clip comes first, then the oldest."""
    by_brand: dict[str, list] = {}
    for c in db.query("SELECT * FROM clips WHERE status='ready' AND safety_status='clear' AND video_path IS NOT NULL "
                      "ORDER BY id"):
        if not c["video_path"] or not Path(c["video_path"]).is_file():
            continue
        brand = clip_brand(db, c)
        if not brand or not derive_rights(db, c):
            continue
        by_brand.setdefault(brand, []).append(c)
    for clips in by_brand.values():
        clips.sort(key=lambda c: (-(c["score"] if c["score"] is not None else 0.0), c["id"]))
    order = sorted(by_brand, key=lambda b: min(c["id"] for c in by_brand[b]))
    out = []
    for i in range(max((len(v) for v in by_brand.values()), default=0)):
        for b in order:
            if i < len(by_brand[b]):
                out.append(by_brand[b][i])
    return out


def queue_counts(db) -> list[tuple[str, int]]:
    """(brand name, waiting clips) for the header of the review page, biggest first."""
    counts: dict[str, int] = {}
    for c in phone_queue(db):
        counts[clip_brand(db, c)] = counts.get(clip_brand(db, c), 0) + 1
    names = {r["key"]: r["name"] for r in db.query("SELECT key, name FROM brands")}
    return sorted(((names.get(k, k), n) for k, n in counts.items()), key=lambda x: -x[1])


def in_quiet_hours(settings, now: float | None = None) -> bool:
    spec = (settings.push_quiet_hours or "").strip()
    if "-" not in spec:
        return False
    try:
        a, b = [s.strip() for s in spec.split("-", 1)]
        ah, am = [int(x) for x in a.split(":")]
        bh, bm = [int(x) for x in b.split(":")]
    except ValueError:
        return False
    t = time.localtime(now or time.time())
    cur, start, end = t.tm_hour * 60 + t.tm_min, ah * 60 + am, bh * 60 + bm
    return (start <= cur < end) if start <= end else (cur >= start or cur < end)


def digest_tick(settings, db, now: float | None = None, client: httpx.Client | None = None) -> dict | None:
    """Send ONE batched alert when new phone-reviewable clips have appeared. Safe to call every minute."""
    if not configured(settings):
        return None
    now = now or time.time()
    last_id = int(db.get_kv(KEY_LAST_ID, "0") or 0)
    queue = phone_queue(db)
    new = [c for c in queue if c["id"] > last_id]
    if not new or in_quiet_hours(settings, now):
        return None
    last_at = float(db.get_kv(KEY_LAST_AT, "0") or 0)
    if now - last_at < settings.push_min_gap_minutes * 60 and len(new) < 5:
        return None                       # batch: wait for the gap to pass unless a lot has piled up
    n, total = len(new), len(queue)
    msg = f"{n} new clip{'s' if n != 1 else ''} ready to review" + (f" ({total} waiting in all)" if total != n else "")
    link = f"{settings.dashboard_url}/m" if settings.dashboard_url else None
    res = send(settings, "Clipping Engine", msg, link, client=client)
    if res["ok"]:
        db.set_kv(KEY_LAST_ID, str(max(c["id"] for c in new)))
        db.set_kv(KEY_LAST_AT, str(now))
    else:
        log.warning("phone alert not sent: %s", res["detail"])
    return {"sent": res["ok"], "new": n, "waiting": total, "detail": res["detail"]}


def start_ticker(settings, db, stop_event, interval: float = 60.0):
    import threading

    def loop():
        while not stop_event.wait(interval):
            try:
                digest_tick(settings, db)
            except Exception:  # noqa: BLE001 - an alert problem must never disturb the engine
                log.exception("phone alert tick failed")
    t = threading.Thread(target=loop, name="clipengine-phone-alerts", daemon=True)
    t.start()
    return t
