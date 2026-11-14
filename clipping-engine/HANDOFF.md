# FULL HANDOFF PROMPT — Local AI Video Clipping & Editorial Review Engine

> **For the next engineer or agent:** paste this whole file into a new Claude Code session, or say
> "read `clipping-engine/HANDOFF.md` and continue". It is self-contained and consolidates **all** work from
> session 1 (initial build) and session 2 (continuation). Earlier handoff versions are in git history
> (`553c922`, then the session-2 version in `ce0ba89`). Treat this document as a map, not proof: re-inspect the
> checkout and CI before relying on any claim.

---

## 0. Your role and the standing rules (binding)

You are the lead engineer of a **working, CI-verified, locally hosted** application. The owner's instructions
across both sessions remain in force.

### 0.1 How to work
- **Execute.** Don't produce proposals or stop after planning. Build, test, repair, verify in CI, then report.
- **Don't rebuild working systems.** Change code only for a demonstrated defect or a requested feature.
- **Owner-intervention protocol.**
  1. When something needs the owner (accounts, developer registration, OAuth approval, permissions, public
     posting), record the dependency.
  2. Continue all independent work.
  3. Implement everything possible without the owner.
  4. At the end, present **one consolidated, prioritised owner-action checklist**.
  5. **Don't scatter questions** through the work; the owner explicitly asked not to be interrupted repeatedly.

  Interrupt only if everything remaining is blocked, a destructive action needs approval, or an essential
  ambiguity can't safely be resolved.
- **Never ask for passwords, API secrets or recovery codes in chat.** Secrets go in the owner's local `.env`
  file or the OS credential store (Windows Credential Manager via `keyring`).
- **Never present simulated functionality as real.**
  - The TEST-ONLY stub transcriber, `clipengine/testsupport.py`, mocked TikTok/Instagram HTTP, and the TEST-ONLY
    `ALLOW_PRIVATE_URLS` switch must stay labelled as such in code, tests, UI and reports.
  - "Connected" requires real stored OAuth tokens. "Ready" for a platform requires a real, platform-confirmed
    transaction.
  - Never claim a live TikTok or Instagram upload succeeded without the platform API confirming it.
- **Never declare the project complete** while owner-dependent integrations are unfinished. Report the exact
  status instead.
- **Forbidden without explicit owner permission:**
  - purchases, creating accounts, registering developer apps, changing external services
  - **public posting** (the first Instagram publish is public and needs explicit owner instruction)
- **Never circumvent** DRM, logins, paywalls, access controls, copyright restrictions, platform terms or rate
  limits. **No browser automation** to evade platform rules.
- **Editorial ethics.**
  - The use case is political-media monitoring, factual reporting, documentary analysis and critical
    commentary.
  - Preserve context and provenance; never change a speaker's meaning; never fabricate quotes.
  - Flag hateful or extremist advocacy for human review and never generate promotional material from it.
  - Never optimise for outrage.

### 0.2 Architecture constraints
- Python-first, local inference, free and open source, no paid LLM or API, no subscriptions.
- FastAPI + HTMX + responsive CSS, SQLite, FFmpeg/FFprobe, faster-whisper.
- No Docker, Redis, Postgres, cloud hosting or heavy frontend frameworks.
- **The target is the owner's Windows PC**, with a one-click `.cmd` launcher. That PC has **never been inspected
  or run**; don't claim anything about its hardware or performance.

### 0.3 Git rules
- Repo: **`ce-optimisation/n8n-automation-portfolio`**. Branch: **`claude/optimistic-noether-k52qjf`**. Develop
  and push only there.
- Push with `git push -u origin claude/optimistic-noether-k52qjf`, retrying network failures up to 4 times with
  exponential backoff (2, 4, 8, 16 s).
- **Don't open a pull request unless the owner asks.**
- End every commit message with the session attribution lines the environment provides. Never put model names or
  identifiers in commits, code or docs.
- All application code is in **`clipping-engine/`**, plus **`.github/workflows/clipping-engine.yml`** at the repo
  root. The repo root also holds unrelated **n8n workflow templates (JSON/MD); never modify them.**

---

## 1. Current status (end of session 2)

| Area | Status | Evidence |
|---|---|---|
| Local workflow: import → real Whisper → selection → 1080×1920 render → mobile review → approve/reject → download → restart persists | **Verified** | Acceptance test 9/9 on Ubuntu **and** Windows with real faster-whisper `base.en` (CI run #8 `43611814430`; latest run #9 `43613236155` green) |
| Unit and integration suite | **Green** | 68 tests. CI Ubuntu: 68 passed. CI Windows: 67 passed + 1 expected failure (live Wikimedia import refused by that runner with HTTP 403). Local sandbox: 66 passed, 2 skipped (real Whisper and live network are blocked there). |
| URL ingestion (direct files, permitted extractor, pending sources, inbox, SSRF protection, resume) | **Verified** | Local-server tests; **live** import of a public-domain NASA video from Wikimedia Commons on the Ubuntu CI runner |
| Rumble automated download | **Deliberately not enabled** | Rumble's Terms of Use forbid automated access without Rumble's prior written permission. Lawful routes are implemented (§7). Live Rumble retrieval is **untested**. |
| Long-form processing | **Verified** | 114-min recording, real Whisper, hard-kill crash test with resume (§10) |
| Selection v2 | **Verified, reproducible** | Evaluation on a real Whisper transcript (§9) |
| TikTok upload-to-inbox | **Implemented, mock-tested only** | Needs the owner's developer app, OAuth connection and a first real upload |
| Instagram Reels publishing | **Implemented (two login modes), mock-tested only** | Needs a Creator account, a Meta app and a connection; the first real publish is **public** and needs explicit owner go-ahead |
| Windows launcher | **Verified on GitHub Windows runners only** | `Start-Clipping-Engine.cmd --check` passes in CI |
| Owner's Windows PC | **Never run** | Owner action C1 |

### 1.1 Commit history on the branch

| SHA | Session | Summary |
|---|---|---|
| `0099d4e` | — | Original repo upload (n8n templates; not ours) |
| `f95fa7a` | 1 | Initial full application, tests, launcher, README, CI |
| `39e5846` | 1 | Fix: a plain "Regenerate" no longer overwrites an owner-edited description |
| `fc51955` | 1 | Fix (found only by real-model CI): pass NumPy samples to Whisper instead of a file path (PyAV `metadata_errors` incompatibility) |
| `4eb9736` | 1 | Fix: read UTF-8 caption files explicitly (Windows cp1252 default) |
| `553c922` | 1 | First handoff document |
| `6ab4b2c` | 2 | Main continuation: lawful link ingestion, checkpointed long-form transcription, selection v2, job-based publishing, setup checklist, security, launcher improvements |
| `0616266` | 2 | Configurable `HTTP_USER_AGENT`; the live import test reports a remote HTTP 403 as an expected failure |
| `ce0ba89` | 2 | Accurate transcription-speed metric after a resume; README long-form figures; session-2 handoff |
| *(this commit)* | 2 | This consolidated handoff |

### 1.2 CI runs worth knowing

| Run | Notes |
|---|---|
| #4 `43600559824` | First fully green session-1 run |
| #6 `43610881618` | Windows live-import 403 discovered |
| #7 `43610890294` | **Long-form dispatch run** (114 min, real Whisper, crash test); artifact `longform-report` |
| #8 `43611814430` | Green on both operating systems with the expected failure on Windows |
| #9 `43613236155` | Latest, green |

---

## 2. Environment facts that shaped decisions

- **Build sandbox:** a Linux cloud container (4 cores, 15 GB RAM, no GPU, Python 3.13). FFmpeg has libass,
  libx264, libflite (used to synthesise test speech) and loudnorm. Python packages were installed system-wide
  with `pip install --break-system-packages`.
- **The sandbox network blocks:**
  - huggingface.co, so no Whisper model download
  - developers.tiktok.com and developers.facebook.com (API details were confirmed through web search of the
    official docs)
  - upload.wikimedia.org

  A personal environment adding the Hugging Face hosts was proposed in session 1. The owner's Claude **mobile
  app couldn't display the approval**, so nothing changed. It was **not re-proposed** in session 2 (the owner
  asked not to be interrupted, and CI covers real Whisper). It is listed as an optional owner action.
