"""Session 16: Reject & delete safety tests (the ten mandatory cases from the owner's brief).
Everything runs in a temporary data folder with a fake cloud; no real files outside tmp_path are touched."""
import time

import pytest
from fastapi.testclient import TestClient

from clipengine.db import Database
from clipengine.lifecycle import Lifecycle
from clipengine.web.app import create_app


class FakeCloud:
    def __init__(self):
        self.on_verified, self.deleted = [], []

    def delete_clip_objects(self, clip_id, force=False):
        self.deleted.append(clip_id)
        return 0

    def clip_verified(self, clip_id):
        return True


@pytest.fixture
def db(settings):
    return Database(settings.db_path)


@pytest.fixture
def life(settings, db):
    return Lifecycle(settings, db, queue=None, cloud=FakeCloud())


def mk(settings, db, *, status="rejected", rid=None, rev=0, n=[0], **extra):
    n[0] += 1
    if rid is None:
        rid = db.insert("recordings", title="R", source_type="upload", status="ready")
    cid = db.insert("clips", recording_id=rid, idx=n[0], start=0, end=20, status=status, title="t", **extra)
    d = settings.clips_dir / f"rec{rid:05d}" / f"clip{cid:05d}_r{rev}"
    d.mkdir(parents=True, exist_ok=True)
    for name in ("clip.mp4", "clip_clean.mp4", "thumb.jpg", "captions.ass"):
        (d / name).write_bytes(b"x" * 1000)
    db.update("clips", cid, video_path=str(d / "clip.mp4"), thumb_path=str(d / "thumb.jpg"))
    return cid, rid, d


# 1 + 2 + 7
def test_rejected_clip_files_removed_record_kept_cloud_untouched(settings, db, life):
    cid, rid, d = mk(settings, db)
    rec_file = settings.recordings_dir / "source.mp4"
    rec_file.parent.mkdir(parents=True, exist_ok=True)
    rec_file.write_bytes(b"source")
    db.update("recordings", rid, file_path=str(rec_file))
    res = life.discard_rejected(cid)
    assert res["deleted"] and res["bytes"] == 4000
    assert not d.exists()
    row = db.clip(cid)
    assert row is not None and row["status"] == "rejected" and row["files_deleted_at"] and row["video_path"] is None
    assert life.cloud.deleted == []                                     # no Google Drive deletion
    assert rec_file.exists()                                            # shared source recording untouched
    ev = db.query("SELECT kind, detail FROM cleanup_events WHERE clip_id=?", (cid,))
    assert any(e["kind"] == "deleted" and "cloud copies untouched" in e["detail"] for e in ev)


def test_all_revisions_of_the_clip_are_removed(settings, db, life):
    cid, rid, d0 = mk(settings, db)
    d1 = d0.parent / f"clip{cid:05d}_r1"
    d1.mkdir()
    (d1 / "clip.mp4").write_bytes(b"y" * 500)
    assert life.discard_rejected(cid)["bytes"] == 4500
    assert not d0.exists() and not d1.exists()


# 3
def test_keep_marked_clip_is_skipped(settings, db, life):
    cid, _, d = mk(settings, db, retain=1)
    res = life.discard_rejected(cid)
    assert not res["deleted"] and res["reason"] == "marked keep" and d.exists()


# 4
@pytest.mark.parametrize("status", ["approved", "ready", "published", "failed"])
def test_non_rejected_clips_are_protected(settings, db, life, status):
    cid, _, d = mk(settings, db, status=status)
    assert not life.discard_rejected(cid)["deleted"] and (d / "clip.mp4").exists()


# 5
def test_uploading_or_published_clips_are_protected(settings, db, life):
    a, _, da = mk(settings, db)
    db.insert("jobs", kind="deliver_clip", clip_id=a, status="running")
    b, _, db_ = mk(settings, db)
    db.insert("publish_attempts", clip_id=b, platform="youtube", status="uploading")
    c, _, dc = mk(settings, db)
    db.insert("publication_packages", clip_id=c, platform="facebook", upload_state="uploading")
    d, _, dd = mk(settings, db, published_at=time.time())
    for cid, folder in ((a, da), (b, db_), (c, dc), (d, dd)):
        res = life.discard_rejected(cid)
        assert not res["deleted"], (cid, res)
        assert (folder / "clip.mp4").exists()
        assert db.one("SELECT COUNT(*) n FROM cleanup_events WHERE clip_id=? AND kind='held'", (cid,))["n"] == 1


# 6
def test_sibling_clip_from_same_recording_is_protected(settings, db, life):
    a, rid, da = mk(settings, db)
    b, _, dbb = mk(settings, db, rid=rid, status="ready")
    life.discard_rejected(a)
    assert not da.exists() and (dbb / "clip.mp4").exists() and (dbb / "clip_clean.mp4").exists()


def test_folder_referenced_by_another_clip_is_never_deleted(settings, db, life):
    a, rid, da = mk(settings, db)
    b = db.insert("clips", recording_id=rid, idx=99, start=0, end=5, status="approved", video_path=str(da / "clip.mp4"))
    life.discard_rejected(a)
    assert (da / "clip.mp4").exists()


def test_nothing_outside_the_clips_folder_is_touched(settings, db, life, tmp_path):
    outside = tmp_path / "elsewhere" / "clip.mp4"
    outside.parent.mkdir()
    outside.write_bytes(b"z")
    rid = db.insert("recordings", title="R", source_type="upload", status="ready")
    cid = db.insert("clips", recording_id=rid, idx=1, start=0, end=5, status="rejected", video_path=str(outside))
    life.discard_rejected(cid)
    assert outside.exists()


