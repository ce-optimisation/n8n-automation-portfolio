"""Render preview frames of the vertical composition (hook title + blue frame lines + captions) for several input formats.
Usage: python scripts/preview_frames.py OUT_DIR [source.mp4]   (default source: the synthetic sample)"""
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from clipengine import captions, framelines, hooks, render, titles  # noqa: E402
from clipengine.config import get_settings  # noqa: E402

FORMATS = {"16x9": (1280, 720), "4x3": (640, 480), "1x1": (720, 720), "9x16": (720, 1280)}
TITLE = "Fuentes on Canadian steel"


def main(out: Path, src: Path | None):
    out.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("CLIPENGINE_DATA_DIR", str(out / "data"))
    s = get_settings(llm_provider="none", open_browser=False)
    if src is None:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from make_sample_video import make
        src = out / "sample.mp4"
        make(src)
    words = [{"word": w, "start": i * .4, "end": i * .4 + .35} for i, w in enumerate("the committee will publish its findings".split())]
    for name, (w, h) in FORMATS.items():
        fsrc = out / f"src_{name}.mp4"          # a REAL source of that shape (the declared size must match the media)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-t", "4", "-i", str(src), "-vf", f"scale={w}:{h}", "-c:v", "libx264",
                        "-preset", "ultrafast", "-c:a", "aac", str(fsrc)], check=True)
        r = render.render_clip(s, fsrc, out / name, 0.0, 3.0, words, w, h, None, style=captions.CaptionStyle(),
                               title=hooks.render_markup(TITLE), title_style=titles.TitleStyle(enabled=True),
                               line_style=framelines.LineStyle(enabled=True))
        png = out / f"preview_{name}.png"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", "1.2", "-i", str(r["video"]), "-frames:v", "1", str(png)], check=True)
        print(png, "lines" if r["frame_lines"] else "no lines (picture fills the frame)")


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]) if len(sys.argv) > 2 else None)
