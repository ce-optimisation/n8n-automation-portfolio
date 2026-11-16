# Handoff prompt for ChatGPT — Local AI Video Clipping & Editorial Review Engine

> Paste everything below the line into ChatGPT (ideally with the repository attached or connected).
> State as of **16 November 2026**, branch `claude/optimistic-noether-k52qjf`, commit `39df912` or later.
> The detailed engineering record is `clipping-engine/HANDOFF.md` (about 1,400 lines). This file is the
> short, self-contained version. When the two disagree, re-check the code: it is the source of truth.

---

## Your role

You are taking over as lead engineer of a **working, locally hosted application** that the owner (James)
runs on his own Windows PC. It watches authorised video sources, downloads or records new videos,
transcribes them on the PC, picks self-contained excerpts, and renders vertical 1080×1920 clips with burned-in
captions for **human review**. Nothing is ever published without the owner's explicit approval.

The owner's working preferences:
- **Do the work, don't just propose it.** Build, test, fix and verify, then report.
- **Don't rebuild what works.** Change code only for a demonstrated defect or a requested feature.
- **Don't scatter questions.** Collect anything that needs the owner into one prioritised list at the end.
- **Plain, honest reporting.** Never present simulated or mocked behaviour as real. Say exactly what was and
  wasn't verified.

## What the product is

- **Purpose:** political-media monitoring, factual reporting, documentary analysis and commentary. The owner
  monitors the Rumble channel `https://rumble.com/c/nickjfuentes` (show "America First", live Mon–Fri 9pm CT)
  and holds Rumble's written permission for automated access, recorded in the app.
- **Editorial ethics (binding):** preserve context and provenance; never change a speaker's meaning; never
  fabricate quotes; flag hateful or extremist advocacy for human review (the engine has a safety screen); never
  optimise for outrage; never generate promotional material from extremist advocacy.
