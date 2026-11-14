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


@dataclass
class Sentence:
    start: float
    end: float
    words: list[dict]
    text: str
    mean_prob: float
    terminal: bool
    question: bool


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
                                  terminal=last.endswith((".", "?", "!")), question=last.endswith("?")))
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


def score_window(sents: list[Sentence], i: int, j: int, target: float) -> tuple[float, list[str]]:
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
    fit = max(0.0, 1 - abs(dur - target) / target)
    end_clean = 1.0 if win[-1].terminal else 0.0
    score = (0.36 * substance + 0.10 * qa + 0.22 * clarity + 0.10 * pauses + 0.12 * fit + 0.10 * end_clean
             - start_pen - 1.5 * filler)
    if pauses > 0.8:
        reasons.append("natural pauses at both ends")
    return round(score, 4), reasons


def generate_candidates(sents: list[Sentence], min_s: float, max_s: float, target: float) -> list[Candidate]:
    cands = []
    for i in range(len(sents)):
        for j in range(i, len(sents)):
            dur = sents[j].end - sents[i].start
            if dur > max_s:
                break
            if dur < min_s or not sents[j].terminal:
                continue
            score, reasons = score_window(sents, i, j, target)
            words = [w for s in sents[i:j + 1] for w in s.words]
            cands.append(Candidate(i, j, sents[i].start, sents[j].end,
                                   " ".join(s.text for s in sents[i:j + 1]), words, score, reasons))
    return cands


def pick_diverse(cands: list[Candidate], n: int, max_similarity: float = 0.45,
                 already: list[Candidate] | None = None) -> list[Candidate]:
    chosen: list[Candidate] = list(already or [])
    toks = {id(c): _content_tokens(c.text) for c in cands + chosen}
    for c in cands:
        if len(chosen) >= n:
            break
        if any(c.start < o.end + 1.0 and o.start < c.end + 1.0 for o in chosen):
            continue
        if any(_jaccard(toks[id(c)], toks[id(o)]) > max_similarity for o in chosen):
            continue
        chosen.append(c)
    return chosen


def effective_bounds(duration: float, min_s: float, max_s: float, target: float) -> tuple[float, float, float]:
    """Relax durations for short recordings so they still yield candidates."""
    if duration and duration < min_s * 2.5:
        min_s = max(8.0, min(min_s, duration * 0.2))
        target = max(min_s * 1.3, min(target, duration * 0.35))
        max_s = max(target * 1.6, min(max_s, duration * 0.6))
    return min_s, max_s, target


def select(segments: list[dict], *, n: int, min_s: float, max_s: float, target: float,
           duration: float = 0.0, reranker=None) -> list[Candidate]:
    sents = build_sentences(segments)
    if not sents:
        return []
    min_s, max_s, target = effective_bounds(duration or sents[-1].end, min_s, max_s, target)
    cands = generate_candidates(sents, min_s, max_s, target)
    if not cands:
        # Fallback: allow shorter windows rather than producing nothing.
        cands = generate_candidates(sents, max(5.0, min_s / 2), max_s, target)
    cands.sort(key=lambda c: c.score, reverse=True)
    if reranker is not None:
        pool = pick_diverse(cands, n * 3, max_similarity=0.6)
        try:
            ranked = reranker(pool, n)
        except Exception as e:  # noqa: BLE001
            log.warning("LLM re-ranking failed, using heuristic ranking: %s", e)
            ranked = None
        if ranked:
            for c in ranked:
                c.method = "llm+heuristic"
            chosen = pick_diverse(ranked, n)
            chosen = pick_diverse(cands, n, already=chosen)
            return sorted(chosen, key=lambda c: c.start)
    return sorted(pick_diverse(cands, n), key=lambda c: c.start)


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
