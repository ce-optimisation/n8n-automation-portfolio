"""TEST-ONLY: make an exact-timing stub transcript look more like real Whisper output.

    python scripts/perturb_transcript.py SAMPLE.timing.json out_segments.json [--seed 3]

Real speech recognition does not give tidy paragraph pauses: word ends are stretched into the
following silence, timings jitter, and neighbouring segments are merged. Selection that only
works on exact timings is over-fitted, so scripts/evaluate_selection.py is also run on this
perturbed transcript. It is NOT real transcription and is labelled as such wherever it is used.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from clipengine.testsupport import stub_transcript_from_timing  # noqa: E402


def perturb(segments: list[dict], seed: int = 3) -> list[dict]:
    rng = random.Random(seed)
    words = [dict(w) for s in segments for w in (s.get("words") or [])]
    for k, w in enumerate(words):
        nxt = words[k + 1]["start"] if k + 1 < len(words) else w["end"] + 1.0
        gap = max(0.0, nxt - w["end"])
        # stretch the word end into most of the following silence (Whisper does this constantly)
        w["end"] = round(w["end"] + gap * rng.uniform(0.55, 0.95), 2)
        jitter = rng.uniform(-0.06, 0.06)
        w["start"] = round(max(0.0, w["start"] + jitter), 2)
        w["end"] = round(max(w["start"] + 0.04, min(w["end"], nxt - 0.01)), 2)
        w["prob"] = round(min(1.0, max(0.2, w.get("prob", 0.9) - rng.random() * 0.25)), 2)
    out, cur = [], []
    limit = rng.randint(18, 42)
    for w in words:
        cur.append(w)
        if len(cur) >= limit:          # segments cut by length, not by meaning
            out.append({"start": cur[0]["start"], "end": cur[-1]["end"],
                        "text": "".join(x["word"] for x in cur).strip(), "words": cur})
            cur, limit = [], rng.randint(18, 42)
    if cur:
        out.append({"start": cur[0]["start"], "end": cur[-1]["end"],
                    "text": "".join(x["word"] for x in cur).strip(), "words": cur})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("timing", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--seed", type=int, default=3)
    a = ap.parse_args()
    segs = perturb(stub_transcript_from_timing(a.timing)["segments"], a.seed)
    a.out.write_text(json.dumps({"segments": segs, "note": "TEST-ONLY perturbed stub transcript"}), encoding="utf-8")
    print(f"wrote {len(segs)} perturbed segments to {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
