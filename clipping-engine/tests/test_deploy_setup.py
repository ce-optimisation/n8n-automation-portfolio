"""Deployment, onboarding and unattended-running features.

External programs and services are replaced by clearly labelled fakes:
  * `tailscale`  → tests/fake_tailscale.py (TEST-ONLY fake CLI)  — no real tailnet is contacted
  * SMTP         → an in-test fake server class                  — no email is ever sent
Windows-only behaviour (keep-awake, installer, launcher) is exercised for real on the Windows CI
runner; here the platform-independent logic is tested.
"""
import json
import os
import shutil
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clipengine import config, doctor, health, ingest, monitor, notify, power, setup_wizard, tailscale
from clipengine.db import Database
from clipengine.inbox import InboxWatcher
from clipengine.jobs import JobQueue
from clipengine.pipeline import Pipeline
from clipengine.web.app import create_app
from conftest import needs_ffmpeg

HERE = Path(__file__).parent


# ---- local settings file -------------------------------------------------------------------

def test_env_writer_keeps_comments_and_other_keys(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# my notes\nCLIPENGINE_PORT=8765\n#NOTIFY_TO=old@example.test\nDASHBOARD_PASSWORD=\n\nX=1\n",
                   encoding="utf-8")
    config.update_env_file({"DASHBOARD_PASSWORD": "abc-def", "NOTIFY_TO": "owner@example.test"}, env)
    text = env.read_text(encoding="utf-8")
    assert "# my notes" in text and "CLIPENGINE_PORT=8765" in text and "X=1" in text
    assert "DASHBOARD_PASSWORD=abc-def" in text and text.count("DASHBOARD_PASSWORD=") == 1
    assert "#NOTIFY_TO=old@example.test" in text and "\nNOTIFY_TO=owner@example.test" in text
    config.update_env_file({"NOTIFY_TO": "new@example.test"}, env)            # second write replaces in place
    assert env.read_text(encoding="utf-8").count("\nNOTIFY_TO=") == 1
    with pytest.raises(ValueError):
        config.update_env_file({"NOTIFY_TO": "a@b.c\nDASHBOARD_PASSWORD=x"}, env)   # no line injection


def test_apply_settings_only_allows_known_keys_and_never_touches_project_env(settings):
    assert settings.env_file != config.PROJECT_ROOT / ".env"
    config.apply_settings(settings, {"NOTIFY_TO": "owner@example.test", "PREVENT_SLEEP": "monitoring"})
    assert settings.notify_to == "owner@example.test" and settings.prevent_sleep == "monitoring"
    assert "NOTIFY_TO=owner@example.test" in settings.env_file.read_text(encoding="utf-8")
    with pytest.raises(ValueError):
        config.apply_settings(settings, {"TIKTOK_CLIENT_SECRET": "x"})
    os.environ.pop("NOTIFY_TO", None)
    os.environ.pop("PREVENT_SLEEP", None)


def test_first_env_is_created_from_the_example(tmp_path):
    env = tmp_path / "new.env"
    config.update_env_file({"DASHBOARD_URL": "https://pc.tail1234.ts.net"}, env)
    text = env.read_text(encoding="utf-8")
    assert "DASHBOARD_URL=https://pc.tail1234.ts.net" in text and "CLIPS_PER_RECORDING" in text


# ---- Tailscale (TEST-ONLY fake CLI) -----------------------------------------------------------

@pytest.fixture
def fake_ts(tmp_path, monkeypatch):
    state = tmp_path / "ts_state.json"

    def write(**kw):
        cur = {"logged_in": True, "https": True, "dns": "office-pc.tail1a2b3.ts.net", "serve": None, "funnel": False}
        if state.exists():
            cur = json.loads(state.read_text(encoding="utf-8"))
        cur.update(kw)
        state.write_text(json.dumps(cur), encoding="utf-8")
    write()
    monkeypatch.setenv("FAKE_TAILSCALE_STATE", str(state))
    monkeypatch.setenv("TAILSCALE_CLI", str(HERE / "fake_tailscale.py"))
    return write


def test_tailscale_not_installed_is_reported_honestly(monkeypatch, settings):
    monkeypatch.setenv("TAILSCALE_CLI", "/nonexistent/tailscale")
    st = tailscale.status()
    assert st["installed"] is False and st["running"] is False
    rep = tailscale.setup_phone_access(settings)
    assert rep["ok"] is False and "Install Tailscale" in rep["next_step"]
    assert settings.dashboard_password == "" and settings.dashboard_url == ""      # nothing changed


