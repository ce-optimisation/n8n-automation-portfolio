"""Prepare a local manual submission packet for an explicitly selected clip."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from commercial_intelligence.select_candidates import select


def prepare(brief, clips, *, clip_id, destination, account_id, media, as_of):
    """Re-evaluate the supplied rules and capture the exact media without submitting it."""
    result = select(brief, clips, as_of=as_of)
    match = next((c for c in result["candidates"] if c["clip_id"] == clip_id), None)
    if not match or match["status"] != "shortlisted_for_review":
        raise ValueError("Clip must pass preflight for the requested time")
    if match["destination"] != destination or not account_id.strip():
        raise ValueError("Exact shortlisted destination and destination account ID are required")
    if not media.is_file() or media.is_symlink():
        raise ValueError("Provide a local regular media file")
    digest = hashlib.sha256()
    size = 0
    with media.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            size += len(block)
            digest.update(block)
    if not size:
        raise ValueError("Media file is empty")
    return {
        "packet_status": "draft_for_manual_review",
        "campaign": result["campaign"],
        "deadline_utc": result["submission_deadline"],
        "prepared_utc": result["as_of"],
        "clip": match,
        "destination": {"platform": destination, "account_id": account_id.strip()},
        "media": {"filename": media.name, "bytes": size, "sha256": digest.hexdigest()},
        "remaining_review": [
            "Verify the media is the saved render for this clip",
            "Confirm original rights evidence",
            "Verify exact platform account identity",
            "Check media profile and campaign rules",
            "Obtain specific publication approval",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("brief", type=Path)
    parser.add_argument("clips", type=Path)
    parser.add_argument("media", type=Path)
    parser.add_argument("--clip-id", type=int, required=True)
    parser.add_argument("--destination", required=True)
    parser.add_argument("--account-id", required=True)
    args = parser.parse_args()
    try:
        brief = json.loads(args.brief.read_text(encoding="utf-8-sig"))
        clips = json.loads(args.clips.read_text(encoding="utf-8-sig"))
        output = prepare(brief, clips, clip_id=args.clip_id, destination=args.destination,
                         account_id=args.account_id, media=args.media, as_of=datetime.now(timezone.utc))
    except (OSError, ValueError, KeyError, AttributeError, TypeError) as exc:
        parser.exit(2, f"{exc}\n")
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
