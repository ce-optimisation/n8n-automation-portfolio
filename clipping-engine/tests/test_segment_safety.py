from clipengine import safety, segment


def seg(start, text, prob=0.95, step=0.4):
    words, t = [], start
    for tok in text.split():
        words.append({"start": round(t, 2), "end": round(t + step * 0.9, 2), "word": " " + tok, "prob": prob})
        t += step
    return {"start": start, "end": t, "text": text, "words": words}


def long_transcript():
    paras = [
        "Welcome everyone. Today we look at the housing budget and what it means for families.",
        "The government plans to spend four billion on new homes because rents rose twelve percent last year.",
        "Why does this matter? Because the evidence shows supply is the main driver. Research from three universities agrees.",
        "And then there was a lot of um talk and uh some more um talk about nothing in particular really.",
        "Critics argue the plan costs too much. They claim the money should go to transport instead.",
        "The vote on the policy is scheduled for March. Every member must decide by then.",
        "Thank you all for coming tonight.",
    ]
    out, t = [], 0.0
    for p in paras * 3:
        s = seg(t, p)
        out.append(s)
        t = s["end"] + 1.0
    return out


def test_sentences_split_on_punctuation_and_gaps():
    sents = segment.build_sentences([seg(0, "Hello there. How are you? Fine"), seg(20, "Later sentence.")])
    assert [s.text for s in sents] == ["Hello there.", "How are you?", "Fine", "Later sentence."]
    assert sents[1].question and not sents[2].terminal


def test_candidates_are_bounded_clean_and_non_overlapping():
    segs = long_transcript()
    cands = segment.select(segs, n=4, min_s=15, max_s=40, target=25, duration=segs[-1]["end"])
    assert 1 <= len(cands) <= 4
    word_starts = {w["start"] for s in segs for w in s["words"]}
    word_ends = {w["end"] for s in segs for w in s["words"]}
    for c in cands:
        assert 15 <= c.duration <= 40
        assert c.start in word_starts and c.end in word_ends  # real timestamps only
        assert c.text.rstrip().endswith((".", "?", "!"))
    for a, b in zip(cands, cands[1:]):
        assert a.end <= b.start


def test_dangling_start_penalised():
    sents = segment.build_sentences([seg(0, "The plan is fair. And it costs a great deal of money overall.")])
    clean, _ = segment.score_window(sents, 0, 1, 5)
    dangling, reasons = segment.score_window(sents, 1, 1, 5)
    assert dangling < clean and any("mid-thought" in r for r in reasons)


def test_reranker_cannot_invent_candidates_and_bad_output_falls_back():
    segs = long_transcript()
    base = segment.select(segs, n=3, min_s=15, max_s=40, target=25)
    boom = segment.select(segs, n=3, min_s=15, max_s=40, target=25,
                          reranker=lambda pool, n: (_ for _ in ()).throw(ValueError("bad json")))
    assert [(c.start, c.end) for c in boom] == [(c.start, c.end) for c in base]
    picked = segment.select(segs, n=2, min_s=15, max_s=40, target=25, reranker=lambda pool, n: [pool[-1]])
    assert any(c.method == "llm+heuristic" for c in picked)


def test_context_before_and_after():
    sents = segment.build_sentences(long_transcript())
    b, a = segment.context_for(sents, sents[3].start, sents[5].end, 10)
    assert sents[2].text in b and sents[6].text in a


def test_short_recording_bounds_relaxed():
    mn, mx, tg = segment.effective_bounds(60, 30, 90, 55)
    assert mn < 30 and mx <= 60 and mn < tg < mx


def test_safety_flags_and_clean_text(tmp_path):
    assert safety.screen("The council approved the bridge budget.") == []
    flags = safety.screen("They are vermin and should be exterminated, a race war is coming.")
    cats = {f["category"] for f in flags}
    assert {"dehumanisation", "extremist_terminology", "violence_advocacy"} <= cats
    (tmp_path / "safety_terms.txt").write_text("# comment\ncustom watch phrase\n")
    assert safety.screen("a custom watch phrase here", tmp_path)[0]["category"] == "owner_watchlist"


def test_low_confidence_words():
    low = safety.low_confidence_words([{"word": " ok", "start": 0, "prob": 0.9},
                                       {"word": " mumble", "start": 1, "prob": 0.2}])
    assert [w["word"] for w in low] == ["mumble"]
