"""Publishing adapter and service tests.

ALL TikTok/Instagram HTTP here is MOCKED (httpx.MockTransport). These tests verify request
construction, state handling, resumption and duplicate protection — NOT live platform access.
"""
import hashlib
import json
import re
import shutil
import time

import httpx
import pytest

from clipengine.db import Database
from clipengine.jobs import JobQueue, TransientError
from clipengine.publishing import (InstagramClient, NotConfigured, PublishError, TikTokClient, TokenStore,
                                   editorial_gate, validate_video)
from clipengine.publishing.service import PublishService
from clipengine.publishing.tiktok import chunk_plan
from conftest import needs_ffmpeg


@pytest.fixture
def store(tmp_path):
    return TokenStore(tmp_path / "secrets", use_keyring=False)


def test_token_store_file_fallback(store):
    store.set("x", {"a": 1})
    assert store.get("x") == {"a": 1}
    store.delete("x")
    assert store.get("x") is None


def test_chunk_plan():
    assert chunk_plan(1000) == (1000, 1)
    assert chunk_plan(23 * 1024 * 1024) == (10 * 1024 * 1024, 2)
    with pytest.raises(PublishError):
        chunk_plan(0)


def test_tiktok_pkce_desktop_hex_and_web_none(store):
    url, st = TikTokClient("k", "s", "http://127.0.0.1:8765/oauth/tiktok/callback", store).authorize_url()
    challenge = re.search(r"code_challenge=([0-9a-f]+)", url).group(1)
    assert len(challenge) == 64 and challenge == hashlib.sha256(st["verifier"].encode()).hexdigest()
    assert "video.upload" in url and "code_challenge_method=S256" in url
    url, st = TikTokClient("k", "s", "https://x.example/cb", store, app_type="web").authorize_url()
    assert "code_challenge" not in url and "verifier" not in st


def test_tiktok_not_configured(store):
    with pytest.raises(NotConfigured):
        TikTokClient("", "", "", store).authorize_url()
    with pytest.raises(NotConfigured):
        TikTokClient("k", "s", "http://localhost/cb", store).access_token()


class TikTokMock:
    """Mocked TikTok API. statuses: queue of status values returned by status/fetch."""

    def __init__(self, statuses, init_error=None):
        self.statuses, self.init_error, self.seen, self.inits = list(statuses), init_error, [], 0

    def __call__(self, req: httpx.Request):
        self.seen.append(req)
        if req.url.path.endswith("/oauth/token/"):
            return httpx.Response(200, json={"access_token": "AT2", "refresh_token": "RT", "open_id": "u",
                                             "scope": "user.info.basic,video.upload", "expires_in": 86400})
        if req.url.path.endswith("/inbox/video/init/"):
            self.inits += 1
            if self.init_error:
                code, http = self.init_error.pop(0) if isinstance(self.init_error, list) else self.init_error
                return httpx.Response(http, json={"error": {"code": code, "message": "m", "log_id": "L"}})
            return httpx.Response(200, json={"data": {"publish_id": f"pub{self.inits}",
                                                      "upload_url": "https://up.example/u"}, "error": {"code": "ok"}})
        if req.url.host == "up.example":
            return httpx.Response(201)
        if req.url.path.endswith("/status/fetch/"):
            st = self.statuses.pop(0) if self.statuses else "PROCESSING_UPLOAD"
            return httpx.Response(200, json={"data": {"status": st}, "error": {"code": "ok"}})
        return httpx.Response(404)


def tt_client(store, mock):
    return TikTokClient("k", "s", "http://127.0.0.1:8765/oauth/tiktok/callback", store,
                        http=httpx.Client(transport=httpx.MockTransport(mock)), sleep=lambda s: None)


@needs_ffmpeg
def test_tiktok_inbox_flow_mocked(store, sample_video):
    mock = TikTokMock(["PROCESSING_UPLOAD", "SEND_TO_USER_INBOX"])
    c = tt_client(store, mock)
    url, st = c.authorize_url()
    c.exchange_code("CODE", st["verifier"])
    tok_req = next(r for r in mock.seen if r.url.path.endswith("/oauth/token/"))
    assert b"code_verifier=" in tok_req.content
    res = c.upload_to_inbox(sample_video)
    assert res.confirmed and res.status == "delivered_inbox" and res.remote_id == "pub1"
    init = next(r for r in mock.seen if r.url.path.endswith("/inbox/video/init/"))
    size = sample_video.stat().st_size
    chunk, total = chunk_plan(size)   # ≥5 MB: 10 MB chunks, final chunk absorbs the remainder
    assert json.loads(init.content)["source_info"] == {"source": "FILE_UPLOAD", "video_size": size,
                                                       "chunk_size": chunk, "total_chunk_count": total}
    put = next(r for r in mock.seen if r.url.host == "up.example")
    assert put.headers["content-range"] == f"bytes 0-{size - 1}/{size}"


