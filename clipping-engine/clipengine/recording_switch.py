"""Master recording switch (Session 13).

One owner-controlled on/off for ALL platforms and creators. It is stored in the database (survives restarts) and defaults
to ON, which is the behaviour before the switch existed.

OFF means the engine starts NO new recording: no new live capture, no new automatic download of a monitored upload, no
backfill, and the discovery queue stops routing downloads. Monitoring and discovery keep running (so nothing is missed:
new uploads are picked up when recording is turned back on). Work that is already recorded keeps processing (transcribe,
select, render, upload to Drive), and a live capture that is already in progress is allowed to finish rather than being
cut mid-stream. Manual imports and downloads the owner asks for by hand are not affected.
"""
from __future__ import annotations

import time

KEY = "recording.enabled"
KEY_AT = "recording.changed_at"


def is_enabled(db) -> bool:
    try:
        return (db.get_kv(KEY, "1") or "1") != "0"
    except Exception:       # noqa: BLE001 - if the state cannot be read, behave as before the switch existed
        return True


def set_enabled(db, on: bool) -> None:
    db.set_kv(KEY, "1" if on else "0")
    db.set_kv(KEY_AT, str(time.time()))


def changed_at(db) -> float | None:
    try:
        return float(db.get_kv(KEY_AT, "") or 0) or None
    except ValueError:
        return None
