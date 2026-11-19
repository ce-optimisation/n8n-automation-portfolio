"""Instagram Reels publishing for professional (Business or Creator) accounts.

Two official login options are supported (INSTAGRAM_AUTH_MODE):

* ``facebook`` (default) — Facebook Login for Business, host graph.facebook.com.
  Requires the Instagram professional account to be linked to a Facebook Page.
  Supports *resumable upload* of the local file to rupload.facebook.com, so the
  dashboard never needs to be reachable from the internet.

* ``instagram`` — Instagram API with Instagram Login (Business Login for Instagram),
  host graph.instagram.com. No Facebook Page needed, but Meta documents resumable
  upload only for Facebook Login; with Instagram Login the video must be fetched by
  Meta from a public HTTPS ``video_url``. The app therefore issues a one-time, expiring
  link (PUBLIC_MEDIA_BASE_URL/public/media/<token>.mp4) that serves only that clip.

Publishing flow (both): create REELS container → (upload) → poll status_code until
FINISHED → media_publish. The API cannot create native drafts: publishing is PUBLIC,
so the dashboard requires an explicit second confirmation. Meta limits API-published
posts to 100 per 24 hours.
"""
from __future__ import annotations

import secrets
import time
import urllib.parse
from pathlib import Path

import httpx

from ..jobs import TransientError
from .base import NotConfigured, PublishError, validate_video

FB_SCOPES = "instagram_basic,instagram_content_publish,pages_show_list,pages_read_engagement,business_management"
IG_SCOPES = "instagram_business_basic,instagram_business_content_publish"
LIMITS = {"min_s": 3, "max_s": 900, "max_bytes": 1024 ** 3, "codecs": ("h264", "hevc")}
TRANSIENT_ERROR_CODES = {1, 2, 4, 17, 32, 341, 613}   # Graph API temporary / throttling codes
AUTH_ERROR_CODES = {10, 190, 200}


