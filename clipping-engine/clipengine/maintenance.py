"""Periodic housekeeping: retention policy and disposable-file cleanup.

Originals are deleted ONLY when DELETE_ORIGINALS_AFTER_DAYS > 0 (explicit
retention policy) and never while any clip from that recording is still
awaiting review. Approved clips keep their own rendered MP4 regardless.
"""
from __future__ import annotations

import logging
import shutil
import threading
import time
from pathlib import Path

from .config import Settings
from .db import Database

log = logging.getLogger("clipengine.maintenance")


def dir_size(p: Path) -> int:
    total = 0
    for f in Path(p).rglob("*"):
        try:
            if f.is_file():
                total += f.stat().st_size
        except OSError:
            pass
    return total


def storage_report(settings: Settings) -> dict:
    rec = dir_size(settings.recordings_dir)
    clips = dir_size(settings.clips_dir)
    models = dir_size(settings.models_dir)
    work = dir_size(settings.work_dir)
    total = rec + clips + models + work
    free = shutil.disk_usage(settings.data_dir).free
    return {"recordings_gb": rec / 1e9, "clips_gb": clips / 1e9, "models_gb": models / 1e9,
            "work_gb": work / 1e9, "total_gb": total / 1e9, "disk_free_gb": free / 1e9,
            "warning": total / 1e9 > settings.storage_warning_gb or free < 5e9}


def run_once(settings: Settings, db: Database, queue=None) -> dict:
    now = time.time()
    if queue is not None:
        report = storage_report(settings)
        if report["warning"]:
            from . import notify
            notify.create(settings, db, queue, kind="disk_low", dedupe_key=f"disk:{time.strftime('%Y-%m-%d')}",
                          content=notify.build_disk_low(settings, report))
    actions = {"stale_work_removed": 0, "rejected_renders_removed": 0, "originals_removed": 0}
    # 1. Disposable intermediates older than a day (never touched by a running job).
    running = db.one("SELECT COUNT(*) n FROM jobs WHERE status='running'")["n"]
    if not running:
        for f in settings.work_dir.glob("*"):
            try:
                if now - f.stat().st_mtime > 86400:
                    shutil.rmtree(f, ignore_errors=True) if f.is_dir() else f.unlink(missing_ok=True)
                    actions["stale_work_removed"] += 1
            except OSError:
                pass
    # 2. Rendered files of rejected clips (DB record and transcript are kept).
    days = settings.delete_rejected_renders_after_days
    if days > 0:
        for c in db.query("SELECT id, video_path FROM clips WHERE status='rejected' AND video_path IS NOT NULL"
                          " AND reviewed_at < ?", (now - days * 86400,)):
            shutil.rmtree(Path(c["video_path"]).parent, ignore_errors=True)
            db.update("clips", c["id"], video_path=None, thumb_path=None, srt_path=None, ass_path=None)
            actions["rejected_renders_removed"] += 1
    # 3. Originals: only under an explicit retention policy, and only when fully processed.
    odays = settings.delete_originals_after_days
    if odays > 0:
        for r in db.query("SELECT id, file_path FROM recordings WHERE status='ready' AND file_path IS NOT NULL"
                          " AND created_at < ?", (now - odays * 86400,)):
            pending = db.one("SELECT COUNT(*) n FROM clips WHERE recording_id=? AND status IN "
                             "('pending_render','rendering','ready')", (r["id"],))["n"]
            if pending:
                continue  # still under review; regeneration needs the original
            Path(r["file_path"]).unlink(missing_ok=True)
            db.update("recordings", r["id"], file_path=None, stage="original deleted by retention policy")
            actions["originals_removed"] += 1
    if any(actions.values()):
        log.info("maintenance: %s", actions)
    return actions


class Maintenance:
    def __init__(self, settings: Settings, db: Database, interval: float = 3600, queue=None):
        self.settings, self.db, self.interval, self.queue = settings, db, interval, queue
        self.extra: list = []        # further periodic tasks (lifecycle: raw deletion gates, clip retention)
        self._stop = threading.Event()
        self._t: threading.Thread | None = None

    def start(self):
        def loop():
            while not self._stop.wait(timeout=30 if self._t is None else self.interval):
                try:
                    run_once(self.settings, self.db, self.queue)
                    for task in self.extra:
                        task()
                except Exception:  # noqa: BLE001
                    log.exception("maintenance failed")
        self._t = threading.Thread(target=loop, name="clipengine-maintenance", daemon=True)
        self._t.start()

    def stop(self):
        self._stop.set()
