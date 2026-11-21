"""Session 13: phone alerts, the one-tap review queue, approve -> auto-upload (drafts/private only) with a daily cap.
All HTTP is mocked; nothing contacts ntfy or a platform."""
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from clipengine import connections as cx
from clipengine import phone, recording_switch
from clipengine.db import Database
from clipengine.jobs import JobQueue, PermanentError, TransientError
from clipengine.publishing import autodeliver
from clipengine.web.app import create_app

PERMIT = '{"granted_by": "Owner", "reference": "email 2026-11-01"}'


@pytest.fixture
def db(settings):
    d = Database(settings.db_path)
    cx.record_directory(d)
    now = time.time()
    d.execute("INSERT OR REPLACE INTO brands (key,name,niche_key,created_at,updated_at) VALUES (?,?,?,?,?)",
              ("afterlive", "Afterlive", "gaming_streams", now, now))
    return d


def mk_clip(settings, db, *, permitted=True, safety="clear", status="ready", n=[0]):
    n[0] += 1
    cr = db.insert("creators", name=f"Host {n[0]}", brand_key="afterlive", permission_json=PERMIT if permitted else "{}")
    rid = db.insert("recordings", title="R", source_type="upload", status="ready", creator_id=cr)
    f = settings.clips_dir / f"c{n[0]}.mp4"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"x" * 64)
    return db.insert("clips", recording_id=rid, idx=1, start=0, end=20, status=status, rights_status="unreviewed",
                     safety_status=safety, video_path=str(f), description="d", title=f"SECRET TITLE {n[0]}", score=0.9)


def enable(db, platform, brand="afterlive"):
    a = db.one("SELECT * FROM social_accounts WHERE platform=? AND brand=?", (platform, brand))
    cx.verify_identity(db, a["id"], [{"id": a["platform_id"]}])
    db.update("social_accounts", a["id"], publishing_enabled=1)
    return db.one("SELECT * FROM social_accounts WHERE id=?", (a["id"],))


class Ctx:
    def __init__(self, payload):
        self.payload, self.logs = payload, []

    def log(self, m, level="INFO"):
        self.logs.append(m)


class Fake:
    def __init__(self):
        self.calls = []

    privacy_result = None            # what the fake YouTube "reports back" in public mode

    def upload(self, db, aid, video, title, description, public=False):
        self.calls.append("youtube-public" if public else "youtube")
        return {"video_id": "VID", "privacy": (self.privacy_result or ("public" if public else "private"))}

    def create_reel(self, db, aid, video, description, publish=False):
        self.calls.append("facebook-public" if publish else "facebook")
        return {"video_id": "REEL", "success": True}


# ---- phone queue / alerts ------------------------------------------------------------------------------------------

def test_phone_queue_only_has_safe_clips_whose_creator_permission_is_on_record(settings, db):
    ok = mk_clip(settings, db)
    mk_clip(settings, db, permitted=False)               # no recorded permission -> desktop review
    mk_clip(settings, db, safety="flagged")              # flagged -> desktop review
    assert [c["id"] for c in phone.phone_queue(db)] == [ok]


def _ntfy(sent):
    def handler(req):
        sent.append((str(req.url), req.content.decode(), dict(req.headers)))
        return httpx.Response(200)
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_alert_is_generic_batched_and_not_repeated(settings, db):
    settings.ntfy_topic, settings.dashboard_url = "t0pic-xyz", "https://pc.example.ts.net"
    a = mk_clip(settings, db)
    sent = []
    now = 1_800_000_000.0
    r = phone.digest_tick(settings, db, now, _ntfy(sent))
    assert r["sent"] and r["new"] == 1 and len(sent) == 1
    url, body, headers = sent[0]
    assert url.endswith("/t0pic-xyz") and "1 new clip ready" in body and headers["click"].endswith("/m")
    assert "SECRET TITLE" not in body and "Host" not in body                      # nothing identifying leaves the computer
    assert phone.digest_tick(settings, db, now + 5, _ntfy(sent)) is None          # nothing new -> no repeat
    b = mk_clip(settings, db)
    assert phone.digest_tick(settings, db, now + 60, _ntfy(sent)) is None         # inside the batching gap
    r = phone.digest_tick(settings, db, now + 11 * 60, _ntfy(sent))
    assert r["sent"] and r["new"] == 1 and r["waiting"] == 2 and "2 waiting in all" in sent[-1][1]


