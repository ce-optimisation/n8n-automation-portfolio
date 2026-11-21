"""Six-brand portfolio: roster, seeding, verification, discovery supervisor, priorities, diagnostics.

Every platform call here is a MOCK (fake adapters or httpx.MockTransport). Passing proves the engine's own
logic; it does NOT prove any live integration, and nothing here labels a platform "live verified"."""
import json
import random
import time

import httpx
import pytest

from clipengine import discovery, events, platforms, portfolio, portfolio_report
from clipengine.db import Database
from clipengine.platforms import AdapterError, LiveStatus, MediaItem, Report

NOW = 1795000000.0   # 2026-11-18 ~10:26 UTC, inside the GTA VI window


@pytest.fixture
def db(settings):
    d = Database(settings.db_path)
    portfolio.seed(d, settings)
    events.seed_events(d)
    return d


class FakeAdapter:
    def __init__(self, platform, configured=True):
        self.platform, self._ok = platform, configured
        self.ident = {}          # handle -> identity dict
        self.rep = None          # Report or exception
        self.calls = 0

    def configured(self):
        return self._ok

    def capabilities(self):
        return {}

    def identity(self, ref):
        if isinstance(self.ident.get(ref.handle), Exception):
            raise self.ident[ref.handle]
        if ref.handle not in self.ident:
            raise AdapterError("not_found", "no such channel")
        return self.ident[ref.handle]

    def report(self, ref):
        self.calls += 1
        if isinstance(self.rep, Exception):
            raise self.rep
        return self.rep


class StubQueue:
    def __init__(self):
        self.jobs = []

    def enqueue(self, kind, **kw):
        key = kw.get("dedupe_key")
        for j in self.jobs:
            if key and j[1].get("dedupe_key") == key:
                return j[2]
        self.jobs.append((kind, kw, len(self.jobs) + 1))
        return len(self.jobs)


def sup(settings, db, adapters, now=NOW, queue=None):
    clock = {"t": now}
    s = discovery.DiscoverySupervisor(settings, db, queue or StubQueue(), adapters=adapters, clock=lambda: clock["t"],
                                      rng=random.Random(1))
    s.clock_box = clock
    return s


def ms(db, uid):
    return db.one("SELECT * FROM monitor_sources WHERE uid=?", (uid,))


def grant_creator(db, uid, **kw):
    """Record this creator's own written permission (host permission alone never permits acquisition)."""
    db.execute("UPDATE creators SET permission_json=? WHERE uid=?",
               (json.dumps({"granted_by": "owner", "reference": "test-ref", **kw}), uid))


def verify_yt(db, uid, handle, name, ucid, fake):
    fake.ident[handle] = {"id": ucid, "name": name, "url": f"https://www.youtube.com/channel/{ucid}"}


# ---- roster -------------------------------------------------------------------------------------

def test_all_eighteen_creators_assigned_to_six_brands(db):
    rows = db.query("SELECT * FROM creators WHERE roster='approved'")
    assert len(rows) == 18
    by = {}
    for r in rows:
        by.setdefault(r["brand_key"], []).append(r["name"])
    assert set(by) == set(portfolio.BRANDS) and all(len(v) == 3 for v in by.values())
    assert sorted(by["afterlive"]) == ["IShowSpeed", "Kai Cenat", "N3on"]
    assert db.one("SELECT COUNT(*) n FROM brands")["n"] == 6


def test_no_extra_afterlive_creator_or_source(db):
    live = db.query("SELECT DISTINCT creator_id FROM monitor_sources WHERE brand_key='afterlive'")
    assert len(live) == 3
    assert db.one("SELECT COUNT(*) n FROM creators WHERE brand_key='afterlive'")["n"] == 3


def test_seed_never_invents_identity(db):
    # nothing is verified, no platform id, and the only canonical URL is the owner-configured Rumble channel
    assert db.one("SELECT COUNT(*) n FROM monitor_sources WHERE verification='VERIFIED'")["n"] == 0
    assert db.one("SELECT COUNT(*) n FROM monitor_sources WHERE platform_id IS NOT NULL")["n"] == 0
    urls = [r["canonical_url"] for r in db.query("SELECT canonical_url FROM monitor_sources WHERE canonical_url IS NOT NULL")]
    assert urls == ["https://rumble.com/c/nickjfuentes"]


