"""Recording lifecycle, safe automatic deletion of raw recordings, and final-clip retention.

Raw recordings are deleted automatically ONLY when ALL of these hold (each is checked, logged and shown):
  1 engine_owned       retrieved/recorded by the engine itself after this feature was introduced
                       (never an owner upload, an inbox file, or any recording from before the feature)
  2 finalised          the file exists inside the engine's recordings folder and the recording is complete
  3 transcript         the transcript is stored in the database
  4 rendered           selection and rendering finished (status ready) and no clip is waiting to render
  5 integrity          every surviving clip's MP4 probes as a valid video with audio
  6 cloud_verified     every surviving clip's video and metadata are in cloud storage, size+MD5 verified
  7 no_jobs            no queued or running job refers to the recording
  8 recorded           the decision is written to the database BEFORE the file is removed (crash-safe)
A recording that produced no valid clip is HELD for review, never deleted. Deleting the raw file
means clips can no longer be re-cut from the source; captions, transcript, timecodes, provenance and
the finished clips are kept.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

log = logging.getLogger("clipengine.lifecycle")

STAGES = ("queued", "recording", "downloaded", "transcribing", "selecting", "rendering", "uploading",
          "awaiting_review", "held", "failed", "completed")
ENGINE_SOURCES = {"url", "monitor", "live", "ytdlp", "direct"}


def mark_engine_owned_since(db) -> float:
    """The moment automatic deletion became available on this installation. Recordings made before it are
    never eligible (no retroactive deletion without the owner's explicit authorisation)."""
    since = db.get_kv("engine_owned_since")
    if not since:
        since = str(time.time())
        db.set_kv("engine_owned_since", since)
    return float(since)


def is_engine_acquired(rec) -> bool:
    meta = json.loads(rec["metadata_json"] or "{}")
    method = (meta.get("retrieval") or {}).get("method", "")
    return (rec["source_type"] in ("url", "monitor") or rec["origin"] == "live_capture"
            or method in ("direct", "ytdlp", "yt_dlp", "live", "monitor"))


class Lifecycle:
    def __init__(self, settings, db, queue, cloud):
        self.settings, self.db, self.queue, self.cloud = settings, db, queue, cloud
        self.since = mark_engine_owned_since(db)
        cloud.on_verified.append(lambda rid: self.check_recording(rid))

    def claim_ownership(self, rid: int) -> bool:
        """Called when a recording is registered/processed: flag engine-acquired new recordings."""
        rec = self.db.recording(rid)
        if rec and not rec["engine_owned"] and rec["created_at"] >= self.since and is_engine_acquired(rec):
            self.db.update("recordings", rid, engine_owned=1)
            return True
        return bool(rec and rec["engine_owned"])

    def stage(self, rec) -> str:
        if rec["raw_deleted_at"]:
            return "completed"
        st = rec["status"]
        if rec["raw_hold_reason"]:
            return "held"
        return {"imported": "downloaded", "transcribing": "transcribing", "transcribed": "selecting",
                "analysing": "selecting", "clips_queued": "rendering", "ready": "awaiting_review",
                "failed": "failed"}.get(st, st)

    # ---- gates -------------------------------------------------------------------------------
    def gates(self, rid: int) -> list[dict]:
        rec = self.db.recording(rid)
        if not rec:
            return []
        from . import media
        g = []

        def add(key, ok, detail):
            g.append({"gate": key, "ok": bool(ok), "detail": detail})
        add("engine_owned", rec["engine_owned"], "retrieved or recorded by the engine" if rec["engine_owned"]
            else "owner-supplied or pre-existing recording: never deleted automatically")
        path = Path(rec["file_path"] or "")
        inside = path.is_file() and self.settings.recordings_dir.resolve() in path.resolve().parents
        add("finalised", inside and rec["status"] not in ("failed",),
            "complete file inside the engine's recordings folder" if inside else "file missing or outside the engine folder")
        nseg = self.db.one("SELECT COUNT(*) n FROM transcript_segments WHERE recording_id=?", (rid,))["n"]
        add("transcript", nseg > 0, f"{nseg} transcript segments stored")
        clips = self.db.query("SELECT * FROM clips WHERE recording_id=?", (rid,))
        live = [c for c in clips if c["status"] in ("ready", "approved")]
        busy = [c for c in clips if c["status"] in ("pending_render", "rendering", "failed")]
        add("rendered", rec["status"] == "ready" and not busy and live,
            f"{len(live)} finished clip(s); {len(busy)} still rendering or failed" if clips else "no clips were produced")
        bad = []
        for c in live:
            if c["local_deleted_at"]:
                # the local MP4 was removed by the verified-upload cleanup, which proved its Drive copy first
                if not self.cloud.clip_verified(c["id"]):
                    bad.append(c["id"])
                continue
            try:
                info = media.probe(Path(c["video_path"]))
                if not (info["has_video"] and info["has_audio"] and info["duration"] > 1):
                    bad.append(c["id"])
            except Exception:  # noqa: BLE001
                bad.append(c["id"])
        add("integrity", live and not bad, "all clip files are valid" if not bad else f"clip(s) {bad} failed the media check")
        unverified = [c["id"] for c in live if not self.cloud.clip_verified(c["id"])]
        add("cloud_verified", live and not unverified,
            "every clip's video and metadata are stored and verified in the cloud" if not unverified else
            f"clip(s) {unverified} not yet verified in cloud storage" +
            ("" if self.cloud.provider.connected() else " (cloud storage not connected)"))
        jobs = self.db.one("SELECT COUNT(*) n FROM jobs WHERE recording_id=? AND status IN ('queued','running') "
                           "AND kind != 'cloud_upload'", (rid,))["n"]
        add("no_jobs", jobs == 0, "no pending work needs the original" if not jobs else f"{jobs} job(s) still need it")
        return g

    # ---- actions -----------------------------------------------------------------------------
    def check_recording(self, rid: int) -> dict:
        """Evaluate one recording and delete its raw file if (and only if) every gate passes."""
        rec = self.db.recording(rid)
        if not rec or rec["raw_deleted_at"] or not rec["file_path"]:
            return {"deleted": False, "reason": "nothing to do"}
        gates = self.gates(rid)
        failed = [x for x in gates if not x["ok"]]
        clips = self.db.query("SELECT status FROM clips WHERE recording_id=?", (rid,))
        if rec["status"] in ("ready", "failed") and rec["engine_owned"] and \
                not any(c["status"] in ("ready", "approved", "pending_render", "rendering") for c in clips):
            reason = "No valid clips were produced; the recording is held for review instead of being deleted."
            if rec["raw_hold_reason"] != reason:
                self.db.update("recordings", rid, raw_hold_reason=reason, lifecycle="held")
            return {"deleted": False, "reason": reason, "gates": gates}
        if failed or not self.settings.raw_delete_after_cloud:
            if not failed:
                return {"deleted": False, "reason": "automatic raw deletion is switched off", "gates": gates}
            return {"deleted": False, "reason": "; ".join(f"{x['gate']}: {x['detail']}" for x in failed), "gates": gates}
        path = Path(rec["file_path"])
        # Gate 8: record the decision durably first. A crash after this line leaves a recording marked
        # 'completed' whose file may still exist; the next check removes it (idempotent).
        self.db.update("recordings", rid, lifecycle="completed", stage="raw deletion approved by all gates")
        try:
            path.unlink(missing_ok=True)              # the folder and its provenance.json are kept
        except OSError as e:
            self.db.update("recordings", rid, lifecycle="awaiting_review", stage=f"raw deletion failed: {e}")
            log.warning("raw deletion of recording %s failed: %s", rid, e)
            return {"deleted": False, "reason": str(e), "gates": gates}
        self.db.update("recordings", rid, file_path=None, raw_deleted_at=time.time(),
                       stage="raw recording deleted after verified processing and cloud copy")
        log.info("recording %s: raw file deleted after all gates passed (%s)", rid, path.name)
        return {"deleted": True, "reason": "all gates passed", "gates": gates}

    def retention(self, now: float | None = None) -> dict:
        """Final-clip retention: rejected → after REJECTED_CLIP_HOURS; published → after PUBLISHED_CLIP_DAYS;
        unreviewed and approved-unpublished → kept; owner 'keep' → never expires. Local files and the
        engine's own cloud copies are removed; the database record, transcript and provenance stay."""
        now = now or time.time()
        done = {"rejected": 0, "published": 0}
        rej = self.db.query("SELECT * FROM clips WHERE status='rejected' AND retain=0 AND files_deleted_at IS NULL "
                            "AND reviewed_at IS NOT NULL AND reviewed_at < ?",
                            (now - self.settings.rejected_clip_hours * 3600,))
        pub = self.db.query("SELECT * FROM clips WHERE published_at IS NOT NULL AND retain=0 AND files_deleted_at IS NULL "
                            "AND published_at < ?", (now - self.settings.published_clip_days * 86400,))
        for kind, rows in (("rejected", rej), ("published", pub)):
            for c in rows:
                self._delete_clip_files(c)
                done[kind] += 1
        return done

    def _delete_clip_files(self, c) -> None:
        import shutil
        if c["video_path"]:
            d = Path(c["video_path"]).parent
            if self.settings.clips_dir.resolve() in d.resolve().parents:
                shutil.rmtree(d, ignore_errors=True)
        self.cloud.delete_clip_objects(c["id"])        # protected: a no-op for kept/published clips (see cloud)
        kept_master = c["status"] != "rejected" and self.cloud.clip_verified(c["id"])
        self.db.update("clips", c["id"], video_path=None, thumb_path=None, srt_path=None, ass_path=None,
                       files_deleted_at=time.time(),
                       **({"local_deleted_at": time.time(), "cleanup_state": "LOCAL_DELETED"} if kept_master else {}))

    def run(self) -> dict:
        out = {"raw_deleted": 0, "held": 0}
        for r in self.db.query("SELECT id FROM recordings WHERE engine_owned=1 AND raw_deleted_at IS NULL "
                               "AND file_path IS NOT NULL"):
            res = self.check_recording(r["id"])
            out["raw_deleted"] += int(res.get("deleted", False))
            out["held"] += int("held" in res.get("reason", ""))
        out.update(self.retention())
        return out
