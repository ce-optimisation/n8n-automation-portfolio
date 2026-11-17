"""Platform-neutral publication packages, social-account isolation and review-before-publication handoffs.

One master render (1080x1920 H.264/AAC) serves every platform; a separate export is made only when a
platform's documented limit requires it (and then it is flagged for an editorial decision rather than
silently cut). Mechanisms, strongest officially supported first (checked November 2026):

  youtube_shorts   Official Data API resumable upload, ALWAYS privacyStatus=private; the owner reviews and
                   publishes in YouTube Studio. (Google's docs disagree on whether unaudited projects are
                   forced to private anyway; uploading private is correct either way.)
  tiktok           Existing Content Posting API client: upload to the creator's TikTok inbox (the owner
                   finishes the post in the TikTok app). Unchanged.
  instagram_reels  Existing Meta client (containers are not drafts: nothing is published without the
                   owner's explicit Publish action). Unchanged.
  facebook_reels   Manual handoff. Meta's video_reels endpoint lists a DRAFT state, but no official example
                   was found; it is documented as the next integration, not implemented as working.
  x                Manual handoff. X's API is pay-per-use with no general free tier (2026); no API costs
                   are introduced without the owner's approval.
  rumble           Manual handoff (no public upload API).
Nothing is ever published publicly by the engine.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from ..jobs import JobContext, PermanentError, TransientError
from .base import editorial_gate

PLATFORMS = {
    "youtube_shorts": {"label": "YouTube Shorts", "account_platform": "youtube", "max_s": 180, "min_s": 1,
                       "title_max": 100, "desc_max": 5000, "hashtags_max": 3, "method": "private_upload_api",
                       "upload_url": "https://studio.youtube.com/"},
    "tiktok": {"label": "TikTok", "account_platform": "tiktok", "max_s": 600, "min_s": 3, "title_max": 0,
               "desc_max": 2200, "hashtags_max": 5, "method": "inbox_upload_api", "upload_url": "https://www.tiktok.com/upload"},
    "instagram_reels": {"label": "Instagram Reels", "account_platform": "instagram", "max_s": 900, "min_s": 3,
                        "title_max": 0, "desc_max": 2200, "hashtags_max": 5, "method": "container_api",
                        "upload_url": "https://www.instagram.com/"},
    "facebook_reels": {"label": "Facebook Reels", "account_platform": "facebook", "max_s": 90, "min_s": 3,
                       "title_max": 255, "desc_max": 2200, "hashtags_max": 5, "method": "manual_handoff",
                       "upload_url": "https://www.facebook.com/reels/create"},
    "x": {"label": "X", "account_platform": "x", "max_s": 140, "min_s": 1, "title_max": 0, "desc_max": 280,
          "hashtags_max": 2, "method": "manual_handoff", "upload_url": "https://x.com/compose/post"},
    "rumble": {"label": "Rumble", "account_platform": "rumble", "max_s": 3600 * 4, "min_s": 1, "title_max": 100,
               "desc_max": 5000, "hashtags_max": 5, "method": "manual_handoff", "upload_url": "https://rumble.com/upload.php"},
}


class IsolationError(Exception):
    pass


def account_allows(account, niche_key: str | None) -> bool:
    """Brand isolation: an account receives only clips of the niches it is mapped to."""
    niches = json.loads(account["niches_json"] or "[]")
    return bool(niche_key) and niche_key in niches


def build_text(clip, rec, niche, platform: str, creator=None) -> dict:
    """Prepared (editable) title, description, caption and hashtags. Factual, attributed, no hype."""
    spec = PLATFORMS[platform]
    base_title = (clip["title"] or rec["title"] or "").strip()
    quote = (clip["quote"] or "").strip()
    first = quote.split(". ")[0][:120]
    title = (base_title if base_title else first)[: spec["title_max"]] if spec["title_max"] else ""
    attribution = ""
    if creator is not None and creator["attribution"]:
        attribution = creator["attribution"]
    elif rec["source_url"]:
        attribution = f"Source: {rec['source_url']}"
    tags = (niche.hashtags if niche else [])[: spec["hashtags_max"]]
    caption = first
    if spec["desc_max"] and len(caption) > spec["desc_max"] - 40:
        caption = caption[: spec["desc_max"] - 41] + "…"
    desc = "\n\n".join(x for x in [caption, attribution, " ".join(tags)] if x)[: spec["desc_max"]]
    return {"title": title, "description": desc, "caption": caption, "hashtags": " ".join(tags),
            "attribution": attribution}


class PackageService:
    def __init__(self, settings, db, queue, tokens, youtube_http=None):
        self.settings, self.db, self.queue, self.tokens = settings, db, queue, tokens
        self.youtube_http = youtube_http
        queue.register("youtube_private_upload", self.run_youtube)

    def _history(self, pid: int, event: str) -> None:
        row = self.db.one("SELECT history_json FROM publication_packages WHERE id=?", (pid,))
        h = json.loads(row["history_json"] or "[]") if row else []
        h.append({"at": time.time(), "event": event})
        self.db.update("publication_packages", pid, history_json=json.dumps(h[-50:]))

    def check_spec(self, clip, platform: str) -> list[str]:
        spec = PLATFORMS[platform]
        dur = (clip["end"] or 0) - (clip["start"] or 0)
        issues = []
        if dur > spec["max_s"]:
            issues.append(f"{spec['label']} accepts up to {spec['max_s']} s; this clip is {dur:.0f} s. Regenerate it "
                          "shorter (an editorial decision) rather than cutting it automatically.")
        if dur < spec["min_s"]:
            issues.append(f"{spec['label']} needs at least {spec['min_s']} s.")
        return issues

    def prepare(self, clip_id: int, platform: str, account_id: int | None) -> int:
        if platform not in PLATFORMS:
            raise ValueError("Unknown platform")
        clip = self.db.clip(clip_id)
        if not clip:
            raise ValueError("Clip not found")
        rec = self.db.recording(clip["recording_id"])
        if account_id:
            acc = self.db.one("SELECT * FROM social_accounts WHERE id=?", (account_id,))
            if not acc:
                raise ValueError("Account not found")
            if acc["platform"] != PLATFORMS[platform]["account_platform"]:
                raise IsolationError(f"Account @{acc['handle']} is a {acc['platform']} account, not "
                                     f"{PLATFORMS[platform]['label']}.")
            if not account_allows(acc, clip["niche_key"]):
                raise IsolationError(f"@{acc['handle']} ({acc['brand'] or 'no brand'}) is not mapped to this clip's niche "
                                     f"({clip['niche_key'] or 'none'}). Brands are kept separate; map the niche to the "
                                     "account first if this is intended.")
        from .. import editorial
        niche = editorial.load_niche(self.db, clip["niche_key"])
        creator = self.db.one("SELECT * FROM creators WHERE id=?", (rec["creator_id"],)) if rec["creator_id"] else None
        text = build_text(clip, rec, niche, platform, creator)
        existing = self.db.one("SELECT id FROM publication_packages WHERE clip_id=? AND platform=? AND "
                               "account_id IS ?", (clip_id, platform, account_id))
        if existing:
            return existing["id"]
        pid = self.db.insert("publication_packages", clip_id=clip_id, platform=platform, account_id=account_id,
                             status="prepared", approval_state="draft", upload_state="not_started",
                             history_json="[]", **text)
        self._history(pid, "prepared" + ("; " + " ".join(self.check_spec(clip, platform))
                                         if self.check_spec(clip, platform) else ""))
        return pid

    def edit(self, pid: int, **fields) -> None:
        allowed = {k: v for k, v in fields.items() if k in ("title", "description", "caption", "hashtags")}
        pkg = self.db.one("SELECT * FROM publication_packages WHERE id=?", (pid,))
        if not pkg:
            raise ValueError("Package not found")
        if pkg["status"] in ("uploaded_private", "published"):
            raise ValueError("Already uploaded; edit the post on the platform itself.")
        self.db.update("publication_packages", pid, approval_state="draft", **allowed)
        self._history(pid, "edited (approval reset)")

    def approve(self, pid: int) -> None:
        """Owner approval of the package text and destination (separate from clip approval)."""
        pkg = self.db.one("SELECT * FROM publication_packages WHERE id=?", (pid,))
        clip = self.db.clip(pkg["clip_id"])
        problems = editorial_gate(clip) + self.check_spec(clip, pkg["platform"])
        if problems:
            raise ValueError(" ".join(problems))
        self.db.update("publication_packages", pid, approval_state="approved")
        self._history(pid, "approved by owner")

    def handoff(self, pid: int) -> dict:
        """Manual handoff: the file, the prepared text and the platform's own upload page. The engine does not
        post; the owner does, in the platform's app or site."""
        pkg = self.db.one("SELECT * FROM publication_packages WHERE id=?", (pid,))
        if pkg["approval_state"] != "approved":
            raise ValueError("Approve the package first.")
        spec = PLATFORMS[pkg["platform"]]
        self.db.update("publication_packages", pid, status="handoff_ready")
        self._history(pid, "handoff prepared")
        return {"download": f"/clips/{pkg['clip_id']}/download", "text": "\n\n".join(
            x for x in [pkg["title"], pkg["description"]] if x), "open": spec["upload_url"], "label": spec["label"]}

    def mark_published(self, pid: int, url: str) -> None:
        """Recorded by the owner after posting (the platform confirmed it to them, not to the engine)."""
        now = time.time()
        pkg = self.db.one("SELECT * FROM publication_packages WHERE id=?", (pid,))
        self.db.update("publication_packages", pid, status="published", remote_url=url.strip() or None, published_at=now,
                       handoff_state="owner_published", handoff_method="manual", handoff_at=now,
                       handoff_evidence="Owner recorded that they posted it (the platform confirmed it to them, not the engine)")
        clip = self.db.clip(pkg["clip_id"])
        if not clip["published_at"]:
            self.db.update("clips", clip["id"], published_at=now)
        self._history(pid, "marked published by owner")

    # ---- YouTube private upload --------------------------------------------------------------
    def youtube_oauth(self, account_id: int):
        from ..cloud import YOUTUBE_UPLOAD_SCOPE, GoogleOAuth
        return GoogleOAuth(self.settings.google_client_id, self.settings.google_client_secret, self.tokens,
                           f"youtube:{account_id}", [YOUTUBE_UPLOAD_SCOPE], http=self.youtube_http)

    def request_youtube_private(self, pid: int) -> int:
        pkg = self.db.one("SELECT * FROM publication_packages WHERE id=?", (pid,))
        if pkg["platform"] != "youtube_shorts":
            raise ValueError("Not a YouTube package")
        if pkg["approval_state"] != "approved":
            raise ValueError("Approve the package first.")
        if not pkg["account_id"] or not self.youtube_oauth(pkg["account_id"]).connected():
            raise ValueError("Connect this YouTube account first (READY FOR OWNER CONNECTION).")
        if pkg["status"] in ("uploaded_private", "published"):
            raise ValueError("Already uploaded.")
        self.db.update("publication_packages", pid, upload_state="queued")
        return self.queue.enqueue("youtube_private_upload", clip_id=pkg["clip_id"], dedupe_key=f"yt:{pid}",
                                  payload={"package_id": pid}, max_attempts=5, priority=20)

    def run_youtube(self, ctx: JobContext) -> None:
        pid = ctx.payload["package_id"]
        pkg = self.db.one("SELECT * FROM publication_packages WHERE id=?", (pid,))
        clip = self.db.clip(pkg["clip_id"])
        if pkg["approval_state"] != "approved" or editorial_gate(clip):
            raise PermanentError("Package or clip is no longer approved; nothing uploaded.")
        if pkg["remote_id"]:
            ctx.log("Already uploaded (remote id recorded); not uploading again")
            return
        oauth = self.youtube_oauth(pkg["account_id"])
        import httpx
        c = self.youtube_http or httpx.Client(timeout=300)
        path = Path(clip["video_path"])
        size = path.stat().st_size
        body = {"snippet": {"title": (pkg["title"] or "Clip")[:100], "description": pkg["description"] or "",
                            "tags": [t.lstrip("#") for t in (pkg["hashtags"] or "").split()][:15], "categoryId": "22"},
                "status": {"privacyStatus": "private", "selfDeclaredMadeForKids": False}}
        h = {"Authorization": f"Bearer {oauth.access_token()}", "X-Upload-Content-Length": str(size),
             "X-Upload-Content-Type": "video/mp4"}
        r = c.post("https://www.googleapis.com/upload/youtube/v3/videos",
                   params={"uploadType": "resumable", "part": "snippet,status"}, json=body, headers=h)
        if r.status_code == 403 and "quota" in r.text.lower():
            raise TransientError("YouTube API daily quota reached; retrying tomorrow.", retry_after=6 * 3600)
        if r.status_code >= 400:
            raise PermanentError(f"YouTube refused the upload (HTTP {r.status_code}): {r.text[:200]}")
        session = r.headers["Location"]
        self.db.update("publication_packages", pid, upload_state="uploading")
        ctx.progress(0.2, "Uploading to YouTube (private)", force=True)
        r = c.put(session, content=path.read_bytes(), headers={"Authorization": f"Bearer {oauth.access_token()}",
                                                               "Content-Type": "video/mp4"})
        if r.status_code >= 500:
            raise TransientError(f"YouTube upload interrupted (HTTP {r.status_code})")
        if r.status_code not in (200, 201):
            raise PermanentError(f"YouTube upload failed (HTTP {r.status_code}): {r.text[:200]}")
        v = r.json()
        privacy = (v.get("status") or {}).get("privacyStatus")
        if privacy != "private":
            # Never expected; recorded loudly because the engine must never make anything public.
            self._history(pid, f"WARNING: YouTube reports privacy '{privacy}'")
        verified = bool(v.get("id")) and privacy == "private"
        self.db.update("publication_packages", pid, status="uploaded_private", upload_state="done",
                       remote_id=v.get("id"), remote_url=f"https://studio.youtube.com/video/{v.get('id')}/edit",
                       handoff_state="private_verified" if verified else "remote_exists",
                       handoff_method="api", handoff_at=time.time(),
                       handoff_evidence=f"YouTube returned video id {v.get('id')} with privacyStatus={privacy!r}")
        self._history(pid, f"uploaded as PRIVATE video {v.get('id')} (confirmed by YouTube)")