def test_reseeding_is_idempotent_and_respects_owner_changes(settings, db):
    portfolio.owner_set(db, ms(db, "mkbhd/youtube/channel")["id"], monitoring_enabled=0, priority=99)
    before = db.one("SELECT COUNT(*) n FROM monitor_sources")["n"], db.one("SELECT COUNT(*) n FROM creators")["n"]
    out = portfolio.seed(db, settings)
    assert out["creators_added"] == out["sources_added"] == 0
    assert (db.one("SELECT COUNT(*) n FROM monitor_sources")["n"], db.one("SELECT COUNT(*) n FROM creators")["n"]) == before
    row = ms(db, "mkbhd/youtube/channel")
    assert row["monitoring_enabled"] == 0 and row["priority"] == 99          # not re-enabled, not overwritten
    assert "monitoring_enabled" in json.loads(row["owner_locked_json"])


def test_seed_adopts_existing_creator_without_overwriting(settings):
    d = Database(settings.data_dir / "adopt.db")
    cid = d.insert("creators", name="Nick Fuentes", platform="rumble", url="https://rumble.com/c/nickjfuentes",
                   status="active", priority=80, permission_json='{"granted_by":"x"}')
    sid = d.insert("sources", name="Fuentes", kind="channel", url="https://rumble.com/c/nickjfuentes", enabled=1)
    portfolio.seed(d, settings)
    row = d.one("SELECT * FROM creators WHERE id=?", (cid,))
    assert row["status"] == "active" and row["priority"] == 80 and row["permission_json"] == '{"granted_by":"x"}'
    assert row["uid"] == "nick-fuentes" and row["brand_key"] == "america_first_monitor"
    assert d.one("SELECT COUNT(*) n FROM creators WHERE name='Nick Fuentes'")["n"] == 1
    assert ms(d, "nick-fuentes/rumble/channel")["legacy_source_id"] == sid


# ---- verification ---------------------------------------------------------------------------------

def test_youtube_verified_only_on_exact_official_name_and_id_persists(settings, db):
    f = FakeAdapter("youtube")
    verify_yt(db, "mkbhd", "@mkbhd", "Marques Brownlee", "UCBJycsmduvYEL83R_U4JriQ", f)
    res = portfolio.verify_source(settings, db, ms(db, "mkbhd/youtube/channel"), {"youtube": f})
    row = ms(db, "mkbhd/youtube/channel")
    assert res["verified"] and row["verification"] == "VERIFIED" and row["platform_id"] == "UCBJycsmduvYEL83R_U4JriQ"
    assert row["canonical_url"].endswith("UCBJycsmduvYEL83R_U4JriQ") and row["evidence"]
    portfolio.seed(db, settings)                                            # re-seed keeps the verified id
    assert ms(db, "mkbhd/youtube/channel")["platform_id"] == "UCBJycsmduvYEL83R_U4JriQ"
    # an impostor with a different name is NOT accepted
    verify_yt(db, "linus-tech-tips", "@LinusTechTips", "Linus Tech Tips Fan Clips", "UCx", f)
    res = portfolio.verify_source(settings, db, ms(db, "linus-tech-tips/youtube/channel"), {"youtube": f})
    assert not res["verified"] and ms(db, "linus-tech-tips/youtube/channel")["state"] == "NEEDS_VERIFICATION"


def test_twitch_name_match_without_platform_signal_needs_owner_confirmation(settings, db):
    f = FakeAdapter("twitch")
    f.ident["kaicenat"] = {"id": "641972806", "name": "KaiCenat", "url": "https://www.twitch.tv/kaicenat",
                           "platform_verified": False}
    portfolio.verify_source(settings, db, ms(db, "kai-cenat/twitch/channel"), {"twitch": f})
    row = ms(db, "kai-cenat/twitch/channel")
    assert row["verification"] == "NEEDS_VERIFICATION" and row["platform_id"] == "641972806"
    portfolio.confirm_identity(db, row["id"], "checked")
    assert ms(db, "kai-cenat/twitch/channel")["verification"] == "VERIFIED"


def test_missing_credentials_and_unknown_handle_are_specific_states(settings, db):
    res = portfolio.verify_source(settings, db, ms(db, "mkbhd/youtube/channel"), {})   # real adapter, no key
    row = ms(db, "mkbhd/youtube/channel")
    assert res["code"] == "needs_credentials" and row["state"] == "NEEDS_CREDENTIALS"
    f = FakeAdapter("youtube")
    portfolio.verify_source(settings, db, ms(db, "mkbhd/youtube/channel"), {"youtube": f})
    assert ms(db, "mkbhd/youtube/channel")["error_code"] == "not_found"