@needs_ffmpeg
def test_tiktok_error_mapping(store, sample_video):
    store.set("tiktok", {"access_token": "AT", "refresh_token": "RT", "expires_at": 9e12, "scope": "video.upload"})
    with pytest.raises(TransientError):
        tt_client(store, TikTokMock([], init_error=("rate_limit_exceeded", 429))).init_upload(sample_video)
    with pytest.raises(PublishError, match="pending"):
        tt_client(store, TikTokMock([], init_error=("spam_risk_too_many_pending_share", 403))).init_upload(sample_video)
    # access_token_invalid → refresh once, then succeed
    mock = TikTokMock([], init_error=[("access_token_invalid", 401)])
    c = tt_client(store, mock)
    c.init_error = None
    pid, _ = c.init_upload(sample_video)
    assert pid and any(r.url.path.endswith("/oauth/token/") for r in mock.seen)
    assert store.get("tiktok")["access_token"] == "AT2"


def test_validate_video_rejects_bad_files(sample_video, tmp_path):
    validate_video(sample_video, min_s=3, max_s=600, max_bytes=10 ** 9)
    with pytest.raises(PublishError, match="accepts"):
        validate_video(sample_video, min_s=3, max_s=10, max_bytes=10 ** 9)
    with pytest.raises(PublishError, match="limit"):
        validate_video(sample_video, min_s=3, max_s=600, max_bytes=1000)


# ---- publishing service (job-based) -----------------------------------------

@pytest.fixture
def svc_env(settings, store, sample_video):
    db = Database(settings.db_path)
    q = JobQueue(db)
    rid = db.insert("recordings", title="T", source_type="upload", status="ready")
    clipdir = settings.clips_dir / "c1"
    clipdir.mkdir(parents=True)
    video = clipdir / "clip.mp4"
    shutil.copy(sample_video, video)
    cid = db.insert("clips", recording_id=rid, idx=1, start=0, end=10, status="approved", rights_status="owned",
                    video_path=str(video), description="desc")
    return settings, db, q, cid


def make_svc(settings, db, q, store, tt_mock=None, ig_mock=None, ig_mode="facebook"):
    tt = tt_client(store, tt_mock or TikTokMock([]))
    ig = InstagramClient("app", "sec", "http://localhost/cb", store,
                         http=httpx.Client(transport=httpx.MockTransport(ig_mock or IgMock())), sleep=lambda s: None,
                         mode=ig_mode, public_base_url="https://pc.tailnet.ts.net")
    return PublishService(settings, db, q, tt, ig, sleep=lambda s: None)


def attempt(db, aid):
    return db.one("SELECT * FROM publish_attempts WHERE id=?", (aid,))


def test_publish_requires_gate_connection_and_blocks_duplicates(svc_env, store):
    settings, db, q, cid = svc_env
    svc = make_svc(settings, db, q, store)
    with pytest.raises(NotConfigured):
        svc.request(cid, "tiktok")
    assert svc.summary(db.clip(cid))["tiktok"]["status"] == "awaiting_connection"
    store.set("tiktok", {"access_token": "AT", "expires_at": 9e12, "scope": "video.upload"})
    db.update("clips", cid, rights_status="unreviewed")
    with pytest.raises(PublishError, match="rights"):
        svc.request(cid, "tiktok")
    db.update("clips", cid, rights_status="owned")
    aid = svc.request(cid, "tiktok")
    with pytest.raises(PublishError, match="in progress"):
        svc.request(cid, "tiktok")
    assert attempt(db, aid)["status"] == "queued"


def test_tiktok_job_delivers_and_never_reuploads_on_resume(svc_env, store):
    settings, db, q, cid = svc_env
    store.set("tiktok", {"access_token": "AT", "expires_at": 9e12, "scope": "video.upload"})
    mock = TikTokMock(["PROCESSING_UPLOAD"] * 40 + ["SEND_TO_USER_INBOX"])
    svc = make_svc(settings, db, q, store, tt_mock=mock)
    aid = svc.request(cid, "tiktok")
    q.run_one()                        # uploads, then TikTok still processing → retrying
    a = attempt(db, aid)
    assert a["status"] == "retrying" and a["stage"] == "uploaded" and a["remote_id"] == "pub1"
    q.db.execute("UPDATE jobs SET run_after = 0")
    q.run_one()                        # resumes by polling only
    a = attempt(db, aid)
    assert a["status"] == "delivered_inbox" and a["confirmed_at"]
    assert mock.inits == 1 and sum(r.url.host == "up.example" for r in mock.seen) == 1
    with pytest.raises(PublishError, match="already sent"):
        svc.request(cid, "tiktok")
    assert svc.request(cid, "tiktok", again=True) != aid


