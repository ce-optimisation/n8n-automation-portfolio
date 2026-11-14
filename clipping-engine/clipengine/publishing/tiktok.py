"""TikTok Content Posting API: upload to the creator's inbox (draft).

Official flow (scope ``video.upload``):
  1. OAuth (Login Kit) -> user access token.
  2. POST /v2/post/publish/inbox/video/init/   (source FILE_UPLOAD) -> publish_id, upload_url
  3. PUT the bytes to upload_url (Content-Range per chunk).
  4. POST /v2/post/publish/status/fetch/ until SEND_TO_USER_INBOX / FAILED.

The creator then finishes and posts the video inside the TikTok app. This
module never reports success unless the status endpoint confirms it.
Rate limit: 6 init requests per minute per user token (enforced locally too).
"""
from __future__ import annotations

import base64
import hashlib
import secrets
import time
import urllib.parse
from collections import deque
from pathlib import Path

import httpx

from .base import NotConfigured, PublishError, PublishResult

AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
INIT_URL = "https://open.tiktokapis.com/v2/post/publish/inbox/video/init/"
STATUS_URL = "https://open.tiktokapis.com/v2/post/publish/status/fetch/"

MIN_CHUNK = 5 * 1024 * 1024
DEFAULT_CHUNK = 10 * 1024 * 1024
MAX_FINAL_CHUNK = 128 * 1024 * 1024

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


class TikTokClient:
    platform = "tiktok"

    def __init__(self, client_key: str, client_secret: str, redirect_uri: str, store,
                 http: httpx.Client | None = None, sleep=time.sleep):
        self.client_key = client_key
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.store = store
        self.http = http or httpx.Client(timeout=60)
        self.sleep = sleep

    # ---- configuration / OAuth -------------------------------------------
    @property
    def configured(self) -> bool:
        return bool(self.client_key and self.client_secret and self.redirect_uri)

    def status(self) -> dict:
        tok = self.store.get(self.platform)
        return {"configured": self.configured, "connected": bool(tok),
                "scope": (tok or {}).get("scope"), "expires_at": (tok or {}).get("expires_at")}

    def authorize_url(self) -> tuple[str, dict]:
        if not self.configured:
            raise NotConfigured("Set TIKTOK_CLIENT_KEY, TIKTOK_CLIENT_SECRET and TIKTOK_REDIRECT_URI in .env")
        state = secrets.token_urlsafe(24)
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        params = {"client_key": self.client_key, "scope": "user.info.basic,video.upload",
                  "response_type": "code", "redirect_uri": self.redirect_uri, "state": state,
                  "code_challenge": challenge, "code_challenge_method": "S256"}
        return AUTH_URL + "?" + urllib.parse.urlencode(params), {"state": state, "verifier": verifier}

    def exchange_code(self, code: str, verifier: str | None) -> dict:
        data = {"client_key": self.client_key, "client_secret": self.client_secret, "code": code,
                "grant_type": "authorization_code", "redirect_uri": self.redirect_uri}
        if verifier:
            data["code_verifier"] = verifier
        return self._save_token(self._token_request(data))

    def _token_request(self, data: dict) -> dict:
        r = self.http.post(TOKEN_URL, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
        body = r.json() if r.content else {}
        if r.status_code != 200 or "access_token" not in body:
            raise PublishError(f"TikTok token request failed: {body.get('error_description') or body}")
        return body

    def _save_token(self, body: dict) -> dict:
        tok = {"access_token": body["access_token"], "refresh_token": body.get("refresh_token"),
               "open_id": body.get("open_id"), "scope": body.get("scope"),
               "expires_at": time.time() + int(body.get("expires_in", 86400)) - 60,
               "refresh_expires_at": time.time() + int(body.get("refresh_expires_in", 0) or 0)}
        if "video.upload" not in (tok["scope"] or ""):
            raise PublishError("TikTok did not grant the video.upload scope.")
        self.store.set(self.platform, tok)
        return tok

    def access_token(self) -> str:
        tok = self.store.get(self.platform)
        if not tok:
            raise NotConfigured("TikTok is not connected. Use Settings → Connect TikTok.")
        if tok["expires_at"] < time.time():
            if not tok.get("refresh_token"):
                raise NotConfigured("TikTok token expired; reconnect in Settings.")
            tok = self._save_token(self._token_request({
                "client_key": self.client_key, "client_secret": self.client_secret,
                "grant_type": "refresh_token", "refresh_token": tok["refresh_token"]}))
        return tok["access_token"]

    # ---- upload ----------------------------------------------------------
    def _rate_limit(self) -> None:
        now = time.time()
        if len(_init_calls) == _init_calls.maxlen and now - _init_calls[0] < 60:
            raise PublishError("TikTok rate limit (6 uploads/minute) reached locally; try again shortly.")
        _init_calls.append(now)

    def upload_to_inbox(self, video: Path, poll_seconds: float = 3.0, max_polls: int = 60) -> PublishResult:
        token = self.access_token()
        size = Path(video).stat().st_size
        chunk, total = chunk_plan(size)
        self._rate_limit()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8"}
        r = self.http.post(INIT_URL, headers=headers, json={"source_info": {
            "source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk, "total_chunk_count": total}})
        body = r.json() if r.content else {}
        err = (body.get("error") or {})
        if r.status_code != 200 or err.get("code") not in (None, "ok"):
            raise PublishError(f"TikTok init failed ({r.status_code}): {err.get('code')} {err.get('message')}")
        publish_id = body["data"]["publish_id"]
        upload_url = body["data"]["upload_url"]
        with open(video, "rb") as f:
            for i in range(total):
                first = i * chunk
                length = chunk if i < total - 1 else size - first
                data = f.read(length)
                ur = self.http.put(upload_url, content=data, headers={
                    "Content-Type": "video/mp4", "Content-Length": str(length),
                    "Content-Range": f"bytes {first}-{first + length - 1}/{size}"})
                if ur.status_code not in (200, 201, 206):
                    raise PublishError(f"TikTok chunk {i + 1}/{total} upload failed: HTTP {ur.status_code}")
        for _ in range(max_polls):
            sr = self.http.post(STATUS_URL, headers=headers, json={"publish_id": publish_id})
            sb = sr.json() if sr.content else {}
            st = (sb.get("data") or {}).get("status")
            if st in ("SEND_TO_USER_INBOX", "PUBLISH_COMPLETE"):
                return PublishResult("confirmed", publish_id,
                                     f"TikTok confirmed status {st}: the video is in the creator's TikTok inbox "
                                     "to review and post from the app.", True)
            if st == "FAILED":
                return PublishResult("failed", publish_id,
                                     f"TikTok reported FAILED: {(sb.get('data') or {}).get('fail_reason')}", False)
            self.sleep(poll_seconds)
        return PublishResult("uploaded", publish_id, "Upload sent; TikTok has not yet confirmed processing. "
                             "Check status again later.", False)
