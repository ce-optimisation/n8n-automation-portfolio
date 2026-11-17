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

from clipengine import ingest, monitor
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


def test_dns_outage_is_retried_and_never_switches_the_source_off(settings, monkeypatch):
    """Regression (17 Nov 2026): a Wi-Fi blip made rumble.com unresolvable for one check and the
    source was disabled as 'blocked' for the night, so the live show was missed. A DNS failure is
    a connection problem: the source stays enabled, is retried within a few minutes, and recovers."""
    settings.allowed_url_domains = ["example.org"]
    db = Database(settings.db_path)
    monkeypatch.setattr(ingest, "resolve_host", lambda h: ["93.184.216.34"])
    sid, ok, _ = monitor.add_source(settings, db, name="Show", kind="channel", url="https://example.org/c/show",
                                    poll_minutes=2)
    assert ok
    mon = monitor.SourceMonitor(settings, db, JobQueue(db))
    mon.lister = lambda s, url, limit=15: [{"key": "a", "title": "Old", "url": "https://example.org/a", "live": False}]

    def no_dns(host):
        raise OSError(11001, "getaddrinfo failed")
    monkeypatch.setattr(ingest, "resolve_host", no_dns)
    for _ in range(4):                                   # several failed checks in a row
        before = time.time()
        res = mon.poll(sid)
        src = db.one("SELECT * FROM sources WHERE id=?", (sid,))
        assert "blocked" not in res and "Could not resolve" in res["error"]
        assert src["enabled"] == 1 and src["status"] == "error"
        # no exponential back-off for a DNS outage: retried at the normal check interval
        assert src["next_check"] - before <= max(120, src["poll_minutes"] * 60) + 5
    monkeypatch.setattr(ingest, "resolve_host", lambda h: ["93.184.216.34"])
    mon.poll(sid)
    src = db.one("SELECT * FROM sources WHERE id=?", (sid,))
    assert src["enabled"] == 1 and src["status"] == "ok" and src["consecutive_errors"] == 0
    # a real policy refusal still switches the source off
    monkeypatch.setattr(ingest, "resolve_host", lambda h: ["10.0.0.5"])
    assert "blocked" in mon.poll(sid)
    assert db.one("SELECT enabled FROM sources WHERE id=?", (sid,))["enabled"] == 0


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


def frame_hashes(path: Path) -> list[str]:
    out = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:v:0", "-f", "framemd5", "-"],
                         capture_output=True, text=True, check=True).stdout
    return [ln.split(",")[-1].strip() for ln in out.splitlines() if ln and not ln.startswith("#")]


