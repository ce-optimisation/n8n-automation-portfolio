"""Verified-upload local cleanup. A fake in-memory Drive and temporary directories: no real files, services or accounts."""
import hashlib
import json
import os
import threading
import time

import pytest

from clipengine import cleanup, cloud as cloudmod
from clipengine.db import Database
from clipengine.jobs import JobQueue

NOW = time.time()
HOUR = 3600


class FakeDrive(cloudmod.StorageProvider):
    name = "gdrive"

    def __init__(self):
        self.files = {}              # id -> bytes
        self.online = True
        self.fail_download = None

    def connected(self):
        return self.online

    def put(self, data: bytes) -> str:
        rid = f"d{len(self.files) + 1}"
        self.files[rid] = data
        return rid

    def metadata(self, remote_id):
        if remote_id not in self.files:
            raise RuntimeError("404 not found")
        d = self.files[remote_id]
        return {"id": remote_id, "size": str(len(d)), "md5Checksum": hashlib.md5(d).hexdigest()}  # noqa: S324

    def download(self, remote_id, dest, progress=None):
        if self.fail_download:
            raise self.fail_download
        d = self.files[remote_id]
        dest.write_bytes(d)
        return len(d)

    def delete(self, remote_id):
        self.files.pop(remote_id, None)


class FakeCloud:
    def __init__(self, provider):
        self.provider = provider
        self.on_verified = []

    def clip_verified(self, clip_id, required=("video", "metadata")):
        return True

    def delete_clip_objects(self, clip_id, force=False):
        return 0


@pytest.fixture
def env(settings):
    db = Database(settings.db_path)
    drive = FakeDrive()
    q = JobQueue(db)
    lc = cleanup.LocalCleanup(settings, db, q, FakeCloud(drive), clock=lambda: NOW)
    lc.set_enabled(True)
    return db, drive, lc


def make_clip(settings, db, drive, *, niche="politics", brand="america_first_monitor", dests=("rumble",), data=b"V" * 4096,
              approved=True, verified=True, age=3 * HOUR, confirm_ago=30 * HOUR, account=True):
    rid = db.insert("recordings", title="Rec", source_type="url", status="ready", engine_owned=1)
    cid = db.insert("clips", recording_id=rid, idx=1, start=10.0, end=80.0, status="approved" if approved else "ready",
                    rights_status="owned", safety_status="clear", niche_key=niche, render_rev=0)
    d = settings.clips_dir / f"rec{rid:05d}" / f"clip{cid:05d}_r0"
    d.mkdir(parents=True)
    path = d / "clip.mp4"
    path.write_bytes(data)
    os.utime(path, (NOW - age, NOW - age))
    db.update("clips", cid, video_path=str(path))
    md5 = hashlib.md5(data).hexdigest()  # noqa: S324
    rem = drive.put(data)
    for kind, payload in (("video", data), ("metadata", b"{}")):
        r = rem if kind == "video" else drive.put(payload)
        db.insert("cloud_objects", provider="gdrive", owner_kind="clip", owner_id=cid, kind=kind, remote_id=r,
                  local_md5=hashlib.md5(payload).hexdigest(), size=len(payload),  # noqa: S324
                  state="verified" if verified else "uploading", local_path=str(path))
    pkgs = {}
    for plat in dests:
        acct = None
        if account:
            ap = {"youtube_shorts": "youtube", "tiktok": "tiktok", "instagram_reels": "instagram", "facebook_reels": "facebook",
                  "x": "x", "rumble": "rumble"}[plat]
            acct = db.insert("social_accounts", platform=ap, handle=f"{brand}_{plat}_{cid}", brand=brand,
                             niches_json=json.dumps([niche]), status="manual")
        pkgs[plat] = db.insert("publication_packages", clip_id=cid, platform=plat, account_id=acct, status="handoff_ready",
                               approval_state="approved", upload_state="not_started", history_json="[]")
    return cid, pkgs, path