def test_podcast_lookup_requires_exact_title_and_feed_is_parsed(settings):
    feed = """<rss xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"><channel><title>Modern Wisdom</title>
    <item><title>Ep 1</title><guid>g-1</guid><pubDate>Mon, 16 Nov 2026 08:00:00 GMT</pubDate>
    <enclosure url="https://cdn.example/1.mp3" type="audio/mpeg"/><itunes:duration>1:02:03</itunes:duration></item>
    <item><title>Ep 1 again</title><guid>g-1</guid></item></channel></rss>"""

    def h(req):
        if "itunes.apple.com" in str(req.url):
            return httpx.Response(200, json={"results": [
                {"collectionName": "Modern Wisdom Clips", "feedUrl": "https://bad.example/f", "collectionId": 1},
                {"collectionName": "Modern Wisdom", "feedUrl": "https://feeds.example/mw", "collectionId": 2}]})
        return httpx.Response(200, text=feed)
    a = platforms.PodcastAdapter(settings, httpx.Client(transport=httpx.MockTransport(h)))
    a._throttle.min_interval = 0
    assert a.find_feed(["Modern Wisdom"])["feed_url"] == "https://feeds.example/mw"
    meta, items = platforms.parse_podcast_feed(feed)
    assert meta["title"] == "Modern Wisdom" and items[0].duration_s == 3723 and items[0].url.endswith("1.mp3")
    assert items[0].key == "podcast:g-1"
    with pytest.raises(AdapterError) as e:
        a.find_feed(["Nonexistent Show"])
    assert e.value.code == "not_found"


# ---- error taxonomy ------------------------------------------------------------------------------

@pytest.mark.parametrize("status,body,code", [(401, "", "unauthorised"), (403, "quotaExceeded", "rate_limited"),
                                              (403, "denied", "unauthorised"), (429, "", "rate_limited"),
                                              (404, "", "not_found"), (503, "", "temporary_failure"),
                                              (451, "", "inaccessible")])
def test_http_errors_map_to_specific_codes(settings, status, body, code):
    a = platforms.YouTubeAdapter(settings, httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(status, text=body))))
    a._throttle.min_interval = 0
    with pytest.raises(AdapterError) as e:
        a._get("https://www.googleapis.com/youtube/v3/x")
    assert e.value.code == code


def test_network_failure_is_temporary(settings):
    def boom(req):
        raise httpx.ConnectError("down")
    a = platforms.KickAdapter(settings, httpx.Client(transport=httpx.MockTransport(boom)))
    a._throttle.min_interval = 0
    with pytest.raises(AdapterError) as e:
        a._get("https://api.kick.com/x")
    assert e.value.code == "temporary_failure"


# ---- supervisor ------------------------------------------------------------------------------------

def make_verified(db, uid, platform_id, name="X"):
    db.execute("UPDATE monitor_sources SET verification='VERIFIED', platform_id=?, channel_name=?, state='PENDING', "
               "canonical_url=? WHERE uid=?", (platform_id, name, f"https://www.youtube.com/channel/{platform_id}", uid))


def yt_report(items, live=None, ident="UC1"):
    return Report("youtube", {}, identity={"id": ident}, items=items, live=live or LiveStatus(False))


def test_missing_credentials_state_then_pickup_when_key_appears(settings, db):
    make_verified(db, "mkbhd/youtube/channel", "UC1")
    s = sup(settings, db, {"youtube": FakeAdapter("youtube", configured=False)})
    s.poll(ms(db, "mkbhd/youtube/channel")["id"])
    row = ms(db, "mkbhd/youtube/channel")
    assert row["error_code"] == "needs_credentials" and row["state"] == "NEEDS_CREDENTIALS"
    assert row["next_check"] - NOW <= 30 * 60 * 1.25 and row["consecutive_errors"] == 0
    f = FakeAdapter("youtube")
    f.rep = yt_report([])
    s.adapters["youtube"] = f
    s.clock_box["t"] += 3600
    s.poll(row["id"])
    assert ms(db, "mkbhd/youtube/channel")["state"] in ("DISCOVERY_ONLY", "PERMISSION_REQUIRED")


