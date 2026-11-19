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
from . import framelines, hooks, ingest, render, safety, segment
from .transcribe import get_transcriber

log = logging.getLogger("clipengine.pipeline")

OVERRIDABLE = {"clips_per_recording": int, "clip_min_seconds": float, "clip_max_seconds": float,
               "clip_target_seconds": float, "language": str, "llm_provider": str, "selection_profile": str,
               "whisper_prompt": str}


def effective_settings(settings: Settings, overrides: dict | None) -> Settings:
    s = replace(settings)
    for k, v in (overrides or {}).items():
        if k in OVERRIDABLE and v not in (None, ""):
            try:
                setattr(s, k, OVERRIDABLE[k](v))
            except (TypeError, ValueError):
                pass
    return s


def clamp_segments(segments: list[dict], duration: float | None) -> list[dict]:
    """Drop speech timed beyond the end of the media and clamp overruns (models occasionally
    emit timestamps past the end of the audio; clips must never point outside the file)."""
    if not duration:
        return segments
    out = []
    for sg in segments:
        if sg["start"] >= duration:
            continue
        words = [dict(w, end=min(w["end"], duration)) for w in sg.get("words", []) if w["start"] < duration]
        out.append(dict(sg, end=min(sg["end"], duration), words=words))
    return out


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
        self.on_clip_rendered: list = []      # callbacks(clip_id): cloud upload
        self.on_recording_done: list = []     # callbacks(recording_id): lifecycle check
        self.on_recording_started: list = []  # callbacks(recording_id): engine-ownership flag
        queue.priority_for = self.priority_for

    # ---- creators, niches and priority -------------------------------------------------------
    def creator_for(self, rec):
        """The creator a recording belongs to: set explicitly, or via the monitored source it came from."""
        if rec["creator_id"]:
            return self.db.one("SELECT * FROM creators WHERE id=?", (rec["creator_id"],))
        row = self.db.one("SELECT s.creator_id FROM source_items i JOIN sources s ON s.id=i.source_id "
                          "WHERE i.recording_id=? AND s.creator_id IS NOT NULL", (rec["id"],))
        if row:
            self.db.update("recordings", rec["id"], creator_id=row["creator_id"])
            return self.db.one("SELECT * FROM creators WHERE id=?", (row["creator_id"],))
        return None

    def policy_for(self, rec):
        from . import editorial
        creator = self.creator_for(rec)
        pol = editorial.policy_for(self.db, rec["niche_key"], creator)
        if pol and not rec["niche_key"]:
            self.db.update("recordings", rec["id"], niche_key=pol.niche.key)
        return pol

    def priority_for(self, recording_id):
        """Live captures first, then by creator priority; back-catalogue (backfill) last."""
        from .jobs import PRIORITY_BACKFILL, PRIORITY_LIVE
        rec = self.db.recording(recording_id) if recording_id else None
        if not rec:
            return None
        if rec["origin"] == "live_capture":
            return PRIORITY_LIVE
        if rec["origin"] == "archive":
            return PRIORITY_BACKFILL
        creator = self.creator_for(rec)
        return int(creator["priority"]) if creator else None

    # ---- recording -------------------------------------------------------
    def _recording_failed(self, ctx: JobContext) -> None:
        rid = ctx.job["recording_id"]
        job = self.db.one("SELECT error FROM jobs WHERE id = ?", (ctx.id,))
        self.db.update("recordings", rid, status="failed", error=job["error"] if job else "failed")

    def _record_stats(self, rid: int, **stats) -> None:
        rec = self.db.recording(rid)
        meta = json.loads(rec["metadata_json"] or "{}")
        meta.setdefault("processing", {}).update(stats)
        self.db.update("recordings", rid, metadata_json=json.dumps(meta, default=str))

    def transcribe_recording(self, ctx: JobContext, rec, s: Settings) -> None:
        """Extract audio once, then transcribe in checkpointed chunks. A crash or restart
        resumes at the first unfinished chunk instead of starting over."""
        from . import media, transcribe
        rid = rec["id"]
        self.db.update("recordings", rid, status="transcribing", stage="transcribing", error=None)
        if ctx.payload.get("retranscribe") and not ctx.payload.get("_chunks_cleared"):
            self.db.execute("DELETE FROM transcript_chunks WHERE recording_id = ?", (rid,))
        wav = self.settings.work_dir / f"rec{rid:05d}_audio.wav"
        t0 = time.time()
        if not wav.exists():
            from . import storage
            storage.require_space(self.settings, (rec["duration"] or 0) * storage.WAV_BYTES_PER_S * 1.1, "audio extraction")
            ctx.progress(0.02, "Extracting audio", force=True)
            tmp = wav.with_suffix(".tmp.wav")
            media.extract_audio(Path(rec["file_path"]), tmp, duration=rec["duration"],
                                on_progress=lambda f: ctx.progress(0.02 + 0.06 * f, "Extracting audio"))
            tmp.replace(wav)
            extract_s = time.time() - t0
        else:
            extract_s = 0.0
            ctx.log("Reusing extracted audio from an interrupted run")
        chunks = transcribe.plan_chunks(wav, s.transcribe_chunk_seconds)
        done = {r["chunk_idx"]: r for r in self.db.query(
            "SELECT * FROM transcript_chunks WHERE recording_id = ? ORDER BY chunk_idx", (rid,))}
        if any(abs(done[i]["start"] - chunks[i][0]) > 0.05 for i in done if i < len(chunks)) or \
                any(i >= len(chunks) for i in done):
            ctx.log("Chunk plan changed since the last run; discarding old checkpoints", "WARNING")
            self.db.execute("DELETE FROM transcript_chunks WHERE recording_id = ?", (rid,))
            done = {}
        transcriber = get_transcriber(s)
        ctx.progress(0.08, f"Loading model ({transcriber.model_label})", force=True)
        load_s = transcriber.prepare()
        ctx.log(f"Transcribing with {transcriber.model_label} in {len(chunks)} chunk(s); model ready in "
                f"{load_s:.1f}s" + (f"; resuming after {len(done)} completed chunk(s)" if done else ""))
        total = sum(b - a for a, b in chunks) or 1.0
        lang = s.language or next((r["language"] for r in done.values() if r["language"]), None)
        t1 = time.time()
        audio_done_now = 0.0
        for i, (a, b) in enumerate(chunks):
            if i in done:
                continue
            audio_done_now += b - a
            base = sum(y - x for x, y in chunks[:i]) / total
            frac = (b - a) / total
            tc = time.time()
            segs, lang = transcriber.transcribe_range(
                wav, a, b, lang, on_progress=lambda f, base=base, frac=frac: ctx.progress(
                    0.1 + 0.75 * (base + frac * f), f"Transcribing chunk {i + 1}/{len(chunks)}"))
            self.db.insert("transcript_chunks", recording_id=rid, chunk_idx=i, start=a, end=b,
                           segments_json=json.dumps(segs), language=lang, model_label=transcriber.model_label)
            el = time.time() - tc
            ctx.log(f"Chunk {i + 1}/{len(chunks)} ({a:.0f}-{b:.0f}s): {len(segs)} segments in {el:.1f}s "
                    f"({(b - a) / max(el, 1e-6):.1f}x real time)")
            if ctx.cancelled():
                return
        rows = self.db.query("SELECT * FROM transcript_chunks WHERE recording_id = ? ORDER BY chunk_idx", (rid,))
        segs = clamp_segments([sg for r in rows for sg in json.loads(r["segments_json"])], rec["duration"])
        if not segs:
            raise PermanentError("No speech was detected in this recording.")
        self.db.replace_transcript(rid, segs)
        self.db.execute("DELETE FROM transcript_chunks WHERE recording_id = ?", (rid,))
        wav.unlink(missing_ok=True)  # disposable intermediate
        transcribe_s = time.time() - t1
        self.db.update("recordings", rid, language=lang, whisper_model=transcriber.model_label,
                       status="transcribed", stage="transcribed")
        self._record_stats(rid, extract_audio_s=round(extract_s, 1), model_load_s=round(load_s, 1),
                           transcribe_s=round(transcribe_s, 1), chunks=len(chunks),
                           chunks_resumed=len(done), audio_transcribed_this_run_s=round(audio_done_now, 1),
                           speed_x_realtime=round(audio_done_now / max(transcribe_s, 1e-6), 2),
                           transcriber=transcriber.model_label)
        if transcriber.name == "whisper":
            self.db.set_kv("verified.transcription", json.dumps({"recording_id": rid, "at": time.time(),
                                                                 "model": transcriber.model_label}))
        ctx.log(f"Transcript stored: {len(segs)} segments, language={lang}, {transcribe_s:.0f}s")

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
        for cb in self.on_recording_started:
            cb(rid)
        policy = self.policy_for(rec)
        overrides = {**json.loads(rec["settings_json"] or "{}"), **(ctx.payload.get("settings") or {})}
        if policy:
            # niche + creator defaults; explicit per-recording overrides still win
            overrides = {"clips_per_recording": policy.clips_per_recording, "clip_min_seconds": policy.min_s,
                         "clip_max_seconds": policy.max_s, "selection_profile": policy.niche.selection_profile,
                         "whisper_prompt": policy.whisper_prompt(), **overrides}
            ctx.log(f"Editorial policy: {policy.label()}")
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
        report: dict = {}
        ts = time.time()
        profile = s.selection_profile if s.selection_profile in segment.PROFILES else segment.DEFAULT_PROFILE
        prof = segment.PROFILES[profile]
        if policy and policy.reply_required and not prof.reply_required:
            prof = replace(prof, reply_required=True)
        # Ask for extra candidates when a niche policy may reject some, so quality is not traded for count.
        ask = s.clips_per_recording * (2 if policy else 1)
        pref = ctype = None
        if prof.complete:
            from . import complete
            pref_lo, pref_hi, ctype = complete.preferred_range(
                policy.niche.key if policy else None, policy.creator.content_type if policy else None,
                (s.clip_min_seconds, s.clip_max_seconds))
            pref = (pref_lo, pref_hi)
            ctype = None if ctype == "general" else ctype
        cands = segment.select(segs, n=ask, min_s=s.clip_min_seconds,
                               max_s=s.clip_max_seconds, target=s.clip_target_seconds or None,
                               duration=rec["duration"], reranker=self.make_reranker(s, ctx), report=report,
                               profile=prof, preferred=pref, content_type=ctype)
        assessments = {}
        if policy:
            kept, rejected = [], []
            for c in sorted(cands, key=lambda c: c.score, reverse=True):
                a = policy.assess(c.text, c.score, c.duration)
                if a["reject"]:
                    rejected.append({"start": round(c.start, 1), "end": round(c.end, 1), "quality": a["quality"],
                                     "reasons": a["reject"]})
                    continue
                if len(kept) < s.clips_per_recording:
                    kept.append(c)
                    assessments[id(c)] = a
            report["policy"] = policy.label()
            report["rejected"] = rejected[:20]
            if rejected:
                report["note"] = ((report.get("note") or "") + f" {len(rejected)} candidate(s) rejected by the "
                                  f"{policy.niche.name} profile (reasons in the selection report).").strip()
            cands = sorted(kept, key=lambda c: c.start)
        self._record_stats(rid, selection_s=round(time.time() - ts, 2), selection=report)
        self.db.update("recordings", rid, selection_note=report.get("note"))
        ctx.log(f"Selection: {report}")
        if not cands and prof.complete:
            # Zero clips is a valid outcome of context-complete selection: nothing is padded or fragmented. The
            # recording is held for review (never auto-deleted) and the reasons are kept in the selection note.
            self.db.update("recordings", rid, status="ready", stage="no complete clips selected",
                           selection_note=report.get("note") or "No complete, worthwhile passage was found.")
            ctx.log("Context-complete selection found no clip worth making: " + (report.get("note") or ""))
            return
        if not cands:
            raise PermanentError("No suitable self-contained passages were found (transcript too short or "
                                 "no complete sentences).")
        sents = segment.build_sentences(segs)
        words = all_words(segs)
        next_idx = (self.db.one("SELECT MAX(idx) m FROM clips WHERE recording_id = ?", (rid,))["m"] or 0) + 1
        made = 0
        for c in cands:
            if rec["duration"] and c.start >= rec["duration"] - 1.0:
                continue                      # cannot point outside the media
            a = assessments.get(id(c))
            cid = self.create_clip(rec, next_idx + made, c.start, min(c.end, rec["duration"] or c.end), words,
                                   sents, s, score=c.score, reasons=c.reasons + (a["reasons"] if a else []),
                                   method=c.method, summary=c.summary, quality=a,
                                   niche_key=policy.niche.key if policy else None, completeness=c.assessment)
            self.enqueue_render(cid)
            made += 1
        if not made:
            raise PermanentError("No candidate passages fall within the recording's media duration.")
        self.db.update("recordings", rid, status="clips_queued", stage="rendering")
        ctx.log(f"Selected {len(cands)} candidate excerpts; render jobs queued")

    def hook_fields(self, rec, transcript: str, rev: int = 1, s: Settings | None = None) -> dict:
        """Proposed hook title for the clip's own transcript (never raises: a failure just means 'enter one manually')."""
        from . import hooks
        try:
            creator = self.creator_for(rec)
            brand = creator["brand_key"] if creator else None
            if not brand:
                # Session 13: never fall back to the permissive default policy when the niche tells us the brand
                # (clips 25-30 were titled with brand_policy "default" before their creator had a brand).
                niche = rec["niche_key"]
                if not niche and creator and creator["niches_json"]:
                    try:
                        niche = (json.loads(creator["niches_json"]) or [None])[0]
                    except (ValueError, TypeError):
                        niche = None
                row = self.db.one("SELECT key FROM brands WHERE niche_key = ?", (niche,)) if niche else None
                brand = row["key"] if row else None
            speaker = None
            if creator and (creator["kind"] or "creator") == "creator" and creator["name"]:
                nm = creator["name"].split()
                speaker = nm[-1] if len(nm) == 2 else creator["name"]
            extra = None
            s = s or self.settings
            if getattr(s, "llm_provider", "none") != "none" and segment.ollama_available(s.ollama_url):
                try:
                    extra = hooks.ollama_candidates(s.ollama_url, s.ollama_model, transcript, brand or "")
                except Exception:      # a local model is optional; deterministic generation still runs
                    extra = None
            result = hooks.generate(transcript, brand, speaker, extra, self.settings.data_dir)
            if getattr(self.settings, "title_semantic", False):
                from . import semantic
                result = semantic.rerank(result, transcript)       # reorders VERIFIED candidates only
            return hooks.fields_for(result, rev)
        except Exception as e:         # noqa: BLE001
            return hooks.fields_for({"status": "manual_needed", "title": None, "warnings": [f"title generation failed: {e}"]}, rev)

    def create_clip(self, rec, idx, start, end, words, sents, s: Settings, *, score=None, reasons=None,
                    method="heuristic", summary=None, quality=None, niche_key=None, completeness=None) -> int:
        start, end = snap_bounds(words, start, end)
        fields = self.clip_text_fields(rec, start, end, words, sents, s, summary)
        fields.update(self.hook_fields(rec, fields["transcript_text"], s=s))
        if quality:
            # niche review flags (claims, financial promotion, political context) join the safety flags:
            # the editorial gate then requires an explicit decision before any publication
            if quality["flags"]:
                flags = json.loads(fields["safety_flags"]) + quality["flags"]
                fields.update(safety_flags=json.dumps(flags), safety_status="flagged")
            fields["quality_json"] = json.dumps(quality)
        if niche_key:
            fields["niche_key"] = niche_key
        if completeness:
            fields["completeness_json"] = json.dumps(completeness)
            fields["duration_class"] = completeness.get("duration_class")
            if completeness.get("verdict") == "REVIEW":
                # potentially worthwhile but context or meaning is uncertain: the owner must decide before publication
                flags = json.loads(fields["safety_flags"]) + [{"category": "context_review", "term": "", "note":
                        "Context completeness is uncertain: " + "; ".join(completeness.get("reasons", [])[-2:])}]
                fields.update(safety_flags=json.dumps(flags), safety_status="flagged")
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
        if ctx.job["recording_id"]:
            self._maybe_complete(ctx.job["recording_id"])

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
        from . import storage
        storage.require_space(self.settings, (clip["end"] - clip["start"]) * storage.CLIP_BYTES_PER_S * 1.3, "rendering this clip")
        self.db.update("clips", clip["id"], status="rendering", error=None)
        words = all_words(self.db.segments(rec["id"]))
        label = f"Excerpt · {rec['title'][:48]} · {render.mmss(clip['start'])}"
        ctx.progress(0.05, "Rendering vertical video", force=True)
        from . import captions, titles
        creator = self.creator_for(rec)
        tstyle = titles.style_for(self.db, creator["brand_key"] if creator else None)
        ttext = titles.format_title(clip["title"], single_phrase=tstyle.single_phrase) if (clip["title"] and tstyle.enabled) else None
        res = render.render_clip(self.settings, Path(rec["file_path"]), out_dir, clip["start"], clip["end"],
                                 words, rec["width"], rec["height"], label,
                                 style=captions.style_for(self.db, creator["brand_key"] if creator else None),
                                 title=ttext, title_style=tstyle,
                                 line_style=framelines.style_for(self.db, creator["brand_key"] if creator else None),
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
        for cb in self.on_clip_rendered:
            try:
                cb(clip["id"])
            except Exception:  # noqa: BLE001 - an upload problem must never fail a render
                log.exception("post-render hook failed")
        self._maybe_complete(rec["id"])

    def _maybe_complete(self, rid: int) -> None:
        """When no clip is still rendering: mark the recording ready and queue ONE notification
        (only if at least one clip is reviewable — never for a recording that failed before clips)."""
        remaining = self.db.one("SELECT COUNT(*) n FROM clips WHERE recording_id = ? AND status IN "
                                "('pending_render','rendering')", (rid,))["n"]
        if remaining:
            return
        ready = self.db.one("SELECT COUNT(*) n FROM clips WHERE recording_id=? AND status IN ('ready','approved')",
                            (rid,))["n"]
        if ready:
            self.db.update("recordings", rid, status="ready", stage="done")
            from . import notify
            notify.on_recording_ready(self.settings, self.db, self.queue, rid)
        for cb in self.on_recording_done:
            try:
                cb(rid)
            except Exception:  # noqa: BLE001
                log.exception("recording-done hook failed")

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
        if end - start < 3:
            raise ValueError("A clip must be at least 3 seconds long.")     # no editorial ceiling: platform limits are shown, not enforced
        if rec["raw_deleted_at"] or not (rec["file_path"] and Path(rec["file_path"]).is_file()):
            raise ValueError("The original recording was removed after verified processing, so this clip cannot be "
                             "re-cut from the source. The finished clip remains (restore it from Google Drive if needed).")
        fields: dict = {}
        if (start, end) != (clip["start"], clip["end"]):
            # New boundaries: recompute quote, context, flags and description from the stored transcript.
            segs = self.db.segments(rec["id"])
            words = all_words(segs)
            start, end = snap_bounds(words, start, end)
            fields = self.clip_text_fields(rec, start, end, words, segment.build_sentences(segs), self.settings)
            method = clip["selection_method"] or ""
            fields.update(machine_summary=None, selection_method=method if "owner-adjusted" in method
                          else method + "+owner-adjusted")
            if hooks.may_regenerate(clip):         # new revision: refresh a GENERATED title; owner titles are never touched
                fields.update(self.hook_fields(rec, fields["transcript_text"], (clip["title_rev"] or 0) + 1))
        # Unchanged boundaries: re-render only; owner edits to the description are preserved.
        self.db.update("clips", clip_id, start=start, end=end, render_rev=clip["render_rev"] + 1,
                       status="pending_render", reviewed_at=None, **fields)
        return self.enqueue_render(clip_id)

    def import_upload(self, path: Path, original_name: str, title: str | None, overrides: dict,
                      attestation: str | None, source_url: str | None = None,
                      recorded_at: str | None = None) -> tuple[int, bool, int | None]:
        extra = {"retrieval": {"method": "owner_supplied_file"}} if source_url else None
        rid, dup = ingest.register_file(self.settings, self.db, path, title=title, original_name=original_name,
                                        source_type="linked_file" if source_url else "upload", move=True,
                                        rights_attestation=attestation, settings_json=overrides,
                                        source_url=source_url or None, extra_meta=extra,
                                        source_site=(__import__("urllib.parse").parse.urlparse(source_url).hostname
                                                     if source_url else None),
                                        recorded_at=recorded_at or ingest.date_from_filename(original_name))
        jid = None
        if not dup:
            jid = self.queue.enqueue("process_recording", recording_id=rid, dedupe_key=f"process:{rid}",
                                     payload={"settings": overrides})
        return rid, dup, jid
