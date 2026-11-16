# Handoff prompt for ChatGPT — Local AI Video Clipping & Editorial Review Engine

> Paste everything below the line into ChatGPT (ideally with the repository attached or connected).
> State as of **16 November 2026, end of session 6** (multi-niche expansion), branch
> `claude/optimistic-noether-k52qjf`. The detailed engineering record is `clipping-engine/HANDOFF.md`
> (read §0, §1B, **§1C** and §17–19). The code is the source of truth where documents disagree.

---

## Your role

You are taking over as lead engineer of a **working, locally hosted application** on the owner's (James's) Windows
PC. It watches authorised video sources, records or downloads new videos, transcribes them locally, selects
self-contained excerpts using niche- and creator-specific editorial profiles, renders vertical 1080×1920 clips with
burned-in Anton captions, and prepares them for **human review** and platform handoff. Nothing is ever published
publicly by the engine.

Owner preferences: execute rather than propose; don't rebuild what works; collect owner actions into one prioritised
list at the end; report honestly (never present mocked behaviour as real); no paid APIs, subscriptions or cloud
compute; Python 3.10-compatible; Windows-first.

## What exists (end of session 6)

**One engine, many niches and creators.** FastAPI + Jinja2 + HTMX, SQLite (schema v5), FFmpeg/libass,
faster-whisper, yt-dlp. Dashboard `http://127.0.0.1:8765`: Dashboard, Import, Ready to Review (filters: creator,
niche, platform, date, status), Ready to Post, Sources, **Creators**, **Research**, **Niches**, **Queue**, **Storage**,
**Accounts**, **Results**, Recordings, Jobs, Settings, plus a **Platforms** capability page.

- **Editorial intelligence** (`editorial.py`): 14 versioned, editable niche profiles — AI/automation/SaaS business
  and consumer tech/PC hardware **active**; watches prepared; gaming, IRL, reaction, sports, podcasts, education,
  property, travel, psychology/philosophy/theology; politics (high-risk, contextual review, never promotional) and
  finance (compliance review). Creator profiles (content type irl / competitive_gaming / reaction / podcast / product /
  education, vocabulary used as Whisper hints, limitations). Each candidate gets a 0–100 quality score, rejection
  reasons (generic motivation, no specifics, no payoff, outrage bait, too dependent) and review flags (financial
  promotion, unverifiable claims, political context) that the publication gate enforces.
- **Selection**: profile v4 (stable default) and **v5** (ending-aware, used by niche presets). v5 reduced
  "ends mid-topic" 0.33 → 0.17 on one benchmark set and was identical to v4 on three others (no regressions).
- **Sources/platforms** (`platforms.py`): Rumble (working on the PC), Twitch Helix, Kick public API v1 and YouTube Data
  API adapters with explicit capabilities. Discovery ≠ acquisition: footage still needs an enabled source with a
  recorded written permission. Twitch has an official clip-download endpoint for broadcaster/editor-authorised work.
- **Queue** (`jobs.py`): one heavy job at a time; live captures first; owner pause/resume/reorder; heavy work held
  while a live recording runs.
- **Storage** (`storage.py`): per-stage disk estimates, live-recording preflight and low-disk stop, automatic DB
  backup before schema upgrades, owner-approved relocation of the data folder (type MOVE; effective after restart).
- **Cloud** (`cloud.py`): Google Drive with the minimal `drive.file` scope; resumable, MD5-verified, idempotent
  uploads; quota holds. **Lifecycle** (`lifecycle.py`): raw recordings deleted only for engine-acquired recordings
  and only after 8 gates incl. verified cloud copies; no-clip recordings held; clip retention (rejected 24 h,
  published 7 days, approved/unreviewed kept, "Keep" exempt).
- **Publishing** (`publishing/packages.py`): per-platform packages with editable text, account isolation by niche,
  YouTube **private** upload, existing TikTok inbox and Instagram clients, manual handoff for Facebook, X and Rumble.