def confirm(lc, pkgs, ago=30 * HOUR):
    for pid in pkgs.values():
        lc.confirm_destination(pid, "checked")
        lc.db.update("publication_packages", pid, handoff_at=NOW - ago)


def blockers(lc, cid, **kw):
    return " | ".join(lc.evaluate(cid, deep=True, **kw)["blockers"])


# ---- the happy path ---------------------------------------------------------------------------------

def test_deletes_only_after_every_gate_and_keeps_drive_and_record(settings, env):
    db, drive, lc = env
    cid, pkgs, path = make_clip(settings, db, drive)
    assert lc.evaluate(cid, deep=True)["state"] == "DESTINATIONS_PENDING"
    confirm(lc, pkgs)
    ev = lc.evaluate(cid, deep=True)
    assert ev["eligible"] and ev["state"] == "READY_FOR_CLEANUP"
    res = lc.delete(cid)
    assert res["deleted"] and not path.exists()
    assert len(drive.files) == 2                                        # Drive master and metadata untouched
    row = db.clip(cid)
    assert row["local_deleted_at"] and row["cleanup_state"] == "LOCAL_DELETED" and row["video_path"] == str(path)
    kinds = [e["kind"] for e in db.query("SELECT kind FROM cleanup_events WHERE clip_id=? ORDER BY id", (cid,))]
    assert kinds.index("deleting") < kinds.index("deleted")             # the decision is recorded first
    again = lc.delete(cid)                                              # idempotent
    assert again["deleted"] is False and "already" in again["reason"]
    assert lc.run()["deleted"] == 0


def test_restore_returns_an_identical_file_and_protects_it(settings, env):
    db, drive, lc = env
    cid, pkgs, path = make_clip(settings, db, drive, data=b"MASTER" * 1000)
    confirm(lc, pkgs)
    assert lc.delete(cid)["deleted"]
    res = lc.restore(cid)
    assert res["restored"] and path.read_bytes() == b"MASTER" * 1000
    row = db.clip(cid)
    assert row["local_deleted_at"] is None and row["cleanup_state"] == "RESTORED" and row["restore_grace_until"] > NOW
    os.utime(path, (NOW - 3 * HOUR, NOW - 3 * HOUR))
    assert lc.evaluate(cid, deep=True)["state"] == "RESTORED"
    assert lc.delete(cid)["deleted"] is False                           # never deleted straight after a restore
    assert lc.restore(cid)["reason"].startswith("the identical")        # idempotent


def test_restore_failure_modes_are_reported_not_faked(settings, env):
    db, drive, lc = env
    cid, pkgs, path = make_clip(settings, db, drive)
    confirm(lc, pkgs)
    lc.delete(cid)
    row = db.one("SELECT * FROM cloud_objects WHERE owner_id=? AND kind='video'", (cid,))
    drive.files[row["remote_id"]] = b"corrupted"                        # Drive copy changed
    res = lc.restore(cid)
    assert not res["restored"] and not path.exists() and db.clip(cid)["cleanup_state"] == "LOCAL_DELETED"
    drive.files.pop(row["remote_id"])                                   # missing in Drive
    assert not lc.restore(cid)["restored"]
    drive.files[row["remote_id"]] = b"V" * 4096
    drive.fail_download = ConnectionError("boom")                       # interrupted download leaves no partial file
    assert not lc.restore(cid)["restored"] and not list(path.parent.glob("*.partial")) and not path.exists()
    drive.fail_download = None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"newer local render")                             # never overwrite a different local file
    res = lc.restore(cid)
    assert not res["restored"] and path.read_bytes() == b"newer local render"


# ---- blocked cases -------------------------------------------------------------------------------------

