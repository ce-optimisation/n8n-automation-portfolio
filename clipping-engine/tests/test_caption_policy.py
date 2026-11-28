"""Session 21 owner decision (28 Nov 2026): no automatically generated captions burned into the six owner brands'
new clips. Transcription and internal subtitle data continue; customer and approved-exception profiles keep captions."""
import json
import re

import pytest

from clipengine import captions, media, render
from clipengine.db import Database


def _phrases(text, t0):
    words, t = [], t0
    for w in text.split():
        words.append({"word": w, "start": t, "end": t + 0.4})
        t += 0.45
    return words


@pytest.mark.parametrize("brand", captions.OWNER_BRANDS_CAPTIONS_OFF)
def test_owner_brands_default_to_no_generated_captions(settings, brand):
    db = Database(settings.db_path)
    assert captions.style_for(db, brand).enabled is False


def test_customer_brands_and_unattributed_keep_captions(settings):
    db = Database(settings.db_path)
    assert captions.style_for(db, "customer_brand").enabled is True
    assert captions.style_for(db, None).enabled is True


def test_exception_is_explicit_and_reversible(settings):
    db = Database(settings.db_path)
    captions.set_caption_exception(db, "open_floor", True)
    assert captions.style_for(db, "open_floor").enabled is True
    assert captions.style_for(db, "afterlive").enabled is False
    captions.set_caption_exception(db, "open_floor", False)
    assert captions.style_for(db, "open_floor").enabled is False
    assert json.loads(db.get_kv("caption_exception_brands")) == []


def test_real_render_owner_brand_has_no_burned_captions_but_keeps_transcript(settings, sample_video, tmp_path):
    db = Database(settings.db_path)
    info = media.probe(sample_video)
    words = _phrases("Tonight the council will discuss the new bridge budget.", 1.0)
    off = render.render_clip(settings, sample_video, tmp_path / "owner", 1.0, 5.0, words, info["width"],
                             info["height"], None, style=captions.style_for(db, "afterlive"))
    on = render.render_clip(settings, sample_video, tmp_path / "customer", 1.0, 5.0, words, info["width"],
                            info["height"], None, style=captions.style_for(db, "customer_brand"))
    assert off["caption_style"] == "off" and on["caption_style"] != "off"
    assert not re.search(r"^Dialogue:.*council", off["ass"].read_text(encoding="utf-8"), re.M)
    assert re.search(r"^Dialogue:", on["ass"].read_text(encoding="utf-8"), re.M)
    assert "council" in off["srt"].read_text(encoding="utf-8")      # internal subtitle data still produced
    assert (off["probe"]["width"], off["probe"]["height"]) == (1080, 1920)


def test_campaign_requiring_captions_on_owner_brand_is_a_reported_conflict(settings, sample_video, tmp_path):
    from clipengine import campaign_render
    from clipengine.jobs import PermanentError
    db = Database(settings.db_path)
    cid = db.insert("creators", name="Fixture Creator", brand_key="afterlive")
    rid = db.insert("recordings", title="r", source_type="file", status="ready", file_path=str(sample_video),
                    creator_id=cid)
    rec = db.one("SELECT * FROM recordings WHERE id=?", (rid,))
    clip = {"start": 1.0, "end": 4.0, "id": 1}
    with pytest.raises(PermanentError, match="owner policy"):
        campaign_render.create_preview(settings, db, rec, clip, tmp_path / "p.mp4", "vertical_720", True)
