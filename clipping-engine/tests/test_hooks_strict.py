"""Session 14 strict title gate: real failures from the 19 Nov 2026 audit must give NO title; good ones must survive."""
from clipengine import hooks

AWS = ("It's not like Amazon AWS went down. It's not like Cloudflare went down. It's not like these systemic services "
       "that carry 70% of the internet had disruption, and it cascaded through the rest of them. No, these are all "
       "different systems all going down in sequence.")
SPONSOR = ("The armrests are 8 direction adjustable with a larger contact surface to make sure your arms are supported. "
           "Grab your C2 Doro C300 Pro V2 during their Black Friday sale and save an additional 10% using our code.")
TAIWAN = ("Taiwan is not the end state for China, and all the people who think that this fight over Taiwan is the real "
          "fight are missing the point. They believe that the Philippines is their territory.")
FLIGHTS = ("Started last week with Microsoft, the whole suite was offline. And then the next day, there was a nationwide "
           "ground stop authorized by the FAA. Over 20,000 flights were affected. It's the longest ground stop since 9.11.")
FILLER = ("What does that tell you? It means driving is going away. They only make two cars now because they want to "
          "focus on robots.")
GAMEPLAY = "Oh, that's a problem. That's gonna be an issue. Why don't we should have gotten out of the other side? Oh shit."


def title(tx, brand="operator_notes"):
    return hooks.generate(tx, brand)["title"]


def test_negated_clip_never_gets_a_bare_verb_fragment():
    t = title(AWS)
    assert t is None or "went" not in t.lower()


def test_sponsor_read_gets_no_title():
    assert title(SPONSOR) is None


def test_good_complete_claims_survive():
    assert title(TAIWAN) == "Taiwan is not the end state for China"
    assert title(FLIGHTS) == "Over 20,000 flights were affected"


def test_filler_and_profane_gameplay_get_no_title():
    assert title(FILLER) is None or "tell you" not in (title(FILLER) or "")
    assert title(GAMEPLAY, "america_first_monitor") is None


def test_strict_gate_rejects_fragments_directly():
    for frag in ("Eventually I'd like to open up some kind", "a lot of them went to Florida",
                 "go check them out at example.com", "Netanyahu was talking about Iran then"):
        c = hooks.Candidate(text=frag, method="claim", support=frag + ".")
        assert hooks.strict_problems(c, frag, frag), frag


def _claim(text, support=None):
    return hooks.Candidate(text=text, method="claim", support=support or text + ".")


def test_screen_pointing_and_comma_spliced_claims_are_rejected():
    for bad in ("The photo in the top right is insane", "The crinkled glass looks different on every one of these",
                "The mother leaned forward, the father was sleeping"):
        assert hooks.strict_problems(_claim(bad), bad, bad), bad


def test_generic_questions_and_bare_topic_labels_are_rejected():
    q = hooks.Candidate(text="What has really changed in the past 14 years?", method="question", support="x")
    assert hooks.strict_problems(q, q.text, q.text)
    named = hooks.Candidate(text="Why is Iran doubling down?", method="question", support="x")
    assert not any("generic" in p for p in hooks.strict_problems(named, named.text, named.text))
    for m in ("topic", "neutral_topic"):
        t = hooks.Candidate(text="Wall Street executives", method=m, support="x")
        assert any("not a headline" in p for p in hooks.strict_problems(t, t.text, t.text))


def test_regression_on_the_audited_clips_most_get_no_title():
    clips = {
        "Taiwan is not the end state for China, and all the people who think this is the real fight are missing the point.":
            "Taiwan is not the end state for China",
        "Started last week with Microsoft. Over 20,000 flights were affected. It was the longest ground stop since 9.11.":
            "Over 20,000 flights were affected",
        "It's not like Amazon AWS went down. It's not like Cloudflare went down. These are different systems.": None,
    }
    for tx, want in clips.items():
        assert title(tx) == want
