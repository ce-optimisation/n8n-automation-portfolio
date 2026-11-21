"""Session 15: ONE approval -> independent delivery to every enabled destination of the clip's brand. TESTED-MOCKED: no platform is
contacted; the clients are fakes. Real publishing is NOT verified by this file."""
import json
import time

import pytest
from fastapi.testclient import TestClient

from clipengine import connections as cx
from clipengine.jobs import JobQueue
from clipengine.publishing import PublishError, autodeliver, delivery
from clipengine.web.app import create_app
from test_phone_delivery import Ctx, db, enable, mk_clip   # noqa: F401  (db is a fixture)


class OkYT:
    def upload(self, db, aid, video, title, description, public=False):
        return {"video_id": "YT1", "privacy": "public" if public else "private"}


class RefusingFB:
    def create_reel(self, db, aid, video, description, publish=False):
        raise PublishError("Facebook refused: permission pages_manage_posts missing")


class OkFB(RefusingFB):
    def create_reel(self, db, aid, video, description, publish=False):
        return {"video_id": "FB1", "success": True}


class OkIG:
    public_enabled = True

    def __init__(self):
        self.calls = 0

    def publish_reel(self, brand, ig_id, video, caption):
        self.calls += 1
        return {"video_id": "IG1", "permalink": "https://instagram.com/reel/q", "success": True}


def _ig(db):
    a = db.one("SELECT * FROM social_accounts WHERE platform='instagram' AND brand='afterlive'")
    cx.verify_identity(db, a["id"], [{"id": a["platform_id"]}])
    db.update("social_accounts", a["id"], publishing_enabled=1)
    delivery.set_mode(db, a["id"], "public")
    return a


def _run_jobs(settings, db, clients):
    results = {}
    for j in db.query("SELECT * FROM jobs WHERE kind='deliver_clip' ORDER BY id"):
        p = json.loads(j["payload_json"])
        acct = db.one("SELECT platform FROM social_accounts WHERE id=?", (p["account_id"],))
        try:
            autodeliver.handle(settings, db, clients, Ctx(p))
            results[acct["platform"]] = "ok"
        except Exception as e:      # noqa: BLE001 - the queue would record it per job
            results[acct["platform"]] = type(e).__name__ + ": " + str(e)[:60]
    return results


def test_one_tap_creates_one_job_per_enabled_destination_and_failures_stay_independent(settings, db):
    cid = mk_clip(settings, db)
    yt, fb, ig = enable(db, "youtube"), enable(db, "facebook"), _ig(db)
    delivery.set_mode(db, yt["id"], "public")
    delivery.set_mode(db, fb["id"], "public")
    with TestClient(create_app(settings, start_worker=False)) as cl:
        assert "Approve &amp; Publish" in cl.get("/m").text
        cl.post(f"/m/{cid}/approve", data={}, follow_redirects=False)
    jobs = db.query("SELECT payload_json FROM jobs WHERE kind='deliver_clip'")
    assert sorted(json.loads(j["payload_json"])["account_id"] for j in jobs) == sorted([yt["id"], fb["id"], ig["id"]])

    instagram = OkIG()
    res = _run_jobs(settings, db, {"youtube": OkYT(), "facebook": RefusingFB(), "instagram": instagram})
    assert res["youtube"] == "ok" and res["instagram"] == "ok" and "refused" in res["facebook"]      # one failure, two successes
    st = {r["platform"]: r["status"] for r in db.query("SELECT platform,status FROM publish_attempts")}
    assert st == {"youtube": "published", "instagram": "published", "facebook": "failed"}

    # Retrying: only the failed destination is attempted again; nothing is posted twice.
    res2 = _run_jobs(settings, db, {"youtube": OkYT(), "facebook": OkFB(), "instagram": instagram})
    assert instagram.calls == 1                                                                      # no duplicate Instagram post
    st2 = {r["platform"]: r["status"] for r in db.query("SELECT platform,status FROM publish_attempts WHERE status!='failed'")}
    assert st2 == {"youtube": "published", "instagram": "published", "facebook": "published"}
    assert db.one("SELECT COUNT(*) n FROM publish_attempts WHERE platform='youtube'")["n"] == 1

    with TestClient(create_app(settings, start_worker=False)) as cl:
        html = cl.get("/m/results").text
        assert html.count("published publicly") >= 3


def test_master_switch_and_disabled_destination_stop_everything(settings, db):
    from clipengine import recording_switch
    cid = mk_clip(settings, db)
    yt = enable(db, "youtube")
    delivery.set_mode(db, yt["id"], "public")
    with TestClient(create_app(settings, start_worker=False)) as cl:
        cl.post(f"/m/{cid}/approve", data={}, follow_redirects=False)
    recording_switch.set_delivery_enabled(db, False)                                       # James's stop-everything switch
    res = _run_jobs(settings, db, {"youtube": OkYT()})
    assert "TransientError" in res["youtube"] and db.one("SELECT COUNT(*) n FROM publish_attempts")["n"] == 0
    recording_switch.set_delivery_enabled(db, True)
    db.update("social_accounts", yt["id"], publishing_enabled=0)                           # destination switched off
    _run_jobs(settings, db, {"youtube": OkYT()})
    assert db.one("SELECT COUNT(*) n FROM publish_attempts")["n"] == 0
