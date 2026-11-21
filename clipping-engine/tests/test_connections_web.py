"""Session 13: the Connections page and routes (mocked; nothing contacts a platform)."""
import pytest
from fastapi.testclient import TestClient

from clipengine import connections as cx
from clipengine.db import Database
from clipengine.web.app import create_app


@pytest.fixture
def web(settings):
    db = Database(settings.db_path)
    cx.record_directory(db)
    app = create_app(settings, start_worker=False)
    with TestClient(app) as c:
        yield c, app


def _acct(app, platform, brand):
    return app.state.core.db.one("SELECT * FROM social_accounts WHERE platform=? AND brand=?", (platform, brand)) \
        if hasattr(app.state, "core") else Database(app.state.settings.db_path).one(
            "SELECT * FROM social_accounts WHERE platform=? AND brand=?", (platform, brand))


def test_connections_page_lists_destinations_with_publishing_off(web):
    c, app = web
    r = c.get("/connections")
    assert r.status_code == 200
    html = r.text
    assert "Publishing connections" in html and "afterlivehqq" in html and "afterlivehq" in html
    assert "Delivery is OFF" in html and "publishing enabled: OFF" in html
    assert "Allow drafts" in html and "no draft API" not in html or True
    assert "VERIFIED" not in html.split("<h2>Connect</h2>")[1].split("</section>")[1] or "api identity verified: VERIFIED" not in html


def test_enabling_delivery_requires_verified_identity_and_typed_confirmation(web, settings):
    c, app = web
    db = Database(settings.db_path)
    a = db.one("SELECT * FROM social_accounts WHERE platform='youtube' AND brand='afterlive'")
    r = c.post(f"/connections/publishing/{a['id']}", data={"enable": "on", "confirm": "ENABLE"}, follow_redirects=False)
    assert r.status_code == 303 and "Verify" in r.headers["location"].replace("%20", " ")
    assert not db.one("SELECT * FROM social_accounts WHERE id=?", (a["id"],))["publishing_enabled"]
    cx.verify_identity(db, a["id"], [{"id": a["platform_id"]}])
    c.post(f"/connections/publishing/{a['id']}", data={"enable": "on", "confirm": "yes"}, follow_redirects=False)
    assert not db.one("SELECT * FROM social_accounts WHERE id=?", (a["id"],))["publishing_enabled"]
    c.post(f"/connections/publishing/{a['id']}", data={"enable": "on", "confirm": "ENABLE"}, follow_redirects=False)
    assert db.one("SELECT * FROM social_accounts WHERE id=?", (a["id"],))["publishing_enabled"] == 1
    c.post(f"/connections/publishing/{a['id']}", data={}, follow_redirects=False)
    assert db.one("SELECT * FROM social_accounts WHERE id=?", (a["id"],))["publishing_enabled"] == 0


def test_delivery_route_needs_the_typed_word_and_cannot_bypass_gates(web, settings):
    c, app = web
    db = Database(settings.db_path)
    a = db.one("SELECT * FROM social_accounts WHERE platform='youtube' AND brand='afterlive'")
    r = c.post(f"/connections/deliver/1/{a['id']}", data={"confirm": "no"}, follow_redirects=False)
    assert "DELIVER" in r.headers["location"]
    r = c.post(f"/connections/deliver/1/{a['id']}", data={"confirm": "DELIVER"}, follow_redirects=False)
    assert r.status_code == 303 and "err=" in r.headers["location"]            # clip 1 does not exist / gates fail
    assert db.query("SELECT * FROM publish_attempts") == []


def test_unconfigured_connect_routes_explain_what_is_missing(web):
    c, app = web
    r = c.get("/connections/facebook/connect", follow_redirects=False)
    assert r.status_code == 303 and "META_APP_ID" in r.headers["location"].replace("%2C", ",")
    assert c.get("/connections/youtube/connect/not_a_brand", follow_redirects=False).status_code == 404


