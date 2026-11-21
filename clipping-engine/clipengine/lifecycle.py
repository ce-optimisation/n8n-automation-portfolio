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
        """Retry eligible rejected files locally. LocalCleanup owns published retention."""
        now = now or time.time()
        done = {"rejected": 0, "published": 0}
        rej = self.db.query("SELECT * FROM clips WHERE status='rejected' AND retain=0 AND files_deleted_at IS NULL "
                            "AND reviewed_at IS NOT NULL AND reviewed_at < ?",
                            (now - self.settings.rejected_clip_hours * 3600,))
        for clip in rej:
            result = self.discard_rejected(clip["id"])
            done["rejected"] += int(result["deleted"])
        return done

    def discard_rejected(self, clip_id: int) -> dict:
        """Reject & delete (Session 15/16). Deletes the LOCAL files of one rejected clip now: every render revision folder of
        that clip (video, title-free master, thumbnail, captions, temp files). Never touches: the source recording, other clips,
        Google Drive copies (no automatic cloud deletion), the database row, transcript or review decision.

        Refused (nothing deleted, reason returned and audited) when the clip: is not rejected; is marked keep; was ever published
        or has a publish attempt / publication package past 'prepared'; has a queued or running job; or is already deleted."""
        import shutil
        with self.db.tx() as conn:
            c = conn.execute("SELECT * FROM clips WHERE id=?", (clip_id,)).fetchone()
            if not c or c["status"] != "rejected":
                return {"deleted": False, "reason": "not a rejected clip"}
            if c["files_deleted_at"]:
                return {"deleted": False, "reason": "already deleted"}
            reason = self._reject_delete_blocker(c)
            freed = 0
            folders = 0
            if not reason:
                try:
                    for folder in self._own_clip_dirs(c):
                        sizes = {f: f.stat().st_size for f in folder.rglob("*") if f.is_file() and not f.is_symlink()}
                        try:
                            shutil.rmtree(folder)
                        finally:
                            freed += sum(size for f, size in sizes.items() if not f.exists())
                        folders += 1
                except (OSError, ValueError) as exc:
                    reason = str(exc)
            if reason:
                kind = "held"
                detail = f"Reject cleanup held: {reason}; {freed} bytes removed"
            else:
                conn.execute("UPDATE clips SET video_path=NULL, thumb_path=NULL, srt_path=NULL, ass_path=NULL, "
                             "files_deleted_at=? WHERE id=?", (time.time(), clip_id))
                kind = "deleted"
                detail = f"Removed {folders} folders, {freed} bytes; cloud copies untouched"
            conn.execute("INSERT INTO cleanup_events(clip_id,ts,kind,detail) VALUES(?,?,?,?)",
                         (clip_id,time.time(),kind,detail[:500]))
        return {"deleted": not reason, "reason": reason, "bytes": freed, "folders": folders}

    def _reject_delete_blocker(self, c) -> str:
        if c["retain"] or c["keep_local"] or c["cleanup_hold"]:
            return "marked keep"
        if c["published_at"]:
            return "clip was published"
        cid = c["id"]
        if self.db.one("SELECT COUNT(*) n FROM jobs WHERE clip_id=? AND status IN ('queued','running')", (cid,))["n"]:
            return "a job for this clip is queued or running"
        if self.db.one("SELECT COUNT(*) n FROM publish_attempts WHERE clip_id=?", (cid,))["n"]:
            return "clip has publish attempts"
        if self.db.one("SELECT COUNT(*) n FROM publication_packages WHERE clip_id=? AND "
                       "(upload_state != 'not_started' OR status NOT IN ('prepared','failed'))", (cid,))["n"]:
            return "clip has an upload in progress or done"
        return ""

    def _own_clip_dirs(self, c) -> list:
        """Only this clip's own revision folders, only inside the engine's clips folder, never a folder another clip points at."""
        root = self.settings.clips_dir.resolve()
        base = self.settings.clips_dir / f"rec{c['recording_id']:05d}"
        if base.resolve().parent != root:
            raise ValueError("recording folder is outside managed storage")
        protected = set()
        for row in self.db.query("SELECT video_path,thumb_path,srt_path,ass_path FROM clips WHERE id != ?", (c["id"],)):
            protected.update(Path(value).resolve() for value in row if value)
        protected.update(Path(row["file_path"]).resolve() for row in self.db.query(
            "SELECT file_path FROM recordings WHERE file_path IS NOT NULL") if row["file_path"])
        out = []
        prefix = f"clip{c['id']:05d}_r"
        for folder in sorted(base.glob(prefix + "*")):
            if not folder.name[len(prefix):].isdigit() or not folder.is_dir():
                continue
            resolved = folder.resolve()
            if resolved != folder.absolute() or any(resolved == p or resolved in p.parents for p in protected):
                raise ValueError("revision folder is linked or contains shared media")
            out.append(folder)
        return out

    def _audit(self, clip_id: int, kind: str, detail: str) -> None:
        try:
            self.db.insert("cleanup_events", clip_id=clip_id, ts=time.time(), kind=kind, detail=detail[:500])
        except Exception as e:  # noqa: BLE001  (audit must never break the review action)
            log.warning("cleanup audit for clip %s failed: %s", clip_id, e)

    def run(self) -> dict:
        out = {"raw_deleted": 0, "held": 0}
        for r in self.db.query("SELECT id FROM recordings WHERE engine_owned=1 AND raw_deleted_at IS NULL "
                               "AND file_path IS NOT NULL"):
            res = self.check_recording(r["id"])
            out["raw_deleted"] += int(res.get("deleted", False))
            out["held"] += int("held" in res.get("reason", ""))
        out.update(self.retention())
        return out
