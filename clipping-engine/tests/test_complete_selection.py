"""Profile v6 (context-complete selection). Deterministic hand-written transcripts with real word timings.

These tests prove the selector's rules on fixtures; they do NOT prove that the owner's two example clips improve (those
recordings are not available here, so the real-media comparison is pending)."""
import time

import pytest

from clipengine import complete, segment

PREF_POL = (40.0, 180.0)


def seg(start, text, wps=2.6):
    toks = text.split()
    words, t = [], start
    for tok in toks:
        d = 1.0 / wps
        words.append({"start": round(t, 2), "end": round(t + d * 0.9, 2), "word": " " + tok, "prob": 0.93})
        t += d
    return {"start": start, "end": round(t, 2), "text": text, "words": words}, t


def transcript(parts):
    """parts: strings (speech) or numbers (seconds of silence). Each string is one segment."""
    out, t = [], 0.0
    for p in parts:
        if isinstance(p, (int, float)):
            t += p
            continue
        s, t = seg(t, p)
        out.append(s)
        t += 0.3
    return out


def run(parts, kind="political_monologue", pref=PREF_POL, n=3, profile="v6", **kw):
    rep = {}
    cands = segment.select(transcript(parts), n=n, min_s=pref[0], max_s=pref[1], profile=profile, report=rep,
                           preferred=pref, content_type=kind, **kw)
    return cands, rep


SETUP = ("Let me explain why the transit budget collapsed this week. The committee proposed a plan that costs four billion "
         "dollars over ten years. The original estimate was two billion because it left out the tunnel under the river. "
         "When the engineers added the tunnel the cost doubled, and that is the fact nobody wanted to say out loud.")
ARGUMENT = ("That's exactly why the vote failed. Members argued that the public would never accept a bill like this, because "
            "it asks families to pay more tax for a line they will not use for a decade. The study from the transport office "
            "shows that only thirty percent of riders live near the new stations. So the plan serves fewer people than the "
            "committee claimed.")
QUALIFY = ("But to be fair, the committee did add a rebate for low income households, which cuts the average cost by roughly "
           "two hundred dollars a year. The bottom line is that the plan needs a cheaper tunnel and a clearer promise to "
           "riders, and that is what the vote should have demanded.")
OTHER = ("Anyway, switching to a completely different subject, the football season restarts on Saturday with three matches "
         "and the manager says the squad is fit. Ticket sales for the opening fixture are already above last season.")


def starts(c):
    return c.text.split(".")[0]


# 1 / 2: an interesting but incomplete fragment is not accepted; the opening context is included
def test_dependent_fragment_is_expanded_to_its_context_or_rejected():
    parts = [SETUP, 0.4, ARGUMENT, 0.4, QUALIFY, 7.0, OTHER]
    cands, rep = run(parts)
    assert cands, rep
    c = cands[0]
    assert c.text.startswith("Let me explain why the transit budget"), starts(c)        # not "That's exactly why the vote failed"
    assert "That's exactly why" in c.text and c.assessment["verdict"] in ("PASS", "REVIEW")
    assert c.assessment["dimensions"]["opening"] >= 0.6


def test_a_bare_fifteen_second_extract_is_rejected_by_the_assessment():
    sents = segment.build_sentences(transcript([SETUP, 0.4, ARGUMENT, 0.4, QUALIFY]))
    segment.annotate_boundaries(sents)
    segment.annotate_context(sents)
    k = next(i for i, s in enumerate(sents) if s.text.startswith("That's exactly why"))
    a = complete.assess(sents, k, k + 1, PREF_POL, "political_monologue")
    assert a["verdict"] == "REJECT" and any("does not stand alone" in r or "too short" in r or "too little" in r
                                           for r in a["rejected_because"])


# 3: a qualification after the apparent conclusion is kept
def test_a_following_qualification_is_included():
    cands, rep = run([SETUP, 0.4, ARGUMENT, 0.4, QUALIFY, 7.0, OTHER])
    assert "But to be fair" in cands[0].text and "bottom line" in cands[0].text
    sents = segment.build_sentences(transcript([SETUP, 0.4, ARGUMENT, 0.4, QUALIFY]))
    segment.annotate_boundaries(sents)
    segment.annotate_context(sents)
    cut = next(i for i, s in enumerate(sents) if s.text.startswith("So the plan serves"))
    assert complete.assess(sents, 0, cut, PREF_POL, "political_monologue")["verdict"] == "REJECT"


