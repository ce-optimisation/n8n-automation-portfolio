"""Session 13: destination connection states, strict identity verification and brand isolation.

ALL platform HTTP is mocked (httpx.MockTransport). These tests verify our logic, NOT live platform access."""
import json

import httpx
import pytest

from clipengine import connections as cx
from clipengine.db import Database


@pytest.fixture
def db(settings):
    return Database(settings.db_path)


def _acct(db, platform, brand):
    return db.one("SELECT * FROM social_accounts WHERE platform=? AND brand=?", (platform, brand))


def test_directory_has_the_nineteen_recorded_accounts():
    d = cx.load_directory()
    accts = d["accounts"]
    assert len(accts) == 19
    by_platform = {p: [a for a in accts if a["platform"] == p] for p in ("youtube", "facebook", "instagram", "tiktok")}
    assert [len(by_platform[p]) for p in ("youtube", "facebook", "instagram", "tiktok")] == [6, 6, 6, 1]
    assert by_platform["tiktok"][0]["brand"] == "afterlive"
    ids = [a["expected_id"] for a in accts if a["expected_id"]]
    assert len(ids) == len(set(ids)) == 18
    keys = set()
    for a in accts:
        keys |= set(a)
    assert not {"token", "secret", "password", "access_token"} & keys


def test_recording_the_directory_is_idempotent_and_verifies_nothing(db):
    first = cx.record_directory(db)
    second = cx.record_directory(db)
    assert first["total"] == 19 and second["created"] == 0
    rows = db.query("SELECT * FROM social_accounts")
    assert len(rows) == 19
    assert all(r["identity_verified_at"] is None and not r["publishing_enabled"] for r in rows)
    assert all(r["status"] == "awaiting_connection" for r in rows)


def test_recording_fills_placeholders_and_keeps_unconfirmed_tiktok_as_not_created(db):
    now = 1.0
    for brand in ("afterlive", "operator_notes"):
        for plat in ("youtube", "tiktok"):
            db.insert("social_accounts", platform=plat, handle=f"PENDING-{brand}-{plat}", brand=brand,
                      status="awaiting_connection", created_at=now, updated_at=now)
    cx.record_directory(db)
    assert _acct(db, "youtube", "afterlive")["handle"] == "afterlivehqq"
    assert _acct(db, "tiktok", "afterlive")["handle"] == "afterlivehq"
    assert _acct(db, "tiktok", "operator_notes")["handle"] == "PENDING-operator_notes-tiktok"   # not invented
    assert db.query("SELECT * FROM social_accounts WHERE platform='tiktok' AND brand!='afterlive' "
                    "AND handle NOT LIKE 'PENDING-%'") == []


def test_identity_match_is_exact_and_never_fuzzy():
    obs = [{"id": "UCaXNIlwujt0KRvmRxWlpzxA", "name": "Afterlive"}]
    assert cx.identity_match("UCaXNIlwujt0KRvmRxWlpzxA", [], obs)["state"] == "VERIFIED"
    assert cx.identity_match("UCaXNIlwujt0KRvmRxWlpzxB", [], obs)["state"] == "MISMATCH"            # one char off
    assert cx.identity_match("1412058561997498", ["61596520177525"], [{"id": "61596520177525"}])["state"] == "ALT_ID_ONLY"
    assert cx.identity_match("1", [], [])["state"] == "NONE_OBSERVED"
    assert cx.identity_match(None, [], [{"id": "x", "username": "AfterliveHQ"}], "afterlivehq")["state"] == "HANDLE_ONLY"
    assert cx.identity_match("17841452912255728", [], [{"id": "999", "name": "afterlivehq"}], "afterlivehq")["state"] == "MISMATCH"


def test_verify_identity_sets_verified_only_on_exact_id(db):
    cx.record_directory(db)
    a = _acct(db, "youtube", "afterlive")
    assert cx.verify_identity(db, a["id"], [{"id": "UC-wrong"}])["state"] == "MISMATCH"
    assert _acct(db, "youtube", "afterlive")["identity_verified_at"] is None
    assert cx.verify_identity(db, a["id"], [{"id": "UCaXNIlwujt0KRvmRxWlpzxA"}])["state"] == "VERIFIED"
    row = _acct(db, "youtube", "afterlive")
    assert row["verified_platform_id"] == "UCaXNIlwujt0KRvmRxWlpzxA" and row["identity_verified_at"]
    assert len(json.loads(row["evidence_json"])["identity_checks"]) == 2          # both attempts kept as evidence


