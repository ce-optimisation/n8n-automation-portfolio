"""Clipping Engine dashboard routes for the local agency prospect register."""

from contextlib import closing
import json
import sqlite3
from urllib.parse import urlencode

from fastapi import Form, Request
from fastapi.responses import JSONResponse, RedirectResponse

from . import crm


def register(app, settings, page):
    """Register routes protected by the Clipping Engine dashboard middleware."""
    db_path = settings.data_dir / "commercial_agency" / "agency.sqlite3"

    def records():
        if not db_path.exists():
            return []
        with closing(crm.connect(db_path)) as db:
            return sorted(crm.export(db), key=lambda record: (-(record["score"] or 0), record["id"]))

    @app.get("/agency")
    def agency(request: Request, added: int | None = None, skipped: int = 0):
        try:
            return page(request, "agency.html", prospects=records(), added=added, skipped=skipped)
        except (sqlite3.Error, ValueError):
            response = page(request, "agency.html", error="The agency register could not be opened.")
            response.status_code = 503
            return response

    @app.get("/agency/export")
    def agency_export():
        try:
            return JSONResponse(records(), headers={
                "Content-Disposition": 'attachment; filename="agency-prospects.json"',
                "Cache-Control": "no-store",
            })
        except (sqlite3.Error, ValueError):
            return JSONResponse({"error": "The agency register could not be opened."}, status_code=503)

    @app.post("/agency/import")
    def agency_import(request: Request, research: str = Form("")):
        try:
            if len(research.encode("utf-8")) > 256 * 1024:
                raise ValueError("Import must be 256 KB or smaller.")
            try:
                prospects = json.loads(research)
            except (ValueError, RecursionError) as error:
                raise ValueError("Enter a valid research JSON array.") from error
            db_path.parent.mkdir(parents=True, exist_ok=True)
            with closing(crm.connect(db_path, initialize=True)) as db:
                result = crm.import_research(db, prospects)
        except ValueError as error:
            response = page(request, "agency.html", prospects=[], error=str(error))
            response.status_code = 400
            return response
        except (OSError, sqlite3.Error):
            response = page(request, "agency.html", prospects=[],
                            error="The agency register could not be updated.")
            response.status_code = 503
            return response
        query = urlencode({"added": len(result["added"]), "skipped": len(result["skipped_existing"])})
        return RedirectResponse("/agency?" + query, status_code=303)
