"""Session 21: favourites are private selections; only rights-cleared, selected, non-rejected clips are exportable."""
from fastapi.testclient import TestClient

from clipengine.web.app import create_app


def _clip(db, tmp_path, status="ready", name="c.mp4"):
    video = tmp_path / name
    video.write_bytes(b"\x00\x00\x00\x18ftypmp42fixture")
    cid = db.insert("creators", name="Fixture Creator", brand_key="operator_notes",
                    permission_json='{"granted_by": "Fixture owner", "reference": "FX-1"}')
    rid = db.insert("recordings", title="Rec", source_type="file", status="ready", creator_id=cid)
    return db.insert("clips", recording_id=rid, idx=1, start=0, end=20, status=status, video_path=str(video), title="Hook",
                     safety_status="clear")


def test_favourite_is_private_and_survives_restart(settings, tmp_path):
    app = create_app(settings, start_worker=False)
    db = app.state.core.db
    cid = _clip(db, tmp_path)
    with TestClient(app, follow_redirects=False) as client:
        r = client.post(f"/clips/{cid}/favourite", headers={"HX-Request": "true"})
        assert r.status_code == 200 and "★ Favourite" in r.text
        clip = db.one("SELECT status FROM clips WHERE id=?", (cid,))
        assert clip["status"] == "ready"                                   # approval untouched
        assert client.get("/showcase/manifest.json").json()["items"] == []  # nothing exportable yet
    app2 = create_app(settings, start_worker=False)                        # persistence across restart
    with TestClient(app2) as client:
        page = client.get("/showcase").text
        assert "Hook" in page and f"/showcase/{cid}/media" in page
        media = client.get(f"/showcase/{cid}/media")
        assert media.status_code == 200 and media.content.startswith(b"\x00\x00\x00\x18ftyp")


def test_export_requires_cleared_rights_with_evidence_and_selection(settings, tmp_path):
    app = create_app(settings, start_worker=False)
    db = app.state.core.db
    cid = _clip(db, tmp_path)
    with TestClient(app, follow_redirects=False) as client:
        client.post(f"/clips/{cid}/favourite")
        assert client.post(f"/showcase/{cid}/select", data={"selected": "true"}).status_code == 422
        assert client.post(f"/showcase/{cid}/rights", data={"status": "cleared", "evidence": "ok"}).status_code == 422
        assert client.post(f"/showcase/{cid}/rights", data={"status": "cleared",
                           "evidence": "Email from rights holder 2026-11-28, ref PC-001"}).status_code == 303
        assert client.post(f"/showcase/{cid}/select", data={"selected": "true",
                           "collection": "business"}).status_code == 303
        items = client.get("/showcase/manifest.json").json()["items"]
        assert [i["clip_id"] for i in items] == [cid] and items[0]["collection"] == "business"
        # withdrawing rights removes it from the external selection
        client.post(f"/showcase/{cid}/rights", data={"status": "not_cleared", "evidence": ""})
        assert client.get("/showcase/manifest.json").json()["items"] == []


def test_rejected_clips_cannot_be_favourited_or_exported(settings, tmp_path):
    app = create_app(settings, start_worker=False)
    db = app.state.core.db
    good = _clip(db, tmp_path)
    bad = _clip(db, tmp_path, status="rejected", name="r.mp4")
    with TestClient(app, follow_redirects=False) as client:
        assert client.post(f"/clips/{bad}/favourite").status_code == 409
        client.post(f"/clips/{good}/favourite")
        client.post(f"/showcase/{good}/rights", data={"status": "cleared", "evidence": "Written grant 2026-11-28"})
        client.post(f"/showcase/{good}/select", data={"selected": "true"})
        db.update("clips", good, status="rejected")                    # later rejected
        assert client.get("/showcase/manifest.json").json()["items"] == []
        assert client.get(f"/showcase/{good}/media").status_code == 404
        events = [r["action"] for r in db.query("SELECT action FROM favourite_events WHERE clip_id=? ORDER BY id", (good,))]
        assert events[:3] == ["favourited", "rights", "selected"]


def test_unfavourite_and_star_shown_on_mobile_review(settings, tmp_path):
    app = create_app(settings, start_worker=False)
    db = app.state.core.db
    cid = _clip(db, tmp_path)
    with TestClient(app, follow_redirects=False) as client:
        assert f'/clips/{cid}/favourite' in client.get("/m").text
        client.post(f"/clips/{cid}/favourite")
        client.post(f"/clips/{cid}/favourite")
        assert db.one("SELECT 1 FROM clip_favourites WHERE clip_id=?", (cid,)) is None
