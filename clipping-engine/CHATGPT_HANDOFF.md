# Handoff prompt for ChatGPT — Local AI Video Clipping & Editorial Review Engine

> Paste everything below the line into ChatGPT (ideally with the repository attached or connected).
> State as of **16 November 2026, ~22:40 UK — armed for the first real livestream test** (session 6), branch
> `claude/optimistic-noether-k52qjf` (latest commits `362bac0`, `8625261`). The detailed engineering record is
> `clipping-engine/HANDOFF.md` (read §0, §1B, **§1C** and §17–19). The code is the source of truth where documents disagree.

---

## Your role

You are taking over as lead engineer of a **working, locally hosted application** on the owner's (James's) Windows 11
laptop. It watches authorised video sources, records or downloads new videos, transcribes them locally, selects
self-contained excerpts using niche- and creator-specific editorial profiles, renders vertical 1080×1920 clips with
burned-in Anton captions, backs them up to Google Drive, and prepares them for **human review** and platform handoff.
Nothing is ever published publicly by the engine.

Owner preferences: execute rather than propose; don't rebuild what works; collect owner actions into one prioritised
list at the end; report honestly (never present mocked behaviour as real); no paid APIs, subscriptions or cloud
compute; Python 3.10-compatible; Windows-first; plain English for the owner; recordings at **1080p minimum**.

## Where things are

