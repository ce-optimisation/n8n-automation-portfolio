"""Session 13: the master recording switch (all platforms and creators). Nothing contacts a platform."""
import pytest
from fastapi.testclient import TestClient

from clipengine import discovery, monitor, recording_switch
from clipengine.db import Database
from clipengine.jobs import JobQueue
from clipengine.web.app import create_app


@pytest.fixture
def env(settings):
    settings.allow_private_urls = True              # TEST-ONLY: local address
    settings.allowed_url_domains = ["127.0.0.1"]
    db = Database(settings.db_path)
    q = JobQueue(db)
    mon = monitor.SourceMonitor(settings, db, q, interval=0.2)
    sid, ok, msg = monitor.add_source(settings, db, name="t", kind="channel", url="http://127.0.0.1/c/test",
                                      poll_minutes=5, authorisation="test")
    assert ok, msg
    return settings, db, q, mon, db.one("SELECT * FROM sources WHERE id=?", (sid,))


def _downloads(db):
    return db.one("SELECT COUNT(*) n FROM jobs WHERE kind='download_url'")["n"]


def test_default_is_on_and_the_choice_survives_a_restart(settings):
    db = Database(settings.db_path)
    assert recording_switch.is_enabled(db) is True            # behaviour before the switch existed
    recording_switch.set_enabled(db, False)
    assert recording_switch.is_enabled(Database(settings.db_path)) is False      # a fresh connection = a restart
    recording_switch.set_enabled(db, True)
    assert recording_switch.is_enabled(Database(settings.db_path)) is True


def test_off_stops_new_uploads_being_queued_and_on_picks_them_up(env):
    s, db, q, mon, src = env
    item = [{"key": "vid1", "title": "T", "url": "http://127.0.0.1/a.mp4", "published": None}]
    recording_switch.set_enabled(db, False)
    assert mon._handle_vods(src, item, first_poll=False) == 0
    assert _downloads(db) == 0 and db.one("SELECT 1 FROM source_items WHERE item_key='vid1'") is None   # not stored: not lost
    recording_switch.set_enabled(db, True)
    assert mon._handle_vods(src, item, first_poll=False) == 1                     # picked up once recording is back on
    assert _downloads(db) == 1


def test_baseline_on_the_first_poll_is_unchanged_when_off(env):
    s, db, q, mon, src = env
    recording_switch.set_enabled(db, False)
    old = [{"key": "old1", "title": "Old", "url": "http://127.0.0.1/o.mp4", "published": None}]
    mon._handle_vods(src, old, first_poll=True)
    row = db.one("SELECT state FROM source_items WHERE item_key='old1'")
    assert row and row["state"] == "baseline"          # back catalogue is still noted, never queued
    assert _downloads(db) == 0


def test_off_never_starts_a_new_live_capture(env):
    s, db, q, mon, src = env
    recording_switch.set_enabled(db, False)
    assert mon._handle_live(src, "live-1", "http://127.0.0.1/live", "Live", "ytdlp") is False
    assert db.one("SELECT 1 FROM source_items WHERE item_key='live-1'") is None      # retried on the next check once ON
    assert "switched OFF" in (db.one("SELECT status_detail FROM sources WHERE id=?", (src["id"],))["status_detail"] or "")


def test_off_blocks_backfill(env):
    s, db, q, mon, src = env
    recording_switch.set_enabled(db, False)
    assert "OFF" in mon.backfill(src["id"], 5).get("blocked", "")


def test_off_stops_discovery_routing_downloads(settings):
    db = Database(settings.db_path)
    sup = discovery.DiscoverySupervisor(settings, db, JobQueue(db))
    recording_switch.set_enabled(db, False)
    assert sup.route_pending() == 0


def test_off_holds_already_queued_automatic_downloads_without_using_attempts(env):
    """Session 16: jobs queued BEFORE the switch went off (found 20 Nov: jobs 95, 125, 148) must not download while OFF."""
    from clipengine import ingest
    s, db, q, mon, src = env
    q.register("download_url", lambda ctx: ingest.handle_download_url(s, db, q, ctx))
    auto = q.enqueue("download_url", max_attempts=6, payload={"url": "http://127.0.0.1/a.mp4", "source_item_id": 1})
    disc = q.enqueue("download_url", max_attempts=6, payload={"url": "http://127.0.0.1/b.mp4", "discovered_item_id": 7})
    db.update("jobs", auto, attempts=5)                               # one attempt left, as on the live engine
    recording_switch.set_enabled(db, False)
    assert q.run_one() and q.run_one()
    for jid, used in ((auto, 5), (disc, 0)):
        j = db.one("SELECT * FROM jobs WHERE id=?", (jid,))
        assert j["status"] == "queued" and j["attempts"] == used and j["run_after"] > 0 and "Recording is switched OFF" in j["message"]
    assert not q.run_one()                                             # both wait; nothing else runs


def test_off_does_not_hold_manual_imports(settings):
    from clipengine import ingest
    from clipengine.jobs import JobContext
    db = Database(settings.db_path)
    recording_switch.set_enabled(db, False)
    q = JobQueue(db)
    jid = q.enqueue("download_url", payload={"url": "ftp://not-allowed.invalid/x"})
    job = db.one("SELECT * FROM jobs WHERE id=?", (jid,))
    with pytest.raises(Exception) as e:
        ingest.handle_download_url(settings, db, q, JobContext(q, job))
    assert "Deferred" not in type(e.value).__name__                    # it went on to the normal URL checks


def test_button_toggles_the_switch_and_shows_the_state(settings):
    app = create_app(settings, start_worker=False)
    with TestClient(app) as c:
        db = Database(settings.db_path)
        html = c.get("/").text
        assert "Recording ON" in html and "Turn OFF" in html
        r = c.post("/recording/toggle", data={"enable": "0"}, headers={"referer": "http://testserver/portfolio"},
                   follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/portfolio"
        assert recording_switch.is_enabled(db) is False
        assert "Recording OFF" in c.get("/").text
        c.post("/recording/toggle", data={"enable": "1"}, headers={"referer": "http://testserver/portfolio"},
               follow_redirects=False)
        assert recording_switch.is_enabled(db) is True
        r = c.post("/recording/toggle", data={"enable": "1"}, headers={"referer": "http://testserver//evil.example"},
                   follow_redirects=False)
        assert r.headers["location"] == "/"                    # a protocol-relative path never redirects off-site


def test_off_skips_source_polling_but_guards_still_run(env, monkeypatch):
    """Session 16: abandoned timed-out lookups piled up while Recording was OFF and slowed the dashboard to minutes."""
    s, db, q, mon, src = env
    polled, supervised = [], []
    monkeypatch.setattr(mon, "_poll_with_timeout", lambda sid: polled.append(sid))
    monkeypatch.setattr(mon, "supervise", lambda: supervised.append(1))
    monkeypatch.setattr(mon, "_retry_awaiting_space", lambda: None)
    recording_switch.set_enabled(db, False)
    mon.tick()
    assert polled == [] and supervised == [1]
    recording_switch.set_enabled(db, True)
    mon.tick()
    assert polled == [src["id"]] and supervised == [1, 1]