- **Stack:** Python (must run on **3.10**, the owner's version), FastAPI + Jinja2 + HTMX, SQLite, FFmpeg/FFprobe
  with libass, faster-whisper (local speech-to-text), yt-dlp. No Docker, Redis, Postgres, cloud hosting, paid
  APIs, subscriptions or heavy frontend frameworks. Everything runs locally and free.

## Where things are

- **Repo:** GitHub `ce-optimisation/n8n-automation-portfolio`, branch **`claude/optimistic-noether-k52qjf`**.
  All application code is in **`clipping-engine/`**, plus CI in `.github/workflows/clipping-engine.yml`.
  The repo root also holds unrelated n8n workflow templates (JSON/MD): **never modify them.**
- **Installed copy on the PC:** `C:\Users\James\AppData\Local\ClippingEngine`. It is **replaced on every update**,
  so never edit it directly. Change the repo, push, then re-run the installer.
- **Updating the PC:** `Install-Clipping-Engine.cmd` (or `scripts/install.ps1`) downloads the branch ZIP, copies it
  into the install folder (keeping `.env`, `data\`, `.venv`, `tools\`), creates shortcuts and runs the setup
  assistant. Flags: `-NonInteractive -NoShortcut -NoStart`.
- **Dashboard:** `http://127.0.0.1:8765`. **Logs:** `data\logs\clipengine.jsonl`. **Inbox folder** (drop files to
  import): `data\inbox`. **Self-test clip:** `data\selftest\selftest_clip.mp4`.

## Main modules (`clipping-engine/clipengine/`)

| Module | Job |
|---|---|
| `__main__.py` | Entry point: `--check`, `--doctor`, `--selftest`, `--setup`, `--supervise` (auto-restart), port handling, logging |
| `config.py` | Settings from `.env` (no python-dotenv); `apply_settings()` writes edits back to `.env` |
| `db.py` | SQLite (WAL); schema via idempotent `MIGRATIONS` |
| `jobs.py` | Persistent job queue, one worker; `TransientError` (retry, can set `retry_after`) vs `PermanentError` |
| `ingest.py` | URL policy (`classify_url`, SSRF protection, restricted platforms need a recorded permission), direct and yt-dlp downloads, `fit_format_to_disk`, provenance, dedupe |
| `monitor.py` | Source monitoring: RSS/Atom feeds, channels (yt-dlp or Rumble's page grid), live HLS recording with restart/overlap handling, baselines, backfill |
| `inbox.py` | Watch folder with optional sidecar metadata |
| `transcribe.py` | Chunked, resumable faster-whisper transcription (a STUB transcriber exists for tests only) |
| `segment.py` | Content-led excerpt selection. Profile **v4** is default, v3 is one setting away |
| `safety.py` | Flags violence advocacy, dehumanisation, extremist terms and owner watch-list words for review |
| `captions.py` | Caption style (Anton font, bundled), ASS generation, real-glyph-width fitting (`FontMetrics`) |
| `render.py` | Caption phrasing, FFmpeg filter graph, 1080×1920 render, thumbnails, output checks |
| `pipeline.py` | Job handlers: process recording, render clip, regenerate clip |
| `notify.py` | "Clips ready" email (Gmail app password or Outlook via Graph); **held until approved in-app** |
| `publishing/` | TikTok and Instagram clients: implemented, mock-tested, **not connected** |
| `doctor.py`, `setup_wizard.py` | Health checks and the 6-step guided setup, with a real end-to-end self-test |
| `health.py`, `power.py`, `tailscale.py`, `autostart.py`, `maintenance.py` | Downtime log and dashboard banner, keep-awake, phone access, start at sign-in, housekeeping |
| `web/` | FastAPI app, templates (dashboard, review, clip, sources, settings…), mobile-first CSS |

Tests are in `clipping-engine/tests/` (about 130 tests). Real Whisper and Windows behaviour are verified in GitHub
Actions (Ubuntu + Windows + an oldest-Python 3.10 job + a Windows install job).

## Status (what is verified, and how)

| Area | Status |
|---|---|
| Import → real Whisper → selection → vertical render with Anton captions → review/approve/reject → download | **Verified** in CI and **on the owner's PC** |
| One-click Windows install/update + setup assistant + self-test | **Verified on the owner's PC** (setup 6/6; self-test clip plays, 8/8 expected words recognised) |
| Rumble channel monitoring | **Working on the owner's PC.** The listing reads Rumble's embedded video grid with one request per check. On 16 Nov the monitor picked up a new upload by itself ("ASSEMBLE THE ARMY!!!", 66 min), downloaded it at 480p to fit the disk, and started processing it |
| Rumble single-video download | **Verified on the PC** (25.6-min video at 1080p → 6 clips, all play) |
| Rumble **live** recording | **Not yet observed.** The live flag is read from the page's `live`/`livestream_status` fields; a real live broadcast hasn't happened during a session |
| Email notifications | Implemented, mock-tested, **never sent**. Two notifications are held until the owner approves recipient and wording in Settings |
| TikTok / Instagram | Implemented, mock-tested, **not connected** (needs the owner's developer apps). Nothing has ever been published |
| Phone access (Tailscale) | Implemented, tested only against a fake CLI; owner hasn't set it up |
| Selection quality | v4 improved clip starts; **clip endings remain the weakest point** (some end before a topic is finished) |

## Work completed on 16 November 2026 (most recent session, run on the owner's PC)

1. **Rumble listing fixed.** Rumble wasn't refusing: it served the full page (HTTP 200) to the engine's honest
   User-Agent. Its redesign moved videos into JSON inside `<rum-videos-grid>`, which yt-dlp's channel extractor
   doesn't read, so every check came back empty. That also made yt-dlp request `?page=2..4`, which explains the
   earlier HTTP 429s. `monitor.rumble_grid_entries()` now parses that JSON: own channel only, shorts skipped,
   scheduled streams left for the next check, live status from the page. yt-dlp remains the fallback.
2. **No back-catalogue flood.** A channel whose only earlier "success" was the old bogus listing now takes a
   baseline (existing videos are noted, not downloaded).
3. **Disk-aware downloads.** Rumble serves HLS with no file size, so `max_filesize` never applied.
   `ingest.fit_format_to_disk()` picks the highest resolution (≤1080p) that fits free space from bitrate ×
   duration, plus the transcription WAV and a 1 GB reserve. If nothing fits, it's a plain retryable error.
4. **Setup crash fixed.** In unattended installs, Windows' cp1252 console crashed setup on a "→" character.
   Console streams now replace characters they can't show.
5. **Dashboard leftovers cleared.** The old bogus "channel as one video" item is removed in any state, and its
   failed download job is cancelled with an explanation.
6. **Caption changes requested by the owner:**
   - Captions are **40% larger** (52 → 73 px), at the **same centre line** (72% of frame height). A two-line
     caption spans about y 1329–1445, clear of a 16:9 video, which ends around y 1264.
   - Captions **never run off either side.** Real Anton glyph widths are read from the bundled TTF (stdlib
     only, matches libass to the pixel). Any line wider than the space between the 120 px side margins is shrunk
     for that caption only.
   - The small "Excerpt · title · timestamp" label is **no longer burned in** (off by default; Settings →
     Caption style can turn it back on). Source and timestamps remain in each clip's description and provenance.
7. **Existing clips** were queued to re-render in the new style.

Commits on the branch for this work: `21d7113`, `33bd000`, `2d9f1e1`, `39df912` (plus earlier `0ed7655` from a
parallel session).

## Rules you must keep (learned the hard way)

- **Python 3.10 compatibility:** no nested same-quote f-strings, no backslashes inside f-string expressions.
- Text I/O always uses `encoding="utf-8"`. Keep **CRLF** line endings in `Start-Clipping-Engine.cmd`.
- Run FFmpeg with `cwd=` the clip folder and **relative** subtitle paths (Windows drive-letter escaping).
  Keep `Anton-OFL.txt` **outside** the fonts folder (libass loads every file in it).
- Port check uses `connect_ex`, never `bind`. Add DB columns only through `MIGRATIONS`.
- Never relax `classify_url` for restricted platforms without a recorded written permission. **Never add cookie,
  login or paywall support. Never disguise the User-Agent, impersonate a browser, or evade rate limits.**
  The honest User-Agent is `LocalClippingEngine/1.1 (self-hosted editorial review tool)`.
- America First Plus (americafirst.plus) is a paid subscription archive: not supported for automated retrieval.
- Never send email without the in-app approval (`is_approved`). Never publish without explicit owner
  instruction. Never report a publish as successful without platform confirmation. Keep TEST-ONLY/MOCK labels.
- Never commit the owner's email address or any secret. Secrets live in `.env` or Windows Credential Manager.
- Owner files are never deleted; `register_file` may delete a duplicate only inside `work/`.
- Live recordings finalise by atomically renaming `.partial` → `.mp4`.
- After changing selection or rendering: run `pytest`, `scripts/acceptance_test.py --stub` and
  `scripts/evaluate_selection.py`, then check CI.
- Git: push only to `claude/optimistic-noether-k52qjf`; don't open a pull request unless the owner asks.

## Known issues and next steps (priority order)

1. **Disk space on the owner's PC is critical:** about 1.4 GB free on C:. A 4-hour live show needs roughly
   5 GB. The engine now refuses downloads that won't fit (and says so), but it can't record a full show until
   space is freed or the data folder moves to a bigger drive (`CLIPENGINE_DATA_DIR`). Owner action.
2. **Observe a real Rumble live broadcast** (Mon–Fri 9pm CT ≈ 3am UK). Confirm the grid marks it `live: true`,
   recording starts, restarts don't duplicate frames, and the file finalises and gets clipped.
3. **Clip endings** still sometimes stop before a topic is complete (selection v4). Ideas not yet tried: an
   embedding-based topic model, or a local LLM re-ranker (optional Ollama hook exists, unevaluated).
4. **Python 3.10 is deprecated by yt-dlp.** Move the PC to Python 3.12 before yt-dlp drops 3.10 (the launcher
   keeps an existing 3.10 `.venv`, so the venv must be rebuilt).
5. **Launching from automation:** an engine started as a child of a short-lived shell gets killed when that shell
   exits. Start it via the Desktop shortcut, the start-at-sign-in option, or a detached process (e.g. WMI
   `Win32_Process.Create`).
6. **Two font tests** (`test_doctor_reports_real_state`, `test_libass_really_uses_bundled_anton`) and
   `test_dashboard_states_problems_plainly` fail only on the owner's PC: the first two from a very long checkout
   path, the third because the disk really is low. They pass in CI, and the installed doctor reports Anton loaded.
7. Owner-dependent and still open: approve email wording/recipient in Settings → Notifications; optional Tailscale
   phone access; optional TikTok/Instagram developer apps.

## How to start

1. Read `clipping-engine/HANDOFF.md` §0 (rules), §1B (deployment and the Rumble history) and §17–19
   (open issues, owner checklist, gotchas).
2. Check CI for the latest commit on the branch.
3. Ask the owner for the current dashboard state and the tail of `data\logs\clipengine.jsonl` if you can't
   reach the PC yourself.
4. Continue from "Known issues and next steps" above.
