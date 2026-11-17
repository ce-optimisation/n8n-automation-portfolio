"""Verified-upload local cleanup of FINISHED clips (separate from the raw-recording lifecycle).

    Render -> review -> verified Google Drive master -> required platform handoffs verified -> recovery window
    -> delete the local MP4 (never the Drive copy) -> restorable from Drive at any time.

Nothing is deleted because an upload was *requested* or returned an initial success. A local video is removed only when
EVERY gate below passes, re-checked immediately before deletion, with the decision and its evidence written first:

  managed        the file is the engine's own render, at clips/recNNNNN/clipNNNNN_rN/clip.mp4 inside the managed folder
  not_kept       the owner has not marked it "keep locally" and has not cancelled/held cleanup
  editorial      approved, rights recorded, any safety flag decided (publishing.base.editorial_gate)
  final          not being rendered/edited; the file has not changed in the last minute
  drive_master   Drive copy verified (identity, size and MD5 against THIS file; re-read from Drive now)
  metadata       attributions/transcript/metadata objects verified in Drive
  destinations   at least one explicit required destination, every one genuinely complete (see below), brand-isolated
  no_jobs        no queued/running job refers to the clip
  grace          the recovery window (default 24 h) has passed since the LAST required destination was confirmed
  consistent     no contradictory records

What counts as a completed destination (never invented; manual confirmations are labelled as such):
  YouTube        private upload whose API response shows the video id AND privacyStatus=private       (api)
  TikTok         only a confirmed publish counts. "Delivered to inbox" is NOT a saved draft            -> owner confirms
  Instagram      only a confirmed publish counts. A media container is not a draft                   -> owner confirms
  Facebook/X/Rumble  manual handoff: the owner confirms the draft/post exists (or records that they posted it)
The Drive master is never touched here. Automatic cleanup is OFF until the owner switches it on.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path

from .jobs import PermanentError, TransientError
from .publishing.base import editorial_gate
from .publishing.packages import PLATFORMS, account_allows

log = logging.getLogger("clipengine.cleanup")

STATES = ("LOCAL_PRESENT", "CLOUD_VERIFIED", "DESTINATIONS_PENDING", "READY_FOR_CLEANUP", "GRACE_PERIOD", "DELETING",
          "LOCAL_DELETED", "RESTORE_PENDING", "RESTORED", "CLEANUP_HELD", "CLEANUP_ERROR")
HANDOFF_STATES = {
    "api_accepted": "The platform API accepted the transfer",
    "remote_exists": "A remote object exists",
    "processing_done": "Remote processing completed",
    "private_verified": "Private state verified by the platform API",
    "manual_done": "Manual handoff prepared",
    "owner_confirmed": "Owner confirmed the draft/handoff exists (manual)",
    "owner_published": "Owner recorded that they posted it (manual)",
    "published": "Published (confirmed by the platform API)",
}
QUALIFYING = {"private_verified", "owner_confirmed", "owner_published", "published"}
SETTLE_SECONDS = 60
HELP = {
    "LOCAL_PRESENT": "On this computer; the Drive copy is not yet verified",
    "CLOUD_VERIFIED": "Drive copy verified; no required destination chosen yet",
    "DESTINATIONS_PENDING": "Waiting for a required destination to be confirmed",
    "GRACE_PERIOD": "All destinations done; waiting out the recovery window",
    "READY_FOR_CLEANUP": "Eligible: can be removed from this computer",
    "LOCAL_DELETED": "Removed locally; restorable from Google Drive",
    "RESTORE_PENDING": "Restoring from Google Drive",
    "RESTORED": "Restored; protected from cleanup for a while",
    "CLEANUP_HELD": "Held (owner hold, keep-locally, or a safeguard)",
    "CLEANUP_ERROR": "The last cleanup/restore attempt failed",
}


def md5_file(path: Path) -> str:
    h = hashlib.md5()  # noqa: S324 - Drive reports MD5; used for integrity, not security
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class LocalCleanup:
    def __init__(self, settings, db, queue, cloud, clock=time.time):
        self.settings, self.db, self.queue, self.cloud, self.clock = settings, db, queue, cloud, clock
        if queue is not None:
            queue.register("cleanup_restore", self._restore_job)

    # ---- switches --------------------------------------------------------------------------------
    def enabled(self) -> bool:
        return self.db.get_kv("local_cleanup_enabled", "0") == "1"

    def set_enabled(self, on: bool) -> None:
        self.db.set_kv("local_cleanup_enabled", "1" if on else "0")
        self._event(0, "manual", f"automatic local cleanup {'enabled' if on else 'disabled'} by the owner")

    def grace_seconds(self) -> float:
        try:
            hours = float(self.db.get_kv("local_cleanup_grace_hours") or self.settings.cleanup_grace_hours)
        except ValueError:
            hours = self.settings.cleanup_grace_hours
        return max(0.0, hours) * 3600

    def set_grace_hours(self, hours: float) -> None:
        self.db.set_kv("local_cleanup_grace_hours", str(max(0.0, float(hours))))

    def _event(self, clip_id: int, kind: str, detail: str, evidence: dict | None = None) -> None:
        self.db.insert("cleanup_events", clip_id=clip_id, ts=self.clock(), kind=kind, detail=detail[:500],
                       evidence_json=json.dumps(evidence) if evidence else None)

    # ---- managed path ----------------------------------------------------------------------------
    def expected_path(self, clip) -> Path:
        return (self.settings.clips_dir / f"rec{clip['recording_id']:05d}" /
                f"clip{clip['id']:05d}_r{clip['render_rev']}" / "clip.mp4")

    def managed_path(self, clip) -> tuple[Path | None, str]:
        """The engine's own render of this clip, or (None, why not). Owner-supplied files never qualify."""
        if not clip["video_path"]:
            return None, "no local video recorded"
        p = Path(clip["video_path"])
        if os.path.islink(p):
            return None, "the path is a symbolic link"
        try:
            rp, root = p.resolve(), self.settings.clips_dir.resolve()
        except OSError as e:
            return None, f"path cannot be resolved ({e})"
        if root not in rp.parents:
            return None, "the file is outside the engine's managed clips folder"
        if rp != self.expected_path(clip).resolve():
            return None, "the file is not this clip's own render (owner-supplied or foreign file)"
        return rp, ""

    # ---- destinations ----------------------------------------------------------------------------
    def destinations(self, clip) -> list[dict]:
        out = []
        attempts = {}
        for a in self.db.query("SELECT * FROM publish_attempts WHERE clip_id=? ORDER BY id", (clip["id"],)):
            attempts[a["platform"]] = a
        for pkg in self.db.query("SELECT * FROM publication_packages WHERE clip_id=? ORDER BY id", (clip["id"],)):
            if pkg["approval_state"] == "withdrawn":
                continue
            out.append(self._destination(clip, pkg, attempts))
        return out

    def _destination(self, clip, pkg, attempts) -> dict:
        spec = PLATFORMS.get(pkg["platform"], {})
        d = {"package_id": pkg["id"], "platform": pkg["platform"], "label": spec.get("label", pkg["platform"]),
             "account_id": pkg["account_id"], "required": bool(pkg["required"]), "ok": False, "state": "pending",
             "method": None, "confirmed_at": None, "reason": "", "inconsistent": False,
             "exclude_reason": pkg["exclude_reason"], "duration_ok": True}
        dur = (clip["end"] or 0) - (clip["start"] or 0)
        if spec and not (spec["min_s"] <= dur <= spec["max_s"]):
            d["duration_ok"] = False
        if not pkg["required"]:
            d.update(state="excluded", ok=True, reason=f"excluded by the owner: {pkg['exclude_reason'] or 'no reason'}")
            return d
        if not pkg["account_id"]:
            d["reason"] = "no account assigned to this destination"
            return d
        acc = self.db.one("SELECT * FROM social_accounts WHERE id=?", (pkg["account_id"],))
        if not acc:
            d.update(inconsistent=True, reason="the destination account no longer exists")
            return d
        if not account_allows(acc, clip["niche_key"]) or acc["platform"] != spec.get("account_platform", acc["platform"]):
            d.update(inconsistent=True, reason=f"@{acc['handle']} is not mapped to this clip's brand/niche (brands never share accounts)")
            return d
        hs, method, at = pkg["handoff_state"], pkg["handoff_method"], pkg["handoff_at"]
        platform = pkg["platform"]
        att = attempts.get({"tiktok": "tiktok", "instagram_reels": "instagram"}.get(platform, ""))
        if platform in ("tiktok", "instagram_reels") and att is not None and not (hs in QUALIFYING):
            if att["status"] == "published" and att["confirmed_at"] and (platform == "tiktok" or att["remote_id"]):
                hs, method, at = "published", "api", att["confirmed_at"]
            elif att["status"] == "delivered_inbox":
                d.update(state="api_accepted", reason="TikTok confirms only delivery to your inbox, not a saved draft: "
                                                     "confirm it in the TikTok app, then confirm here")
                return d
            else:
                busy = att["status"] in ("processing", "uploading", "queued", "retrying")
                why = ("an Instagram media container is not a draft; the platform is still working: confirm only once you "
                       "have saved it yourself" if platform == "instagram_reels" else "the platform is still processing") \
                    if busy else {"failed": "the upload failed"}.get(att["status"], f"state: {att['status']}")
                d.update(state="api_accepted" if busy else att["status"], reason=why)
                return d
        if platform == "youtube_shorts" and pkg["status"] == "uploaded_private" and hs not in QUALIFYING:
            d.update(state="remote_exists", reason="uploaded before privacy evidence was recorded, or privacy was not "
                                                   "'private': check it in YouTube Studio and confirm here")
            return d
        if hs in QUALIFYING:
            if platform == "youtube_shorts" and hs == "private_verified" and not pkg["remote_id"]:
                d.update(inconsistent=True, reason="marked private-verified but no YouTube video id is recorded")
                return d
            d.update(state=hs, ok=True, method=method, confirmed_at=at, reason=HANDOFF_STATES[hs])
            if not at:
                d.update(ok=False, inconsistent=True, reason="completed but no completion time is recorded")
            return d
        if pkg["status"] == "published" and pkg["published_at"]:
            d.update(state="owner_published", ok=True, method="manual", confirmed_at=pkg["published_at"],
                     reason=HANDOFF_STATES["owner_published"])
            return d
        d["reason"] = {"prepared": "not yet handed off", "handoff_ready": "handoff prepared; waiting for you to complete it "
                       "and confirm", "failed": "upload failed"}.get(pkg["status"], f"state: {pkg['status']}")
        d["state"] = "manual_done" if pkg["status"] == "handoff_ready" else "pending"
        return d

    # ---- Drive master ----------------------------------------------------------------------------
    def master_check(self, clip, *, deep: bool = False) -> tuple[bool, str, dict]:
        """Is the finished video safely in Drive? deep=True also re-hashes the local file and re-reads Drive."""
        rows = {r["kind"]: r for r in self.db.query(
            "SELECT * FROM cloud_objects WHERE owner_kind='clip' AND owner_id=?", (clip["id"],))}
        v = rows.get("video")
        if not v or v["state"] != "verified" or not v["remote_id"]:
            return False, "the Google Drive video copy is not verified", {}
        for kind in ("metadata", "transcript"):
            r = rows.get(kind)
            if (kind == "metadata" and (not r or r["state"] != "verified")) or (r and r["state"] != "verified"):
                return False, f"the Drive {kind} copy is not verified", {}
        ev = {"remote_id": v["remote_id"], "md5": v["local_md5"], "size": v["size"]}
        p = Path(clip["video_path"] or "")
        if not p.is_file():
            return False, "the local video file is missing", ev
        size = p.stat().st_size
        if size != v["size"]:
            return False, "the local file differs from the version verified in Drive (size)", ev
        if deep:
            md5 = md5_file(p)
            if md5 != v["local_md5"]:
                return False, "the local file differs from the version verified in Drive (checksum)", ev
            if not self.cloud.provider.connected():
                return False, "Google Drive is not connected, so the master cannot be re-verified now", ev
            try:
                meta = self.cloud.provider.metadata(v["remote_id"])
            except Exception as e:  # noqa: BLE001 - missing, trashed, or unreachable: all block deletion
                return False, f"the Drive master could not be re-read ({type(e).__name__}); it may have been removed", ev
            if meta.get("md5Checksum") != v["local_md5"] or int(meta.get("size") or -1) != v["size"]:
                return False, "the Drive master no longer matches (checksum/size)", ev
            ev["drive_reread"] = True
        return True, "Drive master verified", ev

    # ---- evaluation ------------------------------------------------------------------------------
    def evaluate(self, clip_id: int, *, deep: bool = False, manual: bool = False, now: float | None = None) -> dict:
        """The full gate list for one clip. `manual` waives only the recovery window and the global switch."""
        now = now or self.clock()
        clip = self.db.clip(clip_id)
        if not clip:
            return {"clip_id": clip_id, "state": "CLEANUP_ERROR", "eligible": False, "gates": [], "blockers": ["clip not found"]}
        gates = []

        def gate(key, ok, detail):
            gates.append({"gate": key, "ok": bool(ok), "detail": detail})
        if clip["local_deleted_at"]:
            state = "RESTORE_PENDING" if clip["cleanup_state"] == "RESTORE_PENDING" else "LOCAL_DELETED"
            return {"clip_id": clip_id, "state": state, "eligible": False, "gates": [], "blockers": [], "bytes": clip["local_bytes"] or 0,
                    "destinations": self.destinations(clip), "ready_at": None}
        path, why = self.managed_path(clip)
        gate("managed", path, "the engine's own render inside the managed clips folder" if path else why)
        hold = clip["keep_local"] or clip["cleanup_hold"] or clip["cleanup_auto"] == 0
        gate("not_kept", not hold, "no hold" if not hold else
             ("kept locally by the owner" if clip["keep_local"] else clip["cleanup_hold"] or "automatic cleanup switched off for this clip"))
        problems = editorial_gate(clip)
        gate("editorial", not problems, "approved, rights and flags settled" if not problems else " ".join(problems))
        busy = self.db.one("SELECT COUNT(*) n FROM jobs WHERE clip_id=? AND status IN ('queued','running') "
                           "AND kind NOT IN ('cleanup_restore')", (clip_id,))["n"]
        gate("no_jobs", busy == 0, "no job refers to this clip" if not busy else f"{busy} job(s) still use this clip")
        settled = bool(path and path.is_file() and now - path.stat().st_mtime >= SETTLE_SECONDS
                       and clip["status"] not in ("rendering", "pending_render"))
        gate("final", settled, "final render, unchanged for over a minute" if settled else "the render is not final or was just written")
        ok_m, detail_m, ev = self.master_check(clip, deep=deep) if path is not None else (False, "no managed local file", {})
        gate("drive_master", ok_m, detail_m)
        dests = self.destinations(clip)
        active = [d for d in dests if d["required"]]
        bad = [d for d in dests if d["inconsistent"]]
        rec_ok = self.db.recording(clip["recording_id"]) is not None
        gate("consistent", not bad and rec_ok, "records agree" if not bad and rec_ok else
             "; ".join([d["reason"] for d in bad] + ([] if rec_ok else ["the clip's recording record is missing"])))
        pending = [d for d in active if not d["ok"]]
        if not active:
            gate("destinations", False, "no required destination has been chosen for this clip (add one, or exclude platforms explicitly)")
        else:
            gate("destinations", not pending, "every required destination is complete" if not pending else
                 "; ".join(f"{d['label']}: {d['reason']}" for d in pending))
        confirmed = [d["confirmed_at"] for d in active if d["ok"] and d["confirmed_at"]]
        ready_at = (max(confirmed) + self.grace_seconds()) if active and not pending and confirmed else None
        in_grace = ready_at is not None and now < ready_at
        if clip["restore_grace_until"] and now < clip["restore_grace_until"]:
            ready_at = max(ready_at or 0, clip["restore_grace_until"])
            in_grace = True
        gate("grace", manual or ready_at is not None and not in_grace,
             "recovery window passed" if ready_at is not None and not in_grace else
             ("waiting for the recovery window to pass" if ready_at is not None else "no completed destination yet"))
        blockers = [f"{g['gate']}: {g['detail']}" for g in gates if not g["ok"]]
        eligible = not blockers
        state = self._state(clip, gates, in_grace, bool(active), bool(pending), ok_m, eligible, now)
        return {"clip_id": clip_id, "state": state, "eligible": eligible, "gates": gates, "blockers": blockers,
                "bytes": path.stat().st_size if path and path.is_file() else 0, "destinations": dests, "ready_at": ready_at,
                "evidence": ev, "path": str(path) if path else None, "enabled_for_clip": self._auto_on(clip) or manual}

    def _auto_on(self, clip) -> bool:
        if clip["cleanup_auto"] is not None:
            return bool(clip["cleanup_auto"])
        return self.enabled()

    @staticmethod
    def _state(clip, gates, in_grace, has_dest, pending, master_ok, eligible, now) -> str:
        if clip["cleanup_state"] == "CLEANUP_ERROR" and not eligible:
            return "CLEANUP_ERROR"
        g = {x["gate"]: x["ok"] for x in gates}
        if not g.get("not_kept", True):
            return "CLEANUP_HELD"
        if clip["restore_grace_until"] and clip["restore_grace_until"] > now and not clip["local_deleted_at"]:
            return "RESTORED"
        if not master_ok:
            return "LOCAL_PRESENT"
        if not has_dest:
            return "CLOUD_VERIFIED"
        if pending:
            return "DESTINATIONS_PENDING"
        if in_grace:
            return "GRACE_PERIOD"
        if eligible:
            return "READY_FOR_CLEANUP"
        return "CLEANUP_HELD"

    def refresh(self, clip_id: int) -> dict:
        """Recompute and persist the displayed state (and log a transition)."""
        ev = self.evaluate(clip_id)
        clip = self.db.clip(clip_id)
        if clip and clip["cleanup_state"] != ev["state"] and clip["cleanup_state"] not in ("DELETING",):
            self.db.update("clips", clip_id, cleanup_state=ev["state"])
            self._event(clip_id, "state", f"{clip['cleanup_state'] or 'LOCAL_PRESENT'} -> {ev['state']}")
        return ev

    # ---- the deletion ----------------------------------------------------------------------------
    def delete(self, clip_id: int, *, manual: bool = False) -> dict:
        """Remove the local MP4 if, and only if, every gate passes (checked twice). Idempotent."""
        clip = self.db.clip(clip_id)
        if not clip:
            return {"deleted": False, "reason": "clip not found"}
        if clip["local_deleted_at"]:
            return {"deleted": False, "reason": "already removed locally (restorable from Drive)", "state": "LOCAL_DELETED"}
        ev = self.evaluate(clip_id, deep=True, manual=manual)
        if not manual and not ev.get("enabled_for_clip"):
            return {"deleted": False, "reason": "automatic cleanup is switched off", "state": ev["state"], "blockers": ev["blockers"]}
        if ev["blockers"]:
            self._held(clip_id, ev)
            return {"deleted": False, "reason": "; ".join(ev["blockers"]), "state": ev["state"], "blockers": ev["blockers"]}
        # claim (atomic): only one worker may proceed
        claimed = self.db.execute("UPDATE clips SET cleanup_state='DELETING', updated_at=? WHERE id=? AND local_deleted_at IS NULL "
                                  "AND COALESCE(cleanup_state,'') <> 'DELETING'", (self.clock(), clip_id))
        if claimed != 1:
            return {"deleted": False, "reason": "another cleanup is already running for this clip"}
        evidence = {"path": ev["path"], "bytes": ev["bytes"], "drive": ev.get("evidence"),
                    "destinations": [{k: d[k] for k in ("platform", "state", "method", "confirmed_at", "account_id")}
                                     for d in ev["destinations"] if d["required"]], "manual": manual}
        self._event(clip_id, "deleting", "all gates passed; recording the decision before deleting", evidence)
        # re-check everything now that we hold the claim (a destination or job may have appeared)
        again = self.evaluate(clip_id, deep=False, manual=manual)
        clip = self.db.clip(clip_id)
        gates_now = [g for g in again["gates"] if g["gate"] != "grace" or not manual]
        path, why = self.managed_path(clip)
        if any(not g["ok"] for g in gates_now) or path is None or str(path) != ev["path"] or \
                not path.is_file() or path.stat().st_size != ev["bytes"] or os.path.islink(path):
            self.db.update("clips", clip_id, cleanup_state=None)
            self._held(clip_id, again if again["blockers"] else {"blockers": [why or "the file changed"], "state": "CLEANUP_HELD"})
            return {"deleted": False, "reason": "conditions changed before deletion; nothing removed",
                    "blockers": again["blockers"]}
        try:
            path.unlink()
        except OSError as e:
            self.db.update("clips", clip_id, cleanup_state="CLEANUP_ERROR")
            self._event(clip_id, "error", f"could not delete: {e}")
            return {"deleted": False, "reason": str(e), "state": "CLEANUP_ERROR"}
        self.db.update("clips", clip_id, local_deleted_at=self.clock(), cleanup_state="LOCAL_DELETED", local_bytes=ev["bytes"])
        self._event(clip_id, "deleted", f"local video removed ({ev['bytes'] / 1e6:.1f} MB); Drive master untouched", evidence)
        return {"deleted": True, "reason": "all gates passed", "state": "LOCAL_DELETED", "bytes": ev["bytes"]}

    def _held(self, clip_id: int, ev: dict) -> None:
        sig = " | ".join(ev.get("blockers", []))[:480]
        last = self.db.one("SELECT detail FROM cleanup_events WHERE clip_id=? AND kind='held' ORDER BY id DESC LIMIT 1", (clip_id,))
        if not last or last["detail"] != sig:
            self._event(clip_id, "held", sig)

    def recover(self) -> int:
        """After a crash: a clip left 'DELETING' is either still present (resume normally) or already gone (record it)."""
        n = 0
        for c in self.db.query("SELECT * FROM clips WHERE cleanup_state='DELETING'"):
            p = Path(c["video_path"] or "")
            if p.is_file():
                self.db.update("clips", c["id"], cleanup_state=None)
                self._event(c["id"], "state", "interrupted cleanup rolled back (file still present)")
            else:
                self.db.update("clips", c["id"], local_deleted_at=self.clock(), cleanup_state="LOCAL_DELETED")
                self._event(c["id"], "deleted", "interrupted cleanup completed (file already gone)")
            n += 1
        return n

    def run(self) -> dict:
        """Periodic pass: refresh states; delete eligible clips (largest first) where cleanup is allowed."""
        out = {"deleted": 0, "bytes": 0, "held": 0, "recovered": self.recover()}
        rows = self.db.query("SELECT id FROM clips WHERE local_deleted_at IS NULL AND video_path IS NOT NULL "
                             "AND status IN ('approved') ORDER BY id")
        evs = [self.refresh(r["id"]) for r in rows]
        for ev in sorted((e for e in evs if e["state"] == "READY_FOR_CLEANUP"), key=lambda e: -e["bytes"]):
            res = self.delete(ev["clip_id"])
            if res.get("deleted"):
                out["deleted"] += 1
                out["bytes"] += res["bytes"]
            else:
                out["held"] += 1
        return out

    # ---- owner controls --------------------------------------------------------------------------
    def keep_local(self, clip_id: int, on: bool) -> None:
        self.db.update("clips", clip_id, keep_local=int(on))
        self._event(clip_id, "manual", "kept locally by the owner" if on else "keep-locally removed")
        self.refresh(clip_id)

    def set_auto(self, clip_id: int, value: int | None) -> None:
        self.db.update("clips", clip_id, cleanup_auto=value)
        self._event(clip_id, "manual", {None: "cleanup follows the global switch", 0: "automatic cleanup off for this clip",
                                        1: "automatic cleanup allowed for this clip"}[value])

    def cancel(self, clip_id: int, reason: str = "cancelled by the owner") -> None:
        self.db.update("clips", clip_id, cleanup_hold=reason[:200], cleanup_state="CLEANUP_HELD")
        self._event(clip_id, "cancelled", reason)

    def release_hold(self, clip_id: int) -> None:
        self.db.update("clips", clip_id, cleanup_hold=None)
        self._event(clip_id, "manual", "hold released")
        self.refresh(clip_id)

    def confirm_destination(self, pkg_id: int, note: str = "") -> None:
        """The owner confirms a draft/handoff exists (TikTok/Instagram/Facebook/X/Rumble). Recorded as MANUAL evidence."""
        pkg = self.db.one("SELECT * FROM publication_packages WHERE id=?", (pkg_id,))
        if not pkg:
            raise ValueError("Destination not found")
        if pkg["approval_state"] != "approved":
            raise ValueError("Approve the destination package before confirming its handoff.")
        if not pkg["account_id"]:
            raise ValueError("Assign the account for this destination first.")
        now = self.clock()
        self.db.update("publication_packages", pkg_id, handoff_state="owner_confirmed", handoff_method="manual", handoff_at=now,
                       handoff_evidence=("Owner confirmed: " + (note or "the draft/handoff exists"))[:300])
        self._event(pkg["clip_id"], "manual", f"owner confirmed the {pkg['platform']} handoff", {"package": pkg_id})
        self.refresh(pkg["clip_id"])

    def exclude_destination(self, pkg_id: int, reason: str) -> None:
        if not (reason or "").strip():
            raise ValueError("Give a reason; excluding a destination is recorded.")
        pkg = self.db.one("SELECT * FROM publication_packages WHERE id=?", (pkg_id,))
        if not pkg:
            raise ValueError("Destination not found")
        self.db.update("publication_packages", pkg_id, required=0, excluded_at=self.clock(), exclude_reason=reason.strip()[:200])
        self._event(pkg["clip_id"], "manual", f"{pkg['platform']} excluded from required destinations: {reason.strip()[:120]}")
        self.refresh(pkg["clip_id"])

    def include_destination(self, pkg_id: int) -> None:
        pkg = self.db.one("SELECT * FROM publication_packages WHERE id=?", (pkg_id,))
        if not pkg:
            raise ValueError("Destination not found")
        self.db.update("publication_packages", pkg_id, required=1, excluded_at=None, exclude_reason=None)
        self._event(pkg["clip_id"], "manual", f"{pkg['platform']} is a required destination again")
        self.refresh(pkg["clip_id"])

    # ---- restore ---------------------------------------------------------------------------------
    def request_restore(self, clip_id: int) -> int | None:
        clip = self.db.clip(clip_id)
        if not clip or not clip["local_deleted_at"]:
            raise ValueError("This clip's local video has not been removed.")
        self.db.update("clips", clip_id, cleanup_state="RESTORE_PENDING")
        return self.queue.enqueue("cleanup_restore", clip_id=clip_id, recording_id=clip["recording_id"],
                                  dedupe_key=f"restore:{clip_id}", max_attempts=3, priority=60)

    def _restore_job(self, ctx) -> None:
        res = self.restore(ctx.job["clip_id"])
        if not res["restored"]:
            if res.get("transient"):
                raise TransientError(res["reason"], retry_after=300)
            raise PermanentError(res["reason"])

    def restore(self, clip_id: int) -> dict:
        """Download the verified master from Drive back to the managed location; validate it; never overwrite a
        different local file; protect the restored copy from immediate re-cleanup."""
        clip = self.db.clip(clip_id)
        if not clip:
            return {"restored": False, "reason": "clip not found"}
        row = self.db.one("SELECT * FROM cloud_objects WHERE owner_kind='clip' AND owner_id=? AND kind='video'", (clip_id,))
        fail = lambda why, transient=False: self._restore_failed(clip_id, why, transient)  # noqa: E731
        if not row or row["state"] not in ("verified",) or not row["remote_id"] or not row["local_md5"]:
            return fail("there is no verified Google Drive copy of this clip's video to restore")
        if not self.cloud.provider.connected():
            return fail("Google Drive is not connected (Storage page)", transient=True)
        target = Path(clip["video_path"]) if clip["video_path"] else self.expected_path(clip)
        root = self.settings.clips_dir.resolve()
        if root not in target.resolve().parents or os.path.islink(target):
            return fail("the restore location is outside the managed clips folder")
        if target.is_file():
            if md5_file(target) == row["local_md5"]:
                self._mark_restored(clip, target, "already present and identical")
                return {"restored": True, "reason": "the identical local file already exists", "path": str(target)}
            return fail("a different local file already exists at the restore location; it was not overwritten")
        target.parent.mkdir(parents=True, exist_ok=True)
        part = target.with_name(target.name + ".partial")
        part.unlink(missing_ok=True)
        try:
            meta = self.cloud.provider.metadata(row["remote_id"])
            if meta.get("md5Checksum") != row["local_md5"] or int(meta.get("size") or -1) != row["size"]:
                return fail("the Drive copy no longer matches what was verified (changed or corrupted)")
            self.cloud.provider.download(row["remote_id"], part)
        except Exception as e:  # noqa: BLE001
            part.unlink(missing_ok=True)
            transient = type(e).__name__ in ("TransportError", "ConnectError", "ReadTimeout", "TransientError", "ReadError")
            return fail(f"download from Drive failed ({type(e).__name__}: {e})", transient=transient)
        if part.stat().st_size != row["size"] or md5_file(part) != row["local_md5"]:
            part.unlink(missing_ok=True)
            return fail("the downloaded file failed the size/checksum check; nothing was restored")
        os.replace(part, target)
        self._mark_restored(clip, target, "downloaded from Drive and verified")
        return {"restored": True, "reason": "restored and verified", "path": str(target)}

    def _mark_restored(self, clip, target: Path, how: str) -> None:
        now = self.clock()
        self.db.update("clips", clip["id"], video_path=str(target), local_deleted_at=None, files_deleted_at=None,
                       restored_at=now, restore_grace_until=now + max(self.grace_seconds(), 24 * 3600),
                       cleanup_state="RESTORED", local_bytes=target.stat().st_size)
        self._event(clip["id"], "restored", f"{how}; protected from cleanup for at least 24 h",
                    {"path": str(target), "bytes": target.stat().st_size})

    def _restore_failed(self, clip_id: int, why: str, transient: bool) -> dict:
        self.db.update("clips", clip_id, cleanup_state="LOCAL_DELETED")
        self._event(clip_id, "restore_failed", why)
        return {"restored": False, "reason": why, "transient": transient}

    # ---- reporting -------------------------------------------------------------------------------
    def overview(self) -> dict:
        from . import storage
        out = {"present": [], "totals": {"local_bytes": 0, "present": 0, "removed": 0, "removed_bytes": 0},
               "by_state": {}, "recoverable_now": 0, "in_grace": 0, "blocked_by_destinations": 0, "held": 0,
               "enabled": self.enabled(), "grace_hours": self.grace_seconds() / 3600,
               "free_bytes": storage.free_bytes(self.settings.data_dir)}
        for c in self.db.query("SELECT id FROM clips WHERE video_path IS NOT NULL OR local_deleted_at IS NOT NULL"):
            clip = self.db.clip(c["id"])
            if clip["local_deleted_at"]:
                out["totals"]["removed"] += 1
                out["totals"]["removed_bytes"] += clip["local_bytes"] or 0
                continue
            ev = self.evaluate(c["id"])
            st, b = ev["state"], ev["bytes"]
            if not b:
                continue
            out["totals"]["local_bytes"] += b
            out["totals"]["present"] += 1
            out["by_state"][st] = out["by_state"].get(st, 0) + 1
            if st == "READY_FOR_CLEANUP":
                out["recoverable_now"] += b
            elif st == "GRACE_PERIOD":
                out["in_grace"] += b
            elif st in ("DESTINATIONS_PENDING", "CLOUD_VERIFIED", "LOCAL_PRESENT"):
                out["blocked_by_destinations"] += b
            elif st == "CLEANUP_HELD":
                out["held"] += b
            out["present"].append({"clip_id": c["id"], "state": st, "bytes": b, "blockers": ev["blockers"][:3]})
        out["present"].sort(key=lambda x: -x["bytes"])
        return out
