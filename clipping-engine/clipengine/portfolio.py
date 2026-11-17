"""The owner-approved six-brand / eighteen-creator portfolio: registry, idempotent seeding, identity
verification and the operational state shown to the owner.

Principles
  * MONITORING (discovery) and ACQUISITION (permission to retrieve footage) are separate columns.
  * Nothing here invents a channel URL, handle, id or feed. `candidate_handle` is only a HINT used to
    ask the platform's own API who it belongs to; the canonical URL and stable id are written ONLY from
    the API's answer, and only when the official name matches. Otherwise the record stays
    NEEDS_VERIFICATION (and is not polled).
  * Re-seeding is safe: it adds what is missing, never duplicates, never re-enables a disabled source,
    and never overwrites a field the owner has changed (`owner_locked_json`).
"""
from __future__ import annotations

import json
import re
import time
from urllib.parse import urlparse

from . import ingest
from .platforms import (AdapterError, ChannelRef, ERROR_CODES, PodcastAdapter, adapter_for, parse_channel_url)

BRANDS = {
    "america_first_monitor": dict(
        name="America First Monitor", niche="politics",
        description="US right-wing political commentary and political-media monitoring.",
        note="Documentary / analytical / editorial-review workflow. Preserve political context, separate "
             "allegations from verified facts, flag hateful or extremist material for contextual human review; "
             "never convert extremist advocacy into promotional material."),
    "afterlive": dict(
        name="Afterlive", niche="gaming_streams",
        description="Gaming, livestream moments and GTA VI.",
        note="Complete initial roster is three creators; expansion is deliberately deferred by the owner. Flag "
             "potential spoiler / sensitive-scene material."),
    "operator_notes": dict(
        name="Operator Notes", niche="ai_business",
        description="Entrepreneurship, business, investment and management.",
        note="Prioritise official YouTube episodes and valid podcast feeds; livestream monitoring only where used."),
    "spec_edit": dict(
        name="The Spec Edit", niche="consumer_tech",
        description="Consumer technology, product reviews and hardware.",
        note="Official YouTube uploads, premieres and recurring livestreams."),
    "dial_study": dict(
        name="Dial Study", niche="watches",
        description="Luxury watches, watchmaking and collecting.",
        note="Official YouTube uploads and other verified long-form sources."),
    "open_floor": dict(
        name="Open Floor", niche="podcasts",
        description="Long-form podcasts, conversations and interviews.",
        note="Official YouTube channels and public podcast feeds. Platform-exclusive episodes without an authorised "
             "feed or API are recorded as requiring access, never circumvented."),
}

YT, TW, KK, RB, PC = "youtube", "twitch", "kick", "rumble", "podcast"


def _yt(handle, *names, primary=True, live=True):
    return dict(platform=YT, type="channel", handle=handle, names=list(names), primary=primary,
                caps=dict(uploads=True, premieres=True, live=live))


def _tw(login, *names, primary=False):
    return dict(platform=TW, type="channel", handle=login, names=list(names), primary=primary,
                caps=dict(uploads=True, live=True, schedule=True))


def _pod(*names, feed=None):
    return dict(platform=PC, type="podcast_feed", handle="", names=list(names), primary=False, feed_url=feed,
                caps=dict(podcast=True))