def test_many_new_clips_alert_immediately_and_quiet_hours_hold_alerts(settings, db):
    settings.ntfy_topic = "t0pic"
    now = 1_800_000_000.0
    phone.digest_tick(settings, db, now, _ntfy([]))                                # nothing yet
    db.set_kv(phone.KEY_LAST_AT, str(now))
    for _ in range(5):
        mk_clip(settings, db)
    sent = []
    assert phone.digest_tick(settings, db, now + 30, _ntfy(sent))["sent"] and len(sent) == 1   # 5+ ignores the gap
    mk_clip(settings, db)
    settings.push_quiet_hours = "00:00-23:59"
    assert phone.digest_tick(settings, db, now + 4000, _ntfy(sent)) is None and len(sent) == 1


def test_a_failed_alert_is_retried_and_not_marked_sent(settings, db):
    settings.ntfy_topic = "t0pic"
    mk_clip(settings, db)
    bad = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    r = phone.digest_tick(settings, db, 1_800_000_000.0, bad)
    assert r["sent"] is False and db.get_kv(phone.KEY_LAST_ID, "0") == "0"
    assert phone.digest_tick(settings, db, 1_800_000_100.0, _ntfy([]))["sent"] is True


def test_no_topic_means_no_alert(settings, db):
    mk_clip(settings, db)
    assert phone.digest_tick(settings, db, 1.0, _ntfy([])) is None


# ---- approve -> upload -------------------------------------------------------------------------------------------

def test_approval_queues_uploads_only_for_verified_switched_on_destinations_of_the_clips_brand(settings, db):
    q = JobQueue(db)
    cid = mk_clip(settings, db)
    db.update("clips", cid, status="approved")
    out = autodeliver.prepare_and_enqueue(db, q, cid)
    assert out["queued"] == [] and "switched on" in out["reason"]              # nothing enabled -> nothing queued
    enable(db, "youtube")
    enable(db, "facebook")
    enable(db, "youtube", "america_first_monitor")                              # another brand: never used for this clip
    out = autodeliver.prepare_and_enqueue(db, q, cid)
    assert sorted(x["platform"] for x in out["queued"]) == ["facebook", "youtube"]
    assert db.clip(cid)["rights_status"] == "licensed"                         # derived from the recorded creator permission
    assert db.one("SELECT COUNT(*) n FROM jobs WHERE kind='deliver_clip'")["n"] == 2
    autodeliver.prepare_and_enqueue(db, q, cid)
    assert db.one("SELECT COUNT(*) n FROM jobs WHERE kind='deliver_clip'")["n"] == 2          # never queued twice
    assert autodeliver.manual_platforms(db, db.clip(cid)) == ["instagram", "tiktok"]


def test_flagged_or_unapproved_clips_are_never_queued(settings, db):
    q = JobQueue(db)
    enable(db, "youtube")
    flagged = mk_clip(settings, db, safety="flagged", status="approved")
    assert autodeliver.prepare_and_enqueue(db, q, flagged)["queued"] == []
    ready = mk_clip(settings, db)
    assert autodeliver.prepare_and_enqueue(db, q, ready)["queued"] == []        # not approved
    unpermitted = mk_clip(settings, db, permitted=False, status="approved")
    out = autodeliver.prepare_and_enqueue(db, q, unpermitted)
    assert out["queued"] == [] and "rights" in out["reason"].lower()


def test_job_uploads_a_draft_and_respects_every_switch(settings, db):
    q = JobQueue(db)
    cid = mk_clip(settings, db)
    db.update("clips", cid, status="approved")
    yt = enable(db, "youtube")
    autodeliver.prepare_and_enqueue(db, q, cid)
    fake = Fake()
    clients = {"youtube": fake, "facebook": fake}
    payload = {"clip_id": cid, "account_id": yt["id"]}
    recording_switch.set_delivery_enabled(db, False)
    with pytest.raises(TransientError, match="OFF"):
        autodeliver.handle(settings, db, clients, Ctx(payload))
    assert fake.calls == []
    recording_switch.set_delivery_enabled(db, True)
    autodeliver.handle(settings, db, clients, Ctx(payload))
    assert fake.calls == ["youtube"]
    row = db.one("SELECT status FROM publish_attempts WHERE clip_id=?", (cid,))
    assert row["status"] == "delivered_private"                                  # private, never public
    ctx = Ctx(payload)
    autodeliver.handle(settings, db, clients, ctx)                               # a retry after success does not re-upload
    assert fake.calls == ["youtube"]


