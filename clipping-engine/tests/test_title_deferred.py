"""Session 14: a title is drawn ON TOP of a title-free master, so it can be removed or changed without re-rendering the clip."""
import dataclasses
import hashlib
import subprocess

import pytest

from clipengine import captions, render, titles
from conftest import needs_ffmpeg

WORDS = [{"word": w, "start": i * .4, "end": i * .4 + .35} for i, w in enumerate("the committee will publish its findings".split())]
TITLE = "The *committee* **findings**"


def _sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _frame(video, out):
    np = pytest.importorskip("numpy")
    raw = subprocess.run(["ffmpeg", "-loglevel", "error", "-ss", "1.0", "-i", str(video), "-frames:v", "1", "-f", "rawvideo",
                          "-pix_fmt", "rgb24", "-"], check=True, capture_output=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(1920, 1080, 3).astype(int)


def _render(settings, sample_video, d, title):
    return render.render_clip(settings, sample_video, d, 0.0, 2.0, WORDS, 1280, 720, None,
                              style=captions.CaptionStyle(), title=title, title_style=titles.TitleStyle(enabled=True))


@needs_ffmpeg
def test_titled_render_keeps_an_untouched_title_free_master(settings, sample_video, tmp_path):
    d = tmp_path / "a"
    res = _render(settings, sample_video, d, TITLE)
    clean = d / render.CLEAN_NAME
    assert clean.is_file() and res["video"] == d / "clip.mp4" and res["title"] is True
    assert _sha(clean) != _sha(d / "clip.mp4")
    a, b = _frame(clean, tmp_path / "c.png"), _frame(d / "clip.mp4", tmp_path / "t.png")
    top = slice(0, 560)                                   # the title band above the picture
    assert abs(a[top] - b[top]).mean() > 1.0              # the title is there...
    mid = slice(700, 1200)
    assert abs(a[mid] - b[mid]).mean() < 4.0              # ...and the central picture is unchanged (re-encode noise only)


@needs_ffmpeg
def test_remove_title_is_instant_and_restores_the_master_exactly(settings, sample_video, tmp_path):
    d = tmp_path / "a"
    _render(settings, sample_video, d, TITLE)
    before = _sha(d / render.CLEAN_NAME)
    render.remove_title(d)
    assert _sha(d / "clip.mp4") == before == _sha(d / render.CLEAN_NAME)


@needs_ffmpeg
def test_change_title_never_modifies_the_master(settings, sample_video, tmp_path):
    d = tmp_path / "a"
    _render(settings, sample_video, d, TITLE)
    before = _sha(d / render.CLEAN_NAME)
    render.apply_title(settings, d, "A *different* **headline**", titles.TitleStyle(enabled=True), 1280, 720)
    assert _sha(d / render.CLEAN_NAME) == before
    assert _sha(d / "clip.mp4") != before
    render.apply_title(settings, d, "", titles.TitleStyle(enabled=True), 1280, 720)      # empty = remove
    assert _sha(d / "clip.mp4") == before


@needs_ffmpeg
def test_untitled_render_still_has_a_master_so_a_title_can_be_added_later(settings, sample_video, tmp_path):
    d = tmp_path / "a"
    _render(settings, sample_video, d, None)
    assert _sha(d / "clip.mp4") == _sha(d / render.CLEAN_NAME)
    render.apply_title(settings, d, TITLE, titles.TitleStyle(enabled=True), 1280, 720)
    assert _sha(d / "clip.mp4") != _sha(d / render.CLEAN_NAME)


@needs_ffmpeg
def test_switch_off_keeps_the_old_single_pass_behaviour(settings, sample_video, tmp_path):
    s = dataclasses.replace(settings, keep_title_free_master=False)
    d = tmp_path / "a"
    _render(s, sample_video, d, TITLE)
    assert (d / "clip.mp4").is_file() and not render.has_clean_master(d)
    with pytest.raises(FileNotFoundError):
        render.remove_title(d)
