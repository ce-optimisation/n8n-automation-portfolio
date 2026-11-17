"""Selection profile v6: context-complete clips. Completeness of meaning -> editorial value -> duration suitability.

Duration is a CONSTRAINT, never the objective. There is no fixed editorial ceiling: a genuinely complete passage may run
as long as it stays focused. What bounds the work is computational (seeds, windows scored, sentences expanded), not editorial.

How a clip is built (no fixed padding, no all-pairs window scan):
  1. Split the transcript into coherent REGIONS at hard breaks (long silence, hand-over, greeting) and strong topic breaks.
  2. Pick a limited number of SEEDS: the most substantive sentences of each region.
  3. EXPAND each seed to a complete excerpt: step back to the earliest necessary opening (never starting on a dependent
     line), run forward through a question's answer, a meaning-changing qualification and the point's own explanation,
     stop at a topic change or after sentences that add nothing new.
  4. TRIM to the shortest version that keeps the meaning (drop trailing repetition/filler and leading asides).
  5. ASSESS completeness on eight dimensions -> PASS / REVIEW / REJECT, with reasons. Only PASS (then REVIEW) is selected.
  6. Rank by completeness and editorial value; length earns nothing. A window beyond the content type's preferred range is
     "exceptional" only if it demonstrably stays substantive, cohesive and concludes; otherwise a shorter complete
     alternative (a prefix ending at a natural boundary) is preferred and the long one is demoted to REVIEW.
Zero clips is a valid result, and the report says why.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from . import segment as S

# Preferred working ranges (seconds) per content type. Preferences, not limits.
PREFERRED = {
    "political_monologue": (40.0, 180.0),
    "interview": (35.0, 180.0),
    "business": (30.0, 120.0),
    "podcast": (35.0, 180.0),
    "gaming": (12.0, 60.0),
    "comedy": (10.0, 45.0),
}
NICHE_KIND = {
    "politics": "political_monologue", "podcasts": "podcast", "psych_phil_theo": "podcast",
    "ai_business": "business", "finance": "business", "education": "business", "property": "business",
    "travel": "business", "consumer_tech": "business", "watches": "business",
    "gaming_streams": "gaming", "irl_streams": "comedy", "reaction_streams": "comedy", "sports_creators": "comedy",
}
CREATOR_KIND = {"competitive_gaming": "gaming", "gaming": "gaming", "reaction": "comedy", "irl": "comedy",
                "podcast": "podcast", "interview": "interview", "product": "business", "education": "business"}
SHORT_FORM = {"gaming", "comedy"}
REACTION = re.compile(r"\b(oh my god|omg|no way|let'?s go+|what the|bro+|yo+|holy|wow|ha(ha)+|lol|lmao|are you serious|"
                      r"he'?s dead|we won|i can'?t believe)\b", re.I)


def kind_for(niche_key: str | None, creator_type: str | None = None) -> str | None:
    return CREATOR_KIND.get((creator_type or "").lower()) or NICHE_KIND.get(niche_key or "")


def preferred_range(niche_key: str | None, creator_type: str | None, fallback: tuple[float, float]) -> tuple[float, float, str]:
    k = kind_for(niche_key, creator_type)
    lo, hi = PREFERRED.get(k, fallback)
    return lo, hi, k or "general"


@dataclass
class Budget:
    max_seeds: int = 120               # computational safeguards, NOT editorial limits
    max_windows: int = 4000
    max_expand_sentences: int = 600
    scored: int = 0
    expanded: int = 0
    hit: str = ""


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


def _markers(text: str) -> int:
    low = text.lower()
    return sum(1 for p in S.CLAIM_MARKERS if re.search(p, low))


def opening_ok(sents, i: int) -> bool:
    s = sents[i]
    if not s.follows_terminal or s.dependency >= 0.15 or s.edge in ("turn", "handoff", "aside"):
        return False
    if s.edge == "fragment" and not re.search(r"\d", s.text):
        return False
    return not (i > 0 and sents[i - 1].question and not s.question)


def _region_bounds(sents) -> list[tuple[int, int]]:
    out, lo = [], 0
    for g in range(len(sents) - 1):
        if sents[g].hard_break_after or (sents[g].boundary_after >= 0.9 and not sents[g].question):
            out.append((lo, g))
            lo = g + 1
    out.append((lo, len(sents) - 1))
    return out


def _value(s) -> float:
    txt = s.text
    n = max(1, len(_words(txt)))
    content = sum(1 for t in _words(txt) if t not in S.STOPWORDS and len(t) > 3) / n
    return content + 0.4 * min(3, _markers(txt)) + (0.5 if REACTION.search(txt) else 0.0) + 0.002 * n


def expand(sents, i: int, j: int, lo: int, hi: int, budget: Budget, reply_required: bool = False,
           anchor: bool = True) -> tuple[int, int, list[str]]:
    notes = []
    start = i
    for _ in range(budget.max_expand_sentences):
        if opening_ok(sents, i) and (i == lo or sents[i - 1].boundary_after >= 0.3 or sents[i].opener):
            break
        if i == lo:
            break
        i -= 1
        budget.expanded += 1
    if i != start:
        notes.append(f"opening moved back {start - i} sentence(s) to where the point begins")
    toks = set().union(*[x.ctoks for x in sents[i:j + 1]])
    stale = 0
    ext = 0
    while j < hi and ext < budget.max_expand_sentences:
        cur, nx = sents[j], sents[j + 1]
        gap = nx.start - cur.end
        if cur.hard_break_after or cur.edge == "handoff":
            break
        why = ""
        if not cur.terminal:
            why = "finishes the sentence"
        elif cur.question:
            why = "carries through the answer"
        elif S.QUALIFIER_START.search(S._norm(nx.text)) and gap < 2.5:
            why = "includes the qualification that changes the meaning"
        elif nx.dependency >= 0.3 and nx.edge != "turn" and gap < 2.5:
            why = "includes the sentence that leans on it"
        elif len(nx.ctoks) >= 3 and len(nx.ctoks & toks) / len(nx.ctoks) >= 0.4 and cur.boundary_after < 0.85:
            why = "continues the same point"
            fresh = len(nx.ctoks - toks) / len(nx.ctoks)
            if fresh < 0.15 and _markers(nx.text) == 0:
                stale += 1
                if stale >= 6:
                    break                   # a run of sentences adding nothing new: stop extending
            else:
                stale = 0
        elif cur.boundary_after < 0.6 and gap < 1.0 and nx.edge == "" and not nx.opener and nx.dependency < 0.6:
            why = "continues without a break in the speaker's flow"
            fresh = len(nx.ctoks - toks) / max(1, len(nx.ctoks))
            if fresh < 0.15 and _markers(nx.text) == 0:
                stale += 1
                if stale >= 6:
                    break
            else:
                stale = 0
        elif reply_required and cur.question is False and sents[j].boundary_after < 0.5 and nx.edge == "" and \
                S.REPLY_START.search(S._norm(nx.text)):
            why = "keeps the other side's reply"
        if not why:
            break
        toks |= nx.ctoks
        j += 1
        ext += 1
        budget.expanded += 1
        if why not in notes:
            notes.append(why)
    if ext >= budget.max_expand_sentences:
        budget.hit = "expansion budget reached on one passage"
    while j > i and not sents[j].terminal:
        j -= 1
    if anchor and i > lo and not sents[i].opener and (sents[i - 1].boundary_after < 0.7 or sents[i - 1].opener):
        # earliest NECESSARY opening: back to where the topic is introduced (an announced subject, or the start of the
        # paragraph), but only through material that is still about the same thing
        win = sents[i:j + 1]
        found = None
        for k in range(i - 1, max(lo - 1, i - 60), -1):
            if S._cos(sents[k:k + 3], win) < (0.03 if sents[k].opener else 0.08):
                break
            if opening_ok(sents, k) and (sents[k].opener or k == lo or sents[k - 1].boundary_after >= 0.7):
                found = k
                break
        if found is not None and found > lo and sents[found - 1].opener and opening_ok(sents, found - 1) \
                and S._cos(sents[found - 1:found + 2], win) >= 0.03:
            found -= 1                      # the announcement of the subject belongs with it
        if found is not None:
            notes.append(f"opening moved back {i - found} sentence(s) to where the topic is introduced")
            i = found
    return i, j, notes


def trim(sents, i: int, j: int) -> tuple[int, int, list[str]]:
    notes = []
    while j > i + 1:
        last, prev = sents[j], sents[j - 1]
        window = set().union(*[x.ctoks for x in sents[i:j]])
        repeated = len(last.ctoks) >= 3 and len(last.ctoks & window) / len(last.ctoks) >= 0.85 and _markers(last.text) == 0 \
            and not S.CONCLUSION.search(S._norm(last.text))
        weak = last.edge in ("aside", "fragment", "turn", "handoff") and not S.CONCLUSION.search(S._norm(last.text))
        if (repeated or weak) and not prev.question and prev.terminal:
            j -= 1
            notes.append("trimmed trailing repetition/filler")
        else:
            break
    while i < j - 1 and sents[i].edge in ("aside", "fragment") and opening_ok(sents, i + 1):
        i += 1
        notes.append("trimmed a leading aside")
    return i, j, list(dict.fromkeys(notes))


def assess(sents, i: int, j: int, pref: tuple[float, float], kind: str | None, reply_required: bool = False) -> dict:
    win = sents[i:j + 1]
    text = " ".join(s.text for s in win)
    toks = _words(text)
    n = max(1, len(toks))
    dur = win[-1].end - win[0].start
    lo, hi = pref
    head, tail = win[0], win[-1]
    short_form = kind in SHORT_FORM
    reasons, hard = [], []
    # A. opening / independent comprehensibility
    opening = 1.0 - min(1.0, head.dependency)
    if head.edge in ("turn", "handoff", "aside") or (head.edge == "fragment" and not re.search(r"\d", head.text)):
        opening = min(opening, 0.3)
    if not head.follows_terminal:
        opening = 0.0
    if i > 0 and sents[i - 1].question and not head.question:
        opening = min(opening, 0.3)
    if S.BACK_REFERENCE.search(S._norm(head.text)):
        opening = min(opening, 0.4)
    if opening < 0.6:
        hard.append("does not stand alone: " + ("; ".join(head.dep_reasons) or "the opening depends on earlier material"))
    elif i > 0 and sents[i - 1].boundary_after >= 0.7 or head.opener:
        reasons.append("opens where the point begins")
    # B. natural conclusion
    closure = 0.6
    if tail.terminal:
        closure += 0.15
    else:
        closure = 0.0
    if j + 1 >= len(sents) or tail.boundary_after >= 0.6 or tail.hard_break_after:
        closure = min(1.0, closure + 0.25)
    if S.CONCLUSION.search(S._norm(tail.text)) or (len(win) > 1 and S.CONCLUSION.search(S._norm(win[-2].text))):
        closure = min(1.0, closure + 0.2)
        reasons.append("ends on a concluding line")
    if tail.edge in ("handoff", "fragment") or (tail.edge == "turn" and len(win) > 1):
        closure = min(closure, 0.3)
        hard.append("ends on a hand-over or fragment rather than the point")
    last = tail.words[-1]["word"].strip().lower().strip(",.?!") if tail.words else ""
    if last in S.DANGLING_STARTS:
        closure = min(closure, 0.3)
        hard.append("ends on a dangling conjunction")
    # C. meaning preservation
    meaning = 1.0
    nxt = sents[j + 1] if j + 1 < len(sents) else None
    if tail.question and nxt is not None:
        meaning = 0.0
        hard.append("ends on a question before its answer")
    if nxt is not None and S.QUALIFIER_START.search(S._norm(nxt.text)) and nxt.start - tail.end < 2.5 and not tail.hard_break_after:
        meaning = min(meaning, 0.2)
        hard.append("a qualification that changes the meaning follows this cut")
    if nxt is not None and nxt.dependency >= 0.3 and nxt.edge != "turn" and nxt.start - tail.end < 2.0 and not tail.hard_break_after:
        meaning = min(meaning, 0.5)
        reasons.append("the next sentence still leans on this point")
    if any(s.question for s in win[:-1]):
        reasons.append("keeps a question with its answer")
    # D. strength of the central idea
    markers = _markers(text)
    content = sum(1 for t in toks if t not in S.STOPWORDS and len(t) > 3) / n
    react = bool(REACTION.search(text))
    if short_form:
        idea = min(1.0, 0.45 + 0.35 * react + 0.3 * min(1.0, markers / 2) + 0.2 * content)
        if not react and markers == 0 and content < 0.4:
            hard.append("no clear moment or payoff")
    else:
        idea = min(1.0, 0.5 * min(1.0, content * 1.8) + 0.5 * min(1.0, markers / (1.5 + dur / 40)))
        if len(toks) < 28 or len(win) < 2:
            hard.append("too little substance to be a complete point on its own")
            idea = min(idea, 0.3)
    if markers >= 3:
        reasons.append(f"{markers} substantive markers (claims, figures, reasoning)")
    # E. cohesion
    # Boundary strengths are RANKS within the recording (some gap is always "strong"), so a strong gap only counts as a
    # topic change when the vocabulary also stops flowing across it or the silence is long.
    shifts = 0
    for g in range(i, j):
        gp = sents[g]
        if gp.boundary_after >= 0.85:
            lex = S._cos(sents[i:g + 1], sents[g + 1:min(j + 1, g + 4)])
            if (lex < 0.04 and sents[g + 1].dependency < 0.15) or sents[g + 1].start - gp.end >= 2.5:
                shifts += 1
    cohesion = 1.0 if not shifts else max(0.0, 0.5 - 0.25 * shifts)
    if any(x.hard_break_after for x in win[:-1]):
        cohesion = 0.0
        hard.append("runs across a change of speaker, hand-over or long silence")
    elif shifts:
        hard.append("spans a change of topic")
    # F. economy: filler and repetition
    filler = sum(1 for t in toks if t in S.FILLERS) / n
    repeated = 0
    for k, s in enumerate(win):
        recent = set().union(*[x.ctoks for x in win[max(0, k - 6):k]]) if k else set()
        if s.ctoks and len(s.ctoks) >= 3 and len(s.ctoks & recent) / len(s.ctoks) >= 0.85 and not re.search(r"\d", s.text):
            repeated += 1
    rep_share = repeated / len(win)
    economy = max(0.0, 1.0 - 4.0 * filler - 1.2 * rep_share)
    if rep_share > 0.35:
        reasons.append("repetitive in places")
    dims = {"opening": opening, "conclusion": closure, "meaning": meaning, "central_idea": idea, "cohesion": cohesion,
            "economy": economy}
    composite = (0.22 * opening + 0.20 * closure + 0.22 * meaning + 0.18 * idea + 0.12 * cohesion + 0.06 * economy)
    # G. duration suitability (a constraint, not a goal)
    dclass = "within" if lo <= dur <= hi else ("below" if dur < lo else "above")
    exceptional = ""
    if dclass == "below" and not short_form and dur < lo * 0.6:
        hard.append(f"only {dur:.0f} s: too short to give the audience the context (preferred minimum {lo:.0f} s)")
    elif dclass == "below" and composite < 0.8:
        reasons.append(f"shorter than the preferred {lo:.0f}-{hi:.0f} s range")
    if dclass == "above":
        third = max(1, len(win) // 3)
        parts = [win[k:k + third] for k in range(0, len(win), third)][:3]
        dens = [_markers(" ".join(s.text for s in p)) + len(" ".join(s.text for s in p).split()) / 60 for p in parts]
        sustained = min(dens) >= 0.35 * (sum(dens) / len(dens)) if dens else False
        ok = sustained and cohesion >= 0.75 and closure >= 0.7 and rep_share <= 0.25 and meaning >= 0.8 and markers >= 3
        if ok:
            exceptional = (f"{dur:.0f} s, beyond the preferred {lo:.0f}-{hi:.0f} s: it stays substantive throughout "
                           f"({markers} reasoning/claim markers), is one cohesive passage and ends on a conclusion")
        else:
            reasons.append("beyond the preferred length without sustained substance: a shorter complete excerpt is preferred")
    if hard:
        verdict = "REJECT"
    elif exceptional or (dclass != "above" and composite >= 0.72 and min(dims.values()) >= 0.55):
        verdict = "PASS"
    elif composite >= 0.55:
        verdict = "REVIEW"
        reasons.append("potentially worthwhile but meaning or context is uncertain: human review needed")
    else:
        verdict = "REJECT"
        hard.append("completeness too low")
    if dclass == "above" and not exceptional and verdict == "PASS":
        verdict = "REVIEW"
    return {"verdict": verdict, "composite": round(composite, 3), "dimensions": {k: round(v, 2) for k, v in dims.items()},
            "duration_s": round(dur, 1), "duration_class": dclass, "preferred": [lo, hi], "exceptional": exceptional,
            "reasons": reasons, "rejected_because": hard, "content_type": kind or "general"}


def select_complete(sents, *, n: int, pref: tuple[float, float], kind: str | None, prof, report: dict,
                    reply_required: bool = False, budget: Budget | None = None) -> list:
    budget = budget or Budget()
    regions = _region_bounds(sents)
    report["regions"] = len(regions)
    seeds: list[tuple[float, int, int, int]] = []
    for lo, hi in regions:
        vals = [(_value(sents[k]), k) for k in range(lo, hi + 1) if sents[k].terminal]
        for v, k in sorted(vals, reverse=True)[:max(3, budget.max_seeds // max(1, len(regions)))]:
            seeds.append((v, k, lo, hi))
    seeds.sort(reverse=True)
    seeds = seeds[:budget.max_seeds]
    report["seeds"] = len(seeds)
    seen: set[tuple[int, int]] = set()
    assessed: list[tuple[float, S.Candidate]] = []
    rejected_reasons: dict[str, int] = {}
    for _, k, lo, hi in seeds:
        if budget.scored >= budget.max_windows:
            budget.hit = "window budget reached"
            break
        i, j, notes = expand(sents, k, k, lo, hi, budget, reply_required, anchor=kind not in SHORT_FORM)
        i, j, tnotes = trim(sents, i, j)
        wins = [(i, j, notes + tnotes)]
        # a long window also offers shorter complete alternatives ending at natural boundaries
        dur = sents[j].end - sents[i].start
        if dur > pref[1]:
            cuts = [m for m in range(i, j) if sents[m].terminal and not sents[m].question and
                    (sents[m].boundary_after >= 0.6 or S.CONCLUSION.search(S._norm(sents[m].text)))
                    and sents[m].end - sents[i].start >= pref[0] and sents[m].end - sents[i].start <= pref[1]]
            for m in cuts[-6:]:
                wins.append((i, m, notes + ["shorter complete alternative ending at a natural boundary"]))
        for a, b, nn in wins:
            if (a, b) in seen:
                continue
            seen.add((a, b))
            budget.scored += 1
            asm = assess(sents, a, b, pref, kind, reply_required)
            if asm["verdict"] == "REJECT":
                for r in asm["rejected_because"][:2]:
                    key = re.sub(r"\d+ s", "N s", r)[:90]
                    rejected_reasons[key] = rejected_reasons.get(key, 0) + 1
                continue
            base, why = S.score_window(sents, a, b, None, prof)
            words = [w for x in sents[a:b + 1] for w in x.words]
            c = S.Candidate(a, b, sents[a].start, sents[b].end, " ".join(x.text for x in sents[a:b + 1]), words, base,
                            why + nn, method="context-complete")
            c.assessment, c.exceptional, c.duration_class = asm, asm["exceptional"], asm["duration_class"]
            rank = asm["composite"] + (0.03 if sents[a].opener else 0.0) + 0.15 * max(0.0, min(1.0, base / 0.75)) + (0.05 if asm["verdict"] == "PASS" else 0.0) \
                - (0.0 if asm["duration_class"] != "above" else 0.04)
            assessed.append((rank, c))
    report["windows_scored"] = budget.scored
    report["rejected_reasons"] = dict(sorted(rejected_reasons.items(), key=lambda kv: -kv[1])[:6])
    if budget.hit:
        report["budget"] = budget.hit
    assessed.sort(key=lambda t: (-t[0], t[1].duration))          # ties: the shorter one
    ranked = [c for _, c in assessed]
    chosen = S.pick_diverse([c for c in ranked if c.assessment["verdict"] == "PASS"], n, min_gap=prof.min_gap)
    if len(chosen) < n:
        chosen = S.pick_diverse([c for c in ranked if c.assessment["verdict"] == "REVIEW"], n, already=chosen,
                                min_gap=prof.min_gap)
    report["pass"] = sum(1 for c in chosen if c.assessment["verdict"] == "PASS")
    report["review"] = sum(1 for c in chosen if c.assessment["verdict"] == "REVIEW")
    if not chosen:
        top = ", ".join(f"{k} (x{v})" for k, v in list(report["rejected_reasons"].items())[:3])
        report["note"] = ("No clips selected: no passage was complete and worthwhile on its own"
                          + (f". Main reasons: {top}" if top else "") + ". Nothing was padded or fragmented.")
    return sorted(chosen, key=lambda c: c.start)