def test_job_does_nothing_if_the_clip_was_unapproved_or_the_channel_switched_off(settings, db):
    q = JobQueue(db)
    cid = mk_clip(settings, db)
    db.update("clips", cid, status="approved")
    yt = enable(db, "youtube")
    fake = Fake()
    db.update("clips", cid, status="rejected")
    autodeliver.handle(settings, db, {"youtube": fake}, Ctx({"clip_id": cid, "account_id": yt["id"]}))
    db.update("clips", cid, status="approved")
    db.update("social_accounts", yt["id"], publishing_enabled=0)
    autodeliver.handle(settings, db, {"youtube": fake}, Ctx({"clip_id": cid, "account_id": yt["id"]}))
    assert fake.calls == []


def test_daily_cap_holds_the_rest_for_tomorrow(settings, db):
    settings.delivery_daily_cap = 2
    yt = enable(db, "youtube")
    fake = Fake()
    ids = []
    for _ in range(3):
        cid = mk_clip(settings, db)
        db.update("clips", cid, status="approved", rights_status="licensed")
        ids.append(cid)
    for cid in ids[:2]:
        autodeliver.handle(settings, db, {"youtube": fake}, Ctx({"clip_id": cid, "account_id": yt["id"]}))
    with pytest.raises(TransientError, match="Daily cap of 2") as e:
        autodeliver.handle(settings, db, {"youtube": fake}, Ctx({"clip_id": ids[2], "account_id": yt["id"]}))
    assert e.value.retry_after >= 300 and fake.calls == ["youtube", "youtube"]
    # a different brand's channel has its own allowance
    assert autodeliver.sent_today(db, yt) == 2


def test_a_platform_refusal_fails_the_job_visibly_instead_of_retrying_forever(settings, db):
    from clipengine.publishing.base import PublishError

    class Boom(Fake):
        def upload(self, *a, **k):
            raise PublishError("YouTube refused the upload session (HTTP 403).")
    yt = enable(db, "youtube")
    cid = mk_clip(settings, db)
    db.update("clips", cid, status="approved", rights_status="licensed")
    with pytest.raises(PermanentError, match="403"):
        autodeliver.handle(settings, db, {"youtube": Boom()}, Ctx({"clip_id": cid, "account_id": yt["id"]}))


# ---- the phone page ----------------------------------------------------------------------------------------------

