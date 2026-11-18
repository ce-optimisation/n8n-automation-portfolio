"""Multi-niche editorial workspace: niches, creators & research, social accounts, queue control, storage &
cloud, publication packages and commercial measurement. Same FastAPI + Jinja2 + HTMX stack, same auth/CSRF
middleware (registered in app.py), mobile-first templates."""
from __future__ import annotations

import csv
import io
import json
import time

from fastapi import Form, HTTPException, Request, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import cleanup, editorial, platforms, portfolio, storage, titles
from ..publishing import packages as pk


def _go(url: str, **q) -> RedirectResponse:
    from urllib.parse import urlencode
    return RedirectResponse(url + (("&" if "?" in url else "?") + urlencode(q) if q else ""), status_code=303)


def _list(v: str) -> list[str]:
    return [x.strip() for x in (v or "").replace("\n", ",").split(",") if x.strip()]


def register(app, core, page, templates) -> None:
    db, settings = core.db, core.settings

    # ---- niches --------------------------------------------------------------------------
    @app.get("/niches", response_class=HTMLResponse)
    def niches(request: Request, msg: str = "", err: str = ""):
        rows = db.query("SELECT * FROM niches ORDER BY active DESC, grp, name")
        scores = {r["key"]: r for r in db.query("SELECT * FROM niche_scores")}
        usage = {r["k"]: r["n"] for r in db.query("SELECT niche_key k, COUNT(*) n FROM clips GROUP BY niche_key")}
        return page(request, "niches.html", niches=rows, scores=scores, usage=usage, msg=msg, err=err,
                    parsed={r["key"]: json.loads(r["config_json"]) for r in rows})

    @app.get("/niches/{key}", response_class=HTMLResponse)
    def niche_edit(request: Request, key: str, msg: str = "", err: str = ""):
        p = editorial.load_niche(db, key)
        if not p:
            raise HTTPException(404)
        hist = db.query("SELECT version, note, created_at FROM niche_history WHERE key=? ORDER BY version DESC LIMIT 10", (key,))
        return page(request, "niche_edit.html", p=p, hist=hist, msg=msg, err=err, rules=editorial.REJECT_RULES,
                    flags=editorial.FLAG_RULES, profiles=["v4", "v5", "v6"])

    @app.post("/niches/{key}")
    async def niche_save(request: Request, key: str):
        form = await request.form()
        p = editorial.load_niche(db, key)
        if not p:
            raise HTTPException(404)
        try:
            for f in ("name", "audience", "purpose", "complete_clip", "hook", "pacing", "context_requirements",
                      "post_style"):
                if f in form:
                    setattr(p, f, str(form[f]).strip())
            for f in ("subjects", "terms", "avoid_terms", "hashtags", "claims_checks"):
                if f in form:
                    setattr(p, f, _list(str(form[f])))
            for f in ("min_s", "max_s", "specificity_weight", "reaction_weight", "caption_scale"):
                if f in form and str(form[f]).strip():
                    setattr(p, f, float(form[f]))
            for f in ("clips_per_recording", "min_quality"):
                if f in form and str(form[f]).strip():
                    setattr(p, f, int(form[f]))
            if form.get("selection_profile") in ("v4", "v5", "v6"):
                p.selection_profile = str(form["selection_profile"])
            p.reject_rules = [r for r in editorial.REJECT_RULES if form.get(f"reject_{r}")]
            p.flag_rules = [r for r in editorial.FLAG_RULES if form.get(f"flag_{r}")]
            p.reply_required = bool(form.get("reply_required"))
            p.compliance_review = bool(form.get("compliance_review")) or p.risk == "high"
            p.active = bool(form.get("active"))
            if not 3 <= p.min_s < p.max_s <= 3600:
                raise ValueError("Durations must satisfy 3 ≤ minimum < maximum ≤ 3600 seconds "
                                 "(these are PREFERRED ranges for the v6 selector, not limits).")
        except ValueError as e:
            return _go(f"/niches/{key}", err=str(e))
        editorial.save_niche(db, p, note=str(form.get("note") or "edited in dashboard")[:200])
        return _go(f"/niches/{key}", msg=f"Saved as version {p.version}.")

    @app.post("/niches/{key}/revert/{version}")
    def niche_revert(key: str, version: int):
        row = db.one("SELECT config_json FROM niche_history WHERE key=? AND version=?", (key, version))
        if not row:
            raise HTTPException(404)
        p = editorial.NicheProfile.from_dict(json.loads(row["config_json"]))
        editorial.save_niche(db, p, note=f"reverted to version {version}")
        return _go(f"/niches/{key}", msg=f"Restored version {version} as version {p.version}.")

    # ---- creators & research ---------------------------------------------------------------
    @app.get("/creators", response_class=HTMLResponse)
    def creators(request: Request, status: str = "", niche: str = "", platform: str = "", sort: str = "score",
                 msg: str = "", err: str = ""):
        conds, params = [], []
        if status == "operational":
            conds.append("status IN ('active','paused','blocked','needs_authorisation')")
        elif status:
            conds.append("status = ?")
            params.append(status)
        if platform:
            conds.append("platform = ?")
            params.append(platform)
        if niche:
            conds.append("niches_json LIKE ?")
            params.append(f'%"{niche}"%')
        order = {"score": "commercial_score DESC NULLS LAST, name", "name": "name", "priority": "priority DESC, name",
                 "updated": "updated_at DESC"}.get(sort, "name")
        where = ("WHERE " + " AND ".join(conds)) if conds else ""
        try:
            rows = db.query(f"SELECT * FROM creators {where} ORDER BY {order}", tuple(params))
        except Exception:  # noqa: BLE001 - SQLite before 3.30 has no NULLS LAST
            rows = db.query(f"SELECT * FROM creators {where} ORDER BY commercial_score DESC, name", tuple(params))
        srcs = {r["creator_id"]: r for r in db.query("SELECT * FROM sources WHERE creator_id IS NOT NULL")}
        return page(request, "creators.html", rows=rows, srcs=srcs, msg=msg, err=err,
                    f={"status": status, "niche": niche, "platform": platform, "sort": sort},
                    niches=db.query("SELECT key, name FROM niches ORDER BY name"), platforms=list(platforms.ADAPTERS))

    @app.post("/creators")
    def creator_add(name: str = Form(...), url: str = Form(""), niches: str = Form(""), status: str = Form("research"),
                    platform: str = Form(""), notes: str = Form("")):
        ref = platforms.parse_channel_url(url) if url.strip() else None
        if url.strip() and not ref:
            return _go("/creators", err="That address is not a recognised Twitch, Kick, YouTube or Rumble channel URL.")
        try:
            cid = db.insert("creators", name=name.strip()[:120], url=ref.url if ref else None,
                            platform=(ref.platform if ref else platform) or None,
                            status=status if status in ("research", "shortlisted", "needs_authorisation") else "research",
                            niches_json=json.dumps(_list(niches)), profile_json="{}", permission_json="{}",
                            platforms_json=json.dumps([{"platform": ref.platform, "url": ref.url, "primary": True,
                                                        "verified": False}] if ref else []), notes=notes[:2000])
        except Exception:  # noqa: BLE001 - unique URL
            return _go("/creators", err="A creator with that channel URL already exists.")
        return _go(f"/creators/{cid}", msg="Added. Research data never authorises retrieval.")

    @app.get("/creators/{cid}", response_class=HTMLResponse)
    def creator_page(request: Request, cid: int, msg: str = "", err: str = ""):
        c = db.one("SELECT * FROM creators WHERE id=?", (cid,))
        if not c:
            raise HTTPException(404)
        prof = editorial.CreatorProfile.from_dict(json.loads(c["profile_json"] or "{}"))
        return page(request, "creator.html", c=c, prof=prof, msg=msg, err=err,
                    perm=json.loads(c["permission_json"] or "{}"), plats=json.loads(c["platforms_json"] or "[]"),
                    stats=json.loads(c["stats_json"] or "{}"), cites=json.loads(c["sources_json"] or "[]"),
                    niches=db.query("SELECT key, name FROM niches ORDER BY name"),
                    cniches=json.loads(c["niches_json"] or "[]"), content_types=list(editorial.CONTENT_TYPES),
                    sources=db.query("SELECT * FROM sources WHERE creator_id=?", (cid,)),
                    all_sources=db.query("SELECT id, name, url FROM sources WHERE creator_id IS NULL"),
                    caps=platforms.capability_matrix(settings).get(c["platform"] or "", {}))

    @app.post("/creators/{cid}")
    async def creator_save(request: Request, cid: int):
        form = await request.form()
        c = db.one("SELECT * FROM creators WHERE id=?", (cid,))
        if not c:
            raise HTTPException(404)
        prof = editorial.CreatorProfile.from_dict(json.loads(c["profile_json"] or "{}"))
        for f in ("personality", "speech_patterns", "limitations"):
            if f in form:
                setattr(prof, f, str(form[f]).strip()[:2000])
        for f in ("recurring_topics", "vocabulary", "avoid_terms"):
            if f in form:
                setattr(prof, f, _list(str(form[f]))[:80])
        if form.get("content_type") in editorial.CONTENT_TYPES or form.get("content_type") == "":
            prof.content_type = str(form.get("content_type"))
        status = str(form.get("status") or c["status"])
        if status not in ("research", "shortlisted", "active", "paused", "blocked", "needs_authorisation"):
            status = c["status"]
        perm = json.loads(c["permission_json"] or "{}")
        for f in ("granted_by", "reference", "permitted_uses", "expires", "evidence"):
            if f in form:
                perm[f] = str(form[f]).strip()[:500]
        if status == "active" and not (perm.get("granted_by") and perm.get("reference")):
            return _go(f"/creators/{cid}", err="Record the permission evidence (who granted it and a reference) "
                                               "before marking a creator active. Research does not authorise retrieval.")
        if perm.get("expires") and perm["expires"] < time.strftime("%Y-%m-%d") and status == "active":
            status = "needs_authorisation"
        from dataclasses import asdict
        db.update("creators", cid, status=status, priority=int(form.get("priority") or c["priority"]),
                  niches_json=json.dumps([n for n in form.getlist("niches")]),
                  profile_json=json.dumps(asdict(prof)), permission_json=json.dumps(perm),
                  schedule=str(form.get("schedule") or "")[:300], attribution=str(form.get("attribution") or "")[:300],
                  expected_format=str(form.get("expected_format") or "")[:100],
                  typical_minutes=float(form.get("typical_minutes") or 0) or None,
                  notes=str(form.get("notes") or c["notes"] or "")[:4000])
        link = str(form.get("link_source") or "")
        if link.isdigit():
            db.update("sources", int(link), creator_id=cid)
        return _go(f"/creators/{cid}", msg="Saved." + (" Note: an 'active' creator still needs an enabled, authorised "
                                                       "source on the Sources page before anything is retrieved."
                                                       if status == "active" else ""))

    @app.post("/creators/{cid}/check")
    def creator_check(cid: int):
        """Official-API discovery for this creator's channel (metadata only; nothing is downloaded)."""
        c = db.one("SELECT * FROM creators WHERE id=?", (cid,))
        ref = platforms.parse_channel_url(c["url"] or "") if c else None
        if not ref:
            return _go(f"/creators/{cid}", err="No recognised channel URL to check.")
        try:
            rep = platforms.adapter_for(settings, ref.platform).report(ref)
        except platforms.AdapterError as e:
            return _go(f"/creators/{cid}", err=str(e))
        except Exception as e:  # noqa: BLE001
            return _go(f"/creators/{cid}", err=f"Check failed: {e}")
        stats = json.loads(c["stats_json"] or "{}")
        stats["api_check"] = {"at": time.strftime("%Y-%m-%d %H:%M"), "source": rep.source, "identity": rep.identity,
                              "live": rep.live.__dict__ if rep.live else None,
                              "items": [i.__dict__ for i in rep.items[:15]], "errors": rep.errors}
        plats = json.loads(c["platforms_json"] or "[]")
        for p in plats:
            if p.get("url") == ref.url:
                p.update(verified=True, observed=time.strftime("%Y-%m-%d"))
        db.update("creators", cid, stats_json=json.dumps(stats, default=str), platforms_json=json.dumps(plats))
        return _go(f"/creators/{cid}", msg=f"Checked via {rep.source}.")

    @app.get("/research", response_class=HTMLResponse)
    def research(request: Request, niche: str = "", platform: str = "", sort: str = "score"):
        conds, params = ["status IN ('research','shortlisted')"], []
        if niche:
            conds.append("niches_json LIKE ?")
            params.append(f'%"{niche}"%')
        if platform:
            conds.append("platform = ?")
            params.append(platform)
        order = {"score": "COALESCE(commercial_score, -1) DESC", "name": "name"}.get(sort, "name")
        rows = db.query(f"SELECT * FROM creators WHERE {' AND '.join(conds)} ORDER BY status='shortlisted' DESC, {order}",
                        tuple(params))
        scores = db.query("SELECT s.*, n.name FROM niche_scores s LEFT JOIN niches n ON n.key=s.key ORDER BY overall DESC")
        counts = {r["status"]: r["n"] for r in db.query("SELECT status, COUNT(*) n FROM creators GROUP BY status")}
        enabled = db.one("SELECT COUNT(*) n FROM sources WHERE enabled=1")["n"]
        return page(request, "research.html", rows=rows, scores=scores, counts=counts, enabled=enabled,
                    f={"niche": niche, "platform": platform, "sort": sort},
                    niches=db.query("SELECT key, name FROM niches ORDER BY name"), platforms=list(platforms.ADAPTERS))

    @app.post("/creators/{cid}/shortlist")
    def creator_shortlist(cid: int, on: str = Form("1")):
        c = db.one("SELECT status FROM creators WHERE id=?", (cid,))
        if c and c["status"] in ("research", "shortlisted"):
            db.update("creators", cid, status="shortlisted" if on == "1" else "research")
        return _go("/research")

    @app.get("/platforms", response_class=HTMLResponse)
    def platforms_page(request: Request):
        return page(request, "platforms.html", matrix=platforms.capability_matrix(settings))

    # ---- social accounts -------------------------------------------------------------------
    @app.get("/accounts", response_class=HTMLResponse)
    def accounts(request: Request, msg: str = "", err: str = ""):
        rows = db.query("SELECT * FROM social_accounts ORDER BY platform, handle")
        yt = {r["id"]: core.packages.youtube_oauth(r["id"]).connected() for r in rows if r["platform"] == "youtube"}
        return page(request, "accounts.html", rows=rows, yt=yt, msg=msg, err=err,
                    niches=db.query("SELECT key, name FROM niches ORDER BY name"),
                    brand_names=[(k, b["name"]) for k, b in portfolio.BRANDS.items()],
                    google_ready=bool(settings.google_client_id and settings.google_client_secret))

    @app.post("/accounts")
    async def account_add(request: Request):
        form = await request.form()
        plat = str(form.get("platform") or "")
        handle = str(form.get("handle") or "").strip().lstrip("@")
        if plat not in ("youtube", "tiktok", "instagram", "facebook", "x", "rumble") or not handle:
            return _go("/accounts", err="Choose a platform and enter the account handle.")
        try:
            db.insert("social_accounts", platform=plat, handle=handle[:80], display_name=str(form.get("display_name") or "")[:120],
                      brand=str(form.get("brand") or "")[:120], niches_json=json.dumps(form.getlist("niches")),
                      status="manual", notes=str(form.get("notes") or "")[:1000])
        except Exception:  # noqa: BLE001
            return _go("/accounts", err="That account is already registered.")
        return _go("/accounts", msg="Account added. It receives only clips from the niches mapped to it.")

    @app.post("/accounts/record")
    async def account_record(request: Request):
        """Record the owner's real account for one of the six brands' slots (nothing is created on any platform)."""
        form = await request.form()
        try:
            portfolio.set_account(db, str(form.get("brand") or ""), str(form.get("platform") or ""),
                                  str(form.get("handle") or "").lstrip("@"), str(form.get("display_name") or "")[:120],
                                  str(form.get("account_id") or "")[:80])
        except ValueError as e:
            return _go("/accounts", err=str(e))
        return _go("/accounts", msg="Account recorded for that brand. It still needs your authorisation to connect.")

    @app.post("/accounts/{aid}/niches")
    async def account_niches(request: Request, aid: int):
        form = await request.form()
        acct = db.one("SELECT brand FROM social_accounts WHERE id=?", (aid,))
        own = portfolio.BRANDS.get((acct["brand"] if acct else "") or "")
        if own and set(form.getlist("niches")) - {own["niche"]}:
            return _go("/accounts", err=f"This is the {own['name']} account: it can only receive {own['name']} clips. "
                                        "Brands never share an account.")
        db.update("social_accounts", aid, niches_json=json.dumps(form.getlist("niches")),
                  brand=str(form.get("brand") or "")[:120])
        return _go("/accounts", msg="Mapping saved.")

    @app.post("/accounts/{aid}/delete")
    def account_delete(aid: int):
        if db.one("SELECT COUNT(*) n FROM publication_packages WHERE account_id=?", (aid,))["n"]:
            return _go("/accounts", err="This account has publication packages; it is kept for the record.")
        db.execute("DELETE FROM social_accounts WHERE id=?", (aid,))
        return _go("/accounts", msg="Removed.")

    @app.get("/accounts/{aid}/youtube/connect")
    def account_youtube_connect(request: Request, aid: int):
        oauth = core.packages.youtube_oauth(aid)
        if not oauth.configured():
            return _go("/accounts", err="Add CLIPENGINE_GOOGLE_CLIENT_ID and CLIPENGINE_GOOGLE_CLIENT_SECRET to .env first (see Storage page).")
        redirect = f"{settings.base_url()}/google/oauth/callback"
        url, state, verifier = oauth.auth_url(redirect)
        core.oauth_put(state, {"kind": "youtube", "account_id": aid, "verifier": verifier, "redirect": redirect})
        return RedirectResponse(url, status_code=303)

    # ---- queue -------------------------------------------------------------------------------
    @app.get("/queue", response_class=HTMLResponse)
    def queue_page(request: Request, msg: str = ""):
        running = db.query("SELECT * FROM jobs WHERE status='running'")
        order = core.queue.queued_order()
        recent = db.query("SELECT * FROM jobs WHERE status IN ('done','failed','cancelled') ORDER BY updated_at DESC LIMIT 30")
        recs = {r["id"]: r for r in db.query("SELECT id, title, origin, creator_id, lifecycle, status FROM recordings")}
        live = db.query("SELECT i.*, s.name FROM source_items i JOIN sources s ON s.id=i.source_id WHERE i.state='recording'")
        return page(request, "queue.html", running=running, order=order, recent=recent, recs=recs, live=live, msg=msg,
                    paused=core.queue.paused(), blocked=core.queue.blocked_reason())

    @app.post("/queue/pause")
    def queue_pause():
        core.queue.pause()
        return _go("/queue", msg="Paused: no new job will start. A running step finishes normally.")

    @app.post("/queue/resume")
    def queue_resume():
        core.queue.resume()
        return _go("/queue", msg="Resumed.")

    @app.post("/queue/{jid}/move")
    def queue_move(jid: int, direction: str = Form("up")):
        core.queue.move(jid, "up" if direction == "up" else "down")
        return _go("/queue")

    @app.post("/queue/{jid}/priority")
    def queue_priority(jid: int, priority: int = Form(50)):
        core.queue.set_priority(jid, priority)
        return _go("/queue")

    # ---- storage & cloud ---------------------------------------------------------------------
    @app.get("/storage", response_class=HTMLResponse)
    def storage_page(request: Request, msg: str = "", err: str = "", plan: str = ""):
        rep = storage.capacity_report(settings, db)
        from .. import livestore
        live = livestore.live_folders(settings, db, set(getattr(getattr(core, "monitor", None), "procs", {}) or {}))
        recs = db.query("SELECT * FROM recordings WHERE engine_owned=1 AND raw_deleted_at IS NULL AND file_path IS NOT NULL "
                        "ORDER BY id DESC LIMIT 20")
        gates = {r["id"]: core.lifecycle.gates(r["id"]) for r in recs}
        return page(request, "storage.html", cleanup=core.cleanup.overview(), cleanup_help=cleanup.HELP, rep=rep, cloud=core.cloud.status(), held=core.cloud.uploads_held(),
                    recs=recs, live=live, gates=gates, msg=msg, err=err, plan=json.loads(plan) if plan else None,
                    s=settings, redirect=f"{settings.base_url()}/google/oauth/callback",
                    legacy=db.one("SELECT COUNT(*) n FROM recordings WHERE engine_owned=0 AND file_path IS NOT NULL")["n"])

    @app.get("/cloud/connect")
    def cloud_connect():
        if not core.cloud.oauth.configured():
            return _go("/storage", err="Add CLIPENGINE_GOOGLE_CLIENT_ID and CLIPENGINE_GOOGLE_CLIENT_SECRET to .env first (steps on this page).")
        redirect = f"{settings.base_url()}/google/oauth/callback"
        url, state, verifier = core.cloud.oauth.auth_url(redirect)
        core.oauth_put(state, {"kind": "gdrive", "verifier": verifier, "redirect": redirect})
        return RedirectResponse(url, status_code=303)

    @app.get("/google/oauth/callback")
    def google_callback(state: str = "", code: str = "", error: str = ""):
        st = core.oauth_pop(state) if state else None
        if error or not st or not code:
            return _go("/storage", err=f"Google sign-in was not completed ({error or 'invalid or expired request'}).")
        try:
            if st["kind"] == "gdrive":
                core.cloud.oauth.exchange(code, st["redirect"], st["verifier"])
                core.cloud.refresh_quota()
                for c in db.query("SELECT id FROM clips WHERE status IN ('ready','approved') AND video_path IS NOT NULL"):
                    core.cloud.enqueue_clip(c["id"])
                return _go("/storage", msg="Google Drive connected. Finished clips are being uploaded and verified.")
            oauth = core.packages.youtube_oauth(int(st["account_id"]))
            oauth.exchange(code, st["redirect"], st["verifier"])
            db.update("social_accounts", int(st["account_id"]), status="connected", connection_key=f"youtube:{st['account_id']}")
            return _go("/accounts", msg="YouTube account connected (uploads are always private).")
        except Exception as e:  # noqa: BLE001
            return _go("/storage", err=str(e))

    @app.post("/cloud/disconnect")
    def cloud_disconnect():
        core.cloud.oauth.disconnect()
        return _go("/storage", msg="Disconnected. Nothing in Drive was deleted.")

    @app.post("/storage/retention")
    async def storage_retention(request: Request):
        from ..config import apply_settings
        form = await request.form()
        vals = {k: str(form[k]) for k in ("REJECTED_CLIP_HOURS", "PUBLISHED_CLIP_DAYS", "CLOUD_MIN_FREE_GB") if k in form}
        vals["RAW_DELETE_AFTER_CLOUD"] = "1" if form.get("RAW_DELETE_AFTER_CLOUD") else "0"
        try:
            apply_settings(settings, vals)
        except Exception as e:  # noqa: BLE001
            return _go("/storage", err=str(e))
        return _go("/storage", msg="Retention settings saved.")

    @app.post("/storage/relocate/plan")
    def relocate_plan(target: str = Form("")):
        return _go("/storage", plan=json.dumps(storage.plan_relocation(settings, target)))

    @app.post("/storage/relocate")
    def relocate(target: str = Form(""), confirm: str = Form("")):
        try:
            res = storage.relocate(settings, db, core.queue, target, confirm)
        except Exception as e:  # noqa: BLE001
            return _go("/storage", err=str(e))
        return _go("/storage", msg=f"Copied {res['files']} files ({res['gb']:.1f} GB) to {res['target']}. {res['next']}")

    @app.post("/storage/check/{rid}")
    def storage_check(rid: int):
        res = core.lifecycle.check_recording(rid)
        return _go("/storage", msg=("Raw recording deleted (all gates passed)." if res.get("deleted") else
                                    f"Kept: {res.get('reason')}"))

    # ---- clip workspace (HTMX partial on the clip page) --------------------------------------
    @app.get("/clips/{cid}/workspace", response_class=HTMLResponse)
    def clip_workspace(request: Request, cid: int):
        c = db.clip(cid)
        if not c:
            raise HTTPException(404)
        rec = db.recording(c["recording_id"])
        pkgs = db.query("SELECT p.*, a.handle FROM publication_packages p LEFT JOIN social_accounts a ON a.id=p.account_id "
                        "WHERE p.clip_id=? ORDER BY p.platform", (cid,))
        accts = db.query("SELECT * FROM social_accounts ORDER BY platform, handle")
        allowed = [a for a in accts if pk.account_allows(a, c["niche_key"])]
        creator = db.one("SELECT * FROM creators WHERE id=?", (rec["creator_id"],)) if rec["creator_id"] else None
        cloud_rows = db.query("SELECT kind, state FROM cloud_objects WHERE owner_kind='clip' AND owner_id=?", (cid,))
        ev = core.cleanup.evaluate(cid)
        raw_gone = bool(rec and rec["raw_deleted_at"])
        return templates.TemplateResponse(request, "_clip_workspace.html", {
            "completeness": json.loads(c["completeness_json"] or "{}"), "cleanup": ev, "cleanup_help": cleanup.HELP,
            "raw_gone": raw_gone, "cleanup_on": core.cleanup.enabled(),
            "ready_text": time.strftime("%d %b %H:%M", time.localtime(ev["ready_at"])) if ev.get("ready_at") else "",
            "c": c, "rec": rec, "pkgs": pkgs, "accounts": allowed, "creator": creator, "PLATFORMS": pk.PLATFORMS,
            "quality": json.loads(c["quality_json"] or "{}"), "cloud_rows": cloud_rows,
            "niche": editorial.load_niche(db, c["niche_key"]),
            "issues": {p: core.packages.check_spec(c, p) for p in pk.PLATFORMS}})

    @app.post("/clips/{cid}/cleanup/{action}")
    def clip_cleanup(cid: int, action: str, reason: str = Form("")):
        c = db.clip(cid)
        if not c:
            raise HTTPException(404)
        lc = core.cleanup
        if action == "keep":
            lc.keep_local(cid, True)
            msg = "This clip will stay on this computer."
        elif action == "unkeep":
            lc.keep_local(cid, False)
            msg = "Keep-locally removed."
        elif action == "auto_on":
            lc.set_auto(cid, 1)
            msg = "Automatic cleanup allowed for this clip (once every safeguard passes)."
        elif action == "auto_off":
            lc.set_auto(cid, 0)
            msg = "Automatic cleanup is off for this clip."
        elif action == "auto_default":
            lc.set_auto(cid, None)
            msg = "This clip follows the global cleanup switch."
        elif action == "cancel":
            lc.cancel(cid, reason.strip() or "cancelled by the owner")
            msg = "Cleanup cancelled and held."
        elif action == "release":
            lc.release_hold(cid)
            msg = "Hold released."
        elif action == "delete":
            res = lc.delete(cid, manual=True)
            msg = (f"Local copy removed ({res['bytes'] / 1e6:.1f} MB freed). It is restorable from Google Drive."
                   if res.get("deleted") else "Not removed: " + res.get("reason", "")[:300])
        elif action == "restore":
            try:
                lc.request_restore(cid)
                msg = "Restore from Google Drive queued."
            except ValueError as e:
                msg = str(e)
        else:
            raise HTTPException(404)
        return _go(f"/clips/{cid}", msg=msg)

    @app.post("/packages/{pid}/confirm")
    def package_confirm(pid: int, note: str = Form("")):
        pkg = db.one("SELECT clip_id FROM publication_packages WHERE id=?", (pid,))
        if not pkg:
            raise HTTPException(404)
        try:
            core.cleanup.confirm_destination(pid, note.strip())
        except ValueError as e:
            return _go(f"/clips/{pkg['clip_id']}", err=str(e))
        return _go(f"/clips/{pkg['clip_id']}", msg="Recorded as confirmed by you (manual).")

    @app.post("/packages/{pid}/exclude")
    def package_exclude(pid: int, reason: str = Form("")):
        pkg = db.one("SELECT clip_id FROM publication_packages WHERE id=?", (pid,))
        if not pkg:
            raise HTTPException(404)
        try:
            core.cleanup.exclude_destination(pid, reason)
        except ValueError as e:
            return _go(f"/clips/{pkg['clip_id']}", err=str(e))
        return _go(f"/clips/{pkg['clip_id']}", msg="Excluded from the required destinations (recorded).")

    @app.post("/packages/{pid}/include")
    def package_include(pid: int):
        pkg = db.one("SELECT clip_id FROM publication_packages WHERE id=?", (pid,))
        if not pkg:
            raise HTTPException(404)
        core.cleanup.include_destination(pid)
        return _go(f"/clips/{pkg['clip_id']}", msg="Required again.")

    @app.post("/storage/cleanup")
    def storage_cleanup(enabled: str = Form(""), grace_hours: float = Form(24.0)):
        core.cleanup.set_enabled(enabled == "1")
        core.cleanup.set_grace_hours(min(max(grace_hours, 0.0), 24 * 30))
        return _go("/storage", msg="Finished-clip cleanup settings saved." + (" Automatic cleanup is ON." if enabled == "1" else " Automatic cleanup is OFF."))

    @app.post("/storage/live/{iid}/discard")
    def storage_live_discard(iid: int, confirm: str = Form("")):
        from .. import livestore
        res = livestore.discard(settings, db, iid, confirm.strip(), active_ids=set(getattr(getattr(core, "monitor", None), "procs", {}) or {}))
        if res["deleted"]:
            return _go("/storage", msg=f"Discarded {len(res['removed'])} file(s); {res['recovered_bytes_measured'] / 1e9:.2f} GB recovered (measured).")
        return _go("/storage", err="Nothing was deleted: " + res["reason"])

    @app.post("/storage/archive")
    def storage_archive(enabled: str = Form("")):
        core.cleanup.set_archive(enabled == "1")
        return _go("/storage", msg="Drive-only archive is ON: reviewed clips with a verified Drive master may be removed from this computer."
                   if enabled == "1" else "Drive-only archive is OFF.")

    @app.post("/storage/archive/test")
    def storage_archive_test():
        r = core.cleanup.restore_test()
        return _go("/storage", **({"msg": "Restore test passed: " + r["reason"]} if r["ok"] else {"err": "Restore test failed: " + r["reason"]}))

    @app.post("/storage/cleanup/run")
    def storage_cleanup_run():
        res = core.cleanup.run()
        return _go("/storage", msg=f"Cleanup pass: {res['deleted']} removed ({res['bytes'] / 1e6:.1f} MB), {res['held']} held.")

    @app.post("/clips/{cid}/keep")
    def clip_keep(cid: int, on: str = Form("1")):
        db.update("clips", cid, retain=1 if on == "1" else 0)
        return _go(f"/clips/{cid}", msg="Kept: exempt from automatic expiry." if on == "1" else "Normal retention restored.")

    def _title_saved(c, cid: int, what: str):
        """Re-render so the title appears - but only an unapproved, unpublished clip; an approved one is never silently changed."""
        if c["status"] == "ready" and not c["published_at"] and c["video_path"] and not c["local_deleted_at"]:
            db.update("clips", cid, render_rev=c["render_rev"] + 1, status="pending_render", reviewed_at=None)
            core.pipeline.enqueue_render(cid)
            return _go(f"/clips/{cid}", msg=f"{what}; re-rendering with it.")
        return _go(f"/clips/{cid}", msg=what + "." + (" Approved clips are not re-rendered automatically." if c["status"] == "approved" else ""))

    @app.post("/clips/{cid}/title")
    def clip_title(cid: int, title: str = Form("")):
        """Owner-written title (also the top title burned into the video where the brand's title style is on). Wording is
        only tidied (never rewritten). Owner titles are locked: reprocessing never overwrites them."""
        c = db.clip(cid)
        if not c:
            raise HTTPException(404)
        clean = titles.format_title(title, auto_emphasis=False)
        db.update("clips", cid, title=clean or None, title_source="owner" if clean else c["title_source"],
                  title_status="approved" if clean else "removed", title_locked=1 if clean else 0)
        return _title_saved(c, cid, "Title saved")

    @app.post("/clips/{cid}/title/choose")
    def clip_title_choose(cid: int, text: str = Form(...)):
        """Pick one of the engine's own (verified) alternatives; it stays a generated title until the owner approves it."""
        c = db.clip(cid)
        if not c:
            raise HTTPException(404)
        info = json.loads(c["title_json"] or "{}")
        offered = {x["text"] for x in info.get("candidates", []) if x.get("accepted")}
        if text not in offered:
            return _go(f"/clips/{cid}", err="That wording is not one of the verified suggestions.")
        db.update("clips", cid, title=text, title_status="proposed", title_source="generated")
        return _title_saved(c, cid, "Alternative chosen")

    @app.post("/clips/{cid}/title/approve")
    def clip_title_approve(cid: int, lock: str = Form("1")):
        c = db.clip(cid)
        if not c or not c["title"]:
            raise HTTPException(404)
        db.update("clips", cid, title_status="approved", title_locked=1 if lock == "1" else 0)
        return _go(f"/clips/{cid}", msg="Title approved and locked: reprocessing will not change it." if lock == "1" else "Title approved.")

    @app.post("/clips/{cid}/title/unlock")
    def clip_title_unlock(cid: int):
        db.update("clips", cid, title_locked=0)
        return _go(f"/clips/{cid}", msg="Title unlocked.")

    @app.post("/clips/{cid}/title/remove")
    def clip_title_remove(cid: int):
        c = db.clip(cid)
        if not c:
            raise HTTPException(404)
        db.update("clips", cid, title=None, title_status="removed", title_locked=1)
        return _title_saved(c, cid, "Title removed")

    @app.post("/clips/{cid}/title/regenerate")
    def clip_title_regenerate(cid: int):
        c = db.clip(cid)
        if not c:
            raise HTTPException(404)
        if c["title_locked"]:
            return _go(f"/clips/{cid}", err="This title is locked. Unlock it first to regenerate suggestions.")
        rec = db.recording(c["recording_id"])
        f = core.pipeline.hook_fields(rec, c["transcript_text"] or "", (c["title_rev"] or 0) + 1)
        db.update("clips", cid, **f)
        return _title_saved(db.clip(cid), cid, "New suggestions generated") if f["title"] else _go(
            f"/clips/{cid}", msg="No verified title could be produced from this passage; please enter one.")

    @app.post("/clips/{cid}/package")
    def clip_package(cid: int, platform: str = Form(...), account_id: str = Form("")):
        try:
            core.packages.prepare(cid, platform, int(account_id) if account_id.isdigit() else None)
        except (ValueError, pk.IsolationError) as e:
            return _go(f"/clips/{cid}", err=str(e))
        return _go(f"/clips/{cid}", msg=f"{pk.PLATFORMS[platform]['label']} package prepared — review the text, then approve.")

    @app.post("/packages/{pid}/edit")
    def package_edit(pid: int, title: str = Form(""), description: str = Form(""), hashtags: str = Form("")):
        pkg = db.one("SELECT clip_id FROM publication_packages WHERE id=?", (pid,))
        try:
            core.packages.edit(pid, title=title, description=description, hashtags=hashtags)
        except ValueError as e:
            return _go(f"/clips/{pkg['clip_id']}", err=str(e))
        return _go(f"/clips/{pkg['clip_id']}", msg="Package text saved (approval reset).")

    @app.post("/packages/{pid}/approve")
    def package_approve(pid: int):
        pkg = db.one("SELECT clip_id FROM publication_packages WHERE id=?", (pid,))
        try:
            core.packages.approve(pid)
        except ValueError as e:
            return _go(f"/clips/{pkg['clip_id']}", err=str(e))
        return _go(f"/clips/{pkg['clip_id']}", msg="Package approved. Nothing has been posted.")

    @app.post("/packages/{pid}/handoff", response_class=HTMLResponse)
    def package_handoff(request: Request, pid: int):
        pkg = db.one("SELECT clip_id FROM publication_packages WHERE id=?", (pid,))
        try:
            h = core.packages.handoff(pid)
        except ValueError as e:
            return _go(f"/clips/{pkg['clip_id']}", err=str(e))
        return page(request, "handoff.html", h=h, pid=pid, cid=pkg["clip_id"])

    @app.post("/packages/{pid}/youtube")
    def package_youtube(pid: int):
        pkg = db.one("SELECT clip_id FROM publication_packages WHERE id=?", (pid,))
        try:
            core.packages.request_youtube_private(pid)
        except ValueError as e:
            return _go(f"/clips/{pkg['clip_id']}", err=str(e))
        return _go(f"/clips/{pkg['clip_id']}", msg="Queued: private YouTube upload (you publish it in YouTube Studio).")

    @app.post("/packages/{pid}/published")
    def package_published(pid: int, url: str = Form("")):
        pkg = db.one("SELECT clip_id FROM publication_packages WHERE id=?", (pid,))
        core.packages.mark_published(pid, url)
        return _go(f"/clips/{pkg['clip_id']}", msg="Recorded as published by you.")

    # ---- analytics & commercial ---------------------------------------------------------------
    @app.get("/analytics", response_class=HTMLResponse)
    def analytics(request: Request, msg: str = "", err: str = ""):
        by_niche = db.query(
            "SELECT COALESCE(c.niche_key,'(none)') niche, COUNT(DISTINCT c.id) clips, "
            "SUM(c.status='approved') approved, SUM(c.published_at IS NOT NULL) published, "
            "ROUND(SUM(COALESCE(c.review_seconds,0))/60.0,1) review_min FROM clips c GROUP BY 1 ORDER BY 2 DESC")
        views = {r["niche"]: r for r in db.query(
            "SELECT COALESCE(niche_key,'(none)') niche, SUM(views) views, SUM(likes) likes, SUM(shares) shares, "
            "SUM(followers_delta) followers, SUM(incidents) incidents FROM metrics GROUP BY 1")}
        money = db.query("SELECT COALESCE(niche_key,'(none)') niche, model, kind, realised, currency, ROUND(SUM(amount),2) total "
                         "FROM commercial_entries GROUP BY 1,2,3,4,5 ORDER BY 1")
        realised = db.one("SELECT ROUND(SUM(CASE WHEN kind IN ('revenue','sponsor') THEN amount WHEN kind='cost' THEN -amount "
                          "ELSE 0 END),2) net FROM commercial_entries WHERE realised=1")["net"] or 0
        review_h = (db.one("SELECT SUM(review_seconds) s FROM clips")["s"] or 0) / 3600
        failures = db.one("SELECT COUNT(*) n FROM jobs WHERE status='failed' AND updated_at > ?", (time.time() - 30 * 86400,))["n"]
        return page(request, "analytics.html", by_niche=by_niche, views=views, money=money, realised=realised,
                    review_h=review_h, failures=failures, msg=msg, err=err,
                    per_hour=(realised / review_h) if review_h > 0.05 else None,
                    niches=db.query("SELECT key, name FROM niches ORDER BY name"),
                    scores=db.query("SELECT s.*, n.name FROM niche_scores s LEFT JOIN niches n ON n.key=s.key ORDER BY overall DESC"),
                    entries=db.query("SELECT * FROM commercial_entries ORDER BY date DESC, id DESC LIMIT 50"))

    @app.post("/analytics/entry")
    def analytics_entry(kind: str = Form(...), amount: float = Form(0.0), model: str = Form("B"), realised: str = Form("1"),
                        niche: str = Form(""), date: str = Form(""), note: str = Form(""), currency: str = Form("GBP")):
        if kind not in ("revenue", "cost", "affiliate_click", "conversion", "sponsor", "review_minutes"):
            return _go("/analytics", err="Unknown entry type.")
        db.insert("commercial_entries", kind=kind, amount=amount, model="A" if model == "A" else "B",
                  realised=1 if realised == "1" else 0, niche_key=niche or None, currency=currency[:3].upper(),
                  date=date or time.strftime("%Y-%m-%d"), note=note[:500])
        return _go("/analytics", msg="Entry recorded" + ("" if realised == "1" else " (as an ESTIMATE, shown separately)") + ".")

    @app.post("/analytics/import")
    async def analytics_import(file: UploadFile = File(...)):
        """CSV with columns: date, platform, clip_id, views, likes, comments, shares, watch_seconds, followers_delta,
        incidents (any subset after date). Values come from the platforms' own analytics exports."""
        raw = (await file.read()).decode("utf-8-sig", errors="replace")
        n = 0
        for row in csv.DictReader(io.StringIO(raw)):
            if not row.get("date"):
                continue
            cid = int(row["clip_id"]) if (row.get("clip_id") or "").isdigit() else None
            clip = db.clip(cid) if cid else None

            def num(k):
                try:
                    return int(float(row[k])) if row.get(k) not in (None, "") else None
                except ValueError:
                    return None
            db.insert("metrics", date=row["date"][:10], platform=(row.get("platform") or "")[:30] or None, clip_id=cid,
                      niche_key=clip["niche_key"] if clip else (row.get("niche") or None),
                      views=num("views"), likes=num("likes"), comments=num("comments"), shares=num("shares"),
                      watch_seconds=num("watch_seconds"), followers_delta=num("followers_delta"),
                      incidents=num("incidents"), source="csv")
            n += 1
        return _go("/analytics", msg=f"Imported {n} row(s).")

    @app.post("/analytics/score/{key}")
    async def analytics_score(request: Request, key: str):
        form = await request.form()
        try:
            scores = {k[2:]: float(v) for k, v in form.items() if k.startswith("s_") and str(v).strip()}
        except ValueError:
            return _go("/analytics", err="Scores must be numbers 0-10.")
        from ..research import combine_scores
        a, p, o = combine_scores(scores)
        row = db.one("SELECT key FROM niche_scores WHERE key=?", (key,))
        if row:
            db.execute("UPDATE niche_scores SET scores_json=?, audience_score=?, profit_score=?, overall=?, updated_at=? "
                       "WHERE key=?", (json.dumps(scores), a, p, o, time.time(), key))
        else:
            db.execute("INSERT INTO niche_scores(key, scores_json, audience_score, profit_score, overall, updated_at) "
                       "VALUES(?,?,?,?,?,?)", (key, json.dumps(scores), a, p, o, time.time()))
        return _go("/analytics", msg="Scorecard updated.")
