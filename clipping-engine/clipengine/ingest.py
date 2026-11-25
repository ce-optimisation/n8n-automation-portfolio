"""Video ingestion: local files, direct media URLs, permitted extractor sources,
and "link + file" pending sources for platforms that forbid automated access.

URL ingestion policy (see classify_url)
---------------------------------------
* Only http(s) on standard ports; no credentials in URLs; every hop of every
  redirect must resolve to a public (globally routable) address, and the peer
  address is re-checked after connecting (defeats DNS rebinding).
* Direct media files (e.g. https://example.gov/meeting.mp4) are fetched over plain
  HTTP(S) with resume support, after the owner attests they may use the file.
* Web pages are only handed to yt-dlp when the host is in ALLOWED_URL_DOMAINS or
  the owner has recorded written permission for that domain.
* Platforms whose terms forbid automated access without permission (Rumble,
  YouTube, TikTok, Instagram, Facebook, X, ...) are never fetched unless the owner
  has recorded the platform's written permission (Settings → Source permissions).
  Otherwise the link is saved as a *pending source*: provenance is kept and the
  owner attaches the file obtained through an authorised route.

Nothing here passes cookies, logs in, bypasses DRM, geo-blocks or rate limits.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import shutil
import socket
import time
import unicodedata
from dataclasses import dataclass
from email.message import Message
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse

from .config import Settings
from .db import Database
from .jobs import JobContext, PermanentError, TransientError
from . import media

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".ts"}
CONTENT_TYPE_EXT = {"video/mp4": ".mp4", "video/quicktime": ".mov", "video/webm": ".webm",
                    "video/x-matroska": ".mkv", "video/x-m4v": ".m4v", "video/x-msvideo": ".avi"}
BINARY_TYPES = {"application/octet-stream", "binary/octet-stream", "application/mp4"}
USER_AGENT = "LocalClippingEngine/1.1 (local editorial review tool)"

# Platforms whose terms prohibit automated access/downloading without the platform's
# permission. Never fetched unless a written permission is recorded for the domain.
RESTRICTED_PLATFORMS = {
    "youtube.com": "YouTube's Terms of Service prohibit downloading except through a feature YouTube provides. Creators can download their own uploads from YouTube Studio.",
    "youtu.be": "YouTube's Terms of Service prohibit downloading except through a feature YouTube provides.",
    "rumble.com": "Rumble's Terms of Use (section “Restrictions on automated software”) prohibit any automated access to the site without Rumble's prior written permission. Obtain the original file from the creator/rights holder, or record Rumble's written permission in Settings.",
    "americafirst.plus": "America First Plus is a paid subscription archive (NJF LLC) whose Terms of Service forbid copying, scraping or extracting its content without the owner's express prior written consent. Nothing is fetched from it. Ask the rights holder for the file (or their written consent, recorded in Settings), then attach the file to this link.",
    "tiktok.com": "TikTok's terms prohibit automated scraping/downloading.",
    "instagram.com": "Instagram's terms prohibit automated collection of content.",
    "facebook.com": "Meta's terms prohibit automated collection of content.",
    "fb.watch": "Meta's terms prohibit automated collection of content.",
    "x.com": "X's terms prohibit scraping without prior consent.",
    "twitter.com": "X's terms prohibit scraping without prior consent.",
    "twitch.tv": "Twitch's terms restrict downloading of streams; creators can download their own VODs from Twitch.",
    "kick.com": "Kick's terms restrict automated collection of content.",
    "bitchute.com": "No automated-download permission is granted by the platform's terms.",
    "odysee.com": "Check the uploader's licence and download the file directly, then upload it here.",
}


def safe_filename(name: str, max_len: int = 80) -> str:
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    name = re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip(" ._") or "video"
    name = re.sub(r"\s+", "_", name)
    return name[:max_len]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(4 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def normalize_url(url: str) -> str:
    p = urlparse(url.strip())
    return urlunparse((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/"), "", p.query, ""))


def host_matches(host: str, domain: str) -> bool:
    host = host.lower().split(":")[0]
    return host == domain or host.endswith("." + domain)


def _new_recording_dir(settings: Settings, title: str) -> Path:
    d = settings.recordings_dir / f"{time.strftime('%Y%m%d-%H%M%S')}_{safe_filename(title, 40)}"
    i = 1
    base = d
    while d.exists():
        d = base.with_name(f"{base.name}_{i}")
        i += 1
    d.mkdir(parents=True)
    return d


def find_duplicate(db: Database, *, sha256: str | None = None, url: str | None = None):
    if sha256:
        row = db.one("SELECT * FROM recordings WHERE sha256 = ?", (sha256,))
        if row:
            return row
    if url:
        row = db.one("SELECT * FROM recordings WHERE source_url = ?", (normalize_url(url),))
        if row:
            return row
    return None


FILENAME_DATE_RE = re.compile(r"(?<!\d)(20\d{2}|19\d{2})[-_.]?(0[1-9]|1[0-2])[-_.]?(0[1-9]|[12]\d|3[01])(?!\d)")


def parse_recorded_at(value) -> str | None:
    """Normalise an original recording/broadcast date to 'YYYY-MM-DD' (or 'YYYY-MM-DDTHH:MM').
    Accepts ISO dates, yt-dlp 'YYYYMMDD', RSS dates, and Unix timestamps. None if unusable or
    in the future."""
    if value in (None, ""):
        return None
    import datetime as dt
    from email.utils import parsedate_to_datetime
    d: dt.datetime | None = None
    has_time = False
    try:
        if isinstance(value, (int, float)) or re.fullmatch(r"\d{9,11}(\.\d+)?", str(value).strip()):
            d, has_time = dt.datetime.fromtimestamp(float(value)), True
        else:
            v = str(value).strip()
            if re.fullmatch(r"\d{8}", v):
                d = dt.datetime.strptime(v, "%Y%m%d")
            elif re.match(r"\d{4}-\d{2}-\d{2}", v):
                d = dt.datetime.fromisoformat(v.replace("Z", "+00:00"))
                has_time = len(v) > 10
            else:
                d, has_time = parsedate_to_datetime(v), True
    except (ValueError, TypeError, OverflowError, OSError):
        return None
    if d.tzinfo is not None:
        d = d.astimezone().replace(tzinfo=None)
    if d.year < 1990 or d > dt.datetime.now() + dt.timedelta(days=1):
        return None
    return d.strftime("%Y-%m-%dT%H:%M") if has_time and (d.hour or d.minute) else d.strftime("%Y-%m-%d")


def date_from_filename(name: str) -> str | None:
    m = FILENAME_DATE_RE.search(Path(name).stem)
    return parse_recorded_at(f"{m.group(1)}-{m.group(2)}-{m.group(3)}") if m else None


def classify_origin(recorded_at: str | None, source_type: str, now: float | None = None) -> str:
    """live_capture = recorded by this engine as it was broadcast; archive = an older recording
    brought in afterwards; recent = everything else."""
    import datetime as dt
    if source_type == "monitor":
        return "live_capture"
    if recorded_at:
        try:
            day = dt.date.fromisoformat(recorded_at[:10])
            today = dt.date.fromtimestamp(now) if now else dt.date.today()
            if (today - day).days >= 2:
                return "archive"
        except ValueError:
            pass
    return "recent"


def set_provenance(db: Database, rid: int, *, source_url: str | None = None, recorded_at: str | None = None) -> dict:
    """Owner correction of a recording's source address / original date (also updates provenance.json)."""
    rec = db.recording(rid)
    if not rec:
        raise SourceError("not_found", "Recording not found.")
    fields: dict = {}
    if source_url is not None:
        u = source_url.strip()
        if u and urlparse(u).scheme not in ("http", "https"):
            raise SourceError("bad_url", "Enter a full http(s) address for the original source.")
        fields["source_url"] = normalize_url(u) if u else None
        fields["source_site"] = urlparse(u).hostname if u else rec["source_site"]
    if recorded_at is not None:
        if recorded_at.strip():
            parsed = parse_recorded_at(recorded_at)
            if not parsed:
                raise SourceError("bad_date", "Enter the original date as YYYY-MM-DD (it cannot be in the future).")
            fields["recorded_at"] = parsed
        else:
            fields["recorded_at"] = None
    if "recorded_at" in fields:
        fields["origin"] = classify_origin(fields["recorded_at"], rec["source_type"])
    if fields:
        db.update("recordings", rid, **fields)
        pfile = Path(rec["file_path"]).parent / "provenance.json" if rec["file_path"] else None
        if pfile and pfile.is_file():
            try:
                data = json.loads(pfile.read_text(encoding="utf-8"))
                data.update({k: fields[k] for k in ("source_url", "recorded_at", "origin") if k in fields})
                data.setdefault("corrections", []).append({"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                                                           "fields": sorted(fields)})
                pfile.write_text(json.dumps(data, indent=2), encoding="utf-8")
            except (ValueError, OSError):
                pass
    return fields


def register_file(settings: Settings, db: Database, src: Path, *, title: str | None = None,
                  original_name: str | None = None, source_type: str = "upload",
                  source_url: str | None = None, source_site: str | None = None,
                  source_media_id: str | None = None, extra_meta: dict | None = None,
                  move: bool = True, rights_attestation: str | None = None,
                  settings_json: dict | None = None, dedupe_by_url: bool = True,
                  recorded_at: str | None = None, origin: str | None = None) -> tuple[int, bool]:
    """Store a video file as a recording. Returns (recording_id, is_duplicate)."""
    src = Path(src)
    if src.suffix.lower() not in VIDEO_EXTS:
        raise PermanentError(f"Unsupported file type '{src.suffix}'. Use MP4 or MOV (also MKV/WEBM/M4V/AVI).")
    digest = sha256_file(src)
    # A live channel/stream URL is shared by every broadcast, so live recordings dedupe by content only.
    dup = find_duplicate(db, sha256=digest, url=source_url if dedupe_by_url else None)
    if dup:
        if move and src.exists() and settings.work_dir.resolve() in src.resolve().parents:  # temp copies only
            src.unlink(missing_ok=True)
        return dup["id"], True
    info = media.probe(src)
    if not info["has_video"]:
        raise PermanentError("The file has no video stream.")
    if not info["has_audio"]:
        raise PermanentError("The file has no audio stream, so it cannot be transcribed.")
    title = (title or (extra_meta or {}).get("title") or Path(original_name or src.name).stem).strip()[:200]
    rdir = _new_recording_dir(settings, title)
    dest = rdir / ("source" + src.suffix.lower())
    if move:
        shutil.move(str(src), dest)
    else:
        shutil.copy2(src, dest)
    meta = {"probe": info, **(extra_meta or {})}
    recorded_at = parse_recorded_at(recorded_at)
    origin = origin or classify_origin(recorded_at, source_type)
    rid = db.insert("recordings", title=title, source_type=source_type, recorded_at=recorded_at, origin=origin,
                    source_url=normalize_url(source_url) if source_url else None,
                    source_site=source_site, source_media_id=source_media_id,
                    original_filename=original_name or src.name, file_path=str(dest), sha256=digest,
                    size_bytes=dest.stat().st_size, duration=info["duration"], width=info["width"],
                    height=info["height"], fps=info["fps"], has_audio=int(info["has_audio"]),
                    metadata_json=json.dumps(meta, default=str), status="imported", stage="queued",
                    rights_attestation=rights_attestation,
                    settings_json=json.dumps(settings_json or {}))
    (rdir / "provenance.json").write_text(json.dumps({
        "recording_id": rid, "title": title, "source_type": source_type, "source_url": source_url,
        "source_site": source_site, "source_media_id": source_media_id, "original_filename": original_name,
        "sha256": digest, "imported_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "recorded_at": recorded_at, "origin": origin,
        "rights_attestation": rights_attestation}, indent=2), encoding="utf-8")
    return rid, False


class SourceError(PermanentError):
    """A permanent, explained ingestion failure. `code` is machine-readable."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class UrlDecision:
    method: str      # direct | ytdlp | link_only | refused
    code: str
    message: str
    host: str = ""

    @property
    def ok(self) -> bool:
        return self.method in ("direct", "ytdlp")


def resolve_host(host: str) -> list[str]:
    """Resolve a hostname to IP strings (module-level so tests can replace it)."""
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    return sorted({i[4][0] for i in infos})


def is_public_ip(ip: str) -> bool:
    addr = ipaddress.ip_address(ip.split("%")[0])
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return addr.is_global and not addr.is_multicast


def permission_for(db: Database | None, host: str):
    if db is None:
        return None
    for row in db.query("SELECT * FROM source_permissions"):
        if host_matches(host, row["domain"]):
            return row
    return None


def restricted_reason(host: str) -> tuple[str, str] | None:
    for dom, reason in RESTRICTED_PLATFORMS.items():
        if host_matches(host, dom):
            return dom, reason
    return None


def check_network_target(url: str, settings: Settings) -> str:
    """Validate scheme/port/credentials and that the host resolves only to public IPs.
    Returns the hostname. Raises SourceError."""
    p = urlparse(url.strip())
    if p.scheme not in ("http", "https") or not p.hostname:
        raise SourceError("unsupported_scheme", "Only http:// and https:// links are supported.")
    if p.username or p.password:
        raise SourceError("credentials_in_url", "Links containing usernames or passwords are not accepted.")
    if p.port not in (None, 80, 443) and not settings.allow_private_urls:
        raise SourceError("unsupported_port", f"Port {p.port} is not allowed; use a standard http(s) link.")
    host = p.hostname.lower()
    if settings.allow_private_urls:      # TEST-ONLY switch (ALLOW_PRIVATE_URLS=1)
        return host
    try:
        ips = [host] if _is_ip(host) else resolve_host(host)
    except OSError as e:
        raise SourceError("unresolvable", f"Could not resolve {host} ({e}). Check the link and your "
                                          "internet connection.") from e
    bad = [ip for ip in ips if not is_public_ip(ip)]
    if bad or not ips:
        raise SourceError("private_address", f"{host} points to a private or local network address "
                                             f"({', '.join(bad) or 'none'}); refused for security.")
    return host


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def classify_url(url: str, settings: Settings, db: Database | None = None) -> UrlDecision:
    """Decide how (and whether) a URL may be ingested. Never touches the network
    except for DNS resolution of the host."""
    p = urlparse((url or "").strip())
    host = (p.hostname or "").lower()
    restricted = restricted_reason(host) if host else None
    perm = permission_for(db, host) if host else None
    if restricted and not perm:
        dom, reason = restricted
        return UrlDecision("link_only", "restricted_platform",
                           f"Automated retrieval from {dom} is not permitted: {reason}", host)
    try:
        check_network_target(url, settings)
    except SourceError as e:
        return UrlDecision("refused", e.code, str(e), host)
    if Path(p.path).suffix.lower() in VIDEO_EXTS and not restricted:
        return UrlDecision("direct", "direct_media", "Direct video file link.", host)
    if perm:
        return UrlDecision("ytdlp", "permission_recorded",
                           f"Written permission recorded for {perm['domain']} ({perm['reference']}).", host)
    if any(host_matches(host, d) for d in settings.allowed_url_domains):
        return UrlDecision("ytdlp", "allowlisted", f"{host} is in ALLOWED_URL_DOMAINS.", host)
    return UrlDecision("link_only", "not_allowed",
                       f"{host} is a web page that is not in ALLOWED_URL_DOMAINS, so it will not be fetched "
                       "automatically. Use a direct link to the video file (ending in .mp4/.mov/.webm), add the "
                       "site to ALLOWED_URL_DOMAINS only if its terms and the rights holder permit automated "
                       "downloading, or attach the file yourself.", host)


def check_url_policy(url: str, settings: Settings, db: Database | None = None) -> tuple[bool, str]:
    d = classify_url(url, settings, db)
    return d.ok, d.message


# ---- direct HTTP(S) download with resume -------------------------------------

def _filename_from_disposition(value: str | None) -> str | None:
    if not value:
        return None
    m = Message()
    m["content-disposition"] = value
    name = m.get_filename()
    return Path(name).name if name else None


def _peer_is_public(response) -> bool:
    try:
        stream = response.extensions.get("network_stream")
        addr = stream.get_extra_info("server_addr") if stream else None
        return addr is None or is_public_ip(addr[0])
    except Exception:  # noqa: BLE001 - if unknown, fall back to the DNS check already done
        return True


def download_direct(url: str, part: Path, settings: Settings, *, on_progress=None, cancelled=None,
                    client=None) -> dict:
    """Download a direct media URL to `part`, resuming a previous partial download.

    Transient problems raise TransientError (the job is retried later and resumes);
    permanent ones raise SourceError with a specific code.
    """
    import httpx

    limit = settings.max_upload_mb * 1024 * 1024
    own = client is None
    client = client or httpx.Client(timeout=httpx.Timeout(30.0, read=120.0), follow_redirects=False,
                                    headers={"User-Agent": settings.http_user_agent or USER_AGENT})
    try:
        current, hops = url, 0
        while True:
            check_network_target(current, settings)
            have = part.stat().st_size if part.exists() else 0
            headers = {"Range": f"bytes={have}-"} if have else {}
            try:
                with client.stream("GET", current, headers=headers) as r:
                    if not settings.allow_private_urls and not _peer_is_public(r):
                        raise SourceError("private_address", "Connection reached a private address; refused.")
                    if r.status_code in (301, 302, 303, 307, 308):
                        hops += 1
                        if hops > 5:
                            raise SourceError("too_many_redirects", "The link redirects too many times.")
                        current = urljoin(current, r.headers.get("location", ""))
                        continue
                    if r.status_code == 416 and have:
                        part.unlink(missing_ok=True)   # stale partial file; start again
                        continue
                    if r.status_code in (429,) or r.status_code >= 500:
                        raise TransientError(f"Server returned HTTP {r.status_code}; will retry.")
                    if r.status_code in (401, 403):
                        raise SourceError("access_denied", f"The server refused access (HTTP {r.status_code}). "
                                          "The file may require a login (which this tool will not bypass), or the "
                                          "site may block automated clients; some sites ask for an informative "
                                          "HTTP_USER_AGENT with contact details (set it in .env).")
                    if r.status_code == 404:
                        raise SourceError("not_found", "The file was not found (HTTP 404).")
                    if r.status_code not in (200, 206):
                        raise SourceError("http_error", f"Unexpected HTTP {r.status_code} from the server.")
                    ctype = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
                    if not (ctype.startswith("video/") or ctype in BINARY_TYPES or ctype == ""):
                        raise SourceError("not_video", f"The link returned '{ctype}', not a video file. "
                                          "If it is a web page, use the video's direct file link or upload the file.")
                    if r.status_code == 206:
                        cr = r.headers.get("content-range", "")
                        m = re.match(r"bytes (\d+)-\d+/(\d+|\*)", cr)
                        if not m or int(m.group(1)) != have:
                            part.unlink(missing_ok=True)
                            continue
                        total = int(m.group(2)) if m.group(2) != "*" else 0
                        mode = "ab"
                    else:
                        total = int(r.headers.get("content-length") or 0)
                        have, mode = 0, "wb"
                    if total and total > limit:
                        raise SourceError("too_large", f"The file is {total / 1e9:.1f} GB, above MAX_UPLOAD_MB.")
                    size = have
                    with open(part, mode) as f:
                        for chunk in r.iter_bytes():
                            if cancelled and cancelled():
                                raise TransientError("cancelled")
                            f.write(chunk)
                            size += len(chunk)
                            if size > limit:
                                raise SourceError("too_large", "The file exceeds MAX_UPLOAD_MB.")
                            if on_progress and total:
                                on_progress(size / total)
                    if total and size < total:
                        raise TransientError(f"Download interrupted at {size}/{total} bytes; will resume.")
                    return {"final_url": current, "content_type": ctype, "bytes": size,
                            "etag": r.headers.get("etag"), "last_modified": r.headers.get("last-modified"),
                            "filename": _filename_from_disposition(r.headers.get("content-disposition"))}
            except httpx.TransportError as e:
                raise TransientError(f"Network error while downloading ({type(e).__name__}: {e}); "
                                     "the download will resume.") from e
    finally:
        if own:
            client.close()


def _part_path(settings: Settings, url: str) -> Path:
    return settings.work_dir / f"dl_{hashlib.sha1(normalize_url(url).encode()).hexdigest()[:16]}.part"


ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
# Phrases yt-dlp uses for videos that need a login, a membership or an age check. Matched as whole
# phrases: a bare "age" also matched "webpage", which turned a rate limit into "requires a login".
RESTRICTED_MARKERS = ("sign in", "log in", "login required", "logged-in", "account cookies", "private video",
                      "members-only", "members only", "age-restricted", "age restricted", "confirm your age",
                      "age verification", "this video is private")


def _is_collection_url(yt_dlp, url: str) -> bool:
    """True when yt-dlp would treat the address as a channel/playlist (checked offline, no request)."""
    try:
        from yt_dlp.extractor import gen_extractor_classes
    except Exception:  # noqa: BLE001 - a stand-in yt-dlp (tests) has no extractor list
        return False
    for ie in gen_extractor_classes():
        if ie.suitable(url):
            name = (getattr(ie, "IE_NAME", "") or "").lower()
            return getattr(ie, "_RETURN_TYPE", None) == "playlist" or any(
                k in name for k in ("channel", "playlist", ":tab", "user"))
    return False


DISK_RESERVE_BYTES = 1_000_000_000      # always left free on the data drive after a download
DOWNLOAD_HEIGHTS = (1080, 720, 480, 360)


def fit_format_to_disk(info: dict, free_bytes: int, ctx=None, min_height: int = 360) -> str:
    """Pick the highest resolution (≤1080p) whose download, plus the 16 kHz WAV made for transcription
    and a fixed reserve, fits in the free space. Sites that stream over HLS (Rumble, for example)
    publish no file size, so yt-dlp's own max_filesize cannot protect the disk: the size is estimated
    from each format's bitrate and the duration. Raises TransientError (retried in 1 h) when even the
    smallest choice does not fit, so a full disk is reported plainly instead of failing mid-download."""
    duration = float(info.get("duration") or 0)
    formats = info.get("formats") or []
    if duration <= 0 or not formats:
        return "bv*[height<=1080]+ba/b[height<=1080]/b"
    budget = free_bytes - DISK_RESERVE_BYTES - duration * 32_000          # WAV: 16 kHz mono s16
    audio_kbps = max([f.get("abr") or f.get("tbr") or 0 for f in formats
                      if f.get("vcodec") == "none" and f.get("acodec") not in (None, "none")] or [128])

    def size(h):
        sizes = []
        for f in formats:
            if not f.get("height") or f["height"] > h:
                continue
            if f.get("filesize") or f.get("filesize_approx"):
                sizes.append(f.get("filesize") or f.get("filesize_approx"))
            elif f.get("tbr"):
                extra = audio_kbps if f.get("acodec") == "none" else 0
                sizes.append((f["tbr"] + extra) * 1000 / 8 * duration)
        return min(sizes) if sizes else None    # smallest format at or below h (what "b[height<=h]" can fall to)

    for h in [h for h in DOWNLOAD_HEIGHTS if h >= min_height] or [DOWNLOAD_HEIGHTS[0]]:
        best = max([s for f in formats if f.get("height") == h
                    for s in [(f.get("filesize") or f.get("filesize_approx")
                               or ((f.get("tbr") or 0) + (audio_kbps if f.get("acodec") == "none" else 0))
                               * 1000 / 8 * duration)] if s] or [0])
        if best and best * 1.1 <= budget:
            if h < 1080 and ctx is not None:
                ctx.progress(0.0, f"Low disk space: downloading at {h}p")
            return f"bv*[height<={h}]+ba/b[height<={h}]"
    if min_height >= DOWNLOAD_HEIGHTS[0]:
        # owner's minimum quality: wait for space rather than download a lower resolution
        need = ((size(DOWNLOAD_HEIGHTS[0]) or 0) * 1.1 + DISK_RESERVE_BYTES + duration * 32_000) / 1e9
        raise TransientError(f"Not enough free disk space to download this {duration / 3600:.1f}-hour video at "
                             f"{min_height}p: about {need:.1f} GB is needed and {free_bytes / 1e9:.1f} GB is free. "
                             "Free some space; the download is retried in 1 hour.", retry_after=3600)
    smallest = size(DOWNLOAD_HEIGHTS[-1])
    if smallest is not None and smallest * 1.1 <= budget:
        return "wv*[height>=240]+ba/w[height>=240]/w"
    need = ((smallest or 0) * 1.1 + DISK_RESERVE_BYTES + duration * 32_000) / 1e9
    raise TransientError(f"Not enough free disk space for this {duration / 3600:.1f}-hour video: about "
                         f"{need:.1f} GB is needed and {free_bytes / 1e9:.1f} GB is free on the data drive. Free some "
                         "space (or move the data folder to a bigger drive); the download is retried in 1 hour.",
                         retry_after=3600)


def _download_with_ytdlp(settings: Settings, url: str, ctx: JobContext) -> tuple[Path, dict]:
    try:
        import yt_dlp
    except ImportError as e:  # pragma: no cover
        raise PermanentError("yt-dlp is not installed (pip install -r requirements.txt)") from e
    if _is_collection_url(yt_dlp, url):
        raise SourceError("not_a_video", "This address is a channel or playlist, not a single video, so it was not "
                                         "downloaded (that could fetch every video on the channel). Add it on the "
                                         "Sources page instead, or paste the link of one episode.")
    tmp = settings.work_dir / f"ytdlp_{hashlib.sha1(normalize_url(url).encode()).hexdigest()[:16]}"
    tmp.mkdir(parents=True, exist_ok=True)

    def hook(d):
        if ctx.cancelled():
            raise yt_dlp.utils.DownloadCancelled("cancelled")
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            if total:
                ctx.progress(0.9 * d.get("downloaded_bytes", 0) / total, "Downloading")

    opts = {
        "outtmpl": str(tmp / "download.%(ext)s"), "format": "bv*[height<=1080]+ba/b[height<=1080]/b",
        "merge_output_format": "mp4", "noplaylist": True, "quiet": True, "no_warnings": True,
        "progress_hooks": [hook], "restrictfilenames": True, "retries": 5, "fragment_retries": 5,
        "continuedl": True, "socket_timeout": 30, "sleep_interval_requests": 1,
        "buffersize": 64 * 1024, "noresizebuffer": True,
        "max_filesize": settings.max_upload_mb * 1024 * 1024, "http_headers": {"User-Agent": settings.http_user_agent or USER_AGENT},
    }
    try:
        with yt_dlp.YoutubeDL(dict(opts, skip_download=True)) as ydl:
            info = ydl.extract_info(url, download=False)
        opts["format"] = fit_format_to_disk(info, shutil.disk_usage(settings.data_dir).free, ctx,
                                            min_height=int(getattr(settings, "live_min_height", 1080)))
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.process_ie_result(info, download=True)
    except MemoryError as e:
        raise PermanentError("The downloader ran out of memory. Check available memory before retrying this video.") from e
    except yt_dlp.utils.DownloadCancelled as e:
        raise TransientError("cancelled") from e
    except yt_dlp.utils.DownloadError as e:
        msg = ANSI_RE.sub("", str(e))
        low = msg.lower()
        if "429" in low or "too many requests" in low:
            # Checked first: the message also contains words such as "webpage". Wait at least 30
            # minutes between attempts rather than hammering a site that is limiting us.
            raise TransientError("The site refused the request as too frequent (HTTP 429); the download will be "
                                 "retried later, at least 30 minutes apart. " + msg[:300], retry_after=1800) from e
        if "drm" in low:
            raise SourceError("drm", "This video is DRM-protected; it will not be downloaded.") from e
        if "unsupported url" in low:
            raise SourceError("unsupported_source", "No supported video was found at this page. Use a direct "
                              "video file link or upload the file.") from e
        if any(k in low for k in RESTRICTED_MARKERS):
            raise SourceError("access_restricted", "This video requires a login or is restricted; it will not "
                              "be retrieved. Obtain the file from the rights holder.") from e
        if any(k in low for k in ("timed out", "temporar", "connection", "http error 5", "getaddrinfo",
                                  "name resolution", "failed to resolve", "network is unreachable",
                                  "transporterror", "incompleteread", "errno 11001", "errno 10054",
                                  "errno 10060", "errno 10065")):
            raise TransientError(msg) from e
        raise SourceError("extractor_error", msg[:500]) from e
    files = [p for p in tmp.iterdir() if p.suffix.lower() in VIDEO_EXTS]
    if not files:
        raise SourceError("no_video", "Download finished but no video file was produced.")
    meta = {k: info.get(k) for k in ("title", "uploader", "channel", "upload_date", "webpage_url",
                                     "extractor_key", "id", "duration", "license")}
    return files[0], meta


def handle_download_url(settings: Settings, db: Database, queue, ctx: JobContext) -> None:
    """Job handler: retrieve a permitted URL (direct file or extractor), then queue analysis."""
    url = ctx.payload["url"]
    if ctx.payload.get("discovered_item_id") or ctx.payload.get("source_item_id"):
        from . import recording_switch
        from .jobs import Deferred
        if not recording_switch.is_enabled(db):
            # Session 16: a download found automatically (monitor/discovery/backfill) that was queued before Recording was
            # switched OFF must not start while it is OFF. Manual imports are not affected. No attempt is used up.
            raise Deferred("Recording is switched OFF; this automatically found video waits until it is turned back on")
        if not settings.auto_discovered_downloads:
            raise Deferred("Automatic downloads are held by configuration; live recording and manual imports are separate")
    decision = classify_url(url, settings, db)
    if decision.code == "unresolvable":
        # No internet for a moment is not a reason to give up on the video: retry later.
        raise TransientError(decision.message, retry_after=300)
    if not decision.ok:
        raise SourceError(decision.code, decision.message)
    extra: dict = {"retrieval": {"method": decision.method, "policy": decision.code, "reason": decision.message,
                                 "retrieved_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}}
    title = ctx.payload.get("title") or None
    source_media_id = None
    site = decision.host
    if decision.method == "direct":
        part = _part_path(settings, url)
        ctx.log(f"Direct download{' (resuming)' if part.exists() else ''}: {url}")
        meta = download_direct(url, part, settings, cancelled=ctx.cancelled,
                               on_progress=lambda f: ctx.progress(0.9 * f, "Downloading"))
        ext = Path(urlparse(meta["final_url"]).path).suffix.lower()
        if ext not in VIDEO_EXTS:
            ext = CONTENT_TYPE_EXT.get(meta["content_type"], ".mp4")
        file = part.with_suffix(ext)
        part.replace(file)
        extra["retrieval"].update({k: meta[k] for k in ("final_url", "content_type", "bytes", "etag",
                                                        "last_modified")})
        title = title or (Path(meta["filename"]).stem if meta.get("filename") else
                          Path(urlparse(url).path).stem or decision.host)
        original_name = meta.get("filename") or Path(urlparse(url).path).name or file.name
    else:
        ctx.log(f"Retrieving with yt-dlp ({decision.code}): {url}")
        file, info = _download_with_ytdlp(settings, url, ctx)
        extra["source_info"] = info
        title = title or info.get("title")
        source_media_id = str(info.get("id")) if info.get("id") else None
        site = info.get("extractor_key") or site
        original_name = file.name
        min_src = float(getattr(settings, "min_source_seconds", 0) or 0)
        dur = info.get("duration")
        automatic = bool(ctx.payload.get("discovered_item_id") or ctx.payload.get("source_item_id"))
        if min_src and automatic and isinstance(dur, (int, float)) and 0 < dur < min_src:
            # Safety net for items whose length was unknown at discovery: never register or re-clip a short video that
            # was found automatically. (Videos the owner imports by hand have no item id and are never skipped.)
            ctx.log(f"Skipping: {dur:.0f}s is under the {min_src:.0f}s minimum (already a short clip)")
            if file.parent.name.startswith("ytdlp_"):
                shutil.rmtree(file.parent, ignore_errors=True)
            else:
                file.unlink(missing_ok=True)
            note = f"Skipped: only {int(dur)}s, under the {int(min_src)}s minimum (already a short clip)"
            if ctx.payload.get("source_item_id"):
                db.update("source_items", ctx.payload["source_item_id"], state="skipped", error=note)
            if ctx.payload.get("discovered_item_id"):
                db.execute("UPDATE discovered_items SET state='skipped', detail=? WHERE id=?",
                           (note, ctx.payload["discovered_item_id"]))
            return
    ctx.progress(0.95, "Registering download", force=True)
    perm = permission_for(db, decision.host)
    attestation = ctx.payload.get("rights_attestation") or ""
    if perm:
        attestation += f" | Written permission on record: {perm['granted_by']}, {perm['granted_on']}, ref {perm['reference']}"
    rid, dup = register_file(settings, db, file, title=title, original_name=original_name, source_type="url",
                             source_url=url, source_site=site, source_media_id=source_media_id, extra_meta=extra,
                             move=True, rights_attestation=attestation.strip(" |") or None,
                             settings_json=ctx.payload.get("settings"),
                             recorded_at=ctx.payload.get("recorded_at") or (extra.get("source_info") or {}).get("upload_date")
                             or extra["retrieval"].get("last_modified"))
    if file.parent.name.startswith("ytdlp_"):
        shutil.rmtree(file.parent, ignore_errors=True)
    db.update("jobs", ctx.id, recording_id=rid)
    if ctx.payload.get("source_item_id"):
        db.update("source_items", ctx.payload["source_item_id"], recording_id=rid,
                  state="duplicate" if dup else "imported", error=None)
    if ctx.payload.get("creator_id"):
        db.execute("UPDATE recordings SET creator_id=COALESCE(creator_id, ?) WHERE id=?", (ctx.payload["creator_id"], rid))
    if ctx.payload.get("discovered_item_id"):
        db.execute("UPDATE discovered_items SET state=?, detail=? WHERE id=?",
                   ("duplicate" if dup else "imported", f"recording {rid}", ctx.payload["discovered_item_id"]))
    if dup:
        ctx.log(f"Duplicate of existing recording #{rid}; not re-processing", "WARNING")
        return
    queue.enqueue("process_recording", recording_id=rid, dedupe_key=f"process:{rid}",
                  payload={"settings": ctx.payload.get("settings") or {}})


# ---- pending sources ("link + file") -----------------------------------------

def create_pending_source(db: Database, url: str, *, title: str | None, reason: str,
                          settings_json: dict | None = None) -> tuple[int, bool]:
    """Record a link whose file the owner will supply. Returns (id, already_existed)."""
    norm = normalize_url(url)
    row = db.one("SELECT id FROM pending_sources WHERE url = ? AND status = 'awaiting_file'", (norm,))
    if row:
        return row["id"], True
    pid = db.insert("pending_sources", url=norm, title=title or None, reason=reason,
                    settings_json=json.dumps(settings_json or {}), status="awaiting_file")
    return pid, False


def attach_file_to_pending(settings: Settings, db: Database, pending_id: int, path: Path, *,
                           original_name: str, title: str | None = None,
                           attestation: str | None = None, recorded_at: str | None = None) -> tuple[int, bool]:
    src = db.one("SELECT * FROM pending_sources WHERE id = ?", (pending_id,))
    if not src or src["status"] != "awaiting_file":
        raise SourceError("pending_not_found", "That pending source no longer exists or already has a file.")
    note = (f"Owner-supplied file for {src['url']} (not downloaded by this tool). "
            + (attestation or "")).strip()
    rid, dup = register_file(settings, db, path, title=title or src["title"], original_name=original_name,
                             source_type="linked_file", source_url=src["url"],
                             source_site=urlparse(src["url"]).hostname,
                             extra_meta={"retrieval": {"method": "owner_supplied_file", "pending_source_id": pending_id}},
                             move=True, rights_attestation=note,
                             settings_json=json.loads(src["settings_json"] or "{}"),
                             recorded_at=recorded_at or date_from_filename(original_name))
    db.update("pending_sources", pending_id, status="attached", recording_id=rid)
    return rid, dup