def test_expired_credentials_rate_limit_and_recovery(settings, db):
    make_verified(db, "mkbhd/youtube/channel", "UC1")
    f = FakeAdapter("youtube")
    s = sup(settings, db, {"youtube": f})
    mid = ms(db, "mkbhd/youtube/channel")["id"]
    f.rep = AdapterError("unauthorised", "HTTP 401")
    s.poll(mid)
    assert ms(db, "mkbhd/youtube/channel")["error_code"] == "unauthorised" and ms(db, "mkbhd/youtube/channel")["state"] == "ERROR"
    f.rep = AdapterError("rate_limited", "HTTP 429")
    s.poll(mid)
    r = ms(db, "mkbhd/youtube/channel")
    assert r["error_code"] == "rate_limited" and r["next_check"] - NOW >= 30 * 60 * 0.85      # waits at least ~30 min
    f.rep = yt_report([])
    s.clock_box["t"] += 7200
    s.poll(mid)
    r = ms(db, "mkbhd/youtube/channel")
    assert r["error_code"] is None and r["consecutive_errors"] == 0 and r["last_success"]
    kinds = [e["kind"] for e in db.query("SELECT kind FROM monitor_events WHERE monitor_source_id=?", (mid,))]
    assert "error" in kinds and "recovered" in kinds


def test_temporary_failures_back_off_exponentially_with_jitter(settings, db):
    make_verified(db, "mkbhd/youtube/channel", "UC1")
    f = FakeAdapter("youtube")
    f.rep = AdapterError("temporary_failure", "HTTP 503")
    s = sup(settings, db, {"youtube": f})
    mid = ms(db, "mkbhd/youtube/channel")["id"]
    delays = []
    for _ in range(4):
        s.poll(mid)
        delays.append(ms(db, "mkbhd/youtube/channel")["next_check"] - NOW)
    assert delays[3] > delays[0] * 2 and max(delays) <= 6 * 3600 * 1.25


def test_one_platform_failing_does_not_stop_others(settings, db):
    make_verified(db, "mkbhd/youtube/channel", "UC1")
    make_verified(db, "linus-tech-tips/youtube/channel", "UC2")
    db.execute("UPDATE monitor_sources SET platform='kick', canonical_url='https://kick.com/n3on', verification='VERIFIED', "
               "platform_id='9', candidate_handle='n3on' WHERE uid='n3on/kick/channel'")
    bad, good = FakeAdapter("kick"), FakeAdapter("youtube")
    bad.rep = AdapterError("temporary_failure", "down")
    good.rep = yt_report([])
    s = sup(settings, db, {"kick": bad, "youtube": good})
    out = s.tick()
    assert out["polled"] >= 3
    assert ms(db, "n3on/kick/channel")["error_code"] == "temporary_failure"
    assert ms(db, "mkbhd/youtube/channel")["last_success"] and not ms(db, "n3on/kick/channel")["last_success"]


def test_first_poll_is_baseline_then_new_upload_discovered_once(settings, db):
    make_verified(db, "mkbhd/youtube/channel", "UC1")
    f = FakeAdapter("youtube")
    q = StubQueue()
    s = sup(settings, db, {"youtube": f}, queue=q)
    mid = ms(db, "mkbhd/youtube/channel")["id"]
    old = MediaItem("youtube:old", "Old review", "https://www.youtube.com/watch?v=old", "upload", "2026-11-01T00:00:00Z", 600)
    f.rep = yt_report([old])
    s.poll(mid)
    assert db.one("SELECT state FROM discovered_items")["state"] == "baseline" and not q.jobs
    new = MediaItem("youtube:new", "New phone review", "https://www.youtube.com/watch?v=new", "upload", "2026-11-18T09:00:00Z", 900)
    f.rep = yt_report([new, old])
    s.clock_box["t"] += 3600
    s.poll(mid)
    s.clock_box["t"] += 3600
    s.poll(mid)                                                              # no duplicate on a repeat poll
    rows = db.query("SELECT * FROM discovered_items WHERE state<>'baseline'")
    assert len(rows) == 1 and rows[0]["platform_item_id"] == "youtube:new"
    assert ms(db, "mkbhd/youtube/channel")["latest_media_id"] == "youtube:new"


