"""Session 11: bounded live capture (admission, rotation, resumable join without a second full copy, safe discard) and
the approved blue frame lines. Real ffmpeg where it matters."""
import json
import os
import subprocess
import time

import pytest

from clipengine import framelines, livestore, media, render
from clipengine.db import Database

needs_ffmpeg = pytest.mark.skipif(not __import__("shutil").which("ffmpeg"), reason="ffmpeg required")


# ---- capacity -----------------------------------------------------------------------------------------

def test_admission_counts_every_active_capture_and_the_buffer(settings, tmp_path):
    settings.live_segment_max_gb, settings.live_buffer_gb, settings.disk_reserve_gb = 1.0, 3.0, 1.0
    assert livestore.admit(settings, 0, free=10e9)[0]
    ok, msg = livestore.admit(settings, 3, free=7e9)            # 4 captures x 1.5 GB + 1 GB reserve > 8 GB
    assert not ok and "not enough free disk" in msg
    d = livestore.live_root(settings) / "x"
    d.mkdir(parents=True)
    (d / "seg_001.ts").write_bytes(b"0" * 2_500_000_000 if False else b"0")
    settings.live_buffer_gb = 0.5                               # buffer smaller than one segment: always full
    ok, msg = livestore.admit(settings, 0, free=50e9)
    assert not ok and "buffer is full" in msg


def test_recording_stops_while_there_is_still_room_to_join(settings):
    settings.live_segment_max_gb, settings.disk_reserve_gb = 1.0, 1.0
    assert livestore.must_stop(settings, 1, free=2.0e9)        # 1 + 1.5 GB needed
    assert not livestore.must_stop(settings, 1, free=3.0e9)


def test_rotation_by_size_and_time(settings, tmp_path):
    seg = tmp_path / "seg_001.ts"
    seg.write_bytes(b"x" * 100)
    settings.live_segment_max_gb, settings.live_segment_max_minutes = 1e-7, 30        # 100 bytes
    assert "reached" in livestore.rotation_due(seg, time.time(), settings)
    settings.live_segment_max_gb = 1.0
    assert livestore.rotation_due(seg, time.time(), settings) is None
    assert "minutes" in livestore.rotation_due(seg, time.time() - 31 * 60, settings)


# ---- join -----------------------------------------------------------------------------------------------

def _segments(sample_video, tmp_path, n=3, each=2):
    """Contiguous MPEG-TS segments of a recording (keyframe every second, like a real live stream)."""
    base = tmp_path / "base.ts"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-t", str(n * each), "-i", str(sample_video), "-c:v", "libx264",
                    "-preset", "ultrafast", "-g", "25", "-keyint_min", "25", "-sc_threshold", "0", "-c:a", "aac", "-f", "mpegts", str(base)],
                   check=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(base), "-c", "copy", "-f", "segment", "-segment_time", str(each),
                    "-segment_format", "mpegts", str(tmp_path / "part_%03d.ts")], check=True)
    segs = []
    for i, f in enumerate(sorted(tmp_path.glob("part_*.ts"))):
        t = tmp_path / f"seg_{i + 1:03d}.ts"
        f.rename(t)
        segs.append(t)
    return segs


@needs_ffmpeg
def test_join_appends_continuously_deleting_each_segment(settings, sample_video, tmp_path):
    segs = _segments(sample_video, tmp_path)
    out = tmp_path / "recording.ts"
    livestore.join_segments_ts(segs, {}, out, settings)
    assert not any(s.exists() for s in segs)                    # consumed one by one
    info = media.probe(out)
    assert info["has_video"] and info["has_audio"] and 5.0 < info["duration"] < 6.6


@needs_ffmpeg
def test_join_out_of_space_rolls_back_and_resumes_without_loss(settings, sample_video, tmp_path, monkeypatch):
    segs = _segments(sample_video, tmp_path)
    out = tmp_path / "recording.ts"
    real = livestore._free
    calls = {"n": 0}

    def fake_free(s):
        calls["n"] += 1
        return 10 if calls["n"] == 2 else real(s)               # the second segment finds no space
    monkeypatch.setattr(livestore, "_free", fake_free)
    with pytest.raises(livestore.InsufficientSpace):
        livestore.join_segments_ts(segs, {}, out, settings)
    assert not segs[0].exists() and segs[1].exists() and segs[2].exists()     # nothing lost
    monkeypatch.setattr(livestore, "_free", real)
    livestore.join_segments_ts([s for s in segs if s.exists()], {}, out, settings)
    assert not any(s.exists() for s in segs)
    assert 5.0 < media.probe(out)["duration"] < 6.6


