"""Job handlers that connect ingestion, transcription, selection and rendering."""
from __future__ import annotations

import json
import logging
import shutil
import time
from dataclasses import replace
from pathlib import Path

from .config import Settings
from .db import Database
from .jobs import JobContext, JobQueue, PermanentError
from . import ingest, render, safety, segment
from .transcribe import get_transcriber

log = logging.getLogger("clipengine.pipeline")

OVERRIDABLE = {"clips_per_recording": int, "clip_min_seconds": float, "clip_max_seconds": float,
               "clip_target_seconds": float, "language": str, "llm_provider": str}


def effective_settings(settings: Settings, overrides: dict | None) -> Settings:
    s = replace(settings)
    for k, v in (overrides or {}).items():
        if k in OVERRIDABLE and v not in (None, ""):
            try:
                setattr(s, k, OVERRIDABLE[k](v))
            except (TypeError, ValueError):
                pass
    return s


def all_words(segments: list[dict]) -> list[dict]:
    return segment._flatten_words(segments)


def snap_bounds(words: list[dict], start: float, end: float) -> tuple[float, float]:
    """Move start/end so they never fall inside a word, then add a little breathing room."""
    if not words:
        return start, end
    for w in words:
        if w["start"] < start < w["end"]:
            start = w["start"]
            break
    for w in words:
        if w["start"] < end < w["end"]:
            end = w["end"]
            break
    prev_end = max((w["end"] for w in words if w["end"] <= start + 1e-3), default=0.0)
    next_start = min((w["start"] for w in words if w["start"] >= end - 1e-3), default=end + 10)
    start = max(prev_end, start - 0.2, 0.0)
    end = min(next_start, end + 0.4)
    return round(start, 3), round(end, 3)


def words_in(words: list[dict], start: float, end: float) -> list[dict]:
    return [w for w in words if w["start"] >= start - 0.05 and w["end"] <= end + 0.05]


def describe(rec, start: float, end: float, text: str, flags: list[dict], low_conf: list[dict],
             summary: str | None) -> str:
    """Neutral, factual description. Quotation is clearly separated from interpretation."""
    words = text.split()
    opening = " ".join(words[:28]) + ("…" if len(words) > 28 else "")
    src = rec["source_url"] or f"local file: {rec['original_filename']}"
    parts = [f"Excerpt ({render.mmss(start)}–{render.mmss(end)}) from \"{rec['title']}\".",
             f"Direct quotation (opening): \"{opening}\""]
    if summary and not flags:
        parts.append(f"Automated summary (machine interpretation, verify before use): {summary}")
    if flags:
        parts.append("Editorial note: this excerpt contains language flagged for review. Shared for reporting "
                     "or analysis only, with full context; it does not endorse the views expressed.")
    if low_conf:
        parts.append(f"Transcription note: {len(low_conf)} word(s) were transcribed with low confidence.")
    parts.append(f"Source: {src}")
    return "\n".join(parts)