def test_acquisition_permission_is_enforced_and_separate_from_discovery(settings, db):
    settings.allow_private_urls = True        # tests never resolve DNS
    make_verified(db, "mkbhd/youtube/channel", "UC1")
    f = FakeAdapter("youtube")
    q = StubQueue()
    s = sup(settings, db, {"youtube": f}, queue=q)
    mid = ms(db, "mkbhd/youtube/channel")["id"]
    f.rep = yt_report([MediaItem("youtube:a", "A", "https://www.youtube.com/watch?v=a", "upload", "2026-11-01T00:00:00Z", 600)])
    s.poll(mid)
    f.rep = yt_report([MediaItem("youtube:b", "B", "https://www.youtube.com/watch?v=b", "upload", "2026-11-18T09:00:00Z", 600)])
    s.clock_box["t"] += 3600
    s.poll(mid)
    s.route_pending()
    d = db.one("SELECT * FROM discovered_items WHERE platform_item_id='youtube:b'")
    assert d["state"] == "permission_required" and not q.jobs
    assert ms(db, "mkbhd/youtube/channel")["state"] == "PERMISSION_REQUIRED"      # discovery works, acquisition waits
    now = time.time()
    db.execute("INSERT INTO source_permissions(domain,granted_by,granted_on,reference,scope,created_at,updated_at) "
               "VALUES('youtube.com','owner','2026-11-18','ref-1','test',?,?)", (now, now))
    s.route_pending()
    assert db.one("SELECT state FROM discovered_items WHERE platform_item_id='youtube:b'")["state"] == "permission_required"   # host permission alone is not enough
    grant_creator(db, "mkbhd")
    s.route_pending()
    assert db.one("SELECT state FROM discovered_items WHERE platform_item_id='youtube:b'")["state"] == "queued"
    assert len(q.jobs) == 1 and q.jobs[0][0] == "download_url"
    s.route_pending()
    assert len(q.jobs) == 1                                                      # never queued twice


def test_insufficient_disk_holds_item_and_downloads_nothing(settings, db, monkeypatch):
    settings.allow_private_urls = True
    make_verified(db, "mkbhd/youtube/channel", "UC1")
    now = time.time()
    db.execute("INSERT INTO source_permissions(domain,granted_by,granted_on,reference,scope,created_at,updated_at) "
               "VALUES('youtube.com','owner','2026-11-18','r','t',?,?)", (now, now))
    grant_creator(db, "mkbhd")
    f, q = FakeAdapter("youtube"), StubQueue()
    s = sup(settings, db, {"youtube": f}, queue=q)
    mid = ms(db, "mkbhd/youtube/channel")["id"]
    f.rep = yt_report([])
    s.poll(mid)
    f.rep = yt_report([MediaItem("youtube:c", "C", "https://www.youtube.com/watch?v=c", "upload", "2026-11-18T09:00:00Z", 7200)])
    s.clock_box["t"] += 3600
    s.poll(mid)
    monkeypatch.setattr("clipengine.storage.free_bytes", lambda p: 10_000_000)
    settings.record_gb_per_hour = 1.5
    assert s.route_pending() == 0 and not q.jobs
    d = db.one("SELECT * FROM discovered_items WHERE platform_item_id='youtube:c'")
    assert d["state"] == "discovered" and "not enough free disk" in d["detail"]


