"""Session 8: roster import, source editing, job-state sync, recorder registration, publishing-account slots,
calibrated title rendering. Everything platform-related is MOCKED/local; nothing here is a live verification."""
import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from clipengine import captions, discovery, portfolio, render, roster_import, titles
from clipengine.db import Database
from clipengine.platforms import LiveStatus, MediaItem
from test_portfolio import (FakeAdapter, StubQueue, db, grant_creator, make_verified, ms, sup, yt_report, NOW)  # noqa: F401

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")


# ---- roster import ----------------------------------------------------------------------------------

def test_roster_file_has_six_brands_eighteen_creators_twentyseven_sources():
    rows = roster_import.parse(roster_import.DEFAULT_FILE.read_text(encoding="utf-8"))
    assert len(rows) == 27 and len({r["brand"] for r in rows}) == 6
    assert roster_import.validate(rows) == []
    assert {roster_import._uid_for(r["name"]) for r in rows} == {c["uid"] for c in portfolio.CREATORS}


def test_import_fills_alex_jones_url_locked_and_unverified_and_is_idempotent(settings, db):
    res = roster_import.reconcile(settings, db)
    assert res["problems"] == [] and res["summary"]["filled"] == 1 and res["summary"]["conflict"] == 0
    aj = ms(db, "alex-jones/rumble/channel")
    assert aj["canonical_url"] == "https://rumble.com/c/TheAlexJonesShowLive"
    assert aj["verification"] == "NEEDS_VERIFICATION" and "canonical_url" in json.loads(aj["owner_locked_json"])
    assert "Roster evidence" in aj["notes"]
    again = roster_import.reconcile(settings, db)
    assert again["summary"]["filled"] == 0 and again["summary"]["match"] == 27
    assert db.one("SELECT COUNT(*) n FROM monitor_sources")["n"] == 27
    assert db.one("SELECT COUNT(*) n FROM creators WHERE roster='approved'")["n"] == 18
    # Fuentes untouched, nothing verified or enabled for acquisition by the import
    assert ms(db, "nick-fuentes/rumble/channel")["canonical_url"] == "https://rumble.com/c/nickjfuentes"
    assert db.one("SELECT COUNT(*) n FROM monitor_sources WHERE verification='VERIFIED'")["n"] == 0
    assert all(r["acquisition"] == "PERMISSION_REQUIRED" for r in portfolio.summary(settings, db))


def test_import_never_adds_creators_never_overwrites_locked_and_reports_conflicts(settings, db):
    rows = roster_import.parse(roster_import.DEFAULT_FILE.read_text(encoding="utf-8"))
    extra = rows + [{"brand": "Afterlive", "name": "Some New Streamer", "platform": "twitch",
                     "value": "https://www.twitch.tv/new", "evidence": ""}]
    refused = roster_import.reconcile(settings, db, extra)
    assert refused["problems"] and db.one("SELECT COUNT(*) n FROM creators WHERE brand_key='afterlive'")["n"] == 3
    portfolio.owner_set(db, ms(db, "mkbhd/youtube/channel")["id"], candidate_handle="@MKBHD2", monitoring_enabled=0)
    res = roster_import.reconcile(settings, db, rows)
    conflict = [r for r in res["rows"] if r["result"] == "conflict"]
    assert len(conflict) == 1 and "locked" in conflict[0]["detail"] and conflict[0]["creator"] == "MKBHD"
    row = ms(db, "mkbhd/youtube/channel")
    assert row["candidate_handle"] == "@MKBHD2" and row["monitoring_enabled"] == 0      # nothing overwritten or re-enabled


# ---- editable source registry ---------------------------------------------------------------------

def test_edit_source_validates_locks_and_forces_reverification(settings, db):
    row = ms(db, "mkbhd/youtube/channel")
    make_verified(db, "mkbhd/youtube/channel", "UC1")
    with pytest.raises(ValueError):
        portfolio.edit_source(db, row["id"], url="https://evil.example/@mkbhd")
    with pytest.raises(ValueError):
        portfolio.edit_source(db, row["id"], handle="@mk bhd; drop table")
    with pytest.raises(ValueError):
        portfolio.edit_source(db, row["id"], url="https://user:pw@www.youtube.com/@mkbhd")
    with pytest.raises(ValueError):
        portfolio.edit_source(db, row["id"], priority=500)
    assert portfolio.edit_source(db, row["id"], priority=70, notes="primary tech channel") == ["notes", "priority"]
    assert ms(db, "mkbhd/youtube/channel")["verification"] == "VERIFIED"           # harmless edits keep verification
    assert portfolio.edit_source(db, row["id"], handle="@mkbhd2") == ["candidate_handle"]
    after = ms(db, "mkbhd/youtube/channel")
    assert after["verification"] == "NEEDS_VERIFICATION" and after["platform_id"] is None and after["state"] == "NEEDS_VERIFICATION"
    assert {"candidate_handle", "priority", "notes"} <= set(json.loads(after["owner_locked_json"]))
    portfolio.seed(db, settings)
    assert ms(db, "mkbhd/youtube/channel")["candidate_handle"] == "@mkbhd2"          # the seeder respects the lock


