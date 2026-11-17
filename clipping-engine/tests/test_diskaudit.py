"""Session 10: the storage audit is read-only, classifies raw recordings by their blocking gate and the disk guard defers heavy jobs."""
import json
import os
import time

import pytest

from clipengine import diskaudit, maintenance, storage
from clipengine.db import Database
from clipengine.jobs import TransientError


def _rec(settings, db, name, size, owned, status="ready"):
    p = settings.recordings_dir / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * size)
    return db.insert("recordings", title=name, source_type="url", status=status, file_path=str(p), engine_owned=owned, duration=100.0)


def test_audit_is_read_only_and_reports_each_blocker(settings):
    db = Database(settings.db_path)
    _rec(settings, db, "owner.mp4", 5000, 0)
    _rec(settings, db, "engine_no_transcript.mp4", 7000, 1)
    (settings.work_dir / "old.wav").write_bytes(b"w" * 3000)
    os.utime(settings.work_dir / "old.wav", (time.time() - 3 * 86400,) * 2)
    (settings.work_dir / "dl.part").write_bytes(b"p" * 100)
    before = {f: f.stat().st_mtime for f in settings.data_dir.rglob("*") if f.is_file() and "sqlite3-" not in f.name}
    rep = diskaudit.audit(settings)
    after = {f: f.stat().st_mtime for f in settings.data_dir.rglob("*") if f.is_file() and "sqlite3-" not in f.name}
    assert before == after                                                     # nothing created, changed or removed
    outcomes = rep["class_B_raw_recordings"]["by_outcome"]
    assert any(k.startswith("blocked: engine_owned") for k in outcomes)         # owner-supplied: protected
    assert any(k.startswith("blocked: transcript") for k in outcomes)           # engine-owned but not processed
    assert rep["class_B_raw_recordings"]["eligible_gb"] == 0
    assert rep["class_A"]["scratch_files"] == 2 and rep["drive"]["free_gb"] > 0
    assert "STORAGE AUDIT" in diskaudit.format_report(rep)
    json.dumps(rep)


def test_audit_without_a_database_still_reports_disk(settings):
    rep = diskaudit.audit(settings)
    assert "note" in rep and rep["drive"]["total_gb"] > 0


def test_heavy_jobs_are_deferred_when_the_disk_is_full(settings, monkeypatch):
    storage.require_space(settings, 1000, "x")                                  # plenty of room: no error
    monkeypatch.setattr(storage, "free_bytes", lambda p: 100)
    with pytest.raises(TransientError) as e:
        storage.require_space(settings, 10_000_000, "rendering this clip")
    assert "Not enough free disk space" in str(e.value) and e.value.retry_after == 1800


def test_maintenance_removes_only_old_partial_downloads(settings):
    db = Database(settings.db_path)
    old, new = settings.recordings_dir / "a.part", settings.recordings_dir / "b.part"
    for f in (old, new):
        f.write_bytes(b"p")
    os.utime(old, (time.time() - 5 * 86400,) * 2)
    keep = settings.recordings_dir / "real.mp4"
    keep.write_bytes(b"v")
    os.utime(keep, (time.time() - 9 * 86400,) * 2)
    acts = maintenance.run_once(settings, db)
    assert not old.exists() and new.exists() and keep.exists() and acts["partial_downloads_removed"] == 1