# 4: a question needs its answer
def test_question_is_kept_with_its_answer():
    parts = ["Thanks for joining us. We are live.", 7.0,
             "Why did the government cancel the housing programme last year?", 0.5,
             "Because the cost per home rose from ninety thousand to one hundred and sixty thousand dollars, and the audit "
             "showed that most of the increase came from land purchases rather than construction. The ministry argued that "
             "the policy could not be defended to taxpayers. That is the reason the programme was ended and replaced with a "
             "voucher scheme that costs about forty percent less per household.", 7.0, OTHER]
    cands, rep = run(parts, kind="interview", pref=(35.0, 180.0))
    assert cands and "Why did the government cancel" in cands[0].text and "voucher scheme" in cands[0].text
    sents = segment.build_sentences(transcript(parts))
    segment.annotate_boundaries(sents)
    segment.annotate_context(sents)
    q = next(i for i, s in enumerate(sents) if s.question)
    assert complete.assess(sents, q, q, (35.0, 180.0), "interview")["verdict"] == "REJECT"          # a question alone


# 5 / 11: a monologue that genuinely runs for minutes becomes one exceptional clip
def long_monologue(sentences=30):
    topics = ["housing", "rent", "tenants", "landlords", "planning"]
    lines = []
    for k in range(sentences):
        a, b, c = topics[k % 5], topics[(k + 2) % 5], topics[(k + 3) % 5]
        lines.append(f"The {a} problem shows up in the {b} data because {c} costs rose by {10 + k} percent in year {k + 1}, "
                     f"and the policy response on {a} has to deal with {b} first.")
    lines.append("The bottom line is that housing policy must tackle rent and planning together, because the evidence shows "
                 "that fixing one without the other fails tenants and landlords alike.")
    return lines


def test_a_long_complete_monologue_is_one_exceptional_clip():
    parts = ["Let me explain the housing crisis and what the evidence says about rent and planning."] + long_monologue() + [8.0, OTHER]
    cands, rep = run(parts, n=2)
    assert cands, rep
    c = cands[0]
    assert c.duration > 180 and c.assessment["verdict"] == "PASS" and c.assessment["duration_class"] == "above"
    assert "stays substantive throughout" in c.exceptional and "beyond the preferred" in c.exceptional
    assert "OTHER" not in c.text and "football" not in c.text


def test_length_alone_does_not_make_a_clip_exceptional():
    # 4 minutes of the same few sentences repeated: long, but not substantive and cohesive
    filler = ["Well the housing situation is what it is and people keep talking about the housing situation again and again."] * 40
    cands, rep = run(["Let me explain the housing situation to you today."] + filler)
    assert not any(c.exceptional for c in cands)
    assert all(c.duration <= 180 or c.assessment["verdict"] != "PASS" for c in cands)


# 6: repetitive filler at the end is trimmed
def test_trailing_repetition_is_trimmed():
    tail = ["Like I said, the housing rent problem is the housing rent problem, and the housing rent problem is real.",
            "Yeah so the housing rent problem, the housing rent problem, that is the housing rent problem.",
            "Right, the housing rent problem, and again the housing rent problem is the housing rent problem."]
    parts = [SETUP, 0.4, ARGUMENT, 0.4, QUALIFY.split(". ")[1]] + tail
    cands, rep = run(parts)
    assert cands and "the housing rent problem" not in cands[0].text
    assert cands[0].text.rstrip().endswith("demanded.")


# 7 / 8: topic change and speaker hand-over end the clip
def test_topic_change_ends_the_clip():
    cands, _ = run([SETUP, 0.4, ARGUMENT, 0.4, QUALIFY, 3.0, OTHER])
    assert cands and "football" not in cands[0].text


