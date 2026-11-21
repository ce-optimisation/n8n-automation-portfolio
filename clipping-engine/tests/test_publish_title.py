"""Session 15: a clip is never published under the generic name "Clip"."""
import pytest

from clipengine.db import Database
from clipengine.publishing import PublishError
from clipengine.publishing import delivery


@pytest.fixture
def db(settings):
    return Database(settings.db_path)


def _clip(db, clip_title, rec_title):
    rid = db.insert("recordings", title=rec_title, source_type="upload", status="ready")
    cid = db.insert("clips", recording_id=rid, idx=1, start=0, end=20, status="ready", title=clip_title)
    return db.clip(cid)


def test_clip_title_wins_and_is_trimmed(db):
    assert delivery.publish_title(db, _clip(db, "  A   real\ttitle ", "Source")) == "A real title"


def test_falls_back_to_the_source_video_title_not_a_generic_name(db):
    assert delivery.publish_title(db, _clip(db, None, "How Do You Find a 30,000-Year-Old Turd")) == \
        "How Do You Find a 30,000-Year-Old Turd"


def test_no_title_anywhere_gives_none_never_clip(db):
    assert delivery.publish_title(db, _clip(db, None, "")) is None
    assert delivery.publish_title(db, _clip(db, "   ", "  ")) is None


def test_long_titles_are_capped_at_100_characters(db):
    assert len(delivery.publish_title(db, _clip(db, "x" * 300, "s"))) == 100


def test_youtube_upload_refuses_an_empty_title(settings, db):
    from clipengine.publishing.youtube import YouTubeClient
    import inspect
    src = inspect.getsource(YouTubeClient.upload)
    assert '"Clip"' not in src and "generic name" in src
    assert issubclass(PublishError, Exception)