# `handle` values are HINTS to be confirmed through the official API (exact official-name match required).
CREATORS = [
    # ---- A. America First Monitor ------------------------------------------------------------
    dict(uid="nick-fuentes", name="Nick Fuentes", brand="america_first_monitor", kind="creator", sources=[
        dict(platform=RB, type="channel", handle="nickjfuentes", names=["Nick Fuentes"], primary=True,
             owner_url="https://rumble.com/c/nickjfuentes",   # configured by the owner and already live-tested for listing
             caps=dict(uploads=True, live=True))]),
    dict(uid="alex-jones", name="Alex Jones", brand="america_first_monitor", kind="creator", sources=[
        dict(platform=RB, type="channel", handle="", names=["Alex Jones"], primary=True,
             requirement="Owner: supply Alex Jones's official Rumble channel URL (not guessed here).",
             caps=dict(uploads=True, live=True))]),
    dict(uid="tim-dillon", name="Tim Dillon", brand="america_first_monitor", kind="creator", sources=[
        _yt("@TimDillonShow", "Tim Dillon", "The Tim Dillon Show"),
        _pod("The Tim Dillon Show")]),
    # ---- B. Afterlive (complete initial roster: exactly these three) ---------------------------
    dict(uid="ishowspeed", name="IShowSpeed", brand="afterlive", kind="creator", sources=[
        _yt("@IShowSpeed", "IShowSpeed", "iShowSpeed"),
        _tw("ishowspeed", "IShowSpeed", primary=False)]),
    dict(uid="kai-cenat", name="Kai Cenat", brand="afterlive", kind="creator", sources=[
        _tw("kaicenat", "KaiCenat", "Kai Cenat", primary=True),
        _yt("@KaiCenat", "Kai Cenat", "KaiCenat", primary=False)]),
    dict(uid="n3on", name="N3on", brand="afterlive", kind="creator", sources=[
        dict(platform=KK, type="channel", handle="n3on", names=["N3on", "n3on"], primary=True,
             caps=dict(live=True))]),
    # ---- C. Operator Notes ---------------------------------------------------------------------
    dict(uid="steven-bartlett", name="Steven Bartlett — The Diary of a CEO", brand="operator_notes", kind="show", sources=[
        _yt("@TheDiaryOfACEO", "The Diary Of A CEO", "Diary of a CEO"), _pod("The Diary Of A CEO with Steven Bartlett")]),
    dict(uid="alex-hormozi", name="Alex Hormozi", brand="operator_notes", kind="creator", sources=[
        _yt("@AlexHormozi", "Alex Hormozi"), _pod("The Game with Alex Hormozi")]),
    dict(uid="all-in-podcast", name="All-In Podcast", brand="operator_notes", kind="show", sources=[
        _yt("@allin", "All-In Podcast", "All-In"), _pod("All-In with Chamath, Jason, Sacks & Friedberg")]),
    # ---- D. The Spec Edit ----------------------------------------------------------------------
    dict(uid="mrwhosetheboss", name="Mrwhosetheboss", brand="spec_edit", kind="creator", sources=[
        _yt("@Mrwhosetheboss", "Mrwhosetheboss")]),
    dict(uid="mkbhd", name="MKBHD", brand="spec_edit", kind="creator", sources=[
        _yt("@mkbhd", "Marques Brownlee", "MKBHD")]),
    dict(uid="linus-tech-tips", name="Linus Tech Tips", brand="spec_edit", kind="creator", sources=[
        _yt("@LinusTechTips", "Linus Tech Tips")]),
    # ---- E. Dial Study -------------------------------------------------------------------------
    dict(uid="nico-leonard", name="Nico Leonard", brand="dial_study", kind="creator", sources=[
        _yt("@NicoLeonard", "Nico Leonard")]),
    dict(uid="teddy-baldassarre", name="Teddy Baldassarre", brand="dial_study", kind="creator", sources=[
        _yt("@TeddyBaldassarre", "Teddy Baldassarre")]),
    dict(uid="wristwatch-revival", name="Wristwatch Revival", brand="dial_study", kind="creator", sources=[
        _yt("@WristwatchRevival", "Wristwatch Revival")]),
    # ---- F. Open Floor -------------------------------------------------------------------------
    dict(uid="shawn-ryan-show", name="Shawn Ryan Show", brand="open_floor", kind="show", sources=[
        _yt("@ShawnRyanShow", "Shawn Ryan Show"), _pod("Shawn Ryan Show")]),
    dict(uid="joe-rogan-experience", name="Joe Rogan Experience", brand="open_floor", kind="show", sources=[
        _yt("@joerogan", "PowerfulJRE", "Joe Rogan"), _pod("The Joe Rogan Experience")]),
    dict(uid="modern-wisdom", name="Modern Wisdom — Chris Williamson", brand="open_floor", kind="show", sources=[
        _yt("@ChrisWillx", "Chris Williamson", "Modern Wisdom"), _pod("Modern Wisdom")]),
]