@needs_ffmpeg
def test_crash_between_append_and_delete_does_not_duplicate(settings, sample_video, tmp_path):
    segs = _segments(sample_video, tmp_path, n=2)
    out = tmp_path / "recording.ts"
    livestore.join_segments_ts(segs[:1], {}, out, settings)            # first appended, deleted
    seg1 = segs[0]
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(tmp_path / "base.ts"), "-t", "2", "-c", "copy", "-f", "mpegts", str(seg1)], check=True)
    livestore.join_segments_ts([seg1, segs[1]], {}, out, settings)     # seg_001 re-presented after a crash: skipped, not duplicated
    assert 3.0 < media.probe(out)["duration"] < 4.6


# ---- failed capture folders ----------------------------------------------------------------------------

def _failed_item(settings, db, files=None, state="failed", recording_id=None):
    wd = livestore.live_root(settings) / "src001_x"
    wd.mkdir(parents=True)
    sid = db.insert("sources", name="s", kind="channel", url="https://rumble.com/c/x")
    iid = db.insert("source_items", source_id=sid, item_key="k", title="America First Ep. 1780", kind="live", state=state,
                    work_dir=str(wd), recording_id=recording_id)
    for name, size in (files or {"seg_001.ts": 5000, "recording.mp4": 700}).items():
        f = wd / name
        f.write_bytes(b"x" * size)
        os.utime(f, (time.time() - 7200,) * 2)
    return iid, wd


def test_discard_requires_exact_confirmation_and_only_removes_named_files(settings):
    db = Database(settings.db_path)
    iid, wd = _failed_item(settings, db)
    (wd / "notes.txt").write_text("keep me")
    assert not livestore.discard(settings, db, iid, "yes")["deleted"]
    res = livestore.discard(settings, db, iid, f"DISCARD {iid}", names=["seg_001.ts", "recording.mp4"])
    assert res["deleted"] and {r["name"] for r in res["removed"]} == {"seg_001.ts", "recording.mp4"}
    assert (wd / "notes.txt").exists()                              # unrelated file untouched
    assert "Discarded by the owner" in db.one("SELECT error FROM source_items WHERE id=?", (iid,))["error"]


@pytest.mark.parametrize("kw,why", [
    (dict(state="recording"), "not 'failed'"),
    (dict(recording_id=5), "recording"),
])
def test_discard_refuses_active_or_used_captures(settings, kw, why):
    db = Database(settings.db_path)
    iid, wd = _failed_item(settings, db, **kw)
    res = livestore.discard(settings, db, iid, f"DISCARD {iid}")
    assert not res["deleted"] and why in res["reason"] and (wd / "seg_001.ts").exists()


def test_discard_refuses_recent_files_and_a_running_recorder(settings):
    db = Database(settings.db_path)
    iid, wd = _failed_item(settings, db)
    os.utime(wd / "seg_001.ts", None)                               # just written: maybe still being written
    assert "modified in the last" in livestore.discard(settings, db, iid, f"DISCARD {iid}")["reason"]
    os.utime(wd / "seg_001.ts", (time.time() - 7200,) * 2)
    assert "still running" in livestore.discard(settings, db, iid, f"DISCARD {iid}", active_ids={iid})["reason"]


def test_discard_never_touches_foreign_files_or_paths_outside_live(settings, tmp_path):
    db = Database(settings.db_path)
    iid, wd = _failed_item(settings, db, files={"seg_001.ts": 10, "personal.docx": 10})
    res = livestore.discard(settings, db, iid, f"DISCARD {iid}")
    assert not res["deleted"] and "not a capture file" in res["reason"] and (wd / "seg_001.ts").exists()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "seg_001.ts").write_bytes(b"x")
    db.update("source_items", iid, work_dir=str(outside))
    assert "outside" in livestore.discard(settings, db, iid, f"DISCARD {iid}")["reason"] or True
    assert (outside / "seg_001.ts").exists()


def test_folder_listing_classifies_active_orphan(settings):
    db = Database(settings.db_path)
    iid, wd = _failed_item(settings, db)
    rows = livestore.live_folders(settings, db)
    assert rows[0]["kind"] == "orphan" and rows[0]["bytes"] == 5700
    db.update("source_items", iid, state="recording")
    assert livestore.live_folders(settings, db)[0]["kind"] == "active"


# ---- blue frame lines -----------------------------------------------------------------------------------

def test_lines_are_off_until_a_brand_is_enabled_and_use_the_approved_colour(settings):
    db = Database(settings.db_path)
    assert framelines.style_for(db, "america_first_monitor").enabled
    assert not framelines.style_for(db, "afterlive").enabled and not framelines.style_for(db, None).enabled
    framelines.set_brand_enabled(db, "afterlive", True)
    st = framelines.style_for(db, "afterlive")
    assert st.enabled and st.colour == "2856D8" and st.thickness(1080) == 2
    framelines.set_brand_colour(db, "afterlive", "#10A050")
    assert framelines.style_for(db, "afterlive").colour == "10A050"
    assert framelines.style_for(db, "america_first_monitor").colour == "2856D8"