def test_phone_access_setup_uses_the_real_assigned_name(fake_ts, settings, monkeypatch):
    monkeypatch.setattr(tailscale, "verify", lambda url, timeout=15.0: (True, "The dashboard answered."))
    st = tailscale.status()
    assert st["running"] and st["dns_name"] == "office-pc.tail1a2b3.ts.net" and st["https_enabled"]
    assert tailscale.serve_status(settings.port)["serving"] is False
    rep = tailscale.setup_phone_access(settings)
    assert rep["ok"] and rep["url"] == "https://office-pc.tail1a2b3.ts.net"
    assert settings.dashboard_url == "https://office-pc.tail1a2b3.ts.net"          # used for email links
    assert len(rep["password_created"]) >= 20 and settings.dashboard_password == rep["password_created"]
    text = settings.env_file.read_text(encoding="utf-8")
    assert "DASHBOARD_URL=https://office-pc.tail1a2b3.ts.net" in text and "DASHBOARD_PASSWORD=" + rep["password_created"] in text
    assert tailscale.serve_status(settings.port) == {"serving": True, "url": rep["url"], "funnel": False, "other_targets": []}
    again = tailscale.setup_phone_access(settings)                                 # idempotent; keeps the password
    assert again["ok"] and again["password_created"] == ""
    ok, _ = tailscale.disable_serve()
    assert ok and tailscale.serve_status(settings.port)["serving"] is False
    for k in ("DASHBOARD_URL", "DASHBOARD_PASSWORD"):
        os.environ.pop(k, None)


def test_phone_access_owner_steps_signed_out_https_off_and_funnel(fake_ts, settings, monkeypatch):
    monkeypatch.setattr(tailscale, "verify", lambda url, timeout=15.0: (True, "ok"))
    fake_ts(logged_in=False)
    rep = tailscale.setup_phone_access(settings)
    assert not rep["ok"] and "not signed in" in rep["next_step"] and "login.tailscale.com/a/abc123" in rep["next_step"]
    fake_ts(logged_in=True, https=False)
    monkeypatch.setattr(tailscale, "enable_serve", lambda port, timeout=25.0, _f=tailscale.enable_serve: _f(port, 3.0))
    t0 = time.time()
    rep = tailscale.setup_phone_access(settings)
    assert time.time() - t0 < 30                                                   # did not hang with the CLI
    assert not rep["ok"] and "https://login.tailscale.com/f/serve?node=n123CNTRL" in rep["next_step"]
    assert settings.dashboard_url == ""                                            # no invented address saved
    fake_ts(https=True, funnel=True)
    rep = tailscale.setup_phone_access(settings)
    assert not rep["ok"] and "FUNNEL" in rep["next_step"] and settings.dashboard_url == ""
    os.environ.pop("DASHBOARD_PASSWORD", None)


def test_phone_setup_button_and_settings_page(fake_ts, settings, monkeypatch):
    monkeypatch.setattr(tailscale, "verify", lambda url, timeout=15.0: (True, "ok"))
    c = TestClient(create_app(settings, start_worker=False))
    page = c.get("/settings")
    assert page.status_code == 200 and "office-pc.tail1a2b3.ts.net" in page.text and "Set up phone access" in page.text
    r = c.post("/settings/phone/setup", follow_redirects=False)
    assert r.status_code == 303 and "Phone+access+is+ready" in r.headers["location"]
    # A password now exists, so the dashboard requires sign-in (even on this PC).
    assert c.get("/settings", follow_redirects=False).status_code == 303
    r = c.post("/login", data={"password": settings.dashboard_password}, follow_redirects=False)
    assert r.status_code == 303
    page = c.get("/settings").text
    assert settings.dashboard_password in page and "write it down now" in page    # shown once, to the owner
    assert "https://office-pc.tail1a2b3.ts.net" in page and "On your iPhone" in page
    c.post("/settings/phone/seen")
    assert settings.dashboard_password not in c.get("/settings").text
    for k in ("DASHBOARD_URL", "DASHBOARD_PASSWORD"):
        os.environ.pop(k, None)


# ---- health, downtime, keep-awake ----------------------------------------------------------------

def test_heartbeat_records_when_the_engine_was_not_running(settings):
    db = Database(settings.db_path)
    clock = {"t": 1_000_000.0}
    hb = health.Heartbeat(db, interval=60, clock=lambda: clock["t"])
    hb.beat()
    clock["t"] += 60
    hb.beat()
    assert json.loads(db.get_kv(health.DOWNTIME_KEY) or "[]") == []
    clock["t"] += 3 * 3600                         # same process, long gap → the PC slept
    hb.beat()
    hb2 = health.Heartbeat(db, interval=60, clock=lambda: clock["t"] + 7200)   # new process 2 h later
    hb2.beat()
    log = json.loads(db.get_kv(health.DOWNTIME_KEY))
    assert [d["kind"] for d in log] == ["asleep", "not_running"]
    assert log[0]["seconds"] == 3 * 3600 and log[1]["seconds"] == 7200