def test_tiktok_failure_is_reported(svc_env, store):
    settings, db, q, cid = svc_env
    store.set("tiktok", {"access_token": "AT", "expires_at": 9e12, "scope": "video.upload"})
    svc = make_svc(settings, db, q, store, tt_mock=TikTokMock(["FAILED"]))
    aid = svc.request(cid, "tiktok")
    q.run_one()
    a = attempt(db, aid)
    assert a["status"] == "failed" and "FAILED" in a["detail"] and not a["confirmed_at"]


class IgMock:
    def __init__(self, statuses=("IN_PROGRESS", "FINISHED"), container_after_publish="PUBLISHED", fail_publish=False):
        self.statuses, self.after, self.fail_publish = list(statuses), container_after_publish, fail_publish
        self.seen, self.published = [], 0

    def __call__(self, req):
        self.seen.append(req)
        p = req.url.path
        if p.endswith("/oauth/access_token") or p.endswith("/access_token"):
            return httpx.Response(200, json={"access_token": "LL", "expires_in": 5184000, "user_id": "IG9"})
        if p.endswith("/me/accounts"):
            return httpx.Response(200, json={"data": [{"instagram_business_account": {"id": "IG1", "username": "news"}}]})
        if p.endswith("/me"):
            return httpx.Response(200, json={"user_id": "IG9", "username": "creator"})
        if p.endswith("/media") and req.method == "POST":
            body = {"id": "C1"}
            if b"upload_type=resumable" in req.content:
                body["uri"] = "https://rupload.facebook.com/ig-api-upload/v23.0/C1"
            return httpx.Response(200, json=body)
        if req.url.host == "rupload.facebook.com":
            return httpx.Response(200, json={"success": True})
        if p.endswith("/media_publish"):
            if self.fail_publish:
                self.fail_publish = False
                raise httpx.ReadTimeout("lost response", request=req)
            self.published += 1
            return httpx.Response(200, json={"id": "M1"})
        if p.endswith("/C1"):
            st = self.statuses.pop(0) if self.statuses else (self.after if self.published or not self.fail_publish else "FINISHED")
            return httpx.Response(200, json={"status_code": st})
        if p.endswith("/M1"):
            return httpx.Response(200, json={"id": "M1", "permalink": "https://instagram.com/reel/x"})
        return httpx.Response(404, json={"error": {"message": "unexpected " + p, "code": 100}})


def test_instagram_facebook_mode_job_publishes_once(svc_env, store):
    settings, db, q, cid = svc_env
    mock = IgMock()
    svc = make_svc(settings, db, q, store, ig_mock=mock)
    svc.clients["instagram"].exchange_code("CODE")
    aid = svc.request(cid, "instagram")
    q.run_one()
    a = attempt(db, aid)
    assert a["status"] == "published" and a["remote_id"] == "M1" and a["confirmed_at"]
    assert "instagram.com/reel/x" in a["detail"]
    up = next(r for r in mock.seen if r.url.host == "rupload.facebook.com")
    assert up.headers["authorization"] == "OAuth LL" and up.headers["offset"] == "0"
    assert mock.published == 1


def test_instagram_lost_publish_response_is_not_published_twice(svc_env, store):
    settings, db, q, cid = svc_env
    mock = IgMock(statuses=("FINISHED",), container_after_publish="PUBLISHED", fail_publish=True)
    svc = make_svc(settings, db, q, store, ig_mock=mock)
    svc.clients["instagram"].exchange_code("CODE")
    aid = svc.request(cid, "instagram")
    q.run_one()                                    # publish call times out → retrying, stage publish_requested
    a = attempt(db, aid)
    assert a["status"] == "retrying" and a["stage"] == "publish_requested"
    mock.published = 1                             # Meta actually published it despite the lost response
    q.db.execute("UPDATE jobs SET run_after = 0")
    q.run_one()
    a = attempt(db, aid)
    assert a["status"] == "published" and "not published twice" in a["detail"]
    assert sum(r.url.path.endswith("/media_publish") for r in mock.seen) == 1


def test_instagram_login_mode_uses_one_time_public_link(svc_env, store):
    settings, db, q, cid = svc_env
    settings.public_media_base_url = "https://pc.tailnet.ts.net"
    mock = IgMock(statuses=("FINISHED",))
    svc = make_svc(settings, db, q, store, ig_mock=mock, ig_mode="instagram")
    svc.clients["instagram"].exchange_code("CODE")
    assert store.get("instagram")["ig_user_id"] == "IG9"
    aid = svc.request(cid, "instagram")
    q.run_one()
    create = next(r for r in mock.seen if r.url.path.endswith("/IG9/media"))
    assert create.url.host == "graph.instagram.com" and b"video_url=https%3A%2F%2Fpc.tailnet.ts.net%2Fpublic%2Fmedia%2F" in create.content
    assert attempt(db, aid)["status"] == "published"
    assert db.one("SELECT COUNT(*) n FROM public_media_tokens")["n"] == 0   # revoked after publishing


def test_editorial_gate():
    clip = {"status": "ready", "rights_status": "unreviewed", "safety_status": "clear", "video_path": None}
    assert len(editorial_gate(clip)) == 3
