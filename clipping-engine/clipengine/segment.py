"""Candidate excerpt identification.

Every candidate is built from real transcript sentences, so its start/end
times always come from Whisper's word timestamps. An optional local language
model (Ollama) may *re-rank* candidates by index only; it can never introduce
new timestamps or text. If the model is unavailable or returns anything
invalid, the deterministic ranking is used.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

log = logging.getLogger("clipengine.segment")

STOPWORDS = set("""a about above after again against all am an and any are as at be because been before being
below between both but by can could did do does doing down during each few for from further had has have having he
her here hers herself him himself his how i if in into is it its itself just me more most my myself no nor not now of
off on once only or other our ours ourselves out over own same she should so some such than that the their theirs
them themselves then there these they this those through to too under until up very was we were what when where which
while who whom why will with would you your yours yourself yourselves yeah okay ok well really like know think going
get got gonna um uh mean thing things lot right""".split())

FILLERS = {"um", "uh", "erm", "hmm", "ah", "er"}
DANGLING_STARTS = {"and", "but", "so", "because", "or", "which", "that", "then", "also", "plus"}
PRONOUN_STARTS = {"he", "she", "they", "it", "this", "those", "these", "him", "her", "them"}
CLAIM_MARKERS = [
    r"\bbecause\b", r"\btherefore\b", r"\bthe reason\b", r"\bevidence\b", r"\bdata\b", r"\bpercent\b",
    r"\bper cent\b", r"\bmillion\b", r"\bbillion\b", r"\bpolicy\b", r"\blaw\b", r"\bgovernment\b",
    r"\bwe will\b", r"\bi believe\b", r"\bshould\b", r"\bmust\b", r"\bpropos", r"\bplan\b", r"\bvote\b",
    r"\bbudget\b", r"\bresearch\b", r"\bstudy\b", r"\bfact\b", r"\bclaim", r"\bargu", r"\bcost",
    r"\bfirst\b.*\bsecond\b", r"\d",
]


PROCEDURAL_MARKERS = [
    r"\bthank you\b", r"\bthanks\b", r"\bwelcome\b", r"\bgood (morning|afternoon|evening)\b",
    r"\bcan (everyone|you all) hear\b", r"\bmicrophone\b", r"\bnext (item|slide|question)\b",
    r"\b(short|quick) break\b", r"\bhousekeeping\b", r"\bminutes of the (last|previous)\b",
    r"\bany other business\b", r"\bapologies\b", r"\bmove on\b", r"\bshall we (start|begin)\b",
    r"\bfire exits?\b", r"\bphones? (on silent|switched off)\b", r"\blet'?s get started\b",
]
TOPIC_START_MARKERS = [
    r"^(let me|i want to|i'?d like to) (turn|explain|talk|start|address)", r"^(the|our) (next|main|first|second|third|final) (issue|question|point|item|topic)",
    r"^on the (question|issue|matter|subject) of", r"^(first|second|third|finally)[, ]", r"^so what does",
    r"^the (question|issue|problem) (is|here)", r"\?$",
]


@dataclass
class SelectionProfile:
    """Tunable selection behaviour. 'v1' reproduces the original release for comparison."""
    name: str = "v2"
    procedural_weight: float = 0.6
    topic_bonus: float = 0.05
    min_gap: float = 5.0
    relative_floor: float = 0.6   # drop candidates scoring below this fraction of the best
    absolute_floor: float = 0.25
    fit_weight: float = 0.12      # closeness to a preferred duration (v1/v2 only; v3 = 0)
    pause_weight: float = 0.10
    continuation_weight: float = 0.0   # v3: penalise cutting while the next sentence continues the thought
    unanswered_weight: float = 0.0     # v3: penalise ending on a question whose answer follows
    shift_weight: float = 0.0          # v3: penalise windows that straddle a topic change


PROFILES = {
    "v1": SelectionProfile("v1", procedural_weight=0.0, topic_bonus=0.0, min_gap=1.0, relative_floor=0.0,
                           absolute_floor=-9.0),
    "v2": SelectionProfile(),
    # v3 (default): content-led. No preferred duration; length follows natural boundaries.
    "v3": SelectionProfile("v3", fit_weight=0.0, pause_weight=0.18, continuation_weight=0.45, topic_bonus=0.12,
                           unanswered_weight=0.15, shift_weight=0.25),
}
DEFAULT_PROFILE = "v3"


@dataclass
class Sentence:
    start: float
    end: float
    words: list[dict]
    text: str
    mean_prob: float
    terminal: bool
    question: bool
    ctoks: frozenset = frozenset()
    boundary_after: float = 1.0   # 0..1 strength of the gap after this sentence as a natural break (v3)


@dataclass
class Candidate:
    s0: int
    s1: int
    start: float
    end: float
    text: str
    words: list[dict]
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    method: str = "heuristic"
    summary: str | None = None

    @property
    def duration(self) -> float:
        return self.end - self.start


def _flatten_words(segments: list[dict]) -> list[dict]:
    words = []
    for seg in segments:
        ws = seg.get("words") or []
        if ws:
            words.extend(w for w in ws if w.get("word", "").strip())
        elif seg.get("text"):
            # No word timings: spread the segment's words evenly (marked low-precision).
            toks = seg["text"].split()
            step = (seg["end"] - seg["start"]) / max(1, len(toks))
            for i, t in enumerate(toks):
                words.append({"start": seg["start"] + i * step, "end": seg["start"] + (i + 1) * step,
                              "word": " " + t, "prob": 0.6})
    return words


def build_sentences(segments: list[dict], max_gap: float = 1.6) -> list[Sentence]:
    words = _flatten_words(segments)
    sentences: list[Sentence] = []
    cur: list[dict] = []

    def flush():
        if not cur:
            return
        text = "".join(w["word"] for w in cur).strip()
        last = cur[-1]["word"].strip().rstrip("\"')]”’")
        sentences.append(Sentence(start=cur[0]["start"], end=cur[-1]["end"], words=list(cur), text=text,
                                  mean_prob=sum(w.get("prob", 1.0) for w in cur) / len(cur),
                                  terminal=last.endswith((".", "?", "!")), question=last.endswith("?"),
                                  ctoks=frozenset(_content_tokens(text))))
        cur.clear()

    for i, w in enumerate(words):
        if cur and w["start"] - cur[-1]["end"] > max_gap:
            flush()
        cur.append(w)
        tok = w["word"].strip().rstrip("\"')]”’")
        if tok.endswith((".", "?", "!")) and not re.fullmatch(r"(mr|mrs|ms|dr|st|vs|etc|e\.g|i\.e)\.", tok.lower()):
            flush()
    flush()
    return sentences


def _content_tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z']+", text.lower()) if t not in STOPWORDS and len(t) > 3}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def annotate_boundaries(sents: list[Sentence]) -> None:
    """TextTiling-style boundary strength for every gap between sentences, ranked against the
    recording's own distribution (so it adapts to each speaker and transcript):
    lexical change between the 3 sentences either side (cosine on content words) and the pause
    length, each converted to a percentile and averaged. A gap followed by a dangling
    conjunction ("And…", "But…") or separating a question from its answer is a weak break."""
    from collections import Counter
    import math
    n = len(sents)
    if n < 2:
        if sents:
            sents[0].boundary_after = 1.0
        return
    lex, pause = [], []
    for g in range(n - 1):
        left = Counter(t for s_ in sents[max(0, g - 2):g + 1] for t in s_.ctoks)
        right = Counter(t for s_ in sents[g + 1:g + 4] for t in s_.ctoks)
        dot = sum(v * right.get(k, 0) for k, v in left.items())
        norm = math.sqrt(sum(v * v for v in left.values())) * math.sqrt(sum(v * v for v in right.values()))
        lex.append(1.0 - (dot / norm if norm else 0.0))
        pause.append(max(0.0, sents[g + 1].start - sents[g].end))

    def ranks(xs):
        order = sorted(range(len(xs)), key=lambda k: xs[k])
        r = [0.0] * len(xs)
        for pos, k in enumerate(order):
            r[k] = pos / max(1, len(xs) - 1)
        return r
    rl, rp = ranks(lex), ranks(pause)
    for g in range(n - 1):
        strength = 0.5 * rl[g] + 0.5 * rp[g]
        nxt = sents[g + 1]
        first = nxt.words[0]["word"].strip().lower().strip(",.") if nxt.words else ""
        if first in DANGLING_STARTS:
            strength *= 0.3
        if sents[g].question:
            strength *= 0.5
        sents[g].boundary_after = round(strength, 4)
    sents[-1].boundary_after = 1.0


def _cont(a: list[Sentence], b: list[Sentence]) -> float:
    """Lexical continuity between two sentence groups (0 = unrelated, 1 = same thread)."""
    ta = frozenset().union(*[x.ctoks for x in a]) if a else frozenset()
    tb = frozenset().union(*[x.ctoks for x in b]) if b else frozenset()
    return min(1.0, _jaccard(ta, tb) * 3.0)


def score_window(sents: list[Sentence], i: int, j: int, target: float | None = None,
                 profile: SelectionProfile | None = None) -> tuple[float, list[str]]:
    profile = profile or PROFILES[DEFAULT_PROFILE]
    win = sents[i:j + 1]
    text = " ".join(s.text for s in win)
    toks = re.findall(r"[a-z']+", text.lower())
    n = max(1, len(toks))
    reasons = []
    content_ratio = sum(1 for t in toks if t not in STOPWORDS and len(t) > 3) / n
    markers = sum(1 for p in CLAIM_MARKERS if re.search(p, text.lower()))
    substance = min(1.0, content_ratio * 1.6) * 0.6 + min(1.0, markers / 6) * 0.4
    if markers >= 3:
        reasons.append(f"{markers} substantive markers (claims, figures, reasoning)")
    qa = 0.0
    for k, s in enumerate(win[:-1]):
        if s.question and len(win) - k - 1 >= 2:
            qa = 1.0
            reasons.append("contains a question with its answer")
            break
    clarity = sum(s.mean_prob * len(s.words) for s in win) / max(1, sum(len(s.words) for s in win))
    if clarity < 0.6:
        reasons.append("lower transcription confidence")
    filler = sum(1 for t in toks if t in FILLERS) / n
    first = win[0].words[0]["word"].strip().lower().strip(",.") if win[0].words else ""
    start_pen = 0.35 if first in DANGLING_STARTS else 0.12 if first in PRONOUN_STARTS else 0.0
    if start_pen >= 0.35:
        reasons.append(f"starts mid-thought ('{first}…')")
    pause_before = sents[i].start - sents[i - 1].end if i > 0 else 2.0
    pause_after = sents[j + 1].start - sents[j].end if j + 1 < len(sents) else 2.0
    pauses = min(1.0, pause_before / 0.8) * 0.5 + min(1.0, pause_after / 0.8) * 0.5
    dur = win[-1].end - win[0].start
    fit = max(0.0, 1 - abs(dur - target) / target) if (target and profile.fit_weight) else 0.0
    continuation = unanswered = shift = 0.0
    if profile.continuation_weight:
        end_strength = sents[j].boundary_after if j + 1 < len(sents) else 1.0
        start_strength = sents[i - 1].boundary_after if i > 0 else 1.0
        continuation = (1.0 - end_strength) + 1.0 * (1.0 - start_strength)
        if end_strength < 0.4:
            reasons.append("the speaker's point continues after this cut")
        elif end_strength >= 0.75:
            reasons.append("ends at a natural break")
    if profile.unanswered_weight and win[-1].question and j + 1 < len(sents):
        unanswered = 1.0
        reasons.append("ends on a question before its answer")
    if profile.shift_weight and len(win) >= 2:
        internal = max((x.boundary_after for x in win[:-1]), default=0.0)
        shift = max(0.0, internal - 0.8) / 0.2
        if shift > 0.5:
            reasons.append("spans a change of topic")
    end_clean = 1.0 if win[-1].terminal else 0.0
    procedural = 0.0
    if profile.procedural_weight:
        hits = sum(len(re.findall(p_, text.lower())) for p_ in PROCEDURAL_MARKERS)
        procedural = min(1.0, hits / max(1.0, len(win) * 0.6))
        if procedural >= 0.3:
            reasons.append("mostly procedural/housekeeping talk")
    topic = 0.0
    if profile.topic_bonus:
        first_text = win[0].text.lower().strip()
        if any(re.search(p_, first_text) for p_ in TOPIC_START_MARKERS) or pause_before >= 0.9:
            topic = profile.topic_bonus
            reasons.append("opens at the start of a topic")
    score = (0.36 * substance + 0.10 * qa + 0.22 * clarity + profile.pause_weight * pauses
             + profile.fit_weight * fit + 0.10 * end_clean
             - start_pen - 1.5 * filler - profile.procedural_weight * procedural + topic
             - profile.continuation_weight * continuation - profile.unanswered_weight * unanswered
             - profile.shift_weight * shift)
    if pauses > 0.8:
        reasons.append("natural pauses at both ends")
    return round(score, 4), reasons


def generate_candidates(sents: list[Sentence], min_s: float, max_s: float, target: float | None,
                        profile: SelectionProfile | None = None) -> list[Candidate]:
    cands = []
    for i in range(len(sents)):
        for j in range(i, len(sents)):
            dur = sents[j].end - sents[i].start
            if dur > max_s:
                break
            if dur < min_s or not sents[j].terminal:
                continue
            score, reasons = score_window(sents, i, j, target, profile)
            words = [w for s in sents[i:j + 1] for w in s.words]
            cands.append(Candidate(i, j, sents[i].start, sents[j].end,
                                   " ".join(s.text for s in sents[i:j + 1]), words, score, reasons))
    return cands


def pick_diverse(cands: list[Candidate], n: int, max_similarity: float = 0.45,
                 already: list[Candidate] | None = None, min_gap: float = 1.0) -> list[Candidate]:
    chosen: list[Candidate] = list(already or [])
    toks = {id(c): _content_tokens(c.text) for c in cands + chosen}
    for c in cands:
        if len(chosen) >= n:
            break
        if any(c.start < o.end + min_gap and o.start < c.end + min_gap for o in chosen):
            continue
        if any(_jaccard(toks[id(c)], toks[id(o)]) > max_similarity for o in chosen):
            continue
        chosen.append(c)
    return chosen


def effective_bounds(duration: float, min_s: float, max_s: float, target: float | None) -> tuple[float, float, float]:
    """Relax durations for short recordings so they still yield candidates."""
    target = target or (min_s + max_s) / 2
    if duration and duration < min_s * 2.5:
        min_s = max(8.0, min(min_s, duration * 0.2))
        target = max(min_s * 1.3, min(target, duration * 0.35))
        max_s = max(target * 1.6, min(max_s, duration * 0.6))
    elif duration and max_s > duration * 0.6:
        max_s = max(min_s * 1.5, duration * 0.6)
    return min_s, max_s, target


def select(segments: list[dict], *, n: int, min_s: float, max_s: float, target: float | None = None,
           duration: float = 0.0, reranker=None, profile: str | SelectionProfile = DEFAULT_PROFILE,
           report: dict | None = None) -> list[Candidate]:
    """Choose up to n diverse, self-contained excerpts. If `report` is a dict it is
    filled with an explanation of how many candidates were found and why."""
    prof = PROFILES[profile] if isinstance(profile, str) else profile
    rep = report if report is not None else {}
    sents = build_sentences(segments)
    rep.update(sentences=len(sents), profile=prof.name)
    if not sents:
        rep["note"] = "No sentences in the transcript."
        return []
    min_s, max_s, target = effective_bounds(duration or sents[-1].end, min_s, max_s, target)
    rep["bounds"] = [round(min_s, 1), round(max_s, 1)] + ([round(target, 1)] if prof.fit_weight else [])
    if prof.continuation_weight or prof.shift_weight:
        annotate_boundaries(sents)
    cands = generate_candidates(sents, min_s, max_s, target, prof)
    if not cands:
        # Fallback: allow shorter windows rather than producing nothing.
        cands = generate_candidates(sents, max(5.0, min_s / 2), max_s, target, prof)
        rep["relaxed_min"] = True
    rep["windows_scored"] = len(cands)
    cands.sort(key=lambda c: c.score, reverse=True)
    if cands:
        floor = max(prof.absolute_floor, cands[0].score * prof.relative_floor)
        strong = [c for c in cands if c.score >= floor]
        rep["floor"] = round(floor, 3)
    else:
        strong = []
    chosen: list[Candidate] = []
    if reranker is not None and strong:
        pool = pick_diverse(strong, n * 3, max_similarity=0.6, min_gap=prof.min_gap)
        try:
            ranked = reranker(pool, n)
        except Exception as e:  # noqa: BLE001
            log.warning("LLM re-ranking failed, using heuristic ranking: %s", e)
            rep["llm"] = f"failed: {e}"
            ranked = None
        if ranked:
            for c in ranked:
                c.method = "llm+heuristic"
            chosen = pick_diverse(ranked, n, min_gap=prof.min_gap)
            rep["llm"] = "re-ranked"
    chosen = pick_diverse(strong, n, already=chosen, min_gap=prof.min_gap)
    rep["selected"] = len(chosen)
    if len(chosen) < n:
        rep["note"] = (f"Selected {len(chosen)} of the requested {n}: the remaining passages were too similar, "
                       "overlapping, or scored well below the strongest material (not padded with weak clips).")
    return sorted(chosen, key=lambda c: c.start)


def context_for(sents: list[Sentence], start: float, end: float, seconds: float) -> tuple[str, str]:
    before = [s for s in sents if s.end <= start + 0.01 and s.start >= start - seconds]
    after = [s for s in sents if s.start >= end - 0.01 and s.end <= end + seconds]
    if not before:
        before = [s for s in sents if s.end <= start + 0.01][-2:]
    if not after:
        after = [s for s in sents if s.start >= end - 0.01][:2]
    return " ".join(s.text for s in before), " ".join(s.text for s in after)


# ---- optional local LLM re-ranker (Ollama) ----------------------------------

def ollama_available(url: str, timeout: float = 2.0) -> bool:
    try:
        import httpx
        r = httpx.get(url.rstrip("/") + "/api/tags", timeout=timeout)
        return r.status_code == 200
    except Exception:  # noqa: BLE001
        return False


def make_ollama_reranker(url: str, model: str):
    import httpx

    def rerank(pool: list[Candidate], n: int) -> list[Candidate] | None:
        if not pool:
            return None
        items = "\n\n".join(f"[{k}] ({c.duration:.0f}s) {c.text[:900]}" for k, c in enumerate(pool))
        prompt = (
            "You assist a newsroom editor selecting short excerpts from a recording for factual reporting "
            "and critical analysis. Choose the excerpts that are most self-contained and substantive: clear "
            "claims, arguments, policy statements, or questions with their answers, intelligible without "
            "outside context. Do NOT favour outrage, insults or emotional manipulation. "
            f"Return JSON only: {{\"ranked\": [indices best first, at most {n}], "
            "\"summaries\": {\"index\": \"one neutral sentence describing what the speaker discusses, "
            "without quoting or adding facts\"}}.\n\nExcerpts:\n" + items)
        r = httpx.post(url.rstrip("/") + "/api/generate", timeout=300,
                       json={"model": model, "prompt": prompt, "stream": False, "format": "json",
                             "options": {"temperature": 0}})
        r.raise_for_status()
        data = json.loads(r.json().get("response", "{}"))
        ranked = []
        for k in data.get("ranked", []):
            try:
                k = int(k)
            except (TypeError, ValueError):
                continue
            if 0 <= k < len(pool) and pool[k] not in ranked:
                ranked.append(pool[k])
        summaries = data.get("summaries", {}) or {}
        for k, c in enumerate(pool):
            s = summaries.get(str(k)) or summaries.get(k)
            if isinstance(s, str) and s.strip():
                # Strip quotation marks so a machine summary can never pose as a quote.
                c.summary = re.sub(r"[\"“”]", "", s.strip())[:400]
        return ranked or None

    return rerank
