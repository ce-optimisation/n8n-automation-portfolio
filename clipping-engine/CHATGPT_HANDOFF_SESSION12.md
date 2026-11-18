# HANDOFF PROMPT FOR GPT: Clipping Engine, mid-Session 12 (18 Nov 2026)

You are taking over engineering and owner guidance for a local, low-cost Python video clipping and editorial review engine.
The owner works on a Lenovo LAPTOP-RCINKEK2 running Windows 11. Be strict about what is verified and what is not. Never ask the owner to paste secrets into chat.
Label every claim as one of **completed**, **tested**, **unverified** or **blocked**. Never report simulated or CI success as real-world completion.

## Project
- Repo `ce-optimisation/n8n-automation-portfolio`; the app is in `clipping-engine/`. Stack: Python 3.10–3.13, SQLite, FastAPI/Jinja2/HTMX, local Whisper, ffmpeg vertical renders, Google Drive masters.
- Installed at `%LOCALAPPDATA%\ClippingEngine`. Dashboard: `http://127.0.0.1:8765`. The installer (`Install-Clipping-Engine.cmd` → `scripts/install.ps1`) downloads the
  **established branch** ZIP by default. It preserves `.env`, `data\`, `.venv\` and `tools\`. You can also run it with `-Source <local checkout>` or `-Branch <name>`.
- Six brands; 18 creators; 27 planned monitoring sources (only 1 is configured in the installed DB, see below); 24 planned publishing accounts.
- Read first: `CHATGPT_HANDOFF_SESSION11.md`, `HANDOFF.md` (sections 0, 1B, 1C, 17–19, 21–25), `STORAGE_ARCHITECTURE_SESSION11.md`, `OWNER_SETUP_SESSION10.md`.

## Branch / commit state (checked 18 Nov, afternoon)
- Established branch `claude/optimistic-noether-k52qjf` = **2259593** (Session 10). **Not yet updated.**
- Development branch `claude/sharp-bell-utpc9o` = **2a0434a** (Session 12 Windows fix) → 8423070 (S11 handoff) → b289beb (S11 code). Linear history; no PR.
- **Owner approval given (in chat, 18 Nov):** once the Windows CI job is green on 2a0434a, fast-forward the established branch to 2a0434a.
  Then stop the running app, back up `data\clipengine.sqlite3`, and update the install. No force-push. No PR.
- CI run **44295021720** on 2a0434a, last check:
  - Python 3.10 job: success.
  - ubuntu tests + end-to-end: success.
  - Windows clean installer: success.
  - **windows-latest tests + end-to-end: still in progress.** Check it before doing anything else. Job logs need GitHub auth; annotations and job status are public via the API.
- The earlier failing run was 44283099213 on b289beb. A run on 8423070 (docs-only commit) **passed** the same Windows job, which shows the failure was a timing race.
  One green run is therefore supporting evidence only; the deterministic regression tests below are the main proof.

## Session 12 work completed

### 1. Windows CI defect fixed (commit 2a0434a): tested on Windows locally and pushed; CI pending
**Root cause:** stopping a live recorder only stopped the top process. That process is often a wrapper:
- on the CI runner, the Chocolatey `ffmpeg.exe` shim;
- on the laptop, `python -m yt_dlp`, which runs its own ffmpeg for live HLS.

The real ffmpeg kept appending to `seg_001.ts` and held it open, so the join's delete failed with `WinError 32`. It also wrote duplicate footage.

**Reproduced on the laptop:** after killing a `.cmd` wrapper around ffmpeg, the file grew from 0 to 262,144 bytes and unlink raised WinError 32.

**Fix:**
- New `clipengine/proctree.py` with `RecorderProcess`. It is Popen-compatible: `poll/wait/terminate/kill/pid/returncode`.
  - Windows: the process is created suspended, put in a Job Object with KILL_ON_JOB_CLOSE, then resumed. Stopping it terminates the whole job.
  - When the top process exits by itself, leftover children get a 3 s grace and are then stopped, with a bounded wait.
  - If the engine dies, its recorders die too.
  - POSIX: the recorder runs in its own process group and signals go to the group.
- `monitor._start_segment` now uses `RecorderProcess`. The rotation kill path now waits, which reaps the tree, before starting the next segment.
- New `livestore.unlink_when_released`: bounded retry (20 s, backoff) on PermissionError.
  - A lock that persists raises `FileStillInUse`. The file is kept and nothing is lost.
  - `_finalize` then leaves the item in `finalizing` with an explanatory error, so a restart resumes the join from its manifest.

**Tests:**
- New `tests/test_proctree.py`: 7 tests, all passing on Windows 11. They use a wrapper plus a grandchild writer that never stops on its own, and cover kill, terminate, self-exit, a monitor-level crash, retry success, a persistent lock being reported, and a real Windows handle release.
- The monitor-level test **fails on the old code** ("a descendant process is still writing the segment") and passes with the fix.
- Targeted run of the modules touching monitor/livestore, with a wrapper ffmpeg on PATH: **212 passed, 17 skipped, 1 failed**.
  The failure is `test_deploy_setup.py::test_doctor_reports_real_state`. It fails identically on unchanged code: the `Anton-Regular` font is not found from the repo checkout on this laptop.
  The installed app's own font check passes (`caption_font_check ok`). Not caused by this fix; unresolved.
- No full-suite run this session; that is intentional.

### 2. Disk / failed recording: tested on the laptop
- C: free space: **22.54 GB**.
- Source item 20 ("America First Ep. 1780"): state `failed`, recording_id None.
  - Its folder `data\work\live\src001_v7iial2\` now contains **only** `segments.txt` and `seg_001.log` (0 MB).
  - The authorised files `seg_001.ts` (11.37 GB) and `recording.mp4` are **already gone**. Which earlier operation removed them is unknown; do not guess.
  - `Recover-FailedRecording.cmd` is not needed for those files.
- No other files over 200 MB under `data\`. Folder sizes: clips 0.20 GB, models 0.14 GB, recordings about 0 GB.
- **Not done:** `Storage-Audit.cmd`, the Storage page, a whole-drive scan for other large temporary files, and Codex's separate ENOSPC workspace problem.

### 3. Installed app: inspected read-only, not updated
- `INSTALLED.txt`: installed 16 Nov from the established branch. DB `meta.schema_version` = **5**, so **neither Session 10 (v8) nor Session 11 is installed**.
  Hook titles, frame lines, the storage audit, selection v6 and the bounded live recorder are all **absent** on the laptop.
- The app is running (dashboard returns HTTP 200; supervisor plus app process under the `.venv` Python 3.10.4).
  - Job queue is clean: 24 `cloud_upload` done, 30 renders, 4 processing, 4 downloads, 1 download cancelled; nothing pending or failed.
- **Risk:** source 1 "Rumble" (channel `rumble.com/c/nickjfuentes`) is enabled with `record_live=1` and polling, on the **old unbounded live recorder**, the one that caused the 11 GB disk-fill.
  The update removes this risk; do it promptly once CI is green.
- Recent log warnings: Rumble HTTP 429 (Too Many Requests) on 6 polls; one earlier "Not enough free disk space to download this N-hour video".
- Python 3.10 deprecation warning: not found in the app log.
  The only deprecation seen is in pytest: `PendingDeprecationWarning: Please use 'import python_multipart'`, from the python-multipart package. That may be what the owner saw.
  Python 3.10.4 itself is past upstream end of life (Oct 2026). Upgrade only after confirming dependency support. Unverified.
- `source_permissions`: one row, `rumble.com`, granted_by "Rumble exec", reference "Email", scope "Confirmed", 2026-11-15.
- **Owner attestation (chat, 18 Nov):** this permission covers republishing clips from all four recordings, including the Alex Jones interview.
  Not yet entered in the DB; all clips are still `rights_status=unreviewed`.
- `social_accounts`, `manual_posts`, `publish_attempts` and `publication_packages` are all **empty**. No account is confirmed and **no upload has occurred**.

### 4. Existing clips reviewed: full detail in `SESSION12_CLIP_REVIEW.md` in the owner's session folder; reproduce it from the DB if it is not available
- 24 clips, all `ready`, 6 from each of 4 recordings. All four source recordings were deleted after clipping (`file_path` NULL).
  Re-editing or re-rendering (including adding hook titles or frame lines) therefore requires **re-downloading** the source. Ask the owner before re-downloading.
- Each recording, its Rumble URL and its clips:

  | Rec | Title | Rumble URL | Clips |
  |---|---|---|---|
  | 1 | Alex Jones Interviews Nick Fuentes at NXR Conference | https://rumble.com/v7ieyrk-alex-jones-interviews-nick-fuentes-at-nxr-conference.html | 1–6 |
  | 2 | ASSEMBLE THE ARMY!!! | https://rumble.com/v7iey3o-assemble-the-army.html | 7–12 |
  | 3 | America First Ep. 1779 | https://rumble.com/v7iggak-america-first-ep.-1779.html | 13–18 |
  | 4 | Iran Launches CYBERATTACK Against America??? | https://rumble.com/v7ihdlu-iran-launches-cyberattack-against-america.html | 19–24 |

- Playback check (tested): all 24 decode fully with 0 errors; 1080×1920 H.264 + AAC; mean loudness −18 to −21 dB.
  Start, middle and end frames were viewed for the candidates, and full transcripts plus context were read.
  The layout is a blurred background with a centred 16:9 picture and subtitles below. No hook title and no frame lines, because of the old build.
- **Classification:**
  - **Usable, ready for owner approval:** **#19** (2:26, Fuentes reads the NYT report on the Microsoft/FAA/United/Chase outages, *including* the companies' statements that they were not cyberattacks) and **#14** (0:51, Palantir explainer with a natural ending).
  - **Needs re-editing (source needed):** #4 (answer cut before its qualification), #1 (opens mid-sentence), #6 (two topics; unsupported "Israelis can fabricate a crisis" claim).
  - **Held:** #2 ("clearly an attack" FAA claim, needs context), #3 ("Republicans steal over half the money"), #5 (Israel regime-change advocacy, owner editorial call), #13 (show's profane promo; burned-in text duplicates our subtitles), #15 (low value).
  - **Rejected (14):** #7 (slurs and abuse montage), #8, #10 and #12 (super-chat reading), #9 and #18 (antisemitic content), #11 (boast with a factual error), #16 and #17 (insults, slur, kidnapping "joke"), #20 (COVID conspiracy / "prepare for war"), #21 (bomb rumours), #22 ("apocalypse, buy gold"), #23 ("planes falling out of the sky"), #24 (health-product anecdote).
- **Defects found in the installed (old) build:**
  - The safety classifier marked all 24 clips `clear`, including slurs and antisemitic content. It is unreliable.
  - The heuristic selector gave its top score (0.877) to super-chat reading (#12).
  - Re-check both after the update; selector v6 is meant to help. Unverified.
- **Draft upload text (brand: America First Monitor; awaiting owner approval; nothing posted):**
  - **#19**
    - Title: "Nick Fuentes reads the NYT report on the Microsoft, FAA and Chase outages".
    - Caption: notes that Microsoft and JPMorgan Chase said their outages were not cyberattacks, and adds "Clip shared for commentary and context; views are the speaker's own."
    - Do **not** reuse the stream's "Iran Launches CYBERATTACK" headline; the clip does not support it.
  - **#14**
    - Title (exact transcript quote): `Nick Fuentes: "Palantir seems to be just getting started"`.
    - Caption: attributes the DOGE claim to the speaker.

## Next steps (in order)
1. Check CI run 44295021720.
   - If the windows-latest job is **green**: fast-forward `claude/optimistic-noether-k52qjf` 2259593 → 2a0434a (approved; no force) and confirm with `git ls-remote`.
   - If it is **red**: get the failing test from the job annotations or the owner's browser, fix it on `claude/sharp-bell-utpc9o`, and rerun only the targeted tests.
2. Update the laptop install:
   - stop the engine;
   - copy `data\clipengine.sqlite3` to a timestamped backup;
   - run `Install-Clipping-Engine.cmd`;
   - start the app.

   Then verify on the laptop:
   - migration v5 → v8 succeeds and the data is intact (24 clips, 4 recordings, 24 cloud objects, the source permission);
   - `SELECTION_PROFILE=v6`, worker health, the job queue, the Storage page and `Storage-Audit.cmd`;
   - the Drive connection, and that settings persist across a restart.

   Report the actually installed commit.
3. Owner editorial approval of #19 and #14:
   - enter the owner's rights attestation in the engine;
   - prepare final MP4 + text per platform;
   - support **manual** upload only;
   - record only platform-confirmed uploads.

   Ask whether to re-download recordings 4 and 3 so #19 and #14 can be re-rendered with hook titles and blue frame lines (`#2856D8`, 2 px at 1080 width; America First Monitor only).