def test_simultaneous_live_streams_and_simulcast_dedupe(settings, db):
    # two different creators live at once: both recorded as LIVE items; one creator simulcasting on two
    # platforms: the second copy is a duplicate (no competing job).
    for uid, pid in (("ishowspeed/youtube/channel", "UCs"), ("kai-cenat/youtube/channel", "UCk")):
        make_verified(db, uid, pid)
    db.execute("UPDATE monitor_sources SET verification='VERIFIED', platform_id='77', candidate_handle='kaicenat', "
               "canonical_url='https://www.twitch.tv/kaicenat' WHERE uid='kai-cenat/twitch/channel'")
    fy, ft = FakeAdapter("youtube"), FakeAdapter("twitch")
    s = sup(settings, db, {"youtube": fy, "twitch": ft})
    started = "2026-11-18T10:00:00Z"
    fy.rep = None

    def yt_for(ref):
        vid = "vS" if ref.handle == "UCs" else "vK"
        return yt_report([MediaItem(f"youtube:{vid}", "GTA VI day one LIVE", f"https://www.youtube.com/watch?v={vid}", "live", started)],
                         LiveStatus(True, "GTA VI day one LIVE", 1000, started, "Grand Theft Auto VI",
                                    f"https://www.youtube.com/watch?v={vid}"), ident=ref.handle)
    fy.report = yt_for
    ft.rep = Report("twitch", {}, identity={"id": "77"}, items=[],
                    live=LiveStatus(True, "GTA VI day one LIVE", 5000, started, "Grand Theft Auto VI", "https://www.twitch.tv/kaicenat"))
    s.poll(ms(db, "ishowspeed/youtube/channel")["id"])
    s.poll(ms(db, "kai-cenat/youtube/channel")["id"])
    s.poll(ms(db, "kai-cenat/twitch/channel")["id"])
    assert ms(db, "ishowspeed/youtube/channel")["live_state"] == "LIVE" and ms(db, "kai-cenat/youtube/channel")["live_state"] == "LIVE"
    kai = db.query("SELECT d.* FROM discovered_items d JOIN creators c ON c.id=d.creator_id WHERE c.uid='kai-cenat' AND d.kind='live' ORDER BY d.id")
    assert len(kai) == 2 and kai[0]["state"] == "discovered" and kai[1]["state"] == "duplicate" and kai[1]["dup_of"] == kai[0]["id"]
    speed = db.one("SELECT * FROM discovered_items d JOIN creators c ON c.id=d.creator_id WHERE c.uid='ishowspeed'")
    assert speed["state"] == "discovered"                          # a different creator is never a duplicate
    assert "gta_vi_launch" in (speed["priority_tags"] or "")


def test_restart_resumes_without_duplicates(settings, db):
    make_verified(db, "mkbhd/youtube/channel", "UC1")
    f = FakeAdapter("youtube")
    f.rep = yt_report([MediaItem("youtube:n", "N", "https://www.youtube.com/watch?v=n", "upload", "2026-11-18T09:00:00Z", 60)])
    s1 = sup(settings, db, {"youtube": f})
    mid = ms(db, "mkbhd/youtube/channel")["id"]
    s1.poll(mid)
    row = ms(db, "mkbhd/youtube/channel")
    s2 = sup(settings, db, {"youtube": f}, now=NOW + 5)                       # a fresh process, same database
    calls = f.calls
    s2.tick()
    assert f.calls == calls                                                   # not due yet: no repeat call
    s2.clock_box["t"] = row["next_check"] + 1
    s2.poll(mid)
    assert db.one("SELECT COUNT(*) n FROM discovered_items")["n"] == 1
    assert db.one("SELECT COUNT(*) n FROM monitor_sources")["n"] == 27


def test_platform_id_change_returns_source_to_verification(settings, db):
    make_verified(db, "mkbhd/youtube/channel", "UC1")
    f = FakeAdapter("youtube")
    f.rep = yt_report([], ident="UC_OTHER")
    s = sup(settings, db, {"youtube": f})
    s.poll(ms(db, "mkbhd/youtube/channel")["id"])
    r = ms(db, "mkbhd/youtube/channel")
    assert r["verification"] == "NEEDS_VERIFICATION" and r["state"] == "NEEDS_VERIFICATION" and not r["last_success"]


def test_youtube_quota_budget_is_respected(settings, db):
    make_verified(db, "mkbhd/youtube/channel", "UC1")
    f = FakeAdapter("youtube")
    f.rep = yt_report([])
    s = sup(settings, db, {"youtube": f})
    db.set_kv(s._quota_key("youtube", NOW), str(discovery.YT_DAILY_UNITS - 1))
    s.poll(ms(db, "mkbhd/youtube/channel")["id"])
    assert f.calls == 0 and ms(db, "mkbhd/youtube/channel")["error_code"] == "rate_limited"


# ---- launch-event prioritisation -----------------------------------------------------------------

def test_launch_priority_raises_afterlive_only_and_expires(settings, db):
    spec, speed = ms(db, "mkbhd/youtube/channel"), ms(db, "ishowspeed/youtube/channel")
    assert events.interval_for(db, speed, NOW) == 300 and events.interval_for(db, spec, NOW) == 20 * 60
    assert events.interval_for(db, speed, NOW, live=True) == 5 * 60                 # YouTube floor protects quota
    kai_tw = ms(db, "kai-cenat/twitch/channel")
    assert events.interval_for(db, kai_tw, NOW, live=True) == 90 and events.interval_for(db, kai_tw, NOW) == 300
    after = events.parse_ts("2026-11-27T00:00:00Z")
    assert events.interval_for(db, speed, after) == 20 * 60                          # restored automatically
    before = events.parse_ts("2026-11-10T00:00:00Z")
    assert events.interval_for(db, speed, before) == 20 * 60


