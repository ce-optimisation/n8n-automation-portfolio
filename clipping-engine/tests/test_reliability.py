"""Chunked/checkpointed transcription, crash resume, chunk planning and selection quality.

Uses the TEST-ONLY stub transcriber; real Whisper long-form runs are done by
scripts/longform_benchmark.py in CI (labelled there as REAL)."""
import json
import shutil
import subprocess
import sys
import wave

import numpy as np
import pytest

from clipengine import transcribe
from clipengine.db import Database
from clipengine.jobs import JobQueue
from clipengine.pipeline import Pipeline
from conftest import needs_ffmpeg

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent / "scripts"))


def write_wav(path, seconds_pattern):
    """seconds_pattern: list of (seconds, amplitude) blocks."""
    parts = []
    for secs, amp in seconds_pattern:
        t = np.arange(int(secs * 16000)) / 16000
        parts.append((amp * np.sin(2 * np.pi * 220 * t)).astype(np.float32))
    x = (np.concatenate(parts) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(x.tobytes())


def test_plan_chunks_cuts_at_quiet_point(tmp_path):
    f = tmp_path / "a.wav"
    write_wav(f, [(57.0, 0.5), (1.0, 0.0), (62.0, 0.5)])     # silence at 57–58 s
    chunks = transcribe.plan_chunks(f, chunk_seconds=60, search_seconds=10)
    assert len(chunks) == 2 and 57.0 <= chunks[0][1] <= 58.0
    assert chunks[0][0] == 0 and abs(chunks[-1][1] - 120.0) < 0.01
    x = transcribe.read_wav_range(f, 10, 12)
    assert len(x) == 32000
    assert transcribe.plan_chunks(f, chunk_seconds=1200) == [(0.0, 120.0)]


@needs_ffmpeg
def test_chunked_transcription_resumes_after_crash(stub_settings, sample_video, monkeypatch):
    stub_settings.transcribe_chunk_seconds = 30             # 90 s sample → 3+ chunks (cut at pauses)
    db = Database(stub_settings.db_path)
    q = JobQueue(db)
    p = Pipeline(stub_settings, db, q)
    tmp = stub_settings.work_dir / "in.mp4"
    shutil.copy(sample_video, tmp)
    rid, _, _ = p.import_upload(tmp, "s.mp4", "S", {}, None)
    calls = []
    orig = transcribe.StubTranscriber.transcribe_range

    def crashing(self, audio, a, b, *args, **kw):
        calls.append((a, b))
        if len(calls) == 2:
            raise RuntimeError("simulated crash during chunk 2")
        return orig(self, audio, a, b, *args, **kw)
    monkeypatch.setattr(transcribe.StubTranscriber, "transcribe_range", crashing)
    q.run_one()
    assert db.one("SELECT COUNT(*) n FROM transcript_chunks WHERE recording_id=?", (rid,))["n"] == 1
    assert list(stub_settings.work_dir.glob("rec*_audio.wav")), "audio kept for resume"
    monkeypatch.setattr(transcribe.StubTranscriber, "transcribe_range", orig)
    job = db.one("SELECT id FROM jobs WHERE kind='process_recording'")["id"]
    q.retry(job)
    q.drain(600)
    logs = " ".join(r["message"] for r in db.query("SELECT message FROM job_logs"))
    assert "resuming after 1 completed chunk" in logs and "Reusing extracted audio" in logs
    segs = db.segments(rid)
    fixture = json.loads(open(stub_settings and __import__("os").environ["CLIPENGINE_STUB_TRANSCRIPT"]).read())
    assert [s["text"] for s in segs] == [s["text"] for s in fixture["segments"]]
    assert db.one("SELECT COUNT(*) n FROM transcript_chunks")["n"] == 0
    assert not list(stub_settings.work_dir.glob("rec*_audio.wav"))
    stats = json.loads(db.recording(rid)["metadata_json"])["processing"]
    assert stats["chunks"] >= 3 and "selection_s" in stats


@needs_ffmpeg
def test_selection_v2_beats_v1_on_labelled_long_sample(tmp_path):
    from make_longform_sample import make
    from evaluate_selection import evaluate
    from clipengine.testsupport import stub_transcript_from_timing
    out = make(tmp_path / "long.mp4", minutes=12, seed=3)
    timing = out.with_suffix(".timing.json")
    paras = json.loads(timing.read_text())["paragraphs"]
    segs = stub_transcript_from_timing(timing)["segments"]
    v1, v2 = evaluate(paras, segs, "v1", 6), evaluate(paras, segs, "v2", 6)
    assert 4 <= v2["clips"] <= 8
    assert v2["substantive"] >= v1["substantive"] and v2["procedural"] <= v1["procedural"]
    assert v2["procedural"] <= 0.1 and v2["clean_start"] == 1.0 and v2["terminal_end"] == 1.0


def test_selection_does_not_pad_with_weak_clips():
    from clipengine import segment

    def seg(t, text):
        words, x = [], t
        for tok in text.split():
            words.append({"start": x, "end": x + 0.35, "word": " " + tok, "prob": 0.95})
            x += 0.4
        return {"start": t, "end": x, "text": text, "words": words}
    strong = "The plan costs forty million pounds because the bridge supports lost thirty percent of their strength."
    weak = "Thank you. Thank you very much. Welcome everyone, can everyone hear me? Thank you."
    segs, t = [], 0.0
    for i in range(6):
        s1 = seg(t, strong if i == 0 else weak)
        segs.append(s1)
        t = s1["end"] + 1.0
    report = {}
    got = segment.select(segs, n=6, min_s=4, max_s=30, target=8, report=report)
    assert len(got) < 6 and report.get("note")
