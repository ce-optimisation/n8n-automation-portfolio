# Local AI Video Clipping & Editorial Review Engine

A local-first Python application. It takes a long recording, transcribes it with a local Whisper model, picks
self-contained excerpts that preserve context, renders them as captioned 1080×1920 vertical MP4s, and puts them in
a mobile-friendly review queue. Nothing is published unless you approve it, record its rights basis **and** confirm
the upload.

It is built for political-media monitoring, factual reporting, documentary analysis and critical commentary, with
provenance kept throughout. It is not a growth or promotion engine.

| | |
|---|---|
| Backend | Python, FastAPI, SQLite (WAL) with a persistent, restart-safe job queue |
| UI | Server-rendered HTML, HTMX and responsive CSS; works on a phone |
| Media | FFmpeg / FFprobe: x264 + AAC, libass captions, EBU R128 loudness |
| Speech | faster-whisper with Silero VAD, word timestamps, CPU or NVIDIA GPU; long recordings are transcribed in checkpointed chunks |
| Selection | Deterministic scorer (profile v2), optionally re-ranked by a local Ollama model, which can only choose among real candidates |
| Publishing | Official TikTok Content Posting API (upload to inbox) and Instagram Graph API (Reels), run as resumable background jobs |

There is no Docker, Redis, Postgres, cloud service, paid API or subscription.

---

## 1. Install and launch (Windows)

1. Download the repository and extract it to a local folder. Avoid OneDrive-synced folders for the data, because
   videos are large.
2. Double-click **`clipping-engine\Start-Clipping-Engine.cmd`**. The launcher:
   - finds Python 3.12, 3.13, 3.11 or 3.10. If none is found, it offers `winget install Python.Python.3.12`, which
     needs no admin rights.
   - creates a private `.venv` and installs the packages. This happens on first run and again only when
     `requirements.txt` changes.
   - finds FFmpeg in `tools\ffmpeg\bin`, on PATH, or in the WinGet links folder. If it is missing, it offers
     `winget install Gyan.FFmpeg`.
   - creates `.env` from `.env.example`.
   - prints the detected hardware, the chosen speech model and practical tips.
   - opens **http://127.0.0.1:8765**.
3. Open **Settings → Setup checklist**. It shows what is ready and the next action for anything that isn't. Use
   **Download Whisper model now** to fetch the speech model (75 MB–3 GB) once while you are online. The launcher
   option `--download-model` does the same.

If the launcher finds port 8765 in use, it either opens the engine that is already running or moves to the next
free port. `Start-Clipping-Engine.cmd --check` validates the installation without starting the app. On
macOS/Linux, use `./start.sh`.

**GPU (optional):** with an NVIDIA card, set `INSTALL_CUDA_LIBS=1` in `.env` and relaunch. The CUDA 12 runtime
wheels (~1 GB) are installed. If the GPU fails, the engine falls back to the CPU automatically.

| Detected hardware | Whisper model chosen (`WHISPER_MODEL=auto`) |
|---|---|
| NVIDIA GPU ≥ 10 GB VRAM | large-v3 (float16) |
| NVIDIA GPU ≥ 5 GB | medium (float16) |
| Other NVIDIA GPU | small (float16) |
| CPU with ≥ 8 threads and ≥ 16 GB RAM | small (int8) |
| CPU with ≥ 4 threads and ≥ 8 GB RAM | base (int8) |
| Smaller machines | tiny (int8) |

`TRANSCRIBE_LANGUAGE=en` selects the more accurate English-only variants.

## 2. Process your first recording

You have three ways to bring in a recording:

- **Upload:** go to **Import → Upload a recording**, choose the file, and click **Analyse Video**.
- **Inbox folder:** save or copy a video into `data\inbox`, or the folder set in `INBOX_DIR`. It is imported
  automatically once the file has finished copying.
- **Link:** go to **Import → From a link** (see §3).

Then:

