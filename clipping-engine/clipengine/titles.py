"""Top title treatment for vertical clips (the upper black/blurred band above the central video).

Look: large white Anton capitals; one or two accented words get a vivid yellow perimeter with a dark
shadow for contrast. Different clips and brands use different text with no renderer change: the title is
plain text, and accent words are marked with asterisks, e.g. ``THE *TRUTH* ABOUT THE *DEAL*``.

Sizing: the design reference is `reference_size`; the owner asked for the title to be 20% smaller than
the approved concept, so the effective size is reference_size * scale (scale = 0.80). The mock-up image
itself was not available in the repository when this was built, so `reference_size` is an explicit,
configurable setting rather than a measured value: the visual comparison with the concept is PENDING.
Every title is also auto-fitted (real Anton glyph widths) so it never crosses the safe margins or leaves
the band, whatever its length.

The feature is OFF unless enabled in the meta key ``title_style`` ({"enabled": true}); existing renders
are unchanged until the owner turns it on.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass

from . import captions

YELLOW = "&H0000FFFF&"        # ASS colours are BGR: this is #FFFF00
BLACK = "&H00000000&"
WHITE = "&H00FFFFFF&"


@dataclass
class TitleStyle:
    enabled: bool = False
    font: str = "Anton"
    reference_size: int = 120       # size of the approved concept (assumed until the mock-up is measured)
    scale: float = 0.80             # owner request: reduce the top-title font size by 20%
    accent_outline: float = 9.0     # vivid yellow perimeter around accented words
    outline: float = 4.0
    max_lines: int = 3
    side_margin: int = 70
    uppercase: bool = True
    line_spacing: float = 1.08

    @property
    def size(self) -> int:
        return int(round(self.reference_size * self.scale))


def load_style(db=None) -> TitleStyle:
    raw = db.get_kv("title_style") if db is not None else None
    data = json.loads(raw) if raw else {}
    return TitleStyle(**{k: v for k, v in data.items() if k in TitleStyle.__dataclass_fields__ and k != "font"})


def save_style(db, style: TitleStyle) -> None:
    db.set_kv("title_style", json.dumps(asdict(style)))


def parse(text: str, upper: bool = True) -> list[tuple[str, bool]]:
    """Words with an accent flag; `*word*` (or `*two words*`) marks accented words."""
    words, accent = [], False
    for tok in re.findall(r"\*|[^\s*]+", text or ""):
        if tok == "*":
            accent = not accent
            continue
        words.append((tok.upper() if upper else tok, accent))
    return words


def _width(words: list[tuple[str, bool]], size: float, style: TitleStyle) -> float:
    m = captions.font_metrics(style.font)
    text = " ".join(w for w, _ in words)
    return m.width(text, size) if m else len(text) * captions.AVG_EM.get(style.font, 0.5) * size


def fit(words: list[tuple[str, bool]], style: TitleStyle, width: int, band_h: float) -> tuple[list[list[tuple[str, bool]]], int]:
    """Largest size <= style.size at which the title wraps into <= max_lines lines that fit the width and
    the band height (accent outlines included). Returns (lines, size)."""
    usable = width - 2 * style.side_margin - 2 * style.accent_outline
    size = style.size
    while True:
        lines, cur = [], []
        for w in words:
            if cur and _width(cur + [w], size, style) > usable:
                lines.append(cur)
                cur = []
            cur.append(w)
        if cur:
            lines.append(cur)
        widest = max((_width(l, size, style) for l in lines), default=0)
        height = len(lines) * size * style.line_spacing
        if (widest <= usable and len(lines) <= style.max_lines and height <= band_h * 0.9) or size <= 24:
            return lines, size
        size -= 2


def ass_text(lines: list[list[tuple[str, bool]]], style: TitleStyle) -> str:
    out = []
    for line in lines:
        parts = []
        for word, acc in line:
            word = captions._esc(word)
            if acc:
                parts.append(f"{{\\3c{YELLOW}\\bord{style.accent_outline}\\4c{BLACK}\\shad3}}{word}{{\\rTitle}}")
            else:
                parts.append(word)
        out.append(" ".join(parts))
    return "\\N".join(out)


def build_events(title: str, style: TitleStyle, duration: float, width: int, height: int, fg_h: float) -> tuple[str, str]:
    """(style line, dialogue line) for the title, centred in the upper band above the picture."""
    words = parse(title, style.uppercase)
    if not words or not style.enabled:
        return "", ""
    band_h = max(1.0, (height - fg_h) / 2)
    lines, size = fit(words, style, width, band_h)
    y = int(band_h / 2)
    white, black = WHITE.rstrip("&"), BLACK.rstrip("&")
    st = (f"Style: Title,{style.font},{size},{white},{white},{black},{black},0,0,0,0,100,100,0,0,1,"
          f"{style.outline:.1f},0,5,{style.side_margin},{style.side_margin},0,1")
    dlg = (f"Dialogue: 2,{captions._ts(0)},{captions._ts(duration)},Title,,0,0,0,,"
           f"{{\\an5\\pos({width // 2},{y})}}{ass_text(lines, style)}")
    return st, dlg
