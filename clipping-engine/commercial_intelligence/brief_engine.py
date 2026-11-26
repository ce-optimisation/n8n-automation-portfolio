"""Deterministic campaign brief intelligence.

Reads free-form creator-reward briefs (marketplace listings, emails, PDFs converted
to text) as *untrusted data* and extracts evidence-backed requirements. Every
requirement keeps its exact source line, character span and quoted wording.
Nothing in a brief can change rights, review or publication safeguards: text that
looks like instructions to software is flagged and held, never followed.

The legacy labelled format handled by ``interpret_brief.interpret`` remains the
only way to produce ``selection_rules`` directly. Free-form briefs produce
``proposed_selection_rules`` that an owner must confirm in the dashboard.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import unicodedata

from .interpret_brief import interpret as interpret_labelled

ENGINE_VERSION = "brief-engine/2"

# ---------------------------------------------------------------- patterns
_NEG = re.compile(r"\b(?:no|not|don't|do not|never|avoid|prohibited|forbidden|banned|without|must not|"
                  r"cannot|can't|isn't allowed|aren't allowed|not allowed|not permitted|disallowed)\b", re.I)
_REQ = re.compile(r"\b(?:must|required|require[sd]?|mandatory|need to|needs to|have to|has to|only|"
                  r"should|ensure|always|make sure)\b", re.I)
_URL = re.compile(r"https?://[^\s<>\"')\]]+", re.I)
_HASHTAG = re.compile(r"(?<![\w&])#([A-Za-z][A-Za-z0-9_]{1,60})")
_MENTION = re.compile(r"(?<![\w.])@([A-Za-z0-9_](?:[A-Za-z0-9_.]{0,28}[A-Za-z0-9_])?)")
_MONEY = r"(?:\$|£|€|USD\s*|GBP\s*|EUR\s*)\s?([0-9]+(?:[.,][0-9]{1,2})?)"
_CPM = re.compile(_MONEY + r"\s*(?:per|/|for every|for each|every)\s*(?:1,?000|1k|thousand|k)\b(?:\s*views)?"
                  r"|" + _MONEY + r"\s*CPM|CPM\s*(?:of|:|=)?\s*" + _MONEY, re.I)
_MAX_PAY = re.compile(r"(?:max(?:imum)?|cap(?:ped)?(?: at)?|up to|no more than)\s*(?:payout|earn(?:ings)?|pay)?"
                      r"\s*(?:of|is|:|per (?:clip|video|post))?\s*" + _MONEY, re.I)
_MIN_PAY = re.compile(r"(?:min(?:imum)?)\s*(?:payout|earn(?:ings)?|pay(?:ment)?)\s*(?:of|is|:)?\s*" + _MONEY, re.I)
_VIEW_MIN = re.compile(r"(?:at least|minimum(?: of)?|min\.?|over|more than|reach)\s*([0-9][0-9,.]*)\s*(k|m)?\s*views", re.I)
_BUDGET = re.compile(r"(?:budget|pool|total)\s*(?:of|is|:)?\s*" + _MONEY + r"\s*(k|m)?", re.I)
_DUR_RANGE = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*(s|sec|secs|seconds|m|min|mins|minutes)?\s*(?:-|–|to)\s*"
                        r"([0-9]+(?:\.[0-9]+)?)\s*(s|sec|secs|seconds|m|min|mins|minutes)\b", re.I)
_DUR_MIN = re.compile(r"(?:at least|minimum(?: of)?|min\.?|no shorter than|longer than|over)\s*"
                      r"([0-9]+(?:\.[0-9]+)?)\s*(s|sec|secs|seconds|m|min|mins|minutes)\b", re.I)
_DUR_MAX = re.compile(r"(?:at most|maximum(?: of)?|max\.?|no longer than|under|less than|shorter than|up to)\s*"
                      r"([0-9]+(?:\.[0-9]+)?)\s*(s|sec|secs|seconds|m|min|mins|minutes)\b", re.I)
_TIMESPAN = re.compile(r"\b((?:[0-9]{1,2}:)?[0-9]{1,2}:[0-5][0-9])\s*(?:-|–|to|until)\s*((?:[0-9]{1,2}:)?[0-9]{1,2}:[0-5][0-9])\b")
_EPISODE = re.compile(r"\b(?:episode|ep\.?)\s*#?\s*([0-9]{1,4})\b", re.I)
_ASPECT = re.compile(r"\b(9\s*:\s*16|16\s*:\s*9|1\s*:\s*1|4\s*:\s*5)\b|\b(vertical|portrait|horizontal|landscape|square)\b", re.I)
_RES = re.compile(r"\b([0-9]{3,4})\s*[x×]\s*([0-9]{3,4})\b|\b(2160|1440|1080|720|480)p\b|\b(4k)\b", re.I)
_PLATFORMS = {
    "tiktok": re.compile(r"\btik\s?tok\b", re.I),
    "instagram": re.compile(r"\b(?:instagram|ig reels?|reels)\b", re.I),
    "youtube": re.compile(r"\b(?:youtube(?: shorts)?|yt shorts|shorts)\b", re.I),
    "x": re.compile(r"\b(?:twitter|x\.com)\b", re.I),
    "facebook": re.compile(r"\bfacebook\b", re.I),
    "snapchat": re.compile(r"\bsnap(?:chat)?\b", re.I),
}
_MONTHS = {m: i for i, m in enumerate(
    "jan feb mar apr may jun jul aug sep oct nov dec".split(), start=1)}
_DATE_ISO = re.compile(r"\b(20[0-9]{2})-([01][0-9])-([0-3][0-9])(?:[T ]([0-2][0-9]):([0-5][0-9]))?")
_DATE_DMY = re.compile(r"\b([0-3]?[0-9])(?:st|nd|rd|th)?\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?,?\s*(20[0-9]{2})?\b", re.I)
_DATE_MDY = re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+([0-3]?[0-9])(?:st|nd|rd|th)?,?\s*(20[0-9]{2})?\b", re.I)

# Category keyword triggers. Each sentence matching a trigger becomes evidence for
# that category. Values are parsed separately where a deterministic parser exists.
_TRIGGERS: dict[str, re.Pattern] = {
    "eligible_creators": re.compile(r"\b(?:clips? (?:of|from|featuring)|creator|streamer|host|talent|featuring)\b", re.I),
    "source_urls": re.compile(r"https?://|\b(?:source|footage|vod|stream|episode|video|podcast|use (?:only )?(?:this|these|the))\b", re.I),
    "episodes": _EPISODE,
    "time_ranges": _TIMESPAN,
    "duration": re.compile(r"\b(?:seconds?|secs?|minutes?|mins?|length|long|duration)\b", re.I),
    "topics": re.compile(r"\b(?:topic|topics|about|focus|theme|subject|moments? (?:where|when|about)|talk(?:s|ing)? about|mention)\b", re.I),
    "hook": re.compile(r"\b(?:hook|first [0-9]+ seconds?|opening|intro|start (?:with|strong))\b", re.I),
    "narrative_context": re.compile(r"\b(?:context|out of context|misleading|story|narrative|setup|payoff)\b", re.I),
    "captions": re.compile(r"\b(?:captions?|subtitles?|subs|on-?screen text)\b", re.I),
    "titles": re.compile(r"\btitles?\b", re.I),
    "sponsor_graphics": re.compile(r"\b(?:sponsor|overlay|banner|graphic|lower third|end ?card|cta)\b", re.I),
    "logos": re.compile(r"\b(?:logo|watermark|brand mark)\b", re.I),
    "layout": re.compile(r"\b(?:layout|split[- ]screen|facecam|gameplay|crop|framing|centered|full ?screen)\b", re.I),
    "aspect_ratio": _ASPECT,
    "resolution": _RES,
    "approved_music": re.compile(r"\b(?:music|song|track|sound|audio)\b", re.I),
    "audio_restrictions": re.compile(r"\b(?:copyright(?:ed)? (?:music|audio)|original audio|mute|voice ?over|background music|trending sounds?)\b", re.I),
    "ai_restrictions": re.compile(r"\b(?:ai|a\.i\.|artificial intelligence|ai-generated|generated|voice clon\w*|deepfake|synthetic|auto-?generated)\b", re.I),
    "originality": re.compile(r"\b(?:original|re-?upload\w*|duplicate\w*|unedited|raw clips?|transformative|repost\w*|stolen|same clip)\b", re.I),
    "platforms": re.compile("|".join(p.pattern for p in _PLATFORMS.values()), re.I),
    "account_eligibility": re.compile(r"\b(?:accounts?|followers|account age|new accounts?|country|countries|region|uk|united kingdom|us only|usa|tier[- ]?1|age 1[3-8]|18\+|verified)\b", re.I),
    "descriptions": re.compile(r"\b(?:description|caption text|post caption|bio|link in bio)\b", re.I),
    "hashtags": re.compile(r"(?:#[A-Za-z]|\bhashtags?\b)", re.I),
    "mentions": re.compile(r"(?:(?<![\w.])@[A-Za-z0-9_]|\btag\b|\bmention\b)", re.I),
    "disclosure": re.compile(r"(?:#ad\b|#sponsored\b|#partner\b|paid partnership|branded content|disclos\w*|sponsored)", re.I),
    "payout_rules": re.compile(r"(?:cpm|per (?:1,?000|1k|thousand)|payout|paid|payment|rate|earn|reward)", re.I),
    "payout_limits": re.compile(r"\b(?:max(?:imum)?|minimum|min|cap|capped|up to|threshold|budget|pool)\b", re.I),
    "submission_deadline": re.compile(r"\b(?:deadline|due|submit(?:ted|ssions?)? (?:by|before)|ends?|until|closes?|expires?|last day)\b", re.I),
    "exclusivity": re.compile(r"\bexclusiv\w*|\b(?:other campaigns?|competitor|competing brands?)\b", re.I),
    "ownership_transfer": re.compile(r"\b(?:ownership|own(?:s)? all|assign\w*|transfer\w* (?:of )?(?:rights|ownership)|work for hire|intellectual property|\bip\b)\b", re.I),
    "commercial_usage_rights": re.compile(r"\b(?:usage rights?|licen[cs]e|whitelist\w*|spark ads?|paid (?:ads|media|usage)|perpetu\w*|in perpetuity|repurpose|may use|right to use)\b", re.I),
}

# Phrases that address an automated reader rather than a human clipper, or try to
# lift safeguards. They are flagged and *never* acted upon.
_INJECTION = [
    (re.compile(r"\b(?:ignore|disregard|forget|override)\b.{0,40}\b(?:previous|prior|above|all|any|earlier|system)\b.{0,30}\b(?:instructions?|rules?|prompts?|guidelines?|safeguards?)", re.I), "attempts to override instructions"),
    (re.compile(r"\b(?:you are|act as|pretend to be|as an?)\s+(?:an? )?(?:ai|assistant|language model|llm|chatbot|gpt|claude|system)\b", re.I), "addresses an AI system"),
    (re.compile(r"\b(?:system prompt|developer message|jailbreak|prompt injection)\b", re.I), "references model prompts"),
    (re.compile(r"\b(?:auto-?approve|skip (?:the )?(?:review|approval)|without (?:owner |human )?(?:review|approval)|bypass\w*|publish (?:immediately|automatically|now))\b", re.I), "asks to bypass review or publish automatically"),
    (re.compile(r"\b(?:send|share|post|paste|reveal|upload|email)\b.{0,30}\b(?:credentials?|passwords?|api ?keys?|tokens?|secrets?|cookies?|session)\b", re.I), "requests credentials or secrets"),
    (re.compile(r"\b(?:run|execute|eval|curl|wget|powershell|cmd\.exe|bash|rm -rf|del /)\b.{0,20}(?:`|\(|;|\||https?://)", re.I), "contains executable instructions"),
    (re.compile(r"<\s*(?:script|iframe|img|svg|object)\b", re.I), "contains markup"),
]
_IMPERATIVE = re.compile(r"\s*(?:please\s+)?(?:add|include|use|post|put|keep|place|tag|mention|show|feature|"
                         r"start|end|credit|link|display|burn|overlay|submit|upload)\b", re.I)
_ALWAYS_HOLD = {"eligible_creators", "topics", "narrative_context", "ai_restrictions", "originality",
                "account_eligibility", "exclusivity", "ownership_transfer", "commercial_usage_rights"}
_HIDDEN_CHARS = {"Cf"}  # format characters: zero-width, bidi overrides
_BASE64_BLOB = re.compile(r"[A-Za-z0-9+/]{120,}={0,2}")

_QUALITATIVE = {  # Categories that always need a human decision before use.
    "eligible_creators", "topics", "narrative_context", "approved_music", "audio_restrictions",
    "ai_restrictions", "originality", "account_eligibility", "exclusivity", "ownership_transfer",
    "commercial_usage_rights", "layout", "sponsor_graphics", "logos", "hook", "titles", "descriptions",
}


def _seconds(value: str, unit: str | None) -> float:
    n = float(value)
    return n * 60 if unit and unit.lower().startswith("m") else n


def _clock_s(text: str) -> int:
    parts = [int(p) for p in text.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def _money(match: re.Match) -> float | None:
    for group in match.groups():
        if group and re.fullmatch(r"[0-9]+(?:[.,][0-9]{1,2})?", group):
            return float(group.replace(",", "."))
    return None


def _currency(text: str) -> str | None:
    for sym, code in (("$", "USD"), ("£", "GBP"), ("€", "EUR"), ("USD", "USD"), ("GBP", "GBP"), ("EUR", "EUR")):
        if sym in text:
            return code
    return None


def _scaled(number: str, suffix: str | None) -> int:
    value = float(number.replace(",", ""))
    return int(value * {"k": 1_000, "m": 1_000_000}.get((suffix or "").lower(), 1))


def _dates(text: str, reference_year: int | None) -> list[dict]:
    found = []
    for m in _DATE_ISO.finditer(text):
        hh, mm = int(m.group(4) or 23), int(m.group(5) or 59)
        try:
            d = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), hh, mm, tzinfo=timezone.utc)
        except ValueError:
            continue
        found.append({"iso": d.isoformat(), "year_stated": True, "time_stated": m.group(4) is not None})
    for regex, order in ((_DATE_DMY, "dmy"), (_DATE_MDY, "mdy")):
        for m in regex.finditer(text):
            day, mon, year = (m.group(1), m.group(2), m.group(3)) if order == "dmy" else (m.group(2), m.group(1), m.group(3))
            month = _MONTHS.get(mon.lower()[:3])
            if not month:
                continue
            y = int(year) if year else reference_year
            if y is None:
                found.append({"iso": None, "year_stated": False, "raw": m.group(0)})
                continue
            try:
                d = datetime(y, month, int(day), 23, 59, tzinfo=timezone.utc)
            except ValueError:
                continue
            found.append({"iso": d.isoformat(), "year_stated": bool(year), "time_stated": False})
    return found


def _merge_sentences(lines: list[str]):
    """Sentence split that avoids breaking URLs, decimals and abbreviations like 'e.g.'"""
    out = []
    for line_no, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        protected = _URL.sub(lambda m: "\x00" * len(m.group(0)), line)
        protected = re.sub(r"(?<=[0-9])\.(?=[0-9])", "\x00", protected)
        protected = re.sub(r"\b(?:e\.g|i\.e|etc|vs|approx|min|max)\.", lambda m: m.group(0)[:-1] + "\x00", protected, flags=re.I)
        protected = re.sub(r"\ba\.i\.", "a\x00i\x00", protected, flags=re.I)
        for m in re.finditer(r"[^.!?;•]+(?:[.!?;]+|$)", protected):
            if m.group(0).strip():
                out.append((line_no, m.start(), m.end(), line[m.start():m.end()]))
    return out


def _modality(sentence: str) -> str:
    if _NEG.search(sentence):
        return "prohibited"
    if _REQ.search(sentence) or _IMPERATIVE.match(sentence):
        return "required"
    if re.search(r"\b(?:allowed|permitted|may|can|ok(?:ay)?|welcome|encouraged)\b", sentence, re.I):
        return "permitted"
    return "informational"


def _sanitise(text: str) -> tuple[str, list[dict]]:
    """Normalise to NFC and report hidden format characters (zero-width, bidi)."""
    flags = []
    hidden = [(i, f"U+{ord(c):04X}") for i, c in enumerate(text) if unicodedata.category(c) in _HIDDEN_CHARS
              and c not in "﻿"]
    if hidden:
        flags.append({"reason": "hidden formatting characters", "positions": hidden[:20], "count": len(hidden)})
    cleaned = "".join(c for c in text if unicodedata.category(c) not in _HIDDEN_CHARS)
    return unicodedata.normalize("NFC", cleaned), flags


def analyse(raw: bytes, *, source_ref: str, reference_year: int | None = None,
            observed_at: str | None = None) -> dict:
    """Return an evidence-backed interpretation. Never authorises publication."""
    if not isinstance(raw, (bytes, bytearray)):
        raise TypeError("raw must be bytes")
    if len(raw) > 512_000:
        raise ValueError("Brief exceeds 500 KB; split or summarise the source before importing.")
    legacy = interpret_labelled(bytes(raw), source_ref=source_ref)
    document_hash = legacy["source_sha256"]
    text = raw.decode("utf-8-sig", errors="replace")
    clean, injection = _sanitise(text)
    lines = clean.splitlines()
    evidence_base = f"{source_ref}#sha256={document_hash}"

    requirements: list[dict] = []
    for line_no, start, end, sentence in _merge_sentences(lines):
        quote = sentence.strip()
        for reason_re, reason in _INJECTION:
            if reason_re.search(sentence):
                injection.append({"line": line_no, "start": start, "end": end, "quote": quote, "reason": reason})
        if _BASE64_BLOB.search(sentence):
            injection.append({"line": line_no, "quote": quote[:80] + "…", "reason": "encoded blob"})
        modality = _modality(sentence)
        for category, trigger in _TRIGGERS.items():
            if not trigger.search(sentence):
                continue
            req = {"category": category, "modality": modality, "line": line_no, "start": start, "end": end,
                   "quote": quote, "evidence": f"{evidence_base};line={line_no};chars={start}-{end}",
                   "basis": "explicit" if modality in ("required", "prohibited") else "inferred"}
            value = _value(category, sentence, reference_year)
            if value is not None:
                req["value"] = value
            elif category in {"duration", "payout_rules", "aspect_ratio", "resolution", "platforms",
                              "hashtags", "mentions", "time_ranges", "episodes", "source_urls",
                              "submission_deadline"}:
                # Trigger fired but nothing parseable: keep as qualitative evidence.
                req["basis"] = "context"
            requirements.append(req)

    analysis = _derive(requirements, legacy)
    labelled = any(legacy["evidence"].values())
    holds = ([{"field": h.get("field", "labelled_format"), **h} for h in legacy["holds"]]
             if labelled and legacy["selection_rules"] is None else [])
    holds += analysis.pop("holds")
    if injection:
        holds.append({"field": "security", "reason": "Brief contains text addressed to software or hidden content. "
                      "It was not followed. Review the original before use.", "flags": injection})
    for req in requirements:
        if req["category"] in _ALWAYS_HOLD or (req["category"] in _QUALITATIVE
                                                and req["modality"] in ("required", "prohibited")):
            holds.append({"field": req["category"], "line": req["line"], "quote": req["quote"],
                          "evidence": req["evidence"], "reason": "Qualitative requirement needs owner confirmation."})
    return {
        "schema_version": 2, "engine": ENGINE_VERSION, "source_ref": source_ref, "source_sha256": document_hash,
        "observed_at": observed_at, "status": "needs_review",
        "campaign": legacy.get("campaign"), "selection_rules": legacy["selection_rules"],
        "evidence": legacy["evidence"], "requirements": requirements, "holds": holds,
        "injection_flags": injection, "publication_authorized": False, **analysis,
    }


def _value(category: str, sentence: str, reference_year: int | None):
    if category == "duration":
        out = {}
        if m := _DUR_RANGE.search(sentence):
            out["min_s"] = _seconds(m.group(1), m.group(2) or m.group(4))
            out["max_s"] = _seconds(m.group(3), m.group(4))
        if m := _DUR_MIN.search(sentence):
            out.setdefault("min_s", _seconds(m.group(1), m.group(2)))
        if m := _DUR_MAX.search(sentence):
            out.setdefault("max_s", _seconds(m.group(1), m.group(2)))
        return out or None
    if category == "payout_rules":
        out = {}
        if m := _CPM.search(sentence):
            out["cpm"] = _money(m)
            out["currency"] = _currency(m.group(0)) or "USD"
        if m := _VIEW_MIN.search(sentence):
            out["min_views"] = _scaled(m.group(1), m.group(2))
        return out or None
    if category == "payout_limits":
        out = {}
        if m := _MAX_PAY.search(sentence):
            out["max_payout"] = _money(m)
            out["currency"] = _currency(m.group(0)) or "USD"
            if re.search(r"per (?:clip|video|post)", sentence, re.I):
                out["max_scope"] = "per_clip"
            elif re.search(r"per (?:account|creator|clipper|person|user)", sentence, re.I):
                out["max_scope"] = "per_clipper"
        if m := _MIN_PAY.search(sentence):
            out["min_payout"] = _money(m)
        if m := _BUDGET.search(sentence):
            value = _money(m)
            if value is not None:
                out["budget"] = value * {"k": 1_000, "m": 1_000_000}.get((m.group(m.lastindex) or "").lower(), 1)
        if m := _VIEW_MIN.search(sentence):
            out["min_views"] = _scaled(m.group(1), m.group(2))
        return out or None
    if category == "aspect_ratio":
        m = _ASPECT.search(sentence)
        word = (m.group(1) or m.group(2)).replace(" ", "").lower()
        return {"vertical": "9:16", "portrait": "9:16", "horizontal": "16:9", "landscape": "16:9",
                "square": "1:1"}.get(word, word)
    if category == "resolution":
        m = _RES.search(sentence)
        if m.group(1):
            return {"width": int(m.group(1)), "height": int(m.group(2))}
        if m.group(4):
            return {"short_side": 2160}
        return {"short_side": int(m.group(3))}
    if category == "platforms":
        found = sorted(name for name, regex in _PLATFORMS.items() if regex.search(sentence))
        return found or None
    if category == "hashtags":
        tags = ["#" + t for t in _HASHTAG.findall(sentence)]
        return tags or None
    if category == "mentions":
        handles = ["@" + h for h in _MENTION.findall(sentence) if not re.fullmatch(r"[0-9]+", h)]
        return handles or None
    if category == "time_ranges":
        spans = [{"start_s": _clock_s(a), "end_s": _clock_s(b)} for a, b in _TIMESPAN.findall(sentence)]
        return spans or None
    if category == "episodes":
        return sorted({int(n) for n in _EPISODE.findall(sentence)}) or None
    if category == "source_urls":
        return [u.rstrip(".,") for u in _URL.findall(sentence)] or None
    if category == "submission_deadline":
        return _dates(sentence, reference_year) or None
    if category == "disclosure":
        tags = [t for t in ("#ad", "#sponsored", "#partner") if re.search(re.escape(t) + r"\b", sentence, re.I)]
        return {"tags": tags, "paid_partnership_label": bool(re.search(r"paid partnership|branded content", sentence, re.I))}
    return None


def _derive(requirements: list[dict], legacy: dict) -> dict:
    by: dict[str, list[dict]] = {}
    for r in requirements:
        by.setdefault(r["category"], []).append(r)
    holds: list[dict] = []
    conflicts: list[dict] = []

    def explicit(cat):
        return [r for r in by.get(cat, []) if "value" in r]

    # Duration: combine bounds; conflicting statements are held.
    mins = [(r["value"]["min_s"], r) for r in explicit("duration") if "min_s" in r["value"]]
    maxs = [(r["value"]["max_s"], r) for r in explicit("duration") if "max_s" in r["value"]]
    min_s = max((v for v, _ in mins), default=None)
    max_s = min((v for v, _ in maxs), default=None)
    if len({v for v, _ in mins}) > 1 or len({v for v, _ in maxs}) > 1:
        conflicts.append({"field": "duration", "lines": sorted({r["line"] for _, r in mins + maxs}),
                          "reason": "Different duration limits are stated; the strictest is proposed."})
    if min_s is not None and max_s is not None and min_s > max_s:
        holds.append({"field": "duration", "reason": "Minimum duration exceeds maximum. Resolve before selection."})

    cpms = {(r["value"]["cpm"], r["value"]["currency"]) for r in explicit("payout_rules") if r["value"].get("cpm")}
    if len(cpms) > 1:
        conflicts.append({"field": "payout_rules", "values": sorted(cpms),
                          "reason": "Multiple rates found; confirm tiers or platform-specific rates."})
    limits: dict = {}
    for r in explicit("payout_limits") + explicit("payout_rules"):
        for key in ("max_payout", "min_payout", "budget", "min_views", "max_scope"):
            if key in r["value"]:
                limits.setdefault(key, r["value"][key])
    deadlines = [d for r in explicit("submission_deadline") for d in r["value"]]
    if any(not d.get("year_stated") for d in deadlines):
        holds.append({"field": "submission_deadline", "reason": "A date omits its year; confirm before relying on it."})
    concrete = sorted({d["iso"] for d in deadlines if d.get("iso")})
    if len(concrete) > 1:
        conflicts.append({"field": "submission_deadline", "values": concrete,
                          "reason": "Several dates found; the earliest is proposed as the deadline."})
    platforms_req = sorted({p for r in explicit("platforms") if r["modality"] != "prohibited" for p in r["value"]})
    platforms_no = sorted({p for r in explicit("platforms") if r["modality"] == "prohibited" for p in r["value"]})
    aspects = {r["value"] for r in explicit("aspect_ratio") if r["modality"] != "prohibited"}
    if len(aspects) > 1:
        conflicts.append({"field": "aspect_ratio", "values": sorted(aspects), "reason": "More than one aspect ratio named."})
    resolution = next((r["value"] for r in explicit("resolution") if r["modality"] != "prohibited"), None)
    hashtags = sorted({t for r in explicit("hashtags") if r["modality"] != "prohibited" for t in r["value"]}, key=str.lower)
    mentions = sorted({h for r in explicit("mentions") if r["modality"] != "prohibited" for h in r["value"]}, key=str.lower)
    disclosure = [r for r in by.get("disclosure", []) if r["modality"] != "prohibited"]
    disclosure_tags = sorted({t for r in disclosure for t in (r.get("value") or {}).get("tags", [])})
    sources = sorted({u for r in explicit("source_urls") if r["modality"] != "prohibited" for u in r["value"]})
    spans = [s for r in explicit("time_ranges") for s in r["value"]]

    def flag(cat, modality):
        return [r["line"] for r in by.get(cat, []) if r["modality"] == modality]

    captions_mode = ("required" if flag("captions", "required") else
                     "forbidden" if flag("captions", "prohibited") else "optional")
    if flag("captions", "required") and flag("captions", "prohibited"):
        conflicts.append({"field": "captions", "reason": "Captions are both required and prohibited in different clauses."})
    music_mode = ("original_audio_only" if any(re.search(r"original audio|no (?:background )?music|no copyright", r["quote"], re.I)
                                               for r in by.get("audio_restrictions", []) + by.get("approved_music", []))
                  else "approved_tracks_only" if any(r["modality"] == "required" and re.search(r"approved|provided|only|this (?:song|track|sound)", r["quote"], re.I)
                                                     for r in by.get("approved_music", []))
                  else "unspecified")
    ai_mode = ("prohibited" if flag("ai_restrictions", "prohibited") else
               "restricted" if flag("ai_restrictions", "required") else "unspecified")
    aspect = next(iter(aspects)) if len(aspects) == 1 else ("9:16" if not aspects else None)
    profile = {
        "aspect_ratio": aspect, "resolution": resolution, "captions": captions_mode,
        "logo_overlay_required": bool(flag("logos", "required")),
        "sponsor_graphic_required": bool(flag("sponsor_graphics", "required")),
        "logos_prohibited": bool(flag("logos", "prohibited")),
        "music_policy": music_mode, "ai_policy": ai_mode,
        "max_duration_s": max_s, "min_duration_s": min_s,
    }
    metadata = {"hashtags": hashtags, "mentions": mentions, "disclosure_tags": disclosure_tags,
                "paid_partnership_label": any((r.get("value") or {}).get("paid_partnership_label") for r in disclosure),
                "description_rules": [r["quote"] for r in by.get("descriptions", []) if r["modality"] == "required"]}
    proposed = None
    if min_s is not None and max_s is not None and min_s <= max_s and (sources or legacy["selection_rules"]):
        windows = []
        for url in sources:
            if spans:
                windows += [{"source_id": url, "start_ms": s["start_s"] * 1000, "end_ms": s["end_s"] * 1000,
                             "evidence": "derived"} for s in spans if s["end_s"] > s["start_s"]]
            else:
                windows.append({"source_id": url, "start_ms": 0, "end_ms": None, "evidence": "derived"})
        proposed = {"minimum_duration_ms": int(min_s * 1000), "maximum_duration_ms": int(max_s * 1000),
                    "submission_deadline": concrete[0] if concrete else None,
                    "destinations": {}, "source_windows": windows, "platforms": platforms_req,
                    "requires_owner_confirmation": True}
    economics = {"cpm": next(iter(cpms))[0] if len(cpms) == 1 else None,
                 "currency": next(iter(cpms))[1] if len(cpms) == 1 else (limits and "USD") or None, **limits}
    checks = _compliance_plan(profile, metadata, by)
    missing = [cat for cat in ("duration", "payout_rules", "platforms", "submission_deadline", "source_urls",
                               "ai_restrictions", "disclosure", "commercial_usage_rights", "account_eligibility")
               if not by.get(cat)]
    return {"proposed_selection_rules": proposed, "production_profile": profile, "metadata_requirements": metadata,
            "economics": economics, "platforms": {"allowed": platforms_req, "prohibited": platforms_no},
            "conflicts": conflicts, "not_stated": missing, "compliance_plan": checks,
            "holds": holds + [{"field": c["field"], "reason": c["reason"]} for c in conflicts]}


def _compliance_plan(profile: dict, metadata: dict, by: dict) -> list[dict]:
    """Checks a rendered clip must pass. 'auto' checks are machine-verified after rendering."""
    plan = []
    if profile["min_duration_s"] is not None or profile["max_duration_s"] is not None:
        plan.append({"check": "duration", "mode": "auto", "min_s": profile["min_duration_s"], "max_s": profile["max_duration_s"]})
    if profile["aspect_ratio"]:
        plan.append({"check": "aspect_ratio", "mode": "auto", "expected": profile["aspect_ratio"]})
    if profile["resolution"]:
        plan.append({"check": "resolution", "mode": "auto", "expected": profile["resolution"]})
    if profile["captions"] != "optional":
        plan.append({"check": "captions", "mode": "auto", "expected": profile["captions"]})
    if profile["music_policy"] == "original_audio_only":
        plan.append({"check": "no_added_music", "mode": "auto"})
    if profile["logo_overlay_required"] or profile["sponsor_graphic_required"]:
        plan.append({"check": "brand_overlay_present", "mode": "auto"})
    for tag in metadata["hashtags"]:
        plan.append({"check": "hashtag_present", "mode": "auto", "expected": tag})
    for handle in metadata["mentions"]:
        plan.append({"check": "mention_present", "mode": "auto", "expected": handle})
    if metadata["disclosure_tags"] or metadata["paid_partnership_label"]:
        plan.append({"check": "disclosure_present", "mode": "auto", "expected": metadata["disclosure_tags"] or ["#ad"]})
    for cat in ("ai_restrictions", "originality", "exclusivity", "commercial_usage_rights", "ownership_transfer",
                "account_eligibility", "narrative_context", "topics"):
        for r in by.get(cat, []):
            if r["modality"] in ("required", "prohibited") or cat in _ALWAYS_HOLD:
                plan.append({"check": cat, "mode": "manual", "quote": r["quote"], "evidence": r["evidence"]})
    return plan


def main() -> int:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("brief", type=Path)
    cli.add_argument("--source-ref", required=True)
    cli.add_argument("--reference-year", type=int)
    args = cli.parse_args()
    result = analyse(args.brief.read_bytes(), source_ref=args.source_ref, reference_year=args.reference_year)
    print(json.dumps(result, indent=2, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
