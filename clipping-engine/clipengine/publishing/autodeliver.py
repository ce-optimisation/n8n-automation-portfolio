"""Approve -> automatic upload to each destination (Session 13).

The owner's approval of a clip (one tap) is the explicit command. Per destination (``delivery.mode_for``):
  * DRAFT mode (default): Facebook Reel DRAFT / YouTube PRIVATE upload;
  * PUBLIC mode (owner typed PUBLISH for that one destination): the approved clip is published there;
  * Instagram -> not handled here yet (no draft API; local staging package);  * TikTok -> not handled here.

A clip is queued ONLY for destinations that are (a) for the clip's own brand, (b) identity-verified through the platform API and
(c) switched ON by the owner on the Connections page. ``delivery.deliver_draft`` re-checks every gate again when the job runs.
Uploads are paced: at most ``delivery_daily_cap`` per channel per local day; the rest wait for the next day, in approval order.
The AUTO-UPLOAD master switch (header button) pauses everything without losing the queue.
"""
from __future__ import annotations

import logging
import time

import httpx

from .. import connections as cx
from .. import recording_switch
from ..jobs import PermanentError, TransientError
from .base import PublishError, editorial_gate
from .delivery import clip_brand, deliver_draft, mode_for

log = logging.getLogger("clipengine.autodeliver")

KIND = "deliver_clip"
PLATFORMS = ("facebook", "youtube")
SENT = ("delivered_draft", "delivered_private", "uploading", "processing")


def derive_rights(db, clip) -> str | None:
    """'licensed' when the clip's recording belongs to a creator whose written permission the owner recorded (not expired)."""
    from ..portfolio import creator_permitted
    rec = db.one("SELECT creator_id FROM recordings WHERE id=?", (clip["recording_id"],))
    if not rec or not rec["creator_id"]:
        return None
    cr = db.one("SELECT permission_json FROM creators WHERE id=?", (rec["creator_id"],))
    return "licensed" if creator_permitted(cr) else None


def destinations_for(db, clip) -> list:
    brand = clip_brand(db, clip)
    if not brand:
        return []
    return [a for a in db.query("SELECT * FROM social_accounts WHERE brand=? AND platform IN ('facebook','youtube') "
                                "AND publishing_enabled=1 AND identity_verified_at IS NOT NULL", (brand,))
            if a["verified_platform_id"] and str(a["verified_platform_id"]) == str(a["platform_id"])]


def manual_platforms(db, clip) -> list[str]:
    """Platforms the owner must still post by hand (no draft API)."""
    brand = clip_brand(db, clip)
    rows = db.query("SELECT platform FROM social_accounts WHERE brand=? AND platform IN ('instagram','tiktok')", (brand,)) if brand else []
    return sorted({r["platform"] for r in rows})


def prepare_and_enqueue(db, queue, clip_id: int) -> dict:
    """Called right after the owner approves a clip. Records rights from the owner's permission evidence when possible, then
    queues one upload job per enabled destination. Returns what happened, for display."""
    clip = db.clip(clip_id)
    out = {"queued": [], "reason": ""}
    if not clip or clip["status"] != "approved":
        out["reason"] = "Clip is not approved."
        return out
    if clip["rights_status"] in (None, "", "unreviewed"):
        derived = derive_rights(db, clip)
        if derived:
            db.update("clips", clip_id, rights_status=derived)
            clip = db.clip(clip_id)
    problems = editorial_gate(clip)
    if problems:
        out["reason"] = " ".join(problems)
        return out
    dests = destinations_for(db, clip)
    if not dests:
        out["reason"] = "No destination is switched on for this brand yet (Connections page)."
        return out
    for a in dests:
        jid = queue.enqueue(KIND, clip_id=clip_id, payload={"clip_id": clip_id, "account_id": a["id"]},
                            dedupe_key=f"deliver:{clip_id}:{a['id']}", max_attempts=400)
        out["queued"].append({"job": jid, "platform": a["platform"], "handle": a["handle"], "brand": a["brand"],
                              "account_id": a["id"]})
    return out


def _local_midnight(now: float) -> float:
    t = time.localtime(now)
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))


def sent_today(db, account, now: float | None = None) -> int:
    now = now or time.time()
    since = _local_midnight(now)
    n = 0
    for r in db.query("SELECT pa.clip_id FROM publish_attempts pa WHERE pa.platform=? AND pa.status IN "
                      "('delivered_draft','delivered_private','published','uploading','processing') AND pa.created_at>=?",
                      (account["platform"], since)):
        clip = db.clip(r["clip_id"])
        if clip and clip_brand(db, clip) == account["brand"]:
            n += 1
    return n


def handle(settings, db, clients: dict, ctx) -> None:
    """Job handler for ``deliver_clip``."""
    cid, aid = ctx.payload["clip_id"], ctx.payload["account_id"]
    if not recording_switch.delivery_enabled(db):
        raise TransientError("Auto-upload is switched OFF; waiting.", retry_after=900)
    clip = db.clip(cid)
    acct = db.one("SELECT * FROM social_accounts WHERE id=?", (aid,))
    if not clip or not acct or clip["status"] != "approved":
        ctx.log("Clip is no longer approved (or the destination is gone); nothing to upload.")
        return
    if not acct["publishing_enabled"]:
        ctx.log("This destination was switched off; nothing uploaded.")
        return
    cap = int(getattr(settings, "delivery_daily_cap", 6) or 6)
    if sent_today(db, acct) >= cap:
        wait = max(300.0, min(6 * 3600.0, _local_midnight(time.time()) + 86400 + 60 - time.time()))
        raise TransientError(f"Daily cap of {cap} reached for {acct['brand']} {acct['platform']}; waiting for tomorrow.",
                             retry_after=wait)
    try:
        res = deliver_draft(db, clients, cid, aid, owner_command=True)
    except PublishError as e:
        if "already sent" in str(e):
            ctx.log(str(e))
            return
        raise PermanentError(str(e)) from e
    except httpx.HTTPError as e:
        raise TransientError(f"Network problem while uploading ({type(e).__name__}); will retry.", retry_after=300) from e
    ctx.log(f"{acct['platform']} {acct['handle']}: {res['status']} ({res['remote_id']})")
