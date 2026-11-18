# HANDOFF PROMPT FOR GPT — Clipping Engine, end of Session 11 (18 Nov 2026)

You are taking over engineering and owner-guidance for a local, low-cost Python video clipping and editorial review engine.
Owner: James Callaghan (Lenovo LAPTOP-RCINKEK2, Windows 11). Be strict about verified vs unverified. Never ask the owner to paste secrets into chat.

## Project
- Repo `ce-optimisation/n8n-automation-portfolio`, app in `clipping-engine/`. Python 3.10–3.13, SQLite, FastAPI/Jinja2/HTMX, local Whisper, ffmpeg vertical renders, Google Drive masters.
- Installed at `%LOCALAPPDATA%\ClippingEngine`; dashboard `http://127.0.0.1:8765`.
- Six brands (America First Monitor, Afterlive, Operator Notes, The Spec Edit, Dial Study, Open Floor); 18 creators; 27 monitoring sources; 24 planned publishing accounts (6 × YouTube/TikTok/Instagram/Facebook).
- Read first: `HANDOFF.md` (sections 0, 1B, 1C, 17–19, 21–25), `OWNER_SETUP_SESSION10.md`, `STORAGE_ARCHITECTURE_SESSION11.md`, `CHATGPT_HANDOFF_SESSION10.md`.

## Branch state
- `claude/optimistic-noether-k52qjf` fast-forwarded (no force) to 2259593 (Session 10 head). It does NOT yet contain Session 11.
- Session 11 work: `claude/sharp-bell-utpc9o`, commit **b289beb** (pushed). No PR opened; none authorised. Ask the owner before fast-forwarding the established branch to b289beb (history is linear).
- Tests (Linux, with yt-dlp and faster-whisper installed): **372 passed, 4 skipped, 0 failed** (full suite once, ~8 min — do not rerun needlessly). The 4 Session 10 failures were missing packages, not defects. GitHub CI for b289beb NOT checked. Everything is mocked/synthetic or real-ffmpeg on synthetic media; nothing has run on Windows or against a real stream/API.

## Background: the 17 Nov storage failure
Rumble "America First Ep. 1780" (source item 20, state `failed`, recording_id None): one recorder wrote an unbounded 11.37 GB `seg_001.ts`; the only disk guard stopped at ~0.5 GB free; finalising then tried a second full MP4 remux (0.44 GB written) and ffmpeg failed "No space left on device". Files remain in `%LOCALAPPDATA%\ClippingEngine\data\work\live\src001_v7iial2\` (`seg_001.ts` 11.37 GB, `recording.mp4` 0.44 GB). **The owner explicitly authorised permanently deleting exactly these two files — but that has NOT been done and no space has been recovered.** Rumble recording itself needs platform + rights-holder authorisation (do not enable without it).

## What Session 11 built (b289beb)
1. `clipengine/livestore.py` + `monitor.py`: capture admission (`LIVE_BUFFER_GB` 20, every active capture needs next segment + 0.5 GB headroom), recorder rotation at `LIVE_SEGMENT_MAX_GB` 1.0 / `LIVE_SEGMENT_MAX_MINUTES` 30, low-disk stop while a join still fits, **join without a second full copy** (append each overlap-trimmed, timestamp-continuous segment to one `recording.ts`, delete the segment only after the append is recorded; manifest resume; out-of-space rolls back → state `awaiting_space`, retried each tick). Recordings are now `.ts` (`ingest.VIDEO_EXTS` includes `.ts`; browser can't preview `.ts`). Real-ffmpeg restart-overlap test passes; a bug (absolute overlap cut vs `-t` duration) was found and fixed.
2. Safe discard of failed captures: `livestore.discard_check/discard` (state must be `failed`; no recording registered; no active writer incl. exclusive-open probe + 10-min age; only `seg_*/recording.*/segments.*/join.*` files; inside `work/live`; exact `DISCARD <id>`; measures recovered space), `python -m clipengine --discard-live ID --names a,b`, **`Recover-FailedRecording.cmd`** (defaults to item 20, `seg_001.ts,recording.mp4`), and a Storage-page "Live capture folders" panel.
3. `clipengine/framelines.py`: owner-approved blue `#2856D8`, 2 px at 1080 wide, one line just outside the top and one just outside the bottom edge of the central picture, full picture width, no side/canvas borders, snapped to even rows (4:2:0 exact); per brand (`set_brand_enabled`, `set_brand_colour` — other colours only with owner approval); default ON for America First Monitor only; skipped for full-frame (9:16) pictures; wired through `render.render_clip(line_style=)` and `pipeline.render_clip`. Already-approved clips are not re-rendered. `scripts/preview_frames.py` makes previews. Real pixel tests on 16:9, 4:3, 1:1.
4. Earlier (Session 10, still the baseline): hook titles (`hooks.py`, schema v8), `diskaudit.py`/`Storage-Audit.cmd`, Drive-only archive (off by default), `Set-Credentials.cmd`, `storage.require_space` heavy-job guard.

