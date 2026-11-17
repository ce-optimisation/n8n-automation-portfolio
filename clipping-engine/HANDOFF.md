# FULL HANDOFF PROMPT — Local AI Video Clipping & Editorial Review Engine

> **For the next engineer or agent:** paste this whole file into a new Claude Code session, or say
> "read `clipping-engine/HANDOFF.md` and continue". It is self-contained and consolidates **all** work from
> session 1 (initial build), session 2 (continuation), session 3 (automation batch), **session 4 (Windows
> deployment, onboarding and refinement — read §1B first)** and **session 6 (multi-niche expansion — §1C)**.
> Earlier handoff versions are in git history (`553c922`, the session-2 version in `ce0ba89`, then `c54dab7`).
> Session 3 ended at `337c833`. Session 4's commits and CI runs are listed in §1B.9. Treat this document as a map,
> not proof: re-inspect the checkout and CI before relying on any claim.

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

## 1. Current status (end of session 3)

| Area | Status | Evidence |
|---|---|---|
| Local workflow: import → real Whisper → selection → 1080×1920 render with Anton captions → review → approve/reject → download → restart persists | **Verified** | Acceptance test 9/9 with real faster-whisper `base.en` on Ubuntu and Windows (CI; see §1.2) |
| Test suite | **Green** | 89 tests (locally 87 passed + 2 environment-skipped; CI Ubuntu all passed; Windows has the known remote-403 expected failure) |
| **Authorised source monitoring** (feed / live HLS / permitted channel) → automatic import → clips | **Verified with local test sources** | `tests/test_monitor.py`: local RSS feed and a real-time local FFmpeg HLS live stream, including a recorder crash mid-stream, restart, finalisation, integrity check, inbox handoff, processing, no duplicate re-record, and restart recovery. **No third-party source has been monitored.** |
| Rumble / America First / America First Plus | **Disabled by design** | Rumble's terms require Rumble's prior written permission for automated access, and AF+ appears to be a subscription service (paywall). The source is saved disabled with an explanation. The exact Rumble channel URL was **not verified** (rumble.com and americafirst.plus are unreachable from the build sandbox). |
| Content-led selection (v3, default) | **Verified, reproducible** | No duration target. §1A.3: real-Whisper 58-min evaluation plus local exact and Whisper-like transcripts |
| Approved Anton caption style | **Verified** | libass log confirms the bundled Anton-Regular is loaded; real rendered frames inspected (§1A.4) |
| Ready to Review / Ready to Post / Sources / Notifications pages | **Verified** | Tests plus 390 px phone screenshots (no horizontal overflow) |
| Outlook email notifications | **Implemented; MOCK-tested only; never sent** | Microsoft Graph device-code flow and SMTP fallback are tested against mocks. Sending is **held** until the owner connects Outlook and approves the recipient and wording in-app. |
| Autostart at Windows sign-in + crash supervisor | **Verified (CI Windows + unit tests)** | `Enable/Disable-Autostart.cmd` run on a Windows runner with a temporary Startup folder; supervisor restart test |
| TikTok / Instagram | **Implemented, mock-tested, NOT connected** (unchanged; disconnected by default, tested) | Owner developer accounts needed |
| Owner's Windows PC | **Never run** | Owner action C1 |

### 1.1 Commit history (session 3)
Sessions 1–2: see git history (`f95fa7a` … `c54dab7`).

| SHA | Summary |
|---|---|
| `12ae89a` | Source monitoring, content-led selection v3, Anton captions, Ready to Review/Post, Sources, notifications, autostart, supervisor |
| `e9c735a` | Windows-safe live-segment cleanup (WinError 32 found by CI); whole-clip cohesion in v3 boundaries (found by the real-Whisper long-form evaluation) |
| `305bf24` | Handoff and README refresh |
| `881ca8f` | Handoff: final CI results (runs #13–#15) and real-transcript v3 metrics |
| `7449509` | Ignore the local `.review/` screenshots folder (owner-review images, not committed) |

### 1.2 CI runs worth knowing (session 3)

| Run | Notes |
|---|---|
| #11 `43655906924` (push of `12ae89a`) | Ubuntu: all passed, including real Whisper and acceptance. Windows: launcher and autostart OK, but **one real Windows bug** (segment unlink, WinError 32), fixed in `e9c735a`. |
| #12 `43655915197` (dispatch, 60 min) | Long-form real Whisper with crash test **passed**. Selection evaluation on the real transcript showed v3 ending mid-topic in 0.5 of clips, which led to the cohesion fix. |
| #13 `43658247477` (push of `e9c735a`) | **Success** on Ubuntu and Windows (unit, integration, real Whisper, acceptance, launcher, autostart). The WinError 32 fix is confirmed. |
| #14 `43658252185` (dispatch, 60 min, `e9c735a`) | **Success.** Real Whisper with crash/resume: 6/6 clips rendered, vocabulary recall 0.971, no failed jobs, work dir left empty. Real-transcript v3: ends_mid_topic **0.167** (was 0.5). |
| #15 `43658369597` (push of `305bf24`) | **Success** on Ubuntu and Windows. |

---

## 1B. Session 4: deployment, onboarding and refinement (read this first)

Session 4 ran in a **remote Linux container with no access to the owner's PC** (no computer was attached to the
session). Everything Windows-specific was therefore verified on GitHub's `windows-latest` runner, not on the
owner's machine. **The owner's PC has still never run the engine.**

### 1B.1 Status after session 4

| Area | Status | Evidence |
|---|---|---|
| One-click Windows install/update | **Verified on a clean Windows CI runner**; never run on the owner's PC | `windows-install` job: `Install-Clipping-Engine.cmd` → copy to install dir → venv → **portable FFmpeg downloaded** (none on PATH) → model → real self-test → shortcuts → update keeps `.env`/`data` → installed engine answers on :8765 |
| Setup assistant + doctor + real self-test | **Verified** (Linux sandbox, Windows + Ubuntu CI) | 32 s bundled NASA speech: real Whisper 8/8 expected words, 1080×1920, Anton captions |
| Genuine recording end to end | **Verified** with real Whisper (sandbox; CI on both OSes) | `scripts/real_recording_test.py`: 8-min NASA/USGS briefing, 11 checks |
| Selection v4 (default) | **Verified; partly improved** (§1B.4) | real-speech fixture + synthetic sets; ends-mid-topic NOT resolved |
| HLS restart overlap | **Fixed and verified** on a local real-time HLS stream with a hard-killed recorder | 0 repeated frames, frame-identical to source (§1B.5) |
| Phone access (Tailscale) | **Implemented; tested against a labelled FAKE CLI only** | No real tailnet was available. Owner must install/sign in, then press one button |
| Email notifications | **Implemented; mock-tested; NEVER SENT** | Guided page; template v2 awaits in-app approval |
| Historical imports / backfill | **Verified** (tests + genuine run) | recorded date, origin, dedupe, explicit backfill |
| Rumble / America First Plus | **Blocked by design; facts verified by web research** | §1B.7 |
| TikTok / Instagram | Unchanged: implemented, mock-tested, **not connected**; nothing published | tests still green |

### 1B.2 New files

```
Install-Clipping-Engine.cmd   one-click installer/updater (CRLF, ASCII). Works standalone (downloads install.ps1).
scripts/install.ps1           copy/download → %LOCALAPPDATA%\ClippingEngine (robocopy /XD .venv data tools /XF .env),
                              INSTALLED.txt, Desktop + Start-menu shortcuts, then launcher --setup. No elevation.
scripts/get_ffmpeg.py         portable FFmpeg (BtbN GitHub build, fallback gyan.dev) into tools/ffmpeg/bin; verifies
                              it runs and has libass + libx264.
scripts/real_recording_test.py  genuine-recording acceptance (NASA Landsat 9 briefing excerpt, real Whisper).
scripts/perturb_transcript.py   TEST-ONLY Whisper-like perturbation (was uncommitted in session 3).
clipengine/setup_wizard.py    `--setup [--yes]`: resumable guided setup (6 steps).
clipengine/doctor.py          `--doctor [--selftest]`: checks() + selftest() (own temp data dir; shares models).
clipengine/tailscale.py       status(), serve_status(), enable_serve(), setup_phone_access(), verify().
clipengine/health.py          Heartbeat (downtime log), items() for the dashboard banner, summary() for /api/status.
clipengine/power.py           KeepAwake thread (SetThreadExecutionState; per-process, no system change).
clipengine/assets/selftest/   nasa_landsat9_excerpt.m4a (32 s, 132 KB) + README.txt (source and rights).
tests/test_selection_context.py, tests/test_deploy_setup.py, tests/fake_tailscale.py (TEST-ONLY fake CLI),
tests/fixtures/landsat9_topics.json (hand annotation), landsat9_whisper_base_en.json.gz (real transcript).
```

### 1B.3 Launcher, installer, setup