def test_blocked_when_drive_master_missing_changed_or_removed(settings, env):
    db, drive, lc = env
    cid, pkgs, path = make_clip(settings, db, drive)
    confirm(lc, pkgs)
    vid = db.one("SELECT * FROM cloud_objects WHERE owner_id=? AND kind='video'", (cid,))
    drive.files[vid["remote_id"]] = b"different"
    assert "no longer matches" in blockers(lc, cid)
    drive.files.pop(vid["remote_id"])                                    # removed by another retention process
    assert "may have been removed" in blockers(lc, cid)
    db.execute("UPDATE cloud_objects SET state='deleted' WHERE id=?", (vid["id"],))
    assert "not verified" in blockers(lc, cid)
    assert lc.delete(cid)["deleted"] is False and path.exists()
    drive.online = False
    db.execute("UPDATE cloud_objects SET state='verified' WHERE id=?", (vid["id"],))
    assert "not connected" in blockers(lc, cid)


def test_blocked_when_local_file_differs_from_verified_version(settings, env):
    db, drive, lc = env
    cid, pkgs, path = make_clip(settings, db, drive)
    confirm(lc, pkgs)
    path.write_bytes(b"X" * 4096)                                        # same size, different content
    os.utime(path, (NOW - 3 * HOUR, NOW - 3 * HOUR))
    assert "checksum" in blockers(lc, cid)


def test_blocked_without_verified_drive_copy_or_while_upload_in_progress(settings, env):
    db, drive, lc = env
    cid, pkgs, path = make_clip(settings, db, drive, verified=False)
    confirm(lc, pkgs)
    assert "Drive video copy is not verified" in blockers(lc, cid) and lc.evaluate(cid)["state"] == "LOCAL_PRESENT"
    cid2, pkgs2, path2 = make_clip(settings, db, drive)
    confirm(lc, pkgs2)
    db.insert("jobs", kind="youtube_private_upload", clip_id=cid2, status="running", payload_json="{}", priority=0,
              max_attempts=3, message="")
    assert "job(s) still use this clip" in blockers(lc, cid2)


@pytest.mark.parametrize("plat,setup,expect", [
    ("tiktok", {"att": ("delivered_inbox", None)}, "inbox"),
    ("tiktok", {"att": ("processing", None)}, "processing"),
    ("instagram_reels", {"att": ("processing", None)}, "container is not a draft"),
    ("instagram_reels", {"att": ("uploading", None)}, "container is not a draft"),
    ("facebook_reels", {}, "waiting for you"),
    ("youtube_shorts", {"pkg": {"status": "uploaded_private"}}, "check it in YouTube Studio"),
    ("rumble", {"pkg": {"status": "failed"}}, "upload failed"),
])
def test_unverified_or_partial_platform_states_never_count(settings, env, plat, setup, expect):
    db, drive, lc = env
    cid, pkgs, path = make_clip(settings, db, drive, dests=(plat,))
    if "att" in setup:
        db.insert("publish_attempts", clip_id=cid, platform={"tiktok": "tiktok", "instagram_reels": "instagram"}[plat],
                  status=setup["att"][0], confirmed_at=NOW - 30 * HOUR if setup["att"][0] == "delivered_inbox" else None)
    if "pkg" in setup:
        db.update("publication_packages", pkgs[plat], **setup["pkg"])
    assert expect in blockers(lc, cid)
    assert lc.delete(cid)["deleted"] is False and path.exists()


def test_confirmed_platform_states_do_count_and_label_the_method(settings, env):
    db, drive, lc = env
    cid, pkgs, path = make_clip(settings, db, drive, dests=("youtube_shorts", "tiktok", "instagram_reels"))
    db.update("publication_packages", pkgs["youtube_shorts"], status="uploaded_private", remote_id="yt1",
              handoff_state="private_verified", handoff_method="api", handoff_at=NOW - 30 * HOUR)
    db.insert("publish_attempts", clip_id=cid, platform="tiktok", status="published", confirmed_at=NOW - 30 * HOUR, remote_id="t1")
    db.insert("publish_attempts", clip_id=cid, platform="instagram", status="published", confirmed_at=NOW - 30 * HOUR, remote_id="m1")
    ev = lc.evaluate(cid, deep=True)
    assert ev["eligible"]
    by = {d["platform"]: d for d in ev["destinations"]}
    assert by["youtube_shorts"]["method"] == "api" and by["tiktok"]["state"] == "published"
    tt = {"delivered_inbox": 0}
    db.execute("UPDATE publish_attempts SET status='delivered_inbox' WHERE platform='tiktok'")
    assert not lc.evaluate(cid, deep=True)["eligible"]
    lc.confirm_destination(pkgs["tiktok"], "saved it as a draft in the app")      # manual, labelled as such
    ev = lc.evaluate(cid, deep=True)
    assert {d["platform"]: d["method"] for d in ev["destinations"]}["tiktok"] == "manual"


