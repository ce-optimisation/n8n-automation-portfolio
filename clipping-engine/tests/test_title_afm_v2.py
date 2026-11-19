"""Session 13: the owner-approved America First Monitor title style (0.60 scale, centre 18%, one yellow phrase, 35% shadow).

Other brands must render BYTE-IDENTICALLY to the pre-Session-13 code: the golden file was generated from the committed
de020c1 titles.py, not from the new code."""
import json
import re
from pathlib import Path

import pytest

from clipengine import titles
from clipengine.db import Database

GOLDEN = json.loads((Path(__file__).parent / "golden_title_classic.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", GOLDEN, ids=lambda c: f"{c['w']}x{c['h']}-{c['fg_h']}-{c['title'][:18]}")
def test_other_brands_render_byte_identically_to_the_old_code(case, settings):
    db = Database(settings.db_path)
    for brand in (None, "afterlive", "operator_notes", "spec_edit", "dial_study", "open_floor"):
        st = titles.style_for(db, brand)
        st.enabled = True
        assert (st.variant, st.single_phrase, st.shadow_factor, st.centre_frac, st.emphasis_scale) == ("classic", False, 1.0, None, None)
        text = titles.format_title(case["title"], single_phrase=st.single_phrase)
        assert text == case["formatted"]
        s, e = titles.build_events(text, st, 18.0, case["w"], case["h"], case["fg_h"])
        assert s == case["style"] and e == case["events"]


def _afm(settings, off=False):
    db = Database(settings.db_path)
    if off:
        db.set_kv("title_afm_v2", "0")
    st = titles.style_for(db, "america_first_monitor")
    st.enabled = True
    return st


def test_afm_gets_the_approved_style_unless_the_owner_switches_it_off(settings):
    st = _afm(settings)
    assert (st.scale, st.centre_frac, st.emphasis_scale, st.shadow_factor, st.single_phrase) == (0.60, 0.18, 1.0, 0.35, True)
    assert _afm(settings, off=True).variant == "classic"


def test_afm_marks_exactly_one_whole_yellow_phrase_and_no_half_coloured_words(settings):
    st = _afm(settings)
    for title, phrase in (("Fuentes on Amazon AWS", "AMAZON AWS"), ("Over 20,000 flights were affected", "20,000 FLIGHTS")):
        text = titles.format_title(title, single_phrase=True)
        words = titles.parse(text)
        assert {m for _, m, _ in words} <= {0, 2}                              # never the white-with-yellow-ring mode
        assert " ".join(w for w, m, _ in words if m == 2) == phrase
        _s, ev = titles.build_events(text, st, 18.0, 1080, 1920, 607.5)
        sizes = set(re.findall(r"\\fs(\d+)", ev))
        assert len(sizes) == 1                                                # emphasis is the same size as the lead


def test_afm_title_is_smaller_higher_and_has_a_softer_shadow_than_classic(settings):
    classic = titles.TitleStyle(enabled=True)
    afm = _afm(settings)
    assert afm.size < classic.size * 0.8
    t = "Fuentes on **Amazon AWS**"
    lc = titles.layout(titles.parse(t), classic, 1080, 1920, 607.5)
    la = titles.layout(titles.parse(t), afm, 1080, 1920, 607.5)
    assert la[0]["y"] < lc[0]["y"]                                            # sits higher in the band
    assert max(l["size"] for l in la) < min(l["size"] for l in lc)
    band_h = (1920 - 607.5) / 2
    bottom = max(l["y"] + l["cap"] / 2 for l in la)
    assert bottom <= band_h - 60 + 1                                          # >= 60 px clear of the picture
    shad = lambda s: [float(x) for x in re.findall(r"\\shad([\d.]+)", titles.build_events(t, s, 18.0, 1080, 1920, 607.5)[1]) if float(x) > 0]
    assert max(shad(afm)) < 0.4 * max(shad(classic))


def test_long_afm_titles_still_fit_by_shrinking(settings):
    st = _afm(settings)
    text = titles.format_title("Fuentes on the sweeping social changes that follow a second wave of automation", single_phrase=True)
    lines = titles.layout(titles.parse(text), st, 1080, 1920, 607.5)
    usable = 1080 - 2 * st.side_margin
    assert len(lines) <= st.max_lines
    assert all(titles._width(l["words"], l["size"], st.font) <= usable for l in lines)


def test_owner_written_markup_is_never_overridden():
    assert titles.format_title("Fuentes on *Amazon* **AWS**", single_phrase=True) == "Fuentes on *Amazon* **AWS**"
