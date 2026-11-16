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
from dataclasses import dataclass, field, replace

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
    # broadcast / livestream housekeeping and hand-overs
    r"\b(i'?m|i am) your host\b", r"\bjoining us\b", r"\b(taking|submit|send in) (your )?questions?\b", r"\bhashtag\b",
    r"\bwho will (discuss|tell|provide|talk|speak|join)\b", r"\bon our panel\b", r"\bfor more information\b",
    r"\bplease visit\b", r"\bsubscribe\b", r"\blike button\b", r"\bfollow (me|us) on\b", r"\bsuper ?chats?\b",
    r"\bbefore we (begin|start|get started|get into)\b", r"\bstay tuned\b", r"\bright back\b",
    r"\breplay of this\b", r"\bthanks for (watching|joining|tuning)\b", r"\bturn (it|this|things) (back )?over\b",
    r"\bover to you\b", r"\b(like|want) to (now )?introduce\b", r"\bquestion is for\b", r"\bthat concludes\b",
    r"\btechnical (difficulties|limitations)\b", r"\bcan you (all )?(hear|see) me\b",
]
TOPIC_START_MARKERS = [
    r"^(let me|i want to|i'?d like to) (turn|explain|talk|start|address)", r"^(the|our) (next|main|first|second|third|final) (issue|question|point|item|topic)",
    r"^on the (question|issue|matter|subject) of", r"^(first|second|third|finally)[, ]", r"^so what does",
    r"^the (question|issue|problem) (is|here)", r"\?$",
]


# ---- discourse cues used by the context-boundary model (profile v4) -----------------------------
# A new speaker turn or acknowledgement: a strong break comes BEFORE such a sentence, and the
# sentence itself is a poor first line for a clip.
TURN_START = re.compile(r"^(well,? )?(thank you|thanks|great,? thanks|all ?right,? thanks|okay,? thanks|hi|hello|"
                        r"good (morning|afternoon|evening)|welcome)\b")
# Handing over to someone else / moving the programme on: a strong break comes AFTER it.
HANDOFF = re.compile(r"\bturn (it|this|things) (back )?over\b|\bover to you\b|\b(like|want) to (now )?introduce\b|"
                     r"\bnext question\b|\bquestion is for\b|\bhand (it |things )?(back )?(over )?to\b|"
                     r"\bback to you\b|\bthat concludes\b|\bplease welcome\b|\bwe'?ll (start|begin) with you\b|"
                     r"\bis there anything you (wanted|want|would like) to add\b|\bwe'?ll be right back\b|"
                     r"\b(we|let'?s|let us) move on\b|\bnext item\b|\bshall we (start|begin)\b")
# The start of an answer or reaction: it depends on what was just asked or said.
REPLY_START = re.compile(r"^(yeah|yes|yep|no|nope|right|absolutely|exactly|sure|correct|definitely|of course|"
                         r"well|i mean|i think you)\b[,.! ]|^(well,? )?(thanks|thank you)\b.*\bquestion\b|"
                         r"^(that's|it's) a (good|great|fair|interesting) (one|question|point)\b")
# Words that tie a sentence to something said earlier.
ADDITIVE = {"also", "another", "additionally", "furthermore", "moreover", "similarly", "likewise", "again",
            "addition", "too", "otherwise", "instead", "meanwhile"}
LIST_CONTINUATION = re.compile(r"^(finally|lastly|secondly|thirdly|second|third|next|then|for example|for instance|"
                               r"in fact|in short|in other words|as a result|of course|the other|another|"
                               r"and just lastly|just lastly|one other|whether)\b")
BACK_REFERENCE = re.compile(r"\bas (i|we|you|he|she|they|[a-z]+) (just )?(said|mentioned|noted|heard|alluded|discussed|"
                            r"saw|described|explained)\b|\bas we'?ve (already |just )?(heard|seen|said)\b|"
                            r"\bearlier\b|\bpreviously\b|\b(that|your|this) (question|point)\b|\bfor the question\b|"
                            r"\bsecond part\b|\bto (that|this) (answer|point)\b")
