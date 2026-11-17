# Handoff prompt for ChatGPT - Local AI Video Clipping & Editorial Review Engine (end of session 8, 17 Nov 2026)

> Paste everything below the line into ChatGPT, ideally with the repository attached.
> Read `clipping-engine/HANDOFF.md` §0, §1B, §1C, §17-19 and **§21-22** first. **The code and the tests are the source of truth** where documents disagree.
> This file supersedes `CHATGPT_HANDOFF_17NOV.md` (session 7), which is still accurate history.

---

## Your role

You are lead engineer of a working, locally hosted Python application on the owner's (James Callaghan's) Windows 11 Lenovo laptop. It monitors sources, records/downloads authorised video, transcribes locally (faster-whisper), selects self-contained excerpts using niche- and creator-specific editorial profiles, renders vertical 1080x1920 clips with burned-in Anton captions (and an optional top title), backs them up to Google Drive, and prepares them for **human review**. Nothing is ever published publicly by the engine.

Owner preferences: execute rather than propose; don't rebuild what works; Python-first, minimal model usage (scripts for deterministic work: inventories, migrations, health checks, diagnostics, reports); report honestly (never present mocked behaviour as real); no paid APIs/subscriptions/cloud compute; Python 3.10-compatible; Windows-first; plain English; one consolidated owner checklist at the end; no repeated interruptions. The owner is often on an iPhone without access to the laptop.

## Where things are

