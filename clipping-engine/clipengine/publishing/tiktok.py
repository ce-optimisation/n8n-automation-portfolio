"""TikTok Content Posting API: upload to the creator's inbox (draft).

Official flow (scope ``video.upload``):
  1. OAuth (Login Kit) -> user access token (+ refresh token).
     * Desktop apps use PKCE where code_challenge = lowercase hex SHA-256(code_verifier)
       (TikTok's desktop variant, not RFC 7636 base64url). Web apps send no PKCE.
  2. POST /v2/post/publish/inbox/video/init/   (source FILE_UPLOAD) -> publish_id, upload_url
  3. PUT the bytes to upload_url (Content-Range per chunk; upload_url valid for 1 hour).
  4. POST /v2/post/publish/status/fetch/ until SEND_TO_USER_INBOX / FAILED.

The creator then finishes and posts the video inside the TikTok app. Nothing here reports
success unless the status endpoint confirms it. Rate limit: 6 init requests per minute per
user token (also enforced locally). TikTok allows a limited number of pending inbox shares
per 24 hours (error spam_risk_too_many_pending_share).
"""
from __future__ import annotations

import hashlib
import secrets
import time
import urllib.parse
from collections import deque
from pathlib import Path

import httpx

from ..jobs import TransientError
from .base import NotConfigured, PublishError, PublishResult, validate_video

AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
INIT_URL = "https://open.tiktokapis.com/v2/post/publish/inbox/video/init/"
STATUS_URL = "https://open.tiktokapis.com/v2/post/publish/status/fetch/"
SCOPES = "user.info.basic,video.upload"

MIN_CHUNK = 5 * 1024 * 1024
DEFAULT_CHUNK = 10 * 1024 * 1024
MAX_FINAL_CHUNK = 128 * 1024 * 1024
LIMITS = {"min_s": 3, "max_s": 600, "max_bytes": 4 * 1024 ** 3, "codecs": ("h264", "hevc")}

TRANSIENT_CODES = {"rate_limit_exceeded", "internal_error", "server_error"}
RECONNECT_CODES = {"access_token_invalid", "scope_not_authorized", "scope_permission_missed"}
EXPLAINED_CODES = {
    "spam_risk_too_many_pending_share": "TikTok limits how many un-posted inbox uploads you can have; post or "
                                        "delete pending drafts in the TikTok app, then retry.",
    "spam_risk_user_banned_from_posting": "TikTok reports this account cannot post right now.",
    "unaudited_client_can_only_post_to_private_accounts": "TikTok has not yet approved (audited) this developer "
                                                          "app for this account type. Awaiting platform approval.",
    "invalid_file_upload": "TikTok rejected the video file format.",
}

_init_calls: deque[float] = deque(maxlen=6)


