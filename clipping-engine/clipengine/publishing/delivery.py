"""Owner-commanded delivery of ONE clip to ONE verified destination (Session 13).

Default per destination = DRAFT mode: Facebook Reel draft / YouTube private upload; nothing is made public. The owner can switch a
single destination to PUBLIC mode (typed PUBLISH on the Connections page); then an approved clip is published there. Public mode
is bounded by the platforms: YouTube keeps uploads private until the API project passes Google's audit, and Facebook posts from an
app still in development mode may be visible only to people with a role on the app. The result always records what really happened.

Every gate must pass, in this order, in BOTH modes:
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


MODES = ("draft", "public")


def mode_for(db, account_id: int) -> str:
    """'draft' (default: Facebook Reel draft / YouTube private) or 'public' (owner chose to publish approved clips here)."""
    v = db.get_kv(f"delivery.mode.{account_id}", "draft") or "draft"
    return v if v in MODES else "draft"


def set_mode(db, account_id: int, mode: str) -> None:
    if mode not in MODES:
        raise ValueError("mode must be draft or public")
    db.set_kv(f"delivery.mode.{account_id}", mode)


def clip_brand(db, clip) -> str | None:
    """Brand a clip belongs to: via its recording's creator, else the recording/clip niche."""
    rec = db.one("SELECT * FROM recordings WHERE id=?", (clip["recording_id"],))
    cr = db.one("SELECT * FROM creators WHERE id=?", (rec["creator_id"],)) if rec and rec["creator_id"] else None
    if cr and cr["brand_key"]:
        return cr["brand_key"]
    niche = (rec["niche_key"] if rec else None) or clip["niche_key"]
    row = db.one("SELECT key FROM brands WHERE niche_key=?", (niche,)) if niche else None
    return row["key"] if row else None


def publish_title(db, clip) -> str | None:
    """Title for the platform listing. The owner's/accepted clip title, else the SOURCE video's own title (accurate by
    construction), else None: a clip is never published under a made-up generic name like "Clip" (Session 15)."""
    t = " ".join((clip["title"] or "").split())
    if t:
        return t[:100]
    rec = db.one("SELECT title FROM recordings WHERE id=?", (clip["recording_id"],))
    src = " ".join(((rec["title"] if rec else "") or "").split())
    return src[:100] or None


def deliver_draft(db, clients: dict, clip_id: int, account_id: int, *, owner_command: bool = False) -> dict:
    clip = db.clip(clip_id)
    acct = db.one("SELECT * FROM social_accounts WHERE id=?", (account_id,))
    if not clip or not acct:
        raise PublishError("Unknown clip or destination.")
    problems = list(editorial_gate(clip))
    if acct["platform"] == "youtube" and not publish_title(db, clip):
        problems.append("This clip has no title and its source has none either: write a title before sending it to YouTube.")
    problems += cx.routing_problems(db, clip_brand(db, clip), account_id, explicit_owner_command=owner_command)
    if acct["platform"] == "instagram":
        ig = clients.get("instagram")
        if not (ig is not None and getattr(ig, "public_enabled", False) and mode_for(db, account_id) == "public"):
            problems.append("Instagram has no draft step (publishing is public and immediate): it needs INSTAGRAM_PUBLIC_PUBLISH "
                            "on AND this destination set to public mode.")
    if acct["platform"] not in ("youtube", "facebook", "instagram"):
        problems.append(f"{acct['platform']} delivery is handled by the existing publishing service, not here.")
    if problems:
        raise PublishError(" ".join(problems))
    dup = db.one("SELECT * FROM publish_attempts WHERE clip_id=? AND platform=? AND status IN "
                 "('delivered_draft','delivered_private','published','processing','uploading')", (clip_id, acct["platform"]))
    if dup:
        raise PublishError(f"This clip was already sent to {acct['platform']} ({dup['status']}).")
    public = mode_for(db, account_id) == "public"
    aid = db.insert("publish_attempts", clip_id=clip_id, platform=acct["platform"], status="uploading",
                    detail=f"Owner-commanded {'PUBLIC upload' if public else 'draft delivery'} to {acct['brand']} ({acct['handle']})",
                    stage="initiated")
    try:
        video = Path(clip["video_path"])
        if acct["platform"] == "youtube":
            res = clients["youtube"].upload(db, account_id, video, publish_title(db, clip), clip["description"] or "", public=public)
            remote = res["video_id"]
            if res.get("privacy") == "public":
                status, detail = "published", "Uploaded to YouTube as PUBLIC."
            elif public:
                status = "delivered_private"
                detail = "YouTube returned PRIVATE after PUBLIC was requested. Check visibility in YouTube Studio."
            else:
                status, detail = "delivered_private", "Uploaded to YouTube as PRIVATE (not public). Review and publish manually in YouTube Studio."
        elif acct["platform"] == "instagram":
            res = clients["instagram"].publish_reel(acct["brand"], acct["platform_id"], video, clip["description"] or "")
            remote = res["video_id"]
            status = "published"
            detail = "Instagram accepted and PUBLISHED the Reel (public)." + (f" {res['permalink']}" if res.get("permalink") else "")
        else:
            res = clients["facebook"].create_reel(db, account_id, video, clip["description"] or "", publish=public)
            remote = res["video_id"]
            if public:
                status = "published" if res.get("success", True) else "processing"
                detail = "Facebook accepted the Reel for publishing. Check the Page for its public visibility."
            else:
                status, detail = "delivered_draft", "Created as a Facebook Reel DRAFT (not published). Finish it on the Page yourself."
    except Exception as e:                                              # noqa: BLE001
        db.update("publish_attempts", aid, status="failed", detail=str(e)[:500], updated_at=time.time())
        raise
    db.update("publish_attempts", aid, status=status, remote_id=remote, detail=detail, stage="delivered",
              confirmed_at=time.time(), updated_at=time.time())
    return {"attempt_id": aid, "status": status, "remote_id": remote, "detail": detail}