def test_mobile_page_approve_reject_and_skip(settings, db):
    a, b, c = (mk_clip(settings, db) for _ in range(3))
    enable(db, "youtube")
    app = create_app(settings, start_worker=False)
    with TestClient(app) as cl:
        html = cl.get("/m").text
        assert "3 to review" in html and f"/clips/{a}/video" in html and "SECRET TITLE" in html and "private upload" in html
        assert f"skip={a}" in cl.get("/m").text
        assert f"/clips/{b}/video" in cl.get(f"/m?skip={a}").text                        # skipping moves to the next
        r = cl.post(f"/m/{a}/approve", data={"skip": ""}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/m")
        assert db.clip(a)["status"] == "approved" and db.clip(a)["rights_status"] == "licensed"
        assert db.one("SELECT COUNT(*) n FROM jobs WHERE kind='deliver_clip'")["n"] == 1
        cl.post(f"/m/{b}/reject", data={"skip": ""}, follow_redirects=False)
        assert db.clip(b)["status"] == "rejected"
        assert "1 to review" in cl.get("/m").text
        assert cl.post(f"/m/{a}/approve", data={}, follow_redirects=False).status_code == 303   # double tap is harmless
        assert db.one("SELECT COUNT(*) n FROM jobs WHERE kind='deliver_clip'")["n"] == 1


def test_approving_from_the_desktop_page_also_queues_uploads(settings, db):
    enable(db, "facebook")
    cid = mk_clip(settings, db)
    app = create_app(settings, start_worker=False)
    with TestClient(app) as cl:
        cl.post(f"/clips/{cid}/approve", data={}, headers={"referer": "http://testserver/review"}, follow_redirects=False)
        assert db.one("SELECT COUNT(*) n FROM jobs WHERE kind='deliver_clip'")["n"] == 1


def test_phone_page_switches_and_test_alert(settings, db):
    settings.ntfy_topic = ""
    app = create_app(settings, start_worker=False)
    with TestClient(app) as cl:
        html = cl.get("/phone").text
        assert "Not set up" in html and "Auto-upload ON" in cl.get("/").text
        cl.post("/delivery/toggle", data={"enable": "0"}, headers={"referer": "http://testserver/phone"}, follow_redirects=False)
        assert recording_switch.delivery_enabled(db) is False and "Auto-upload OFF" in cl.get("/phone").text
        r = cl.post("/phone/test", follow_redirects=False)
        assert r.status_code == 303 and "err=" in r.headers["location"]                 # no topic -> reported, not crashed


# ---- public mode ---------------------------------------------------------------------------------------------------

def test_destinations_default_to_drafts_and_public_mode_publishes_only_there(settings, db):
    from clipengine.publishing import delivery
    yt = enable(db, "youtube")
    fb = enable(db, "facebook")
    assert delivery.mode_for(db, yt["id"]) == "draft" and delivery.mode_for(db, fb["id"]) == "draft"
    delivery.set_mode(db, fb["id"], "public")                     # only Facebook goes public
    fake = Fake()
    cid = mk_clip(settings, db)
    db.update("clips", cid, status="approved", rights_status="licensed")
    for a in (yt, fb):
        autodeliver.handle(settings, db, {"youtube": fake, "facebook": fake}, Ctx({"clip_id": cid, "account_id": a["id"]}))
    assert sorted(fake.calls) == ["facebook-public", "youtube"]
    states = {r["platform"]: r["status"] for r in db.query("SELECT platform,status FROM publish_attempts WHERE clip_id=?", (cid,))}
    assert states == {"facebook": "published", "youtube": "delivered_private"}


def test_public_youtube_upload_forced_private_by_google_is_reported_honestly(settings, db):
    from clipengine.publishing import delivery
    yt = enable(db, "youtube")
    delivery.set_mode(db, yt["id"], "public")
    fake = Fake()
    fake.privacy_result = "private"                              # unaudited API project: YouTube keeps it private
    cid = mk_clip(settings, db)
    db.update("clips", cid, status="approved", rights_status="licensed")
    autodeliver.handle(settings, db, {"youtube": fake}, Ctx({"clip_id": cid, "account_id": yt["id"]}))
    row = db.one("SELECT status, detail FROM publish_attempts WHERE clip_id=?", (cid,))
    assert row["status"] == "delivered_private" and "returned PRIVATE" in row["detail"] and "YouTube Studio" in row["detail"]
    fake2 = Fake()
    cid2 = mk_clip(settings, db)
    db.update("clips", cid2, status="approved", rights_status="licensed")
    autodeliver.handle(settings, db, {"youtube": fake2}, Ctx({"clip_id": cid2, "account_id": yt["id"]}))
    assert db.one("SELECT status FROM publish_attempts WHERE clip_id=?", (cid2,))["status"] == "published"   # audited: truly public


def test_going_public_needs_the_switch_on_and_typed_publish_and_can_be_undone(settings, db):
    from clipengine.publishing import delivery
    yt = db.one("SELECT * FROM social_accounts WHERE platform='youtube' AND brand='afterlive'")
    app = create_app(settings, start_worker=False)
    with TestClient(app) as cl:
        r = cl.post(f"/connections/mode/{yt['id']}", data={"mode": "public", "confirm": "PUBLISH"}, follow_redirects=False)
        assert delivery.mode_for(db, yt["id"]) == "draft" and "switch+delivery+on" in r.headers["location"].lower()
        enable(db, "youtube")
        cl.post(f"/connections/mode/{yt['id']}", data={"mode": "public", "confirm": "publish"}, follow_redirects=False)
        assert delivery.mode_for(db, yt["id"]) == "draft"                       # typed confirmation is exact
        cl.post(f"/connections/mode/{yt['id']}", data={"mode": "public", "confirm": "PUBLISH"}, follow_redirects=False)
        assert delivery.mode_for(db, yt["id"]) == "public" and "mode: PUBLIC" in cl.get("/connections").text
        cl.post(f"/connections/mode/{yt['id']}", data={"mode": "draft"}, follow_redirects=False)
        assert delivery.mode_for(db, yt["id"]) == "draft"
        ig = db.one("SELECT * FROM social_accounts WHERE platform='instagram' AND brand='afterlive'")
        r = cl.post(f"/connections/mode/{ig['id']}", data={"mode": "public", "confirm": "PUBLISH"}, follow_redirects=False)
        # Session 16: refused while INSTAGRAM_PUBLIC_PUBLISH is off (allowed once it is on: test_connections_web)
        assert "INSTAGRAM_PUBLIC_PUBLISH" in r.headers["location"] and delivery.mode_for(db, ig["id"]) == "draft"


# ---- Session 15: balanced queue, title routes, retitle job -----------------------------------------------------------

def _mk_brand_clip(settings, db, brand, score=0.9, n=[1000]):
    n[0] += 1
    cr = db.insert("creators", name=f"Host {n[0]}", brand_key=brand, permission_json=PERMIT)
    rid = db.insert("recordings", title="R", source_type="upload", status="ready", creator_id=cr)
    f = settings.clips_dir / f"b{n[0]}.mp4"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"x" * 64)
    return db.insert("clips", recording_id=rid, idx=1, start=0, end=20, status="ready", rights_status="unreviewed",
                     safety_status="clear", video_path=str(f), description="d", title="T", score=score)


