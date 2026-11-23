"""Top title treatment for vertical clips (the upper black/blurred band above the central video).

The look reproduces the approved America First Monitor concept, 20% smaller:
  * bold condensed Anton capitals, white, black outline and soft shadow, centred in the upper band;
  * emphasised words get a thick black ring and a vivid yellow perimeter outside it: `*word*` keeps the white
    fill, `**word**` fills the word yellow (the concept shows both);
  * the line that carries the emphasis is set larger than the lead line, as in the concept.

Calibration is MEASURED, not assumed: ``scripts/calibrate_title.py concept.png`` reads the approved image
(cap heights, line pitch, yellow colour, ring thickness, vertical position) and a real libass render
(Anton's cap-height ratio) and writes ``assets/title_calibration.json``. All sizes below derive from it and are
multiplied by ``scale`` (0.80) because the owner asked for the title to be 20% smaller than pictured.

Titles are plain text (any clip, any brand, no renderer change). They are fitted with real Anton glyph widths so
nothing crosses the safe margins or leaves the band: long titles shrink uniformly and keep their look.

OFF unless meta key ``title_style`` has {"enabled": true} (a brand can switch it on via ``set_brand_enabled``).
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from . import captions

CAL_FILE = Path(__file__).resolve().parent / "assets" / "title_calibration.json"
# Measured from the approved concept on 17 Nov 2026 (used only if the calibration file is missing).
_DEFAULT_CAL = {"lead_ass_size_1080": 199.4, "emphasis_scale": 1.209, "libass_cap_ratio": 0.495,
                "libass_cap_centre_offset": 0.06, "line_pitch_px_1080": 152.6,
                "cap_height_px_1080": {"lead": 98.7, "emphasis": 119.4}, "title_centre_y_frac": 0.15,
                "yellow_rgb": [253, 251, 4], "accent_perimeter_px_1080": 4.6, "accent_black_ring_px_1080": 9.2}


def calibration() -> dict:
    try:
        return {**_DEFAULT_CAL, **json.loads(CAL_FILE.read_text(encoding="utf-8"))}
    except (OSError, ValueError):
        return dict(_DEFAULT_CAL)


def _bgr(rgb) -> str:
    r, g, b = rgb
    return f"&H00{b:02X}{g:02X}{r:02X}&"


@dataclass
class TitleStyle:
    enabled: bool = False
    font: str = "Anton"
    scale: float = 0.80             # owner request: 20% smaller than the approved concept
    max_lines: int = 4
    side_margin: int = 56
    uppercase: bool = True
    # Session 13 (America First Monitor only): one meaningful yellow phrase, smaller, higher, softer shadow.
    variant: str = "classic"
    centre_frac: float | None = None
    emphasis_scale: float | None = None
    shadow_factor: float = 1.0
    single_phrase: bool = False

    @property
    def reference_size(self) -> int:
        """ASS font size of the concept's lead line at 1080 px width (measured)."""
        return int(round(calibration()["lead_ass_size_1080"]))

    @property
    def size(self) -> int:
        return int(round(calibration()["lead_ass_size_1080"] * self.scale))


def load_style(db=None) -> TitleStyle:
    raw = db.get_kv("title_style") if db is not None else None
    data = json.loads(raw) if raw else {}
    return TitleStyle(**{k: v for k, v in data.items() if k in TitleStyle.__dataclass_fields__ and k != "font"})


def save_style(db, style: TitleStyle) -> None:
    db.set_kv("title_style", json.dumps(asdict(style)))


def set_brand_enabled(db, brand_key: str, enabled: bool) -> None:
    """Per-brand switch (meta key title_brands = JSON list). The global `title_style.enabled` still wins if set."""
    cur = set(json.loads(db.get_kv("title_brands") or "[]"))
    (cur.add if enabled else cur.discard)(brand_key)
    db.set_kv("title_brands", json.dumps(sorted(cur)))


AFM_BRAND = "america_first_monitor"
# Owner-approved 19 Nov 2026 (ChatGPT proposal, Session 12 mockups): 0.60 scale (0.56 only if fitting needs it - the existing
# shrink loop already does that), centre at 18% of frame height, ONE yellow phrase at the same size as the rest, shadow at 35%.
AFM_V2 = {"scale": 0.60, "centre_frac": 0.18, "emphasis_scale": 1.0, "shadow_factor": 0.35, "single_phrase": True,
          "variant": "afm_v2"}


def style_for(db, brand_key: str | None) -> TitleStyle:
    st = load_style(db)
    if not st.enabled and brand_key and brand_key in set(json.loads(db.get_kv("title_brands") or "[]")):
        st.enabled = True
    if brand_key == AFM_BRAND and (db.get_kv("title_afm_v2") if db is not None else None) != "0":
        for k, v in AFM_V2.items():          # other brands are never touched: they keep the classic look byte-for-byte
            setattr(st, k, v)
    return st


# ---- parsing and layout ------------------------------------------------------------------------

