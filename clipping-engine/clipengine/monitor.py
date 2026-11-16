"""Authorised source monitoring and live recording.

Watches configured sources and feeds new recordings into the EXISTING pipeline:
  * feed    — RSS/Atom feed (e.g. a council, parliament or podcast feed) whose items carry
              video enclosures/links. New items → existing `download_url` job.
  * channel — a channel/page listed by yt-dlp (only for domains in ALLOWED_URL_DOMAINS or with a
              recorded written permission). New uploads → `download_url` job; a live entry → recorder.
  * hls     — a direct HLS (.m3u8) live stream. When it goes live it is recorded with FFmpeg.

Live recordings are written as MPEG-TS segments (robust to interruption). If the recorder dies
while the stream is still live, a new segment starts (bounded restarts). When the stream ends,
segments are joined into one MP4, integrity-checked, and handed to the EXISTING inbox importer
with a `.source.json` provenance sidecar, so processing starts automatically.

Authorisation: monitoring is recurring automated access, so a source is only enabled when its
host is in ALLOWED_URL_DOMAINS or a written permission is recorded for it, the host resolves to a
public address, and (for restricted platforms such as Rumble) the platform's own written
permission is on record. Nothing passes cookies, logs in, or bypasses DRM/paywalls/rate limits.
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import shutil
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urljoin, urlparse

from . import ingest, media
from .config import Settings
from .db import Database

log = logging.getLogger("clipengine.monitor")

KINDS = ("feed", "channel", "hls")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
MIN_POLL_MINUTES = {"channel": 5}        # a channel listing is a full page fetch; be a polite client
LIVE_STATES = ("recording", "finalizing")
MEDIA_NS = "{http://search.yahoo.com/mrss/}"
ATOM_NS = "{http://www.w3.org/2005/Atom}"


# ---- authorisation ------------------------------------------------------------

def check_source_allowed(settings: Settings, db: Database, url: str) -> tuple[bool, str]:
    """Is recurring automated retrieval from this URL authorised by configuration?"""
    p = urlparse((url or "").strip())
    host = (p.hostname or "").lower()
    if not host or p.scheme not in ("http", "https"):
        return False, "Enter a full http(s) address."
    restricted = ingest.restricted_reason(host)
    perm = ingest.permission_for(db, host)
    if restricted and not perm:
        dom, reason = restricted
        return False, (f"Monitoring {dom} is disabled: {reason} To enable it, record the platform's written "
                       "permission in Settings → Source permissions (who granted it, date, reference). Until then, "
                       "save individual episode links as pending sources and attach files you are authorised to use.")
    try:
        ingest.check_network_target(url, settings)
    except ingest.SourceError as e:
        return False, str(e)
    if perm:
        return True, f"Authorised by recorded written permission ({perm['granted_by']}, ref {perm['reference']})."
    if any(ingest.host_matches(host, d) for d in settings.allowed_url_domains):
        return True, f"{host} is in ALLOWED_URL_DOMAINS (owner-declared as permitting automated retrieval)."
    return False, (f"{host} is not authorised for automated monitoring. Add it to ALLOWED_URL_DOMAINS in .env only "
                   "if its terms and the rights holder permit automated downloading, or record a written permission "
                   "in Settings → Source permissions.")


def add_source(settings: Settings, db: Database, *, name: str, kind: str, url: str, poll_minutes: int = 15,
               record_live: bool = True, backfill: int = 0, authorisation: str = "") -> tuple[int, bool, str]:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    ok, msg = check_source_allowed(settings, db, url)
    sid = db.insert("sources", name=name.strip()[:200] or url, kind=kind, url=url.strip(),
                    enabled=1 if ok else 0, poll_minutes=max(MIN_POLL_MINUTES.get(kind, 2), int(poll_minutes)),
                    record_live=int(record_live),
                    backfill=max(0, int(backfill)), authorisation=authorisation.strip()[:1000],
                    status="new" if ok else "blocked", status_detail=msg, next_check=0)
    return sid, ok, msg


def set_enabled(settings: Settings, db: Database, sid: int, enabled: bool) -> tuple[bool, str]:
    src = db.one("SELECT * FROM sources WHERE id = ?", (sid,))
    if not src:
        return False, "Source not found."
    if enabled:
        ok, msg = check_source_allowed(settings, db, src["url"])
        if not ok:
            db.update("sources", sid, enabled=0, status="blocked", status_detail=msg)
            return False, msg
        db.update("sources", sid, enabled=1, status="ok" if src["last_success"] else "new", status_detail=msg,
                  next_check=0, consecutive_errors=0)
        return True, msg
    db.update("sources", sid, enabled=0, status="disabled", status_detail="Disabled by owner.")
    return True, "Disabled."


# ---- small safe HTTP fetch -----------------------------------------------------

def fetch_text(settings: Settings, url: str, limit: int = 5_000_000, timeout: float = 20.0) -> tuple[int, str, str]:
    """GET with SSRF checks on every redirect hop. Returns (status, final_url, text)."""
    import httpx
    current = url
    with httpx.Client(timeout=timeout, follow_redirects=False,
                      headers={"User-Agent": settings.http_user_agent}) as c:
        for _ in range(6):
            ingest.check_network_target(current, settings)
            with c.stream("GET", current) as r:
                if r.status_code in (301, 302, 303, 307, 308):
                    current = urljoin(current, r.headers.get("location", ""))
                    continue
                if not settings.allow_private_urls and not ingest._peer_is_public(r):
                    raise ingest.SourceError("private_address", "Connection reached a private address; refused.")
                data = b""
                for chunk in r.iter_bytes():
                    data += chunk
                    if len(data) > limit:
                        raise ingest.SourceError("too_large", "Response too large for a feed/playlist.")
                return r.status_code, current, data.decode(r.encoding or "utf-8", errors="replace")
    raise ingest.SourceError("too_many_redirects", "Too many redirects.")


# ---- feed / channel / hls listing ------------------------------------------------

def parse_feed(text: str, base_url: str) -> list[dict]:
    """RSS 2.0 / Atom → [{key, title, url, published}] for items that carry a video."""
    # Feeds are data, not documents: refuse DTDs and entity declarations outright (entity-expansion
    # and external-entity tricks), and anything implausibly large.
    head = text[:20000].lower()
    if "<!doctype" in head or "<!entity" in text.lower():
        raise ingest.SourceError("unsafe_feed", "The feed contains a DTD/entity declaration, which is refused.")
    if len(text) > 5_000_000:
        raise ingest.SourceError("too_large", "The feed is too large.")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        snippet = " ".join(text[:80].split())
        raise ingest.SourceError("bad_feed", f"This address did not return a readable RSS/Atom feed ({e}). "
                                             f"It starts with: “{snippet}”. Check that the address is the feed "
                                             "itself, not a web page.") from e
    items = []
    for it in root.iter("item"):
        url = None
        for enc in it.findall("enclosure"):
            if (enc.get("type") or "").startswith("video/") or Path(urlparse(enc.get("url", "")).path).suffix.lower() in ingest.VIDEO_EXTS:
                url = enc.get("url")
                break
        if not url:
            for mc in it.iter(MEDIA_NS + "content"):
                if (mc.get("type") or "").startswith("video/") or mc.get("medium") == "video":
                    url = mc.get("url")
                    break
        link = (it.findtext("link") or "").strip()
        url = url or link
        if not url:
            continue
        key = (it.findtext("guid") or "").strip() or url
        items.append({"key": key, "title": (it.findtext("title") or "").strip(), "url": urljoin(base_url, url),
                      "published": (it.findtext("pubDate") or "").strip()})
    for entry in root.iter(ATOM_NS + "entry"):
        url = None
        for ln in entry.findall(ATOM_NS + "link"):
            if ln.get("rel") == "enclosure" and ((ln.get("type") or "").startswith("video/") or
                                                  Path(urlparse(ln.get("href", "")).path).suffix.lower() in ingest.VIDEO_EXTS):
                url = ln.get("href")
                break
        if not url:
            alt = entry.find(ATOM_NS + "link")
            url = alt.get("href") if alt is not None else None
        if not url:
            continue
        items.append({"key": (entry.findtext(ATOM_NS + "id") or url).strip(),
                      "title": (entry.findtext(ATOM_NS + "title") or "").strip(), "url": urljoin(base_url, url),
                      "published": (entry.findtext(ATOM_NS + "updated") or entry.findtext(ATOM_NS + "published") or "").strip()})
    return items


LISTING_MAX_REQUESTS = 4    # web requests one channel listing may make (a normal listing needs 1-2)


class _ListingCapReached(Exception):
    pass


_RUMBLE_GRID_RE = re.compile(r'<rum-videos-grid[^>]*>\s*<script[^>]*type=["\']application/json["\'][^>]*>(.*?)</script>',
                             re.S | re.I)
_RUMBLE_CHANNEL_PATH = re.compile(r"^/(c|user)/([^/?#]+)/?(videos|livestreams)?/?$", re.I)


def _is_rumble_channel(url: str) -> bool:
    p = urlparse(url)
    host = (p.hostname or "").lower()
    return (host == "rumble.com" or host.endswith(".rumble.com")) and bool(_RUMBLE_CHANNEL_PATH.match(p.path or "/"))


def rumble_grid_entries(url: str, body: str, limit: int = 15) -> list[dict] | None:
    """Rumble channel pages (since their late-2026 redesign) carry the channel's videos as JSON inside
    <rum-videos-grid> elements, with title, upload date and live status; there are no
    `videostream__link` anchors any more, which is what yt-dlp's channel extractor looks for.

    Returns None when the page has no such grid (an older layout: the caller falls back to yt-dlp),
    otherwise the channel's own videos, newest first. Videos by other channels (recommendations),
    shorts and scheduled-but-not-started broadcasts are left out; a scheduled broadcast is therefore
    looked at again on the next check and recorded once it is live."""
    blocks = _RUMBLE_GRID_RE.findall(body or "")
    if not blocks:
        return None
    m = _RUMBLE_CHANNEL_PATH.match(urlparse(url).path or "/")
    channel = f"/{m.group(1)}/{m.group(2)}".lower() if m else ""
    seen, items = set(), []
    for block in blocks:
        try:
            data = json.loads(block)
        except ValueError:
            continue
        for it in data.get("items") or [] if isinstance(data, dict) else []:
            if not isinstance(it, dict) or it.get("object_type", "video") != "video":
                continue
            by = it.get("by") or {}
            owner = (by.get("relative_url") or urlparse(by.get("url") or "").path or "").rstrip("/").lower()
            if channel and owner and owner != channel:
                continue
            if it.get("is_short"):
                continue
            link = it.get("url") or ("https://rumble.com" + it["relative_url"] if it.get("relative_url") else "")
            key = str(it.get("permalink_id") or it.get("id") or link)
            if not link or key in seen:
                continue
            seen.add(key)
            status = it.get("livestream_status")
            if it.get("live") is True:
                live = True
            elif it.get("live_placeholder"):
                continue                                # scheduled, not started: look again next check
            elif status in (None, 0, "0"):
                live = False
            else:
                live = None                             # unfamiliar state: the monitor asks the video page
            items.append({"key": key, "title": it.get("title") or "", "url": link, "live": live,
                          "published": str(it.get("upload_date") or "")})
    items.sort(key=lambda e: (e["live"] is True, e["published"]), reverse=True)
    return items[:limit]


def list_channel(settings: Settings, url: str, limit: int = 15) -> list[dict]:
    """Listing of a channel/page (only called for authorised domains).

    Rumble channel pages are read directly: ONE request, then the video grid embedded in the page.
    Everything else (and a Rumble page without that grid) goes through yt-dlp's flat listing."""
    if _is_rumble_channel(url):
        status, final, body = fetch_text(settings, url)
        if status == 429:
            raise ingest.SourceError("rate_limited", "HTTP Error 429: Too Many Requests")
        if status != 200:
            raise ingest.SourceError("http_error", f"The channel page returned HTTP {status}, so nothing was "
                                                   "imported. " + _describe_page({"status": status, "body": body}))
        entries = rumble_grid_entries(final, body, limit)
        if entries:
            return entries
        if entries is not None:
            raise ingest.SourceError("empty_listing", "The channel page's video list was empty, so nothing was "
                                                      "imported. " + _describe_page({"status": status, "body": body}))
    import yt_dlp
    first_page: dict = {}

    class _Capped(yt_dlp.YoutubeDL):
        """Stops a listing after LISTING_MAX_REQUESTS web requests. yt-dlp's Rumble channel extractor
        keeps asking for ?page=2, 3, 4 ... until the site answers 404, so a page that shows no videos
        (an empty or challenge page) would otherwise turn one check into an unbounded run of requests.
        It also keeps the first page it received, so an empty listing can say what the site sent."""
        requests_made = 0

        def urlopen(self, req):
            self.requests_made += 1
            if self.requests_made > LISTING_MAX_REQUESTS:
                raise _ListingCapReached()
            resp = super().urlopen(req)
            if first_page or not hasattr(resp, "read"):
                return resp
            try:
                from yt_dlp.networking import Response
                body = resp.read(PAGE_KEEP_BYTES + 1)
                first_page.update(url=getattr(resp, "url", "") or "", status=getattr(resp, "status", None),
                                  headers={k.lower(): v for k, v in dict(getattr(resp, "headers", {}) or {}).items()},
                                  body=body.decode("utf-8", "replace"))
                return Response(io.BytesIO(body), resp.url, dict(resp.headers), status=resp.status, reason=resp.reason,
                                extensions=getattr(resp, "extensions", None))
            except Exception:  # noqa: BLE001 - diagnostics must never break a listing
                return resp

    opts = {"quiet": True, "no_warnings": True, "extract_flat": "in_playlist", "playlistend": limit,
            "skip_download": True, "http_headers": {"User-Agent": settings.http_user_agent}, "socket_timeout": 30,
            "color": "never", "retries": 1, "extractor_retries": 0}
    entries = None
    try:
        with _Capped(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except _ListingCapReached:
        info = {"_type": "playlist", "entries": []}
    if info.get("_type") == "playlist" or "entries" in info:
        entries = list(info.get("entries") or [])
        if not entries:
            entries = _links_from_page(url, first_page.get("body") or "")
        if not entries:
            # A channel page that lists nothing is not "one video called <channel>".
            raise ingest.SourceError("empty_listing", "The channel page listed no videos, so nothing was imported. "
                                                      + _describe_page(first_page))
    else:
        entries = [info]            # the address is a single video page
    out = []
    for e in entries:
        if not e:
            continue
        u = e.get("webpage_url") or e.get("url")
        if not u:
            continue
        out.append({"key": str(e.get("id") or u), "title": e.get("title") or "", "url": u,
                    "published": str(e.get("upload_date") or e.get("timestamp") or ""),
                    "live": _live_flag(e)})
    return out


PAGE_KEEP_BYTES = 3_000_000     # first listing page kept in memory for diagnosis (never written to disk)
_RUMBLE_VIDEO_HREF = re.compile(r'href=["\'](?:https?://(?:www\.)?rumble\.com)?(/v[0-9a-z]{4,}-[^"\'#?<>\s]*\.html)', re.I)
_BOT_CHECK_HINTS = ("just a moment", "verify you are human", "are you a robot", "captcha", "challenge-platform",
                    "checking your browser", "access denied", "attention required")


def _links_from_page(url: str, body: str) -> list[dict]:
    """Rumble only: read the video links from the channel page the engine was given, when yt-dlp's
    extractor found none (it looks for one particular CSS class, which a page redesign can rename).
    This reads the same page with the same permission; it makes no extra request."""
    host = (urlparse(url).hostname or "").lower()
    if not body or not (host == "rumble.com" or host.endswith(".rumble.com")):
        return []
    seen, out = set(), []
    for path in _RUMBLE_VIDEO_HREF.findall(body):
        u = "https://rumble.com" + path
        if u not in seen:
            seen.add(u)
            out.append({"url": u, "id": None, "title": "", "_type": "url"})
    return out


def _describe_page(page: dict) -> str:
    """One plain sentence on what the site actually sent, for the Sources page and the owner's report."""
    if not page:
        return "The site sent no page the engine could read; it may be limiting or filtering automated requests."
    body = page.get("body") or ""
    m = re.search(r"<title[^>]*>(.*?)</title>", body, re.I | re.S)
    title = re.sub(r"\s+", " ", m.group(1)).strip()[:80] if m else ""
    low = body[:200_000].lower()
    headers = page.get("headers") or {}
    bot = headers.get("cf-mitigated") == "challenge" or any(h in low for h in _BOT_CHECK_HINTS)
    facts = (f"(HTTP {page.get('status')}, {len(body) // 1024} KB, page title \u201c{title or 'none'}\u201d, "
             f"server {headers.get('server', 'unknown')}).")
    if bot:
        return ("The site answered with a robot check instead of the channel page " + facts + " It is filtering "
                "automated requests: ask the site how they want this access made (for example allow-listing this "
                "engine's User-Agent or your IP address).")
    return ("The site sent a page without video links the engine recognises " + facts + " Either the page layout "
            "changed or the site is filtering automated requests; send this message to whoever maintains the engine.")


def _live_flag(e: dict):
    """True / False when the listing says whether the entry is live, None when it does not.

    Some sites' listings (Rumble's channel pages, for example) carry only the video links, with no
    title and no live status. Such entries must be looked up individually (probe_entry) before the
    engine can tell a live broadcast from an ordinary upload."""
    if e.get("is_live") or e.get("live_status") == "is_live":
        return True
    if e.get("live_status") or e.get("is_live") is False:
        return False
    return None


def probe_entry(settings: Settings, url: str) -> dict:
    """Look up ONE listed video page (yt-dlp, no download) to learn its title and live status.

    Used only for entries whose listing did not say whether they are live, at most a few per check.
    The same identifying User-Agent and authorisation gate apply as for the listing itself."""
    import yt_dlp
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "socket_timeout": 30,
            "http_headers": {"User-Agent": settings.http_user_agent}, "color": "never", "retries": 1,
            "extractor_retries": 0, "ignore_no_formats_error": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    status = info.get("live_status") or ("is_live" if info.get("is_live") else None)
    return {"title": info.get("title") or "", "live_status": status,
            "published": str(info.get("upload_date") or info.get("timestamp") or "")}


def hls_state(text: str) -> dict:
    """Minimal HLS inspection: is it a playlist, does it have media, has it ended?"""
    if "#EXTM3U" not in text:
        return {"valid": False}
    seq = re.search(r"#EXT-X-MEDIA-SEQUENCE:(\d+)", text)
    uris = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
    return {"valid": True, "ended": "#EXT-X-ENDLIST" in text, "uris": uris,
            "master": "#EXT-X-STREAM-INF" in text,
            "segments": len(re.findall(r"^#EXTINF", text, re.M)),
            "sequence": int(seq.group(1)) if seq else 0,
            "fingerprint": hashlib.sha1("\n".join(ln for ln in text.splitlines() if ln and not ln.startswith("#"))[:4000]
                                        .encode()).hexdigest()[:12]}


# ---- integrity --------------------------------------------------------------------

def verify_recording(path: Path, min_seconds: float) -> tuple[bool, list[str], dict]:
    """Detect incomplete/corrupt files: probe streams and duration, then decode the last 10 s."""
    problems: list[str] = []
    try:
        info = media.probe(path)
    except Exception as e:  # noqa: BLE001
        return False, [f"unreadable: {e}"], {}
    if not info["has_video"]:
        problems.append("no video stream")
    if not info["has_audio"]:
        problems.append("no audio stream")
    if info["duration"] < min_seconds:
        problems.append(f"too short ({info['duration']:.1f}s < {min_seconds:.0f}s)")
    ffmpeg = media.require("ffmpeg")
    tail = subprocess.run([ffmpeg, "-v", "error", "-sseof", "-10", "-i", str(path), "-f", "null", "-"],
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          **media._popen_kwargs())
    errs = [ln for ln in tail.stderr.splitlines() if ln.strip()]
    if tail.returncode != 0 or len(errs) > 5:
        problems.append(f"decode errors near the end ({len(errs)} lines): {' | '.join(errs[:2])[:200]}")
    return not problems, problems, info


# ---- restart overlap ----------------------------------------------------------------

def _video_packets(path: Path, *, head: float | None = None, tail: float | None = None) -> list[tuple[float, str]]:
    """(dts seconds, signature) for the video packets at the start or end of a file.
    The signature is the packet's content hash when ffprobe provides one (stream-copied packets
    of the same broadcast are byte-identical), otherwise its size and flags."""
    ffprobe = media.require("ffprobe")
    args = [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_packets", "-show_data_hash", "md5",
            "-show_entries", "packet=dts_time,pts_time,size,flags,data_hash", "-of", "json"]
    if head:
        args += ["-read_intervals", f"%+{head:.3f}"]
    elif tail:
        fmt = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=start_time,duration", "-of", "json",
                              str(path)], capture_output=True, text=True, encoding="utf-8", errors="replace",
                             **media._popen_kwargs())
        try:
            f = json.loads(fmt.stdout or "{}").get("format", {})
            start, dur = float(f.get("start_time") or 0.0), float(f["duration"])
            args += ["-read_intervals", f"{max(start, start + dur - tail):.3f}%"]
        except (ValueError, KeyError, TypeError):
            pass                      # unknown duration: read the whole file
    proc = subprocess.run(args + [str(path)], capture_output=True, text=True, encoding="utf-8", errors="replace",
                          **media._popen_kwargs())
    try:
        packets = json.loads(proc.stdout or "{}").get("packets", [])
    except ValueError:
        return []
    out: list[tuple[float, str]] = []
    for pk in packets:
        t = pk.get("dts_time") if pk.get("dts_time") not in (None, "N/A") else pk.get("pts_time")
        try:
            t = float(t)
        except (TypeError, ValueError):
            continue
        sig = pk.get("data_hash") or f"size:{pk.get('size')}:{(pk.get('flags') or '')[:1]}"
        out.append((t, sig))
    return out