def test_queue_is_balanced_so_one_busy_brand_cannot_bury_the_others(settings, db):
    a = [_mk_brand_clip(settings, db, "afterlive") for _ in range(4)]
    o = [_mk_brand_clip(settings, db, "operator_notes") for _ in range(2)]
    order = [c["id"] for c in phone.phone_queue(db)]
    assert order == [a[0], o[0], a[1], o[1], a[2], a[3]]
    assert phone.queue_counts(db)[0][1] == 4


def test_best_scoring_clip_of_a_brand_comes_first(settings, db):
    low = _mk_brand_clip(settings, db, "afterlive", score=0.2)
    high = _mk_brand_clip(settings, db, "afterlive", score=0.95)
    assert [c["id"] for c in phone.phone_queue(db)] == [high, low]


def _with_master(settings, db, cid):
    from clipengine.pipeline import Pipeline
    p = Pipeline(settings, db, JobQueue(db))
    d = p.clip_dir(db.clip(cid))
    d.mkdir(parents=True, exist_ok=True)
    (d / "clip_clean.mp4").write_bytes(b"CLEAN" * 10)
    (d / "clip.mp4").write_bytes(b"TITLED" * 10)
    return p, d


def test_remove_title_route_restores_the_clean_video_and_locks_the_choice(settings, db):
    cid = mk_clip(settings, db)
    p, d = _with_master(settings, db, cid)
    with TestClient(create_app(settings, start_worker=False)) as cl:
        assert "Remove title" in cl.get("/m").text
        r = cl.post(f"/m/{cid}/title/remove", data={"skip": ""}, follow_redirects=False)
        assert r.status_code == 303
    assert (d / "clip.mp4").read_bytes() == (d / "clip_clean.mp4").read_bytes()
    row = db.clip(cid)
    assert row["title"] is None and row["title_locked"] == 1 and row["title_status"] == "removed"


def test_old_clips_without_a_master_say_so_instead_of_pretending(settings, db):
    cid = mk_clip(settings, db)
    with TestClient(create_app(settings, start_worker=False)) as cl:
        assert "without a re-render" in cl.get("/m").text and "Remove title" not in cl.get("/m").text
        r = cl.post(f"/m/{cid}/title/remove", data={}, follow_redirects=False)
        assert r.status_code == 303 and "Could not" in r.headers["location"].replace("+", " ").replace("%20", " ")
    assert db.clip(cid)["title"].startswith("SECRET TITLE")                      # untouched


