"""Instagram Reels publishing through Meta's RESUMABLE upload (Session 15).

Why resumable: the engine runs on a private Windows PC, and the normal Instagram flow needs Meta to fetch the video from a
public HTTPS URL. The resumable flow lets the engine send the bytes straight to Meta, so no public hosting is needed.

Flow (Meta docs: Instagram content publishing / resumable uploads; verify names against the live docs before the first real use):
  1. POST graph.facebook.com/<ver>/<IG_USER_ID>/media   media_type=REELS, upload_type=resumable, caption  -> {id, uri}
  2. POST rupload.facebook.com/ig-api-upload/<ver>/<container id>   (Authorization: OAuth <token>, offset: 0, file_size: N)
  3. poll GET graph.facebook.com/<ver>/<container id>?fields=status_code  until FINISHED (ERROR/EXPIRED -> refuse)
  4. POST graph.facebook.com/<ver>/<IG_USER_ID>/media_publish   creation_id=<container id>     -> {id}
  5. GET <media id>?fields=permalink  (the proof of what was published)

Instagram has NO draft step: step 4 is PUBLIC and immediate. So this class refuses to publish unless the owner has switched it on
(`public_enabled`, from INSTAGRAM_PUBLIC_PUBLISH) and the caller has already passed every gate in delivery.deliver_draft.
Status: IMPLEMENTED and TESTED-MOCKED only. Never run against Meta. Nothing here is registered unless Instagram is configured.
"""
from __future__ import annotations

import time
from pathlib import Path

import httpx

from .base import PublishError


class InstagramReelsPublisher:
    platform = "instagram"

    def __init__(self, token_for, *, version: str = "v23.0", public_enabled: bool = False, http: httpx.Client | None = None,
                 sleep=time.sleep, poll_seconds: float = 5.0, max_wait: float = 600.0):
        self.token_for = token_for                  # callable(brand) -> access token string (never stored here)
        self.version = version
        self.public_enabled = public_enabled
        self.http = http or httpx.Client(timeout=httpx.Timeout(60.0, read=600.0))
        self.sleep, self.poll_seconds, self.max_wait = sleep, poll_seconds, max_wait
        self.graph = f"https://graph.facebook.com/{version}"
        self.rupload = f"https://rupload.facebook.com/ig-api-upload/{version}"

    # ---- identity (read-only) -------------------------------------------------------------------------------
    def identity(self, brand: str, ig_user_id: str) -> dict:
        tok = self._token(brand)
        r = self.http.get(f"{self.graph}/{ig_user_id}", params={"fields": "id,username", "access_token": tok})
        body = self._json(r, "identity check")
        return {"id": str(body.get("id", "")), "username": body.get("username", "")}

    # ---- publish ---------------------------------------------------------------------------------------------
    def publish_reel(self, brand: str, ig_user_id: str, video: Path, caption: str) -> dict:
        if not self.public_enabled:
            raise PublishError("Instagram public publishing is OFF (INSTAGRAM_PUBLIC_PUBLISH). Instagram has no draft step, so "
                               "nothing is sent.")
        video = Path(video)
        if not video.is_file():
            raise PublishError("Video file not found.")
        tok = self._token(brand)
        live = self.identity(brand, ig_user_id)                       # re-check immediately before publishing
        if live["id"] != str(ig_user_id):
            raise PublishError("The authorised Instagram account is not this brand's verified account; publish refused.")
        size = video.stat().st_size
        c = self._json(self.http.post(f"{self.graph}/{ig_user_id}/media", data={
            "media_type": "REELS", "upload_type": "resumable", "caption": (caption or "")[:2200], "access_token": tok}),
            "creating the upload container")
        container = str(c.get("id") or "")
        if not container:
            raise PublishError("Instagram returned no upload container.")
        # Session 16 (first live test, 20 Nov): the self-built v23.0 rupload address got HTTP 400. Meta returns the upload
        # address to use in `uri`; use it (only if it is Meta's rupload host), else fall back to the built one.
        uri = str(c.get("uri") or "")
        target = uri if uri.startswith("https://rupload.facebook.com/") else f"{self.rupload}/{container}"
        data = video.read_bytes()

        # Meta requires the token that created the container for its resumable upload.
        r = self.http.post(target, content=data,
                           headers={"Authorization": f"OAuth {tok}", "offset": "0", "file_size": str(size)})
        up = self._json(r, "uploading the video")
        if up.get("success") is False:
            raise PublishError("Instagram did not accept the uploaded video.")
        self._wait_finished(container, tok)
        pub = self._json(self.http.post(f"{self.graph}/{ig_user_id}/media_publish",
                                        data={"creation_id": container, "access_token": tok}), "publishing")
        media_id = str(pub.get("id") or "")
        if not media_id:
            raise PublishError("Instagram returned no media id after publishing.")
        link = ""
        try:
            link = self._json(self.http.get(f"{self.graph}/{media_id}", params={"fields": "permalink", "access_token": tok}),
                              "reading the permalink").get("permalink", "")
        except PublishError:
            pass                                                     # published; the link is only a nicety
        return {"video_id": media_id, "permalink": link, "success": True}

    # ---- helpers ---------------------------------------------------------------------------------------------
    def _wait_finished(self, container: str, tok: str) -> None:
        waited = 0.0
        while True:
            st = self._json(self.http.get(f"{self.graph}/{container}", params={"fields": "status_code,status",
                                                                                 "access_token": tok}), "checking processing")
            code = st.get("status_code")
            if code == "FINISHED":
                return
            if code in ("ERROR", "EXPIRED"):
                raise PublishError(f"Instagram could not process the video ({code}: {st.get('status', '')}).")
            if waited >= self.max_wait:
                raise PublishError("Instagram is still processing the video after the wait limit; not published. "
                                   "Retry later (the upload is not repeated automatically).")
            self.sleep(self.poll_seconds)
            waited += self.poll_seconds

    def _token(self, brand: str) -> str:
        tok = self.token_for(brand)
        if not tok:
            raise PublishError(f"No Instagram authorisation is stored for {brand}.")
        return tok

    @staticmethod
    def _json(r: httpx.Response, what: str) -> dict:
        try:
            body = r.json() if r.content else {}
        except ValueError:
            body = {}
        if r.status_code >= 400 or "error" in body:
            err = (body.get("error") or {}) if isinstance(body, dict) else {}
            dbg = (body.get("debug_info") or {}) if isinstance(body, dict) else {}       # rupload's error format
            msg = (err.get("message") if isinstance(err, dict) else str(err)) or dbg.get("message") or ""
            if not msg:
                msg = (r.text or "")[:300].replace("\n", " ")             # never lose the platform's own reason
            raise PublishError(f"Instagram refused {what}: HTTP {r.status_code} {msg}".strip())
        return body