## NOT done / NOT verified (do not claim otherwise)
- **No file deleted, no disk space recovered.** The owner's laptop could not be attached to the cloud session (`list_computer_folders` shows `ClippingEngine`, `Brain`, `WhoFundsWestminster` under `cannot_be_used: not_available`; likely outdated Claude desktop app, hidden `AppData` path, or account not enabled). The owner was told to update the Claude desktop app and re-check Settings → Claude Code → Remote Control.
- Windows install/update, schema v7→v8 migration, `SELECTION_PROFILE=v6`, worker health, Drive connection, hook titles and storage audit in the installed app: not verified.
- Existing clips: not inspected (no access). Upload-today workflow (review queue → approve → MP4 + platform text → manual handoff → record confirmations → safe cleanup) is existing functionality, not exercised.
- `scripts/compare_selectors.py` on `Alex_Jones_Interviews_Nick_Fuentes_at_NX_clip6_1377s.mp4` and `ASSEMBLE_THE_ARMY_clip12_3651s(1).mp4`; Drive restore test on a disposable clip; real YouTube/Twitch/Kick authentication; real-clip hook-title review: not run.
- **Cloud raw buffer not implemented.** MEGA free (20 GB) was not integrated: its limits and scripted-use terms could not be verified (third parties report an IP-based rolling transfer quota; official terms/MEGAcmd guide not read; docs.kick.com also unreachable earlier). Google Drive is reserved for masters. Not implemented: temporary-cloud upload of raw segments, fetching only needed footage back, a temporary-cloud dashboard figure, "transcribe while recording", eight-gate extension for cloud-backed segments (none exist).
- Python 3.10 deprecation warning on the laptop: message never seen; not diagnosed.
- Guided browser setup (Google Cloud, Twitch, Kick, 24 accounts): not started; no account confirmed.
- The "Codex ENOSPC workspace error" the owner mentioned was not investigated (separate from the Windows disk problem).

## Next steps (in order)
1. Get the laptop attached, or have the owner run on Windows: `Recover-FailedRecording.cmd` (type `DISCARD 20`) and report the printed before/after free space; then `Storage-Audit.cmd` and share the summary.
2. Check CI on b289beb; ask the owner to approve fast-forwarding `claude/optimistic-noether-k52qjf` to it (no force, no PR).
3. Guide `Install-Clipping-Engine.cmd` → start → confirm schema v8, `SELECTION_PROFILE=v6`, worker running, Drive connected; run Storage-Audit and `Check-Portfolio.cmd`.
4. Review the existing clips with the owner: separate usable / rejected / incomplete / failed; keep re-edit candidates; review hook titles; approve only rights-cleared clips; manual handoff; record real upload confirmations only. Do not publish automatically.
5. Real-world validation: compare_selectors on the two recordings (report honestly if the originals are gone), Drive restore on a disposable engine clip (verify master → cleanup → restore → checksum → playable), hook titles on real clips.
6. Credentials via `Set-Credentials.cmd` (YouTube key, Twitch app, Kick app), then `Check-Portfolio.cmd`; label only successful real requests as verified.
7. Before any new long recording: confirm Rumble authorisation/rights; run one short real capture to validate rotation/join on Windows (the join and rotation are untested against a live stream); consider MEGA only after reading its official terms.
8. Ask the owner whether to enable frame lines or titles for other brands; do not enable without approval.
9. Last: 24 publishing accounts (matrix in `OWNER_SETUP_SESSION10.md`), pausing for sign-in/verification; record only platform-confirmed accounts.

## Non-negotiable rules
Discovery ≠ permission to record/download; no DRM/paywall/auth bypass; no unpermitted recording; no automatic public publishing (YouTube private until separately authorised); human editorial review mandatory; America First Monitor needs accurate contextual handling (no inflammatory or promotional framing); never invent or mislead in hook titles; preserve brand/account isolation, rights records, database history and Drive masters; never delete personal/OneDrive files, unverified cloud masters or unrelated clips/recordings/databases/credentials; never turn on a new destructive retention policy without explicit owner approval; never commit or print secrets; no force-push; no PR without authorisation; no paid APIs/services; avoid repeated full-suite runs.

Owner's priority: a reliable, mostly autonomous system producing fewer, better clips, minimal laptop storage use, and getting existing usable clips uploaded today.
