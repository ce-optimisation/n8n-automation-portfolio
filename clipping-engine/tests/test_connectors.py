"""Session 13: Facebook / YouTube connectors and the owner-commanded draft delivery gate.

ALL HTTP is mocked (httpx.MockTransport). These tests verify request construction, per-brand isolation and refusal paths,
NOT live platform access."""
import json
import shutil
import time

import httpx
import pytest

from clipengine import connections as cx
from clipengine.db import Database
from clipengine.publishing import TokenStore
from clipengine.publishing.base import PublishError
from clipengine.publishing.delivery import clip_brand, deliver_draft
from clipengine.publishing.facebook import FacebookClient
from clipengine.publishing.youtube import YouTubeClient


@pytest.fixture
def store(tmp_path):
    return TokenStore(tmp_path / "secrets", use_keyring=False)


@pytest.fixture
def db(settings):
    d = Database(settings.db_path)
    cx.record_directory(d)
    return d


def _acct(db, platform, brand):
    return db.one("SELECT * FROM social_accounts WHERE platform=? AND brand=?", (platform, brand))


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def _fb(store, handler):
    return FacebookClient("APPID", "APPSECRET", "http://localhost/cb", store, http=_client(handler))


def _pages(db, *brands, wrong_ig_for=None):
    out = []
    for b in brands:
        fb, ig = _acct(db, "facebook", b), _acct(db, "instagram", b)
        p = {"id": fb["platform_id"], "name": b, "access_token": f"PAGETOKEN-{b}",
             "instagram_business_account": {"id": ig["platform_id"], "username": ig["handle"]}}
        if b == wrong_ig_for:
            p["instagram_business_account"] = {"id": "17841000000000000", "username": "someoneelse"}
        out.append(p)
    return out


def test_facebook_login_url_asks_for_page_scopes_and_needs_config(store):
    c, st = _fb(store, lambda r: httpx.Response(404)).authorize_url()
    assert "pages_manage_posts" in c and "state=" in c and "instagram_content_publish" not in c
    with pytest.raises(Exception, match="META_APP_ID"):
        FacebookClient("", "", "", store).authorize_url()


def test_one_login_adopts_each_brand_only_on_exact_page_and_instagram_id(db, store):
    pages = _pages(db, "afterlive", "america_first_monitor", wrong_ig_for="afterlive")
    pages.append({"id": "999", "name": "A stranger's page", "access_token": "STRANGER"})

    def handler(r):
        u = str(r.url)
        if "oauth/access_token" in u:
            return httpx.Response(200, json={"access_token": "USERTOKEN"})
        if "me/accounts" in u:
            return httpx.Response(200, json={"data": pages})
        return httpx.Response(404)
    c = _fb(store, handler)
    c.exchange_code("CODE")
    res = {(r["brand"], r["platform"]): r["state"] for r in c.adopt_pages(db)}
    assert res[("afterlive", "facebook")] == "VERIFIED" and res[("america_first_monitor", "facebook")] == "VERIFIED"
    assert res[("america_first_monitor", "instagram")] == "VERIFIED"
    assert res[("afterlive", "instagram")] == "MISMATCH"                          # linked IG is not Afterlive's recorded id
    assert store.get("facebook:afterlive")["page_id"] == _acct(db, "facebook", "afterlive")["platform_id"]
    assert store.get("instagram:afterlive") is None and store.get("instagram:america_first_monitor")
    assert store.get("facebook:operator_notes") is None                          # not managed by this login: nothing stored
    assert "STRANGER" not in json.dumps([store.get(f"facebook:{b}") for b in ("afterlive", "america_first_monitor")])
    assert _acct(db, "facebook", "operator_notes")["identity_verified_at"] is None