def test_shared_admin_login_cannot_verify_one_brand_as_another(db):
    """One Google login manages all six channels. Authorising it as Afterlive must not verify AFM's destination."""
    cx.record_directory(db)
    afterlive = _acct(db, "youtube", "afterlive")
    afm_channel = _acct(db, "youtube", "america_first_monitor")["platform_id"]
    res = cx.verify_identity(db, afterlive["id"], [{"id": afm_channel}])
    assert res["state"] == "CROSS_BRAND" and "america_first_monitor" in res["reason"]
    assert _acct(db, "youtube", "afterlive")["identity_verified_at"] is None


def test_routing_guard_requires_brand_identity_switch_and_explicit_command(db):
    cx.record_directory(db)
    a = _acct(db, "youtube", "america_first_monitor")
    p = cx.routing_problems(db, "afterlive", a["id"])
    assert any("Brand mismatch" in x for x in p) and any("identity" in x for x in p) \
        and any("not enabled" in x for x in p) and any("explicit owner command" in x for x in p)
    cx.verify_identity(db, a["id"], [{"id": a["platform_id"]}])
    assert cx.routing_problems(db, "america_first_monitor", a["id"], explicit_owner_command=True) \
        == ["Publishing is not enabled for this destination (it is OFF by default)."]
    db.update("social_accounts", a["id"], publishing_enabled=1)
    assert cx.routing_problems(db, "america_first_monitor", a["id"], explicit_owner_command=True) == []
    assert cx.routing_problems(db, "afterlive", a["id"], explicit_owner_command=True)             # still blocked


def _http(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_read_only_identity_adapters_parse_official_shapes():
    def handler(req):
        u = str(req.url)
        assert req.method == "GET" and req.headers["authorization"] == "Bearer T"
        if "youtube/v3/channels" in u:
            assert "mine=true" in u
            return httpx.Response(200, json={"items": [{"id": "UC1", "snippet": {"title": "Afterlive", "customUrl": "@afterlivehqq"}}]})
        if "me/accounts" in u:
            return httpx.Response(200, json={"data": [{"id": "P1", "name": "Afterlive", "instagram_business_account": {"id": "IG1", "username": "afterlivehq"}}, {"id": "P2", "name": "Other"}]})
        if "user/info" in u:
            return httpx.Response(200, json={"data": {"user": {"open_id": "OID", "display_name": "Afterlive", "username": "afterlivehq"}}})
        return httpx.Response(404)
    h = _http(handler)
    assert cx.youtube_identity(h, "T")[0]["id"] == "UC1"
    fb = cx.facebook_identity(h, "T")
    assert [o["id"] for o in fb] == ["P1", "IG1", "P2"] and fb[1]["kind"] == "instagram_account"
    assert cx.tiktok_identity(h, "T")[0]["id"] == "OID"
    with pytest.raises(RuntimeError, match="HTTP 401"):
        cx.youtube_identity(_http(lambda r: httpx.Response(401)), "T")


def test_matrix_is_honest_about_unknowns_and_keeps_publishing_off(db, settings, tmp_path):
    from clipengine.publishing.tokens import TokenStore
    cx.record_directory(db)
    db.insert("social_accounts", platform="tiktok", handle="PENDING-spec_edit-tiktok", brand="spec_edit",
              status="awaiting_connection", created_at=1.0, updated_at=1.0)
    rows = cx.build_matrix(db, settings, TokenStore(tmp_path / "s", use_keyring=False))
    assert len(rows) == 20
    for r in rows:
        s = r["states"]
        assert set(s) == set(cx.STATE_KEYS)
        assert s["api_identity_verified"][0] != cx.VERIFIED and s["oauth_connected"][0] != cx.VERIFIED
        assert s["publishing_enabled"][0] == cx.OFF and s["draft_delivery_verified"][0] == cx.NOT_TESTED
    ig = next(r for r in rows if r["platform"] == "instagram")
    assert ig["states"]["draft_supported"][0] == cx.UNSUPPORTED
    assert next(r for r in rows if r["platform"] == "youtube")["states"]["draft_supported"][0] == cx.PARTIAL
    pending = next(r for r in rows if r["handle"].startswith("PENDING-"))
    assert pending["states"]["account_created"][0] == cx.BLOCKED
    assert pending["states"]["profile_branded"][0] == cx.UNKNOWN
