"""Session 12 regression: recorder process TREES must be stopped, and segment files released, before joining.

Root cause of the Windows CI failure (WinError 32 on seg_001.ts): the recorder was started through a wrapper
(the Chocolatey ``ffmpeg.exe`` shim on the CI runner; on the owner's laptop ``python -m yt_dlp``, which runs its
own ffmpeg for live HLS). Killing the wrapper left the real writer running, still holding and appending to the
segment, so the join could not delete it. These tests use a wrapper that starts a grandchild writer which never
stops on its own, so they reproduce the lock deterministically on Windows and the leaked writer on every OS.
"""
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from clipengine import livestore, proctree

# Grandchild: append to the file forever (self-limits to 60 s so a failed test never leaves it running long).
WRITER = ("import sys,time\n"
          "f=open(sys.argv[1],'ab',buffering=0)\n"
          "end=time.time()+60\n"
          "while time.time()<end:\n"
          "    f.write(b'x'*188); time.sleep(0.02)\n")


def wrapper_cmd(path: Path, *, wait: bool) -> list[str]:
    """A wrapper like a package-manager shim (wait=True) or a launcher that exits and leaves its child (wait=False)."""
    tail = ".wait()" if wait else ""
    code = (f"import subprocess,sys; p=subprocess.Popen([sys.executable,'-c',{WRITER!r},{str(path)!r}]){tail}; "
            "import time; time.sleep(0.01)")
    return [sys.executable, "-c", code]


def wait_until(cond, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def assert_writer_gone(path: Path) -> None:
    s1 = path.stat().st_size
    time.sleep(0.5)
    assert path.stat().st_size == s1, "a descendant process is still writing the segment"
    path.unlink()          # on Windows this is exactly the call that raised WinError 32


def test_killing_recorder_stops_grandchild_writer(tmp_path):
    seg = tmp_path / "seg_001.ts"
    p = proctree.RecorderProcess(wrapper_cmd(seg, wait=True), stdin=subprocess.DEVNULL)
    assert wait_until(lambda: seg.exists() and seg.stat().st_size > 0)
    p.kill()                                   # how the CI test simulates a recorder crash
    p.wait(timeout=15)
    assert_writer_gone(seg)


def test_terminating_recorder_stops_grandchild_writer(tmp_path):
    seg = tmp_path / "seg_001.ts"
    p = proctree.RecorderProcess(wrapper_cmd(seg, wait=True), stdin=subprocess.DEVNULL)
    assert wait_until(lambda: seg.exists() and seg.stat().st_size > 0)
    p.terminate()                              # rotation / low-disk stop / app shutdown path
    p.wait(timeout=15)
    assert_writer_gone(seg)


def test_wrapper_exiting_by_itself_does_not_leave_writer(tmp_path):
    seg = tmp_path / "seg_001.ts"
    p = proctree.RecorderProcess(wrapper_cmd(seg, wait=False), stdin=subprocess.DEVNULL, grace=0.5)
    assert wait_until(lambda: seg.exists() and seg.stat().st_size > 0)
    assert wait_until(lambda: p.poll() is not None)     # poll() reaps what the wrapper left behind
    assert_writer_gone(seg)


def test_monitor_recorder_crash_releases_segment(tmp_path, settings, monkeypatch):
    """The CI scenario at monitor level: crash the recorder, then the segment must be deletable by the join."""
    from clipengine import monitor
    from clipengine.db import Database
    from clipengine.jobs import JobQueue
    db = Database(settings.db_path)
    mon = monitor.SourceMonitor(settings, db, JobQueue(db))
    monkeypatch.setattr(mon, "_recorder_cmd", lambda src, it, seg, method: wrapper_cmd(seg, wait=True))
    wd = tmp_path / "live" / "x"
    sid = db.insert("sources", name="t", kind="hls", url="http://127.0.0.1/x.m3u8", enabled=1)
    iid = db.insert("source_items", source_id=sid, item_key="k", url="http://127.0.0.1/x.m3u8", kind="live",
                    state="recording", work_dir=str(wd))
    src = db.one("SELECT * FROM sources WHERE id=?", (sid,))
    mon._start_segment(src, db.one("SELECT * FROM source_items WHERE id=?", (iid,)), "hls")
    seg = wd / "seg_001.ts"
    assert wait_until(lambda: seg.exists() and seg.stat().st_size > 0)
    mon.procs[iid].kill()
    mon.procs[iid].wait()
    assert_writer_gone(seg)
    mon.stop()


def test_unlink_retries_transient_lock_then_succeeds(tmp_path, monkeypatch):
    f = tmp_path / "seg_001.ts"
    f.write_bytes(b"x")
    real = Path.unlink
    calls = {"n": 0}

    def flaky(self, missing_ok=False):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError(32, "The process cannot access the file because it is being used by another process")
        return real(self, missing_ok=missing_ok)
    monkeypatch.setattr(Path, "unlink", flaky)
    livestore.unlink_when_released(f, timeout=5, sleep=lambda s: None)
    assert calls["n"] == 3 and not f.exists()


def test_unlink_persistent_lock_is_reported_not_masked(tmp_path, monkeypatch):
    f = tmp_path / "seg_001.ts"
    f.write_bytes(b"x")

    def locked(self, missing_ok=False):
        raise PermissionError(32, "being used by another process")
    monkeypatch.setattr(Path, "unlink", locked)
    t = {"now": 0.0}
    monkeypatch.setattr(livestore.time, "monotonic", lambda: t["now"])

    def fake_sleep(s):
        t["now"] += s
    with pytest.raises(livestore.FileStillInUse, match="seg_001.ts"):
        livestore.unlink_when_released(f, timeout=3, sleep=fake_sleep)
    assert f.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows share-mode locking")
def test_unlink_waits_for_real_windows_handle_release(tmp_path):
    import threading
    f = tmp_path / "seg_001.ts"
    fh = open(f, "ab")
    threading.Timer(0.6, fh.close).start()
    with pytest.raises(PermissionError):
        f.unlink()                              # really locked (WinError 32)
    livestore.unlink_when_released(f, timeout=10)
    assert not f.exists()