def test_failed_destination_and_missing_account_block(settings, env):
    db, drive, lc = env
    cid, pkgs, path = make_clip(settings, db, drive, dests=("rumble", "x"))
    lc.confirm_destination(pkgs["rumble"], "ok")
    db.update("publication_packages", pkgs["rumble"], handoff_at=NOW - 30 * HOUR)
    db.update("publication_packages", pkgs["x"], status="failed")
    assert "X:" in blockers(lc, cid)
    db.update("publication_packages", pkgs["x"], account_id=None)
    assert "no account assigned" in blockers(lc, cid)
    cid2, pkgs2, _ = make_clip(settings, db, drive, dests=("rumble",), account=False)
    assert "no account assigned" in blockers(lc, cid2)                    # an unconfigured account is not a success


def test_no_required_destination_means_no_deletion_and_exclusion_is_explicit_and_recorded(settings, env):
    db, drive, lc = env
    cid, pkgs, path = make_clip(settings, db, drive, dests=())
    assert "no required destination" in blockers(lc, cid)
    cid2, pkgs2, path2 = make_clip(settings, db, drive, dests=("rumble", "facebook_reels"))
    lc.confirm_destination(pkgs2["rumble"], "ok")
    db.update("publication_packages", pkgs2["rumble"], handoff_at=NOW - 30 * HOUR)
    assert not lc.evaluate(cid2, deep=True)["eligible"]
    with pytest.raises(ValueError):
        lc.exclude_destination(pkgs2["facebook_reels"], "  ")             # a reason is mandatory
    lc.exclude_destination(pkgs2["facebook_reels"], "90 s limit; clip is 70 s? owner chose to skip")
    assert lc.evaluate(cid2, deep=True)["eligible"]
    ex = db.one("SELECT * FROM publication_packages WHERE id=?", (pkgs2["facebook_reels"],))
    assert ex["required"] == 0 and ex["excluded_at"] and ex["exclude_reason"]
    lc.include_destination(pkgs2["facebook_reels"])
    assert not lc.evaluate(cid2, deep=True)["eligible"]


def test_destination_added_during_recovery_window_pauses_and_restarts_the_window(settings, env):
    db, drive, lc = env
    cid, pkgs, path = make_clip(settings, db, drive)
    confirm(lc, pkgs, ago=30 * HOUR)
    assert lc.evaluate(cid, deep=True)["eligible"]
    new = db.insert("publication_packages", clip_id=cid, platform="x", account_id=db.insert(
        "social_accounts", platform="x", handle="xacct", brand="america_first_monitor", niches_json='["politics"]', status="manual"),
        status="prepared", approval_state="approved", upload_state="not_started", history_json="[]")
    assert lc.evaluate(cid, deep=True)["state"] == "DESTINATIONS_PENDING" and lc.delete(cid)["deleted"] is False
    lc.confirm_destination(new, "done")
    ev = lc.evaluate(cid, deep=True)                                      # the window restarts from the new confirmation
    assert ev["state"] == "GRACE_PERIOD" and not ev["eligible"]


def test_recovery_window_counts_from_the_last_confirmation_not_the_upload_request(settings, env):
    db, drive, lc = env
    cid, pkgs, path = make_clip(settings, db, drive)
    confirm(lc, pkgs, ago=2 * HOUR)
    ev = lc.evaluate(cid, deep=True)
    assert ev["state"] == "GRACE_PERIOD" and "recovery window" in ev["blockers"][0]
    assert abs(ev["ready_at"] - (NOW - 2 * HOUR + 24 * HOUR)) < 1
    assert lc.delete(cid)["deleted"] is False and path.exists()
    lc.set_grace_hours(1)
    assert lc.evaluate(cid, deep=True)["eligible"]