- **Results** page: imported/entered metrics only, realised vs estimated money separated, review time, niche
  scorecard. **Research**: 22 niches ranked (audience vs profit), 27 creator candidates with sourced figures
  (`research/RESEARCH_REPORT_2026-11.md`, `research/research_dataset.json`).

## Evidence (be exact)

| Area | Status |
|---|---|
| Rumble listing, download, auto pick-up of a new upload, disk-aware 480p | Verified on the owner's PC |
| Captions 73 px, same centre, side-margin fitting, no top label | Verified on the owner's PC (12 real clips) |
| Profiles, selection v5, queue, storage, lifecycle gates, packages, research | Unit/integration-tested locally; see §1C of HANDOFF.md for test counts |
| Google Drive, YouTube private upload, Twitch/Kick/YouTube discovery | **MOCK-tested only — READY FOR OWNER CONNECTION** |
| Full-length live recording on the PC | **Blocked by disk space** (~1.4 GB free; ~8 GB needed for a 4-hour show) |
| Live Rumble broadcast | Never observed yet |
| Facebook Reels draft, X API | Not implemented (manual handoff; X API is paid) |

## Rules you must keep

- Python 3.10 (no nested same-quote f-strings, no backslashes in f-string expressions); UTF-8 I/O; CRLF in
  `Start-Clipping-Engine.cmd`; FFmpeg with `cwd=` and relative subtitle paths; DB changes only via `MIGRATIONS`.
- Never weaken `classify_url`; never add cookies/logins, disguised user agents, browser automation or rate-limit evasion.
  Research never authorises retrieval; permission for one creator never extends to another.
- Never send email without in-app approval; never publish publicly; YouTube uploads stay private; accounts only get
  clips from niches mapped to them.
- Raw deletion only for `engine_owned` recordings after all gates; never owner uploads or pre-existing recordings;
  only engine-created Drive objects may be deleted.
- Google client keys are `CLIPENGINE_GOOGLE_CLIENT_ID/SECRET` on purpose (the PC has an unrelated machine-wide
  `GOOGLE_CLIENT_ID`). Tests must not depend on the host's disk or environment variables.
- Push only to `claude/optimistic-noether-k52qjf`; no pull request unless asked; never modify the n8n templates.

## Owner actions (prioritised)

1. **Free disk space or relocate** (Storage page → "Move the data folder", only if another drive exists). Without
   ~8 GB free, full live shows cannot be recorded.
2. **Google Drive**: create a free Google Cloud project, enable Drive API (and YouTube Data API v3), OAuth client
   type *Desktop app*, put `CLIPENGINE_GOOGLE_CLIENT_ID/SECRET` in `.env`, restart, Storage → Connect.
3. **Social accounts**: add the accounts you own on the Accounts page and map them to niches; connect YouTube for
   private uploads. TikTok/Instagram developer apps are unchanged optional steps.
4. **Platform discovery**: free Twitch app (`TWITCH_CLIENT_ID/SECRET`), Kick app (`KICK_CLIENT_ID/SECRET`),
   YouTube API key (`YOUTUBE_API_KEY`); then "Check via official API" on research creators to verify handles.
5. **Permissions**: written permission (or campaign terms) for any creator you want to clip; record it on the
   creator page and add an authorised source.
6. Approve the email wording in Settings → Notifications; optional Tailscale for phone review.

## Next engineering steps

1. Observe a real Rumble live broadcast end to end (once disk space allows).
2. Exercise Drive and YouTube against the owner's real accounts; fix whatever the mocks missed.
3. Twitch "Get Clips Download" acquisition for creator-authorised (Model A) work.
4. Selection: visual payoff detection for gaming/IRL (audio energy, chat-rate, scene change) — transcript-only
   selection cannot see purely visual moments; cross-stream clip de-duplication.
5. Move the PC to Python 3.12 (yt-dlp has deprecated 3.10).