4. Google Drive restore test on an expendable clip (suggest a rejected one, e.g. #12, with owner OK): verify master → safe local cleanup → restore → checksum match → playback → DB lifecycle.
5. Validate hook titles and frame lines on real renders (16:9, 4:3 and 1:1 sources) after the update.
6. Validate live segmentation on Windows with a short, **authorised** stream:
   - rotation; timestamp, audio and video continuity; overlap trim;
   - interruption recovery; low-disk handling; join to `.ts`; ingest; clip generation; cleanup.

   The proctree fix is now part of this path.
7. `scripts/compare_selectors.py`: the named source files (`Alex_Jones_..._clip6_1377s.mp4`, `ASSEMBLE_THE_ARMY_clip12_3651s(1).mp4`) were not found in the engine data.
   Search the owner's Downloads or ask; do not invent results.
8. API credentials via `Set-Credentials.cmd` → `Check-Portfolio.cmd`. Mark a platform verified only after a real successful request (YouTube Data API v3, Twitch, Kick).
9. 24 publishing accounts in batches. The owner keeps notes in `OneDrive\Documents\ChatGPT\Clipping\publishing-account-directory.csv` and `publishing-setup-notes.txt`; read only with permission.
   Pause for sign-in, CAPTCHA and verification.
10. Only after the above: a bounded proposal for temporary raw-segment cloud storage (Google Drive vs MEGA). It must cover free quotas, transfer limits, verification, retention, the local scratch space needed and the eight-gate raw-deletion policy. No paid services. Not implemented yet.

## Environment notes for whoever runs locally
- The Session 12 checkout lived in a temporary session folder on the laptop; clone again if needed. `gh` CLI is not installed. git over HTTPS works for push.
- To run tests on the laptop without touching the install, use the installed venv interpreter `%LOCALAPPDATA%\ClippingEngine\.venv\Scripts\python.exe -m pytest ...`
  from a separate checkout. That venv has pytest, faster-whisper and yt-dlp. Tests use temp dirs and never touch the owner's `.env` or data.
- ffmpeg on the laptop is winget Gyan.FFmpeg 9.0.1 (a real exe, not a shim). Chocolatey is also installed.

## Non-negotiable rules
- Discovery is not permission to record or download. No DRM, paywall or auth bypass. No unpermitted recording.
- No automatic public publishing. Human editorial approval is mandatory.
- Accurate, neutral handling of political footage. Never invent, sensationalise or mislead in hook titles.
- Preserve brand and account isolation, rights records, DB history, Drive masters and credentials.
- Never delete personal or OneDrive files, unverified cloud masters, or unrelated recordings.
- No new destructive retention policy without explicit owner approval. Do not re-render already-approved clips without permission.
- Never commit or print secrets. No force-push. No PR without authorisation. No paid APIs or services. Avoid repeated full-suite runs.

Owner's priority: a reliable engine on the Lenovo, and genuinely usable, approved clips uploaded manually as soon as possible.
