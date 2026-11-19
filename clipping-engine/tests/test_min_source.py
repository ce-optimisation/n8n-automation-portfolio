"""Session 13: safety net at download time for the MIN_SOURCE_SECONDS rule (the extractor is mocked; nothing is downloaded)."""
from types import SimpleNamespace

from clipengine import ingest
from clipengine.db import Database


class Ctx:
    def __init__(self, payload):
        self.payload, self.id, self.logs = payload, 1, []

    def log(self, msg, level="INFO"):
        self.logs.append(msg)

    def progress(self, *a, **k):
        pass

    def cancelled(self):
        return False


def _setup(settings, tmp_path, monkeypatch, duration):
    f = tmp_path / "ytdlp_x" / "v.mp4"
    f.parent.mkdir()
    f.write_bytes(b"not a real video")
    monkeypatch.setattr(ingest, "classify_url", lambda *a, **k: SimpleNamespace(
        code="ok", ok=True, method="ytdlp", host="youtube.com", message="test"))
    monkeypatch.setattr(ingest, "_download_with_ytdlp", lambda s, u, c: (f, {"id": "x", "title": "T", "duration": duration,
                                                                             "extractor_key": "Youtube"}))
    return f


def test_automatic_short_is_dropped_and_marked_skipped(settings, tmp_path, monkeypatch):
    settings.min_source_seconds = 180
    db = Database(settings.db_path)
    f = _setup(settings, tmp_path, monkeypatch, 45)
    sid = db.insert("sources", name="t", kind="channel", url="https://www.youtube.com/channel/UC1", enabled=1)
    iid = db.insert("source_items", source_id=sid, item_key="k1", title="T", url="https://www.youtube.com/watch?v=x", state="queued")
    ctx = Ctx({"url": "https://www.youtube.com/watch?v=x", "source_item_id": iid})
    ingest.handle_download_url(settings, db, queue=None, ctx=ctx)       # returns before registering or enqueuing anything
    assert db.one("SELECT state FROM source_items WHERE id=?", (iid,))["state"] == "skipped"
    assert not f.exists() and db.one("SELECT COUNT(*) n FROM recordings")["n"] == 0
    assert any("minimum" in m for m in ctx.logs)


def test_manual_import_of_a_short_video_is_never_skipped(settings, tmp_path, monkeypatch):
    settings.min_source_seconds = 180
    db = Database(settings.db_path)
    _setup(settings, tmp_path, monkeypatch, 45)
    reached = []
    monkeypatch.setattr(ingest, "register_file", lambda *a, **k: reached.append(1) or (1, False))
    monkeypatch.setattr(ingest, "permission_for", lambda *a, **k: None)
    q = SimpleNamespace(enqueue=lambda *a, **k: 1)
    monkeypatch.setattr(db, "update", lambda *a, **k: None)
    ingest.handle_download_url(settings, db, queue=q, ctx=Ctx({"url": "https://www.youtube.com/watch?v=x"}))
    assert reached == [1]                      # no item id = the owner asked for it by hand: processed as normal