def test_dashboard_states_problems_plainly(settings, monkeypatch):
    from clipengine import ingest as ing
    monkeypatch.setattr(ing, "resolve_host", lambda h: ["93.184.216.34"])
    app = create_app(settings, start_worker=False)
    core, c = app.state.core, TestClient(app)
    assert "nothing needs your attention" in c.get("/").text
    now = time.time()
    core.db.set_kv(health.DOWNTIME_KEY, json.dumps([{"from": now - 7200, "to": now - 3600, "seconds": 3600,
                                                      "kind": "not_running"}]))
    monitor.add_source(settings, core.db, name="Rumble channel", kind="channel", url="https://rumble.com/c/example")
    settings.allowed_url_domains = ["example.org"]
    sid, ok, _ = monitor.add_source(settings, core.db, name="Council feed", kind="feed", url="https://example.org/f.xml")
    assert ok
    core.db.update("sources", sid, status="error", consecutive_errors=3, last_error="Feed returned HTTP 500")
    jid = core.queue.enqueue("process_recording", payload={})
    core.db.update("jobs", jid, status="failed", error="boom")
    settings.monitor_enabled = False
    texts = " | ".join(i["text"] for i in health.items(core))
    assert "Not running for 1 h 0 min" in texts and "NOT recorded" in texts
    assert "“Rumble channel” is disabled — permission missing" in texts
    assert "“Council feed” could not be checked (3 failure(s) in a row): Feed returned HTTP 500" in texts
    assert "1 processing job(s) failed" in texts and "monitoring is switched off" in texts
    page = c.get("/").text
    assert "Needs your attention" in page and "permission missing" in page
    api = c.get("/api/status").json()
    assert api["health"]["ok"] is False and api["health"]["monitoring"]["enabled"] is False
    c.post("/health/dismiss")
    assert "Not running for" not in " ".join(i["text"] for i in health.items(core))


def test_keep_awake_follows_work_and_mode(settings):
    db = Database(settings.db_path)
    calls = []
    ka = power.KeepAwake(settings, db, setter=calls.append)
    assert ka.tick() is False and calls == []
    q = JobQueue(db)
    jid = q.enqueue("process_recording", payload={})
    assert ka.tick() is True and calls == [True]                 # work queued → hold the PC awake
    db.update("jobs", jid, status="done")
    assert ka.tick() is False and calls == [True, False]         # released when idle
    db.insert("sources", name="s", kind="feed", url="https://example.org/f", enabled=1, poll_minutes=15,
              record_live=1, backfill=0, authorisation="", status="ok", status_detail="", next_check=0)
    assert ka.tick() is False                                    # 'active' mode does not hold for idle monitoring
    settings.prevent_sleep = "monitoring"
    assert ka.tick() is True and "monitored" in ka.reason
    settings.prevent_sleep = "off"
    assert ka.tick() is False
    c = TestClient(create_app(settings, start_worker=False))
    r = c.post("/settings/unattended", data={"prevent_sleep": "monitoring"}, follow_redirects=False)
    assert r.status_code == 303 and settings.prevent_sleep == "monitoring"
    assert "PREVENT_SLEEP=monitoring" in settings.env_file.read_text(encoding="utf-8")
    os.environ.pop("PREVENT_SLEEP", None)


# ---- historical recordings: provenance, dates, duplicates -------------------------------------------

def test_recorded_date_parsing_and_origin():
    assert ingest.parse_recorded_at("2024-05-03") == "2024-05-03"
    assert ingest.parse_recorded_at("20240503") == "2024-05-03"
    assert ingest.parse_recorded_at("Fri, 03 May 2024 00:00:00 GMT")[:10] == "2024-05-03"
    assert ingest.parse_recorded_at("2999-01-01") is None and ingest.parse_recorded_at("soon") is None
    assert ingest.date_from_filename("America First 2024-05-03 full.mp4") == "2024-05-03"
    assert ingest.date_from_filename("stream_20231107.mp4") == "2023-11-07"
    assert ingest.date_from_filename("clip_1080p_2160.mp4") is None
    assert ingest.classify_origin("2024-05-03", "upload") == "archive"
    assert ingest.classify_origin(time.strftime("%Y-%m-%d"), "upload") == "recent"
    assert ingest.classify_origin(None, "monitor") == "live_capture"


