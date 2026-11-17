# Handoff prompt for ChatGPT — Local AI Video Clipping & Editorial Review Engine (state at end of session 7, 17 Nov 2026)

> Paste everything below the line into ChatGPT, ideally with the repository attached.
> Read `clipping-engine/HANDOFF.md` §0, §1B, §1C, §17–19 and **§21** (this session) first. Where documents and code
> disagree, **the code and the tests are the source of truth**.

---

## Your role

You are lead engineer of a working, locally hosted Python application on the owner's (James Callaghan's) Windows 11
Lenovo laptop. It watches sources, records or downloads authorised video, transcribes locally (faster-whisper), selects
self-contained excerpts using niche- and creator-specific editorial profiles, renders vertical 1080×1920 clips with
burned-in Anton captions, backs them up to Google Drive, and prepares them for **human review**. Nothing is ever
published publicly by the engine.

Owner preferences: execute rather than propose; don't rebuild what works; Python-first and minimal model usage
(use scripts for deterministic work: inventories, migrations, health checks, diagnostics, reports); report honestly
(never present mocked behaviour as real); no paid APIs, subscriptions or cloud compute; Windows-first; Python
3.10-compatible; plain English; collect owner actions into **one** consolidated list at the end; no repeated interruptions.

## Where things are

- **Repo:** `ce-optimisation/n8n-automation-portfolio`, folder `clipping-engine/`. Never modify the n8n templates at the repo root.
- **Branches (important):** the engine code lives on `claude/optimistic-noether-k52qjf`. Session 7's work was pushed to
  `claude/wizardly-mccarthy-i5kf4r` (commit `f133367`), built on top of the optimistic-noether code, because that was the
  branch the session was assigned. **Reconcile the two** (e.g. merge `wizardly-mccarthy-i5kf4r` into
  `optimistic-noether-k52qjf`) before the owner updates their install. No pull request has been created; don't create one unless asked.
