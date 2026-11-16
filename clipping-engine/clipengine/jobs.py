"""Persistent, restart-safe SQLite job queue with a single background worker.

A single worker is used deliberately: transcription and rendering are CPU/GPU
bound, so running them concurrently on one machine slows everything down.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import traceback
from typing import Callable

from .db import Database

log = logging.getLogger("clipengine.jobs")


class TransientError(Exception):
    """An error worth retrying automatically (network blip, locked file...).

    `retry_after` (seconds), when given, is the minimum wait before the retry: used when a site
    refuses requests as too frequent (HTTP 429), where retrying quickly only makes matters worse."""

    def __init__(self, *args, retry_after: float | None = None):
        super().__init__(*args)
        self.retry_after = retry_after


class PermanentError(Exception):
    """An error that retrying will not fix; reported to the owner as-is."""


class JobContext:
    def __init__(self, queue: "JobQueue", job: sqlite3.Row):
        self.queue = queue
        self.job = job
        self.id = job["id"]
        self.payload = json.loads(job["payload_json"] or "{}")
        self._last_progress_write = 0.0

    def progress(self, fraction: float, message: str | None = None, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_progress_write < 1.0:
            return
        self._last_progress_write = now
        fields = {"progress": max(0.0, min(1.0, fraction))}
        if message is not None:
            fields["message"] = message
        self.queue.db.update("jobs", self.id, **fields)

    def log(self, message: str, level: str = "INFO") -> None:
        self.queue.log(self.id, message, level)

    def cancelled(self) -> bool:
        row = self.queue.db.one("SELECT status FROM jobs WHERE id = ?", (self.id,))
        return row is None or row["status"] == "cancelled"


Handler = Callable[[JobContext], None]

# Resource-intensive work: one at a time, held while a live recording is being captured (so the
# recording never competes with transcription for disk and CPU), and paused by the owner's Pause.
HEAVY_KINDS = ("download_url", "process_recording", "render_clip")
PRIORITY_LIVE = 100        # a finished live capture is processed before back-catalogue work
PRIORITY_DEFAULT = 50
PRIORITY_BACKFILL = 10


class JobQueue:
    def __init__(self, db: Database, max_attempts: int = 3):
        self.db = db
        self.max_attempts = max_attempts
        self.handlers: dict[str, Handler] = {}
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self.current_job_id: int | None = None
        self.failure_listeners: list[Callable] = []   # called with the failed job row (e.g. notifications)
        # Returns True while a live recording is in progress (set by the source monitor).
        self.recording_active: Callable[[], bool] = lambda: False
        self.priority_for = None          # set by the pipeline: recording_id -> priority or None
        self.defer_heavy_while_recording = True

    def register(self, kind: str, handler: Handler) -> None:
        self.handlers[kind] = handler

    # ---- producer side ---------------------------------------------------
    def enqueue(self, kind: str, *, recording_id: int | None = None, clip_id: int | None = None,
                payload: dict | None = None, dedupe_key: str | None = None,
                max_attempts: int | None = None, priority: int | None = None) -> int:
        """Queue a job. If an identical (dedupe_key) job is already queued or
        running, return that job's id instead of creating a duplicate."""
        if dedupe_key:
            existing = self.db.one("SELECT id FROM jobs WHERE dedupe_key = ? AND status IN ('queued','running')",
                                   (dedupe_key,))
            if existing:
                return existing["id"]
        try:
            jid = self.db.insert("jobs", kind=kind, recording_id=recording_id, clip_id=clip_id,
                                 payload_json=json.dumps(payload or {}), dedupe_key=dedupe_key,
                                 max_attempts=max_attempts or self.max_attempts, status="queued",
                                 message="Waiting in queue",
                                 priority=self._inherit_priority(recording_id) if priority is None else int(priority))
        except sqlite3.IntegrityError:
            existing = self.db.one("SELECT id FROM jobs WHERE dedupe_key = ? AND status IN ('queued','running')",
                                   (dedupe_key,))
            return existing["id"]
        self._wake.set()
        return jid

    def retry(self, job_id: int) -> bool:
        row = self.db.one("SELECT * FROM jobs WHERE id = ?", (job_id,))
        if not row or row["status"] not in ("failed", "cancelled"):
            return False
        if row["dedupe_key"] and self.db.one(
                "SELECT id FROM jobs WHERE dedupe_key = ? AND status IN ('queued','running')", (row["dedupe_key"],)):
            return False
        self.db.update("jobs", job_id, status="queued", attempts=0, error=None, progress=0,
                       message="Manually re-queued", run_after=0, finished_at=None)
        self.log(job_id, "Manually re-queued by owner")
        self._wake.set()
        return True

    def cancel(self, job_id: int) -> bool:
        n = self.db.execute("UPDATE jobs SET status='cancelled', message='Cancelled by owner', updated_at=?"
                            " WHERE id = ? AND status IN ('queued','running')", (time.time(), job_id))
        return bool(n)

    # ---- owner controls ---------------------------------------------------
    def _inherit_priority(self, recording_id: int | None) -> int:
        """Render jobs inherit the priority of their recording's processing job (so a live capture's
        clips are not stuck behind a back-catalogue import)."""
        if recording_id and self.priority_for is not None:
            try:
                p = self.priority_for(recording_id)
            except Exception:  # noqa: BLE001
                p = None
            if p is not None:
                return int(p)
        if recording_id:
            row = self.db.one("SELECT MAX(priority) p FROM jobs WHERE recording_id=? AND kind IN "
                              "('process_recording','download_url')", (recording_id,))
            if row and row["p"] is not None:
                return int(row["p"])
        return PRIORITY_DEFAULT

    def paused(self) -> bool:
        return self.db.get_kv("queue_paused") == "1"

    def pause(self) -> None:
        """Owner pause: no new job starts (a running job finishes its current step normally)."""
        self.db.set_kv("queue_paused", "1")

    def resume(self) -> None:
        self.db.set_kv("queue_paused", "0")
        self._wake.set()

    def set_priority(self, job_id: int, priority: int) -> bool:
        n = self.db.execute("UPDATE jobs SET priority=?, updated_at=? WHERE id=? AND status='queued'",
                            (max(0, min(1000, int(priority))), time.time(), job_id))
        self._wake.set()
        return bool(n)

    def move(self, job_id: int, direction: str) -> bool:
        """Move a queued job one place up or down the visible order (adjusts its priority)."""
        order = self.queued_order()
        ids = [r["id"] for r in order]
        if job_id not in ids:
            return False
        i = ids.index(job_id)
        k = i - 1 if direction == "up" else i + 1
        if not 0 <= k < len(order):
            return False
        other = order[k]["priority"]
        return self.set_priority(job_id, other + (1 if direction == "up" else -1))

    def queued_order(self) -> list[sqlite3.Row]:
        """Queued jobs in the order they will run."""
        return self.db.query("SELECT * FROM jobs WHERE status='queued' ORDER BY priority DESC, id")

    def blocked_reason(self) -> str | None:
        if self.paused():
            return "Paused by owner"
        if self.defer_heavy_while_recording and self._recording():
            return "Holding heavy work while a live recording is in progress"
        return None

    def _recording(self) -> bool:
        try:
            return bool(self.recording_active())
        except Exception:  # noqa: BLE001
            return False

    def log(self, job_id: int, message: str, level: str = "INFO") -> None:
        self.db.insert("job_logs", job_id=job_id, ts=time.time(), level=level, message=message[:4000])
        log.log(getattr(logging, level, logging.INFO), "job %s: %s", job_id, message)

    # ---- recovery --------------------------------------------------------
    def recover(self) -> int:
        """Re-queue jobs left 'running' by a crash or restart."""
        rows = self.db.query("SELECT id FROM jobs WHERE status = 'running'")
        for r in rows:
            self.db.update("jobs", r["id"], status="queued", message="Recovered after restart", run_after=0)
            self.log(r["id"], "Recovered after application restart; will resume", "WARNING")
        return len(rows)

    # ---- consumer side ---------------------------------------------------
    def claim(self) -> sqlite3.Row | None:
        now = time.time()
        if self.paused():
            return None
        heavy_held = self.defer_heavy_while_recording and self._recording()
        exclude = " AND kind NOT IN (%s)" % ",".join("'%s'" % k for k in HEAVY_KINDS) if heavy_held else ""
        with self.db.tx() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE status = 'queued' AND run_after <= ?" + exclude +
                               " ORDER BY priority DESC, id LIMIT 1", (now,)).fetchone()
            if not row:
                return None
            conn.execute("UPDATE jobs SET status='running', attempts=attempts+1, started_at=?, updated_at=?,"
                         " message='Started' WHERE id = ?", (now, now, row["id"]))
            return conn.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone()

    def run_one(self) -> bool:
        job = self.claim()
        if not job:
            return False
        self.current_job_id = job["id"]
        ctx = JobContext(self, job)
        handler = self.handlers.get(job["kind"])
        try:
            if handler is None:
                raise PermanentError(f"No handler registered for job kind '{job['kind']}'")
            ctx.log(f"Running {job['kind']} (attempt {job['attempts']}/{job['max_attempts']})")
            handler(ctx)
            if ctx.cancelled():
                ctx.log("Job was cancelled", "WARNING")
            else:
                self.db.update("jobs", job["id"], status="done", progress=1.0, finished_at=time.time(),
                               message="Completed", error=None)
                ctx.log("Completed")
        except Exception as exc:  # noqa: BLE001 - every failure must be recorded
            tb = traceback.format_exc()
            transient = isinstance(exc, TransientError)
            can_retry = transient and job["attempts"] < job["max_attempts"]
            if can_retry:
                delay = min(600, 15 * 2 ** (job["attempts"] - 1))
                if getattr(exc, "retry_after", None):
                    delay = max(delay, int(exc.retry_after))
                self.db.update("jobs", job["id"], status="queued", run_after=time.time() + delay,
                               error=str(exc), message=f"Transient error; retrying in {delay}s")
                ctx.log(f"Transient error, retry in {delay}s: {exc}", "WARNING")
            else:
                self.db.update("jobs", job["id"], status="failed", finished_at=time.time(),
                               error=f"{type(exc).__name__}: {exc}", message="Failed")
                ctx.log(f"Failed: {exc}\n{tb}", "ERROR")
                on_fail = self.handlers.get(job["kind"] + ":failed")
                if on_fail:
                    try:
                        on_fail(ctx)
                    except Exception:  # noqa: BLE001
                        log.exception("failure hook crashed")
                failed_row = self.db.one("SELECT * FROM jobs WHERE id = ?", (job["id"],))
                for listener in self.failure_listeners:
                    try:
                        listener(failed_row)
                    except Exception:  # noqa: BLE001
                        log.exception("failure listener crashed")
        finally:
            self.current_job_id = None
        return True

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                worked = self.run_one()
            except Exception:  # noqa: BLE001 - keep the worker alive
                log.exception("worker loop error")
                worked = False
            if not worked:
                self._wake.wait(timeout=2.0)
                self._wake.clear()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self.recover()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="clipengine-worker", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout)

    def drain(self, timeout: float = 600.0) -> None:
        """Run jobs synchronously until the queue is empty (used by tests/CLI)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self.run_one():
                pending = self.db.one("SELECT MIN(run_after) t FROM jobs WHERE status='queued'")
                if not pending or pending["t"] is None:
                    return
                time.sleep(min(1.0, max(0.0, pending["t"] - time.time())))
        raise TimeoutError("queue did not drain in time")