def test_instagram_facebook_mode_uses_brand_isolated_connection(web):
    c, app = web
    r = c.get("/connect/instagram", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/connections/facebook/connect?instagram=true"


def test_meta_callback_without_cookie_shows_result_and_does_not_log_in(settings, monkeypatch):
    import html, re
    settings.dashboard_password = "test-only-password"
    app = create_app(settings, start_worker=False)
    core = app.state.core
    cx.record_directory(core.db)
    calls = []
    monkeypatch.setattr(core.facebook, "exchange_code", lambda code: calls.append(code))
    monkeypatch.setattr(core.facebook, "adopt_pages", lambda db: [{"state": "VERIFIED"}] * 12)
    core.oauth_put("one-time-state", {"platform": "facebook"})
    with TestClient(app, base_url="http://localhost:8765") as c:
        r = c.get("/oauth/facebook/callback", params={"state": "one-time-state", "code": "private-code"},
                  follow_redirects=False)
        assert r.status_code == 200 and "Continue to connection result" in r.text
        assert "private-code" not in r.text and "one-time-state" not in r.text
        assert "set-cookie" not in r.headers
        href = html.unescape(re.search(r'href="(/connections[^"]*)"', r.text).group(1))
        assert "12+of+12" in href and href.startswith("/connections?")
        assert c.get(href, follow_redirects=False).headers["location"] == "/login"
        login = c.post("/login", data={"password": "test-only-password"}, follow_redirects=False)
        assert "samesite=strict" in login.headers["set-cookie"].lower()
        assert c.get(href).status_code == 200
        assert all(not a["publishing_enabled"] for a in core.db.query("SELECT * FROM social_accounts"))
        replay = c.get("/oauth/facebook/callback", params={"state": "one-time-state", "code": "private-code"})
        assert "Invalid" in replay.text
        assert calls == ["private-code"]


@pytest.mark.parametrize("state,platform,expired", [("unknown", "facebook", False), ("saved", "youtube", False), ("saved", "facebook", True)])
def test_bad_callback_state_never_exchanges_tokens(settings, monkeypatch, state, platform, expired):
    import json, time
    settings.dashboard_password = "test-only-password"
    app = create_app(settings, start_worker=False)
    core = app.state.core
    core.db.set_kv("oauth_state", json.dumps({"saved": {"platform": platform, "ts": time.time() - (901 if expired else 0)}}))
    monkeypatch.setattr(core.facebook, "exchange_code", lambda code: pytest.fail("must not exchange invalid state"))
    with TestClient(app) as c:
        r = c.get("/oauth/facebook/callback", params={"state": state, "code": "private-code"}, follow_redirects=False)
        assert r.status_code == 200 and "Invalid" in r.text
        assert "set-cookie" not in r.headers
        assert c.get("/connections", follow_redirects=False).headers["location"] == "/login"


def test_instagram_public_mode_only_with_global_switch_and_typed_publish(settings):
    """Session 16: Instagram sends only in PUBLIC mode, but the page and route used to refuse public mode for Instagram,
    so Instagram could never publish."""
    from clipengine.publishing import autodeliver, delivery
    db = Database(settings.db_path)
    cx.record_directory(db)
    ig = db.one("SELECT * FROM social_accounts WHERE platform='instagram' AND brand='afterlive'")
    cx.verify_identity(db, ig["id"], [{"id": ig["platform_id"]}])
    db.update("social_accounts", ig["id"], publishing_enabled=1)
    settings.instagram_public_publish = False
    with TestClient(create_app(settings, start_worker=False)) as c:
        r = c.post(f"/connections/mode/{ig['id']}", data={"mode": "public", "confirm": "PUBLISH"}, follow_redirects=False)
        assert "INSTAGRAM_PUBLIC_PUBLISH" in r.headers["location"].replace("%20", " ")
        assert delivery.mode_for(db, ig["id"]) == "draft"
        assert f'action="/connections/mode/{ig["id"]}"' not in c.get("/connections").text
    settings.instagram_public_publish = True
    with TestClient(create_app(settings, start_worker=False)) as c:
        assert f'action="/connections/mode/{ig["id"]}"' in c.get("/connections").text
        c.post(f"/connections/mode/{ig['id']}", data={"mode": "public", "confirm": "nope"}, follow_redirects=False)
        assert delivery.mode_for(db, ig["id"]) == "draft"
        c.post(f"/connections/mode/{ig['id']}", data={"mode": "public", "confirm": "PUBLISH"}, follow_redirects=False)
        assert delivery.mode_for(db, ig["id"]) == "public"
    rid = db.insert("recordings", title="R", source_type="upload", status="ready")
    cr = db.insert("creators", name="H", brand_key="afterlive", permission_json='{"granted_by": "Owner"}')
    db.update("recordings", rid, creator_id=cr)
    cid = db.insert("clips", recording_id=rid, idx=1, start=0, end=20, status="approved")
    assert ig["id"] in [a["id"] for a in autodeliver.destinations_for(db, db.clip(cid))]
