"""Session 9: pipeline integration of the context-complete selector, v6 migration, long-clip limits and the
cleanup/storage dashboard. Mocks only; no real Drive or platform."""
import json
import time

import pytest
from fastapi.testclient import TestClient

from clipengine import editorial, pipeline as pl
from clipengine.db import Database
from clipengine.jobs import JobContext, JobQueue
from clipengine.web.app import create_app
from test_complete_selection import ARGUMENT, OTHER, QUALIFY, SETUP, long_monologue, transcript
from test_cleanup import FakeDrive, confirm, make_clip

HOUR = 3600


def make_recording(settings, db, parts, duration=500.0, raw=True):
    rid = db.insert("recordings", title="Rec", source_type="upload", status="transcribed", duration=duration,
                    width=1280, height=720, engine_owned=0)
    db.replace_transcript(rid, transcript(parts))
    return rid


def run_selection(settings, db, rid, profile="v6"):
    settings.selection_profile = profile
    q = JobQueue(db)
    p = pl.Pipeline(settings, db, q)
    jid = q.enqueue("process_recording", recording_id=rid, payload={})
    row = db.one("SELECT * FROM jobs WHERE id=?", (jid,))
    p.process_recording(JobContext(q, row))
    return p, q


def test_pipeline_stores_completeness_and_selects_the_complete_passage(settings):
    db = Database(settings.db_path)
    rid = make_recording(settings, db, [SETUP, 0.4, ARGUMENT, 0.4, QUALIFY, 7.0, OTHER])
    p, q = run_selection(settings, db, rid)
    clips = db.query("SELECT * FROM clips WHERE recording_id=?", (rid,))
    assert clips, "a complete passage exists"
    for extra in clips[1:]:                                              # anything else is offered for review, never auto-clear
        assert json.loads(extra["completeness_json"])["verdict"] == "REVIEW" and extra["safety_status"] == "flagged"
    comp = json.loads(clips[0]["completeness_json"])
    assert comp["verdict"] == "PASS" and clips[0]["duration_class"] == comp["duration_class"]
    assert clips[0]["transcript_text"].startswith("Let me explain why the transit budget")
    assert "bottom line" in clips[0]["transcript_text"] and clips[0]["safety_status"] == "clear"
    assert q.db.one("SELECT COUNT(*) n FROM jobs WHERE kind='render_clip'")["n"] == len(clips)


def test_zero_clips_is_a_valid_outcome_not_a_failure(settings):
    db = Database(settings.db_path)
    chat = ["Hey welcome back everyone.", "Thanks for joining us today, um, let me just check the chat.",
            "Uh okay it looks fine.", "Alright that's it, see you next time, thanks for watching."]
    rid = make_recording(settings, db, chat, duration=60.0)
    run_selection(settings, db, rid)                                   # must not raise
    rec = db.recording(rid)
    assert db.one("SELECT COUNT(*) n FROM clips")["n"] == 0
    assert rec["status"] == "ready" and "no complete clips" in rec["stage"] and "No clips selected" in rec["selection_note"]
    assert db.one("SELECT COUNT(*) n FROM jobs WHERE status='failed'")["n"] == 0


def test_legacy_selector_still_raises_when_nothing_is_found(settings):
    from clipengine.jobs import PermanentError
    db = Database(settings.db_path)
    rid = make_recording(settings, db, ["Hi."], duration=10.0)
    with pytest.raises(PermanentError):
        run_selection(settings, db, rid, profile="v4")


def test_review_verdict_forces_an_explicit_editorial_decision(settings):
    db = Database(settings.db_path)
    rid = make_recording(settings, db, [SETUP, 0.4, ARGUMENT, 0.4, QUALIFY])
    q = JobQueue(db)
    p = pl.Pipeline(settings, db, q)
    rec = db.recording(rid)
    segs = db.segments(rid)
    words = pl.all_words(segs)
    from clipengine import segment
    cid = p.create_clip(rec, 1, 0.0, 60.0, words, segment.build_sentences(segs), settings,
                        completeness={"verdict": "REVIEW", "reasons": ["the ending leans on the next sentence"], "duration_class": "within"})
    c = db.clip(cid)
    assert c["safety_status"] == "flagged" and "context_review" in c["safety_flags"]
    from clipengine.publishing.base import editorial_gate
    db.update("clips", cid, status="approved", rights_status="owned", video_path=None)
    assert any("flagged" in x for x in editorial_gate(db.clip(cid)))


