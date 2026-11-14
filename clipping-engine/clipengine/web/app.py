"""FastAPI + HTMX editorial dashboard."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlencode, urlparse

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import hardware, inbox, ingest, maintenance, render, segment, setup_status
from ..config import Settings
from ..db import Database
from ..jobs import JobQueue, PermanentError
from ..pipeline import Pipeline
from ..publishing import (STATES, InstagramClient, NotConfigured, PublishError, TikTokClient, TokenStore,
                          editorial_gate)
from ..publishing.service import PublishService

log = logging.getLogger("clipengine.web")
HERE = Path(__file__).resolve().parent

RIGHTS_CHOICES = [
    ("unreviewed", "Not yet reviewed"),
    ("owned", "We own the recording"),
    ("licensed", "Licensed / permission obtained"),
    ("public_domain", "Public domain / open licence"),
    ("fair_dealing_reviewed", "Fair dealing / fair use reviewed (reporting, criticism)"),
    ("not_cleared", "Not cleared — do not publish"),
]


class App:
    """Holds shared services for the web layer."""

    def __init__(self, settings: Settings):
        settings.ensure_dirs()
        self.settings = settings
        self.db = Database(settings.db_path)
        self.queue = JobQueue(self.db, settings.max_attempts)
        self.pipeline = Pipeline(settings, self.db, self.queue)
        self.tokens = TokenStore(settings.secrets_dir)
        self.tiktok = TikTokClient(settings.tiktok_client_key, settings.tiktok_client_secret,
                                   settings.tiktok_redirect_uri, self.tokens, app_type=settings.tiktok_app_type)
        if settings.instagram_auth_mode == "instagram":
            self.instagram = InstagramClient(settings.instagram_app_id, settings.instagram_app_secret,
                                             settings.instagram_redirect_uri, self.tokens, settings.meta_graph_version,
                                             mode="instagram", public_base_url=settings.public_media_base_url)
        else:
            self.instagram = InstagramClient(settings.meta_app_id, settings.meta_app_secret,
                                             settings.meta_redirect_uri, self.tokens, settings.meta_graph_version)
        self.publisher = PublishService(settings, self.db, self.queue, self.tiktok, self.instagram)
        self.queue.register("download_model", self._download_model)
        self.maintenance = maintenance.Maintenance(settings, self.db)
        self.inbox = inbox.InboxWatcher(settings, self.db, self.pipeline)
        self.login_failures: dict[str, list[float]] = {}
        self.oauth_state: dict[str, dict] = {}
        key_file = settings.secrets_dir / "session_key"
        if not key_file.exists():
            key_file.write_text(secrets.token_hex(32))
        self.session_key = key_file.read_text().strip().encode()

    def _download_model(self, ctx) -> None:
        from ..transcribe import WhisperTranscriber
        t = WhisperTranscriber(self.settings)
        ctx.progress(0.1, f"Downloading/loading {t.model_label} (one-time, may take several minutes)", force=True)
        secs = t.prepare()
        ctx.log(f"Model {t.model_label} ready in {secs:.0f}s")


def create_app(settings: Settings, start_worker: bool = True) -> FastAPI:
    core = App(settings)

    @asynccontextmanager
    async def lifespan(_app):
        if start_worker:
            core.queue.start()
            core.maintenance.start()
            core.inbox.start()
        try:
            yield
        finally:
            core.queue.stop()
            core.maintenance.stop()
            core.inbox.stop()

    app = FastAPI(title="Local Clipping Engine", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.core = core
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.filters["mmss"] = render.mmss
    templates.env.filters["fromjson"] = lambda s: json.loads(s) if s else []
    templates.env.filters["dt"] = lambda t: time.strftime("%Y-%m-%d %H:%M", time.localtime(t)) if t else ""
    templates.env.globals["RIGHTS_CHOICES"] = RIGHTS_CHOICES
    templates.env.globals["PUB_STATES"] = STATES
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    def page(request: Request, name: str, **ctx):
        ctx.setdefault("counts", core.db.counts())
        return templates.TemplateResponse(request, name, ctx)

    # ---- auth & CSRF -----------------------------------------------------
    def _auth_token() -> str:
        return hmac.new(core.session_key, settings.dashboard_password.encode(), hashlib.sha256).hexdigest()

    LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}

    @app.middleware("http")
    async def security(request: Request, call_next):
        path = request.url.path
        if path.startswith("/public/media/"):
            return await call_next(request)      # one-time tokenised clip links only (see route)
        host = (request.headers.get("host") or "").rsplit(":", 1)[0].lower() if not \
            (request.headers.get("host") or "").startswith("[") else (request.headers.get("host") or "").split("]")[0] + "]"
        if not settings.dashboard_password and host not in LOCAL_HOSTS and host != "testserver":
            # Without a password the dashboard only answers to localhost names. This also blocks
            # DNS-rebinding attacks from web pages open in the owner's browser.
            return PlainTextResponse("This dashboard has no password, so it only accepts connections addressed "
                                     "to localhost. Set DASHBOARD_PASSWORD in .env to use it from another device.",
                                     status_code=403)
        if request.method == "POST":
            origin = request.headers.get("origin") or request.headers.get("referer")
            if origin and urlparse(origin).netloc != request.headers.get("host"):
                return PlainTextResponse("Cross-origin request refused", status_code=403)
        if settings.dashboard_password and not (path.startswith("/static") or path in ("/login", "/healthz")
                                                or path.startswith("/oauth/")):
            if not hmac.compare_digest(request.cookies.get("ce_auth", ""), _auth_token()):
                return RedirectResponse("/login", status_code=303)
        return await call_next(request)

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request, error: str = ""):
        return templates.TemplateResponse(request, "login.html", {"error": error, "counts": None})

    @app.post("/login")
    def login(request: Request, password: str = Form(...)):
        ip = request.client.host if request.client else "?"
        now = time.time()
        recent = [t for t in core.login_failures.get(ip, []) if now - t < 600]
        if len(recent) >= 5:
            return PlainTextResponse("Too many failed sign-in attempts. Wait 10 minutes.", status_code=429)
        if settings.dashboard_password and hmac.compare_digest(password.encode(), settings.dashboard_password.encode()):
            core.login_failures.pop(ip, None)
            resp = RedirectResponse("/", status_code=303)
            https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
            resp.set_cookie("ce_auth", _auth_token(), httponly=True, samesite="strict", max_age=30 * 86400,
                            secure=https)
            return resp
        core.login_failures[ip] = recent + [now]
        time.sleep(1)
        return RedirectResponse("/login?error=1", status_code=303)

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "app": "clipengine", "worker_alive": bool(core.queue._thread and core.queue._thread.is_alive())}

    # ---- dashboard -------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        recs = core.db.query("SELECT * FROM recordings ORDER BY id DESC LIMIT 10")
        return page(request, "dashboard.html", recordings=recs, storage=maintenance.storage_report(settings))

    @app.get("/partials/jobs", response_class=HTMLResponse)
    def jobs_partial(request: Request, limit: int = 15):
        jobs = core.db.query("SELECT j.*, r.title FROM jobs j LEFT JOIN recordings r ON r.id = j.recording_id "
                             "ORDER BY CASE j.status WHEN 'running' THEN 0 WHEN 'queued' THEN 1 "
                             "WHEN 'failed' THEN 2 ELSE 3 END, j.id DESC LIMIT ?", (limit,))
        return templates.TemplateResponse(request, "_jobs.html", {"jobs": jobs})

    @app.get("/partials/counts", response_class=HTMLResponse)
    def counts_partial(request: Request):
        return templates.TemplateResponse(request, "_counts.html", {"counts": core.db.counts()})

    # ---- import ----------------------------------------------------------
    @app.get("/import", response_class=HTMLResponse)
    def import_form(request: Request, msg: str = "", err: str = ""):
        pending = core.db.query("SELECT * FROM pending_sources WHERE status='awaiting_file' ORDER BY id DESC")
        return page(request, "import.html", msg=msg, err=err, s=settings, pending=pending,
                    restricted=sorted(ingest.RESTRICTED_PLATFORMS))

    def _overrides(clips, min_s, max_s, language) -> dict:
        return {k: v for k, v in {"clips_per_recording": clips, "clip_min_seconds": min_s,
                                  "clip_max_seconds": max_s, "language": language}.items() if v not in (None, "")}

    @app.post("/import/upload")
    async def import_upload(request: Request, file: UploadFile = File(...), title: str = Form(""),
                            clips: str = Form(""), min_s: str = Form(""), max_s: str = Form(""),
                            language: str = Form(""), rights: str = Form(""), pending_id: str = Form("")):
        name = ingest.safe_filename(file.filename or "upload.mp4")
        if Path(name).suffix.lower() not in ingest.VIDEO_EXTS:
            return _import_result(request, err=f"Unsupported file type: {file.filename}")
        tmp = settings.work_dir / f"upload_{int(time.time() * 1000)}_{name}"
        limit = settings.max_upload_mb * 1024 * 1024
        written = 0
        with open(tmp, "wb") as out:
            while chunk := await file.read(8 * 1024 * 1024):
                written += len(chunk)
                if written > limit:
                    out.close()
                    tmp.unlink(missing_ok=True)
                    return _import_result(request, err=f"File exceeds MAX_UPLOAD_MB ({settings.max_upload_mb} MB)")
                out.write(chunk)
        try:
            if pending_id.strip():
                rid, dup = ingest.attach_file_to_pending(settings, core.db, int(pending_id), tmp,
                                                         original_name=file.filename or name, title=title or None,
                                                         attestation=rights or None)
                if not dup:
                    core.queue.enqueue("process_recording", recording_id=rid, dedupe_key=f"process:{rid}",
                                       payload={"settings": {}})
            else:
                rid, dup, _ = core.pipeline.import_upload(tmp, file.filename or name, title or None,
                                                          _overrides(clips, min_s, max_s, language), rights or None)
        except (PermanentError, ValueError) as e:
            tmp.unlink(missing_ok=True)
            return _import_result(request, err=str(e))
        if dup:
            return _import_result(request, msg=f"This file was already imported as recording #{rid}. "
                                                "It was not processed again.", rid=rid)
        return _import_result(request, msg=f"Imported as recording #{rid}. Analysis queued.", rid=rid)

    def _import_result(request: Request, msg: str = "", err: str = "", rid: int | None = None):
        if request.headers.get("hx-request") or request.headers.get("x-requested-with"):
            return JSONResponse({"msg": msg, "err": err, "recording_id": rid}, status_code=400 if err else 200)
        return RedirectResponse("/import?" + urlencode({"msg": msg} if msg else {"err": err}), status_code=303)

    @app.post("/import/url")
    def import_url(request: Request, url: str = Form(...), title: str = Form(""), clips: str = Form(""),
                   min_s: str = Form(""), max_s: str = Form(""), language: str = Form(""),
                   attest: str = Form("")):
        decision = ingest.classify_url(url, settings, core.db)
        overrides = _overrides(clips, min_s, max_s, language)
        if decision.method == "link_only":
            pid, existed = ingest.create_pending_source(core.db, url, title=title or None, reason=decision.message,
                                                        settings_json=overrides)
            return _import_result(request, msg=(
                f"Saved as pending source #{pid}{' (already saved)' if existed else ''}. {decision.message} "
                f"To finish, upload the file under 'Pending sources' below, or save it into the inbox folder with "
                f"'src-{pid}' in its file name. The link is kept as the recording's provenance."))
        if not decision.ok:
            return _import_result(request, err=decision.message)
        if attest != "yes":
            return _import_result(request, err="Confirm that you are authorised to retrieve this video.")
        dup = ingest.find_duplicate(core.db, url=url)
        if dup:
            return _import_result(request, msg=f"This URL was already imported as recording #{dup['id']}.",
                                  rid=dup["id"])
        core.queue.enqueue("download_url", payload={
            "url": url.strip(), "title": title, "settings": _overrides(clips, min_s, max_s, language),
            "rights_attestation": f"Owner attested authorisation to retrieve {url} at {time.ctime()}"},
            dedupe_key=f"download:{ingest.normalize_url(url)}", max_attempts=6)
        return _import_result(request, msg="Download queued. It will be analysed automatically when complete.")

    # ---- recordings ------------------------------------------------------
    @app.get("/recordings", response_class=HTMLResponse)
    def recordings(request: Request):
        recs = core.db.query("SELECT r.*, (SELECT COUNT(*) FROM clips c WHERE c.recording_id=r.id) nclips "
                             "FROM recordings r ORDER BY id DESC")
        return page(request, "recordings.html", recordings=recs)

    def _rec(rid: int):
        rec = core.db.recording(rid)
        if not rec:
            raise HTTPException(404, "Recording not found")
        return rec

    @app.get("/recordings/{rid}", response_class=HTMLResponse)
    def recording_detail(request: Request, rid: int):
        rec = _rec(rid)
        clips = core.db.query("SELECT * FROM clips WHERE recording_id = ? ORDER BY start", (rid,))
        segs = core.db.segments(rid)
        jobs = core.db.query("SELECT * FROM jobs WHERE recording_id = ? ORDER BY id DESC LIMIT 20", (rid,))
        conn = core.publisher.connections()
        pubs = {c["id"]: core.publisher.summary(c, conn) for c in clips if c["status"] == "approved"}
        return page(request, "recording.html", rec=rec, clips=clips, segments=segs, jobs=jobs, pubs=pubs,
                    meta=json.loads(rec["metadata_json"] or "{}"))

    @app.get("/recordings/{rid}/media")
    def recording_media(rid: int):
        rec = _rec(rid)
        if not rec["file_path"] or not Path(rec["file_path"]).is_file():
            raise HTTPException(404, "Original file not available")
        return FileResponse(rec["file_path"], media_type="video/mp4")

    @app.get("/recordings/{rid}/transcript.txt")
    def recording_transcript(rid: int):
        rec = _rec(rid)
        lines = [f"{rec['title']}\nSource: {rec['source_url'] or rec['original_filename']}\n"]
        lines += [f"[{render.mmss(s['start'])}] {s['text']}" for s in core.db.segments(rid)]
        return PlainTextResponse("\n".join(lines), headers={
            "Content-Disposition": f'attachment; filename="{ingest.safe_filename(rec["title"])}_transcript.txt"'})

    @app.post("/recordings/{rid}/reanalyse")
    def reanalyse(rid: int, retranscribe: str = Form("")):
        _rec(rid)
        core.queue.enqueue("process_recording", recording_id=rid, dedupe_key=f"process:{rid}",
                           payload={"reselect": True, "retranscribe": retranscribe == "yes"})
        return RedirectResponse(f"/recordings/{rid}", status_code=303)

    @app.post("/recordings/{rid}/delete")
    def delete_recording(rid: int, confirm: str = Form("")):
        rec = _rec(rid)
        if confirm != rec["title"]:
            raise HTTPException(400, "Type the exact recording title to confirm deletion.")
        for c in core.db.query("SELECT id FROM clips WHERE recording_id=?", (rid,)):
            core.pipeline.delete_clip_files(c["id"])
        if rec["file_path"]:
            shutil.rmtree(Path(rec["file_path"]).parent, ignore_errors=True)
        core.db.execute("DELETE FROM transcript_fts WHERE recording_id=?", (rid,))
        core.db.execute("DELETE FROM recordings WHERE id=?", (rid,))
        return RedirectResponse("/recordings", status_code=303)

    # ---- review queue & clips -------------------------------------------
    @app.get("/review", response_class=HTMLResponse)
    def review(request: Request, status: str = "ready"):
        allowed = {"ready", "approved", "rejected", "failed", "pending_render", "rendering", "all"}
        status = status if status in allowed else "ready"
        where = "" if status == "all" else "WHERE c.status = ?"
        params = () if status == "all" else (status,)
        clips = core.db.query(f"SELECT c.*, r.title, r.source_url, r.original_filename FROM clips c "
                              f"JOIN recordings r ON r.id = c.recording_id {where} ORDER BY c.id DESC LIMIT 200",
                              params)
        conn = core.publisher.connections()
        pubs = {c["id"]: core.publisher.summary(c, conn) for c in clips if c["status"] == "approved"}
        return page(request, "review.html", clips=clips, status=status, pubs=pubs)

    def _clip(cid: int):
        c = core.db.clip(cid)
        if not c:
            raise HTTPException(404, "Clip not found")
        return c

    @app.get("/clips/{cid}", response_class=HTMLResponse)
    def clip_detail(request: Request, cid: int, err: str = "", msg: str = ""):
        c = _clip(cid)
        rec = core.db.recording(c["recording_id"])
        attempts = core.db.query("SELECT * FROM publish_attempts WHERE clip_id=? ORDER BY id DESC", (cid,))
        words = [w for s in core.db.segments(rec["id"]) for w in s["words"]
                 if w["start"] >= c["start"] - 0.05 and w["end"] <= c["end"] + 0.05]
        return page(request, "clip.html", c=c, rec=rec, words=words, attempts=attempts, err=err, msg=msg,
                    gate=editorial_gate(c), tiktok=core.tiktok.status(), instagram=core.instagram.status(),
                    pub=core.publisher.summary(c))

    @app.get("/partials/clip/{cid}/status", response_class=HTMLResponse)
    def clip_status_partial(request: Request, cid: int):
        return templates.TemplateResponse(request, "_clip_status.html", {"c": _clip(cid)})

    def _back(request: Request, cid: int, **q):
        ref = request.headers.get("referer") or f"/clips/{cid}"
        if q:
            from urllib.parse import urlencode
            ref = f"/clips/{cid}?" + urlencode(q)
        return RedirectResponse(ref, status_code=303)

    @app.post("/clips/{cid}/approve")
    def approve(request: Request, cid: int, note: str = Form("")):
        c = _clip(cid)
        if c["status"] not in ("ready", "rejected", "approved"):
            return _back(request, cid, err="Only rendered clips can be approved.")
        core.db.update("clips", cid, status="approved", reviewed_at=time.time(), review_note=note or c["review_note"])
        return _back(request, cid)

    @app.post("/clips/{cid}/reject")
    def reject(request: Request, cid: int, note: str = Form("")):
        c = _clip(cid)
        core.db.update("clips", cid, status="rejected", reviewed_at=time.time(), review_note=note or c["review_note"])
        return _back(request, cid)

    @app.post("/clips/{cid}/regenerate")
    def regenerate(request: Request, cid: int, start: str = Form(""), end: str = Form("")):
        def parse(v: str):
            v = v.strip()
            if not v:
                return None
            if ":" in v:
                total = 0.0
                for part in v.split(":"):
                    total = total * 60 + float(part)
                return total
            return float(v)
        try:
            core.pipeline.adjust_and_regenerate(cid, parse(start), parse(end))
        except ValueError as e:
            return _back(request, cid, err=str(e))
        return _back(request, cid, msg="Regeneration queued (transcript reused, no re-transcription).")

    @app.post("/clips/{cid}/rights")
    def set_rights(request: Request, cid: int, rights_status: str = Form(...)):
        _clip(cid)
        if rights_status not in dict(RIGHTS_CHOICES):
            raise HTTPException(400, "invalid rights status")
        core.db.update("clips", cid, rights_status=rights_status)
        return _back(request, cid)

    @app.post("/clips/{cid}/acknowledge")
    def acknowledge(request: Request, cid: int, note: str = Form(...)):
        c = _clip(cid)
        if len(note.strip()) < 15:
            return _back(request, cid, err="Write an editorial note (at least 15 characters) explaining the "
                                           "reporting purpose and context.")
        core.db.update("clips", cid, safety_status="acknowledged",
                       review_note=f"[Editorial decision on flag] {note.strip()}\n{c['review_note'] or ''}".strip())
        return _back(request, cid)

    @app.post("/clips/{cid}/description")
    def set_description(request: Request, cid: int, description: str = Form(...)):
        _clip(cid)
        core.db.update("clips", cid, description=description.strip()[:5000])
        return _back(request, cid, msg="Description saved.")

    def _clip_file(cid: int, field: str) -> Path:
        c = _clip(cid)
        p = c[field]
        if not p or not Path(p).is_file():
            raise HTTPException(404, "File not available (not rendered yet, or removed by retention policy)")
        return Path(p)

    def _dl_name(cid: int, ext: str) -> str:
        c = _clip(cid)
        rec = core.db.recording(c["recording_id"])
        return f"{ingest.safe_filename(rec['title'], 40)}_clip{cid}_{int(c['start'])}s.{ext}"

    @app.get("/clips/{cid}/video")
    def clip_video(cid: int):
        return FileResponse(_clip_file(cid, "video_path"), media_type="video/mp4")

    @app.get("/clips/{cid}/thumb")
    def clip_thumb(cid: int):
        return FileResponse(_clip_file(cid, "thumb_path"), media_type="image/jpeg")

    @app.get("/clips/{cid}/download")
    def clip_download(cid: int):
        return FileResponse(_clip_file(cid, "video_path"), media_type="video/mp4", filename=_dl_name(cid, "mp4"))

    @app.get("/clips/{cid}/captions.srt")
    def clip_srt(cid: int):
        return FileResponse(_clip_file(cid, "srt_path"), media_type="application/x-subrip",
                            filename=_dl_name(cid, "srt"))

    @app.get("/clips/{cid}/description.txt")
    def clip_desc(cid: int):
        c = _clip(cid)
        return PlainTextResponse(c["description"] or "", headers={
            "Content-Disposition": f'attachment; filename="{_dl_name(cid, "txt")}"'})

    # ---- jobs ------------------------------------------------------------
    @app.get("/jobs", response_class=HTMLResponse)
    def jobs_page(request: Request):
        return page(request, "jobs.html")

    @app.get("/jobs/{jid}", response_class=HTMLResponse)
    def job_detail(request: Request, jid: int):
        job = core.db.one("SELECT * FROM jobs WHERE id = ?", (jid,))
        if not job:
            raise HTTPException(404)
        logs = core.db.query("SELECT * FROM job_logs WHERE job_id = ? ORDER BY id", (jid,))
        return page(request, "job.html", job=job, logs=logs)

    @app.post("/jobs/{jid}/retry")
    def job_retry(request: Request, jid: int):
        job = core.db.one("SELECT * FROM jobs WHERE id = ?", (jid,))
        if job and core.queue.retry(jid) and job["clip_id"]:
            core.db.update("clips", job["clip_id"], status="pending_render", error=None)
        if job and job["recording_id"] and job["kind"] == "process_recording":
            core.db.update("recordings", job["recording_id"], status="queued", error=None)
        return RedirectResponse(request.headers.get("referer") or "/jobs", status_code=303)

    @app.post("/jobs/{jid}/cancel")
    def job_cancel(request: Request, jid: int):
        core.queue.cancel(jid)
        return RedirectResponse(request.headers.get("referer") or "/jobs", status_code=303)

    # ---- JSON status API (local automation / acceptance testing) ----------
    @app.get("/api/status")
    def api_status():
        return {"counts": core.db.counts(),
                "worker_alive": bool(core.queue._thread and core.queue._thread.is_alive())}

    @app.get("/api/recordings/{rid}")
    def api_recording(rid: int):
        rec = _rec(rid)
        clips = core.db.query("SELECT id, start, end, status, safety_status, rights_status, confidence, "
                              "selection_method, video_path IS NOT NULL AS rendered, transcript_text "
                              "FROM clips WHERE recording_id=? ORDER BY start", (rid,))
        jobs = core.db.query("SELECT id, kind, status, error FROM jobs WHERE recording_id=?", (rid,))
        segs = core.db.segments(rid)
        return {"recording": {k: rec[k] for k in ("id", "title", "status", "duration", "language",
                                                  "whisper_model", "error", "sha256", "source_type")},
                "transcript_segments": len(segs), "transcript_text": " ".join(s["text"] for s in segs),
                "clips": [dict(c) for c in clips], "jobs": [dict(j) for j in jobs]}

    # ---- search ----------------------------------------------------------
    @app.get("/search", response_class=HTMLResponse)
    def search(request: Request, q: str = ""):
        return page(request, "search.html", q=q, results=core.db.search_transcripts(q) if q.strip() else [])

    # ---- settings & publishing ------------------------------------------
    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request, msg: str = "", err: str = ""):
        hw = hardware.detect()
        model = hardware.choose_whisper(hw, settings.whisper_model, settings.whisper_device,
                                        settings.whisper_compute_type, settings.language)
        llm = "disabled" if settings.llm_provider == "none" else (
            f"Ollama reachable ({settings.ollama_model})" if segment.ollama_available(settings.ollama_url)
            else "not detected — deterministic selection in use")
        perms = core.db.query("SELECT * FROM source_permissions ORDER BY domain")
        return page(request, "settings.html", hw=hw, model=model, llm=llm, s=settings,
                    storage=maintenance.storage_report(settings), tiktok=core.tiktok.status(),
                    instagram=core.instagram.status(), token_backend=core.tokens.backend, msg=msg, err=err,
                    checklist=setup_status.checklist(core), perms=perms,
                    restricted=sorted(ingest.RESTRICTED_PLATFORMS), threads=__import__(
                        "clipengine.transcribe", fromlist=["auto_cpu_threads"]).auto_cpu_threads())

    def _settings_redirect(**q):
        return RedirectResponse("/settings?" + urlencode(q), status_code=303)

    @app.post("/settings/download-model")
    def download_model():
        core.queue.enqueue("download_model", dedupe_key="download_model", max_attempts=4)
        return _settings_redirect(msg="Model download queued; progress is shown on the Jobs page.")

    @app.post("/settings/permissions")
    def add_permission(domain: str = Form(...), granted_by: str = Form(...), granted_on: str = Form(...),
                       reference: str = Form(...), scope: str = Form(""), confirm: str = Form("")):
        domain = domain.strip().lower().removeprefix("https://").removeprefix("http://").split("/")[0]
        if confirm != "yes" or not (domain and granted_by.strip() and reference.strip()):
            return _settings_redirect(err="Fill in every field and confirm you hold this written permission.")
        core.db.execute("INSERT OR REPLACE INTO source_permissions(domain, granted_by, granted_on, reference, scope, "
                        "created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                        (domain, granted_by.strip(), granted_on.strip(), reference.strip(), scope.strip(),
                         time.time(), time.time()))
        return _settings_redirect(msg=f"Written permission recorded for {domain}. It is cited in each import's provenance.")

    @app.post("/settings/permissions/{pid}/delete")
    def delete_permission(pid: int):
        core.db.execute("DELETE FROM source_permissions WHERE id = ?", (pid,))
        return _settings_redirect(msg="Permission removed.")

    @app.post("/sources/{pid}/cancel")
    def cancel_pending(pid: int):
        core.db.update("pending_sources", pid, status="cancelled")
        return RedirectResponse("/import?" + urlencode({"msg": f"Pending source #{pid} removed."}), status_code=303)

    @app.get("/public/media/{token}.mp4")
    def public_media(token: str):
        row = core.db.one("SELECT * FROM public_media_tokens WHERE token = ? AND expires_at > ?", (token, time.time()))
        clip = core.db.clip(row["clip_id"]) if row else None
        if not clip or not clip["video_path"] or not Path(clip["video_path"]).is_file():
            raise HTTPException(404)
        return FileResponse(clip["video_path"], media_type="video/mp4")

    @app.get("/connect/{platform}")
    def connect(platform: str):
        client = {"tiktok": core.tiktok, "instagram": core.instagram}.get(platform)
        if not client:
            raise HTTPException(404)
        try:
            url, state = client.authorize_url()
        except NotConfigured as e:
            return _settings_redirect(err=str(e))
        core.oauth_state[state["state"]] = {"platform": platform, "ts": time.time(), **state}
        return RedirectResponse(url, status_code=303)

    @app.post("/disconnect/{platform}")
    def disconnect(platform: str):
        if platform in ("tiktok", "instagram"):
            core.tokens.delete(platform)
        return RedirectResponse("/settings?msg=Disconnected", status_code=303)

    @app.get("/oauth/{platform}/callback")
    def oauth_callback(platform: str, code: str = "", state: str = "", error: str = "",
                       error_description: str = ""):
        st = core.oauth_state.pop(state, None)
        if error:
            msg = f"Authorisation declined or refused by {platform}: {error_description or error}"
            core.db.set_kv(f"oauth_error.{platform}", msg)
            return _settings_redirect(err=msg)
        if not st or st["platform"] != platform or time.time() - st["ts"] > 900:
            return RedirectResponse("/settings?err=Invalid or expired OAuth state; try connecting again.",
                                    status_code=303)
        try:
            if platform == "tiktok":
                core.tiktok.exchange_code(code, st.get("verifier"))
            else:
                core.instagram.exchange_code(code)
        except (PublishError, NotConfigured, PermanentError) as e:
            core.db.set_kv(f"oauth_error.{platform}", str(e))
            return _settings_redirect(err=str(e))
        except Exception as e:  # noqa: BLE001 - network problems etc. are shown, not crashed on
            core.db.set_kv(f"oauth_error.{platform}", f"{type(e).__name__}: {e}")
            return _settings_redirect(err=f"Could not complete sign-in: {e}")
        core.db.execute("DELETE FROM meta WHERE key = ?", (f"oauth_error.{platform}",))
        return _settings_redirect(msg=f"{platform} connected")

    @app.post("/clips/{cid}/publish/{platform}")
    def publish(request: Request, cid: int, platform: str, confirm: str = Form(""), again: str = Form("")):
        _clip(cid)
        if platform == "instagram" and confirm != "PUBLISH":
            return _back(request, cid, err="Instagram's API publishes publicly (no drafts). Type PUBLISH to confirm.")
        try:
            core.publisher.request(cid, platform, again=again == "yes")
        except (PublishError, NotConfigured) as e:
            return _back(request, cid, err=f"{platform}: {e}")
        return _back(request, cid, msg=f"{platform}: upload queued. Progress appears below; it continues in the "
                                       "background and resumes after a restart.")

    return app
