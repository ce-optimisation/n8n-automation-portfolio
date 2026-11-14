# HANDOFF — Local AI Video Clipping & Editorial Review Engine (session 2)

> Paste this file into a new Claude Code session, or tell the agent to read
> `clipping-engine/HANDOFF.md`. It supersedes the session-1 handoff, which is in git history
> (commit `553c922`).

## 0. Standing instructions (still binding)

- **Execute; don't plan.** Build, test, fix, verify in CI, then report.
- **Owner-intervention protocol.** Keep working on anything that doesn't depend on the owner and track the
  dependency. Collect owner actions into **one** prioritised list at the end; never ask in scattered questions.
  Never request passwords, API secrets or recovery codes in chat.
- **Never present mocks or stubs as real.**
  - The TEST-ONLY stub transcriber, `clipengine/testsupport.py`, mocked TikTok/Instagram HTTP and the
    `allow_private_urls` switch are labelled as such everywhere and must stay labelled.
  - "Connected" requires real OAuth tokens. "Ready" for a platform requires a confirmed real API transaction.
- **No** purchases, account creation, external-service changes, or public posting without explicit owner
  permission. No browser automation to get around platform rules. No DRM, login or terms circumvention.
- Keep the architecture: Python-first, local, free, SQLite, FFmpeg, faster-whisper, FastAPI + HTMX. No Docker,
  Redis or cloud services.
- **Git:**
  - Repo `ce-optimisation/n8n-automation-portfolio`, branch **`claude/optimistic-noether-k52qjf`**.
  - Push with `git push -u origin claude/optimistic-noether-k52qjf`, retrying network errors up to 4 times.
  - **No PR unless asked.** End commits with the session attribution lines; put no model names in commits or
    code.
  - All code lives in `clipping-engine/`, plus `.github/workflows/clipping-engine.yml`. Leave the n8n templates at
    the repo root untouched.

## 1. Status at the end of session 2

