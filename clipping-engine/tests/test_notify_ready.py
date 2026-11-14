"""Notifications (MOCKED Microsoft Graph / SMTP — no email is ever sent by tests), Ready-to-Post,
caption styling (real Anton rendering), autostart, crash supervisor, and publishing defaults."""
import json
import shutil
import sys
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from clipengine import autostart, captions, notify, render
from clipengine.db import Database
from clipengine.jobs import JobQueue
from clipengine.pipeline import Pipeline
from clipengine.publishing import NotConfigured, TokenStore
from clipengine.web.app import create_app
from conftest import needs_ffmpeg

TO = "owner@example.test"


class GraphMock:
    """MOCK of Microsoft identity + Graph endpoints."""

    def __init__(self, send_codes=(202,)):
        self.send_codes, self.seen, self.polls = list(send_codes), [], 0

    def __call__(self, req: httpx.Request):
        self.seen.append(req)
        p = req.url.path
        if p.endswith("/devicecode"):
            return httpx.Response(200, json={"user_code": "ABCD-1234", "device_code": "DEV", "interval": 1,
                                             "verification_uri": "https://microsoft.com/devicelogin", "expires_in": 900})
        if p.endswith("/token"):
            if b"device_code" in req.content:
                self.polls += 1
                if self.polls == 1:
                    return httpx.Response(400, json={"error": "authorization_pending"})
            return httpx.Response(200, json={"access_token": f"AT{self.polls}", "refresh_token": "RT", "expires_in": 3600})
        if p.endswith("/me"):
            return httpx.Response(200, json={"mail": "sender@outlook.com"})
        if p.endswith("/me/sendMail"):
            code = self.send_codes.pop(0) if self.send_codes else 202
            return httpx.Response(code, json={} if code == 202 else {"error": {"message": "x"}})
        return httpx.Response(404)


@pytest.fixture
def processed(stub_settings, sample_video):
    s = stub_settings
    s.notify_to, s.outlook_client_id, s.dashboard_url = TO, "client-id", "https://pc.tailnet.ts.net"
    db = Database(s.db_path)
    q = JobQueue(db)
    p = Pipeline(s, db, q)
    store = TokenStore(s.secrets_dir, use_keyring=False)
    mock = GraphMock()
    n = notify.Notifier(s, db, q, store, graph_http=httpx.Client(transport=httpx.MockTransport(mock)))
    q.failure_listeners.append(lambda job: notify.on_job_failed(s, db, q, job))
    tmp = s.work_dir / "in.mp4"
    shutil.copy(sample_video, tmp)
    rid, _, _ = p.import_upload(tmp, "s.mp4", "Council briefing", {}, None)
    q.drain(600)
    return s, db, q, p, n, store, mock, rid


@needs_ffmpeg
def test_one_held_notification_per_recording_until_approved(processed):
    s, db, q, p, n, store, mock, rid = processed
    rows = db.query("SELECT * FROM notifications")
    assert len(rows) == 1 and rows[0]["status"] == "held" and rows[0]["kind"] == "recording_ready"
    body = rows[0]["body_html"]
    assert "Council briefing" in body and "awaiting your review" in body
    assert "https://pc.tailnet.ts.net/clips/" in body and "cid:clip" in body
    assert "/video" not in body and "/download" not in body and "/public/media" not in body  # no media links
    assert "sign-in" in body.lower()
    assert not any(r.url.path.endswith("/me/sendMail") for r in mock.seen)          # nothing sent
    assert notify.on_recording_ready(s, db, q, rid) is None                           # deduplicated
    assert db.one("SELECT COUNT(*) c FROM notifications")["c"] == 1


@needs_ffmpeg
def test_device_code_connect_approve_release_and_send(processed):
    s, db, q, p, n, store, mock, rid = processed
    flow = n.graph.start_device_code()
    assert flow["user_code"] == "ABCD-1234"
    assert n.graph.poll_device_code("DEV") == "pending"
    assert n.graph.poll_device_code("DEV") == "connected"
    assert n.graph.status()["connected"] and n.graph.status()["account"] == "sender@outlook.com"
    notify.approve(s, db)
    assert notify.release_held(s, db, q) == 1
    q.drain(60)
    row = db.one("SELECT * FROM notifications")
    assert row["status"] == "sent" and row["to_addr"] == TO
    send = [r for r in mock.seen if r.url.path.endswith("/me/sendMail")]
    assert len(send) == 1
    payload = json.loads(send[0].content)
    assert payload["message"]["toRecipients"][0]["emailAddress"]["address"] == TO
    att = payload["message"]["attachments"]
    assert att and all(a["isInline"] and a["contentType"] == "image/jpeg" for a in att)
    s.notify_to = "someone.else@example.test"                                          # recipient change
    assert not notify.is_approved(s, db)