@needs_ffmpeg
def test_archive_upload_keeps_source_and_date_and_is_never_duplicated(stub_settings, sample_video, tmp_path):
    s = stub_settings
    c = TestClient(create_app(s, start_worker=False))
    url = "https://rumble.com/v7i48ms-example-episode.html"

    def upload(src, **data):
        with open(src, "rb") as fh:
            return c.post("/import/upload", files={"file": ("episode.mp4", fh, "video/mp4")}, data=data,
                          headers={"X-Requested-With": "XMLHttpRequest"})
    r = upload(sample_video, source_url=url, recorded_at="2024-05-03", rights="File supplied by the rights holder")
    assert r.status_code == 200, r.text
    rid = r.json()["recording_id"]
    db = Database(s.db_path)
    rec = db.recording(rid)
    assert rec["source_url"] == ingest.normalize_url(url) and rec["recorded_at"] == "2024-05-03"
    assert rec["origin"] == "archive" and rec["source_type"] == "linked_file"
    meta = json.loads(rec["metadata_json"])
    assert meta["retrieval"]["method"] == "owner_supplied_file"       # the engine did not download it
    prov = json.loads((Path(rec["file_path"]).parent / "provenance.json").read_text(encoding="utf-8"))
    assert prov["recorded_at"] == "2024-05-03" and prov["origin"] == "archive" and prov["source_url"] == url
    assert db.one("SELECT COUNT(*) n FROM jobs WHERE kind='download_url'")["n"] == 0     # nothing fetched from Rumble
    # Same file again → duplicate. A different file for the same episode address → duplicate too.
    assert "already imported" in upload(sample_video, source_url=url).json()["msg"]
    other = tmp_path / "other.mp4"
    import subprocess
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(sample_video), "-t", "20", "-c:v", "libx264", "-preset",
                    "ultrafast", "-c:a", "aac", str(other)], check=True)
    assert "already imported" in upload(other, source_url=url).json()["msg"]
    assert db.one("SELECT COUNT(*) n FROM recordings")["n"] == 1
    assert db.one("SELECT COUNT(*) n FROM jobs WHERE kind='process_recording'")["n"] == 1
    # Invalid input is refused with a clear message.
    assert "YYYY-MM-DD" in upload(other, recorded_at="3024-01-01").json()["err"]
    assert "http" in upload(other, source_url="ftp://x/y").json()["err"]
    # The owner can correct provenance later; the page shows origin and date.
    r = c.post(f"/recordings/{rid}/provenance", data={"source_url": url, "recorded_at": "2024-05-04"},
               follow_redirects=False)
    assert r.status_code == 303 and db.recording(rid)["recorded_at"] == "2024-05-04"
    page = c.get(f"/recordings/{rid}").text
    assert "archive recording" in page and "2024-05-04" in page
    assert "archive" in c.get("/recordings").text


@needs_ffmpeg
def test_inbox_archive_dates_from_filename_and_sidecar(stub_settings, sample_video, tmp_path):
    s = stub_settings
    db = Database(s.db_path)
    p = Pipeline(s, db, JobQueue(db))
    w = InboxWatcher(s, db, p)
    a = s.inbox / "Council meeting 2023-11-07.mp4"
    shutil.copy(sample_video, a)
    b = s.inbox / "old episode.mp4"
    import subprocess
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(sample_video), "-t", "25", "-c:v", "libx264", "-preset",
                    "ultrafast", "-c:a", "aac", str(b)], check=True)
    (s.inbox / "old episode.mp4.json").write_text(json.dumps(
        {"source_url": "https://example.org/archive/ep12", "recorded_at": "2022-02-01", "title": "Episode 12"}),
        encoding="utf-8")
    old = time.time() - 60
    for f in (a, b):
        os.utime(f, (old, old))
    w.scan_once()
    w.scan_once()
    recs = {r["title"]: r for r in db.query("SELECT * FROM recordings")}
    assert recs["Council meeting 2023-11-07"]["recorded_at"] == "2023-11-07"
    assert recs["Council meeting 2023-11-07"]["origin"] == "archive"
    ep = recs["Episode 12"]
    assert ep["recorded_at"] == "2022-02-01" and ep["source_url"] == "https://example.org/archive/ep12"
    assert ep["origin"] == "archive" and ep["source_type"] == "recorder_import"
    # Dropping the same file again never creates a second recording or job.
    shutil.copy(Path(recs["Council meeting 2023-11-07"]["file_path"]), s.inbox / "copy 2023-11-07.mp4")
    os.utime(s.inbox / "copy 2023-11-07.mp4", (old, old))
    w.scan_once()
    assert "duplicate" in w.scan_once()[0][1]
    assert db.one("SELECT COUNT(*) n FROM recordings")["n"] == 2
    assert (s.inbox / "duplicates" / "copy 2023-11-07.mp4").exists()            # moved aside, not deleted


def test_feed_backfill_is_explicit_and_never_repeats(settings, monkeypatch):
    from clipengine import ingest as ing
    monkeypatch.setattr(ing, "resolve_host", lambda h: ["93.184.216.34"])
    settings.allowed_url_domains = ["example.org"]
    db = Database(settings.db_path)
    q = JobQueue(db)
    mon = monitor.SourceMonitor(settings, db, q)
    items = "".join(f"<item><guid>ep{i}</guid><title>Episode {i}</title><enclosure url='https://example.org/ep{i}.mp4' "
                    f"type='video/mp4'/><pubDate>Mon, 0{i} Jan 2024 10:00:00 GMT</pubDate></item>" for i in range(1, 6))
    xml = f"<?xml version='1.0'?><rss version='2.0'><channel>{items}</channel></rss>"
    monkeypatch.setattr(monitor, "fetch_text", lambda s_, url, **kw: (200, url, xml))
    sid, ok, _ = monitor.add_source(settings, db, name="Archive feed", kind="feed", url="https://example.org/feed.xml")
    assert ok and mon.poll(sid)["new"] == 0
    assert db.one("SELECT COUNT(*) n FROM source_items WHERE state='baseline'")["n"] == 5     # noted, not downloaded
    assert db.one("SELECT COUNT(*) n FROM jobs")["n"] == 0
    res = mon.backfill(sid, 2)
    assert res == {"queued": 2, "refused": 0, "remaining": 3}
    jobs = db.query("SELECT payload_json FROM jobs WHERE kind='download_url'")
    assert len(jobs) == 2 and json.loads(jobs[0]["payload_json"])["recorded_at"].startswith("2024-01-0")
    assert "historical backfill" in json.loads(jobs[0]["payload_json"])["rights_attestation"]
    db.execute("UPDATE sources SET next_check = 0")
    assert mon.poll(sid)["new"] == 0                                              # polling never re-queues them
    assert mon.backfill(sid, 50)["queued"] == 3 and mon.backfill(sid, 50)["queued"] == 0
    assert db.one("SELECT COUNT(*) n FROM jobs WHERE kind='download_url'")["n"] == 5
    c = TestClient(create_app(settings, start_worker=False))
    page = c.get("/sources").text
    assert "How it works" in page and "baseline" in page
    # A blocked source can never be back-filled.
    bid, bok, _ = monitor.add_source(settings, db, name="Rumble", kind="channel", url="https://rumble.com/c/x")
    assert not bok and "written permission" in mon.backfill(bid, 5)["blocked"]


