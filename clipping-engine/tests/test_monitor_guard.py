"""Session 15: the recorder safety guard must not depend on source polling (a poll hung for 10+ hours on 20 Nov 2026,
so the disk guard never ran and a capture grew to 5 GB on a nearly full disk)."""
import threading
import time

from clipengine import livestore, monitor
from clipengine.db import Database
from clipengine.jobs import JobQueue


class FakeProc:
    def __init__(self):
        self.terminated = False

    def poll(self):
        return 0 if self.terminated else None

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0


def _mon(settings):
    db = Database(settings.db_path)
    return db, monitor.SourceMonitor(settings, db, JobQueue(db), interval=0.05)


def _item(db):
    sid = db.insert("sources", name="Live", kind="channel", url="https://example.test/c", enabled=1, record_live=1)
    return db.insert("source_items", source_id=sid, item_key="k", url="https://example.test/l", kind="live",
                     state="recording", work_dir="x", segments=1, started_at=time.time())


def test_guard_stops_recorder_when_disk_is_low_without_any_polling(settings, monkeypatch):
    db, mon = _mon(settings)
    iid = _item(db)
    p = FakeProc()
    mon.procs[iid] = p
    finalised = []
    monkeypatch.setattr(mon, "_finalize", lambda i: finalised.append(i))
    monkeypatch.setattr(livestore, "must_stop", lambda s, n, free=None: True)
    mon.guard_once()                                   # no tick, no poll
    assert p.terminated and finalised == [iid] and iid not in mon.procs


def test_guard_leaves_a_healthy_recorder_alone(settings, monkeypatch):
    db, mon = _mon(settings)
    iid = _item(db)
    p = FakeProc()
    mon.procs[iid] = p
    monkeypatch.setattr(livestore, "must_stop", lambda s, n, free=None: False)
    mon.guard_once()
    assert not p.terminated and iid in mon.procs


def test_hung_poll_cannot_starve_the_guard(settings, monkeypatch):
    db, mon = _mon(settings)
    iid = _item(db)
    p = FakeProc()
    mon.procs[iid] = p
    release = threading.Event()
    monkeypatch.setattr(mon, "poll", lambda sid: release.wait(30))       # a poll that never returns
    monkeypatch.setattr(mon, "_finalize", lambda i: None)
    monkeypatch.setattr(livestore, "must_stop", lambda s, n, free=None: True)
    mon.POLL_TIMEOUT = 0.3
    t0 = time.time()
    mon.tick()                                                           # must come back despite the hung poll
    assert time.time() - t0 < 5
    assert p.terminated                                                  # and the supervise step ran after it
    release.set()


def test_guard_thread_runs_on_its_own(settings, monkeypatch):
    db, mon = _mon(settings)
    iid = _item(db)
    p = FakeProc()
    mon.procs[iid] = p
    monkeypatch.setattr(mon, "_finalize", lambda i: None)
    monkeypatch.setattr(livestore, "must_stop", lambda s, n, free=None: True)
    mon.GUARD_INTERVAL = 0.05
    mon._start_guard_thread()
    for _ in range(100):
        if p.terminated:
            break
        time.sleep(0.05)
    mon.stop()
    assert p.terminated


def test_circuit_breaker_stops_piling_up_hung_polls(settings, monkeypatch):
    db, mon = _mon(settings)
    release = threading.Event()
    calls = []
    monkeypatch.setattr(mon, "poll", lambda sid: (calls.append(sid), release.wait(30)))
    mon.POLL_TIMEOUT = 0.1
    mon.MAX_STUCK_POLLS = 2
    sid = db.insert("sources", name="S", kind="channel", url="https://example.test/c", enabled=1, record_live=0)
    for _ in range(5):
        mon._poll_with_timeout(sid)
    assert len(calls) == 2                      # the third and later polls were skipped, not started
    release.set()


def test_a_stalled_video_lookup_is_skipped_not_waited_for(settings):
    db, mon = _mon(settings)
    sid = db.insert("sources", name="S", kind="channel", url="https://example.test/c", enabled=1, record_live=1)
    src = db.one("SELECT * FROM sources WHERE id=?", (sid,))
    release = threading.Event()
    mon.PROBE_TIMEOUT = 0.1
    mon.prober = lambda s, url: (release.wait(30), {"live_status": "is_live"})[1]
    entries = [{"key": f"v{i}", "url": f"https://example.test/v{i}", "live": None, "title": "t"} for i in range(4)]
    t0 = time.time()
    out = mon._resolve_unknown(src, entries, first_poll=False)
    assert time.time() - t0 < 3 and out == []              # nothing stored; will be retried on the next check
    release.set()