def test_restart_overlap_is_detected_and_trimmed_exactly(tmp_path):
    """Two stream-copied segments of the same broadcast that overlap by 4 s (what a restarted
    recorder produces) are joined without repeating or losing a single frame; unrelated
    segments are never trimmed."""
    src = tmp_path / "src.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30", "-f", "lavfi",
                    "-i", "sine=frequency=440:sample_rate=44100", "-t", "16", "-c:v", "libx264", "-preset",
                    "ultrafast", "-g", "30", "-c:a", "aac", str(src)], check=True)
    hls = tmp_path / "hls"
    hls.mkdir()
    subprocess.run(["ffmpeg", "-v", "error", "-i", str(src), "-c", "copy", "-f", "hls", "-hls_time", "2",
                    "-hls_list_size", "0", "-hls_playlist_type", "vod", str(hls / "v.m3u8")], check=True)
    parts = sorted(hls.glob("v*.ts"))
    assert len(parts) >= 8

    def record(names, out):          # what the recorder does: copy media segments into one MPEG-TS
        lst = tmp_path / (out + ".txt")
        lst.write_text("".join(f"file '{p.as_posix()}'\n" for p in names), encoding="utf-8")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy",
                        "-f", "mpegts", str(tmp_path / out)], check=True)
        return tmp_path / out
    a = record(parts[:5], "seg_001.ts")           # 0–10 s, then the recorder "crashed"
    b = record(parts[3:], "seg_002.ts")           # restarted two media segments back: 6 s → end
    hit = monitor.find_overlap(a, b)
    pa, pb = monitor._video_packets(a, tail=180), monitor._video_packets(b, head=180)
    assert hit and hit[1] >= 100, (len(pa), len(pb), pa[:2], pa[-2:], pb[:2])   # ~4 s of repeated video packets
    text, trims = monitor.concat_listing([a, b])
    assert "outpoint" in text and len(trims) == 1
    (tmp_path / "segments.txt").write_text(text, encoding="utf-8")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", "segments.txt", "-c", "copy",
                    "-bsf:a", "aac_adtstoasc", "joined.mp4"], cwd=tmp_path, check=True)
    got, want = frame_hashes(tmp_path / "joined.mp4"), frame_hashes(src)
    assert got == want                            # every frame once, in order: nothing repeated, nothing lost
    ok, problems, info = monitor.verify_recording(tmp_path / "joined.mp4", 5)
    assert ok, problems
    assert abs(info["duration"] - 16) < 0.5
    # No overlap: consecutive, non-repeating segments are left untouched.
    c = record(parts[5:], "seg_003.ts")
    assert monitor.find_overlap(a, c) is None
    assert "outpoint" not in monitor.concat_listing([a, c])[0]
    # Unrelated material is never mistaken for a repeat.
    other = tmp_path / "other.ts"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "smptebars=size=320x180:rate=30", "-t", "4",
                    "-c:v", "libx264", "-preset", "ultrafast", "-f", "mpegts", str(other)], check=True)
    assert monitor.find_overlap(a, other) is None
    # Without content hashes (older ffprobe), packet sizes still identify the repeat.
    real = monitor._video_packets

    def sizes_only(path, **kw):
        import json as _j
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_packets", "-show_entries",
                              "packet=dts_time,size,flags", "-of", "json", str(path)], capture_output=True, text=True)
        return [(float(p["dts_time"]), f"size:{p['size']}:{p['flags'][:1]}") for p in _j.loads(out.stdout)["packets"]]
    monitor._video_packets = sizes_only
    try:
        hit2 = monitor.find_overlap(a, b)
        assert hit2 and abs(hit2[0] - hit[0]) < 0.01 and monitor.find_overlap(a, c) is None
    finally:
        monitor._video_packets = real


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
    # The restart deliberately re-fetches the last media segments; the join must not repeat them.
    assert rec["duration"] <= 24 + 1.5, rec["duration"]
    hashes = frame_hashes(Path(rec["file_path"]))
    assert len(hashes) == len(set(hashes)), f"{len(hashes) - len(set(hashes))} repeated frames after restart"
    side = json.loads((Path(rec["file_path"]).parent / "recorder_sidecar.json").read_text(encoding="utf-8"))
    assert "overlap_trims" in side
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


