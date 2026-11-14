"""Pipeline + web integration tests.

NOTE: these use the TEST-ONLY stub transcriber so they run without a model
download. Real transcription is covered by test_whisper_real.py and by
scripts/acceptance_test.py (run in CI with the real faster-whisper model).
"""
import json
import shutil

import pytest
from fastapi.testclient import TestClient

from clipengine import transcribe
from clipengine.db import Database
from clipengine.jobs import JobQueue
from clipengine.pipeline import Pipeline
from clipengine.web.app import create_app
from conftest import needs_ffmpeg

pytestmark = needs_ffmpeg


@pytest.fixture
def processed(stub_settings, sample_video):
    db = Database(stub_settings.db_path)
    q = JobQueue(db)
    p = Pipeline(stub_settings, db, q)
    tmp = stub_settings.work_dir / "in.mp4"
    shutil.copy(sample_video, tmp)
    rid, dup, _ = p.import_upload(tmp, "sample.mp4", "Council briefing", {"clips_per_recording": 3}, "test")
    q.drain(600)
    return stub_settings, db, q, p, rid


def test_full_pipeline_creates_rendered_clips(processed):
    s, db, q, p, rid = processed
    rec = db.recording(rid)
    assert rec["status"] == "ready" and "STUB" in rec["whisper_model"]
    assert not list(s.work_dir.glob("*.wav")), "intermediate audio must be cleaned up"
    clips = db.query("SELECT * FROM clips WHERE recording_id=?", (rid,))
    assert clips and all(c["status"] == "ready" for c in clips)
    for c in clips:
        assert c["context_before"] is not None and c["transcript_text"]
        assert "Direct quotation" in c["description"] and "Source:" in c["description"]
        assert c["safety_status"] == "clear"
    assert db.search_transcripts("ferry")[0]["recording_id"] == rid
    assert db.search_transcripts('"; DROP TABLE') == []


def test_rerun_does_not_duplicate(processed):
    s, db, q, p, rid = processed
    before = db.query("SELECT id, video_path FROM clips ORDER BY id")
    q.enqueue("process_recording", recording_id=rid, dedupe_key=f"process:{rid}")
    q.drain(600)
    after = db.query("SELECT id, video_path FROM clips ORDER BY id")
    assert [tuple(r) for r in before] == [tuple(r) for r in after]


def test_regenerate_with_new_timestamps_reuses_transcript(processed, monkeypatch):
    s, db, q, p, rid = processed
    calls = []
    orig = transcribe.StubTranscriber.transcribe
    monkeypatch.setattr(transcribe.StubTranscriber, "transcribe",
                        lambda self, *a, **k: calls.append(1) or orig(self, *a, **k))
    c = db.query("SELECT * FROM clips WHERE recording_id=? ORDER BY id", (rid,))[0]
    old_path = c["video_path"]
    p.adjust_and_regenerate(c["id"], c["start"] + 3.3, c["end"] - 2.1)
    q.drain(600)
    c2 = db.clip(c["id"])
    assert calls == [], "regeneration must not re-transcribe"
    assert c2["render_rev"] == c["render_rev"] + 1 and c2["status"] == "ready"
    assert c2["video_path"] != old_path and not shutil.os.path.exists(old_path)
    assert "owner-adjusted" in c2["selection_method"]
    with pytest.raises(ValueError):
        p.adjust_and_regenerate(c["id"], 50, 10)


def test_web_review_flow_and_persistence(processed):
    s, db, q, p, rid = processed
    app = create_app(s, start_worker=False)
    with TestClient(app) as client:
        assert "Awaiting review" in client.get("/review").text
        cid = db.query("SELECT id FROM clips WHERE recording_id=? ORDER BY id", (rid,))[0]["id"]
        page = client.get(f"/clips/{cid}").text
        assert f'/clips/{cid}/video' in page and "Context before" in page
        r = client.get(f"/clips/{cid}/video", headers={"Range": "bytes=0-99"})
        assert r.status_code == 206 and len(r.content) == 100
        assert client.post(f"/clips/{cid}/approve", follow_redirects=False).status_code == 303
        d = client.get(f"/clips/{cid}/download")
        assert d.status_code == 200 and "attachment" in d.headers["content-disposition"]
        assert client.get(f"/clips/{cid}/captions.srt").status_code == 200
        assert client.get(f"/recordings/{rid}/transcript.txt").status_code == 200
        assert "ferry" in client.get("/search?q=ferry").text
        assert client.get("/settings").status_code == 200
        # Publishing is blocked until rights are recorded.
        r = client.post(f"/clips/{cid}/publish/tiktok", follow_redirects=False)
        assert "rights" in r.headers["location"].lower()
        # Cross-origin POSTs are refused.
        assert client.post(f"/clips/{cid}/reject", headers={"origin": "http://evil.example"}).status_code == 403
    # Fresh database handle = application restart.
    assert Database(s.db_path).clip(cid)["status"] == "approved"


def test_flagged_clip_requires_editorial_decision(processed):
    s, db, q, p, rid = processed
    cid = db.query("SELECT id FROM clips")[0]["id"]
    db.update("clips", cid, safety_status="flagged", safety_flags=json.dumps([{"category": "x", "match": "y"}]),
              status="approved", rights_status="owned")
    from clipengine.publishing import editorial_gate
    assert any("flagged" in g for g in editorial_gate(db.clip(cid)))
    app = create_app(s, start_worker=False)
    with TestClient(app) as client:
        client.post(f"/clips/{cid}/acknowledge", data={"note": "short"})
        assert db.clip(cid)["safety_status"] == "flagged"
        client.post(f"/clips/{cid}/acknowledge", data={"note": "Used in fact-check with presenter context."})
        assert db.clip(cid)["safety_status"] == "acknowledged"
    assert editorial_gate(db.clip(cid)) == []


def test_upload_endpoint_and_duplicate(stub_settings, sample_video):
    app = create_app(stub_settings, start_worker=False)
    with TestClient(app) as client:
        for expect_dup in (False, True):
            with open(sample_video, "rb") as f:
                r = client.post("/import/upload", files={"file": ("s.mp4", f, "video/mp4")},
                                headers={"X-Requested-With": "XMLHttpRequest"})
            assert r.status_code == 200
            assert ("already imported" in r.json()["msg"]) == expect_dup
        r = client.post("/import/upload", files={"file": ("x.exe", b"MZ", "application/octet-stream")},
                        headers={"X-Requested-With": "XMLHttpRequest"})
        assert r.status_code == 400
        r = client.post("/import/url", data={"url": "https://rumble.com/v1-x.html", "attest": "yes"},
                        headers={"X-Requested-With": "XMLHttpRequest"})
        assert r.status_code == 400 and "rumble" in r.json()["err"].lower()


def test_password_protects_dashboard(settings):
    settings.dashboard_password = "correct horse"
    app = create_app(settings, start_worker=False)
    with TestClient(app) as client:
        assert client.get("/", follow_redirects=False).headers["location"] == "/login"
        client.post("/login", data={"password": "correct horse"})
        assert client.get("/").status_code == 200