# A speaker announcing a fresh subject (a good first line).
OPENER = re.compile(r"^((now|so|okay|ok|alright|all right|well),? ?)?(let's|let us|let me|i want to|i'd like to|"
                    r"i would like to|we're going to|we are going to|i'm going to) (talk|turn|step|start|begin|look|"
                    r"move|discuss|address|explain|speak|get into|go through)|^(moving on|turning to|speaking of)\b|"
                    r"^on the (question|issue|subject|topic|matter) of\b|^when it comes to\b|^so,? what\b|"
                    r"^(the|our|my) (first|next|main|key|big|biggest) (thing|point|question|issue|story|topic|item)\b|"
                    r"^first\b")


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
    context_weight: float = 0.0        # v4: discourse-cue boundary model (dependent starts, hand-offs,
                                       #     speaker turns, question/answer cohesion, sentence fragments)
    density_substance: bool = False    # v4: substance markers per unit time, so length alone earns nothing
    density_seconds: float = 15.0      # v4: one further marker is expected per this many seconds
    close_share: float = 0.0           # v4: extend the end while the next sentence shares at least this
                                       #     fraction of its content words with the clip (0 = off)
    ending_weight: float = 0.0         # v5: reward a concluding last line; penalise a cut just before a
                                       #     qualification ("but…", "to be fair…") that changes the meaning
    complete_meaning: bool = False     # v5: after selection, carry the end through a question's answer, a
                                       #     meaning-changing qualification, or (debates) the reply
    reply_required: bool = False       # v5 + interview/debate niches: keep the other side's reply
    salient_continue: bool = False     # v5: keep going while the next paragraph is still about the clip's
                                       #     distinctive subject (terms frequent in the clip, rare elsewhere)


PROFILES = {
    "v1": SelectionProfile("v1", procedural_weight=0.0, topic_bonus=0.0, min_gap=1.0, relative_floor=0.0,
                           absolute_floor=-9.0),
    "v2": SelectionProfile(),
    # v3 (default): content-led. No preferred duration; length follows natural boundaries.
    "v3": SelectionProfile("v3", fit_weight=0.0, pause_weight=0.18, continuation_weight=0.45, topic_bonus=0.12,
                           unanswered_weight=0.15, shift_weight=0.25),
}
# v4 (default): v3 plus a context-boundary model. A clip must begin where a thought begins: never
# mid-sentence, not on an answer whose question is missing, not on "also/another/finally…" that
# leans on earlier material, not across a change of speaker; it must not end on a hand-over or a
# stray fragment. Durations stay content-led (no target, no padding).
PROFILES["v4"] = SelectionProfile("v4", fit_weight=0.0, pause_weight=0.18, continuation_weight=0.45,
                                  topic_bonus=0.12, unanswered_weight=0.15, shift_weight=0.25,
                                  context_weight=1.0, density_substance=True, close_share=0.4)
# v5 (opt-in until it measurably beats v4): v4 plus ending awareness. A clip should end where the
# speaker's meaning is complete — after the answer to a question, after a qualification that changes
# what was just said, after the reply in a debate — and a concluding line ("that's why…", "the
# bottom line is…") is a better ending than a mid-argument pause.
PROFILES["v5"] = replace(PROFILES["v4"], name="v5", ending_weight=0.15, complete_meaning=True,
                         salient_continue=True)
DEFAULT_PROFILE = "v4"

QUALIFIER_START = re.compile(r"^(but|however|although|though|that said|having said that|to be fair|in fairness|"
                             r"on the other hand|except|unless|mind you|then again|the catch is|the problem is|"
                             r"the caveat is|the only thing is|now,? (to be fair|that said))\b")
CONCLUSION = re.compile(r"\b(that'?s why|which is why|the (bottom line|point|takeaway|lesson|key thing) (is|here is)|"
                        r"in short|in other words|so the answer is|long story short|what (that|this) means is|"
                        r"and that'?s (the|what|how|why)|so (basically|ultimately|in the end)|ultimately)\b")


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
    follows_terminal: bool = True  # False = the previous words did not finish a sentence (mid-sentence)
    dependency: float = 0.0        # v4: 0..1 how much this sentence leans on what came before it
    dep_reasons: tuple = ()
    edge: str = ""                 # v4: "turn" (greeting/thanks), "handoff", "fragment" or ""
    opener: bool = False           # v4: announces a fresh subject
    ctx: bool = False              # v4: annotate_context() has described this sentence
    hard_break_after: bool = False  # v4: a new speaker, a hand-over or a long silence follows


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


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower().replace("’", "'")).strip(" ,.;:-–—\"'")


