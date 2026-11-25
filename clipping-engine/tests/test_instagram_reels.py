"""Session 15: Instagram resumable-upload publisher and its gates. TESTED-MOCKED: every HTTP call is a fake; Meta is never contacted."""
import time
from urllib.parse import parse_qs

import httpx
import pytest

from clipengine import connections as cx
from clipengine.db import Database
from clipengine.publishing import PublishError, autodeliver, delivery
from clipengine.publishing.instagram_reels import InstagramReelsPublisher

PERMIT = '{"granted_by": "Owner", "reference": "email 2026-11-01"}'
IG_ID = "17841418780627771"


def make_http(calls, *, status_codes=("IN_PROGRESS", "FINISHED"), ig_id=IG_ID):
    codes = list(status_codes)

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append((req.method, str(req.url).split("?")[0], dict(req.headers)))
        url = str(req.url)
        base = url.split("?")[0]
        if req.method == "GET" and base.endswith(f"/{IG_ID}"):
            return httpx.Response(200, json={"id": ig_id, "username": "operatornotes"})
        if req.method == "POST" and url.endswith(f"/{IG_ID}/media"):
            return httpx.Response(200, json={"id": "CONT1", "uri": "https://rupload.facebook.com/x"})
        if req.method == "POST" and "rupload.facebook.com" in url:
            return httpx.Response(200, json={"success": True})
        if req.method == "GET" and base.endswith("/CONT1"):
            return httpx.Response(200, json={"status_code": codes.pop(0) if len(codes) > 1 else codes[0]})
        if req.method == "POST" and url.endswith(f"/{IG_ID}/media_publish"):
            return httpx.Response(200, json={"id": "MEDIA9"})
        if req.method == "GET" and base.endswith("/MEDIA9"):
            return httpx.Response(200, json={"permalink": "https://instagram.com/reel/abc"})
        return httpx.Response(404, json={"error": {"message": "unexpected " + url}})
    return httpx.Client(transport=httpx.MockTransport(handler))


def pub(http, enabled=True):
    return InstagramReelsPublisher(lambda b: "TOKEN", public_enabled=enabled, http=http, sleep=lambda s: None)


@pytest.fixture
def video(tmp_path):
    f = tmp_path / "v.mp4"
    f.write_bytes(b"v" * 2048)
    return f


def test_full_resumable_flow_in_order_with_no_public_url(video):
    calls = []
    res = pub(make_http(calls)).publish_reel("operator_notes", IG_ID, video, "caption")
    assert res["video_id"] == "MEDIA9" and res["permalink"].endswith("/abc")
    urls = [c[1] for c in calls]
    assert urls[0].endswith(f"/{IG_ID}") and urls[1].endswith(f"/{IG_ID}/media")           # identity first, then container
    up = next(c for c in calls if "rupload.facebook.com" in c[1])
    assert up[2]["offset"] == "0" and up[2]["file_size"] == "2048" and up[2]["authorization"] == "OAuth TOKEN"
    assert urls.index(next(u for u in urls if u.endswith("/media_publish"))) > urls.index(up[1])   # publish only after upload


def test_uses_the_upload_address_meta_returns_and_reports_rupload_errors(video):
    """Session 16, first live test: the self-built v23.0 address got HTTP 400 with no reason recorded."""
    calls = []
    pub(make_http(calls)).publish_reel("operator_notes", IG_ID, video, "caption")
    assert any(c[1] == "https://rupload.facebook.com/x" for c in calls)          # Meta's own `uri`, not the built one

    def bad(req):
        if req.method == "GET":
            return httpx.Response(200, json={"id": IG_ID, "username": "operatornotes"})
        if str(req.url).endswith("/media"):
            return httpx.Response(200, json={"id": "CONT1", "uri": "https://rupload.facebook.com/y"})
        return httpx.Response(400, json={"debug_info": {"type": "ProcessingFailedError", "message": "Request processing failed"}})
    with pytest.raises(PublishError, match="Request processing failed"):
        pub(httpx.Client(transport=httpx.MockTransport(bad))).publish_reel("operator_notes", IG_ID, video, "c")


def test_upload_uses_the_same_token_as_container_creation(video):
    seen = []

    def h(req):
        u = str(req.url)
        if req.method == "GET" and u.split("?")[0].endswith(f"/{IG_ID}"):
            return httpx.Response(200, json={"id": IG_ID, "username": "operatornotes"})
        if u.endswith("/media"):
            seen.append(parse_qs(req.content.decode())["access_token"][0])
            return httpx.Response(200, json={"id": "CONT1", "uri": "https://rupload.facebook.com/x"})
        if "rupload" in u:
            seen.append(req.headers["authorization"])
            return httpx.Response(200, json={"success": True})
        if req.method == "GET" and "/CONT1" in u:
            return httpx.Response(200, json={"status_code": "FINISHED"})
        if u.endswith("/media_publish"):
            return httpx.Response(200, json={"id": "MEDIA9"})
        return httpx.Response(200, json={"permalink": "p"})
    p = InstagramReelsPublisher(lambda b: "TOKEN", public_enabled=True, http=httpx.Client(transport=httpx.MockTransport(h)),
                                sleep=lambda s: None)
    assert p.publish_reel("operator_notes", IG_ID, video, "c")["video_id"] == "MEDIA9"
    assert seen == ["TOKEN", "OAuth TOKEN"]


