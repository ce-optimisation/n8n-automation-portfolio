# Handoff prompt for ChatGPT - Local AI Video Clipping & Editorial Review Engine (end of session 9, 17 Nov 2026)

> Paste everything below the line into ChatGPT, ideally with the repository attached.
> Read `clipping-engine/HANDOFF.md` §0, §1B, §1C, §17-19 and **§21-23** first. **The code and the tests are the source of truth** where documents disagree.
> This supersedes `CHATGPT_HANDOFF_SESSION8.md` (still accurate history for sessions 7-8).

---

## Your role

You are lead engineer of a working, locally hosted Python application on the owner's (James Callaghan's) Windows 11 Lenovo laptop. It monitors sources, records/downloads authorised video, transcribes locally (faster-whisper), selects self-contained excerpts using niche- and creator-specific editorial profiles, renders vertical 1080x1920 clips (Anton captions, optional top title), backs them up to Google Drive, and prepares them for **human review**. Nothing is ever published publicly by the engine.

Owner preferences: execute rather than propose; don't rebuild what works; Python-first, minimal model usage (scripts for deterministic work); report honestly (never present mocked behaviour as real); no paid APIs/subscriptions/cloud compute; Python 3.10-compatible; Windows-first; plain English; one consolidated owner checklist at the end; no repeated interruptions. The owner is often on an iPhone without access to the laptop and often has very little free disk.

## Where things are