def test_events_are_configurable_and_tag_by_metadata_not_creator(settings, db):
    assert events.tag_item(db, "afterlive", NOW, "Playing Minecraft", "Minecraft") == []
    assert events.tag_item(db, "afterlive", NOW, "late night", "Grand Theft Auto VI") == ["gta_vi_launch"]
    assert events.tag_item(db, "spec_edit", NOW, "GTA 6 on a laptop?") == []        # other brands never tagged
    db.execute("INSERT INTO priority_events(key,name,brand_key,starts_at,ends_at,poll_seconds,live_poll_seconds,"
               "match_terms_json,enabled,created_at,updated_at) VALUES('x','Other launch','spec_edit',"
               "'2026-11-01T00:00:00Z','2026-12-01T00:00:00Z',600,300,'[\"foldable\"]',1,0,0)")
    assert events.tag_item(db, "spec_edit", NOW, "New foldable") == ["x"]


def test_imminent_scheduled_broadcast_polls_faster_but_never_assumes_live(settings, db):
    kai_tw = ms(db, "kai-cenat/twitch/channel")
    soon = [MediaItem("twitch:s1", "Launch stream", "u", "upcoming", "2026-11-18T10:50:00Z")]
    assert events.is_imminent(soon, NOW)
    make_verified(db, "kai-cenat/twitch/channel", "77")
    f = FakeAdapter("twitch")
    f.rep = Report("twitch", {}, identity={"id": "77"}, items=soon, live=LiveStatus(False))
    s = sup(settings, db, {"twitch": f})
    s.poll(kai_tw["id"])
    r = ms(db, "kai-cenat/twitch/channel")
    assert r["live_state"] == "OFFLINE" and r["next_check"] - NOW <= 90 * 1.1


# ---- diagnostics and dashboard ---------------------------------------------------------------------

def test_diagnostics_report_is_read_only_and_reflects_real_state(settings, db):
    make_verified(db, "mkbhd/youtube/channel", "UC1")
    before = {t: db.one(f"SELECT COUNT(*) n FROM {t}")["n"] for t in ("monitor_sources", "monitor_events", "creators")}
    rep = portfolio_report.build(settings, None)
    after = {t: db.one(f"SELECT COUNT(*) n FROM {t}")["n"] for t in before}
    assert before == after
    assert len(rep["sources"]) == 27 and rep["creators"] == 18
    assert rep["states"].get("MONITORING", 0) == 0                    # nothing has been checked yet
    assert rep["credentials"]["youtube"] == "needs_credentials"
    text = portfolio_report.render(rep)
    assert "Afterlive" in text and "Needs the owner" in text
    for secret in ("secret", "token"):
        assert secret not in json.dumps(rep).lower().replace("credentials", "")


def test_dashboard_portfolio_page_and_toggle(settings, db, monkeypatch):
    from fastapi.testclient import TestClient
    from clipengine.web.app import create_app
    app = create_app(settings, start_worker=False)
    with TestClient(app) as c:
        r = c.get("/portfolio")
        assert r.status_code == 200 and "Afterlive" in r.text and "Operator Notes" in r.text and "NEEDS_VERIFICATION" in r.text
        assert "Nick Fuentes" in r.text and "N3on" in r.text


# ---- rendering: caption +30%, top title -20% -------------------------------------------------------

def test_caption_size_is_thirty_percent_larger_than_the_73px_it_replaces():
    from clipengine import captions
    assert captions.CaptionStyle().size == round(73 * 1.3) == 95
    assert captions.PRESETS["anton"].size <= captions.LIMITS["size"][1]


def test_legacy_saved_73px_override_does_not_keep_the_old_size(db):
    from clipengine import captions
    db.set_kv("caption_style", json.dumps({"size": 73, "position": 0.72}))
    assert captions.load_style(db).size == 95
    db.set_kv("caption_style", json.dumps({"size": 80}))
    assert captions.load_style(db).size == 80          # a deliberate owner choice is respected