def annotate_context(sents: list[Sentence]) -> None:
    """Profile v4: describe how each sentence relates to its surroundings, and sharpen the
    boundary strengths with discourse cues that hold across formats (talks, interviews, panels,
    monologues): speaker turns and hand-overs, long silences, question→answer adjacency,
    additive/anaphoric openings, list continuations and stray fragments."""
    n = len(sents)
    for k, s_ in enumerate(sents):
        text = _norm(s_.text)
        toks = re.findall(r"[a-z']+", text)
        prev = sents[k - 1] if k else None
        s_.follows_terminal = prev is None or prev.terminal
        s_.ctx = True
        s_.opener = bool(OPENER.search(text))
        edge = ""
        if len(toks) <= 2:
            edge = "fragment"
        elif any(re.search(p_, text) for p_ in PROCEDURAL_MARKERS) or \
                sum(1 for t in toks if t in FILLERS) / len(toks) >= 0.12:
            edge = "aside"            # housekeeping or a filler-heavy aside: fine inside, poor as a first/last line
        if TURN_START.search(text) and len(toks) <= 14:
            edge = "turn"
        if HANDOFF.search(text):
            edge = "handoff"
        s_.edge = edge
        dep, why = 0.0, []
        first = toks[0] if toks else ""
        if not s_.follows_terminal:
            dep, why = 1.0, ["begins mid-sentence"]
        else:
            if first in DANGLING_STARTS and not s_.opener:
                # "So…" is often just a spoken discourse marker; "And/But/Because…" really do lean back
                dep += 0.15 if first == "so" else 0.35
                why.append(f"opens with '{first}…'")
            elif first in PRONOUN_STARTS or first in ("that", "that's", "there's") and len(toks) > 2:
                dep += 0.15
                why.append("opens with a pronoun that points back")
            if LIST_CONTINUATION.search(text):
                dep += 0.3
                why.append("continues a list or example started earlier")
            elif set(toks[:7]) & ADDITIVE:
                dep += 0.25
                why.append("adds to an earlier point ('also/another…')")
            if REPLY_START.search(text) or edge == "turn":
                dep += 0.3
                why.append("opens as a reply or acknowledgement")
            if BACK_REFERENCE.search(text):
                dep += 0.2
                why.append("refers back to something said earlier")
            if s_.question and len(toks) <= 8 and set(toks[:6]) & {"this", "that", "it", "these", "those"}:
                dep += 0.3
                why.append("a follow-up question about the previous point")
            if prev is not None and prev.question and not s_.question:
                dep += 0.3
                why.append("answers a question that would be cut off")
            elif prev is not None and len(re.findall(r"[a-z0-9']+", _norm(prev.text))) <= 2 and not TURN_START.search(_norm(prev.text)):
                dep += 0.25
                why.append("follows a heading or fragment that belongs with it")
        s_.dependency, s_.dep_reasons = round(min(1.0, dep), 3), tuple(why)
    for g in range(n - 1):
        cur, nxt = sents[g], sents[g + 1]
        strength = cur.boundary_after
        pause = max(0.0, nxt.start - cur.end)
        # strengthen: silence, a new speaker, a hand-over, an announced new subject
        if pause >= 6.0:
            strength = 1.0
        elif pause >= 2.5:
            strength = max(strength, 0.85)
        if nxt.edge == "turn":
            strength = max(strength, 0.9)
        if cur.edge == "handoff" and nxt.edge != "handoff":
            strength = max(strength, 0.95)
        if nxt.opener:
            strength = max(strength, 0.75)
        if cur.edge == "turn" and nxt.edge not in ("turn", "handoff"):
            strength = max(strength, 0.8)      # the substance begins right after a greeting/thanks
        cur.hard_break_after = bool(cur.terminal and not cur.question and (
            pause >= 6.0 or nxt.edge == "turn" or (cur.edge == "handoff" and nxt.edge != "handoff")))
        # weaken: the thought visibly carries on
        if not cur.terminal:
            strength = min(strength, 0.05)
        elif cur.question and not (cur.edge == "handoff"):
            strength = min(strength, 0.3)          # keep a question with its answer
        elif nxt.dependency >= 0.3 and nxt.edge != "turn" and pause < 2.5:
            strength = min(strength, max(0.15, 0.6 - nxt.dependency))
        cur.boundary_after = round(strength, 4)


