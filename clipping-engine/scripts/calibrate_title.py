"""Measure the approved America First Monitor title concept and write clipengine/assets/title_calibration.json.

    python scripts/calibrate_title.py path/to/concept.png

Reproducible: everything the renderer needs is derived from the image's pixels (cap heights, line pitch,
yellow colour, perimeter thickness, vertical position) and from a libass test render that gives the font's
real cap-height ratio. Only the measured numbers are stored; the image itself is NOT copied into the repo.
Requires Pillow, numpy and FFmpeg (all present in the project environment / CI).
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "clipengine" / "assets" / "title_calibration.json"
FRAME_W, FRAME_H = 1080, 1920


def spans(mask, minc=3, gap=6):
    idx = np.where(mask.sum(axis=1) >= minc)[0]
    out, st, p = [], None, None
    for i in idx:
        if st is None:
            st = p = i
        elif i - p > gap:
            out.append((int(st), int(p)))
            st = p = i
        else:
            p = i
    if st is not None:
        out.append((int(st), int(p)))
    return out


def libass_cap_metrics() -> tuple[float, float]:
    """(cap height, cap-centre offset from the \\an5 anchor) per ASS font-size unit for the bundled Anton, from a real libass render."""
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        (d / "t.ass").write_text(
            "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\nWrapStyle: 2\n\n[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
            "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
            "MarginR, MarginV, Encoding\nStyle: T,Anton,200,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,5,0,0,0,1\n\n"
            "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
            "Dialogue: 0,0:00:00.00,0:00:02.00,T,,0,0,0,,{\\an5\\pos(540,960)}HEHEH\n", encoding="utf-8")
        fonts = ROOT / "clipengine" / "assets" / "fonts"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=black:s=1080x1920:d=1", "-vf",
                        f"ass=t.ass:fontsdir={fonts.as_posix()}", "-frames:v", "1", "t.png"], cwd=d, check=True)
        a = np.asarray(Image.open(d / "t.png").convert("L")).astype(int)
        rows = np.where((a > 200).sum(axis=1) > 0)[0]
        return float((rows.max() - rows.min() + 1) / 200), float(((rows.max() + rows.min()) / 2 - 960) / 200)


def main(path: str) -> int:
    im = Image.open(path).convert("RGB")
    W, H = im.size
    k = FRAME_W / W
    a = np.asarray(im).astype(int)
    band = a[: int(H * 0.37)]                                   # the title sits in the upper band
    white = (band[..., 0] > 235) & (band[..., 1] > 235) & (band[..., 2] > 235)
    yellow = (band[..., 0] > 220) & (band[..., 1] > 190) & (band[..., 2] < 90)
    ws = spans(white)
    if len(ws) < 2:
        print("Expected a two-line title in the upper band; found", ws)
        return 1
    (l1a, l1b), (l2a, l2b) = ws[0], ws[1]
    cap1 = (l1b - l1a + 1)
    q = white[l2a:l2b + 1]
    cols = np.where(q.sum(axis=0) > 0)[0]
    last = cols.max()                                           # the closing "?" is plain white: its height is the line's cap height
    qcols = np.where(white[l2a:l2b + 1, max(0, last - 60):last + 1].sum(axis=1) > 0)[0]
    cap2 = int(qcols.max() - qcols.min() + 1)
    ycols = np.where(yellow.sum(axis=0) > 0)[0]
    ys = spans(yellow)
    ycol = np.median(band[yellow], axis=0).astype(int)
    # perimeter thickness: length of the LEFT-MOST yellow run (the outer ring of the first accented word),
    # sampled over the middle rows of line 2 where the stem of its first letter is crossed
    loose = (band[..., 0] > 150) & (band[..., 1] > 140) & (band[..., 2] < 110)   # includes anti-aliased ring pixels
    runs_ = []
    for row in range(l2a + (l2b - l2a) // 4, l2b - (l2b - l2a) // 4):
        xs = np.where(loose[row])[0]
        if len(xs):
            n = 1
            while n < len(xs) and xs[n] == xs[n - 1] + 1:
                n += 1
            runs_.append(n)
    perim = int(np.median(runs_)) if runs_ else 0
    ratio, cap_off = libass_cap_metrics()
    # black ring: dark pixels immediately inside the left-most yellow ring, same sampling rows
    dark_runs = []
    for row in range(l2a + (l2b - l2a) // 4, l2b - (l2b - l2a) // 4):
        xs = np.where(loose[row])[0]
        if len(xs):
            x = xs[0]
            while x < band.shape[1] and loose[row, x]:
                x += 1
            x += 2                                              # step over the anti-aliased edge of the ring
            n = 0
            while x + n < band.shape[1] and band[row, x + n].sum() < 150:
                n += 1
            dark_runs.append(n)
    black = int(np.median(dark_runs)) if dark_runs else 0
    lead_w = np.where(white[l1a:l1b + 1].sum(axis=0) > 0)[0]
    cal = {
        "source": "approved America First Monitor title concept, measured 17 Nov 2026 (image not stored)",
        "image_px": [W, H], "scale_to_1080": round(k, 4),
        "cap_height_px_1080": {"lead": round(cap1 * k, 1), "emphasis": round(cap2 * k, 1)},
        "libass_cap_ratio": round(ratio, 4),
        "libass_cap_centre_offset": round(cap_off, 4),
        "lead_ass_size_1080": round(cap1 * k / ratio, 1),
        "emphasis_scale": round(cap2 / cap1, 3),
        "line_pitch_px_1080": round(((l2a + l2b) / 2 - (l1a + l1b) / 2) * k, 1),
        "title_centre_y_frac": round(((l1a + ys[-1][1]) / 2) / H, 4),
        "lead_width_frac": round((lead_w.max() - lead_w.min() + 1) / W, 3),
        "yellow_rgb": [int(c) for c in ycol],
        "accent_perimeter_px_1080": round(perim * k, 1),
        "accent_black_ring_px_1080": round(black * k, 1),
    }
    OUT.write_text(json.dumps(cal, indent=1), encoding="utf-8")
    print(json.dumps(cal, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]) if len(sys.argv) > 1 else print(__doc__) or 2)
