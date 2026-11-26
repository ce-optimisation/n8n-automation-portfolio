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
from html import escape as html_escape
from urllib.parse import urlencode, urlparse

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import (autostart, captions, cloud, editorial, hardware, health, inbox, ingest, lifecycle, cleanup, maintenance,
               discovery, events, monitor, titles, roster_import, notify, platforms, portfolio, power, render, segment, setup_status, storage, tailscale)
from ..publishing import packages
from ..config import Settings, apply_settings
from ..db import Database
from ..jobs import JobQueue, PermanentError
from ..pipeline import Pipeline
from ..publishing import (STATES, InstagramClient, NotConfigured, PublishError, TikTokClient, TokenStore,
                          editorial_gate)
from ..publishing.service import PublishService
from ..publishing.facebook import FacebookClient
from ..publishing.youtube import YouTubeClient
from .. import connections as cx
from .. import phone, recording_switch
from ..publishing import autodeliver, delivery

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
        self.tokens = TokenStore.for_settings(settings)
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
        self.facebook = FacebookClient(settings.meta_app_id, settings.meta_app_secret, settings.meta_redirect_uri,
                                       self.tokens, settings.meta_graph_version)
        self.youtube = YouTubeClient(settings.youtube_oauth_client_id or settings.google_client_id,
                                     settings.youtube_oauth_client_secret or settings.google_client_secret, self.tokens)
        self.queue.register("download_model", self._download_model)
        from ..publishing.instagram_reels import InstagramReelsPublisher
        self.instagram_reels = InstagramReelsPublisher(
            lambda brand: (self.tokens.get(f"instagram:{brand}") or {}).get("access_token"),
            version=settings.meta_graph_version, public_enabled=bool(settings.instagram_public_publish))
        self.queue.register(autodeliver.KIND, lambda ctx: autodeliver.handle(
            settings, self.db, {"youtube": self.youtube, "facebook": self.facebook,
                                "instagram": self.instagram_reels}, ctx))
        self.phone_stop = __import__("threading").Event()
        self.maintenance = maintenance.Maintenance(settings, self.db, queue=self.queue)
        self.inbox = inbox.InboxWatcher(settings, self.db, self.pipeline)
        self.monitor = monitor.SourceMonitor(settings, self.db, self.queue)
        self.discovery = discovery.DiscoverySupervisor(settings, self.db, self.queue)
        # One heavy workload at a time: transcription/rendering wait while a live capture runs.
        self.queue.recording_active = self.monitor.recording_active
        self.queue.defer_heavy_while_recording = settings.defer_heavy_while_recording
        editorial.seed_presets(self.db)
        if settings.workspace_id:
            for key in (recording_switch.KEY, recording_switch.DELIVERY_KEY):
                self.db.execute("INSERT OR IGNORE INTO meta(key,value) VALUES (?, '0')", (key,))
        else:
            try:
                portfolio.seed(self.db, settings)
                events.seed_events(self.db)
                roster_import.reconcile(settings, self.db)
            except Exception:  # noqa: BLE001 - the portfolio must never stop the engine
                log.exception("portfolio could not be seeded")
        research_file = Path(__file__).resolve().parents[2] / "research" / "research_dataset.json"
        if not settings.workspace_id and research_file.is_file() and self.db.get_kv("research_loaded") != str(research_file.stat().st_mtime):
            try:
                from .. import research
                research.load_dataset(self.db, research_file)
                self.db.set_kv("research_loaded", str(research_file.stat().st_mtime))
            except Exception:  # noqa: BLE001 - research data must never stop the engine
                log.exception("research dataset could not be loaded")
        self.cloud = cloud.CloudService(settings, self.db, self.queue, self.tokens)
        self.lifecycle = lifecycle.Lifecycle(settings, self.db, self.queue, self.cloud)
        self.packages = packages.PackageService(settings, self.db, self.queue, self.tokens)
        self.pipeline.on_recording_started.append(self.lifecycle.claim_ownership)
        self.pipeline.on_clip_rendered.append(self.cloud.enqueue_clip)
        self.pipeline.on_recording_done.append(self.lifecycle.check_recording)
        self.maintenance.extra.append(self.lifecycle.run)
        self.cleanup = cleanup.LocalCleanup(settings, self.db, self.queue, self.cloud)
        self.maintenance.extra.append(self.cleanup.run)
        from .. import account_analytics
        self.maintenance.extra.append(lambda: account_analytics.collect(
            self.db, self.youtube, self.tokens, self.settings.meta_graph_version))
        self.notifier = notify.Notifier(settings, self.db, self.queue, self.tokens)
        self.queue.failure_listeners.append(
            lambda job: notify.on_job_failed(self.settings, self.db, self.queue, job))
        self.device_flow: dict = {}
        self.keepawake = power.KeepAwake(settings, self.db)
        self.heartbeat = health.Heartbeat(self.db)
        self.phone_report: dict = {}

        self.login_failures: dict[str, list[float]] = {}
        key_file = settings.secrets_dir / "session_key"
        if not key_file.exists():
            key_file.write_text(secrets.token_hex(32))
        self.session_key = key_file.read_text().strip().encode()

    # OAuth "state" values are kept in the database (15-minute lifetime) so a sign-in that was
    # started before an engine restart can still be completed afterwards.
    def oauth_put(self, state: str, data: dict) -> None:
        now = time.time()
        allst = {k: v for k, v in json.loads(self.db.get_kv("oauth_state") or "{}").items() if now - v.get("ts", 0) < 900}
        allst[state] = {**data, "ts": now}
        self.db.set_kv("oauth_state", json.dumps(allst))

    def oauth_pop(self, state: str) -> dict | None:
        allst = json.loads(self.db.get_kv("oauth_state") or "{}")
        st = allst.pop(state, None)
        self.db.set_kv("oauth_state", json.dumps(allst))
        return st

    def check_caption_font(self) -> dict:
        """Render a frame with libass and record which font file was actually used (Settings checklist)."""
        try:
            res = captions.verify_font(self.settings.work_dir / "fontcheck", captions.load_style(self.db))
        except Exception as e:  # noqa: BLE001
            res = {"ok": False, "error": str(e)}
        res["checked_at"] = time.time()
        self.db.set_kv("caption_font_check", json.dumps(res))
        return res

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
            core.monitor.start()
            core.discovery.start()
            core.keepawake.start()
            core.heartbeat.start()
            phone.start_ticker(settings, core.db, core.phone_stop)
            __import__("threading").Thread(target=core.check_caption_font, daemon=True).start()
        try:
            yield
        finally:
            core.phone_stop.set()
            core.queue.stop()
            core.maintenance.stop()
            core.inbox.stop()
            core.monitor.stop()
            core.discovery.stop()
            core.keepawake.stop()
            if start_worker:
                core.heartbeat.stop()

    app = FastAPI(title="Local Clipping Engine", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.core = core
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.filters["mmss"] = render.mmss
    templates.env.filters["fromjson"] = lambda s: json.loads(s) if s else []
    templates.env.filters["dt"] = lambda t: time.strftime("%Y-%m-%d %H:%M", time.localtime(t)) if t else ""
    templates.env.globals["RIGHTS_CHOICES"] = RIGHTS_CHOICES
    templates.env.globals["PUB_STATES"] = STATES
    templates.env.globals["recording_enabled"] = lambda: recording_switch.is_enabled(core.db)
    templates.env.globals["delivery_enabled"] = lambda: recording_switch.delivery_enabled(core.db)

    def _same_site_back(request: Request) -> RedirectResponse:
        from urllib.parse import urlparse as _up
        path = _up(request.headers.get("referer") or "/").path or "/"     # same-site path only; never redirect off-site
        if not path.startswith("/") or path.startswith("//") or "\\" in path:
            path = "/"                                  # "//host" would be a protocol-relative redirect off-site
        return RedirectResponse(path, status_code=303)

    @app.post("/recording/toggle")
    def recording_toggle(request: Request, enable: str = Form("")):
        """Master recording switch for ALL platforms and creators (see clipengine/recording_switch.py)."""
        recording_switch.set_enabled(core.db, enable == "1")
        return _same_site_back(request)

    @app.post("/delivery/toggle")
    def delivery_toggle(request: Request, enable: str = Form("")):
        """Master AUTO-UPLOAD switch: OFF = approved clips wait and nothing is sent to any platform."""
        recording_switch.set_delivery_enabled(core.db, enable == "1")
        return _same_site_back(request)

    # ---- phone: one-tap review queue and alert settings (Session 13) --------------------------------------------
    def _mobile_redirect(skip: str, msg: str = "") -> RedirectResponse:
        from urllib.parse import urlencode as _ue
        q = {k: v for k, v in (("skip", skip), ("msg", msg)) if v}
        return RedirectResponse("/m" + ("?" + _ue(q) if q else ""), status_code=303)

    def _has_clean(c) -> bool:
        from ..render import has_clean_master
        try:
            return has_clean_master(core.pipeline.clip_dir(c))
        except Exception:      # noqa: BLE001 - a display helper must never break the review page
            return False

    @app.get("/m", response_class=HTMLResponse)
    def mobile_review(request: Request, skip: str = "", msg: str = "", *, rejected_id: int | None = None):
        skipped = {int(x) for x in skip.split(",") if x.strip().isdigit()}
        rest = [c for c in phone.phone_queue(core.db) if c["id"] not in skipped]
        c = rest[0] if rest else None
        ctx = {"c": c, "left": len(rest), "skip": skip, "msg": msg[:200], "dests": [], "manual": [], "creator": "", "brand": "",
               "counts": phone.queue_counts(core.db), "rejected_id": rejected_id,
               "rejection_reasons": rejection_reasons}
        if c:
            if not c["review_opened_at"]:
                core.db.update("clips", c["id"], review_opened_at=time.time())
            rec = core.db.recording(c["recording_id"])
            cr = core.db.one("SELECT name FROM creators WHERE id=?", (rec["creator_id"],)) if rec and rec["creator_id"] else None
            b = core.db.one("SELECT name FROM brands WHERE key=?", (delivery.clip_brand(core.db, c),))
            dests_ = autodeliver.destinations_for(core.db, c)
            ctx.update(dests=dests_, modes={a["id"]: delivery.mode_for(core.db, a["id"]) for a in dests_},
                       manual=autodeliver.manual_platforms(core.db, c),
                       creator=cr["name"] if cr else "", brand=b["name"] if b else "", rec=rec,
                       duration=round((c["end"] or 0) - (c["start"] or 0)),
                       can_title=_has_clean(c))
            ctx["any_public"] = any(ctx["modes"].get(a["id"]) == "public" for a in dests_)
            try:
                alts = json.loads(c["title_json"] or "{}").get("alternatives", []) if c["title_json"] else []
            except ValueError:
                alts = []
            ctx["alts"] = [a for a in alts if a and a != c["title"]][:3]
        try:
            from .. import livestore
            lvl, txt = livestore.disk_level(settings)
            ctx["disk"] = txt if lvl != "ok" else ""
        except Exception:      # noqa: BLE001 - a banner must never break the review page
            ctx["disk"] = ""
        return templates.TemplateResponse(request, "mobile.html", ctx)

    @app.post("/m/{cid}/description")
    def mobile_description(cid: int, text: str = Form(""), skip: str = Form("")):
        """Edit the description that will be sent with the clip (only while it is still waiting for review)."""
        c = _clip(cid)
        if c["status"] != "ready":
            return _mobile_redirect(skip, "That clip was already decided.")
        core.db.update("clips", cid, description=text.strip()[:2000])
        return _mobile_redirect(skip, "Description saved.")

    @app.get("/m/results", response_class=HTMLResponse)
    def mobile_results(request: Request):
        """What happened to recently approved clips, per destination (published, private, failed, waiting)."""
        rows = []
        for c in core.db.query("SELECT * FROM clips WHERE status IN ('approved','published') ORDER BY reviewed_at DESC LIMIT 15"):
            atts = core.db.query("SELECT platform, status, detail FROM publish_attempts WHERE clip_id=? ORDER BY id", (c["id"],))
            jobs = core.db.query("SELECT status, message FROM jobs WHERE kind='deliver_clip' AND clip_id=?", (c["id"],))
            rows.append({"c": c, "attempts": atts, "jobs": jobs})
        return templates.TemplateResponse(request, "mobile_results.html", {"rows": rows})

    @app.post("/m/{cid}/approve")
    def mobile_approve(cid: int, skip: str = Form("")):
        c = _clip(cid)
        if not record_approval(c):
            return _mobile_redirect(skip, "That clip was already decided.")
        res = autodeliver.prepare_and_enqueue(core.db, core.queue, cid)
        if res["queued"]:
            txt = "Approved. Sending to " + ", ".join(
                f"{q['brand']} {q['platform']} ({delivery.mode_for(core.db, q['account_id'])})" for q in res["queued"])
        else:
            txt = "Approved (not uploaded: " + (res["reason"] or "no destination") + ")"
        return _mobile_redirect(skip, txt)

    def record_approval(c, note: str = "") -> bool:
        with core.db.tx() as conn:
            current = conn.execute("SELECT * FROM clips WHERE id=?", (c["id"],)).fetchone()
            if (current["status"] != c["status"] or current["status"] not in ("ready", "approved")
                    or not current["video_path"] or not Path(current["video_path"]).is_file()):
                return False
            conn.execute("UPDATE clips SET status='approved', reviewed_at=?, review_note=?, review_seconds=? WHERE id=?",
                         (time.time(), note or current["review_note"], _review_seconds(current), current["id"]))
            return True

    def reject_reviewable(cid: int, note: str = ""):
        c = _clip(cid)
        with core.db.tx() as conn:
            changed = conn.execute(
                "UPDATE clips SET status='rejected', reviewed_at=?, review_note=?, review_seconds=? "
                "WHERE id=? AND status IN ('ready','failed')",
                (time.time(), note or c["review_note"], _review_seconds(c), cid),
            ).rowcount
        if not changed and _clip(cid)["status"] != "rejected":
            return False, "Not rejected. This clip is no longer awaiting review. Its files were kept."
        result = core.lifecycle.discard_rejected(cid)
        if result.get("deleted") and result.get("bytes"):
            return True, f"Rejected and deleted ({result['bytes']:,} bytes freed)."
        if result.get("deleted") or result.get("reason") == "already deleted":
            return True, "Rejected. No local video files remained to remove."
        if result.get("bytes"):
            return True, f"Rejected. {result['bytes']:,} bytes freed. Remaining files kept ({result['reason']})."
        return True, f"Rejected. Local files kept ({result['reason']})."

    @app.post("/m/{cid}/reject")
    def mobile_reject(request: Request, cid: int, skip: str = Form("")):
        decided, message = reject_reviewable(cid)
        if request.headers.get("HX-Request") == "true":
            return mobile_review(request, skip, message, rejected_id=cid if decided else None)
        return _mobile_redirect(skip, message)

    rejection_reasons = {
        "weak_opening": "Weak opening",
        "missing_context": "Missing context",
        "duplicate_topic": "Duplicate topic",
        "overlapping_captions": "Overlapping captions",
        "other": "Other",
    }

    @app.post("/clips/{cid}/rejection-feedback", response_class=HTMLResponse)
    def rejection_feedback(cid: int, reason: str = Form(...)):
        if reason not in rejection_reasons:
            raise HTTPException(422, "Choose a rejection reason.")
        with core.db.tx() as conn:
            row = conn.execute("SELECT status FROM clips WHERE id=?", (cid,)).fetchone()
            if not row or row["status"] != "rejected":
                raise HTTPException(409, "Feedback is only available for rejected clips.")
            conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
                         (f"rejection.reason.{cid}", reason))
        return HTMLResponse('<p role="status">Feedback saved.</p>')

    @app.get("/rejection-feedback", response_class=HTMLResponse)
    def rejection_report(request: Request):
        from collections import Counter
        counts = Counter()
        for c in core.db.query("SELECT c.*, m.value AS rejection_reason FROM clips c LEFT JOIN meta m "
                               "ON m.key='rejection.reason.' || c.id WHERE c.status='rejected'"):
            brand = delivery.clip_brand(core.db, c) or "Unassigned"
            reason = rejection_reasons.get(c["rejection_reason"], "No reason provided")
            counts[brand, reason] += 1
        return templates.TemplateResponse(request, "rejection_feedback.html", {"counts": sorted(counts.items())})

    @app.post("/m/{cid}/title/remove")
    def mobile_title_remove(cid: int, skip: str = Form("")):
        """Take the on-screen title off without re-rendering the clip (instant; locks the removal)."""
        c = _clip(cid)
        if c["status"] != "ready":
            return _mobile_redirect(skip, "That clip was already decided.")
        try:
            core.pipeline.remove_clip_title(cid)
        except (FileNotFoundError, PermanentError) as e:
            return _mobile_redirect(skip, f"Could not remove the title: {e}")
        return _mobile_redirect(skip, "Title removed. The video is back to no title.")

    @app.post("/m/{cid}/title")
    def mobile_title_set(cid: int, text: str = Form(""), skip: str = Form("")):
        """Write your own title; it is drawn onto the clean video in the background (the clip is not re-rendered)."""
        c = _clip(cid)
        if c["status"] != "ready":
            return _mobile_redirect(skip, "That clip was already decided.")
        try:
            r = core.pipeline.request_title(cid, text)
        except (FileNotFoundError, PermanentError) as e:
            return _mobile_redirect(skip, f"Could not change the title: {e}")
        return _mobile_redirect(skip, "Title saved. It will be on the video in about a minute; refresh to see it."
                                if r.get("job") else "Title removed.")

    @app.get("/phone", response_class=HTMLResponse)
    def phone_page(request: Request, msg: str = "", err: str = ""):
        dests = core.db.query("SELECT * FROM social_accounts WHERE platform IN ('facebook','youtube') ORDER BY brand, platform")
        return page(request, "phone.html", msg=msg[:200], err=err[:200], configured=phone.configured(settings),
                    topic=settings.ntfy_topic, server=settings.ntfy_server, base=settings.dashboard_url,
                    waiting=len(phone.phone_queue(core.db)), cap=settings.delivery_daily_cap,
                    quiet=settings.push_quiet_hours, gap=settings.push_min_gap_minutes,
                    dests=[dict(a, today=autodeliver.sent_today(core.db, a), mode=delivery.mode_for(core.db, a["id"]))
                           for a in dests])

    @app.post("/phone/test")
    def phone_test():
        from urllib.parse import urlencode as _ue
        res = phone.send(settings, "Clipping Engine", "Test alert: your phone is connected.",
                         f"{settings.dashboard_url}/m" if settings.dashboard_url else None)
        return RedirectResponse("/phone?" + _ue({"msg" if res["ok"] else "err":
                                                 "Test alert sent." if res["ok"] else f"Not sent: {res['detail']}"}),
                                status_code=303)

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
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin") or request.headers.get("referer")
            if origin and urlparse(origin).netloc != request.headers.get("host"):
                return PlainTextResponse("Cross-origin request refused", status_code=403)
            # Browsers send Sec-Fetch-Site even when a page suppresses Origin/Referer.
            if request.headers.get("sec-fetch-site") in ("cross-site", "same-site") and \
                    not (path.startswith("/oauth/") or path == "/google/oauth/callback"):
                return PlainTextResponse("Cross-origin request refused", status_code=403)
        if settings.dashboard_password and not (path.startswith("/static") or path in ("/login", "/healthz")
                                                or path.startswith("/oauth/") or path == "/google/oauth/callback"):
            # OAuth callbacks are reached by a cross-site redirect, so the SameSite=Strict login cookie is not
            # sent. They are protected instead by a one-time state value plus a PKCE verifier held in memory.
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
        return page(request, "dashboard.html", recordings=recs, storage=maintenance.storage_report(settings),
                    health=health.items(core), s=settings)

    @app.post("/health/dismiss")
    def health_dismiss():
        health.dismiss_downtime(core.db)
        return RedirectResponse("/", status_code=303)

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
                            language: str = Form(""), rights: str = Form(""), pending_id: str = Form(""),
                            source_url: str = Form(""), recorded_at: str = Form("")):
        name = ingest.safe_filename(file.filename or "upload.mp4")
        if Path(name).suffix.lower() not in ingest.VIDEO_EXTS:
            return _import_result(request, err=f"Unsupported file type: {file.filename}")
        source_url, recorded_at = source_url.strip(), recorded_at.strip()
        if source_url and urlparse(source_url).scheme not in ("http", "https"):
            return _import_result(request, err="The original source address must start with http:// or https://")
        if recorded_at and not ingest.parse_recorded_at(recorded_at):
            return _import_result(request, err="Enter the original recording date as YYYY-MM-DD (not in the future).")
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
                                                         attestation=rights or None, recorded_at=recorded_at or None)
                if not dup:
                    core.queue.enqueue("process_recording", recording_id=rid, dedupe_key=f"process:{rid}",
                                       payload={"settings": {}})
            else:
                rid, dup, _ = core.pipeline.import_upload(tmp, file.filename or name, title or None,
                                                          _overrides(clips, min_s, max_s, language), rights or None,
                                                          source_url=source_url or None,
                                                          recorded_at=recorded_at or None)
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
    def recording_detail(request: Request, rid: int, msg: str = "", err: str = ""):
        rec = _rec(rid)
        clips = core.db.query("SELECT * FROM clips WHERE recording_id = ? ORDER BY start", (rid,))
        segs = core.db.segments(rid)
        jobs = core.db.query("SELECT * FROM jobs WHERE recording_id = ? ORDER BY id DESC LIMIT 20", (rid,))
        conn = core.publisher.connections()
        pubs = {c["id"]: core.publisher.summary(c, conn) for c in clips if c["status"] == "approved"}
        return page(request, "recording.html", rec=rec, clips=clips, segments=segs, jobs=jobs, pubs=pubs,
                    meta=json.loads(rec["metadata_json"] or "{}"), msg=msg, err=err)

    @app.post("/recordings/{rid}/provenance")
    def recording_provenance(rid: int, source_url: str = Form(""), recorded_at: str = Form("")):
        _rec(rid)
        try:
            ingest.set_provenance(core.db, rid, source_url=source_url, recorded_at=recorded_at)
        except PermanentError as e:
            return RedirectResponse(f"/recordings/{rid}?" + urlencode({"err": str(e)}), status_code=303)
        return RedirectResponse(f"/recordings/{rid}?" + urlencode({"msg": "Source details saved."}), status_code=303)

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
    def review(request: Request, status: str = "ready", creator: str = "", niche: str = "", since: str = "",
               platform: str = ""):
        allowed = {"ready", "approved", "rejected", "failed", "pending_render", "rendering", "all", "published"}
        status = status if status in allowed else "ready"
        conds, params = [], []
        if status == "published":
            conds.append("c.published_at IS NOT NULL")
        elif status != "all":
            conds.append("c.status = ?")
            params.append(status)
        if creator.isdigit():
            conds.append("r.creator_id = ?")
            params.append(int(creator))
        if niche:
            conds.append("COALESCE(c.niche_key, r.niche_key) = ?")
            params.append(niche)
        if since:
            try:
                conds.append("c.created_at >= ?")
                params.append(time.mktime(time.strptime(since, "%Y-%m-%d")))
            except ValueError:
                conds.pop()
        if platform:
            conds.append("EXISTS (SELECT 1 FROM publication_packages pp WHERE pp.clip_id = c.id AND pp.platform = ?)")
            params.append(platform)
        where = ("WHERE " + " AND ".join(conds)) if conds else ""
        clips = core.db.query(f"SELECT c.*, r.title, r.source_url, r.original_filename FROM clips c "
                              f"JOIN recordings r ON r.id = c.recording_id {where} ORDER BY c.id DESC LIMIT 200",
                              tuple(params))
        conn = core.publisher.connections()
        pubs = {c["id"]: core.publisher.summary(c, conn) for c in clips if c["status"] == "approved"}
        filters = {"creator": creator, "niche": niche, "since": since, "platform": platform}
        return page(request, "review.html", clips=clips, status=status, pubs=pubs, filters=filters,
                    creators=core.db.query("SELECT id, name FROM creators WHERE status NOT IN ('research') ORDER BY name"),
                    niches=core.db.query("SELECT key, name FROM niches ORDER BY name"),
                    platforms=list(packages.PLATFORMS))

    def _clip(cid: int):
        c = core.db.clip(cid)
        if not c:
            raise HTTPException(404, "Clip not found")
        return c

    @app.get("/clips/{cid}", response_class=HTMLResponse)
    def clip_detail(request: Request, cid: int, err: str = "", msg: str = ""):
        c = _clip(cid)
        if not c["review_opened_at"] and c["status"] == "ready":
            core.db.update("clips", cid, review_opened_at=time.time())   # owner review-time measurement
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
        if not record_approval(c, note):
            return _back(request, cid, err="The clip changed or the rendered file is unavailable. Refresh the review queue.")
        autodeliver.prepare_and_enqueue(core.db, core.queue, cid)       # owner's approval = the command; gates re-checked there
        return _back(request, cid)

    @app.post("/clips/{cid}/reject")
    def reject(request: Request, cid: int, note: str = Form("")):
        decided, message = reject_reviewable(cid, note)
        if request.headers.get("HX-Request") == "true":
            if not decided:
                return HTMLResponse(html_escape(message), headers={"HX-Reswap": "beforeend"})
            return templates.TemplateResponse(request, "_rejection_feedback.html", {
                "msg": message, "rejected_id": cid, "rejection_reasons": rejection_reasons})
        return _back(request, cid, msg=message)

    def _review_seconds(c):
        """Seconds between first opening the clip and the decision (capped at 30 min per clip)."""
        if not c["review_opened_at"]:
            return c["review_seconds"]
        return (c["review_seconds"] or 0) + min(1800.0, max(0.0, time.time() - c["review_opened_at"]))

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
    def clip_video(request: Request, cid: int):
        from .review_media import review_video
        return review_video(_clip_file(cid, "video_path"), request)

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

    # ---- ready to post -----------------------------------------------------
    from ..publishing import base as pubbase
    from ..publishing import instagram as ig_mod
    from ..publishing import tiktok as tt_mod
    EXPORT_LIMITS = {"tiktok": tt_mod.LIMITS, "instagram": ig_mod.LIMITS}

    def export_checks(c) -> dict:
        out = {}
        for plat, lim in EXPORT_LIMITS.items():
            try:
                pubbase.validate_video(Path(c["video_path"]), **lim)
                out[plat] = (True, "Meets the platform's documented limits")
            except Exception as e:  # noqa: BLE001
                out[plat] = (False, str(e))
        return out

    @app.get("/ready", response_class=HTMLResponse)
    def ready_to_post(request: Request):
        rows = core.db.query("SELECT c.*, r.title, r.source_url, r.original_filename, r.created_at AS rec_created "
                             "FROM clips c JOIN recordings r ON r.id = c.recording_id WHERE c.status='approved' "
                             "ORDER BY c.reviewed_at DESC")
        cleared, blocked = [], []
        for c in rows:
            (blocked if editorial_gate(c) else cleared).append(c)
        posts = {}
        for r in core.db.query("SELECT * FROM manual_posts"):
            posts.setdefault(r["clip_id"], {})[r["platform"]] = r
        conn = core.publisher.connections()
        return page(request, "ready.html", cleared=cleared, blocked=blocked, posts=posts,
                    checks={c["id"]: export_checks(c) for c in cleared}, gates={c["id"]: editorial_gate(c) for c in blocked},
                    pubs={c["id"]: core.publisher.summary(c, conn) for c in cleared})

    @app.post("/clips/{cid}/posted")
    def mark_posted(request: Request, cid: int, platform: str = Form(...), posted: str = Form("1"),
                    post_url: str = Form(""), note: str = Form("")):
        _clip(cid)
        platform = platform.strip().lower()[:30] or "other"
        if post_url and urlparse(post_url).scheme not in ("http", "https"):
            post_url = ""
        core.db.execute("INSERT INTO manual_posts(clip_id, platform, posted, post_url, note, created_at, updated_at) "
                        "VALUES (?,?,?,?,?,?,?) ON CONFLICT(clip_id, platform) DO UPDATE SET posted=excluded.posted, "
                        "post_url=excluded.post_url, note=excluded.note, updated_at=excluded.updated_at",
                        (cid, platform, 1 if posted == "1" else 0, post_url.strip()[:500], note.strip()[:500],
                         time.time(), time.time()))
        return RedirectResponse(request.headers.get("referer") or "/ready", status_code=303)

    # ---- sources -----------------------------------------------------------
    @app.get("/portfolio", response_class=HTMLResponse)
    def portfolio_page(request: Request, msg: str = "", brand: str = ""):
        rows = portfolio.summary(settings, core.db)
        counts = {r["monitor_source_id"]: r["n"] for r in core.db.query(
            "SELECT monitor_source_id, COUNT(*) n FROM discovered_items WHERE state<>'baseline' GROUP BY monitor_source_id")}
        for r in rows:
            r["items"] = counts.get(r["id"], 0)
        if brand:
            rows = [r for r in rows if r["brand_key"] == brand]
        brands = core.db.query("SELECT * FROM brands ORDER BY name")
        qcount = {r["brand_key"]: r["n"] for r in core.db.query(
            "SELECT brand_key, COUNT(*) n FROM jobs j JOIN discovered_items d ON d.job_id=j.id "
            "WHERE j.status IN ('queued','running') GROUP BY brand_key")}
        held = core.db.query("SELECT brand_key, state, COUNT(*) n FROM discovered_items WHERE state IN "
                             "('permission_required','discovered','failed') AND kind IN ('upload','vod','episode') "
                             "GROUP BY brand_key, state")
        new_items = core.db.query("SELECT d.*, c.name AS creator FROM discovered_items d JOIN creators c ON c.id=d.creator_id "
                                  "WHERE d.state<>'baseline' ORDER BY d.first_seen DESC LIMIT 30")
        evs = core.db.query("SELECT e.*, m.uid FROM monitor_events e LEFT JOIN monitor_sources m ON m.id=e.monitor_source_id "
                            "ORDER BY e.id DESC LIMIT 25")
        creds = {n: platforms.adapter_for(settings, n).configured() for n in ("youtube", "twitch", "kick")}
        return page(request, "portfolio.html", rows=rows, brands=brands, brand=brand, msg=msg, qcount=qcount, held=held,
                    new_items=new_items, evs=evs, state_help=portfolio.STATE_HELP, creds=creds,
                    active_events=events.active_events(core.db, __import__("time").time()),
                    quota=core.discovery.quota_used("youtube"), quota_limit=discovery.YT_DAILY_UNITS)

    def _pf(msg: str, brand: str = ""):
        return RedirectResponse("/portfolio?" + urlencode({"msg": msg, **({"brand": brand} if brand else {})}), status_code=303)

    @app.post("/portfolio/{ms_id}/edit")
    def portfolio_edit(ms_id: int, handle: str = Form(None), url: str = Form(None), priority: str = Form(""),
                       notes: str = Form(None)):
        try:
            changed = portfolio.edit_source(core.db, ms_id, handle=handle, url=url, priority=priority or None, notes=notes)
        except (ValueError, KeyError) as e:
            return _pf(f"Not saved: {e}")
        return _pf("Saved " + ", ".join(changed) + ("; identity must be verified again." if {"candidate_handle", "canonical_url"} & set(changed) else ".")
                   if changed else "Nothing changed.")

    @app.post("/portfolio/{ms_id}/link-rumble")
    def portfolio_link_rumble(ms_id: int):
        try:
            sid = portfolio.link_rumble_source(settings, core.db, ms_id)
        except ValueError as e:
            return _pf(str(e))
        return RedirectResponse("/sources?" + urlencode({"msg": f"Rumble source #{sid} added, switched off. Enable it here "
                                                              "once Rumble's and the creator's permissions are recorded."}), status_code=303)

    @app.post("/portfolio/import-roster")
    def portfolio_import_roster():
        res = roster_import.reconcile(settings, core.db)
        return _pf("Roster refused: " + "; ".join(res["problems"]) if res["problems"] else
                   "Roster reconciled: " + ", ".join(f"{k} {v}" for k, v in res["summary"].items() if k != "verification" and v))

    @app.post("/portfolio/item/{item_id}/retry")
    def portfolio_retry(item_id: int):
        return _pf("Item will be considered again (permission and disk checks still apply)." if core.discovery.retry_item(item_id)
                   else "Only failed items can be retried.")

    @app.post("/portfolio/{ms_id}/toggle")
    def portfolio_toggle(ms_id: int, enabled: str = Form("0")):
        portfolio.owner_set(core.db, ms_id, monitoring_enabled=1 if enabled == "1" else 0)
        return RedirectResponse("/portfolio?msg=Saved", status_code=303)

    @app.post("/portfolio/{ms_id}/check")
    def portfolio_check(ms_id: int):
        res = core.discovery.poll(ms_id)
        return RedirectResponse("/portfolio?" + urlencode({"msg": "Checked: " + json.dumps(res, default=str)[:160]}),
                                status_code=303)

    @app.post("/portfolio/{ms_id}/confirm")
    def portfolio_confirm(ms_id: int, note: str = Form("Owner checked the account")):
        try:
            portfolio.confirm_identity(core.db, ms_id, note)
        except ValueError as e:
            return RedirectResponse("/portfolio?" + urlencode({"msg": str(e)}), status_code=303)
        return RedirectResponse("/portfolio?msg=Confirmed", status_code=303)

    @app.get("/sources", response_class=HTMLResponse)
    def sources_page(request: Request, msg: str = "", err: str = ""):
        srcs = core.db.query("SELECT * FROM sources ORDER BY id")
        items = {s_["id"]: core.db.query("SELECT * FROM source_items WHERE source_id=? ORDER BY id DESC LIMIT 15",
                                         (s_["id"],)) for s_ in srcs}
        baseline = {r["source_id"]: r["n"] for r in core.db.query(
            "SELECT source_id, COUNT(*) n FROM source_items WHERE state='baseline' AND kind='vod' GROUP BY source_id")}
        perms = core.db.query("SELECT * FROM source_permissions ORDER BY domain")
        return page(request, "sources.html", srcs=srcs, items=items, msg=msg, err=err, s=settings, baseline=baseline,
                    perms=perms, restricted=sorted(ingest.RESTRICTED_PLATFORMS))

    def _sources_redirect(**q):
        return RedirectResponse("/sources?" + urlencode(q), status_code=303)

    @app.post("/sources")
    def add_source(name: str = Form(...), kind: str = Form(...), url: str = Form(...), poll_minutes: int = Form(15),
                   record_live: str = Form(""), backfill: int = Form(0), authorisation: str = Form("")):
        try:
            sid, ok, msg = monitor.add_source(settings, core.db, name=name, kind=kind, url=url,
                                              poll_minutes=poll_minutes, record_live=record_live == "1",
                                              backfill=backfill, authorisation=authorisation)
        except ValueError as e:
            return _sources_redirect(err=str(e))
        return _sources_redirect(msg=f"Source #{sid} saved and enabled. {msg}") if ok else \
            _sources_redirect(err=f"Source #{sid} saved but DISABLED: {msg}")

    @app.post("/sources/{sid}/enable")
    def enable_source(sid: int, enabled: str = Form("1")):
        ok, msg = monitor.set_enabled(settings, core.db, sid, enabled == "1")
        return _sources_redirect(msg=msg) if ok else _sources_redirect(err=msg)

    @app.post("/sources/{sid}/poll")
    def poll_source(sid: int):
        res = core.monitor.poll(sid)
        if res.get("blocked"):
            return _sources_redirect(err=res["blocked"])
        if res.get("error"):
            return _sources_redirect(err=f"Check failed: {res['error']}")
        return _sources_redirect(msg=f"Checked: {res.get('new', 0)} new item(s)"
                                     + ("; live stream recording" if res.get("live") else ""))

    @app.post("/sources/{sid}/backfill")
    def backfill_source(sid: int, count: int = Form(3)):
        res = core.monitor.backfill(sid, min(max(count, 1), 50))
        if res.get("blocked") or res.get("error"):
            return _sources_redirect(err=res.get("blocked") or res.get("error"))
        return _sources_redirect(msg=f"{res['queued']} older item(s) queued for import"
                                     + (f", {res['refused']} refused by the source rules" if res["refused"] else "")
                                     + f"; {res['remaining']} older item(s) remain. Each item is imported at most once.")

    @app.post("/sources/{sid}/delete")
    def delete_source(sid: int):
        core.db.execute("DELETE FROM sources WHERE id=?", (sid,))
        return _sources_redirect(msg="Source removed (already imported recordings are kept).")

    # ---- notifications -----------------------------------------------------
    @app.get("/settings/notifications", response_class=HTMLResponse)
    def notifications_page(request: Request, msg: str = "", err: str = ""):
        rows = core.db.query("SELECT * FROM notifications ORDER BY id DESC LIMIT 50")
        sample = None
        rec = core.db.one("SELECT r.id FROM recordings r WHERE EXISTS (SELECT 1 FROM clips c WHERE c.recording_id=r.id "
                          "AND c.status IN ('ready','approved')) ORDER BY r.id DESC LIMIT 1")
        if rec:
            sample = notify.build_ready(settings, core.db, rec["id"])
        return page(request, "notifications.html", rows=rows, s=settings, approved=notify.is_approved(settings, core.db),
                    approval=notify.approval(core.db), mailer=core.notifier.mailer.status(),
                    transport=settings.notify_transport, device=core.device_flow, sample=sample,
                    template_version=notify.TEMPLATE_VERSION, msg=msg, err=err,
                    graph=core.notifier.graph.status(), smtp=core.notifier.smtp.status(),
                    smtp_password_saved=bool(core.notifier.smtp.password()), token_backend=core.tokens.backend,
                    held=core.db.one("SELECT COUNT(*) n FROM notifications WHERE status='held'")["n"])

    def _notify_redirect(**q):
        return RedirectResponse("/settings/notifications?" + urlencode(q), status_code=303)

    @app.get("/settings/notifications/{nid}/preview", response_class=HTMLResponse)
    def notification_preview(nid: int):
        if not core.db.one("SELECT id FROM notifications WHERE id=?", (nid,)):
            raise HTTPException(404)
        return HTMLResponse(core.notifier.preview_html(nid))

    @app.get("/settings/notifications/sample", response_class=HTMLResponse)
    def notification_sample():
        rec = core.db.one("SELECT r.id FROM recordings r WHERE EXISTS (SELECT 1 FROM clips c WHERE c.recording_id=r.id "
                          "AND c.status IN ('ready','approved')) ORDER BY r.id DESC LIMIT 1")
        content = notify.build_ready(settings, core.db, rec["id"]) if rec else None
        if not content:
            return HTMLResponse("<p>No processed recording yet — import one to see a real sample.</p>")
        body = content["html"]
        for i in content["images"]:
            body = body.replace(f"cid:{i['cid']}", f"/clips/{i['clip_id']}/thumb")
        return HTMLResponse(f"<p style='font:13px sans-serif;color:#666'>Subject: <b>{html_escape(content['subject'])}</b>"
                            f" — To: {html_escape(settings.notify_to or '(NOTIFY_TO not set)')}</p>{body}")

    @app.post("/settings/notifications/recipient")
    def notify_recipient(notify_to: str = Form(""), dashboard_url: str = Form("")):
        notify_to, dashboard_url = notify_to.strip(), dashboard_url.strip().rstrip("/")
        if notify_to and not __import__("re").fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", notify_to):
            return _notify_redirect(err="That does not look like an email address.")
        if dashboard_url and urlparse(dashboard_url).scheme not in ("http", "https"):
            return _notify_redirect(err="The dashboard address must start with https:// (or http://).")
        changed = notify_to.lower() != settings.notify_to.lower()
        apply_settings(settings, {"NOTIFY_TO": notify_to, "DASHBOARD_URL": dashboard_url})
        return _notify_redirect(msg="Saved in your local .env." + (
            " The recipient changed, so sending is held again until you approve it below." if changed else ""))

    @app.post("/settings/notifications/transport")
    def notify_transport(transport: str = Form(...), outlook_client_id: str = Form(""), smtp_host: str = Form(""),
                         smtp_port: str = Form("587"), smtp_user: str = Form(""), smtp_from: str = Form(""),
                         smtp_password: str = Form("")):
        if transport == "graph":
            cid = outlook_client_id.strip()
            if not __import__("re").fullmatch(r"[0-9a-fA-F-]{36}", cid):
                return _notify_redirect(err="Paste the Application (client) ID exactly as shown in the Microsoft Entra "
                                            "portal (36 characters, like 1a2b3c4d-….).")
            apply_settings(settings, {"NOTIFY_TRANSPORT": "graph", "OUTLOOK_CLIENT_ID": cid})
            return _notify_redirect(msg="Saved. Now press “Connect Outlook” to sign in.")
        if transport == "smtp":
            if not (smtp_host.strip() and smtp_user.strip()):
                return _notify_redirect(err="Enter the mail server and the sending address.")
            if not smtp_port.strip().isdigit():
                return _notify_redirect(err="The port must be a number (587 for most providers).")
            apply_settings(settings, {"NOTIFY_TRANSPORT": "smtp", "SMTP_HOST": smtp_host.strip(),
                                      "SMTP_PORT": smtp_port.strip(), "SMTP_USER": smtp_user.strip(),
                                      "SMTP_FROM": smtp_from.strip(), "SMTP_SSL": "1" if smtp_port.strip() == "465" else "0"})
            if smtp_password:
                core.notifier.smtp.save_password(smtp_password.replace(" ", "") if "gmail" in smtp_host.lower()
                                                 else smtp_password)
            if not core.notifier.smtp.password():
                return _notify_redirect(err="Saved the server details, but no password is stored yet — enter the app password.")
            return _notify_redirect(msg=f"Sending account saved (password kept in: {core.tokens.backend}). "
                                        "Nothing is sent until you approve below.")
        return _notify_redirect(err="Unknown sending method.")

    @app.post("/settings/notifications/smtp/forget")
    def notify_smtp_forget():
        core.notifier.smtp.forget_password()
        return _notify_redirect(msg="Stored mail password removed.")

    @app.post("/settings/notifications/approve")
    def approve_notifications(confirm_to: str = Form(...), agree: str = Form("")):
        if agree != "yes" or confirm_to.strip().lower() != settings.notify_to.lower() or not settings.notify_to:
            return _notify_redirect(err="Type the recipient address exactly as configured and tick the confirmation.")
        notify.approve(settings, core.db)
        return _notify_redirect(msg=f"Approved: template v{notify.TEMPLATE_VERSION} to {settings.notify_to}. "
                                    "New notifications will now be sent automatically.")

    @app.post("/settings/notifications/revoke")
    def revoke_notifications():
        notify.revoke(core.db)
        return _notify_redirect(msg="Approval revoked; notifications will be held again.")

    @app.post("/settings/notifications/release")
    def release_notifications():
        n = notify.release_held(settings, core.db, core.queue)
        return _notify_redirect(msg=f"{n} held notification(s) queued for sending.")

    @app.post("/settings/notifications/test")
    def test_notification(confirm: str = Form("")):
        if confirm != "SEND":
            return _notify_redirect(err="Type SEND to confirm the test email.")
        if not notify.is_approved(settings, core.db):
            return _notify_redirect(err="Approve the recipient and template first.")
        content = {"subject": "[Clip review] Test email from your Clipping Engine",
                   "html": "<p>This is a test sent at your request from Settings → Notifications.</p>",
                   "text": "This is a test sent at your request from Settings → Notifications.", "images": []}
        nid = notify.create(settings, core.db, core.queue, kind="test", dedupe_key=f"test:{int(time.time())}",
                            content=content)
        return _notify_redirect(msg=f"Test email #{nid} queued.")

    @app.post("/settings/outlook/connect")
    def outlook_connect():
        try:
            flow = core.notifier.graph.start_device_code()
        except notify.NotifyNotConfigured as e:
            return _notify_redirect(err=str(e))
        core.device_flow.clear()
        core.device_flow.update(user_code=flow["user_code"], uri=flow["verification_uri"], state="pending",
                                expires=time.time() + int(flow.get("expires_in", 900)))

        def poll():
            interval = int(flow.get("interval", 5))
            while time.time() < core.device_flow.get("expires", 0):
                time.sleep(interval)
                try:
                    if core.notifier.graph.poll_device_code(flow["device_code"]) == "connected":
                        core.device_flow.update(state="connected")
                        return
                except Exception as e:  # noqa: BLE001
                    core.device_flow.update(state="failed", error=str(e))
                    return
            core.device_flow.update(state="expired")
        __import__("threading").Thread(target=poll, daemon=True).start()
        return _notify_redirect(msg="Follow the sign-in steps shown below.")

    @app.post("/settings/outlook/disconnect")
    def outlook_disconnect():
        core.tokens.delete("outlook")
        return _notify_redirect(msg="Outlook disconnected.")

    # ---- captions & autostart -----------------------------------------------
    @app.post("/settings/captions")
    def save_captions(preset: str = Form("anton"), size: int = Form(52), outline: float = Form(3.0),
                      position: float = Form(0.72), enabled: str = Form(""), backing: str = Form(""),
                      source_label: str = Form("")):
        base = captions.PRESETS.get(preset, captions.PRESETS["anton"])
        style = captions.CaptionStyle(**{**captions.asdict(base), "size": size, "outline": outline,
                                         "position": position, "enabled": enabled == "1", "backing": backing == "1",
                                         "source_label": source_label == "1"})
        captions.save_style(core.db, style)
        return _settings_redirect(msg="Caption settings saved; they apply to clips rendered or regenerated from now on.")

    @app.post("/settings/captions/reset")
    def reset_captions():
        core.db.execute("DELETE FROM meta WHERE key='caption_style'")
        return _settings_redirect(msg="Captions reset to the approved Anton style.")

    @app.get("/settings/captions/preview.png")
    def caption_preview():
        style = captions.load_style(core.db)
        out = settings.work_dir / "caption_preview"
        res = captions.verify_font(out, style)
        clip = core.db.one("SELECT thumb_path FROM clips WHERE thumb_path IS NOT NULL ORDER BY id DESC LIMIT 1")
        img = Path(res["image"]) if res.get("image") else None
        if clip and Path(clip["thumb_path"]).is_file():
            # Composite captions over a real (authorised, already imported) frame for realism.
            from .. import media as _media
            frame = out / "frame.png"
            _media.run_ffmpeg(["-i", str(Path(clip["thumb_path"]).resolve()), "-vf",
                               f"scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
                               f"ass=fontcheck.ass:fontsdir={captions.fontsdir_for(out)}", "-frames:v", "1",
                               frame.name], cwd=out)
            img = frame
        if not img or not img.is_file():
            raise HTTPException(500, "Preview could not be rendered")
        return FileResponse(img, media_type="image/png", headers={"Cache-Control": "no-store"})

    @app.post("/settings/autostart")
    def set_autostart(enabled: str = Form("")):
        try:
            if enabled == "1":
                path = autostart.install()
                return _settings_redirect(msg=f"The engine will start automatically when you sign in to Windows ({path}).")
            autostart.remove()
            return _settings_redirect(msg="Automatic start at sign-in removed.")
        except autostart.AutostartUnsupported as e:
            return _settings_redirect(err=str(e))

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
        if job and core.queue.retry(jid) and job["clip_id"] and job["kind"] == "render_clip":
            # Session 16: only a render retry re-renders. Retrying a delivery must leave the approved clip as it is.
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
                "worker_alive": bool(core.queue._thread and core.queue._thread.is_alive()),
                "health": health.summary(core), "keep_awake": core.keepawake.status()}

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
                    checklist=setup_status.checklist(core), perms=perms, caption=captions.load_style(core.db),
                    font_check=core.db.get_kv("caption_font_check"), autostart_on=autostart.is_installed(),
                    platform_labels={p_: setup_status.platform_label(core, p_) for p_ in ("tiktok", "instagram")},
                    autostart_supported=autostart.supported(), keepawake=core.keepawake.status(),
                    ts=tailscale.status(), ts_serve=tailscale.serve_status(settings.port) if tailscale.find_cli() else None,
                    phone=core.phone_report, phone_steps=tailscale.phone_instructions(settings.dashboard_url),
                    downtime=health.downtime(core.db, only_unseen=False),
                    restricted=sorted(ingest.RESTRICTED_PLATFORMS), threads=__import__(
                        "clipengine.transcribe", fromlist=["auto_cpu_threads"]).auto_cpu_threads())

    def _settings_redirect(**q):
        return RedirectResponse("/settings?" + urlencode(q), status_code=303)

    @app.post("/settings/phone/setup")
    def phone_setup():
        try:
            rep_ = tailscale.setup_phone_access(settings)
        except Exception as e:  # noqa: BLE001 - shown to the owner, never a crash page
            return _settings_redirect(err=f"Phone access setup failed: {e}")
        core.phone_report.clear()
        core.phone_report.update(rep_)
        if rep_["ok"]:
            return RedirectResponse("/settings?" + urlencode({"msg": f"Phone access is ready: {rep_['url']}"}) + "#phone",
                                    status_code=303)
        return RedirectResponse("/settings?" + urlencode({"err": rep_["next_step"]}) + "#phone", status_code=303)

    @app.post("/settings/phone/disable")
    def phone_disable():
        try:
            ok, out = tailscale.disable_serve()
        except Exception as e:  # noqa: BLE001
            return _settings_redirect(err=str(e))
        core.phone_report.clear()
        return _settings_redirect(msg="The dashboard is no longer published on your tailnet." if ok else out)

    @app.post("/settings/phone/seen")
    def phone_seen():
        core.phone_report.pop("password_created", None)
        return RedirectResponse("/settings#phone", status_code=303)

    @app.post("/settings/unattended")
    def set_unattended(prevent_sleep: str = Form("active")):
        if prevent_sleep not in power.MODES:
            return _settings_redirect(err="Unknown option.")
        apply_settings(settings, {"PREVENT_SLEEP": prevent_sleep})
        core.keepawake.tick()
        return RedirectResponse("/settings?" + urlencode({"msg": "Saved."}) + "#unattended", status_code=303)

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
        if platform == "instagram" and core.instagram.mode == "facebook":
            # One Meta login, with exact-id isolation for all six brand destinations.
            return RedirectResponse("/connections/facebook/connect?instagram=true", status_code=303)
        client = {"tiktok": core.tiktok, "instagram": core.instagram}.get(platform)
        if not client:
            raise HTTPException(404)
        try:
            url, state = client.authorize_url()
        except NotConfigured as e:
            return _settings_redirect(err=str(e))
        core.oauth_put(state["state"], {"platform": platform, **state})
        return RedirectResponse(url, status_code=303)

    @app.post("/disconnect/{platform}")
    def disconnect(platform: str):
        if platform in ("tiktok", "instagram"):
            core.tokens.delete(platform)
        return RedirectResponse("/settings?msg=Disconnected", status_code=303)

    def oauth_result(request: Request, result):
        # A 200 document ends the provider's cross-site redirect chain. The owner's
        # explicit same-site link can then send the existing Strict cookie. Never
        # mint a dashboard session from OAuth state or relax the password gate.
        target = result.headers.get("location", "/connections")
        parsed = urlparse(target)
        if parsed.scheme or parsed.netloc or parsed.path not in ("/connections", "/settings"):
            target = "/connections"
        return templates.TemplateResponse(request, "oauth_result.html", {"target": target, "counts": None})

    @app.get("/oauth/{platform}/callback")
    def oauth_callback(request: Request, platform: str, code: str = "", state: str = "", error: str = "",
                       error_description: str = ""):
        st = core.oauth_pop(state) if state else None
        if not st or st["platform"] != platform or time.time() - st["ts"] > 900:
            return oauth_result(request, _settings_redirect(err="Invalid or expired OAuth state; try connecting again."))
        if error:
            msg = f"Authorisation declined or refused by {platform}: {error_description or error}"
            core.db.set_kv(f"oauth_error.{platform}", msg)
            return oauth_result(request, _settings_redirect(err=msg))
        if not code:
            return oauth_result(request, _settings_redirect(err="No authorization code received; try connecting again."))
        if platform in ("facebook", "youtube"):
            return oauth_result(request, _connections_callback(platform, code, st))
        try:
            if platform == "tiktok":
                core.tiktok.exchange_code(code, st.get("verifier"))
            else:
                core.instagram.exchange_code(code)
        except (PublishError, NotConfigured, PermanentError) as e:
            core.db.set_kv(f"oauth_error.{platform}", str(e))
            return oauth_result(request, _settings_redirect(err=str(e)))
        except Exception as e:
            core.db.set_kv(f"oauth_error.{platform}", f"{type(e).__name__}: {e}")
            return oauth_result(request, _settings_redirect(err=f"Could not complete sign-in: {e}"))
        core.db.execute("DELETE FROM meta WHERE key = ?", (f"oauth_error.{platform}",))
        return oauth_result(request, _settings_redirect(msg=f"{platform} connected"))

    # ---- Session 13: per-destination connections (verify identity; publishing stays OFF) ---------------------------
    def _conn_redirect(**q):
        return RedirectResponse("/connections?" + urlencode(q), status_code=303)

    def _youtube_redirect_uri() -> str:
        return f"http://127.0.0.1:{settings.port}/oauth/youtube/callback"

    def _connections_callback(platform: str, code: str, st: dict):
        try:
            if platform == "facebook":
                if st.get("analytics"):
                    count = core.facebook.connect_analytics(core.db, code, st.get("redirect"))
                    if not count:
                        return _conn_redirect(err="No verified Meta destinations granted the required Insights permissions.")
                    core.db.set_kv("analytics.next_refresh", "0")
                    return _conn_redirect(msg=f"Read-only Meta analytics connected for {count} verified destinations.")
                core.facebook.exchange_code(code)
                res = core.facebook.adopt_pages(core.db)
                ok = sum(1 for r in res if r["state"] == "VERIFIED")
                return _conn_redirect(msg=f"Facebook login accepted. {ok} of {len(res)} destinations verified by exact id; "
                                          "any others were NOT connected (see the matrix).")
            brand = st.get("brand")
            if st.get("analytics"):
                acct = core.db.one("SELECT * FROM social_accounts WHERE platform='youtube' AND brand=? "
                                   "AND identity_verified_at IS NOT NULL", (brand,))
                if not acct:
                    return _conn_redirect(err="Verify the YouTube channel before connecting analytics.")
                core.youtube.connect_analytics(acct, code, st["redirect"], st["verifier"])
                core.db.set_kv("analytics.next_refresh", "0")
                return _conn_redirect(msg=f"YouTube analytics connected for {brand}.")
            core.youtube.oauth(brand).exchange(code, st["redirect"], st["verifier"])
            acct = core.db.one("SELECT * FROM social_accounts WHERE platform='youtube' AND brand=?", (brand,))
            res = core.youtube.verify(core.db, acct["id"])
            if res["state"] == "VERIFIED":
                return _conn_redirect(msg=f"YouTube ({brand}) connected and verified. Delivery settings were not changed.")
            return _conn_redirect(err=f"YouTube ({brand}) was NOT connected: {res['reason']} Authorise again and pick the "
                                      "correct channel on Google's channel chooser.")
        except (PublishError, NotConfigured, PermanentError) as e:
            return _conn_redirect(err=str(e))
        except Exception as e:  # noqa: BLE001
            return _conn_redirect(err=f"Could not complete sign-in: {type(e).__name__}: {e}")

    @app.get("/connections", response_class=HTMLResponse)
    def connections_page(request: Request, msg: str = "", err: str = ""):
        rows = cx.build_matrix(core.db, settings, core.tokens)
        return page(request, "connections.html", rows=rows, keys=cx.STATE_KEYS, msg=msg, err=err,
                    modes={r["id"]: delivery.mode_for(core.db, r["id"]) for r in rows},
                    fb_user=core.facebook.user_connected(), fb_cfg=core.facebook.configured,
                    fb_missing=core.facebook.missing_config(), yt_cfg=core.youtube.configured,
                    ig_public=settings.instagram_public_publish)

    @app.get("/connections/facebook/connect")
    def connections_facebook_connect(instagram: bool = False, analytics: bool = False):
        try:
            redirect = settings.meta_analytics_redirect_uri if analytics else None
            url, state = core.facebook.authorize_url(include_instagram=instagram, analytics=analytics,
                                                     redirect_uri=redirect)
        except NotConfigured as e:
            return _conn_redirect(err=str(e))
        core.oauth_put(state["state"], {"platform": "facebook", "analytics": analytics, **state})
        return RedirectResponse(url, status_code=303)

    @app.get("/connections/youtube/connect/{brand}")
    def connections_youtube_connect(brand: str):
        if not core.db.one("SELECT 1 FROM social_accounts WHERE platform='youtube' AND brand=? "
                           "AND handle NOT LIKE 'PENDING-%'", (brand,)):
            raise HTTPException(404)
        try:
            url, state, verifier = core.youtube.authorize_url(brand, _youtube_redirect_uri())
        except NotConfigured as e:
            return _conn_redirect(err=str(e))
        core.oauth_put(state, {"platform": "youtube", "state": state, "brand": brand, "verifier": verifier,
                               "redirect": _youtube_redirect_uri()})
        return RedirectResponse(url, status_code=303)

    @app.get("/connections/youtube/analytics/{brand}")
    def connections_youtube_analytics(brand: str):
        if not core.db.one("SELECT 1 FROM social_accounts WHERE platform='youtube' AND brand=? "
                           "AND identity_verified_at IS NOT NULL", (brand,)):
            raise HTTPException(404)
        oauth = core.youtube.analytics_oauth(brand)
        if not oauth.configured():
            return _conn_redirect(err="Configure Google OAuth before connecting YouTube analytics.")
        redirect = _youtube_redirect_uri()
        url, state, verifier = oauth.auth_url(redirect, prompt="select_account consent")
        core.oauth_put(state, {"platform": "youtube", "brand": brand, "analytics": True,
                               "verifier": verifier, "redirect": redirect})
        return RedirectResponse(url, status_code=303)

    @app.post("/connections/verify/{account_id}")
    def connections_verify(account_id: int):
        acct = core.db.one("SELECT * FROM social_accounts WHERE id=?", (account_id,))
        if not acct:
            raise HTTPException(404)
        try:
            if acct["platform"] == "youtube":
                res = core.youtube.verify(core.db, account_id)
                return _conn_redirect(msg=f"{acct['brand']} YouTube: {res['state']}. {res['reason']}")
            if acct["platform"] in ("facebook", "instagram"):
                res = [r for r in core.facebook.adopt_pages(core.db)
                       if r["brand"] == acct["brand"] and r["platform"] == acct["platform"]]
                return _conn_redirect(msg=f"{acct['brand']} {acct['platform']}: {res[0]['state'] if res else 'not checked'}.")
        except (PublishError, NotConfigured, PermanentError) as e:
            return _conn_redirect(err=str(e))
        return _conn_redirect(err="Verification for this platform is handled elsewhere.")

    @app.post("/connections/publishing/{account_id}")
    def connections_publishing(account_id: int, enable: str = Form(""), confirm: str = Form("")):
        acct = core.db.one("SELECT * FROM social_accounts WHERE id=?", (account_id,))
        if not acct:
            raise HTTPException(404)
        if enable == "on":
            if not acct["identity_verified_at"]:
                return _conn_redirect(err="Verify this destination's identity first.")
            if confirm != "ENABLE":
                return _conn_redirect(err="Type ENABLE to switch delivery on for this one destination.")
            core.db.update("social_accounts", account_id, publishing_enabled=1)
            return _conn_redirect(msg=f"Delivery switched ON for {acct['brand']} {acct['platform']} "
                                      "(drafts/private only, per clip, on your command).")
        core.db.update("social_accounts", account_id, publishing_enabled=0)
        return _conn_redirect(msg=f"Delivery switched OFF for {acct['brand']} {acct['platform']}.")

    @app.post("/connections/mode/{account_id}")
    def connections_mode(account_id: int, mode: str = Form(""), confirm: str = Form("")):
        """Per-destination visibility: 'draft' (default) or 'public' (needs the destination switched on and typed PUBLISH)."""
        acct = core.db.one("SELECT * FROM social_accounts WHERE id=?", (account_id,))
        if not acct:
            raise HTTPException(404)
        if acct["platform"] == "instagram":
            if mode == "public" and not settings.instagram_public_publish:
                return _conn_redirect(err="Instagram public mode needs INSTAGRAM_PUBLIC_PUBLISH switched on first.")
        elif acct["platform"] not in ("facebook", "youtube"):
            return _conn_redirect(err="Public mode applies to Facebook, YouTube and Instagram destinations only.")
        if mode == "public":
            if not acct["publishing_enabled"]:
                return _conn_redirect(err="Switch delivery on for this destination first.")
            if confirm != "PUBLISH":
                return _conn_redirect(err="Type PUBLISH to make approved clips go PUBLIC on this one destination.")
            delivery.set_mode(core.db, account_id, "public")
            return _conn_redirect(msg=f"{acct['brand']} {acct['platform']}: approved clips will now be published PUBLICLY "
                                      "(subject to the platform's own limits, shown on each result).")
        delivery.set_mode(core.db, account_id, "draft")
        return _conn_redirect(msg=f"{acct['brand']} {acct['platform']}: back to drafts / private only.")

    @app.post("/connections/deliver/{clip_id}/{account_id}")
    def connections_deliver(clip_id: int, account_id: int, confirm: str = Form("")):
        if confirm != "DELIVER":
            return _conn_redirect(err="Type DELIVER to confirm this one delivery.")
        try:
            res = delivery.deliver_draft(core.db, {"youtube": core.youtube, "facebook": core.facebook}, clip_id,
                                         account_id, owner_command=True)
        except (PublishError, NotConfigured, PermanentError) as e:
            return _conn_redirect(err=str(e))
        return _conn_redirect(msg=res["detail"])

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

    from . import campaigns, workspace
    from commercial_agency import web as agency_web
    campaigns.register(app, core, page)
    agency_web.register(app, settings, page)
    workspace.register(app, core, page, templates)
    return app
