"""Free cloud storage for finished clips: Google Drive (default), behind a small provider interface.

Design
- Owner-controlled OAuth ("Desktop app" client the owner creates in their own Google Cloud project;
  free). Scope `drive.file`: the engine can see and manage ONLY files it created itself — never the
  owner's other Drive files. Tokens live in the OS credential store (TokenStore), never in Git.
- One application folder ("Clipping Engine") holding a sub-folder per recording.
- Resumable uploads (8 MiB chunks, multiples of 256 KiB) whose session URI is persisted, so an
  interrupted upload resumes instead of starting again.
- Idempotent: every object carries appProperties.ce_key; before uploading, the engine looks for an
  existing file with that key, so a retried job never creates a duplicate.
- Verified: an object counts as stored only when Drive reports the same size and MD5 as the local file.
- Quota-aware: Drive's free 15 GB is shared with Gmail and Google Photos. Below CLOUD_MIN_FREE_GB the
  engine holds uploads (and therefore raw-recording deletion) and tells the owner.
A future provider (e.g. Cloudflare R2) implements the same `StorageProvider` methods; none is added now
because R2 needs a billing account.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import time
from pathlib import Path
from urllib.parse import urlencode

from .jobs import JobContext, PermanentError, TransientError

log = logging.getLogger("clipengine.cloud")

GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.file"
YOUTUBE_UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
CHUNK = 8 * 1024 * 1024


def md5_file(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


class GoogleOAuth:
    """Authorization-code flow with PKCE for an installed (desktop) app on a loopback redirect."""

    def __init__(self, client_id: str, client_secret: str, tokens, key: str, scopes: list[str], http=None):
        self.client_id, self.client_secret, self.tokens, self.key, self.scopes = client_id, client_secret, tokens, key, scopes
        self.http = http

    def _client(self):
        if self.http is None:
            import httpx
            self.http = httpx.Client(timeout=60)
        return self.http

    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def connected(self) -> bool:
        t = self.tokens.get(self.key)
        return bool(t and t.get("refresh_token"))

    def auth_url(self, redirect_uri: str) -> tuple[str, str, str]:
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(24)
        q = {"client_id": self.client_id, "redirect_uri": redirect_uri, "response_type": "code",
             "scope": " ".join(self.scopes), "access_type": "offline", "prompt": "consent", "state": state,
             "code_challenge": challenge, "code_challenge_method": "S256"}
        return f"{GOOGLE_AUTH}?{urlencode(q)}", state, verifier

    def exchange(self, code: str, redirect_uri: str, verifier: str) -> dict:
        r = self._client().post(GOOGLE_TOKEN, data={"code": code, "client_id": self.client_id,
                                                    "client_secret": self.client_secret, "redirect_uri": redirect_uri,
                                                    "grant_type": "authorization_code", "code_verifier": verifier})
        if r.status_code != 200:
            raise PermanentError(f"Google sign-in failed (HTTP {r.status_code}): {r.text[:200]}")
        d = r.json()
        tok = {"access_token": d["access_token"], "refresh_token": d.get("refresh_token"),
               "expires_at": time.time() + float(d.get("expires_in", 3600)), "scope": d.get("scope", "")}
        if not tok["refresh_token"]:
            raise PermanentError("Google did not return a refresh token; disconnect and connect again.")
        self.tokens.set(self.key, tok)
        return tok

    def access_token(self) -> str:
        tok = self.tokens.get(self.key)
        if not tok or not tok.get("refresh_token"):
            raise PermanentError("Not connected to Google. Open the Storage page and connect.")
        if tok.get("expires_at", 0) > time.time() + 120:
            return tok["access_token"]
        r = self._client().post(GOOGLE_TOKEN, data={"client_id": self.client_id, "client_secret": self.client_secret,
                                                    "refresh_token": tok["refresh_token"], "grant_type": "refresh_token"})
        if r.status_code in (400, 401):
            raise PermanentError("Google sign-in has expired or was revoked; connect again on the Storage page.")
        if r.status_code != 200:
            raise TransientError(f"Google token refresh failed (HTTP {r.status_code})")
        d = r.json()
        tok.update(access_token=d["access_token"], expires_at=time.time() + float(d.get("expires_in", 3600)))
        self.tokens.set(self.key, tok)
        return tok["access_token"]

    def disconnect(self) -> None:
        self.tokens.delete(self.key)


class StorageProvider:
    name = "none"

    def connected(self) -> bool:
        return False

    def quota(self) -> dict:
        raise NotImplementedError

    def ensure_folder(self, name: str, parent: str | None = None) -> str:
        raise NotImplementedError

    def find(self, ce_key: str) -> dict | None:
        raise NotImplementedError

    def upload(self, path: Path, name: str, folder: str, ce_key: str, session_uri: str | None,
               save_session) -> dict:
        raise NotImplementedError

    def delete(self, remote_id: str) -> None:
        raise NotImplementedError

    def metadata(self, remote_id: str) -> dict:
        raise NotImplementedError

    def download(self, remote_id: str, dest: Path, progress=None) -> int:
        raise NotImplementedError


class GoogleDriveProvider(StorageProvider):
    name = "gdrive"
    API = "https://www.googleapis.com/drive/v3"
    UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"

    def __init__(self, oauth: GoogleOAuth, http=None):
        self.oauth = oauth
        self.http = http

    def _client(self):
        if self.http is None:
            import httpx
            self.http = httpx.Client(timeout=120)
        return self.http

    def _h(self, extra: dict | None = None) -> dict:
        return {"Authorization": f"Bearer {self.oauth.access_token()}", **(extra or {})}

    def _check(self, r, what: str):
        if r.status_code in (401, 403) and "storageQuotaExceeded" in r.text:
            raise TransientError(f"Google Drive is full ({what}); uploads are held.", retry_after=3600)
        if r.status_code == 429 or r.status_code >= 500 or (r.status_code == 403 and "rateLimit" in r.text):
            raise TransientError(f"Google Drive temporarily refused {what} (HTTP {r.status_code})", retry_after=120)
        if r.status_code >= 400:
            raise PermanentError(f"Google Drive {what} failed (HTTP {r.status_code}): {r.text[:200]}")
        return r

    def connected(self) -> bool:
        return self.oauth.connected()

    def quota(self) -> dict:
        r = self._check(self._client().get(f"{self.API}/about", params={"fields": "storageQuota,user(emailAddress)"},
                                           headers=self._h()), "quota check")
        q = r.json().get("storageQuota", {})
        limit = int(q["limit"]) if q.get("limit") else None
        usage = int(q.get("usage", 0))
        return {"limit": limit, "usage": usage, "usage_in_drive": int(q.get("usageInDrive", 0)),
                "free": (limit - usage) if limit else None}

    def ensure_folder(self, name: str, parent: str | None = None) -> str:
        key = f"folder:{parent or 'root'}:{name}"
        found = self.find(key)
        if found:
            return found["id"]
        meta = {"name": name, "mimeType": "application/vnd.google-apps.folder", "appProperties": {"ce_key": key}}
        if parent:
            meta["parents"] = [parent]
        r = self._check(self._client().post(f"{self.API}/files", params={"fields": "id"}, json=meta,
                                            headers=self._h()), "folder creation")
        return r.json()["id"]

    def find(self, ce_key: str) -> dict | None:
        q = f"appProperties has {{ key='ce_key' and value='{ce_key.replace(chr(39), '')}' }} and trashed=false"
        r = self._check(self._client().get(f"{self.API}/files", params={"q": q, "fields": "files(id,name,size,md5Checksum)",
                                                                         "spaces": "drive"}, headers=self._h()), "lookup")
        files = r.json().get("files", [])
        return files[0] if files else None

    def upload(self, path: Path, name: str, folder: str, ce_key: str, session_uri: str | None,
               save_session) -> dict:
        size = path.stat().st_size
        c = self._client()
        offset = 0
        if session_uri:
            r = c.put(session_uri, headers=self._h({"Content-Range": f"bytes */{size}", "Content-Length": "0"}))
            if r.status_code in (200, 201):
                return r.json()
            if r.status_code == 308:
                rng = r.headers.get("Range")
                offset = int(rng.split("-")[1]) + 1 if rng else 0
            else:
                session_uri = None          # expired session: start again (the lookup prevents duplicates)
        if not session_uri:
            meta = {"name": name, "parents": [folder], "appProperties": {"ce_key": ce_key}}
            r = self._check(c.post(self.UPLOAD, params={"uploadType": "resumable", "fields": "id,size,md5Checksum"},
                                   json=meta, headers=self._h({"X-Upload-Content-Length": str(size)})), "upload start")
            session_uri = r.headers["Location"]
            save_session(session_uri)
            offset = 0
        with open(path, "rb") as f:
            while True:
                f.seek(offset)
                chunk = f.read(CHUNK)
                end = offset + len(chunk) - 1
                hdr = {"Content-Range": f"bytes {offset}-{end}/{size}" if chunk else f"bytes */{size}",
                       "Content-Length": str(len(chunk))}
                r = c.put(session_uri, content=chunk, headers=self._h(hdr))
                if r.status_code in (200, 201):
                    return r.json()
                if r.status_code == 308:
                    rng = r.headers.get("Range")
                    offset = int(rng.split("-")[1]) + 1 if rng else 0
                    continue
                self._check(r, "upload")
                raise TransientError(f"Unexpected upload response HTTP {r.status_code}")

    def metadata(self, remote_id: str) -> dict:
        r = self._check(self._client().get(f"{self.API}/files/{remote_id}", params={"fields": "id,size,md5Checksum"},
                                           headers=self._h()), "verification")
        return r.json()

    def delete(self, remote_id: str) -> None:
        r = self._client().delete(f"{self.API}/files/{remote_id}", headers=self._h())
        if r.status_code not in (200, 204, 404):
            self._check(r, "deletion")

    def download(self, remote_id: str, dest: Path, progress=None) -> int:
        """Stream a Drive file into `dest` (the caller supplies a temporary path). Returns the bytes written."""
        written = 0
        with self._client().stream("GET", f"{self.API}/files/{remote_id}", params={"alt": "media"},
                                   headers=self._h()) as r:
            if r.status_code >= 400:
                r.read()
                self._check(r, "download")
            with open(dest, "wb") as f:
                for chunk in r.iter_bytes(1 << 20):
                    f.write(chunk)
                    written += len(chunk)
                    if progress:
                        progress(written)
        return written


class CloudService:
    """Queues and runs verified uploads of finished clips (job kind `cloud_upload`, a light job)."""
    KINDS = ("video", "thumb", "transcript", "metadata")

    def __init__(self, settings, db, queue, tokens, provider: StorageProvider | None = None):
        self.settings, self.db, self.queue = settings, db, queue
        self.oauth = GoogleOAuth(settings.google_client_id, settings.google_client_secret, tokens, "gdrive",
                                 [DRIVE_SCOPE])
        self.provider = provider or (GoogleDriveProvider(self.oauth) if settings.cloud_provider == "gdrive"
                                     else StorageProvider())
        self.on_verified = []          # callbacks(recording_id) after a clip's objects are all verified
        queue.register("cloud_upload", self.run)

    # ---- status ------------------------------------------------------------------------------
    def status(self) -> dict:
        rows = self.db.query("SELECT state, COUNT(*) n, COALESCE(SUM(size),0) b FROM cloud_objects GROUP BY state")
        st = {"provider": self.provider.name, "configured": self.oauth.configured(),
              "connected": self.provider.connected(),
              "objects": {r["state"]: {"n": r["n"], "gb": r["b"] / 1e9} for r in rows},
              "label": "Not set up" if not self.oauth.configured() else
              ("Connected" if self.provider.connected() else "READY FOR OWNER CONNECTION")}
        cached = self.db.get_kv("cloud_quota")
        if cached:
            st["quota"] = json.loads(cached)
        return st

    def refresh_quota(self) -> dict | None:
        if not self.provider.connected():
            return None
        q = self.provider.quota()
        q["checked_at"] = time.time()
        self.db.set_kv("cloud_quota", json.dumps(q))
        return q

    def uploads_held(self) -> str | None:
        q = json.loads(self.db.get_kv("cloud_quota") or "{}")
        if q.get("free") is not None and q["free"] < self.settings.cloud_min_free_gb * 1e9:
            return (f"Google Drive has {q['free'] / 1e9:.1f} GB free (minimum {self.settings.cloud_min_free_gb:g} GB). "
                    "Uploads and raw-recording deletion are held until space is freed in Drive.")
        return None

    # ---- producer ----------------------------------------------------------------------------
    def enqueue_clip(self, clip_id: int) -> int | None:
        """Called when a clip is rendered. Creates the object rows (idempotent) and queues one upload job.
        Without a connection the rows wait (state 'pending') and nothing is sent."""
        clip = self.db.clip(clip_id)
        if not clip or not clip["video_path"]:
            return None
        files = self._files(clip)
        for kind, path in files.items():
            row = self.db.one("SELECT * FROM cloud_objects WHERE provider=? AND owner_kind='clip' AND owner_id=? AND kind=?",
                              (self.provider.name, clip_id, kind))
            if row and row["local_path"] != str(path) and row["state"] in ("verified", "uploaded"):
                # a re-render replaced the file: upload the new version
                self.db.update("cloud_objects", row["id"], local_path=str(path), state="pending", session_uri=None,
                               local_md5=None, size=None)
            elif not row:
                self.db.insert("cloud_objects", provider=self.provider.name, owner_kind="clip", owner_id=clip_id,
                               kind=kind, local_path=str(path), state="pending")
        if not self.provider.connected():
            return None
        return self.queue.enqueue("cloud_upload", recording_id=clip["recording_id"], clip_id=clip_id,
                                  dedupe_key=f"cloud:{clip_id}", max_attempts=8, priority=30)

    def _files(self, clip) -> dict:
        d = Path(clip["video_path"]).parent
        out = {"video": Path(clip["video_path"])}
        if clip["thumb_path"] and Path(clip["thumb_path"]).is_file():
            out["thumb"] = Path(clip["thumb_path"])
        if (d / "transcript.txt").is_file():
            out["transcript"] = d / "transcript.txt"
        meta = d / "metadata.json"
        rec = self.db.recording(clip["recording_id"])
        meta.write_text(json.dumps({
            "clip_id": clip["id"], "recording_id": clip["recording_id"], "title": clip["title"] or rec["title"],
            "source_url": rec["source_url"], "source_title": rec["title"], "start": clip["start"], "end": clip["end"],
            "niche": clip["niche_key"], "rights_status": clip["rights_status"], "safety_status": clip["safety_status"],
            "description": clip["description"], "render_rev": clip["render_rev"]}, indent=1), encoding="utf-8")
        out["metadata"] = meta
        return out

    # ---- job ---------------------------------------------------------------------------------
    def run(self, ctx: JobContext) -> None:
        """A dropped connection or time-out (Wi-Fi blip) is retried like any temporary refusal;
        before, it failed the upload for good, which also kept the raw recording on disk forever."""
        import httpx
        try:
            self._run(ctx)
        except httpx.TransportError as e:
            raise TransientError(f"Network problem talking to Google Drive ({type(e).__name__}: {e}); "
                                 "will retry.", retry_after=120) from e

    def _run(self, ctx: JobContext) -> None:
        clip_id = ctx.job["clip_id"]
        clip = self.db.clip(clip_id)
        if not clip:
            return
        if not self.provider.connected():
            raise PermanentError("Google Drive is not connected (Storage page).")
        try:
            self.refresh_quota()
        except TransientError:
            raise
        except Exception as e:  # noqa: BLE001 - a quota check failure is not fatal for the upload
            ctx.log(f"quota check failed: {e}", "WARNING")
        held = self.uploads_held()
        if held:
            raise TransientError(held, retry_after=3600)
        rec = self.db.recording(clip["recording_id"])
        root = self.provider.ensure_folder(self.settings.cloud_folder_name)
        folder = self.provider.ensure_folder(f"rec{rec['id']:05d} {rec['title'][:60]}", root)
        rows = self.db.query("SELECT * FROM cloud_objects WHERE provider=? AND owner_kind='clip' AND owner_id=? "
                             "AND state NOT IN ('verified','deleted')", (self.provider.name, clip_id))
        for i, row in enumerate(rows):
            path = Path(row["local_path"] or "")
            if not path.is_file():
                self.db.update("cloud_objects", row["id"], state="failed", error="local file missing")
                continue
            ctx.progress(i / max(1, len(rows)), f"Uploading {row['kind']} to Google Drive")
            md5, size = md5_file(path), path.stat().st_size
            ce_key = f"clip:{clip_id}:{row['kind']}:{md5}"
            self.db.update("cloud_objects", row["id"], state="uploading", local_md5=md5, size=size,
                           attempts=row["attempts"] + 1, remote_folder=folder)
            existing = self.provider.find(ce_key)
            if existing and existing.get("md5Checksum") == md5:
                info = existing
                ctx.log(f"{row['kind']}: already in Drive (no duplicate upload)")
            else:
                info = self.provider.upload(path, f"clip{clip_id:05d}_{row['kind']}{path.suffix}", folder, ce_key,
                                            row["session_uri"],
                                            lambda uri, rid=row["id"]: self.db.update("cloud_objects", rid, session_uri=uri))
            remote = info.get("id")
            check = info if info.get("md5Checksum") else self.provider.metadata(remote)
            if check.get("md5Checksum") != md5 or int(check.get("size") or -1) != size:
                self.db.update("cloud_objects", row["id"], state="failed", remote_id=remote,
                               error="Drive copy does not match the local file (size/MD5)")
                raise TransientError(f"Verification failed for {row['kind']}; will retry")
            self.db.update("cloud_objects", row["id"], state="verified", remote_id=remote, verified_at=time.time(),
                           session_uri=None, error=None)
        ctx.progress(1.0, "Uploaded and verified", force=True)
        for cb in self.on_verified:
            cb(rec["id"])

    def clip_verified(self, clip_id: int, required=("video", "metadata")) -> bool:
        rows = {r["kind"]: r["state"] for r in self.db.query(
            "SELECT kind, state FROM cloud_objects WHERE owner_kind='clip' AND owner_id=?", (clip_id,))}
        return all(rows.get(k) == "verified" for k in required)

    def delete_clip_objects(self, clip_id: int, force: bool = False) -> int:
        """Delete this engine's own Drive copies of a clip (only objects recorded as ours).

        The Drive master is the recoverable copy behind local cleanup, so it is PROTECTED: refused while the local video
        has been removed, and for any clip that was not rejected while KEEP_DRIVE_MASTER is on (the default). `force`
        exists only for an explicit owner action."""
        clip = self.db.clip(clip_id)
        if clip and not force:
            if clip["local_deleted_at"]:
                log.warning("clip %s: Drive copy kept (it is the only copy of the finished video)", clip_id)
                return 0
            if getattr(self.settings, "keep_drive_master", True) and clip["status"] != "rejected":
                return 0
        n = 0
        for row in self.db.query("SELECT * FROM cloud_objects WHERE owner_kind='clip' AND owner_id=? AND remote_id IS NOT NULL "
                                 "AND state != 'deleted'", (clip_id,)):
            if not self.provider.connected():
                break                       # cannot delete now; the row stays as it is and is retried later
            try:
                self.provider.delete(row["remote_id"])
                self.db.update("cloud_objects", row["id"], state="deleted", deleted_at=time.time())
                n += 1
            except Exception as e:  # noqa: BLE001
                log.warning("could not delete cloud object %s: %s", row["id"], e)
        return n
