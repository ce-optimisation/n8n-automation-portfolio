"""Vertical 9:16 rendering with burned-in word-timed captions."""
from __future__ import annotations

import math
from pathlib import Path

from .config import Settings
from . import media


def fmt_ts(t: float, sep: str = ",") -> str:
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    ms = int(round((s - int(s)) * 1000))
    if ms == 1000:
        s, ms = s + 1, 0
    return f"{int(h):02d}:{int(m):02d}:{int(s):02d}{sep}{ms:03d}"


def fmt_ass_ts(t: float) -> str:
    t = max(0.0, t)
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def mmss(t: float) -> str:
    t = int(max(0, t))
    h, r = divmod(t, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def wrap_lines(words: list[str], max_chars: int) -> list[str]:
    lines, cur = [], ""
    for w in words:
        cand = (cur + " " + w).strip()
        if len(cand) > max_chars and cur:
            lines.append(cur)
            cur = w
        else:
            cur = cand
    if cur:
        lines.append(cur)
    return lines


def build_phrases(words: list[dict], clip_start: float, clip_end: float, max_chars: int = 26,
                  max_lines: int = 2, max_dur: float = 3.2, max_gap: float = 0.6) -> list[dict]:
    """Group word timings into caption phrases relative to the clip start."""
    ws = [w for w in words if w["end"] > clip_start and w["start"] < clip_end and w["word"].strip()]
    phrases: list[dict] = []
    cur: list[dict] = []

    def fits(tokens: list[str]) -> bool:
        return len(wrap_lines(tokens, max_chars)) <= max_lines

    def flush():
        if cur:
            toks = [w["word"].strip() for w in cur]
            phrases.append({"start": max(0.0, cur[0]["start"] - clip_start),
                            "end": min(clip_end, cur[-1]["end"]) - clip_start,
                            "lines": wrap_lines(toks, max_chars)})
            cur.clear()

    for w in ws:
        if cur:
            gap = w["start"] - cur[-1]["end"]
            tokens = [x["word"].strip() for x in cur] + [w["word"].strip()]
            if gap > max_gap or w["end"] - cur[0]["start"] > max_dur or not fits(tokens):
                flush()
        cur.append(w)
        if w["word"].strip().endswith((".", "?", "!")):
            flush()
    flush()
    # Smooth timing: close tiny gaps, guarantee a minimum on-screen time.
    for i, p in enumerate(phrases):
        nxt = phrases[i + 1]["start"] if i + 1 < len(phrases) else clip_end - clip_start
        if nxt - p["end"] < 0.35:
            p["end"] = nxt
        if p["end"] - p["start"] < 0.8:
            p["end"] = min(nxt, p["start"] + 0.8)
    return phrases


def _ass_escape(s: str) -> str:
    return s.replace("\\", "⧵").replace("{", "(").replace("}", ")")


def write_srt(phrases: list[dict], path: Path) -> None:
    out = []
    for i, p in enumerate(phrases, 1):
        out.append(f"{i}\n{fmt_ts(p['start'])} --> {fmt_ts(p['end'])}\n" + "\n".join(p["lines"]) + "\n")
    path.write_text("\n".join(out), encoding="utf-8")


def layout(settings: Settings, src_w: int, src_h: int) -> dict:
    W, H = settings.out_width, settings.out_height
    if not src_w or not src_h:
        src_w, src_h = 16, 9
    scale = min(W / src_w, H / src_h)
    fg_h = src_h * scale
    fg_bottom = (H + fg_h) / 2
    if fg_bottom < H - 420:
        return {"align": 8, "margin_v": int(fg_bottom + 70), "fg_h": fg_h}   # captions below the picture
    return {"align": 2, "margin_v": int(H * 0.16), "fg_h": fg_h}              # captions over lower third


def write_ass(phrases: list[dict], path: Path, settings: Settings, src_w: int, src_h: int,
              label: str | None = None) -> None:
    W, H = settings.out_width, settings.out_height
    lay = layout(settings, src_w, src_h)
    fs = settings.caption_font_size
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,{settings.caption_font},{fs},&H00FFFFFF,&H00FFFFFF,&H00101010,&H64000000,1,0,0,0,100,100,0,0,1,4,1,{lay['align']},70,70,{lay['margin_v']},1
Style: Label,{settings.caption_font},{max(26, fs // 2)},&H00E6E6E6,&H00FFFFFF,&H00101010,&H64000000,0,0,0,0,100,100,0,0,1,2,0,8,60,60,110,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    if label and phrases:
        end = phrases[-1]["end"]
        lines.append(f"Dialogue: 1,{fmt_ass_ts(0)},{fmt_ass_ts(end + 5)},Label,,0,0,0,,{_ass_escape(label)}")
    for p in phrases:
        text = "\\N".join(_ass_escape(x) for x in p["lines"])
        lines.append(f"Dialogue: 0,{fmt_ass_ts(p['start'])},{fmt_ass_ts(p['end'])},Caption,,0,0,0,,{text}")
    path.write_text(header + "\n".join(lines) + "\n", encoding="utf-8")


def filter_graph(settings: Settings, ass_name: str = "captions.ass") -> str:
    W, H = settings.out_width, settings.out_height
    bw, bh = W // 4, H // 4
    return (
        f"[0:v]split=2[bgsrc][fgsrc];"
        f"[bgsrc]scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},"
        f"boxblur=luma_radius=12:luma_power=2,eq=brightness=-0.10:saturation=0.85,scale={W}:{H},setsar=1[bg];"
        f"[fgsrc]scale={W}:{H}:force_original_aspect_ratio=decrease:flags=lanczos,setsar=1[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2,format=yuv420p,ass={ass_name}[v];"
        f"[0:a]highpass=f=80,loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000[a]"
    )


def render_clip(settings: Settings, src: Path, out_dir: Path, start: float, end: float,
                words: list[dict], src_w: int, src_h: int, label: str | None,
                on_progress=None) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    phrases = build_phrases(words, start, end)
    srt, ass = out_dir / "captions.srt", out_dir / "captions.ass"
    write_srt(phrases, srt)
    write_ass(phrases, ass, settings, src_w, src_h, label)
    tmp_out = out_dir / "clip.tmp.mp4"
    final = out_dir / "clip.mp4"
    dur = end - start
    media.run_ffmpeg([
        "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(Path(src).resolve()),
        "-filter_complex", filter_graph(settings), "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", settings.x264_preset, "-crf", str(settings.video_crf),
        "-profile:v", "high", "-r", "30", "-c:a", "aac", "-b:a", settings.audio_bitrate,
        "-movflags", "+faststart", "-f", "mp4", tmp_out.name,
    ], duration=dur, on_progress=on_progress, cwd=out_dir)
    tmp_out.replace(final)
    thumb = out_dir / "thumb.jpg"
    media.run_ffmpeg(["-ss", f"{min(dur / 2, max(0.0, dur - 0.5)):.2f}", "-i", final.name, "-frames:v", "1",
                      "-vf", "scale=540:-2", "-q:v", "3", thumb.name], cwd=out_dir)
    info = media.probe(final)
    if not (info["width"] == settings.out_width and info["height"] == settings.out_height):
        raise media.PermanentError(f"Render produced {info['width']}x{info['height']}, expected "
                                   f"{settings.out_width}x{settings.out_height}")
    if math.isclose(info["duration"], 0.0):
        raise media.PermanentError("Render produced an empty video")
    return {"video": final, "thumb": thumb, "srt": srt, "ass": ass, "probe": info, "phrases": len(phrases)}
