"""One lightweight supervisor that DISCOVERS new material across the whole portfolio.

Discovery is deliberately independent of acquisition:
  * it asks each platform's official API (or a public feed) what exists and who is live;
  * it records everything in SQLite (`monitor_sources`, `discovered_items`, `monitor_events`);
  * it hands an item to the EXISTING download queue only when acquisition is permitted for that host
    (recorded written permission / owner-declared domain), the item is not a duplicate, and the disk
    can hold it on top of what is already queued. Otherwise the item is kept as metadata
    (`permission_required`) and nothing is fetched;
  * it never starts a recorder or a heavy job itself, so the single-heavy-job queue and the existing
    live-recording priorities are untouched. Rumble keeps using the existing channel monitor: this
    supervisor only mirrors its state.

Failures are isolated per source (one platform failing never stops the others), backed off with jitter,
and persisted, so a crash, sleep or lost Wi-Fi resumes where it left off.
"""
from __future__ import annotations

import json
import logging
import random
import re
import threading
import time

from . import events, ingest, portfolio, recording_switch
from .platforms import AdapterError, ChannelRef, ERROR_CODES, adapter_for, parse_channel_url

log = logging.getLogger("clipengine.discovery")

YT_DAILY_UNITS = 8000          # of YouTube's default 10,000; the rest stays for owner/other tools
YT_UNITS_PER_CHECK = 3         # channels.list + playlistItems.list + videos.list
MAX_POLLS_PER_TICK = 8
MAX_PENDING_DOWNLOADS = 4      # discovery never floods the queue
SIMULCAST_WINDOW_S = 15 * 60
ACTIONABLE = ("upload", "vod", "episode")


def _norm_title(t: str) -> str:
    t = re.sub(r"\b(live|stream|ep(isode)?|part|#?\d+)\b", " ", (t or "").lower())
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


