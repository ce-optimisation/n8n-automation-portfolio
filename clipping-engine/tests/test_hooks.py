"""Session 10: automatic hook titles. Deterministic generation is checked against the clip's own words; the real render
test draws generated titles through the unchanged approved title renderer."""
import json
import subprocess

import pytest
from fastapi.testclient import TestClient

from clipengine import captions, hooks, render, titles
from clipengine import pipeline as pl
from clipengine.db import Database
from clipengine.jobs import JobContext, JobQueue
from clipengine.web.app import create_app
from test_complete_selection import ARGUMENT, OTHER, QUALIFY, SETUP, transcript
from test_session9 import make_recording, run_selection

TARIFF = ("The tariff on Canadian steel was raised to 25 percent last week. Why would the administration do that now? "
          "Because they say domestic mills are losing contracts. But the Treasury has not published any figures that show it.")
BUSINESS = ("We stopped hiring salespeople and doubled our revenue in six months. The reason is simple: every inbound "
            "lead was answered within five minutes. Speed to lead beat headcount every time.")


def test_titles_use_only_words_from_the_clip():
    for text, brand in ((BUSINESS, "operator_notes"), (TARIFF, "afterlive"), (TARIFF, "open_floor")):
        r = hooks.generate(text, brand)
        assert r["title"], r
        stems = {hooks._stem(t) for t in hooks._toks(text)}
        assert all(hooks._stem(t) in stems for t in hooks._content(r["title"]))
        assert 3 <= len(r["title"].split()) <= 12


def test_business_title_is_specific_and_in_range():
    r = hooks.generate(BUSINESS, "operator_notes")
    assert r["status"] == "proposed" and r["title"] == "Speed to lead beat headcount every time"
    assert r["alternatives"] and r["support"]


def test_candidate_dropping_a_qualification_is_rejected():
    r = hooks.generate("The Pixel battery lasts about nine hours of screen time, which is not enough if you game.", "spec_edit")
    assert "lasts about nine hours" not in (r["title"] or "")
    bad = [c for c in r["candidates"] if c["method"] == "claim"]
    assert bad and all(not c["accepted"] and "qualification" in c["warnings"][0] for c in bad)


def test_hedge_in_the_rest_of_the_sentence_is_never_dropped():
    r = hooks.generate("Prices will double next year, but only if the supplier raises its rates.", "dial_study")
    assert all("Prices will double next year" != c["text"] or not c["accepted"] for c in r["candidates"])


def test_invented_llm_candidates_are_rejected_but_faithful_ones_pass():
    r = hooks.generate(BUSINESS, "operator_notes", llm_titles=[
        "How they secretly made 10 million dollars", "Speed to lead beat headcount", 'Their "never hire again" rule',
        "Shocking sales secret exposed"])
    by = {c["text"]: c for c in r["candidates"]}
    assert not by["How they secretly made 10 million dollars"]["accepted"]
    assert not by['Their "never hire again" rule']["accepted"]
    assert not by["Shocking sales secret exposed"]["accepted"]
    assert by["Speed to lead beat headcount"]["accepted"]


def test_llm_candidate_must_keep_the_sources_negation():
    t = "The new model is not faster than the old one. Battery life is the real upgrade here."
    r = hooks.generate(t, "spec_edit", llm_titles=["The new model is faster than the old one"])
    c = next(c for c in r["candidates"] if c["method"] == "llm")
    assert not c["accepted"] and "negat" in " ".join(c["warnings"])


def test_america_first_monitor_is_attributed_never_a_bare_assertion():
    r = hooks.generate(TARIFF, "america_first_monitor", speaker="Fuentes")
    assert r["title"] == "Fuentes on Canadian steel" and r["method"] == "attributed_topic"
    assert any("confirm" in w for w in r["warnings"])
    assert all(c["method"] != "claim" for c in r["candidates"])
    r2 = hooks.generate(TARIFF, "america_first_monitor", llm_titles=["Treasury has not published any figures"])
    llm = next(c for c in r2["candidates"] if c["method"] == "llm")
    assert not llm["accepted"] and "attributed" in " ".join(llm["warnings"])