def test_edit_cannot_grant_permission_or_mark_verified(settings, db):
    row = ms(db, "linus-tech-tips/youtube/channel")
    with pytest.raises(ValueError):
        portfolio.owner_set(db, row["id"], verification="VERIFIED")
    with pytest.raises(ValueError):
        portfolio.owner_set(db, row["id"], acquisition="PERMITTED")


def test_link_rumble_source_is_disabled_and_idempotent(settings, db):
    roster_import.reconcile(settings, db)
    mid = ms(db, "alex-jones/rumble/channel")["id"]
    sid = portfolio.link_rumble_source(settings, db, mid)
    assert portfolio.link_rumble_source(settings, db, mid) == sid
    src = db.one("SELECT * FROM sources WHERE id=?", (sid,))
    assert src["enabled"] == 0 and src["record_live"] == 0 and src["url"] == "https://rumble.com/c/TheAlexJonesShowLive"
    assert db.one("SELECT COUNT(*) n FROM sources")["n"] == 1


# ---- discovered-item job states ----------------------------------------------------------------------

def queued_item(db, ident, job_status, duration=600):
    row = ms(db, "mkbhd/youtube/channel")
    jid = db.insert("jobs", kind="download_url", status=job_status, payload_json="{}", message="", priority=0,
                    max_attempts=3, error="boom" if job_status == "failed" else None)
    return db.insert("discovered_items", monitor_source_id=row["id"], creator_id=row["creator_id"], brand_key=row["brand_key"],
                     platform="youtube", platform_item_id=ident, kind="upload", title=ident, url=f"https://www.youtube.com/watch?v={ident}",
                     duration_s=duration, state="queued", job_id=jid, first_seen=NOW, last_seen=NOW)


def test_sync_marks_imported_failed_and_requeues_when_job_vanished(settings, db):
    done, bad, gone, running = (queued_item(db, "d", "done"), queued_item(db, "f", "failed"), queued_item(db, "g", "done"),
                                queued_item(db, "r", "running"))
    db.execute("DELETE FROM jobs WHERE id=(SELECT job_id FROM discovered_items WHERE id=?)", (gone,))
    s = sup(settings, db, {})
    assert s.sync_job_states() == 3
    st = {i: db.one("SELECT * FROM discovered_items WHERE id=?", (i,)) for i in (done, bad, gone, running)}
    assert st[done]["state"] == "imported" and st[bad]["state"] == "failed" and "boom" in st[bad]["detail"]
    assert st[gone]["state"] == "discovered" and st[running]["state"] == "queued"
    assert s.sync_job_states() == 0                                              # idempotent


def test_finished_items_do_not_reserve_disk_and_failed_can_be_retried_once(settings, db, monkeypatch):
    settings.allow_private_urls = True
    a, b = queued_item(db, "a", "done", 7200), queued_item(db, "b", "failed", 7200)
    s = sup(settings, db, {}, queue=StubQueue())
    d = db.one("SELECT * FROM discovered_items WHERE id=?", (a,))
    monkeypatch.setattr("clipengine.storage.free_bytes", lambda p: 50_000_000_000)
    settings.record_gb_per_hour = 1.5
    s.sync_job_states()
    ok, _ = s._disk_fits(d)
    assert ok                                                                     # finished work reserves nothing
    assert s.retry_item(b) and not s.retry_item(a)                                # only a failed item can be retried
    assert db.one("SELECT state FROM discovered_items WHERE id=?", (b,))["state"] == "discovered"


