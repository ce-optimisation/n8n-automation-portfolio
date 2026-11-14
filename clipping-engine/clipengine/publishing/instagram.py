"""Instagram content publishing (Reels) for professional accounts.

Uses the Instagram Graph API via Facebook Login for Business:
  1. OAuth -> user token -> long-lived token; find the linked IG professional account.
  2. POST /{ig-user-id}/media  media_type=REELS, upload_type=resumable -> container id + upload uri
  3. POST the bytes to rupload.facebook.com (headers: Authorization: OAuth <token>, offset, file_size)
  4. GET /{container}?fields=status_code until FINISHED
  5. POST /{ig-user-id}/media_publish creation_id=<container>  -> media id (PUBLIC post)

IMPORTANT: The official API cannot create native Instagram drafts. Publishing
through it makes the Reel public immediately, so the dashboard requires an
explicit second confirmation. API-published posts are limited to 100 per 24h.
"""
from __future__ import annotations

import secrets
import time
import urllib.parse
from pathlib import Path

import httpx

from .base import NotConfigured, PublishError, PublishResult

SCOPES = "instagram_basic,instagram_content_publish,pages_show_list,pages_read_engagement,business_management"


class InstagramClient:
    platform = "instagram"

    def __init__(self, app_id: str, app_secret: str, redirect_uri: str, store, version: str = "v23.0",
                 http: httpx.Client | None = None, sleep=time.sleep):
        self.app_id = app_id
        self.app_secret = app_secret
        self.redirect_uri = redirect_uri
        self.store = store
        self.version = version
        self.graph = f"https://graph.facebook.com/{version}"
        self.http = http or httpx.Client(timeout=120)
        self.sleep = sleep

    @property
    def configured(self) -> bool:
        return bool(self.app_id and self.app_secret and self.redirect_uri)

    def status(self) -> dict:
        tok = self.store.get(self.platform)
        return {"configured": self.configured, "connected": bool(tok and tok.get("ig_user_id")),
                "username": (tok or {}).get("username"), "expires_at": (tok or {}).get("expires_at")}

    def authorize_url(self) -> tuple[str, dict]:
        if not self.configured:
            raise NotConfigured("Set META_APP_ID, META_APP_SECRET and META_REDIRECT_URI in .env")
        state = secrets.token_urlsafe(24)
        q = {"client_id": self.app_id, "redirect_uri": self.redirect_uri, "state": state, "scope": SCOPES,
             "response_type": "code"}
        return f"https://www.facebook.com/{self.version}/dialog/oauth?" + urllib.parse.urlencode(q), {"state": state}

    def _get(self, path: str, **params) -> dict:
        r = self.http.get(path if path.startswith("http") else f"{self.graph}/{path.lstrip('/')}", params=params)
        body = r.json() if r.content else {}
        if r.status_code != 200 or "error" in body:
            raise PublishError(f"Graph API error: {(body.get('error') or {}).get('message') or r.status_code}")
        return body

    def exchange_code(self, code: str) -> dict:
        short = self._get("oauth/access_token", client_id=self.app_id, redirect_uri=self.redirect_uri,
                          client_secret=self.app_secret, code=code)
        long = self._get("oauth/access_token", grant_type="fb_exchange_token", client_id=self.app_id,
                         client_secret=self.app_secret, fb_exchange_token=short["access_token"])
        token = long["access_token"]
        pages = self._get("me/accounts", fields="name,instagram_business_account{id,username}",
                          access_token=token).get("data", [])
        ig = next((p["instagram_business_account"] for p in pages if p.get("instagram_business_account")), None)
        if not ig:
            raise PublishError("No Instagram professional account is linked to the authorised Facebook Pages.")
        tok = {"access_token": token, "ig_user_id": ig["id"], "username": ig.get("username"),
               "expires_at": time.time() + int(long.get("expires_in", 5184000))}
        self.store.set(self.platform, tok)
        return tok

    def _token(self) -> dict:
        tok = self.store.get(self.platform)
        if not tok or not tok.get("ig_user_id"):
            raise NotConfigured("Instagram is not connected. Use Settings → Connect Instagram.")
        if tok.get("expires_at", 0) < time.time():
            raise NotConfigured("Instagram token expired; reconnect in Settings.")
        return tok

    def publishing_quota(self) -> dict:
        tok = self._token()
        return self._get(f"{tok['ig_user_id']}/content_publishing_limit", fields="quota_usage,config",
                         access_token=tok["access_token"])

    def publish_reel(self, video: Path, caption: str, poll_seconds: float = 5.0,
                     max_polls: int = 120) -> PublishResult:
        tok = self._token()
        ig, at = tok["ig_user_id"], tok["access_token"]
        r = self.http.post(f"{self.graph}/{ig}/media", data={
            "media_type": "REELS", "upload_type": "resumable", "caption": caption[:2200],
            "share_to_feed": "true", "access_token": at})
        body = r.json() if r.content else {}
        if r.status_code != 200 or "id" not in body:
            raise PublishError(f"Instagram container creation failed: {(body.get('error') or {}).get('message') or body}")
        container = body["id"]
        upload_uri = body.get("uri") or f"https://rupload.facebook.com/ig-api-upload/{self.version}/{container}"
        size = Path(video).stat().st_size
        with open(video, "rb") as f:
            ur = self.http.post(upload_uri, content=f.read(), headers={
                "Authorization": f"OAuth {at}", "offset": "0", "file_size": str(size)})
        ub = ur.json() if ur.content else {}
        if ur.status_code != 200 or not ub.get("success", True):
            raise PublishError(f"Instagram upload failed: HTTP {ur.status_code} {ub}")
        for _ in range(max_polls):
            st = self._get(container, fields="status_code,status", access_token=at)
            code = st.get("status_code")
            if code == "FINISHED":
                break
            if code in ("ERROR", "EXPIRED"):
                return PublishResult("failed", container, f"Instagram processing {code}: {st.get('status')}", False)
            self.sleep(poll_seconds)
        else:
            return PublishResult("uploaded", container, "Uploaded; Instagram is still processing. Not published.",
                                 False)
        pr = self.http.post(f"{self.graph}/{ig}/media_publish", data={"creation_id": container, "access_token": at})
        pb = pr.json() if pr.content else {}
        if pr.status_code != 200 or "id" not in pb:
            raise PublishError(f"Instagram publish failed: {(pb.get('error') or {}).get('message') or pb}")
        media = self._get(pb["id"], fields="id,permalink", access_token=at)
        return PublishResult("confirmed", pb["id"], f"Instagram confirmed publication: {media.get('permalink')}", True)