# 8
def test_missing_files_do_not_crash(settings, db, life):
    rid = db.insert("recordings", title="R", source_type="upload", status="ready")
    cid = db.insert("clips", recording_id=rid, idx=1, start=0, end=5, status="rejected",
                    video_path=str(settings.clips_dir / "gone" / "clip.mp4"))
    res = life.discard_rejected(cid)
    assert res["deleted"] and res["bytes"] == 0
    assert life.discard_rejected(999999)["reason"] == "not a rejected clip"


# 9
def test_duplicate_rejection_deletes_nothing_more(settings, db, life):
    cid, rid, d = mk(settings, db)
    assert life.discard_rejected(cid)["deleted"]
    # a new folder that happens to appear later (e.g. a re-render) is not removed by a repeated reject
    d.mkdir(parents=True)
    (d / "clip.mp4").write_bytes(b"new")
    res = life.discard_rejected(cid)
    assert not res["deleted"] and res["reason"] == "already deleted" and (d / "clip.mp4").exists()


# 10: both dashboard interfaces
def test_mobile_and_desktop_reject_delete(settings, db):
    m, _, dm = mk(settings, db, status="ready")
    k, _, dk = mk(settings, db, status="ready")
    p, _, dp = mk(settings, db, status="approved")
    co = db.insert("cloud_objects", provider="gdrive", owner_kind="clip", owner_id=m, kind="video",
                   remote_id="DRIVE1", state="verified")
    app = create_app(settings, start_worker=False)
    from pathlib import Path
    tdir = Path(__file__).resolve().parent.parent / "clipengine" / "web" / "templates"
    assert "Reject &amp; delete" in (tdir / "mobile.html").read_text(encoding="utf-8")
    assert "Reject &amp; delete" in (tdir / "_clip_card.html").read_text(encoding="utf-8")
    with TestClient(app) as cl:
        r = cl.post(f"/m/{m}/reject", data={"skip": ""}, follow_redirects=False)
        assert r.status_code == 303 and "deleted" in r.headers["location"]
        assert db.clip(m)["status"] == "rejected" and not dm.exists()
        r = cl.post(f"/clips/{k}/reject", data={}, follow_redirects=False)
        assert r.status_code == 303 and "deleted" in r.headers["location"]
        assert db.clip(k)["status"] == "rejected" and not dk.exists()
        # approved clip rejected from the desktop: status changes but files are kept (may be uploading)
        r = cl.post(f"/clips/{p}/reject", data={}, follow_redirects=False)
        assert db.clip(p)["status"] == "approved" and "files+were+kept" in r.headers["location"] and (dp / "clip.mp4").exists()
        # mobile refuses to reject something not awaiting review
        r = cl.post(f"/m/{p}/reject", data={"skip": ""}, follow_redirects=False)
        assert "Not+rejected" in r.headers["location"]
    row = db.one("SELECT state, remote_id FROM cloud_objects WHERE id=?", (co,))
    assert row["state"] == "verified" and row["remote_id"] == "DRIVE1"     # Drive master untouched
def test_inline_reject_pending_rights_and_flagged_without_media(settings, db):
    first, _, folder = mk(settings, db, status="ready", safety_status="flagged", rights_status="unreviewed")
    second, _, _ = mk(settings, db, status="ready")
    import shutil
    shutil.rmtree(folder)
    with TestClient(create_app(settings, start_worker=False)) as client:
        headers = {"HX-Request": "true"}
        response = client.post(f"/m/{first}/reject", headers=headers, follow_redirects=False)
        assert response.status_code == 200
        assert "Rejected. No local video files remained to remove." in response.text
        assert "All caught up" in response.text
        assert db.clip(first)["status"] == "rejected"
        response = client.post(f"/clips/{second}/reject", headers=headers, follow_redirects=False)
        assert response.status_code == 200 and "4,000 bytes freed" in response.text
        repeat = client.post(f"/clips/{second}/reject", headers=headers)
        assert "No local video files remained" in repeat.text

def test_scheduled_retention_preserves_drive_and_keep_local(settings, db, life):
    cid, _, folder = mk(settings, db, reviewed_at=1)
    kept, _, protected = mk(settings, db, reviewed_at=1, keep_local=1)
    assert life.retention()["rejected"] == 1
    assert not folder.exists() and protected.exists()
    assert life.cloud.deleted == []
def test_housekeeping_preserves_old_live_capture(settings, db):
    import os
    from clipengine.maintenance import run_once
    live = settings.work_dir / "live"
    live.mkdir(parents=True, exist_ok=True)
    capture = live / "recording.ts"
    capture.write_bytes(b"preserved capture")
    partial = live / "capture.part"
    partial.write_bytes(b"partial capture")
    os.utime(partial, (1, 1))
    os.utime(live, (1, 1))
    run_once(settings, db)
    assert capture.read_bytes() == b"preserved capture"
    assert partial.read_bytes() == b"partial capture"

def test_cross_recording_subtitle_reference_holds_revision(settings, db, life):
    cid, _, folder = mk(settings, db)
    other, _, _ = mk(settings, db, status="ready")
    db.update("clips", other, ass_path=str(folder / "captions.ass"))
    result = life.discard_rejected(cid)
    assert not result["deleted"] and "shared" in result["reason"]
    assert (folder / "clip.mp4").exists() and db.clip(cid)["video_path"]


def test_simultaneous_rejections_count_bytes_once(settings, db, life):
    from concurrent.futures import ThreadPoolExecutor
    cid, _, folder = mk(settings, db)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(life.discard_rejected, [cid, cid]))
    assert sum(r.get("bytes", 0) for r in results) == 4000
    assert sum(r["deleted"] for r in results) == 1 and not folder.exists()
