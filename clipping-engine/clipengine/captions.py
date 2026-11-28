"""Caption styling: the owner-approved minimalist Anton style and a small set of options.

Approved default ("anton"):
  * Font: Anton (official Google Fonts build v2.116, SIL OFL 1.1 — licence in
    clipengine/assets/Anton-OFL.txt), bundled in clipengine/assets/fonts and passed to libass
    via `fontsdir`, so the real font is used
    on every machine (verified by parsing libass's `fontselect` log; see verify_font()).
  * Sentence case exactly as transcribed. Text is never upper-cased.
  * Solid white text with a modest solid black outline; no shadow, no background box,
    no colour effects, no animation.
  * 95 px on a 1080×1920 frame (was 73, which was 52 enlarged 40%; enlarged a further 30% at the owner's
    request, 17 Nov 2026).
  * Centred horizontally; caption block centred at 72% of the frame height (lower-middle),
    clear of TikTok/Instagram overlays at the bottom and the right-hand button column.
  * At most two lines, wrapped at word boundaries using Anton's average glyph width; any line still
    wider than the space between the side margins (real glyph widths, FontMetrics) is shrunk to fit.
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


class FontMetrics:
    """Real advance widths from a TrueType file (cmap format 4 + hmtx), read with the standard
    library only. Used to guarantee that no caption line is wider than the safe area: the
    average-width wrap in max_chars() is only an estimate, and a line of wide glyphs could
    otherwise run off the sides of the frame."""

    def __init__(self, path: Path):
        import struct
        data = path.read_bytes()
        n = struct.unpack(">H", data[4:6])[0]
        tables = {data[12 + 16 * i:16 + 16 * i].decode("latin-1"):
                  struct.unpack(">II", data[20 + 16 * i:28 + 16 * i]) for i in range(n)}
        head, hhea, hmtx, cmap = (tables[t][0] for t in ("head", "hhea", "hmtx", "cmap"))
        self.units = struct.unpack(">H", data[head + 18:head + 20])[0]
        n_hmetrics = struct.unpack(">H", data[hhea + 34:hhea + 36])[0]
        adv = [struct.unpack(">H", data[hmtx + 4 * i:hmtx + 4 * i + 2])[0] for i in range(n_hmetrics)]
        # libass sizes a font so that its ascent + descent equals the ASS font size (OS/2 win
        # metrics where present, else hhea).
        if "OS/2" in tables:
            o = tables["OS/2"][0]
            asc, desc = struct.unpack(">HH", data[o + 74:o + 78])
        else:
            asc, desc = struct.unpack(">hh", data[hhea + 4:hhea + 8])
            desc = -desc
        self.size_units = asc + desc
        self.cmap: dict[int, int] = {}
        for i in range(struct.unpack(">H", data[cmap + 2:cmap + 4])[0]):
            pid, eid, off = struct.unpack(">HHI", data[cmap + 4 + 8 * i:cmap + 12 + 8 * i])
            sub = cmap + off
            if pid in (0, 3) and struct.unpack(">H", data[sub:sub + 2])[0] == 4:
                seg2 = struct.unpack(">H", data[sub + 6:sub + 8])[0]
                ends = sub + 14
                starts = ends + seg2 + 2
                deltas, ranges = starts + seg2, starts + 2 * seg2
                for s in range(seg2 // 2):
                    end, start = (struct.unpack(">H", data[b + 2 * s:b + 2 * s + 2])[0] for b in (ends, starts))
                    delta, roff = (struct.unpack(">H", data[b + 2 * s:b + 2 * s + 2])[0] for b in (deltas, ranges))
                    for c in range(start, min(end, 0xFFFE) + 1):
                        if roff:
                            p = ranges + 2 * s + roff + 2 * (c - start)
                            g = struct.unpack(">H", data[p:p + 2])[0]
                            g = (g + delta) & 0xFFFF if g else 0
                        else:
                            g = (c + delta) & 0xFFFF
                        self.cmap.setdefault(c, g)
                break
        self.adv = [adv[min(g, len(adv) - 1)] for g in range(max(self.cmap.values(), default=0) + 1)]
        self.missing = adv[0]

    def width(self, text: str, size: float) -> float:
        """Rendered width in pixels of `text` at ASS font size `size` (no outline)."""
        units = sum(self.adv[g] if (g := self.cmap.get(ord(ch), 0)) < len(self.adv) else self.missing for ch in text)
        return units * size / self.size_units


_METRICS: dict[str, FontMetrics | None] = {}


def font_metrics(font: str) -> FontMetrics | None:
    if font not in _METRICS:
        path = ANTON_FILE if font == "Anton" else None
        try:
            _METRICS[font] = FontMetrics(path) if path and path.exists() else None
        except Exception:  # noqa: BLE001 - fall back to the average-width estimate
            _METRICS[font] = None
    return _METRICS[font]


def fitted_size(lines: list[str], style: "CaptionStyle", width: int = 1080) -> int:
    """Largest font size <= style.size at which every line, including its outline, fits between
    the side margins. Uses the real glyph widths for Anton, the average estimate otherwise."""
    usable = width - 2 * SIDE_MARGIN - 2 * style.outline
    m = font_metrics(style.font)
    widest = max((m.width(l, style.size) if m else len(l) * AVG_EM.get(style.font, 0.5) * style.size)
                 for l in lines) if lines else 0
    if widest <= usable:
        return style.size
    return max(12, int(style.size * usable / widest))


@dataclass
class CaptionStyle:
    preset: str = "anton"
    enabled: bool = True
    font: str = "Anton"
    size: int = 95              # was 73 (52 +40%); +30% at the owner's request (17 Nov 2026), same centre line
    outline: float = 3.0
    position: float = 0.72      # vertical centre of the caption block, as a fraction of frame height
    backing: bool = False       # optional subtle dark box behind text (off by default)
    bold: bool = False          # Anton is a single heavy weight; faux-bold would distort it
    # Small provenance line at the top ("Excerpt · title · m:ss"). Off by default at the owner's request
    # (16 Nov 2026); the source and timestamps stay in each clip's description and provenance record.
    # Settings -> Caption style -> "Small source label at top" turns it back on.
    source_label: bool = False
    # "phrase" = the approved style above (up to two lines per phrase). "active_word" = PROPOSED single-line style
    # (18 Nov 2026, owner-approved for America First Monitor the same day): one line only, white words with the usual black outline, and the word
    # being spoken gets a yellow perimeter outside its black outline, advancing word by word with the audio.
    # Used for brands in DEFAULT_ACTIVE_BRANDS / set_brand_active_word, or for all if chosen in caption_style.
    mode: str = "phrase"
    active_colour: str = "FDFB04"   # the approved title yellow (assets/title_calibration.json)
    active_ring: float = 5.0        # px of yellow outside the black outline, at 1080 px width

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
    if fields.get("size") == 73:           # the superseded default saved by an earlier Settings page: use the new one
        fields.pop("size")
    style = replace(base, **fields)
    return clamp(style)


def clamp(style: CaptionStyle) -> CaptionStyle:
    lo, hi = LIMITS["size"]
    style.size = int(min(hi, max(lo, int(style.size))))
    lo, hi = LIMITS["outline"]
    style.outline = float(min(hi, max(lo, float(style.outline))))
    lo, hi = LIMITS["position"]
    style.position = float(min(hi, max(lo, float(style.position))))
    if style.mode not in MODES:
        style.mode = "phrase"
    c = str(style.active_colour or "").lstrip("#").upper()
    style.active_colour = c if re.fullmatch(r"[0-9A-F]{6}", c) else "FDFB04"
    style.active_ring = float(min(10.0, max(2.0, float(style.active_ring))))
    return style


MODES = ("phrase", "active_word")
# Owner approved the single-line active-word captions for America First Monitor on 18 Nov 2026. Every other brand keeps
# the approved two-line style until the owner approves it for that brand too.
DEFAULT_ACTIVE_BRANDS = ("america_first_monitor",)


def _active_brands(db) -> set:
    raw = db.get_kv("caption_active_brands") if db is not None else None
    try:
        return set(json.loads(raw)) if raw else set(DEFAULT_ACTIVE_BRANDS)
    except ValueError:
        return set(DEFAULT_ACTIVE_BRANDS)


def set_brand_active_word(db, brand_key: str, enabled: bool) -> None:
    """Per-brand switch for the single-line active-word captions (meta key caption_active_brands = JSON list).
    Only call this once the owner has approved the style for that brand."""
    cur = _active_brands(db)
    (cur.add if enabled else cur.discard)(brand_key)
    db.set_kv("caption_active_brands", json.dumps(sorted(cur)))


# Owner decision, Session 21 (28 Nov 2026): automatically generated captions are not burned into new clips for the
# owner's six media brands. Transcription, transcript-led selection and titles continue; the caption renderer stays
# available for customer workspaces and for brands/campaigns the owner approves as an explicit exception.
OWNER_BRANDS_CAPTIONS_OFF = ("afterlive", "america_first_monitor", "operator_notes", "spec_edit", "dial_study",
                             "open_floor")


def caption_exceptions(db) -> set[str]:
    """Brands the owner has explicitly approved for burned-in captions (meta key caption_exception_brands)."""
    raw = db.get_kv("caption_exception_brands")
    try:
        return set(json.loads(raw)) if raw else set()
    except ValueError:
        return set()


def set_caption_exception(db, brand_key: str, allowed: bool) -> None:
    """Owner-approved exception to the captions-off policy. Only call after explicit owner approval."""
    cur = caption_exceptions(db)
    (cur.add if allowed else cur.discard)(brand_key)
    db.set_kv("caption_exception_brands", json.dumps(sorted(cur)))


def captions_blocked_by_policy(db, brand_key: str | None) -> bool:
    return bool(brand_key) and brand_key in OWNER_BRANDS_CAPTIONS_OFF and brand_key not in caption_exceptions(db)


def style_for(db, brand_key: str | None) -> CaptionStyle:
    """The caption style a brand's clips are rendered with: the saved style, switched to active_word for brands
    the owner has approved it for (America First Monitor by default). Owner brands render without generated
    captions unless the owner approved an exception (see OWNER_BRANDS_CAPTIONS_OFF)."""
    st = load_style(db)
    if brand_key and st.mode == "phrase" and brand_key in _active_brands(db):
        st.mode = "active_word"
    if captions_blocked_by_policy(db, brand_key):
        st.enabled = False
    return st


# ---- single-line, active-word captions -------------------------------------------------------------

MAX_GROUP_WORDS = 6
WEAK_END = {"a", "an", "the", "of", "to", "in", "on", "at", "for", "and", "or", "but", "with", "from", "by", "my",
            "your", "our", "their", "his", "her", "its", "that", "this", "i", "we", "you"}
MAX_GROUP_DUR = 2.6     # s on screen before the next group replaces it
MAX_GAP = 0.6           # a pause longer than this starts a new group
MIN_FONT_FRACTION = 0.72  # a single over-long word may shrink this far (95 -> 68 px), never further
SAFE_BOTTOM = 0.86      # lowest point (fraction of frame height) caption ink may reach: clear of platform UI


def _usable(style: CaptionStyle, width: int) -> float:
    return width - 2 * SIDE_MARGIN - 2 * (style.outline + style.active_ring)


def text_width(text: str, style: CaptionStyle, size: float | None = None) -> float:
    size = size or style.size
    m = font_metrics(style.font)
    return m.width(text, size) if m else len(text) * AVG_EM.get(style.font, 0.5) * size


def _timing_ok(ws: list[dict]) -> bool:
    """Word timings usable for word-by-word highlighting: real Whisper timings, positive, ordered and plausible."""
    prev = -1.0
    for w in ws:
        s, e = w.get("start"), w.get("end")
        if s is None or e is None or w.get("est") or e <= s or e - s > 2.5 or s < prev - 0.05:
            return False
        prev = s
    return True


def build_word_groups(words: list[dict], clip_start: float, clip_end: float, style: CaptionStyle,
                      width: int = 1080) -> list[dict]:
    """Short single-line groups of words, relative to the clip start, each guaranteed (real Anton glyph widths) to fit
    between the side margins with its outlines at the normal size. Text is verbatim. A group whose word timings are
    missing or implausible is marked timed=False and is shown as a plain static line (no highlight)."""
    ws = [w for w in words if w["end"] > clip_start and w["start"] < clip_end and w["word"].strip()]
    usable = _usable(style, width)
    groups: list[dict] = []
    cur: list[dict] = []

    def flush():
        if not cur:
            return
        toks = [w["word"].strip() for w in cur]
        text = " ".join(toks)
        size = style.size
        wpx = text_width(text, style)
        if wpx > usable:      # only possible for a single word longer than the line
            size = max(int(style.size * MIN_FONT_FRACTION), int(style.size * usable / wpx))
        groups.append({"start": max(0.0, cur[0]["start"] - clip_start),
                       "end": min(clip_end, cur[-1]["end"]) - clip_start,
                       "words": [{"text": t, "start": w["start"] - clip_start, "end": w["end"] - clip_start}
                                 for t, w in zip(toks, cur)],
                       "timed": _timing_ok(cur), "size": size})
        cur.clear()

    for w in ws:
        tok = w["word"].strip()
        if cur:
            prev = cur[-1]["word"].strip()
            line = " ".join(x["word"].strip() for x in cur) + " " + tok
            natural = w["start"] - cur[-1]["end"] > MAX_GAP or (prev.endswith((",", ";", ":")) and len(cur) >= 2)
            forced = (w["end"] - cur[0]["start"] > MAX_GROUP_DUR or len(cur) >= MAX_GROUP_WORDS
                      or text_width(line, style) > usable)
            if natural:
                flush()
            elif forced:
                # Never end a line on "the", "a", "of"...: carry it into the next group (it is spoken right before).
                carry = []
                while len(cur) >= 3 and len(carry) < 2 and cur[-1]["word"].strip().lower() in WEAK_END:
                    carry.insert(0, cur.pop())
                flush()
                cur.extend(carry)
        cur.append(w)
        if tok.endswith((".", "?", "!")):
            flush()
    flush()
    for i, g in enumerate(groups):       # same smoothing as the phrase captions
        nxt = groups[i + 1]["start"] if i + 1 < len(groups) else clip_end - clip_start
        if nxt - g["end"] < 0.35:
            g["end"] = nxt
        if g["end"] - g["start"] < 0.8:
            g["end"] = min(nxt, g["start"] + 0.8)
    return groups


def active_word_y(style: CaptionStyle, width: int, height: int, fg_h: float | None, line_px: int = 2) -> int:
    """Centre line for single-line captions: the approved lower-middle position, moved down only as far as needed to
    clear the picture and its frame line, and never below the platform-safe bottom."""
    y = height * style.position
    half = style.size / 2 + style.outline + style.active_ring
    if fg_h:
        pic_bottom = (height + fg_h) / 2 + line_px
        if pic_bottom < height * 0.97:
            y = max(y, pic_bottom + 24 + half)
    return int(min(y, height * SAFE_BOTTOM - half))


def _group_events(g: dict, style: CaptionStyle, x: int, y: int) -> list[str]:
    pos = f"\\an5\\pos({x},{y})" + (f"\\fs{g['size']}" if g["size"] != style.size else "")
    plain = " ".join(_esc(w["text"]) for w in g["words"])
    if not g["timed"]:
        return [f"Dialogue: 1,{_ts(g['start'])},{_ts(g['end'])},Caption,,0,0,0,,{{{pos}}}{plain}"]
    yel = f"&H00{style.active_colour[4:6]}{style.active_colour[2:4]}{style.active_colour[0:2]}&"
    ring = style.outline + style.active_ring
    out = []
    n = len(g["words"])
    for i, w in enumerate(g["words"]):
        s = g["start"] if i == 0 else max(g["start"], w["start"])
        e = g["end"] if i == n - 1 else max(s, min(g["end"], g["words"][i + 1]["start"]))
        if e - s < 0.01:
            continue
        # Layer 0: only the active word is drawn, as a yellow outline ring (fill and others transparent).
        # Layer 1: the whole line exactly as the approved style draws it. Same string, same anchor -> identical layout,
        # so the line never moves; only the ring advances.
        ringed = " ".join((f"{{\\1a&HFF&\\3a&H00&\\3c{yel}\\bord{ring:.1f}}}" if j == i else "{\\alpha&HFF&\\bord0}")
                          + _esc(v["text"]) for j, v in enumerate(g["words"]))
        out.append(f"Dialogue: 0,{_ts(s)},{_ts(e)},Caption,,0,0,0,,{{{pos}}}{ringed}")
        out.append(f"Dialogue: 1,{_ts(s)},{_ts(e)},Caption,,0,0,0,,{{{pos}}}{plain}")
    return out


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
              label: str | None = None, title_events: tuple[str, str] | None = None,
              groups: list[dict] | None = None, fg_h: float | None = None) -> str:
    """ASS document for the given phrases. Text is used verbatim (sentence case preserved)."""
    bold = -1 if style.bold else 0
    if style.backing:
        # BorderStyle 3 = box behind text; translucent black (alpha 0x70), small padding.
        border_style, outline_col, outline = 3, "&H70000000", max(4.0, style.outline * 2)
    else:
        border_style, outline_col, outline = 1, "&H00000000", style.outline
    label_size = max(24, int(style.size * 0.5))
    title_style_line = (title_events[0] + "\n") if title_events and title_events[0] else ""
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
{title_style_line}
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    x, y = width // 2, int(height * style.position)
    out = []
    if title_events and title_events[1]:
        out.append(title_events[1])
    if label and style.source_label and phrases:
        out.append(f"Dialogue: 1,{_ts(0)},{_ts(phrases[-1]['end'] + 5)},Label,,0,0,0,,{_esc(label)}")
    if style.mode == "active_word" and groups is not None:
        ya = active_word_y(style, width, height, fg_h)
        for g in groups:
            out.extend(_group_events(g, style, x, ya))
        return header + "\n".join(out) + "\n"
    for p in phrases:
        lines = p["lines"][:2]
        text = "\\N".join(_esc(line) for line in lines)
        fs = fitted_size(lines, style, width)
        shrink = f"\\fs{fs}" if fs < style.size else ""         # only a line that would cross the margins
        out.append(f"Dialogue: 0,{_ts(p['start'])},{_ts(p['end'])},Caption,,0,0,0,,"
                   f"{{\\an5\\pos({x},{y}){shrink}}}{text}")
    return header + "\n".join(out) + "\n"


def fontsdir_for(cwd: Path) -> str:
    """Path to the bundled fonts relative to the FFmpeg working directory (avoids Windows
    drive-letter escaping inside filtergraphs). Falls back to copying the font if the data
    folder is on a different drive."""
    try:
        rel = os.path.relpath(FONTS_DIR, cwd)
        # libass opens <cwd>/<rel>/<file> un-normalised; past Windows' 260-character limit it silently falls back to
        # Arial (seen 18 Nov 2026 with a deep working folder). Copy the font next to the job instead.
        if len(str(Path(cwd).resolve())) + len(rel) + len(ANTON_FILE.name) + 2 > 240:
            raise ValueError("path too long for libass")
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
