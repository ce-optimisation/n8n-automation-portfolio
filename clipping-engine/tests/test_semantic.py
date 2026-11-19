"""Session 13: semantic reranking is off by default and can only reorder candidates the verifier already accepted."""
import copy

import pytest

from clipengine import hooks, semantic

CLIP = ("The grid operator said demand will outstrip supply by Friday. Grid operators warned that demand will outstrip "
        "supply in twelve states. Nobody has said the lights will fail. Why would the grid operator wait until Friday?")


class FakeEmbedder:
    """Rewards a chosen word so the ranking is controllable."""

    def __init__(self, favour):
        self.favour = favour

    def embed(self, text):
        return [1.0 if self.favour in text.lower() else 0.1, 0.5]


def test_rerank_never_adds_or_promotes_a_rejected_candidate():
    r = hooks.generate(CLIP, "afterlive")
    before = copy.deepcopy(r)
    out = semantic.rerank(r, CLIP, FakeEmbedder("friday"))
    assert {c["text"] for c in out["candidates"]} == {c["text"] for c in before["candidates"]}      # untouched list
    accepted = {c["text"] for c in before["candidates"] if c["accepted"]}
    assert out["title"] in accepted
    assert set(out["alternatives"]) <= accepted and len(out["alternatives"]) <= 3


def test_embedder_can_change_the_order_but_not_the_wording():
    r = hooks.generate(CLIP, "afterlive")
    acc = [c for c in r["candidates"] if c["accepted"]]
    assert len(acc) >= 2
    pick = acc[-1]["text"].split()[-1].lower().strip("?.,")
    out = semantic.rerank(r, CLIP, FakeEmbedder(pick))
    assert out["semantic"]["embedder"] == "FakeEmbedder"
    assert all(entry["text"] in {c["text"] for c in acc} for entry in out["semantic"]["ranking"])


def test_default_tfidf_embedder_runs_offline_and_is_deterministic():
    r = hooks.generate(CLIP, "afterlive")
    a = semantic.rerank(copy.deepcopy(r), CLIP)
    b = semantic.rerank(copy.deepcopy(r), CLIP)
    assert a["semantic"] == b["semantic"] and a["semantic"]["embedder"] == "TfidfEmbedder"


def test_single_candidate_or_nothing_accepted_is_returned_unchanged():
    r = {"candidates": [{"text": "x", "accepted": False, "score": 1, "method": "claim"}], "title": None}
    assert semantic.rerank(r, CLIP) is r


def test_optional_model_backend_never_downloads_silently():
    try:
        import fastembed  # noqa: F401
        pytest.skip("fastembed installed here")
    except ImportError:
        with pytest.raises(semantic.Unavailable):
            semantic.FastEmbedder()
