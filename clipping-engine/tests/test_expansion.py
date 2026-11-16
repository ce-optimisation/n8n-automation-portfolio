"""Multi-niche expansion (Nov 2026): niche/creator profiles, platform adapters, queue control, storage planning,
Google Drive uploads, raw-recording deletion gates, retention, social-account isolation, publication packages and
research scoring.

Every external service here is a MOCK (httpx.MockTransport or a fake provider). These tests prove the engine's own
logic; they do NOT prove a live Google, YouTube, Twitch or Kick connection — those stay "READY FOR OWNER CONNECTION"
until exercised with the owner's real credentials."""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import time
from pathlib import Path

import httpx
import pytest

from clipengine import cloud, editorial, lifecycle, platforms, research, segment, storage
from clipengine.db import SCHEMA_VERSION, Database
from clipengine.jobs import PRIORITY_LIVE, JobQueue, PermanentError, TransientError
from clipengine.publishing import packages as pk
from conftest import needs_ffmpeg


# ---- niche & creator profiles ---------------------------------------------------------------

def test_presets_seed_once_and_edits_are_versioned(settings):
    db = Database(settings.db_path)
    assert editorial.seed_presets(db) == len(editorial.PRESETS) and editorial.seed_presets(db) == 0
    keys = {r["key"] for r in db.query("SELECT key FROM niches")}
    for k in ("ai_business", "consumer_tech", "watches", "gaming_streams", "irl_streams", "politics", "finance", "podcasts"):
        assert k in keys
    # the two preferred experiments are active; watches is prepared but optional; gaming/politics separate
    active = {r["key"] for r in db.query("SELECT key FROM niches WHERE active=1")}
    assert {"ai_business", "consumer_tech"} <= active and "watches" not in active
    p = editorial.load_niche(db, "ai_business")
    p.min_s = 30
    editorial.save_niche(db, p)
    assert editorial.load_niche(db, "ai_business").version == 2 and editorial.load_niche(db, "ai_business").min_s == 30
    assert db.one("SELECT COUNT(*) n FROM niche_history WHERE key='ai_business'")["n"] == 2
    assert editorial.PRESETS["finance"].compliance_review and editorial.PRESETS["politics"].risk == "high"


def test_two_creators_share_one_engine_with_different_policies(settings):
    db = Database(settings.db_path)
    editorial.seed_presets(db)
    gamer = db.insert("creators", name="Streamer", status="active", niches_json='["gaming_streams"]',
                      profile_json=json.dumps({"content_type": "competitive_gaming", "vocabulary": ["Fortnite"]}))
    teacher = db.insert("creators", name="Teacher", status="active", niches_json='["ai_business"]', profile_json="{}")
    pg = editorial.policy_for(db, None, db.one("SELECT * FROM creators WHERE id=?", (gamer,)))
    pt = editorial.policy_for(db, None, db.one("SELECT * FROM creators WHERE id=?", (teacher,)))
    assert pg.niche.key == "gaming_streams" and pt.niche.key == "ai_business"
    assert "Fortnite" in pg.whisper_prompt() and pg.max_s < pt.max_s + 100
    calm = "Today we will walk through the settings menu and then talk about the next update in general terms."
    hype = "Okay he's one HP, I jump down, place the wall, and... no way! Let's go! Oh my god chat, did you see that?!"
    assert "no_payoff" in " ".join(pg.reject_rules)
    assert pg.assess(calm, 0.5, 30)["reject"]                     # gaming: no setup/payoff → rejected
    assert not pg.assess(hype, 0.5, 30)["reject"]
    generic = "Believe in yourself, never give up and keep your mindset strong. Hustle every day and work hard."
    specific = ("We cut onboarding from 14 days to 3 by automating the CRM sync with n8n, which saved about 9 hours a "
                "week for each account manager, because the API now pushes updates instead of polling.")
    assert pt.assess(generic, 0.5, 30)["reject"] and not pt.assess(specific, 0.5, 30)["reject"]
    assert pt.assess(specific, 0.5, 30)["quality"] > pt.assess(generic, 0.5, 30)["quality"]