@needs_ffmpeg
def test_transient_failure_retries_then_sends(processed):
    s, db, q, p, n, store, mock, rid = processed
    store.set("outlook", {"access_token": "AT", "refresh_token": "RT", "expires_at": 9e12})
    mock.send_codes = [503, 202]
    notify.approve(s, db)
    notify.release_held(s, db, q)
    q.run_one()
    assert db.one("SELECT status FROM notifications")["status"] == "queued"
    db.execute("UPDATE jobs SET run_after = 0")
    q.drain(60)
    assert db.one("SELECT status, attempts FROM notifications")["status"] == "sent"


def test_failure_before_clips_sends_no_ready_email(stub_settings, tmp_path):
    s = stub_settings
    s.notify_to = TO
    db = Database(s.db_path)
    q = JobQueue(db)
    Pipeline(s, db, q)
    q.failure_listeners.append(lambda job: notify.on_job_failed(s, db, q, job))
    rid = db.insert("recordings", title="Broken", source_type="upload", status="imported", file_path=str(tmp_path / "x.mp4"))
    q.enqueue("process_recording", recording_id=rid)
    q.drain(30)
    kinds = [r["kind"] for r in db.query("SELECT kind FROM notifications")]
    assert "recording_ready" not in kinds and kinds == ["job_failed"]
    s.notify_on_failure = False
    s.notify_enabled = False
    assert notify.create(s, db, q, kind="test", dedupe_key="x", content={"subject": "a", "html": "", "text": ""}) is None


@needs_ffmpeg
def test_smtp_transport_inline_thumbnail(processed):
    s, db, q, p, n, store, mock, rid = processed
    sent = []

    class FakeSMTP:   # MOCK SMTP server
        def __init__(self, host, port, timeout=30): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self): pass
        def login(self, u, pw): sent.append(("login", u))
        def send_message(self, m): sent.append(("msg", m))
    s.notify_transport, s.smtp_host, s.smtp_user, s.smtp_password = "smtp", "smtp.example.test", "u", "p"
    n.smtp.smtp_factory = FakeSMTP
    notify.approve(s, db)
    notify.release_held(s, db, q)
    q.drain(60)
    msg = [x[1] for x in sent if x[0] == "msg"][0]
    assert msg["To"] == TO and "image/jpeg" in msg.as_string()
    assert db.one("SELECT status FROM notifications")["status"] == "sent"


@needs_ffmpeg
def test_ready_to_post_and_manual_posting(processed):
    s, db, q, p, n, store, mock, rid = processed
    app = create_app(s, start_worker=False)
    cid = db.query("SELECT id FROM clips WHERE recording_id=?", (rid,))[0]["id"]
    with TestClient(app) as c:
        c.post(f"/clips/{cid}/approve")
        page = c.get("/ready").text
        assert "Approved but not yet cleared" in page and f"Clip #{cid}" in page
        c.post(f"/clips/{cid}/rights", data={"rights_status": "owned"})
        page = c.get("/ready").text
        assert f"/clips/{cid}/download" in page and "Copy description" in page
        assert "TikTok ✓" in page and "Instagram Reels ✓" in page
        c.post(f"/clips/{cid}/posted", data={"platform": "tiktok", "posted": "1",
                                             "post_url": "https://www.tiktok.com/@me/video/1", "note": "manual"})
        assert "posted" in c.get("/ready").text
        assert db.one("SELECT * FROM manual_posts")["post_url"].startswith("https://www.tiktok.com/")
        assert "Ready to Review" in c.get("/review").text


def test_publishing_disconnected_by_default(settings):
    app = create_app(settings, start_worker=False)
    core = app.state.core
    from clipengine import setup_status
    assert setup_status.platform_label(core, "tiktok")[0] == "Not connected"
    assert setup_status.platform_label(core, "instagram")[0] == "Not connected"
    rid = core.db.insert("recordings", title="T", source_type="upload", status="ready")
    cid = core.db.insert("clips", recording_id=rid, idx=1, start=0, end=5, status="approved", rights_status="owned",
                         video_path=__file__)
    with pytest.raises(NotConfigured):
        core.publisher.request(cid, "tiktok")
    assert core.db.one("SELECT COUNT(*) n FROM publish_attempts")["n"] == 0


# ---- captions --------------------------------------------------------------------

def phr(text, start=0.0, step=0.4):
    return [{"start": start + i * step, "end": start + i * step + 0.35, "word": " " + w, "prob": 0.9}
            for i, w in enumerate(text.split())]