class InstagramClient:
    platform = "instagram"

    def __init__(self, app_id: str, app_secret: str, redirect_uri: str, store, version: str = "v23.0",
                 http: httpx.Client | None = None, sleep=time.sleep, mode: str = "facebook",
                 public_base_url: str = ""):
        self.app_id = app_id
        self.app_secret = app_secret
        self.redirect_uri = redirect_uri
        self.store = store
        self.version = version
        self.mode = mode
        self.public_base_url = public_base_url.rstrip("/")
        host = "graph.instagram.com" if mode == "instagram" else "graph.facebook.com"
        self.graph = f"https://{host}/{version}"
        self.http = http or httpx.Client(timeout=httpx.Timeout(60.0, read=600.0))
        self.sleep = sleep

    # ---- configuration / OAuth -------------------------------------------
    @property
    def configured(self) -> bool:
        base = bool(self.app_id and self.app_secret and self.redirect_uri)
        if self.mode == "instagram":
            return base and self.public_base_url.startswith("https://")
        return base

    def missing_config(self) -> list[str]:
        out = []
        prefix = "INSTAGRAM" if self.mode == "instagram" else "META"
        if not self.app_id:
            out.append(f"{prefix}_APP_ID")
        if not self.app_secret:
            out.append(f"{prefix}_APP_SECRET")
        if not self.redirect_uri:
            out.append(f"{prefix}_REDIRECT_URI")
        if self.mode == "instagram" and not self.public_base_url.startswith("https://"):
            out.append("PUBLIC_MEDIA_BASE_URL (https)")
        return out

    def status(self) -> dict:
        tok = self.store.get(self.platform)
        expires = (tok or {}).get("expires_at", 0)
        return {"configured": self.configured, "mode": self.mode,
                "connected": bool(tok and tok.get("ig_user_id")) and expires > time.time(),
                "username": (tok or {}).get("username"), "expires_at": expires,
                "expiring_soon": bool(tok) and expires - time.time() < 7 * 86400}

    def authorize_url(self) -> tuple[str, dict]:
        if not self.configured:
            raise NotConfigured("Instagram is not configured: set " + ", ".join(self.missing_config()) + " in .env")
        state = secrets.token_urlsafe(24)
        if self.mode == "instagram":
            q = {"client_id": self.app_id, "redirect_uri": self.redirect_uri, "state": state, "scope": IG_SCOPES,
                 "response_type": "code"}
            return "https://www.instagram.com/oauth/authorize?" + urllib.parse.urlencode(q), {"state": state}
        q = {"client_id": self.app_id, "redirect_uri": self.redirect_uri, "state": state, "scope": FB_SCOPES,
             "response_type": "code"}
        return f"https://www.facebook.com/{self.version}/dialog/oauth?" + urllib.parse.urlencode(q), {"state": state}

    def _check(self, r: httpx.Response) -> dict:
        body = r.json() if r.content else {}
        err = body.get("error") if isinstance(body, dict) else None
        if r.status_code == 200 and not err:
            return body
        err = err or {}
        code = err.get("code")
        msg = err.get("error_user_msg") or err.get("message") or f"HTTP {r.status_code}"
        if r.status_code >= 500 or r.status_code == 429 or code in TRANSIENT_ERROR_CODES or err.get("is_transient"):
            raise TransientError(f"Instagram temporarily unavailable ({code}): {msg}")
        if code in AUTH_ERROR_CODES or r.status_code in (401, 403):
            raise NotConfigured(f"Instagram authorisation problem ({code}): {msg}. Reconnect Instagram in Settings.")
        raise PublishError(f"Instagram API error ({code}): {msg}")

    def _req(self, method: str, url: str, **kw) -> dict:
        try:
            return self._check(self.http.request(method, url, **kw))
        except httpx.TransportError as e:
            raise TransientError(f"Instagram network error: {e}") from e

    def exchange_code(self, code: str) -> dict:
        if self.mode == "instagram":
            short = self._req("POST", "https://api.instagram.com/oauth/access_token", data={
                "client_id": self.app_id, "client_secret": self.app_secret, "grant_type": "authorization_code",
                "redirect_uri": self.redirect_uri, "code": code})
            long = self._req("GET", "https://graph.instagram.com/access_token", params={
                "grant_type": "ig_exchange_token", "client_secret": self.app_secret,
                "access_token": short["access_token"]})
            token = long["access_token"]
            me = self._req("GET", f"{self.graph}/me", params={"fields": "user_id,username", "access_token": token})
            ig = {"id": str(me.get("user_id") or me.get("id") or short.get("user_id")), "username": me.get("username")}
        else:
            short = self._req("GET", f"{self.graph}/oauth/access_token", params={
                "client_id": self.app_id, "redirect_uri": self.redirect_uri, "client_secret": self.app_secret,
                "code": code})
            long = self._req("GET", f"{self.graph}/oauth/access_token", params={
                "grant_type": "fb_exchange_token", "client_id": self.app_id, "client_secret": self.app_secret,
                "fb_exchange_token": short["access_token"]})
            token = long["access_token"]
            pages = self._req("GET", f"{self.graph}/me/accounts", params={
                "fields": "name,instagram_business_account{id,username}", "access_token": token}).get("data", [])
            linked = [p["instagram_business_account"] for p in pages if p.get("instagram_business_account")]
            if len(linked) > 1:
                raise PublishError("This login manages several Instagram accounts; the legacy single-account connector refuses to "
                                   "guess which brand is meant. Use the Connections page, which verifies each brand separately.")
            ig = linked[0] if linked else None
            if not ig:
                raise PublishError("No Instagram professional account is linked to the Facebook Pages you authorised. "
                                   "Link the account to a Page (Instagram app → Settings → Account type and tools / "
                                   "Meta Business Suite), or switch to INSTAGRAM_AUTH_MODE=instagram.")
        tok = {"access_token": token, "ig_user_id": ig["id"], "username": ig.get("username"), "mode": self.mode,
               "issued_at": time.time(), "expires_at": time.time() + int(long.get("expires_in", 5184000))}
        self.store.set(self.platform, tok)
        return tok

    def _token(self) -> dict:
        tok = self.store.get(self.platform)
        if not tok or not tok.get("ig_user_id"):
            raise NotConfigured("Instagram is not connected. Use Settings → Connect Instagram.")
        if tok.get("mode", "facebook") != self.mode:
            raise NotConfigured("Instagram was connected with a different login mode; reconnect in Settings.")
        if tok.get("expires_at", 0) < time.time():
            raise NotConfigured("Instagram token expired; reconnect in Settings.")
        # Instagram Login long-lived tokens can be refreshed once they are 24h old.
        if self.mode == "instagram" and tok["expires_at"] - time.time() < 10 * 86400 \
                and time.time() - tok.get("issued_at", 0) > 86400:
            try:
                r = self._req("GET", "https://graph.instagram.com/refresh_access_token",
                              params={"grant_type": "ig_refresh_token", "access_token": tok["access_token"]})
                tok.update(access_token=r["access_token"], issued_at=time.time(),
                           expires_at=time.time() + int(r.get("expires_in", 5184000)))
                self.store.set(self.platform, tok)
            except (PublishError, TransientError):
                pass   # keep using the still-valid token; Settings shows "expiring soon"
        return tok

    def publishing_quota(self) -> dict:
        tok = self._token()
        return self._req("GET", f"{self.graph}/{tok['ig_user_id']}/content_publishing_limit",
                         params={"fields": "quota_usage,config", "access_token": tok["access_token"]})

    # ---- resumable steps (used by the publishing job) ---------------------
    def create_container(self, video: Path, caption: str, video_url: str | None = None) -> tuple[str, str | None]:
        validate_video(video, **LIMITS)
        tok = self._token()
        data = {"media_type": "REELS", "caption": caption[:2200], "share_to_feed": "true",
                "access_token": tok["access_token"]}
        if self.mode == "instagram":
            if not video_url:
                raise PublishError("Instagram Login needs a public video URL (PUBLIC_MEDIA_BASE_URL).")
            data["video_url"] = video_url
        else:
            data["upload_type"] = "resumable"
        body = self._req("POST", f"{self.graph}/{tok['ig_user_id']}/media", data=data)
        uri = body.get("uri") or (None if self.mode == "instagram" else
                                  f"https://rupload.facebook.com/ig-api-upload/{self.version}/{body['id']}")
        return body["id"], uri

    def upload(self, upload_uri: str, video: Path) -> None:
        tok = self._token()
        size = Path(video).stat().st_size
        try:
            with open(video, "rb") as f:
                r = self.http.post(upload_uri, content=f.read(), headers={
                    "Authorization": f"OAuth {tok['access_token']}", "offset": "0", "file_size": str(size)})
        except httpx.TransportError as e:
            raise TransientError(f"Instagram upload interrupted: {e}") from e
        body = self._check(r)
        if body.get("success") is False:
            raise PublishError(f"Instagram upload was not accepted: {body}")

    def container_status(self, container: str) -> tuple[str | None, str | None]:
        tok = self._token()
        st = self._req("GET", f"{self.graph}/{container}",
                       params={"fields": "status_code,status", "access_token": tok["access_token"]})
        return st.get("status_code"), st.get("status")

    def publish(self, container: str) -> str:
        tok = self._token()
        pb = self._req("POST", f"{self.graph}/{tok['ig_user_id']}/media_publish",
                       data={"creation_id": container, "access_token": tok["access_token"]})
        if "id" not in pb:
            raise PublishError(f"Instagram publish returned no media id: {pb}")
        return pb["id"]

    def permalink(self, media_id: str) -> str | None:
        tok = self._token()
        try:
            return self._req("GET", f"{self.graph}/{media_id}",
                             params={"fields": "id,permalink", "access_token": tok["access_token"]}).get("permalink")
        except (PublishError, TransientError):
            return None
