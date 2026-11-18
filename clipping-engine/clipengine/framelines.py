"""Two thin horizontal accent lines on the vertical composition: one along the top edge and one along the bottom edge of the
central picture, exactly as wide as the picture. No side borders, no border around the 9:16 canvas.

Owner-approved look (17 Nov 2026): colour #2856D8, about 2 px at 1080 px width (half the first proposal). The lines sit just
just OUTSIDE the picture (in the blurred band; moved at most one row inward to land on an even row), so no picture is cropped and the title/caption layers are unaffected.

Per brand, like the title style: a brand uses the lines only when switched on (`set_brand_enabled`), and a brand's own colour is
used only if one has been approved for it (`set_brand_colour`); otherwise the approved #2856D8 applies. OFF for every brand
until enabled; America First Monitor is the brand the colour was approved for and is enabled by default at seeding time.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

APPROVED_COLOUR = "2856D8"
BASE_THICKNESS_1080 = 2.0
DEFAULT_BRANDS = ("america_first_monitor",)


@dataclass
class LineStyle:
    enabled: bool = False
    colour: str = APPROVED_COLOUR
    thickness_1080: float = BASE_THICKNESS_1080

    def thickness(self, canvas_w: int) -> int:
        return max(1, int(round(self.thickness_1080 * canvas_w / 1080)))


def _clean(colour: str) -> str:
    c = (colour or "").lstrip("#").upper()
    return c if re.fullmatch(r"[0-9A-F]{6}", c) else APPROVED_COLOUR


def _cfg(db) -> dict:
    try:
        return json.loads(db.get_kv("frame_lines") or "{}") if db is not None else {}
    except ValueError:
        return {}


def _save(db, cfg: dict) -> None:
    db.set_kv("frame_lines", json.dumps(cfg))


def set_brand_enabled(db, brand_key: str, enabled: bool) -> None:
    cfg = _cfg(db)
    brands = set(cfg.get("brands", list(DEFAULT_BRANDS)))
    (brands.add if enabled else brands.discard)(brand_key)
    cfg["brands"] = sorted(brands)
    _save(db, cfg)


def set_brand_colour(db, brand_key: str, colour: str) -> None:
    """Record an owner-APPROVED colour for one brand. Never call this without the owner's approval."""
    cfg = _cfg(db)
    cfg.setdefault("colours", {})[brand_key] = _clean(colour)
    _save(db, cfg)


def style_for(db, brand_key: str | None) -> LineStyle:
    cfg = _cfg(db)
    brands = set(cfg.get("brands", list(DEFAULT_BRANDS)))
    on = bool(brand_key and brand_key in brands)
    colour = cfg.get("colours", {}).get(brand_key or "", APPROVED_COLOUR)
    return LineStyle(enabled=on, colour=_clean(colour), thickness_1080=float(cfg.get("thickness_1080", BASE_THICKNESS_1080)))


def drawboxes(style: LineStyle | None, canvas_w: int, canvas_h: int, src_w: int, src_h: int) -> str:
    """ffmpeg filter fragment (starting with a comma) or '' when off / when the picture fills the frame."""
    if style is None or not style.enabled or not src_w or not src_h:
        return ""
    scale = min(canvas_w / src_w, canvas_h / src_h)
    fg_w, fg_h = min(canvas_w, 2 * round(src_w * scale / 2)), 2 * round(src_h * scale / 2)   # ffmpeg scales to even sizes
    if canvas_h - fg_h < 8:               # (near) full-frame picture: there is no band to hold the lines
        return ""
    t = style.thickness(canvas_w)
    x = int((canvas_w - fg_w) / 2)
    top = int(round((canvas_h - fg_h) / 2))
    bottom = top + fg_h
    # Snap to even rows so the 4:2:0 chroma is exact (otherwise the colour bleeds into a neighbouring row). Where the natural
    # position is odd the line moves one row INTO the picture edge, never leaving a gap.
    y_top = top - t + ((top - t) % 2)
    y_bottom = bottom - (bottom % 2)
    col = f"0x{style.colour}"
    return (f",drawbox=x={x}:y={y_top}:w={fg_w}:h={t}:color={col}:t=fill"
            f",drawbox=x={x}:y={y_bottom}:w={fg_w}:h={t}:color={col}:t=fill")