| Area | Status |
|---|---|
| Local workflow: import → real Whisper → selection → 1080×1920 render → mobile review → approve → download → restart persists | **Verified** in CI with real Whisper on Ubuntu and Windows (9/9 acceptance steps on both, run #8 `43611814430`). |
| URL ingestion: direct files, permitted extractor, pending sources, inbox, SSRF protection, resume | **Verified.** Local-server tests, plus a **live** import of a public-domain NASA video from Wikimedia Commons on the Ubuntu runner. The Windows runner got HTTP 403 from Wikimedia, recorded as an expected failure. |
| Rumble automated download | **Deliberately not enabled.** Rumble's Terms of Use require Rumble's *prior written permission* for automated access. Two routes exist: link + file (pending source), or a recorded written permission. Live Rumble retrieval is **untested** (no permission exists). |
| Long-form | **Verified**: 114 min with real `base.en`, hard-kill crash test and resume (see §5). |
| Selection v2 | **Verified**, with a reproducible evaluation on the real Whisper transcript (see §6). |
| TikTok upload-to-inbox | **Implemented and mock-tested only.** It needs the owner's developer app, OAuth connection and a first real upload. |
| Instagram Reels | **Implemented (two login modes) and mock-tested only.** It needs a Creator account, a Meta app and a connection. The first real publish is **public** and needs the owner's explicit go-ahead. |
| Owner's Windows PC | **Never inspected or run.** The launcher has been verified on a GitHub Windows runner only. |

**Commits this session:**

| SHA | Change |
|---|---|
| `6ab4b2c` | The main continuation |
| `0616266` | Configurable User-Agent; honest expected failure on a remote 403 |
| (this commit) | Speed-metric fix and documentation |

The last fully green CI run is #8.

## 2. What changed in session 2 (file map of new and changed parts)

- **`clipengine/ingest.py`**
  - `classify_url()` returns a decision: `direct`, `ytdlp`, `link_only` or `refused`, each with a machine
    `code` and a human message.
    - Restricted platforms (`RESTRICTED_PLATFORMS`, including rumble.com) become `link_only` unless the
      `source_permissions` table has a row for the domain.
    - Direct media paths (`.mp4`, `.mov`, …) on public hosts become `direct`.
    - `ALLOWED_URL_DOMAINS` or a recorded permission gives `ytdlp`.
    - Any other web page becomes `link_only`.
  - `check_network_target()`: http(s) only, ports 80/443, no credentials in the URL, and every resolved IP must
    be global.
  - `download_direct()`:
    - follows up to 5 redirects manually and re-checks each hop
    - re-checks the **peer IP after connecting**, which defeats DNS rebinding
    - resumes with HTTP Range into `work/dl_<sha1(url)>.part`
    - validates the content type and enforces `MAX_UPLOAD_MB`
    - raises TransientError for 5xx, 429 and network drops (the job retries and resumes), and SourceError with a
      specific code for 401/403/404 and non-video responses
  - The yt-dlp path adds `continuedl`, `max_filesize` and clear error mapping for DRM, login-only and
    unsupported pages.
  - Pending sources: `create_pending_source()` and `attach_file_to_pending()`. A recording attached this way has
    `source_type='linked_file'`, keeps the original URL, and its provenance states "not downloaded by this
    tool".
  - **Fix:** a duplicate import now deletes only temporary copies in `work/`; it previously deleted inbox files.
- **`clipengine/inbox.py`** — `InboxWatcher` scans `INBOX_DIR` (default `data/inbox`) every 10 s.
  - A file is imported only once its size and modification time are stable across two scans.
  - `src-<id>` in a file name attaches it to that pending source.
  - Duplicates go to `inbox/duplicates`, failures to `inbox/failed` with a `.error.txt`. Files are never
    deleted.
- **`clipengine/transcribe.py`**
  - `plan_chunks()` cuts at the lowest-energy 0.25 s frame within ±20 s of every `TRANSCRIBE_CHUNK_SECONDS`
    (1200 s).
  - `read_wav_range()` loads only the requested range.
  - The transcriber API is `prepare()` plus `transcribe_range(wav, start, end, language)`.
  - `cpu_threads` is automatic: half the logical CPUs on machines with 8 or more, otherwise all of them. The beam
    size is configurable.
- **`clipengine/pipeline.py`** — `transcribe_recording()`:
  - extracts audio once to `work/recNNNNN_audio.wav` (written as `.tmp` and renamed; reused after a crash)
  - checkpoints each chunk into `transcript_chunks` and resumes from them
  - discards checkpoints if the chunk plan changed
  - after success, replaces the transcript, deletes the checkpoints and the WAV, and records stats in
    `recordings.metadata_json.processing`
  - sets `verified.transcription` in the `meta` key-value table when real Whisper succeeds

  Selection writes a report and `recordings.selection_note`.
- **`clipengine/segment.py`** — `SelectionProfile` with profiles `v1` (the original) and `v2` (the default).
  v2 adds:
  - a procedural/filler penalty (`PROCEDURAL_MARKERS`)
  - a topic-start bonus
  - a relative quality floor (0.6 × best) plus an absolute floor, so weak clips are never used as padding
  - a minimum gap of 5 s between clips

  `select(..., profile=, report=)`.
- **`clipengine/db.py`**
  - `MIGRATIONS`: idempotent ALTER TABLE ADD COLUMN; schema_version 2.
  - New tables: `transcript_chunks`, `pending_sources`, `source_permissions`, `public_media_tokens`.
  - `get_kv` / `set_kv` on `meta`.
  - **Fix:** `insert()` now sets `created_at`/`updated_at` only on tables that have those columns.
- **`clipengine/publishing/`**
  - `base.py`:
    - `STATES`: queued, uploading, processing, retrying, awaiting_connection, delivered_inbox, published,
      failed, cancelled; `CONFIRMED_STATES` are delivered_inbox and published.
    - `validate_video()`.
    - `editorial_gate()` now also checks that the rendered file exists.
  - `tiktok.py`:
    - **desktop PKCE: challenge = lowercase hex SHA-256** (TikTok's variant; the earlier base64url was wrong).
      Web apps send no PKCE (`TIKTOK_APP_TYPE`).
    - Steps: `init_upload`, `upload_file`, `fetch_status`, `wait_for_result`.
    - Error-code mapping: transient codes retry; `access_token_invalid` refreshes once; spam and unaudited
      errors are explained.
    - Redirect default `http://127.0.0.1:8765/oauth/tiktok/callback`.
  - `instagram.py`:
    - `mode="facebook"` (graph.facebook.com plus resumable rupload; needs a linked Facebook Page).
    - `mode="instagram"` (graph.instagram.com, scopes `instagram_business_basic,instagram_business_content_publish`,
      `video_url` from a one-time public link, automatic `ig_refresh_token`).
    - Steps: `create_container`, `upload`, `container_status`, `publish`, `permalink`.
    - Graph error codes map to transient, reconnect or permanent.
  - `service.py`: `PublishService`, with `request()` and the `publish_clip` job.
    - Each attempt moves through checkpointed stages.
    - On resume, TikTok only polls if already uploaded.
    - For Instagram, the stage is set to `publish_requested` before `media_publish`. On resume, a container
      reporting `PUBLISHED` is recorded as published and **never published twice**.
    - Duplicate guard: a new request is refused while an attempt is active or after it was confirmed, unless
      `again=yes`.
    - Public media tokens last 2 hours and are revoked after a terminal state.
- **`clipengine/setup_status.py`** — the Settings checklist (10 areas) with honest statuses.
- **`clipengine/web/app.py`**
  - Without `DASHBOARD_PASSWORD`, only localhost Host headers are answered (blocks DNS rebinding and accidental
    exposure).
  - Login is rate-limited (5 per 10 minutes); the cookie is Secure over HTTPS.
  - The `/public/media/<token>.mp4` route is exempt from auth only for valid, unexpired tokens.
  - URL import creates pending sources; upload accepts `pending_id`.
  - Routes for source permissions, model download, pending-source cancellation and job-based publishing.
  - OAuth errors are stored in the key-value table.
  - Redirect query strings are URL-encoded (a session-1 bug).
- **Templates:**
  - `settings.html`: checklist, source permissions, secure phone access.
  - `import.html`: link rules and pending sources.
  - `clip.html` and `_clip_card.html`: publishing state badges and "Send again".
  - `recording.html`: processing stats, selection note, provenance type.
  - CSS: no horizontal overflow at 390 px (verified with a screenshot).
- **`clipengine/__main__.py`**
  - Port conflict: if `/healthz` returns `app=clipengine`, open that instance; otherwise use the next free port
    and warn about OAuth redirect URIs.
  - Hardware report and tips at start-up; OneDrive warning.
  - `--download-model`.
- **`Start-Clipping-Engine.cmd`** (keep CRLF line endings)
  - Tries `py -3.12`, `3.13`, `3.11`, `3.10`, then `python`.
  - Recreates `.venv` if it was built with an unsupported Python.
  - Shows the log path on failure.
- **Config (`.env.example`):** `WHISPER_CPU_THREADS`, `WHISPER_BEAM_SIZE`, `TRANSCRIBE_CHUNK_SECONDS`,
  `INBOX_DIR`, `HTTP_USER_AGENT`, `TIKTOK_APP_TYPE`, `INSTAGRAM_AUTH_MODE`, `INSTAGRAM_APP_ID`/`SECRET`/`REDIRECT_URI`,
  `PUBLIC_MEDIA_BASE_URL`. Test-only: `ALLOW_PRIVATE_URLS`.
- **Scripts:**
  - `make_longform_sample.py`: labelled synthetic recordings of any length; original text, flite voice.
  - `evaluate_selection.py`: v1 vs v2 metrics, on the stub or a real transcript.
  - `longform_benchmark.py`: real pipeline in a worker subprocess, sampling RSS and work-directory size; the
    `--crash-test` option hard-kills the worker after the first checkpoint.
- **CI:**
  - Push runs the Ubuntu and Windows tests with `CLIPENGINE_REAL_WHISPER=1` and `CLIPENGINE_LIVE_NET=1`, plus
    acceptance.
  - `workflow_dispatch` with input `longform_minutes` greater than 0 runs the long-form job (real Whisper plus
    the crash test) and uploads the report.

## 3. Bugs found and fixed this session (all have regression tests)

1. `Database.insert` assumed every table has `updated_at`. It crashed on checkpoint inserts and was caught by
   the long-form benchmark.
2. The direct downloader buffered 1 MB before writing, so the data before a dropped connection was lost and
   nothing could be resumed.
3. A duplicate import deleted the source file when it was anywhere inside `data/`, which included **inbox** files.
4. TikTok desktop PKCE used a base64url challenge; TikTok expects hex.
5. Import redirects didn't URL-encode their messages.
6. The speed metric divided the full recording length by a resumed run's time. It now counts only audio
   transcribed in that run.

## 4. Tests

- Locally: 66 passed, with the real-Whisper and live-network tests skipped because Hugging Face and Wikimedia
  are blocked in the sandbox.
- CI run #8: **Ubuntu 68/68**; **Windows 67 passed + 1 expected failure** (the live Wikimedia import got
  HTTP 403 on the Windows runner).
- Acceptance 9/9 on both operating systems with real Whisper.

New test files:

- `test_ingest_sources.py` — local-server downloads, resume, redirect, the DNS-rebinding peer check,
  classification, permission gating, pending sources, inbox
- `test_publishing.py` — **mocked**: PKCE, error mapping, job resume, no double publish, Instagram Login link
  tokens
- `test_reliability.py` — chunk planning, crash or resume, v2 versus v1, no padding
- `test_security_setup.py` — localhost-only host check, rate limit, honest checklist, public tokens, v1 → v2
  migration, port helpers
- `test_live_network.py` — live; only with `CLIPENGINE_LIVE_NET=1`

## 5. Long-form evidence

Real Whisper `base.en`, CPU int8, GitHub Ubuntu runner with 4 vCPU and 16 GB; dispatch run #7 `43610890294`,
artifact `longform-report`.

| Measure | Result |
|---|---|
| Recording | 114.3 min, 6 chunks |
| Crash test | Worker hard-killed after chunk 1 was checkpointed. Restart logged "resuming after 1 completed chunk(s)" and "Reusing extracted audio". |
| Speed | 5 remaining chunks (~94 min) in 306 s, about **18× real time** |
| Model load | 0.5 s (cached) |
| Peak RSS | **1.6 GB**, model included |
| Peak work-directory disk | 0.22 GB; 0 MB left afterwards |
| Selection | 3.2 s |
| Clips | 6 rendered, 7.6–13.3 s each; 0 failed jobs |
| Vocabulary recall vs the script | 97.7% |
| Not measured | `small`/`medium` models, GPU, and the owner's hardware |

## 6. Selection evaluation (reproducible)

On the 114-min sample's **real Whisper transcript**:

| Profile | Substantive share | Procedural share |
|---|---|---|
| v1 | 0.80 | 0.118 |
| v2 | **0.937** | **0.0** |

Both had clean starts, terminal ends and topic diversity of 0.83, with 6 clips each. The stub transcript gives
the same picture. **Caveat:** the sample text is template-generated and the scorer uses similar cues, so compare
profiles rather than absolute levels. The **Ollama re-ranker was not evaluated**, because no Ollama was available
in the sandbox or CI.

## 7. Remaining engineering issues

1. **Live TikTok and Instagram transactions are unverified.** Everything is mocked until the owner connects real
   accounts. Specifically unverified:
   - whether TikTok's portal accepts the `127.0.0.1` loopback redirect for desktop apps (fallback: a Web app with
     a Tailscale HTTPS redirect)
   - whether unaudited apps can use inbox upload for the owner's account type
   - Meta's exact error codes in practice
