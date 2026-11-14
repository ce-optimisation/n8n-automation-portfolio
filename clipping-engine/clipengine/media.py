"""FFmpeg / FFprobe helpers."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable

from .jobs import PermanentError

_CREATE_NO_WINDOW = 0x08000000


def _popen_kwargs() -> dict:
    import os
    return {"creationflags": _CREATE_NO_WINDOW} if os.name == "nt" else {}


def require(tool: str) -> str:
    path = shutil.which(tool)
    if not path:
        raise PermanentError(
            f"{tool} was not found on PATH. Install FFmpeg (Windows: run 'winget install Gyan.FFmpeg' "
            f"or place ffmpeg.exe/ffprobe.exe in clipping-engine\\tools\\ffmpeg\\bin) and restart.")
    return path


def probe(path: Path) -> dict:
    ffprobe = require("ffprobe")
    proc = subprocess.run([ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams",
                           str(path)], capture_output=True, text=True, encoding="utf-8", errors="replace",
                          **_popen_kwargs())
    if proc.returncode != 0:
        raise PermanentError(f"ffprobe could not read the file: {proc.stderr.strip()[:500]}")
    data = json.loads(proc.stdout or "{}")
    v = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    a = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    fmt = data.get("format", {})
    duration = float(fmt.get("duration") or (v or {}).get("duration") or 0)
    fps = 0.0
    if v and v.get("avg_frame_rate") and v["avg_frame_rate"] != "0/0":
        n, _, d = v["avg_frame_rate"].partition("/")
        fps = float(n) / float(d or 1) if float(d or 1) else 0.0
    width, height = (v or {}).get("width"), (v or {}).get("height")
    rot = 0
    for sd in (v or {}).get("side_data_list", []) or []:
        if "rotation" in sd:
            rot = int(sd["rotation"])
    if (v or {}).get("tags", {}).get("rotate"):
        rot = int(v["tags"]["rotate"])
    if abs(rot) in (90, 270) and width and height:
        width, height = height, width
    return {
        "duration": duration, "width": width, "height": height, "fps": round(fps, 3),
        "has_video": v is not None, "has_audio": a is not None,
        "video_codec": (v or {}).get("codec_name"), "audio_codec": (a or {}).get("codec_name"),
        "format_name": fmt.get("format_name"), "tags": fmt.get("tags", {}),
    }


_TIME_RE = re.compile(r"out_time_us=(\d+)")


def run_ffmpeg(args: list[str], *, duration: float | None = None,
               on_progress: Callable[[float], None] | None = None, cwd: Path | None = None) -> None:
    """Run ffmpeg, reporting fractional progress. Raises PermanentError with stderr tail on failure."""
    ffmpeg = require("ffmpeg")
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-progress", "pipe:1", "-nostats", *args]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", errors="replace", cwd=str(cwd) if cwd else None,
                            **_popen_kwargs())
    stderr_tail: list[str] = []

    import threading

    def _drain_err():
        for line in proc.stderr:  # type: ignore[union-attr]
            stderr_tail.append(line)
            if len(stderr_tail) > 60:
                del stderr_tail[:20]
    t = threading.Thread(target=_drain_err, daemon=True)
    t.start()
    for line in proc.stdout:  # type: ignore[union-attr]
        m = _TIME_RE.match(line.strip())
        if m and duration and on_progress:
            on_progress(min(1.0, int(m.group(1)) / 1e6 / duration))
    proc.wait()
    t.join(timeout=5)
    if proc.returncode != 0:
        raise PermanentError("ffmpeg failed: " + "".join(stderr_tail[-15:]).strip()[-1500:])


def extract_audio(src: Path, dst: Path, on_progress=None, duration: float | None = None) -> Path:
    """Extract mono 16 kHz WAV, the format Whisper expects."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(["-i", str(src), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(dst)],
               duration=duration, on_progress=on_progress)
    return dst