def test_inflammatory_wording_is_screened_for_political_brand():
    r = hooks.generate("Kill all the traitors in the senate, he said, kill them all today.", "america_first_monitor", speaker="Smith")
    assert "kill" not in (r["title"] or "").lower() and r["status"] != "proposed"
    r2 = hooks.generate("They will destroy the Senate, the Senate must be told.", "america_first_monitor", speaker="Smith")
    assert "destroy" not in (r2["title"] or "").lower()


def test_no_usable_passage_asks_for_a_manual_title_instead_of_inventing_one():
    r = hooks.generate("Yeah. Um. So. Right.", "afterlive")
    assert r["status"] == "manual_needed" and r["title"] is None
    assert hooks.fields_for(r)["title_status"] == "manual_needed"


def test_topic_fallback_is_neutral():
    r = hooks.generate("It lasts nine hours. Pixel battery. The Pixel battery again.", "spec_edit")
    assert r["status"] in ("neutral", "proposed") and r["title"]


def test_owner_titles_are_never_regenerated():
    gen = {"title_source": "generated", "title_locked": 0, "title_status": "proposed"}
    assert hooks.may_regenerate(gen)
    assert not hooks.may_regenerate({**gen, "title_source": "owner"})
    assert not hooks.may_regenerate({**gen, "title_locked": 1})
    assert not hooks.may_regenerate({**gen, "title_status": "approved"})


def test_emphasis_markup_is_the_approved_last_two_words():
    assert hooks.render_markup("Speed to lead beat headcount every time") == "Speed to lead beat headcount *every* **time**"
    assert hooks.render_markup(None) is None


# ---- pipeline + routes -----------------------------------------------------------------------------

def test_pipeline_stores_a_generated_title_with_evidence(settings):
    db = Database(settings.db_path)
    rid = make_recording(settings, db, [SETUP, 0.4, ARGUMENT, 0.4, QUALIFY, 7.0, OTHER])
    run_selection(settings, db, rid)
    clips = db.query("SELECT * FROM clips WHERE recording_id=?", (rid,))
    assert clips
    for c in clips:
        assert c["title_source"] == "generated" and c["title_status"] in ("proposed", "neutral", "manual_needed")
        info = json.loads(c["title_json"])
        assert info["candidates"] is not None and info["brand_policy"]
        if c["title"]:
            stems = {hooks._stem(t) for t in hooks._toks(c["transcript_text"])}
            assert all(hooks._stem(t) in stems for t in hooks._content(c["title"]))


@pytest.fixture
def web(settings):
    app = create_app(settings, start_worker=False)
    with TestClient(app) as c:
        yield c, app


def _clip(settings, db, status="ready"):
    rid = db.insert("recordings", title="Rec", source_type="upload", status="ready")
    r = hooks.generate(BUSINESS, "operator_notes")
    return db.insert("clips", recording_id=rid, idx=1, start=1.0, end=40.0, status=status, transcript_text=BUSINESS,
                     rights_status="owned", **hooks.fields_for(r))


def test_owner_can_edit_lock_choose_regenerate_and_remove(settings, web):
    c, app = web
    db = Database(settings.db_path)
    cid = _clip(settings, db, status="ready")
    row = db.clip(cid)
    assert row["title_source"] == "generated" and not row["title_locked"]
    alt = json.loads(row["title_json"])["alternatives"][0]
    c.post(f"/clips/{cid}/title/choose", data={"text": alt})
    assert db.clip(cid)["title"] == alt and db.clip(cid)["title_source"] == "generated"
    c.post(f"/clips/{cid}/title/choose", data={"text": "Totally invented headline"})
    assert db.clip(cid)["title"] == alt                                         # unverified wording refused
    c.post(f"/clips/{cid}/title", data={"title": "My own *words*"})
    row = db.clip(cid)
    assert row["title"] == "My own *words*" and row["title_source"] == "owner" and row["title_locked"] == 1
    c.post(f"/clips/{cid}/title/regenerate")
    assert db.clip(cid)["title"] == "My own *words*"                            # locked: untouched
    c.post(f"/clips/{cid}/title/unlock")
    c.post(f"/clips/{cid}/title/regenerate")
    assert db.clip(cid)["title_source"] == "generated" and db.clip(cid)["title"]
    c.post(f"/clips/{cid}/title/approve")
    assert db.clip(cid)["title_status"] == "approved" and db.clip(cid)["title_locked"] == 1
    c.post(f"/clips/{cid}/title/remove")
    assert db.clip(cid)["title"] is None and db.clip(cid)["title_status"] == "removed"
    page = c.get(f"/clips/{cid}/workspace")
    assert page.status_code == 200 and "Hook title" in page.text