def test_feed_parser_refuses_entities_and_explains_non_feeds():
    bomb = ("<?xml version='1.0'?><!DOCTYPE lolz [<!ENTITY lol 'lol'><!ENTITY lol2 '&lol;&lol;&lol;'>]>"
            "<rss><channel><item><title>&lol2;</title></item></channel></rss>")
    with pytest.raises(ingest.SourceError) as e:
        monitor.parse_feed(bomb, "https://example.org/")
    assert e.value.code == "unsafe_feed"
    with pytest.raises(ingest.SourceError) as e:
        monitor.parse_feed("<html><body>Just a web page</body>", "https://example.org/")
    assert e.value.code == "bad_feed" and "not a web page" in str(e.value)
    ok = monitor.parse_feed("<rss><channel><item><guid>a</guid><enclosure url='/a.mp4' type='video/mp4'/></item>"
                            "</channel></rss>", "https://example.org/feed")
    assert ok[0]["url"] == "https://example.org/a.mp4"


# ---- notifications set-up (MOCK SMTP; nothing is sent) --------------------------------------------------

def test_notification_setup_from_the_dashboard_and_approval_rules(settings):
    app = create_app(settings, start_worker=False)
    core, c = app.state.core, TestClient(app)
    page = c.get("/settings/notifications").text
    assert "Set up in four steps" in page and "Option A" in page and "Option B" in page
    assert "Personal Microsoft accounts only" in page and "apppasswords" in page
    r = c.post("/settings/notifications/recipient", data={"notify_to": "not-an-email"}, follow_redirects=False)
    assert "does+not+look+like" in r.headers["location"]
    c.post("/settings/notifications/recipient", data={"notify_to": "owner@example.test",
                                                      "dashboard_url": "https://pc.tail1a2b3.ts.net/"})
    assert settings.notify_to == "owner@example.test" and settings.dashboard_url == "https://pc.tail1a2b3.ts.net"
    r = c.post("/settings/notifications/transport", data={"transport": "graph", "outlook_client_id": "nope"},
               follow_redirects=False)
    assert "Application+%28client%29+ID" in r.headers["location"] and settings.outlook_client_id == ""
    cid = "12345678-90ab-cdef-1234-567890abcdef"
    c.post("/settings/notifications/transport", data={"transport": "graph", "outlook_client_id": cid})
    assert settings.outlook_client_id == cid and settings.notify_transport == "graph"
    c.post("/settings/notifications/transport", data={"transport": "smtp", "smtp_host": "smtp.gmail.com",
                                                      "smtp_port": "587", "smtp_user": "sender@gmail.test",
                                                      "smtp_password": "abcd efgh ijkl mnop"})
    assert settings.notify_transport == "smtp" and core.notifier.smtp.password() == "abcdefghijklmnop"
    env = settings.env_file.read_text(encoding="utf-8")
    assert "abcdefghijklmnop" not in env and "SMTP_HOST=smtp.gmail.com" in env    # password not written to .env
    assert core.notifier.mailer.status()["connected"] is True
    # Still held: a configured account is not an approval.
    assert notify.is_approved(settings, core.db) is False
    c.post("/settings/notifications/approve", data={"confirm_to": "owner@example.test", "agree": "yes"})
    assert notify.is_approved(settings, core.db) is True
    c.post("/settings/notifications/recipient", data={"notify_to": "other@example.test"})
    assert notify.is_approved(settings, core.db) is False                         # recipient change revokes
    c.post("/settings/notifications/smtp/forget")
    assert core.notifier.smtp.password() == ""
    for k in ("NOTIFY_TO", "DASHBOARD_URL", "OUTLOOK_CLIENT_ID", "NOTIFY_TRANSPORT", "SMTP_HOST", "SMTP_PORT",
              "SMTP_USER", "SMTP_FROM", "SMTP_SSL"):
        os.environ.pop(k, None)


