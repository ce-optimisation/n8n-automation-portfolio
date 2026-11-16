"""What needs the owner's attention, in plain words, for the dashboard banner and /api/status.

The engine cannot report anything while the PC is off or asleep, so it records a heartbeat and,
the next time it runs, states exactly when it was NOT running — monitoring and recording did not
happen in that window, and the dashboard says so instead of implying otherwise.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time

BAD, WARN, INFO = "bad", "warn", "info"
HEARTBEAT_KEY, DOWNTIME_KEY, SEEN_KEY = "heartbeat.last", "downtime.log", "downtime.seen_until"



_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _plain(text) -> str:
    """Error text without terminal colour codes (stored by older builds) for display."""
    return _ANSI_RE.sub("", text or "")

def _fmt(t: float) -> str:
    return time.strftime("%a %d %b %H:%M", time.localtime(t))


def _span(seconds: float) -> str:
    m = int(seconds // 60)
    return f"{m // 60} h {m % 60} min" if m >= 60 else f"{m} min"


class Heartbeat:
    """Writes a timestamp every `interval` seconds. A long gap between two beats while the
    process stayed alive means the PC was asleep; a gap found at start-up means the engine (or
    the PC) was off."""

    def __init__(self, db, interval: float = 60.0, clock=time.time):
        self.db, self.interval, self.clock = db, interval, clock
        self._last: float | None = None
        self._stop = threading.Event()

    def _record(self, start: float, end: float, kind: str) -> None:
        log = json.loads(self.db.get_kv(DOWNTIME_KEY) or "[]")
        log.append({"from": start, "to": end, "seconds": round(end - start), "kind": kind})
        self.db.set_kv(DOWNTIME_KEY, json.dumps(log[-30:]))

    def beat(self) -> None:
        now = self.clock()
        if self._last is None:
            prev = self.db.get_kv(HEARTBEAT_KEY)
            if prev and now - float(prev) > max(180.0, 3 * self.interval):
                self._record(float(prev), now, "not_running")
        elif now - self._last > max(180.0, 3 * self.interval):
            self._record(self._last, now, "asleep")
        self._last = now
        self.db.set_kv(HEARTBEAT_KEY, str(now))

    def start(self) -> None:
        def loop():
            self.beat()
            while not self._stop.wait(self.interval):
                try:
                    self.beat()
                except Exception:  # noqa: BLE001
                    pass
        threading.Thread(target=loop, name="clipengine-heartbeat", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        try:
            self.beat()
        except Exception:  # noqa: BLE001
            pass


def downtime(db, days: float = 7.0, only_unseen: bool = True) -> list[dict]:
    seen = float(db.get_kv(SEEN_KEY) or 0) if only_unseen else 0.0
    since = time.time() - days * 86400
    return [d for d in json.loads(db.get_kv(DOWNTIME_KEY) or "[]") if d["to"] > max(since, seen) and d["seconds"] >= 300]


def dismiss_downtime(db) -> None:
    db.set_kv(SEEN_KEY, str(time.time()))


def items(core) -> list[dict]:
    """[{level, text, link, link_text}] — most serious first."""
    from . import maintenance, notify
    s, db = core.settings, core.db
    out: list[dict] = []

    def add(level, text, link="", link_text=""):
        out.append({"level": level, "text": text, "link": link, "link_text": link_text})

    worker = getattr(core.queue, "_thread", None)
    if worker is not None and not worker.is_alive():
        add(BAD, "The processing worker has stopped. Close the engine window and start it again.")
    if s.dashboard_url and not s.dashboard_password:
        add(BAD, "A phone address is configured but the dashboard has no password.", "/settings#phone", "Fix in Settings")
    sources = db.query("SELECT * FROM sources ORDER BY id")
    enabled = [x for x in sources if x["enabled"]]
    gaps = downtime(db)
    if gaps:
        total = sum(g["seconds"] for g in gaps)
        last = gaps[-1]
        why = "the PC was asleep" if last["kind"] == "asleep" else "the engine was not running (PC off, asleep, or the engine closed)"
        add(WARN if enabled else INFO,
            f"Not running for {_span(total)} in the last 7 days — most recently {_fmt(last['from'])} → {_fmt(last['to'])} "
            f"({why}). " + ("Live broadcasts in those periods were NOT recorded; feed items published then are still "
                            "picked up at the next check." if enabled else "Nothing was monitored or processed then."),
            "/settings#unattended", "Running unattended")
    if sources and not s.monitor_enabled:
        add(BAD, "Source monitoring is switched off (MONITOR_ENABLED=0), so no source is being checked.", "/sources", "Sources")
    for src in sources:
        if src["status"] == "blocked":
            add(WARN, f"Source “{src['name']}” is disabled — permission missing: {(src['status_detail'] or '')[:220]}",
                "/sources", "Sources")
        elif src["enabled"] and src["status"] == "error":
            add(BAD, f"Source “{src['name']}” could not be checked ({src['consecutive_errors']} failure(s) in a row): "
                     f"{_plain(src['last_error'])[:200]}", "/sources", "Sources")
        elif not src["enabled"]:
            add(INFO, f"Source “{src['name']}” is switched off.", "/sources", "Sources")
    n_items = db.one("SELECT COUNT(*) n FROM source_items WHERE state='failed' AND updated_at > ?",
                     (time.time() - 7 * 86400,))["n"]
    if n_items:
        add(BAD, f"{n_items} recording(s)/download(s) from monitored sources failed in the last 7 days.", "/sources", "See which")
    n_jobs = db.one("SELECT COUNT(*) n FROM jobs WHERE status='failed' AND updated_at > ?", (time.time() - 7 * 86400,))["n"]
    if n_jobs:
        add(BAD, f"{n_jobs} processing job(s) failed in the last 7 days. Each can be retried.", "/jobs", "Open Jobs")
    if s.notify_to and s.notify_enabled:
        held = db.one("SELECT COUNT(*) n FROM notifications WHERE status='held'")["n"]
        approved = notify.is_approved(s, db)
        mailer = core.notifier.mailer.status()
        if approved and not mailer["connected"]:
            add(BAD, "Email notifications are approved but the sending account is not signed in (expired or "
                     "disconnected), so nothing can be sent.", "/settings/notifications", "Reconnect")
        elif not approved and held:
            add(INFO, f"{held} email notification(s) are being held until you approve the recipient and wording.",
                "/settings/notifications", "Review")
        bad_mail = db.one("SELECT COUNT(*) n FROM notifications WHERE status='failed' AND updated_at > ?",
                          (time.time() - 7 * 86400,))["n"]
        if bad_mail:
            add(BAD, f"{bad_mail} email notification(s) failed to send in the last 7 days.", "/settings/notifications", "Details")
    for plat in ("tiktok", "instagram"):
        err = db.get_kv(f"oauth_error.{plat}")
        if err:
            add(WARN, f"{plat.capitalize()} sign-in problem: {err[:200]}", "/settings#connections", "Connections")
    try:
        st = maintenance.storage_report(s)
        if st["disk_free_gb"] < 5:
            add(BAD, f"Storage is nearly full: only {st['disk_free_gb']:.1f} GB free. Recording and processing will fail "
                     "when it runs out.", "/settings#storage", "Storage")
        elif st["warning"]:
            add(WARN, f"Clipping data uses {st['total_gb']:.1f} GB (warning level {s.storage_warning_gb:.0f} GB); "
                      f"{st['disk_free_gb']:.1f} GB free.", "/settings#storage", "Storage")
    except OSError:
        pass
    if os.name == "nt" and enabled and s.prevent_sleep != "monitoring":
        add(INFO, "This PC may go to sleep when idle; a live broadcast that starts while it sleeps is missed.",
            "/settings#unattended", "Keep awake while monitoring")
    order = {BAD: 0, WARN: 1, INFO: 2}
    return sorted(out, key=lambda x: order[x["level"]])


def summary(core) -> dict:
    its = items(core)
    return {"ok": not any(i["level"] == BAD for i in its), "items": its,
            "monitoring": {"enabled": core.settings.monitor_enabled,
                           "sources_enabled": core.db.one("SELECT COUNT(*) n FROM sources WHERE enabled=1")["n"]},
            "heartbeat": float(core.db.get_kv(HEARTBEAT_KEY) or 0)}
