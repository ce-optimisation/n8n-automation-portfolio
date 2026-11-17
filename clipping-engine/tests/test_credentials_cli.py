import io

import httpx

from clipengine import credentials_cli as cc
from clipengine.config import get_settings


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_authentication_test_reports_status_without_secrets(settings):
    settings.youtube_api_key, settings.twitch_client_id, settings.twitch_client_secret = "KEY-SECRET-1", "tid", "TSECRET"
    settings.kick_client_id = settings.kick_client_secret = ""

    def handler(req):
        if "googleapis" in str(req.url):
            return httpx.Response(403, json={"error": {"message": "API not enabled"}})
        return httpx.Response(200, json={"access_token": "ZZTOKEN", "expires_in": 100})
    r = cc.authentication_test(settings, _client(handler))
    assert r["youtube"]["state"] == "failed" and "403" in r["youtube"]["detail"]
    assert r["twitch"]["state"] == "ok" and r["kick"]["state"] == "skipped"
    blob = str(r)
    assert "KEY-SECRET-1" not in blob and "TSECRET" not in blob and "ZZTOKEN" not in blob


def test_network_errors_never_leak_the_key_in_text(settings):
    settings.youtube_api_key = "KEY-SECRET-2"

    def handler(req):
        raise httpx.ConnectError("failed for " + str(req.url))
    r = cc.authentication_test(settings, _client(handler))
    assert r["youtube"]["state"] == "failed" and "KEY-SECRET-2" not in str(r)


def test_entry_writes_only_given_keys_hidden_and_keeps_the_rest(settings, monkeypatch):
    env = settings.env_file
    env.write_text("OTHER=keep\nYOUTUBE_API_KEY=\n", encoding="utf-8")
    answers = iter(["NEW-KEY-123", "", "", "", ""])
    out = io.StringIO()
    monkeypatch.setattr(cc, "get_settings", lambda: get_settings())
    cc.main(settings, prompt=lambda p: next(answers), out=out, client=_client(lambda r: httpx.Response(200, json={"items": [{"id": "x"}]})))
    text = env.read_text(encoding="utf-8")
    assert "OTHER=keep" in text and "YOUTUBE_API_KEY=NEW-KEY-123" in text
    assert "NEW-KEY-123" not in out.getvalue()