def test_email_states_original_recording_date(settings):
    db = Database(settings.db_path)
    rid = db.insert("recordings", title="Council meeting", source_type="upload", status="ready", stage="",
                    recorded_at="2024-05-03", origin="archive", original_filename="m.mp4", metadata_json="{}",
                    settings_json="{}")
    db.insert("clips", recording_id=rid, idx=0, start=10.0, end=40.0, status="ready", transcript_text="The plan is fair.",
              safety_status="clear", rights_status="unreviewed", render_rev=1)
    settings.dashboard_url = "https://pc.tail1a2b3.ts.net"
    content = notify.build_ready(settings, db, rid)
    assert "Recorded 03 May 2024" in content["html"] and "imported" in content["html"]
    assert "(03 May 2024)" in content["subject"] and "1 clip ready" in content["subject"]
    assert "https://pc.tail1a2b3.ts.net/clips/" in content["html"] and "30s" in content["html"]
    assert notify.TEMPLATE_VERSION == "2"


def test_oauth_state_survives_a_restart(settings):
    app = create_app(settings, start_worker=False)
    app.state.core.oauth_put("abc", {"platform": "tiktok", "verifier": "v"})
    core2 = create_app(settings, start_worker=False).state.core          # "restarted" engine, same database
    st = core2.oauth_pop("abc")
    assert st["platform"] == "tiktok" and st["verifier"] == "v"
    assert core2.oauth_pop("abc") is None                                 # single use


# ---- diagnostics and setup assistant -----------------------------------------------------------------

@needs_ffmpeg
def test_doctor_reports_real_state(settings, monkeypatch, capsys):
    monkeypatch.setenv("TAILSCALE_CLI", "/nonexistent/tailscale")
    items = {i["key"]: i for i in doctor.checks(settings)}
    assert items["python"]["level"] == doctor.OK and items["ffmpeg"]["level"] == doctor.OK
    assert items["font"]["level"] == doctor.OK and "Anton-Regular" in items["font"]["detail"]
    assert items["tailscale"]["level"] == doctor.WARN and "not installed" in items["tailscale"]["detail"]
    assert items["notify"]["level"] == doctor.WARN
    settings.host = "0.0.0.0"
    assert any(i["key"] == "exposure" and i["level"] == doctor.FAIL for i in doctor.checks(settings))
    settings.host = "127.0.0.1"
    assert doctor.print_report(doctor.checks(settings)) == 0
    assert "need attention" in capsys.readouterr().out


@needs_ffmpeg
def test_selftest_refuses_to_pass_with_the_stub_transcriber(stub_settings):
    rep = doctor.selftest(stub_settings, log=lambda *_: None)
    assert rep["ok"] is False and "stub" in rep["steps"][0]["detail"]


@pytest.mark.skipif(os.environ.get("CLIPENGINE_REAL_WHISPER") != "1", reason="needs the real Whisper model")
@needs_ffmpeg
def test_setup_assistant_unattended_runs_real_selftest(settings, monkeypatch):
    """REAL faster-whisper on genuine speech (bundled NASA briefing excerpt)."""
    monkeypatch.setenv("TAILSCALE_CLI", "/nonexistent/tailscale")
    said = []
    ui = setup_wizard.Console(assume_yes=True, out=said.append)
    assert setup_wizard.main(settings, assume_yes=True, ui=ui) == 0, "\n".join(said)
    state = json.loads((settings.data_dir / "setup_state.json").read_text(encoding="utf-8"))
    assert state["ok"] and state["selftest"]["ok"] and state["selftest"]["clip"]["duration"] >= 8
    assert (settings.data_dir / "selftest" / "selftest_clip.mp4").stat().st_size > 100_000
    assert db_empty(settings)                                             # the owner's library is untouched
    assert any("Setup complete" in ln for ln in said)


def db_empty(settings) -> bool:
    return Database(settings.db_path).one("SELECT COUNT(*) n FROM recordings")["n"] == 0


def test_setup_assistant_interactive_choices(settings, monkeypatch, fake_ts):
    """Owner answers are applied; model/self-test steps are replaced (covered by the real test above)."""
    monkeypatch.setattr(tailscale, "verify", lambda url, timeout=15.0: (True, "ok"))
    monkeypatch.setattr(setup_wizard, "step_model", lambda s, ui: {"ok": True, "model": "(skipped in this test)"})
    monkeypatch.setattr(setup_wizard, "step_selftest", lambda s, ui: {"ok": True, "clip_file": "x"})
    monkeypatch.setattr(doctor, "checks", lambda s: [])
    answers = iter(["y",                       # publish on the tailnet
                    "owner@example.test",      # notification address
                    ])
    said = []
    ui = setup_wizard.Console(input_fn=lambda prompt: next(answers, ""), out=said.append)
    assert setup_wizard.main(settings, ui=ui) == 0
    assert settings.dashboard_url == "https://office-pc.tail1a2b3.ts.net" and settings.dashboard_password
    assert settings.notify_to == "owner@example.test"
    out = "\n".join(said)
    assert settings.dashboard_password in out and "On the iPhone" in out
    saved = (settings.data_dir / "setup_state.json").read_text(encoding="utf-8")
    assert settings.dashboard_password not in saved                       # never stored in the report
    for k in ("DASHBOARD_URL", "DASHBOARD_PASSWORD", "NOTIFY_TO"):
        os.environ.pop(k, None)