def test_own_title_queues_a_light_job_and_locks_it(settings, db):
    cid = mk_clip(settings, db)
    _with_master(settings, db, cid)
    with TestClient(create_app(settings, start_worker=False)) as cl:
        cl.post(f"/m/{cid}/title", data={"text": "  My   own title ", "skip": ""}, follow_redirects=False)
    row = db.clip(cid)
    assert row["title"] == "My own title" and row["title_source"] == "owner" and row["title_locked"] == 1
    assert db.one("SELECT COUNT(*) n FROM jobs WHERE kind='retitle_clip'")["n"] == 1


def test_retitle_job_draws_the_title_on_the_master_and_skips_stale_requests(settings, db, monkeypatch):
    from clipengine import render
    cid = mk_clip(settings, db)
    p, d = _with_master(settings, db, cid)
    db.update("clips", cid, title="Latest title")
    seen = []
    monkeypatch.setattr(render, "apply_title", lambda s, out, t, st, w, h, dur=None, on_progress=None: seen.append(t))

    class C(Ctx):
        job = {"clip_id": cid}

        def progress(self, *a, **k):
            pass
    p.retitle_clip(C({"title": "Old title"}))
    assert seen == []                                                          # a newer title replaced it: nothing drawn
    p.retitle_clip(C({"title": "Latest title"}))
    assert len(seen) == 1


def test_description_is_editable_and_saved(settings, db):
    cid = mk_clip(settings, db)
    with TestClient(create_app(settings, start_worker=False)) as cl:
        assert "Save description" in cl.get("/m").text
        cl.post(f"/m/{cid}/description", data={"text": "  New words for the post  "}, follow_redirects=False)
    assert db.clip(cid)["description"] == "New words for the post"


def test_title_alternatives_are_offered_only_when_a_master_exists(settings, db):
    import json as _json
    cid = mk_clip(settings, db)
    db.update("clips", cid, title_json=_json.dumps({"alternatives": ["Alt one", "Alt two"]}))
    with TestClient(create_app(settings, start_worker=False)) as cl:
        assert "Use: Alt one" not in cl.get("/m").text                        # old clip: no clean master
        _with_master(settings, db, cid)
        html = cl.get("/m").text
        assert "Use: Alt one" in html and "Use: Alt two" in html


def test_approve_button_says_publish_only_when_a_destination_is_public(settings, db):
    mk_clip(settings, db)
    acct = enable(db, "youtube")
    with TestClient(create_app(settings, start_worker=False)) as cl:
        assert "Approve &amp; send" in cl.get("/m").text and "Approve &amp; Publish" not in cl.get("/m").text
        from clipengine.publishing import delivery as _d
        _d.set_mode(db, acct["id"], "public")
        assert "Approve &amp; Publish" in cl.get("/m").text and "PUBLIC video" in cl.get("/m").text


def test_results_page_lists_each_destination_outcome(settings, db):
    cid = mk_clip(settings, db)
    db.update("clips", cid, status="approved", reviewed_at=time.time())
    db.insert("publish_attempts", clip_id=cid, platform="youtube", status="delivered_private", detail="kept private", stage="done")
    db.insert("publish_attempts", clip_id=cid, platform="facebook", status="published", detail="live", stage="done")
    with TestClient(create_app(settings, start_worker=False)) as cl:
        html = cl.get("/m/results").text
        assert "published publicly" in html and "private (not public)" in html and f"clip {cid}" in html


def test_retrying_a_failed_delivery_keeps_the_clip_approved(settings, db):
    """Session 16: the Retry button set ANY clip job's clip to pending_render, which would re-render an approved clip."""
    cid = mk_clip(settings, db, status="approved")
    jid = db.insert("jobs", kind="deliver_clip", clip_id=cid, status="failed", payload_json='{"clip_id": %d, "account_id": 1}' % cid)
    app = create_app(settings, start_worker=False)
    with TestClient(app) as cl:
        cl.post(f"/jobs/{jid}/retry", headers={"referer": "http://testserver/jobs"}, follow_redirects=False)
    assert db.clip(cid)["status"] == "approved"
    assert db.one("SELECT status FROM jobs WHERE id=?", (jid,))["status"] == "queued"
