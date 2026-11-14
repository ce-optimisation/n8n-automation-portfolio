import json
import shutil

import pytest

from clipengine import ingest, media, render
from conftest import needs_ffmpeg


def words(text, start=0.0, step=0.5):
    return [{"start": start + i * step, "end": start + i * step + 0.4, "word": " " + w, "prob": 0.9}
            for i, w in enumerate(text.split())]


def test_phrases_wrap_and_time_relative_to_clip():
    ws = words("This is a fairly long caption line that should wrap onto two lines nicely. Next sentence here.", 10)
    ph = render.build_phrases(ws, 10.0, 30.0)
    assert ph[0]["start"] == 0.0
    for p in ph:
        assert 1 <= len(p["lines"]) <= 2 and all(len(l) <= 26 or " " not in l for l in p["lines"])
        assert p["end"] > p["start"]
    assert ph[0]["lines"][-1].endswith("nicely.") or any(p["lines"][-1].endswith("nicely.") for p in ph)


def test_srt_and_ass_output(tmp_path, settings):
    ph = render.build_phrases(words("Hello world. {Braces} are escaped."), 0, 5)
    render.write_srt(ph, tmp_path / "c.srt")
    assert (tmp_path / "c.srt").read_text(encoding="utf-8").startswith("1\n00:00:00,000 --> ")
    render.write_ass(ph, tmp_path / "c.ass", settings, 1920, 1080, "Excerpt · Test")
    ass = (tmp_path / "c.ass").read_text(encoding="utf-8")
    assert "PlayResY: 1920" in ass and "{Braces}" not in ass and "Excerpt · Test" in ass


def test_layout_places_captions_below_landscape_video(settings):
    assert render.layout(settings, 1920, 1080)["align"] == 8
    assert render.layout(settings, 1080, 1920)["align"] == 2


def test_url_policy(settings, monkeypatch):
    monkeypatch.setattr(ingest, "resolve_host", lambda h: ["93.184.216.34"])
    d = ingest.classify_url("https://rumble.com/v123-video.html", settings)
    assert d.method == "link_only" and d.code == "restricted_platform" and "rumble" in d.message.lower()
    assert ingest.classify_url("https://www.youtube.com/watch?v=x", settings).method == "link_only"
    assert ingest.classify_url("https://example.org/a.mp4", settings).method == "direct"
    d = ingest.classify_url("https://example.org/watch/123", settings)
    assert d.method == "link_only" and "ALLOWED_URL_DOMAINS" in d.message
    settings.allowed_url_domains = ["example.org"]
    assert ingest.classify_url("https://media.example.org/page", settings).method == "ytdlp"
    assert ingest.classify_url("ftp://example.org/a.mp4", settings).code == "unsupported_scheme"
    assert ingest.classify_url("https://notexample.org/page", settings).method == "link_only"


def test_safe_filename():
    name = ingest.safe_filename('../../evil "name"?.mp4')
    assert name.endswith(".mp4") and "/" not in name and "\\" not in name and not name.startswith(".")
    assert '"' not in name and "?" not in name
    assert ingest.safe_filename("") == "video"


@needs_ffmpeg
def test_register_file_and_duplicate_detection(settings, sample_video):
    from clipengine.db import Database
    db = Database(settings.db_path)
    a = settings.work_dir / "a.mp4"
    shutil.copy(sample_video, a)
    rid, dup = ingest.register_file(settings, db, a, title="Sample")
    assert not dup
    rec = db.recording(rid)
    assert rec["duration"] > 60 and rec["width"] == 1280 and rec["sha256"]
    prov = json.loads((settings.recordings_dir / next(p.name for p in settings.recordings_dir.iterdir())
                       / "provenance.json").read_text(encoding="utf-8"))
    assert prov["sha256"] == rec["sha256"]
    b = settings.work_dir / "b.mp4"
    shutil.copy(sample_video, b)
    rid2, dup2 = ingest.register_file(settings, db, b, title="Same file again")
    assert dup2 and rid2 == rid
    assert db.one("SELECT COUNT(*) n FROM recordings")["n"] == 1


@needs_ffmpeg
def test_rejects_non_video(settings, tmp_path):
    from clipengine.db import Database
    f = settings.work_dir / "notes.txt"
    f.write_text("hi")
    with pytest.raises(media.PermanentError):
        ingest.register_file(settings, Database(settings.db_path), f)


@needs_ffmpeg
def test_render_vertical_clip(settings, sample_video, tmp_path):
    info = media.probe(sample_video)
    res = render.render_clip(settings, sample_video, tmp_path / "out", 2.0, 9.0,
                             words("Good evening tonight the council will discuss the bridge.", 2.0),
                             info["width"], info["height"], "Excerpt · test")
    p = res["probe"]
    assert (p["width"], p["height"], p["video_codec"], p["audio_codec"]) == (1080, 1920, "h264", "aac")
    assert 6.5 < p["duration"] < 7.5
    assert res["thumb"].stat().st_size > 1000 and res["srt"].read_text(encoding="utf-8").strip()
