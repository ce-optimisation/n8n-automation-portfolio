"""Reproducible clip-selection evaluation against a labelled recording.

    python scripts/evaluate_selection.py SAMPLE.timing.json [--transcript real_segments.json]
                                         [--profiles v1,v2] [--clips 6] [--json out.json]

Ground truth comes from make_longform_sample.py labels (substantive/procedural, topic).
By default the transcript is the TEST-ONLY exact-timing stub, which isolates selection
quality from transcription errors; pass --transcript with real Whisper segments
(e.g. exported by longform_benchmark.py) to evaluate on genuine transcription.

Metrics per profile:
  clips             number selected
  substantive       mean share of each clip's duration inside substantive paragraphs
  procedural        mean share inside procedural paragraphs (lower is better)
  clean_start       share of clips whose first word is not a dangling conjunction
  para_start        share of clips that begin within 0.6 s of a paragraph start
  terminal_end      share of clips ending on . ? !
  topic_diversity   distinct dominant topics / clips
  seconds           selection runtime
NOTE: the sample text is template-generated, and the heuristic uses similar cues, so
absolute scores are optimistic; use the comparison between profiles, not the levels.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from clipengine import segment  # noqa: E402
from clipengine.testsupport import stub_transcript_from_timing  # noqa: E402


def overlap(a0, a1, b0, b1) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def evaluate(paras: list[dict], segs: list[dict], profile: str, n: int, min_s=30.0, max_s=90.0,
             target=55.0) -> dict:
    duration = paras[-1]["end"]
    t = time.time()
    report: dict = {}
    clips = segment.select(segs, n=n, min_s=min_s, max_s=max_s, target=target, duration=duration,
                           profile=profile, report=report)
    secs = time.time() - t
    if not clips:
        return {"profile": profile, "clips": 0, "seconds": round(secs, 2), "report": report}
    sub, proc, clean, pstart, term, topics = [], [], [], [], [], []
    starts = [p["start"] for p in paras]
    for c in clips:
        dur = c.end - c.start
        by_label = {"substantive": 0.0, "procedural": 0.0}
        by_topic: dict = {}
        for p in paras:
            o = overlap(c.start, c.end, p["start"], p["end"])
            if o:
                by_label[p["label"]] += o
                if p["topic"]:
                    by_topic[p["topic"]] = by_topic.get(p["topic"], 0) + o
        sub.append(by_label["substantive"] / dur)
        proc.append(by_label["procedural"] / dur)
        first = c.words[0]["word"].strip().lower().strip(",.") if c.words else ""
        clean.append(first not in segment.DANGLING_STARTS)
        pstart.append(any(abs(c.start - s) <= 0.6 for s in starts))
        term.append(c.text.rstrip().endswith((".", "?", "!")))
        topics.append(max(by_topic, key=by_topic.get) if by_topic else None)
    k = len(clips)
    return {"profile": profile, "clips": k, "substantive": round(sum(sub) / k, 3),
            "procedural": round(sum(proc) / k, 3), "clean_start": round(sum(clean) / k, 3),
            "para_start": round(sum(pstart) / k, 3), "terminal_end": round(sum(term) / k, 3),
            "topic_diversity": round(len({x for x in topics if x}) / k, 3), "seconds": round(secs, 2),
            "windows_scored": report.get("windows_scored"), "note": report.get("note"),
            "clips_detail": [{"start": round(c.start, 1), "end": round(c.end, 1), "score": c.score,
                              "text": c.text[:90]} for c in clips]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("timing", type=Path)
    ap.add_argument("--transcript", type=Path, help="real segments JSON ({segments:[...]}) instead of stub")
    ap.add_argument("--profiles", default="v1,v2")
    ap.add_argument("--clips", type=int, default=6)
    ap.add_argument("--json", type=Path)
    a = ap.parse_args()
    paras = json.loads(a.timing.read_text(encoding="utf-8"))["paragraphs"]
    if a.transcript:
        segs = json.loads(a.transcript.read_text(encoding="utf-8"))["segments"]
        source = f"real transcript ({a.transcript.name})"
    else:
        segs = stub_transcript_from_timing(a.timing)["segments"]
        source = "TEST-ONLY exact-timing stub transcript"
    results = [evaluate(paras, segs, p, a.clips) for p in a.profiles.split(",")]
    print(f"Transcript: {source}; recording {paras[-1]['end'] / 60:.1f} min; {len(paras)} paragraphs")
    cols = ["profile", "clips", "substantive", "procedural", "clean_start", "para_start", "terminal_end",
            "topic_diversity", "seconds"]
    print(" | ".join(cols))
    for r in results:
        print(" | ".join(str(r.get(c, "")) for c in cols))
    if a.json:
        a.json.write_text(json.dumps({"source": source, "results": results}, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