- **Real Whisper, live-network and Windows verification therefore happen in GitHub Actions.** Use the GitHub MCP
  tools:
  - `mcp__github__actions_list` (`list_workflow_runs` / `list_workflow_jobs`)
  - `mcp__github__get_job_logs` (`return_content`, `tail_lines`)
  - `mcp__github__actions_run_trigger` (`run_workflow`, `inputs.longform_minutes`)

  There is no `gh` CLI except for search.
- **Python Playwright** is installed in the sandbox, but its browser revision doesn't match. Locally, set
  `PW_CHROMIUM=/opt/pw-browsers/chromium`; the acceptance script honours it. CI installs its own Chromium.
- **Scratchpad:** `/tmp/claude-0/-home-user-n8n-automation-portfolio/8d594302-4454-53f8-8af2-bf3d07b5f399/scratchpad`.
  It holds disposable samples (`long60.mp4`, `bench60/`, screenshots). It is not durable.

---

## 3. Directory map (every file and its purpose)

```
.github/workflows/clipping-engine.yml
    Jobs:
    - test (ubuntu-latest, windows-latest; Python 3.12; env CLIPENGINE_REAL_WHISPER=1,
      CLIPENGINE_LIVE_NET=1, WHISPER_MODEL=base.en):
      FFmpeg install (apt / choco ffmpeg-full incl. libflite)
      → Windows-only Start-Clipping-Engine.cmd --check (CLIPENGINE_NONINTERACTIVE=1)
      → pip install -r requirements.txt playwright + playwright install chromium
      → pytest -v -rs → scripts/acceptance_test.py --keep acceptance-artifacts → upload artifacts.
    - longform (ubuntu, only on workflow_dispatch with input longform_minutes != '0', timeout 180 min):
      longform_benchmark.py --crash-test (REAL Whisper) → evaluate_selection.py on the real and stub
      transcripts → upload longform-report.
    Triggers: push/PR touching clipping-engine/** or the workflow; workflow_dispatch (input longform_minutes, default 0).

clipping-engine/
  Start-Clipping-Engine.cmd   Windows one-click launcher. **CRLF line endings — keep them.**
  start.sh                    macOS/Linux launcher.
  requirements.txt            fastapi, uvicorn, jinja2, python-multipart, httpx, faster-whisper, yt-dlp, keyring, pytest.
  .env.example                Every setting, documented (copied to .env on first launch). See §14.
  .gitignore                  .env, .venv/, data/, tools/ffmpeg/, caches.
  README.md                   Owner-facing guide (install, first recording, link rules, long-form, selection,
                              safeguards, TikTok/Instagram setup, reliability, phone access, tests, troubleshooting).
  HANDOFF.md                  This file.
  clipengine/
    __init__.py
    __main__.py      `python -m clipengine [--check] [--no-browser] [--download-model]`. Dependency checks;
                     refuses CLIPENGINE_HOST=0.0.0.0 without DASHBOARD_PASSWORD; adds tools/ffmpeg/bin and
                     %LOCALAPPDATA%\Microsoft\WinGet\Links to PATH; JSON rotating logs (data/logs/clipengine.jsonl,
                     5 MB × 5); hardware_report() (hardware, model, threads, GPU tip, low-RAM tip, OneDrive warning);
                     port conflicts via port_free() (connect_ex — NOT bind) and is_our_engine() (/healthz returns
                     app=clipengine): open the existing instance, or move to the next free port (+1..+19) with a
                     warning about OAuth redirect ports; prints URL, inbox path and log path; runs uvicorn.
    config.py        Settings dataclass plus a tiny .env loader (no python-dotenv). get_settings(**overrides).
                     Properties: db_path, recordings_dir, clips_dir, work_dir, models_dir, secrets_dir, inbox;
                     ensure_dirs().
    hardware.py      detect(): OS, CPU, cores, RAM (ctypes on Windows), CUDA count (ctranslate2), GPU name and VRAM
                     (nvidia-smi), ffmpeg/ffprobe paths. choose_whisper() → (model, device, compute_type).
    db.py            SQLite (WAL, foreign keys, busy_timeout 30 s, IMMEDIATE transactions). SCHEMA_VERSION=2.
                     MIGRATIONS (idempotent ADD COLUMN). Helpers: insert (sets created_at/updated_at only if the
                     table has them, via cached PRAGMA table_info), update, query, one, execute, tx, segments,
                     replace_transcript (also fills FTS), search_transcripts (terms quoted, so no FTS injection),
                     counts, get_kv/set_kv (meta table). See §5.
    jobs.py          Persistent queue: enqueue (dedupe_key + partial unique index on active jobs), atomic claim,
                     a single worker thread, TransientError (backoff 15·2^(n−1) s, max 600 s, up to max_attempts)
                     vs PermanentError (fail now), "<kind>:failed" hooks, recover() re-queues 'running' jobs at
                     start, retry(), cancel(), drain() for tests/CLI, JobContext.progress/log/cancelled.
    media.py         probe() (ffprobe JSON; handles rotation), run_ffmpeg() (-progress parsing, stderr tail,
                     CREATE_NO_WINDOW on Windows), extract_audio() → 16 kHz mono s16le WAV.
    ingest.py        Link classification, SSRF protection, direct download with resume, yt-dlp path, pending
                     sources, register_file (provenance, dedupe). See §7.
    inbox.py         InboxWatcher (watch folder). See §7.4.
    transcribe.py    Chunked, range-based transcription; WhisperTranscriber; StubTranscriber (TEST-ONLY). See §8.
    segment.py       Sentence building, candidate windows, scoring profiles v1/v2, diversity, optional Ollama
                     re-ranker. See §9.
    safety.py        Regex screen (violence_advocacy, dehumanisation, extremist_terminology, owner_watchlist from
                     data/safety_terms.txt); low_confidence_words (prob < 0.5).
    render.py        Caption phrasing, SRT/ASS, layout, FFmpeg filter graph, render_clip(), thumbnail, output
                     verification. See §11.
    pipeline.py      Job handlers: process_recording (checkpointed transcription, selection), render_clip,
                     adjust_and_regenerate, import_upload, describe(). See §6.
    maintenance.py   Hourly housekeeping thread and storage_report(). See §13.
    setup_status.py  Settings → Setup checklist (honest statuses). See §12.4.
    testsupport.py   TEST-ONLY stub_transcript_from_timing() (not imported by the app).
    publishing/
      __init__.py    Exports STATES, ACTIVE_STATES, CONFIRMED_STATES, NotConfigured, PublishError, PublishResult,
                     editorial_gate, validate_video, TokenStore, TikTokClient, InstagramClient.
      base.py        PublishResult, PublishError, NotConfigured(PublishError), STATES, RIGHTS_OK,
                     editorial_gate(), validate_video().
      tokens.py      TokenStore: keyring (Windows Credential Manager) or fallback data/secrets/tokens.json (chmod 600).
      tiktok.py      TikTokClient. See §12.2.
      instagram.py   InstagramClient (modes facebook and instagram). See §12.3.
      service.py     PublishService: job-based, checkpointed, duplicate-safe publishing. See §12.1.
    web/
      app.py         FastAPI app factory create_app(settings, start_worker=True); App service container; all
                     routes and middleware. See §12.5.
      static/        htmx.min.js (1.9.12, vendored, works offline); style.css (mobile-first, dark mode, no
                     overflow at 390 px).
      templates/     base, login, dashboard, _counts, _jobs, import, _proc_settings, review, _clip_card,
                     _clip_status, clip, recordings, recording, jobs, job, search, settings.
  scripts/
    make_sample_video.py     90 s lawful sample: ORIGINAL council-bridge briefing text, flite voice, testsrc2
                             1280x720; also writes <name>.timing.json with exact paragraph timings.
    make_longform_sample.py  Labelled long recordings of any length: 12 civic topics, substantive templates
                             (claims, figures, Q&A, objections, "And…" continuations) plus procedural paragraphs
                             (greetings, housekeeping, filler). Original text, flite voice, testsrc2 640x360 at
                             15 fps. Writes .timing.json with label and topic per paragraph. About 2 s per minute
                             of audio to generate.
    evaluate_selection.py    Reproducible v1-vs-v2 metrics against the labels; stub or real transcript (--transcript).
    longform_benchmark.py    Runs the real pipeline in a worker subprocess, sampling RSS and work-directory size;
                             --crash-test hard-kills the worker after the first checkpoint, then restarts and
                             verifies the resume; writes report.json and transcript_segments.json.
    acceptance_test.py       9-step end-to-end against the real running server (real Whisper, or --stub TEST-ONLY).
    ensure_deps.py           pip install only when the requirements hash (+cuda flag) changes;
                             INSTALL_CUDA_LIBS=1 adds nvidia-cublas-cu12 and nvidia-cudnn-cu12==9.*.
  tests/   conftest.py, test_hardware.py, test_ingest_sources.py, test_jobs.py, test_live_network.py,
           test_pipeline_web.py, test_publishing.py, test_reliability.py, test_render_ingest.py,
           test_security_setup.py, test_segment_safety.py, test_whisper_real.py   (§15)
```