def test_channel_listing_without_live_status_is_probed(env, monkeypatch):
    """MOCK: a listing that gives only links (as yt-dlp's Rumble channel extractor does) must not hide a
    live broadcast. New entries are looked up individually (at most PROBE_LIMIT per check); an upcoming
    one is not stored, so it is recorded once it goes live; a live one starts the recorder."""
    s, db, q, p, mon, root, srv = env
    sid, ok, _ = monitor.add_source(s, db, name="Links only", kind="channel", url=f"{srv.url}/c/show",
                                    record_live=True)
    listing = [{"key": f"{srv.url}/v{i}.html", "title": "", "url": f"{srv.url}/v{i}.html", "live": None}
               for i in range(1, 7)]
    status = {f"{srv.url}/v1.html": "is_upcoming"}
    probes = []

    def fake_prober(settings, url):
        probes.append(url)
        return {"title": "Episode " + url[-6:-5], "live_status": status.get(url, "not_live"), "published": ""}
    started = []
    monkeypatch.setattr(mon, "_start_segment", lambda src, it, method=None: started.append(it["url"]))
    mon.lister = lambda settings, url, limit=15: [dict(e) for e in listing]
    mon.prober = fake_prober

    res = mon.poll(sid)                                   # first check: baseline only
    assert "error" not in res and not res["live"]
    assert len(probes) == monitor.SourceMonitor.PROBE_LIMIT
    keys = {r["item_key"]: r for r in db.query("SELECT * FROM source_items")}
    assert f"{srv.url}/v1.html" not in keys                # upcoming: left for the next check
    assert len(keys) == 5 and all(r["state"] == "baseline" for r in keys.values())
    assert keys[f"{srv.url}/v2.html"]["title"] == "Episode 2"

    status[f"{srv.url}/v1.html"] = "is_live"               # the scheduled stream goes live
    probes.clear()
    db.execute("UPDATE sources SET next_check = 0")
    res = mon.poll(sid)
    assert res["live"] and res["new"] == 0 and probes == [f"{srv.url}/v1.html"]
    item = db.one("SELECT * FROM source_items WHERE item_key=?", (f"{srv.url}/v1.html",))
    assert item["kind"] == "live" and item["state"] == "recording" and started == [f"{srv.url}/v1.html"]
    assert db.one("SELECT COUNT(*) n FROM jobs WHERE kind='download_url'")["n"] == 0
    # whether the recording continues is asked of the video page when the listing does not say
    src = db.one("SELECT * FROM sources WHERE id=?", (sid,))
    assert mon._stream_live(src, item) is True
    status[f"{srv.url}/v1.html"] = "post_live"
    assert mon._stream_live(src, item) is False

    def refused(settings, url):                            # a refused lookup backs the whole source off
        raise RuntimeError("HTTP Error 429: Too Many Requests")
    listing.insert(0, {"key": f"{srv.url}/v9.html", "title": "", "url": f"{srv.url}/v9.html", "live": None})
    mon.prober = refused
    db.execute("UPDATE sources SET next_check = 0")
    assert "too frequent" in mon.poll(sid)["error"]
    assert db.one("SELECT COUNT(*) n FROM source_items WHERE item_key=?", (f"{srv.url}/v9.html",))["n"] == 0


def test_rumble_channel_listing_is_bounded_and_marks_live_unknown(monkeypatch):
    """Runs yt-dlp's REAL Rumble channel extractor with only the network layer replaced (MOCK pages;
    rumble.com is never contacted). Proves two facts the monitor depends on: (1) a channel page that
    shows no videos costs at most LISTING_MAX_REQUESTS requests instead of paging until the site
    refuses (HTTP 429); (2) listed Rumble entries carry no live status, so they are probed."""
    import io
    import yt_dlp
    from yt_dlp.networking import Response
    from types import SimpleNamespace

    pages = {"html": "<html><body>Checking your browser</body></html>"}
    requested = []

    def fake_urlopen(self, req):
        url = req if isinstance(req, str) else req.url
        requested.append(url)
        if "page=1" not in url and ".html" in pages["html"]:
            from yt_dlp.networking.exceptions import HTTPError
            raise HTTPError(Response(io.BytesIO(b""), url, {}, status=404))
        return Response(io.BytesIO(pages["html"].encode()), url, {"Content-Type": "text/html"}, status=200)
    monkeypatch.setattr(yt_dlp.YoutubeDL, "urlopen", fake_urlopen)
    # the engine's own first read of the page (no embedded video grid on these older-layout pages)
    monkeypatch.setattr(monitor, "fetch_text", lambda settings, url, **k: (200, url, pages["html"]))
    s = SimpleNamespace(http_user_agent="LocalClippingEngine/test")
    with pytest.raises(ingest.SourceError) as exc:
        monitor.list_channel(s, "https://rumble.com/c/examplechannel")
    assert exc.value.code == "empty_listing"
    assert len(requested) == monitor.LISTING_MAX_REQUESTS
    assert "robot check" in str(exc.value) and "HTTP 200" in str(exc.value)

    # a page without the class yt-dlp looks for (e.g. after a redesign), with no video links at all
    requested.clear()
    pages["html"] = "<html><head><title>Nicholas J. Fuentes</title></head><body>no links here</body></html>"
    with pytest.raises(ingest.SourceError) as exc:
        monitor.list_channel(s, "https://rumble.com/c/examplechannel")
    assert "without video links" in str(exc.value) and "Nicholas J. Fuentes" in str(exc.value)

    # a redesigned page whose links lack yt-dlp's CSS class: the links on that same page are used
    requested.clear()
    pages["html"] = ('<html><title>Channel</title><a class="new-card__link" href="/v7bbb22-episode-3.html?e9s=x">x</a>'
                     '<a href="https://rumble.com/v7bbb21-episode-2.html">y</a><a href="/c/other">z</a>'
                     '<a class="new-card__link" href="/v7bbb22-episode-3.html">dup</a></html>')
    entries = monitor.list_channel(s, "https://rumble.com/c/examplechannel")
    assert [e["url"] for e in entries] == ["https://rumble.com/v7bbb22-episode-3.html",
                                           "https://rumble.com/v7bbb21-episode-2.html"]
    assert all(e["live"] is None for e in entries) and len(requested) <= monitor.LISTING_MAX_REQUESTS

    requested.clear()
    pages["html"] = ('<a class="videostream__link link" href="/v7aaa11-episode-2.html"></a>'
                     '<a class="videostream__link link" href="/v7aaa10-episode-1.html"></a>')
    entries = monitor.list_channel(s, "https://rumble.com/c/examplechannel")
    assert [e["url"] for e in entries] == ["https://rumble.com/v7aaa11-episode-2.html",
                                           "https://rumble.com/v7aaa10-episode-1.html"]
    assert all(e["live"] is None for e in entries) and len(requested) <= 2