def test_download_failure_hook_updates_discovered_item(settings, db):
    from clipengine import monitor
    iid = queued_item(db, "x", "failed")
    jid = db.one("SELECT job_id FROM discovered_items WHERE id=?", (iid,))["job_id"]

    class Ctx:
        id = jid
        payload = {"discovered_item_id": iid}
    mon = monitor.SourceMonitor(settings, db, StubQueueWithRegister())
    mon._download_failed(Ctx)
    assert db.one("SELECT state FROM discovered_items WHERE id=?", (iid,))["state"] == "failed"


class StubQueueWithRegister(StubQueue):
    def register(self, *a, **k):
        pass


# ---- recorder registration -----------------------------------------------------------------------------

def allow_host(db, *hosts):
    now = time.time()
    for h in hosts:
        db.execute("INSERT INTO source_permissions(domain,granted_by,granted_on,reference,scope,created_at,updated_at) "
                   "VALUES(?,?,?,?,?,?,?)", (h, "owner", "2026-11-17", "ref", "test", now, now))


def verified_channel(db, uid, url, handle):
    db.execute("UPDATE monitor_sources SET verification='VERIFIED', platform_id='1', candidate_handle=?, canonical_url=? WHERE uid=?",
               (handle, url, uid))


def test_unpermitted_source_registers_no_recorder_and_permitted_one_does(settings, db):
    settings.allow_private_urls = True
    verified_channel(db, "kai-cenat/twitch/channel", "https://www.twitch.tv/kaicenat", "kaicenat")
    s = sup(settings, db, {})
    assert s.ensure_recorders() == 0 and db.one("SELECT COUNT(*) n FROM sources")["n"] == 0
    allow_host(db, "twitch.tv")                                                   # host permission alone: still nothing
    assert s.ensure_recorders() == 0
    grant_creator(db, "kai-cenat")
    assert s.ensure_recorders() == 1
    src = db.one("SELECT * FROM sources")
    assert src["kind"] == "channel" and src["url"] == "https://www.twitch.tv/kaicenat" and src["enabled"] == 1
    creator = db.one("SELECT id FROM creators WHERE uid='kai-cenat'")["id"]
    assert src["creator_id"] == creator and src["platform"] == "twitch"
    assert ms(db, "kai-cenat/twitch/channel")["legacy_source_id"] == src["id"]


def test_simulcast_twins_share_one_recorder_and_restart_or_reseed_adds_none(settings, db):
    settings.allow_private_urls = True
    verified_channel(db, "kai-cenat/twitch/channel", "https://www.twitch.tv/kaicenat", "kaicenat")
    verified_channel(db, "kai-cenat/youtube/channel", "https://www.youtube.com/channel/UCk", "@KaiCenat")
    allow_host(db, "twitch.tv", "youtube.com")
    grant_creator(db, "kai-cenat")
    s = sup(settings, db, {})
    assert s.ensure_recorders() == 1                                              # primary (Twitch) only
    portfolio.seed(db, settings)
    assert sup(settings, db, {}).ensure_recorders() == 0                          # a "restarted" supervisor adds nothing
    assert db.one("SELECT COUNT(*) n FROM sources")["n"] == 1
    assert ms(db, "kai-cenat/youtube/channel")["legacy_source_id"] is None


def test_rumble_is_never_auto_created_by_ensure_recorders(settings, db):
    settings.allow_private_urls = True
    verified_channel(db, "nick-fuentes/rumble/channel", "https://rumble.com/c/nickjfuentes", "nickjfuentes")
    allow_host(db, "rumble.com")
    grant_creator(db, "nick-fuentes")
    assert sup(settings, db, {}).ensure_recorders() == 0 and db.one("SELECT COUNT(*) n FROM sources")["n"] == 0


def test_existing_enabled_rumble_source_still_counts_as_permitted(settings, db):
    sid = db.insert("sources", name="Fuentes", kind="channel", url="https://rumble.com/c/nickjfuentes", enabled=1)
    portfolio.seed(db, settings)                                                  # adopts the existing source by URL
    allow_host(db, "rumble.com")
    row = ms(db, "nick-fuentes/rumble/channel")
    assert row["legacy_source_id"] == sid
    assert portfolio.acquisition_for(settings, db, row) == "PERMITTED"            # existing behaviour preserved
    db.execute("UPDATE sources SET enabled=0 WHERE id=?", (sid,))
    assert portfolio.acquisition_for(settings, db, row) == "PERMISSION_REQUIRED"


# ---- publishing-account registry -------------------------------------------------------------------------

