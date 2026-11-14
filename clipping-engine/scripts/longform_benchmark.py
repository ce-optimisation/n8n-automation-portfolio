"""Long-form processing benchmark and crash-recovery test.

    python scripts/longform_benchmark.py --minutes 60 --out bench/            # REAL faster-whisper
    python scripts/longform_benchmark.py --minutes 60 --out bench/ --stub     # TEST-ONLY stub transcript
    python scripts/longform_benchmark.py --minutes 60 --out bench/ --crash-test

Generates (or reuses with --sample) a labelled long recording, runs the real job
pipeline (transcription → selection → rendering) in a worker subprocess, and reports:
stage timings, real-time factor, peak memory (RSS), peak temporary disk use, clip count,
render times, and selection-quality metrics on the resulting transcript.

--crash-test hard-kills the worker after the first transcription chunk is checkpointed,
restarts it, and verifies it resumes from the checkpoint instead of starting over.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))


def dir_size(p: Path) -> int:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) if p.exists() else 0


def worker(data_dir: Path, sample: Path | None, stats_path: Path) -> int:
    """Run the job pipeline until the queue is empty, sampling memory and work-dir size."""
    from clipengine.config import get_settings
    from clipengine.db import Database
    from clipengine.jobs import JobQueue
    from clipengine.pipeline import Pipeline
    s = get_settings(data_dir=data_dir, llm_provider="none")
    s.ensure_dirs()
    db = Database(s.db_path)
    q = JobQueue(db)
    p = Pipeline(s, db, q)
    peak = {"rss_mb": 0.0, "work_mb": 0.0}
    stop = threading.Event()

    def sample_loop():
        while not stop.wait(1.0):
            try:
                rss = 0.0
                if Path("/proc/self/status").exists():
                    for ln in Path("/proc/self/status").read_text().splitlines():
                        if ln.startswith("VmRSS:"):
                            rss = int(ln.split()[1]) / 1024
                peak["rss_mb"] = max(peak["rss_mb"], rss)
                peak["work_mb"] = max(peak["work_mb"], dir_size(s.work_dir) / 1e6)
            except OSError:
                pass
    threading.Thread(target=sample_loop, daemon=True).start()
    if sample is not None:
        tmp = s.work_dir / "input.mp4"
        shutil.copy(sample, tmp)
        p.import_upload(tmp, sample.name, "Long-form benchmark", {}, "benchmark sample (original synthetic speech)")
    q.recover()
    t = time.time()
    q.drain(timeout=6 * 3600)
    stop.set()
    try:
        import resource
        peak["ru_maxrss_mb"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    except ImportError:
        pass
    peak["drain_s"] = round(time.time() - t, 1)
    stats_path.write_text(json.dumps(peak), encoding="utf-8")
    return 0


def run_worker(data_dir, sample, stats, env, kill_after_chunk=False):
    args = [sys.executable, __file__, "--worker", str(data_dir), "--stats", str(stats)]
    if sample:
        args += ["--sample", str(sample)]
    proc = subprocess.Popen(args, env=env)
    if not kill_after_chunk:
        return proc.wait()
    import sqlite3
    db = data_dir / "clipengine.sqlite3"
    deadline = time.time() + 3 * 3600
    while time.time() < deadline and proc.poll() is None:
        time.sleep(2)
        try:
            n = sqlite3.connect(db).execute("SELECT COUNT(*) FROM transcript_chunks").fetchone()[0]
        except sqlite3.Error:
            n = 0
        if n >= 1:
            proc.kill()   # simulate a crash / power loss mid-transcription
            proc.wait()
            return -9
    return proc.wait()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--worker")
    ap.add_argument("--stats")
    ap.add_argument("--sample", type=Path)
    ap.add_argument("--minutes", type=float, default=60)
    ap.add_argument("--out", type=Path, default=Path("bench"))
    ap.add_argument("--stub", action="store_true")
    ap.add_argument("--crash-test", action="store_true")
    ap.add_argument("--model", default=os.environ.get("WHISPER_MODEL", "base.en"))
    a = ap.parse_args()
    if a.worker:
        return worker(Path(a.worker), a.sample, Path(a.stats))

    from make_longform_sample import make
    from clipengine.testsupport import stub_transcript_from_timing
    a.out.mkdir(parents=True, exist_ok=True)
    sample = a.sample or make(a.out / f"longform_{int(a.minutes)}min.mp4", a.minutes)
    timing = sample.with_suffix(".timing.json")
    data_dir = a.out / "data"
    shutil.rmtree(data_dir, ignore_errors=True)
    env = dict(os.environ, WHISPER_MODEL=a.model, TRANSCRIBE_LANGUAGE="en", LLM_PROVIDER="none",
               PYTHONUNBUFFERED="1")
    mode = f"REAL faster-whisper ({a.model})"
    if a.stub:
        stub = a.out / "stub.json"
        stub.write_text(json.dumps(stub_transcript_from_timing(timing)))
        env.update(CLIPENGINE_TRANSCRIBER="stub", CLIPENGINE_STUB_TRANSCRIPT=str(stub))
        mode = "TEST-ONLY STUB transcript (transcription NOT measured)"
    t0 = time.time()
    crash = None
    if a.crash_test:
        rc = run_worker(data_dir, sample, a.out / "stats_crash.json", env, kill_after_chunk=True)
        import sqlite3
        con = sqlite3.connect(data_dir / "clipengine.sqlite3")
        chunks_before = con.execute("SELECT COUNT(*) FROM transcript_chunks").fetchone()[0]
        crash = {"killed": rc == -9, "chunks_checkpointed_before_kill": chunks_before}
        rc = run_worker(data_dir, None, a.out / "stats.json", env)
    else:
        rc = run_worker(data_dir, sample, a.out / "stats.json", env)
    wall = time.time() - t0
    import sqlite3
    con = sqlite3.connect(data_dir / "clipengine.sqlite3")
    con.row_factory = sqlite3.Row
    rec = con.execute("SELECT * FROM recordings ORDER BY id LIMIT 1").fetchone()
    meta = json.loads(rec["metadata_json"] or "{}")
    jobs = [dict(r) for r in con.execute("SELECT id, kind, status, error, attempts, started_at, finished_at FROM jobs")]
    logs = [r["message"] for r in con.execute("SELECT message FROM job_logs ORDER BY id")]
    clips = [dict(r) for r in con.execute("SELECT id, start, end, status, video_path FROM clips ORDER BY start")]
    renders = [round(j["finished_at"] - j["started_at"], 1) for j in jobs
               if j["kind"] == "render_clip" and j["status"] == "done" and j["finished_at"] and j["started_at"]]
    segs = [{"start": r["start"], "end": r["end"], "text": r["text"], "words": json.loads(r["words_json"])}
            for r in con.execute("SELECT * FROM transcript_segments ORDER BY idx")]
    (a.out / "transcript_segments.json").write_text(json.dumps({"segments": segs}), encoding="utf-8")
    from evaluate_selection import evaluate
    paras = json.loads(timing.read_text(encoding="utf-8"))["paragraphs"]
    quality = evaluate(paras, segs, "v2", 6) if segs else None
    if quality:
        quality.pop("clips_detail", None)
    word_hits = None
    if segs:
        spoken = " ".join(p["text"] for p in paras).lower().split()
        heard = set(" ".join(s["text"] for s in segs).lower().replace(",", " ").replace(".", " ").split())
        word_hits = round(sum(1 for w in spoken if w.strip(",.?") in heard) / max(1, len(spoken)), 3)
    stats = json.loads((a.out / "stats.json").read_text()) if (a.out / "stats.json").exists() else {}
    report = {
        "mode": mode, "recording_minutes": round((rec["duration"] or 0) / 60, 1), "worker_exit": rc,
        "wall_clock_s": round(wall, 1), "processing": meta.get("processing"), "peak": stats,
        "crash_recovery": crash, "resume_logged": any("resuming after" in m for m in logs),
        "audio_reused_after_crash": any("Reusing extracted audio" in m for m in logs),
        "clips": len(clips), "clips_rendered": sum(1 for c in clips if c["video_path"]),
        "render_s_each": renders, "failed_jobs": [j for j in jobs if j["status"] == "failed"],
        "transcript_segments": len(segs), "vocabulary_recall": word_hits, "selection_quality_v2": quality,
        "work_dir_left_mb": round(dir_size(data_dir / "work") / 1e6, 1),
    }
    (a.out / "report.json").write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    print(json.dumps(report, indent=1, default=str))
    ok = rc == 0 and report["clips_rendered"] >= 1 and not report["failed_jobs"]
    if a.crash_test:
        ok = ok and bool(crash and crash["killed"] and crash["chunks_checkpointed_before_kill"] >= 1
                         and report["resume_logged"])
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