def _cos(a: list[Sentence], b: list[Sentence]) -> float:
    from collections import Counter
    import math
    ca = Counter(t for x in a for t in x.ctoks)
    cb = Counter(t for x in b for t in x.ctoks)
    dot = sum(v * cb.get(k, 0) for k, v in ca.items())
    na = math.sqrt(sum(v * v for v in ca.values()))
    nb = math.sqrt(sum(v * v for v in cb.values()))
    return dot / (na * nb) if na and nb else 0.0


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
    dur = win[-1].end - win[0].start
    if profile.density_substance:
        # markers per unit time: a longer window must keep earning its length
        substance = min(1.0, content_ratio * 1.6) * 0.6 + min(1.0, markers / (2.5 + dur / profile.density_seconds)) * 0.4
    else:
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
    if profile.context_weight and win[0].ctx:
        start_pen = 0.0               # v4 measures this (and more) as Sentence.dependency
    if start_pen >= 0.35:
        reasons.append(f"starts mid-thought ('{first}…')")
    pause_before = sents[i].start - sents[i - 1].end if i > 0 else 2.0
    pause_after = sents[j + 1].start - sents[j].end if j + 1 < len(sents) else 2.0
    pauses = min(1.0, pause_before / 0.8) * 0.5 + min(1.0, pause_after / 0.8) * 0.5
    fit = max(0.0, 1 - abs(dur - target) / target) if (target and profile.fit_weight) else 0.0
    continuation = unanswered = shift = 0.0
    if profile.continuation_weight:
        end_strength = sents[j].boundary_after if j + 1 < len(sents) else 1.0
        if j + 1 < len(sents):
            # Whole-clip cohesion with what follows: if the next sentences keep using the clip's
            # vocabulary, the discussion is still going (robust when pauses are unreliable).
            follow = _cos(win, sents[j + 1:j + 4])
            end_strength = min(end_strength, 1.0 - min(1.0, follow * 1.6))
        start_strength = sents[i - 1].boundary_after if i > 0 else 1.0
        if i > 0:
            precede = _cos(sents[max(0, i - 3):i], win)
            start_strength = min(start_strength, 1.0 - min(1.0, precede * 1.6))
        continuation = (1.0 - end_strength) + 1.0 * (1.0 - start_strength)
        if end_strength < 0.4:
            reasons.append("the speaker's point continues after this cut")
        elif end_strength >= 0.75:
            reasons.append("ends at a natural break")
    if profile.unanswered_weight and win[-1].question and j + 1 < len(sents):
        unanswered = 1.0
        reasons.append("ends on a question before its answer")
    if profile.shift_weight and len(win) >= 2 and (win[0].ctx or not profile.context_weight):
        internal = max((x.boundary_after for x in win[:-1]), default=0.0)
        shift = max(0.0, internal - 0.8) / 0.2
        if shift > 0.5:
            reasons.append("spans a change of topic")
    context = 0.0
    if profile.context_weight:
        head, tail = win[0], win[-1]
        if head.dependency:
            context += head.dependency
            reasons.append("start leans on earlier material: " + "; ".join(head.dep_reasons))
        if head.edge in ("turn", "handoff") or (head.edge == "fragment" and not re.search(r"\d", head.text)):
            context += 0.3
            reasons.append("opens on a greeting/hand-over/fragment")
        if head.edge == "aside" or (tail.edge == "aside" and len(win) > 1):
            context += 0.25
            reasons.append("opens or closes on housekeeping talk")
        if tail.edge in ("handoff", "fragment") or (tail.edge == "turn" and len(win) > 1):
            context += 0.35
            reasons.append("ends on a hand-over or fragment rather than the speaker's point")
        if len(win) > 1 and len(tail.words) <= 12 and j + 1 < len(sents) and \
                (tail.opener or LIST_CONTINUATION.search(_norm(tail.text))):
            context += 0.3
            reasons.append("ends by announcing the next point")
        if j + 1 < len(sents) and sents[j + 1].dependency >= 0.3 and sents[j + 1].edge != "turn" \
                and sents[j + 1].start - tail.end < 2.5:
            context += 0.5 * sents[j + 1].dependency
            reasons.append("the next sentence continues this point")
        # a change of speaker or an announced new subject inside the window (not a question→answer)
        if len(win) >= 2 and win[0].ctx and any(x.hard_break_after for x in win[:-1]):
            context += 0.4
            reasons.append("runs across a change of speaker or a long silence")
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
        if profile.context_weight:
            lead = sents[i - 1].boundary_after if i > 0 else 1.0
            if win[0].dependency < 0.15 and (win[0].opener or lead >= 0.7
                                             or any(re.search(p_, first_text) for p_ in TOPIC_START_MARKERS[:-1])):
                topic = profile.topic_bonus
                reasons.append("opens at the start of a topic")
        elif any(re.search(p_, first_text) for p_ in TOPIC_START_MARKERS) or pause_before >= 0.9:
            topic = profile.topic_bonus
            reasons.append("opens at the start of a topic")
    ending = 0.0
    if profile.ending_weight:
        tail_text = _norm(win[-1].text)
        nxt = sents[j + 1] if j + 1 < len(sents) else None
        if CONCLUSION.search(tail_text) or (len(win) > 1 and CONCLUSION.search(_norm(win[-2].text))):
            ending += 1.0
            reasons.append("ends on a concluding line")
        if nxt is not None and QUALIFIER_START.search(_norm(nxt.text)) and nxt.start - win[-1].end < 2.5 \
                and not win[-1].hard_break_after:
            ending -= 1.0
            reasons.append("a qualification that changes the meaning follows this cut")
        last = win[-1].words[-1]["word"].strip().lower().strip(",.?!") if win[-1].words else ""
        if last in DANGLING_STARTS:
            ending -= 0.5
            reasons.append("ends on a dangling conjunction")
    score = (0.36 * substance + 0.10 * qa + 0.22 * clarity + profile.pause_weight * pauses
             + profile.fit_weight * fit + 0.10 * end_clean + profile.ending_weight * ending
             - start_pen - 1.5 * filler - profile.procedural_weight * procedural + topic
             - profile.continuation_weight * continuation - profile.unanswered_weight * unanswered
             - profile.shift_weight * shift - profile.context_weight * context)
    if pauses > 0.8:
        reasons.append("natural pauses at both ends")
    return round(score, 4), reasons


