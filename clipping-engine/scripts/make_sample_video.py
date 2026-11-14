"""Generate a short, lawful sample recording for testing.

The speech is original text written for this project and synthesised locally
with FFmpeg's built-in flite voice; the picture is FFmpeg's test pattern.
No third-party content is involved.

    python scripts/make_sample_video.py [output.mp4]
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PARAGRAPHS = [
    "Good evening. Tonight the council will discuss the proposal to rebuild the river bridge, "
    "and I want to explain the plan in plain terms.",
    "The current bridge was built in nineteen fifty eight. Engineers inspected it last spring. "
    "They found that two of the main supports have lost about thirty percent of their strength. "
    "That is the reason the weight limit was lowered in June.",
    "So what does the plan actually propose? First, a new bridge with two traffic lanes and a separate path "
    "for people walking and cycling. Second, construction would start next year and take about eighteen months. "
    "Third, the cost is estimated at forty two million pounds, and the national transport fund would pay "
    "for roughly half of that amount.",
    "A resident asked me last week whether the ferry would run during the works. The answer is yes. "
    "The council has agreed to fund a temporary ferry service, and it would run every twenty minutes during the day.",
    "There are reasonable objections. Some businesses on the east bank worry that customers will stay away "
    "while the road is closed. The council should take that seriously, and the plan includes a small support "
    "fund for affected shops.",
    "Let me be clear about what is not yet decided. The final design has not been chosen, and the public "
    "consultation is open until the end of March. Every resident can read the documents at the library or online.",
    "Thank you for listening. The full report, including the engineering study, is available to everyone.",
]


def make(out: Path) -> Path:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise SystemExit("ffmpeg not found")
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        parts = []
        timing = []
        t = 0.0
        for i, p in enumerate(PARAGRAPHS):
            (td / f"p{i}.txt").write_text(p, encoding="utf-8")
            wav = td / f"p{i}.wav"
            # flite's textfile path is used relative to cwd to avoid filter-escaping issues on Windows.
            subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                            f"flite=textfile=p{i}.txt:voice=slt", "-ar", "16000", "-ac", "1", wav.name],
                           check=True, cwd=td)
            parts.append(wav.name)
            dur = float(subprocess.run([shutil.which("ffprobe"), "-v", "error", "-show_entries", "format=duration",
                                        "-of", "csv=p=0", wav.name], capture_output=True, text=True,
                                       cwd=td).stdout.strip())
            timing.append({"start": round(t, 3), "end": round(t + dur, 3), "text": p})
            t += dur + 1.2
            sil = f"s{i}.wav"
            subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                            "anullsrc=r=16000:cl=mono", "-t", "1.2", sil], check=True, cwd=td)
            parts.append(sil)
        (td / "list.txt").write_text("".join(f"file '{p}'\n" for p in parts), encoding="utf-8")
        subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i",
                        "list.txt", "-c", "copy", "speech.wav"], check=True, cwd=td)
        subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                        "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=25",
                        "-i", "speech.wav", "-shortest", "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
                        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k", str(out.resolve())],
                       check=True, cwd=td)
    out.with_suffix(".timing.json").write_text(json.dumps({"paragraphs": timing}, indent=2), encoding="utf-8")
    return out


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/samples/sample_council_briefing.mp4")
    print(make(target))
