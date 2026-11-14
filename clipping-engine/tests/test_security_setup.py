"""Dashboard security controls, setup checklist honesty, migrations, public media links."""
import sqlite3
import time

from fastapi.testclient import TestClient

from clipengine.db import Database
from clipengine.web.app import create_app


def test_no_password_only_answers_localhost(settings):
    with TestClient(create_app(settings, start_worker=False), base_url="http://127.0.0.1:8765") as c:
        assert c.get("/healthz").status_code == 200
        assert c.get("/", headers={"host": "evil.example"}).status_code == 403   # DNS rebinding / exposure
        assert c.get("/", headers={"host": "192.168.1.20:8765"}).status_code == 403


def test_login_rate_limited_and_cookie(settings):
    settings.dashboard_password = "correct horse battery"
    with TestClient(create_app(settings, start_worker=False), base_url="https://pc.tailnet.ts.net") as c:
        assert c.get("/", follow_redirects=False).status_code == 303
        for _ in range(5):
            c.post("/login", data={"password": "wrong"}, follow_redirects=False)
        assert c.post("/login", data={"password": "correct horse battery"}).status_code == 429
    with TestClient(create_app(settings, start_worker=False), base_url="https://pc.tailnet.ts.net") as c:
        r = c.post("/login", data={"password": "correct horse battery"}, follow_redirects=False)
        assert "secure" in r.headers["set-cookie"].lower() and "httponly" in r.headers["set-cookie"].lower()


def test_setup_checklist_is_honest(settings):
    app = create_app(settings, start_worker=False)
    with TestClient(app) as c:
        page = c.get("/settings").text
        assert "Setup checklist" in page and "Owner action required" in page
        items = {i["key"]: i for i in __import__("clipengine.setup_status", fromlist=["x"]).checklist(app.state.core)}
        assert items["tiktok_oauth"]["status"] != "Connected" and items["ig_oauth"]["status"] != "Connected"
        assert items["transcription"]["status"] in ("Untested", "Failed")
        assert items["tiktok_ready"]["status"] in ("Not configured", "Owner action required")
        settings.transcriber_backend = "stub"
        items = {i["key"]: i for i in __import__("clipengine.setup_status", fromlist=["x"]).checklist(app.state.core)}
        assert items["whisper"]["status"] == "Failed"   # stub is never shown as ready


def test_public_media_links_are_tokenised_and_expire(settings, sample_video):
    app = create_app(settings, start_worker=False)
    db = app.state.core.db
    rid = db.insert("recordings", title="T", source_type="upload", status="ready")
    cid = db.insert("clips", recording_id=rid, idx=1, start=0, end=5, status="approved", video_path=str(sample_video))
    db.insert("public_media_tokens", token="good", clip_id=cid, expires_at=time.time() + 60)
    db.insert("public_media_tokens", token="old", clip_id=cid, expires_at=time.time() - 1)
    settings.dashboard_password = "pw-pw-pw-pw"
    with TestClient(app, base_url="https://pc.tailnet.ts.net") as c:
        assert c.get("/public/media/good.mp4").status_code == 200
        assert c.get("/public/media/old.mp4").status_code == 404
        assert c.get("/public/media/nope.mp4").status_code == 404
        assert c.get(f"/clips/{cid}/video", follow_redirects=False).status_code == 303   # still needs login


def test_migrates_database_from_first_release(tmp_path):
    """Recreate the v1 shape (publish_attempts without stage columns, recordings without
    selection_note) and check that opening it upgrades in place without losing rows."""
    path = tmp_path / "old.sqlite3"
    Database(path)
    con = sqlite3.connect(path)
    con.executescript("""DROP TABLE publish_attempts;
        CREATE TABLE publish_attempts (id INTEGER PRIMARY KEY, clip_id INTEGER NOT NULL,
        platform TEXT NOT NULL, status TEXT NOT NULL, remote_id TEXT, detail TEXT, created_at REAL NOT NULL,
        updated_at REAL NOT NULL);
        ALTER TABLE recordings DROP COLUMN selection_note;
        UPDATE meta SET value='1' WHERE key='schema_version';
        INSERT INTO publish_attempts VALUES (1, 1, 'tiktok', 'confirmed', 'p', 'd', 0, 0);""")
    con.commit()
    con.close()
    db = Database(path)
    cols = {r["name"] for r in db.query("PRAGMA table_info(publish_attempts)")}
    assert {"stage", "container_id", "job_id", "confirmed_at"} <= cols
    assert "selection_note" in {r["name"] for r in db.query("PRAGMA table_info(recordings)")}
    assert db.one("SELECT status FROM publish_attempts WHERE id=1")["status"] == "confirmed"
    from clipengine.db import SCHEMA_VERSION
    assert db.get_kv("schema_version") == str(SCHEMA_VERSION)
    assert {"sources", "source_items", "notifications", "manual_posts"} <= {
        r["name"] for r in db.query("SELECT name FROM sqlite_master WHERE type='table'")}


def test_port_helpers_distinguish_our_engine_from_other_programs():
    import socket
    from clipengine.__main__ import is_our_engine, port_free
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    port = s.getsockname()[1]
    try:
        assert not port_free("127.0.0.1", port)
        assert not is_our_engine(port)      # something else is listening there
    finally:
        s.close()
    assert port_free("127.0.0.1", port)