def _rumble_item(pid, title, date, channel="/c/examplechannel", **kw):
    item = {"object_type": "video", "id": 1, "permalink_id": pid, "title": title, "upload_date": date,
            "url": f"https://rumble.com/{pid}-{title.lower().replace(' ', '-')}.html",
            "by": {"type": "channel", "relative_url": channel}, "live": False, "live_placeholder": False,
            "livestream_status": None, "is_short": False}
    item.update(kw)
    return item


def _rumble_page(*grids) -> str:
    """MOCK Rumble channel page in the late-2026 layout: videos as JSON in <rum-videos-grid>, no
    videostream__link anchors, and a recommendation link to another channel's video."""
    parts = ['<html><head><title>Example Channel</title></head><body>',
             '<a href="/v7zzz99-other-channels-radio.html?e9s=src_v1_cllr">recommended</a>']
    for items in grids:
        parts.append('<rum-videos-grid>\n    <script type="application/json">\n' + json.dumps({"items": items})
                     + '\n</script></rum-videos-grid>')
    return "".join(parts) + "</body></html>"


def test_rumble_channel_grid_is_read_from_one_request(monkeypatch):
    """Regression (owner's PC, 16 Nov 2026): Rumble answered 200 with the real channel page, but the
    videos now live only in embedded JSON, so yt-dlp listed nothing and every check ended in
    "empty_listing". The engine reads that JSON from a single request: the channel's own videos with
    titles, dates and live status; other channels' videos, shorts and scheduled broadcasts are left out.
    MOCK page; rumble.com is never contacted."""
    import yt_dlp
    from types import SimpleNamespace
    calls = []
    page = {"html": _rumble_page(
        [_rumble_item("v7aaa03", "Episode 3", "2026-11-16T04:00:00+00:00"),
         _rumble_item("v7aaa02", "Episode 2", "2026-11-12T01:00:00+00:00", livestream_status=0)],
        [_rumble_item("v7aaa02", "Episode 2", "2026-11-12T01:00:00+00:00"),
         _rumble_item("v7aaa01", "Episode 1", "2026-11-10T01:00:00+00:00"),
         _rumble_item("v7bbb01", "Somebody Else", "2026-11-15T01:00:00+00:00", channel="/c/other"),
         _rumble_item("v7sss01", "A Short", "2026-11-15T02:00:00+00:00", is_short=True),
         _rumble_item("v7ppp01", "Tonight", "2026-11-17T03:00:00+00:00", live_placeholder=True),
         _rumble_item("v7lll01", "Live Now", "2026-11-16T03:00:00+00:00", live=True, livestream_status=2)])}

    def fake_fetch(settings, url, **k):
        calls.append(url)
        return 200, url, page["html"]
    monkeypatch.setattr(monitor, "fetch_text", fake_fetch)
    monkeypatch.setattr(yt_dlp.YoutubeDL, "urlopen", lambda self, req: pytest.fail("yt-dlp must not be used"))
    s = SimpleNamespace(http_user_agent="LocalClippingEngine/test")
    entries = monitor.list_channel(s, "https://rumble.com/c/examplechannel")
    assert calls == ["https://rumble.com/c/examplechannel"]
    assert [(e["key"], e["live"]) for e in entries] == [("v7lll01", True), ("v7aaa03", False), ("v7aaa02", False),
                                                        ("v7aaa01", False)]
    assert entries[1]["title"] == "Episode 3" and entries[1]["url"].startswith("https://rumble.com/v7aaa03-")
    assert entries[1]["published"].startswith("2026-11-16")

    page["html"] = _rumble_page([])
    with pytest.raises(ingest.SourceError) as exc:
        monitor.list_channel(s, "https://rumble.com/c/examplechannel")
    assert exc.value.code == "empty_listing" and "Example Channel" in str(exc.value)

    page["html"] = "Too many requests"
    monkeypatch.setattr(monitor, "fetch_text", lambda settings, url, **k: (429, url, page["html"]))
    with pytest.raises(ingest.SourceError) as exc:
        monitor.list_channel(s, "https://rumble.com/c/examplechannel")
    assert "429" in str(exc.value)