def find_overlap(prev: Path, nxt: Path, window: float = 180.0, min_run: int = 5) -> tuple[float, int] | None:
    """Does `nxt` (a recording restarted after an interruption) begin with picture data that
    `prev` already holds? Returns (time in `prev` where the repeated part begins, number of
    repeated video packets), or None when there is no repeat.

    A match needs an unbroken run of identical packets starting at the first packet of `nxt`
    and reaching (to within three packets of) the end of `prev` or the end of the inspected
    head — and at least `min_run` of them (30 when only packet sizes are available) — so a
    coincidental frame can never trim footage. The crash that interrupted `prev` may have cut
    its last packets short; those are dropped with the repeat. The latest qualifying position
    is used, so the smallest possible amount is ever removed."""
    try:
        heads = _video_packets(nxt, head=window)
        tails = _video_packets(prev, tail=window)
    except Exception:  # noqa: BLE001 - detection is best effort; never block finalisation
        return None
    if heads and not heads[0][1].startswith("MD5:") and not heads[0][1].startswith("md5:"):
        min_run = max(min_run, 30)            # size-only signatures need a much longer run
    if len(heads) < min_run or len(tails) < min_run:
        return None
    # Some FFmpeg versions write the very first packet of a file slightly differently (parameter
    # sets are repeated in front of the first keyframe), so if anchoring on packet 0 finds
    # nothing, anchor on packet 1 and step the cut point back by one.
    for anchor in (0, 1):
        first = heads[anchor][1]
        for k in range(len(tails) - 1, anchor - 1, -1):
            if tails[k][1] != first:
                continue
            m = 0
            while k + m < len(tails) and anchor + m < len(heads) and tails[k + m][1] == heads[anchor + m][1]:
                m += 1
            if m >= min_run and (k + m >= len(tails) - 3 or anchor + m == len(heads)):
                return tails[k - anchor][0], len(tails) - (k - anchor)
    return None


