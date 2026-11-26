"""Campaign-driven production: moment selection, render profiles, overlays,
metadata and measurable compliance checks.

Inputs are a ``brief_engine.analyse`` result (confirmed by the owner) and a
transcript. All logic is deterministic; FFmpeg/FFprobe do the media work.
Nothing here publishes or submits anything.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

_STOP = set("""a an and are as at be been but by can clip clips do does for from get got has have he her his how i if in
into is it its just like make me more most must my no not of on only or our out over post posts should so some such
than that the their them then there these they this those to too up us very was we were what when where which while
who why will with you your about focus moments moment where talks talk talking speaks discuss discusses mention
mentions include including topics topic content video videos stream streams episode please ensure campaign""".split())
_SYNONYMS = {
    "politics": {"politics", "political", "election", "president", "trump", "biden", "democrat", "republican",
                 "vote", "voting", "congress", "parliament", "government", "senator"},
    "travel": {"travel", "trip", "flight", "flights", "hotel", "hotels", "airport", "vacation", "holiday",
               "booking", "book", "tokyo", "abroad", "journey", "tour"},
    "gambling": {"gambling", "casino", "bet", "betting", "slots", "stake", "poker", "roulette"},
    "drugs": {"drugs", "weed", "cocaine", "alcohol", "drunk", "vape"},
}
_HOOK_WORDS = re.compile(r"\b(?:you|never|nobody|secret|crazy|insane|wait|watch|why|how|what|biggest|worst|best|"
                         r"first|last|million|billion|literally|actually|truth)\b|[?!]|\b[0-9]+\b", re.I)
_SENTENCE_END = re.compile(r"[.!?][\"')\]]?\s*$")

ASPECTS = {"9:16": (1080, 1920), "1:1": (1080, 1080), "4:5": (1080, 1350), "16:9": (1920, 1080)}
PLATFORM_LIMITS = {"youtube": {"title": 100, "description": 5000}, "tiktok": {"description": 2200},
                   "instagram": {"description": 2200}, "facebook": {"description": 63206}, "x": {"description": 280}}


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


def topic_keywords(analysis: dict) -> tuple[set[str], set[str]]:
    """Return (wanted, banned) keyword sets from topic clauses, with small synonym expansion."""
    wanted, banned = set(), set()
    for req in analysis.get("requirements", []):
        if req["category"] != "topics":
            continue
        words = {w for w in _tokens(req["quote"]) if len(w) >= 4 and w not in _STOP}
        target = banned if req["modality"] == "prohibited" else wanted
        # Split "Focus on travel. No politics." style mixed sentences conservatively.
        if req["modality"] == "prohibited":
            neg = re.split(r"\b(?:no|not|avoid|never|don't|do not)\b", req["quote"], flags=re.I, maxsplit=1)
            if len(neg) == 2:
                wanted |= {w for w in _tokens(neg[0]) if len(w) >= 4 and w not in _STOP}
                words = {w for w in _tokens(neg[1]) if len(w) >= 4 and w not in _STOP}
        target |= words
    for group in (wanted, banned):
        for key, syn in _SYNONYMS.items():
            if group & syn:
                group |= syn
    return wanted - banned, banned


def hook_seconds(analysis: dict) -> float:
    for req in analysis.get("requirements", []):
        if req["category"] == "hook" and (m := re.search(r"first ([0-9]+) seconds?", req["quote"], re.I)):
            return float(m.group(1))
    return 3.0


@dataclass
class Moment:
    start: float
    end: float
    score: float
    text: str
    reasons: list = field(default_factory=list)

    @property
    def duration(self):
        return self.end - self.start


def select_moments(segments: list[dict], analysis: dict, rules: dict, *, limit: int = 5,
                   source_offset_s: float = 0.0) -> list[Moment]:
    """Rank non-overlapping transcript windows that satisfy the campaign constraints.

    ``rules`` is the owner-confirmed selection rule set (min/max duration in ms and
    optional source windows in ms, relative to the source).
    """
    lo, hi = rules["minimum_duration_ms"] / 1000, rules["maximum_duration_ms"] / 1000
    windows = [(w["start_ms"] / 1000, (w["end_ms"] or 10**9) / 1000) for w in rules.get("source_windows") or []]
    wanted, banned = topic_keywords(analysis)
    hook_s = hook_seconds(analysis)
    segs = sorted((s for s in segments if s["end"] > s["start"] and (s.get("text") or "").strip()),
                  key=lambda s: s["start"])
    candidates: list[Moment] = []
    for i, first in enumerate(segs):
        text_parts = []
        for j in range(i, len(segs)):
            seg = segs[j]
            if j > i and seg["start"] - segs[j - 1]["end"] > 4.0:
                break  # long silence breaks narrative continuity
            text_parts.append(seg["text"].strip())
            start, end = first["start"], seg["end"]
            dur = end - start
            if dur > hi:
                break
            if dur < lo:
                continue
            abs_start, abs_end = start + source_offset_s, end + source_offset_s
            if windows and not any(ws <= abs_start and abs_end <= we for ws, we in windows):
                continue
            text = " ".join(text_parts)
            words = _tokens(text)
            if banned & set(words):
                continue
            reasons, score = [], 0.0
            hits = wanted & set(words)
            if wanted:
                if not hits:
                    continue
                score += 3.0 * len(hits)
                reasons.append("topics: " + ", ".join(sorted(hits)))
            opening = " ".join(s["text"] for s in segs[i:j + 1] if s["start"] < start + hook_s)
            hook_hits = len(_HOOK_WORDS.findall(opening))
            score += min(hook_hits, 4) * 1.5
            if hook_hits:
                reasons.append(f"hook within {hook_s:g}s ({hook_hits} signals)")
            density = len(words) / dur
            if 1.8 <= density <= 4.5:
                score += 2.0
                reasons.append(f"speech density {density:.1f} w/s")
            if _SENTENCE_END.search(seg["text"]):
                score += 1.5
                reasons.append("ends on a complete sentence")
            # Prefer the middle of the allowed duration band slightly.
            score += 1.0 - abs((dur - (lo + hi) / 2) / max(hi - lo, 1))
            candidates.append(Moment(round(start, 3), round(end, 3), round(score, 3), text, reasons))
    chosen: list[Moment] = []
    for cand in sorted(candidates, key=lambda m: (-m.score, m.start)):
        if all(cand.end <= c.start or cand.start >= c.end for c in chosen):
            chosen.append(cand)
        if len(chosen) >= limit:
            break
    return chosen


@dataclass
class RenderProfile:
    name: str
    width: int
    height: int
    captions: bool
    logo_path: str | None = None
    logo_position: str = "top_right"
    sponsor_text: str | None = None
    audio_policy: str = "original"  # original | approved_track | mute
    approved_track: str | None = None
    max_duration_s: float | None = None
    min_duration_s: float | None = None

    def to_json(self):
        return asdict(self)


def build_profiles(analysis: dict, *, logo_path: str | None = None, sponsor_text: str | None = None,
                   approved_track: str | None = None) -> list[RenderProfile]:
    """Turn a brief's production requirements into concrete render profiles.

    Raises ValueError when the brief requires an asset the operator has not supplied,
    or prohibits what was supplied.
    """
    p = analysis["production_profile"]
    if p.get("logo_overlay_required") and not logo_path:
        raise ValueError("The brief requires a logo overlay. Supply the approved logo file.")
    if p.get("logos_prohibited") and logo_path:
        raise ValueError("The brief prohibits added logos.")
    if p.get("sponsor_graphic_required") and not (sponsor_text or logo_path):
        raise ValueError("The brief requires a sponsor graphic. Supply approved sponsor text or artwork.")
    if p.get("music_policy") == "approved_tracks_only" and not approved_track:
        raise ValueError("The brief requires an approved track. Supply the licensed audio file.")
    if p.get("music_policy") == "original_audio_only" and approved_track:
        raise ValueError("The brief requires original audio only; do not add a track.")
    res = p.get("resolution") or {}
    aspects = [p["aspect_ratio"]] if p.get("aspect_ratio") else ["9:16"]
    profiles = []
    for aspect in aspects:
        w, h = ASPECTS.get(aspect, ASPECTS["9:16"])
        if "width" in res:
            w, h = res["width"], res["height"]
        elif "short_side" in res:
            scale = res["short_side"] / min(w, h)
            w, h = int(round(w * scale / 2) * 2), int(round(h * scale / 2) * 2)
        profiles.append(RenderProfile(
            name=f"campaign_{aspect.replace(':', 'x')}_{w}x{h}", width=w, height=h,
            captions=p.get("captions") != "forbidden", logo_path=logo_path, sponsor_text=sponsor_text,
            audio_policy="approved_track" if approved_track else "original", approved_track=approved_track,
            max_duration_s=p.get("max_duration_s"), min_duration_s=p.get("min_duration_s")))
    return profiles


def _tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise RuntimeError(f"{name} was not found on PATH.")
    return path


def probe(path: Path) -> dict:
    out = subprocess.run([_tool("ffprobe"), "-v", "error", "-print_format", "json", "-show_streams", "-show_format",
                          str(path)], capture_output=True, text=True, check=True, timeout=120)
    data = json.loads(out.stdout)
    video = next((s for s in data["streams"] if s["codec_type"] == "video"), None)
    audio = next((s for s in data["streams"] if s["codec_type"] == "audio"), None)
    num, den = (video or {}).get("avg_frame_rate", "0/1").split("/")
    return {"width": video and video["width"], "height": video and video["height"],
            "video_codec": video and video["codec_name"], "audio_codec": audio and audio["codec_name"],
            "fps": float(num) / float(den) if float(den) else 0.0, "duration": float(data["format"]["duration"]),
            "size": int(data["format"]["size"]), "pix_fmt": video and video.get("pix_fmt")}


_POS = {"top_right": ("W-w-{m}", "{m}"), "top_left": ("{m}", "{m}"),
        "bottom_right": ("W-w-{m}", "H-h-{m}"), "bottom_left": ("{m}", "H-h-{m}")}


def logo_box(profile: RenderProfile) -> tuple[int, int, int, int]:
    """(x, y, w, h) of the logo area, matching the overlay filter."""
    lw = max(64, profile.width // 6)
    m = max(16, profile.width // 40)
    x = profile.width - lw - m if profile.logo_position.endswith("right") else m
    y = m if profile.logo_position.startswith("top") else profile.height - lw - m
    return x, y, lw, lw


def apply_campaign_layers(src: Path, dst: Path, profile: RenderProfile, font_file: Path | None = None) -> dict:
    """Second pass over an ordinary render: scale/pad to profile, brand overlays, audio policy.

    Returns a manifest recording exactly what was applied (used by validation).
    """
    src, dst = Path(src), Path(dst)
    inputs = ["-i", str(src)]
    chains = [f"[0:v]scale={profile.width}:{profile.height}:force_original_aspect_ratio=decrease,"
              f"pad={profile.width}:{profile.height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1[v0]"]
    last = "v0"
    manifest = {"profile": profile.to_json(), "layers": []}
    with tempfile.TemporaryDirectory(prefix=".campaign-layers-") as scratch:
        scratch = Path(scratch)
        if profile.logo_path:
            inputs += ["-i", str(profile.logo_path)]
            x, y, lw, _ = logo_box(profile)
            chains.append(f"[1:v]scale={lw}:{lw}:force_original_aspect_ratio=decrease,"
                          f"pad={lw}:{lw}:(ow-iw)/2:(oh-ih)/2:color=black@0,format=rgba[logo]")
            chains.append(f"[{last}][logo]overlay={x}:{y}:format=auto[v1]")
            last = "v1"
            manifest["layers"].append({"type": "logo", "box": [x, y, lw, lw],
                                       "sha256": hashlib.sha256(Path(profile.logo_path).read_bytes()).hexdigest()})
        if profile.sponsor_text:
            if not font_file:
                raise ValueError("A font file is required for sponsor text.")
            shutil.copyfile(font_file, scratch / "font.ttf")
            (scratch / "sponsor.txt").write_text(profile.sponsor_text, encoding="utf-8")
            bar = max(48, profile.height // 18)
            chains.append(f"[{last}]drawbox=x=0:y=ih-{bar}:w=iw:h={bar}:color=black@0.65:t=fill,"
                          f"drawtext=fontfile=font.ttf:textfile=sponsor.txt:fontcolor=white:"
                          f"fontsize={bar // 2}:x=(w-text_w)/2:y=h-{bar}+({bar}-text_h)/2[v2]")
            last = "v2"
            manifest["layers"].append({"type": "sponsor_text", "text": profile.sponsor_text, "bar_height": bar})
        audio_args = ["-map", "0:a?"]
        if profile.audio_policy == "approved_track":
            idx = len(inputs) // 2
            inputs += ["-i", str(profile.approved_track)]
            audio_args = ["-map", f"{idx}:a", "-shortest"]
            manifest["layers"].append({"type": "approved_track",
                                       "sha256": hashlib.sha256(Path(profile.approved_track).read_bytes()).hexdigest()})
        elif profile.audio_policy == "mute":
            audio_args = []
        manifest["layers"].append({"type": "audio_policy", "value": profile.audio_policy})
        cmd = [_tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", *inputs,
               "-filter_complex", ";".join(chains), "-map", f"[{last}]", *audio_args,
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
               "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(dst)]
        subprocess.run(cmd, check=True, cwd=scratch, capture_output=True, timeout=1800)
    manifest["output_sha256"] = hashlib.sha256(dst.read_bytes()).hexdigest()
    return manifest


def _mean_rgb(path: Path, *, at: float | None, crop: tuple[int, int, int, int] | None, size: tuple[int, int] | None = None):
    vf = []
    if size:
        vf.append(f"scale={size[0]}:{size[1]}:force_original_aspect_ratio=decrease,pad={size[0]}:{size[1]}:(ow-iw)/2:(oh-ih)/2:color=black")
    if crop:
        x, y, w, h = crop
        vf.append(f"crop={w}:{h}:{x}:{y}")
    vf.append("scale=1:1:flags=area,format=rgb24")
    cmd = [_tool("ffmpeg"), "-v", "error"] + (["-ss", f"{at:.3f}"] if at is not None else []) + [
        "-i", str(path), "-frames:v", "1", "-vf", ",".join(vf), "-f", "rawvideo", "-"]
    data = subprocess.run(cmd, capture_output=True, check=True, timeout=120).stdout
    return tuple(data[:3])


def validate(output: Path, profile: RenderProfile, analysis: dict, manifest: dict, metadata: dict | None = None) -> dict:
    """Measure the rendered clip against the brief's compliance plan."""
    info = probe(output)
    results = []

    def record(check, passed, expected, actual, how="measured"):
        results.append({"check": check, "passed": bool(passed), "expected": expected, "actual": actual, "method": how})

    record("container_codecs", info["video_codec"] == "h264" and info["audio_codec"] in ("aac", None),
           "h264/aac", f"{info['video_codec']}/{info['audio_codec']}")
    record("dimensions", (info["width"], info["height"]) == (profile.width, profile.height),
           [profile.width, profile.height], [info["width"], info["height"]])
    record("frame_rate", 23.0 <= info["fps"] <= 61.0, "23-61 fps", round(info["fps"], 3))
    for item in analysis.get("compliance_plan", []):
        check = item["check"]
        if check == "duration":
            lo, hi = item.get("min_s"), item.get("max_s")
            ok = (lo is None or info["duration"] >= lo - 0.05) and (hi is None or info["duration"] <= hi + 0.05)
            record("duration", ok, [lo, hi], round(info["duration"], 3))
        elif check == "aspect_ratio":
            a, b = (int(x) for x in item["expected"].split(":"))
            record("aspect_ratio", abs(info["width"] / info["height"] - a / b) < 0.01, item["expected"],
                   f"{info['width']}:{info['height']}")
        elif check == "resolution":
            exp = item["expected"]
            ok = ((info["width"], info["height"]) == (exp["width"], exp["height"]) if "width" in exp
                  else min(info["width"], info["height"]) >= exp["short_side"])
            record("resolution", ok, exp, [info["width"], info["height"]])
        elif check == "captions":
            want = item["expected"] == "required"
            record("captions", profile.captions == want, item["expected"], profile.captions, "render manifest")
        elif check == "no_added_music":
            applied = [l for l in manifest["layers"] if l["type"] == "approved_track"]
            record("no_added_music", not applied and info["audio_codec"] is not None,
                   "original audio only", "added track" if applied else "original", "render manifest + probe")
        elif check == "brand_overlay_present":
            logo = next((l for l in manifest["layers"] if l["type"] == "logo"), None)
            if logo:
                x, y, w, h = logo["box"]
                rendered = _mean_rgb(output, at=min(1.0, info["duration"] / 2), crop=(x, y, w, h))
                expected = _mean_rgb(Path(profile.logo_path), at=None, crop=None, size=(w, h))
                diff = max(abs(p - q) for p, q in zip(rendered, expected))
                record("brand_overlay_present", diff <= 24, list(expected), list(rendered), "pixel sample")
            else:
                record("brand_overlay_present", any(l["type"] == "sponsor_text" for l in manifest["layers"]),
                       "logo or sponsor graphic", [l["type"] for l in manifest["layers"]], "render manifest")
        elif metadata is not None and check in ("hashtag_present", "mention_present", "disclosure_present"):
            text = f"{metadata.get('title', '')}\n{metadata.get('description', '')}"
            expected = item["expected"] if isinstance(item["expected"], list) else [item["expected"]]
            ok = all(re.search(re.escape(e) + r"(?![A-Za-z0-9_])", text, re.I) for e in expected)
            record(check, ok, expected, "present" if ok else "missing", "metadata text")
        elif item.get("mode") == "manual":
            results.append({"check": check, "passed": None, "expected": item.get("quote"), "actual": None,
                            "method": "owner review required"})
    if metadata is not None:
        for platform in metadata.get("platforms", []):
            limits = PLATFORM_LIMITS.get(platform, {})
            for key, cap in limits.items():
                value = metadata.get(key, "")
                record(f"{platform}_{key}_length", len(value) <= cap, f"<= {cap}", len(value), "metadata text")
    auto = [r for r in results if r["passed"] is not None]
    return {"output": str(output), "probe": info, "results": results,
            "automatic_passed": all(r["passed"] for r in auto),
            "owner_review_items": [r for r in results if r["passed"] is None]}


def build_metadata(analysis: dict, title: str, summary: str = "", platforms: list[str] | None = None) -> dict:
    """Compose title/description that carry every required tag, mention and disclosure."""
    req = analysis.get("metadata_requirements", {})
    platforms = platforms or analysis.get("platforms", {}).get("allowed") or []
    disclosure = req.get("disclosure_tags") or (["#ad"] if req.get("paid_partnership_label") else [])
    tags = []
    for t in [*disclosure, *req.get("hashtags", [])]:
        if t.lower() not in {x.lower() for x in tags}:
            tags.append(t)
    parts = [p for p in (" ".join(disclosure), summary.strip(), " ".join(req.get("mentions", [])),
                         " ".join(t for t in tags if t not in disclosure)) if p]
    title = title.strip()
    if "youtube" in platforms and len(title) > 100:
        title = title[:99].rstrip() + "…"
    return {"title": title, "description": "\n".join(parts), "platforms": platforms,
            "disclosure_first": bool(disclosure), "paid_partnership_label": bool(req.get("paid_partnership_label"))}