- **Repo:** `ce-optimisation/n8n-automation-portfolio`, folder `clipping-engine/`. Never touch the n8n templates at the repo root.
- **Branches:** the established source branch is `claude/optimistic-noether-k52qjf` (still at session-8 commit `35e0040`). **Session 9 work is on `claude/wizardly-mccarthy-i5kf4r`, commit `d8468de`** (plus any later handoff-only commit). It was NOT fast-forwarded into optimistic-noether in session 9 (that was only authorised in session 8). Fast-forward it (no force push) when the owner agrees, then use optimistic-noether as the single source branch. No pull request exists; don't create one unless asked.
- **CI for `d8468de`: green** (run #63: Python 3.10 compile+tests+real self-test, Windows one-click installer, ubuntu and windows full tests incl. the real-Whisper genuine-recording step).
- **Install on the PC** (replaced on each update; never the source of truth): `%LOCALAPPDATA%\ClippingEngine` (`.venv\Scripts\python.exe`, data in `data\`, settings in `.env`). Update by re-running the installer, then restart fully. Dashboard `http://127.0.0.1:8765`; phone via Tailscale `https://laptop-rcinkek2.taildd0ae9.ts.net`. The installed copy has no git metadata.
- **Stack:** FastAPI + Jinja2 + HTMX, SQLite (**schema v7**, changes only via `SCHEMA`/`MIGRATIONS` in `db.py`), FFmpeg/libass, faster-whisper, yt-dlp, httpx. Tests: `python scripts/test_report.py [pytest args]` prints one concise summary (full suite about 9 minutes: **320 passed, 3 skipped, 0 failed** locally; the skips need the real Whisper model / live internet).

## Earlier sessions (preserve; details in HANDOFF.md)

Sessions 1-6: one engine for many niches/creators; versioned niche + creator editorial profiles; selection profiles v1-v5; persistent job queue (one heavy job at a time, live captures first); disk preflight and 1080p-minimum live planning; Google Drive backup (MD5-verified, idempotent; sign-in expires about every 7 days in Testing mode); engine-owned raw-recording deletion only after **eight gates** (`lifecycle.py`); per-platform publication packages (`publishing/packages.py`), YouTube *private* upload, manual handoff for others; Rumble channel monitor; network-drop survival.
Session 7: six brands / 18 creators / 27 monitor sources (`portfolio.py`), `DiscoverySupervisor`, launch-week priority, `/portfolio` page. Session 8: roster import, creator-level acquisition permission, calibrated top title (enabled for America First Monitor), 24 brand account placeholders, Afterlive spoiler/sensitive-scene review flags, `Check-Portfolio.cmd`. **Nothing from sessions 7-8 was changed in a breaking way in session 9.**

## Session 9 - what was built (commit d8468de)

### A. Context-complete selection, profile v6 (`complete.py`, `segment.PROFILES["v6"]`)

Principle: **completeness of meaning -> editorial value -> duration suitability.** Duration is a constraint, not an objective. Legacy v1-v5 behaviour is unchanged and still selectable.

- **Method (bounded, no all-pairs window scan):** split the transcript into coherent regions (hard breaks, strong topic breaks); pick a limited set of seeds (<=120) = the most substantive sentences; **expand** each seed: step back to the earliest necessary opening (a non-dependent sentence, or an announced topic start such as "Let me explain..."), carry a question through its answer, include a meaning-changing qualification ("But to be fair..."), include sentences that lean on the point, continue the same point, stop at a topic change or after 6 consecutive sentences adding nothing new; then **trim** trailing repetition/filler and leading asides.
- **Assessment** on opening / conclusion / meaning / central idea / cohesion / economy -> **PASS / REVIEW / REJECT** with reasons. Hard rejects: starts on a dependent line, ends on a question or before a qualification, hand-over/fragment ending, runs across a speaker change or topic change, too little substance (non-short-form), too short to give context (<60% of the preferred minimum, non-short-form). Topic change = strong boundary AND vocabulary stops flowing (boundary strengths are percentile ranks, so "strong" alone is meaningless) or a >=2.5 s silence.
- **Duration policy** (`complete.PREFERRED`, preferences not limits): political monologue 40-180 s, interview 35-180, business 30-120, podcast 35-180, gaming 12-60, comedy/reaction 10-45; niche/creator content type picks the range (`complete.preferred_range`). **No editorial ceiling:** a window beyond its range is "exceptional" only if it stays substantive across its thirds, cohesive, concludes, and does not repeat; the justification text is stored (`clips.completeness_json.exceptional`) and shown. Otherwise shorter complete prefixes ending at natural boundaries are generated and preferred, and the long window is demoted to REVIEW. Length earns nothing in ranking; ties go to the shorter. **Removed the old hard caps**: owner adjust (3-180 s) in `pipeline.adjust_and_regenerate` and the niche editor's <=180 s (now <=3600, described as preferred). Adjusting a clip whose raw recording was removed now fails with a clear explanation.
- **Zero clips is valid:** `process_recording` marks the recording `ready`, stage "no complete clips selected", note in `selection_note` (no failed job); the lifecycle holds (never deletes) such a recording. The legacy selector still raises as before.
- **REVIEW** candidates get a `context_review` safety flag (the editorial gate then needs an explicit decision) and are offered only when no PASS exists or composite >= 0.8 (no clutter next to good clips).
- **Budget** (`complete.Budget`: seeds, windows scored, expansion sentences) is computational only; a 3-hour transcript runs in seconds. Restart-safe (selection is a pure function of the stored transcript).
- **Rollout:** default selector is now v6 (`SELECTION_PROFILE`, niche presets); existing niches on v4/v5 move to v6 **once** (`editorial._migrate_to_context_complete`, recorded in `niche_history`; a later owner choice of v4/v5 sticks). Saved `.env` with `SELECTION_PROFILE=v4` overrides the default for recordings without a niche policy: tell the owner to set v6. Schema: `clips.completeness_json`, `clips.duration_class`.
- **Evidence:** `scripts/compare_selectors.py` (read-only) on fixtures: monologue with setup + qualification: v4/v5 choose a 22 s fragment ending before the argument; v6 chooses the full 68 s passage (PASS). 4-minute monologue: v4 104 s, v5 60 s tail, v6 the whole 360 s as an exceptional PASS. Short gaming moment: all pick the same 18 s. No-substance chat: all return nothing. **These are my own deterministic fixtures: they prove the rules, not that clips are better for the owner.** The owner's two example recordings (Alex Jones interviewing Nick Fuentes at NX, "ASSEMBLE THE ARMY") were not available: `python scripts/compare_selectors.py --recording N [--kind political_monologue]` is the prepared way to test them.

### B. Verified-upload local cleanup of FINISHED clips (`cleanup.py`, schema v7)

Separate from the raw-recording lifecycle. **OFF until the owner enables it** (Storage page, or per clip).

- **Pipeline:** render -> review -> verified Drive master -> required destinations complete -> recovery window (default 24 h, from the LAST required confirmation) -> delete the local MP4 (never the Drive copy) -> restorable.
- **Gates** (all re-checked after an atomic claim and just before unlinking, with a fresh MD5 and a live Drive re-read; the decision + evidence is written first to `cleanup_events`): `managed` (exact path `clips/recNNNNN/clipNNNNN_rN/clip.mp4`, not a symlink; owner-supplied/foreign files never qualify), `not_kept` (keep-locally, hold/cancel, per-clip off), `editorial` (`editorial_gate`: approved, rights, flags decided), `no_jobs`, `final` (not rendering; unchanged >60 s), `drive_master` (video object verified, local file size+MD5 equal the verified version, Drive re-read matches; metadata/transcript objects verified), `consistent` (recording exists; destination accounts brand-isolated), `destinations` (>=1 explicit required destination, all complete), `grace`. Manual "delete now" waives only the window and the global switch.
- **Destination states** (`HANDOFF_STATES`; manual is always labelled): YouTube = private upload whose API response has the video id AND `privacyStatus=private` (`handoff_state=private_verified`, written by `run_youtube`); TikTok = only a confirmed publish counts (**inbox delivery is not a draft** -> owner confirms); Instagram = only a confirmed publish with media id (**a container is not a draft** -> owner confirms); Facebook/X/Rumble = manual (owner confirms the draft/post exists, or records it posted). Missing/unassigned accounts and failed uploads never count. **Facebook draft upload is NOT implemented.** YouTube "processing complete" is not polled.
- **Manifest:** `publication_packages` rows are the destination manifest; new columns `required`, `excluded_at`, `exclude_reason`, `handoff_state/method/at/evidence`. Exclusion requires a recorded reason; a destination added during the window pauses cleanup and restarts the window.
- **Drive master protection:** `CloudService.delete_clip_objects(clip_id, force=False)` now refuses once a clip is locally removed and (with `KEEP_DRIVE_MASTER=1`, default) for any non-rejected clip. `Lifecycle._delete_clip_files` (published-clip retention) deletes **local files only** and marks the clip `LOCAL_DELETED` (restorable). The raw-recording `integrity` gate accepts a cleaned clip whose Drive copy is verified.
- **States** (`cleanup.STATES`): LOCAL_PRESENT, CLOUD_VERIFIED, DESTINATIONS_PENDING, GRACE_PERIOD, READY_FOR_CLEANUP, DELETING, LOCAL_DELETED, RESTORE_PENDING, RESTORED, CLEANUP_HELD, CLEANUP_ERROR. Crash recovery: `recover()` rolls back or completes a clip left `DELETING`.
- **Restore:** `request_restore` queues `cleanup_restore`; `restore()` downloads from Drive (`GoogleDriveProvider.download`), validates size+MD5, atomically replaces, never overwrites a different local file, protects the copy for >=24 h; interrupted downloads leave no partial file; a missing/changed Drive copy is reported, never faked as restored.
- **Dashboard:** clip workspace cards (Completeness; Storage on this computer with required destinations, "I confirm it is done" / "Not needed" with reason, keep locally, allow auto, cancel/release hold, delete now, restore, why held); Storage page card (switch + recovery window, recoverable now / in window / waiting on uploads / held, largest clips, run a pass now); review-card badges. Routes in `web/workspace.py` (`/clips/{id}/cleanup/{action}`, `/packages/{id}/confirm|exclude|include`, `/storage/cleanup[/run]`).
- **Disk pressure:** `run()` removes eligible clips largest-first and never deletes unverified/held content; `overview()` reports bytes blocked by pending destinations.
- **Correction:** session 8's handoff said `/clips/{id}/title` was missing; it already existed in `web/workspace.py`. A duplicate added in session 8 was removed. The route now formats the title and re-renders only unapproved clips.

## Evidence (be exact)

| Area | Status |
|---|---|
| Local suite | 320 passed, 3 skipped, 0 failed (`scripts/test_report.py`) |
| CI at d8468de | **Green**, all jobs |
| New tests | `tests/test_complete_selection.py` (15), `tests/test_cleanup.py` (34), `tests/test_session9.py` (13) |
| Cleanup/restore | **Fake in-memory Drive + temp dirs only. No real Drive, YouTube, TikTok or Instagram call has been made.** Blocked cases covered: Drive master missing/changed/removed/not connected, checksum differs, upload job running, TikTok inbox-only, Instagram container-only, Facebook handoff incomplete, failed destination, destination added during the window, window not expired, keep-locally, job using the file, owner-supplied/outside/foreign path, symlink, inconsistent records, race/concurrency, crash recovery; success keeps the Drive master and DB record, restore checksum matches, idempotent |
| Selection | 12 required scenarios covered on deterministic fixtures + timestamps/words fidelity + 3-hour budget test. **Real-media evaluation on the owner's two example clips is pending** |
| Migration | A DB created by the real session-8 code (`35e0040`) opened with the new code: schema v7, 18 creators / 27 sources / 24 accounts kept, backup written. Also a repeatable DROP COLUMN test (skipped if SQLite < 3.35) |
| Fuentes live test (armed 16-17 Nov) | **Result still unknown**; do not report it passed until `scripts\live_report.py` shows a real broadcast |
| Platform APIs (YouTube/Twitch/Kick/podcast) | Never exercised with real credentials (owner has not added keys) |

## Rules you must keep

- Python 3.10 (no nested same-quote f-strings, no backslashes in f-string expressions); UTF-8 I/O (Windows cp1252-safe console output); CRLF in `.cmd` files; FFmpeg with `cwd=` and relative subtitle paths; DB changes only via `SCHEMA`/`MIGRATIONS`.
- **Discovery is not acquisition.** Never weaken `ingest.classify_url`; no cookies/logins, disguised agents, proxies, browser automation or rate-limit evasion; no protected/DRM/login-only media. Acquisition needs platform AND creator permission.
- Never publish publicly; never send email without in-app approval; YouTube uploads stay private; the editorial review gate stays mandatory (extra care for America First Monitor: contextual review, never promotional handling of extremist material, no outrage optimisation, preserve attribution, qualifications and rebuttals).
- One heavy job at a time; live captures first; 1080p minimum for live; raw deletion only for engine-owned recordings after all eight gates; only engine-created Drive objects may be deleted; **never delete the Drive master via local cleanup**; never delete OneDrive/personal files.
- Never print or commit secrets or the owner's email; never ask the owner to paste secrets into chat; never invent channel URLs, handles, ids, remote video ids or upload confirmations; never present a mock as a live verification.
- Tests must not depend on the host's disk, environment or network, and must never delete real user files. Push only to the assigned development branch; no PR unless asked.

## Outstanding - owner actions (present once, prioritised)

1. **Disk is very low** (owner just reported it). Open Storage and read each recording's gate list. Most likely blocker: **Google Drive sign-in expired** (about every 7 days): press Connect Google Drive so clips verify and the engine deletes raw recordings itself (needs `Delete engine-acquired raw recordings...` ticked). Do not delete raw recordings by hand unless their clips show verified in Drive; deleting means clips can no longer be re-cut (transcript, timecodes and finished clips are kept).
2. Update the PC install, restart; set `SELECTION_PROFILE=v6` in `.env` if it says v4.
3. Run `python scripts/compare_selectors.py --recording N` on the two example recordings and share the output.
4. Switch on finished-clip cleanup (Storage page) once comfortable; prepare a version per platform for each clip and press "I confirm it is done" for TikTok/Instagram/Facebook/X/Rumble after saving or posting.
5. Earlier: add YouTube/Twitch/Kick keys to `.env` and run `Check-Portfolio.cmd`; record platform + creator permissions before any acquisition; run `scripts\live_report.py` for the Fuentes test; record real publishing accounts (6 brands x 4 platforms) on the Accounts page.

## Outstanding - engineering (suggested order)

1. Fast-forward `claude/wizardly-mccarthy-i5kf4r` into `claude/optimistic-noether-k52qjf` (no force) when the owner agrees; check CI.
2. **Real validation:** run cleanup + restore against the owner's real Drive on a throwaway clip (watch that the Drive re-read and restore work end to end); try a real YouTube private upload and check `handoff_state=private_verified`.
3. Evaluate v6 on the owner's real recordings; tune `complete.py` thresholds only with real evidence (opening/closure/meaning thresholds, the 0.04 topic-shift cosine, stale-sentence limit 6, REVIEW composite cut 0.8). Known limit: opening/topic detection is lexical and rule-based, so anaphora ("a bill like this") can still slip through; the REVIEW flag and human approval remain the safety net.
4. Optional: poll YouTube `videos.list` (processingDetails) to record "processing complete"; implement Facebook Reels draft upload only if an official, testable path exists (until then it stays manual).
5. UI polish: a per-recording "no clips selected, here is why" view (the note is stored in `recordings.selection_note`); show recoverable disk space on the main dashboard.
6. Older backlog: Outlook email, Twitch "Get Clips Download" for creator-authorised work, visual payoff detection for gaming/IRL, move the PC to Python 3.12.

## First steps for you

1. Read `HANDOFF.md` §21-23, `clipengine/complete.py`, `clipengine/cleanup.py`, `tests/test_complete_selection.py`, `tests/test_cleanup.py`, `tests/test_session9.py`.
2. Run `python scripts/test_report.py` (about 9 min) and `python scripts/compare_selectors.py` to establish the baseline.
3. Work the engineering list; keep to the rules; report with exact evidence and one consolidated owner checklist.
