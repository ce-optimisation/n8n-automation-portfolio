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

    def authorize_url(self, brand: str, redirect_uri: str) -> tuple[str, str, str]:
        if not self.configured:
            raise NotConfigured("No Google OAuth client is configured (CLIPENGINE_GOOGLE_CLIENT_ID/SECRET).")
        return self.oauth(brand).auth_url(redirect_uri)

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
        acct = db.one("SELECT * FROM social_accounts WHERE id=?", (account_id,))
        if not acct or acct["platform"] != "youtube" or not acct["identity_verified_at"]:
            raise NotConfigured("This YouTube destination's identity has not been verified.")
        video = Path(video)
        if not video.is_file():
            raise PublishError("Video file not found.")
        oa = self.oauth(acct["brand"])
        token = oa.access_token()
        live = cx.youtube_identity(self.http, token)                       # re-check immediately before uploading
        if str(acct["verified_platform_id"]) not in {str(o["id"]) for o in live}:
            raise PublishError("The authorised channel is not this brand's verified channel; upload refused.")
        meta = {"snippet": {"title": (title or "Clip")[:100], "description": (description or "")[:5000]},
                "status": {"privacyStatus": "private", "selfDeclaredMadeForKids": False}}
        assert meta["status"]["privacyStatus"] == "private"
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
        if (j.get("status") or {}).get("privacyStatus") != "private":
            raise PublishError("YouTube did not report the video as private; check it in YouTube Studio immediately.")
        return {"video_id": j.get("id"), "privacy": "private"}
