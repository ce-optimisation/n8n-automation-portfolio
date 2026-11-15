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
  para_end          share of clips that end within 0.6 s of a paragraph end
  ends_mid_topic    share of clips cut while the same topic's discussion continues (lower is better)
  starts_mid_topic  share of clips that start part-way through a topic's discussion
  mid_sentence_start share of clips whose first word is mid-sentence (lower-case start)
  multi_topic       share of clips that run across more than one labelled discussion
  procedural_edge   share of clips whose first or last paragraph is housekeeping/hand-over
  dur_min/mean/max  clip durations (v3 has no preferred duration; lengths should follow the content)
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


def evaluate(paras: list[dict], segs: list[dict], profile: str, n: int, min_s=None, max_s=None,
             target=None) -> dict:
    # v1/v2 were designed around a 30–90 s window with a 55 s target; v3 is content-led (15–150 s, no target).
    if min_s is None:
        min_s, max_s, target = (15.0, 150.0, None) if profile in ("v3", "v4") else (30.0, 90.0, 55.0)
    duration = paras[-1]["end"]
    t = time.time()
    report: dict = {}
    clips = segment.select(segs, n=n, min_s=min_s, max_s=max_s, target=target, duration=duration,
                           profile=profile, report=report)
    secs = time.time() - t
    if not clips:
        return {"profile": profile, "clips": 0, "seconds": round(secs, 2), "report": report}
    sub, proc, clean, pstart, pend, term, topics, cont = [], [], [], [], [], [], [], []
    midsent, multi, edgeproc = [], [], []
    starts = [p["start"] for p in paras]
    ends = [p["end"] for p in paras]
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
        pend.append(any(abs(c.end - e) <= 0.6 for e in ends))
        # Did the clip stop (or start) while the same topic's discussion carried straight on?
        dom = max(by_topic, key=by_topic.get) if by_topic else None
        nxt = next((p for p in paras if p["start"] >= c.end - 0.3), None)
        prv = [p for p in paras if p["end"] <= c.start + 0.3]
        cont.append((bool(nxt and dom and nxt["topic"] == dom and nxt["start"] - c.end < 3.0),
                     bool(prv and dom and prv[-1]["topic"] == dom and c.start - prv[-1]["end"] < 3.0)))
        term.append(c.text.rstrip().endswith((".", "?", "!")))
        # Coherence checks that need no interpretation: does the clip begin in the middle of a
        # sentence, does it run across more than one discussion, does it open/close on housekeeping?
        midsent.append(c.text.lstrip()[:1].islower())
        multi.append(len([t for t, o in by_topic.items() if o >= 4.0]) > 1)
        inside = [p for p in paras if overlap(c.start, c.end, p["start"], p["end"]) >= 1.5]
        edgeproc.append(bool(inside) and (inside[0]["label"] == "procedural" or inside[-1]["label"] == "procedural"))
        topics.append(max(by_topic, key=by_topic.get) if by_topic else None)
    k = len(clips)
    return {"profile": profile, "clips": k, "substantive": round(sum(sub) / k, 3),
            "procedural": round(sum(proc) / k, 3), "clean_start": round(sum(clean) / k, 3),
            "para_start": round(sum(pstart) / k, 3), "para_end": round(sum(pend) / k, 3),
            "ends_mid_topic": round(sum(a for a, _ in cont) / k, 3),
            "starts_mid_topic": round(sum(b for _, b in cont) / k, 3),
            "mid_sentence_start": round(sum(midsent) / k, 3), "multi_topic": round(sum(multi) / k, 3),
            "procedural_edge": round(sum(edgeproc) / k, 3), "terminal_end": round(sum(term) / k, 3),
            "dur_mean": round(sum(c.end - c.start for c in clips) / k, 1),
            "dur_min": round(min(c.end - c.start for c in clips), 1),
            "dur_max": round(max(c.end - c.start for c in clips), 1),
            "topic_diversity": round(len({x for x in topics if x}) / k, 3), "seconds": round(secs, 2),
            "windows_scored": report.get("windows_scored"), "note": report.get("note"),
            "clips_detail": [{"start": round(c.start, 1), "end": round(c.end, 1), "score": c.score,
                              "text": c.text[:90]} for c in clips]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("timing", type=Path)
    ap.add_argument("--transcript", type=Path, help="real segments JSON ({segments:[...]}) instead of stub")
    ap.add_argument("--profiles", default="v1,v2,v3,v4")
    ap.add_argument("--clips", type=int, default=6)
    ap.add_argument("--json", type=Path)
    a = ap.parse_args()
    paras = json.loads(a.timing.read_text(encoding="utf-8"))["paragraphs"]
    if a.transcript:
        if a.transcript.suffix == ".gz":
            import gzip
            segs = json.loads(gzip.open(a.transcript, "rt", encoding="utf-8").read())["segments"]
        else:
            segs = json.loads(a.transcript.read_text(encoding="utf-8"))["segments"]
        source = f"real transcript ({a.transcript.name})"
    else:
        segs = stub_transcript_from_timing(a.timing)["segments"]
        source = "TEST-ONLY exact-timing stub transcript"
    results = [evaluate(paras, segs, p, a.clips) for p in a.profiles.split(",")]
    print(f"Transcript: {source}; recording {paras[-1]['end'] / 60:.1f} min; {len(paras)} paragraphs")
    cols = ["profile", "clips", "substantive", "procedural", "clean_start", "para_start", "para_end",
            "ends_mid_topic", "starts_mid_topic", "mid_sentence_start", "multi_topic", "procedural_edge",
            "terminal_end", "topic_diversity", "dur_min", "dur_mean", "dur_max", "seconds"]
    print(" | ".join(cols))
    for r in results:
        print(" | ".join(str(r.get(c, "")) for c in cols))
    if a.json:
        a.json.write_text(json.dumps({"source": source, "results": results}, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