- **Install (replaced on every update, never edit as the source of truth):** `%LOCALAPPDATA%\ClippingEngine`
  (`.venv\Scripts\python.exe`, Python 3.10; data in `data\`; settings in `.env`).
- **Source:** GitHub `ce-optimisation/n8n-automation-portfolio`, folder `clipping-engine/`, branch
  `claude/optimistic-noether-k52qjf`. Commit from a separate clone, then re-run the installer (or copy changed files
  for a hot fix) and restart.
- **Dashboard:** `http://127.0.0.1:8765` on the PC; on the phone `https://laptop-rcinkek2.taildd0ae9.ts.net`
  (Tailscale serve, tailnet-only). `DASHBOARD_URL` is set to that address so email links work on the phone.
- **Starting/restarting:** the engine starts at Windows sign-in from the Startup-folder `ClippingEngine.cmd`
  (`Start-Clipping-Engine.cmd --no-browser --supervise`). `.env` is loaded with `os.environ.setdefault`, so a changed
  setting needs a **full** restart (supervisor + worker), not just killing the worker. From an automation shell,
  launch detached via WMI (`Invoke-CimMethod Win32_Process Create`) or the engine dies with the shell.

## What exists (end of session 6)

**One engine, many niches and creators.** FastAPI + Jinja2 + HTMX, SQLite (schema v5), FFmpeg/libass,
faster-whisper, yt-dlp. Pages: Dashboard, Import, Ready to Review (filters: creator, niche, platform, date, status),
Ready to Post, Sources, Creators, Research, Niches, Queue, Storage, Accounts, Results, Recordings, Jobs, Settings,
Platforms.

- **Rumble**: channel pages are parsed from the embedded `<rum-videos-grid>` JSON (live flag, livestream_status 2 =
  live). New uploads are picked up automatically; a channel with no history takes a baseline instead of back-filling.
- **Editorial intelligence** (`editorial.py`): 14 versioned niche profiles plus creator profiles; each candidate gets a
  0–100 quality score, rejection reasons and review flags the publication gate enforces. Politics is high-risk
  (contextual review, never promotional).
- **Selection**: profile v4 (stable default) and v5 (ending-aware, used by niche presets).
- **Captions**: Anton 73 px (40% larger than before), same vertical centre, auto-shrinks per caption so text never
  runs off either side, no "Excerpt · title · timestamp" label at the top.
- **Platforms** (`platforms.py`): Rumble working; Twitch Helix, Kick and YouTube Data API adapters (discovery only until
  keys are added). Discovery never authorises acquisition.
- **Queue** (`jobs.py`): one heavy job at a time; live captures first; heavy work held while a live recording runs.
- **Storage** (`storage.py`): live preflight (needs ≥0.5 h at 1080p), low-disk safe stop, DB backup before upgrades.
- **Cloud** (`cloud.py`): Google Drive, `drive.file` scope, resumable MD5-verified idempotent uploads of clips
  (video, thumbnail, transcript, metadata — raw recordings are never uploaded).
- **Lifecycle** (`lifecycle.py`): a raw recording is deleted only if it is engine-acquired and all 8 gates pass
  (including every clip verified in Drive); otherwise kept. Clip retention: rejected 24 h, published 7 days.
- **Publishing**: per-platform packages, YouTube private upload, manual handoff for Facebook, X and Rumble.
- **Notifications** (`notify.py`): "recording ready" / failure emails via Microsoft Graph (Outlook) or SMTP, held
  until the owner approves the wording.

## This session (16 November), in order

1. Rumble source fixed end to end (page redesign → grid JSON parsing), disk-aware downloads, setup console crash on
   cp1252 fixed, engine launch made independent of the Claude shell.
2. Dashboard leftovers cleaned (bogus item, failed job); captions resized (+40%), side-fitted, top label removed; all
   12 clips re-rendered.
3. Master Implementation Directive + Addendum 001: multi-niche/creator/platform expansion, research dataset and report.
4. Operational readiness: phone dashboard password over Tailscale; disk raised to ~21 GB free (owner-approved cleanup,
   OneDrive untouched); 1080p minimum enforced for live and downloads; live detection verified on real Rumble data.
5. This evening:
   - **Dashboard password** changed by the owner to a very short one at their request (acceptable because the address is
     tailnet-only). It lives only in `.env` and Windows Credential Manager (`local-clipping-engine` /
     `dashboard_password`) — never print, log or commit it. A password is required for phone access: without one the
     app refuses non-localhost hosts.
   - **Google Drive connected for real.** Owner's Google Cloud project "Clipping Engine", Drive API enabled,
     consent screen External / Testing with the owner as test user, Desktop OAuth client. Keys are
     `CLIPENGINE_GOOGLE_CLIENT_ID/SECRET` in `.env` (pasted by the owner; never shown in chat).
     `scripts/cloud_selftest.py`: **6/6 PASS** (upload + MD5/size, no duplicate on retry, byte-identical retrieval,
     only own test files deleted). All 12 existing clips uploaded and verified (48/48 objects).
   - **Bug fixed (`362bac0`)**: the Google callback `/google/oauth/callback` was not exempt from the login
     middleware; Google's cross-site redirect doesn't carry the SameSite=Strict cookie, so the owner was bounced to
     /login and the code was lost. Now exempt (guarded by one-time state + PKCE), with a regression test.
   - App stays in Google **Testing** mode (publishing needs a public homepage and privacy-policy URL), so Google expires
     the Drive sign-in about **every 7 days**: the owner reconnects on the Storage page. Clips wait locally meanwhile.

## Evidence (be exact)

| Area | Status |
|---|---|
| Rumble listing, download, auto pick-up of a new upload | Verified on the owner's PC |
| Captions 73 px, side fitting, no top label | Verified on the owner's PC (12 real clips) |
| Google Drive upload/verify/retrieve | **Verified for real** (self-test 6/6; 12 clips, 48/48 objects) |
| Phone dashboard over Tailscale | Working (tested via the tailnet address); iPhone sign-in itself to be confirmed by the owner |
| Live detection on Rumble | Verified on real Rumble data (another channel's live stream, same layout); **Fuentes' own broadcast not yet observed** |
| Full unattended lifecycle (live → record → process → upload → verify → raw deletion) | Verified on engine-generated media with a MOCK Drive; real Drive now connected but the full chain has **not yet run on a real broadcast** |
| Disk / quality | ~21.5 GB free; 4.5 h at 1080p needs ~10.5 GB; preflight passes |
| Email notifications | **Not working yet**: enabled with recipient set, but no sending account connected (Outlook needs a free Microsoft app registration) and wording not approved |
| YouTube private upload, Twitch/Kick/YouTube discovery | MOCK-tested only — ready for owner connection |
| Facebook Reels draft, X API | Not implemented (manual handoff; X API is paid) |
| CI | Green (runs #49, #50); local suite passes except 3 known host-only failures |

## The first real livestream (pending)

Nick Fuentes (America First) on Rumble, `https://rumble.com/c/nickjfuentes`, early hours of **Tue 17 Nov UK time**.
At 22:37 on 16 Nov: engine healthy, source enabled/OK (checks every ~2 min), no queued or failed jobs, preflight
"1080p fits the whole expected 4.5 h broadcast". Expected chain: detect → record at 1080p → finalise → transcribe →
select → render → upload to Drive and verify → delete the raw recording → clips wait in Ready to Review.

Afterwards run, in `%LOCALAPPDATA%\ClippingEngine`:

    .venv\Scripts\python.exe scripts\live_report.py

It prints detection/recording times, size, restarts, transcription, clips, Drive ids, deletion gates and the review
queue. **Do not report the test as passed until that report shows a real broadcast.** Known risks: his stream has never
been observed; Wi-Fi only (recorder reconnects, gaps are lost); lid must stay open; engine needs a Windows sign-in after
any reboot; no email alert yet.

## Rules you must keep

- Python 3.10 (no nested same-quote f-strings, no backslashes in f-string expressions); UTF-8 I/O; CRLF in
  `Start-Clipping-Engine.cmd`; FFmpeg with `cwd=` and relative subtitle paths; DB changes only via `MIGRATIONS`.
- Never weaken `classify_url`; never add cookies/logins, disguised user agents, browser automation or rate-limit
  evasion. Research never authorises retrieval; permission for one creator never extends to another.
- Never send email without in-app approval; never publish publicly; YouTube uploads stay private.
- Raw deletion only for engine-owned recordings after all gates; only engine-created Drive objects may be deleted.
- Never expose passwords, client secrets or tokens in chat, logs, Git or handoffs; never commit the owner's email.
- Don't delete anything from OneDrive (not backed up) or other personal files without explicit approval.
- Google keys are `CLIPENGINE_GOOGLE_*` on purpose (the PC has an unrelated machine-wide `GOOGLE_CLIENT_ID`).
- Tests must not depend on the host's disk or environment. Push only to `claude/optimistic-noether-k52qjf`; no pull
  request unless asked; never modify the n8n templates at the repo root.

## Owner actions (prioritised)

1. **Tonight:** keep the laptop plugged in with the lid open; don't restart or sign out.
2. **After the show:** run `live_report.py` (or ask the assistant) and review clips on Ready to Review.
3. **Email (optional, ~10 min):** register a free Microsoft app (Outlook sending), connect it in Settings →
   Notifications, then approve the email wording. Until then no email is sent.
4. **Every ~7 days:** Storage → Connect Google Drive when Google expires the sign-in.
5. Confirm the iPhone sign-in to the dashboard.
6. Later: social accounts mapped to niches, YouTube private uploads, optional Twitch/Kick/YouTube keys, written
   permissions for any new creator, decide whether the Rumble source belongs to the politics niche.

## Next engineering steps

1. Analyse the first real broadcast with `live_report.py`; fix whatever it reveals.
2. Outlook (Graph) email once the owner registers the app; consider an in-app reminder before the Drive sign-in expires.
3. Exercise YouTube private upload against the owner's real account.
4. Twitch "Get Clips Download" acquisition for creator-authorised work.
5. Selection: visual payoff detection for gaming/IRL (audio energy, chat rate, scene change); cross-stream de-duplication.
6. Move the PC to Python 3.12 (yt-dlp has deprecated 3.10).

## Update, session 7 (17 Nov 2026): six-brand portfolio monitor

See `HANDOFF.md` §21. Added schema v6 (brands, monitor_sources, discovered_items, monitor_events, priority_events),
the 6-brand / 18-creator roster with idempotent seeding, `DiscoverySupervisor`, podcast RSS adapter, specific
platform error codes, GTA VI launch-week priority (a table row), a Portfolio dashboard page, the read-only
`python -m clipengine --portfolio`, caption size 95 px (+30%) and an optional top title (96 px = 80% of a 120 px
reference; mock-up asset absent, comparison pending). All platform checks are MOCK-tested only; no identity is
verified until the owner adds YouTube/Twitch/Kick keys and runs `scripts/portfolio_admin.py verify`.