def generate_candidates(sents: list[Sentence], min_s: float, max_s: float, target: float | None,
                        profile: SelectionProfile | None = None) -> list[Candidate]:
    cands = []
    strict = bool(profile and profile.context_weight)
    for i in range(len(sents)):
        if strict and not sents[i].follows_terminal:
            continue                      # never begin a clip in the middle of a sentence
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
    if prof.context_weight:
        annotate_context(sents)
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
    if prof.context_weight and chosen:
        rep["context_adjusted"] = refine_context(sents, chosen, prof, max_s, target)
    if prof.complete_meaning and chosen:
        rep["meaning_completed"] = complete_meaning(sents, chosen, prof, max_s, target)
    rep["selected"] = len(chosen)
    if len(chosen) < n:
        rep["note"] = (f"Selected {len(chosen)} of the requested {n}: the remaining passages were too similar, "
                       "overlapping, or scored well below the strongest material (not padded with weak clips).")
    return sorted(chosen, key=lambda c: c.start)


def refine_context(sents: list[Sentence], chosen: list[Candidate], prof: SelectionProfile, max_s: float,
                   target: float | None, look_seconds: float = 60.0, tolerance: float = 0.08) -> int:
    """Profile v4: for each chosen clip, look back (and ahead) over the neighbouring one or two
    paragraphs. If the clip starts part-way into a discussion and a clean opening lies just
    before it — a sentence that follows a clearly stronger break, does not lean on earlier
    material, and is not separated from the clip by a change of topic — move the start back to
    it. Likewise extend a weak ending to the next clear break. A change is kept only if the
    enlarged window still fits the limits, does not collide with another clip, and scores
    about as well as before; clips are never padded just to be longer. Returns the number of
    clips adjusted."""
    changed = 0
    for c in chosen:
        others = [o for o in chosen if o is not c]
        i, j = c.s0, c.s1

        def free(a: int, b: int) -> bool:
            return not any(sents[a].start < o.end + prof.min_gap and o.start < sents[b].end + prof.min_gap
                           for o in others)
        lead = sents[i - 1].boundary_after if i > 0 else 1.0
        new_i = None
        if i > 0 and (lead < 0.7 or sents[i].dependency >= 0.15):
            k = i - 1
            while k >= 0 and sents[i].start - sents[k].start <= look_seconds and sents[j].end - sents[k].start <= max_s:
                if sents[k].boundary_after >= 0.85 or sents[k].edge in ("handoff", "turn"):
                    break                                   # that sentence belongs to an earlier discussion
                lead_k = sents[k - 1].boundary_after if k > 0 else 1.0
                if (sents[k].follows_terminal and sents[k].dependency < 0.15 and not sents[k].edge
                        and lead_k >= max(0.7, lead + 0.2)):
                    new_i = k
                    break
                k -= 1
        end = sents[j].boundary_after if j + 1 < len(sents) else 1.0
        new_j = None
        if j + 1 < len(sents) and end < 0.5:
            m = j + 1
            start_i = new_i if new_i is not None else i
            while m < len(sents) and sents[m].end - sents[j].end <= look_seconds and sents[m].end - sents[start_i].start <= max_s:
                if sents[m - 1].boundary_after >= 0.85 or sents[m].edge in ("handoff", "turn"):
                    break
                after = sents[m].boundary_after if m + 1 < len(sents) else 1.0
                if sents[m].terminal and not sents[m].edge and after >= max(0.7, end + 0.2):
                    new_j = m
                    break
                m += 1
        # Closing the thought: keep going while the next sentence is still about what the clip is
        # about (most of its content words were already used in the clip), with no change of
        # speaker, hand-over, aside or long silence in between.
        if new_j is None and prof.close_share:
            start_i = new_i if new_i is not None else i
            toks = set().union(*[x.ctoks for x in sents[start_i:j + 1]])
            m = j
            while m + 1 < len(sents):
                nx = sents[m + 1]
                if nx.edge in ("handoff", "turn", "aside") or sents[m].boundary_after >= 0.9:
                    break
                if nx.end - sents[start_i].start > max_s or nx.end - sents[j].end > look_seconds:
                    break
                if len(nx.ctoks) < 3 or len(nx.ctoks & toks) / len(nx.ctoks) < prof.close_share:
                    break
                toks |= nx.ctoks
                m += 1
            while m > j and not sents[m].terminal:
                m -= 1
            if m > j:
                new_j = m
        for a, b, what in ((new_i, j, "start moved back to the opening of the point"),
                           (c.s0 if new_i is None else new_i, new_j, "end extended to the close of the point")):
            if a is None or b is None or (a, b) == (c.s0, c.s1) or not free(a, b):
                continue
            score, reasons = score_window(sents, a, b, target, prof)
            if score < c.score - tolerance:
                continue
            c.s0, c.s1, c.start, c.end = a, b, sents[a].start, sents[b].end
            c.text = " ".join(x.text for x in sents[a:b + 1])
            c.words = [w for x in sents[a:b + 1] for w in x.words]
            c.score, c.reasons = score, reasons + [what]
            changed += 1
    return changed