def test_owner_controls_hold_keep_cancel_and_manual_delete(settings, env):
    db, drive, lc = env
    cid, pkgs, path = make_clip(settings, db, drive)
    confirm(lc, pkgs, ago=1 * HOUR)                                       # still inside the window
    lc.keep_local(cid, True)
    assert lc.evaluate(cid, deep=True, manual=True)["state"] == "CLEANUP_HELD" and not lc.delete(cid, manual=True)["deleted"]
    lc.keep_local(cid, False)
    lc.cancel(cid, "wait until Friday")
    assert "wait until Friday" in blockers(lc, cid, manual=True)
    lc.release_hold(cid)
    assert lc.delete(cid)["deleted"] is False                              # automatic: still inside the window
    res = lc.delete(cid, manual=True)                                      # manual waives only the window
    assert res["deleted"] and not path.exists()


def test_manual_delete_still_needs_drive_master_and_managed_path(settings, env):
    db, drive, lc = env
    cid, pkgs, path = make_clip(settings, db, drive, verified=False)
    confirm(lc, pkgs)
    assert not lc.delete(cid, manual=True)["deleted"] and path.exists()


def test_switch_off_by_default_per_clip_and_global(settings):
    db = Database(settings.db_path)
    lc = cleanup.LocalCleanup(settings, db, JobQueue(db), FakeCloud(FakeDrive()), clock=lambda: NOW)
    assert lc.enabled() is False                                           # opt-in
    drive = lc.cloud.provider
    cid, pkgs, path = make_clip(settings, db, drive)
    confirm(lc, pkgs)
    assert lc.delete(cid)["reason"] == "automatic cleanup is switched off" and path.exists()
    assert lc.run()["deleted"] == 0
    lc.set_auto(cid, 1)                                                    # per-clip opt-in works while global is off
    assert lc.run()["deleted"] == 1
    cid2, pkgs2, path2 = make_clip(settings, db, drive)
    confirm(lc, pkgs2)
    lc.set_enabled(True)
    lc.set_auto(cid2, 0)
    assert lc.run()["deleted"] == 0 and path2.exists()


# ---- managed path, ownership, consistency -------------------------------------------------------------

def test_owner_supplied_outside_and_foreign_files_are_never_deleted(settings, env, tmp_path):
    db, drive, lc = env
    cid, pkgs, path = make_clip(settings, db, drive)
    confirm(lc, pkgs)
    owner_file = tmp_path / "owner" / "my_video.mp4"
    owner_file.parent.mkdir()
    owner_file.write_bytes(b"V" * 4096)
    os.utime(owner_file, (NOW - 3 * HOUR, NOW - 3 * HOUR))
    db.update("clips", cid, video_path=str(owner_file))
    assert "outside the engine's managed clips folder" in blockers(lc, cid)
    foreign = settings.clips_dir / "rec99999" / "clip99999_r0" / "clip.mp4"      # inside the folder, not this clip's render
    foreign.parent.mkdir(parents=True)
    foreign.write_bytes(b"V" * 4096)
    os.utime(foreign, (NOW - 3 * HOUR, NOW - 3 * HOUR))
    db.update("clips", cid, video_path=str(foreign))
    assert "not this clip's own render" in blockers(lc, cid)
    assert not lc.delete(cid, manual=True)["deleted"] and owner_file.exists() and foreign.exists()
    if hasattr(os, "symlink"):
        link = path.parent / "link.mp4"
        try:
            os.symlink(owner_file, link)
        except OSError:
            return
        db.update("clips", cid, video_path=str(link))
        assert "symbolic link" in blockers(lc, cid)


