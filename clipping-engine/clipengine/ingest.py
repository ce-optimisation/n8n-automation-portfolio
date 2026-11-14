"""Video ingestion: local files and authorised URLs.

URL ingestion policy
--------------------
Automated retrieval is only attempted when:
  1. the host is NOT on the restricted-platform list below (their terms of
     service prohibit automated downloading without permission), and
  2. the host is listed in ALLOWED_URL_DOMAINS (configured by the owner), and
  3. the owner attests, per import, that they are authorised to retrieve it.

Nothing here passes cookies, logs in, bypasses DRM, geo-blocks or rate limits.
For restricted platforms, the owner should obtain the file through an
authorised route (the platform's own download/export feature, or directly from
the rights holder) and use the file upload instead.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
import unicodedata
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from .config import Settings
from .db import Database
from .jobs import JobContext, PermanentError, TransientError
from . import media

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi"}

# Platforms whose terms prohibit automated downloading by third-party tools
# without permission. Retrieval from these is refused outright.
RESTRICTED_PLATFORMS = {
    "youtube.com": "YouTube's Terms of Service prohibit downloading content except via a download feature YouTube provides. Creators can download their own uploads from YouTube Studio.",
    "youtu.be": "YouTube's Terms of Service prohibit downloading content except via a download feature YouTube provides.",
    "rumble.com": "Rumble's terms do not grant permission for automated downloading. Obtain the file from the rights holder or Rumble's own download option where the uploader enabled it, then upload it here.",
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


def check_url_policy(url: str, settings: Settings) -> tuple[bool, str]:
    p = urlparse(url.strip())
    if p.scheme not in ("http", "https") or not p.netloc:
        return False, "Enter a full http(s) URL."
    host = p.netloc.lower()
    for dom, reason in RESTRICTED_PLATFORMS.items():
        if host_matches(host, dom):
            return False, (f"Automated retrieval from {dom} is not supported: {reason} "
                           "Use 'Upload a recording' with a file you are authorised to use.")
    if not any(host_matches(host, d) for d in settings.allowed_url_domains):
        allowed = ", ".join(settings.allowed_url_domains) or "(none configured)"
        return False, (f"{host} is not in ALLOWED_URL_DOMAINS ({allowed}). Add it to .env only if the "
                       "site's terms and the rights holder permit automated downloading, or upload the file instead.")
    return True, "ok"


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


def register_file(settings: Settings, db: Database, src: Path, *, title: str | None = None,
                  original_name: str | None = None, source_type: str = "upload",
                  source_url: str | None = None, source_site: str | None = None,
                  source_media_id: str | None = None, extra_meta: dict | None = None,
                  move: bool = True, rights_attestation: str | None = None,
                  settings_json: dict | None = None) -> tuple[int, bool]:
    """Store a video file as a recording. Returns (recording_id, is_duplicate)."""
    src = Path(src)
    if src.suffix.lower() not in VIDEO_EXTS:
        raise PermanentError(f"Unsupported file type '{src.suffix}'. Use MP4 or MOV (also MKV/WEBM/M4V/AVI).")
    digest = sha256_file(src)
    dup = find_duplicate(db, sha256=digest, url=source_url)
    if dup:
        if move and src.exists() and settings.data_dir in src.resolve().parents:
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
    rid = db.insert("recordings", title=title, source_type=source_type,
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
        "rights_attestation": rights_attestation}, indent=2), encoding="utf-8")
    return rid, False


def handle_download_url(settings: Settings, db: Database, queue, ctx: JobContext) -> None:
    """Job handler: retrieve an authorised URL with yt-dlp, then queue analysis."""
    url = ctx.payload["url"]
    ok, reason = check_url_policy(url, settings)
    if not ok:
        raise PermanentError(reason)
    try:
        import yt_dlp
    except ImportError as e:  # pragma: no cover
        raise PermanentError("yt-dlp is not installed (pip install -r requirements.txt)") from e

    tmp = settings.work_dir / f"download_{ctx.id}"
    tmp.mkdir(parents=True, exist_ok=True)

    def hook(d):
        if ctx.cancelled():
            raise yt_dlp.utils.DownloadCancelled("cancelled")
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            if total:
                ctx.progress(0.9 * d.get("downloaded_bytes", 0) / total, "Downloading")

    opts = {
        "outtmpl": str(tmp / "download.%(ext)s"),
        "format": "bv*[height<=1080]+ba/b[height<=1080]/b",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [hook],
        "restrictfilenames": True,
        "retries": 3,
        "sleep_interval_requests": 1,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except yt_dlp.utils.DownloadCancelled:
        return
    except yt_dlp.utils.DownloadError as e:
        msg = str(e)
        if any(k in msg.lower() for k in ("timed out", "temporary", "connection", "http error 5")):
            raise TransientError(msg) from e
        if "drm" in msg.lower():
            raise PermanentError("This video is DRM-protected; it will not be downloaded.") from e
        raise PermanentError(msg) from e
    files = [p for p in tmp.iterdir() if p.suffix.lower() in VIDEO_EXTS]
    if not files:
        raise PermanentError("Download finished but no video file was produced.")
    meta = {k: info.get(k) for k in ("title", "uploader", "channel", "upload_date", "webpage_url",
                                     "extractor_key", "id", "duration", "license", "description")}
    ctx.progress(0.95, "Registering download")
    rid, dup = register_file(settings, db, files[0], title=ctx.payload.get("title") or info.get("title"),
                             original_name=files[0].name, source_type="url", source_url=url,
                             source_site=info.get("extractor_key"), source_media_id=str(info.get("id")),
                             extra_meta={"source_info": meta}, move=True,
                             rights_attestation=ctx.payload.get("rights_attestation"),
                             settings_json=ctx.payload.get("settings"))
    shutil.rmtree(tmp, ignore_errors=True)
    db.update("jobs", ctx.id, recording_id=rid)
    if dup:
        ctx.log(f"Duplicate of existing recording #{rid}; not re-processing", "WARNING")
        return
    queue.enqueue("process_recording", recording_id=rid, dedupe_key=f"process:{rid}",
                  payload={"settings": ctx.payload.get("settings") or {}})
