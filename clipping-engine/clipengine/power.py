"""Keep the PC awake while the engine has work that sleep would interrupt.

Windows suspends every program when the PC sleeps, so a sleeping PC cannot notice a live stream
or finish a transcription. While there is work in progress this module asks Windows not to
sleep, using the documented per-process request ``SetThreadExecutionState`` (the same mechanism
media players use). It changes NO system power setting, needs no administrator rights, lets the
display turn off normally, and the request disappears the moment the engine stops.

PREVENT_SLEEP (in .env or Settings):
  off         never ask
  active      (default) only while a recording or a processing job is running
  monitoring  also whenever at least one enabled source is being watched for new broadcasts

It cannot wake a PC that is already asleep, hibernating or switched off, and closing a laptop
lid still sleeps the machine unless the owner changes that Windows setting themselves.
"""
from __future__ import annotations

import logging
import os
import threading

log = logging.getLogger("clipengine.power")

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
MODES = ("off", "active", "monitoring")


def supported() -> bool:
    return os.name == "nt"


def wanted(settings, db) -> tuple[bool, str]:
    """Should sleep be held off right now, and why?"""
    mode = settings.prevent_sleep if settings.prevent_sleep in MODES else "active"
    if mode == "off":
        return False, "PREVENT_SLEEP=off"
    if db.one("SELECT 1 AS x FROM source_items WHERE state IN ('recording','finalizing') LIMIT 1"):
        return True, "a live recording is in progress"
    if db.one("SELECT 1 AS x FROM jobs WHERE status IN ('running','queued') LIMIT 1"):
        return True, "a processing job is running or queued"
    if mode == "monitoring" and settings.monitor_enabled and \
            db.one("SELECT 1 AS x FROM sources WHERE enabled = 1 LIMIT 1"):
        return True, "an enabled source is being monitored"
    return False, "nothing in progress"


class KeepAwake:
    """Background thread that holds (or releases) the stay-awake request. `setter` is replaceable
    in tests; on other operating systems the default setter does nothing."""

    def __init__(self, settings, db, interval: float = 20.0, setter=None):
        self.settings, self.db, self.interval = settings, db, interval
        self.setter = setter or self._set
        self.active = False
        self.reason = ""
        self._stop = threading.Event()
        self._t: threading.Thread | None = None

    @staticmethod
    def _set(on: bool) -> None:
        if os.name != "nt":
            return
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0))

    def tick(self) -> bool:
        try:
            on, why = wanted(self.settings, self.db)
        except Exception as e:  # noqa: BLE001 - never let this thread die
            on, why = False, f"check failed: {e}"
        if on != self.active:
            self.setter(on)        # must be called from this thread: the request belongs to it
            log.info("keep-awake %s (%s)", "on" if on else "off", why)
        self.active, self.reason = on, why
        return on

    def start(self) -> None:
        def loop():
            self.tick()
            while not self._stop.wait(self.interval):
                self.tick()
            if self.active:
                self.setter(False)
                self.active = False
        self._t = threading.Thread(target=loop, name="clipengine-keepawake", daemon=True)
        self._t.start()

    def stop(self) -> None:
        self._stop.set()

    def status(self) -> dict:
        return {"supported": supported(), "mode": self.settings.prevent_sleep, "holding": self.active,
                "reason": self.reason}