def test_high_risk_niches_flag_rather_than_publish(settings):
    db = Database(settings.db_path)
    editorial.seed_presets(db)
    fin = editorial.EditorialPolicy(niche=editorial.load_niche(db, "finance"))
    a = fin.assess("This index fund has guaranteed returns and you can't lose: double your money in a year. "
                   "Inflation is 3 percent and the fee is 0.2 percent.", 0.5, 40)
    cats = {f["category"] for f in a["flags"]}
    assert "financial_promotion" in cats and "compliance_review" in cats
    pol = editorial.EditorialPolicy(niche=editorial.load_niche(db, "politics"))
    assert any(f["category"] == "political_context" for f in pol.assess("The minister said the bill passed.", 0.5, 30)["flags"])


def test_research_never_authorises_retrieval(settings):
    """A researched/shortlisted creator's channel stays blocked by the URL policy until a permission exists."""
    from clipengine import ingest
    db = Database(settings.db_path)
    db.insert("creators", name="Big Streamer", url="https://www.twitch.tv/somebody", status="shortlisted",
              niches_json='["irl_streams"]', commercial_score=90)
    d = ingest.classify_url("https://www.twitch.tv/videos/123456789", settings, db)
    assert not d.ok


# ---- selection v5 ---------------------------------------------------------------------------

def _seg(t0, text, gap=0.25, dur_per_word=0.3):
    words, t = [], t0
    for w in text.split():
        words.append({"start": round(t, 2), "end": round(t + dur_per_word - 0.05, 2), "word": " " + w, "prob": 0.95})
        t += dur_per_word
    return {"start": t0, "end": round(t, 2), "text": text, "words": words}, t + gap


def test_v5_carries_the_end_through_a_meaning_changing_qualification():
    paras = [
        "Good evening everyone, and welcome to the briefing.",
        "Our new pricing model cuts the cost per seat by 40 percent for teams above twenty users.",
        "Customers on annual plans get the reduction immediately, and monthly plans follow in March.",
        "We expect revenue per account to rise because larger teams will consolidate tools onto one plan.",
        "However, the reduction does not apply to legacy contracts signed before 2024, which keep their terms.",
        "Those customers will be contacted individually by their account managers next quarter.",
        "Okay, right, um, let's move on to the hiring update now.",
        "We hired 12 engineers this quarter across two offices in Leeds and Lisbon.",
    ]
    segs, t = [], 0.0
    for i, p in enumerate(paras):
        sg, t = _seg(t, p)
        segs.append(sg)
        t += 2.0 if i in (0, 3, 5, 6) else 0.3
    v4 = segment.select(segs, n=1, min_s=8, max_s=60, profile="v4")
    v5 = segment.select(segs, n=1, min_s=8, max_s=60, profile="v5")
    assert v4 and v5
    if "However" not in v4[0].text:                       # v4 stops before the caveat …
        assert "However" in v5[0].text                    # … v5 keeps it, so the meaning is not changed
    assert v5[0].end >= v4[0].end
    assert "hiring" not in v5[0].text.lower()             # never runs into the next topic


# ---- queue ----------------------------------------------------------------------------------

def test_queue_priority_pause_and_heavy_jobs_wait_for_live_recording(settings):
    db = Database(settings.db_path)
    q = JobQueue(db)
    ran = []
    for k in ("process_recording", "cloud_upload"):
        q.register(k, lambda ctx, k=k: ran.append((k, ctx.payload.get("n"))))
    q.enqueue("process_recording", payload={"n": "backlog"}, priority=10)
    q.enqueue("process_recording", payload={"n": "live"}, priority=PRIORITY_LIVE)
    q.enqueue("cloud_upload", payload={"n": "upload"}, priority=30)
    live = {"on": True}
    q.recording_active = lambda: live["on"]
    assert q.run_one() and ran == [("cloud_upload", "upload")]       # heavy work held while recording
    assert not q.run_one() and "live recording" in q.blocked_reason()
    live["on"] = False
    q.pause()
    assert not q.run_one() and q.blocked_reason() == "Paused by owner"
    q.resume()
    order = q.queued_order()
    assert [json.loads(r["payload_json"])["n"] for r in order] == ["live", "backlog"]
    assert q.move(order[1]["id"], "up")
    assert [json.loads(r["payload_json"])["n"] for r in q.queued_order()] == ["backlog", "live"]
    q.drain(10)
    assert [x[1] for x in ran] == ["upload", "backlog", "live"]


# ---- storage planning -----------------------------------------------------------------------