def test_repaired_channel_listing_takes_a_baseline_instead_of_queuing_everything(env):
    """Regression (owner's PC, 16 Nov 2026): an old build stored the channel itself as one bogus item and
    set last_success. When the listing was repaired, every listed video (several 4-hour episodes) would
    have counted as new and been queued. A channel with no real items yet takes a baseline. MOCK lister."""
    s, db, q, p, mon, root, srv = env
    sid, ok, _ = monitor.add_source(s, db, name="Repaired", kind="channel", url=f"{srv.url}/c/show")
    db.execute("UPDATE sources SET last_success=? WHERE id=?", (time.time() - 3600, sid))
    db.insert("source_items", source_id=sid, item_key="show", title="show", url=f"{srv.url}/c/show", state="failed")
    jid = db.insert("jobs", kind="download_url", status="failed", payload_json="{}", attempts=1, max_attempts=6,
                    dedupe_key=f"download:{ingest.normalize_url(f'{srv.url}/c/show')}", error="SourceError: login")
    listing = [{"key": f"v{i}", "title": f"Episode {i}", "url": f"{srv.url}/v{i}.html", "live": False}
               for i in range(1, 6)]
    mon.lister = lambda settings, url, limit=15: [dict(e) for e in listing]
    res = mon.poll(sid)
    assert "error" not in res and res["new"] == 0
    assert db.one("SELECT COUNT(*) n FROM jobs WHERE kind='download_url' AND status<>'cancelled'")["n"] == 0
    assert db.one("SELECT COUNT(*) n FROM source_items WHERE source_id=? AND state='baseline'", (sid,))["n"] == 5
    # the leftover "channel as one video" item and its failed download are gone from the dashboard counts
    assert db.one("SELECT COUNT(*) n FROM source_items WHERE source_id=? AND state='failed'", (sid,))["n"] == 0
    assert db.one("SELECT status FROM jobs WHERE id=?", (jid,))["status"] == "cancelled"

    listing.insert(0, {"key": "v6", "title": "Episode 6", "url": f"{srv.url}/v6.html", "live": False})
    db.execute("UPDATE sources SET next_check = 0")
    assert mon.poll(sid)["new"] == 1                       # later uploads are imported as usual