- **Repo:** `ce-optimisation/n8n-automation-portfolio`, folder `clipping-engine/`. Never touch the n8n templates at the repo root.
- **Source branch: `claude/optimistic-noether-k52qjf`**, head `35e0040` (session 8). `claude/wizardly-mccarthy-i5kf4r` is at the same commit (plus any later handoff-only commit). Branches were reconciled by a safe fast-forward, no force push. **CI for `35e0040` is green on both branches** (runs #60, #61: Python 3.10 job, Windows installer, ubuntu and windows full tests incl. real-Whisper genuine-recording test). No pull request exists; don't create one unless asked.
- **Install on the PC** (replaced on each update; never the source of truth): `%LOCALAPPDATA%\ClippingEngine` (`.venv\Scripts\python.exe`, data in `data\`, settings in `.env`). Update by re-running the installer, then restart fully (supervisor + worker). Dashboard `http://127.0.0.1:8765`; phone via Tailscale `https://laptop-rcinkek2.taildd0ae9.ts.net`. The installed copy has no git metadata.
- **Stack:** FastAPI + Jinja2 + HTMX, SQLite (**schema v6**, changes only via `SCHEMA`/`MIGRATIONS` in `db.py`), FFmpeg/libass, faster-whisper, yt-dlp, httpx. Tests: `python scripts/test_report.py [pytest args]` prints one concise summary (full suite about 10 minutes: 258 passed, 3 skipped locally; the 3 skips need the real Whisper model / live internet).

## What existed before sessions 7-8 (preserve)

14 versioned niche profiles + creator profiles (`editorial.py`); selection profiles v4/v5; Anton captions with side-fitting; persistent job queue, **one heavy job at a time**, live captures first (`jobs.py`); disk preflight and 1080p-minimum live planning (`storage.py`); Google Drive backup, MD5-verified idempotent (`cloud.py`; verified for real; sign-in expires about every 7 days); engine-owned raw-recording deletion only after **eight gates** (`lifecycle.py`); per-platform publication packages, YouTube *private* upload, manual handoff for others; email notifications held until the owner approves wording (not yet sending); Rumble channel monitor reading the embedded `<rum-videos-grid>` JSON with live detection (`monitor.py`); network-drop survival; account isolation by niche (`publishing/packages.py: account_allows`).

## Session 7 (commit f133367): six-brand portfolio monitor

Six brands, 18 creators, 27 `monitor_sources`:

- **America First Monitor** (politics, high-risk): Nick Fuentes (Rumble), Alex Jones (Rumble), Tim Dillon (YouTube + podcast)
- **Afterlive** (gaming_streams): IShowSpeed (YouTube + Twitch), Kai Cenat (Twitch + YouTube), N3on (Kick). **Exactly three; expansion deliberately deferred. Do not add creators.**
- **Operator Notes** (ai_business): Steven Bartlett/Diary of a CEO, Alex Hormozi, All-In (YouTube + podcast each)
- **The Spec Edit** (consumer_tech): Mrwhosetheboss, MKBHD, Linus Tech Tips (YouTube)
- **Dial Study** (watches): Nico Leonard, Teddy Baldassarre, Wristwatch Revival (YouTube)
- **Open Floor** (podcasts): Shawn Ryan Show, Joe Rogan Experience, Modern Wisdom (YouTube + podcast)

Modules: `portfolio.py` (roster, idempotent `seed`, `verify_source`, `owner_set`, `compute_state`, `reconcile`), `discovery.py` (`DiscoverySupervisor`), `events.py` (launch priority and polling policy), `platforms.py` (error taxonomy `ERROR_CODES`, `identity()` per adapter, `PodcastAdapter`), `portfolio_report.py` (read-only `python -m clipengine --portfolio [--json]`), `scripts/portfolio_admin.py`.

Key behaviours: **discovery is not acquisition**. Identity is verified only through an official API with an **exact official-name match** (Twitch/Kick also need a platform signal or the owner's "Confirm official"); unverified sources are not polled. Supervisor: per-source failure isolation, exponential back-off with jitter, YouTube quota budget 8,000 of 10,000 units/day, first poll baselines existing uploads, simulcast duplicates (same `dup_group` and start within 15 min, or equal titles) create no second job, queueing only when acquisition is PERMITTED, not duplicate, fewer than 4 download jobs pending and the disk fits it plus everything queued. Rumble is mirrored from the existing channel monitor, never re-polled. GTA VI launch week (17-26 Nov 2026) is a table row (`priority_events`): faster polling for Afterlive only (YouTube never under 5 min), tags from title/category text only, expires itself. States shown: MONITORING, DISCOVERY_ONLY, PERMISSION_REQUIRED, NEEDS_CREDENTIALS, NEEDS_VERIFICATION, OFFLINE, ERROR, PENDING, DISABLED.

## Session 8 (commit 35e0040): what was added or fixed

1. **CI fix:** `scripts/real_recording_test.py` asserted the superseded 73 px caption size; now 95 (73 + 30%).
2. **Acquisition rule tightened (`portfolio.acquisition_for`):** PERMITTED needs BOTH a platform permission (recorded written permission or owner-declared domain) AND the creator's own recorded permission (`creators.permission_json` with `granted_by` + `reference`, not expired). An enabled legacy Rumble `sources` row still counts as the owner's decision (existing Fuentes behaviour preserved). Permission for one creator never extends to another.
3. **Roster import (`roster_import.py`, input `research/portfolio_sources_2026-11-17.txt`):** validates 6 brands/18 creators/27 sources; never adds creators or sources; fills missing candidates through `owner_set` (locked, forces re-verification); reports conflicts instead of overwriting; never re-enables. Runs at start-up and from the Portfolio page. **Alex Jones' Rumble `https://rumble.com/c/TheAlexJonesShowLive` is a locked candidate, NEEDS_VERIFICATION.** "Add to Sources (switched off)" creates the existing-engine Rumble source disabled with `record_live=0`. `monitor_sources.notes` added (roster evidence is a note, not verification).
4. **Discovery fixes:** `sync_job_states()` moves queued items to imported / failed / (job vanished) discovered; finished items stop reserving disk; `ingest.handle_download_url` and `monitor._download_failed` also update `discovered_item_id` and set the recording's creator; failed items retry from the dashboard (`retry_item`); `seed()` links a channel source added later. **Added the missing `/clips/{id}/title` route** (the template posted to a route that did not exist); it re-renders only unapproved clips.
5. **Portfolio page (`/portfolio`):** brand > creator > source; identity, address/candidate, acquisition, last good check, discovered count, errors, held work, credential checklist, retry, validated edit form (`portfolio.edit_source`: platform host check, safe handle, priority 0-100; cannot verify or permit anything; identity edits force NEEDS_VERIFICATION). Tested in real Chromium at iPhone 13 width (no sideways scroll, 44 px targets, 16 px inputs).
6. **Title rendering, measured (`titles.py`, `scripts/calibrate_title.py`, `assets/title_calibration.json`):** calibrated from the owner's approved concept image (not stored in the repo). At 1080 px width: lead cap height 98.7 px = Anton ASS size **199.4** (the session-7 assumption of 120 was wrong), emphasis line 1.209x, yellow perimeter 4.6 px, black ring 9.2 px, centre at 26.6% of frame height. Effective size = x0.80 (lead 160). Markup: `*word*` white fill + black ring + yellow perimeter; `**word**` yellow fill; punctuation glues (`**Palestine**?`). `format_title` is deterministic (cuts at a word boundary, never adds words; if no markup it accents the last two words). **Enabled for America First Monitor** via meta `title_brands` (set once at first seed; `titles.set_brand_enabled` turns any brand on/off; reseeding never re-enables). Titles come from `clips.title`; **no title means no overlay.** Captions remain 95 px with auto-fit; a saved legacy 73 px override is treated as the superseded default.
7. **Publishing-account registry:** 24 clearly-labelled placeholders (6 brands x YouTube/TikTok/Instagram/Facebook), each mapped only to its brand's niche. `portfolio.set_account` and Accounts-page "Record a brand's real account" refuse an account owned by another brand; mapping an account to another brand's niche is refused. Nothing is created, connected or published.
8. **Editorial:** `spoiler_risk` and `sensitive_scene` review flags on `gaming_streams` (additive, applied to existing installs, never rejects, owner edits untouched).
9. **Laptop onboarding:** `Check-Portfolio.cmd` / `python -m clipengine --portfolio-check` (`setup_check.py`): version/commit, Python/packages, schema, credentials present/partial/missing (never values; ASCII-only output), identity verification once keys exist, one discovery health pass, accounts needing the owner's confirmation, live-report command, short next-actions list. `scripts/portfolio_admin.py import-roster|seed|verify|check|status`. `.env.example` documents where each key comes from. Verified that a database created by the pre-session-7 code upgrades cleanly to v6 (backup made; Fuentes creator/source adopted untouched).

## Evidence (be exact)

| Area | Status |
|---|---|
| Local suite | 258 passed, 3 skipped, 0 failed (`scripts/test_report.py`) |
| CI at 35e0040 | **Green** on both branches (all five jobs) |
| Nature of platform tests | **All MOCKED** (fake adapters / `httpx.MockTransport`). No platform API has ever been exercised with real credentials. **Nothing may be called "live verified".** |
| Identity verification | 0 of 27 sources VERIFIED. Fuentes verifies from the existing Rumble monitor's first successful listing on the PC. |
| Podcast feed discovery | Never run (Apple's directory was unreachable from the build sandbox) |
| Real Fuentes livestream test (armed 16-17 Nov) | **Result unknown, not inspected.** Do not report it passed until `scripts\live_report.py` shows a real broadcast. |
| Windows install | Not touched from the build environment; CI's Windows jobs passed |
| Title look | Compared with the concept only on a synthetic dark clip; no real America First clip rendered yet |
| Google Drive | Verified for real in session 6 |

## Rules you must keep

- Python 3.10 (no nested same-quote f-strings, no backslashes in f-string expressions); UTF-8 I/O (console output must survive Windows cp1252: use ASCII or `reconfigure(errors="replace")`); CRLF in `.cmd` files; FFmpeg with `cwd=` and relative subtitle paths; DB changes only via `SCHEMA`/`MIGRATIONS`.
- **Discovery is not acquisition.** Never weaken `ingest.classify_url`; never add cookies/logins, disguised user agents, proxies, browser automation or rate-limit evasion; never download protected/DRM/login-only media; podcast enclosures are recorded, never auto-downloaded.
- Never publish publicly; never send email without in-app approval; YouTube uploads stay private; the editorial review gate stays mandatory (extra care for America First Monitor: contextual review, never promotional handling of extremist material, no outrage optimisation, don't misrepresent a speaker).
- One heavy job at a time; live captures first; 1080p minimum for live; raw deletion only for engine-owned recordings after all eight gates; only engine-created Drive objects may be deleted; never delete OneDrive or personal files.
- Never print or commit secrets or the owner's email; never ask the owner to paste secrets into chat; never invent channel URLs, handles, ids or feeds.
- Tests must not depend on the host's disk, environment or network. Push only to the assigned development branch; no PR unless asked.

## Outstanding - owner actions (present once, prioritised)

1. Update the PC install and restart; open **Portfolio**.
2. On the laptop only, add to `.env`: `YOUTUBE_API_KEY`, `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET`, `KICK_CLIENT_ID`, `KICK_CLIENT_SECRET`; restart; double-click `Check-Portfolio.cmd`. Press "Confirm official" for any Twitch/Kick account that only name-matched.
3. Record each platform's written permission and each creator's own permission (Settings > Source permissions / Creators) before any recording or download. Until both exist everything is discovery-only.
4. Run `.venv\Scripts\python.exe scripts\live_report.py` and share the output (Fuentes test).
5. Record the real publishing accounts on the Accounts page when they exist (6 brands x 4 platforms).
6. Older: Drive reconnect about every 7 days; optional Outlook (Graph) app registration for email; confirm iPhone sign-in.

## Outstanding - engineering (suggested order)

1. **Live-validate every adapter once keys exist:** YouTube verify -> one real `check` pass -> Twitch -> Kick (field names least certain; the public API has no VOD/clip export) -> Apple podcast directory lookup. Fix whatever real responses reveal.
2. Try `ensure_recorders` and the Rumble "Add to Sources" flow on the PC with one permitted source; confirm one recorder per show during a simulcast.
3. Render a real America First clip with a real working title and compare with the concept; adjust only through `title_calibration.json` / `scripts/calibrate_title.py` if needed. Check non-16:9 and vertical sources visually.
4. Title workflow: titles are owner-entered (`clips.title`); there is no automatic title generation by design (no invented claims). Consider a review-page hint if the owner wants titles faster.
5. Confirm schema v6 upgrade on the owner's real database after installing (a backup is made first).
6. Older backlog: Outlook email, Twitch "Get Clips Download" for creator-authorised work, visual payoff detection for gaming/IRL, cross-stream de-duplication of clips, move the PC to Python 3.12.

## First steps for you

1. Read `HANDOFF.md` §21-22, `clipengine/portfolio.py`, `discovery.py`, `roster_import.py`, `titles.py`, `setup_check.py`, `tests/test_portfolio.py`, `tests/test_session8.py`.
2. Run `python -m clipengine --portfolio` (read-only) and `python scripts/test_report.py` to establish the baseline.
3. Work the engineering list; keep to the rules; report with exact evidence and one consolidated owner checklist.

---

## UPDATE, end of session 9 (17 Nov 2026) - read `HANDOFF.md` §23

Added: **selection profile v6 (context-complete)** in `complete.py` (default; legacy v1-v5 unchanged; no editorial length ceiling; PASS/REVIEW/REJECT
with reasons; zero clips is valid; old 180 s caps removed) and **verified-upload local cleanup** in `cleanup.py` (schema v7; OFF until the owner enables it;
Drive masters protected; restore from Drive; per-destination verification with manual confirmations labelled). 320 tests pass locally (3 skipped, as before);
everything involving Drive/platforms is mocked. Pending: run `scripts/compare_selectors.py --recording N` on the owner's two example recordings (Alex Jones /
Fuentes at NX; "Assemble the Army"); try cleanup and restore against the real Drive; Facebook draft upload is not implemented.