def test_a_fast_lookup_still_resolves(settings):
    db, mon = _mon(settings)
    sid = db.insert("sources", name="S", kind="channel", url="https://example.test/c", enabled=1, record_live=1)
    src = db.one("SELECT * FROM sources WHERE id=?", (sid,))
    mon.prober = lambda s, url: {"live_status": "is_live", "title": "Live now"}
    out = mon._resolve_unknown(src, [{"key": "v1", "url": "https://example.test/v1", "live": None, "title": ""}], first_poll=False)
    assert out and out[0]["live"] is True and out[0]["title"] == "Live now"


class ExitedProc(FakeProc):
    returncode = 1

    def poll(self):
        return 1


def test_guard_restarts_an_exited_recorder_while_live_without_polling(settings, monkeypatch):
    """Session 21 (26 Nov 2026): a recorder died mid-broadcast and went unnoticed for most of a ~19 minute poll cycle."""
    db, mon = _mon(settings)
    iid = _item(db)
    mon.procs[iid] = ExitedProc()
    started = []
    monkeypatch.setattr(livestore, "must_stop", lambda s, n, free=None: False)
    monkeypatch.setattr(mon, "_stream_live", lambda src, it: True)
    monkeypatch.setattr(mon, "_start_segment", lambda src, it, method=None: started.append(it["id"]))
    monkeypatch.setattr(mon, "poll", lambda sid: (_ for _ in ()).throw(AssertionError("no polling")))
    mon.guard_once()
    assert started == [iid] and iid not in mon.procs
    assert db.one("SELECT restarts FROM source_items WHERE id=?", (iid,))["restarts"] == 1


def test_guard_finalises_an_exited_recorder_when_stream_ended(settings, monkeypatch):
    db, mon = _mon(settings)
    iid = _item(db)
    mon.procs[iid] = ExitedProc()
    finalised = []
    monkeypatch.setattr(livestore, "must_stop", lambda s, n, free=None: False)
    monkeypatch.setattr(mon, "_stream_live", lambda src, it: False)
    monkeypatch.setattr(mon, "_finalize", lambda i: finalised.append(i))
    mon.guard_once()
    assert finalised == [iid]


def test_hung_live_check_is_bounded_and_treated_as_unknown(settings, monkeypatch):
    db, mon = _mon(settings)
    iid = _item(db)
    mon.procs[iid] = ExitedProc()
    release = threading.Event()
    finalised = []
    monkeypatch.setattr(livestore, "must_stop", lambda s, n, free=None: False)
    monkeypatch.setattr(mon, "_stream_live", lambda src, it: release.wait(30))
    monkeypatch.setattr(mon, "_finalize", lambda i: finalised.append(i))
    mon.PROBE_TIMEOUT = 0.1
    t0 = time.time()
    mon.guard_once()
    assert time.time() - t0 < 3 and finalised == [] and iid in mon._unsure   # keeps re-checking, never finishes early
    release.set()


def test_hung_lookup_process_is_killed_and_leaves_no_thread(settings, monkeypatch):
    """Session 21: abandoned in-process lookups grew engine memory to 13.4 GB. Lookups now run in a killable child."""
    import subprocess
    import sys
    monkeypatch.setattr(monitor, "PROBE_PROCESS_TIMEOUT", 1.0)
    monkeypatch.setattr(monitor, "_probe_cmd", lambda url, ua: [sys.executable, "-c", "import time; time.sleep(60)"])
    before = threading.active_count()
    spawned = []
    real_popen = subprocess.Popen
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: spawned.append(real_popen(*a, **k)) or spawned[-1])
    t0 = time.time()
    try:
        monitor.probe_entry_isolated(settings, "https://example.test/v")
        raise AssertionError("expected a timeout")
    except TimeoutError:
        pass
    assert time.time() - t0 < 15
    assert spawned and spawned[0].poll() is not None          # child is dead, memory returned to the OS
    assert threading.active_count() <= before


def test_isolated_lookup_returns_child_result(settings, monkeypatch):
    import sys
    payload = '{"title": "Live now", "live_status": "is_live", "published": ""}'
    monkeypatch.setattr(monitor, "_probe_cmd", lambda url, ua: [sys.executable, "-c", f"print({payload!r})"])
    assert monitor.probe_entry_isolated(settings, "https://example.test/v")["live_status"] == "is_live"


def test_monitor_uses_the_isolated_prober_by_default(settings):
    _, mon = _mon(settings)
    assert mon.prober is monitor.probe_entry_isolated
