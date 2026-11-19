"""Local semantic reranking of verified title candidates (Session 13). OFF unless TITLE_SEMANTIC=1.

What it does: scores each candidate the deterministic generator has ALREADY verified (every word and number in the clip, no
dropped negation or hedge, brand policy, safety screen) by how well it represents the clip as a whole, and reorders them.
What it cannot do: add a candidate, change a word, or promote a rejected one. Verification therefore stays the only gate.

Embedders are pluggable and free/local:
  * ``TfidfEmbedder`` - no download, always available; a weak but honest topical signal (default when enabled).
  * ``FastEmbedder`` - optional ONNX model through the ``fastembed`` package (e.g. BAAI/bge-small-en-v1.5, roughly 130 MB on disk,
    CPU only). Used only if the package is installed; never downloaded implicitly.
A local LLM (Ollama) may still PROPOSE extra wordings upstream, but they go through the same verifier before reaching this module.
"""
from __future__ import annotations

import math
import re
from collections import Counter


class Unavailable(RuntimeError):
    """The optional embedding backend is not installed."""


_TOK = re.compile(r"[a-z0-9][a-z0-9'\-]*")
_STOP = set("a an the of to in on at for with by from and or but is are was were be been it its this that these those i you he "
            "she they we not no do does did has have had as if so than then there here what which who whom".split())


class TfidfEmbedder:
    """Bag-of-content-words vectors with IDF computed over the clip's own sentences. Deterministic, dependency-free."""

    def __init__(self, corpus: list[str]):
        docs = [self._terms(c) for c in corpus if c.strip()] or [[]]
        df = Counter(t for d in docs for t in set(d))
        n = len(docs)
        self.idf = {t: math.log((1 + n) / (1 + c)) + 1.0 for t, c in df.items()}

    @staticmethod
    def _terms(text: str) -> list[str]:
        out = []
        for t in _TOK.findall(text.lower()):
            if t in _STOP:
                continue
            for suf in ("ing", "ed", "es", "s"):
                if len(t) > len(suf) + 3 and t.endswith(suf):
                    t = t[: -len(suf)]
                    break
            out.append(t)
        return out

    def embed(self, text: str) -> dict[str, float]:
        c = Counter(self._terms(text))
        return {t: n * self.idf.get(t, 1.0) for t, n in c.items()}


class FastEmbedder:
    def __init__(self, model: str = "BAAI/bge-small-en-v1.5"):
        try:
            from fastembed import TextEmbedding
        except ImportError as e:                                   # not installed: never fall back to a download
            raise Unavailable("fastembed is not installed") from e
        self._m = TextEmbedding(model_name=model)

    def embed(self, text: str):
        return list(next(iter(self._m.embed([text]))))


def _cos(a, b) -> float:
    if isinstance(a, dict):
        num = sum(v * b.get(k, 0.0) for k, v in a.items())
        da, db = math.sqrt(sum(v * v for v in a.values())), math.sqrt(sum(v * v for v in b.values()))
    else:
        num = sum(x * y for x, y in zip(a, b))
        da, db = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return num / (da * db) if da and db else 0.0


def rerank(result: dict, transcript: str, embedder=None, *, max_alternatives: int = 3) -> dict:
    """Return ``result`` with accepted candidates reordered by (verified score x topical similarity to the whole clip) and
    up to three alternatives each carrying its supporting passage. Rejected candidates are left exactly as they were."""
    cands = result.get("candidates") or []
    accepted = [c for c in cands if c.get("accepted")]
    if len(accepted) < 2:
        return result
    from . import hooks
    emb = embedder or TfidfEmbedder(hooks.split_sentences(transcript))
    whole = emb.embed(transcript)
    scored = []
    for c in accepted:
        sim = max(0.0, _cos(emb.embed(re.sub(r"\*+", "", c["text"])), whole))
        scored.append((c["score"] * (0.7 + 0.6 * sim), sim, c))
    scored.sort(key=lambda x: -x[0])
    real = [s for s in scored if s[2]["method"] != "neutral_topic"] or scored
    best = real[0][2]
    out = dict(result)
    out["title"], out["method"] = best["text"], best["method"]
    out["status"] = "neutral" if best["method"] in ("neutral_topic", "topic") else "proposed"
    out["alternatives"] = [c["text"] for _, _, c in scored if c is not best][:max_alternatives]
    out["semantic"] = {"embedder": type(emb).__name__,
                       "ranking": [{"text": c["text"], "similarity": round(sim, 3), "blended": round(b, 3)} for b, sim, c in scored]}
    return out