1. Leave it running. The Dashboard shows each stage: extract audio, transcribe (chunk by chunk), select excerpts,
   render.
2. Open **Review**. Each candidate shows:
   - the vertical preview
   - the source title and link, and the timestamps
   - the direct transcript, with context before and after
   - a neutral description with a Copy button
   - safety, rights, confidence and publishing badges
   - **Approve**, **Reject**, **Regenerate** and **Download MP4**
3. **Adjust / details** shows the transcript with low-confidence words underlined and the original recording cued
   to the excerpt, so you can verify it. You can also change the start and end times and regenerate (the stored
   transcript is reused), edit the description, record the rights basis, and publish.

## 3. Importing from links: what is fetched, and why

| Link | What happens |
|---|---|
| **Direct video file** (`https://…/meeting.mp4`, `.mov`, `.webm`…) from a public website | Downloaded automatically. Resumes after interruptions using HTTP Range, checks the content type and size, and retries temporary failures. |
| **Web page** on a site listed in `ALLOWED_URL_DOMAINS` | Retrieved with yt-dlp. List a site only if its terms and the rights holder permit automated downloading. |
| **Rumble, YouTube, TikTok, Instagram, Facebook, X, Twitch, Kick, BitChute, Odysee** | **Not fetched.** Saved as a *pending source* (see below). |
| Any other web page | Saved as a pending source, with an explanation. |

**Why Rumble links are not downloaded automatically:** Rumble's Terms of Use prohibit automated access to the site
without Rumble's *prior written permission*. The engine therefore offers two lawful routes:

1. **Link + file (works now).** Paste the Rumble link. It is saved as *pending source #N*, and the link stays as
   the recording's provenance. Get the original file from the creator or rights holder, then either click
   **Attach & analyse** on the pending source, or save the file into the inbox folder with `src-N` in its name.
   Everything after that is automatic.
2. **Recorded platform permission.** If Rumble (or another platform) grants you written permission, record it in
   **Settings → Source permissions** (who granted it, the date, a reference and the scope). Links from that
   domain are then retrieved automatically, and the permission is cited in each import's provenance.

**Network safety:** only `http(s)` on standard ports is accepted, and credentials in URLs are refused. Every
redirect hop must resolve to a public address, and the connected peer is re-checked, which defeats DNS
rebinding. Nothing passes cookies or logins, or bypasses DRM, paywalls or rate limits. Duplicates are detected
both by normalised URL and by SHA-256 of the file.

## 4. Long recordings

- **Checkpointed transcription.** Audio is extracted once to 16 kHz mono (about 115 MB per hour), then
  transcribed in chunks of about 20 minutes (`TRANSCRIBE_CHUNK_SECONDS`). Chunks are cut at the quietest moment
  near each boundary so words aren't split. Each chunk is saved as soon as it finishes. After a crash, power loss
  or restart, the job resumes at the first unfinished chunk and reuses the extracted audio.
- **Bounded memory.** Only the current chunk's audio is held in memory, about 77 MB for 20 minutes.
- **Disposable files.** The audio file is deleted once transcription completes. Stale work files are cleaned up
  after 24 hours.
- **Processing times** for each recording (audio, model load, transcription speed and selection) are shown on its
  page.
- Measurements in this sandbox with a 58-minute recording, excluding transcription (a TEST-ONLY stub transcript
  was used):
  - selection took 5.9 s across 13,398 candidate windows
  - each 55 s clip rendered in 12–16 s on 4 CPU threads
  - peak memory was 123 MB (excluding the Whisper model)

  Real-Whisper long-form figures come from the CI benchmark (`scripts/longform_benchmark.py`; see HANDOFF.md for
  the latest numbers).

## 5. How excerpts are chosen (profile v2)