def parse(text: str, upper: bool = True) -> list[tuple[str, int, bool]]:
    """(word, mode, glued): mode 0 plain, 1 `*white with yellow perimeter*`, 2 `**yellow with perimeter**`;
    glued=True means no space before the word (e.g. the `?` in `**Palestine**?`)."""
    words, one, two, space = [], False, False, True
    for m in re.finditer(r"\s+|\*\*|\*|[^\s*]+", text or ""):
        tok = m.group(0)
        if tok.isspace():
            space = True
        elif tok == "**":
            two = not two
        elif tok == "*":
            one = not one
        else:
            words.append((tok.upper() if upper else tok, 2 if two else 1 if one else 0, not space and bool(words)))
            space = False
    return words


def _text(words) -> str:
    return "".join((" " if i and not g else "") + w for i, (w, _, g) in enumerate(words))


def _width(words, size: float, font: str = "Anton") -> float:
    m = captions.font_metrics(font)
    text = _text(words)
    return m.width(text, size) if m else len(text) * captions.AVG_EM.get(font, 0.5) * size


def _wrap(words, size: float, usable: float, font: str):
    """Greedy wrap; a glued word never starts a line (it stays with the word before it)."""
    lines, cur = [], []
    for w in words:
        if cur and not w[2] and _width(cur + [w], size, font) > usable:
            lines.append(cur)
            cur = []
        cur.append(w)
    if cur:
        lines.append(cur)
    return lines


def _segments(words):
    first = next((i for i, (_, m, _g) in enumerate(words) if m), None)
    if first is None:
        return [(words, False)]
    return [(s, emph) for s, emph in ((words[:first], False), (words[first:], True)) if s]


def layout(words, style: TitleStyle, width: int, height: int, fg_h: float) -> list[dict]:
    """Lines with their ASS size and the y of their cap-centre, fitted to the width and the band."""
    cal = calibration()
    k = style.scale
    ring = (cal["accent_black_ring_px_1080"] + cal["accent_perimeter_px_1080"]) * k
    usable = width - 2 * style.side_margin - 2 * ring
    band_h = (height - fg_h) / 2
    ratio, off = cal["libass_cap_ratio"], cal["libass_cap_centre_offset"]
    gap_ratio = (cal["line_pitch_px_1080"] - (cal["cap_height_px_1080"]["lead"] + cal["cap_height_px_1080"]["emphasis"]) / 2) \
        / cal["cap_height_px_1080"]["lead"]
    base = cal["lead_ass_size_1080"] * k
    emph_scale = style.emphasis_scale if style.emphasis_scale is not None else cal["emphasis_scale"]
    shrink = 1.0
    while True:
        lines = []
        for seg, emph in _segments(words):
            size = base * shrink * (emph_scale if emph else 1.0)
            for ln in _wrap(seg, size, usable, style.font):
                lines.append({"words": ln, "size": size, "emph": emph})
        caps = [ratio * l["size"] for l in lines]
        gap = gap_ratio * ratio * base * shrink
        block = sum(caps) + gap * max(0, len(lines) - 1)
        room = band_h - 48 if band_h >= 160 else 10 ** 6
        fits_w = all(_width(l["words"], l["size"], style.font) <= usable + 0.5 for l in lines)
        if (fits_w and len(lines) <= style.max_lines and block <= room) or shrink < 0.25:
            break
        shrink -= 0.02
    centre = (style.centre_frac if style.centre_frac is not None else cal["title_centre_y_frac"]) * height
    if style.centre_frac is None and band_h >= 160:
        centre = band_h / 2 + height * (48 / 1920)
    pad_b = 60 if style.single_phrase else 24          # AFM v2: the block stays >= 60 px clear of the picture
    if band_h >= 160:
        centre = min(max(centre, 24 + block / 2), band_h - pad_b - block / 2)
    else:                                   # a (near) full-frame source leaves no band: sit in the top safe area
        centre = max(centre * 0.55, block / 2 + 90)
    y = centre - block / 2
    for l, cap in zip(lines, caps):
        l["cap"] = cap
        l["y"] = y + cap / 2 - off * l["size"]          # \an5 anchors the line box: shift so the CAPS are centred at y
        y += cap + gap
    return lines


# ---- ASS ---------------------------------------------------------------------------------------

def _line_text(line, style: TitleStyle, ring_layer: bool, cal: dict) -> str:
    k = style.scale
    yellow = _bgr(cal["yellow_rgb"])
    black_ring = cal["accent_black_ring_px_1080"] * k
    perim = cal["accent_perimeter_px_1080"] * k
    plain = max(2.0, 0.045 * line["size"])
    sf = style.shadow_factor
    out = []
    for i, (word, mode, glued) in enumerate(line["words"]):
        w = captions._esc(word)
        sep = " " if i and not glued else ""
        if ring_layer:
            if mode:
                out.append(f"{sep}{{\\1a&H00&\\3a&H00&\\3c{yellow}\\bord{black_ring + perim:.1f}\\shad0}}{w}")
            else:
                out.append(f"{sep}{{\\1a&HFF&\\3a&HFF&\\bord0\\shad0}}{w}")
        elif mode:
            fill = yellow if mode == 2 else "&H00FFFFFF&"
            out.append(f"{sep}{{\\1a&H00&\\1c{fill}\\3a&H00&\\3c&H00000000&\\bord{black_ring:.1f}\\shad{0.04 * line['size'] * sf:.1f}\\4c&H00000000&}}{w}")
        else:
            out.append(f"{sep}{{\\1a&H00&\\1c&H00FFFFFF&\\3a&H00&\\3c&H00000000&\\bord{plain:.1f}\\shad{0.03 * line['size'] * sf:.1f}\\4c&H00000000&}}{w}")
    return "".join(out)