- **Install on the PC (replaced on each update; never edit as source of truth):** `%LOCALAPPDATA%\ClippingEngine`
  (`.venv\Scripts\python.exe`, data in `data\`, settings in `.env`). Update by re-running the installer, restart fully
  (supervisor + worker). Dashboard `http://127.0.0.1:8765`; phone via Tailscale `https://laptop-rcinkek2.taildd0ae9.ts.net`.
- **Stack:** FastAPI + Jinja2 + HTMX, SQLite (**schema v6**, changes only via `SCHEMA`/`MIGRATIONS`), FFmpeg/libass,
  faster-whisper, yt-dlp, httpx. CI: `.github/workflows/clipping-engine.yml`.

## What existed before session 7 (unchanged, preserve)

One engine, many niches/creators: 14 versioned niche profiles + creator profiles (`editorial.py`); selection profiles v4/v5;
Anton captions with side-fitting; persistent job queue with **one heavy job at a time** and live captures first (`jobs.py`);
disk preflight and 1080p-minimum live planning (`storage.py`); Google Drive backup with MD5-verified idempotent uploads
(`cloud.py`, verified for real, 48/48 objects; sign-in expires about every 7 days in Testing mode); engine-owned raw
recording deletion only after **eight gates** (`lifecycle.py`); per-platform publication packages, YouTube *private* upload,
manual handoff for others; email notifications held until the owner approves the wording (not yet sending);
Rumble channel monitor parsing the embedded `<rum-videos-grid>` JSON with live detection (`monitor.py`);
platform adapters Twitch/Kick/YouTube (`platforms.py`); network-drop survival for live capture and downloads.

## What session 7 built (all committed in `f133367`)

**1. Registry and seeding (`portfolio.py`).** Six brands and 18 creators/shows, one shared engine:
- America First Monitor (politics, high-risk editorial): Nick Fuentes (Rumble), Alex Jones (Rumble), Tim Dillon (YouTube + podcast)
- Afterlive (gaming_streams): IShowSpeed (YouTube + Twitch), Kai Cenat (Twitch + YouTube), N3on (Kick). **Exactly three; expansion deliberately deferred by the owner. Do not add creators.**
- Operator Notes (ai_business): Steven Bartlett/Diary of a CEO, Alex Hormozi, All-In (YouTube + podcast each)
- The Spec Edit (consumer_tech): Mrwhosetheboss, MKBHD, Linus Tech Tips (YouTube)
- Dial Study (watches): Nico Leonard, Teddy Baldassarre, Wristwatch Revival (YouTube)
- Open Floor (podcasts): Shawn Ryan Show, Joe Rogan Experience, Modern Wisdom (YouTube + podcast)

`seed()` is idempotent: adds only what is missing, adopts an existing creator/source (matches by uid, then owner URL, then
name) without overwriting `status`, `priority`, `permission_json`; never re-enables a disabled source; never overwrites fields in
`owner_locked_json` (set via `owner_set`). New creators start `needs_authorisation`. 27 `monitor_sources` rows result.

**2. Identity verification (never invent).** `candidate_handle` values are *hints*. `canonical_url`/`platform_id` are written only from the
platform's answer. VERIFIED requires an exact official-name match (YouTube `channels.list forHandle`); Twitch/Kick also need a platform
verification signal (Twitch partner) or the owner presses **Confirm official** on the dashboard. Podcasts resolve through Apple's public
directory with an exact-title match. Rumble has no identity API: it is verified by the first successful listing of the existing Rumble monitor
(circular by design; the seeder links the existing `sources` row by URL). Unverified sources are **not polled**. If a verified source's platform
id later changes, it returns to NEEDS_VERIFICATION.

**3. Discovery supervisor (`discovery.py`).** One thread beside `SourceMonitor`, state persisted in SQLite. Polls due sources (live first,
verified first, event-boosted next), max 8 per tick, per-source error isolation, exponential back-off with jitter, specific error codes
(`needs_credentials, not_found, unauthorised, rate_limited, offline, inaccessible, permission_missing, temporary_failure`), YouTube quota budget
8,000 of 10,000 units/day (3 units per check; never `search.list`). First poll of a source baselines existing uploads (noted, not downloaded).
Simulcast duplicates (same `dup_group` and start time within 15 min, or equal normalised titles) become `duplicate` and create no second job.
`route_pending()` queues a `download_url` job **only** when acquisition is PERMITTED (recorded written permission or owner-declared domain),
not a duplicate, fewer than 4 download jobs pending, and the disk holds this item plus everything already queued (`storage.estimate`; 2 h assumed if
duration unknown). Otherwise it stays metadata (`permission_required` / held with a reason). Podcast enclosures are recorded, never auto-downloaded.
Rumble is **mirrored** from the existing channel monitor, not re-polled. `ensure_recorders()` links/creates one legacy `sources` channel row per show
(simulcast twins share a group) for permitted, live-capable, non-Rumble sources so the existing recorder does the recording.
Discovery never starts a recorder or heavy job itself.

**4. Platform layer (`platforms.py`).** Error taxonomy (`ERROR_CODES`; network failure and 5xx = temporary; 403 quota = rate_limited; 404 = not_found;
401/403 = unauthorised), `identity()` on every adapter, new `PodcastAdapter` (RSS/Atom, guid keys, enclosure URL recorded, duration/pubDate parsing,
directory lookup).

**5. Launch priority (`events.py`).** Table-driven `priority_events`; seeded `gta_vi_launch` (Afterlive, 17–26 Nov 2026, launch 19 Nov): polling 5 min,
90 s when live or a verified scheduled start is within 30 min (floors: YouTube 5 min, Twitch/Kick 60 s). Tags `priority_tags` come from title/category text only
(never "every stream is GTA VI"). Changes polling cadence and review ordering only; never permissions, other brands, job priority; expires automatically.

**6. Visibility.** Dashboard **Portfolio** page (`/portfolio`: brand filter, per-source state, verification, acquisition, live status, last good check,
errors, discovered items, transitions; toggle / check-now / confirm-official; YouTube quota meter). Read-only diagnostics `python -m clipengine --portfolio [--json]`
(opens SQLite read-only, writes nothing, calls no platform). `scripts/portfolio_admin.py seed|verify|check|status`.
States: MONITORING, DISCOVERY_ONLY, PERMISSION_REQUIRED, NEEDS_CREDENTIALS, NEEDS_VERIFICATION, OFFLINE, ERROR, plus PENDING (verified, unchecked) and DISABLED.
MONITORING is only shown after a real successful check **and** permitted acquisition.

**7. Rendering (owner-approved).** Caption default 73 → **95 px** (+30%), auto-fit retained; a saved 73 px override is treated as the superseded default.
Optional **top title** (`titles.py`): Anton capitals in the upper band, `*accent*` words get a yellow perimeter with dark shadow, auto-fitted, effective size
96 px = 0.80 × an **assumed** 120 px reference. **The approved mock-up image was not in the repository**, so the reference size is unmeasured and the visual
comparison is **pending**. The title is **off** until meta key `title_style` = `{"enabled": true}`; it uses `clips.title`; `*` markup selects accents.

## Evidence (be exact)

| Area | Status |
|---|---|
| Test suite | 207 passed, 3 skipped locally (baseline before session 7: 163 + 3). 33 new tests in `tests/test_portfolio.py` |
| Nature of new tests | **All mocked** (fake adapters, `httpx.MockTransport`) plus one real FFmpeg render. None is a live verification |
| Live API verification of any of the 18 identities | **None.** The session sandbox reached only googleapis (no key); Apple/Rumble were blocked |
| Rumble listing/download/auto pick-up | Verified earlier on the owner's PC (session 6) |
| Real Fuentes livestream test (armed 16–17 Nov) | **Result unknown.** Not checked this session (no machine attached). Do not report it passed until `scripts/live_report.py` shows a real broadcast |
| Google Drive | Verified for real (session 6) |
| CI for commit `f133367` | **Not checked** |
| Python 3.10 syntax of new modules | Checked with `ast` feature_version 3.10 |
| New schema on the owner's existing DB | Not exercised on their real database (migration is additive; DB backup runs before upgrade) |
| Title look vs approved concept | **Pending** (asset absent) |

## Rules you must keep

- Python 3.10 (no nested same-quote f-strings, no backslashes in f-string expressions); UTF-8 I/O; CRLF in `Start-Clipping-Engine.cmd`; FFmpeg with `cwd=` and relative subtitle paths; DB changes only via `SCHEMA`/`MIGRATIONS`.
- **Discovery is not acquisition.** Never weaken `ingest.classify_url`; never add cookies/logins, disguised user agents, proxies, browser automation or rate-limit evasion. Permission for one creator/platform never extends to another. Never download protected/DRM/login-only media.
- Never publish publicly; never send email without in-app approval; YouTube uploads stay private; the editorial review gate stays mandatory.
- One heavy job at a time; live captures first; 1080p minimum for live; raw deletion only for engine-owned recordings after all eight gates; only engine-created Drive objects may be deleted; never delete OneDrive/personal files.
- Never print or commit secrets (dashboard password, Google/Twitch/Kick/YouTube keys); never commit the owner's email. Google keys are `CLIPENGINE_GOOGLE_*` on purpose. Never ask the owner to paste secrets into chat.
- Never invent channel URLs, handles, ids or feeds. Never call an integration "LIVE VERIFIED" because a mock passed. Preserve speaker attribution/provenance; flag misinformation, defamation, extremist advocacy, spoilers for human review; don't imply affiliation with creators.
- Tests must not depend on the host's disk/environment/network. Push only to the assigned development branch; no PR unless asked.

## Outstanding — owner actions (present once, prioritised)

1. Reconcile branches, update/reinstall, restart, open **Portfolio** and run `python -m clipengine --portfolio`.
2. Free developer credentials in `.env`: **YouTube API key**, **Twitch** client id + secret, **Kick** client id + secret (see `.env.example`). Then `python scripts\portfolio_admin.py verify`; press **Confirm official** for any Twitch/Kick account that only name-matched.
3. Supply **Alex Jones's official Rumble channel URL** (not guessed).
4. Record written acquisition permission per platform/creator in Settings → Source permissions. Until then everything is discovery-only.
5. Run `live_report.py` for the Fuentes broadcast and share the output.
6. Provide the America First Monitor title mock-up so the 120 px reference can be measured; then enable `title_style`.
7. From earlier sessions: Drive reconnect about every 7 days; optional Outlook (Graph) app registration for email; confirm iPhone sign-in; social accounts per niche; YouTube private-upload test on the real account.

## Outstanding — engineering (suggested order)

1. **Check CI** on `f133367`; run the full suite on the PC's Python 3.10; reconcile branches.
2. **Close the loop on queued discovery items:** `discovered_items.state='queued'` is never advanced to `imported`/`failed` when the download job finishes (the payload carries `discovered_item_id`, but `monitor._download_failed` only handles `source_item_id`). Add a completion/failure hook, and make `_disk_fits` ignore finished items.
3. **Dashboard editing of the roster:** `portfolio.owner_set` exists but there is no form to supply a Rumble URL/handle or change priority; Alex Jones's URL currently needs a Python call. Add a small edit form that goes through `owner_set` (locks the field, forces re-verification).
4. **Live validation once keys exist**, in this order: YouTube verify → one real `check` pass → Twitch/Kick → podcast directory lookup (Apple host was unreachable in the sandbox). Fix whatever real responses reveal (adapter field names for Kick are the least certain; Kick VOD/clip export is not available in the public API).
5. **`ensure_recorders`** is unit-untested against real hosts (it calls `monitor.add_source`, which does DNS and permission checks). Test on the PC with one permitted source and confirm a single recorder per show during a simulcast.
6. Verify the new `/portfolio` page on the phone over Tailscale; check mobile layout.
7. Rendering: measure the mock-up, set `reference_size`, compare a real America First clip side by side, then enable the title; test with real long/short titles and non-16:9 sources.
8. Editorial flags for gaming (spoiler / sensitive-scene) are a documented requirement but only the launch-event tag exists; add review flags to the Afterlive profile if wanted.
9. Earlier backlog: Outlook email, Twitch "Get Clips Download" for creator-authorised work, visual payoff detection for gaming/IRL, cross-stream de-duplication of *clips*, move the PC to Python 3.12.

## First steps for you

1. Read `HANDOFF.md` §21 and `clipengine/portfolio.py`, `discovery.py`, `events.py`, `tests/test_portfolio.py`.
2. Run `python -m clipengine --portfolio` (read-only) and `python -m pytest -q` (about 9 minutes) to establish the baseline.
3. Work the engineering list above; keep to the rules; report with exact evidence and one consolidated owner checklist.