class DiscoverySupervisor:
    def __init__(self, settings, db, queue=None, adapters: dict | None = None, clock=time.time,
                 rng: random.Random | None = None, interval: float = 30.0):
        self.settings, self.db, self.queue = settings, db, queue
        self.adapters = adapters if adapters is not None else {}     # platform -> adapter (tests inject mocks)
        self.clock, self.rng, self.interval = clock, rng or random.Random(), interval
        self._stop = threading.Event()
        self._t: threading.Thread | None = None

    # ---- lifecycle (state lives in SQLite, so a restart simply carries on) ----------------------
    def start(self) -> None:
        portfolio.seed(self.db, self.settings)
        if not self.settings.workspace_id:
            events.seed_events(self.db)

        def loop():
            while not self._stop.wait(self.interval):
                try:
                    self.tick()
                except Exception:  # noqa: BLE001 - never let discovery die
                    log.exception("discovery tick failed")
        self._t = threading.Thread(target=loop, name="clipengine-discovery", daemon=True)
        self._t.start()

    def stop(self) -> None:
        self._stop.set()

    def adapter(self, platform: str):
        if platform not in self.adapters:
            self.adapters[platform] = adapter_for(self.settings, platform)
        return self.adapters[platform]

    # ---- one pass -----------------------------------------------------------------------------
    def tick(self) -> dict:
        now = self.clock()
        portfolio.reconcile(self.settings, self.db)
        out = {"polled": 0, "errors": 0, "routed": 0}
        due = self.db.query("SELECT * FROM monitor_sources WHERE monitoring_enabled=1 AND COALESCE(next_check,0)<=?",
                            (now,))
        # Known-live sources first, then imminent/elevated brands, then by configured priority.
        def rank(r):
            boost = 1 if events.active_events(self.db, now, r["brand_key"]) else 0
            return (r["live_state"] != "LIVE", r["verification"] != "VERIFIED", -boost, -r["priority"], r["next_check"] or 0)
        for ms in sorted(due, key=rank)[:MAX_POLLS_PER_TICK]:
            res = self.poll(ms["id"])
            out["polled"] += 1
            out["errors"] += 1 if res.get("error") else 0
        out["routed"] = self.route_pending()
        return out

    # ---- quota --------------------------------------------------------------------------------
    def _quota_key(self, platform: str, now: float) -> str:
        # YouTube quota resets at midnight Pacific; UTC-8 is a safe conservative day boundary.
        return f"quota:{platform}:{time.strftime('%Y-%m-%d', time.gmtime(now - 8 * 3600))}"

    def quota_used(self, platform: str = "youtube") -> int:
        return int(self.db.get_kv(self._quota_key(platform, self.clock()), "0") or 0)

    def _spend(self, platform: str, units: int) -> bool:
        key = self._quota_key(platform, self.clock())
        used = int(self.db.get_kv(key, "0") or 0)
        if used + units > YT_DAILY_UNITS:
            return False
        self.db.set_kv(key, str(used + units))
        return True

    # ---- polling one source -----------------------------------------------------------------
    def poll(self, ms_id: int) -> dict:
        ms = self.db.one("SELECT * FROM monitor_sources WHERE id=?", (ms_id,))
        if not ms or not ms["monitoring_enabled"]:
            return {}
        now = self.clock()
        try:
            if ms["verification"] != "VERIFIED":
                return self._try_verify(ms, now)
            if ms["platform"] == "rumble":
                return self._mirror_rumble(ms, now)
            ad = self.adapter(ms["platform"])
            if not ad.configured():
                raise AdapterError("needs_credentials", ERROR_CODES["needs_credentials"])
            if ms["platform"] == "youtube" and not self._spend("youtube", YT_UNITS_PER_CHECK):
                raise AdapterError("rate_limited", "YouTube daily quota budget reached; resuming after the reset")
            ref = ChannelRef(ms["platform"], ms["platform_id"] if ms["platform"] in ("youtube",) else
                             (ms["candidate_handle"] or ms["platform_id"] or ""), ms["canonical_url"] or "")
            if ms["platform"] == "podcast":
                ref = ChannelRef("podcast", ms["uid"], ms["canonical_url"])
            rep = ad.report(ref)
            return self._apply(ms, rep, now)
        except AdapterError as e:
            return self._fail(ms, e.code, str(e), now)
        except Exception as e:  # noqa: BLE001 - recorded and backed off, never fatal
            code = getattr(e, "code", "")
            code = "temporary_failure" if code in ("unresolvable",) else (code or "temporary_failure")
            return self._fail(ms, code, f"{type(e).__name__}: {e}", now)

    def _try_verify(self, ms, now) -> dict:
        res = portfolio.verify_source(self.settings, self.db, ms, self.adapters)
        fresh = self.db.one("SELECT * FROM monitor_sources WHERE id=?", (ms["id"],))
        wait = 30 * 60 if res.get("verified") is False and res.get("code") == "needs_credentials" else (
            6 * 3600 if res.get("verified") is False else 5)
        self.db.update("monitor_sources", ms["id"], next_check=now + wait)
        if res.get("verified"):
            portfolio.reconcile(self.settings, self.db)
        return {"verified": res.get("verified"), "state": fresh["state"]}

    def _mirror_rumble(self, ms, now) -> dict:
        """Rumble is monitored (and recorded, where permitted) by the existing channel monitor."""
        lg = self.db.one("SELECT * FROM sources WHERE id=?", (ms["legacy_source_id"],)) if ms["legacy_source_id"] else None
        if not lg:
            return self._fail(ms, "permission_missing", "No enabled Rumble channel source exists for this creator", now,
                              count=False)
        live = self.db.one("SELECT * FROM source_items WHERE source_id=? AND state IN ('recording','finalizing') "
                           "ORDER BY id DESC LIMIT 1", (lg["id"],))
        newest = self.db.one("SELECT item_key FROM source_items WHERE source_id=? AND kind='vod' ORDER BY id DESC LIMIT 1",
                             (lg["id"],))
        err = None if lg["status"] in ("ok", "new") else (lg["last_error"] or lg["status_detail"] or lg["status"])
        self._set_live(ms, "LIVE" if live else ("OFFLINE" if lg["last_success"] else "UNKNOWN"),
                       live["title"] if live else "", "", live["published"] if live else "", now)
        fields = dict(last_checked=lg["last_checked"] or now, next_check=now + 120,
                      latest_media_id=newest["item_key"] if newest else ms["latest_media_id"])
        if err:
            fields.update(error_code="blocked" if lg["status"] == "blocked" else "temporary_failure",
                          last_error=str(err)[:500], consecutive_errors=lg["consecutive_errors"])
        else:
            fields.update(error_code=None, last_error=None, consecutive_errors=0, last_success=lg["last_success"])
        self.db.update("monitor_sources", ms["id"], **fields)
        portfolio.reconcile(self.settings, self.db)
        return {"mirrored": True, "error": err}

    # ---- applying an adapter report ---------------------------------------------------------
    def _apply(self, ms, rep, now) -> dict:
        ident = rep.identity or {}
        if ms["platform_id"] and ident.get("id") and str(ident["id"]) != str(ms["platform_id"]) \
                and ms["platform"] != "podcast":
            # The handle now points at a different channel: never keep monitoring the wrong one.
            self.db.update("monitor_sources", ms["id"], verification="NEEDS_VERIFICATION", error_code="not_found",
                           last_error=f"Platform id changed ({ms['platform_id']} → {ident['id']}); re-verification needed",
                           state="NEEDS_VERIFICATION", next_check=now + 12 * 3600)
            portfolio.event(self.db, ms["id"], "error", "Platform id changed; source returned to NEEDS_VERIFICATION")
            return {"error": "identity changed"}
        was_error = ms["error_code"] not in (None, "")
        first = ms["last_success"] is None and not self.db.one(
            "SELECT 1 FROM discovered_items WHERE monitor_source_id=? LIMIT 1", (ms["id"],))
        live = rep.live
        live_state = "LIVE" if (live and live.live) else "OFFLINE"
        self._set_live(ms, live_state, live.title if live else "", live.category if live else "",
                       live.started_at if live else "", now, stream_url=(live.stream_url if live else ""))
        new = 0
        for it in rep.items:
            new += self._record_item(ms, it, first, live, now)
        # A live broadcast that has no listing entry (Twitch/Kick report live status only) is itself an item.
        if live and live.live and not any(i.kind == "live" for i in rep.items):
            new += self._record_item(ms, _live_item(ms, live), False, live, now)
        newest = next((i.key for i in sorted(rep.items, key=lambda i: i.published or "", reverse=True)
                       if i.kind in ACTIONABLE), ms["latest_media_id"])
        interval = events.interval_for(self.db, ms, now, live=live_state == "LIVE", imminent=events.is_imminent(rep.items, now))
        self.db.update("monitor_sources", ms["id"], last_checked=now, last_success=now, consecutive_errors=0,
                       error_code=None, last_error=None, latest_media_id=newest,
                       next_check=now + interval * self.rng.uniform(0.9, 1.1))
        if was_error:
            portfolio.event(self.db, ms["id"], "recovered", "Source recovered after errors")
        portfolio.reconcile(self.settings, self.db)
        return {"new": new, "live": live_state == "LIVE", "interval": interval}

    def _set_live(self, ms, state, title, category, since, now, stream_url="") -> None:
        if state != ms["live_state"]:
            portfolio.event(self.db, ms["id"], "live",
                            f"{ms['live_state']} → {state}" + (f": {title[:120]}" if title else ""))
        self.db.update("monitor_sources", ms["id"], live_state=state, live_title=title or None,
                       live_category=category or None, live_since=since or None)

    # ---- items, duplicates and routing ---------------------------------------------------------
    def _record_item(self, ms, it, first_poll, live, now) -> int:
        existing = self.db.one("SELECT * FROM discovered_items WHERE monitor_source_id=? AND platform_item_id=?",
                               (ms["id"], it.key))
        if existing:
            self.db.execute("UPDATE discovered_items SET last_seen=?, title=COALESCE(?,title), kind=? WHERE id=?",
                            (now, it.title or None, it.kind, existing["id"]))
            return 0
        category = (live.category if live and it.kind == "live" else "") or ""
        tags = events.tag_item(self.db, ms["brand_key"], now, it.title, category)
        state = "baseline" if (first_poll and it.kind in ACTIONABLE) else "discovered"
        dup_of, content_key = self._find_duplicate(ms, it, live, now) if state != "baseline" else (None, None)
        if dup_of:
            state = "duplicate"
        iid = self.db.insert("discovered_items", monitor_source_id=ms["id"], creator_id=ms["creator_id"],
                             brand_key=ms["brand_key"], platform=ms["platform"], platform_item_id=it.key, kind=it.kind,
                             title=it.title, url=it.url, published=it.published,
                             scheduled_start=it.published if it.kind == "upcoming" else None, category=category or None,
                             duration_s=it.duration_s, content_key=content_key, dup_of=dup_of, state=state,
                             priority_tags=",".join(tags) or None, first_seen=now, last_seen=now)
        if state != "baseline":
            portfolio.event(self.db, ms["id"], "duplicate" if dup_of else "discovered",
                            f"{it.kind}: {it.title[:100]}" + (f" [duplicate of item {dup_of}]" if dup_of else "")
                            + (f" [{','.join(tags)}]" if tags else ""))
        return 0 if state == "baseline" else 1

    def _find_duplicate(self, ms, it, live, now) -> tuple[int | None, str | None]:
        """Same show on another platform, same moment: ONE processing job. Identity is confirmed only
        when the sources share a duplicate group AND (start times within 15 min OR equal normalised
        titles); anything weaker is treated as distinct content."""
        if not ms["dup_group"]:
            return None, None
        mine_t = events.parse_ts((live.started_at if live and it.kind == "live" else it.published)) or now
        rows = self.db.query(
            "SELECT d.*, m.dup_group FROM discovered_items d JOIN monitor_sources m ON m.id=d.monitor_source_id "
            "WHERE m.dup_group=? AND d.monitor_source_id<>? AND d.kind=? AND d.state<>'duplicate' "
            "AND d.state<>'baseline' ORDER BY d.id", (ms["dup_group"], ms["id"], it.kind))
        for d in rows:
            other_t = events.parse_ts(d["published"]) or d["first_seen"]
            same_time = abs(other_t - mine_t) <= SIMULCAST_WINDOW_S
            same_title = bool(_norm_title(d["title"])) and _norm_title(d["title"]) == _norm_title(it.title)
            if (it.kind == "live" and same_time and d["last_seen"] >= now - 3 * 3600) or \
                    (it.kind in ACTIONABLE and same_title and abs(other_t - mine_t) <= 36 * 3600):
                return d["id"], d["content_key"] or f"{ms['dup_group']}#{d['id']}"
        return None, None

    # ---- routing eligible items to the existing queue (never starts heavy work itself) -------------
    def ensure_recorders(self) -> int:
        """For a source whose acquisition is PERMITTED, link (or create) the existing channel monitor row that
        records live shows. One recorder per show: simulcast copies of the same show share a duplicate
        group, and only its highest-priority source gets a recorder. Nothing is created without permission."""
        from . import monitor
        made = 0
        for ms in self.db.query("SELECT * FROM monitor_sources WHERE monitoring_enabled=1 AND verification='VERIFIED' "
                                "AND legacy_source_id IS NULL AND platform NOT IN ('podcast','rumble') "
                                "ORDER BY priority DESC, id"):
            caps = json.loads(ms["capabilities_json"] or "{}")
            if not caps.get("live") or portfolio.acquisition_for(self.settings, self.db, ms) != "PERMITTED":
                continue
            twin = self.db.one("SELECT 1 FROM monitor_sources WHERE dup_group=? AND id<>? AND legacy_source_id IS NOT NULL",
                               (ms["dup_group"], ms["id"]))
            if twin:
                continue
            creator = self.db.one("SELECT name FROM creators WHERE id=?", (ms["creator_id"],))
            sid, ok, msg = monitor.add_source(self.settings, self.db, name=f"{creator['name']} ({ms['platform']})",
                                              kind="channel", url=ms["canonical_url"], poll_minutes=5,
                                              authorisation=f"Portfolio source; {portfolio.acquisition_for(self.settings, self.db, ms)}")
            self.db.update("sources", sid, creator_id=ms["creator_id"], platform=ms["platform"])
            self.db.update("monitor_sources", ms["id"], legacy_source_id=sid)
            portfolio.event(self.db, ms["id"], "state", f"Linked recorder source {sid} ({'enabled' if ok else 'blocked: ' + msg[:80]})")
            made += 1
        return made

    def sync_job_states(self) -> int:
        """Bring queued items in line with their download jobs, so nothing stays 'queued' after the job finished
        or failed (also after a restart), and finished work stops counting against the disk reservation."""
        n = 0
        for d in self.db.query("SELECT * FROM discovered_items WHERE state='queued'"):
            job = self.db.one("SELECT status, error, recording_id FROM jobs WHERE id=?", (d["job_id"],)) if d["job_id"] else None
            if job is None:
                new, why = "discovered", "Download job no longer exists; eligible to be queued again"
            elif job["status"] == "done":
                new, why = "imported", (f"Downloaded into recording {job['recording_id']}" if job["recording_id"] else "Downloaded")
            elif job["status"] in ("failed", "cancelled"):
                new, why = "failed", (job["error"] or job["status"] or "")[:300]
            else:
                continue
            self.db.execute("UPDATE discovered_items SET state=?, detail=? WHERE id=?", (new, why, d["id"]))
            portfolio.event(self.db, d["monitor_source_id"], "discovered" if new != "failed" else "error",
                            f"item {d['id']} {new}: {(d['title'] or '')[:60]}")
            n += 1
        return n

    def retry_item(self, item_id: int) -> bool:
        """Owner action: a failed item becomes eligible again (permission and disk checks still apply)."""
        d = self.db.one("SELECT state FROM discovered_items WHERE id=?", (item_id,))
        if not d or d["state"] != "failed":
            return False
        self.db.execute("UPDATE discovered_items SET state='discovered', job_id=NULL, detail='Retry requested' WHERE id=?",
                        (item_id,))
        return True

    def route_pending(self) -> int:
        routed = 0
        if self.queue is None:
            return 0
        self.sync_job_states()
        self.ensure_recorders()
        if not recording_switch.is_enabled(self.db):
            return 0            # master switch OFF: discovery still records metadata, but no download is queued
        rows = self.db.query("SELECT d.*, m.canonical_url AS src_url, m.legacy_source_id, m.id AS msid, "
                             "m.platform AS plat FROM discovered_items d JOIN monitor_sources m ON m.id=d.monitor_source_id "
                             "WHERE d.state IN ('discovered','permission_required') AND d.kind IN ('upload','vod','episode') "
                             "ORDER BY (d.priority_tags IS NULL), d.first_seen LIMIT 25")
        for d in rows:
            ms = self.db.one("SELECT * FROM monitor_sources WHERE id=?", (d["msid"],))
            min_src = float(getattr(self.settings, "min_source_seconds", 0) or 0)
            if min_src and d["duration_s"] and d["duration_s"] < min_src:
                # Owner decision (19 Nov): videos this short are already clips (e.g. Shorts); do not download or re-clip them.
                self._mark(d, "skipped", f"Skipped: only {int(d['duration_s'])}s, under the {int(min_src)}s minimum "
                                         "(already a short clip)")
                continue
            if ms["platform"] in ("podcast", "rumble"):
                self._mark(d, "permission_required" if ms["platform"] == "podcast" else "discovered",
                           "Podcast enclosures are recorded, not auto-downloaded" if ms["platform"] == "podcast"
                           else "Rumble uploads are queued by the existing Rumble monitor")
                continue
            if portfolio.acquisition_for(self.settings, self.db, ms) != "PERMITTED":
                self._mark(d, "permission_required", "Discovered. Acquisition not permitted: record a written "
                                                     "permission for this platform to allow retrieval")
                continue
            if self._pending_downloads() >= MAX_PENDING_DOWNLOADS:
                self._mark(d, "discovered", "Held: download queue already has enough pending work")
                continue
            ok, why = self._disk_fits(d)
            if not ok:
                self._mark(d, "discovered", why)
                continue
            decision = ingest.classify_url(d["url"], self.settings, self.db)
            if not decision.ok:
                self._mark(d, "permission_required" if decision.code in ("restricted_platform", "permission_required")
                           else "failed", decision.message[:300])
                continue
            creator = self.db.one("SELECT name FROM creators WHERE id=?", (d["creator_id"],))
            jid = self.queue.enqueue("download_url", max_attempts=6,
                                     dedupe_key=f"download:{ingest.normalize_url(d['url'])}",
                                     payload={"url": d["url"], "title": d["title"] or None, "discovered_item_id": d["id"],
                                              "creator_id": d["creator_id"],
                                              "recorded_at": ingest.parse_recorded_at(d["published"]),
                                              "rights_attestation": f"Portfolio source '{creator['name'] if creator else ''}': "
                                                                    "acquisition permitted by recorded permission"})
            self.db.execute("UPDATE discovered_items SET state='queued', job_id=?, detail=NULL WHERE id=?", (jid, d["id"]))
            portfolio.event(self.db, d["monitor_source_id"], "discovered", f"queued download job {jid}: {d['title'][:80]}")
            routed += 1
        portfolio.reconcile(self.settings, self.db)
        return routed

    def _mark(self, d, state, detail) -> None:
        if d["state"] != state or d["detail"] != detail:
            self.db.execute("UPDATE discovered_items SET state=?, detail=? WHERE id=?", (state, detail, d["id"]))

    def _pending_downloads(self) -> int:
        return self.db.one("SELECT COUNT(*) n FROM jobs WHERE kind='download_url' AND status IN ('queued','running')")["n"]

    def _disk_fits(self, d) -> tuple[bool, str]:
        """Conservative: the item AND everything already queued must fit together (assume 2 h when unknown)."""
        from . import storage
        dur = d["duration_s"] or 2 * 3600
        queued = self.db.query("SELECT duration_s FROM discovered_items WHERE state='queued'")
        total = dur + sum((q["duration_s"] or 2 * 3600) for q in queued)
        est = storage.estimate(self.settings, total)
        if est["fits"]:
            return True, ""
        return False, (f"Held: not enough free disk for this and the {len(queued)} item(s) already queued "
                       f"(short by {est['shortfall'] / 1e9:.1f} GB). Metadata kept; nothing downloaded.")

    # ---- failures --------------------------------------------------------------------------------
    def _fail(self, ms, code: str, message: str, now: float, count: bool = True) -> dict:
        n = ms["consecutive_errors"] + (1 if count and code not in ("needs_credentials",) else 0)
        base = events.BASE_INTERVAL.get(ms["platform"], 600)
        if code == "needs_credentials":
            delay = 30 * 60                                  # a key appearing is picked up within 30 min
        elif code == "rate_limited":
            delay = max(30 * 60, min(6 * 3600, 1800 * 2 ** min(n - 1, 3)))
            if ms["platform"] == "youtube" and "quota" in message.lower():
                delay = max(delay, 3600)
        elif code == "unauthorised":
            delay = min(6 * 3600, 3600 * 2 ** min(n - 1, 2))
        elif code == "not_found":
            delay = 12 * 3600
        else:                                                # temporary_failure, offline, http_error ...
            delay = min(6 * 3600, base * 2 ** min(n, 6))
            if ms["live_state"] == "LIVE" or events.active_events(self.db, now, ms["brand_key"]):
                delay = min(delay, 10 * 60)                  # never go quiet for hours around a live show
        delay *= self.rng.uniform(0.85, 1.25)                # jitter: sources never retry in lock-step
        fields = dict(last_checked=now, next_check=now + delay, consecutive_errors=n, error_code=code,
                      last_error=message[:500])
        if code == "not_found":
            fields.update(verification="NEEDS_VERIFICATION")
        self.db.update("monitor_sources", ms["id"], **fields)
        if code != ms["error_code"]:
            portfolio.event(self.db, ms["id"], "error", f"{code}: {message[:200]}")
        portfolio.reconcile(self.settings, self.db)
        log.warning("monitor source %s: %s", ms["uid"], message)
        return {"error": code, "retry_in": int(delay)}


def _live_item(ms, live):
    from .platforms import MediaItem
    started = live.started_at or ""
    return MediaItem(key=f"{ms['platform']}:live:{started or 'now'}", title=live.title or "", url=live.stream_url or ms["canonical_url"] or "",
                     kind="live", published=started)