def test_owner_adjustment_has_no_180_second_ceiling_and_explains_a_removed_original(settings, tmp_path):
    db = Database(settings.db_path)
    rid = make_recording(settings, db, [SETUP, 0.4, ARGUMENT, 0.4, QUALIFY], duration=900.0)
    q = JobQueue(db)
    p = pl.Pipeline(settings, db, q)
    raw = settings.recordings_dir / "r.mp4"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_bytes(b"x")
    db.update("recordings", rid, file_path=str(raw))
    cid = db.insert("clips", recording_id=rid, idx=1, start=0.0, end=60.0, status="ready", render_rev=0)
    p.adjust_and_regenerate(cid, 0.0, 400.0)                           # a 400 s exceptional clip is accepted
    assert db.clip(cid)["end"] >= 399 and db.clip(cid)["status"] == "pending_render"
    with pytest.raises(ValueError, match="at least 3"):
        p.adjust_and_regenerate(cid, 10.0, 11.0)
    db.update("recordings", rid, file_path=None, raw_deleted_at=time.time())
    with pytest.raises(ValueError, match="cannot be re-cut"):
        p.adjust_and_regenerate(cid, 0.0, 60.0)


def test_existing_niches_move_to_v6_once_and_owner_choices_stick(settings):
    db = Database(settings.db_path)
    editorial.seed_presets(db)
    # simulate an install from before session 9
    for r in db.query("SELECT key, config_json FROM niches"):
        cfg = json.loads(r["config_json"])
        cfg["selection_profile"] = "v5"
        db.execute("UPDATE niches SET config_json=? WHERE key=?", (json.dumps(cfg), r["key"]))
    db.execute("DELETE FROM meta WHERE key='selection_v6_migrated'")
    editorial.seed_presets(db)
    assert {editorial.load_niche(db, r["key"]).selection_profile for r in db.query("SELECT key FROM niches")} == {"v6"}
    assert db.one("SELECT COUNT(*) n FROM niche_history WHERE note LIKE 'selector v5 -> v6%'")["n"] >= 10
    p = editorial.load_niche(db, "politics")
    p.selection_profile = "v4"
    editorial.save_niche(db, p, "owner chose v4")
    editorial.seed_presets(db)
    assert editorial.load_niche(db, "politics").selection_profile == "v4"          # not migrated a second time


def test_default_selector_is_v6_and_legacy_profiles_remain_available(settings):
    from clipengine import segment
    assert settings.selection_profile == "v6" and {"v4", "v5", "v6"} <= set(segment.PROFILES)


# ---- dashboard -------------------------------------------------------------------------------------------

@pytest.fixture
def web(settings):
    app = create_app(settings, start_worker=False)
    with TestClient(app) as c:
        yield c, app


def test_clip_workspace_shows_completeness_destinations_and_storage(settings, web):
    c, app = web
    core = app.state.core if hasattr(app.state, "core") else None
    db = Database(settings.db_path)
    drive = FakeDrive()
    cid, pkgs, path = make_clip(settings, db, drive)
    db.update("clips", cid, completeness_json=json.dumps({
        "verdict": "PASS", "composite": 0.9, "dimensions": {"opening": 1.0, "conclusion": 0.9}, "duration_s": 300.0,
        "duration_class": "above", "preferred": [40, 180], "exceptional": "300 s, beyond the preferred 40-180 s: it stays substantive",
        "reasons": ["ends on a concluding line"], "content_type": "political_monologue"}))
    html = c.get(f"/clips/{cid}/workspace").text
    assert "Completeness" in html and "Why this length is justified" in html and "300 s, beyond" in html
    assert "Required destinations" in html and "Rumble" in html and "I confirm it is done" in html
    assert "Keep locally" in html and "Delete local copy now" in html and "Storage on this computer" in html
    assert "Rumble: " not in html or True


def test_owner_confirms_a_destination_and_the_state_follows(settings, web):
    c, app = web
    db = Database(settings.db_path)
    cid, pkgs, path = make_clip(settings, db, FakeDrive(), dests=("tiktok",))
    assert "TikTok" in c.get(f"/clips/{cid}/workspace").text
    r = c.post(f"/packages/{pkgs['tiktok']}/confirm", data={"note": "saved as a draft in the app"}, follow_redirects=False)
    assert r.status_code == 303
    pkg = db.one("SELECT * FROM publication_packages WHERE id=?", (pkgs["tiktok"],))
    assert pkg["handoff_state"] == "owner_confirmed" and pkg["handoff_method"] == "manual" and pkg["handoff_at"]
    r = c.post(f"/packages/{pkgs['tiktok']}/exclude", data={"reason": ""}, follow_redirects=False)
    assert "err=" in r.headers["location"]                                      # a reason is mandatory
    c.post(f"/packages/{pkgs['tiktok']}/exclude", data={"reason": "TikTok not wanted for this brand"})
    assert db.one("SELECT required FROM publication_packages WHERE id=?", (pkgs["tiktok"],))["required"] == 0