def test_adjusting_a_clip_refreshes_generated_titles_but_never_owner_titles(settings):
    db = Database(settings.db_path)
    rid = make_recording(settings, db, [SETUP, 0.4, ARGUMENT, 0.4, QUALIFY, 7.0, OTHER])
    p, q = run_selection(settings, db, rid)
    src = settings.data_dir / "src.mp4"
    src.write_bytes(b"x")
    db.update("recordings", rid, file_path=str(src))
    c0 = db.query("SELECT * FROM clips WHERE recording_id=? ORDER BY id", (rid,))[0]
    db.update("clips", c0["id"], title="Owner wording", title_source="owner", title_locked=1, title_status="approved", status="ready")
    p.adjust_and_regenerate(c0["id"], c0["start"] + 0.5, c0["end"] - 0.5)
    assert db.clip(c0["id"])["title"] == "Owner wording"


# ---- real render ---------------------------------------------------------------------------------

needs_ffmpeg = pytest.mark.skipif(not __import__("shutil").which("ffmpeg"), reason="ffmpeg required")


@needs_ffmpeg
@pytest.mark.parametrize("src_w,src_h", [(1280, 720), (640, 480)])
@pytest.mark.parametrize("title", [
    "Fuentes on Canadian steel",
    "Why would the administration do that now?",
    "Speed to lead beat headcount every time",
    "The Nautilus 5711 was discontinued in 2021",
    "Most people prepare their points but never prepare their questions",
])
def test_generated_titles_render_inside_the_frame(settings, sample_video, tmp_path, title, src_w, src_h):
    np = pytest.importorskip("numpy")
    Image = pytest.importorskip("PIL.Image")
    words = [{"word": w, "start": i * .4, "end": i * .4 + .35} for i, w in enumerate("the committee will publish its findings".split())]
    kw = dict(style=captions.CaptionStyle())
    plain = render.render_clip(settings, sample_video, tmp_path / "a", 0.0, 2.0, words, src_w, src_h, None, **kw)
    titled = render.render_clip(settings, sample_video, tmp_path / "b", 0.0, 2.0, words, src_w, src_h, None,
                                title=hooks.render_markup(title), title_style=titles.TitleStyle(enabled=True), **kw)
    fr = {}
    for tag, res in (("a", plain), ("b", titled)):
        out = tmp_path / f"{tag}.png"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", "1.0", "-i", str(res["video"]), "-frames:v", "1", str(out)], check=True)
        fr[tag] = np.asarray(Image.open(out).convert("RGB")).astype(int)
    a, b = fr["a"], fr["b"]
    fg_h = render.layout(settings, src_w, src_h)["fg_h"]
    top = int((1920 - fg_h) / 2) + 4
    assert np.abs(a[top:-400] - b[top:-400]).mean() < 3.0                       # picture unchanged
    edge = np.abs(a[:top - 8] - b[:top - 8]).sum(axis=2) > 60
    assert edge.any()                                                           # title drawn in the upper band
    cols = np.where(edge.any(axis=0))[0]
    assert cols.min() >= 20 and cols.max() <= 1080 - 20
    assert np.abs(a[-400:] - b[-400:]).mean() < 3.0                             # captions untouched