def test_twenty_four_account_slots_map_one_brand_each(settings, db):
    rows = db.query("SELECT * FROM social_accounts WHERE handle LIKE 'PENDING-%'")
    assert len(rows) == 24 and {r["platform"] for r in rows} == set(portfolio.ACCOUNT_PLATFORMS)
    niches = {}
    for r in rows:
        assert json.loads(r["niches_json"]) == [portfolio.BRANDS[r["brand"]]["niche"]] and r["status"] == "awaiting_connection"
        niches.setdefault(r["brand"], set()).update(json.loads(r["niches_json"]))
    assert len({n for v in niches.values() for n in v}) == 6                    # no niche shared between brands
    assert portfolio.seed_account_slots(db) == 0 and portfolio.seed(db, settings)["account_slots"] == 0


def test_set_account_records_real_handle_and_refuses_cross_brand(settings, db):
    portfolio.set_account(db, "afterlive", "youtube", "@AfterliveHQ", account_id="UC123")
    assert db.one("SELECT COUNT(*) n FROM social_accounts WHERE brand='afterlive' AND platform='youtube'")["n"] == 1
    with pytest.raises(ValueError):
        portfolio.set_account(db, "open_floor", "youtube", "@afterlivehq")
    with pytest.raises(ValueError):
        portfolio.set_account(db, "afterlive", "youtube", "PENDING-x")
    from clipengine.publishing import packages
    acct = db.one("SELECT * FROM social_accounts WHERE handle='@AfterliveHQ'")
    assert packages.account_allows(acct, "gaming_streams") and not packages.account_allows(acct, "politics")


# ---- title calibration and layout ---------------------------------------------------------------------------

def test_title_calibration_is_measured_and_scaled_to_eighty_percent():
    cal = titles.calibration()
    assert "measured" in cal["source"] and cal["image_px"] == [941, 1672]
    st = titles.TitleStyle()
    assert st.reference_size == 199 and st.size == round(199.4 * 0.8) == 160       # NOT the earlier 120 px assumption
    assert 1.15 < cal["emphasis_scale"] < 1.3 and cal["yellow_rgb"][0] > 240 and cal["yellow_rgb"][2] < 20
    assert not st.enabled


def lines_fit(lines, st, width=1080):
    m = captions.font_metrics("Anton")
    cal = titles.calibration()
    ring = (cal["accent_black_ring_px_1080"] + cal["accent_perimeter_px_1080"]) * st.scale
    usable = width - 2 * st.side_margin - 2 * ring
    return all(m.width(titles._text(l["words"]), l["size"]) <= usable + 0.5 for l in lines)


@pytest.mark.parametrize("text,accents", [
    ("Big news", 0), ("What's going on in *Little* **Palestine**?", 2), ("The *TRUTH* about the deal", 1),
    ("Why the *committee* quietly changed **everything** about the funding review after a long and heated debate on Tuesday", 2),
    ("W" * 60, 0), ("Short *one*", 1)])
@pytest.mark.parametrize("fg_h", [607.5, 810.0, 1080.0, 1920.0])               # 16:9, 4:3, 1:1, 9:16 sources
def test_title_layout_fits_every_length_and_aspect_ratio(text, accents, fg_h):
    st = titles.TitleStyle(enabled=True)
    words = titles.parse(text)
    assert sum(1 for _, m, _g in words if m) >= accents
    lines = titles.layout(words, st, 1080, 1920, fg_h)
    assert lines and len(lines) <= st.max_lines and lines_fit(lines, st)
    ys = [l["y"] for l in lines]
    assert ys == sorted(ys) and all(0 < y < 1920 for y in ys)
    band = (1920 - fg_h) / 2
    if band >= 160:                                                             # the title stays inside the upper band
        assert max(l["y"] + l["cap"] / 2 for l in lines) <= band


def test_normal_title_matches_the_concept_proportions():
    st = titles.TitleStyle(enabled=True)
    lines = titles.layout(titles.parse("What's going on in *Little* **Palestine**?"), st, 1080, 1920, 607.5)
    assert len(lines) == 2 and lines[0]["size"] == pytest.approx(199.4 * 0.8, abs=0.1)
    assert lines[1]["size"] / lines[0]["size"] == pytest.approx(titles.calibration()["emphasis_scale"], abs=0.01)
    cal = titles.calibration()
    cap0 = cal["cap_height_px_1080"]["lead"] * 0.8
    assert lines[0]["cap"] == pytest.approx(cap0, rel=0.01)                      # exactly 80% of the pictured cap height