STATES = ("MONITORING", "OFFLINE", "NEEDS_CREDENTIALS", "NEEDS_VERIFICATION", "DISCOVERY_ONLY",
          "PERMISSION_REQUIRED", "ERROR", "DISABLED", "PENDING")

# What each state means on the dashboard (kept next to the code that computes them).
STATE_HELP = {
    "MONITORING": "Verified, checked successfully, and acquisition is permitted.",
    "DISCOVERY_ONLY": "Verified and checked successfully; discovery only (no acquisition permission needed or granted yet).",
    "PERMISSION_REQUIRED": "Discovery works and eligible material is waiting for an acquisition permission.",
    "NEEDS_CREDENTIALS": "A free developer key / app is required for this platform.",
    "NEEDS_VERIFICATION": "Identity not yet confirmed through an official source; not polled.",
    "OFFLINE": "The source could not be reached (site or channel unavailable).",
    "ERROR": "The last check failed; retrying with back-off.",
    "DISABLED": "Switched off by the owner; the seeder never re-enables it.",
    "PENDING": "Verified and enabled but not yet checked.",
}


def sid(c: dict, s: dict) -> str:
    return f"{c['uid']}/{s['platform']}/{s['type']}"


def _dup_group(c: dict) -> str:
    return f"show:{c['uid']}"


def _now() -> float:
    return time.time()


def seed(db, settings=None) -> dict:
    """Idempotent. Returns counts. Safe to run on every start."""
    t = _now()
    out = {"brands": 0, "creators_added": 0, "creators_adopted": 0, "sources_added": 0, "sources_updated": 0}
    for key, b in BRANDS.items():
        row = db.one("SELECT * FROM brands WHERE key=?", (key,))
        if not row:
            db.execute("INSERT INTO brands(key,name,niche_key,description,editorial_note,created_at,updated_at) "
                       "VALUES(?,?,?,?,?,?,?)", (key, b["name"], b["niche"], b["description"], b["note"], t, t))
            out["brands"] += 1
    for c in CREATORS:
        row = db.one("SELECT * FROM creators WHERE uid=?", (c["uid"],))
        if not row:
            for s in c["sources"]:                       # adopt an existing creator the owner already has
                url = s.get("owner_url")
                if url:
                    row = db.one("SELECT * FROM creators WHERE url=?", (url,))
                    if row:
                        break
            if not row:
                row = db.one("SELECT * FROM creators WHERE lower(name)=lower(?) AND status<>'research'", (c["name"],))
            if row:
                out["creators_adopted"] += 1
        if not row:
            cid = db.insert("creators", name=c["name"], status="needs_authorisation", priority=50, permission_json="{}",
                            niches_json=json.dumps([BRANDS[c["brand"]]["niche"]]), uid=c["uid"], brand_key=c["brand"],
                            kind=c["kind"], roster="approved", acquisition_state="PERMISSION_REQUIRED")
            out["creators_added"] += 1
        else:
            cid = row["id"]
            fill = {}
            for col, val in (("uid", c["uid"]), ("brand_key", c["brand"]), ("kind", c["kind"]), ("roster", "approved")):
                if not row[col]:                         # never overwrite an owner/previous value
                    fill[col] = val
            if not row["acquisition_state"]:
                fill["acquisition_state"] = "PERMISSION_REQUIRED"
            if not row["niches_json"] or row["niches_json"] in ("[]", "null"):
                fill["niches_json"] = json.dumps([BRANDS[c["brand"]]["niche"]])
            if fill:
                db.update("creators", cid, **fill)
        for s in c["sources"]:
            uid = sid(c, s)
            existing = db.one("SELECT * FROM monitor_sources WHERE uid=?", (uid,))
            caps = json.dumps(s["caps"])
            if not existing:
                canonical = s.get("owner_url")
                legacy = None
                if canonical:
                    lg = db.one("SELECT * FROM sources WHERE url=?", (canonical,))
                    legacy = lg["id"] if lg else None
                requirement = s.get("requirement") or ""
                db.insert("monitor_sources", uid=uid, creator_id=cid, brand_key=c["brand"], platform=s["platform"],
                          source_type=s["type"], candidate_handle=s.get("handle") or None,
                          canonical_url=None, capabilities_json=caps, priority=60 if s.get("primary") else 50,
                          dup_group=_dup_group(c), legacy_source_id=legacy,
                          requirement=requirement, state="NEEDS_VERIFICATION",
                          evidence=(f"Candidate from owner-configured source {canonical}" if canonical else None),
                          owner_locked_json="[]", next_check=0,
                          channel_name=None)
                if canonical:
                    db.execute("UPDATE monitor_sources SET canonical_url=? WHERE uid=?", (canonical, uid))
                out["sources_added"] += 1
            else:
                locked = set(json.loads(existing["owner_locked_json"] or "[]"))
                upd = {}
                if "capabilities_json" not in locked and existing["capabilities_json"] != caps:
                    upd["capabilities_json"] = caps
                if "candidate_handle" not in locked and not existing["candidate_handle"] and s.get("handle"):
                    upd["candidate_handle"] = s["handle"]
                if "dup_group" not in locked and not existing["dup_group"]:
                    upd["dup_group"] = _dup_group(c)
                if not existing["legacy_source_id"] and existing["canonical_url"]:
                    lg = db.one("SELECT id FROM sources WHERE url=?", (existing["canonical_url"],))
                    if lg:                                   # a channel source added since (e.g. on the Sources page)
                        upd["legacy_source_id"] = lg["id"]
                if upd:
                    db.update("monitor_sources", existing["id"], **upd)
                    out["sources_updated"] += 1
                # monitoring_enabled, acquisition, verification, priority and runtime state are NEVER touched here.
    out["account_slots"] = seed_account_slots(db)
    if db.get_kv("title_brands") is None:          # first run only: the approved title style for America First Monitor
        db.set_kv("title_brands", json.dumps(["america_first_monitor"]))
    return out


