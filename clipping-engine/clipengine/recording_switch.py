"""Master switches (Session 13): RECORDING and AUTO-UPLOAD.

Both are stored in the database (survive restarts) and are controlled from buttons in the dashboard header.

RECORDING (default ON): OFF means the engine starts NO new recording: no new live capture, no new automatic download of a
monitored upload, no backfill, and the discovery queue stops routing downloads. Monitoring and discovery keep running (so nothing
is missed: new uploads are picked up when recording is turned back on). Work that is already recorded keeps processing, and a live
capture already in progress is allowed to finish rather than being cut mid-stream. Manual imports are not affected.

AUTO-UPLOAD (default ON, but inert until a destination's own publishing switch is enabled): OFF means approved clips are NOT sent
to any platform; they simply wait, and uploads resume when it is turned back on.
"""
from __future__ import annotations

import time

KEY = "recording.enabled"
KEY_AT = "recording.changed_at"
DELIVERY_KEY = "delivery.enabled"
DELIVERY_AT = "delivery.changed_at"


def _get(db, key: str) -> bool:
    try:
        return (db.get_kv(key, "1") or "1") != "0"
    except Exception:       # noqa: BLE001 - if the state cannot be read, behave as before the switch existed
        return True


def _set(db, key: str, at_key: str, on: bool) -> None:
    db.set_kv(key, "1" if on else "0")
    db.set_kv(at_key, str(time.time()))


def is_enabled(db) -> bool:
    return _get(db, KEY)


def set_enabled(db, on: bool) -> None:
    _set(db, KEY, KEY_AT, on)


def delivery_enabled(db) -> bool:
    return _get(db, DELIVERY_KEY)


def set_delivery_enabled(db, on: bool) -> None:
    _set(db, DELIVERY_KEY, DELIVERY_AT, on)


def changed_at(db) -> float | None:
    try:
        return float(db.get_kv(KEY_AT, "") or 0) or None
    except ValueError:
        return None
