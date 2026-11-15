"""Selection profile v4 (context boundaries).

Unit tests use small hand-written transcripts. The regression test uses a REAL faster-whisper
(base.en) transcript of a genuine recording — NASA/USGS "Landsat 9 Media Briefing" (31 Aug 2021,
NASA image library id GSFC_20210831_Landsat_9_Briefing) — with hand-annotated discussion units
(tests/fixtures/landsat9_topics.json). The transcript was produced once and stored, so the test
needs no network or model; it checks selection only, not transcription.
"""
import gzip
import json
import sys
from pathlib import Path

from clipengine import segment

FIX = Path(__file__).parent / "fixtures"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))


def seg(start, text, wps=2.6, gap_after=0.3):
    """One transcript segment with evenly spaced word timings."""
    toks = text.split()
    words, t = [], start
    for tok in toks:
        d = 1.0 / wps
        words.append({"start": round(t, 2), "end": round(t + d * 0.9, 2), "word": " " + tok, "prob": 0.93})
        t += d
    return {"start": start, "end": round(t, 2), "text": text, "words": words}, t + gap_after


def transcript(lines):
    out, t = [], 0.0
    for ln in lines:
        gap = 0.3
        if isinstance(ln, tuple):
            ln, gap = ln
        s, t = seg(t, ln, gap_after=gap)
        out.append(s)
    return out


def annotated(lines):
    sents = segment.build_sentences(transcript(lines))
    segment.annotate_boundaries(sents)
    segment.annotate_context(sents)
    return sents


def test_default_profile_is_v4_and_v3_remains_available():
    assert segment.DEFAULT_PROFILE == "v4"
    assert {"v1", "v2", "v3", "v4"} <= set(segment.PROFILES)
    assert segment.PROFILES["v4"].fit_weight == 0.0          # still no preferred duration


def test_dependency_cues():
    s = annotated([
        "The council approved the budget for the new bridge on Tuesday.",
        "Also the report found that costs rose by twelve percent.",
        "And the contractor has asked for more time.",
        "Why does this matter?",
        "Because the bridge carries nine thousand vehicles every day.",
        "Thanks for that question.",
        "As I said earlier the design is not final.",
        "Let me turn to the question of housing in the north ward.",
    ])
    dep = {x.text.split()[0].strip(",."): x for x in s}
    assert dep["The"].dependency == 0.0
    assert dep["Also"].dependency >= 0.25                    # additive: leans on the previous point
    assert dep["And"].dependency >= 0.35                     # dangling conjunction
    assert dep["Why"].dependency >= 0.3                      # follow-up question about "this"
    assert dep["Because"].dependency >= 0.6                  # dangling + answers the question
    assert dep["Thanks"].edge == "turn" and dep["Thanks"].dependency >= 0.3
    assert dep["As"].dependency >= 0.2                       # back-reference
    assert dep["Let"].opener and dep["Let"].dependency == 0.0


def test_question_stays_with_its_answer_and_turns_are_strong_breaks():
    s = annotated([
        "Our next question is for the director.",
        "Is it correct that the launch has been delayed by a week?",
        "Yes the launch moved because of a shortage of delivery trucks.",
        ("We expect to launch on the twenty third.", 0.4),
        "Thank you director.",
        "Now I will turn it over to our project scientist.",
        "Great thanks.",
        "The satellite carries two instruments that measure reflected light and heat.",
    ])
    def one(prefix):
        return next(x for x in s if x.text.startswith(prefix))
    assert one("Is it correct").boundary_after <= 0.3        # never split a question from its answer
    assert one("We expect").boundary_after >= 0.9            # a new speaker turn follows
    assert one("Now I will").edge == "handoff" and one("Now I will").boundary_after >= 0.9
    assert one("Our next question").edge == "handoff"