- `Start-Clipping-Engine.cmd` (still CRLF): `:find_python` also checks `%LOCALAPPDATA%\Programs\Python\Python3xx`
  (a just-installed per-user Python is not on PATH yet); Python install via winget **or** the python.org installer
  (`/quiet InstallAllUsers=0 PrependPath=1`) when winget is missing; FFmpeg via winget, falling back to
  `scripts\get_ffmpeg.py`; first start (no `data\setup_state.json`, no arguments, interactive) runs `--setup`;
  `--setup` is consumed by the launcher; other arguments pass through. `CLIPENGINE_AUTO_FFMPEG=1` allows the portable
  download in non-interactive mode (used by the installer's `-NonInteractive`).
- `__main__`: `--doctor`, `--selftest`, `--setup`, `--yes`. **Doctor/setup bypass `--supervise`** (a non-zero exit
  would otherwise be "restarted" forever).
- `config.py`: `env_file_path()` (`CLIPENGINE_ENV_FILE` override — tests set it; never write the real `.env` in tests),
  `EDITABLE` map, `update_env_file()` (keeps comments/other keys; refuses newlines), `apply_settings()` (live +
  `.env`), `Settings.prevent_sleep`, `.env_file`, `.models_path`.
- Self-test: `doctor.selftest()` builds a test video from the bundled NASA audio + testsrc2, runs the real pipeline
  in `work/selftest_*` with `models_path` shared, requires ≥4 of 8 expected words, a rendered 1080×1920 clip and
  Anton `Dialogue:` lines, copies the clip to `data/selftest/`. It refuses to pass with the stub transcriber.

### 1B.4 Selection profile v4 (`segment.py`, default; v3 kept)

- `annotate_context()` after `annotate_boundaries()`: per sentence `follows_terminal`, `dependency` (0–1 with
  reasons), `edge` (turn | handoff | fragment | aside), `opener`, `ctx`. Cue regexes at the top of the file
  (TURN_START, HANDOFF, REPLY_START, ADDITIVE, LIST_CONTINUATION, BACK_REFERENCE, OPENER).
- Boundary sharpening: pause ≥2.5 s → ≥0.85, ≥6 s → 1.0; next is a turn → ≥0.9; after a hand-over → ≥0.95; next is
  an opener → ≥0.75; after a greeting/thanks → ≥0.8; non-terminal → ≤0.05; **after a question → ≤0.3**; next sentence
  dependent → weakened.
- `score_window` (v4): `context` penalty (head dependency, edge greeting/hand-over/fragment/aside, tail hand-over or
  fragment, next sentence continues, inner boundary ≥0.9, tail announces the next point); legacy `start_pen` only
  for un-annotated sentences; `density_substance` (markers / (2.5 + dur/15)); topic bonus needs low dependency and a
  strong lead or opener. `generate_candidates` skips starts that are mid-sentence.
- `refine_context()`: per chosen clip, look back ≤60 s for the nearest clean opening behind a clearly stronger
  break (no crossing of a ≥0.85 boundary, turn or hand-over), likewise extend a weak end; accept only if it fits
  `max_s`, does not collide, and scores within 0.08. Report key `context_adjusted`.
- Final v4 parameters: `shift_weight=0.25`, `context_weight=1.0`, `density_substance=True` (15 s), `close_share=0.4`
  (end extension while the next sentence shares ≥40% of its content words with the clip); the in-window penalty
  applies only to **hard breaks** (`Sentence.hard_break_after`: next is a speaker turn, after a hand-over, or ≥6 s
  silence), not to rank-based boundaries.
- **Results** (6 clips/recording; full table in README §5). Mean of seven sets, v3 → v4: starts at a paragraph
  opening 0.88 → 0.95; **mid-sentence starts 0.07 → 0**; multi-topic 0.19 → 0.07; housekeeping edge 0.07 → 0.02;
  starts mid-topic 0.29 → 0.21; **ends mid-topic 0.14 → 0.24 (worse)**; substantive 0.925 → 0.938.
  - Session-3 benchmark (synthetic 58 min, **real Whisper**; CI run #21 with the first v4 build, and reproduced
    locally with the final build): v3 starts 0.333 / ends 0.167; **v4 starts 0.167 / ends 0.50** (first build 0.667).
  - Real speech (NASA 55 min, base.en): para start 0.67 → 1.0, mid-sentence 0.17 → 0, multi-topic 0.50 → 0.33,
    ends mid-topic 0.33 → 0.17. small.en: mid-sentence 0.33 → 0, multi-topic 0.33 → 0.17, ends 0.33 → 0.50.
  - **Unresolved:** clip *endings*. v4's flagged endings are complete paragraphs followed by another paragraph on
    the same subject; lexical cohesion cannot see that on the template-generated sample (the paragraphs share only
    the topic name), and sweeps of density (15–60 s), continuation (0.45–0.65), shift (0.25–0.45) and close_share
    (0.4–0.6) did not move it. Ideas not tried: an embedding-based topic model; the optional local LLM re-ranker
    judging "is this a complete thought". v4 was kept as default on the strength of the real-speech clips; v3 is
    one setting away (`SELECTION_PROFILE=v3`). The owner should be told this plainly.
- Evaluate: `evaluate_selection.py` now reports `mid_sentence_start`, `multi_topic`, `procedural_edge`, accepts `.gz`.

### 1B.5 Live restart overlap (`monitor.py`)

- The recorder always starts with `-live_start_index -3` (also after a restart: deliberate overlap instead of a gap).
- `_video_packets()` (ffprobe `-show_data_hash md5`), `find_overlap(prev, nxt)` (unbroken run of identical packets
  from the first packet of `nxt` to within 3 packets of the end of `prev`, ≥5 packets — crash-truncated last packets
  are tolerated), `concat_listing()` writes `outpoint` on the previous segment. Segment files are not modified.
  Sidecar gets `overlap_trims`.
- Evidence: 40 s local HLS, recorder `kill -9` mid-stream: naive join 1305 frames (105 repeated) → fixed join 1200
  frames, 0 repeated, frame-for-frame equal to the source; the live integration test asserts no repeated frames.

### 1B.6 Dashboard and unattended running

- Routes added: `/health/dismiss`, `/recordings/{id}/provenance`, `/sources/{id}/backfill`,
  `/settings/phone/setup|disable|seen`, `/settings/unattended`, `/settings/notifications/recipient|transport`,
  `/settings/notifications/smtp/forget`. `/api/status` now includes `health` and `keep_awake`.
- Dashboard "Needs your attention": downtime periods (heartbeat), monitoring off, blocked/erroring sources, failed
  source items/jobs, held/failed notifications, expired mail sign-in, storage, password missing with a phone URL.
- OAuth state is stored in the `meta` table (15 min) instead of memory.
- Historical: `recordings.recorded_at`, `recordings.origin` (schema v4 migrations); `ingest.parse_recorded_at`,
  `date_from_filename`, `classify_origin`, `set_provenance`; `SourceMonitor.backfill()`.
- Feeds: DTD/entity declarations refused (`unsafe_feed`); a non-feed gives `bad_feed` with a readable explanation.
- Notifications: `TEMPLATE_VERSION = "2"` (recorded date line). SMTP password can live in the TokenStore (`smtp`),
  entered in the dashboard. **Any earlier chat "approval" of v1 wording is void; in-app approval is still required.**

### 1B.7 Source facts established in session 4 (web research; the sandbox cannot reach these sites)

- **Rumble channel (verified):** `https://rumble.com/c/nickjfuentes` — "Nicholas J. Fuentes", Verified badge, joined
  4 Mar 2021, description "America First with Nicholas J. Fuentes is LIVE Mon-Fri @9pm CT … Stream Archive:
  https://americafirst.plus/". The owner's example `https://rumble.com/v7i48ms-america-first-ep.-1776.html` matches
  AF 1776 (9 Nov 2026) in the AF+ archive listing.
- **Rumble terms** (last modified 1 Sep 2026), section "Restrictions on automated software": automated access is
  prohibited "without the prior written permission of Rumble". The engine's gate is unchanged.
- **America First Plus:** `https://americafirst.plus/`, operated by **NJF LLC**; a paid subscription archive (a
  July 2025 clip describes $15/$30/$100 monthly tiers — unverified against the site's own pricing page). Its Terms
  forbid copying, reproducing, scraping or extracting content "without the express prior written consent of the
  owner". No official download, feed or export was found. Now listed in `RESTRICTED_PLATFORMS` with that reason.
- **What the owner needs:** (a) for Rumble monitoring — Rumble's written permission for automated access to that
  channel, recorded in Settings → Source permissions (domain `rumble.com`); (b) for AF+ — NJF LLC's written consent
  (and it would still need a non-paywalled delivery method: the engine has no login/cookie support by design);
  (c) simplest and available today — files supplied by the rights holder, attached to the episode link (pending
  source) or dropped in the inbox with a sidecar. No permission request was sent in this session.

### 1B.8 Outlook / email facts (web research)

- Since mid-2024 a personal Microsoft account **without a directory** cannot create an app registration ("The
  ability to create applications outside of a directory has been deprecated"). A free Azure sign-up creates a
  Default Directory but asks for a payment card for identity verification. The M365 Developer Program rarely
  qualifies personal accounts.
- Outlook.com no longer accepts basic auth or app passwords for SMTP (OAuth2 only), so "SMTP with the Outlook
  password" is not an option.
- Therefore the page offers: **A** a Gmail (or other) sender with an app password → recipient stays the Outlook
  address; **B** Graph device-code with the owner's own client ID (needs the Azure directory; "Personal Microsoft
  accounts only"; **Allow public client flows = Yes**).
- No sign-in was performed and no email was sent in session 4.

### 1B.9 Commits and CI (session 4)

| SHA | Summary |
|---|---|
| `687df11` | Selection v4, real-speech fixture, perturbation script |
| `38d826b` | Live restart overlap removal |
| `ebf7f78` | Installer, launcher, setup assistant, doctor, Tailscale, health, keep-awake, historical imports, notifications page |
| `48efb1f` | Genuine-recording test, `windows-install` CI job, doctor/setup bypass supervisor |
| `d3e7b6c` | Overlap detection robust across FFmpeg versions; AF+ restriction; README/HANDOFF |
| `120e4b1` | Selection v4 final (hard-break penalty, end extension) and the full v3-vs-v4 table |

### 1B.10 CI results (session 4)

| Run | Commit | Result |
|---|---|---|
| #19 / #20 (push) | `48efb1f` | **Failed on Ubuntu only**: the new overlap detection found nothing on the runner's newer FFmpeg (ubuntu-latest is now 26.04) — a real compatibility bug, fixed in `d3e7b6c`. Windows tests, Windows installer job: passed. |
| #21 (dispatch, 60 min) | `48efb1f` | Long-form job **passed** (real Whisper, crash/resume). Selection on the real transcript: v3 ends 0.167 / starts 0.333; first v4 build ends 0.667 / starts 0.167 → led to the final v4 changes. Same Ubuntu unit-test failure as #19. |
| #22 / #23 (push) | `d3e7b6c` | **Success** on both branches: Ubuntu + Windows unit/integration (real Whisper), acceptance, **genuine-recording 11/11 on both OSes**, `windows-install` job. |
| #24 / #25 (push) | `120e4b1` | **Success** on both branches (same jobs). |

Artifacts per run (Actions → run → Artifacts): `genuine-recording-windows-latest` / `-ubuntu-latest` (the clips, a
frame of each, `report.json`), `windows-install-selftest` (the self-test clip rendered on Windows),
`acceptance-*`. The long-form job was **not** re-run on the final v4 build; its numbers for the final build were
reproduced locally with real Whisper (§1B.4).

Both `claude/optimistic-noether-k52qjf` and the session branch `claude/wonderful-galileo-vbbfln` carry identical
commits (the session's environment designated the latter; the owner's brief named the former).

### 1B.11 After the first install on the owner's PC (end of session 4)

- **The owner installed and started the engine on their Windows PC** (user `James`, install folder
  `C:\Users\James\AppData\Local\ClippingEngine`, **Python 3.10**). Not yet confirmed by the owner: that the
  self-test clip plays and that a real recording was processed there.
- First start crashed with `SyntaxError: f-string: unmatched '['` in `notify.py`: two nested same-quote f-strings,
  valid only on Python 3.12+. Fixed in `669f7ef`; CI now has an `oldest-python` job (3.10: compile, tests, real
  self-test). **Rule: code must run on Python 3.10** (no nested same-quote f-strings, no backslashes in f-string
  expressions).
- The owner states they hold **Rumble's written permission** and recorded it in-app (Settings → Source permissions:
  granted by "Rumble exec", reference "Email"), then added source `https://rumble.com/c/nickjfuentes` (channel,
  record live, every 2 min). First "Check now": the listing came back **empty** and was stored as one bogus baseline
  item named `nickjfuentes`; the next check got **HTTP 429 Too Many Requests** from Rumble.
- Fixed in `5edc464`: empty channel listing is an error (`empty_listing`), nothing stored; 429 backs off ≥30 min
  with a plain message; channel sources poll no faster than 5 min; ANSI codes stripped; bogus item removed on the
  next good listing. **The owner had not yet updated to this build.**
- **Open problem:** Rumble is throttling/filtering the engine's requests. Real Rumble compatibility remains
  unverified. Do NOT work around it with browser impersonation, cookies or a disguised User-Agent. The supported
  route is for the owner to ask their Rumble contact how automated access should be made (allow-list the engine's
  User-Agent `LocalClippingEngine/1.1 (self-hosted editorial review tool)` or the owner's IP, or an official
  API/feed). Fallbacks: replay picked up later by the same source if listing works; owner-supplied files.
- The owner also says they hold permission for **americafirst.plus** and wants it as a failsafe. Advised: record the
  permission, add `https://americafirst.plus/live` as a channel source and press Check now; it will probably fail
  because the site is subscription/sign-in and the engine has no login support by design. Clean alternatives: a
  direct `.m3u8`, an RSS feed, or files from the rights holder. Outcome not yet reported.

### 1B.12 Session 5: Rumble listing defects found offline (read with 1B.11)

Session 5 again ran in a cloud container with no access to the owner's PC and no route to rumble.com. It read
yt-dlp's own Rumble extractor (yt-dlp 2026.10.26, `yt_dlp/extractor/rumble.py`, `RumbleChannelIE`) and ran it
**offline with only the network layer replaced**. That exposed two real defects in the channel monitor:

1. **Unbounded paging.** `RumbleChannelIE` requests `?page=1`, `?page=2`, ... and stops only on HTTP 404. A page
   with no `videostream__link` elements (an empty or "checking your browser" page) therefore made one "Check now"
   send request after request (201 in the offline reproduction before the fake site answered 404). This is a
   likely cause of the owner's "empty listing" followed by **HTTP 429**. Fix: `list_channel` runs yt-dlp through a
   subclass whose `urlopen` stops after `LISTING_MAX_REQUESTS = 4` and reports `empty_listing`.
2. **Live never detected on Rumble.** Rumble listing entries are bare links (no title, no `live_status`). The old
   code turned "not stated" into "not live", so a live broadcast would have been queued as an ordinary download,
   never recorded live. Now `_live_flag` returns `None` for "not stated". `SourceMonitor._resolve_unknown` looks
   up **new** entries one by one (`probe_entry`, at most `PROBE_LIMIT = 3` per check, newest first). An
   **upcoming** entry is not stored, so it is checked again and recorded once it goes live. `_stream_live` asks
   the video page itself when the listing does not say. A refused lookup (429) backs the whole source off.

Tests: `tests/test_monitor.py::test_channel_listing_without_live_status_is_probed` (MOCK lister/prober) and
`::test_rumble_channel_listing_is_bounded_and_marks_live_unknown` (the real yt-dlp extractor, MOCK pages).

**Second report from the owner's PC (16 Nov, after updating):** the setup assistant stopped at step 1 because the
disk had 3 GB free (the doctor treated < 5 GB as FAIL), so steps 2–6 never ran; the dashboard still showed the
old build's raw Rumble 429 text with terminal colour codes (4 failures in a row, i.e. the 2-minute source had not
been removed and re-added); and download job #1 had failed as "requires a login or is restricted". Fixes:
- `ingest._download_with_ytdlp`: the restricted-video check matched the bare word "age", which is inside
  "webp**age**", so every "Unable to download webpage: HTTP Error 429" became a permanent "requires a login".
  Now 429 is checked first and raises `TransientError(retry_after=1800)` (jobs honour `retry_after`); restricted
  markers are whole phrases (`RESTRICTED_MARKERS`); ANSI codes stripped.
- `_is_collection_url`: a channel/playlist address (offline check against yt-dlp's extractor table) is refused
  as a single-video download, so retrying the bogus "nickjfuentes" item can never fetch a whole channel.
- `doctor`: data-folder space is FAIL only below 1 GB, WARN below 20 GB with plain steps to free space;
  the setup assistant prints the warning and continues.
- `health`: source errors are shown without colour codes (older stored errors included).
Tests: `test_rate_limit_on_download_is_not_reported_as_login_required`, `test_low_disk_warns_but_does_not_stop_setup`,
`test_channel_address_is_never_downloaded_as_one_video` (tests/test_deploy_setup.py).
The owner's PC has about **2.5 GB free on C:**: too little for a full live show (roughly 1–2 GB per hour).

**Third report (16 Nov 11:39, build 5371a7a):** setup completed (6/6). The Rumble check no longer got 429 but
stopped after 4 requests with no video links on the pages. `list_channel` now keeps the first page in memory
(never written to disk), and if yt-dlp finds no links it (a) reads `/v...html` video links from that same Rumble
page (`_links_from_page`, covers a redesign that renamed yt-dlp's CSS class; no extra request) and (b) otherwise
reports what the site sent (`_describe_page`: HTTP status, size, page title, server, robot-check detection).
yt-dlp also printed "Support for Python version 3.10 has been deprecated": moving the owner's PC to Python 3.12
is advisable before yt-dlp drops 3.10 (the launcher keeps an existing 3.10 `.venv`).

**Session 6 (run ON the owner's PC, 16 Nov 2026): Rumble diagnosed against the real site.** One request with the
engine's honest User-Agent returned **HTTP 200 and the real 464 KB channel page** (no robot check, no 429). The page
no longer has `videostream__link` anchors: the channel's videos are JSON inside `<rum-videos-grid><script
type="application/json">{"items":[...]}` (fields: `url`, `title`, `upload_date`, `permalink_id`, `by.relative_url`,
`live`, `live_placeholder`, `livestream_status`, `is_short`, `duration`). yt-dlp 2026.10.26 (latest) still looks for
the old class, so it found nothing and paged ?page=2..4. The `_links_from_page` fallback from `0ed7655` would have
picked up only `href="/v..."` anchors — on the real page that was a single recommendation from **another channel**.
Fixes:
- `monitor.list_channel`: for `rumble.com/c/<name>` (or `/user/`) it now calls `fetch_text` once and parses the grid
  (`rumble_grid_entries`): own-channel only, shorts dropped, `live` true → live, `live_placeholder` → skipped (re-checked
  next poll), `livestream_status` 0/None → not live, any other value → None (probed). One request per check; no
  per-video probes needed. 429 raises with "429" in the text (existing back-off). No grid → old yt-dlp path.
- `ingest.fit_format_to_disk`: Rumble video pages work with yt-dlp (HLS 360–1080p, no file size published), so
  `max_filesize` never applied. The format is now chosen from bitrate × duration + 16 kHz WAV + 1 GB reserve; if
  even 360p does not fit, a plain `TransientError` (retry in 1 h). On the owner's PC (1.8 GB free) a 25-min video
  downloads at 720p; a 4.4-hour episode is refused with "about 3.0 GB is needed and 1.8 GB is free".
- `test_dashboard_states_problems_plainly` fails on the owner's PC only because its disk really is low (the
  dashboard correctly shows the storage warning); it passes on CI.
Tests: `test_rumble_channel_grid_is_read_from_one_request`, `test_download_resolution_fits_free_disk_space`.
Live detection from the grid is inferred from field names; a real live broadcast has not yet been observed.

**Verified on the owner's PC after reinstalling `33bd000`:** setup 6/6 non-interactive (self-test 8/8 words, clip decodes); Rumble "Check now" -> status ok, 15 real videos stored as baseline from one request, nothing mass-queued; one real Rumble video (25.6 min) imported by URL -> downloaded at 1080p (768 MB) -> real Whisper base.en -> 6 clips 1080x1920 (26-91 s), all decode, in Ready to Review. `33bd000` also fixed setup crashing on a cp1252 redirected console (UnicodeEncodeError on "→"). Two libass font tests fail only when run from a very long checkout path on that PC; the installed doctor reports Anton loaded. Not yet seen: a live broadcast in the grid, live recording.

**Later on 16 Nov (`39df912`):** owner caption requests: Anton size 52 -> 73 (+40%), same centre line (0.72); `captions.FontMetrics` reads real glyph widths from the bundled TTF (stdlib; prediction 524 px = libass 524 px) and `fitted_size()` adds `s` to any caption whose line would cross the 120 px side margins (a 35-W line rendered at x 124-955); two-line block measured y 1329-1445. The burned-in source label is now off by default (`source_label=False`). The bogus channel item is deleted in any state and its download job cancelled (dashboard no longer shows a failed download). **The monitor picked up a new upload by itself** ("ASSEMBLE THE ARMY!!!", 66 min) and downloaded it at 480p because of low disk (1.4 GB free afterwards). An engine started as a child of a short-lived shell is killed with it: launch detached (shortcut, autostart, or WMI `Win32_Process.Create`). A shorter, self-contained summary for other assistants is in `CHATGPT_HANDOFF.md`.

**Still unverified against the real site:** whether Rumble serves the engine a real listing at all (the 429 may
persist; the answer is a sanctioned access method from Rumble, never a disguised client), whether a live
broadcast appears in the channel listing, recording via `yt_dlp --hls-use-mpegts`, and replay download.
Per check, a Rumble source now costs 1 listing request (2 for a channel with fewer than 15 videos), plus at most
3 lookups of about 2 requests each when new videos appear.

---

## 1C. Session 6 (16 Nov 2026, run ON the owner's PC): multi-niche expansion

Owner directive: turn the single-source engine into a multi-niche, multi-creator, multi-platform operation without
breaking what works; addendum 001: commercial discovery (separate from authorisation) and first-class Twitch, Kick
and YouTube Live source support. Everything below lives in the same engine (no second install, no new services).

### 1C.1 Status (be exact about evidence)

| Feature | Evidence level |
|---|---|
| Rumble listing (embedded grid), download, **automatic pick-up of a new upload**, disk-aware 480p download | **Verified on the owner's PC** |
| Captions 73 px, same centre (0.72), real-glyph-width side-margin fitting, no burned-in label | **Verified on the owner's PC** (12 real clips re-rendered/rendered; frame measured: caption y≈1324–1445 below the video band, x within 120–960) |
| Niche profiles (14 presets, versioned), creator profiles, combined policy, quality scores, rejection reasons, review flags, Whisper vocabulary hints | Unit-tested locally (`tests/test_expansion.py`); pages checked with TestClient |
| Selection v5 (ending completion) | Measured locally on 4 sets (below); **opt-in per niche**; v4 remains the global default |
| Queue: priority (live first), pause/resume, move, heavy jobs held during live capture | Unit-tested; existing queue tests green |
| Storage: stage estimates, live preflight, low-disk stop, pre-migration DB backup, relocation | Unit-tested; migration proven on a **copy of the owner's real DB** |
| Google Drive (drive.file): resumable, MD5-verified, idempotent uploads, quota holds | **MOCK-tested only** — READY FOR OWNER CONNECTION |
| Raw-recording deletion (8 gates, engine-acquired only, never retroactive), held recordings, clip retention | Unit-tested with a MOCK Drive; in production it stays inactive until Drive is connected (gate 6) |
| Publication packages, niche-based account isolation, manual handoffs | Unit-tested |
| YouTube private upload (official resumable API, privacyStatus=private) | **MOCK-tested only** — READY FOR OWNER CONNECTION |
| Twitch (Helix) / Kick (public v1) / YouTube Data API discovery adapters | **MOCK-tested only** — READY FOR OWNER CONNECTION (free developer apps) |
| Twitch official clip download, Kick/Twitch/YouTube footage acquisition | **Not implemented as working**: Twitch's endpoint needs broadcaster/editor authorisation (Model A); others only via yt-dlp under recorded permission, unverified on those platforms |
| Facebook Reels draft upload | Not implemented (Meta lists DRAFT for video_reels but no official example; manual handoff instead) |
| X | Manual handoff only (pay-per-use API; no costs introduced) |
| Full-length live recording on the owner's PC | **BLOCKED by disk space** (~1.4 GB free; a 4-hour show needs ~8 GB incl. transcription, clips and the 1 GB reserve). The new preflight refuses to start rather than produce a broken file. |
| Live Rumble broadcast | Still **never observed** |

**Test evidence (owner's PC, Python 3.10, 16 Nov 2026):** full `pytest`: **151 passed, 3 skipped, 3 failed** — the 3 are
the known host-only failures (dashboard storage warning because the disk really is low; two libass font checks that fail
only from a very long checkout path — the installed doctor reports Anton loaded); they fail identically on the code before
this session. `tests/test_expansion.py`: 25 passed. `scripts/acceptance_test.py --stub`: 9/9 PASS (TEST-ONLY stub
transcript). CI: runs #46/#47 failed only in the genuine-recording step, which still asserted the old 52 px caption size;
fixed in this commit (asserts 73 px and no burned-in label).

### 1C.2 Architecture (shared, not per platform)

```
creators (research | shortlisted | active ...) ──┐            niches (versioned profiles)
platforms.py adapters (discovery only) ──────────┤                     │
sources (+creator_id) → monitor.py ──► ingest (classify_url gate) ──► recordings (+creator, niche, engine_owned)
                                                                        │
             jobs.py: ONE heavy job at a time, priority order, paused/held while recording
                                                                        │
  transcribe (creator/niche vocabulary hint) → segment.select (v4|v5) → editorial.assess (quality, reject, flags)
                                                                        │
                       render (Anton 73 px, fitted) → cloud.py (Drive, verified) → lifecycle.py (8 gates)
                                                                        │
            review (filters) → clip workspace → publication_packages (account isolation) → YouTube private /
            TikTok inbox / Instagram (existing) / manual handoff → metrics & commercial_entries (Results page)
```

New files: `editorial.py`, `platforms.py`, `storage.py`, `cloud.py`, `lifecycle.py`, `research.py`,
`publishing/packages.py`, `web/workspace.py`, templates `niches, niche_edit, creators, creator, research, queue,
storage, accounts, analytics, platforms, handoff, _clip_workspace`, `research/research_dataset.json`,
`research/RESEARCH_REPORT_2026-11.md`, `tests/test_expansion.py`. Schema **v5** (tables niches, niche_history,
creators, social_accounts, publication_packages, cloud_objects, metrics, commercial_entries, niche_scores; new
columns via `MIGRATIONS`). Settings: `TWITCH_CLIENT_ID/SECRET`, `KICK_CLIENT_ID/SECRET`, `YOUTUBE_API_KEY`,
`CLIPENGINE_GOOGLE_CLIENT_ID/SECRET` (**prefixed on purpose**: the owner's PC has an unrelated machine-wide
`GOOGLE_CLIENT_ID` that must never be picked up), `RAW_DELETE_AFTER_CLOUD=1`, `REJECTED_CLIP_HOURS=24`,
`PUBLISHED_CLIP_DAYS=7`, `CLOUD_MIN_FREE_GB=2`, `DEFER_HEAVY_WHILE_RECORDING=1`, `DISK_RESERVE_GB=1`,
`RECORD_GB_PER_HOUR=1.5`, `MAX_CONCURRENT_JOBS=1` (only 1 supported).

### 1C.3 Selection v5 results (6 clips per recording; lower is better for "ends mid-topic")

| Set | v4 ends mid-topic | v5 ends mid-topic | other metrics |
|---|---|---|---|
| Real speech, NASA 55 min, Whisper base.en | 0.167 | 0.167 | identical |
| Synthetic 58 min, exact timings | 0.0 | 0.0 | identical |
| Synthetic 9 min, exact timings | 0.333 | **0.167** | substantive 0.935 → 0.933, otherwise identical |
| Synthetic 58 min, Whisper-like perturbation | 0.167 | 0.167 | identical |

v5 = v4 + (a) a concluding-line reward and a penalty for cutting just before a meaning-changing qualification,
(b) carrying the end through a question's answer, a qualification ("but/however/to be fair…"), a debate reply, and
(c) following the clip's distinctive subject (salient terms) across paragraph breaks to the furthest paragraph end
that still scores within 0.15. A modest, real improvement with no regressions; niche presets use v5, the global
default stays v4 (one setting away), and politics stays on v4.

### 1C.4 Rules added this session (keep them)

- Discovery never authorises retrieval; licensing is not a research score; research creators never get sources.
- Raw deletion only for `engine_owned=1` recordings created after `meta.engine_owned_since`; owner uploads, inbox
  files and older recordings are never deleted automatically. The decision is written before the file is removed.
- Only engine-created Drive objects (rows in `cloud_objects`) are ever deleted; scope `drive.file`.
- Accounts receive only clips of niches mapped to them; YouTube uploads are always private; nothing is posted publicly.
- Tests must not depend on the host's disk or environment (conftest pins tiny storage figures and clears platform
  credentials).
- Long streams: chunked, checkpointed transcription with resume, selection before any rendering, dedupe keys and
  SHA-256 dedupe, live restart-overlap trimming are in place. **Not implemented:** skipping low-value sections and
  cross-stream clip de-duplication (no reliable signal yet; would risk losing material).

### 1C.5 Operational readiness for the first real livestream (16 Nov 2026, evening)

| Item | State | Evidence |
|---|---|---|
| Phone dashboard | **Working over Tailscale** (iPhone sign-in itself still to be confirmed by the owner) | `DASHBOARD_PASSWORD` set in `.env` (strong random, also in Windows Credential Manager, service `local-clipping-engine`, user `dashboard_password`; never in Git/logs). Tested from the PC via `https://laptop-rcinkek2.taildd0ae9.ts.net`: unauthenticated → /login, wrong password refused, right password → HttpOnly + SameSite=Strict + Secure cookie (30 days), pages 200, cross-origin POST 403. Tailscale serve is tailnet-only. |
| Live detection on Rumble | **Verified against real Rumble data** (another channel's live stream; same page layout) | Channel grid item `live: true, livestream_status: 2` → engine parses live=True; yt-dlp on a live Rumble page: `is_live`, 1080p HLS selected by the recorder's format. Fuentes' own broadcast not yet observed. |
| 1080p minimum (owner requirement) | Implemented | `LIVE_MIN_HEIGHT=1080`: live capture and downloads never drop below 1080p for disk reasons; `storage.live_plan` records what fits at 1080p, the low-disk stop keeps what was captured; preflight needs ≥0.5 h at 1080p. A full 4.5 h show needs ~10.5 GB free. |
| Unattended lifecycle | **Verified end to end on disposable engine-generated media** (real FFmpeg live HLS, real recorder, real render; stub transcript; MOCK Drive) | `test_live_capture_to_verified_cloud_copy_then_safe_raw_deletion`: detection → recording (heavy work held) → finalise → automatic import → transcribe/select/render → upload/verify → raw deleted; clips, transcript, provenance kept; nothing published. Drive full → raw kept. `test_failed_render_or_pending_job_blocks_raw_deletion`. |
| Google Drive | **Connected and verified for real (16 Nov 2026, evening)** | Owner Google Cloud project, Desktop OAuth client, `drive.file` scope; app left in *Testing* (publishing needs a homepage + privacy URL), so Google expires the sign-in about every 7 days: reconnect on Storage. `scripts/cloud_selftest.py` all 6 checks PASS (upload, MD5/size, no duplicate on retry, byte-identical retrieval, own test files deleted). All 12 existing clips uploaded and verified (48/48 objects). Bug found and fixed on the way: the dashboard login sent the cross-site Google callback to /login (commit 362bac0). |
| Windows unattended | Ready, with caveats | Engine starts at sign-in (Startup folder `ClippingEngine.cmd`); runs detached from any Claude session; AC sleep = never (High performance plan); plugged in; no Windows Update restart pending. Lid-close action not exposed by this PC's power plan: keep the lid open. Engine does not start before sign-in (after a reboot someone must sign in). Wi-Fi only (recorder restarts on reconnect while the stream is live; restart limit 30). |
| Disk | **20.8 GB free**: a full 4.5 h show fits at 1080p | Cleanup: test artefacts 1.55 GB; Claude desktop Cowork VM images 11.4 GB (owner-approved; service stopped first; Claude re-creates them when needed); the two raw test recordings 1.28 GB at the owner's request (12 clips, transcripts, provenance kept). OneDrive and personal folders untouched. |
| Evidence after the show | `scripts/live_report.py` (read-only) | Prints detection/recording times, size, restarts, transcription time, clips, Drive ids, deletion gates, review queue. "PENDING" until a broadcast is detected. |

## 1A. Session 3: what was built (read this first)

### 1A.1 Source monitoring (`clipengine/monitor.py`, Sources page)

- **Source kinds:**
  - `feed`: RSS/Atom with video enclosures, `media:content` or links.
  - `hls`: a direct `.m3u8` live stream.
  - `channel`: a yt-dlp flat listing, only for permitted or allowlisted domains.
- **Authorisation** (`check_source_allowed`):
  - A restricted platform (e.g. rumble.com) without a `source_permissions` row is blocked.
  - Otherwise the host must be in `ALLOWED_URL_DOMAINS` or have a recorded permission, and pass the
    public-address/SSRF check.
  - A blocked source is **saved disabled** with an actionable `status_detail`.
  - Every poll re-checks authorisation and disables the source if it no longer passes.
- **Polling:**
  - Runs in its own thread (`SourceMonitor`, every 30 s, for sources that are due), so long transcriptions never
    delay live detection.
  - Errors back off exponentially (capped at 6 h).
  - Fetches use `fetch_text`: SSRF checks on every redirect hop, a peer-IP check, a 5 MB limit.
- **New items:**
  - On the first successful poll, existing items are stored as `baseline` and not downloaded, unless `backfill`
    is greater than 0.
  - Later items create `source_items` rows (unique per `source_id` and `item_key`) and existing `download_url`
    jobs.
  - A `source_item_id` in the job payload updates the item to `imported` or `duplicate`.
  - The `download_url:failed` hook marks the item `failed`.
- **Live recording:**
  - The recorder is FFmpeg for HLS (`-protocol_whitelist http,https,tcp,tls,crypto`, `-c copy -f mpegts`,
    `-live_start_index -1` on restarts) or `python -m yt_dlp --hls-use-mpegts` for permitted channels.
  - Output is `seg_NNN.ts` files in `work/live/<item>`.
  - `supervise()`: if the recorder exits while the stream is still live, a new segment starts (up to
    `LIVE_RESTART_LIMIT` times). `MAX_RECORDING_HOURS` is enforced.
  - **Broadcast identity:** the playlist's segment URIs are stored in `source_items.fingerprint`. A playlist
    sharing any URI with a recent recorded item is the same broadcast and is not re-recorded. An already-ended
    playlist on the first poll is a baseline.
  - `_finalize()`:
    1. join the segments with the concat demuxer into MP4 with `+faststart`
    2. `verify_recording` (video and audio streams, at least `MIN_RECORDING_SECONDS`, decode test of the last
       10 s)
    3. write `<name>.mp4.source.json` into the inbox, then atomically rename `.partial` to `.mp4`
    4. mark the item `handed_off`; segment deletion is best-effort
  - `recover()` at start-up: `recording` items resume (new segment) if still live, otherwise finalise.
- **Inbox sidecars** (`inbox.read_sidecar`):
  - Ours: `<file>.source.json`, giving `source_type='monitor'`.
  - External: `<file>.json` or `<stem>.json` / `.info.json` (Multi-Stream-Recorder or yt-dlp style), giving
    `source_type='recorder_import'`.
  - The sidecar is moved to `recordings/…/recorder_sidecar.json`.
  - `register_file(dedupe_by_url=False)` is used, because one live URL serves many broadcasts.
- **Recorder evaluation:**
  - **Multi-Stream-Recorder:** MIT licence, Python/Tkinter, yt-dlp/streamlink, Windows-first, MP4 plus JSON
    sidecar. Not adopted as the default: it is a separate GUI with no permission enforcement, while the built-in
    monitor uses the same underlying tools. It is supported through the inbox and its sidecar, but **untested
    against its real output**.
  - **Rekordly:** unreachable and not findable, so not evaluated.

### 1A.2 Durations, clamps and robustness

- Transcripts are clamped to the media duration (`pipeline.clamp_segments`).
- Clip windows that start beyond the media are skipped.
- `render_clip` refuses windows outside the media.

### 1A.3 Selection v3 (`segment.py`, default `SELECTION_PROFILE=v3`)

- No duration target (`fit_weight=0`). The limits are `CLIP_MIN_SECONDS=15` and `CLIP_MAX_SECONDS=150`.
- `annotate_boundaries()`: for each gap, the cosine-distance rank of the 3 sentences either side and the pause
  rank are averaged. Dangling "And/But/So…" (×0.3) and question→answer (×0.5) weaken the gap.
- **Whole-clip cohesion** (added after the real-transcript run): the end boundary is weakened by the cosine
  similarity between the whole clip and the next 3 sentences; the start boundary likewise with the previous 3.
- Penalties:
  - continuation, i.e. a weak end boundary plus a weak start boundary
  - an unanswered question at the end
  - straddling a strong internal boundary
- The topic-opener bonus is 0.12. Quality floors mean weak clips are never used as padding.
- **Evaluation** (substantive / procedural / ends mid-topic / starts mid-topic):

  | Transcript | Profile | Substantive | Procedural | Ends mid-topic | Starts mid-topic |
  |---|---|---|---|---|---|
  | 58 min, **real Whisper**, before the cohesion fix (run #12) | v2 | 0.919 | 0.015 | 0.0 | 0.333 |
  | 58 min, **real Whisper**, before the cohesion fix (run #12) | v3 | 0.932 | 0.0 | **0.5** | 0.0 |
  | 58 min, exact, after the fix | v3 | 0.935 | 0.0 | **0.0** | 0.333 |
  | 58 min, Whisper-like, after the fix | v3 | 0.934 | 0.0 | **0.0** | 0.333 |
  | 9 min, exact, after the fix | v3 | 0.924 | 0.015 | **0.0** | 0.5 |
  | 9 min, Whisper-like, after the fix | v3 | 0.917 | 0.015 | **0.0** | 0.5 |
  | Real Whisper 58 min (CI #14), after the fix | v3 | 0.936 | 0.0 | **0.167** | 0.333 |
  | Real Whisper 58 min (CI #14), for comparison | v2 | 0.935 | 0.0 | 0.167 | 0.333 |

  "Whisper-like" is a TEST-ONLY perturbation of the exact transcript: word ends stretched into pauses, timing
  jitter, merged segments. The script is in the session scratchpad and is not committed. Known weakness: v3 still
  starts one paragraph into a topic in 33–50% of clips on synthetic data. Caveat: the sample text is
  template-generated.

### 1A.4 Captions (`captions.py`)

- **Font:** official Anton v2.116 (Google Fonts build, from npm `@expo-google-fonts/anton`;
  sha256 `97aae409…de1d0`), stored as `assets/fonts/Anton-Regular.ttf`. The licence is in `assets/Anton-OFL.txt`.
  It must **not** live in the fonts folder, because libass tries to load every file there.
- **Rendering:** `ass=captions.ass:fontsdir=<relative path>`. `fontsdir_for()` uses `os.path.relpath`, falling
  back to copying the font next to the clip if the data folder is on another drive.
- **Style:**
  - Anton 52 px, Bold 0 (no faux bold), white `&H00FFFFFF`, black outline `&H00000000` 3 px, no shadow
  - BorderStyle 1; the optional backing uses BorderStyle 3 with a translucent box
  - `{\an5\pos(540, 0.72·H)}` centring
  - at most two lines, `max_chars` derived from Anton's measured 0.391 em average advance and 120 px side margins
  - sentence case is never altered
- **Verification:** `verify_font()` renders a frame at verbose log level and parses `fontselect`. It passes only
  if `Anton-Regular` is selected **and** "Loading font file …Anton-Regular.ttf" appears. Substitutes are reported
  (the classic Arial preset becomes LiberationSans on Linux). The result is stored in the `caption_font_check`
  key-value entry at start-up and shown in the checklist.
- **Settings:** preset, size, outline, position, on/off, backing and source label are stored in the key-value
  table `caption_style`, clamped to safe ranges, with a live preview at `/settings/captions/preview.png`.

### 1A.5 Notifications, Ready to Post, autostart

- **`notify.py`:**
  - Notifications are rows in `notifications` (unique `dedupe_key`).
  - `ready:<rid>` is created when a recording's last clip finishes, and only if at least one clip is reviewable
    (`Pipeline._maybe_complete`, also called from the render failure hook).
  - `failed:rec<rid>` comes from `JobQueue.failure_listeners`; `disk:<date>` from maintenance.
  - **Status is `held` unless `is_approved`**: the stored approval's `to` equals `NOTIFY_TO` and its template
    version equals `TEMPLATE_VERSION` (1).
  - Approval is an in-app owner action: type the recipient and tick the box. Changing the recipient or the
    template revokes it.
  - Sending is the `send_notification` job (max 6 attempts).
  - **Graph:** device code at `login.microsoftonline.com/{OUTLOOK_TENANT=consumers}/oauth2/v2.0/devicecode`,
    scopes `offline_access Mail.Send User.Read`, tokens in the `TokenStore` under `outlook`, `POST
    /me/sendMail` with inline 240 px JPEG thumbnails (cid). A 202 means accepted; 401 refreshes once; 429/5xx
    are transient.
  - **SMTP fallback** also uses inline related images.
  - Emails link only to `DASHBOARD_URL` pages (sign-in required); no media links.
  - **No email has ever been sent.** `NOTIFY_TO` is **not** committed anywhere. The owner supplied their address
    in chat; it belongs only in their local `.env`.
- **Ready to Post** (`/ready`):
  - approved clips that pass `editorial_gate`
  - export checks (`validate_video` against the TikTok and Instagram limits)
  - download, copy description, SRT
  - `manual_posts` per platform (posted flag, optional http(s) link, note)
  - approved-but-blocked clips listed with their reasons
- **Autostart:** `autostart.py` writes `%APPDATA%\…\Startup\ClippingEngine.cmd`, which runs `start /min
  Start-Clipping-Engine.cmd --no-browser --supervise`. `CLIPENGINE_STARTUP_DIR` overrides the folder for tests.
  There is a Settings toggle plus `Enable-/Disable-Autostart.cmd`.
- **Supervisor:** `python -m clipengine --supervise` restarts the child on a non-zero exit (5 s back-off; after 5
  crashes in 10 minutes it waits 10 minutes). Exit codes 0, 2 and 3 stop supervision. The launcher uses
  `--supervise` by default.
- **Platform labels** (`setup_status.platform_label`): Not connected / Awaiting developer approval / Upload
  available / Upload failed / Platform-confirmed …. It never shows a mocked success.

### 1A.6 Final CI verification (filled in at the end of session 3)
- **#13** (push `e9c735a`): success on Ubuntu and Windows.
- **#14** (dispatch, 60-min long-form on `e9c735a`): success.
  - The crash test hard-killed the worker after 1 checkpointed chunk; the restarted worker logged the resume and
    reused the extracted audio.
  - 6/6 clips rendered (4.8–13.5 s each); vocabulary recall 0.971.
  - Real-transcript selection (57.7 min):

    | Profile | Substantive | Procedural | Ends mid-topic | Starts mid-topic | Duration min/mean/max (s) |
    |---|---|---|---|---|---|
    | v1 | 0.800 | 0.114 | 0.500 | 0.333 | 52.5 / 54.8 / 57.1 |
    | v2 | 0.935 | 0.0 | 0.167 | 0.333 | 41.3 / 50.7 / 58.2 |
    | v3 | 0.936 | 0.0 | 0.167 | 0.333 | 19.3 / 37.9 / 58.2 |

  - v3 (the default) matches v2's boundary quality with content-led lengths and no 55 s target.
  - Starts mid-topic is still 1 in 3 clips; this is the main remaining selection weakness (§17).
- **#15** (push `305bf24`): success on Ubuntu and Windows.

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

## 5. Data model (SQLite, schema_version 3; v3 added sources, source_items (+fingerprint), notifications, manual_posts)

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

**Status after session 4** (details in §1B): item 3 (Outlook) — set-up flow rebuilt, still never sent live;
item 4 (selection starts) — improved by v4, **endings remain the weak point**; item 5 (HLS overlap) — **fixed**;
item 6 (XML) — DTD/entities now refused; item 7 — OAuth state now persisted; Ollama re-ranker still unevaluated
(no Ollama available; deliberately deferred — it is optional and off unless a local server runs); multipart upload
still spools to the system temp folder before being copied (deferred: the Import page now steers large files to the
inbox folder, which involves no upload); item 8 (Multi-Stream-Recorder sidecar) — still unverified against real
output, though the sidecar reader now also takes dates. New open items: Tailscale flow untested against a real
tailnet; the installer's "no Python at all" and "no winget" branches are untested (the CI runner has Python);
keep-awake is unit-tested with a replaceable setter but its real effect on Windows sleep is unverified.

1. **No third-party source has been monitored live.** Monitoring is verified only against local test feeds and
   streams.
   - The `channel` kind's yt-dlp listing is mock-tested.
   - Real sources need the owner's authorisation (`ALLOWED_URL_DOMAINS` or a recorded permission).
2. **Rumble (America First) is blocked** pending Rumble's written permission, and the channel URL is unverified.
   AF+ is presumed to be paywalled, so it is not supported for automated retrieval.
3. **Outlook sending has never been executed live** (mocked only).
   - Whether a personal Microsoft account can create the Entra app registration is unconfirmed; use the SMTP
     fallback if it can't.
   - Graph returns 202 "accepted", not proof of delivery.
4. **v3 still starts one paragraph into a topic** in about 1 clip in 3. Confirmed on the real-Whisper 58-minute
   transcript (CI #14): starts_mid_topic 0.333 and ends_mid_topic 0.167, the same as v2, so it is no regression.
   The next selection improvement should target starts, e.g. pulling the start back to the strongest boundary
   within the preceding 1–2 paragraphs when the cohesion between them and the window is high.
   - Raising the lexical weight in `annotate_boundaries` made results worse; it was reverted to 0.5/0.5.
5. **Live HLS restart overlap:** a restarted segment can repeat about 1 segment of content (`live_start_index -1`).
   It is not trimmed.
6. **XML feeds** are parsed with stdlib ElementTree, which relies on expat's entity-expansion limits. Feeds come
   only from authorised hosts.
7. Carried over from earlier sessions: TikTok/Instagram never connected live; Ollama re-ranker unevaluated;
   multipart upload spooling; OAuth state held in memory.
8. Multi-Stream-Recorder's sidecar format is untested against real output.

## 18. Owner action checklist (prioritised; present it once, at the end)

**Session 4 version (supersedes the list below where they differ):**

1. **Install.** Download `Install-Clipping-Engine.cmd` (GitHub → branch `claude/optimistic-noether-k52qjf` →
   `clipping-engine` → the file → *Download raw file*), double-click it, accept the prompts. *Return:* the last
   lines of the setup assistant ("Setup complete" or the failing step) and the "Computer:" line.
2. **Watch** `%LOCALAPPDATA%\ClippingEngine\data\selftest\selftest_clip.mp4` and confirm the captions look right.
3. **Phone:** install Tailscale on PC + iPhone, sign in on both, then Settings → Phone access → *Set up phone
   access*. *Return:* whether the address opens on the phone.
4. **Email:** Settings → Notifications → steps 1–3 (Option A Gmail app password, or Option B Outlook client ID).
   *Return:* "approve" in-app; optionally type SEND and say whether it arrived.
5. **Sources:** obtain written permission (Rumble) or files from the rights holder; see §1B.7.
6. Optional: TikTok / Instagram developer apps (unchanged).

Earlier checklist (session 3), kept for reference:

1. **C1 — Install and launch on the Windows PC (mandatory).**
   - Download the branch ZIP, extract it, and double-click `clipping-engine\Start-Clipping-Engine.cmd`.
   - Accept the Python and FFmpeg prompts.
   - Settings: click **Download Whisper model now**, and check the Setup checklist (caption font should show
     Ready).
   - *Return:* the console "Computer:" line and the checklist statuses.
2. **C2 — First real recording (mandatory).** Upload or drop into the inbox a short recording you are authorised
   to use.
   - *Return:* whether clips appeared in Ready to Review, whether the Anton captions look right on your phone,
     and the processing stats line.
3. **C3 — Email notifications (needed for "notify me").**
   - Register a free Entra app (README §7b), then put `OUTLOOK_CLIENT_ID`, `NOTIFY_TO=<your address>` and
     `DASHBOARD_URL` in `.env`.
   - Settings → Notifications → Connect Outlook (device code), preview the sample, approve, and optionally send a
     test (type `SEND`).
   - *Return:* whether the test email arrived.
   - Nothing is sent before this approval.
4. **C4 — Authorised automatic sources (needed for hands-off import).** For each source you are authorised to
   retrieve automatically:
   - add its domain to `ALLOWED_URL_DOMAINS` or record a written permission
   - add it on the Sources page and click Check now

   For **Rumble/America First**, either obtain Rumble's written permission (then record it in Settings → Source
   permissions and add the channel URL), or keep using pending sources with files supplied by the rights holder.
   *Return:* the Sources page status.
5. **C5 — Optional:**
   - enable start at sign-in
   - Tailscale for phone access and HTTPS email links
   - TikTok and Instagram developer apps (unchanged from session 2)
   - `INSTALL_CUDA_LIBS=1` for an NVIDIA GPU

**Pending owner decision at the end of session 3:** the owner was sent the sample "clips ready" email
(`notify.build_ready`, TEMPLATE_VERSION 1), a rendered Anton caption frame and a Ready to Post phone screenshot,
and was asked to reply "approve email wording" or request changes.
- If the wording changes, bump `TEMPLATE_VERSION`. Approval is keyed to it, so any earlier approval lapses.
- In-app approval (Settings → Notifications) is still required on the owner's PC, whatever is agreed in chat.

**Readiness verdict:** local import → transcription → content-led clips → Anton captions → review → email is
complete and verified in CI. Hands-off operation depends on C3 (approval to send) and C4 (an authorised
source). Publishing remains manual (download and post) until the platforms are connected.

## 19. Gotchas — never break these

- **Session 3 additions:**
  - Never enable a restricted-platform source without a recorded written permission.
  - Never send email without `is_approved`.
  - Never commit the owner's email address.
  - Keep `Anton-OFL.txt` **outside** the fonts folder (libass loads every file there).
  - Keep the caption text case untouched.
  - Live finalisation must atomically rename `.partial` to `.mp4`, so the inbox never imports a half-written file.
  - Keep `dedupe_by_url=False` for live and sidecar imports.

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

## 21. Six-brand portfolio monitor (session 7, 17 Nov 2026)

**What exists (schema v6).** `brands`, `monitor_sources`, `discovered_items`, `monitor_events`, `priority_events`
plus creator columns `uid, brand_key, kind, roster, acquisition_state`. Code: `portfolio.py` (roster, idempotent
`seed`, `verify_source`, `owner_set`/`confirm_identity`, `compute_state`, `reconcile`), `discovery.py`
(`DiscoverySupervisor`, one thread, started beside `SourceMonitor`), `events.py` (launch-event priority and the
polling-interval policy), `portfolio_report.py` (`python -m clipengine --portfolio [--json]`, read-only),
`scripts/portfolio_admin.py` (seed / verify / check / status, no model use), `titles.py` (top title),
dashboard page **Portfolio** (`/portfolio`).

**Principles.** Discovery is not acquisition. Nothing is queued unless acquisition is PERMITTED for the host
(recorded written permission or owner-declared domain), the item is not a duplicate, fewer than 4 downloads are
pending and the disk holds it plus everything already queued; otherwise the item stays metadata
(`permission_required` / held). Discovery never starts a recorder or heavy job. Rumble keeps using the existing
channel monitor; the supervisor only mirrors its state (`legacy_source_id`). For a permitted non-Rumble live-capable
source `ensure_recorders` links/creates ONE legacy channel row per show (simulcast twins share `dup_group`).

**Identity.** `candidate_handle` values are HINTS looked up through the platform API; `canonical_url` and
`platform_id` are written only from the API answer, and VERIFIED needs an exact official-name match (Twitch/Kick
also need a platform verification signal, else the owner confirms on the dashboard). Unverified sources are not polled.
No creator identity in this repository has been verified against a live API yet.

**States** (what was observed, not configured): MONITORING, DISCOVERY_ONLY, PERMISSION_REQUIRED, NEEDS_CREDENTIALS,
NEEDS_VERIFICATION, OFFLINE, ERROR, PENDING (verified, not yet checked), DISABLED. Error codes: needs_credentials,
not_found (invalid identity), unauthorised, rate_limited, offline, inaccessible, permission_missing, temporary_failure.

**Launch priority.** Row `gta_vi_launch` (Afterlive, 17-26 Nov 2026, launch 19 Nov): normal polling 5 min and 90 s
while live or a verified scheduled start is within 30 min; YouTube never faster than 5 min and a daily quota
budget of 8,000 of 10,000 units is enforced. Tags (`priority_tags`) come from title/category text only. It never
changes permissions, other brands, or job priorities, and expires by itself. Future events are table rows.

**Rendering (approved 17 Nov).** Caption default 73 -> 95 px (a saved 73 override is treated as the superseded
default). Top title: `titles.py`, Anton, accent words `*like this*` get a yellow perimeter, effective size =
reference 120 px x 0.80 = 96 px, auto-fitted. The approved mock-up image was NOT in the repository, so
`reference_size` is an assumption and the visual comparison is PENDING. The title is OFF until meta key
`title_style` = `{"enabled": true}` (`titles.save_style`); it uses `clips.title`.

**Tests.** `tests/test_portfolio.py` (all mocked: fake adapters / `httpx.MockTransport`). Nothing here is a live
verification. This sandbox could only reach googleapis (no key), so no platform was exercised for real.


## 22. Session 8 (17 Nov 2026): reconciliation, registry completion, calibrated title, laptop onboarding

**CI defect fixed.** Session 7's `scripts/real_recording_test.py` still asserted caption size 73; CI failed on both OSes at that step only.
It now asserts 95 (73 + 30%).

**Registry.** `roster_import.py` + `research/portfolio_sources_2026-11-17.txt` (the owner's 27-line input list). It validates 6 brands /
18 creators / 27 sources, never adds creators or sources, fills missing candidates through `owner_set` (locked, re-verification forced),
reports conflicts instead of overwriting, never re-enables. Runs at start-up and from the Portfolio page. Alex Jones'
`https://rumble.com/c/TheAlexJonesShowLive` is recorded as a locked candidate, NEEDS_VERIFICATION; "Add to Sources (switched off)" creates the
existing-engine Rumble source disabled with `record_live=0`. `monitor_sources.notes` added (roster evidence is a note, not verification).
**Acquisition now needs BOTH** a platform-wide permission and the creator's own recorded permission (`creators.permission_json` with granted_by +
reference, not expired); an enabled legacy Rumble source still counts as the owner's decision (existing behaviour kept).

**Discovery fixes.** `sync_job_states()` moves queued items to imported / failed / (job vanished) discovered, so nothing stays queued and
finished items stop reserving disk; `ingest.handle_download_url` and `monitor._download_failed` also update `discovered_item_id` and set the
recording's creator; failed items can be retried from the dashboard; `seed()` links a channel source added later. (Correction in session 9: `/clips/{id}/title` already existed in `web/workspace.py`; session 8 briefly added a duplicate in `app.py`, removed in session 9. The route now formats the title and re-renders only unapproved clips.)

**Portfolio page.** Grouped brand > creator > source; shows identity, address/candidate, acquisition, last good check, discovered count, errors,
held work, credential checklist, retry, edit form (mobile tested with Chromium at iPhone 13: no sideways scroll, 44 px targets, 16 px inputs).
`portfolio.edit_source` is validated (platform host, safe handle, priority 0-100) and cannot verify or permit anything.

**Title, measured.** `scripts/calibrate_title.py concept.png` measures the approved concept and writes `assets/title_calibration.json` (numbers only;
the image is not stored). Result at 1080 px width: lead cap height 98.7 px = Anton ASS size 199.4 (NOT the earlier 120 assumption), emphasis line 1.209x,
yellow perimeter 4.6 px, black ring 9.2 px, centre at 26.6% of height. Effective size = x0.80 (lead 160). `*word*` = white fill + black ring + yellow
perimeter, `**word**` = yellow fill; punctuation glues. `titles.format_title` is deterministic (cuts at a word boundary, never adds words,
emphasises the last two words if none marked). On for America First Monitor via `title_brands` (set once at first seed, owner can turn off with
`titles.set_brand_enabled`; reusable by any brand); other brands off. Titles come from `clips.title`; no title means no overlay.

**Accounts.** 24 clearly-labelled placeholders (6 brands x YouTube/TikTok/Instagram/Facebook), each mapped only to its brand's niche; `set_account`
and the Accounts page "Record a brand's real account" refuse an account owned by another brand; mapping an account to another brand's niche is refused.
Nothing is created, connected or published.

**Editorial.** `spoiler_risk` and `sensitive_scene` review flags on gaming_streams (additive, added to existing installs, never rejects).

**Laptop onboarding.** `Check-Portfolio.cmd` / `python -m clipengine --portfolio-check` (ASCII output, no secrets, no admin, never edits `.env`):
version/commit, Python/packages, schema, credentials present/partial/missing, identity verification when keys exist, discovery health pass,
accounts needing confirmation, live-report command, next actions. `scripts/portfolio_admin.py import-roster|seed|verify|check|status`.
`scripts/test_report.py` prints one concise test summary.

**Still not done / unverified:** no platform API has been exercised (no credentials); the Fuentes live test was not inspected; the installed Windows
copy was not touched; the title was compared visually with the concept only on a synthetic dark source.

## 23. Session 9 (17 Nov 2026): context-complete selection (v6) and verified-upload local cleanup

**A. Selection profile v6 (`complete.py`, `segment.PROFILES["v6"]`; legacy v1-v5 unchanged).** Principle: completeness of meaning ->
editorial value -> duration suitability. Duration is a constraint: **no editorial ceiling** (the old 3-180 s owner-adjust cap and the
niche editor's <=180 s cap were removed; platform limits are shown, never enforced by cutting). Method: split into coherent regions;
bounded seeds (<=120) of the most substantive sentences; expand each seed (earliest necessary opening, an announced topic start, through a
question's answer, a meaning-changing qualification, the point's own explanation; stop at a topic change or after 6 sentences adding nothing new);
trim trailing repetition/leading asides; assess on opening / conclusion / meaning / central idea / cohesion / economy -> PASS / REVIEW /
REJECT with reasons; rank by completeness (length earns nothing; ties go to the shorter). Per-type preferred ranges (`complete.PREFERRED`:
political 40-180, interview 35-180, business 30-120, podcast 35-180, gaming 12-60, comedy 10-45) are preferences. A window beyond its range is
"exceptional" only if it stays substantive, cohesive, concludes and does not repeat (the reason is stored and shown); otherwise a shorter complete
prefix is preferred and the long one is demoted to REVIEW. A bare short extract that does not stand alone is REJECT. Zero clips is valid:
`process_recording` marks the recording ready with a note (never a failed job), and the raw file is held, not deleted. REVIEW clips get a
`context_review` flag (the editorial gate then needs an explicit decision) and are offered only when nothing stronger exists (or composite >= 0.8).
Computational safeguards (`Budget`: seeds, windows scored, expansion sentences) are not editorial limits; a 3-hour transcript is processed in
seconds. `clips.completeness_json` / `duration_class` store the assessment. Default selector is now v6 (`SELECTION_PROFILE`, niche presets); existing
niches on v4/v5 move to v6 ONCE (recorded in niche history; a later owner choice sticks). `scripts/compare_selectors.py [--recording N]` compares
v4/v5/v6 (read-only) and is the way to test the owner's two example recordings (not available in the build environment: real-media evaluation pending).

**B. Verified-upload local cleanup (`cleanup.py`, schema v7).** Separate from the raw-recording lifecycle. States: LOCAL_PRESENT, CLOUD_VERIFIED,
DESTINATIONS_PENDING, GRACE_PERIOD, READY_FOR_CLEANUP, DELETING, LOCAL_DELETED, RESTORE_PENDING, RESTORED, CLEANUP_HELD, CLEANUP_ERROR. **Off until the
owner enables it** (Storage page; or per clip). A local MP4 is removed only if all gates pass, re-checked (with a fresh MD5 and a live Drive re-read)
immediately before deleting, after an atomic claim and after the decision + evidence are written: managed engine render at the exact path; not kept/held;
editorial gate (approved, rights, flags decided); final (no job, unchanged >60 s); Drive video verified against THIS file (size+MD5, re-read) and
metadata/transcript objects verified; >=1 explicit required destination, each genuinely complete; brand-isolated accounts; consistent records; recovery
window (default 24 h) since the LAST required confirmation. Destination states (never invented; manual is labelled): YouTube = private upload with
video id and `privacyStatus=private` (api); TikTok = only a confirmed publish (inbox delivery is not a draft -> owner confirms); Instagram = only a
confirmed publish (a container is not a draft -> owner confirms); Facebook/X/Rumble = manual (owner confirms the draft/post exists, or records posted).
The owner can exclude a destination with a recorded reason, keep a clip locally, cancel/hold, delete one now (waives only the window and the global
switch), and restore from Drive (download -> size/MD5 check -> atomic replace; never overwrites a different local file; 24 h protection afterwards).
Drive masters are protected: `CloudService.delete_clip_objects` refuses once a clip is locally removed, and (with `KEEP_DRIVE_MASTER=1`, default) for
any non-rejected clip; published-clip retention now removes local files only and marks them restorable; the raw-recording `integrity` gate accepts a
cleaned clip whose Drive copy is verified. Dashboard: clip workspace (Completeness; Storage on this computer; Required destinations with confirm /
exclude), Storage page overview and switches, review-card badges. Crash recovery: `recover()` resolves a clip left DELETING.

**Not verified live:** no real Drive, YouTube, TikTok or Instagram call was made (fake Drive/mocks only); Facebook draft upload is NOT implemented
(manual); YouTube "processing complete" is not polled (the upload response's id + private status is the evidence).
