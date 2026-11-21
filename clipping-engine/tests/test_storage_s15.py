"""Session 15: capacity-aware admission (pending join) and the free-space level."""
from clipengine import livestore
from clipengine.db import Database

GB = 10 ** 9


def test_new_capture_refused_while_an_earlier_one_still_needs_room_to_join(settings):
    settings.disk_reserve_gb = 1.0
    settings.live_segment_max_gb = 1.0
    ok, _ = livestore.admit(settings, 0, free=int(4 * GB))
    assert ok                                                           # plain case: 4 GB is enough
    ok, msg = livestore.admit(settings, 0, free=int(4 * GB), pending_join=int(4.4 * GB))
    assert not ok and "finish joining" in msg


def test_pending_join_bytes_is_the_largest_unjoined_segment(settings):
    db = Database(settings.db_path)
    wd = settings.work_dir / "live" / "x"
    wd.mkdir(parents=True)
    (wd / "seg_001.ts").write_bytes(b"a" * 1000)
    (wd / "seg_002.ts").write_bytes(b"b" * 5000)
    sid = db.insert("sources", name="S", kind="channel", url="https://e.test/c", enabled=1, record_live=1)
    db.insert("source_items", source_id=sid, item_key="k", url="u", kind="live", state="awaiting_space",
              work_dir=str(wd), segments=2)
    assert livestore.pending_join_bytes(db) == 5000
    db.execute("UPDATE source_items SET state='imported'")
    assert livestore.pending_join_bytes(db) == 0


def test_disk_level_ok_warning_critical(settings):
    settings.disk_reserve_gb = 1.0
    settings.live_segment_max_gb = 1.0
    settings.disk_warn_gb = 5.0
    assert livestore.disk_level(settings, free=int(20 * GB))[0] == "ok"
    assert livestore.disk_level(settings, free=int(4 * GB))[0] == "warning"
    lvl, text = livestore.disk_level(settings, free=int(1.4 * GB))
    assert lvl == "critical" and "stopped" in text