def build_events(title: str, style: TitleStyle, duration: float, width: int, height: int, fg_h: float) -> tuple[str, str]:
    """(style line, dialogue lines) for the title, centred in the upper band above the picture."""
    words = parse(title, style.uppercase)
    if not words or not style.enabled:
        return "", ""
    cal = calibration()
    lines = layout(words, style, width, height, fg_h)
    horizontal_scale = 89 if style.variant == "classic" else 100
    st = (f"Style: Title,{style.font},{style.size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,{horizontal_scale},100,0,0,1,"
          f"2,0,5,0,0,0,1")
    ev = []
    t0, t1 = captions._ts(0), captions._ts(duration)
    for ln in lines:
        pos = f"{{\\an5\\pos({width // 2},{int(round(ln['y']))})\\fs{ln['size']:.0f}}}"
        if any(m for _, m, _g in ln["words"]):
            ev.append(f"Dialogue: 2,{t0},{t1},Title,,0,0,0,,{pos}{_line_text(ln, style, True, cal)}")
        ev.append(f"Dialogue: 3,{t0},{t1},Title,,0,0,0,,{pos}{_line_text(ln, style, False, cal)}")
    return st, "\n".join(ev)


# ---- deterministic title formatting (no model, no invented words) ---------------------------------

_MAX_TITLE = 72
_EMOJI = re.compile("[\U00010000-\U0010ffff\u2600-\u27bf]")


def pick_phrase(tokens: list[str]) -> tuple[int, int] | None:
    """[start, end) of the ONE meaningful phrase to colour: 2-4 words (a lone proper noun/number is allowed), never
    starting or ending on a function word, preferring proper nouns, numbers and content words, a phrase at the end of the
    title, and leaving a leading attribution/subject plain. Deterministic; never changes any word."""
    from .hooks import STOP
    core = [re.sub(r"^[^\w$]+|[^\w%]+$", "", t) for t in tokens]
    n, best, best_score = len(tokens), None, -1e9
    def stop(i):
        return core[i].lower() in STOP or not core[i]
    def proper(i):
        return bool(core[i]) and (any(ch.isdigit() for ch in core[i]) or (i > 0 and core[i][:1].isupper()))
    for a in range(n):
        for b in range(a + 1, min(n, a + 4) + 1):
            if stop(a) or stop(b - 1) or any(core[i].endswith(('?', '!')) for i in range(a, b - 1)):
                continue
            ln = b - a
            if ln == 1 and not proper(a):
                continue
            sc = 2.0 * sum(proper(i) for i in range(a, b)) + sum(not stop(i) for i in range(a, b)) - 1.0 * max(0, ln - 2)
            sc += 0.8 * (b == n) - (2.0 if a == 0 and ln < n else 0.0) - (0.5 if ln == 1 else 0.0)
            if sc >= best_score:
                best, best_score = (a, b), sc
    return best


def format_title(text: str, auto_emphasis: bool = True, single_phrase: bool = False) -> str:
    """Tidy an owner/clip title: collapse whitespace, drop emoji and stray markup, cut at a word boundary
    (never mid-word, never adding words), and - unless the text already marks emphasis - accent the LAST two words
    (the second-last white with a yellow perimeter, the last yellow), as in the approved concept.
    Wording is never changed, so the title cannot claim anything the owner did not write."""
    t = _EMOJI.sub("", text or "")
    marked = "*" in t
    t = re.sub(r"\s+", " ", t.replace("\u201c", '"').replace("\u201d", '"')).strip(" \"'")
    if not marked:
        t = t.replace("*", "")
    plain = re.sub(r"\*+", "", t)
    if len(plain) > _MAX_TITLE:
        cut = plain[:_MAX_TITLE].rsplit(" ", 1)[0].rstrip(" ,;:-")
        t = cut if not marked else re.sub(r"\*+", "", cut)
        marked = False
    if marked or not auto_emphasis:
        return t
    toks = t.split(" ")
    if single_phrase:
        span = pick_phrase(toks)
        if span is None:
            return t
        a, b = span
        last = toks[b - 1]
        core = last.rstrip("?!.,;:")
        toks[b - 1] = core + "**" + last[len(core):]
        toks[a] = "**" + toks[a]
        return " ".join(toks)
    if len(toks) < 3:
        return t
    last = toks[-1]
    core = last.rstrip("?!.,;:")
    tail = last[len(core):]
    if not core:
        return t
    toks[-1] = f"**{core}**{tail}"
    toks[-2] = f"*{toks[-2]}*"
    return " ".join(toks)