1. Whisper word timings are grouped into sentences, splitting on punctuation and on pauses over 1.6 s.
2. Every window of whole sentences between `CLIP_MIN_SECONDS` and `CLIP_MAX_SECONDS` that ends on a full stop
   is scored. It gains points for:
   - **substance:** content density, claims, figures and reasoning
   - **questions with their answers**
   - **clarity:** word confidence
   - **natural pauses** at both ends
   - **opening at the start of a topic**
   - **closeness to the target length**

   It loses points for **starting mid-thought** ("And…", "But…"), **filler** and **procedural talk** (greetings,
   housekeeping, "next item").
3. Choice: greedy, with no overlap, a gap of at least 5 s, and limited word overlap between clips. Candidates
   scoring well below the best are **not used as padding**; the recording page explains when fewer than
   `CLIPS_PER_RECORDING` (default 6) were found.
4. **Optional local LLM (Ollama):** it may re-rank candidates by index number only. It can't invent text or
   timestamps, and its summaries are labelled *machine interpretation*.

**Reproducible evaluation:** `scripts/make_longform_sample.py` generates labelled long recordings. The text is
original, the speech is synthesised locally, and each paragraph is tagged substantive or procedural, with a topic.
`scripts/evaluate_selection.py` compares profiles on that labelled sample.

| Sample | Profile | Substantive share | Procedural share | Topic diversity |
|---|---|---|---|---|
| 58 min | v1 | 0.81 | 0.115 | 0.83 |
| 58 min | v2 | **0.94** | **0.00** | 0.83 |
| 9 min | v1 | 0.77 | 0.15 | 0.83 |
| 9 min | v2 | **0.92** | **0.02** | **1.0** |

All of these clips started cleanly and ended on terminal punctuation. The sample text is template-based and the
scorer uses similar cues, so compare the profiles rather than trusting the absolute numbers.

## 6. Editorial safeguards

- **Quotes vs interpretation.** Descriptions separate *Direct quotation* from any *Automated summary*. Factual
  titles only; the engine never writes headlines.
- **Context and provenance.** Each clip stores the transcript before and after it. Each rendered clip carries a
  source label. Every recording keeps `provenance.json` with:
  - the SHA-256 hash
  - the source URL
  - the retrieval method
  - the final download URL
  - any recorded permission
- **Safety screen.** Violence advocacy, dehumanising language and extremist terminology are flagged. You can
  extend the term list in `data/safety_terms.txt`. A flagged clip gets no summary, and publishing is blocked until
  you record an editorial decision note.
- **Rights.** Publishing requires a recorded rights basis: owned, licensed, public domain, or fair dealing
  reviewed. Approval alone is not enough.

## 7. Publishing (needs your developer accounts)

Until a platform is connected, use **Download MP4** and **Copy description**.

Uploads run as **background jobs that survive restarts** and appear on each clip as:

- Upload queued
- Uploading
- Platform processing
- Retrying
- Awaiting platform connection
- **Delivered to TikTok inbox (confirmed)**
- **Published (confirmed)**
- Failed

"Confirmed" only ever comes from the platform's own API. Each stage is checkpointed, so a failure or restart
never uploads or publishes twice. Sending an already-delivered clip again requires an explicit "Send again".

### TikTok: upload to inbox (draft)

**Flow:**

1. `POST /v2/post/publish/inbox/video/init/` (FILE_UPLOAD)
2. Upload the file in chunks.
3. Poll `/v2/post/publish/status/fetch/` until TikTok returns `SEND_TO_USER_INBOX`.
4. You then edit and post the video in the TikTok app.

The engine validates the video first (H.264/AAC, 3–600 s). It recognises TikTok's error codes:

- rate limits are retried automatically
- an expired token is refreshed automatically
- "too many pending shares" explains that you need to clear drafts in the TikTok app
- "unaudited client" is shown as awaiting platform approval

**Owner steps:**