def test_off_by_default_sends_nothing(video):
    calls = []
    with pytest.raises(PublishError, match="OFF"):
        pub(make_http(calls), enabled=False).publish_reel("operator_notes", IG_ID, video, "c")
    assert calls == []


def test_wrong_account_is_refused_before_any_upload(video):
    calls = []
    with pytest.raises(PublishError, match="verified account"):
        pub(make_http(calls, ig_id="999")).publish_reel("operator_notes", IG_ID, video, "c")
    assert not any(c[0] == "POST" for c in calls)


def test_processing_error_means_not_published(video):
    calls = []
    with pytest.raises(PublishError, match="ERROR"):
        pub(make_http(calls, status_codes=("ERROR",))).publish_reel("operator_notes", IG_ID, video, "c")
    assert not any(c[1].endswith("/media_publish") for c in calls)


def test_waits_then_refuses_when_processing_never_finishes(video):
    p = InstagramReelsPublisher(lambda b: "T", public_enabled=True, http=make_http([], status_codes=("IN_PROGRESS",)),
                                sleep=lambda s: None, poll_seconds=5, max_wait=15)
    with pytest.raises(PublishError, match="wait limit"):
        p.publish_reel("operator_notes", IG_ID, video, "c")


def test_missing_token_is_refused(video):
    p = InstagramReelsPublisher(lambda b: None, public_enabled=True, http=make_http([]), sleep=lambda s: None)
    with pytest.raises(PublishError, match="No Instagram authorisation"):
        p.publish_reel("operator_notes", IG_ID, video, "c")


# ---- through the real delivery gates ---------------------------------------------------------------------------------

@pytest.fixture
def db(settings):
    d = Database(settings.db_path)
    cx.record_directory(d)
    now = time.time()
    d.execute("INSERT OR REPLACE INTO brands (key,name,niche_key,created_at,updated_at) VALUES (?,?,?,?,?)",
              ("operator_notes", "Operator Notes", "ai_business", now, now))
    return d


def _clip(settings, db):
    cr = db.insert("creators", name="Host", brand_key="operator_notes", permission_json=PERMIT)
    rid = db.insert("recordings", title="Source title", source_type="upload", status="ready", creator_id=cr)
    f = settings.clips_dir / "ig.mp4"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"x" * 64)
    return db.insert("clips", recording_id=rid, idx=1, start=0, end=20, status="approved", rights_status="licensed",
                     safety_status="clear", video_path=str(f), description="d", title="T", score=0.9)


def _ig_account(db, brand="operator_notes"):
    a = db.one("SELECT * FROM social_accounts WHERE platform='instagram' AND brand=?", (brand,))
    cx.verify_identity(db, a["id"], [{"id": a["platform_id"]}])
    db.update("social_accounts", a["id"], publishing_enabled=1)
    return db.one("SELECT * FROM social_accounts WHERE id=?", (a["id"],))


class FakeIG:
    def __init__(self, enabled=True):
        self.public_enabled, self.calls = enabled, []

    def publish_reel(self, brand, ig_id, video, caption):
        self.calls.append((brand, ig_id))
        return {"video_id": "M1", "permalink": "https://instagram.com/reel/z", "success": True}


def test_delivery_refuses_instagram_in_draft_mode_or_when_the_switch_is_off(settings, db):
    cid, acct = _clip(settings, db), _ig_account(db)
    with pytest.raises(PublishError, match="no draft step"):
        delivery.deliver_draft(db, {"instagram": FakeIG(True)}, cid, acct["id"], owner_command=True)      # mode = draft
    delivery.set_mode(db, acct["id"], "public")
    with pytest.raises(PublishError, match="no draft step"):
        delivery.deliver_draft(db, {"instagram": FakeIG(False)}, cid, acct["id"], owner_command=True)     # env switch off


def test_delivery_publishes_to_the_clips_own_brand_account_only(settings, db):
    cid, acct = _clip(settings, db), _ig_account(db)
    delivery.set_mode(db, acct["id"], "public")
    fake = FakeIG(True)
    out = delivery.deliver_draft(db, {"instagram": fake}, cid, acct["id"], owner_command=True)
    assert out["status"] == "published" and fake.calls == [("operator_notes", acct["platform_id"])]
    with pytest.raises(PublishError, match="already sent"):
        delivery.deliver_draft(db, {"instagram": fake}, cid, acct["id"], owner_command=True)             # no duplicate post
    other = _ig_account(db, "afterlive")
    delivery.set_mode(db, other["id"], "public")
    cid2 = _clip(settings, db)
    with pytest.raises(PublishError, match="Brand mismatch"):
        delivery.deliver_draft(db, {"instagram": fake}, cid2, other["id"], owner_command=True)           # cross-brand blocked


def test_instagram_is_only_a_destination_when_enabled_verified_and_public(settings, db):
    cid, acct = _clip(settings, db), _ig_account(db)
    clip = db.clip(cid)
    assert autodeliver.destinations_for(db, clip) == [] and "instagram" in autodeliver.manual_platforms(db, clip)
    delivery.set_mode(db, acct["id"], "public")
    assert [a["platform"] for a in autodeliver.destinations_for(db, clip)] == ["instagram"]
    assert "instagram" not in autodeliver.manual_platforms(db, clip)