# ---- owner edits (locked against the seeder) ---------------------------------------------------

EDITABLE = ("monitoring_enabled", "canonical_url", "candidate_handle", "priority", "dup_group", "requirement", "notes")


def owner_set(db, ms_id: int, **fields) -> None:
    row = db.one("SELECT * FROM monitor_sources WHERE id=?", (ms_id,))
    if not row:
        raise KeyError(ms_id)
    bad = set(fields) - set(EDITABLE)
    if bad:
        raise ValueError(f"not editable: {sorted(bad)}")
    locked = set(json.loads(row["owner_locked_json"] or "[]")) | set(fields)
    if "canonical_url" in fields or "candidate_handle" in fields:
        fields.setdefault("verification", "NEEDS_VERIFICATION")     # changed identity must be re-verified
        fields.setdefault("platform_id", None)
        fields.setdefault("state", "NEEDS_VERIFICATION")
        fields.setdefault("requirement", None)
    db.update("monitor_sources", ms_id, owner_locked_json=json.dumps(sorted(locked)), **fields)
    event(db, ms_id, "state", "Owner changed " + ", ".join(sorted(k for k in fields if k in EDITABLE)))


PLATFORM_HOSTS = {"youtube": ("youtube.com", "youtu.be"), "twitch": ("twitch.tv",), "kick": ("kick.com",),
                  "rumble": ("rumble.com",)}


