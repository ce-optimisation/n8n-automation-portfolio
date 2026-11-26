"""Child process for one video-page lookup (see monitor.probe_entry_isolated).

Session 21 (26 Nov 2026): lookups that timed out were abandoned in threads that never finished; the engine's private
memory reached 13.4 GB and pushed the pagefile into the last free disk space. A process can be killed; a thread cannot.
Usage: python -m clipengine.probe_child <url> <user-agent>   (prints one JSON object)
"""
import json
import sys


def main() -> int:
    url, agent = sys.argv[1], sys.argv[2]
    import yt_dlp
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "socket_timeout": 20,
            "http_headers": {"User-Agent": agent}, "color": "never", "retries": 1,
            "extractor_retries": 0, "ignore_no_formats_error": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    status = info.get("live_status") or ("is_live" if info.get("is_live") else None)
    sys.stdout.write(json.dumps({"title": info.get("title") or "", "live_status": status,
                                 "published": str(info.get("upload_date") or info.get("timestamp") or "")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