def chunk_plan(size: int, chunk: int = DEFAULT_CHUNK) -> tuple[int, int]:
    """Return (chunk_size, total_chunk_count) following TikTok's chunking rules:
    files under 5 MB go in one chunk; otherwise chunks are 5–64 MB and the final
    chunk absorbs the remainder (total = floor(size / chunk_size))."""
    if size <= 0:
        raise PublishError("Empty video file")
    if size < MIN_CHUNK:
        return size, 1
    chunk = max(MIN_CHUNK, min(chunk, 64 * 1024 * 1024))
    total = max(1, size // chunk)
    if size - chunk * (total - 1) > MAX_FINAL_CHUNK:
        raise PublishError("File too large for chunk plan")
    return chunk, total


def pkce_pair() -> tuple[str, str]:
    """TikTok desktop PKCE: verifier of unreserved chars, challenge = hex SHA-256."""
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
    verifier = "".join(secrets.choice(alphabet) for _ in range(64))
    return verifier, hashlib.sha256(verifier.encode()).hexdigest()


class TikTokClient:
    platform = "tiktok"

    def __init__(self, client_key: str, client_secret: str, redirect_uri: str, store,
                 http: httpx.Client | None = None, sleep=time.sleep, app_type: str = "desktop"):
        self.client_key = client_key
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.store = store
        self.http = http or httpx.Client(timeout=httpx.Timeout(60.0, read=300.0))
        self.sleep = sleep
        self.app_type = app_type

    # ---- configuration / OAuth -------------------------------------------
    @property
    def configured(self) -> bool:
        return bool(self.client_key and self.client_secret and self.redirect_uri)

    def status(self) -> dict:
        tok = self.store.get(self.platform)
        expired = bool(tok and tok.get("refresh_expires_at") and tok["refresh_expires_at"] < time.time())
        return {"configured": self.configured, "connected": bool(tok) and not expired,
                "scope": (tok or {}).get("scope"), "expires_at": (tok or {}).get("expires_at"),
                "refresh_expires_at": (tok or {}).get("refresh_expires_at")}

    def authorize_url(self) -> tuple[str, dict]:
        if not self.configured:
            raise NotConfigured("Set TIKTOK_CLIENT_KEY, TIKTOK_CLIENT_SECRET and TIKTOK_REDIRECT_URI in .env")
        state = secrets.token_urlsafe(24)
        params = {"client_key": self.client_key, "scope": SCOPES, "response_type": "code",
                  "redirect_uri": self.redirect_uri, "state": state}
        extra = {"state": state}
        if self.app_type == "desktop":
            verifier, challenge = pkce_pair()
            params.update(code_challenge=challenge, code_challenge_method="S256")
            extra["verifier"] = verifier
        return AUTH_URL + "?" + urllib.parse.urlencode(params), extra

    def exchange_code(self, code: str, verifier: str | None) -> dict:
        data = {"client_key": self.client_key, "client_secret": self.client_secret, "code": code,
                "grant_type": "authorization_code", "redirect_uri": self.redirect_uri}
        if verifier:
            data["code_verifier"] = verifier
        return self._save_token(self._token_request(data))

    def _token_request(self, data: dict) -> dict:
        try:
            r = self.http.post(TOKEN_URL, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
        except httpx.TransportError as e:
            raise TransientError(f"TikTok token request network error: {e}") from e
        body = r.json() if r.content else {}
        if r.status_code >= 500:
            raise TransientError(f"TikTok token endpoint HTTP {r.status_code}")
        if r.status_code != 200 or "access_token" not in body:
            raise NotConfigured(f"TikTok sign-in failed: {body.get('error_description') or body.get('error') or body}. "
                                "Reconnect TikTok in Settings.")
        return body

    def _save_token(self, body: dict) -> dict:
        tok = {"access_token": body["access_token"], "refresh_token": body.get("refresh_token"),
               "open_id": body.get("open_id"), "scope": body.get("scope"),
               "expires_at": time.time() + int(body.get("expires_in", 86400)) - 120,
               "refresh_expires_at": time.time() + int(body.get("refresh_expires_in", 0) or 31536000)}
        if "video.upload" not in (tok["scope"] or ""):
            raise PublishError("TikTok did not grant the video.upload scope. Check the app's Content Posting "
                               "API product and scopes in the TikTok developer portal, then reconnect.")
        self.store.set(self.platform, tok)
        return tok

    def access_token(self, force_refresh: bool = False) -> str:
        tok = self.store.get(self.platform)
        if not tok:
            raise NotConfigured("TikTok is not connected. Use Settings → Connect TikTok.")
        if force_refresh or tok["expires_at"] < time.time():
            if not tok.get("refresh_token"):
                raise NotConfigured("TikTok token expired; reconnect in Settings.")
            tok = self._save_token(self._token_request({
                "client_key": self.client_key, "client_secret": self.client_secret,
                "grant_type": "refresh_token", "refresh_token": tok["refresh_token"]}))
        return tok["access_token"]

    # ---- API helpers -----------------------------------------------------
    def _post_json(self, url: str, payload: dict, _retried: bool = False) -> dict:
        headers = {"Authorization": f"Bearer {self.access_token()}", "Content-Type": "application/json; charset=UTF-8"}
        try:
            r = self.http.post(url, headers=headers, json=payload)
        except httpx.TransportError as e:
            raise TransientError(f"TikTok network error: {e}") from e
        body = r.json() if r.content else {}
        err = body.get("error") or {}
        code = err.get("code")
        if r.status_code == 200 and code in (None, "ok"):
            return body.get("data") or {}
        if code in RECONNECT_CODES or r.status_code == 401:
            if code == "access_token_invalid" and not _retried:
                self.access_token(force_refresh=True)
                return self._post_json(url, payload, _retried=True)
            raise NotConfigured(f"TikTok authorisation problem ({code}); reconnect TikTok in Settings.")
        if code in TRANSIENT_CODES or r.status_code == 429 or r.status_code >= 500:
            raise TransientError(f"TikTok temporarily refused the request ({code or r.status_code}); will retry.")
        detail = EXPLAINED_CODES.get(code, err.get("message") or f"HTTP {r.status_code}")
        raise PublishError(f"TikTok error {code}: {detail} (log_id {err.get('log_id', '-')})")

    def _rate_limit(self) -> None:
        now = time.time()
        if len(_init_calls) == _init_calls.maxlen and now - _init_calls[0] < 60:
            raise TransientError("TikTok rate limit (6 uploads/minute) reached locally; will retry shortly.")
        _init_calls.append(now)

    # ---- resumable steps (used by the publishing job) ---------------------
    def init_upload(self, video: Path) -> tuple[str, str]:
        validate_video(video, **LIMITS)
        size = Path(video).stat().st_size
        chunk, total = chunk_plan(size)
        self._rate_limit()
        data = self._post_json(INIT_URL, {"source_info": {
            "source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk, "total_chunk_count": total}})
        return data["publish_id"], data["upload_url"]

    def upload_file(self, upload_url: str, video: Path) -> None:
        size = Path(video).stat().st_size
        chunk, total = chunk_plan(size)
        with open(video, "rb") as f:
            for i in range(total):
                first = i * chunk
                length = chunk if i < total - 1 else size - first
                f.seek(first)
                data = f.read(length)
                try:
                    ur = self.http.put(upload_url, content=data, headers={
                        "Content-Type": "video/mp4", "Content-Length": str(length),
                        "Content-Range": f"bytes {first}-{first + length - 1}/{size}"})
                except httpx.TransportError as e:
                    raise TransientError(f"TikTok upload interrupted at chunk {i + 1}/{total}: {e}") from e
                if ur.status_code >= 500 or ur.status_code == 429:
                    raise TransientError(f"TikTok upload chunk {i + 1}/{total}: HTTP {ur.status_code}")
                if ur.status_code not in (200, 201, 206):
                    raise PublishError(f"TikTok chunk {i + 1}/{total} upload failed: HTTP {ur.status_code}")

    def fetch_status(self, publish_id: str) -> tuple[str | None, str | None]:
        data = self._post_json(STATUS_URL, {"publish_id": publish_id})
        return data.get("status"), data.get("fail_reason")

    def wait_for_result(self, publish_id: str, poll_seconds: float = 3.0, max_polls: int = 40) -> PublishResult:
        for _ in range(max_polls):
            st, reason = self.fetch_status(publish_id)
            if st == "SEND_TO_USER_INBOX":
                return PublishResult("delivered_inbox", publish_id, "TikTok confirmed SEND_TO_USER_INBOX: the video "
                                     "is in your TikTok inbox. Open the TikTok app to edit and post it.", True)
            if st == "PUBLISH_COMPLETE":
                return PublishResult("published", publish_id, "TikTok confirmed PUBLISH_COMPLETE (posted from the "
                                     "app).", True)
            if st == "FAILED":
                return PublishResult("failed", publish_id, f"TikTok reported FAILED: {reason}", False)
            self.sleep(poll_seconds)
        return PublishResult("processing", publish_id, "Uploaded; TikTok has not confirmed processing yet.", False)

    def upload_to_inbox(self, video: Path, poll_seconds: float = 3.0, max_polls: int = 60) -> PublishResult:
        """One-shot helper (init → upload → poll). The job system uses the steps directly."""
        publish_id, upload_url = self.init_upload(video)
        self.upload_file(upload_url, video)
        res = self.wait_for_result(publish_id, poll_seconds, max_polls)
        if res.status == "processing":
            res.status = "uploaded"
        return res