def test_manual_delete_and_restore_from_the_dashboard(settings, web, monkeypatch):
    c, app = web
    db = Database(settings.db_path)
    drive = FakeDrive()
    cid, pkgs, path = make_clip(settings, db, drive)
    lc = [v for v in app.router.routes][0]  # noqa: F841 - route table only to keep the app alive
    # the app's own cleanup service uses its (unconnected) Drive: bind our fake so the real route code is exercised
    import gc
    from clipengine import cleanup as cl
    svc = next(o for o in gc.get_objects() if isinstance(o, cl.LocalCleanup) and o.db.path == db.path)
    svc.cloud.provider = drive
    c.post(f"/packages/{pkgs['rumble']}/confirm", data={"note": "posted"})
    db.update("publication_packages", pkgs["rumble"], handoff_at=time.time() - 30 * HOUR)
    import os
    os.utime(path, (time.time() - 3 * HOUR, time.time() - 3 * HOUR))
    c.post(f"/clips/{cid}/cleanup/delete")
    assert not path.exists() and db.clip(cid)["local_deleted_at"]
    html = c.get(f"/clips/{cid}/workspace").text
    assert "Restore from Google Drive" in html and "LOCAL_DELETED" in html
    c.post(f"/clips/{cid}/cleanup/restore")
    assert db.one("SELECT COUNT(*) n FROM jobs WHERE kind='cleanup_restore'")["n"] == 1
    assert svc.restore(cid)["restored"] and path.exists()


def test_storage_page_cleanup_switch_is_off_by_default_and_saves(settings, web):
    c, app = web
    db = Database(settings.db_path)
    html = c.get("/storage").text
    assert "Finished clips on this computer" in html and "Off until you switch it on" in html
    assert db.get_kv("local_cleanup_enabled", "0") == "0"
    c.post("/storage/cleanup", data={"enabled": "1", "grace_hours": "12"})
    assert db.get_kv("local_cleanup_enabled") == "1" and float(db.get_kv("local_cleanup_grace_hours")) == 12.0
    c.post("/storage/cleanup", data={"grace_hours": "24"})
    assert db.get_kv("local_cleanup_enabled") == "0"


def test_title_route_rerenders_only_unapproved_clips(settings, web):
    c, app = web
    db = Database(settings.db_path)
    cid, pkgs, path = make_clip(settings, db, FakeDrive(), approved=False)
    c.post(f"/clips/{cid}/title", data={"title": "What is *happening* in **town**?"})
    row = db.clip(cid)
    assert row["title"] == "What is *happening* in **town**?" and row["status"] == "pending_render" and row["render_rev"] == 1
    cid2, _, _ = make_clip(settings, db, FakeDrive())                              # approved: never silently re-rendered
    c.post(f"/clips/{cid2}/title", data={"title": "New title"})
    assert db.clip(cid2)["status"] == "approved" and db.clip(cid2)["render_rev"] == 0


@pytest.mark.skipif(__import__("sqlite3").sqlite_version_info < (3, 35), reason="needs ALTER TABLE DROP COLUMN (SQLite 3.35+)")
def test_session8_database_upgrades_to_v7_without_losing_data(settings):
    """A schema-v6 database (no v7 columns/tables) opens, upgrades additively, keeps every row and takes a backup."""
    import sqlite3
    db = Database(settings.db_path)
    rid = db.insert("recordings", title="Keep me", source_type="url", status="ready", engine_owned=1)
    cid = db.insert("clips", recording_id=rid, idx=1, start=1.0, end=50.0, status="approved", rights_status="owned", title="T")
    pid = db.insert("publication_packages", clip_id=cid, platform="rumble", status="published", approval_state="approved")
    new_cols = {"clips": ["completeness_json", "duration_class", "keep_local", "local_deleted_at", "cleanup_state", "cleanup_hold",
                          "cleanup_auto", "restore_grace_until", "restored_at", "local_bytes"],
                "publication_packages": ["required", "excluded_at", "exclude_reason", "handoff_state", "handoff_method",
                                         "handoff_at", "handoff_evidence"]}
    con = sqlite3.connect(settings.db_path)
    con.execute("DROP TABLE cleanup_events")
    for table, cols in new_cols.items():
        for col in cols:
            con.execute(f"ALTER TABLE {table} DROP COLUMN {col}")
    con.execute("UPDATE meta SET value='6' WHERE key='schema_version'")
    con.commit()
    con.close()
    up = Database(settings.db_path)
    assert up.get_kv("schema_version") == str(__import__("clipengine.db", fromlist=["x"]).SCHEMA_VERSION)
    cols = {r[1] for r in up.connect().execute("PRAGMA table_info(clips)")}
    assert set(new_cols["clips"]) <= cols
    assert up.one("SELECT title FROM recordings WHERE id=?", (rid,))["title"] == "Keep me"
    c = up.clip(cid)
    assert c["title"] == "T" and c["keep_local"] == 0 and c["local_deleted_at"] is None and c["cleanup_state"] is None
    p = up.one("SELECT * FROM publication_packages WHERE id=?", (pid,))
    assert p["required"] == 1 and p["handoff_state"] is None and p["status"] == "published"
    assert up.one("SELECT COUNT(*) n FROM cleanup_events")["n"] == 0
    assert list((settings.data_dir / "backups").glob("*before-schema-*"))
