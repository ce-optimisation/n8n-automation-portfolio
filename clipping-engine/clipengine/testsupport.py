"""TEST-ONLY helpers. Nothing in the application imports this module.

stub_transcript_from_timing() builds a word-timed transcript from a sample
generator's exact paragraph timings (words spread proportionally to length). It is
used where the real Whisper model cannot be downloaded, and for evaluating clip
selection independently of transcription errors. It never verifies transcription.
"""
from __future__ import annotations

import json
from pathlib import Path


def stub_transcript_from_timing(timing_path: Path) -> dict:
    data = json.loads(Path(timing_path).read_text(encoding="utf-8"))
    segments = []
    for p in data["paragraphs"]:
        toks = p["text"].split()
        total = sum(len(t) + 1 for t in toks)
        t = p["start"]
        span = p["end"] - p["start"]
        words = []
        for tok in toks:
            d = span * (len(tok) + 1) / total
            words.append({"start": round(t, 3), "end": round(t + d * 0.92, 3), "word": " " + tok, "prob": 0.93})
            t += d
        segments.append({"start": p["start"], "end": p["end"], "text": p["text"], "avg_logprob": -0.2,
                         "no_speech_prob": 0.01, "words": words})
    return {"language": "en", "segments": segments}
