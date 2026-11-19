"""Owner-commanded delivery of ONE clip to ONE verified destination's closest supported draft workflow (Session 13).

Every gate must pass, in this order, and nothing here ever publishes:
  1. the clip passes the editorial gate (approved, rights recorded, safety decision recorded, file present);
  2. the clip's own brand equals the destination's brand (a shared administrator login never bridges brands);
  3. the destination's identity was verified through the platform API, its per-destination publishing switch is ON, and the
     caller passes ``owner_command=True`` (an explicit owner action, never a background job);
  4. the platform-specific step re-checks the stored token against the verified account again immediately before uploading.

Instagram is refused here by design: its API has no drafts and ``media_publish`` is PUBLIC (use the staging/export package).
"""
from __future__ import annotations

import time
from pathlib import Path

from .. import connections as cx
from .base import PublishError, editorial_gate


def clip_brand(db, clip) -> str | None:
    """Brand a clip belongs to: via its recording's creator, else the recording/clip niche."""
    rec = db.one("SELECT * FROM recordings WHERE id=?", (clip["recording_id"],))
    cr = db.one("SELECT * FROM creators WHERE id=?", (rec["creator_id"],)) if rec and rec["creator_id"] else None
    if cr and cr["brand_key"]:
        return cr["brand_key"]
    niche = (rec["niche_key"] if rec else None) or clip["niche_key"]
    row = db.one("SELECT key FROM brands WHERE niche_key=?", (niche,)) if niche else None
    return row["key"] if row else None


def deliver_draft(db, clients: dict, clip_id: int, account_id: int, *, owner_command: bool = False) -> dict:
    clip = db.clip(clip_id)
    acct = db.one("SELECT * FROM social_accounts WHERE id=?", (account_id,))
    if not clip or not acct:
        raise PublishError("Unknown clip or destination.")
    problems = list(editorial_gate(clip))
    problems += cx.routing_problems(db, clip_brand(db, clip), account_id, explicit_owner_command=owner_command)
    if acct["platform"] == "instagram":
        problems.append("Instagram has no draft API (media_publish is public): use the local staging/export package.")
    if acct["platform"] not in ("youtube", "facebook"):
        problems.append(f"{acct['platform']} delivery is handled by the existing publishing service, not here.")
    if problems:
        raise PublishError(" ".join(problems))
    dup = db.one("SELECT * FROM publish_attempts WHERE clip_id=? AND platform=? AND status IN "
                 "('delivered_draft','delivered_private','processing','uploading')", (clip_id, acct["platform"]))
    if dup:
        raise PublishError(f"This clip was already sent to {acct['platform']} ({dup['status']}).")
    aid = db.insert("publish_attempts", clip_id=clip_id, platform=acct["platform"], status="uploading",
                    detail=f"Owner-commanded draft delivery to {acct['brand']} ({acct['handle']})", stage="initiated")
    try:
        video = Path(clip["video_path"])
        if acct["platform"] == "youtube":
            res = clients["youtube"].upload_private(db, account_id, video, clip["title"] or "Clip", clip["description"] or "")
            status, remote = "delivered_private", res["video_id"]
            detail = "Uploaded to YouTube as PRIVATE (not public). Review and publish manually in YouTube Studio."
        else:
            res = clients["facebook"].create_draft_reel(db, account_id, video, clip["description"] or "")
            status, remote = "delivered_draft", res["video_id"]
            detail = "Created as a Facebook Reel DRAFT (not published). Finish it on the Page yourself."
    except Exception as e:                                              # noqa: BLE001
        db.update("publish_attempts", aid, status="failed", detail=str(e)[:500], updated_at=time.time())
        raise
    db.update("publish_attempts", aid, status=status, remote_id=remote, detail=detail, stage="delivered",
              confirmed_at=time.time(), updated_at=time.time())
    return {"attempt_id": aid, "status": status, "remote_id": remote, "detail": detail}