**Runtime data layout** (`clipping-engine/data/`, git-ignored; override with `CLIPENGINE_DATA_DIR`):

- `clipengine.sqlite3`
- `recordings/<YYYYmmdd-HHMMSS>_<title>/source.<ext>` and `provenance.json`
- `clips/recNNNNN/clipNNNNN_rN/`:
  - `clip.mp4` (`clip.tmp.mp4` while rendering), `thumb.jpg`
  - `captions.srt`, `captions.ass`
  - `transcript.txt`, `description.txt`
- `models/` (Whisper weights)
- `work/` (disposable):
  - `recNNNNN_audio.wav` (kept across a crash for resume)
  - `dl_<sha1>.part` (resumable downloads)
  - `ytdlp_<sha1>/`
  - temporary upload copies
- `inbox/`, with `duplicates/` and `failed/` subfolders
- `secrets/`: `session_key`, and `tokens.json` only when the keyring fallback is in use
- `logs/clipengine.jsonl`
- optional `safety_terms.txt`

---

## 4. End-to-end data flow

1. **Import.** A recording arrives by one of four routes:
   - Upload (`POST /import/upload`)
   - Inbox folder (InboxWatcher)
   - Direct or permitted link (`POST /import/url` → `download_url` job)
   - Pending source plus attached file

   `register_file()` then:
   - computes SHA-256 and checks for duplicates (by hash or normalised URL)
   - probes with ffprobe, requiring both video and audio streams
   - moves the file into `recordings/…/source.<ext>`
   - writes `provenance.json`
   - inserts the `recordings` row

   A `process_recording` job is then queued (dedupe key `process:<rid>`).
2. **process_recording.** Transcribes in checkpointed chunks if there is no transcript yet (§8), then selects
   candidates (§9). For each candidate it creates a clip row: snapped bounds, verbatim quote, context, safety
   flags, confidence and description. Then it enqueues `render_clip` jobs (dedupe `render:<cid>`).
3. **render_clip.** Renders a 1080×1920 MP4 with captions, a thumbnail and text files, then sets the clip status
   to `ready`. When no clips are pending, the recording becomes `ready`.
4. **Review.** Approve, reject, regenerate (optionally with new timestamps; the transcript is reused), record
   rights, record the editorial decision on a flag, edit the description, download.
5. **Publish (optional).** `PublishService.request()` checks the editorial gate, the connection and duplicates,
   then creates a `publish_attempts` row and a `publish_clip` job. The job runs checkpointed stages through to a
   platform-confirmed state (§12).

---

## 5. Data model (SQLite, schema_version 2)

- **meta**: `key`, `value`.
  - `schema_version`
  - `verified.transcription` (JSON: recording id, timestamp, model; set when real Whisper succeeds)
  - `oauth_error.tiktok` and `oauth_error.instagram` (last sign-in error text)
- **recordings**:
  - Identity and source: `id`, `title`, `source_type` (upload | url | linked_file | inbox | local_path),
    `source_url` (normalised), `source_site`, `source_media_id`, `original_filename`.
  - File: `file_path`, `sha256` (unique when not null), `size_bytes`, `duration`, `width`, `height`, `fps`,
    `has_audio`.
  - `metadata_json`: the probe, `retrieval` {method, policy, reason, retrieved_at, final_url, content_type,
    bytes, etag, last_modified, pending_source_id}, `source_info` (yt-dlp), and `processing` {extract_audio_s,
    model_load_s, transcribe_s, chunks, chunks_resumed, audio_transcribed_this_run_s, speed_x_realtime,
    transcriber, selection_s, selection{...report}}.
  - Status: `status` (imported → transcribing → transcribed → analysing → clips_queued → ready, or failed),
    `stage`, `error`.
  - Transcription and settings: `language`, `whisper_model` (label), `settings_json` (per-import overrides),
    `rights_attestation`, `selection_note` (v2 migration), timestamps.
- **transcript_segments**: `recording_id`, `idx`, `start`, `end`, `text`, `avg_logprob`, `no_speech_prob`,
  `words_json` (a list of {start, end, word, prob}). **transcript_fts**: FTS5 over `text`, with
  `recording_id`, `segment_id` and `start` unindexed.
- **transcript_chunks** (v2): (`recording_id`, `chunk_idx`) primary key, `start`, `end`, `segments_json`,
  `language`, `model_label`, `created_at`. These are checkpoints, deleted after the transcript is assembled.
- **clips**:
  - Position: `recording_id`, `idx`, `start`, `end`.
  - `status`: pending_render | rendering | ready | approved | rejected | failed.
  - Text: `transcript_text`, `quote`, `context_before`, `context_after`, `description`, `machine_summary`.
  - Selection: `score`, `score_reasons`, `selection_method` (heuristic | llm+heuristic | …+owner-adjusted),
    `confidence`, `low_conf_words`.
  - Review gates: `safety_flags`, `safety_status` (clear | flagged | acknowledged), `rights_status` (unreviewed
    | owned | licensed | public_domain | fair_dealing_reviewed | not_cleared).
  - Outputs: `video_path`, `thumb_path`, `srt_path`, `ass_path`, `render_rev`, `rendered_at`.
  - Review: `reviewed_at`, `review_note`, `error`.
- **jobs**:
  - `kind`: download_url | process_recording | render_clip | publish_clip | download_model.
  - `recording_id`, `clip_id`, `payload_json`, `dedupe_key`.
  - `status`: queued | running | done | failed | cancelled.
  - `progress`, `message`, `attempts`, `max_attempts`, `error`, `run_after`, timestamps.
  - A partial unique index on `dedupe_key` covers queued and running jobs.
- **job_logs**: `job_id`, `ts`, `level`, `message`. This table has no created_at or updated_at.
- **pending_sources** (v2): `url` (normalised), `title`, `note`, `reason`, `status` (awaiting_file | attached |
  cancelled), `recording_id`, `settings_json`, timestamps.
- **source_permissions** (v2): `domain` (unique), `granted_by`, `granted_on`, `reference`, `scope`,
  timestamps.
- **public_media_tokens** (v2): `token` (primary key), `clip_id`, `expires_at`, `created_at`.
- **publish_attempts**:
  - `clip_id`, `platform`, `status` (see STATES), `remote_id` (TikTok publish_id or Instagram media id),
    `detail`.
  - Added in v2: `stage`, `container_id`, `job_id`, `confirmed_at`.
- **Migration:** `MIGRATIONS` runs `ALTER TABLE ADD COLUMN` when a column is missing. It is tested against a
  recreated v1-shape database (`test_migrates_database_from_first_release`).

---

## 6. Processing pipeline details (pipeline.py)

- **effective_settings()** merges the global settings, `recordings.settings_json`, then job payload settings.
  Only these OVERRIDABLE keys apply: `clips_per_recording`, `clip_min_seconds`, `clip_max_seconds`,
  `clip_target_seconds`, `language`, `llm_provider`.
- **transcribe_recording()**:
  1. Optional `retranscribe` clears checkpoints.
  2. Audio is extracted once to `work/recNNNNN_audio.wav`, written as `.tmp.wav` then renamed. If it already
     exists it is reused, and the log says "Reusing extracted audio from an interrupted run".
  3. `plan_chunks()` plans the chunks. Existing checkpoints are loaded and discarded if the plan changed.
  4. `transcriber.prepare()` loads the model (timed).
  5. For each unfinished chunk: `transcribe_range()`, then insert the checkpoint and log the chunk speed. A
     cancelled job returns.
  6. The checkpoints are assembled and `replace_transcript()` runs. Then the checkpoints are deleted, the **WAV
     is deleted**, stats are recorded, and `verified.transcription` is set if real Whisper ran.
  7. No speech at all is a PermanentError.
