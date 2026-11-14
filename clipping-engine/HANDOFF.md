# HANDOFF PROMPT — Local AI Video Clipping & Editorial Review Engine

> Paste this whole document into a new Claude Code session, or tell the agent to read
> `clipping-engine/HANDOFF.md`. It is self-contained: it records what was asked, what was built, how
> it works, what was verified (and how), what is outstanding, and the rules the next engineer must
> keep following.

---

## 0. Your role and the standing instructions (still in force)

You are the lead engineer continuing a **completed, working, CI-verified** project. The owner's original
one-shot instruction remains binding. Its essentials:

- **Deliver a working local application, not proposals.** Build it, run the tests, repair failures, and only
  then report.
- **Owner-intervention protocol.** When something truly needs the owner (accounts, developer registration,
  OAuth approval, external permissions):
  1. Keep working on anything independent of it.
  2. Track the dependency.
  3. If you are blocked, give exact step-by-step instructions for the owner.
  4. Resume once the owner confirms.
- **Never request passwords, recovery codes or API secrets in chat.** Secrets go only in the owner's local
  `.env` or the OS credential store.
- **Never present simulated functionality as real.** Stubs and mocks must be labelled as such everywhere: code,
  tests and reports.
- **Never declare the project complete** while owner-dependent integrations are unfinished. Report the exact
  operational status instead.
- **No purchases, no publishing content, no registering external accounts, no changes to external services.**
- **No automated public posting** without explicit human approval, and approval alone is not sufficient (see
  the editorial gate).
- **Never bypass** authentication, DRM, access controls, copyright restrictions or platform rate limits.
  **No browser automation** to get around publishing restrictions.
- **Do not optimise for outrage.** The use case is political-media monitoring, factual reporting, documentary
  analysis and critical commentary. Context and provenance must be preserved, and the tool must not become an
  unattended promotion engine for hateful individuals or ideologies.
- **Stack constraints:**
  - Python-first, free and open source, local inference, no paid LLM or API, no new subscriptions.
  - Must support Windows with a one-click `.cmd` launcher.
  - Avoid Docker, Redis, Postgres, cloud hosting and heavy frontend frameworks unless genuinely needed.
  - Keep it modular but simple: no microservices.
- **Git:**
  - Develop on branch **`claude/optimistic-noether-k52qjf`** in **`ce-optimisation/n8n-automation-portfolio`**.
  - Push with `git push -u origin claude/optimistic-noether-k52qjf`, retrying network errors up to 4 times
    with backoff.
  - **Do not open a PR unless the owner asks.**
  - End commit messages with the session attribution lines the environment supplies.
  - Never put model identifiers in commits or code.

---

## 1. Current status in one paragraph

All code is in `clipping-engine/` (the repo root also holds unrelated n8n workflow templates; do not touch
them), plus `.github/workflows/clipping-engine.yml` at the repo root. The branch head is **`4eb9736`**. The
**final CI run (#4, run id `43600559824`) is fully green on `ubuntu-latest` and `windows-latest`**. That run
covered:

- the Windows `.cmd` launcher check
- 41/41 pytest tests, including **real faster-whisper transcription**
- the 9-step acceptance test against the real running app with **real Whisper** (`base.en`, CPU int8)

The local end-to-end workflow is complete. The **only unfinished items are owner-dependent publishing
credentials** (TikTok, Instagram) and **first launch on the owner's own PC** (only GitHub-hosted Windows runners
have run it so far).

---

## 2. Commit history on the branch

| SHA | Summary |
|---|---|
| `f95fa7a` | Initial full application, tests, launcher, README, CI workflow |
| `39e5846` | Fix: a plain "Regenerate" (unchanged timestamps) no longer overwrites an owner-edited description; adds a regression test |
| `fc51955` | Fix found **only** by CI with the real model: faster-whisper's `decode_audio` passed `metadata_errors=` to `av.open()`, which PyAV 19 rejects. Audio is now loaded with stdlib `wave` and NumPy (`transcribe.load_wav_16k`) and passed to Whisper as a float32 array, bypassing PyAV entirely |
| `4eb9736` | Test fix for Windows: read UTF-8 `.ass`/`.srt`/`provenance.json` explicitly (the default cp1252 code page broke on `·`) |