class Pipeline:
    def __init__(self, settings: Settings, db: Database, queue: JobQueue):
        self.settings = settings
        self.db = db
        self.queue = queue
        queue.register("download_url", lambda ctx: ingest.handle_download_url(settings, db, queue, ctx))
        queue.register("process_recording", self.process_recording)
        queue.register("process_recording:failed", self._recording_failed)
        queue.register("render_clip", self.render_clip)
        queue.register("render_clip:failed", self._render_failed)

    # ---- recording -------------------------------------------------------
    def _recording_failed(self, ctx: JobContext) -> None:
        rid = ctx.job["recording_id"]
        job = self.db.one("SELECT error FROM jobs WHERE id = ?", (ctx.id,))
        self.db.update("recordings", rid, status="failed", error=job["error"] if job else "failed")

    def transcribe_recording(self, ctx: JobContext, rec, s: Settings) -> None:
        self.db.update("recordings", rec["id"], status="transcribing", stage="transcribing", error=None)
        wav = self.settings.work_dir / f"rec{rec['id']}_job{ctx.id}.wav"
        try:
            ctx.progress(0.02, "Extracting audio", force=True)
            from . import media
            media.extract_audio(Path(rec["file_path"]), wav, duration=rec["duration"],
                                on_progress=lambda f: ctx.progress(0.02 + 0.08 * f, "Extracting audio"))
            transcriber = get_transcriber(s)
            ctx.log(f"Transcribing with {transcriber.model_label}")
            ctx.progress(0.1, f"Loading model ({transcriber.model_label})", force=True)
            segs, lang = transcriber.transcribe(
                wav, rec["duration"], on_progress=lambda f: ctx.progress(0.1 + 0.75 * f, "Transcribing"))
        finally:
            wav.unlink(missing_ok=True)  # disposable intermediate
        if not segs:
            raise PermanentError("No speech was detected in this recording.")
        self.db.replace_transcript(rec["id"], segs)
        self.db.update("recordings", rec["id"], language=lang, whisper_model=transcriber.model_label,
                       status="transcribed", stage="transcribed")
        ctx.log(f"Transcript stored: {len(segs)} segments, language={lang}")

    def make_reranker(self, s: Settings, ctx: JobContext | None = None):
        if s.llm_provider == "none":
            return None
        if segment.ollama_available(s.ollama_url):
            if ctx:
                ctx.log(f"Local LLM available (Ollama, {s.ollama_model}); used for re-ranking only")
            return segment.make_ollama_reranker(s.ollama_url, s.ollama_model)
        if s.llm_provider == "ollama" and ctx:
            ctx.log("LLM_PROVIDER=ollama but Ollama is not reachable; using deterministic selection", "WARNING")
        return None

    def process_recording(self, ctx: JobContext) -> None:
        rid = ctx.job["recording_id"]
        rec = self.db.recording(rid)
        if not rec:
            raise PermanentError(f"Recording {rid} no longer exists")
        overrides = {**json.loads(rec["settings_json"] or "{}"), **(ctx.payload.get("settings") or {})}
        s = effective_settings(self.settings, overrides)
        segs = self.db.segments(rid)
        if not segs or ctx.payload.get("retranscribe"):
            self.transcribe_recording(ctx, rec, s)
            segs = self.db.segments(rid)
        else:
            ctx.log("Transcript already exists; skipping transcription (resume)")
        if ctx.cancelled():
            return
        existing = self.db.query("SELECT id, status FROM clips WHERE recording_id = ?", (rid,))
        if existing and not ctx.payload.get("reselect"):
            ctx.log(f"{len(existing)} clips already exist; not regenerating (no duplicates)")
            for c in existing:
                if c["status"] in ("pending_render", "failed"):
                    self.enqueue_render(c["id"])
            self.db.update("recordings", rid, status="clips_queued", stage="rendering")
            return
        if existing and ctx.payload.get("reselect"):
            # Only unreviewed candidates are replaced; reviewed clips are kept.
            for c in existing:
                if c["status"] not in ("approved", "rejected"):
                    self.delete_clip_files(c["id"])
                    self.db.execute("DELETE FROM clips WHERE id = ?", (c["id"],))
        self.db.update("recordings", rid, status="analysing", stage="selecting")
        ctx.progress(0.88, "Selecting candidate excerpts", force=True)
        cands = segment.select(segs, n=s.clips_per_recording, min_s=s.clip_min_seconds,
                               max_s=s.clip_max_seconds, target=s.clip_target_seconds,
                               duration=rec["duration"], reranker=self.make_reranker(s, ctx))
        if not cands:
            raise PermanentError("No suitable self-contained passages were found (transcript too short or "
                                 "no complete sentences).")
        sents = segment.build_sentences(segs)
        words = all_words(segs)
        next_idx = (self.db.one("SELECT MAX(idx) m FROM clips WHERE recording_id = ?", (rid,))["m"] or 0) + 1
        for k, c in enumerate(cands):
            cid = self.create_clip(rec, next_idx + k, c.start, c.end, words, sents, s, score=c.score,
                                   reasons=c.reasons, method=c.method, summary=c.summary)
            self.enqueue_render(cid)
        self.db.update("recordings", rid, status="clips_queued", stage="rendering")
        ctx.log(f"Selected {len(cands)} candidate excerpts; render jobs queued")

    def create_clip(self, rec, idx, start, end, words, sents, s: Settings, *, score=None, reasons=None,
                    method="heuristic", summary=None) -> int:
        start, end = snap_bounds(words, start, end)
        fields = self.clip_text_fields(rec, start, end, words, sents, s, summary)
        return self.db.insert("clips", recording_id=rec["id"], idx=idx, start=start, end=end,
                              status="pending_render", score=score, score_reasons=json.dumps(reasons or []),
                              selection_method=method, machine_summary=summary, **fields)

    def clip_text_fields(self, rec, start, end, words, sents, s: Settings, summary=None) -> dict:
        ws = words_in(words, start, end)
        text = "".join(w["word"] for w in ws).strip()
        before, after = segment.context_for(sents, start, end, s.context_seconds)
        flags = safety.screen(" ".join([text]), self.settings.data_dir)
        low = safety.low_confidence_words(ws)
        conf = sum(w.get("prob", 1.0) for w in ws) / len(ws) if ws else 0.0
        return {
            "transcript_text": text, "quote": text, "context_before": before, "context_after": after,
            "safety_flags": json.dumps(flags), "safety_status": "flagged" if flags else "clear",
            "low_conf_words": json.dumps(low), "confidence": round(conf, 3),
            "description": describe(rec, start, end, text, flags, low, summary),
        }

    def enqueue_render(self, clip_id: int) -> int:
        clip = self.db.clip(clip_id)
        self.db.update("clips", clip_id, status="pending_render", error=None)
        return self.queue.enqueue("render_clip", recording_id=clip["recording_id"], clip_id=clip_id,
                                  dedupe_key=f"render:{clip_id}", payload={"rev": clip["render_rev"]})

    # ---- clip ------------------------------------------------------------
    def clip_dir(self, clip, rev: int | None = None) -> Path:
        rev = clip["render_rev"] if rev is None else rev
        return self.settings.clips_dir / f"rec{clip['recording_id']:05d}" / f"clip{clip['id']:05d}_r{rev}"

    def delete_clip_files(self, clip_id: int) -> None:
        clip = self.db.clip(clip_id)
        if clip:
            base = self.settings.clips_dir / f"rec{clip['recording_id']:05d}"
            for d in base.glob(f"clip{clip['id']:05d}_r*"):
                shutil.rmtree(d, ignore_errors=True)

    def _render_failed(self, ctx: JobContext) -> None:
        job = self.db.one("SELECT error FROM jobs WHERE id = ?", (ctx.id,))
        self.db.update("clips", ctx.job["clip_id"], status="failed", error=job["error"] if job else "failed")

    def render_clip(self, ctx: JobContext) -> None:
        clip = self.db.clip(ctx.job["clip_id"])
        if not clip:
            raise PermanentError("Clip no longer exists")
        rec = self.db.recording(clip["recording_id"])
        out_dir = self.clip_dir(clip)
        if clip["video_path"] and Path(clip["video_path"]).is_file() and Path(clip["video_path"]).parent == out_dir:
            ctx.log("Render for this revision already exists; skipping (no duplicate generation)")
            if clip["status"] in ("pending_render", "rendering", "failed"):
                self.db.update("clips", clip["id"], status="ready")
            return
        if not Path(rec["file_path"] or "").is_file():
            raise PermanentError("The original recording file is missing; cannot render.")
        self.db.update("clips", clip["id"], status="rendering", error=None)
        words = all_words(self.db.segments(rec["id"]))
        label = f"Excerpt · {rec['title'][:48]} · {render.mmss(clip['start'])}"
        ctx.progress(0.05, "Rendering vertical video", force=True)
        res = render.render_clip(self.settings, Path(rec["file_path"]), out_dir, clip["start"], clip["end"],
                                 words, rec["width"], rec["height"], label,
                                 on_progress=lambda f: ctx.progress(0.05 + 0.9 * f, "Rendering"))
        (out_dir / "transcript.txt").write_text(
            f"Source: {rec['source_url'] or rec['original_filename']}\nRecording: {rec['title']}\n"
            f"Excerpt: {render.mmss(clip['start'])} - {render.mmss(clip['end'])}\n\n"
            f"[Context before]\n{clip['context_before']}\n\n[Excerpt - direct transcript]\n{clip['transcript_text']}"
            f"\n\n[Context after]\n{clip['context_after']}\n", encoding="utf-8")
        (out_dir / "description.txt").write_text(clip["description"] or "", encoding="utf-8")
        new_status = "ready"
        self.db.update("clips", clip["id"], status=new_status, video_path=str(res["video"]),
                       thumb_path=str(res["thumb"]), srt_path=str(res["srt"]), ass_path=str(res["ass"]),
                       rendered_at=time.time(), error=None)
        # Remove older revisions of this clip.
        base = out_dir.parent
        for d in base.glob(f"clip{clip['id']:05d}_r*"):
            if d != out_dir:
                shutil.rmtree(d, ignore_errors=True)
        remaining = self.db.one("SELECT COUNT(*) n FROM clips WHERE recording_id = ? AND status IN "
                                "('pending_render','rendering')", (rec["id"],))["n"]
        if remaining == 0:
            self.db.update("recordings", rec["id"], status="ready", stage="done")

    # ---- owner actions ---------------------------------------------------
    def adjust_and_regenerate(self, clip_id: int, start: float | None = None, end: float | None = None) -> int:
        clip = self.db.clip(clip_id)
        if not clip:
            raise ValueError("clip not found")
        rec = self.db.recording(clip["recording_id"])
        start = clip["start"] if start is None else float(start)
        end = clip["end"] if end is None else float(end)
        if not (0 <= start < end <= (rec["duration"] or end)):
            raise ValueError("Timestamps must satisfy 0 ≤ start < end ≤ recording length.")
        if end - start < 3 or end - start > 180:
            raise ValueError("Clip length must be between 3 and 180 seconds.")
        segs = self.db.segments(rec["id"])
        words = all_words(segs)
        sents = segment.build_sentences(segs)
        if (start, end) != (clip["start"], clip["end"]):
            start, end = snap_bounds(words, start, end)
        fields = self.clip_text_fields(rec, start, end, words, sents, self.settings, None)
        self.db.update("clips", clip_id, start=start, end=end, render_rev=clip["render_rev"] + 1,
                       status="pending_render", reviewed_at=None, machine_summary=None,
                       selection_method=(clip["selection_method"] or "") + "+owner-adjusted"
                       if (start, end) != (clip["start"], clip["end"]) else clip["selection_method"], **fields)
        return self.enqueue_render(clip_id)

    def import_upload(self, path: Path, original_name: str, title: str | None, overrides: dict,
                      attestation: str | None) -> tuple[int, bool, int | None]:
        rid, dup = ingest.register_file(self.settings, self.db, path, title=title, original_name=original_name,
                                        source_type="upload", move=True, rights_attestation=attestation,
                                        settings_json=overrides)
        jid = None
        if not dup:
            jid = self.queue.enqueue("process_recording", recording_id=rid, dedupe_key=f"process:{rid}",
                                     payload={"settings": overrides})
        return rid, dup, jid