def test_draft_reel_uses_video_state_draft_and_never_published(db, store, tmp_path):
    cx.verify_identity(db, _acct(db, "facebook", "afterlive")["id"], [{"id": _acct(db, "facebook", "afterlive")["platform_id"]}])
    fb = _acct(db, "facebook", "afterlive")
    store.set("facebook:afterlive", {"access_token": "PT", "page_id": fb["platform_id"]})
    seen = []

    def handler(r):
        seen.append((r.method, str(r.url), r.content.decode(errors="ignore")[:200]))
        if "video_reels" in str(r.url) and b"start" in r.content:
            return httpx.Response(200, json={"video_id": "V1", "upload_url": "https://rupload.facebook.com/video-upload/V1"})
        if "rupload" in str(r.url):
            assert r.headers["authorization"] == "OAuth PT" and r.headers["file_size"] == "5"
            return httpx.Response(200, json={"success": True})
        if "video_reels" in str(r.url):
            return httpx.Response(200, json={"success": True})
        return httpx.Response(404)
    v = tmp_path / "c.mp4"
    v.write_bytes(b"12345")
    out = _fb(store, handler).create_draft_reel(db, fb["id"], v, "hello")
    assert out["state"] == "DRAFT"
    bodies = " ".join(s[2] for s in seen)
    assert "video_state=DRAFT" in bodies and "PUBLISHED" not in bodies and "SCHEDULED" not in bodies


def test_draft_reel_refuses_a_token_that_is_not_the_verified_page(db, store, tmp_path):
    fb = _acct(db, "facebook", "afterlive")
    cx.verify_identity(db, fb["id"], [{"id": fb["platform_id"]}])
    store.set("facebook:afterlive", {"access_token": "PT", "page_id": "SOME-OTHER-PAGE"})
    v = tmp_path / "c.mp4"
    v.write_bytes(b"1")
    with pytest.raises(Exception, match="verified Page token"):
        _fb(store, lambda r: httpx.Response(500)).create_draft_reel(db, fb["id"], v, "")


def _yt(store, handler):
    return YouTubeClient("CID", "CSEC", store, http=_client(handler))


def _yt_token(store, brand):
    store.set(f"youtube:{brand}", {"access_token": "AT", "refresh_token": "RT", "expires_at": time.time() + 3600})


def test_youtube_verify_keeps_only_the_expected_channels_token(db, store):
    afterlive = _acct(db, "youtube", "afterlive")
    _yt_token(store, "afterlive")
    ok = _yt(store, lambda r: httpx.Response(200, json={"items": [{"id": afterlive["platform_id"], "snippet": {"title": "Afterlive"}}]}))
    assert ok.verify(db, afterlive["id"])["state"] == "VERIFIED" and store.get("youtube:afterlive")
    afm = _acct(db, "youtube", "america_first_monitor")
    _yt_token(store, "america_first_monitor")
    wrong = _yt(store, lambda r: httpx.Response(200, json={"items": [{"id": afterlive["platform_id"], "snippet": {"title": "Afterlive"}}]}))
    res = wrong.verify(db, afm["id"])                      # AFM's authorisation landed on Afterlive's channel
    assert res["state"] == "CROSS_BRAND" and store.get("youtube:america_first_monitor") is None
    assert _acct(db, "youtube", "america_first_monitor")["identity_verified_at"] is None


def test_youtube_upload_is_always_private_and_rechecks_the_channel(db, store, tmp_path):
    a = _acct(db, "youtube", "afterlive")
    cx.verify_identity(db, a["id"], [{"id": a["platform_id"]}])
    _yt_token(store, "afterlive")
    sent = {}

    def handler(r):
        u = str(r.url)
        if "youtube/v3/channels" in u:
            return httpx.Response(200, json={"items": [{"id": a["platform_id"]}]})
        if "upload/youtube" in u and r.method == "POST":
            sent["meta"] = json.loads(r.content)
            return httpx.Response(200, headers={"location": "https://upload.test/session"})
        if "upload.test" in u:
            return httpx.Response(200, json={"id": "VID1", "status": {"privacyStatus": "private"}})
        return httpx.Response(404)
    v = tmp_path / "c.mp4"
    v.write_bytes(b"123")
    out = _yt(store, handler).upload_private(db, a["id"], v, "T", "D")
    assert out == {"video_id": "VID1", "privacy": "private"} and sent["meta"]["status"]["privacyStatus"] == "private"

    def wrong_channel(r):
        return httpx.Response(200, json={"items": [{"id": "UC-someone-else"}]})
    with pytest.raises(PublishError, match="not this brand's verified channel"):
        _yt(store, wrong_channel).upload_private(db, a["id"], v, "T", "D")

    def goes_public(r):
        u = str(r.url)
        if "youtube/v3/channels" in u:
            return httpx.Response(200, json={"items": [{"id": a["platform_id"]}]})
        if "upload/youtube" in u:
            return httpx.Response(200, headers={"location": "https://upload.test/s"})
        return httpx.Response(200, json={"id": "VID2", "status": {"privacyStatus": "public"}})
    with pytest.raises(PublishError, match="did not report the video as private"):
        _yt(store, goes_public).upload_private(db, a["id"], v, "T", "D")