2. **Rumble live retrieval** has never been executed. It is permission-gated by design.
3. **The Windows runner was refused by Wikimedia (403).** The owner's PC may need
   `HTTP_USER_AGENT=… (contact: …)` for Wikimedia direct links.
4. **Peak RAM of 1.6 GB with `base`**; `small` will need more. PCs with 4 GB of RAM should use `tiny.en` or
   `base.en`.
5. **The OAuth state lives in memory**, so a restart during sign-in means starting the connection again (minor).
6. **python-multipart spools uploads to temp before copying**, briefly using double the disk space for large
   uploads. Use the inbox folder for very large files.
7. **TikTok `chunk_plan` near 5 MB and 64 MB boundaries** is unverified against the live API.

## 8. Owner action list (order to do them)

1. **Install and launch on the Windows PC.** Download the repo, double-click
   `clipping-engine\Start-Clipping-Engine.cmd`, accept the winget prompts, then open Settings → Setup checklist and
   click "Download Whisper model now". Report the checklist statuses and the hardware line printed in the
   console.
2. **Run a first real recording.** Upload, or drop into the inbox folder, a short recording you are authorised to
   use. Confirm the checklist shows "A real transcription has succeeded: Ready", and report the processing stats
   from the recording page.
3. **TikTok:**
   - create or sign in to the account
   - register a developer app at developers.tiktok.com (Login Kit + Content Posting API, scopes `user.info.basic`
     and `video.upload`, redirect `http://127.0.0.1:8765/oauth/tiktok/callback`)
   - add yourself as a tester or submit for review
   - put the key and secret in `.env`, then use Connect TikTok
   - approve a clip, set its rights, send it to the inbox, and confirm it appears in the TikTok app
