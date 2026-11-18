"""Bounded live-capture storage: admission control, segment rotation, a join that never needs a second full copy, and
safe handling of failed/orphaned capture folders.

Why this exists (observed 17 Nov 2026): one recorder wrote a single 11.4 GB ``seg_001.ts``; the only guard stopped at ~0.5 GB
free, then finalising tried to write a second full-size ``recording.mp4`` on the same drive and ffmpeg failed with "No space
left on device", leaving both files behind indefinitely. The rules now are:

  * a capture is only ADMITTED if the drive can hold every active capture's next segment plus join headroom, and the total
    live buffer stays under ``LIVE_BUFFER_GB``; otherwise it is refused (and retried on the next check);
  * a recorder is ROTATED to a new file at ``LIVE_SEGMENT_MAX_GB`` / ``LIVE_SEGMENT_MAX_MINUTES`` (existing restart overlap
    trimming joins them), so no single file can grow without bound;
  * the recording is JOINED by appending each segment (overlap-trimmed, timestamps made continuous) to one MPEG-TS file and
    deleting that segment only after the append succeeded and was recorded: peak extra space is ONE segment, not the whole
    recording, and there is no MP4 remux. A crash mid-join resumes from the manifest; running out of space rolls the append back
    and keeps every remaining segment;
  * failed or abandoned capture folders are LISTED with exact sizes and removed only by an explicit, verified discard.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from . import media

HEADROOM = 512_000_000            # extra bytes kept free for the join and container overhead
MIN_AGE_FOR_DISCARD = 600         # a file touched in the last 10 minutes is treated as possibly still being written
DISCARD_NAMES = ("seg_", "recording.", "segments.", "join.")   # file-name prefixes a capture folder may contain


class InsufficientSpace(Exception):
    pass


class FileStillInUse(Exception):
    """A segment stayed locked by another process beyond the bounded wait (reported, never ignored)."""


RELEASE_TIMEOUT = 20.0              # seconds to wait for Windows (or an AV scanner) to release a just-closed file


def unlink_when_released(path: Path, timeout: float = RELEASE_TIMEOUT, sleep=time.sleep) -> None:
    """Delete ``path``, retrying while Windows reports it in use (WinError 32: a writer that is just exiting,
    an antivirus or indexer scan). A lock that persists past ``timeout`` raises FileStillInUse; the file is kept."""
    end = time.monotonic() + timeout
    delay = 0.1
    while True:
        try:
            path.unlink(missing_ok=True)
            return
        except PermissionError as e:
            if time.monotonic() >= end:
                raise FileStillInUse(f"{path.name} is still in use by another process after {timeout:.0f}s "
                                     f"(kept; nothing lost): {e}") from e
            sleep(delay)
            delay = min(delay * 2, 1.0)


def _free(settings) -> int:
    return shutil.disk_usage(settings.data_dir).free


def live_root(settings) -> Path:
    return settings.work_dir / "live"


def dir_bytes(p: Path) -> int:
    n = 0
    for f in Path(p).rglob("*"):
        try:
            if f.is_file():
                n += f.stat().st_size
        except OSError:
            pass
    return n


def segment_cap(settings) -> int:
    return int(settings.live_segment_max_gb * 1e9)


# ---- admission and rotation -----------------------------------------------------------------------------

def admit(settings, active: int, free: int | None = None) -> tuple[bool, str]:
    """May ANOTHER capture start? Every active capture (and the new one) needs room for its next segment plus join headroom."""
    free = _free(settings) if free is None else free
    reserve = settings.disk_reserve_gb * 1e9
    need = (active + 1) * (segment_cap(settings) + HEADROOM)
    if free - reserve < need:
        return False, (f"not enough free disk for {active + 1} live capture(s): about {need / 1e9:.1f} GB needed (next segment + join "
                       f"headroom each) plus a {settings.disk_reserve_gb:g} GB reserve, {free / 1e9:.1f} GB free")
    used = dir_bytes(live_root(settings)) if live_root(settings).exists() else 0
    cap = settings.live_buffer_gb * 1e9
    if used + segment_cap(settings) > cap:
        return False, (f"the live capture buffer is full ({used / 1e9:.1f} of {settings.live_buffer_gb:g} GB): finish or clear "
                       "pending recordings first (Storage page)")
    return True, "capacity available"


def must_stop(settings, active: int, free: int | None = None) -> bool:
    """While recording: stop (and keep what was captured) while there is still room to join it."""
    free = _free(settings) if free is None else free
    return free < settings.disk_reserve_gb * 1e9 + max(1, active) * (segment_cap(settings) + HEADROOM)


def rotation_due(seg: Path, started: float, settings, now: float | None = None) -> str | None:
    try:
        size = seg.stat().st_size
    except OSError:
        return None
    if size >= segment_cap(settings):
        return f"segment reached {size / 1e9:.2f} GB"
    if (now or time.time()) - started >= settings.live_segment_max_minutes * 60 and size > 0:
        return f"segment ran {settings.live_segment_max_minutes:g} minutes"
    return None


def _start_time(path: Path) -> float:
    """First timestamp of the file (MPEG-TS does not start at 0). Overlap cut points are absolute; -t is a duration."""
    r = subprocess.run([media.require("ffprobe"), "-v", "error", "-show_entries", "format=start_time", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True, **media._popen_kwargs())
    try:
        return float(r.stdout.strip().splitlines()[0])
    except (ValueError, IndexError):
        return 0.0


# ---- join without a second full copy --------------------------------------------------------------------

def join_segments_ts(segs: list[Path], trims: dict[str, float], out: Path, settings=None) -> dict:
    """Append every segment (cut at its overlap outpoint, timestamps continuous) to ``out`` (MPEG-TS), deleting each segment only
    after its append succeeded. Resumable via ``out.with_suffix('.join.json')``. Raises InsufficientSpace (nothing lost) when the
    drive cannot take the next segment."""
    ffmpeg = media.require("ffmpeg")
    mf = out.with_suffix(".join.json")
    state = json.loads(mf.read_text(encoding="utf-8")) if mf.is_file() else {"done": [], "offset": 0.0, "size_before": 0}
    if not mf.is_file() and out.exists():
        unlink_when_released(out)          # a stale partial output without a manifest is not trustworthy
    for seg in segs:
        if seg.name in state["done"]:
            unlink_when_released(seg)      # appended earlier; the crash came before the delete
            continue
        if not seg.is_file():
            continue
        free = _free(settings) if settings is not None else shutil.disk_usage(out.parent).free
        need = seg.stat().st_size + HEADROOM
        if settings is not None:
            need += settings.disk_reserve_gb * 1e9 * 0.5
        if free < need:
            raise InsufficientSpace(f"{seg.name}: needs about {need / 1e9:.2f} GB free to append, {free / 1e9:.2f} GB available")
        before = out.stat().st_size if out.exists() else 0
        state.update(current=seg.name, size_before=before)
        mf.write_text(json.dumps(state), encoding="utf-8")
        if out.exists() and out.stat().st_size != before:
            with open(out, "r+b") as fh:
                fh.truncate(before)
        info = media.probe(seg)
        dur = float(info["duration"])
        cut = trims.get(seg.name)
        if cut is not None:
            cut = max(0.0, float(cut) - _start_time(seg))      # absolute outpoint -> duration from this segment's start
            dur = min(dur, cut)
        cmd = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-i", str(seg)]
        if cut is not None:
            cmd += ["-t", f"{cut:.6f}"]
        cmd += ["-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy", "-output_ts_offset", f"{state['offset']:.6f}",
                "-f", "mpegts", "pipe:1"]
        with open(out, "ab") as fh:
            r = subprocess.run(cmd, stdout=fh, stderr=subprocess.PIPE, **media._popen_kwargs())
        if r.returncode != 0:
            with open(out, "r+b") as fh:       # roll the failed append back; the segment is untouched
                fh.truncate(before)
            err = (r.stderr or b"").decode("utf-8", "replace").strip()[-300:]
            if "no space" in err.lower() or "enospc" in err.lower():
                raise InsufficientSpace(f"ran out of disk space appending {seg.name} (rolled back, nothing lost)")
            raise RuntimeError(f"could not append {seg.name}: {err}")
        state["offset"] += dur
        state["done"].append(seg.name)
        state.pop("current", None)
        mf.write_text(json.dumps(state), encoding="utf-8")
        unlink_when_released(seg)              # only now: the data is safely in the joined file and recorded
    return state


# ---- failed / orphaned capture folders ------------------------------------------------------------------

def live_folders(settings, db, active_ids: set[int] | None = None) -> list[dict]:
    """Every folder under work/live with exact size, owning item/state and what may be done with it."""
    active_ids = active_ids or set()
    root = live_root(settings)
    out = []
    if not root.exists():
        return out
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        item = db.one("SELECT * FROM source_items WHERE work_dir=?", (str(d),))
        files = [{"name": f.name, "bytes": f.stat().st_size, "age_s": time.time() - f.stat().st_mtime}
                 for f in d.iterdir() if f.is_file()]
        state = item["state"] if item else "no_item"
        live = bool(item and (item["id"] in active_ids or state in ("recording", "finalizing", "awaiting_space")))
        out.append({"dir": str(d), "item_id": item["id"] if item else None, "title": (item["title"] if item else None),
                    "state": state, "bytes": sum(f["bytes"] for f in files), "files": files,
                    "kind": "active" if live else "orphan" if state in ("failed", "no_item") else "leftover",
                    "error": item["error"] if item else None})
    return out


def _writer_probe(path: Path) -> str | None:
    """A reason the file may still be in use, or None. Opening for append fails on Windows while another process holds it."""
    try:
        if time.time() - path.stat().st_mtime < MIN_AGE_FOR_DISCARD:
            return f"{path.name} was modified in the last {MIN_AGE_FOR_DISCARD // 60} minutes"
        with open(path, "ab"):
            pass
    except OSError as e:
        return f"{path.name} cannot be opened exclusively ({type(e).__name__}); another process may be writing it"
    return None


def discard_check(settings, db, item_id: int, names: list[str] | None = None, active_ids: set[int] | None = None) -> dict:
    """Verify that the named files of a FAILED capture can be permanently removed. Returns {'ok', 'problems', 'files', 'bytes'}."""
    problems: list[str] = []
    item = db.one("SELECT * FROM source_items WHERE id=?", (item_id,))
    if not item:
        return {"ok": False, "problems": [f"no capture item #{item_id}"], "files": [], "bytes": 0}
    if item["state"] != "failed":
        problems.append(f"capture #{item_id} is '{item['state']}', not 'failed'")
    if item_id in (active_ids or set()):
        problems.append("a recorder process for this capture is still running")
    wd = Path(item["work_dir"] or "")
    try:
        rwd, root = wd.resolve(), live_root(settings).resolve()
    except OSError as e:
        return {"ok": False, "problems": [f"path cannot be resolved ({e})"], "files": [], "bytes": 0}
    if os.path.islink(wd) or root not in rwd.parents:
        problems.append("the folder is not inside the engine's live-capture area")
    if item["recording_id"]:
        problems.append(f"a recording (#{item['recording_id']}) was registered from this capture")
    other = db.one("SELECT id FROM source_items WHERE work_dir=? AND id<>?", (item["work_dir"], item_id))
    if other:
        problems.append(f"capture #{other['id']} shares this folder")
    busy = db.one("SELECT COUNT(*) n FROM jobs WHERE status IN ('queued','running') AND "
                  "(recording_id IS NOT NULL AND recording_id IN (SELECT recording_id FROM source_items WHERE id=?))", (item_id,))["n"]
    if busy:
        problems.append("a queued or running job refers to this capture")
    files = []
    if wd.is_dir() and not problems:
        wanted = set(names) if names else None
        for f in sorted(wd.iterdir()):
            if not f.is_file() or (wanted is not None and f.name not in wanted):
                continue
            if not f.name.startswith(DISCARD_NAMES):
                problems.append(f"{f.name} is not a capture file; it will not be touched")
                continue
            if os.path.islink(f):
                problems.append(f"{f.name} is a symbolic link")
                continue
            why = _writer_probe(f)
            if why:
                problems.append(why)
            files.append(f)
        if wanted:
            for missing in wanted - {f.name for f in files}:
                if not (wd / missing).exists():
                    problems.append(f"{missing} does not exist (already removed?)")
    return {"ok": not problems, "problems": problems, "files": [str(f) for f in files],
            "bytes": sum(Path(f).stat().st_size for f in files if Path(f).exists())}


def discard(settings, db, item_id: int, confirm: str, names: list[str] | None = None, active_ids: set[int] | None = None) -> dict:
    """Permanently delete the named files of a failed capture after verification. `confirm` must be exactly 'DISCARD <id>'.
    Removes only the verified files (and the folder if it is then empty). Measures the space actually recovered."""
    if confirm != f"DISCARD {item_id}":
        return {"deleted": False, "reason": f"confirmation must be exactly 'DISCARD {item_id}'"}
    chk = discard_check(settings, db, item_id, names, active_ids)
    if not chk["ok"]:
        return {"deleted": False, "reason": "; ".join(chk["problems"]), "problems": chk["problems"]}
    before = _free(settings)
    removed = []
    for f in chk["files"]:
        p = Path(f)
        try:
            size = p.stat().st_size
            p.unlink()
            removed.append({"name": p.name, "bytes": size})
        except OSError as e:
            return {"deleted": False, "reason": f"could not remove {p.name}: {e}", "removed": removed}
    wd = Path(db.one("SELECT work_dir FROM source_items WHERE id=?", (item_id,))["work_dir"])
    try:
        wd.rmdir()                 # only succeeds if empty
    except OSError:
        pass
    after = _free(settings)
    note = f"Discarded by the owner: {', '.join(r['name'] for r in removed)} ({sum(r['bytes'] for r in removed) / 1e9:.2f} GB)"
    cur = db.one("SELECT error FROM source_items WHERE id=?", (item_id,))["error"] or ""
    db.update("source_items", item_id, error=(cur + " | " + note)[:1000])
    return {"deleted": True, "removed": removed, "freed_bytes_reported": sum(r["bytes"] for r in removed),
            "free_before": before, "free_after": after, "recovered_bytes_measured": after - before}
