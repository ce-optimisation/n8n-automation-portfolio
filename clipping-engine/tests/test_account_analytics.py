import httpx
from fastapi.testclient import TestClient
from clipengine import account_analytics as analytics
from clipengine.web.app import create_app


def test_limited_platform_is_queried_once(settings, monkeypatch):
    app = create_app(settings, start_worker=False)
    db = app.state.core.db
    db.execute("UPDATE social_accounts SET identity_verified_at=1 WHERE platform='instagram'")
    calls = []
    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(429)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(analytics.httpx, "Client", lambda **kwargs: client)
    class Tokens:
        def get(self, key):
            return {"access_token": "fixture"}
    analytics.collect(db, None, Tokens(), "v23.0")
    analytics.collect(db, None, Tokens(), "v23.0")
    assert len(calls) == 1
    rows = db.query("SELECT * FROM account_snapshots")
    assert len(rows) == 6 and all(r["audience"] is None and r["error"] for r in rows)


def test_filtered_analytics_trends(settings):
    app = create_app(settings, start_worker=False)
    db = app.state.core.db
    aid = db.one("SELECT id FROM social_accounts WHERE platform='youtube' AND brand='afterlive'")["id"]
    now = 1800000000
    for days, audience in [(30,3),(7,7),(0,11)]:
        db.insert("account_snapshots",account_id=aid,collected_at=now-days*86400,audience=audience)
    with TestClient(app) as client:
        response = client.get("/analytics?brand=afterlive&platform=youtube")
        assert response.status_code == 200
        assert "4 / 8" in response.text and "Unavailable" in response.text
