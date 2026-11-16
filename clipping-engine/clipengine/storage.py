"""Local disk planning: preflight estimates, live-recording checks, database backups and data relocation.

Cloud storage does not remove the need for local scratch space: a recording must exist locally while it
is transcribed and rendered. This module tells the owner, before anything starts, whether it will fit.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import time
from pathlib import Path

from .config import Settings, update_env_file

log = logging.getLogger("clipengine.storage")

WAV_BYTES_PER_S = 32_000            # 16 kHz mono s16 audio extracted for transcription
CLIP_BYTES_PER_S = 1_400_000        # rendered 1080x1920 clip (CRF 21, measured ~1.0-1.5 MB/s on real clips)
DIRS = ("recordings", "clips", "work", "models", "inbox", "selftest", "secrets", "logs")


def free_bytes(path: Path) -> int:
    p = Path(path)
    while not p.exists() and p.parent != p:
        p = p.parent
    return shutil.disk_usage(p).free


def estimate(settings: Settings, duration_s: float, source_bytes: float | None = None, clips: int | None = None,
             clip_seconds: float = 60.0) -> dict:
    """Separate estimates for the stages of one recording (bytes)."""
    rec = source_bytes if source_bytes is not None else duration_s / 3600 * settings.record_gb_per_hour * 1e9
    wav = duration_s * WAV_BYTES_PER_S
    n = clips if clips is not None else settings.clips_per_recording
    render = n * clip_seconds * CLIP_BYTES_PER_S
    peak = rec + wav + render                     # the WAV is deleted after transcription, renders stay
    reserve = settings.disk_reserve_gb * 1e9
    free = free_bytes(settings.data_dir)
    return {"recording": rec, "transcription_wav": wav, "rendering": render, "peak": peak, "reserve": reserve,
            "free": free, "fits": free - reserve >= peak, "shortfall": max(0.0, peak + reserve - free)}


# Typical live HLS bitrates (video+audio) per height, GB per hour (Rumble's ladder measured 16 Nov 2026:
# 1080p ~4.1 Mbps, 720p ~2.1, 480p ~1.06, 360p ~0.68).
LIVE_GB_PER_HOUR = {1080: 1.85, 720: 0.97, 480: 0.48, 360: 0.31}


def live_plan(settings: Settings, hours: float | None = None) -> dict:
    """Choose the live-recording resolution so the WHOLE expected broadcast fits with transcription space,
    clip renders and the reserve: the highest height <= LIVE_MAX_HEIGHT that fits; never below
    LIVE_MIN_HEIGHT (then the plan says how many hours will fit before the low-disk stop). The owner's
    minimum is 1080p: quality is never traded for length; a full disk stops the recording instead."""
    hours = float(hours or getattr(settings, "live_expected_hours", 4.5))
    free = free_bytes(settings.data_dir)
    budget = free - settings.disk_reserve_gb * 1e9 - hours * 3600 * WAV_BYTES_PER_S \
        - settings.clips_per_recording * 60 * CLIP_BYTES_PER_S
    hi = int(getattr(settings, "live_max_height", 1080))
    lo = int(getattr(settings, "live_min_height", 1080))
    heights = [h for h in sorted(LIVE_GB_PER_HOUR, reverse=True) if lo <= h <= hi] or [lo]
    rate = {h: LIVE_GB_PER_HOUR.get(h, settings.record_gb_per_hour) * (settings.record_gb_per_hour / 1.5) for h in heights}
    for h in heights:
        if rate[h] * hours * 1e9 <= budget:
            return {"height": h, "hours": hours, "covers_hours": hours, "full": True, "free_gb": free / 1e9,
                    "format": f"bv*[height<={h}]+ba/b[height<={h}]/b",
                    "note": f"{h}p fits the whole expected {hours:g} h broadcast."}
    h = heights[-1]
    covers = max(0.0, budget / (rate[h] * 1e9))
    return {"height": h, "hours": hours, "covers_hours": covers, "full": False, "free_gb": free / 1e9,
            "format": f"bv*[height<={h}]+ba/b[height<={h}]/b",
            "note": f"Only about {covers:.1f} h of a {hours:g} h broadcast fits at {h}p; the recording stops safely "
                    "when the disk is nearly full (what was captured is kept and processed)."}


def live_preflight(settings: Settings, expected_hours: float = 1.0) -> tuple[bool, str]:
    """Before a live recording starts: refuse when not even `expected_hours` of capture would fit at the lowest
    allowed resolution, so a full disk is reported plainly instead of producing a broken file."""
    plan = live_plan(settings)
    if plan["covers_hours"] >= expected_hours:
        return True, plan["note"]
    return False, (f"Not enough free space to record even {expected_hours:g} h at {plan['height']}p "
                   f"({plan['free_gb']:.1f} GB free, {settings.disk_reserve_gb:g} GB reserve kept). "
                   "Free space or move the data folder (Storage page).")


def capacity_report(settings: Settings, db=None) -> dict:
    from .maintenance import dir_size
    used = {d: dir_size(settings.data_dir / d) for d in DIRS if (settings.data_dir / d).exists()}
    free = free_bytes(settings.data_dir)
    per_hour = settings.record_gb_per_hour * 1e9
    usable = max(0.0, free - settings.disk_reserve_gb * 1e9)
    show = estimate(settings, 4 * 3600)
    out = {"data_dir": str(settings.data_dir), "free_gb": free / 1e9, "used_gb": {k: v / 1e9 for k, v in used.items()},
           "total_used_gb": sum(used.values()) / 1e9, "reserve_gb": settings.disk_reserve_gb,
           "record_gb_per_hour": settings.record_gb_per_hour,
           "live_hours_possible": usable / (per_hour + 3600 * WAV_BYTES_PER_S),
           "four_hour_show": {"needed_gb": (show["peak"] + show["reserve"]) / 1e9, "fits": show["fits"]},
           "blocker": None if show["fits"] else
           (f"A 4-hour live show needs about {(show['peak'] + show['reserve']) / 1e9:.1f} GB here; "
            f"{free / 1e9:.1f} GB is free. Full-length live recording is NOT operational until space is "
            "freed or the data folder is moved to a larger drive.")}
    if db is not None:
        out["raw_awaiting_deletion"] = db.one("SELECT COUNT(*) n FROM recordings WHERE engine_owned=1 AND "
                                              "file_path IS NOT NULL AND raw_deleted_at IS NULL")["n"]
    return out


# ---- database backups --------------------------------------------------------------------------

def backup_database(db_path: Path, dest_dir: Path, keep: int = 5, reason: str = "manual") -> Path | None:
    """Consistent copy with SQLite's online backup API (safe while the engine runs). Keeps `keep` copies.
    Secrets are not in the database (they live in .env / the OS credential store)."""
    db_path = Path(db_path)
    if not db_path.is_file():
        return None
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"clipengine-{time.strftime('%Y%m%d-%H%M%S')}-{reason}.sqlite3"
    src = sqlite3.connect(db_path)
    try:
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    for old in sorted(dest_dir.glob("clipengine-*.sqlite3"))[:-keep]:
        old.unlink(missing_ok=True)
    return dest


# ---- relocation (owner-approved) ---------------------------------------------------------------

def plan_relocation(settings: Settings, target: str) -> dict:
    """Check a proposed new data folder. Nothing is copied here."""
    from .maintenance import dir_size
    t = Path(target).expanduser()
    problems = []
    if not t.is_absolute():
        problems.append("Give a full path, e.g. D:\\ClippingEngineData.")
    cur = settings.data_dir.resolve()
    try:
        tr = t.resolve()
    except OSError:
        tr = t
    if tr == cur or cur in tr.parents or tr in cur.parents:
        problems.append("The new folder must be separate from the current data folder.")
    if t.exists() and any(t.iterdir()):
        problems.append("The new folder must be empty (or not exist yet).")
    root = Path(t.anchor) if t.anchor else None
    if root is not None and not root.exists():
        problems.append(f"Drive {t.anchor} does not exist on this PC.")
    need = dir_size(settings.data_dir) * 1.05 + 2e9
    free = free_bytes(t) if root is not None and root.exists() else 0
    if free and free < need:
        problems.append(f"Not enough space there: {need / 1e9:.1f} GB needed, {free / 1e9:.1f} GB free.")
    same_drive = bool(root) and Path(cur.anchor).resolve() == root.resolve()
    return {"target": str(t), "ok": not problems, "problems": problems, "need_gb": need / 1e9,
            "free_gb": free / 1e9, "same_drive": same_drive,
            "note": ("Moving to another folder on the SAME drive frees no space." if same_drive else "")}


def relocate(settings: Settings, db, queue, target: str, confirm: str) -> dict:
    """Copy all engine-owned data to `target`, verify it, then point .env at it (takes effect after a
    restart). The ORIGINAL folder is left untouched; the owner deletes it once the engine has run from
    the new location. Requires the owner to type MOVE."""
    if confirm.strip().upper() != "MOVE":
        raise ValueError("Type MOVE to confirm the relocation.")
    plan = plan_relocation(settings, target)
    if not plan["ok"]:
        raise ValueError(" ".join(plan["problems"]))
    if db.one("SELECT COUNT(*) n FROM jobs WHERE status='running'")["n"] or \
            db.one("SELECT COUNT(*) n FROM source_items WHERE state='recording'")["n"]:
        raise ValueError("Wait until no job is running and no live recording is in progress.")
    queue.pause()
    t = Path(plan["target"])
    t.mkdir(parents=True, exist_ok=True)
    copied, bytes_ = 0, 0
    try:
        backup_database(settings.db_path, t, keep=1, reason="relocated")
        made = sorted(t.glob("clipengine-*-relocated.sqlite3"))[-1]
        made.replace(t / "clipengine.sqlite3")
        for d in DIRS:
            src = settings.data_dir / d
            if not src.exists():
                continue
            for f in src.rglob("*"):
                if f.is_file():
                    dst = t / d / f.relative_to(src)
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(f, dst)
                    if dst.stat().st_size != f.stat().st_size:
                        raise OSError(f"Size mismatch after copying {f}")
                    copied += 1
                    bytes_ += f.stat().st_size
        for extra in ("setup_state.json", "safety_terms.txt"):
            if (settings.data_dir / extra).is_file():
                shutil.copy2(settings.data_dir / extra, t / extra)
        chk = sqlite3.connect(t / "clipengine.sqlite3")
        try:
            n_rec = chk.execute("SELECT COUNT(*) FROM recordings").fetchone()[0]
        finally:
            chk.close()
        if n_rec != db.one("SELECT COUNT(*) n FROM recordings")["n"]:
            raise OSError("Database copy does not match the original.")
        update_env_file({"CLIPENGINE_DATA_DIR": str(t)}, settings.env_file)
        (t / "RELOCATED_FROM.txt").write_text(json.dumps({"from": str(settings.data_dir), "at": time.ctime(),
                                                          "files": copied, "bytes": bytes_}), encoding="utf-8")
    except Exception:
        queue.resume()
        raise
    log.info("data relocated to %s (%s files, %.1f GB); restart required", t, copied, bytes_ / 1e9)
    return {"target": str(t), "files": copied, "gb": bytes_ / 1e9,
            "next": "Restart the engine (close its window and start it again). It will then use the new folder. "
                    "Your original folder is untouched: delete it yourself once everything looks right."}