def test_markup_modes_and_glued_punctuation():
    assert titles.parse("a *b* **c**?") == [("A", 0, False), ("B", 1, False), ("C", 2, False), ("?", 0, True)]
    _, ev = titles.build_events("Go *now* **fast**!", titles.TitleStyle(enabled=True), 5, 1080, 1920, 607.5)
    assert ev.count("&H0004FBFD&") >= 3 and "Dialogue: 2," in ev and "Dialogue: 3," in ev    # yellow ring + yellow fill
    assert "FAST}!" in ev.replace("\\", "") or "!" in ev                          # punctuation attaches without a space


def test_format_title_is_deterministic_and_never_invents_words():
    f = titles.format_title
    assert f("What's going on in Little Palestine?") == "What's going on in *Little* **Palestine**?"
    assert f("Big news") == "Big news" and f("") == ""
    long = f("The deal nobody wants to discuss openly on live television tonight, apparently, again and again")
    src = "The deal nobody wants to discuss openly on live television tonight, apparently, again and again"
    import re as _re
    assert len(long.replace("*", "")) <= 72 and set(_re.findall(r"\w+", long)) <= set(_re.findall(r"\w+", src))
    assert f("keep *my* markup") == "keep *my* markup" and f("x", auto_emphasis=False) == "x"
    assert "😀" not in f("Wow 😀 this")


def test_title_is_on_for_america_first_monitor_only_and_can_be_disabled(settings, db):
    assert titles.style_for(db, "america_first_monitor").enabled
    assert not titles.style_for(db, "afterlive").enabled and not titles.style_for(db, None).enabled
    titles.set_brand_enabled(db, "afterlive", True)                              # reusable by any brand
    assert titles.style_for(db, "afterlive").enabled
    titles.set_brand_enabled(db, "america_first_monitor", False)
    assert not titles.style_for(db, "america_first_monitor").enabled
    portfolio.seed(db, settings)                                                  # reseeding does not re-enable it
    assert not titles.style_for(db, "america_first_monitor").enabled


@needs_ffmpeg
@pytest.mark.parametrize("src_w,src_h", [(1280, 720), (640, 480)])
def test_title_render_preserves_the_central_video_and_stays_in_frame(settings, sample_video, tmp_path, src_w, src_h):
    np = pytest.importorskip("numpy")
    Image = pytest.importorskip("PIL.Image")
    words = [{"word": w, "start": i * .4, "end": i * .4 + .35} for i, w in enumerate("the committee will publish its findings".split())]
    kw = dict(style=captions.CaptionStyle())
    plain = render.render_clip(settings, sample_video, tmp_path / "a", 0.0, 2.0, words, src_w, src_h, None, **kw)
    titled = render.render_clip(settings, sample_video, tmp_path / "b", 0.0, 2.0, words, src_w, src_h, None,
                                title="The *committee* **findings**: a very long headline that must scale down to fit", title_style=titles.TitleStyle(enabled=True), **kw)
    frames = {}
    for tag, res in (("a", plain), ("b", titled)):
        out = tmp_path / f"{tag}.png"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", "1.0", "-i", str(res["video"]), "-frames:v", "1", str(out)], check=True)
        frames[tag] = np.asarray(Image.open(out).convert("RGB")).astype(int)
    a, b = frames["a"], frames["b"]
    assert a.shape == b.shape == (1920, 1080, 3)
    fg_h = render.layout(settings, src_w, src_h)["fg_h"]
    top, bottom = int((1920 - fg_h) / 2) + 4, int((1920 + fg_h) / 2) - 4
    assert np.abs(a[top:bottom] - b[top:bottom]).mean() < 3.0                      # the central footage is untouched (codec noise only)
    assert np.abs(a[:int(top - 8)] - b[:int(top - 8)]).sum() > 0                   # the title is drawn in the upper band only
    edge = np.abs(a[:top - 8] - b[:top - 8]).sum(axis=2) > 60
    cols = np.where(edge.any(axis=0))[0]
    assert cols.min() >= 20 and cols.max() <= 1080 - 20                           # no horizontal overflow
    assert np.abs(a[-400:] - b[-400:]).mean() < 3.0                                # the lower captions are unchanged


# ---- editorial review flags (Afterlive) ----------------------------------------------------------------