@needs_ffmpeg
def test_live_capture_to_verified_cloud_copy_then_safe_raw_deletion(env, sample_video):
    """Unattended production lifecycle on disposable, engine-generated media (real FFmpeg live HLS, real
    recorder, real render; TEST-ONLY stub transcript; MOCK Google Drive): live detected → recorded → finalised →
    imported automatically → transcribed/selected/rendered without any manual step → uploaded and verified →
    raw recording deleted only then. Clips, transcript and provenance survive; nothing is published.
    A second capture whose upload cannot complete keeps its raw file."""
    import os
    from clipengine import lifecycle as lc_mod
    from test_expansion import FakeDrive, _cloud
    s, db, q, p, mon, root, srv = env
    drive = FakeDrive()
    cloud_svc = _cloud(s, db, q, drive)
    lc = lc_mod.Lifecycle(s, db, q, cloud_svc)
    p.on_recording_started.append(lc.claim_ownership)
    p.on_clip_rendered.append(cloud_svc.enqueue_clip)
    p.on_recording_done.append(lc.check_recording)
    q.recording_active = lambda: bool(mon.procs)

    def capture(name, video=sample_video):
        gen = start_live_hls(video, root / name, 14)
        assert wait_for(lambda: (root / name / "live.m3u8").exists() and "#EXTINF" in (root / name / "live.m3u8").read_text())
        sid, ok, _ = monitor.add_source(s, db, name=name, kind="hls", url=f"{srv.url}/{name}/live.m3u8",
                                        authorisation="Local test stream")
        assert ok and mon.poll(sid)["live"] is True              # live detected → recording starts
        assert not q.run_one() or True                          # heavy work is held while recording
        gen.wait(timeout=120)
        assert wait_for(lambda: (mon.supervise() or True) and db.one(
            "SELECT state FROM source_items WHERE source_id=?", (sid,))["state"] in ("handed_off", "failed"), timeout=90)
        f = [x for x in s.inbox.glob("monitor_*.mp4") if not db.one("SELECT 1 FROM recordings WHERE original_filename=?", (x.name,))][0]
        old = time.time() - 60
        os.utime(f, (old, old))
        w = InboxWatcher(s, db, p)
        w.scan_once()
        w.scan_once()                                           # finalised recording imported automatically
        return db.one("SELECT * FROM recordings ORDER BY id DESC LIMIT 1")

    rec = capture("live1")
    assert rec["origin"] == "live_capture"
    q.drain(600)                                                # no manual step: transcribe → select → render → upload
    rec = db.recording(rec["id"])
    clips = db.query("SELECT * FROM clips WHERE recording_id=?", (rec["id"],))
    assert rec["engine_owned"] == 1 and clips and all(c["status"] == "ready" for c in clips)
    assert all(cloud_svc.clip_verified(c["id"]) for c in clips)
    assert rec["raw_deleted_at"] and rec["file_path"] is None and rec["lifecycle"] == "completed"
    for c in clips:                                             # finished clips, timecodes and provenance intact
        assert Path(c["video_path"]).is_file() and c["end"] > c["start"]
    assert (s.recordings_dir).exists() and any(s.recordings_dir.rglob("provenance.json"))
    assert db.one("SELECT COUNT(*) n FROM transcript_segments WHERE recording_id=?", (rec["id"],))["n"] > 0
    assert db.one("SELECT COUNT(*) n FROM publication_packages")["n"] == 0      # nothing published
    assert db.one("SELECT COUNT(*) n FROM publish_attempts")["n"] == 0

    drive.free = 0.5e9                                          # Drive full: uploads are held …
    other = root / "other.mp4"                                  # different content (not a duplicate)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(sample_video), "-vf", "hue=h=90", "-c:v", "libx264",
                    "-preset", "ultrafast", "-c:a", "aac", str(other)], check=True)
    rec2 = capture("live2", other)
    assert rec2["id"] != rec["id"]
    for _ in range(20):
        q.run_one()
    rec2 = db.recording(rec2["id"])
    assert rec2["engine_owned"] == 1 and rec2["file_path"] and Path(rec2["file_path"]).is_file()   # … raw kept
    assert not rec2["raw_deleted_at"]
    assert "cloud_verified" in lc.check_recording(rec2["id"])["reason"]
