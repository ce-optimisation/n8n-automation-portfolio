"""Caption styling: the owner-approved minimalist Anton style and a small set of options.

Approved default ("anton"):
  * Font: Anton (official Google Fonts build v2.116, SIL OFL 1.1 — licence in
    clipengine/assets/Anton-OFL.txt), bundled in clipengine/assets/fonts and passed to libass
    via `fontsdir`, so the real font is used
    on every machine (verified by parsing libass's `fontselect` log; see verify_font()).
  * Sentence case exactly as transcribed. Text is never upper-cased.
  * Solid white text with a modest solid black outline; no shadow, no background box,
    no colour effects, no animation.
  * Relatively small (52 px on a 1080×1920 frame; Anton's capitals are ~45 px tall).
  * Centred horizontally; caption block centred at 72% of the frame height (lower-middle),
    clear of TikTok/Instagram overlays at the bottom and the right-hand button column.
  * At most two lines, wrapped at word boundaries using Anton's measured average glyph width.
An optional subtle dark backing exists for difficult footage; it is off by default.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass, replace
from pathlib import Path

FONTS_DIR = Path(__file__).resolve().parent / "assets" / "fonts"
ANTON_FILE = FONTS_DIR / "Anton-Regular.ttf"

# Average advance width per character in em, measured from the font files (fontTools).
AVG_EM = {"Anton": 0.391, "Arial": 0.50}
SIDE_MARGIN = 120          # px each side: clears TikTok's right-hand action buttons


@dataclass
class CaptionStyle:
    preset: str = "anton"
    enabled: bool = True
    font: str = "Anton"
    size: int = 52
    outline: float = 3.0
    position: float = 0.72      # vertical centre of the caption block, as a fraction of frame height
    backing: bool = False       # optional subtle dark box behind text (off by default)
    bold: bool = False          # Anton is a single heavy weight; faux-bold would distort it
    source_label: bool = True   # small provenance line at the top ("Excerpt · title · m:ss")

    def max_chars(self, width: int = 1080) -> int:
        usable = width - 2 * SIDE_MARGIN
        return max(16, int(usable / (AVG_EM.get(self.font, 0.5) * self.size) * 0.92))


PRESETS = {
    "anton": CaptionStyle(),
    "classic": CaptionStyle(preset="classic", font="Arial", size=60, outline=4.0, bold=True),
}
LIMITS = {"size": (32, 96), "outline": (0.0, 8.0), "position": (0.45, 0.85)}


def load_style(db=None) -> CaptionStyle:
    """Default Anton style, with owner overrides saved from Settings (meta key caption_style)."""
    raw = db.get_kv("caption_style") if db is not None else None
    data = json.loads(raw) if raw else {}
    base = PRESETS.get(data.get("preset", "anton"), PRESETS["anton"])
    fields = {k: v for k, v in data.items() if k in CaptionStyle.__dataclass_fields__ and k != "font"}
    style = replace(base, **fields)
    return clamp(style)


def clamp(style: CaptionStyle) -> CaptionStyle:
    lo, hi = LIMITS["size"]
    style.size = int(min(hi, max(lo, int(style.size))))
    lo, hi = LIMITS["outline"]
    style.outline = float(min(hi, max(lo, float(style.outline))))
    lo, hi = LIMITS["position"]
    style.position = float(min(hi, max(lo, float(style.position))))
    return style


def save_style(db, style: CaptionStyle) -> None:
    db.set_kv("caption_style", json.dumps(asdict(clamp(style))))


def _esc(s: str) -> str:
    return s.replace("\\", "⧵").replace("{", "(").replace("}", ")")


def _ts(t: float) -> str:
    cs = int(round(max(0.0, t) * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def build_ass(phrases: list[dict], style: CaptionStyle, width: int = 1080, height: int = 1920,
              label: str | None = None) -> str:
    """ASS document for the given phrases. Text is used verbatim (sentence case preserved)."""
    bold = -1 if style.bold else 0
    if style.backing:
        # BorderStyle 3 = box behind text; translucent black (alpha 0x70), small padding.
        border_style, outline_col, outline = 3, "&H70000000", max(4.0, style.outline * 2)
    else:
        border_style, outline_col, outline = 1, "&H00000000", style.outline
    label_size = max(24, int(style.size * 0.5))
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,{style.font},{style.size},&H00FFFFFF,&H00FFFFFF,{outline_col},&H00000000,{bold},0,0,0,100,100,0,0,{border_style},{outline:.1f},0,5,{SIDE_MARGIN},{SIDE_MARGIN},0,1
Style: Label,{style.font},{label_size},&H00F0F0F0,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,{max(1.0, style.outline * 0.6):.1f},0,8,60,60,110,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    x, y = width // 2, int(height * style.position)
    out = []
    if label and style.source_label and phrases:
        out.append(f"Dialogue: 1,{_ts(0)},{_ts(phrases[-1]['end'] + 5)},Label,,0,0,0,,{_esc(label)}")
    for p in phrases:
        text = "\\N".join(_esc(line) for line in p["lines"][:2])
        out.append(f"Dialogue: 0,{_ts(p['start'])},{_ts(p['end'])},Caption,,0,0,0,,{{\\an5\\pos({x},{y})}}{text}")
    return header + "\n".join(out) + "\n"


def fontsdir_for(cwd: Path) -> str:
    """Path to the bundled fonts relative to the FFmpeg working directory (avoids Windows
    drive-letter escaping inside filtergraphs). Falls back to copying the font if the data
    folder is on a different drive."""
    try:
        rel = os.path.relpath(FONTS_DIR, cwd)
        return rel.replace("\\", "/")
    except ValueError:
        local = Path(cwd) / "fonts"
        local.mkdir(exist_ok=True)
        target = local / ANTON_FILE.name
        if not target.exists():
            target.write_bytes(ANTON_FILE.read_bytes())
        return "fonts"


FONTSELECT_RE = re.compile(r"fontselect:\s*\((?P<req>[^,]+),[^)]*\)\s*->\s*(?P<file>[^,]+),\s*\d+,\s*(?P<ps>\S+)")


def verify_font(work_dir: Path, style: CaptionStyle | None = None) -> dict:
    """Render one frame with libass at verbose log level and report which font file libass
    actually selected. Returns {requested, file, postscript, ok, image}."""
    from . import media
    style = style or PRESETS["anton"]
    work_dir.mkdir(parents=True, exist_ok=True)
    ass = work_dir / "fontcheck.ass"
    ass.write_text(build_ass([{"start": 0, "end": 2, "lines": ["The council will vote in March,",
                                                                  "and every resident can comment."]}],
                             style, label="Excerpt · Font check · 0:00"), encoding="utf-8")
    png = work_dir / "fontcheck.png"
    ffmpeg = media.require("ffmpeg")
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-v", "verbose", "-y", "-f", "lavfi", "-i", "color=c=0x3a4a5a:s=1080x1920:d=1",
         "-vf", f"ass={ass.name}:fontsdir={fontsdir_for(work_dir)}", "-frames:v", "1", png.name],
        cwd=work_dir, capture_output=True, text=True, encoding="utf-8", errors="replace")
    picks = [m.groupdict() for m in FONTSELECT_RE.finditer(proc.stderr)]
    cap = next((p for p in picks if p["req"].strip() == style.font), picks[0] if picks else None)
    loaded_bundled = f"{ANTON_FILE.name}'" in proc.stderr and "Loading font file" in proc.stderr
    if style.font == "Anton":
        # libass reports fonts loaded from fontsdir as in-memory fonts named by PostScript name.
        ok = bool(cap) and cap["ps"] == "Anton-Regular" and loaded_bundled
    else:
        ok = bool(cap) and style.font.lower().replace(" ", "") in (cap["ps"] or "").lower()
    substituted = bool(cap) and not ok
    return {"substituted_by": cap["ps"] if substituted else None,"requested": style.font, "selected": cap["file"].strip() if cap else None,
            "postscript": cap["ps"] if cap else None, "bundled_font_loaded": loaded_bundled, "ok": ok and proc.returncode == 0,
            "image": str(png) if png.exists() else None, "returncode": proc.returncode}