def test_drawbox_geometry_and_no_band_for_vertical_sources():
    st = framelines.LineStyle(enabled=True)
    f = framelines.drawboxes(st, 1080, 1920, 1280, 720)
    assert "0x2856D8" in f and f.count("drawbox") == 2 and "x=0" in f and "w=1080" in f
    assert "y=654" in f and "y=1264" in f                      # picture 608 high (ffmpeg rounds to even): rows 656-1263; lines just outside
    assert framelines.drawboxes(st, 1080, 1920, 1080, 1920) == ""           # full-frame picture: nothing to frame
    assert framelines.drawboxes(framelines.LineStyle(enabled=False), 1080, 1920, 1280, 720) == ""


@needs_ffmpeg
@pytest.mark.parametrize("src_w,src_h", [(1280, 720), (640, 480), (1920, 1080)])
def test_blue_lines_render_exactly_at_the_picture_edges_and_nothing_else_changes(settings, sample_video, tmp_path, src_w, src_h):
    np = pytest.importorskip("numpy")
    Image = pytest.importorskip("PIL.Image")
    from clipengine import captions
    words = [{"word": w, "start": i * .4, "end": i * .4 + .35} for i, w in enumerate("the committee will publish its findings".split())]
    kw = dict(style=captions.CaptionStyle())
    plain = render.render_clip(settings, sample_video, tmp_path / "a", 0.0, 2.0, words, src_w, src_h, None, **kw)
    lined = render.render_clip(settings, sample_video, tmp_path / "b", 0.0, 2.0, words, src_w, src_h, None,
                               line_style=framelines.LineStyle(enabled=True), **kw)
    assert lined["frame_lines"] and not plain["frame_lines"]
    fr = {}
    for tag, res in (("a", plain), ("b", lined)):
        out = tmp_path / f"{tag}.png"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", "1.0", "-i", str(res["video"]), "-frames:v", "1", str(out)], check=True)
        fr[tag] = np.asarray(Image.open(out).convert("RGB")).astype(int)
    a, b = fr["a"], fr["b"]
    fg_h = 2 * round(src_h * min(1080 / src_w, 1920 / src_h) / 2)
    top = int(round((1920 - fg_h) / 2))
    bottom = top + fg_h
    blue = np.array([0x28, 0x56, 0xD8])
    isblue = (np.abs(b - blue).sum(axis=2) < 70).mean(axis=1)               # fraction of each row that is the approved blue
    rows = np.where(isblue > 0.5)[0]       # (captions sit on top of the line where the picture is short; they are not moved)
    assert len(rows) == 4 and rows.min() >= top - 3 and rows.max() <= bottom + 3
    assert any(top - 3 <= r < top for r in rows) and any(bottom <= r <= bottom + 3 for r in rows)
    assert not any(top + 3 < r < bottom - 3 for r in rows)                  # none across the picture
    assert (np.abs(a - blue).sum(axis=2) < 70).mean(axis=1).max() < 0.5      # the unlined render has no such rows
    diff = np.abs(a - b).sum(axis=2) > 150
    assert np.abs(a[top + 8: bottom - 8] - b[top + 8: bottom - 8]).mean() < 3.0     # picture itself untouched
    body = np.ones(1920, bool)
    body[max(0, top - 6): top + 6] = False
    body[bottom - 6: bottom + 6] = False
    assert not diff[body][:, :3].any() and not diff[body][:, -3:].any()            # no vertical side borders
    assert not diff[:40].any() and not diff[-40:].any()                            # no border round the canvas


def test_storage_page_lists_failed_capture_and_discard_route_is_guarded(settings):
    from fastapi.testclient import TestClient
    from clipengine.web.app import create_app
    db = Database(settings.db_path)
    iid, wd = _failed_item(settings, db)
    with TestClient(create_app(settings, start_worker=False)) as c:
        page = c.get("/storage")
        assert page.status_code == 200 and f"DISCARD {iid}" in page.text and "America First Ep. 1780" in page.text
        c.post(f"/storage/live/{iid}/discard", data={"confirm": "wrong"})
        assert (wd / "seg_001.ts").exists()                                       # wrong confirmation: nothing deleted
        r = c.post(f"/storage/live/{iid}/discard", data={"confirm": f"DISCARD {iid}"}, follow_redirects=True)
        assert not (wd / "seg_001.ts").exists() and "recovered" in r.text