- **process_recording()**:
  - Transcribes if needed; otherwise logs "skipping (resume)".
  - If clips exist and `reselect` is not set, **nothing is created**; renders are re-enqueued only for pending
    or failed clips.
  - With `reselect`, unreviewed clips are deleted and approved or rejected ones kept.
  - Runs `segment.select(..., report=report)`, records `selection_s` and the selection report, and sets
    `recordings.selection_note`.
  - For each candidate, `create_clip()` runs `snap_bounds` (never inside a word; pad −0.2 s / +0.4 s clamped to
    the neighbouring words) and `clip_text_fields()`.
- **clip_text_fields()**: the verbatim words in the range, context before and after, `safety.screen()`, low
  confidence words, mean confidence, and `describe()`.
- **describe()** (neutral, never a headline):
  - `Excerpt (m:ss–m:ss) from "<title>".`
  - `Direct quotation (opening): "<first 28 words>…"`
  - an `Automated summary (machine interpretation, verify before use)`, only if an LLM summary exists and the
    clip is not flagged
  - an editorial note if flagged
  - a transcription note if there are low-confidence words
  - `Source: <url or local file>`
- **render_clip()**:
  - **Idempotent**: skips if `video_path` exists inside the current revision's directory.
  - Writes `transcript.txt` (context before, the direct transcript, context after, source) and
    `description.txt`.
  - Deletes old revision folders.
- **adjust_and_regenerate()**:
  - Validates `0 ≤ start < end ≤ duration` and a length of 3–180 s.
  - **Changed bounds:** snap, recompute text fields from the stored transcript (never re-transcribes), clear
    `machine_summary`, append `+owner-adjusted` once.
  - **Unchanged bounds:** re-render only; the **owner-edited description is preserved** (session-1 fix).
  - Always bumps `render_rev` and resets the status to `pending_render`.

---

## 7. Ingestion (ingest.py, inbox.py) — legal and safety design

### 7.1 classify_url(url, settings, db) → UrlDecision(method, code, message, host)

| Condition | method / code |
|---|---|
| Host in `RESTRICTED_PLATFORMS` and **no** `source_permissions` row | `link_only` / `restricted_platform`, with a platform-specific reason. Rumble: its Terms of Use prohibit automated access without Rumble's prior written permission. |
| `check_network_target` fails | `refused` with code `unsupported_scheme`, `credentials_in_url`, `unsupported_port`, `unresolvable` or `private_address` |
| Path ends with .mp4/.mov/.m4v/.mkv/.webm/.avi (not restricted) | `direct` / `direct_media` |
| Recorded permission for the domain | `ytdlp` / `permission_recorded` (the reference is cited) |
| Host in `ALLOWED_URL_DOMAINS` | `ytdlp` / `allowlisted` |
| Anything else | `link_only` / `not_allowed`, explaining direct links, the allowlist and attaching the file |

`RESTRICTED_PLATFORMS`: youtube.com, youtu.be, rumble.com, tiktok.com, instagram.com, facebook.com, fb.watch,
x.com, twitter.com, twitch.tv, kick.com, bitchute.com, odysee.com. Subdomains match through `host_matches`;
look-alike domains don't match.

### 7.2 Network safety
- **`check_network_target()`**:
  - only http(s); ports 80/443 only
  - no `user:pass@` in the URL
  - every resolved IP must be `is_global` and not multicast; IPv4-mapped IPv6 is unwrapped
  - `resolve_host` is module-level so tests can replace it
  - the TEST-ONLY `allow_private_urls` (`ALLOW_PRIVATE_URLS=1`) bypasses these checks
- **`download_direct()`**:
  - follows redirects manually (at most 5) and **re-checks every hop**
  - after connecting, checks the **peer IP** (`network_stream.get_extra_info("server_addr")`) and refuses
    private peers, which defeats DNS rebinding
  - uses `User-Agent` = `settings.http_user_agent`
- **Resume:**
  - The partial file is `work/dl_<sha1(normalised url)[:16]>.part`.
  - A follow-up request sends `Range: bytes=N-`. The response must be 206 with a Content-Range starting at N;
    otherwise the partial file is discarded and the download restarts.
  - 416 means a stale partial file; it is discarded and the download restarts.
  - Data is written as it arrives (`iter_bytes()` with no chunk size). **Session-2 fix:** it previously buffered
    1 MB, so bytes received before a drop were lost.
- **Errors:**
  - 429, 5xx, network drops and short bodies raise TransientError. The job retries and resumes; `download_url`
    jobs get max_attempts 6.
  - 401/403 raises `access_denied`, whose message mentions logins and `HTTP_USER_AGENT`.
  - 404 raises `not_found`; text/html and other non-video types raise `not_video`.
  - Size above `MAX_UPLOAD_MB` raises `too_large`, checked against the header and while streaming.
- **The yt-dlp path** (only for permitted or allowlisted domains):
  - format `bv*[height<=1080]+ba/b`, mp4 merge, noplaylist, no cookies, 5 retries, `continuedl`, 30 s socket
    timeout, 1 s sleep between requests, `max_filesize`, the custom User-Agent
  - error mapping: `drm`, `unsupported_source`, `access_restricted` (login, private, members-only, age), or
    transient
- **After the download**, `register_file(source_type='url')` records the retrieval metadata. If permission was
  used, it is appended to `rights_attestation`.

### 7.3 Pending sources ("link + file")
- `POST /import/url` with a `link_only` decision calls `create_pending_source()`. Entries are deduplicated by
  normalised URL while awaiting a file. The message tells the owner to attach the file or save it into the inbox
  with `src-<id>` in its name.
- `attach_file_to_pending()` registers the file with `source_type='linked_file'` and `source_url` = the
  pending URL. `rights_attestation` starts "Owner-supplied file for <url> (not downloaded by this tool)", and
  `retrieval.method` is `owner_supplied_file`.
- UI:
  - Import page → "Pending sources", with an upload form per item (`pending_id`) and a Remove button.
  - Settings → "Source permissions", with add and remove, and a required confirmation that the owner holds the
    permission.

### 7.4 Inbox watch folder (inbox.py)
- `InboxWatcher.scan_once()` runs every 10 s, started in the app lifespan.
- Only video extensions are considered. A file is imported once its size and mtime are unchanged across two
  scans and its mtime is at least 5 s old.
- `src-(\d+)` in the file name attaches the file to that awaiting pending source; otherwise
  `source_type='inbox'`.
- Duplicates are moved to `inbox/duplicates`; failures to `inbox/failed`, with a `<file>.error.txt`.
  **Owner files are never deleted.** Session-2 fix: `register_file` had deleted duplicate source files located
  anywhere under `data/`; it now deletes only temporary copies inside `work/`.

---

## 8. Transcription (transcribe.py)

- `read_wav_range(path, start, end)` reads only that range of the 16 kHz mono s16 WAV into float32 using the
  stdlib `wave` module. **Never pass file paths to `WhisperModel.transcribe`**: PyAV 19 rejects faster-whisper's
  `metadata_errors=` keyword. That was a session-1 CI-only bug.
- `plan_chunks(path, chunk_seconds=1200, search_seconds=20)`:
  - a single chunk if the total is at most 1.25 × the chunk length
  - otherwise each boundary is cut at the minimum-energy 0.25 s frame within ±20 s of the target
- `Transcriber` API: `prepare()` returns the load seconds; `transcribe_range(audio, start, end, language,
  on_progress)` returns segments with absolute times; `transcribe()` covers the full range.
- `WhisperTranscriber`:
  - auto model (§14)
  - `download_root=data/models`
  - `cpu_threads` = `WHISPER_CPU_THREADS`, or automatic: half the logical CPUs if there are 8 or more,
    otherwise all of them
  - a model cache shared across jobs
  - word timestamps, Silero VAD, configurable beam (default 5), `condition_on_previous_text=False`
  - CUDA falls back to CPU int8 at load time **and** at inference time (missing cuBLAS/cuDNN)
  - `_add_nvidia_dll_dirs()` exposes the pip CUDA wheel DLLs on Windows
  - the language detected on the first chunk is reused for later chunks
- `StubTranscriber` is **TEST-ONLY**: it returns fixture segments in range, its label is "TEST STUB (not real
  transcription)", and it is selected only with `CLIPENGINE_TRANSCRIBER=stub`.

---

## 9. Excerpt selection (segment.py)

- **Sentences:**
  - words are flattened from segments; segments without timings are spread evenly at prob 0.6
  - splits happen after `. ? !` (ignoring trailing quotes and brackets, and abbreviations such as Mr/Dr/e.g.),
    or on gaps over 1.6 s
