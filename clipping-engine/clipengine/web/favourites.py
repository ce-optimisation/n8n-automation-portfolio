"""Favourites and the private commercial portfolio (Session 21, owner-approved 28 Nov 2026).

A favourite is a private selection preference. It never publishes a clip, changes its review status, shares a
cloud file or establishes rights. Only clips the owner marks as promotionally cleared (with written evidence) and
selects for the external portfolio appear in the export manifest used by commercial_agency/portfolio_site.py.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from fastapi import Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

RIGHTS = ("unknown", "cleared", "not_cleared")
SCHEMA = """
CREATE TABLE IF NOT EXISTS clip_favourites (
    clip_id INTEGER PRIMARY KEY REFERENCES clips(id) ON DELETE CASCADE,
    favourited_at REAL NOT NULL,
    display_order INTEGER NOT NULL DEFAULT 0,
    featured INTEGER NOT NULL DEFAULT 0,
    promo_rights TEXT NOT NULL DEFAULT 'unknown' CHECK (promo_rights IN ('unknown','cleared','not_cleared')),
    promo_rights_evidence TEXT,
    portfolio_selected INTEGER NOT NULL DEFAULT 0,
    collection TEXT,
    note TEXT
);
CREATE TABLE IF NOT EXISTS favourite_events (
    id INTEGER PRIMARY KEY,
    clip_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    detail TEXT,
    at REAL NOT NULL
);
"""
EXCLUDED_STATUSES = ("rejected", "deleted", "discarded")


def _event(db, cid: int, action: str, detail: str | None = None) -> None:
    db.execute("INSERT INTO favourite_events (clip_id,action,detail,at) VALUES (?,?,?,?)", (cid, action, detail, time.time()))


def is_favourite(db, cid: int) -> bool:
    return db.one("SELECT 1 FROM clip_favourites WHERE clip_id=?", (cid,)) is not None


def favourite_ids(db) -> set[int]:
    return {r["clip_id"] for r in db.query("SELECT clip_id FROM clip_favourites")}


class _FavCache:
    """One query per page render instead of one per star (the mobile results list showed 15 extra reads).
    Toggling invalidates immediately; the short TTL only covers changes made outside this process."""
    TTL = 2.0

    def __init__(self, db):
        self.db, self.ids, self.at = db, set(), 0.0

    def __call__(self, cid) -> bool:
        if time.monotonic() - self.at > self.TTL:
            self.ids, self.at = favourite_ids(self.db), time.monotonic()
        return cid in self.ids

    def invalidate(self) -> None:
        self.at = 0.0


def portfolio_rows(db, brand: str = "", creator: str = "", rights: str = "") -> list[dict]:
    sql = ("SELECT f.*, c.title, c.status, c.start, c.end, c.video_path, c.recording_id, "
           "cr.name AS creator, cr.brand_key AS brand_key, b.name AS brand, "
           "(SELECT o.remote_id FROM cloud_objects o WHERE o.owner_kind='clip' AND o.owner_id=c.id AND o.kind='video' "
           " AND o.state='verified') AS cloud_id "
           "FROM clip_favourites f JOIN clips c ON c.id=f.clip_id "
           "LEFT JOIN recordings r ON r.id=c.recording_id LEFT JOIN creators cr ON cr.id=r.creator_id "
           "LEFT JOIN brands b ON b.key=cr.brand_key WHERE 1=1")
    args: list = []
    if brand:
        sql += " AND cr.brand_key=?"; args.append(brand)
    if creator:
        sql += " AND cr.name=?"; args.append(creator)
    if rights in RIGHTS:
        sql += " AND f.promo_rights=?"; args.append(rights)
    sql += " ORDER BY f.featured DESC, f.display_order, f.favourited_at DESC"
    out = []
    for r in db.query(sql, tuple(args)):
        d = dict(r)
        d["local"] = bool(d["video_path"]) and Path(d["video_path"]).is_file()
        d["eligible"] = (d["promo_rights"] == "cleared" and d["status"] not in EXCLUDED_STATUSES
                         and (d["local"] or d["cloud_id"]))
        d["duration"] = round((d["end"] or 0) - (d["start"] or 0), 1)
        out.append(d)
    return out


def export_manifest(db) -> dict:
    """What the external portfolio may show: cleared, selected, non-rejected clips with available media."""
    items = [r for r in portfolio_rows(db) if r["portfolio_selected"] and r["eligible"]]
    return {"generated_at": time.time(), "items": [
        {"clip_id": r["clip_id"], "title": r["title"] or f"Clip {r['clip_id']}", "brand": r["brand"],
         "creator": r["creator"], "collection": r["collection"], "featured": bool(r["featured"]),
         "order": r["display_order"], "duration": r["duration"], "video_path": r["video_path"] if r["local"] else None,
         "cloud_id": r["cloud_id"], "rights_evidence": r["promo_rights_evidence"]} for r in items]}


def register(app, core, page, templates=None) -> None:
    db = core.db
    if templates is not None:
        cache = _FavCache(db)
        templates.env.globals["is_favourite"] = cache
        app.state.favourite_cache = cache

    def clip_or_404(cid: int):
        c = db.one("SELECT * FROM clips WHERE id=?", (cid,))
        if c is None:
            raise HTTPException(404)
        return c

    def star(cid: int, on: bool) -> HTMLResponse:
        label = "★ Favourite" if on else "☆ Favourite"
        return HTMLResponse(
            f'<form class="fav" method="post" action="/clips/{cid}/favourite" hx-post="/clips/{cid}/favourite" '
            f'hx-swap="outerHTML"><button class="btn{" on" if on else ""}" aria-pressed="{str(on).lower()}" '
            f'title="Private selection only: does not publish, approve or share">{label}</button></form>')

    @app.post("/clips/{cid}/favourite")
    def toggle_favourite(request: Request, cid: int):
        c = clip_or_404(cid)
        if is_favourite(db, cid):
            db.execute("DELETE FROM clip_favourites WHERE clip_id=?", (cid,))
            _event(db, cid, "unfavourited")
            on = False
        else:
            if c["status"] in EXCLUDED_STATUSES:
                raise HTTPException(409, "A rejected clip cannot be favourited.")
            order = (db.one("SELECT COALESCE(MAX(display_order),0)+1 AS n FROM clip_favourites") or {"n": 1})["n"]
            db.execute("INSERT INTO clip_favourites (clip_id,favourited_at,display_order) VALUES (?,?,?)",
                       (cid, time.time(), order))
            _event(db, cid, "favourited")
            on = True
        if getattr(app.state, "favourite_cache", None):
            app.state.favourite_cache.invalidate()
        if request.headers.get("HX-Request") == "true":
            return star(cid, on)
        back = request.headers.get("referer") or "/showcase"
        return RedirectResponse(back if back.startswith(("/", str(request.base_url))) else "/showcase", status_code=303)

    @app.get("/clips/{cid}/favourite", response_class=HTMLResponse)
    def favourite_button(cid: int):
        clip_or_404(cid)
        return star(cid, is_favourite(db, cid))

    @app.get("/showcase", response_class=HTMLResponse)
    def showcase(request: Request, brand: str = "", creator: str = "", rights: str = ""):
        rows = portfolio_rows(db, brand, creator, rights)
        brands = db.query("SELECT key, name FROM brands ORDER BY name")
        creators = sorted({r["creator"] for r in portfolio_rows(db) if r["creator"]})
        return page(request, "showcase.html", rows=rows, brands=brands, creators=creators,
                    f={"brand": brand, "creator": creator, "rights": rights},
                    selected=sum(1 for r in rows if r["portfolio_selected"] and r["eligible"]))

    def fav_or_404(cid: int):
        row = db.one("SELECT * FROM clip_favourites WHERE clip_id=?", (cid,))
        if row is None:
            raise HTTPException(404, "Not a favourite.")
        return row

    @app.post("/showcase/{cid}/rights")
    def set_rights(cid: int, status: str = Form(...), evidence: str = Form("")):
        fav_or_404(cid)
        evidence = evidence.strip()[:1000]
        if status not in RIGHTS:
            raise HTTPException(422, "Unknown rights status.")
        if status == "cleared" and len(evidence) < 10:
            raise HTTPException(422, "Record where the promotional-use permission is written (document, email, date).")
        selected_sql = ", portfolio_selected=0" if status != "cleared" else ""
        db.execute(f"UPDATE clip_favourites SET promo_rights=?, promo_rights_evidence=?{selected_sql} WHERE clip_id=?",
                   (status, evidence or None, cid))
        _event(db, cid, "rights", f"{status}: {evidence}")
        return RedirectResponse("/showcase", status_code=303)

    @app.post("/showcase/{cid}/select")
    def select_for_portfolio(cid: int, selected: bool = Form(False), collection: str = Form("")):
        fav = fav_or_404(cid)
        c = clip_or_404(cid)
        if selected and (fav["promo_rights"] != "cleared" or c["status"] in EXCLUDED_STATUSES):
            raise HTTPException(422, "Only clips with cleared promotional rights can be selected for the external portfolio.")
        db.execute("UPDATE clip_favourites SET portfolio_selected=?, collection=? WHERE clip_id=?",
                   (int(selected), collection.strip()[:60] or None, cid))
        _event(db, cid, "selected" if selected else "deselected", collection or None)
        return RedirectResponse("/showcase", status_code=303)

    @app.post("/showcase/{cid}/arrange")
    def arrange(cid: int, order: int = Form(0), featured: bool = Form(False)):
        fav_or_404(cid)
        db.execute("UPDATE clip_favourites SET display_order=?, featured=? WHERE clip_id=?", (order, int(featured), cid))
        return RedirectResponse("/showcase", status_code=303)

    @app.post("/showcase/{cid}/recover")
    def recover(cid: int):
        """Download a favourite's verified Drive backup into a private cache when the local file is gone."""
        fav_or_404(cid)
        c = clip_or_404(cid)
        if c["status"] in EXCLUDED_STATUSES:
            raise HTTPException(409, "Rejected clips are never restored.")
        obj = db.one("SELECT * FROM cloud_objects WHERE owner_kind='clip' AND owner_id=? AND kind='video' "
                     "AND state='verified'", (cid,))
        if obj is None:
            raise HTTPException(404, "No verified cloud copy of this clip.")
        dest = core.settings.data_dir / "portfolio_cache" / f"clip_{cid}.mp4"
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_suffix(".part")
        core.cloud.provider.download(obj["remote_id"], part)
        md5 = hashlib.md5(part.read_bytes()).hexdigest()
        if obj["local_md5"] and md5 != obj["local_md5"]:
            part.unlink(missing_ok=True)
            raise HTTPException(502, "Downloaded copy failed its checksum; nothing was kept.")
        part.replace(dest)
        _event(db, cid, "recovered", md5)
        return RedirectResponse("/showcase", status_code=303)

    @app.get("/showcase/{cid}/media")
    def portfolio_media(cid: int):
        fav_or_404(cid)
        c = clip_or_404(cid)
        if c["status"] in EXCLUDED_STATUSES:
            raise HTTPException(404)
        for p in (c["video_path"], str(core.settings.data_dir / "portfolio_cache" / f"clip_{cid}.mp4")):
            if p and Path(p).is_file():
                return FileResponse(p, media_type="video/mp4", filename=f"clip_{cid}.mp4")
        raise HTTPException(404, "Media not available locally; use Recover from Drive.")

    @app.get("/showcase/manifest.json")
    def manifest():
        return JSONResponse(export_manifest(db))