def test_ass_uses_anton_sentence_case_two_lines_and_safe_position():
    words = phr("The Council said the plan costs forty two million pounds, and residents can comment until March.")
    style = captions.PRESETS["anton"]
    ph = render.build_phrases(words, 0, 20, max_chars=style.max_chars())
    doc = captions.build_ass(ph, style, label="Excerpt · Test · 0:00")
    caption_style = next(l for l in doc.splitlines() if l.startswith("Style: Caption"))
    f = caption_style.split(",")
    assert f[1] == "Anton" and f[2] == "52" and f[3] == "&H00FFFFFF" and f[5] == "&H00000000"
    assert f[7] == "0" and f[15] == "1" and float(f[16]) == 3.0 and f[17] == "0"   # no faux bold, outline, no shadow
    events = [l for l in doc.splitlines() if l.startswith("Dialogue: 0")]
    assert all("\\an5\\pos(540,1382)" in e for e in events)
    text = " ".join(e.split("}", 1)[1].replace("\\N", " ") for e in events)
    assert "The Council said the plan" in text and text != text.upper()
    assert all(e.count("\\N") <= 1 for e in events)
    assert "\\c&" not in doc and "\\t(" not in doc and "\\move" not in doc        # no colour effects/animation


def test_caption_settings_clamped_and_persisted(settings):
    db = Database(settings.db_path)
    captions.save_style(db, captions.CaptionStyle(size=500, outline=-3, position=0.99, backing=True))
    st = captions.load_style(db)
    assert (st.size, st.outline, st.position, st.backing) == (96, 0.0, 0.85, True)
    assert "BorderStyle" in captions.build_ass([], st) and ",3," in next(
        l for l in captions.build_ass([], st).splitlines() if l.startswith("Style: Caption"))


@needs_ffmpeg
def test_libass_really_uses_bundled_anton(tmp_path):
    res = captions.verify_font(tmp_path)
    assert res["ok"] and res["postscript"] == "Anton-Regular" and res["bundled_font_loaded"]
    classic = captions.verify_font(tmp_path / "c", captions.PRESETS["classic"])
    assert classic["ok"] or classic["substituted_by"]       # a substitute is reported, never hidden


@needs_ffmpeg
def test_render_burns_anton_captions(settings, sample_video, tmp_path):
    from clipengine import media
    info = media.probe(sample_video)
    res = render.render_clip(settings, sample_video, tmp_path / "out", 2.0, 9.0,
                             phr("Good evening. Tonight the council will discuss the bridge.", 2.0),
                             info["width"], info["height"], "Excerpt · Test · 0:02", style=captions.PRESETS["anton"])
    assert (res["probe"]["width"], res["probe"]["height"]) == (1080, 1920) and res["caption_style"] == "anton"
    assert "Style: Caption,Anton," in res["ass"].read_text(encoding="utf-8")
    off = render.render_clip(settings, sample_video, tmp_path / "off", 2.0, 6.0, phr("No burn.", 2.0),
                             info["width"], info["height"], None,
                             style=captions.CaptionStyle(enabled=False))
    assert off["caption_style"] == "off" and off["srt"].read_text(encoding="utf-8").strip()


@needs_ffmpeg
def test_caption_settings_page_and_preview(settings):
    app = create_app(settings, start_worker=False)
    with TestClient(app) as c:
        assert "Captions" in c.get("/settings").text
        c.post("/settings/captions", data={"preset": "anton", "size": "48", "outline": "2.5", "position": "0.7",
                                           "enabled": "1"})
        assert captions.load_style(app.state.core.db).size == 48
        r = c.get("/settings/captions/preview.png")
        assert r.status_code == 200 and r.headers["content-type"] == "image/png"


# ---- autostart & supervisor ----------------------------------------------------------

def test_autostart_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPENGINE_STARTUP_DIR", str(tmp_path))
    p = autostart.install()
    body = p.read_bytes().decode()
    assert autostart.is_installed() and "--supervise" in body and "--no-browser" in body and "\r\n" in body
    autostart.remove()
    assert not autostart.is_installed()


def test_supervisor_restarts_after_crash(tmp_path):
    from clipengine.__main__ import supervise
    counter = tmp_path / "n"
    script = tmp_path / "child.py"
    script.write_text("import pathlib,sys\np=pathlib.Path(sys.argv[1])\nn=int(p.read_text()) if p.exists() else 0\n"
                      "p.write_text(str(n+1))\nsys.exit(1 if n == 0 else 0)\n")
    rc = supervise(["--no-browser"], sleep=lambda s: None,
                   cmd_factory=lambda args: [sys.executable, str(script), str(counter)])
    assert rc == 0 and counter.read_text() == "2"