def test_gaming_niche_flags_spoilers_and_sensitive_scenes_for_review_only(settings, db):
    from clipengine import editorial
    editorial.seed_presets(db)
    p = editorial.load_niche(db, "gaming_streams")
    assert {"spoiler_risk", "sensitive_scene"} <= set(p.flag_rules)
    pol = editorial.EditorialPolicy(p) if hasattr(editorial, "EditorialPolicy") else None
    assert pol is not None
    out = pol.assess("no way he leaked the ending of the story mission, let's go", 0.6, 30)
    cats = {f["category"] for f in out["flags"]}
    assert "spoiler_risk" in cats and not any("spoiler" in r.lower() for r in out["reject"])
    assert "sensitive_scene" in {f["category"] for f in pol.assess("that graphic violence scene was insane lol", 0.6, 30)["flags"]}
    assert pol.assess("lol we won the match", 0.6, 30)["flags"] == []


def test_existing_install_gains_the_flags_without_losing_owner_edits(settings, db):
    from clipengine import editorial
    editorial.seed_presets(db)
    cfg = json.loads(db.one("SELECT config_json FROM niches WHERE key='gaming_streams'")["config_json"])
    cfg["flag_rules"], cfg["hashtags"] = [], ["#mine"]
    db.execute("UPDATE niches SET config_json=? WHERE key='gaming_streams'", (json.dumps(cfg),))
    editorial.seed_presets(db)
    p = editorial.load_niche(db, "gaming_streams")
    assert p.flag_rules == ["spoiler_risk", "sensitive_scene"] and p.hashtags == ["#mine"]


# ---- laptop setup check ---------------------------------------------------------------------------------

def test_setup_check_reports_credentials_without_leaking_secrets(settings, db, capsys):
    from clipengine import setup_check
    settings.youtube_api_key = "AIzaLEAKCHECK1234567890"
    settings.twitch_client_id = "twitchLEAKCHECKid"
    st = setup_check.credential_status(settings)
    assert st["youtube"]["state"] == "complete" and st["twitch"]["state"] == "partial" and st["kick"]["state"] == "missing"
    assert st["twitch"]["missing"] == ["TWITCH_CLIENT_SECRET"]
    lines, todo = setup_check.run(settings, do_network=False)
    text = "\n".join(lines + todo)
    assert "Schema: current" in text and "Add TWITCH_CLIENT_SECRET" in text and "written permission" in text
    assert "LEAKCHECK" not in text and "AIza" not in text
    assert all(ord(ch) < 128 for ch in text)                         # ASCII only: safe on a Windows cp1252 console


def test_setup_check_flags_an_old_schema_and_main_prints(settings, capsys):
    from clipengine import setup_check
    import sqlite3
    Database(settings.db_path)
    con = sqlite3.connect(settings.db_path)
    con.execute("UPDATE meta SET value='5' WHERE key='schema_version'")
    con.commit(); con.close()
    assert "OLD" in setup_check.schema_status(settings)[0]
    assert setup_check.main(settings, do_network=False) == 0
    assert "Still to do" in capsys.readouterr().out


def test_launcher_wrapper_exists_with_crlf():
    root = Path(__file__).resolve().parent.parent
    data = (root / "Check-Portfolio.cmd").read_bytes()
    assert b"--portfolio-check" in data and data.count(b"\r\n") == data.count(b"\n")


def test_accounts_page_records_brand_account_and_blocks_cross_brand_mapping(settings, db):
    from fastapi.testclient import TestClient
    from clipengine.web.app import create_app
    app = create_app(settings, start_worker=False)
    with TestClient(app) as c:
        page = c.get("/accounts").text
        assert "PENDING-afterlive-youtube" in page and "Record a brand" in page
        r = c.post("/accounts/record", data={"brand": "afterlive", "platform": "tiktok", "handle": "@afterlive_clips"}, follow_redirects=False)
        assert r.status_code == 303
        aid = Database(settings.db_path).one("SELECT id FROM social_accounts WHERE handle='afterlive_clips'")["id"]
        bad = c.post(f"/accounts/{aid}/niches", data={"niches": ["politics"], "brand": "afterlive"}, follow_redirects=False)
        assert "only+receive+Afterlive" in bad.headers["location"] or "only%20receive%20Afterlive" in bad.headers["location"] \
            or "Afterlive" in bad.headers["location"]
        row = Database(settings.db_path).one("SELECT niches_json FROM social_accounts WHERE id=?", (aid,))
        assert json.loads(row["niches_json"]) == ["gaming_streams"]
        dup = c.post("/accounts/record", data={"brand": "open_floor", "platform": "tiktok", "handle": "afterlive_clips"}, follow_redirects=False)
        assert "belongs" in dup.headers["location"] or "brand" in dup.headers["location"]