# ---- the delivery gate ----------------------------------------------------------------------------------------------

@pytest.fixture
def clip_env(settings, db, sample_video):
    now = time.time()
    db.execute("INSERT OR REPLACE INTO brands (key,name,niche_key,created_at,updated_at) VALUES (?,?,?,?,?)",
               ("afterlive", "Afterlive", "gaming_streams", now, now))
    cid_cr = db.insert("creators", name="Host", brand_key="afterlive")
    rid = db.insert("recordings", title="R", source_type="upload", status="ready", creator_id=cid_cr)
    d = settings.clips_dir / "c1"
    d.mkdir(parents=True)
    video = d / "clip.mp4"
    shutil.copy(sample_video, video)
    clip = db.insert("clips", recording_id=rid, idx=1, start=0, end=10, status="approved", rights_status="owned",
                     safety_status="clear", video_path=str(video), description="desc", title="Title")
    return clip


class _Fake:
    def __init__(self):
        self.calls = []

    def upload_private(self, db, account_id, video, title, description):
        self.calls.append("youtube")
        return {"video_id": "VID"}

    def create_draft_reel(self, db, account_id, video, description):
        self.calls.append("facebook")
        return {"video_id": "REEL"}


def test_delivery_is_blocked_until_every_gate_passes(db, clip_env):
    fake = _Fake()
    clients = {"youtube": fake, "facebook": fake}
    yt_afterlive, yt_afm = _acct(db, "youtube", "afterlive"), _acct(db, "youtube", "america_first_monitor")
    assert clip_brand(db, db.clip(clip_env)) == "afterlive"
    with pytest.raises(PublishError, match="identity has not been verified"):
        deliver_draft(db, clients, clip_env, yt_afterlive["id"], owner_command=True)
    cx.verify_identity(db, yt_afterlive["id"], [{"id": yt_afterlive["platform_id"]}])
    with pytest.raises(PublishError, match="not enabled"):
        deliver_draft(db, clients, clip_env, yt_afterlive["id"], owner_command=True)
    db.update("social_accounts", yt_afterlive["id"], publishing_enabled=1)
    with pytest.raises(PublishError, match="explicit owner command"):
        deliver_draft(db, clients, clip_env, yt_afterlive["id"], owner_command=False)
    cx.verify_identity(db, yt_afm["id"], [{"id": yt_afm["platform_id"]}])
    db.update("social_accounts", yt_afm["id"], publishing_enabled=1)
    with pytest.raises(PublishError, match="Brand mismatch"):                       # Afterlive clip -> AFM channel
        deliver_draft(db, clients, clip_env, yt_afm["id"], owner_command=True)
    assert fake.calls == []
    res = deliver_draft(db, clients, clip_env, yt_afterlive["id"], owner_command=True)
    assert res["status"] == "delivered_private" and fake.calls == ["youtube"]
    with pytest.raises(PublishError, match="already sent"):
        deliver_draft(db, clients, clip_env, yt_afterlive["id"], owner_command=True)


def test_delivery_refuses_instagram_and_unapproved_clips(db, clip_env):
    ig = _acct(db, "instagram", "afterlive")
    cx.verify_identity(db, ig["id"], [{"id": ig["platform_id"]}])
    db.update("social_accounts", ig["id"], publishing_enabled=1)
    with pytest.raises(PublishError, match="no draft API"):
        deliver_draft(db, {"youtube": _Fake(), "facebook": _Fake()}, clip_env, ig["id"], owner_command=True)
    fb = _acct(db, "facebook", "afterlive")
    cx.verify_identity(db, fb["id"], [{"id": fb["platform_id"]}])
    db.update("social_accounts", fb["id"], publishing_enabled=1)
    db.update("clips", clip_env, status="ready")
    with pytest.raises(PublishError, match="approved by the owner"):
        deliver_draft(db, {"youtube": _Fake(), "facebook": _Fake()}, clip_env, fb["id"], owner_command=True)
    db.update("clips", clip_env, status="approved", rights_status="unreviewed")
    with pytest.raises(PublishError, match="rights"):
        deliver_draft(db, {"youtube": _Fake(), "facebook": _Fake()}, clip_env, fb["id"], owner_command=True)