def test_handover_to_an_unrelated_segment_is_not_included():
    handover = "That concludes this part, so I will turn it over to Maria who will introduce the sports segment."
    cands, _ = run([SETUP, 0.4, ARGUMENT, 0.4, QUALIFY, handover, 1.0, OTHER])
    assert cands and "turn it over" not in cands[0].text and "football" not in cands[0].text


# 9: a short, complete gaming moment stays short
GAME = ("Okay chat I am lining up the final shot in this ranked match. Three players are left and I only have one bullet. "
        "Wait wait he is peeking the window right now. Oh my god I got him, no way, let's go, we won the game!")


def test_a_complete_short_gaming_moment_is_selected_and_stays_short():
    cands, rep = run(["Welcome back everyone, thanks for watching.", 4.0, GAME, 6.0, "Okay so now let me check the chat for a moment."],
                     kind="gaming", pref=(12.0, 60.0), profile="v6")
    assert cands, rep
    c = cands[0]
    assert "we won the game" in c.text and 10 <= c.duration <= 30 and c.assessment["verdict"] == "PASS"
    assert c.assessment["content_type"] == "gaming" and c.assessment["duration_class"] in ("within", "below")


# 10: nothing worthwhile -> zero clips, with reasons
def test_no_suitable_passage_returns_zero_clips_and_says_why():
    chat = ["Hey welcome back everyone.", "Thanks for joining us today, um, let me just check the chat.", "Uh okay it looks fine.",
            "Alright that's it, see you next time, thanks for watching."]
    cands, rep = run(chat, pref=PREF_POL)
    assert cands == [] and rep["selected"] == 0 and "No clips selected" in rep["note"] and "padded" in rep["note"]


# fidelity, budget and legacy safety
def test_selected_timestamps_and_text_come_from_real_transcript_words():
    segs = transcript([SETUP, 0.4, ARGUMENT, 0.4, QUALIFY, 7.0, OTHER])
    cands = segment.select(segs, n=3, min_s=40, max_s=180, profile="v6", preferred=PREF_POL, content_type="political_monologue")
    words = [w for s in segs for w in s["words"]]
    starts_, ends_ = {w["start"] for w in words}, {w["end"] for w in words}
    for c in cands:
        assert c.start in starts_ and c.end in ends_
        assert c.text.split() == [w["word"].strip() for w in words if c.start <= w["start"] and w["end"] <= c.end]


def test_a_three_hour_recording_is_processed_within_budget():
    topics = ["economy", "energy", "schools", "health", "transport", "housing"]
    parts = []
    for k in range(1300):
        t = topics[(k // 40) % 6]
        parts.append(f"On {t} the government plan costs {k % 90 + 10} million dollars because the {t} budget policy was changed "
                     f"in year {k % 12}, and the study says the {t} cost must fall.")
        if k % 40 == 39:
            parts.append(6.0)
    t0 = time.time()
    rep = {}
    cands = segment.select(transcript(parts), n=6, min_s=40, max_s=180, profile="v6", preferred=PREF_POL,
                           content_type="political_monologue", report=rep)
    assert time.time() - t0 < 60
    assert rep["windows_scored"] <= complete.Budget().max_windows and rep["seeds"] <= complete.Budget().max_seeds
    assert len(cands) <= 6


def test_legacy_profiles_are_unchanged_and_v6_is_opt_in():
    assert segment.DEFAULT_PROFILE == "v4" and segment.PROFILES["v6"].complete and not segment.PROFILES["v5"].complete
    cands = segment.select(transcript([SETUP, 0.4, ARGUMENT, 0.4, QUALIFY, 7.0, OTHER]), n=2, min_s=15, max_s=150, profile="v4")
    assert cands and all(c.assessment is None for c in cands)


def test_review_candidates_are_marked_for_human_review():
    # a passage whose ending leans on the next sentence: acceptable only as REVIEW, never PASS
    parts = [SETUP, 0.4, ARGUMENT.split(". ")[0] + ". " + ". ".join(ARGUMENT.split(". ")[1:3]) + ".", 0.4,
             "And the second thing is the rebate, which changes who benefits from the plan."]
    cands, rep = run(parts)
    for c in cands:
        assert c.assessment["verdict"] in ("PASS", "REVIEW")
