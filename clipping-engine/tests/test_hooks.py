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


# ---- Session 13 regressions: real wording from recording 5 (clips 25-30) -----------------------------------------
AWS = ("It's not like Amazon AWS went down. It's not like Cloudflare went down. It's not like these systemic services "
       "that carry 70% of the internet had disruption, and it cascaded through the rest of them. No, these are all "
       "different systems all going down in sequence.")
WAVE = ("Since that second wave of technology, we have seen youth unemployment rise. What does that tell you? "
        "Down detector data also show the highest weekly outages in twelve months.")


def _accepted_texts(result):
    return [c["text"] for c in result["candidates"] if c["accepted"]]


@pytest.mark.parametrize("brand", [None, "america_first_monitor", "afterlive"])
def test_negated_passage_never_becomes_a_verb_fragment_title(brand):
    """'Amazon AWS went' dropped 'not like ... down' and reversed the meaning. No accepted candidate may contain it."""
    r = hooks.generate(AWS, brand, "Fuentes")
    assert not any("went" in t.lower().split() for t in _accepted_texts(r)), r["candidates"]
    if r["title"]:
        assert "went" not in r["title"].lower().split()


def test_topic_labels_are_noun_phrases_without_verbs():
    for text in (AWS, WAVE, TARIFF, BUSINESS):
        r = hooks.generate(text, "america_first_monitor", "Fuentes")
        for c in r["candidates"]:
            if c["accepted"] and c["method"] in ("topic", "attributed_topic", "neutral_topic"):
                body = c["text"].replace("Fuentes", "").strip()
                body = body[3:] if body.lower().startswith("on ") else body
                assert not any(hooks._is_verbal(t) for t in hooks._toks(body)), c


def test_topic_from_a_negated_sentence_is_flagged_for_the_reviewer():
    r = hooks.generate(AWS, "america_first_monitor", "Fuentes")
    cand = next(c for c in r["candidates"] if c["accepted"] and "Amazon AWS" in c["text"])
    assert any("negated or hedged" in w for w in cand["warnings"]), cand


def test_dangling_clauses_and_subjectless_questions_are_rejected():
    r = hooks.generate(WAVE, "afterlive")
    texts = _accepted_texts(r)
    assert not any(t.lower().startswith("since ") for t in texts), texts
    assert "What does that tell you?" not in texts, texts
    assert not any(t.lower().endswith(" also show") for t in texts), texts


def test_complete_factual_claims_still_pass():
    r = hooks.generate("Over 20,000 flights were affected. Airlines said the delays would continue.", "afterlive")
    assert "Over 20,000 flights were affected" in _accepted_texts(r), r["candidates"]


def test_creator_without_a_brand_gets_its_brand_policy_from_the_niche(settings):
    """Clips 25-30 were titled with brand_policy 'default'. A politics creator with no brand_key must still be held to
    the America First Monitor policy (attributed/topic wording only), never the permissive default."""
    import time
    db = Database(settings.db_path)
    now = time.time()
    db.execute("INSERT OR REPLACE INTO brands (key, name, niche_key, created_at, updated_at) VALUES (?,?,?,?,?)",
               ("america_first_monitor", "America First Monitor", "politics", now, now))
    cid = db.insert("creators", name="Some Host", niches_json='["politics"]')
    rid = db.insert("recordings", title="Rec", source_type="upload", status="ready", creator_id=cid)
    p = pl.Pipeline(settings, db, JobQueue(db))
    fields = p.hook_fields(db.recording(rid), AWS)
    info = json.loads(fields["title_json"])
    assert info["brand_policy"] == "america_first_monitor", info["brand_policy"]
    assert all(c["method"] != "claim" for c in info["candidates"] if c["accepted"])


# ---- Session 13, part 2: real recording-6 wording (a profane GTA gameplay stream) --------------------------------------
GTA_RAGE = "What are you looking at, bitch? Huh? You fucking bitch. What are you looking at? Like the leg sweep. Idiot, bitch."
GTA_STUMBLE = "I'm gonna just straight up rage quit this game. Here's the gun store. This car is broke. Civilians dude dude dude."
GTA_FILLER = "Where's the good stuff? Is it time, sure, let me see. I gotta get rid of the evidence, huh? Where's the gun store?"
SENTENCE_RUNON = "Tomorrow they're putting me in a holding pattern and starting all the paperwork It's great. That's a problem That's gonna be an issue."


@pytest.mark.parametrize("brand", [None, "america_first_monitor", "afterlive", "operator_notes", "open_floor"])
def test_profanity_never_reaches_a_generated_headline(brand):
    r = hooks.generate(GTA_RAGE, brand, "Fuentes")
    assert not any(hooks.PROFANE.search(t) for t in _accepted_texts(r)), r["candidates"]
    assert not (r["title"] and hooks.PROFANE.search(r["title"]))


def test_stumbles_contractions_and_filler_are_not_subjects():
    for text in (GTA_STUMBLE, SENTENCE_RUNON, GTA_FILLER):
        r = hooks.generate(text, "america_first_monitor", "Fuentes")
        for t in _accepted_texts(r):
            toks = hooks._toks(t)
            assert not any(a.lower() == b.lower() for a, b in zip(toks, toks[1:])), t          # 'dude dude'
            assert not any(hooks.CONTRACTION.search(w) for w in toks if t.startswith("Fuentes on")), t
            assert not (t.startswith("Fuentes on") and any(w.lower() in hooks.GENERIC for w in toks)), t
        assert "Where's the good stuff?" not in _accepted_texts(r)


def test_political_brand_never_turns_a_first_person_line_into_a_headline():
    r = hooks.generate(GTA_FILLER, "america_first_monitor", "Fuentes")
    assert not any(t.lower().startswith(("i ", "i'm", "we ", "my ")) for t in _accepted_texts(r)), r["candidates"]


def test_unusable_chatter_falls_back_to_a_manual_title_not_an_invented_one():
    r = hooks.generate(GTA_STUMBLE, "america_first_monitor", "Fuentes")
    assert r["status"] in ("manual_needed", "proposed", "neutral")
    if r["status"] == "manual_needed":
        assert r["title"] is None
