"""Common publishing types, video validation and the editorial gate applied before any upload."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class PublishError(Exception):
    pass


class NotConfigured(PublishError):
    """Credentials missing, not connected, expired or lacking a permission: owner action needed."""


@dataclass
class PublishResult:
    status: str          # one of STATES
    remote_id: str | None
    detail: str
    confirmed: bool      # True only when the platform API confirmed the outcome


# Publication states shown in the dashboard. Only the two *confirmed* states mean the
# platform itself reported success.
STATES = {
    "queued": "Upload queued",
    "uploading": "Uploading",
    "processing": "Platform processing",
    "retrying": "Retrying",
    "awaiting_connection": "Awaiting platform connection",
    "delivered_inbox": "Delivered to TikTok inbox (confirmed)",
    "delivered_draft": "Facebook Reel DRAFT created (not published)",
    "delivered_private": "YouTube PRIVATE upload (not public)",
    "published": "Published (confirmed)",
    "failed": "Failed",
    "cancelled": "Cancelled",
}
ACTIVE_STATES = {"queued", "uploading", "processing", "retrying"}
CONFIRMED_STATES = {"delivered_inbox", "published", "delivered_draft", "delivered_private"}

RIGHTS_OK = {"owned", "licensed", "public_domain", "fair_dealing_reviewed"}


def editorial_gate(clip) -> list[str]:
    """Reasons a clip may NOT be sent to a platform. Empty list = allowed.

    Human approval is necessary but not sufficient: rights must be recorded and
    any safety flag must have an explicit editorial decision.
    """
    problems = []
    if clip["status"] != "approved":
        problems.append("Clip must be approved by the owner first.")
    if clip["rights_status"] not in RIGHTS_OK:
        problems.append("Record the copyright/rights basis (owned, licensed, public domain, or "
                        "fair dealing reviewed) before publishing.")
    if clip["safety_status"] == "flagged":
        problems.append("This clip was flagged for editorial review; record an editorial decision "
                        "(acknowledge with a context note) before publishing.")
    if not clip["video_path"] or not Path(clip["video_path"]).is_file():
        problems.append("Clip has not been rendered (or its file was removed).")
    return problems


def validate_video(path: Path, *, min_s: float, max_s: float, max_bytes: int, codecs=("h264",)) -> dict:
    """Check a rendered clip against a platform's documented limits before uploading."""
    from .. import media
    path = Path(path)
    if not path.is_file():
        raise PublishError(f"Video file not found: {path}")
    size = path.stat().st_size
    if size > max_bytes:
        raise PublishError(f"Video is {size / 1e6:.0f} MB, above the platform limit of {max_bytes / 1e6:.0f} MB.")
    info = media.probe(path)
    if info["video_codec"] not in codecs:
        raise PublishError(f"Video codec {info['video_codec']} is not accepted ({', '.join(codecs)} required).")
    if info["audio_codec"] not in ("aac", None):
        raise PublishError(f"Audio codec {info['audio_codec']} is not accepted (AAC required).")
    if not (min_s <= info["duration"] <= max_s):
        raise PublishError(f"Video is {info['duration']:.1f}s; the platform accepts {min_s}–{max_s}s.")
    if (info["width"] or 0) < 360 or (info["height"] or 0) < 360:
        raise PublishError("Video resolution is too small for the platform.")
    return info