1. Create or sign in to your TikTok account. In the TikTok app, set the account to Public (TikTok may require
   this, depending on your app's review status).
2. Go to <https://developers.tiktok.com>, then **Manage apps → Connect an app**, and choose a **Desktop** app.
3. Add the **Login Kit** and **Content Posting API** products, and enable the scopes `user.info.basic` and
   `video.upload`.
4. Register the redirect URI `http://127.0.0.1:8765/oauth/tiktok/callback`. If the portal rejects loopback
   addresses, use a **Web** app with your Tailscale HTTPS address
   (`https://<pc>.<tailnet>.ts.net/oauth/tiktok/callback`, see §9) and set `TIKTOK_APP_TYPE=web`.
5. Add your own account as a tester, or submit the app for review if TikTok requires it. Review can take days.
6. In your local `.env`, set `TIKTOK_CLIENT_KEY`, `TIKTOK_CLIENT_SECRET` and `TIKTOK_REDIRECT_URI`. Never paste
   these into a chat.
7. Restart the app, then go to **Settings → Connect TikTok** and approve the request.
8. Approve a clip, set its rights basis, and click **Send to my TikTok inbox**.

### Instagram: publish a Reel (public; the API has no drafts)

The dashboard's review queue is your draft stage. Publishing requires typing `PUBLISH`.

| `INSTAGRAM_AUTH_MODE` | Requirements | How the video reaches Meta |
|---|---|---|
| `facebook` (default) | Instagram **Business or Creator** account **linked to a Facebook Page**, plus a Meta app with Facebook Login for Business | Resumable upload of the local file to `rupload.facebook.com`. The dashboard never needs to be reachable from the internet. |
| `instagram` | Instagram Business or Creator account, **no Facebook Page**, plus a Meta app with "Instagram API with Instagram Login" | Meta's docs offer resumable upload only for Facebook Login. With Instagram Login, Meta fetches the clip from a **one-time link**, `PUBLIC_MEDIA_BASE_URL/public/media/<token>.mp4`. The link serves only that clip, expires after 2 hours, and is revoked after publishing. It requires a public HTTPS address such as **Tailscale Funnel**. |

`facebook` mode is the default because it keeps the PC completely private. Linking a free Facebook Page takes a
few minutes, whereas the alternative requires exposing a public endpoint.

**Owner steps (facebook mode):**

1. In the Instagram app, go to **Settings → Account type and tools**, switch to a **Creator** (or Business)
   account, and link a Facebook Page. Create one at <https://www.facebook.com/pages/create> if needed.
2. At <https://developers.facebook.com/apps>, create an app and add **Facebook Login for Business** and the
   **Instagram Graph API**.
3. Add the valid OAuth redirect URI `http://localhost:8765/oauth/instagram/callback`. Meta allows localhost in
   development mode; otherwise use your Tailscale HTTPS address.
4. Request these permissions:
   - `instagram_basic`
   - `instagram_content_publish`
   - `pages_show_list`
   - `pages_read_engagement`
   - `business_management`

   Then either keep the app in **development mode** with your own account added under **App roles**, or complete
   App Review.
5. In `.env`, set `META_APP_ID` and `META_APP_SECRET`, restart, and use **Settings → Connect Instagram**.

**Instagram mode instead:** create an app with **Instagram API → API setup with Instagram login**, request
`instagram_business_basic` and `instagram_business_content_publish`, and add an HTTPS redirect URI. Then set
`INSTAGRAM_AUTH_MODE=instagram`, `INSTAGRAM_APP_ID`, `INSTAGRAM_APP_SECRET`, `INSTAGRAM_REDIRECT_URI` and
`PUBLIC_MEDIA_BASE_URL`. Long-lived tokens are refreshed automatically.

Meta allows 100 API-published posts per 24 hours.

## 8. Reliability and storage

- Jobs are stored in SQLite. Running jobs resume after a restart. Transient errors are retried with backoff, and
  permanent errors (an unreadable file, no speech, an unsupported link) fail immediately with a specific message.
  Use **Retry** on the Jobs page.
- Re-running a job never creates duplicate clips. *Re-select excerpts* replaces only unreviewed candidates.
- Retention:
  - rejected renders are removed after `DELETE_REJECTED_RENDERS_AFTER_DAYS`
  - originals are **never** deleted unless you set `DELETE_ORIGINALS_AFTER_DAYS`
  - inbox files that are duplicates or fail to import are moved to `inbox\duplicates` or `inbox\failed`, never
    deleted
- Logs go to `data/logs/clipengine.jsonl` (structured JSON), and each job also has its own log.

## 9. Phone access, securely

- **Without a password,** the dashboard only answers requests addressed to `localhost` on this PC. This also
  blocks DNS-rebinding attacks.
- **Recommended: Tailscale** (free, private, encrypted):
  1. Install Tailscale on the PC and on your phone, and sign in with the same account. Download it from
     <https://tailscale.com/download>.
  2. Set `DASHBOARD_PASSWORD` in `.env` and restart.
  3. On the PC, run `tailscale serve --bg 8765`.
  4. On your phone, open `https://<pc-name>.<tailnet>.ts.net`.

  Nothing is exposed to the internet, and the app stays bound to 127.0.0.1.
- **Home Wi-Fi only** (unencrypted): set `CLIPENGINE_HOST=0.0.0.0` and `DASHBOARD_PASSWORD`, and allow the
  firewall prompt for *Private networks* only. The launcher refuses `0.0.0.0` without a password.
- Login attempts are rate-limited (5 per 10 minutes). Cookies are HttpOnly, SameSite=strict, and Secure over
  HTTPS. Cross-origin POSTs are refused.
- Never port-forward the dashboard.

## 10. Tests

```
python -m pytest -v                                  # offline suite
CLIPENGINE_REAL_WHISPER=1 python -m pytest -v        # + real Whisper transcription
CLIPENGINE_LIVE_NET=1 python -m pytest -v            # + live public-domain URL import (internet)
python scripts/acceptance_test.py                    # 9-step end-to-end against the running app
python scripts/longform_benchmark.py --minutes 60 --crash-test --out bench   # real long-form + hard-kill resume
python scripts/evaluate_selection.py bench/longform_60min.timing.json --transcript bench/transcript_segments.json
```

The tests are labelled by what they actually prove:

- Tests marked *stub* use a **test-only** transcript, so they never count as verification of transcription.
- Publishing tests use **mocked** TikTok and Instagram HTTP, and verify request construction, resumption and
  duplicate protection only.
- Local-server download tests use the test-only `allow_private_urls` switch. One test keeps protection on and
  shows that a private peer is refused.

CI (`.github/workflows/clipping-engine.yml`) runs everything on Ubuntu and Windows. That covers the Windows
launcher check, real Whisper, the live internet import and acceptance. The long-form job runs on manual dispatch.

## 11. Troubleshooting

| Symptom | Fix |
|---|---|
| "ffmpeg not found" | Run `winget install Gyan.FFmpeg`, or put `ffmpeg.exe` and `ffprobe.exe` in `tools\ffmpeg\bin`, then relaunch. |
| First transcription seems stuck at "Loading model" | The model is downloading once. Use Settings → Download Whisper model now while online. |
| Slow transcription | Use `WHISPER_MODEL=base.en` and `TRANSCRIBE_LANGUAGE=en`; `WHISPER_BEAM_SIZE=2`; or enable the GPU with `INSTALL_CUDA_LIBS=1`. |
| A link was "saved as pending source" | The site doesn't permit automated download (see §3). Attach the file, or record written permission. |
| Phone gets "only accepts connections addressed to localhost" | Set `DASHBOARD_PASSWORD` and use Tailscale (§9). |
| TikTok "awaiting platform approval" or "unaudited client" | Your TikTok app needs review, or your account must be added as a tester. |
| Instagram "No Instagram professional account is linked" | Link the account to a Facebook Page, or use `INSTAGRAM_AUTH_MODE=instagram`. |