def concat_listing(segs: list[Path]) -> tuple[str, list[dict]]:
    """Concat-demuxer listing for the recorded segments. Where a restarted segment repeats the
    end of the previous one, the previous segment is cut at the point the repeat begins (a
    keyframe, because a restarted HLS recording starts on a media-segment boundary), so the
    joined recording contains each moment once. Nothing on disk is modified."""
    lines, trims = [], []
    for idx, p in enumerate(segs):
        lines.append(f"file '{p.name}'")
        if idx + 1 < len(segs):
            hit = find_overlap(p, segs[idx + 1])
            if hit:
                cut, packets = hit
                lines.append(f"outpoint {cut:.6f}")
                trims.append({"segment": p.name, "cut_at": round(cut, 3), "repeated_video_packets": packets})
    return "\n".join(lines) + "\n", trims


# ---- monitor ------------------------------------------------------------------------

class SourceMonitor:
    """Polls due sources (in its own thread, so long transcriptions never delay live detection)
    and supervises live recorder subprocesses."""

    def __init__(self, settings: Settings, db: Database, queue, interval: float = 30.0, sleep=time.sleep):
        self.settings, self.db, self.queue, self.interval, self.sleep = settings, db, queue, interval, sleep
        self.procs: dict[int, subprocess.Popen] = {}
        self._stop = threading.Event()
        self._t: threading.Thread | None = None
        self.lister = list_channel           # replaceable in tests (MOCK)
        self.prober = probe_entry            # replaceable in tests (MOCK)
        queue.register("download_url:failed", self._download_failed)

    # -- lifecycle
    def start(self) -> None:
        self.recover()

        def loop():
            while not self._stop.wait(self.interval):
                try:
                    self.tick()
                except Exception:  # noqa: BLE001 - never let the monitor die
                    log.exception("monitor tick failed")
        self._t = threading.Thread(target=loop, name="clipengine-monitor", daemon=True)
        self._t.start()

    def stop(self) -> None:
        """Stop polling; terminate recorders but leave items in 'recording' so a restart resumes."""
        self._stop.set()
        for p in list(self.procs.values()):
            try:
                p.terminate()
                p.wait(timeout=10)
            except Exception:  # noqa: BLE001
                p.kill()
        self.procs.clear()

    def recover(self) -> None:
        """After a restart: items left 'recording' resume (new segment) if still live, else finalize."""
        for it in self.db.query("SELECT * FROM source_items WHERE state IN ('recording','finalizing')"):
            src = self.db.one("SELECT * FROM sources WHERE id=?", (it["source_id"],))
            if it["state"] == "finalizing" or not src or not src["enabled"]:
                self._finalize(it["id"])
                continue
            if self._stream_live(src, it):
                self._log_item(it["id"], "Resuming recording after restart")
                self._start_segment(src, self.db.one("SELECT * FROM source_items WHERE id=?", (it["id"],)))
            else:
                self._finalize(it["id"])

    def tick(self) -> None:
        now = time.time()
        if self.settings.monitor_enabled:
            for src in self.db.query("SELECT * FROM sources WHERE enabled = 1 AND COALESCE(next_check, 0) <= ?", (now,)):
                self.poll(src["id"])
        self.supervise()

    # -- polling
    def poll(self, sid: int) -> dict:
        src = self.db.one("SELECT * FROM sources WHERE id = ?", (sid,))
        if not src:
            return {}
        ok, msg = check_source_allowed(self.settings, self.db, src["url"])
        if not ok:
            self.db.update("sources", sid, enabled=0, status="blocked", status_detail=msg, last_checked=time.time())
            return {"blocked": msg}
        result = {"new": 0, "live": False}
        try:
            # First real listing: what is already published becomes the baseline (noted, not downloaded).
            # A channel whose only "success" was an earlier bogus/empty listing (no real items stored; an
            # empty channel listing is an error since 5edc464) still counts as first, so a repaired
            # listing never queues the whole back catalogue at once.
            first = src["last_success"] is None or src["kind"] == "channel" and self.db.one(
                "SELECT 1 FROM source_items WHERE source_id=? AND (url IS NULL OR url<>?) LIMIT 1",
                (sid, src["url"])) is None
            if src["kind"] == "feed":
                status, final, text = fetch_text(self.settings, src["url"])
                if status != 200:
                    raise ingest.SourceError("http_error", f"Feed returned HTTP {status}")
                result["new"] = self._handle_vods(src, parse_feed(text, final), first)
            elif src["kind"] == "channel":
                entries = self.lister(self.settings, src["url"])
                # Drop a bogus "the channel itself" item left by an earlier empty listing (stored as
                # baseline, or queued and then failed), and retire the download job it caused: that job
                # can never succeed (a channel address is refused as a single video) and would otherwise
                # stay on the dashboard as a failed download with a Retry button.
                self.db.execute("DELETE FROM source_items WHERE source_id=? AND kind='vod' AND state IN "
                                "('baseline', 'queued', 'failed') AND (url=? OR url IS NULL)", (sid, src["url"]))
                self.db.execute("UPDATE jobs SET status='cancelled', message='Obsolete: the channel address was "
                                "never a single video; the channel is now listed correctly', updated_at=? "
                                "WHERE kind='download_url' AND status IN ('failed', 'queued') AND dedupe_key=?",
                                (time.time(), f"download:{ingest.normalize_url(src['url'])}"))
                entries = self._resolve_unknown(src, entries, first)
                live = [e for e in entries if e.get("live")]
                result["new"] = self._handle_vods(src, [e for e in entries if not e.get("live")], first)
                if live and src["record_live"]:
                    result["live"] = self._handle_live(src, live[0]["key"], live[0]["url"], live[0]["title"], "ytdlp")
            elif src["kind"] == "hls":
                status, _, text = fetch_text(self.settings, src["url"])
                st = hls_state(text) if status == 200 else {"valid": False}
                if st.get("valid") and st.get("segments", 0) + (1 if st.get("master") else 0) > 0:
                    result["live"] = self._handle_hls(src, st, first)
            self.db.update("sources", sid, status="ok", status_detail=msg, last_checked=time.time(),
                           last_success=time.time(), consecutive_errors=0, last_error=None,
                           next_check=time.time() + src["poll_minutes"] * 60)
        except Exception as e:  # noqa: BLE001 - recorded and backed off, never fatal
            n = src["consecutive_errors"] + 1
            delay = min(6 * 3600, src["poll_minutes"] * 60 * 2 ** min(n, 6))
            text = ANSI_RE.sub("", f"{type(e).__name__}: {e}")
            if "429" in text or "too many requests" in text.lower():
                # The site is rate-limiting us. Hammering it makes that worse and is not what any
                # permission covers: wait at least 30 minutes (longer after repeated refusals).
                delay = max(delay, min(6 * 3600, 1800 * 2 ** min(n - 1, 3)))
                text = (f"The site refused the request as too frequent (HTTP 429). The engine will wait "
                        f"{int(delay // 60)} minutes before checking again; pressing “Check now” repeatedly makes "
                        "this worse. If it persists, ask the site how they want automated access made (for "
                        "example allow-listing this engine's User-Agent or your IP address). Details: " + text)
            self.db.update("sources", sid, status="error", last_checked=time.time(), consecutive_errors=n,
                           last_error=text[:1000], next_check=time.time() + delay)
            result["error"] = text
            log.warning("source %s poll failed: %s", sid, e)
        return result

    PROBE_LIMIT = 3     # per check: listed entries looked up individually when the listing omits live status

    def _resolve_unknown(self, src, entries: list[dict], first_poll: bool) -> list[dict]:
        """Fill in the live status of NEW listed entries whose listing did not give it.

        Only entries the engine has not seen before are looked up, newest first and at most
        PROBE_LIMIT per check, so a check costs a handful of requests at most. An entry that is
        scheduled but not yet live ("upcoming") is skipped without being stored, so it is looked at
        again on the next check and recorded once it goes live. Entries beyond the limit are also
        left for the next check, except on the very first check, where they only become the
        baseline (older items noted, not downloaded). A refused lookup (HTTP 429) propagates, so
        the whole source backs off."""
        known = {r["item_key"] for r in self.db.query("SELECT item_key FROM source_items WHERE source_id=?", (src["id"],))}
        out, probed = [], 0
        for e in entries:
            if e.get("live") is not None or e["key"] in known:
                out.append(e)
                continue
            if probed >= self.PROBE_LIMIT:
                if first_poll:
                    out.append(dict(e, live=False))
                continue
            probed += 1
            info = self.prober(self.settings, e["url"])
            status = info.get("live_status")
            if status == "is_upcoming":
                continue
            out.append(dict(e, live=status == "is_live", title=e.get("title") or info.get("title") or "",
                            published=e.get("published") or info.get("published") or ""))
        return out

    def _handle_vods(self, src, items: list[dict], first_poll: bool) -> int:
        new = 0
        known = {r["item_key"] for r in self.db.query("SELECT item_key FROM source_items WHERE source_id=?", (src["id"],))}
        fresh = [it for it in items if it["key"] not in known]
        backfill_keys = {it["key"] for it in fresh[:src["backfill"]]} if first_poll else set()
        for it in fresh:
            if first_poll and it["key"] not in backfill_keys:
                # Baseline: items already published when monitoring began are recorded, not downloaded.
                self.db.insert("source_items", source_id=src["id"], item_key=it["key"], title=it["title"],
                               url=it["url"], published=it.get("published"), state="baseline")
                continue
            decision = ingest.classify_url(it["url"], self.settings, self.db)
            iid = self.db.insert("source_items", source_id=src["id"], item_key=it["key"], title=it["title"],
                                 url=it["url"], published=it.get("published"), state="queued")
            if not decision.ok:
                self.db.update("source_items", iid, state="failed", error=decision.message)
                continue
            jid = self.queue.enqueue("download_url", max_attempts=6, dedupe_key=f"download:{ingest.normalize_url(it['url'])}",
                                     payload={"url": it["url"], "title": it["title"] or None, "source_item_id": iid,
                                              "recorded_at": ingest.parse_recorded_at(it.get("published")),
                                              "rights_attestation": f"Monitored source '{src['name']}': {src['authorisation'] or 'authorised by configuration'}"})
            self.db.update("source_items", iid, job_id=jid)
            new += 1
        return new

    def backfill(self, sid: int, count: int) -> dict:
        """Owner action: import up to `count` of the OLDER items that were only noted as the
        baseline when monitoring began (newest first). Each item is queued at most once: its
        state moves from 'baseline' to 'queued', and the download job is de-duplicated by URL."""
        src = self.db.one("SELECT * FROM sources WHERE id = ?", (sid,))
        if not src:
            return {"error": "Source not found."}
        ok, msg = check_source_allowed(self.settings, self.db, src["url"])
        if not ok:
            return {"blocked": msg}
        rows = self.db.query("SELECT * FROM source_items WHERE source_id=? AND state='baseline' AND kind='vod' "
                             "ORDER BY id LIMIT ?", (sid, max(0, int(count))))
        queued, refused = 0, 0
        for it in rows:
            decision = ingest.classify_url(it["url"], self.settings, self.db)
            if not decision.ok:
                self.db.update("source_items", it["id"], state="failed", error=decision.message)
                refused += 1
                continue
            jid = self.queue.enqueue(
                "download_url", max_attempts=6, dedupe_key=f"download:{ingest.normalize_url(it['url'])}",
                payload={"url": it["url"], "title": it["title"] or None, "source_item_id": it["id"],
                         "recorded_at": ingest.parse_recorded_at(it["published"]),
                         "rights_attestation": f"Monitored source '{src['name']}' (historical backfill requested by "
                                               f"the owner): {src['authorisation'] or 'authorised by configuration'}"})
            self.db.update("source_items", it["id"], state="queued", job_id=jid)
            queued += 1
        left = self.db.one("SELECT COUNT(*) n FROM source_items WHERE source_id=? AND state='baseline' AND kind='vod'",
                           (sid,))["n"]
        return {"queued": queued, "refused": refused, "remaining": left}

    def _download_failed(self, ctx) -> None:
        iid = ctx.payload.get("source_item_id")
        if iid:
            job = self.db.one("SELECT error FROM jobs WHERE id=?", (ctx.id,))
            self.db.update("source_items", iid, state="failed", error=(job["error"] if job else "failed"))

    # -- live recording
    def _handle_hls(self, src, st: dict, first_poll: bool) -> bool:
        """One broadcast = one recording. Identity comes from the playlist's media segment URIs."""
        active = self.db.one("SELECT * FROM source_items WHERE source_id=? AND state IN ('recording','finalizing')",
                             (src["id"],))
        if active:
            return True
        uris = set(st.get("uris") or [])
        for prev in self.db.query("SELECT fingerprint FROM source_items WHERE source_id=? AND kind='live' "
                                  "ORDER BY id DESC LIMIT 5", (src["id"],)):
            if prev["fingerprint"] and uris & set(json.loads(prev["fingerprint"])):
                return False                  # same broadcast as one already recorded
        key = f"live-{time.strftime('%Y%m%d-%H%M%S')}-{hashlib.sha1((st['uris'] or [''])[0].encode()).hexdigest()[:8]}"
        if st.get("ended") and first_poll and not src["backfill"]:
            # An archived broadcast that ended before monitoring began: baseline, not downloaded.
            self.db.insert("source_items", source_id=src["id"], item_key=key, title="(ended before monitoring began)",
                           url=src["url"], kind="live", state="baseline", fingerprint=json.dumps(st["uris"][-200:]))
            return False
        title = f"{src['name']} {time.strftime('%Y-%m-%d %H:%M')}"
        started = self._handle_live(src, key, src["url"], title, "hls")
        self.db.execute("UPDATE source_items SET fingerprint=? WHERE source_id=? AND item_key=?",
                        (json.dumps(st["uris"][-200:]), src["id"], key))
        return started

    def _remember_segments(self, it) -> None:
        """Extend the broadcast fingerprint with the playlist's current segment URIs."""
        try:
            status, _, text = fetch_text(self.settings, it["url"])
            if status == 200:
                st = hls_state(text)
                seen = set(json.loads(it["fingerprint"] or "[]")) | set(st.get("uris") or [])
                self.db.update("source_items", it["id"], fingerprint=json.dumps(sorted(seen)[-500:]))
        except Exception:  # noqa: BLE001 - best effort
            pass

    def _handle_live(self, src, key: str, url: str, title: str, method: str) -> bool:
        it = self.db.one("SELECT * FROM source_items WHERE source_id=? AND item_key=?", (src["id"], key))
        if it and (it["state"] not in LIVE_STATES or it["id"] in self.procs):
            return it["state"] in LIVE_STATES
        if not it:
            from . import storage
            ok, msg = storage.live_preflight(self.settings, 1.0)
            if not ok:
                # Not stored as an item, so the next check tries again once space has been freed.
                self.db.update("sources", src["id"], status_detail=f"Live stream detected but NOT recorded: {msg}")
                log.warning("source %s: live stream not recorded: %s", src["id"], msg)
                return False
            wd = self.settings.work_dir / "live" / f"src{src['id']:03d}_{re.sub(r'[^A-Za-z0-9_-]', '_', key)[:60]}"
            wd.mkdir(parents=True, exist_ok=True)
            iid = self.db.insert("source_items", source_id=src["id"], item_key=key, title=title, url=url, kind="live",
                                 state="recording", work_dir=str(wd), started_at=time.time(),
                                 published=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
            it = self.db.one("SELECT * FROM source_items WHERE id=?", (iid,))
            self._log_item(iid, f"Live stream detected ({method}); recording started")
        self._start_segment(src, it, method)
        return True

    def _recorder_cmd(self, src, it, seg: Path, method: str) -> list[str]:
        if method == "hls" or src["kind"] == "hls":
            return [media.require("ffmpeg"), "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
                    "-protocol_whitelist", "http,https,tcp,tls,crypto", "-rw_timeout", "20000000",
                    # Always begin a few media segments back. After a restart this deliberately
                    # re-fetches the last segments (so nothing is missed while the recorder was
                    # down); the repeated part is removed exactly when the recording is joined.
                    "-live_start_index", "-3",
                    "-i", it["url"], "-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy", "-f", "mpegts", str(seg)]
        return [sys.executable, "-m", "yt_dlp", "--no-part", "--hls-use-mpegts", "--no-progress", "--quiet",
                "--user-agent", self.settings.http_user_agent, "-o", str(seg), it["url"]]

    def _start_segment(self, src, it, method: str | None = None) -> None:
        wd = Path(it["work_dir"])
        wd.mkdir(parents=True, exist_ok=True)
        n = it["segments"] + 1
        seg = wd / f"seg_{n:03d}.ts"
        method = method or ("hls" if src["kind"] == "hls" else "ytdlp")
        logf = open(wd / f"seg_{n:03d}.log", "w", encoding="utf-8")
        p = subprocess.Popen(self._recorder_cmd(src, it, seg, method), stdout=logf, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, **media._popen_kwargs())
        logf.close()      # the child has its own handle; keeping ours would leak (and lock on Windows)
        self.procs[it["id"]] = p
        self.db.update("source_items", it["id"], segments=n, state="recording")

    def _stream_live(self, src, it) -> bool:
        try:
            if src["kind"] == "hls":
                status, _, text = fetch_text(self.settings, it["url"])
                st = hls_state(text) if status == 200 else {"valid": False}
                return bool(st.get("valid")) and not st.get("ended")
            entries = self.lister(self.settings, src["url"])
            listed = [e for e in entries if e["key"] == it["item_key"]]
            if listed and listed[0].get("live") is not None:
                return bool(listed[0]["live"])
            # The listing does not say (or no longer shows the item): ask the video page itself.
            return self.prober(self.settings, it["url"]).get("live_status") == "is_live"
        except Exception:  # noqa: BLE001 - unreachable counts as not live
            return False

    def supervise(self) -> None:
        for iid, p in list(self.procs.items()):
            it = self.db.one("SELECT * FROM source_items WHERE id=?", (iid,))
            if not it:
                self.procs.pop(iid, None)
                continue
            elapsed = time.time() - (it["started_at"] or time.time())
            if p.poll() is None:
                from . import storage
                if storage.free_bytes(self.settings.data_dir) < self.settings.disk_reserve_gb * 0.5e9:
                    self._log_item(iid, "Disk almost full: stopping the recording and keeping what was captured",
                                   "WARNING")
                    p.terminate()
                    p.wait(timeout=15)
                    self.procs.pop(iid, None)
                    self._finalize(iid)
                    continue
                if elapsed > self.settings.max_recording_hours * 3600:
                    self._log_item(iid, "Maximum recording length reached; stopping", "WARNING")
                    p.terminate()
                    p.wait(timeout=15)
                    self.procs.pop(iid, None)
                    self._finalize(iid)
                continue
            self.procs.pop(iid, None)
            src = self.db.one("SELECT * FROM sources WHERE id=?", (it["source_id"],))
            if src and src["enabled"] and self._stream_live(src, it) and it["restarts"] < self.settings.live_restart_limit:
                self.db.update("source_items", iid, restarts=it["restarts"] + 1)
                self._log_item(iid, f"Recorder exited (code {p.returncode}) while the stream is still live; "
                                    f"starting a new segment (restart {it['restarts'] + 1})", "WARNING")
                self._start_segment(src, self.db.one("SELECT * FROM source_items WHERE id=?", (iid,)))
            else:
                self._finalize(iid)

    def _log_item(self, iid: int, msg: str, level: str = "INFO") -> None:
        log.log(getattr(logging, level), "source item %s: %s", iid, msg)

    def _finalize(self, iid: int) -> None:
        """Join segments → MP4, verify integrity, hand to the inbox with a provenance sidecar."""
        it = self.db.one("SELECT * FROM source_items WHERE id=?", (iid,))
        if not it:
            return
        if (it["url"] or "").split("?")[0].endswith(".m3u8"):
            self._remember_segments(it)
        self.db.update("source_items", iid, state="finalizing")
        wd = Path(it["work_dir"])
        segs = sorted(p for p in wd.glob("seg_*.ts") if p.stat().st_size > 0)
        if not segs:
            self.db.update("source_items", iid, state="failed", error="No data was recorded.", ended_at=time.time())
            return
        out = wd / "recording.mp4"
        listing = wd / "segments.txt"
        text, trims = concat_listing(segs)
        listing.write_text(text, encoding="utf-8")
        if trims:
            self._log_item(iid, f"Removed repeated footage at {len(trims)} restart point(s): "
                                + ", ".join(f"{t['segment']} cut at {t['cut_at']}s" for t in trims))
        try:
            media.run_ffmpeg(["-f", "concat", "-safe", "0", "-i", listing.name, "-map", "0:v:0?", "-map", "0:a:0?",
                              "-c", "copy", "-bsf:a", "aac_adtstoasc", "-movflags", "+faststart", out.name], cwd=wd)
        except Exception as e:  # noqa: BLE001
            self.db.update("source_items", iid, state="failed", error=f"Could not join segments: {e}"[:1000],
                           ended_at=time.time())
            return
        ok, problems, info = verify_recording(out, self.settings.min_recording_seconds)
        if not ok:
            self.db.update("source_items", iid, state="failed", ended_at=time.time(),
                           error="Integrity check failed: " + "; ".join(problems))
            return
        src = self.db.one("SELECT * FROM sources WHERE id=?", (it["source_id"],))
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(it["started_at"] or time.time()))
        name = f"monitor_{ingest.safe_filename(src['name'] if src else 'source', 40)}_{stamp}_item{iid}.mp4"
        inbox = self.settings.inbox
        inbox.mkdir(parents=True, exist_ok=True)
        sidecar = {"recorder": "clipengine", "source_item_id": iid, "source_id": it["source_id"],
                   "source_name": src["name"] if src else None, "source_url": it["url"],
                   "title": it["title"], "item_key": it["item_key"],
                   "started_at": it["started_at"], "ended_at": time.time(), "segments": len(segs),
                   "restarts": it["restarts"], "duration": info.get("duration"), "overlap_trims": trims,
                   "authorisation": src["authorisation"] if src else None}
        (inbox / (name + ".source.json")).write_text(json.dumps(sidecar, indent=1), encoding="utf-8")
        tmp = inbox / (name + ".partial")
        shutil.move(str(out), tmp)
        tmp.replace(inbox / name)        # becomes visible to the inbox importer only when complete
        self.db.update("source_items", iid, state="handed_off", ended_at=time.time(), error=None)
        for p in segs:
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass      # e.g. Windows still releasing a handle; stale work files are cleaned up later
        self._log_item(iid, f"Recording complete ({info.get('duration', 0):.0f}s, {len(segs)} segment(s)); "
                            "handed to inbox")