def test_unreviewed_unapproved_or_flagged_clips_are_never_cleaned(settings, env):
    db, drive, lc = env
    cid, pkgs, path = make_clip(settings, db, drive, approved=False)
    confirm(lc, pkgs)
    assert "approved" in blockers(lc, cid)
    cid2, pkgs2, _ = make_clip(settings, db, drive)
    confirm(lc, pkgs2)
    db.update("clips", cid2, safety_status="flagged")
    assert "flagged" in blockers(lc, cid2)
    db.update("clips", cid2, safety_status="clear", rights_status="unreviewed")
    assert "rights" in blockers(lc, cid2).lower()


def test_fresh_or_changing_render_is_not_final(settings, env):
    db, drive, lc = env
    cid, pkgs, path = make_clip(settings, db, drive, age=5)
    confirm(lc, pkgs)
    assert "not final" in blockers(lc, cid)


def test_brand_isolation_blocks_wrong_account(settings, env):
    db, drive, lc = env
    cid, pkgs, path = make_clip(settings, db, drive, niche="politics")
    confirm(lc, pkgs)
    acct = db.one("SELECT id FROM publication_packages WHERE id=?", (pkgs["rumble"],))
    other = db.insert("social_accounts", platform="rumble", handle="afterlive_rumble", brand="afterlive",
                      niches_json='["gaming_streams"]', status="manual")
    db.update("publication_packages", pkgs["rumble"], account_id=other)
    assert "never share accounts" in blockers(lc, cid)


def test_missing_recording_is_an_inconsistent_record(settings, env):
    db, drive, lc = env
    cid, pkgs, path = make_clip(settings, db, drive)
    confirm(lc, pkgs)
    db.execute("PRAGMA foreign_keys=OFF")
    conn = db.connect()
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("UPDATE clips SET recording_id=424242 WHERE id=?", (cid,))
    conn.close()
    assert "recording record is missing" in blockers(lc, cid)


# ---- crash safety and concurrency -------------------------------------------------------------------------

def test_crash_between_decision_and_unlink_is_recovered_both_ways(settings, env):
    db, drive, lc = env
    cid, pkgs, path = make_clip(settings, db, drive)
    db.update("clips", cid, cleanup_state="DELETING")                      # claimed, then the process died; file still there
    assert lc.recover() == 1 and db.clip(cid)["cleanup_state"] is None and path.exists()
    db.update("clips", cid, cleanup_state="DELETING")
    path.unlink()                                                          # died right after the unlink
    assert lc.recover() == 1 and db.clip(cid)["local_deleted_at"] and db.clip(cid)["cleanup_state"] == "LOCAL_DELETED"