def test_launcher_and_installer_files_are_well_formed():
    root = HERE.parent
    for name in ("Start-Clipping-Engine.cmd", "Install-Clipping-Engine.cmd", "Enable-Autostart.cmd"):
        raw = (root / name).read_bytes()
        assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b""), f"{name} must use CRLF line endings"
        assert raw.isascii(), f"{name} must be plain ASCII"
    launcher = (root / "Start-Clipping-Engine.cmd").read_text(encoding="ascii")
    for needle in ("--setup", "get_ffmpeg.py", "setup_state.json", "--supervise", "python-3.12", "--doctor"):
        assert needle in launcher
    ps = (root / "scripts" / "install.ps1").read_text(encoding="utf-8")
    assert "/XD .venv data tools" in ps and "/XF .env" in ps              # an update never overwrites owner data
    assert "LOCALAPPDATA" in ps and "RunAs" not in ps                    # no administrator elevation
    assert sys.version_info >= (3, 10)


def test_america_first_plus_and_rumble_are_never_fetched_without_written_permission(settings, monkeypatch):
    """The owner's stated sources: the verified Rumble channel and the paid AF+ archive."""
    from clipengine import ingest as ing
    monkeypatch.setattr(ing, "resolve_host", lambda h: ["93.184.216.34"])
    db = Database(settings.db_path)
    for url, word in (("https://rumble.com/c/nickjfuentes", "Rumble"),
                      ("https://rumble.com/v7i48ms-america-first-ep.-1776.html", "Rumble"),
                      ("https://americafirst.plus/", "subscription"),
                      ("https://www.americafirst.plus/live", "subscription")):
        d = ingest.classify_url(url, settings, db)
        assert d.method == "link_only" and d.code == "restricted_platform" and word in d.message, url
        sid, ok, msg = monitor.add_source(settings, db, name=url, kind="channel", url=url)
        assert not ok and "written" in msg
        assert monitor.set_enabled(settings, db, sid, True)[0] is False
    settings.allowed_url_domains = ["rumble.com", "americafirst.plus"]      # an allowlist entry is NOT enough
    assert ingest.classify_url("https://rumble.com/c/nickjfuentes", settings, db).method == "link_only"
    assert monitor.check_source_allowed(settings, db, "https://americafirst.plus/")[0] is False
    c = TestClient(create_app(settings, start_worker=False))
    r = c.post("/import/url", data={"url": "https://rumble.com/v7i48ms-america-first-ep.-1776.html", "attest": "yes"},
               follow_redirects=False)
    assert r.status_code == 303
    assert db.one("SELECT COUNT(*) n FROM pending_sources")["n"] == 1 and db.one("SELECT COUNT(*) n FROM jobs")["n"] == 0


def test_rate_limited_or_empty_channel_listing_backs_off_and_imports_nothing(settings, monkeypatch):
    """What happened on the owner's first real check: the site answered 429 / an empty listing."""
    import sys
    import types
    from clipengine import ingest as ing
    monkeypatch.setattr(ing, "resolve_host", lambda h: ["93.184.216.34"])
    settings.allowed_url_domains = ["example.org"]
    db = Database(settings.db_path)
    mon = monitor.SourceMonitor(settings, db, JobQueue(db))
    sid, ok, _ = monitor.add_source(settings, db, name="Chan", kind="channel", url="https://example.org/c/x",
                                    poll_minutes=2)
    assert ok and db.one("SELECT poll_minutes FROM sources")["poll_minutes"] == 5      # polite minimum for channels

    class FakeYDL:                                       # MOCK yt-dlp: a channel page that lists nothing
        result = {"_type": "playlist", "id": "x", "title": "x", "entries": []}

        def __init__(self, opts): self.opts = opts
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def extract_info(self, url, download=False): return dict(self.result)
    monkeypatch.setitem(sys.modules, "yt_dlp", types.SimpleNamespace(YoutubeDL=FakeYDL))
    res = mon.poll(sid)
    assert "listed no videos" in res["error"]
    assert db.one("SELECT COUNT(*) n FROM source_items")["n"] == 0       # never "one video called <channel>"
    FakeYDL.result = {"id": "v1", "title": "A single video", "webpage_url": "https://example.org/v1"}
    assert [e["key"] for e in monitor.list_channel(settings, "https://example.org/v1")] == ["v1"]

    def limited(settings_, url, limit=15):
        raise RuntimeError("\x1b[0;31mERROR:\x1b[0m x: Unable to download webpage: HTTP Error 429: Too Many Requests")
    mon.lister = limited
    db.execute("UPDATE sources SET next_check=0, consecutive_errors=0")
    before = time.time()
    res = mon.poll(sid)
    src = db.one("SELECT * FROM sources")
    assert "too frequent (HTTP 429)" in src["last_error"] and "\x1b" not in src["last_error"]
    assert src["next_check"] - before >= 1800 - 5                         # waits at least 30 minutes
    # a leftover bogus item from an earlier empty listing is removed by the next good listing
    db.insert("source_items", source_id=sid, item_key="x", title="x", url="https://example.org/c/x", state="baseline")
    mon.lister = lambda s_, u, limit=15: [{"key": "a", "title": "Ep", "url": "https://example.org/a.mp4", "live": False}]
    mon.poll(sid)
    assert [r["item_key"] for r in db.query("SELECT item_key FROM source_items")] == ["a"]