`0099d4e` (the repo's original upload) predates this work.

---

## 3. Environment facts that shaped decisions (important for the next session)

- **The build sandbox is a Linux cloud container**: 4 cores, 15 GB RAM, no GPU, Python 3.13, FFmpeg with
  libass, libx264, libflite and loudnorm. **The owner's target is a Windows PC** whose hardware has not been
  inspected. Hardware detection therefore happens **at runtime** (`clipengine/hardware.py`) and is shown on the
  Settings page.
- **This sandbox's network proxy blocks huggingface.co** (HTTP 403 on CONNECT), so the Whisper model cannot be
  downloaded here.
  - I proposed a personal environment adding `huggingface.co`, `cdn-lfs.hf.co`, `cdn-lfs-us-1.hf.co` and
    `cas-bridge.xethub.hf.co`. The request **could not be shown** because the owner's Claude mobile app version
    can't display approval prompts. Nothing was changed.
  - The owner can approve it from claude.ai on a computer or a newer app, but **it is not required**: real
    transcription is verified in GitHub Actions instead.
- `developers.tiktok.com` and `developers.facebook.com` were also unreachable from the sandbox. The API details
  were confirmed through web search of the official docs.
- **Python Playwright is installed in the sandbox.** Its browser revision didn't match the pre-installed Chromium,
  so locally use `PW_CHROMIUM=/opt/pw-browsers/chromium`. The acceptance script honours that env var. CI installs
  its own Chromium.
- Packages were installed system-wide in the sandbox with `pip install --break-system-packages`. The owner's PC
  uses `.venv`, created by the launcher.

---

## 4. Directory map (every file, with its purpose)

```
.github/workflows/clipping-engine.yml   CI: ubuntu+windows; FFmpeg install; Windows .cmd --check; pytest with
                                        CLIPENGINE_REAL_WHISPER=1 WHISPER_MODEL=base.en; acceptance test;
                                        uploads artifacts (screenshots, approved clip, results.json, server.log)
clipping-engine/
  Start-Clipping-Engine.cmd   Windows one-click launcher (CRLF line endings — keep them)
  start.sh                    macOS/Linux launcher
  requirements.txt            fastapi, uvicorn, jinja2, python-multipart, httpx, faster-whisper, yt-dlp, keyring, pytest
  .env.example                every setting, documented (copied to .env on first launch)
  .gitignore                  .env, .venv/, data/, tools/ffmpeg/, caches
  README.md                   owner-facing docs: launch, first recording, phone access, selection logic,
                              safeguards, URL policy, TikTok/Instagram setup steps, reliability, tests, troubleshooting
  HANDOFF.md                  this file
  clipengine/
    __init__.py
    __main__.py      Entry point `python -m clipengine [--check] [--no-browser]`: dependency checks, refuses
                     0.0.0.0 without DASHBOARD_PASSWORD, adds tools/ffmpeg/bin and %LOCALAPPDATA%\Microsoft\WinGet\Links
                     to PATH, JSON rotating logs (data/logs/clipengine.jsonl), port-in-use detection via
                     connect_ex (NOT bind — bind falsely failed on TIME_WAIT), opens browser, runs uvicorn
    config.py        Settings dataclass + tiny .env loader (no python-dotenv); get_settings(**overrides)
    hardware.py      detect() → OS/CPU/cores/RAM (ctypes on Windows)/CUDA count (ctranslate2)/GPU name+VRAM (nvidia-smi)/
                     ffmpeg paths; choose_whisper() → (model, device, compute_type)
    db.py            SQLite (WAL, foreign keys, busy_timeout, IMMEDIATE tx). Tables: meta, recordings,
                     transcript_segments, transcript_fts (FTS5), clips, jobs, job_logs, publish_attempts.
                     Helpers: insert/update/query/one, segments(), replace_transcript(), search_transcripts()
                     (each term quoted → no FTS injection), counts()
    jobs.py          Persistent queue: enqueue (dedupe_key + partial unique index on active jobs), claim (atomic),
                     single worker thread, TransientError (exponential backoff 15s·2^n ≤600s, up to max_attempts) vs
                     PermanentError (fail immediately), "<kind>:failed" hooks, recover() re-queues 'running' jobs at
                     startup, retry(), cancel(), drain() for tests/CLI
    media.py         ffprobe probe (handles rotation), run_ffmpeg with -progress parsing & stderr tail,
                     extract_audio → 16 kHz mono s16le WAV. CREATE_NO_WINDOW on Windows
    ingest.py        URL policy (RESTRICTED_PLATFORMS hard-refused + ALLOWED_URL_DOMAINS allowlist + attestation),
                     safe_filename, sha256 dedupe, URL normalisation/dedupe, register_file() (probe, require audio+video,
                     move into data/recordings/<ts>_<title>/source.<ext>, write provenance.json),
                     handle_download_url() (yt-dlp, ≤1080p, no cookies, DRM → permanent error)
    transcribe.py    WhisperTranscriber (auto model, download_root=data/models, model cache, CUDA→CPU fallback at
                     load AND at inference, _add_nvidia_dll_dirs() for Windows pip CUDA wheels,
                     load_wav_16k() feeds NumPy samples — DO NOT revert to passing a file path),
                     word_timestamps=True, vad_filter (Silero, bundled), beam 5, condition_on_previous_text=False.
                     StubTranscriber = TEST-ONLY, reads CLIPENGINE_STUB_TRANSCRIPT JSON; selected only via
                     CLIPENGINE_TRANSCRIBER=stub; model_label "TEST STUB (not real transcription)"
    segment.py       Sentences from word timings; candidate windows; scoring; diversity; optional Ollama re-ranker
    safety.py        Regex screen (violence_advocacy, dehumanisation, extremist_terminology, owner_watchlist from
                     data/safety_terms.txt); low_confidence_words (prob < 0.5)
    render.py        Caption phrasing, SRT/ASS writers, layout, FFmpeg filter graph, render_clip() + thumbnail + verification
    pipeline.py      Job handlers & owner actions (see §6)
    maintenance.py   Hourly housekeeping thread + storage_report()
    publishing/
      base.py        PublishResult, PublishError, NotConfigured, RIGHTS_OK, editorial_gate()
      tokens.py      TokenStore: keyring (Windows Credential Manager) or fallback data/secrets/tokens.json (chmod 600)
      tiktok.py      TikTokClient (OAuth+PKCE S256, inbox upload, chunk_plan, status polling, local 6/min limit)
      instagram.py   InstagramClient (Facebook Login for Business, resumable Reels upload, publish, quota)
    web/
      app.py         FastAPI app factory create_app(settings, start_worker=True) — all routes (§8)
      static/        htmx.min.js (1.9.12, vendored from npm — works offline), style.css (mobile-first, dark mode)
      templates/     base, login, dashboard, _counts, _jobs, import, _proc_settings, review, _clip_card,
                     _clip_status, clip, recordings, recording, jobs, job, search, settings
  scripts/
    make_sample_video.py   Lawful 90 s sample: ORIGINAL text (council bridge briefing) synthesised by FFmpeg's
                           built-in flite voice over testsrc2 1280x720; also writes <name>.timing.json
                           (exact paragraph start/end) used by the test stub
    acceptance_test.py     9-step end-to-end against the real running server (see §10). --stub for offline sandboxes
    ensure_deps.py         pip install only when requirements.txt hash (+cuda flag) changes; INSTALL_CUDA_LIBS=1
                           adds nvidia-cublas-cu12 + nvidia-cudnn-cu12==9.*
  tests/  conftest.py, test_jobs.py, test_segment_safety.py, test_render_ingest.py, test_pipeline_web.py,
          test_publishing.py, test_hardware.py, test_whisper_real.py
```

Runtime data lives under `clipping-engine/data/` (git-ignored; override it with `CLIPENGINE_DATA_DIR`):

- `clipengine.sqlite3`
- `recordings/<YYYYmmdd-HHMMSS>_<title>/source.mp4` and `provenance.json`
- `clips/recNNNNN/clipNNNNN_rN/`: `clip.mp4`, `clip.tmp.mp4` (only while rendering), `thumb.jpg`,
  `captions.srt`, `captions.ass`, `transcript.txt`, `description.txt`
- `models/`
- `work/` (disposable)
- `secrets/`: `session_key`, and `tokens.json` only when the keyring fallback is in use
- `logs/`
- optional `safety_terms.txt`

---

## 5. Data model (SQLite)

- **recordings**
  - Identity and source: `id`, `title`, `source_type` (upload | url | local_path), `source_url` (normalised),
    `source_site`, `source_media_id`, `original_filename`.
  - File: `file_path`, `sha256` (unique when not null), `size_bytes`, `duration`, `width`, `height`, `fps`,
    `has_audio`, `metadata_json` (probe output plus yt-dlp info).
  - Processing: `status`, `stage`, `error`, `language`, `whisper_model` (a human label, e.g.
    `faster-whisper base.en (cpu/int8)`).
  - Other: `settings_json` (per-import overrides), `rights_attestation`, timestamps.
  - Status flow: imported → transcribing → transcribed → analysing → clips_queued → ready, or failed.
- **transcript_segments**: `recording_id`, `idx`, `start`, `end`, `text`, `avg_logprob`, `no_speech_prob`,
  `words_json` (a list of `{start, end, word, prob}`).
- **transcript_fts**: FTS5 over `text`, with `recording_id`, `segment_id` and `start` unindexed.
- **clips**
  - Position: `recording_id`, `idx`, `start`, `end`.
  - `status`: pending_render | rendering | ready (awaiting review) | approved | rejected | failed.
  - Text: `transcript_text`, `quote` (verbatim), `context_before`, `context_after`, `description`,
    `machine_summary`.
  - Selection: `score`, `score_reasons` (JSON), `selection_method` (heuristic | llm+heuristic | …+owner-adjusted),
    `confidence` (mean word probability), `low_conf_words` (JSON).
  - Review gates: `safety_flags` (JSON), `safety_status` (clear | flagged | acknowledged), `rights_status`
    (unreviewed | owned | licensed | public_domain | fair_dealing_reviewed | not_cleared).
  - Outputs: `video_path`, `thumb_path`, `srt_path`, `ass_path`, `render_rev`, `rendered_at`.
  - Review: `reviewed_at`, `review_note`, `error`.
- **jobs**: `kind` (download_url | process_recording | render_clip), `recording_id`, `clip_id`, `payload_json`,
  `dedupe_key`, `status` (queued | running | done | failed | cancelled), `progress` 0–1, `message`, `attempts`,
  `max_attempts`, `error`, `run_after`, timestamps. A **partial unique index** on `dedupe_key` covers queued and
  running jobs.
- **job_logs**: `job_id`, `ts`, `level`, `message`. This table has **no** `created_at`/`updated_at`;
  `Database.insert` special-cases it.
- **publish_attempts**: `clip_id`, `platform`, `status` (started | uploaded | confirmed | failed), `remote_id`,
  `detail`.

---

## 6. Processing pipeline (pipeline.py)

- **Pipeline.\_\_init\_\_** registers these handlers:
  - `download_url`
  - `process_recording` and `process_recording:failed` (sets the recording to failed with the error)
  - `render_clip` and `render_clip:failed` (sets the clip to failed)
- **process_recording**
  1. Merge overrides: the global settings, then `recordings.settings_json`, then the job payload settings.
     Only `OVERRIDABLE` keys are applied: `clips_per_recording`, `clip_min_seconds`, `clip_max_seconds`,
     `clip_target_seconds`, `language`, `llm_provider`.
  2. **Transcribe only if there is no transcript yet** (or `payload.retranscribe`). This is what makes jobs
     resumable. Steps: extract the WAV to `data/work/rec{id}_job{jid}.wav`, transcribe, **delete the WAV in
     `finally`**, then store the segments and FTS rows. Progress runs 0.02–0.10 for extraction and 0.10–0.85 for
     transcription.
  3. If clips already exist and `reselect` is not set, **create nothing**. Instead re-enqueue renders for
     pending or failed clips. This is the "no duplicate generation" guarantee.
  4. With `reselect`, delete only clips that are not approved or rejected (and their files), then re-select.
  5. Selection calls `segment.select(...)`, using the reranker when Ollama is reachable. For each candidate,
     `create_clip()` runs:
     - `snap_bounds`: never inside a word; pad −0.2 s / +0.4 s, clamped to the neighbouring words.
     - `clip_text_fields`: verbatim words, context before and after, safety screen, low-confidence words,
       confidence, `describe()`.
  6. Enqueue `render_clip` with dedupe key `render:{clip_id}`.
- **render_clip**
  - **Idempotent**: if `video_path` exists and lives in the current revision's directory, it is skipped.
  - It needs the original file to exist.
  - Output goes to `clip_dir(clip)` = `clips/rec%05d/clip%05d_r{render_rev}`.
  - After rendering it writes `transcript.txt` (context before, the excerpt marked as a direct transcript,
    context after, and the source) and `description.txt`, then sets the status to `ready`.
  - Older revision directories are removed.
  - When no clips of the recording are still pending or rendering, the recording becomes `ready`.
- **adjust_and_regenerate(clip_id, start=None, end=None)**
  - Validates `0 ≤ start < end ≤ duration` and a length of 3–180 s.
  - **If the boundaries changed:** snap them, recompute all text fields from the **stored transcript (never
    re-transcribes)**, clear `machine_summary`, and append `+owner-adjusted` to `selection_method` once.
  - **If unchanged:** only re-render. The owner's edited description is preserved; this is the regression
    fixed in `39e5846`.
  - Always bumps `render_rev`, resets the status to `pending_render`, clears `reviewed_at`, and enqueues a
    render.
- **describe()**: neutral, factual text. It always contains:
  - `Excerpt (m:ss–m:ss) from "<title>".`
  - `Direct quotation (opening): "<first 28 words>…"`
  - an `Automated summary (machine interpretation, verify before use)` line, only when an LLM summary exists
    **and** the clip is not flagged
  - an editorial note when flagged
  - a transcription note when there are low-confidence words
  - `Source: <url or local file>`

  It never writes headlines or promotional copy.

---

## 7. Excerpt selection algorithm (segment.py) — exact behaviour

- **Words** are flattened from segments. If a segment lacks word timings, its words are spread evenly at
  `prob=0.6`.
- **Sentences** split after a token ending in `. ? !` (ignoring trailing quotes and brackets, and abbreviations
  such as Mr/Dr/e.g.), or on a gap of more than 1.6 s between words.
- **Bounds**: `effective_bounds()` relaxes min, max and target when the recording is shorter than 2.5× the
  minimum, so short recordings still yield clips. For the 90 s sample this gave about 18–54 s windows.
- **Candidates**: every window of whole sentences `[i..j]` whose duration falls between min and max **and whose
  last sentence ends with terminal punctuation**. If there are none, retry with `min/2` (at least 5 s).
- **Score:**

  ```
  0.36·substance + 0.10·qa + 0.22·clarity + 0.10·pauses + 0.12·fit + 0.10·end_clean − start_pen − 1.5·filler
  ```

  - substance = content-word ratio (non-stopwords longer than 3 characters) and `CLAIM_MARKERS` regex hits
    (because, evidence, percent, policy, numbers, …)
  - qa = a question followed by at least 2 sentences in the window
  - clarity = the word-weighted mean probability
  - pauses = gaps before and after, capped at 0.8 s
  - fit = closeness to the target length
  - start_pen = 0.35 if the first word is a conjunction (and, but, so, because…), 0.12 if it is a pronoun
  - filler = um, uh and similar
  - Human-readable reasons are stored with the score.
- **Diversity** (`pick_diverse`): greedy over the sorted candidates. A candidate is rejected if it overlaps a
  chosen one within 1 s, or if its content-word Jaccard similarity to any chosen one exceeds 0.45. Final clips
  are sorted by start time.
- **LLM (optional, Ollama at `OLLAMA_URL`, model `OLLAMA_MODEL`, temperature 0, `format=json`)**:
  - It gets a pool of up to 3N diverse candidates as numbered texts and must return
    `{"ranked":[indices], "summaries":{"index":"..."}}`.
  - Only integer indices within the pool are accepted. Summaries have their quote characters stripped and are
    capped at 400 characters.
  - Any exception or invalid output **falls back to the heuristic ranking**.
  - The chosen clips are topped up from the heuristic list.
  - `LLM_PROVIDER`: auto (use it if reachable) | ollama | none.
- **Context**: sentences within `CONTEXT_SECONDS` (25 s) before and after the clip. If none fall in that window,
  the 2 nearest sentences are used.

---

## 8. Web app (web/app.py) — routes and behaviour

- **Security middleware:**
  - Any POST whose Origin or Referer host differs from Host gets a **403** (CSRF protection).
  - If `DASHBOARD_PASSWORD` is set, every path except `/static`, `/login`, `/healthz` and `/oauth/*` requires
    the `ce_auth` cookie: an HMAC of the password with `data/secrets/session_key`, httponly, SameSite=strict,
    30 days.
  - A failed login sleeps for 1 s.
- **Lifespan**: starts the worker and maintenance threads when `start_worker=True` (tests pass `False`).
- **Routes:**
  - Pages: `GET /` (dashboard), `GET /partials/jobs`, `GET /partials/counts` (HTMX polling every 3–5 s).
  - Import:
    - `GET /import`
    - `POST /import/upload`: multipart, streamed in 8 MB chunks with a `MAX_UPLOAD_MB` cap. It returns JSON when
      the request carries the `X-Requested-With` or `HX-Request` header, otherwise it redirects.
    - `POST /import/url`: policy check, attestation required, URL dedupe, enqueues `download_url`.
  - Recordings:
    - `GET /recordings` and `GET /recordings/{id}` (metadata, jobs, clip cards, full transcript with
      low-confidence highlighting and timestamp links)
    - `GET /recordings/{id}/media` (the original, for context verification)
    - `GET /recordings/{id}/transcript.txt`
    - `POST /recordings/{id}/reanalyse` (`retranscribe=yes` optional)
    - `POST /recordings/{id}/delete`: the owner must type the exact title.
  - Review and clips:
    - `GET /review?status=ready|approved|rejected|rendering|pending_render|failed|all`
    - `GET /clips/{id}`: the detail page, with word-confidence transcript, flag box and decision form, source
      context video cued to `#t=start-20,end+20`, timestamp adjust form, description editor, rights selector,
      publishing panel and publish history.
    - `POST /clips/{id}/approve | reject | regenerate (start,end in seconds or m:ss) | rights | acknowledge
      (note ≥ 15 chars) | description`
    - `GET /clips/{id}/video | thumb | download (attachment) | captions.srt | description.txt`. Starlette's
      `FileResponse` supports **Range → 206**, which iOS Safari needs.
  - Jobs: `GET /jobs`, `GET /jobs/{id}` (log), `POST /jobs/{id}/retry | cancel`.
  - Other pages:
    - `GET /search?q=`
    - `GET /settings` (hardware, chosen model, LLM status, storage, retention, connection status, token
      backend)
  - Publishing:
    - `GET /connect/{tiktok|instagram}` stores OAuth state in memory for 15 minutes.
    - `GET /oauth/{platform}/callback`
    - `POST /disconnect/{platform}`
    - `POST /clips/{id}/publish/{tiktok|instagram}`: the editorial gate is enforced server-side, and Instagram
      also requires `confirm=PUBLISH`.
  - JSON: `GET /api/status`, `GET /api/recordings/{id}` (used by the acceptance test), `GET /healthz`.
- **Review card** (`_clip_card.html`):
  - 9:16 video with a poster image
  - status, safety and low-confidence badges
  - the source title and link, start → end, and the selection method and rights status
  - collapsible context before and after
  - the quote in a blockquote
  - the description with a Copy button
  - Approve / Reject / Regenerate / Download MP4 / Adjust

---

## 9. Rendering (render.py)

- **Phrases**: words grouped into captions of at most 26 characters per line and 2 lines. A phrase breaks on a
  gap over 0.6 s, a duration over 3.2 s or a sentence end. Gaps under 0.35 s are closed, and each phrase shows
  for at least 0.8 s. Times are relative to the clip start.
- **Caption layout**: if the fitted foreground video ends above `H−420` (landscape sources), captions sit
  **below** the picture (ASS alignment 8, MarginV = foreground bottom + 70). Otherwise they sit at the lower
  third (alignment 2, MarginV = 16% of H).
- **ASS styles**: Caption is Arial 64 bold, white text with a 4 px outline and a 1 px shadow. A Label style
  (32 px, top) shows `Excerpt · <title> · m:ss` (provenance). `{` and `}` are escaped to `( )`.
- **FFmpeg graph** (run with `cwd=` the clip folder, so the `ass=captions.ass` path needs no Windows escaping):

  ```
  [0:v]split → bg: scale 270x480 increase, crop, boxblur 12, eq brightness −0.10 / saturation 0.85, scale 1080x1920
             → fg: scale to fit 1080x1920 (decrease, lanczos)
  overlay centred, format=yuv420p, ass
  [0:a] highpass 80 Hz, loudnorm I=−16 TP=−1.5 LRA=11, aresample 48000
  ```

  Encoding: input-side `-ss`/`-t` seek; libx264 (`X264_PRESET` veryfast, `VIDEO_CRF` 21, high profile) at 30 fps;
  AAC 160k; `+faststart`. Output goes to `clip.tmp.mp4` and is then atomically renamed to `clip.mp4`.
- **Thumbnail**: the midpoint frame at 540 px wide.
- **Verification**: the output is probed and must be exactly 1080×1920 with a non-zero duration, otherwise the
  render raises `PermanentError`.

---

## 10. Testing — what exists and what it proves

Run from `clipping-engine/`:

```
python -m pytest -v                                   # offline suite (real-Whisper test skipped)
CLIPENGINE_REAL_WHISPER=1 WHISPER_MODEL=base.en python -m pytest -v   # needs Hugging Face access
python scripts/acceptance_test.py [--stub] [--keep DIR] [--port 8799]
PW_CHROMIUM=/opt/pw-browsers/chromium                 # needed for the browser check in this sandbox only
```

| File | Covers |
|---|---|
| test_jobs.py (7) | dedupe, success, transient retry then failure, transient then success, permanent failure plus hook, crash recovery, manual retry and cancel |
| test_segment_safety.py (9) | sentence splitting, bounded/clean/non-overlapping candidates using only real word timestamps, dangling-start penalty, reranker can't invent and bad output falls back, context, short-recording bounds, safety categories and owner watchlist, low-confidence words |
| test_render_ingest.py (8) | phrase wrapping and timing, SRT/ASS output and escaping, layout, URL policy (Rumble, YouTube, allowlist, subdomain, scheme, look-alike domain), safe_filename, register and duplicate detection with provenance.json, non-video rejected, real 1080×1920 h264/aac render |
| test_pipeline_web.py (8) | **stub transcript**: full pipeline, WAV cleanup, FTS search plus injection safety; rerun creates no duplicates; regenerate with new timestamps doesn't re-transcribe and deletes the old revision; plain regenerate preserves the description; web review flow (Range 206, approve, download, SRT, transcript, search, settings, publish blocked without rights, cross-origin 403, persistence via a fresh DB handle); flagged clip needs a note of at least 15 chars; upload endpoint, duplicate, bad file type, Rumble URL refused; password login |
| test_publishing.py (7) | **MOCKED HTTP** (httpx.MockTransport): token-store file fallback, chunk plan, not-configured errors, TikTok OAuth/init/PUT Content-Range/status → confirmed, never confirms without status (uploaded, failed), Instagram OAuth/container/rupload headers/FINISHED/publish/permalink, editorial gate |
| test_hardware.py (2) | model-selection rules, `load_wav_16k` |
| test_whisper_real.py (1) | **REAL** faster-whisper on the sample: language en, at least 3 of council/bridge/ferry/consultation/library, word probabilities valid. Skipped unless `CLIPENGINE_REAL_WHISPER=1` |

The **acceptance test** (`scripts/acceptance_test.py`) works as follows:

- It generates the sample and starts `python -m clipengine --no-browser` as a subprocess with a temporary data
  directory, then runs these steps:
  1. start
  2. HTTP upload
  3. wait for jobs, then check the transcript contains at least 3 expected words
  4. candidates exist
  5. `/clips/{id}/video` returns 200, Range gives 206, and ffprobe reports 1080×1920 h264 + aac
  6. the review HTML embeds the video, and Playwright with a 390×844 mobile viewport loads the video metadata
     and takes screenshots
  7. approve one clip and reject another
  8. download returns an attachment
  9. stop and restart the server: statuses, transcript count and the download all persist
- `--stub` labels everything "TEST-ONLY STUB transcript (transcription NOT verified)".

**Verified results:**

- **Sandbox**: 41 passed and 1 skipped (the real-Whisper test). Acceptance `--stub` passed all 9 steps
  (14.6 MB clip; mobile screenshots were reviewed visually and the layout and captions are correct).
- **CI run #4 (`43600559824`), ubuntu-latest**: 41/41 passed; acceptance with real `base.en` passed all 9 steps;
  processing took 40 s; 14 segments; candidates (0.0–53.9 s) and (54.6–89.3 s).
- **CI run #4, windows-latest** (Python 3.12.10): `Start-Clipping-Engine.cmd --check` passed; 41/41 passed in
  302 s; acceptance with real Whisper passed all 9 steps; processing took 46 s; 13 segments; 14.4 MB download.
- Artifacts: https://github.com/ce-optimisation/n8n-automation-portfolio/actions/runs/43600559824
- Runs #1–#3 failed, for the PyAV and encoding bugs above; both are fixed.

---

## 11. Launcher behaviour (Start-Clipping-Engine.cmd)

1. `cd` to the script's folder.
2. Find Python 3.10–3.13, trying `py -3` and then `python`. The interpreter is actually run, which rejects the
   Microsoft Store stub. If none is found, offer `winget install Python.Python.3.12 --scope user`.
3. Create `.venv` and run `scripts\ensure_deps.py`.
4. Add FFmpeg to PATH from `tools\ffmpeg\bin` or the WinGet Links folder. If FFmpeg is still missing, offer
   `winget install Gyan.FFmpeg`, or explain how to place the files manually.
5. Copy `.env.example` to `.env` if needed.
6. Run `.venv\Scripts\python.exe -m clipengine %*`. On failure it shows the exit code and pauses.

Prompts use `choice` and are suppressed by `CLIPENGINE_NONINTERACTIVE=1` (CI). **Keep the CRLF line endings.**

---

## 12. Publishing — implementation details and the safety gate

**`editorial_gate(clip)`** blocks publishing unless all of these hold:

- status = approved
- `rights_status` is one of owned, licensed, public_domain or fair_dealing_reviewed
- `safety_status` is not `flagged` (an acknowledged flag is fine)
- the clip is rendered

The gate is enforced server-side and shown in the UI.

**TikTok** (`tiktok.py`):

- **Authorisation**: `https://www.tiktok.com/v2/auth/authorize/` with scope `user.info.basic,video.upload`,
  state and PKCE S256.
- **Token**: `POST https://open.tiktokapis.com/v2/oauth/token/` (form-encoded; the code verifier is sent when
  present). It refreshes using `grant_type=refresh_token`. If the `video.upload` scope is missing it raises.
- **Upload**:
  1. `POST /v2/post/publish/inbox/video/init/` with
     `{"source_info":{"source":"FILE_UPLOAD","video_size","chunk_size","total_chunk_count"}}`
  2. PUT each chunk to `upload_url` with `Content-Type: video/mp4` and
     `Content-Range: bytes a-b/size`.
  3. `POST /v2/post/publish/status/fetch/ {"publish_id"}`, polling 60 times at 3 s intervals.
- **Chunking**: files under 5 MB go in a single chunk. Otherwise chunks are 10 MB (clamped to 5–64 MB),
  `total = floor(size/chunk)`, and the final chunk absorbs the remainder (128 MB maximum).
- **Outcome**: success (`confirmed=True`) only on `SEND_TO_USER_INBOX` or `PUBLISH_COMPLETE`. FAILED returns
  `failed`; a timeout returns `uploaded` (not confirmed).
- **Rate limit**: a local deque enforces 6 init calls per minute.

**Instagram** (`instagram.py`, Graph `https://graph.facebook.com/{META_GRAPH_VERSION=v23.0}`):

- **Authorisation**: `https://www.facebook.com/{v}/dialog/oauth` with scopes `instagram_basic`,
  `instagram_content_publish`, `pages_show_list`, `pages_read_engagement` and `business_management`.
- **Token**: exchange the code, then swap for a long-lived token with `fb_exchange_token`. Then call
  `me/accounts?fields=name,instagram_business_account{id,username}` and store `ig_user_id` and the username.
- **Publish**:
  1. `POST /{ig}/media` with `media_type=REELS`, `upload_type=resumable`, `caption` (2,200 characters maximum)
     and `share_to_feed`.
  2. `POST` the bytes to `uri` (or `https://rupload.facebook.com/ig-api-upload/{v}/{container}`) with headers
     `Authorization: OAuth <token>`, `offset: 0` and `file_size`.
  3. Poll `GET /{container}?fields=status_code,status` until FINISHED; ERROR or EXPIRED returns `failed`.
  4. `POST /{ig}/media_publish creation_id`, then `GET /{media}?fields=permalink` to confirm.
- **Quota**: `publishing_quota()` reads `content_publishing_limit` (100 posts per 24 h).
- **No drafts exist in this API.** The UI warns about this and requires typing PUBLISH.
- **Unknown, not validated live**: Meta's docs suggested resumable upload is tied to Facebook Login for
  Business. That path was chosen deliberately. Instagram-Login (`graph.instagram.com`) support is not
  implemented.

**Tokens**: stored with `keyring` (service `local-clipping-engine`) in Windows Credential Manager, falling back
to `data/secrets/tokens.json` (chmod 600). The Settings page shows which backend is in use. Client IDs and
secrets come only from `.env`.

---

## 13. Ingestion policy (keep it)

`RESTRICTED_PLATFORMS` are **always refused**, each with its specific reason:

- youtube.com, youtu.be
- rumble.com
- tiktok.com, instagram.com, facebook.com, fb.watch
- x.com, twitter.com
- twitch.tv, kick.com
- bitchute.com, odysee.com

Other hosts must match `ALLOWED_URL_DOMAINS` (the host itself or a subdomain; look-alike domains are refused)
**and** have the attestation checkbox ticked.

yt-dlp options: `bv*[height<=1080]+ba/b`, mp4 merge, noplaylist, no cookies, 3 retries, 1 s sleep between
requests. Errors are classified as transient (timeout, connection, HTTP 5xx) or permanent (DRM, other).
Uploads are the guaranteed path.

---

## 14. Reliability, retention and logging

- Jobs survive restarts: `recover()` runs at worker start. The transcript is reused, so a crashed
  `process_recording` resumes at selection.
- Retries: `JOB_MAX_ATTEMPTS` (default 3). The UI has Retry and Cancel; manual retry resets attempts and resets
  the clip or recording status.
- Maintenance thread (first pass after 30 s, then hourly):
  - removes work files older than 24 h, only when no job is running
  - removes rendered files of rejected clips after `DELETE_REJECTED_RENDERS_AFTER_DAYS` (14); the database rows
    are kept
  - deletes originals only if `DELETE_ORIGINALS_AFTER_DAYS` > 0 (default **0 = never**), only for `ready`
    recordings, and never while any clip is pending, rendering or ready
- `storage_report()` warns above `STORAGE_WARNING_GB` (50) or when free disk is under 5 GB.
- Logs: JSON lines in `data/logs/clipengine.jsonl` (5 MB × 5 rotation), plus per-job logs in the database.

---

## 15. Configuration reference (.env)

| Group | Variables |
|---|---|
| Server | `CLIPENGINE_PORT` (8765), `CLIPENGINE_HOST` (127.0.0.1; 0.0.0.0 for phone access needs a password), `DASHBOARD_PASSWORD`, `OPEN_BROWSER`, `CLIPENGINE_DATA_DIR` |
| Transcription | `WHISPER_MODEL` (auto), `WHISPER_DEVICE`, `WHISPER_COMPUTE_TYPE`, `TRANSCRIBE_LANGUAGE`, `VAD_FILTER`, `INSTALL_CUDA_LIBS` |
| Selection | `CLIPS_PER_RECORDING` (6), `CLIP_MIN_SECONDS` (30), `CLIP_MAX_SECONDS` (90), `CLIP_TARGET_SECONDS` (55), `CONTEXT_SECONDS` (25), `LLM_PROVIDER` (auto), `OLLAMA_URL`, `OLLAMA_MODEL` (llama3.2:3b) |
| Render | `VIDEO_CRF` (21), `X264_PRESET` (veryfast), `CAPTION_FONT` (Arial), `CAPTION_FONT_SIZE` (64) |
| Ingest | `ALLOWED_URL_DOMAINS` (empty), `MAX_UPLOAD_MB` (8192) |
| Jobs and retention | `JOB_MAX_ATTEMPTS`, `DELETE_REJECTED_RENDERS_AFTER_DAYS`, `DELETE_ORIGINALS_AFTER_DAYS`, `STORAGE_WARNING_GB` |
| Publishing | `TIKTOK_CLIENT_KEY`/`SECRET`/`REDIRECT_URI` (http://localhost:8765/oauth/tiktok/callback), `META_APP_ID`/`SECRET`/`REDIRECT_URI` (…/oauth/instagram/callback), `META_GRAPH_VERSION` |
| Test-only | `CLIPENGINE_TRANSCRIBER=stub`, `CLIPENGINE_STUB_TRANSCRIPT`, `CLIPENGINE_REAL_WHISPER=1`, `PW_CHROMIUM`, `CLIPENGINE_NONINTERACTIVE` |

The automatic Whisper choice is:

- CUDA with ≥10 GB VRAM: large-v3 (float16)
- CUDA with ≥5 GB: medium (float16)
- other CUDA GPUs: small (float16)
- CPU with ≥8 cores and ≥15 GB RAM: small (int8)
- CPU with ≥4 cores and ≥7 GB RAM: base (int8)
- anything smaller: tiny (int8)

`.en` is appended for tiny, base, small and medium when the language is `en`.

---

## 16. Outstanding work and owner dependencies (do NOT mark the project "complete" until resolved or accepted)

**Owner-dependent:**

1. **TikTok**:
   - Create a developer app with Login Kit and the Content Posting API, and request `user.info.basic` and
     `video.upload`.
   - Register the redirect URI. TikTok may require HTTPS; if so, the owner needs an HTTPS address they control
     and must set `TIKTOK_REDIRECT_URI` to match.
   - Pass review or add themselves as a tester.
   - Put the key and secret in their local `.env`, then use Settings → Connect TikTok.
   - Then do a **live, owner-initiated** inbox upload and confirm `SEND_TO_USER_INBOX`. This has never been
     tested live.
2. **Instagram**:
   - The account must be Business or Creator and linked to a Facebook Page.
   - Create a Meta Business app with Facebook Login for Business and the Instagram Graph API, add the redirect
     URI and the permissions, and use dev mode or App Review.
   - Put the ID and secret in `.env`, then use Settings → Connect Instagram.
   - The first live publish is public, so do it only with explicit owner instruction. Never tested live.
3. **First run on the owner's PC**: run the launcher and confirm:
   - the hardware detection and model choice on the Settings page
   - transcription speed on a real long recording
   - Windows Firewall prompt handling if phone access is enabled
   - GPU set-up with `INSTALL_CUDA_LIBS=1` if they have an NVIDIA card (only the CPU-fallback logic exists;
     the CUDA path is unverified)
4. *(Optional)* The sandbox network change for Hugging Face, which must be approved from claude.ai on desktop.

**Engineering items not done** (none block the local workflow):

- **Long-recording validation**: only the 90 s sample has been processed. The O(n²) candidate generation is
  bounded by the max window length, but test a 1–3 h recording for selection time and memory.
- **Instagram-Login variant** (`graph.instagram.com`) is not implemented; only Facebook Login for Business.
- **Uploads**: python-multipart spools the upload to the temp directory before it is copied, so disk usage
  briefly doubles. A raw streaming PUT endpoint would avoid this.
- **Optional subject tracking or face-aware cropping** was deliberately not built. The blurred-background
  fallback is robust; add tracking only if it is reliable on the owner's hardware.
- **Publishing runs synchronously** in the request thread (up to about 3 minutes of polling). It could be moved
  to a job.
- **Speaker diarisation** is not implemented (it is not required by the spec).
- **TikTok `chunk_plan` boundary**: rounding at the ~5 MB edge is unverified against the live API.
- The safety screen is a deliberately conservative regex prompt for human review, not a classifier. Owners
  extend it via `data/safety_terms.txt`.

---

## 17. Gotchas and rules for the next engineer

- **Do not** pass file paths to `WhisperModel.transcribe`. Keep `load_wav_16k` (the PyAV incompatibility).
- **Do not** replace the `connect_ex` port check with `bind` (it fails falsely on TIME_WAIT after a restart).
- Always pass `encoding="utf-8"` for text I/O; Windows defaults to cp1252.
- Run FFmpeg with `cwd=` the clip directory and **relative** subtitle filenames. Windows drive-letter colons
  break the `ass=` filter argument otherwise.
- `job_logs` has no `created_at`/`updated_at` (see the special case in `Database.insert`).
- Keep stub and mock labelling intact in names, `model_label`, docstrings, the acceptance output and any
  report. Never present stub results as transcription verification.
- Keep CRLF in `Start-Clipping-Engine.cmd`.
- When changing selection or rendering, rerun **both** `pytest` and `scripts/acceptance_test.py --stub` locally,
  and rely on CI for real-Whisper verification. Check runs with the GitHub MCP tools
  (`mcp__github__actions_list`, `mcp__github__get_job_logs`); there is no `gh` CLI in the sandbox except for
  search.
- Read test-run output before reporting. Report failures with their output, and never claim a platform post
  succeeded without an API confirmation.

---

## 18. How the owner uses it (for your completion reports)

1. Double-click `clipping-engine\Start-Clipping-Engine.cmd`. The dashboard opens at http://127.0.0.1:8765.
2. Go to **Import → Upload a recording**, choose an MP4 or MOV, and click **Analyse Video**.
3. Leave it running; progress shows on the Dashboard.
4. In **Review**, watch each clip and check the context, then **Approve** or **Reject**.
5. Use **Adjust / details** for timestamps, the rights basis, the description and verification against the
   original.
6. **Download MP4** and **Copy description**. TikTok and Instagram buttons appear only once credentials are
   configured and connected.

Verdict carried forward: **ready for routine local use**. Publishing integrations await the owner's developer
credentials and approvals.