def test_never_starts_mid_sentence():
    # Whisper sometimes splits one sentence across a long gap ("For … more than 50 years").
    lines = [("For", 9.0)] + ["more than fifty years the agency has studied the planet from space and "
                              "measured twelve percent growth in forests because of careful policy."] + [
        "The programme costs four million pounds a year and the evidence shows the plan works.",
        "Researchers use the data to study crops, water and cities across the whole country.",
        "The government should fund it because the research has clear benefits for farmers.",
        ("That is the main argument for the budget.", 3.0),
        "A separate matter is the law on recycling which the council must review next year.",
        "The study found that the recycling rate rose to forty one percent after the change.",
        "Therefore the policy should continue and the plan is to expand it to every ward.",
    ]
    segs = transcript(lines)
    clips = segment.select(segs, n=3, min_s=8, max_s=60, duration=segs[-1]["end"], profile="v4")
    assert clips
    for c in clips:
        assert not c.text.lstrip()[0].islower(), c.text[:60]
    sents = segment.build_sentences(segs)
    assert not sents[1].terminal or sents[1].text[0].islower()   # the fragment really is mid-sentence


def test_clip_does_not_end_on_a_handover_or_open_on_thanks():
    lines = [
        "Good morning everyone and welcome.",
        ("I am your host today.", 1.2),
        "The river bridge has lost thirty percent of its strength according to the engineers report.",
        "The plan is to rebuild it at a cost of forty two million pounds because repairs would not last.",
        "The evidence shows that nine thousand vehicles cross it every day so the policy matters.",
        ("That is why the council should vote for the proposal this year.", 0.5),
        "Now I would like to introduce our next speaker.",
        ("Over to you.", 1.5),
        "Thank you chair.",
        "Affordable housing is the second issue and rents rose by twelve percent last year.",
        "The plan is to build four hundred homes on the old depot site for sixty million pounds.",
        "The research shows demand will keep rising because the population is growing quickly.",
        "Therefore the government must act and the council should approve the budget.",
    ]
    segs = transcript(lines)
    clips = segment.select(segs, n=2, min_s=10, max_s=60, duration=segs[-1]["end"], profile="v4")
    assert clips
    for c in clips:
        low = c.text.lower()
        assert not low.startswith(("thank you", "good morning", "i am your host"))
        assert "introduce our next speaker" not in low and "over to you" not in low
        assert c.text.startswith(("The river bridge", "Affordable housing"))   # the opening of a speaker's point


def test_refine_moves_start_back_to_the_opening_of_the_point():
    sents = annotated([
        ("Thank you chair.", 1.5),
        "Let me turn to flood defences on the east bank of the river.",
        "The east bank flooded three times in the last five years.",
        "Also the wall is one metre too low according to the engineers study.",
        "Because of that the plan is to raise it at a cost of eighteen million pounds.",
        ("The council should approve the budget this year.", 2.8),
        "Good evening and welcome back.",
    ])
    prof = segment.PROFILES["v4"]
    i = next(k for k, x in enumerate(sents) if x.text.startswith("Also"))
    j = next(k for k, x in enumerate(sents) if x.text.startswith("The council"))
    score, reasons = segment.score_window(sents, i, j, None, prof)
    c = segment.Candidate(i, j, sents[i].start, sents[j].end, "", [], score, reasons)
    assert segment.refine_context(sents, [c], prof, max_s=120, target=None) == 1
    assert c.text.startswith("Let me turn to flood defences")
    assert c.text.endswith("this year.")


def test_real_speech_regression_landsat_briefing():
    """REAL transcript (stored). v4 must begin every clip at a sentence opening on substantive
    material, never on housekeeping, and mostly at an annotated paragraph opening."""
    import evaluate_selection as ev
    paras = json.loads((FIX / "landsat9_topics.json").read_text(encoding="utf-8"))["paragraphs"]
    segs = json.loads(gzip.open(FIX / "landsat9_whisper_base_en.json.gz", "rt", encoding="utf-8").read())["segments"]
    v3 = ev.evaluate(paras, segs, "v3", 6)
    v4 = ev.evaluate(paras, segs, "v4", 6)
    assert v4["clips"] == 6
    assert v4["mid_sentence_start"] == 0.0 and v3["mid_sentence_start"] > 0   # defect present in v3
    assert v4["procedural_edge"] == 0.0 and v4["procedural"] <= 0.02
    assert v4["para_start"] >= 0.8 > v3["para_start"]
    assert v4["para_end"] >= 0.8
    assert v4["multi_topic"] <= v3["multi_topic"]
    assert v4["substantive"] >= 0.93
    assert 15 <= v4["dur_min"] and v4["dur_max"] <= 150      # content-led limits respected