def edit_source(db, ms_id: int, *, handle: str | None = None, url: str | None = None, priority=None,
                notes: str | None = None, enabled=None) -> list[str]:
    """Validated owner edit from the dashboard. Goes through `owner_set` (locks the fields, and an identity change
    forces re-verification). It can never mark an identity verified, grant permission or start acquisition."""
    ms = db.one("SELECT * FROM monitor_sources WHERE id=?", (ms_id,))
    if not ms:
        raise KeyError(ms_id)
    changes = {}
    if handle is not None and handle.strip() != (ms["candidate_handle"] or ""):
        h = handle.strip()
        if h and not re.fullmatch(r"[@A-Za-z0-9_.\-]{1,100}", h):
            raise ValueError("Handle may only contain letters, digits and @ _ . -")
        changes["candidate_handle"] = h or None
    if url is not None and url.strip() != (ms["canonical_url"] or ""):
        u = url.strip()
        if u:
            p = urlparse(u)
            host = (p.hostname or "").lower().removeprefix("www.")
            if p.scheme not in ("http", "https") or not host or p.username or p.password:
                raise ValueError("Enter a full https:// address without a login in it.")
            allowed = PLATFORM_HOSTS.get(ms["platform"])
            if allowed and not any(host == a or host.endswith("." + a) for a in allowed):
                raise ValueError(f"A {ms['platform']} source must use a {allowed[0]} address.")
        changes["canonical_url"] = u or None
    if priority not in (None, ""):
        pr = int(priority)
        if not 0 <= pr <= 100:
            raise ValueError("Priority must be between 0 and 100.")
        if pr != ms["priority"]:
            changes["priority"] = pr
    if notes is not None and notes.strip() != (ms["notes"] or ""):
        changes["notes"] = notes.strip()[:500] or None
    if enabled is not None and int(bool(enabled)) != ms["monitoring_enabled"]:
        changes["monitoring_enabled"] = int(bool(enabled))
    if changes:
        owner_set(db, ms_id, **changes)
    return sorted(changes)


def event(db, ms_id: int | None, kind: str, message: str) -> None:
    db.insert("monitor_events", ts=_now(), monitor_source_id=ms_id, kind=kind, message=message[:500])


# ---- identity verification ---------------------------------------------------------------------

def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _names_for(uid: str, platform: str, stype: str) -> list[str]:
    for c in CREATORS:
        if c["uid"] == uid:
            for s in c["sources"]:
                if s["platform"] == platform and s["type"] == stype:
                    return s["names"]
    return []


def verify_source(settings, db, ms, adapters: dict | None = None) -> dict:
    """Resolve one source's identity through its official API and persist the result.

    VERIFIED requires: the platform answered, the official name equals an expected name, and (for Twitch/
    Kick) the platform's own verification signal where it exists. Twitch/Kick name matches without a
    platform signal stay NEEDS_VERIFICATION with the resolved id stored for the owner to confirm."""
    adapters = adapters if adapters is not None else {}
    names = [_norm(n) for n in _names_for(ms["uid"].split("/")[0], ms["platform"], ms["source_type"])]
    ad = adapters.get(ms["platform"]) or adapter_for(settings, ms["platform"])
    try:
        if ms["platform"] == PC:
            url = ms["canonical_url"]
            if not url:
                found = ad.find_feed(_names_for(ms["uid"].split("/")[0], PC, "podcast_feed"))
                url = found["feed_url"]
                ident = ad.identity(ChannelRef("podcast", ms["uid"], url))
                ok = _norm(ident.get("name")) in names or _norm(found["title"]) in names
                evidence = found["evidence"]
            else:
                ident = ad.identity(ChannelRef("podcast", ms["uid"], url))
                ok = _norm(ident.get("name")) in names
                evidence = f"Feed title '{ident.get('name')}' matches the expected show name"
            return _persist_identity(db, ms, ok, ident, evidence, url_override=url, id_=ident.get("id"))
        if ms["platform"] == RB:
            url = ms["canonical_url"]
            if not url:
                return _needs(db, ms, "NEEDS_VERIFICATION", None, ms["requirement"] or "Owner: supply the channel URL.")
            ref = parse_channel_url(url)
            if not ref:
                return _needs(db, ms, "NEEDS_VERIFICATION", "invalid_identity", "The Rumble URL is not a channel address.")
            legacy = db.one("SELECT * FROM sources WHERE id=?", (ms["legacy_source_id"],)) if ms["legacy_source_id"] else None
            ident = {"id": ref.handle, "name": ref.handle, "url": ref.url}
            if legacy and legacy["last_success"]:
                ev = (f"Existing Rumble monitor listed this channel successfully on "
                      f"{time.strftime('%Y-%m-%d', time.localtime(legacy['last_success']))} (owner-configured URL)")
                return _persist_identity(db, ms, True, ident, ev, url_override=ref.url, id_=ref.handle)
            return _needs(db, ms, "NEEDS_VERIFICATION", None,
                          "Rumble has no identity API: the channel is verified by the first successful listing "
                          "(the existing Rumble monitor).")
        handle = ms["candidate_handle"] or (parse_channel_url(ms["canonical_url"] or "") or ChannelRef("", "", "")).handle
        if not handle:
            return _needs(db, ms, "NEEDS_VERIFICATION", None, ms["requirement"] or "No handle to look up.")
        ident = ad.identity(ChannelRef(ms["platform"], handle, ""))
        if ms["platform"] == YT:
            ok = _norm(ident.get("name")) in names
            ev = f"YouTube Data API channels.list forHandle={handle}: official title '{ident.get('name')}' matches"
        else:
            ok = _norm(ident.get("name")) in names and bool(ident.get("platform_verified"))
            ev = (f"{ms['platform']} API resolved '{ident.get('name')}' (id {ident.get('id')}); "
                  + ("platform-verified partner account" if ident.get("platform_verified")
                     else "no platform verification signal, so the owner must confirm this is the official account"))
        return _persist_identity(db, ms, ok, ident, ev, id_=str(ident.get("id")))
    except AdapterError as e:
        return _needs(db, ms, e.code, e.code, str(e))