- **Bounds:** `effective_bounds()` relaxes min, max and target for recordings shorter than 2.5 × min.
- **Candidates:** every whole-sentence window between min and max that ends on terminal punctuation. If there
  are none, the minimum is halved (at least 5 s).
- **Score (v2):**

  ```
  0.36·substance + 0.10·qa + 0.22·clarity + 0.10·pauses + 0.12·fit + 0.10·end_clean
  − start_pen − 1.5·filler − procedural_weight·procedural + topic_bonus
  ```

  - substance: content-word ratio and `CLAIM_MARKERS`
  - qa: a question followed by at least 2 sentences
  - clarity: mean word probability
  - pauses: before and after, capped at 0.8 s
  - start_pen: 0.35 for a dangling conjunction, 0.12 for a starting pronoun
  - procedural: hits of `PROCEDURAL_MARKERS` (greetings, mic checks, next item, breaks, housekeeping, minutes,
    apologies…), normalised
  - topic_bonus (0.05): the opening matches `TOPIC_START_MARKERS`, or the pause before is at least 0.9 s
- **Profiles:**
  - `v1`: the original, with no procedural or topic terms, min_gap 1 s and no floor
  - `v2` (default): procedural_weight 0.6, topic_bonus 0.05, min_gap 5 s, relative_floor 0.6 × the best score,
    absolute_floor 0.25
- **Choice:** greedy over the strong candidates, with no overlap within min_gap and content-word Jaccard of at
  most 0.45. Weak candidates are **never used as padding**; the report note explains a shortfall. Final clips are
  sorted by start time.
- **LLM re-ranker (optional, Ollama):**
  - receives a pool of up to 3N diverse candidates as numbered texts
  - only integer indices within the pool are accepted
  - summaries are stripped of quote characters and capped at 400 characters, and labelled machine
    interpretation
  - any error falls back to the heuristic ranking
  - **It was never evaluated**, because Ollama was unavailable.
- **Context:** sentences within `CONTEXT_SECONDS` (25 s) before and after, or the 2 nearest if none fall in that
  window.
- **Evaluation** (`make_longform_sample.py` + `evaluate_selection.py`). Metrics:
  - substantive share and procedural share of clip duration
  - clean_start (no dangling first word), para_start (within 0.6 s of a paragraph start), terminal_end
  - topic_diversity (distinct dominant topics / clips)
  - runtime

| Transcript | Profile | Clips | Substantive | Procedural | Clean start | Topic diversity |
|---|---|---|---|---|---|---|
| **Real Whisper**, 114 min | v1 | 6 | 0.80 | 0.118 | 1.0 | 0.833 |
| **Real Whisper**, 114 min | v2 | 6 | **0.937** | **0.0** | 1.0 | 0.833 |
| Stub, 114 min | v1 / v2 | 6 / 6 | 0.822 / **0.938** | 0.108 / **0.0** | 1.0 | 0.833 |
| Stub, 58 min | v1 / v2 | 6 / 6 | 0.81 / **0.939** | 0.115 / **0.0** | 1.0 | 0.833 |
| Stub, 9 min | v1 / v2 | 6 / 6 | 0.768 / **0.916** | 0.154 / **0.016** | 1.0 | 0.833 / **1.0** |

**Caveat:** the sample text is template-generated and the scorer uses similar cues, so compare profiles, not
absolute levels. On the 90 s acceptance sample, v2 picks one 60 s clip (8.0–67.8 s, skipping the "Good evening"
opener), where v1 picked two.

---

## 10. Long-form evidence (real Whisper)

CI dispatch run #7 `43610890294`; `base.en`, CPU int8; GitHub Ubuntu runner with 4 vCPU and 16 GB; input
`longform_minutes=120`, which produced a 114.3-min sample.

| Measure | Result |
|---|---|
| Chunks | 6 (about 20 min each) |
| Crash test | Worker **hard-killed** after chunk 1 was checkpointed. The restart logged "resuming after 1 completed chunk(s)" and "Reusing extracted audio". Nothing was lost or duplicated. |
| Speed | The remaining ~94 min were transcribed in 306 s, about **18× real time**, so roughly 3–4 min per recorded hour on that runner |
| Model load | 0.5 s (cached after the first run) |
| Peak RSS | **1.6 GB**, model included |
| Peak work-directory disk | 0.22 GB; **0 MB left** afterwards |
| Selection | 3.2 s (5,679 windows scored) |
| Clips | 6 rendered, 7.6–13.3 s each; 0 failed jobs |
| Vocabulary recall vs the script | 97.7% |
| Stub-transcript pipeline in the sandbox (58 min) | Selection 5.9 s (13,398 windows); renders 12–16 s each; peak RSS 123 MB without Whisper |
| **Not measured** | `small`/`medium` models, GPU, 3-hour recordings, the owner's hardware |

The speed metric was corrected in `ce0ba89`: it now divides only the audio transcribed in that run
(`audio_transcribed_this_run_s`) by the transcription time.

---

## 11. Rendering (render.py)

- **Phrases:** at most 26 characters per line and 2 lines. A phrase breaks on a gap over 0.6 s, a duration over
  3.2 s or a sentence end. Gaps under 0.35 s are closed, and each phrase is shown for at least 0.8 s. Times are
  relative to the clip.
- **Layout:** for landscape sources, captions sit below the picture (ASS alignment 8, MarginV = foreground
  bottom + 70). Otherwise they sit in the lower third (alignment 2, MarginV = 16% of H).
- **ASS styles:**
  - Caption: Arial 64 bold, white, 4 px outline, 1 px shadow.
  - Label: 32 px at the top, showing `Excerpt · <title> · m:ss` for provenance.
  - Braces are escaped to `( )`.
- **FFmpeg** runs with `cwd=` the clip directory and the relative `captions.ass`, which avoids Windows path
  escaping:
  - Video: split. Background: scale to 270x480, crop, boxblur 12, eq brightness −0.10 / saturation 0.85, scale
    up to 1080x1920. Foreground: scale to fit (lanczos). Overlay centred, format yuv420p, burn in the ASS.
  - Audio: highpass 80 Hz, loudnorm I=−16 TP=−1.5 LRA=11, aresample 48000.
  - Encoding: input-side `-ss`/`-t` seek; libx264 (preset veryfast, CRF 21, high profile) at 30 fps; AAC 160k;
    `+faststart`. Output goes to `clip.tmp.mp4`, then is renamed to `clip.mp4`.
- **Thumbnail:** the midpoint frame at 540 px wide.
- **Verification:** the output must be exactly 1080×1920 with a non-zero duration.

---

## 12. Publishing

### 12.1 PublishService (publishing/service.py)

- **States** (`base.STATES`): queued, uploading, processing, retrying, awaiting_connection, **delivered_inbox**
  (TikTok confirmed), **published** (confirmed), failed, cancelled. `CONFIRMED_STATES` are delivered_inbox and
  published. The dashboard also shows "Awaiting editorial review", "Needs rights/flag decision" and "Ready to
  send".
- **`request(clip_id, platform, again=False)`** refuses if:
  - the editorial gate fails: not approved, rights not in RIGHTS_OK, an unacknowledged flag, or the file is
    missing
  - the platform is not connected (NotConfigured)
  - an attempt is active ("already in progress")
  - an attempt was already confirmed and `again` is not set ("already sent")

  Otherwise it inserts `publish_attempts` (queued) and enqueues `publish_clip` with dedupe `publish:<aid>` and
  max_attempts 8.
- **The job** re-checks the gate and dispatches by platform.
  - NotConfigured sets `awaiting_connection` (permanent).
  - TransientError sets `retrying`, and the job retries with backoff.
  - PublishError sets `failed`.
  - The failure hook marks the attempt failed and revokes public tokens.
- **TikTok stages:** (none) → init (`remote_id`=publish_id, stage `initiated`) → upload → stage `uploaded`,
  status processing → poll up to 40 × 3 s.
  - SEND_TO_USER_INBOX gives `delivered_inbox`; PUBLISH_COMPLETE gives `published`; both are confirmed.
  - FAILED gives failed.
  - Still processing raises TransientError; the retry **only polls** and never re-uploads.
  - Interrupted before stage `uploaded`: a fresh init.
- **Instagram stages:** (none) → container (`container_id`) → upload (facebook mode) → `uploaded`/processing →
  poll status_code (60 × 5 s): FINISHED gives `ready`; EXPIRED resets and recreates; ERROR fails.
  - Before calling `media_publish`, the stage is set to `publish_requested`, then publish and fetch the
    permalink to reach `published`.
  - **On resume at `publish_requested`:** if the container's status_code is PUBLISHED, it is recorded as
    published ("not published twice"); otherwise publishing is retried.
