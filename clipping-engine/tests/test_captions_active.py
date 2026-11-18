"""Single-line, active-word captions (proposed 18 Nov 2026; opt-in per brand, off by default)."""
import json
import os
import re
from dataclasses import replace
from pathlib import Path

import pytest

from clipengine import captions, segment

A = captions.PRESETS["anton"]
B = replace(A, mode="active_word")


def W(text, start=0.0, step=0.3, gap=0.0):
    out, t = [], start
    for tok in text.split():
        out.append({"word": " " + tok, "start": t, "end": t + step, "prob": 0.9})
        t += step + gap
    return out


class KV:
    def __init__(self):
        self.d = {}

    def get_kv(self, k):
        return self.d.get(k)

    def set_kv(self, k, v):
        self.d[k] = v


SPEECH = ("Our other story from the night, I mean, you already know what it is. In the past four or five days, we've had "
          "a series of massive outages of digital infrastructure in America. And the major ones are a blackout of "
          "Microsoft office services last week. The day after that, a nationwide ground stop, 10, 12,000 flights "
          "grounded and canceled. Simultaneously, troubleshooting internationalisation counterrevolutionaries?! Yes.")


def test_groups_single_line_fit_and_verbatim():
    words = W(SPEECH, step=0.12)          # fast speech: ~8 words/second
    groups = captions.build_word_groups(words, 0.0, 999, B)
    usable = captions._usable(B, 1080)
    assert " ".join(x["text"] for g in groups for x in g["words"]) == " ".join(SPEECH.split())   # wording/punctuation/case
    for g in groups:
        text = " ".join(x["text"] for x in g["words"])
        assert captions.text_width(text, B, g["size"]) <= usable + 0.5, text
        assert len(g["words"]) <= captions.MAX_GROUP_WORDS
        assert g["timed"]
        if len(g["words"]) >= 3:
            assert g["words"][-1]["text"].lower() not in captions.WEAK_END or g["words"][-1]["text"][-1] in ".,?!"
    ass = captions.build_ass([], B, groups=groups, fg_h=607.5)
    assert "\\N" not in ass                 # never a second line


def test_sentence_end_and_pause_split_groups():
    words = W("It failed. Then", step=0.3) + W("again later", start=3.0)
    groups = captions.build_word_groups(words, 0, 99, B)
    assert [" ".join(x["text"] for x in g["words"]) for g in groups] == ["It failed.", "Then", "again later"]


def test_overlong_word_shrinks_only_as_far_as_needed():
    groups = captions.build_word_groups(W("Antidisestablishmentarianismstyle-overwhelmingly words"), 0, 99, B)
    g = groups[0]
    assert len(g["words"]) == 1 and g["size"] < B.size
    assert g["size"] >= int(B.size * captions.MIN_FONT_FRACTION)
    assert groups[1]["size"] == B.size


def test_missing_or_estimated_timings_fall_back_to_static_line():
    est = segment._flatten_words([{"start": 0, "end": 2, "text": "no word timings here", "words": []}])
    assert all(w.get("est") for w in est)
    groups = captions.build_word_groups(est, 0, 99, B)
    assert groups and not any(g["timed"] for g in groups)
    ass = captions.build_ass([], B, groups=groups, fg_h=607.5)
    assert "Dialogue: 0," not in ass and ass.count("Dialogue: 1,") == len(groups)   # no highlight layer
    bad = W("one two three")
    bad[1]["end"] = bad[1]["start"]          # zero-length word
    assert not captions.build_word_groups(bad, 0, 99, B)[0]["timed"]


def test_highlight_events_are_contiguous_and_the_line_never_moves():
    groups = captions.build_word_groups(W("a blackout of Microsoft office", step=0.25, gap=0.05), 0, 99, B)
    ass = captions.build_ass([], B, groups=groups, fg_h=607.5)
    ring = [l for l in ass.splitlines() if l.startswith("Dialogue: 0,")]
    text = [l for l in ass.splitlines() if l.startswith("Dialogue: 1,")]
    assert len(ring) == len(text) == 5
    assert len({re.search(r"\\pos\(\d+,\d+\)", l).group(0) for l in ring + text}) == 1
    assert len({l.split(",,", 1)[1].split("}", 1)[1] for l in text}) == 1           # identical visible line
    ends = [l.split(",")[2] for l in ring]
    starts = [l.split(",")[1] for l in ring]
    assert starts[1:] == ends[:-1]                                                   # no gaps, no overlaps
    for i, l in enumerate(ring):
        active = re.findall(r"\\3c&H0004FBFD&\\bord8\.0\}([^{ ]+)", l)
        assert active == [["a", "blackout", "of", "Microsoft", "office"][i]]


def test_vertical_position_clears_picture_and_stays_in_safe_area():
    H = 1920
    for fg_h in (607.5, 810, 1080):
        y = captions.active_word_y(B, 1080, H, fg_h)
        half = B.size / 2 + B.outline + B.active_ring
        assert y - half >= (H + fg_h) / 2 + 2 + 20                 # below the picture and its bottom frame line
        assert y + half <= H * captions.SAFE_BOTTOM + 1
    assert captions.active_word_y(B, 1080, H, 607.5) == int(H * B.position)   # 16:9: unchanged approved position


def test_on_only_for_approved_brand_and_switchable_per_brand():
    db = KV()
    assert captions.style_for(db, "america_first_monitor").mode == "active_word"   # owner-approved 18 Nov 2026
    assert captions.style_for(db, "another_brand").mode == "phrase"
    assert captions.style_for(db, None).mode == "phrase"
    assert captions.style_for(None, "another_brand").mode == "phrase"
    captions.set_brand_active_word(db, "america_first_monitor", False)
    assert captions.style_for(db, "america_first_monitor").mode == "phrase"
    captions.set_brand_active_word(db, "another_brand", True)
    assert captions.style_for(db, "another_brand").mode == "active_word"
    db.set_kv("caption_style", json.dumps({"mode": "bogus", "active_colour": "zz"}))
    st = captions.load_style(db)
    assert st.mode == "phrase" and st.active_colour == "FDFB04"


def test_phrase_mode_output_unchanged_by_new_parameters():
    phrases = [{"start": 0, "end": 2, "lines": ["The council will vote", "in March."]}]
    groups = captions.build_word_groups(W("The council will vote in March."), 0, 99, B)
    assert captions.build_ass(phrases, A) == captions.build_ass(phrases, A, groups=groups, fg_h=607.5)


def test_fontsdir_falls_back_to_local_copy_for_long_paths(tmp_path):
    deep = tmp_path / ("d" * 70) / ("e" * 70)
    deep.mkdir(parents=True)
    assert captions.fontsdir_for(deep) == "fonts"
    assert (deep / "fonts" / captions.ANTON_FILE.name).stat().st_size == captions.ANTON_FILE.stat().st_size