def _needs(db, ms, state_or_code, code, message) -> dict:
    state = {"needs_credentials": "NEEDS_CREDENTIALS"}.get(state_or_code, "NEEDS_VERIFICATION")
    if code in ("temporary_failure", "rate_limited"):
        state = ms["state"] if ms["state"] != "NEEDS_VERIFICATION" else "NEEDS_VERIFICATION"
    db.update("monitor_sources", ms["id"], state=state, error_code=code, last_error=message[:500],
              requirement=message[:500] if code in (None, "needs_credentials", "not_found") else ms["requirement"],
              last_checked=_now())
    event(db, ms["id"], "state", f"Verification not completed: {message[:200]}")
    return {"id": ms["id"], "verified": False, "code": code, "message": message}


def _persist_identity(db, ms, ok, ident, evidence, url_override=None, id_=None) -> dict:
    url = url_override or ident.get("url")
    fields = dict(platform_id=id_ or ms["platform_id"], channel_name=ident.get("name"), last_checked=_now(),
                  error_code=None, last_error=None, evidence=evidence[:500])
    locked = set(json.loads(ms["owner_locked_json"] or "[]"))
    if "canonical_url" not in locked or not ms["canonical_url"]:
        fields["canonical_url"] = url
    if ok:
        fields.update(verification="VERIFIED", verified_at=_now(), state="PENDING", requirement=None)
    else:
        fields.update(verification="NEEDS_VERIFICATION", state="NEEDS_VERIFICATION",
                      requirement="Resolved by the platform but not confirmed as the official account; "
                                  "owner confirmation needed.")
    db.update("monitor_sources", ms["id"], **fields)
    event(db, ms["id"], "state", ("Identity VERIFIED: " if ok else "Identity resolved, NOT confirmed: ") + evidence[:200])
    return {"id": ms["id"], "verified": ok, "evidence": evidence}


def confirm_identity(db, ms_id: int, note: str) -> None:
    """Owner confirms a resolved-but-unconfirmed account is the official one (recorded as owner evidence)."""
    ms = db.one("SELECT * FROM monitor_sources WHERE id=?", (ms_id,))
    if not ms or not ms["platform_id"]:
        raise ValueError("Only an identity the platform has already resolved can be confirmed.")
    db.update("monitor_sources", ms_id, verification="VERIFIED", verified_at=_now(), state="PENDING", requirement=None,
              evidence=(f"Owner confirmed: {note[:200]} | " + (ms["evidence"] or ""))[:500])
    event(db, ms_id, "state", "Owner confirmed the resolved account is official")


def verify_all(settings, db, adapters: dict | None = None) -> list[dict]:
    out = []
    for ms in db.query("SELECT * FROM monitor_sources WHERE verification<>'VERIFIED' ORDER BY id"):
        out.append(verify_source(settings, db, ms, adapters))
    return out


# ---- acquisition (separate from monitoring) -----------------------------------------------------

