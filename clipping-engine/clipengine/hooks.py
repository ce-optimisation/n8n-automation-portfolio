"""Automatic hook titles for the upper title band, grounded in the selected clip's own transcript.

Pipeline for one clip:  sentences -> candidate hooks -> fidelity verification -> brand policy -> best accepted title.

Design rules (accuracy is mandatory, cleverness is optional):
  * Candidates are EXTRACTIVE: they are made of words the speaker actually said in this clip (compressed at clause
    boundaries), or a verified topic phrase from the clip. Nothing is invented; a local LLM (Ollama, if running) may
    only PROPOSE extra candidates and they go through exactly the same verification. No paid API, no model download.
  * Verification rejects a candidate when: a content word or number is not in the clip; negation or a hedge
    (might/probably/allegedly...) in the source clause was dropped; quoted text is not verbatim; it starts on an
    unresolved pronoun (needs context the clip may not give); or the brand policy forbids it.
  * Brand policy: America First Monitor only gets attributed/topic forms ("<Name> on <topic>"), never a bare
    assertion lifted from speech, and never inflammatory wording (safety screen). Other brands differ only in
    preferred shape.
  * Nothing here renders anything. The result is text in the existing `*emphasis*` markup, drawn by titles.py.
  * Owner-written or owner-approved titles are never overwritten (see ``may_regenerate``).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from . import safety, titles

TITLE_REV_KEY = "hook_rev"
MIN_WORDS, MAX_WORDS = 3, 9

STOP = set("""a about above after again against all also am an and any are as at be because been before being below
between both but by can could did do does doing down during each few for from further had has have having he her here
hers him his how i if in into is it its itself just me more most my no nor not now of off on once only or other our out
over own same she should so some such than that the their them then there these they this those through to too under
until up very was we were what when where which while who whom why will with would you your yours yeah okay ok like
think thought say said says thing things really kind sort gonna wanna got get going know mean right look well oh um uh actually basically literally""".split())
LEAD_FILLER = re.compile(r"^(?:(?:and|but|so|well|now|look|listen|yeah|yes|no|okay|ok|right|then|because|i mean|you know|"
                         r"i think|i guess|honestly|basically|frankly|anyway|see|like)\b[,\s]*)+", re.I)
DANGLING_END = set("""a an the of to in on at for with by from and or but because that which who whose is are was were be
been being has have had will would could should can may might must than as if when while so very more most""".split())
PRONOUN_START = re.compile(r"^(?:he|she|they|it|this|that|these|those|him|her|them|his|their|its|there|here)\b", re.I)
NEGATION = re.compile(r"\b(?:not|never|no|none|nobody|nothing|neither|nor|cannot|can't|won't|don't|doesn't|didn't|isn't|"
                       r"aren't|wasn't|weren't|wouldn't|couldn't|shouldn't|haven't|hasn't|hadn't)\b", re.I)
HEDGE = re.compile(r"\b(?:might|maybe|perhaps|probably|possibly|apparently|allegedly|reportedly|supposedly|seems?|"
                   r"could|may|claims?|claimed|rumou?red?|if)\b", re.I)
CLAIM_VERBS = re.compile(r"\b(?:is|are|was|were|will|would|means?|shows?|proves?|found|discovered|launched|announced|"
                         r"decided|learned|realised|realized|works?|changed|broke|won|lost|built|made|failed|"
                         r"happened|says?|said|argues?|argued|think|thinks|believe|believes)\b", re.I)
VERBISH = set("last cost get make take need want use work run go come show seem look feel mean tell become start stop keep "
              "give find happen change drive hold pick land buy sell rush double prepare answer match drop rise fall".split())
INFLAMMATORY = re.compile(r"\b(?:kill(?:s|ed|ing)?|murder\w*|shoot\w*|hang(?:ed|ing)?|lynch\w*|exterminat\w*|genocid\w*|"
                          r"eliminat\w*|traitors?|vermin|subhuman|invaders?|destroy\w*)\b", re.I)
EXAGGERATION = re.compile(r"\b(?:shocking|insane|destroyed|obliterated|exposed|you won'?t believe|bombshell|"
                          r"breaking|crazy|unbelievable|nobody is talking)\b", re.I)

# Shape preference per brand: weights for candidate methods, and the politics-style restrictions.
POLICY = {
    "america_first_monitor": dict(shapes={"attributed_topic": 2.5, "question": 0.8, "neutral_topic": 0.5},
                                  attribute_only=True, screen=True),
    "afterlive": dict(shapes={"claim": 1.0, "question": 0.7, "topic": 0.6, "neutral_topic": 0.4}),
    "operator_notes": dict(shapes={"claim": 1.0, "topic": 0.8, "question": 0.7, "neutral_topic": 0.4}),
    "spec_edit": dict(shapes={"claim": 1.0, "topic": 0.9, "question": 0.6, "neutral_topic": 0.4}),
    "dial_study": dict(shapes={"claim": 1.0, "topic": 0.9, "question": 0.6, "neutral_topic": 0.4}),
    "open_floor": dict(shapes={"question": 1.0, "claim": 0.9, "topic": 0.8, "neutral_topic": 0.4}),
}
DEFAULT_POLICY = dict(shapes={"claim": 1.0, "question": 0.8, "topic": 0.7, "neutral_topic": 0.4})

# Session 13 title-quality fixes. A topic label must be a NOUN phrase and a claim must be a complete clause:
#  * "Amazon AWS went" (from "It's not like Amazon AWS went down") was a verb fragment that also lost the negation;
#  * "Down detector data also show" / "Since that second wave of technology" were dangling clauses;
#  * "What does that tell you?" named no subject at all.
SUBORDINATOR_START = re.compile(r"^(?:since|although|though|while|whereas|unless|until|because|if|when|whenever|after|before|"
                                r"as|once|than|whether|which|who|whose|whom)\b", re.I)
VERBAL_FORMS = set("""went gone goes going came come comes took taken takes gave given gives saw seen sees made makes knew
known knows wrote written ran runs fell fallen falls felt feels got gotten became begin began begun broke broken brought
built bought caught chose chosen drove driven ate eaten flew flown forgot held hit kept led left lost met paid put read rose
sat sold sent set shut spoke spent stood stole taught told thought threw understood woke won wore means mean meant creates
create created shows show showed shown tells tell tells revolutionizes revolutionize carries carry cascaded cascade
caused causes cause leads lead brings bring turns turn becomes become gets get gives give takes take needs need wants want
uses use works work runs run happens happen happened decides decide decided found finds find said say says
lives live lived living looks look looked seems seem seemed feels feel knows know thinks think thinking wants wanted
playing plays played tries try tried getting coming doing saying telling asking asks asked sits stands waits wait waited
helps help helped keeps keep kept starts start started stops stop stopped opens open opened""".split())
VERBAL_SUFFIX = re.compile(r"(?:izes|ises|ifies|ized|ised)$", re.I)
# Conversational filler that carries no subject ('the good stuff', 'time sure let'). A topic may not contain it and a question
# needs two words that are NOT in this list.
GENERIC = set("""time sure let stuff thing things guy guys dude dudes man bro way lot lots bit kind something anything everything
nothing someone anyone everyone good bad great nice real right wrong little big new old other same own much many more most
whole entire actual literally damn okay alright yeah nah gonna gotta wanna""".split())
# A headline must never carry profanity or slurs, whatever the brand: recording 6 (a profane gameplay stream) produced
# 'Fuentes on fucking bitch'. The owner can still write such a title by hand; the generator never will.
PROFANE = re.compile(r"\b(?:f+u+c+k\w*|sh[i1]+t\w*|b[i1]tch\w*|cunt\w*|assh\w*|bastard\w*|whore\w*|slut\w*|dickhead\w*|"
                     r"n[i1]gg\w*|fagg?[o0]t\w*|retard\w*|k[i1]ke\w*|sp[i1]c\b|ch[i1]nk\b|tranny\w*|motherf\w*)\b", re.I)
CONTRACTION = re.compile(r"^(?:i|you|he|she|it|we|they|that|there|here|what|who|how|let|ain|do|does|did|is|are|was|were|"
                         r"have|has|had|would|could|should|can|will|wo)'(?:s|re|m|ll|d|ve|t)$|n't$", re.I)


def _is_verbal(tok: str) -> bool:
    """True for words that are (or are very likely) verbs, so they cannot sit inside a noun-phrase topic label."""
    t = tok.lower().strip("'-")
    return t in VERBAL_FORMS or bool(CLAIM_VERBS.fullmatch(t)) or bool(VERBAL_SUFFIX.search(t)) or _stem(t) in VERBISH


def _noun_runs(run: list[str]) -> list[list[str]]:
    """Split a run of content words at verbs; proper nouns/numbers (capitalised mid-sentence) are never treated as verbs."""
    out, cur = [], []
    for w in run:
        if CONTRACTION.search(w) or (_is_verbal(w) and not (w[:1].isupper() and len(cur) > 0)):
            if cur:
                out.append(cur)
            cur = []
        else:
            cur.append(w)
    if cur:
        out.append(cur)
    return out


@dataclass
class Candidate:
    text: str                       # plain words, no emphasis markup
    method: str                     # claim | question | topic | attributed_topic | neutral_topic | llm
    support: str = ""               # transcript sentence(s) the words come from
    score: float = 0.0
    warnings: list[str] = field(default_factory=list)
    accepted: bool = False
    tail: str = ""                  # words of the source sentence the hook did not use


# ---- text helpers ---------------------------------------------------------------------------------

def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')).strip()


def _toks(s: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9][A-Za-z0-9'\-]*", _norm(s))


def _stem(w: str) -> str:
    w = w.lower().strip("'-")
    for suf in ("'s", "ing", "ed", "es", "s", "ly"):
        if len(w) > len(suf) + 3 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def _content(s: str) -> list[str]:
    return [t for t in _toks(s) if t.lower() not in STOP]


def split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+(?=[\"'A-Z0-9])", _norm(text))
    return [p.strip() for p in parts if p.strip()]


def _is_proper(tok: str, sentence: str) -> bool:
    return tok[:1].isupper() and not sentence.lower().startswith(tok.lower()) and tok.lower() not in STOP


# ---- candidate generation -------------------------------------------------------------------------

def _clause_cut(words: list[str]) -> list[str] | None:
    """Shortest natural-looking prefix of <= MAX_WORDS ending on a content word, preferring a clause boundary."""
    if len(words) <= MAX_WORDS + 3:
        cut = words
    else:
        cut = None
        for i in range(min(MAX_WORDS, len(words)), MIN_WORDS - 1, -1):
            if words[i - 1].endswith((",", ";", ":")) or (i < len(words) and words[i].lower() in {"and", "but", "because", "which", "so", "that"}):
                cut = words[:i]
                break
        if cut is None:
            cut = words[:MAX_WORDS]
            while len(cut) >= MIN_WORDS and cut[-1].lower().strip(",;:.") in DANGLING_END:
                cut = cut[:-1]
    cut = [w for w in cut]
    while cut and cut[-1].lower().strip(",;:.") in DANGLING_END:
        cut = cut[:-1]
    return cut if len(cut) >= MIN_WORDS else None


def _claim_candidates(sentences: list[str]) -> list[Candidate]:
    out = []
    for s in sentences:
        if s.rstrip().endswith("?"):
            continue
        body = LEAD_FILLER.sub("", s).strip()
        body = re.sub(r"[.!]+$", "", body)
        if not body or PRONOUN_START.match(body):
            continue
        words = body.split()
        cut = _clause_cut(words)
        if not cut:
            continue
        text = " ".join(cut).rstrip(",;:-")
        proper = any(_is_proper(t, text) for t in _toks(text)) or any(t.isdigit() for t in _toks(text))
        if len(_content(text)) < (2 if proper else 3):
            continue
        # the hook is only the lead of a longer sentence: the dropped tail must not carry a negation or a hedge
        tail = " ".join(words[len(cut):])
        out.append(Candidate(text=text, method="claim", support=s, tail=tail))
    return out


def _question_candidates(sentences: list[str]) -> list[Candidate]:
    out = []
    for s in sentences:
        if not s.rstrip().endswith("?"):
            continue
        q = LEAD_FILLER.sub("", s).strip()
        words = q.split()
        if len(words) < 4 or len(words) > MAX_WORDS + 3 or PRONOUN_START.match(q):
            continue
        out.append(Candidate(text=q, method="question", support=s))
    return out


def _topic_phrase(text: str) -> tuple[str, int] | None:
    """Most characteristic noun-like phrase: runs of consecutive content words (<=3), scored by length, proper
    nouns/numbers and how often the clip returns to them. Words are exactly as spoken."""
    freq: dict[str, int] = {}
    for t in _content(text):
        freq[_stem(t)] = freq.get(_stem(t), 0) + 1
    best, best_score = None, 0.0
    for s in split_sentences(text):
        toks = re.findall(r"[A-Za-z0-9][A-Za-z0-9'\-]*|[,;:.?!]", _norm(s))
        run: list[str] = []
        for i, t in enumerate(toks + ["."]):
            if re.match(r"[A-Za-z0-9]", t) and t.lower() not in STOP and not (len(run) == 0 and _stem(t) in {"said", "says"}):
                run.append(t)
                continue
            for sub in (_noun_runs(run) if run else []):     # noun phrases only: never a verb fragment
                phrase = sub[:3]
                while len(phrase) > 1 and _is_verbal(phrase[-1]):
                    phrase = phrase[:-1]
                proper = sum(1 for w in phrase if w[:1].isupper() and not s.lower().startswith(w.lower()) or w.isdigit())
                score = len(phrase) * 1.0 + proper * 1.5 + sum(freq[_stem(w)] - 1 for w in phrase) * 0.7
                if len(phrase) == 1 and not proper:
                    score -= 1.5
                if score > best_score:
                    best, best_score = " ".join(phrase), score
            run = []
    return (best, 1) if best else None


def _topic_candidates(text: str, speaker: str | None, attribute: bool) -> list[Candidate]:
    tp = _topic_phrase(text)
    if not tp:
        return []
    phrase, n = tp
    phrase = phrase if phrase[:1].isupper() else phrase
    out = []
    if speaker:
        out.append(Candidate(text=f"{speaker} on {phrase}", method="attributed_topic", support=phrase))
    if not attribute:
        out.append(Candidate(text=phrase, method="topic", support=phrase))
    out.append(Candidate(text=phrase, method="neutral_topic", support=phrase))
    return out


def ollama_candidates(url: str, model: str, text: str, brand_name: str) -> list[str]:
    """Optional: ask a LOCAL model for extra hook wordings. Used only to propose; every one is verified like the rest."""
    import httpx
    prompt = (f"Write 4 short hook headlines (4-9 words each) for a vertical video clip for the brand '{brand_name}'. "
              "Use ONLY facts and wording stated in the transcript below; do not add claims, names or numbers; keep any "
              "qualification the speaker made; attribute controversial claims to the speaker. Return JSON only: "
              "{\"titles\": [\"...\"]}.\n\nTranscript:\n" + text[:3000])
    r = httpx.post(url.rstrip("/") + "/api/generate", timeout=120,
                   json={"model": model, "prompt": prompt, "stream": False, "format": "json", "options": {"temperature": 0.2}})
    r.raise_for_status()
    data = json.loads(r.json().get("response", "{}"))
    return [str(t) for t in data.get("titles", []) if isinstance(t, str)][:6]


def _best_sentence(title: str, transcript: str) -> str:
    stems = {_stem(t) for t in _content(title)}
    best, hit = "", 0
    for sent in split_sentences(transcript):
        n = len(stems & {_stem(t) for t in _toks(sent)})
        if n > hit:
            best, hit = sent, n
    return best


# ---- verification ---------------------------------------------------------------------------------

def verify(c: Candidate, transcript: str, policy: dict, speaker: str | None = None, data_dir=None) -> Candidate:
    """Reject (c.accepted False, reasons in c.warnings starting 'REJECT') or accept a candidate against the clip."""
    tx = _norm(transcript)
    tx_stems = {_stem(t) for t in _toks(tx)}
    reject = []
    speaker_toks = {_stem(t) for t in _toks(speaker or "")}
    plain = re.sub(r"\*+", "", c.text)
    for t in _content(plain):
        if _stem(t) in speaker_toks and c.method == "attributed_topic":
            continue
        if _stem(t) not in tx_stems and not (t.isdigit() and t in tx):
            reject.append(f"'{t}' does not appear in the clip")
    for n in re.findall(r"\d[\d,.]*", plain):
        if n not in tx:
            reject.append(f"number {n} is not in the clip")
    for q in re.findall(r'"([^"]+)"', plain):
        if _norm(q).lower() not in tx.lower():
            reject.append("quoted wording is not verbatim from the clip")
    if c.method == "claim" and c.tail and (NEGATION.search(c.tail) or HEDGE.search(c.tail)):
        reject.append("the rest of the sentence carries a qualification (negation/hedge) that this hook leaves out")
    if c.method == "llm":
        src = _best_sentence(plain, tx)
        if src and NEGATION.search(src) and not NEGATION.search(plain):
            reject.append("the nearest source sentence is negated but the hook is not")
        if src and HEDGE.search(src) and not HEDGE.search(plain):
            reject.append("the nearest source sentence is hedged but the hook states it plainly")
    if c.method in ("claim", "llm") and PRONOUN_START.match(plain):
        reject.append("starts on a pronoun that needs context the clip may not give")
    if len(_toks(plain)) < (2 if c.method in ("topic", "neutral_topic") else MIN_WORDS):
        reject.append("too short to be a meaningful hook")
    if EXAGGERATION.search(plain) and not EXAGGERATION.search(tx):
        reject.append("sensational wording not used in the clip")
    if policy.get("attribute_only") and c.method in ("claim", "topic", "llm"):
        reject.append("this brand only uses attributed or topic wording, never a bare assertion")
    if policy.get("screen") and (safety.screen(plain, data_dir) or INFLAMMATORY.search(plain)):
        reject.append("contains inflammatory language that a headline would amplify")
    if c.method in ("attributed_topic", "topic") and len(_toks(plain.replace(speaker or "", ""))) <= 2 \
            and not any(t[:1].isupper() or t.isdigit() for t in _toks(plain.replace(speaker or "", "")) if t.lower() != "on"):
        reject.append("topic is a single common word - too vague to be a fair summary")
    # Session 13 structural checks: complete statements, noun-phrase topics, questions that name a subject.
    toks_plain = _toks(plain)
    if PROFANE.search(plain):
        reject.append("contains profanity or a slur; the generator never puts that in a headline")
    if any(a.lower() == b.lower() for a, b in zip(toks_plain, toks_plain[1:])):
        reject.append("repeats the same word twice in a row, so it is a speech stumble rather than a subject")
    if c.method == "claim":
        if SUBORDINATOR_START.match(plain):
            reject.append("starts as a dependent clause (since/although/when...); it is not a complete statement")
        if toks_plain and _is_verbal(toks_plain[-1]):
            reject.append("ends on a verb that still needs its object, so the sentence is left hanging")
    if c.method == "question" and len(_content(plain)) < 2:
        reject.append("the question names no subject, so it means nothing without the surrounding clip")
    if policy.get("attribute_only") and re.match(r"^(?:i|i'm|i've|i'll|i'd|my|me|we|we're|we've|our|let me|let's)\b", plain, re.I):
        reject.append("a first-person line would read as the creator's own admission or claim in a headline")
    if c.method == "question" and sum(1 for t in _content(plain) if t.lower() not in GENERIC) < 2:
        reject.append("the question is built from conversational filler, not a subject")
    if c.method == "question" and re.search(r"\b(?:it|that|this|them|these|those|he|she|they)\b", plain, re.I) \
            and not any(t[:1].isupper() and i > 0 for i, t in enumerate(toks_plain)) and not any(t.isdigit() for t in toks_plain):
        reject.append("the question points at something only the surrounding clip explains (it/that/this) and names nothing")
    if c.method in ("topic", "attributed_topic", "neutral_topic"):
        body = re.sub(r"^\s*on\s+", "", plain.replace(speaker or "", "", 1).strip(), flags=re.I)
        if any(CONTRACTION.search(t) for t in _toks(body)):
            reject.append("the topic label contains a contraction, so it ran across a sentence boundary")
        if any(t.lower() in GENERIC for t in _toks(body)):
            reject.append("the topic label contains conversational filler, so it is not a real subject")
        if any(_is_verbal(t) for t in _toks(body)):
            reject.append("the topic label contains a verb, so it is a sentence fragment rather than a subject")
        src = _best_sentence(body, tx)
        if src and (NEGATION.search(src) or HEDGE.search(src)):
            c.warnings.append("the passage this label comes from is negated or hedged; the label names the subject only - "
                              "check it does not imply the opposite")
    reject += strict_problems(c, plain, tx)
    c.warnings = [f"REJECT: {r}" for r in reject] + [w for w in c.warnings if not w.startswith("REJECT")]
    c.accepted = not reject
    return c


# ---- Session 14 strict gate: "good title or no title" ----------------------------------------------
# Audit of 27 live titles found ~11% usable. Every rule below comes from a real failure in that audit; when in doubt the
# generator now returns NO title rather than a fragment.
AD_READ = re.compile(r"\b(?:sponsor\w*|promo\w*|discount|use (?:the )?code|our code|coupon|% off|black friday|sale|"
                     r"check (?:them|it|this|that) out|go check|grab your|link (?:in|below)|subscribe|use my link|"
                     r"brought to you|affiliate|dot com|https?|www)\b|\.(?:com|app|io|tv|net|org)\b|\d+%\s*off", re.I)
DANGLING_TAIL = set("then now here there essentially though anyway like you it them him her us me too also yet still "
                    "again basically really kind sort bit lot some of".split())
FIRST_PERSON = re.compile(r"^(?:i|i'm|i've|i'll|i'd|my|me|we|we're|we've|we'll|let's|let me|you|you're|your)\b", re.I)
GERUND = re.compile(r"^[A-Za-z]{4,}ing$")
DEICTIC = re.compile(r"\b(?:top|bottom)\s+(?:left|right)\b|\b(?:this|that) (?:one|thing|guy|photo|picture|video|screen)\b|"
                     r"\bover here\b|\bon (?:the )?screen\b|\bright there\b|\blook at (?:this|that)\b|"
                     r"\b(?:of|on|in) (?:these|those)\b", re.I)


def strict_problems(c: Candidate, plain: str, tx: str) -> list[str]:
    """Extra rejection reasons for claims, questions and topic labels. Empty list = passes the strict gate."""
    out: list[str] = []
    toks = _toks(plain)
    if not toks:
        return out
    if AD_READ.search(plain) or AD_READ.search(c.support or "") or AD_READ.search(tx):
        out.append("this clip contains an advert or sponsor read; no automatic title")
    if re.search(r"damn|hell\b|crap", plain, re.I):
        out.append("contains mild profanity")
    if c.method == "claim":
        if plain[:1].islower():
            out.append("starts mid-sentence (lower-case), so it is a fragment")
        tail_words = c.tail.split()
        if tail_words and tail_words[0].lower().strip(",") not in {"and", "but", "so", "which", "because", "that", "while"} \
                and (c.support or "").find(plain + ",") < 0:
            out.append("only the front of a longer sentence; a title must be a complete statement")
        if toks[-1].lower() in DANGLING_TAIL:
            out.append("ends on a word that needs more after it")
        if FIRST_PERSON.match(plain):
            out.append("first/second-person speech is not a headline without its context")
        if len(toks) < 4:
            out.append("too short to be a complete statement")
        if not any(_is_verbal(t) or t.lower() in {"is", "are", "was", "were", "has", "have", "had", "did", "does", "do"}
                   for t in toks[1:]):
            out.append("no verb, so it is not a statement")
        if "," in plain and plain.rstrip().endswith(","):
            out.append("ends on a comma")
        if DEICTIC.search(plain):
            out.append("points at something on screen or in the room (top right, this one...) that a headline cannot show")
        if re.search(r",\s+(?:the|he|she|they|it|i|we|you|there)\b", plain, re.I):
            out.append("two clauses run together with a comma: not one clear statement")
        sup = _best_sentence(plain, tx)
        if sup and re.search(r"\b(?:what if|imagine|suppose|hypothetical|scenario|let's say|for example|say that)\b", tx, re.I):
            out.append("the clip discusses a hypothetical or example; a bare statement could be read as fact")
    if c.method == "question":
        if FIRST_PERSON.match(plain) or re.search(r"\b(?:i|we|you)\b", plain, re.I):
            out.append("a first/second-person question needs the surrounding conversation")
        if not any(t[:1].isupper() and i > 0 for i, t in enumerate(toks)):
            out.append("a question that names no person, place or organisation is too generic to be a headline")
    if c.method in ("topic", "neutral_topic"):
        out.append("a bare topic label is not a headline (Session 15: good title or no title)")
    if c.method in ("topic", "attributed_topic", "neutral_topic"):
        body = re.sub(r"^\s*\w[\w' .-]*? on\s+", "", plain, count=1) if c.method == "attributed_topic" else plain
        if any(GERUND.match(t) for t in _toks(body)):
            out.append("the label contains an -ing word, so it is probably a verb fragment")
        src = _best_sentence(body, tx)
        if src and (NEGATION.search(src) or HEDGE.search(src)):
            out.append("the passage this label comes from is negated or hedged; a bare label could imply the opposite")
        btoks = _toks(body)
        if len(btoks) < 2 or not any(t[:1].isupper() for t in btoks):
            out.append("a label needs at least two words including a name, so it names a real subject")
    return out


# ---- scoring and selection ------------------------------------------------------------------------

def _score(c: Candidate, policy: dict, transcript: str) -> float:
    toks = _toks(c.text)
    n = len(toks)
    shape = policy["shapes"].get(c.method, 0.3)
    specificity = sum(1 for t in toks if t[:1].isupper() and t.lower() not in STOP) * 0.5 + sum(1 for t in toks if t.isdigit()) * 0.6
    specificity += 0.25 * len(_content(c.text))
    length = 1.0 if 4 <= n <= 8 else 0.7 if n == 9 else 0.5
    verbs = 0.4 if CLAIM_VERBS.search(c.text) else 0.0
    penalty = 0.6 * len([w for w in c.warnings if not w.startswith("REJECT")])
    return round(shape * (1.0 + specificity + verbs) * length - penalty, 3)


def policy_for(brand_key: str | None) -> dict:
    return POLICY.get(brand_key or "", DEFAULT_POLICY)


def generate(transcript: str, brand_key: str | None = None, speaker: str | None = None, llm_titles: list[str] | None = None,
             data_dir=None, max_alternatives: int = 4) -> dict:
    """Propose hook titles for one clip. Returns a JSON-ready dict:
    {status: proposed|neutral|manual_needed, title, alternatives[], candidates[], method, brand_policy, support}."""
    policy = policy_for(brand_key)
    speaker = (speaker or "").strip() or None
    sents = split_sentences(transcript)
    cands = []
    if sents:
        if not policy.get("attribute_only"):
            cands += _claim_candidates(sents)
        cands += _question_candidates(sents)
        cands += _topic_candidates(transcript, speaker, bool(policy.get("attribute_only")))
    for t in llm_titles or []:
        cands.append(Candidate(text=_norm(t).strip(' "'), method="llm", support=transcript))
    seen, uniq = set(), []
    for c in cands:
        key = c.text.lower()
        if key not in seen:
            seen.add(key)
            uniq.append(c)
    for c in uniq:
        verify(c, transcript, policy, speaker, data_dir)
        if c.method == "attributed_topic":
            c.warnings.append(f"assumes {speaker} is the speaker of this passage - confirm before approving")
        c.score = _score(c, policy, transcript)
    ok = sorted((c for c in uniq if c.accepted), key=lambda c: -c.score)
    real = [c for c in ok if c.method != "neutral_topic"]
    best = real[0] if real else (ok[0] if ok else None)
    status = "manual_needed" if not best else ("neutral" if best.method in ("neutral_topic", "topic") else "proposed")
    alts = [c.text for c in ok if c is not best][:max_alternatives]
    return {
        "status": status, "title": best.text if best else None, "method": best.method if best else None,
        "support": best.support if best else None, "brand_policy": brand_key or "default",
        "alternatives": alts,
        "candidates": [{"text": c.text, "method": c.method, "score": c.score, "accepted": c.accepted,
                        "warnings": c.warnings} for c in sorted(uniq, key=lambda c: -c.score)[:12]],
        "warnings": best.warnings if best else ["no candidate passed verification; enter a title manually"],
    }


def render_markup(title: str | None) -> str | None:
    """The generated wording with the approved emphasis (last two words), as the existing renderer expects."""
    return titles.format_title(title) if title else None


# ---- clip-row helpers (owner choices always win) ----------------------------------------------------

def may_regenerate(clip) -> bool:
    """Generated titles may be refreshed for a new clip revision; owner-written, approved or locked ones never are."""
    return (clip["title_source"] or "generated") != "owner" and not clip["title_locked"] and (clip["title_status"] or "") not in ("approved", "removed")


def fields_for(result: dict, rev: int = 1) -> dict:
    """DB columns for a generation result (title stays as plain words; emphasis is applied at render time when unmarked)."""
    t = result.get("title")
    return {"title": t, "title_source": "generated", "title_status": result["status"] if t else "manual_needed",
            "title_locked": 0, "title_rev": rev, "title_json": json.dumps(result)}
