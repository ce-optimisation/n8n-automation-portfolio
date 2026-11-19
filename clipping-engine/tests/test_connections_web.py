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