def test_concurrent_workers_cannot_both_delete_or_bypass_checks(settings, env):
    db, drive, lc = env
    cid, pkgs, path = make_clip(settings, db, drive)
    confirm(lc, pkgs)
    results = []

    def worker():
        results.append(lc.delete(cid)["deleted"])
    ts = [threading.Thread(target=worker) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert results.count(True) == 1 and not path.exists()
    # a job appearing between evaluation and deletion stops it
    cid2, pkgs2, path2 = make_clip(settings, db, drive)
    confirm(lc, pkgs2)
    real = lc.evaluate

    def racing(clip_id, **kw):
        out = real(clip_id, **kw)
        if kw.get("deep"):
            db.insert("jobs", kind="publish_clip", clip_id=clip_id, status="queued", payload_json="{}", priority=0,
                      max_attempts=1, message="")
        return out
    lc.evaluate = racing
    res = lc.delete(cid2)
    assert res["deleted"] is False and path2.exists() and "changed" in res["reason"]


def test_deleting_never_touches_the_database_record_or_other_files(settings, env):
    db, drive, lc = env
    cid, pkgs, path = make_clip(settings, db, drive)
    (path.parent / "transcript.txt").write_text("t")
    (path.parent / "thumb.jpg").write_bytes(b"j")
    confirm(lc, pkgs)
    assert lc.delete(cid)["deleted"]
    assert (path.parent / "transcript.txt").exists() and (path.parent / "thumb.jpg").exists()
    assert db.clip(cid)["title"] is None and db.clip(cid)["status"] == "approved"


# ---- Drive preservation against other retention -------------------------------------------------------------

def test_retention_cannot_delete_the_drive_master(settings):
    db = Database(settings.db_path)
    drive = FakeDrive()
    q = JobQueue(db)

    class Prov(cloudmod.StorageProvider):
        name = "gdrive"
        def connected(self): return True
        def delete(self, rid): drive.files.pop(rid, None)
    svc = cloudmod.CloudService(settings, db, q, tokens=None, provider=Prov())
    cid, pkgs, path = make_clip(settings, db, drive)
    assert svc.delete_clip_objects(cid) == 0                                    # approved clip: protected by default
    db.update("clips", cid, local_deleted_at=NOW)
    settings.keep_drive_master = False
    assert svc.delete_clip_objects(cid) == 0                                    # the only copy: always protected
    assert len(drive.files) == 2
    db.update("clips", cid, local_deleted_at=None, status="rejected")
    settings.keep_drive_master = True
    assert svc.delete_clip_objects(cid) == 2                                    # a rejected clip may still be removed
    assert len(drive.files) == 0


def test_published_clip_retention_keeps_drive_and_marks_it_restorable(settings):
    from clipengine import lifecycle
    db = Database(settings.db_path)
    drive = FakeDrive()
    q = JobQueue(db)
    svc = cloudmod.CloudService(settings, db, q, tokens=None, provider=drive)
    lc = lifecycle.Lifecycle(settings, db, q, svc)
    cid, pkgs, path = make_clip(settings, db, drive)
    db.update("clips", cid, published_at=NOW - 30 * 86400)
    lc.retention(now=NOW)
    row = db.clip(cid)
    assert row["local_deleted_at"] and row["cleanup_state"] == "LOCAL_DELETED" and len(drive.files) == 2 and not path.exists()


def test_raw_recording_gate_accepts_cleaned_clips_with_verified_drive_copy(settings):
    from clipengine import lifecycle
    db = Database(settings.db_path)
    drive = FakeDrive()
    q = JobQueue(db)
    svc = cloudmod.CloudService(settings, db, q, tokens=None, provider=drive)
    lc = lifecycle.Lifecycle(settings, db, q, svc)
    cid, pkgs, path = make_clip(settings, db, drive)
    rid = db.clip(cid)["recording_id"]
    db.update("clips", cid, local_deleted_at=NOW)
    gate = {g["gate"]: g for g in lc.gates(rid)}["integrity"]
    assert gate["ok"]
    db.execute("UPDATE cloud_objects SET state='failed' WHERE owner_id=?", (cid,))
    assert not {g["gate"]: g for g in lc.gates(rid)}["integrity"]["ok"]


def test_overview_reports_recoverable_and_blocked_storage(settings, env):
    db, drive, lc = env
    a, pa, _ = make_clip(settings, db, drive, data=b"A" * 5000)
    confirm(lc, pa)
    b, pb, _ = make_clip(settings, db, drive, data=b"B" * 3000)
    c, pc, _ = make_clip(settings, db, drive, data=b"C" * 2000)
    confirm(lc, pc, ago=1 * HOUR)
    ov = lc.overview()
    assert ov["recoverable_now"] == 5000 and ov["in_grace"] == 2000 and ov["blocked_by_destinations"] == 3000
    assert ov["totals"]["local_bytes"] == 10000 and ov["enabled"]
    lc.run()
    assert lc.overview()["totals"]["removed"] == 1


def test_restore_job_runs_through_the_queue(settings, env):
    db, drive, lc = env
    cid, pkgs, path = make_clip(settings, db, drive)
    confirm(lc, pkgs)
    lc.delete(cid)
    jid = lc.request_restore(cid)
    assert jid and db.clip(cid)["cleanup_state"] == "RESTORE_PENDING"
    assert lc.request_restore(cid) == jid                                   # dedupe: one restore at a time
    with pytest.raises(ValueError):
        lc.request_restore(make_clip(settings, db, drive)[0])               # nothing to restore