def complete_meaning(sents: list[Sentence], chosen: list[Candidate], prof: SelectionProfile, max_s: float,
                     target: float | None, tolerance: float = 0.15, max_extra: int = 4) -> int:
    """Profile v5: carry each clip's end through what completes its meaning.

    - a clip ending on a question continues through the answer (up to the next non-question line);
    - a following qualification ("but…", "however…", "to be fair…", "the catch is…") that changes what
      was just said is included, with the sentences that lean on it;
    - in interview/debate niches (prof.reply_required) the immediate reply is included.
    Never across a hand-over, a greeting or a long silence, never beyond max_s, never into another
    clip, and only if the result still scores within `tolerance` of the original. Returns the number
    of clips changed."""
    changed = 0
    df: dict[str, int] = {}
    for s in sents:
        for t in s.ctoks:
            df[t] = df.get(t, 0) + 1
    rare = max(2, int(len(sents) * 0.15))
    for c in chosen:
        others = [o for o in chosen if o is not c]
        j = c.s1
        m = j
        why = ""
        if prof.salient_continue:
            # The clip's distinctive subject: content words used at least twice in the clip and rare in
            # the recording as a whole. While the following sentences keep talking about it (and no
            # speaker change, hand-over, aside, new-subject announcement or long silence intervenes), the
            # discussion has not finished.
            counts: dict[str, int] = {}
            for s in sents[c.s0:j + 1]:
                for t in s.ctoks:
                    counts[t] = counts.get(t, 0) + 1
            salient = {t for t, k in sorted(counts.items(), key=lambda kv: -kv[1])[:6]
                       if k >= 2 and df.get(t, 0) <= rare}
            # Walk forward paragraph by paragraph: a new paragraph is followed only if its first sentence
            # is still about the clip's subject; inside a paragraph every sentence belongs. Candidate ends
            # are paragraph ends; the furthest one that still scores within tolerance wins.
            k, ends = j, []
            while salient and k + 1 < len(sents):
                cur, nx = sents[k], sents[k + 1]
                if cur.hard_break_after or nx.edge in ("handoff", "turn", "aside") or nx.opener \
                        or nx.start - cur.end > 4.0 or nx.end - sents[c.s0].start > max_s:
                    break
                if cur.boundary_after >= 0.85 and not (nx.ctoks & salient):
                    break                               # the next paragraph has moved on
                k += 1
                if sents[k].terminal and not sents[k].question and \
                        (k + 1 >= len(sents) or sents[k].boundary_after >= 0.85):
                    ends.append(k)
            for k in reversed(ends):
                if any(sents[c.s0].start < o.end + prof.min_gap and o.start < sents[k].end + prof.min_gap
                       for o in others):
                    continue
                if score_window(sents, c.s0, k, target, prof)[0] >= c.score - tolerance:
                    m = k
                    why = "end carried to the close of the discussion (same subject continues)"
                    break
        while m + 1 < len(sents) and m - j < max_extra:
            cur, nx = sents[m], sents[m + 1]
            if cur.hard_break_after and not cur.question and not prof.reply_required:
                break
            if nx.edge in ("handoff", "turn", "aside") or nx.start - cur.end > 4.0:
                break
            if nx.end - sents[c.s0].start > max_s:
                break
            text = _norm(nx.text)
            if cur.question:
                m += 1
                why = why or "end carried through the answer to its question"
                continue
            if QUALIFIER_START.search(text):
                m += 1
                why = why or "end carried through a qualification that changes the meaning"
                continue
            if prof.reply_required and REPLY_START.search(text):
                m += 1
                why = why or "end carried through the reply"
                continue
            if m > j and nx.dependency >= 0.3:
                m += 1                    # the added material is still being developed
                continue
            break
        while m > j and (not sents[m].terminal or sents[m].question):
            m -= 1
        if m <= j:
            continue
        if any(sents[c.s0].start < o.end + prof.min_gap and o.start < sents[m].end + prof.min_gap for o in others):
            continue
        score, reasons = score_window(sents, c.s0, m, target, prof)
        if score < c.score - tolerance:
            continue
        c.s1, c.end = m, sents[m].end
        c.text = " ".join(x.text for x in sents[c.s0:m + 1])
        c.words = [w for x in sents[c.s0:m + 1] for w in x.words]
        c.score, c.reasons = score, reasons + [why or "end carried to the close of the point"]
        changed += 1
    return changed


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
