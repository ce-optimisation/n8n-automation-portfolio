"""Authorised source monitoring and live recording.

These use a LOCAL test server and a LOCAL FFmpeg-generated HLS live stream (no third-party
sources). Because the app refuses private addresses, they enable the TEST-ONLY
`allow_private_urls` switch and add 127.0.0.1 to the test's ALLOWED_URL_DOMAINS. The yt-dlp
channel lister is replaced by a labelled MOCK. No real platform is contacted.
"""
import functools
import http.server
import json
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

from clipengine import monitor
from clipengine.db import Database
from clipengine.inbox import InboxWatcher, read_sidecar
from clipengine.jobs import JobQueue
from clipengine.pipeline import Pipeline
from conftest import needs_ffmpeg

pytestmark = needs_ffmpeg


class DirServer:
    def __init__(self, root: Path):
        self.root = root
        handler = functools.partial(_Quiet, directory=str(root))
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


@pytest.fixture
def env(stub_settings, tmp_path):
    s = stub_settings
    s.allow_private_urls = True                 # TEST-ONLY: local server
    s.allowed_url_domains = ["127.0.0.1"]       # owner-declared authorisation, test scope
    s.min_recording_seconds = 5
    db = Database(s.db_path)
    q = JobQueue(db)
    p = Pipeline(s, db, q)
    mon = monitor.SourceMonitor(s, db, q, interval=0.2)
    root = tmp_path / "www"
    root.mkdir()
    srv = DirServer(root)
    yield s, db, q, p, mon, root, srv
    mon.stop()
    srv.close()


def test_restricted_platform_source_stays_disabled_until_permission(settings, monkeypatch):
    from clipengine import ingest
    monkeypatch.setattr(ingest, "resolve_host", lambda h: ["93.184.216.34"])
    db = Database(settings.db_path)
    sid, ok, msg = monitor.add_source(settings, db, name="America First (Rumble)", kind="channel",
                                      url="https://rumble.com/c/example", authorisation="")
    assert not ok and "written permission" in msg
    src = db.one("SELECT * FROM sources WHERE id=?", (sid,))
    assert src["enabled"] == 0 and src["status"] == "blocked"
    assert monitor.set_enabled(settings, db, sid, True)[0] is False
    sid2, ok2, msg2 = monitor.add_source(settings, db, name="Unknown site", kind="feed",
                                         url="https://example.org/feed.xml")
    assert not ok2 and "ALLOWED_URL_DOMAINS" in msg2
    db.execute("INSERT INTO source_permissions(domain, granted_by, granted_on, reference, created_at, updated_at) "
               "VALUES ('rumble.com','Rumble (test record)','2026-11-01','REF',0,0)")
    assert monitor.set_enabled(settings, db, sid, True)[0] is True


def feed_xml(items):
    body = "".join(f"<item><guid>{k}</guid><title>{t}</title><enclosure url='{u}' type='video/mp4'/>"
                   f"<pubDate>Mon, 0{i + 1} Jan 2026 10:00:00 GMT</pubDate></item>" for i, (k, t, u) in enumerate(items))
    return f"<?xml version='1.0'?><rss version='2.0'><channel><title>T</title>{body}</channel></rss>"


def test_feed_baseline_then_new_item_imported_once(env, sample_video):
    s, db, q, p, mon, root, srv = env
    shutil.copy(sample_video, root / "ep1.mp4")
    (root / "feed.xml").write_text(feed_xml([("ep1", "Episode 1", f"{srv.url}/ep1.mp4")]))
    sid, ok, _ = monitor.add_source(s, db, name="Council feed", kind="feed", url=f"{srv.url}/feed.xml",
                                    authorisation="Council publishes recordings for reuse (test)")
    assert ok
    assert mon.poll(sid)["new"] == 0                       # first poll records the baseline only
    assert db.one("SELECT state FROM source_items")["state"] == "baseline"
    # A new episode appears (a re-encoded copy so it is not a byte-identical duplicate)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(sample_video), "-t", "40", "-c:v", "libx264",
                    "-preset", "ultrafast", "-c:a", "aac", str(root / "ep2.mp4")], check=True)
    (root / "feed.xml").write_text(feed_xml([("ep2", "Episode 2", f"{srv.url}/ep2.mp4"),
                                             ("ep1", "Episode 1", f"{srv.url}/ep1.mp4")]))
    db.execute("UPDATE sources SET next_check = 0")
    assert mon.poll(sid)["new"] == 1
    q.drain(600)
    item = db.one("SELECT * FROM source_items WHERE item_key='ep2'")
    assert item["state"] == "imported" and item["recording_id"]
    rec = db.recording(item["recording_id"])
    assert rec["source_url"].endswith("/ep2.mp4") and "Monitored source" in rec["rights_attestation"]
    assert db.one("SELECT COUNT(*) n FROM clips WHERE recording_id=?", (rec["id"],))["n"] >= 1
    db.execute("UPDATE sources SET next_check = 0")
    assert mon.poll(sid)["new"] == 0                       # no duplicates on later polls
    assert db.one("SELECT COUNT(*) n FROM recordings")["n"] == 1


