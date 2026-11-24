"""YouTube: per-brand channel authorisation, identity read-back and the closest "prepare, never publish" upload (Session 13).

The YouTube Data API has no draft. The closest officially supported preparation is a PRIVATE upload. Documentation (checked
19 Nov 2026): if ``status.privacyStatus`` is omitted the video defaults to PUBLIC and uploads from unverified API projects are
no longer forced private, so this module ALWAYS sends ``privacyStatus='private'`` explicitly and refuses anything else.

One Google login administers all six channels, but a Brand Account channel token is bound to the channel chosen on the consent
screen. So each brand is authorised separately, stored under ``youtube:<brand>``, and the channel the API reports must equal the
brand's expected channel id before any upload is allowed. ``upload_private`` is only ever called by the delivery layer after the
routing guard passes and the owner has issued an explicit command; connecting and verifying never upload.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx

from .. import connections as cx
from ..cloud import GoogleOAuth
from ..jobs import PermanentError
from .base import NotConfigured, PublishError

SCOPES = ["https://www.googleapis.com/auth/youtube.readonly", "https://www.googleapis.com/auth/youtube.upload"]
ANALYTICS_SCOPES = [SCOPES[0], "https://www.googleapis.com/auth/yt-analytics.readonly"]
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"


class YouTubeClient:
    platform = "youtube"

    def __init__(self, client_id: str, client_secret: str, store, http: httpx.Client | None = None):
        self.client_id, self.client_secret, self.store = client_id, client_secret, store
        self.http = http or httpx.Client(timeout=httpx.Timeout(60.0, read=600.0))

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def oauth(self, brand: str) -> GoogleOAuth:
        return GoogleOAuth(self.client_id, self.client_secret, self.store, f"youtube:{brand}", SCOPES, http=self.http)

    def analytics_oauth(self, brand: str) -> GoogleOAuth:
        return GoogleOAuth(self.client_id, self.client_secret, self.store, f"youtube-analytics:{brand}",
                           ANALYTICS_SCOPES, http=self.http)

    def connect_analytics(self, account, code: str, redirect_uri: str, verifier: str) -> None:
        oauth = self.analytics_oauth(account["brand"])
        token = oauth.exchange(code, redirect_uri, verifier, save=False)
        if not set(ANALYTICS_SCOPES).issubset(token["scope"].split()):
            raise PermanentError("Google did not grant the requested read-only analytics permissions.")
        identities = cx.youtube_identity(self.http, token["access_token"])
        if {row["id"] for row in identities} != {account["verified_platform_id"]}:
            raise PermanentError("Select the verified YouTube channel for this brand and try again.")
        self.store.set(oauth.key, token)

    def authorize_url(self, brand: str, redirect_uri: str) -> tuple[str, str, str]:
        if not self.configured:
            raise NotConfigured("No Google OAuth client is configured (CLIPENGINE_GOOGLE_CLIENT_ID/SECRET).")
        # select_account forces Google's account/channel chooser every time. Without it Google silently reuses the channel
        # from a previous approval, and every brand would authorise the same channel (the engine then refuses it as CROSS_BRAND).
        return self.oauth(brand).auth_url(redirect_uri, prompt="select_account consent")

    def connected(self, brand: str) -> bool:
        return self.oauth(brand).connected()

    def verify(self, db, account_id: int) -> dict:
        """Read back which channel this brand's token acts for and apply the exact-id check. A token that turns out to belong
        to another brand's channel is deleted, so it cannot be used by mistake."""
        acct = db.one("SELECT * FROM social_accounts WHERE id=?", (account_id,))
        if not acct or acct["platform"] != "youtube":
            raise KeyError("not a YouTube destination")
        oa = self.oauth(acct["brand"])
        try:
            observed = cx.youtube_identity(self.http, oa.access_token())
        except PermanentError as e:
            raise NotConfigured(str(e)) from e
        res = cx.verify_identity(db, account_id, observed)
        if res["state"] != "VERIFIED":
            oa.disconnect()                                  # never keep a token that is not the expected channel
        return res

    def upload_private(self, db, account_id: int, video: Path, title: str, description: str) -> dict:
        return self.upload(db, account_id, video, title, description, public=False)

    def upload(self, db, account_id: int, video: Path, title: str, description: str, public: bool = False) -> dict:
        """Private by default. With public=True (owner chose public mode for this channel) the upload is requested as PUBLIC;
        YouTube may still keep it private (API projects that have not passed Google's audit are locked to private), in which case
        the result says so and nothing is hidden from the owner."""
        acct = db.one("SELECT * FROM social_accounts WHERE id=?", (account_id,))
        if not acct or acct["platform"] != "youtube" or not acct["identity_verified_at"]:
            raise NotConfigured("This YouTube destination's identity has not been verified.")
        video = Path(video)
        if not video.is_file():
            raise PublishError("Video file not found.")
        from ..media import probe
        info = probe(video)
        if (not info["has_video"] or not info["width"] or not info["height"]
                or info["width"] > info["height"] or not 0 < info["duration"] <= 180
                or info["sample_aspect_ratio"] not in (None, "N/A", "1:1")):
            raise PublishError("YouTube Shorts requires a square or vertical render up to 180 seconds. "
                               "Use a square-pixel export of the approved composition.")
        oa = self.oauth(acct["brand"])
        token = oa.access_token()
        live = cx.youtube_identity(self.http, token)                       # re-check immediately before uploading
        if str(acct["verified_platform_id"]) not in {str(o["id"]) for o in live}:
            raise PublishError("The authorised channel is not this brand's verified channel; upload refused.")
        want = "public" if public else "private"
        if not (title or "").strip():
            raise PublishError("A YouTube upload needs a real title; refusing to publish under a generic name.")
        meta = {"snippet": {"title": title.strip()[:100], "description": (description or "")[:5000]},
                "status": {"privacyStatus": want, "selfDeclaredMadeForKids": False}}      # always set explicitly: omitted = PUBLIC
        assert meta["status"]["privacyStatus"] == want
        size = video.stat().st_size
        init = self.http.post(UPLOAD_URL, params={"uploadType": "resumable", "part": "snippet,status"},
                              headers={"Authorization": f"Bearer {token}", "X-Upload-Content-Type": "video/*",
                                       "X-Upload-Content-Length": str(size), "Content-Type": "application/json; charset=UTF-8"},
                              content=json.dumps(meta))
        if init.status_code != 200 or not init.headers.get("location"):
            raise PublishError(f"YouTube refused the upload session (HTTP {init.status_code}).")
        with video.open("rb") as fh:
            put = self.http.put(init.headers["location"], content=fh.read(), headers={"Content-Type": "video/*"})
        if put.status_code not in (200, 201):
            raise PublishError(f"YouTube upload failed (HTTP {put.status_code}).")
        j = put.json()
        got = (j.get("status") or {}).get("privacyStatus")
        if not public and got != "private":
            raise PublishError("YouTube did not report the video as private; check it in YouTube Studio immediately.")
        if public and got not in ("public", "private"):
            raise PublishError(f"YouTube reported an unexpected visibility ({got!r}); check it in YouTube Studio.")
        return {"video_id": j.get("id"), "privacy": got, "requested": want}