- **One-time public links** (Instagram Login mode): `_public_url()` stores a token in `public_media_tokens`
  (2-hour TTL) and returns `PUBLIC_MEDIA_BASE_URL/public/media/<token>.mp4`. Tokens are revoked on a terminal
  state and expired ones are purged.

### 12.2 TikTok (publishing/tiktok.py) — Content Posting API, upload to inbox

- **OAuth:** `https://www.tiktok.com/v2/auth/authorize/` with scopes `user.info.basic,video.upload`, plus state.
  - `TIKTOK_APP_TYPE=desktop` (default): PKCE with **code_challenge = lowercase hex SHA-256 of a 64-character
    verifier**, method S256. This is TikTok's desktop variant. Session 2 fixed it; session 1 used base64url,
    which was wrong.
  - `web`: no PKCE.
  - Token endpoint: `POST https://open.tiktokapis.com/v2/oauth/token/` (form). Refresh uses
    `grant_type=refresh_token`. Raises if the scope lacks `video.upload`.
- **API calls:** `POST /v2/post/publish/inbox/video/init/` (FILE_UPLOAD with video_size, chunk_size,
  total_chunk_count), then PUT chunks to `upload_url` with Content-Range, then
  `POST /v2/post/publish/status/fetch/`.
- **Chunking:** under 5 MB, a single chunk; otherwise 10 MB chunks (clamped to 5–64 MB),
  total = floor(size / chunk), and the final chunk absorbs the remainder (128 MB maximum).
- **Validation before upload:** H.264 or HEVC, AAC, 3–600 s, at most 4 GB, at least 360 px.
- **Error mapping:**
  - transient (rate_limit_exceeded, internal_error, server_error, 429, 5xx, network) raises TransientError
  - `access_token_invalid` refreshes once and retries; other auth codes raise NotConfigured
  - explained codes: `spam_risk_too_many_pending_share`, `spam_risk_user_banned_from_posting`,
    `unaudited_client_can_only_post_to_private_accounts`, `invalid_file_upload`
  - a local limit allows 6 init calls per minute
- **Default redirect:** `http://127.0.0.1:8765/oauth/tiktok/callback`. Whether the portal accepts loopback
  addresses is **unverified**; the fallback is a Web app with a Tailscale HTTPS redirect.

### 12.3 Instagram (publishing/instagram.py)

| | `INSTAGRAM_AUTH_MODE=facebook` (default) | `INSTAGRAM_AUTH_MODE=instagram` |
|---|---|---|
| Login | Facebook Login for Business: `https://www.facebook.com/{v}/dialog/oauth`, scopes `instagram_basic,instagram_content_publish,pages_show_list,pages_read_engagement,business_management` | Instagram Login: `https://www.instagram.com/oauth/authorize`, scopes `instagram_business_basic,instagram_business_content_publish` |
| Token | code → `graph.facebook.com/{v}/oauth/access_token` → long-lived token via `fb_exchange_token`; `me/accounts{instagram_business_account}` | `POST api.instagram.com/oauth/access_token` → `graph.instagram.com/access_token?grant_type=ig_exchange_token`; `me?fields=user_id,username`; automatic `ig_refresh_token` when fewer than 10 days remain and the token is at least 24 h old |
| Requirement | Instagram professional account **linked to a Facebook Page** | No Page; **`PUBLIC_MEDIA_BASE_URL` (https)** that Meta can reach |
| Video transfer | Resumable: `POST /{ig}/media` (REELS, `upload_type=resumable`) → POST the bytes to `rupload.facebook.com/ig-api-upload/{v}/{container}` with headers `Authorization: OAuth`, `offset: 0`, `file_size` | `video_url` = one-time token link |
| Config | `META_APP_ID`/`SECRET`/`REDIRECT_URI` | `INSTAGRAM_APP_ID`/`SECRET`/`REDIRECT_URI` + `PUBLIC_MEDIA_BASE_URL` |

- **Why facebook mode is the default:** Meta documents resumable upload only for Facebook Login; Instagram Login
  requires a public URL. Facebook mode keeps the PC fully private, and linking a free Facebook Page takes
  minutes.
- **Validation:** H.264 or HEVC, AAC, 3–900 s, at most 1 GB.
- **Error mapping:** 5xx, 429, `is_transient`, or codes {1, 2, 4, 17, 32, 341, 613} are transient; codes {10, 190,
  200}, 401 and 403 raise NotConfigured (reconnect); others are PublishError.
- **No drafts exist in the API.** Publishing is public, so the UI requires typing `PUBLISH`. Meta's limit is 100
  API posts per 24 hours (`publishing_quota()`).

### 12.4 Setup checklist (setup_status.py, Settings page)

Statuses: Ready, Connected, Untested, Not configured, Owner action required, Awaiting platform approval, Failed.
Items:

1. Python and FFmpeg
2. Whisper model (Failed if the stub is active; Ready if downloaded; Untested otherwise)
3. A real transcription has succeeded (uses `verified.transcription`)
4. Source importing
5. TikTok credentials
6. TikTok OAuth (uses `oauth_error.tiktok`, which maps to Awaiting platform approval or Failed)
7. Instagram credentials (lists the missing variables per mode)
8. Instagram OAuth (shows an "expiring soon" warning)
9. Dashboard ready for review (worker alive, rendered clips)
10. Platform readiness (Ready only after a confirmed real transaction)

Each item shows a detail line and a next action. A "Download Whisper model now" button queues a `download_model`
job. Tested in `test_setup_checklist_is_honest`.

### 12.5 Web app (web/app.py) — middleware and routes

- **Middleware:**
  - `/public/media/*` passes through; only valid, unexpired tokens are served.
  - **Without `DASHBOARD_PASSWORD`, only Host headers of 127.0.0.1, localhost or ::1 are answered**, plus
    `testserver` for tests. This blocks DNS rebinding and accidental exposure.
  - POST requests whose Origin or Referer host differs from Host get a 403 (CSRF).
  - With a password, the `ce_auth` cookie (HMAC of the password with `data/secrets/session_key`) is required
    everywhere except `/static`, `/login`, `/healthz` and `/oauth/*`.
- **Login:** at most 5 failures per IP per 10 minutes, then 429. The password is compared with `hmac` on bytes.
  The cookie is httponly, SameSite=strict, 30 days, and **Secure over HTTPS** (scheme or X-Forwarded-Proto).
- **Routes:**
  - `/` (dashboard), `/partials/jobs`, `/partials/counts`
  - Import:
    - `/import`
    - `POST /import/upload` (8 MB streaming chunks, MAX_UPLOAD_MB cap, `pending_id` support, JSON for XHR)
    - `POST /import/url` (classify → pending, refuse or queue; attestation required; URL dedupe)
    - `POST /sources/{id}/cancel`
  - Recordings:
    - `/recordings`, `/recordings/{id}` (processing stats, selection note, provenance type)
    - `/recordings/{id}/media`, `/recordings/{id}/transcript.txt`
    - `POST /recordings/{id}/reanalyse`
    - `POST /recordings/{id}/delete` (exact-title confirmation)
  - Review and clips:
    - `/review?status=…` (publishing badges for approved clips)
    - `/clips/{id}` (publishing panel with states, Send again, Instagram PUBLISH confirmation)
    - `POST /clips/{id}/approve | reject | regenerate | rights | acknowledge (note ≥ 15 chars) | description`
    - `/clips/{id}/video | thumb | download | captions.srt | description.txt` (Range gives 206)
    - `POST /clips/{id}/publish/{platform}` (`confirm`, `again`)
  - Jobs: `/jobs`, `/jobs/{id}`, `POST /jobs/{id}/retry | cancel`
  - `/search`
  - Settings:
    - `/settings` (checklist, hardware, guidance, storage, connections, source permissions, phone-access guide)
    - `POST /settings/download-model`
    - `POST /settings/permissions` and `/settings/permissions/{id}/delete`
  - OAuth: `/connect/{platform}`, `POST /disconnect/{platform}`, `/oauth/{platform}/callback` (stores errors in
    the key-value table; state is in memory with a 15-minute TTL)
  - `/public/media/{token}.mp4`
  - JSON: `/api/status`, `/api/recordings/{id}`, `/healthz` (returns `app: clipengine`)
- Redirect query strings use `urlencode` (session-2 fix).

---

## 13. Reliability, retention, logging