def creator_permitted(row) -> bool:
    """A written permission recorded FOR THIS CREATOR (who granted it, and a reference), not expired.
    Permission for one creator never extends to another, and a platform-wide permission alone is not enough."""
    try:
        p = json.loads((row["permission_json"] if row else None) or "{}")
    except (TypeError, ValueError):
        return False
    if not (p.get("granted_by") and p.get("reference")):
        return False
    exp = p.get("expires")
    if exp:
        try:
            if time.strptime(str(exp)[:10], "%Y-%m-%d") < time.localtime():
                return False
        except ValueError:
            return False
    return True


def acquisition_for(settings, db, ms) -> str:
    """PERMITTED only when BOTH hold: the existing host rules allow retrieval (recorded written permission for the
    platform, or an owner-declared domain) AND this creator's own permission is on record. An enabled legacy
    Rumble channel source counts as the owner's explicit decision for that channel (existing behaviour kept).
    Discovery never changes this."""
    url = ms["canonical_url"]
    if not url or ms["platform"] == PC:
        return "PERMISSION_REQUIRED"          # enclosures are recorded, never fetched automatically
    host = (urlparse(url).hostname or "").lower()
    restricted = ingest.restricted_reason(host)
    perm = ingest.permission_for(db, host)
    host_ok = bool(perm) or (not restricted and any(ingest.host_matches(host, d) for d in settings.allowed_url_domains))
    if not host_ok:
        return "PERMISSION_REQUIRED"
    creator = db.one("SELECT permission_json FROM creators WHERE id=?", (ms["creator_id"],))
    if creator_permitted(creator):
        return "PERMITTED"
    if ms["platform"] == RB and ms["legacy_source_id"]:
        lg = db.one("SELECT enabled FROM sources WHERE id=?", (ms["legacy_source_id"],))
        if lg and lg["enabled"]:
            return "PERMITTED"
    return "PERMISSION_REQUIRED"


def link_rumble_source(settings, db, ms_id: int) -> int:
    """Create (DISABLED, not recording) the existing-engine channel source for a Rumble candidate so the existing
    Rumble monitor can be switched on from the Sources page. Never enables anything: that needs Rumble's written
    permission and the creator's own permission."""
    ms = db.one("SELECT * FROM monitor_sources WHERE id=?", (ms_id,))
    if not ms or ms["platform"] != RB or not ms["canonical_url"]:
        raise ValueError("Only a Rumble source with a channel URL can be linked.")
    if ms["legacy_source_id"]:
        return ms["legacy_source_id"]
    lg = db.one("SELECT id FROM sources WHERE url=?", (ms["canonical_url"],))
    if lg:
        db.update("monitor_sources", ms_id, legacy_source_id=lg["id"])
        return lg["id"]
    creator = db.one("SELECT name FROM creators WHERE id=?", (ms["creator_id"],))
    sid_ = db.insert("sources", name=f"{creator['name']} (Rumble)", kind="channel", url=ms["canonical_url"], enabled=0,
                     poll_minutes=5, record_live=0, backfill=0, creator_id=ms["creator_id"], platform="rumble",
                     authorisation="", status="disabled", next_check=0,
                     status_detail="Added from the Portfolio, switched off. Enable it on the Sources page once Rumble's "
                                   "written permission and this creator's permission are recorded.")
    db.update("monitor_sources", ms_id, legacy_source_id=sid_)
    event(db, ms_id, "state", f"Linked disabled Rumble source {sid_} (acquisition not enabled)")
    return sid_


# ---- publishing-account registry: six brands x four platforms --------------------------------------------

ACCOUNT_PLATFORMS = ("youtube", "tiktok", "instagram", "facebook")


def seed_account_slots(db) -> int:
    """One clearly-labelled placeholder per brand and platform (24). Nothing is created on any platform; the owner
    records the real account with `set_account`. Each slot carries only its own brand's niche, so clips from
    one brand can never be packaged for another brand's account."""
    n = 0
    for bk, b in BRANDS.items():
        for plat in ACCOUNT_PLATFORMS:
            ph = f"PENDING-{bk}-{plat}"
            if db.one("SELECT 1 FROM social_accounts WHERE brand=? AND platform=?", (bk, plat)):
                continue
            db.insert("social_accounts", platform=plat, handle=ph, display_name=f"{b['name']} ({plat}) - not yet created",
                      brand=bk, niches_json=json.dumps([b["niche"]]), status="awaiting_connection",
                      notes="Placeholder. Record the official account with the Accounts page; nothing is created for you.")
            n += 1
    return n


