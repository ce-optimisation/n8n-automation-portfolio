"""Check a local render against an explicitly chosen campaign render profile."""

from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import subprocess


def verify_metadata(probe, *, width, height, min_seconds, max_seconds):
    streams = probe.get("streams", [])
    video = next((row for row in streams if row["codec_type"] == "video"), {})
    audio = next((row for row in streams if row["codec_type"] == "audio"), {})
    raw_duration = probe.get("format", {}).get("duration")
    try:
        duration = Decimal(str(raw_duration))
    except InvalidOperation:
        duration = None
    checks = {
        "video_dimensions": (video.get("width"), video.get("height")) == (width, height),
        "duration": bool(duration is not None and duration.is_finite() and min_seconds <= duration <= max_seconds),
        "h264_video": video.get("codec_name") == "h264",
        "aac_audio": audio.get("codec_name") == "aac",
    }
    return {"passes_measured_checks": all(checks.values()), "checks": checks,
            "duration_seconds": str(duration) if duration is not None and duration.is_finite() else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", type=Path)
    parser.add_argument("--width", type=int, required=True)
    parser.add_argument("--height", type=int, required=True)
    parser.add_argument("--min-seconds", type=Decimal, required=True)
    parser.add_argument("--max-seconds", type=Decimal, required=True)
    args = parser.parse_args()
    if (args.width <= 0 or args.height <= 0 or not args.min_seconds.is_finite()
            or not args.max_seconds.is_finite() or not 0 <= args.min_seconds <= args.max_seconds):
        parser.error("Provide positive dimensions and finite, ordered, nonnegative duration limits")
    if not args.video.is_file():
        parser.error("Video must be an existing local file")
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "stream=codec_name,codec_type,width,height:format=duration", "-of", "json", str(args.video.resolve())],
            check=True, capture_output=True, text=True, timeout=15,
        )
        report = verify_metadata(json.loads(result.stdout), width=args.width, height=args.height,
                                 min_seconds=args.min_seconds, max_seconds=args.max_seconds)
    except (OSError, subprocess.SubprocessError, ValueError, KeyError) as exc:
        parser.exit(2, f"Unable to inspect render: {exc}\n")
    print(json.dumps(report, indent=2))
    return 0 if report["passes_measured_checks"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
