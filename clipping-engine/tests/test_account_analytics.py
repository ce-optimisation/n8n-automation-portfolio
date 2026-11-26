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


def test_read_only_analytics_authorisation_routes(settings):
    app = create_app(settings, start_worker=False)
    core = app.state.core
    core.youtube.client_id, core.youtube.client_secret = "google-id", "google-secret"
    core.facebook.app_id, core.facebook.app_secret, core.facebook.redirect_uri = "meta-id", "meta-secret", "https://example.test/callback"
    core.db.execute("UPDATE social_accounts SET identity_verified_at=1 WHERE platform='youtube' AND brand='afterlive'")
    with TestClient(app, follow_redirects=False) as client:
        youtube = client.get("/connections/youtube/analytics/afterlive")
        meta = client.get("/connections/facebook/connect?analytics=true")
    youtube_scopes = httpx.URL(youtube.headers["location"]).params["scope"].split()
    meta_scopes = httpx.URL(meta.headers["location"]).params["scope"].split(",")
    assert youtube.status_code == meta.status_code == 303
    assert {scope.rsplit("/", 1)[-1] for scope in youtube_scopes} == {"youtube.readonly", "yt-analytics.readonly"}
    assert set(meta_scopes) == {"pages_show_list", "pages_read_engagement", "read_insights",
                                "instagram_basic", "instagram_manage_insights"}


def test_report_collectors_keep_zero_and_omitted_metrics_distinct():
    def handler(request):
        assert request.headers["Authorization"] == "Bearer read-only"
        if request.url.host == "youtubeanalytics.googleapis.com":
            assert request.url.params["ids"] == "channel==MINE"
            return httpx.Response(200, json={"columnHeaders": [{"name": "day"}, {"name": "views"}],
                                             "rows": [["2026-11-22", 0]]})
        assert request.url.params["metric_type"] == "total_value"
        return httpx.Response(200, json={"data": [{"name": "views", "total_value": {"value": 0}},
                                                 {"name": "reach"}]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert analytics.youtube_report(client, "read-only", 1795540000) == [{"day": "2026-11-22", "views": 0}]
        report = analytics.meta_report(client, "instagram", "123", "read-only", "v23.0", 1795540000)
    assert report["values"] == {"7": {"views": 0}, "30": {"views": 0}}


def test_meta_analytics_callback_is_separate_from_publishing(settings, monkeypatch):
    settings.meta_analytics_redirect_uri = "https://private.example.test/oauth/facebook/callback"
    app = create_app(settings, start_worker=False)
    core = app.state.core
    core.facebook.app_id = "meta-id"
    core.facebook.app_secret = "fixture"
    core.facebook.redirect_uri = "http://localhost:8765/oauth/facebook/callback"
    exchanges = []

    def connect_analytics(db, code, redirect):
        exchanges.append((code, redirect))
        return 1

    monkeypatch.setattr(core.facebook, "connect_analytics", connect_analytics)
    with TestClient(app, follow_redirects=False) as client:
        start = client.get("/connections/facebook/connect?analytics=true")
        regular = client.get("/connections/facebook/connect")
        analytics_params = httpx.URL(start.headers["location"]).params
        regular_params = httpx.URL(regular.headers["location"]).params
        assert analytics_params["redirect_uri"] == settings.meta_analytics_redirect_uri
        assert regular_params["redirect_uri"] == core.facebook.redirect_uri
        response = client.get("/oauth/facebook/callback", params={"state": analytics_params["state"], "code": "fixture"})
        assert response.status_code in (200, 303)
    assert exchanges == [("fixture", settings.meta_analytics_redirect_uri)]


def test_commercial_summary_keeps_currencies_and_estimates_separate(settings):
    app = create_app(settings, start_worker=False)
    db = app.state.core.db
    with TestClient(app) as client:
        empty = client.get("/analytics")
        assert empty.status_code == 200
        assert "No realised money recorded." in empty.text
        for kind, amount, currency, realised in (("revenue", 10, "GBP", 1), ("cost", 2, "GBP", 1),
                                                  ("sponsor", 5, "USD", 1), ("revenue", 100, "USD", 0),
                                                  ("affiliate_click", 1_000, "GBP", 1)):
            db.insert("commercial_entries", kind=kind, amount=amount, currency=currency,
                      realised=realised, date="2026-11-25")
        page = client.get("/analytics")
        assert page.status_code == 200
        assert "<b>GBP 8.00</b>" in page.text
        assert "<b>USD 5.00</b>" in page.text
        assert "<b>USD 105.00</b>" not in page.text