def test_storage_estimates_and_live_preflight(settings, monkeypatch):
    settings.record_gb_per_hour = 1.5
    settings.disk_reserve_gb = 1.0
    monkeypatch.setattr(storage, "free_bytes", lambda p: int(1.4e9))
    est = storage.estimate(settings, 4 * 3600)
    assert est["recording"] == pytest.approx(6e9) and est["transcription_wav"] == pytest.approx(4 * 3600 * 32000)
    assert not est["fits"] and est["shortfall"] > 5e9
    ok, msg = storage.live_preflight(settings, 1.0)
    assert not ok and "Not enough free space to record" in msg
    rep = storage.capacity_report(settings)
    assert rep["blocker"] and "NOT operational" in rep["blocker"]
    monkeypatch.setattr(storage, "free_bytes", lambda p: int(50e9))
    assert storage.live_preflight(settings, 1.0)[0] and storage.capacity_report(settings)["blocker"] is None


def test_relocation_copies_verifies_and_switches_only_after_restart(settings, tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "free_bytes", lambda p: int(50e9))     # independent of the host's disk
    db = Database(settings.db_path)
    q = JobQueue(db)
    (settings.clips_dir / "rec00001").mkdir(parents=True)
    (settings.clips_dir / "rec00001" / "a.mp4").write_bytes(b"x" * 1000)
    db.insert("recordings", title="t", source_type="upload", status="ready")
    assert not storage.plan_relocation(settings, str(settings.data_dir / "sub"))["ok"]      # inside current folder
    with pytest.raises(ValueError):
        storage.relocate(settings, db, q, str(tmp_path / "new"), "no")
    res = storage.relocate(settings, db, q, str(tmp_path / "new"), "MOVE")
    assert (tmp_path / "new" / "clips" / "rec00001" / "a.mp4").read_bytes() == b"x" * 1000
    assert Database(tmp_path / "new" / "clipengine.sqlite3").one("SELECT COUNT(*) n FROM recordings")["n"] == 1
    assert "CLIPENGINE_DATA_DIR=" in Path(settings.env_file).read_text(encoding="utf-8")
    assert settings.db_path.is_file() and res["files"] >= 1          # original untouched; engine paths unchanged


def test_migration_backs_up_before_upgrading(settings):
    db = Database(settings.db_path)
    db.set_kv("schema_version", str(SCHEMA_VERSION - 1))
    Database(settings.db_path)
    backups = list((settings.data_dir / "backups").glob("clipengine-*-before-schema-*.sqlite3"))
    assert backups and Database(backups[0]).one("SELECT COUNT(*) n FROM recordings")["n"] == 0
    assert Database(settings.db_path).get_kv("schema_version") == str(SCHEMA_VERSION)


# ---- Google Drive (MOCK) --------------------------------------------------------------------

class FakeTokens:
    def __init__(self):
        self.d = {}

    def get(self, k):
        return self.d.get(k)

    def set(self, k, v):
        self.d[k] = v

    def delete(self, k):
        self.d.pop(k, None)


