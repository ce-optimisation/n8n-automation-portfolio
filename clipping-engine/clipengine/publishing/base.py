"""Common publishing types and the editorial gate applied before any upload."""
from __future__ import annotations

from dataclasses import dataclass


class PublishError(Exception):
    pass


class NotConfigured(PublishError):
    pass


@dataclass
class PublishResult:
    status: str          # uploaded | confirmed | failed
    remote_id: str | None
    detail: str
    confirmed: bool      # True only when the platform API confirmed the outcome


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
    if not clip["video_path"]:
        problems.append("Clip has not been rendered.")
    return problems
