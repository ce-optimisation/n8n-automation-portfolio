"""Publishing as persistent, resumable background jobs.

Each request creates a `publish_attempts` row and a `publish_clip` job. The job advances
the attempt through checkpointed stages, so a crash, restart or transient error resumes
where it stopped instead of uploading (or publishing) twice:

TikTok:     (none) → initiated(publish_id) → uploaded → delivered_inbox | failed
Instagram:  (none) → container(container_id) → uploaded → ready → publish_requested → published | failed

Before Instagram's media_publish call the stage is set to `publish_requested`; if the
outcome is then unknown (crash, timeout), the container's status_code is checked first
— PUBLISHED means it went out and is recorded, never re-published.
"""
from __future__ import annotations

import secrets
import time
from pathlib import Path

from ..jobs import JobContext, PermanentError, TransientError
from .base import (ACTIVE_STATES, CONFIRMED_STATES, STATES, NotConfigured, PublishError, editorial_gate)

PUBLIC_TOKEN_TTL = 2 * 3600


class PublishService:
    def __init__(self, settings, db, queue, tiktok, instagram, sleep=time.sleep):
        self.settings, self.db, self.queue = settings, db, queue
        self.clients = {"tiktok": tiktok, "instagram": instagram}
        self.sleep = sleep
        queue.register("publish_clip", self.run)
        queue.register("publish_clip:failed", self._on_failed)

    # ---- queries ---------------------------------------------------------
    def latest(self, clip_id: int) -> dict:
        out = {}
        for r in self.db.query("SELECT * FROM publish_attempts WHERE clip_id = ? ORDER BY id", (clip_id,)):
            out[r["platform"]] = dict(r, label=STATES.get(r["status"], r["status"]))
        return out

    def connections(self) -> dict:
        return {name: c.status()["connected"] for name, c in self.clients.items()}

    def summary(self, clip, conn: dict | None = None) -> dict:
        """Per-platform publishing state for the dashboard, including readiness."""
        conn = conn or self.connections()
        latest = self.latest(clip["id"])
        gate = editorial_gate(clip)
        res = {}
        for name, client in self.clients.items():
            if name in latest:
                res[name] = latest[name]
            elif gate:
                res[name] = {"status": "not_ready", "label": "Awaiting editorial review" if clip["status"] != "approved"
                             else "Needs rights/flag decision"}
            elif not conn.get(name):
                res[name] = {"status": "awaiting_connection", "label": STATES["awaiting_connection"]}
            else:
                res[name] = {"status": "ready", "label": "Ready to send"}
        return res

    # ---- requests --------------------------------------------------------
    def request(self, clip_id: int, platform: str, *, again: bool = False) -> int:
        if platform not in self.clients:
            raise PublishError("Unknown platform")
        clip = self.db.clip(clip_id)
        if not clip:
            raise PublishError("Clip not found")
        problems = editorial_gate(clip)
        if problems:
            raise PublishError(" ".join(problems))
        if not self.clients[platform].status()["connected"]:
            raise NotConfigured(f"{platform.title()} is not connected. Open Settings → Setup checklist.")
        prev = self.latest(clip_id).get(platform)
        if prev and prev["status"] in ACTIVE_STATES:
            raise PublishError(f"A {platform} upload for this clip is already in progress ({prev['label']}).")
        if prev and prev["status"] in CONFIRMED_STATES and not again:
            raise PublishError(f"This clip was already sent to {platform} ({prev['label']}). Use 'Send again' "
                               "only if you really want a second copy.")
        aid = self.db.insert("publish_attempts", clip_id=clip_id, platform=platform, status="queued",
                             detail="Waiting in queue")
        jid = self.queue.enqueue("publish_clip", clip_id=clip_id, recording_id=clip["recording_id"],
                                 payload={"attempt_id": aid}, dedupe_key=f"publish:{aid}", max_attempts=8)
        self.db.update("publish_attempts", aid, job_id=jid)
        return aid

    # ---- job -------------------------------------------------------------
    def _set(self, aid: int, **fields) -> None:
        self.db.update("publish_attempts", aid, **fields)

    def _on_failed(self, ctx: JobContext) -> None:
        aid = ctx.payload.get("attempt_id")
        att = self.db.one("SELECT * FROM publish_attempts WHERE id = ?", (aid,))
        if att and att["status"] not in CONFIRMED_STATES and att["status"] != "awaiting_connection":
            job = self.db.one("SELECT error FROM jobs WHERE id = ?", (ctx.id,))
            self._set(aid, status="failed", detail=(job["error"] if job else "failed")[:1000])
        self._revoke_tokens(att["clip_id"] if att else None)

    def run(self, ctx: JobContext) -> None:
        aid = ctx.payload["attempt_id"]
        att = self.db.one("SELECT * FROM publish_attempts WHERE id = ?", (aid,))
        if not att or att["status"] in CONFIRMED_STATES or att["status"] in ("failed", "cancelled"):
            return
        clip = self.db.clip(att["clip_id"])
        problems = editorial_gate(clip) if clip else ["Clip deleted"]
        if problems:
            self._set(aid, status="failed", detail="Blocked by editorial gate: " + " ".join(problems))
            return
        try:
            if att["platform"] == "tiktok":
                self._run_tiktok(ctx, att, clip)
            else:
                self._run_instagram(ctx, att, clip)
        except NotConfigured as e:
            self._set(aid, status="awaiting_connection", detail=str(e))
            raise PermanentError(str(e)) from e
        except TransientError as e:
            self._set(aid, status="retrying", detail=str(e)[:1000])
            raise
        except PublishError as e:
            self._set(aid, status="failed", detail=str(e)[:1000])
            raise PermanentError(str(e)) from e

    def _run_tiktok(self, ctx, att, clip) -> None:
        tt = self.clients["tiktok"]
        aid, video = att["id"], Path(clip["video_path"])
        if att["stage"] != "uploaded":
            # Not uploaded (or interrupted mid-upload): start a fresh upload session.
            self._set(aid, status="uploading", detail="Initialising TikTok upload")
            publish_id, upload_url = tt.init_upload(video)
            self._set(aid, remote_id=publish_id, stage="initiated", detail="Uploading to TikTok")
            tt.upload_file(upload_url, video)
            self._set(aid, stage="uploaded", status="processing", detail="Uploaded; waiting for TikTok to confirm")
            att = self.db.one("SELECT * FROM publish_attempts WHERE id = ?", (aid,))
        res = tt.wait_for_result(att["remote_id"])
        if res.confirmed:
            self._set(aid, status=res.status, detail=res.detail, confirmed_at=time.time())
        elif res.status == "failed":
            raise PublishError(res.detail)
        else:
            raise TransientError("TikTok is still processing the upload; checking again shortly.")

    def _public_url(self, clip_id: int) -> str:
        base = self.settings.public_media_base_url
        if not base.startswith("https://"):
            raise NotConfigured("INSTAGRAM_AUTH_MODE=instagram requires PUBLIC_MEDIA_BASE_URL (an https address "
                                "that Meta can reach, e.g. a Tailscale Funnel URL).")
        token = secrets.token_urlsafe(32)
        self.db.insert("public_media_tokens", token=token, clip_id=clip_id, expires_at=time.time() + PUBLIC_TOKEN_TTL)
        return f"{base}/public/media/{token}.mp4"

    def _revoke_tokens(self, clip_id: int | None) -> None:
        if clip_id is not None:
            self.db.execute("DELETE FROM public_media_tokens WHERE clip_id = ?", (clip_id,))
        self.db.execute("DELETE FROM public_media_tokens WHERE expires_at < ?", (time.time(),))

    def _run_instagram(self, ctx, att, clip) -> None:
        ig = self.clients["instagram"]
        aid, video = att["id"], Path(clip["video_path"])
        stage, container = att["stage"], att["container_id"]
        if stage == "publish_requested":
            code, _ = ig.container_status(container)
            if code == "PUBLISHED":
                self._set(aid, status="published", stage="published", confirmed_at=time.time(),
                          detail="Instagram reports the container as PUBLISHED (confirmed after an interrupted "
                                 "publish call; not published twice).")
                self._revoke_tokens(clip["id"])
                return
            stage = "ready"
        if stage in (None, "", "container") and not (stage == "container" and container):
            self._set(aid, status="uploading", detail="Creating Instagram Reels container")
            video_url = self._public_url(clip["id"]) if ig.mode == "instagram" else None
            container, uri = ig.create_container(video, clip["description"] or "", video_url)
            self._set(aid, container_id=container, stage="container", detail="Uploading to Instagram")
            if uri:
                ig.upload(uri, video)
            self._set(aid, stage="uploaded", status="processing", detail="Instagram is processing the video")
            stage = "uploaded"
        elif stage == "container":
            uri = f"https://rupload.facebook.com/ig-api-upload/{ig.version}/{container}"
            if ig.mode != "instagram":
                ig.upload(uri, video)
            self._set(aid, stage="uploaded", status="processing")
            stage = "uploaded"
        if stage == "uploaded":
            for _ in range(60):
                code, detail = ig.container_status(container)
                if code == "FINISHED":
                    break
                if code == "EXPIRED":
                    self._set(aid, stage=None, container_id=None, detail="Container expired; recreating")
                    raise TransientError("Instagram container expired before publishing; recreating it.")
                if code == "ERROR":
                    raise PublishError(f"Instagram could not process the video: {detail}")
                self.sleep(5)
            else:
                raise TransientError("Instagram is still processing the video; checking again shortly.")
            self._set(aid, stage="ready")
        self._set(aid, stage="publish_requested", detail="Publishing on Instagram")
        media_id = ig.publish(container)
        link = ig.permalink(media_id)
        self._set(aid, status="published", stage="published", remote_id=media_id, confirmed_at=time.time(),
                  detail=f"Instagram confirmed publication{': ' + link if link else ''} (media id {media_id}).")
        self._revoke_tokens(clip["id"])