def start_live_hls(sample_video: Path, out_dir: Path, seconds: int) -> subprocess.Popen:
    """Real-time HLS 'live stream' generated locally by FFmpeg (event playlist, ENDLIST at the end)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    return subprocess.Popen(["ffmpeg", "-v", "error", "-re", "-i", str(sample_video), "-t", str(seconds),
                             "-c:v", "libx264", "-preset", "ultrafast", "-g", "30", "-c:a", "aac",
                             "-f", "hls", "-hls_time", "2", "-hls_list_size", "0", "-hls_playlist_type", "event",
                             str(out_dir / "live.m3u8")])


def wait_for(cond, timeout=90, step=0.5):
    t = time.time()
    while time.time() - t < timeout:
        if cond():
            return True
        time.sleep(step)
    return False


def test_live_hls_recorded_with_interruption_recovery_and_imported(env, sample_video):
    s, db, q, p, mon, root, srv = env
    gen = start_live_hls(sample_video, root / "live", 24)
    assert wait_for(lambda: (root / "live" / "live.m3u8").exists() and
                    "#EXTINF" in (root / "live" / "live.m3u8").read_text())
    sid, ok, _ = monitor.add_source(s, db, name="Test live", kind="hls", url=f"{srv.url}/live/live.m3u8",
                                    authorisation="Local test stream")
    assert ok and mon.poll(sid)["live"] is True
    item = db.one("SELECT * FROM source_items")
    assert item["state"] == "recording" and item["kind"] == "live"
    time.sleep(6)
    mon.procs[item["id"]].kill()                           # simulate the recorder crashing mid-stream
    mon.procs[item["id"]].wait()
    mon.supervise()                                        # still live → a new segment starts
    assert db.one("SELECT restarts, segments FROM source_items")["restarts"] == 1
    gen.wait(timeout=120)                                  # stream ends (ENDLIST written)
    assert wait_for(lambda: (mon.supervise() or True) and
                    db.one("SELECT state FROM source_items")["state"] in ("handed_off", "failed"), timeout=90)
    item = db.one("SELECT * FROM source_items")
    assert item["state"] == "handed_off", item["error"]
    assert item["segments"] >= 2
    files = list(s.inbox.glob("monitor_*.mp4"))
    assert len(files) == 1 and Path(str(files[0]) + ".source.json").exists()
    # Existing inbox importer picks it up (size must be stable across two scans)
    w = InboxWatcher(s, db, p)
    import os
    old = time.time() - 60
    os.utime(files[0], (old, old))
    w.scan_once()
    res = w.scan_once()
    assert res and "imported" in res[0][1]
    rec = db.one("SELECT * FROM recordings")
    assert rec["source_type"] == "monitor" and rec["source_url"].endswith("/live/live.m3u8")
    assert rec["duration"] > 15
    assert db.one("SELECT state, recording_id FROM source_items")["state"] == "imported"
    q.drain(600)
    assert db.one("SELECT COUNT(*) n FROM clips WHERE recording_id=? AND status='ready'", (rec["id"],))["n"] >= 1
    # The same broadcast is not recorded again
    db.execute("UPDATE sources SET next_check = 0")
    mon.poll(sid)
    assert db.one("SELECT COUNT(*) n FROM source_items")["n"] == 1


def test_restart_recovery_finalizes_ended_stream(env, sample_video):
    s, db, q, p, mon, root, srv = env
    gen = start_live_hls(sample_video, root / "live2", 8)
    gen.wait(timeout=60)                                   # stream already over
    sid, ok, _ = monitor.add_source(s, db, name="Ended", kind="hls", url=f"{srv.url}/live2/live.m3u8")
    wd = s.work_dir / "live" / "manual"
    wd.mkdir(parents=True)
    # Simulate an app crash mid-recording: one segment on disk, item still 'recording'
    subprocess.run(["ffmpeg", "-v", "error", "-i", f"{srv.url}/live2/live.m3u8", "-c", "copy", "-f", "mpegts",
                    str(wd / "seg_001.ts")], check=True)
    iid = db.insert("source_items", source_id=sid, item_key="k1", url=f"{srv.url}/live2/live.m3u8", kind="live",
                    state="recording", work_dir=str(wd), segments=1, started_at=time.time() - 30)
    mon2 = monitor.SourceMonitor(s, db, q)
    mon2.recover()
    assert db.one("SELECT state FROM source_items WHERE id=?", (iid,))["state"] == "handed_off"


def test_integrity_check_rejects_truncated_file(tmp_path, sample_video):
    ok, problems, _ = monitor.verify_recording(sample_video, 5)
    assert ok, problems
    bad = tmp_path / "bad.mp4"
    data = sample_video.read_bytes()
    bad.write_bytes(data[: len(data) // 3])
    ok, problems, _ = monitor.verify_recording(bad, 5)
    assert not ok and problems
    short_ok, short_problems, _ = monitor.verify_recording(sample_video, 10_000)
    assert not short_ok and "too short" in short_problems[0]


def test_channel_source_with_mocked_lister(env):
    """MOCK: yt-dlp channel listing is replaced; verifies VOD queuing and live-recorder command."""
    s, db, q, p, mon, root, srv = env
    sid, ok, _ = monitor.add_source(s, db, name="Channel", kind="channel", url=f"{srv.url}/channel")
    calls = {"n": 0}

    def fake_lister(settings, url, limit=15):
        calls["n"] += 1
        base = [{"key": "a", "title": "Old", "url": f"{srv.url}/a.mp4", "live": False}]
        if calls["n"] > 1:
            base.insert(0, {"key": "b", "title": "New", "url": f"{srv.url}/b.mp4", "live": False})
        return base
    mon.lister = fake_lister
    assert mon.poll(sid)["new"] == 0
    db.execute("UPDATE sources SET next_check = 0")
    assert mon.poll(sid)["new"] == 1
    assert db.one("SELECT COUNT(*) n FROM jobs WHERE kind='download_url'")["n"] == 1
    src = db.one("SELECT * FROM sources WHERE id=?", (sid,))
    cmd = mon._recorder_cmd(src, {"url": "https://example.org/live"}, Path("seg.ts"), "ytdlp")
    assert "yt_dlp" in cmd and "--hls-use-mpegts" in cmd and "--cookies" not in " ".join(cmd)


def test_external_recorder_sidecar_import(env, sample_video):
    """A Multi-Stream-Recorder/yt-dlp style JSON sidecar is used for provenance."""
    s, db, q, p, mon, root, srv = env
    f = s.inbox / "stream_2026-11-14.mp4"
    shutil.copy(sample_video, f)
    (s.inbox / "stream_2026-11-14.json").write_text(json.dumps({
        "webpage_url": "https://council.example.gov/live", "title": "Council meeting", "channel": "Council"}))
    meta, path = read_sidecar(f)
    assert meta["source_url"] == "https://council.example.gov/live" and meta["title"] == "Council meeting"
    w = InboxWatcher(s, db, p)
    import os
    old = time.time() - 60
    os.utime(f, (old, old))
    w.scan_once()
    assert "provenance sidecar" in w.scan_once()[0][1]
    rec = db.one("SELECT * FROM recordings")
    assert rec["source_type"] == "recorder_import" and rec["title"] == "Council meeting"
    assert (Path(rec["file_path"]).parent / "recorder_sidecar.json").exists()
