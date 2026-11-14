"""Publishing adapter tests. ALL HTTP here is MOCKED (httpx.MockTransport):
they verify request construction and status handling, not live platform access."""
import json

import httpx
import pytest

from clipengine.publishing import (InstagramClient, NotConfigured, PublishError, TikTokClient, TokenStore,
                                   editorial_gate)
from clipengine.publishing.tiktok import chunk_plan


@pytest.fixture
def store(tmp_path):
    return TokenStore(tmp_path / "secrets", use_keyring=False)


def test_token_store_file_fallback(store, tmp_path):
    store.set("x", {"a": 1})
    assert store.get("x") == {"a": 1}
    store.delete("x")
    assert store.get("x") is None


def test_chunk_plan():
    assert chunk_plan(1000) == (1000, 1)
    size = 23 * 1024 * 1024
    chunk, total = chunk_plan(size)
    assert chunk == 10 * 1024 * 1024 and total == 2
    with pytest.raises(PublishError):
        chunk_plan(0)


def test_tiktok_not_configured(store):
    c = TikTokClient("", "", "", store)
    with pytest.raises(NotConfigured):
        c.authorize_url()
    with pytest.raises(NotConfigured):
        TikTokClient("k", "s", "http://localhost/cb", store).access_token()


def tiktok_transport(statuses, seen):
    def handler(req: httpx.Request):
        seen.append(req)
        if req.url.path.endswith("/oauth/token/"):
            return httpx.Response(200, json={"access_token": "AT", "refresh_token": "RT", "open_id": "u",
                                             "scope": "user.info.basic,video.upload", "expires_in": 86400})
        if req.url.path.endswith("/inbox/video/init/"):
            return httpx.Response(200, json={"data": {"publish_id": "pub1", "upload_url": "https://up.example/u"},
                                             "error": {"code": "ok"}})
        if req.url.host == "up.example":
            return httpx.Response(201)
        if req.url.path.endswith("/status/fetch/"):
            return httpx.Response(200, json={"data": {"status": statuses.pop(0)}, "error": {"code": "ok"}})
        return httpx.Response(404)
    return httpx.MockTransport(handler)


def test_tiktok_inbox_flow_mocked(store, tmp_path):
    seen = []
    c = TikTokClient("k", "s", "http://localhost:8765/oauth/tiktok/callback", store,
                     http=httpx.Client(transport=tiktok_transport(["PROCESSING_UPLOAD", "SEND_TO_USER_INBOX"], seen)),
                     sleep=lambda s: None)
    url, st = c.authorize_url()
    assert "video.upload" in url and st["state"] in url
    c.exchange_code("CODE", st["verifier"])
    video = tmp_path / "v.mp4"
    video.write_bytes(b"\x00" * 2048)
    res = c.upload_to_inbox(video)
    assert res.confirmed and res.remote_id == "pub1"
    init = next(r for r in seen if r.url.path.endswith("/inbox/video/init/"))
    body = json.loads(init.content)
    assert body["source_info"] == {"source": "FILE_UPLOAD", "video_size": 2048, "chunk_size": 2048,
                                   "total_chunk_count": 1}
    assert init.headers["authorization"] == "Bearer AT"
    put = next(r for r in seen if r.url.host == "up.example")
    assert put.headers["content-range"] == "bytes 0-2047/2048"


def test_tiktok_never_claims_success_without_confirmation(store, tmp_path):
    seen = []
    store.set("tiktok", {"access_token": "AT", "expires_at": 9e12, "scope": "video.upload"})
    c = TikTokClient("k", "s", "r", store, sleep=lambda s: None,
                     http=httpx.Client(transport=tiktok_transport(["PROCESSING_UPLOAD"] * 3, seen)))
    video = tmp_path / "v.mp4"
    video.write_bytes(b"\x00" * 10)
    res = c.upload_to_inbox(video, max_polls=3)
    assert not res.confirmed and res.status == "uploaded"
    c2 = TikTokClient("k", "s", "r", store, sleep=lambda s: None,
                      http=httpx.Client(transport=tiktok_transport(["FAILED"], [])))
    assert c2.upload_to_inbox(video).status == "failed"


def test_instagram_flow_mocked(store, tmp_path):
    seen = []

    def handler(req):
        seen.append(req)
        p = req.url.path
        if p.endswith("/oauth/access_token"):
            return httpx.Response(200, json={"access_token": "LL", "expires_in": 5184000})
        if p.endswith("/me/accounts"):
            return httpx.Response(200, json={"data": [{"instagram_business_account": {"id": "IG1", "username": "news"}}]})
        if p.endswith("/IG1/media") and req.method == "POST":
            return httpx.Response(200, json={"id": "C1", "uri": "https://rupload.facebook.com/ig-api-upload/v23.0/C1"})
        if req.url.host == "rupload.facebook.com":
            return httpx.Response(200, json={"success": True})
        if p.endswith("/C1"):
            return httpx.Response(200, json={"status_code": "FINISHED"})
        if p.endswith("/media_publish"):
            return httpx.Response(200, json={"id": "M1"})
        if p.endswith("/M1"):
            return httpx.Response(200, json={"id": "M1", "permalink": "https://instagram.com/reel/x"})
        return httpx.Response(404, json={"error": {"message": "unexpected " + p}})

    c = InstagramClient("app", "sec", "http://localhost/cb", store, http=httpx.Client(transport=httpx.MockTransport(handler)),
                        sleep=lambda s: None)
    c.exchange_code("CODE")
    assert c.status()["connected"] and c.status()["username"] == "news"
    v = tmp_path / "v.mp4"
    v.write_bytes(b"\x01" * 500)
    res = c.publish_reel(v, "caption")
    assert res.confirmed and res.remote_id == "M1"
    up = next(r for r in seen if r.url.host == "rupload.facebook.com")
    assert up.headers["authorization"] == "OAuth LL" and up.headers["file_size"] == "500"
    create = next(r for r in seen if r.url.path.endswith("/IG1/media"))
    assert b"media_type=REELS" in create.content and b"upload_type=resumable" in create.content


def test_editorial_gate():
    clip = {"status": "ready", "rights_status": "unreviewed", "safety_status": "clear", "video_path": "x"}
    assert len(editorial_gate(clip)) == 2
    clip.update(status="approved", rights_status="owned")
    assert editorial_gate(clip) == []