- Jobs survive restarts (`recover()`); transcription resumes per chunk; downloads resume through Range;
  publishing resumes per stage.
- Manual Retry and Cancel are on the Jobs page. Retry resets the clip or recording status.
- **Maintenance thread** (first pass after 30 s, then hourly):
  - removes work files older than 24 h, only when no job is running
  - removes rejected-clip renders after `DELETE_REJECTED_RENDERS_AFTER_DAYS` (14); the database rows are kept
  - deletes originals only if `DELETE_ORIGINALS_AFTER_DAYS` > 0 (default **0 = never**), only for ready
    recordings, and never while any clip is pending, rendering or ready
- `storage_report()` warns above `STORAGE_WARNING_GB` (50) or when free disk is under 5 GB.
- Logs are structured JSONL; each job also has its own log in the database. The launcher prints the log path on
  failure.

---

## 14. Configuration (.env) and hardware auto-selection

| Group | Variables (defaults) |
|---|---|
| Server | `CLIPENGINE_PORT` (8765), `CLIPENGINE_HOST` (127.0.0.1), `DASHBOARD_PASSWORD`, `OPEN_BROWSER` (1), `CLIPENGINE_DATA_DIR` |
| Transcription | `WHISPER_MODEL` (auto), `WHISPER_DEVICE` (auto), `WHISPER_COMPUTE_TYPE` (auto), `TRANSCRIBE_LANGUAGE`, `VAD_FILTER` (1), `WHISPER_CPU_THREADS` (0 = auto), `WHISPER_BEAM_SIZE` (5), `TRANSCRIBE_CHUNK_SECONDS` (1200), `INSTALL_CUDA_LIBS` (0) |
| Selection | `CLIPS_PER_RECORDING` (6), `CLIP_MIN_SECONDS` (30), `CLIP_MAX_SECONDS` (90), `CLIP_TARGET_SECONDS` (55), `CONTEXT_SECONDS` (25), `LLM_PROVIDER` (auto), `OLLAMA_URL`, `OLLAMA_MODEL` (llama3.2:3b) |
| Render | `VIDEO_CRF` (21), `X264_PRESET` (veryfast), `CAPTION_FONT` (Arial), `CAPTION_FONT_SIZE` (64) |
| Ingest | `ALLOWED_URL_DOMAINS` (empty), `MAX_UPLOAD_MB` (8192), `INBOX_DIR` (data/inbox), `HTTP_USER_AGENT` |
| Jobs and retention | `JOB_MAX_ATTEMPTS` (3), `DELETE_REJECTED_RENDERS_AFTER_DAYS` (14), `DELETE_ORIGINALS_AFTER_DAYS` (0), `STORAGE_WARNING_GB` (50) |
| TikTok | `TIKTOK_APP_TYPE` (desktop), `TIKTOK_CLIENT_KEY`, `TIKTOK_CLIENT_SECRET`, `TIKTOK_REDIRECT_URI` (http://127.0.0.1:8765/oauth/tiktok/callback) |
| Instagram | `INSTAGRAM_AUTH_MODE` (facebook), `META_APP_ID`, `META_APP_SECRET`, `META_REDIRECT_URI` (http://localhost:8765/oauth/instagram/callback), `META_GRAPH_VERSION` (v23.0), `INSTAGRAM_APP_ID`, `INSTAGRAM_APP_SECRET`, `INSTAGRAM_REDIRECT_URI`, `PUBLIC_MEDIA_BASE_URL` |
| **TEST-ONLY** | `CLIPENGINE_TRANSCRIBER=stub`, `CLIPENGINE_STUB_TRANSCRIPT`, `ALLOW_PRIVATE_URLS=1`, `CLIPENGINE_REAL_WHISPER=1`, `CLIPENGINE_LIVE_NET=1`, `PW_CHROMIUM`, `CLIPENGINE_NONINTERACTIVE=1` |

**Automatic Whisper model choice:**

- CUDA with ≥10 GB VRAM: large-v3 (float16)
- CUDA with ≥5 GB: medium (float16)
- other CUDA GPUs: small (float16)
- CPU with ≥8 threads and ≥15 GB RAM: small (int8)
- CPU with ≥4 threads and ≥7 GB RAM: base (int8)
- anything smaller: tiny (int8)

`.en` is appended for tiny, base, small and medium when the language is `en`.

**Launcher (`Start-Clipping-Engine.cmd`):**

1. Python: tries `py -3.12`, `-3.13`, `-3.11`, `-3.10`, then a `python` in the 3.10–3.13 range. If none is
   found, offers `winget install Python.Python.3.12 --scope user`.
2. Recreates `.venv` if it was built with an unsupported Python, then runs `scripts\ensure_deps.py`.
3. FFmpeg: looks in `tools\ffmpeg\bin`, then the WinGet Links folder. If it is missing, offers
   `winget install Gyan.FFmpeg`.
4. Copies `.env.example` to `.env` if needed.
5. Runs `.venv\Scripts\python.exe -m clipengine %*`. On failure it shows the log path and the README
   Troubleshooting pointer, then pauses (unless `CLIPENGINE_NONINTERACTIVE=1`).

**Phone access:**

- Recommended: Tailscale — `tailscale serve --bg 8765` plus `DASHBOARD_PASSWORD`; open
  `https://<pc>.<tailnet>.ts.net`. The app stays bound to 127.0.0.1.
- Alternative: home Wi-Fi with `CLIPENGINE_HOST=0.0.0.0` and a password (unencrypted).
- Never port-forward the dashboard.

---

## 15. Tests (what each proves)

Run from `clipping-engine/`:

```
python -m pytest -v                                  # offline suite
CLIPENGINE_REAL_WHISPER=1 python -m pytest -v        # + real Whisper (needs Hugging Face)
CLIPENGINE_LIVE_NET=1 python -m pytest -v            # + live public-domain URL import
python scripts/acceptance_test.py [--stub] [--keep DIR]
python scripts/longform_benchmark.py --minutes 60 [--stub] [--crash-test] --out bench
python scripts/evaluate_selection.py bench/longform_60min.timing.json [--transcript bench/transcript_segments.json]
PW_CHROMIUM=/opt/pw-browsers/chromium                # sandbox only, for the acceptance browser check
```

| File (count) | Proves |
|---|---|
| test_jobs.py (7) | dedupe, success, transient retry then failure, transient then success, permanent failure plus hook, crash recovery, retry and cancel |
| test_segment_safety.py (8) | sentence splitting; bounded/clean/non-overlapping candidates with real timestamps only; dangling-start penalty; reranker can't invent and bad output falls back; context; short-recording bounds; safety categories and watchlist; low-confidence words |
| test_render_ingest.py (8) | caption phrasing, SRT/ASS escaping, layout, URL policy, safe_filename, register and duplicate detection with provenance, non-video rejected, real 1080×1920 h264/aac render |
| test_ingest_sources.py (9) | classification and SSRF rules (private, mixed DNS, metadata IP, IPv4-mapped IPv6, ports, credentials, schemes); permission gating; **local-server** direct download with interruption and resume (byte-identical); errors (not_video, not_found, 503, too_large); redirect; **peer check defeats DNS rebinding**; full URL import job with provenance and dedupe; pending source attach; inbox watcher (stability, `src-N`, duplicates moved and not deleted) |
| test_pipeline_web.py (8) | **stub transcript**: full pipeline and FTS; no duplicates on rerun; regenerate with new times doesn't re-transcribe; description preserved on plain regenerate; web flow (Range 206, approve, download, SRT, search, settings, publish blocked without rights, cross-origin 403, persistence); flag needs a note; upload, duplicate, bad type, Rumble link becomes a pending source; password login |
| test_publishing.py (14) | **MOCKED HTTP**: token store; chunk plan; TikTok hex PKCE (desktop) and none (web); not-configured; TikTok inbox flow; error mapping (rate limit, pending shares, token refresh); video validation; service gate, connection and duplicate guard; TikTok job resumes by polling only (one init, one upload); failure reported; Instagram facebook mode publishes once; **lost publish response never double-publishes**; Instagram Login one-time link and revocation; editorial gate |
| test_reliability.py (4) | chunk cut at a quiet point; **chunked transcription resumes after a simulated crash** with identical output and cleanup; v2 beats v1 on a labelled 12-min sample; no padding with weak clips |
| test_security_setup.py (6) | localhost-only without a password; login rate limit and Secure cookie; honest checklist (no false Connected; stub shows Failed); public media tokens expire and don't open other routes; v1→v2 migration; port helpers |
| test_hardware.py (2) | model-selection rules; WAV loader |
| test_whisper_real.py (1) | **REAL** faster-whisper on the sample (with `CLIPENGINE_REAL_WHISPER=1`) |
| test_live_network.py (1) | **LIVE** import of a public-domain NASA video through Wikimedia `Special:FilePath` with all protections on (with `CLIPENGINE_LIVE_NET=1`). A remote HTTP 403 on every candidate becomes an expected failure with the reason; any other failure fails. |

**Acceptance test steps:**

1. start the real server
2. HTTP upload
3. the transcript contains at least 3 of the expected words
4. candidates exist
5. the video returns 200, Range returns 206, and ffprobe reports 1080×1920 h264 + aac
6. the review HTML embeds the video, and a Playwright 390×844 mobile viewport loads its metadata (screenshots
   taken)
7. approve and reject
8. download is an attachment
9. stop and restart; statuses, transcript and download persist

---

## 16. Bugs found and fixed (all have regression tests)

**Session 1:**

1. PyAV `metadata_errors` incompatibility: samples are now passed to Whisper.
2. Windows cp1252 decoding in tests: UTF-8 is now explicit.
3. Regenerate overwrote the owner's description.
4. `job_logs` insert crash (this table has no timestamp columns).
5. Port check used `bind`, which failed falsely during TIME_WAIT; it now uses `connect_ex`.

**Session 2:**

6. `Database.insert` assumed every table has `updated_at`. It crashed on checkpoints and was caught by the
   long-form benchmark.
7. The downloader lost the data before a dropped connection (1 MB buffering), so resume was impossible.
8. A duplicate import deleted inbox files (anything under `data/`).
9. TikTok desktop PKCE used base64url instead of hex.
10. Import redirect messages weren't URL-encoded.
11. The speed metric overstated speed after a resume.
12. Settings overflowed horizontally on phones (now 390 px wide, verified with a screenshot).

---

## 17. Remaining engineering issues

1. **Live TikTok and Instagram transactions are unverified.** Everything is mocked until the owner connects real
   accounts. Unknowns:
   - TikTok portal acceptance of the `127.0.0.1` loopback redirect
   - inbox-upload limits for unaudited apps
   - real Meta error codes
   - TikTok chunk boundaries near 5 MB and 64 MB
2. **Rumble live retrieval has never been executed.** It is permission-gated by design.
3. **Wikimedia refused the Windows CI runner (403).** The owner may need `HTTP_USER_AGENT` with contact details.
4. **The Ollama re-ranker hasn't been evaluated.**
5. **Peak RAM is 1.6 GB with `base`**; `small` needs more. Use `tiny.en` or `base.en` on PCs with 4 GB of RAM.
   `small`, GPU and 3-hour runs are unmeasured.
6. **The OAuth state lives in memory**; a restart during sign-in means reconnecting (minor).
7. **python-multipart spools uploads to temp**, briefly doubling disk use. The inbox avoids this.
8. Optional subject-tracking crop and speaker diarisation were **intentionally not built**.

---

## 18. Owner action checklist (prioritised; present it once, at the end)

1. **C1 — Install and launch on the Windows PC (mandatory).**
   - Download the repository and double-click `clipping-engine\Start-Clipping-Engine.cmd`.
   - Accept the Python and FFmpeg winget prompts.
   - Settings → Setup checklist → **Download Whisper model now**.
   - *Return:* the console "Computer:" line and every checklist status.
2. **C2 — First real recording (mandatory).**
   - Upload a short authorised recording, or save it into the inbox folder.
   - *Return:* whether "A real transcription has succeeded" shows Ready, the processing stats line, and whether
     the clips are usable.
3. **C3 — TikTok (may take days for app review).**
   - Create or sign in to the TikTok account; set it to Public if required.
   - At [developers.tiktok.com](https://developers.tiktok.com), Manage apps → connect a **Desktop** app.
   - Add Login Kit and the Content Posting API, with scopes `user.info.basic` and `video.upload`.
   - Register the redirect `http://127.0.0.1:8765/oauth/tiktok/callback`. If loopback is rejected, use a Web app
     with a Tailscale HTTPS redirect and set `TIKTOK_APP_TYPE=web`.
   - Add yourself as a tester, or submit for review.
   - Put `TIKTOK_CLIENT_KEY` and `TIKTOK_CLIENT_SECRET` in `.env` (never in chat). Restart, then Settings →
     Connect TikTok.
   - Approve a clip, set its rights, and click **Send to my TikTok inbox**.
   - *Return:* whether the clip shows "Delivered to TikTok inbox (confirmed)" and appears in the app, or the
     exact error.
4. **C4 — Instagram (Meta review may delay this).**
   - In the Instagram app, Settings → Account type and tools → **Creator** account; link a Facebook Page
     ([create one](https://www.facebook.com/pages/create) if needed).
   - At [developers.facebook.com/apps](https://developers.facebook.com/apps), create an app with Facebook Login
     for Business and the Instagram Graph API.
   - Request `instagram_basic`, `instagram_content_publish`, `pages_show_list`, `pages_read_engagement` and
     `business_management`.
   - Add the redirect `http://localhost:8765/oauth/instagram/callback`. Keep the app in development mode with
     yourself under App roles.
   - Put `META_APP_ID` and `META_APP_SECRET` in `.env`, then Connect Instagram.
   - *Return:* the checklist status.
   - **The first public publish only on the owner's explicit instruction.**
5. **C5 — Optional.**
   - Tailscale for secure phone access ([download](https://tailscale.com/download)).
   - Rumble's written permission, recorded in Settings.
   - `HTTP_USER_AGENT` with contact details.
   - `INSTALL_CUDA_LIBS=1` for an NVIDIA GPU.
   - Approve the cloud-session network change (Hugging Face) from claude.ai on desktop, only if real
     transcription is wanted inside the sandbox.

**Recommended order:** C1, then C2, then start C3 (because of review time) and do C4 in parallel.

**Readiness verdict:**

1. **Local clipping and review:** ready for routine use (verified in CI; not yet run on the owner's PC).
2. **URL ingestion:** ready (direct, permitted, inbox, link + file); Rumble only through link + file or a
   recorded permission.
3. **TikTok:** built and mock-tested, not connected.
4. **Instagram:** built and mock-tested, not connected.
5. **Overall:** ready for local editorial work once installed; publishing awaits the owner's actions.

---

## 19. Gotchas — never break these

- Keep `read_wav_range` and the NumPy input to Whisper; never pass file paths (PyAV).
- The port check uses `connect_ex`, never `bind`.
- Always use `encoding="utf-8"` for text I/O (Windows defaults to cp1252).
- Run FFmpeg with `cwd=` the clip directory and **relative** subtitle filenames.
- Keep **CRLF** line endings in `Start-Clipping-Engine.cmd`.
- `Database.insert` sets timestamps only on columns that exist. Add new columns through `MIGRATIONS`.
- `register_file` may delete a duplicate source **only inside `work/`**; owner files are never deleted.
- Never relax `classify_url` for restricted platforms without a recorded written permission. Never add
  cookie or login support.
- Publishing: never report success without a platform-confirmed state. Keep the `publish_requested` check before
  any Instagram re-publish. Keep the editorial gate mandatory.
- Never move a stub or mock result into "Ready" or "Connected"; keep the TEST-ONLY labels.
- When changing selection or rendering, rerun `pytest`, `scripts/acceptance_test.py --stub` and
  `scripts/evaluate_selection.py`, then verify in CI.
- After pushing, check CI. To rerun the long-form evidence, dispatch the workflow with `longform_minutes`.

## 20. Sources used for platform decisions

- Rumble Terms of Use: <https://rumble.com/s/terms> (automated access requires Rumble's prior written permission)
- TikTok Login Kit for Desktop (hex SHA-256 PKCE): <https://developers.tiktok.com/doc/login-kit-desktop/>
- TikTok Content Posting API, upload: <https://developers.tiktok.com/doc/content-posting-api-reference-upload-video>
- TikTok Get Video Status: <https://developers.tiktok.com/doc/content-posting-api-reference-get-video-status>
- Meta Content Publishing (hosts per login type; resumable upload only for Facebook Login):
  <https://developers.facebook.com/documentation/instagram-platform/content-publishing.md>
- Meta resumable uploads: <https://developers.facebook.com/documentation/instagram-platform/content-publishing/resumable-uploads>