4. **Instagram:**
   - switch to a Creator account and link a Facebook Page
   - create a Meta app at developers.facebook.com (Facebook Login for Business + Instagram Graph API, the
     permissions listed in README §7, the app in dev mode with you in App roles)
   - put the ID and secret in `.env`, then use Connect Instagram
   - **first public publish only on your explicit instruction**
5. **Optional:**
   - Tailscale for secure phone access
   - Rumble written permission, recorded in Settings
   - `HTTP_USER_AGENT` with contact details
   - `INSTALL_CUDA_LIBS=1` for an NVIDIA GPU
   - approve the cloud-session network change (Hugging Face) from claude.ai on desktop if real transcription is
     wanted inside the sandbox

## 9. Gotchas (carry forward)

- Keep `read_wav_range` and the NumPy sample input; don't pass file paths to `WhisperModel.transcribe`, because
  PyAV's keyword arguments are incompatible.
- The port check uses `connect_ex`, not `bind`.
- Always use `encoding="utf-8"`. Run FFmpeg with `cwd=` the clip directory and relative subtitle names.
- Keep CRLF line endings in the `.cmd`.
- Never relax `classify_url` for restricted platforms without a recorded written permission.
- Never let publishing report success without a platform-confirmed state.
- Never move a stub or mock result into "Ready" or "Connected".
- Check CI with `mcp__github__actions_list` and `mcp__github__get_job_logs`. Trigger the long-form job with
  `mcp__github__actions_run_trigger` (`run_workflow`, `inputs.longform_minutes`).
