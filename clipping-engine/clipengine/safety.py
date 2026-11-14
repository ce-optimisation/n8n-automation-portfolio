"""Editorial safety checks: flags passages that need human editorial review.

This is a conservative pattern screen, not a classifier of truth or intent. A
flag never deletes anything; it marks the clip so a human reviews it with
context, and it blocks publishing until the reviewer records a decision.
Owners can add terms (one per line, regex allowed) in data/safety_terms.txt.
"""
from __future__ import annotations

import re
from pathlib import Path

CATEGORIES: dict[str, list[str]] = {
    "violence_advocacy": [
        r"\b(kill|exterminate|eliminate|wipe out|slaughter|execute|hang|shoot|gas)\s+(them|all of them|every one of them|those people)\b",
        r"\b(should|must|deserve to|need to) be (killed|shot|hanged|executed|exterminated|wiped out|eradicated)\b",
        r"\btake up arms\b", r"\bviolent uprising\b",
    ],
    "dehumanisation": [
        r"\bsub-?humans?\b", r"\bvermin\b", r"\bcockroaches\b", r"\bparasites\b",
        r"\b(they|these people|those people) are (animals|rats|insects|a disease|a plague)\b",
        r"\binfest(ing|ation)\b",
    ],
    "extremist_terminology": [
        r"\brace war\b", r"\bethnic cleansing\b", r"\bfinal solution\b", r"\bgreat replacement\b",
        r"\bwhite genocide\b", r"\bday of the rope\b", r"\bsieg heil\b", r"\b1488\b", r"\bfourteen words\b",
        r"\bholy war against\b", r"\bremigration\b",
    ],
}

LOW_CONFIDENCE_PROB = 0.5


def _custom_terms(data_dir: Path | None) -> list[str]:
    if not data_dir:
        return []
    f = Path(data_dir) / "safety_terms.txt"
    if not f.is_file():
        return []
    return [ln.strip() for ln in f.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.startswith("#")]


def screen(text: str, data_dir: Path | None = None) -> list[dict]:
    """Return a list of {category, match} flags found in the text."""
    flags = []
    lowered = text.lower()
    cats = dict(CATEGORIES)
    custom = _custom_terms(data_dir)
    if custom:
        cats = {**cats, "owner_watchlist": custom}
    for cat, patterns in cats.items():
        for pat in patterns:
            try:
                m = re.search(pat, lowered)
            except re.error:
                m = re.search(re.escape(pat.lower()), lowered)
            if m:
                flags.append({"category": cat, "match": m.group(0)})
    return flags


def low_confidence_words(words: list[dict]) -> list[dict]:
    return [{"word": w["word"].strip(), "start": w["start"], "prob": w.get("prob", 1.0)}
            for w in words if w.get("prob", 1.0) < LOW_CONFIDENCE_PROB]