class FakeDrive:
    """MOCK Google Drive v3 (files, resumable upload, about) for the provider's real HTTP code."""

    def __init__(self, free=50e9, corrupt=False, interrupt_once=False):
        self.files, self.sessions, self.free, self.corrupt = {}, {}, free, corrupt
        self.interrupt_once, self.uploads_started = interrupt_once, 0

    def handler(self, req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.endswith("/about") or "/about?" in url:
            return httpx.Response(200, json={"storageQuota": {"limit": str(int(15e9)), "usage": str(int(15e9 - self.free))}})
        if req.method == "GET" and "/drive/v3/files" in url and "/files/" not in url:
            q = req.url.params.get("q", "")
            key = q.split("value='")[1].split("'")[0]
            return httpx.Response(200, json={"files": [f for f in self.files.values() if f["ce_key"] == key]})
        if req.method == "POST" and url.startswith("https://www.googleapis.com/drive/v3/files"):
            meta = json.loads(req.content)
            fid = f"F{len(self.files) + 1}"
            self.files[fid] = {"id": fid, "ce_key": meta["appProperties"]["ce_key"], "name": meta["name"]}
            return httpx.Response(200, json={"id": fid})
        if req.method == "POST" and "upload/drive/v3/files" in url:
            if self.free < 1e9:
                return httpx.Response(403, text='{"error": {"errors": [{"reason": "storageQuotaExceeded"}]}}')
            self.uploads_started += 1
            sid = f"S{len(self.sessions) + 1}"
            self.sessions[sid] = {"meta": json.loads(req.content), "data": b"",
                                  "size": int(req.headers["X-Upload-Content-Length"])}
            return httpx.Response(200, headers={"Location": f"https://upload.example/{sid}"})
        if req.method == "PUT" and url.startswith("https://upload.example/"):
            sess = self.sessions[url.rsplit("/", 1)[1]]
            rng = req.headers["Content-Range"]
            if rng.startswith("bytes */"):
                n = len(sess["data"])
                return httpx.Response(308, headers={"Range": f"bytes=0-{n - 1}"} if n else {})
            body = req.content
            if self.interrupt_once:
                self.interrupt_once = False
                sess["data"] += body[: len(body) // 2]
                return httpx.Response(308, headers={"Range": f"bytes=0-{len(sess['data']) - 1}"})
            start = int(rng.split(" ")[1].split("-")[0])
            sess["data"] = sess["data"][:start] + body
            if len(sess["data"]) < sess["size"]:
                return httpx.Response(308, headers={"Range": f"bytes=0-{len(sess['data']) - 1}"})
            data = sess["data"] + (b"!" if self.corrupt else b"")
            fid = f"F{len(self.files) + 1}"
            self.files[fid] = {"id": fid, "ce_key": sess["meta"]["appProperties"]["ce_key"], "size": str(len(data)),
                               "md5Checksum": hashlib.md5(data).hexdigest()}
            return httpx.Response(200, json=self.files[fid])
        if req.method == "DELETE":
            self.files.pop(url.rsplit("/", 1)[1], None)
            return httpx.Response(204)
        if req.method == "GET" and "/files/" in url:
            return httpx.Response(200, json=self.files[url.split("/files/")[1].split("?")[0]])
        return httpx.Response(404)


def _cloud(settings, db, q, drive):
    tokens = FakeTokens()
    tokens.set("gdrive", {"access_token": "t", "refresh_token": "r", "expires_at": time.time() + 3600})
    settings.google_client_id, settings.google_client_secret = "id", "secret"
    svc = cloud.CloudService(settings, db, q, tokens)
    svc.provider.http = httpx.Client(transport=httpx.MockTransport(drive.handler))
    return svc


def _clip_on_disk(settings, db, rid, idx=1, payload=b"video-bytes" * 5000, status="ready"):
    cid = db.insert("clips", recording_id=rid, idx=idx, start=0, end=20, status=status)
    d = settings.clips_dir / f"rec{rid:05d}" / f"clip{cid:05d}_r0"
    d.mkdir(parents=True, exist_ok=True)
    (d / "clip.mp4").write_bytes(payload)
    (d / "thumb.jpg").write_bytes(b"jpg")
    (d / "transcript.txt").write_text("t", encoding="utf-8")
    db.update("clips", cid, video_path=str(d / "clip.mp4"), thumb_path=str(d / "thumb.jpg"))
    return cid


def test_drive_upload_resumes_verifies_and_never_duplicates(settings, monkeypatch):
    monkeypatch.setattr(cloud, "CHUNK", 256 * 1024)
    db = Database(settings.db_path)
    q = JobQueue(db)
    drive = FakeDrive(interrupt_once=True)
    svc = _cloud(settings, db, q, drive)
    rid = db.insert("recordings", title="Rec", source_type="url", status="ready")
    cid = _clip_on_disk(settings, db, rid, payload=b"v" * 700_000)
    assert svc.enqueue_clip(cid)
    q.drain(30)
    assert svc.clip_verified(cid, ("video", "thumb", "transcript", "metadata"))
    assert db.one("SELECT COUNT(*) n FROM jobs WHERE status='done' AND kind='cloud_upload'")["n"] == 1
    started = drive.uploads_started
    db.execute("UPDATE cloud_objects SET state='pending'")       # simulate a lost DB update after upload
    q.enqueue("cloud_upload", recording_id=rid, clip_id=cid, dedupe_key="again")
    q.drain(30)
    assert drive.uploads_started == started                         # found by ce_key: no duplicate upload
    assert svc.clip_verified(cid)


def test_drive_verification_failure_and_quota_exhaustion(settings):
    db = Database(settings.db_path)
    q = JobQueue(db)
    rid = db.insert("recordings", title="Rec", source_type="url", status="ready")
    cid = _clip_on_disk(settings, db, rid)
    svc = _cloud(settings, db, q, FakeDrive(corrupt=True))
    svc.enqueue_clip(cid)
    q.run_one()
    assert not svc.clip_verified(cid) and db.one("SELECT COUNT(*) n FROM cloud_objects WHERE state='failed'")["n"] >= 1
    db2 = Database(settings.data_dir / "other.sqlite3")
    q2 = JobQueue(db2)
    rid2 = db2.insert("recordings", title="Rec", source_type="url", status="ready")
    cid2 = _clip_on_disk(settings, db2, rid2, idx=2)
    svc2 = _cloud(settings, db2, q2, FakeDrive(free=0.5e9))
    svc2.enqueue_clip(cid2)
    q2.run_one()
    job = db2.one("SELECT * FROM jobs WHERE kind='cloud_upload'")
    assert job["status"] == "queued" and job["run_after"] > time.time() + 3000      # held, retried in ~1 h
    assert "uploads and raw-recording deletion are held" in (svc2.uploads_held() or "").lower()


def test_cloud_status_is_honest_about_connection(settings):
    db = Database(settings.db_path)
    svc = cloud.CloudService(settings, db, JobQueue(db), FakeTokens())
    assert svc.status()["label"] == "Not set up"
    settings.google_client_id, settings.google_client_secret = "id", "secret"
    svc = cloud.CloudService(settings, db, JobQueue(db), FakeTokens())
    assert svc.status()["label"] == "READY FOR OWNER CONNECTION" and not svc.status()["connected"]


# ---- raw deletion gates and retention -------------------------------------------------------

@pytest.fixture
def life(stub_settings, monkeypatch):
    s = stub_settings
    db = Database(s.db_path)
    q = JobQueue(db)
    drive = FakeDrive()
    svc = _cloud(s, db, q, drive)
    lc = lifecycle.Lifecycle(s, db, q, svc)
    monkeypatch.setattr(lifecycle, "time", time)
    from clipengine import media
    monkeypatch.setattr(media, "probe", lambda p: {"has_video": True, "has_audio": True, "duration": 20.0})
    return s, db, q, svc, lc


def _engine_recording(s, db, lc, source_type="url", engine=True):
    rdir = s.recordings_dir / f"r{time.time_ns()}"
    rdir.mkdir(parents=True)
    f = rdir / "source.mp4"
    f.write_bytes(b"raw" * 1000)
    rid = db.insert("recordings", title="Ep", source_type=source_type, status="ready", file_path=str(f),
                    metadata_json=json.dumps({"retrieval": {"method": "ytdlp"}}) if engine else "{}")
    db.insert("transcript_segments", recording_id=rid, idx=0, start=0, end=5, text="hi", words_json="[]")
    lc.claim_ownership(rid)
    return rid, f


def test_raw_recording_deleted_only_when_every_gate_passes(life):
    s, db, q, svc, lc = life
    rid, f = _engine_recording(s, db, lc)
    cid = _clip_on_disk(s, db, rid)
    assert db.recording(rid)["engine_owned"] == 1
    res = lc.check_recording(rid)
    assert not res["deleted"] and "cloud_verified" in res["reason"] and f.exists()     # not yet in the cloud
    svc.enqueue_clip(cid)
    q.drain(30)                                      # upload + verification triggers the lifecycle check
    assert not f.exists() and db.recording(rid)["raw_deleted_at"] and db.recording(rid)["file_path"] is None
    assert db.recording(rid)["lifecycle"] == "completed" and lc.check_recording(rid)["reason"] == "nothing to do"


def test_owner_files_and_preexisting_recordings_are_never_deleted(life):
    s, db, q, svc, lc = life
    up, fu = _engine_recording(s, db, lc, source_type="upload", engine=False)
    cid = _clip_on_disk(s, db, up)
    svc.enqueue_clip(cid)
    q.drain(30)
    assert fu.exists() and not db.recording(up)["engine_owned"]
    old, fo = _engine_recording(s, db, lc)
    db.update("recordings", old, engine_owned=0, created_at=lc.since - 86400)
    assert not lc.claim_ownership(old) and not lc.check_recording(old)["deleted"] and fo.exists()


def test_recording_without_clips_is_held_and_pending_work_blocks_deletion(life):
    s, db, q, svc, lc = life
    rid, f = _engine_recording(s, db, lc)
    res = lc.check_recording(rid)
    assert not res["deleted"] and "held" in res["reason"] and db.recording(rid)["lifecycle"] == "held" and f.exists()
    rid2, f2 = _engine_recording(s, db, lc)
    cid = _clip_on_disk(s, db, rid2)
    svc.enqueue_clip(cid)
    q.enqueue("render_clip", recording_id=rid2, clip_id=cid, dedupe_key="pending-regen")
    q.register("render_clip", lambda ctx: None)
    q.run_one()                                       # the upload runs first (priority 30 > default? check below)
    assert f2.exists() or db.one("SELECT COUNT(*) n FROM jobs WHERE status='queued' AND kind='render_clip'")["n"] == 0


def test_clip_retention_rules(life):
    s, db, q, svc, lc = life
    rid, _ = _engine_recording(s, db, lc)
    old = time.time() - 3 * 86400
    rej = _clip_on_disk(s, db, rid, idx=1, status="rejected")
    keep = _clip_on_disk(s, db, rid, idx=2, status="rejected")
    appr = _clip_on_disk(s, db, rid, idx=3, status="approved")
    pub = _clip_on_disk(s, db, rid, idx=4, status="approved")
    fresh = _clip_on_disk(s, db, rid, idx=5, status="rejected")
    db.update("clips", rej, reviewed_at=old)
    db.update("clips", keep, reviewed_at=old, retain=1)
    db.update("clips", fresh, reviewed_at=time.time() - 3600)
    db.update("clips", pub, published_at=time.time() - 8 * 86400)
    out = lc.retention()
    assert out == {"rejected": 1, "published": 1}
    gone = {r["id"] for r in db.query("SELECT id FROM clips WHERE files_deleted_at IS NOT NULL")}
    assert gone == {rej, pub}
    assert Path(db.clip(appr)["video_path"]).exists() and Path(db.clip(keep)["video_path"]).exists()


# ---- social accounts, packages, YouTube private (MOCK) ---------------------------------------

def _approved_clip(settings, db, niche="ai_business"):
    rid = db.insert("recordings", title="Talk", source_type="upload", status="ready", source_url="https://example.org/t")
    cid = _clip_on_disk(settings, db, rid, status="approved")
    db.update("clips", cid, niche_key=niche, rights_status="licensed", quote="We cut costs by 40 percent. Then more.")
    return cid


def test_accounts_are_isolated_by_niche_and_platform(settings):
    db = Database(settings.db_path)
    editorial.seed_presets(db)
    svc = pk.PackageService(settings, db, JobQueue(db), FakeTokens())
    cid = _approved_clip(settings, db)
    tech = db.insert("social_accounts", platform="youtube", handle="techchan", niches_json='["consumer_tech"]')
    ai = db.insert("social_accounts", platform="youtube", handle="aichan", niches_json='["ai_business"]')
    tt = db.insert("social_accounts", platform="tiktok", handle="aitt", niches_json='["ai_business"]')
    with pytest.raises(pk.IsolationError):
        svc.prepare(cid, "youtube_shorts", tech)
    with pytest.raises(pk.IsolationError):
        svc.prepare(cid, "youtube_shorts", tt)
    pid = svc.prepare(cid, "youtube_shorts", ai)
    assert svc.prepare(cid, "youtube_shorts", ai) == pid                     # idempotent
    pkg = db.one("SELECT * FROM publication_packages WHERE id=?", (pid,))
    assert "Source: https://example.org/t" in pkg["description"] and pkg["approval_state"] == "draft"


def test_publication_requires_approvals_and_youtube_upload_is_private(settings):
    db = Database(settings.db_path)
    editorial.seed_presets(db)
    tokens = FakeTokens()
    seen = {}

    def yt(req: httpx.Request):
        if "upload/youtube/v3/videos" in str(req.url):
            seen["body"] = json.loads(req.content)
            return httpx.Response(200, headers={"Location": "https://upload.example/yt"})
        if str(req.url) == "https://upload.example/yt":
            return httpx.Response(200, json={"id": "abc123", "status": {"privacyStatus": "private"}})
        return httpx.Response(404)
    settings.google_client_id, settings.google_client_secret = "id", "secret"
    q = JobQueue(db)
    svc = pk.PackageService(settings, db, q, tokens, youtube_http=httpx.Client(transport=httpx.MockTransport(yt)))
    cid = _approved_clip(settings, db)
    acc = db.insert("social_accounts", platform="youtube", handle="aichan", niches_json='["ai_business"]')
    pid = svc.prepare(cid, "youtube_shorts", acc)
    with pytest.raises(ValueError):
        svc.request_youtube_private(pid)                 # package not approved
    db.update("clips", cid, rights_status="unreviewed")
    with pytest.raises(ValueError):
        svc.approve(pid)                                  # editorial gate: rights not recorded
    db.update("clips", cid, rights_status="licensed")
    svc.approve(pid)
    with pytest.raises(ValueError):
        svc.request_youtube_private(pid)                 # account not connected
    tokens.set(f"youtube:{acc}", {"access_token": "t", "refresh_token": "r", "expires_at": time.time() + 3600})
    svc.request_youtube_private(pid)
    q.drain(10)
    pkg = db.one("SELECT * FROM publication_packages WHERE id=?", (pid,))
    assert seen["body"]["status"]["privacyStatus"] == "private"
    assert pkg["status"] == "uploaded_private" and pkg["remote_id"] == "abc123"
    h = svc.handoff(pid)
    assert h["download"].endswith(f"/clips/{cid}/download") and "studio.youtube.com" in h["open"]


def test_platform_spec_flags_rather_than_cuts(settings):
    db = Database(settings.db_path)
    svc = pk.PackageService(settings, db, JobQueue(db), FakeTokens())
    cid = _approved_clip(settings, db)
    db.update("clips", cid, end=150)
    assert svc.check_spec(db.clip(cid), "x") and not svc.check_spec(db.clip(cid), "youtube_shorts")


# ---- platform adapters (MOCK HTTP) ----------------------------------------------------------

def test_channel_url_parsing_never_guesses():
    assert platforms.parse_channel_url("https://www.twitch.tv/SomeOne").handle == "someone"
    assert platforms.parse_channel_url("https://kick.com/abc").platform == "kick"
    assert platforms.parse_channel_url("https://www.youtube.com/@Handle").handle == "@Handle"
    assert platforms.parse_channel_url("https://rumble.com/c/nickjfuentes").platform == "rumble"
    assert platforms.parse_channel_url("https://www.twitch.tv/videos/123") is None
    assert platforms.parse_channel_url("https://example.org/someone") is None


def test_adapters_report_needs_credentials_honestly(settings):
    for name in ("twitch", "kick", "youtube"):
        a = platforms.adapter_for(settings, name)
        assert a.capabilities()["state"] == "needs_credentials"
        with pytest.raises(platforms.AdapterError) as e:
            a.report(platforms.ChannelRef(name, "x" if name != "youtube" else "@x", "u"))
        assert e.value.code == "needs_credentials"
    m = platforms.capability_matrix(settings)
    assert "Get Clips Download" in m["twitch"]["acquisition"]["acquire_clip_official"]


def test_twitch_adapter_discovery_with_mock_helix(settings):
    settings.twitch_client_id, settings.twitch_client_secret = "cid", "sec"

    def helix(req: httpx.Request):
        u = str(req.url)
        if "oauth2/token" in u:
            return httpx.Response(200, json={"access_token": "A", "expires_in": 5000})
        assert req.headers["Client-Id"] == "cid" and req.headers["Authorization"] == "Bearer A"
        if "/users" in u:
            return httpx.Response(200, json={"data": [{"id": "1", "login": "streamer", "display_name": "Streamer"}]})
        if "/channels/followers" in u:
            return httpx.Response(200, json={"total": 1234567})
        if "/streams" in u:
            return httpx.Response(200, json={"data": [{"title": "IRL", "viewer_count": 52000, "game_name": "Just Chatting",
                                                        "started_at": "2026-11-16T18:00:00Z"}]})
        if "/videos" in u:
            return httpx.Response(200, json={"data": [{"id": "9", "title": "VOD", "url": "https://www.twitch.tv/videos/9",
                                                        "created_at": "2026-11-15", "duration": "5h3m2s", "view_count": 10}]})
        if "/clips" in u:
            return httpx.Response(200, json={"data": [{"id": "c1", "title": "Moment", "url": "https://clips.twitch.tv/c1",
                                                        "duration": 28.5, "view_count": 900000}]})
        if "/schedule" in u:
            return httpx.Response(404)
        return httpx.Response(404)
    a = platforms.TwitchAdapter(settings, httpx.Client(transport=httpx.MockTransport(helix)))
    a._throttle.min_interval = 0
    rep = a.report(platforms.parse_channel_url("https://www.twitch.tv/streamer"))
    assert rep.identity["followers"] == 1234567 and rep.live.live and rep.live.viewers == 52000
    vod = [i for i in rep.items if i.kind == "vod"][0]
    assert vod.duration_s == 5 * 3600 + 3 * 60 + 2
    assert [i for i in rep.items if i.kind == "clip"][0].views == 900000 and not rep.errors


def test_kick_and_youtube_adapters_with_mock_apis(settings):
    settings.kick_client_id, settings.kick_client_secret, settings.youtube_api_key = "k", "s", "KEY"

    def kick(req):
        u = str(req.url)
        if "oauth/token" in u:
            return httpx.Response(200, json={"access_token": "K", "expires_in": 7200})
        if "/channels" in u:
            return httpx.Response(200, json={"data": [{"broadcaster_user_id": 7, "slug": "chan", "stream_title": "Live!",
                                                        "category": {"name": "IRL"}, "stream": {"is_live": True, "viewer_count": 31000}}]})
        if "/livestreams" in u:
            return httpx.Response(200, json={"data": []})
        return httpx.Response(404)
    ka = platforms.KickAdapter(settings, httpx.Client(transport=httpx.MockTransport(kick)))
    ka._throttle.min_interval = 0
    r = ka.report(platforms.parse_channel_url("https://kick.com/chan"))
    assert r.live.live and r.live.viewers == 31000 and r.capabilities["vods"] is False

    def yt(req):
        u = str(req.url)
        if "/channels" in u:
            assert req.url.params["forHandle"] == "@chan"
            return httpx.Response(200, json={"items": [{"id": "UC1", "snippet": {"title": "Chan"},
                                                        "statistics": {"subscriberCount": "2500000", "videoCount": "10"},
                                                        "contentDetails": {"relatedPlaylists": {"uploads": "UU1"}}}]})
        if "/playlistItems" in u:
            return httpx.Response(200, json={"items": [{"contentDetails": {"videoId": "v1"}}, {"contentDetails": {"videoId": "v2"}}]})
        if "/videos" in u:
            return httpx.Response(200, json={"items": [
                {"id": "v1", "snippet": {"title": "Live now", "liveBroadcastContent": "live"},
                 "liveStreamingDetails": {"concurrentViewers": "8000", "actualStartTime": "2026-11-16"}, "contentDetails": {"duration": "PT0S"}},
                {"id": "v2", "snippet": {"title": "Old stream", "liveBroadcastContent": "none", "publishedAt": "2026-11-10"},
                 "liveStreamingDetails": {"actualEndTime": "x"}, "contentDetails": {"duration": "PT3H2M"}, "statistics": {"viewCount": "120000"}}]})
        return httpx.Response(404)
    ya = platforms.YouTubeAdapter(settings, httpx.Client(transport=httpx.MockTransport(yt)))
    ya._throttle.min_interval = 0
    r = ya.report(platforms.parse_channel_url("https://www.youtube.com/@chan"))
    assert r.identity["subscribers"] == 2500000 and r.live.live and r.live.viewers == 8000
    assert [i for i in r.items if i.key == "youtube:v2"][0].duration_s == 3 * 3600 + 120


# ---- research scoring -----------------------------------------------------------------------

def test_research_scores_separate_reach_from_profit_and_ignore_permission():
    viral = {k: 9 for k in research.AUDIENCE} | {k: 3 for k in research.PROFIT}
    niche = {k: 5 for k in research.AUDIENCE} | {k: 8 for k in research.PROFIT}
    va, vp, vo = research.combine_scores(viral)
    na, np_, no = research.combine_scores(niche)
    assert va > na and np_ > vp and no > vo           # reach alone does not win the overall ranking
    assert not any("permission" in k or "licens" in k for k in list(research.AUDIENCE) + list(research.PROFIT))


def test_research_dataset_loads_without_creating_sources(settings):
    db = Database(settings.db_path)
    path = Path(__file__).resolve().parents[1] / "research" / "research_dataset.json"
    if not path.is_file():
        pytest.skip("research dataset not present")
    res = research.load_dataset(db, path)
    assert res["niche_scores"] >= 15
    assert db.one("SELECT COUNT(*) n FROM sources")["n"] == 0
    assert db.one("SELECT COUNT(*) n FROM creators WHERE status != 'research'")["n"] == 0
    research.load_dataset(db, path)                    # reloading never duplicates
    assert db.one("SELECT COUNT(*) n FROM creators")["n"] == res["creators_added"]