def test_rate_limit_on_download_is_not_reported_as_login_required(tmp_path, monkeypatch):
    """Regression (owner's PC, Nov 2026): yt-dlp's "Unable to download webpage: HTTP Error 429" was matched by
    the bare word "age" and reported as "requires a login or is restricted". A 429 must be a retryable error
    that waits at least 30 minutes; real login/age messages stay permanent. MOCK yt-dlp, no network."""
    import types
    from clipengine import ingest
    from clipengine.jobs import TransientError

    class DLError(Exception):
        pass

    class Cancelled(Exception):
        pass
    msg = {"text": ""}

    class FakeYDL:
        def __init__(self, opts): self.opts = opts
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def extract_info(self, url, download=False): raise DLError(msg["text"])
    fake = types.SimpleNamespace(YoutubeDL=FakeYDL, utils=types.SimpleNamespace(DownloadError=DLError,
                                                                                 DownloadCancelled=Cancelled))
    monkeypatch.setitem(sys.modules, "yt_dlp", fake)
    settings = types.SimpleNamespace(work_dir=tmp_path, max_upload_mb=100, http_user_agent="LocalClippingEngine/test")
    ctx = types.SimpleNamespace(cancelled=lambda: False, progress=lambda *a, **k: None)

    msg["text"] = ("\x1b[0;31mERROR:\x1b[0m [Rumble] v7abc: Unable to download webpage: HTTP Error 429: "
                   "Too Many Requests (caused by <HTTPError 429: Too Many Requests>)")
    with pytest.raises(TransientError) as exc:
        ingest._download_with_ytdlp(settings, "https://rumble.com/v7abc-episode.html", ctx)
    assert exc.value.retry_after >= 1800 and "\x1b" not in str(exc.value) and "too frequent" in str(exc.value)

    for text in ("ERROR: [youtube] x: Sign in to confirm your age. This video may be inappropriate",
                 "ERROR: [vimeo] x: This video is private", "ERROR: x: This video is age-restricted"):
        msg["text"] = text
        with pytest.raises(ingest.SourceError) as exc2:
            ingest._download_with_ytdlp(settings, "https://example.org/v", ctx)
        assert exc2.value.code == "access_restricted"
    msg["text"] = "ERROR: x: Unable to extract video data from the webpage; please report this issue"
    with pytest.raises(ingest.SourceError) as exc3:
        ingest._download_with_ytdlp(settings, "https://example.org/v", ctx)
    assert exc3.value.code == "extractor_error"


def test_low_disk_warns_but_does_not_stop_setup(settings, monkeypatch):
    """Regression (owner's PC): 3 GB free stopped the setup assistant at step 1. Under 20 GB is a warning
    (with plain steps to free space); only under 1 GB, too little even for the self-test, is a failure."""
    import collections
    from clipengine import doctor
    s = settings
    usage = collections.namedtuple("usage", "total used free")
    for free_gb, level in ((3, doctor.WARN), (0.5, doctor.FAIL), (40, doctor.OK)):
        monkeypatch.setattr(doctor.shutil, "disk_usage", lambda p, f=free_gb: usage(100e9, 0, f * 1e9))
        c = [c for c in doctor.checks(s) if c["key"] == "data"][0]
        assert c["level"] == level, (free_gb, c)
    monkeypatch.setattr(doctor.shutil, "disk_usage", lambda p: usage(100e9, 0, 3e9))
    assert "Temporary files" in [c for c in doctor.checks(s) if c["key"] == "data"][0]["fix"]


def test_channel_address_is_never_downloaded_as_one_video():
    """The bogus item an early empty listing created pointed at the channel itself; retrying its
    download must not fetch the whole channel. Uses yt-dlp's real extractor table (offline)."""
    import yt_dlp
    from clipengine import ingest
    assert ingest._is_collection_url(yt_dlp, "https://rumble.com/c/examplechannel")
    assert ingest._is_collection_url(yt_dlp, "https://www.youtube.com/playlist?list=PL0000000000")
    assert not ingest._is_collection_url(yt_dlp, "https://rumble.com/v7i48ms-america-first-ep.-1776.html")
    assert not ingest._is_collection_url(yt_dlp, "https://example.org/clip.mp4")
