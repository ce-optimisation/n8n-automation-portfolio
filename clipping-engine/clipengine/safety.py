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
    # Session 13: context-sensitive. Three clips the owner rejected (2: 9, 10, 12) were all stored as "clear" because
    # nothing above can see targeting of an ethnic, religious or national-origin group. These patterns are deliberately
    # narrow (a group term next to a stereotyping cue, or a dehumanising noun-use) and only ever ask for HUMAN review.
    "group_targeting_review": [
        r"\billegals\b", r"\billegal aliens\b",
        r"\b(?:jew(?:s|ish)?|zionists?|muslims?|arabs?|palestinians?|mexicans?|blacks?|asians?|immigrants?|migrants?|"
        r"refugees?)\b(?:\W+\w+){0,6}?\W+(?:what did you expect|of course|typical|always|all of them|are the problem|"
        r"control|run the|own the|go back|do not belong|don't belong)\b",
        r"\b(?:what did you expect|typical|of course|those people|these people|they all|you people)\b(?:\W+\w+){0,6}?\W+"
        r"(?:jew(?:s|ish)?|zionists?|muslims?|arabs?|palestinians?|mexicans?|immigrants?|migrants?)\b",
        r"\b(?:jews?|zionists?|globalists?)\b(?:\W+\w+){0,4}?\W+(?:control|run|own|rule)\s+(?:the |our |all )?"
        r"(?:media|banks?|government|world|country|economy)\b",
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


_LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s"})


def _normalise(text: str) -> str:
    """Undo cheap evasion before matching: accents, curly quotes, leetspeak inside words ('v3rmin'), dotted-out words
    ('s.u.b.h.u.m.a.n') and stretched letters ('verrrmin'). Pure numbers such as '1488' and prices are left alone."""
    import unicodedata
    t = unicodedata.normalize("NFKD", text)
    t = "".join(ch for ch in t if not unicodedata.combining(ch)).replace("’", "'").replace("‘", "'")

    def leet(m):
        w = m.group(0)
        return w.translate(_LEET) if re.search(r"[A-Za-z]", w) and re.search(r"[0-9@$]", w) else w
    t = re.sub(r"[A-Za-z0-9@$]+", leet, t)
    t = re.sub(r"\b(?:[A-Za-z][.\-_*]){3,}[A-Za-z]\b", lambda m: re.sub(r"[.\-_*]", "", m.group(0)), t)
    return t


def _stretch(t: str, keep: int) -> str:
    """Shorten runs of 3+ repeated letters to `keep` letters ('verrrmin' -> 'verrmin' or 'vermin')."""
    return re.sub(r"([A-Za-z])\1{2,}", r"\1" * keep, t)


def screen(text: str, data_dir: Path | None = None) -> list[dict]:
    """Return a list of {category, match} flags found in the text."""
    flags = []
    base = _normalise(text).lower()
    variants = [base] + [v for v in (_stretch(base, 2), _stretch(base, 1)) if v != base]
    lowered = base
    cats = dict(CATEGORIES)
    custom = _custom_terms(data_dir)
    if custom:
        cats = {**cats, "owner_watchlist": custom}
    for cat, patterns in cats.items():
        for pat in patterns:
            m = None
            for v in variants:
                try:
                    m = re.search(pat, v)
                except re.error:
                    m = re.search(re.escape(pat.lower()), v)
                if m:
                    break
            if m:
                flags.append({"category": cat, "match": m.group(0)})
    return flags


def low_confidence_words(words: list[dict]) -> list[dict]:
    return [{"word": w["word"].strip(), "start": w["start"], "prob": w.get("prob", 1.0)}
            for w in words if w.get("prob", 1.0) < LOW_CONFIDENCE_PROB]