def set_account(db, brand: str, platform: str, handle: str, display_name: str = "", account_id: str = "") -> int:
    """Record the owner's real account for a brand slot. Refuses an account already registered to another brand."""
    if brand not in BRANDS or platform not in ACCOUNT_PLATFORMS:
        raise ValueError("Unknown brand or platform.")
    handle = handle.strip()
    if not handle or handle.startswith("PENDING-"):
        raise ValueError("Enter the real account handle.")
    other = db.one("SELECT brand FROM social_accounts WHERE platform=? AND lower(handle)=lower(?) AND COALESCE(brand,'')<>?",
                   (platform, handle, brand))
    if other:
        raise ValueError(f"{platform} @{handle} already belongs to brand '{other['brand']}'; brands never share an account.")
    slot = db.one("SELECT * FROM social_accounts WHERE brand=? AND platform=? ORDER BY (handle LIKE 'PENDING-%') DESC, id LIMIT 1",
                  (brand, platform))
    fields = dict(handle=handle, display_name=display_name or handle, status="awaiting_connection",
                  notes=(f"Account id: {account_id}. " if account_id else "") + "Registered by the owner; connection still needs authorisation.")
    if slot:
        db.update("social_accounts", slot["id"], **fields)
        return slot["id"]
    return db.insert("social_accounts", platform=platform, brand=brand, niches_json=json.dumps([BRANDS[brand]["niche"]]), **fields)


# ---- operational state (what is actually true, not what is configured) --------------------------

def compute_state(settings, db, ms) -> str:
    if not ms["monitoring_enabled"]:
        return "DISABLED"
    if ms["verification"] != "VERIFIED":
        return "NEEDS_CREDENTIALS" if ms["error_code"] == "needs_credentials" else "NEEDS_VERIFICATION"
    code = ms["error_code"]
    if code == "needs_credentials":
        return "NEEDS_CREDENTIALS"
    if code in ("offline",):
        return "OFFLINE"
    if code:
        return "ERROR"
    if not ms["last_success"]:
        ad = adapter_for(settings, ms["platform"])
        return "PENDING" if ad.configured() else "NEEDS_CREDENTIALS"
    acq = acquisition_for(settings, db, ms)
    if acq == "PERMITTED":
        return "MONITORING"
    waiting = db.one("SELECT 1 FROM discovered_items WHERE monitor_source_id=? AND state='permission_required' LIMIT 1",
                     (ms["id"],))
    return "PERMISSION_REQUIRED" if waiting else "DISCOVERY_ONLY"


def reconcile(settings, db) -> int:
    """Recompute every source's stored state/acquisition from evidence. Idempotent, read-mostly."""
    n = 0
    for ms in db.query("SELECT * FROM monitor_sources"):
        acq = acquisition_for(settings, db, ms)
        st = compute_state(settings, db, ms)
        if (acq, st) != (ms["acquisition"], ms["state"]):
            db.update("monitor_sources", ms["id"], acquisition=acq, state=st)
            if st != ms["state"]:
                event(db, ms["id"], "state", f"{ms['state']} → {st}")
            n += 1
        db.execute("UPDATE creators SET acquisition_state=? WHERE id=?", (acq, ms["creator_id"]))
    return n


def summary(settings, db, persist: bool = True) -> list[dict]:
    if persist:
        reconcile(settings, db)
    rows = db.query("SELECT m.*, c.name AS creator, c.uid AS creator_uid FROM monitor_sources m "
                    "JOIN creators c ON c.id=m.creator_id ORDER BY m.brand_key, c.name, m.priority DESC, m.platform")
    out = [dict(r) for r in rows]
    if not persist:                      # read-only diagnostics: recompute from evidence, write nothing
        for r, ms in zip(out, rows):
            r["acquisition"] = acquisition_for(settings, db, ms)
            r["state"] = compute_state(settings, db, ms)
    return out