@pytest.mark.parametrize("phrase", [["Wow"], ["No way"], ["The committee will publish"], ["its findings on the funding", "review next week"],
                                    ["Representatives disagreed", "about institutional"], ["W" * 30, "WWWW WWWW WWWW"]])
def test_enlarged_captions_never_cross_the_safe_margins(phrase):
    from clipengine import captions
    style = captions.CaptionStyle()
    m = captions.font_metrics("Anton")
    usable = 1080 - 2 * captions.SIDE_MARGIN - 2 * style.outline
    fs = captions.fitted_size(phrase, style)
    assert fs <= 95 and all(m.width(l, fs) <= usable + 0.5 for l in phrase)
    # two lines at the largest size stay inside the frame around the 72% centre line
    assert 1920 * 0.72 + 2 * 95 * 0.6 < 1920


def test_title_renders_into_a_real_clip_without_changing_the_video_frame(settings, sample_video, tmp_path):
    from clipengine import captions, render, titles
    words = [{"word": w, "start": i * .4, "end": i * .4 + .35} for i, w in enumerate("the committee will publish its findings".split())]
    plain = render.render_clip(settings, sample_video, tmp_path / "a", 0.0, 3.0, words, 1280, 720, None, style=captions.CaptionStyle())
    titled = render.render_clip(settings, sample_video, tmp_path / "b", 0.0, 3.0, words, 1280, 720, None, style=captions.CaptionStyle(),
                                title="The *TRUTH* about **it**", title_style=titles.TitleStyle(enabled=True))
    assert plain["title"] is False and titled["title"] is True
    assert (titled["probe"]["width"], titled["probe"]["height"]) == (1080, 1920) == (plain["probe"]["width"], plain["probe"]["height"])
    # Session 14/15: with the title-free master on (default) the title is drawn in a second step from title.ass and captions.ass stays
    # title-free; with it off the title is in captions.ass as before. Either way the title style is present in the render inputs.
    ass = tmp_path / "b" / ("title.ass" if render.has_clean_master(tmp_path / "b") else "captions.ass")
    assert "Style: Title,Anton,160" in ass.read_text(encoding="utf-8")
    if render.has_clean_master(tmp_path / "b"):
        assert "Style: Title" not in (tmp_path / "b" / "captions.ass").read_text(encoding="utf-8")


def test_short_videos_are_skipped_when_a_minimum_is_set_and_long_ones_still_queue(settings, db):
    """Owner decision 19 Nov: automatically found videos under MIN_SOURCE_SECONDS are already clips (e.g. Shorts)."""
    settings.allow_private_urls = True
    settings.min_source_seconds = 180
    make_verified(db, "mkbhd/youtube/channel", "UC1")
    now = time.time()
    db.execute("INSERT INTO source_permissions(domain,granted_by,granted_on,reference,scope,created_at,updated_at) "
               "VALUES('youtube.com','owner','2026-11-18','r','t',?,?)", (now, now))
    grant_creator(db, "mkbhd")
    f, q = FakeAdapter("youtube"), StubQueue()
    s = sup(settings, db, {"youtube": f}, queue=q)
    mid = ms(db, "mkbhd/youtube/channel")["id"]
    f.rep = yt_report([])
    s.poll(mid)
    short = MediaItem("youtube:short", "A Short", "https://www.youtube.com/watch?v=short", "upload", "2026-11-18T09:00:00Z", 59)
    long_ = MediaItem("youtube:long", "A Review", "https://www.youtube.com/watch?v=long", "upload", "2026-11-18T09:05:00Z", 900)
    f.rep = yt_report([short, long_])
    s.clock_box["t"] += 3600
    s.poll(mid)
    s.route_pending()
    assert db.one("SELECT state FROM discovered_items WHERE platform_item_id='youtube:short'")["state"] == "skipped"
    assert db.one("SELECT state FROM discovered_items WHERE platform_item_id='youtube:long'")["state"] == "queued"
    assert len(q.jobs) == 1 and q.jobs[0][1]["payload"]["url"].endswith("v=long")
    s.route_pending()
    assert len(q.jobs) == 1                                   # the skipped Short is never reconsidered


def test_minimum_is_off_by_default_so_short_videos_are_unchanged(settings, db):
    assert settings.min_source_seconds == 0
