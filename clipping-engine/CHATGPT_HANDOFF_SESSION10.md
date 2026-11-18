# HANDOFF PROMPT FOR GPT — Clipping Engine, end of Session 10 (17–18 Nov 2026)

You are taking over engineering and owner-guidance for a local, low-cost Python video clipping and editorial review engine.
Owner: James Callaghan (Lenovo, Windows 11). Be honest about what is verified versus unverified. Never ask the owner to paste secrets into chat.

## Project
- Repo: `ce-optimisation/n8n-automation-portfolio`, app in `clipping-engine/`. Python 3.10–3.13, SQLite, FastAPI/Jinja2/HTMX, local Whisper transcription, ffmpeg vertical renders, verified backups in Google Drive.
- Installed at `%LOCALAPPDATA%\ClippingEngine`; dashboard `http://127.0.0.1:8765`.
- Six brands (America First Monitor, Afterlive, Operator Notes, The Spec Edit, Dial Study, Open Floor), 18 creators, 27 monitoring sources, 24 planned publishing accounts (6 brands × YouTube/TikTok/Instagram/Facebook).
- Read first: `HANDOFF.md` (sections 0, 1B, 1C, 17–19, 21–24) and `OWNER_SETUP_SESSION10.md`.

## Branches and state
- `claude/optimistic-noether-k52qjf` was fast-forwarded (no force) 35e0040 → bfb0f8d (Session 9, includes d8468de, selection profile v6).
- Session 10 work: branch `claude/sharp-bell-utpc9o`, commit b3b527a (pushed). **No PR opened; owner has not authorised one.** This branch is not yet merged into the established branch.
- Tests (Linux container): 350 passed, 4 failed, 4 skipped. The 4 failures were missing packages in the container (3 need yt-dlp — they pass with it installed; `test_doctor_reports_real_state` needs faster-whisper and yt-dlp, not re-run with both). GitHub CI status for b3b527a was NOT checked. All new tests are mocked/synthetic except the hook-title ffmpeg renders.

## What Session 10 built (all in b3b527a)
1. **Hook titles** — `clipengine/hooks.py`. Extractive candidates (claim / question / topic / attributed topic) from the clip's own transcript; verification rejects unsupported words/numbers, non-verbatim quotes, dropped negation/hedge, pronoun openings, sensational wording, brand-policy violations. America First Monitor: attributed/topic wording only and inflammatory terms rejected. Optional local Ollama only proposes extra wordings (same verification). If nothing passes → `manual_needed`. Stored in new clips columns (schema v8): `title_source/title_status/title_locked/title_rev/title_json`. Owner-written/approved/locked titles are never regenerated. Routes: `/clips/{id}/title`, `/title/choose|approve|unlock|remove|regenerate`; UI card "Hook title" in `_clip_workspace.html`. Renderer (`titles.py`) untouched. Titles draw only for brands whose title style is on (only America First Monitor by default).
2. **Storage** — `clipengine/diskaudit.py` + `Storage-Audit.cmd` (`--storage-audit`, read-only; classes A safe scratch / B raw recordings with first blocking deletion gate / C finished clips / D protected). `storage.require_space()` defers audio extraction and rendering with `TransientError` (retry 1800 s) on low disk. Maintenance also removes partial downloads (`*.part/.ytdl/.tmp/.tmp.wav`) older than 3 days. Eight-gate raw deletion policy and one-heavy-job rule unchanged.
3. **Drive-only archive** — in `cleanup.py`, OFF by default, Storage-page toggle + "Run restore test". Applies only to engine-rendered, owner-reviewed/approved clips with a verified Drive master and a passing restore test (≤30 days); deep Drive re-check before deletion; Restore from Drive reverses; logged `mode=drive_only_archive`. Drive masters never deleted.
4. **Credentials** — `clipengine/credentials_cli.py` + `Set-Credentials.cmd` (`--set-credentials`): hidden prompts, writes only named keys to `.env`, one real auth request per platform, HTTP status only, no secrets in output/exceptions. `Check-Portfolio.cmd` runs the same authentication test.

## NOT done / NOT verified (do not claim otherwise)
- The work ran in a Linux cloud container with no access to the Lenovo. **The actual disk consumption is unknown** — owner must run `Storage-Audit.cmd`. No cleanup was performed.
- Windows installer update, schema v7→v8 migration, restart and `SELECTION_PROFILE=v6` confirmation: not done.
- `scripts/compare_selectors.py` on the two real recordings (`Alex_Jones_Interviews_Nick_Fuentes_at_NX_clip6_1377s.mp4`, `ASSEMBLE_THE_ARMY_clip12_3651s(1).mp4`): not run (files are on the laptop).
- Real Drive restore on a disposable clip: not run. No real platform identity checks: no credentials exist yet.
- Kick: official docs (`docs.kick.com`) were unreachable; token URL `id.kick.com/oauth/token` and base `api.kick.com/public/v1` match third-party clients only. Treat as unverified until a real request succeeds. Kick's API is discovery only, not footage.
- Twitch/YouTube setup steps in the owner guide are from knowledge (e.g. Twitch needs 2FA to register an app; `http://localhost` redirect for client-credentials) — verify against the live consoles; do not invent URLs/scopes.
- Browser-guided owner setup (Google Cloud, Twitch, Kick, 24 accounts): not started. No account is confirmed to exist.
- Hook-title quality has been checked only on synthetic transcripts; assess on real clips and tune.

## Next steps (in order)
1. Ask the owner to run `Storage-Audit.cmd` and share the summary (no secrets). Report what occupies space and which gates block raw deletion; propose cleanup; get explicit approval for anything outside existing authorised retention settings.
2. Check CI for b3b527a; ask the owner whether to fast-forward `claude/optimistic-noether-k52qjf` to it (history is linear from bfb0f8d). Do not force-push; do not open a PR unless asked.
3. Guide `Install-Clipping-Engine.cmd` update → engine start → schema v8 → confirm v6 profile.
4. Guide credentials: YouTube Data API v3 key (restrict to that API), Twitch app (Client ID/Secret), Kick app → `Set-Credentials.cmd` → `Check-Portfolio.cmd`. Fix any confirmed adapter defect; label only real-request successes as live-verified.
5. Run `scripts/compare_selectors.py` (v4/v5/v6) on the two real recordings; judge context completeness, start/end, qualifications, duration, PASS/REVIEW/REJECT.
6. Exercise Drive restore on a disposable engine-generated clip (verify master → cleanup → restore → checksum → playable). Never test destructively on unique originals.
7. Review generated hook titles on real clips with the owner; refine; have them enable title style for other brands if wanted.
8. Last: 24 publishing accounts (matrix in `OWNER_SETUP_SESSION10.md`), pausing for sign-in/verification; confirm each profile URL before recording it; connect only approved accounts; never publish.

## Non-negotiable rules
Discovery ≠ permission to acquire footage; no bypassing DRM/paywalls/auth; no unpermitted recording/downloading; no automatic public publishing (YouTube stays private until separately authorised); human editorial review mandatory; America First Monitor needs accurate contextual handling and never promotional framing of extremist statements; preserve brand isolation (brands never share accounts), rights/permission records, database history and Drive backups; never delete personal/OneDrive files; never commit or print secrets; never force-push; no PR without authorisation; no paid APIs, hosted transcription or large mandatory model downloads; avoid repeated full-suite runs (it takes ~8 minutes).

Owner's priority: a reliable, largely autonomous system producing fewer, better clips, minimal laptop storage use and minimal administration.
