# Local AI Video Clipping & Editorial Review Engine

A local-first Python application that takes a long recording, transcribes it with a local Whisper model,
picks out self-contained excerpts that preserve context, renders them as captioned 1080×1920 vertical MP4s, and
puts them in a mobile-friendly review queue. Nothing is published unless you approve it **and** confirm it.

Use it for political-media monitoring, factual reporting, documentary analysis and critical commentary. It is
built to preserve context and provenance. It is not a growth or promotion engine.

| | |
|---|---|
| Backend | Python, FastAPI, SQLite (WAL) with a persistent job queue |
| UI | Server-rendered HTML, HTMX, responsive CSS (works on a phone) |
| Media | FFmpeg / FFprobe (x264 + AAC, libass captions, EBU R128 loudness) |
| Speech | faster-whisper (CTranslate2) with Silero VAD, word timestamps, CPU or NVIDIA GPU |
| Selection | Deterministic scorer; can be re-ranked by a local Ollama model, which may only choose among real candidates |
| Publishing | Official TikTok Content Posting API (inbox upload) and Instagram Graph API (Reels) |

No Docker, Redis, Postgres, cloud services, paid APIs or subscriptions are needed.

---

## 1. Launch (Windows)

1. Double-click **`Start-Clipping-Engine.cmd`**.
2. On the first run the launcher:
   - finds Python 3.10–3.13. If it is missing, it offers to install Python 3.12 with `winget` (no admin needed).
   - creates a private environment in `.venv` and installs `requirements.txt`. This takes a few minutes and is
     repeated only when the requirements change.
   - finds FFmpeg. If it is missing, it offers `winget install Gyan.FFmpeg` (no admin needed). Alternatively,
     extract a build so that `tools\ffmpeg\bin\ffmpeg.exe` exists.
   - creates `.env` from `.env.example`.
3. The dashboard opens at **http://127.0.0.1:8765**. Keep the console window open while processing. Closing it
   stops the engine, and unfinished jobs resume on the next launch.

The first transcription downloads the Whisper model into `data\models` (75 MB–3 GB depending on the model).
After that, everything runs offline.

`Start-Clipping-Engine.cmd --check` validates the setup and prints the detected hardware and the chosen model.
On macOS/Linux, use `./start.sh` instead.

### Automatic model choice

The engine inspects the CPU, RAM and GPU at runtime (shown on **Settings**):

| Hardware | Whisper model |
|---|---|
| NVIDIA GPU ≥ 10 GB VRAM | large-v3 (float16) |
| NVIDIA GPU ≥ 5 GB | medium (float16) |
| Other NVIDIA GPU | small (float16) |
| CPU, ≥ 8 threads and ≥ 16 GB RAM | small (int8) |
| CPU, ≥ 4 threads and ≥ 8 GB RAM | base (int8) |
| Smaller machines | tiny (int8) |

Setting `TRANSCRIBE_LANGUAGE=en` uses the more accurate English-only variants. You can override the choice with
`WHISPER_MODEL`.

For an NVIDIA GPU on Windows, set `INSTALL_CUDA_LIBS=1` in `.env` before launching. The launcher then installs
the CUDA 12 runtime wheels (~1 GB). If the GPU cannot be used, the engine falls back to the CPU automatically.

## 2. Process your first recording

1. **Import → Upload a recording**: choose an MP4 or MOV, optionally give it a title and a rights note, and
   click **Analyse Video**. An upload progress bar is shown.
2. Leave it running. The **Dashboard** shows live job progress: extract audio → transcribe → select excerpts →
   render each clip. As a rough guide, a one-hour recording with the `small` model on a modern 8-core CPU
   takes about 15–30 minutes to transcribe, plus about 10–30 s per clip to render.
3. Open **Review**. Each candidate shows:
   - a vertical video preview
   - the source title and URL, and the start and end times
   - the direct transcript, plus expandable context before and after
   - a suggested neutral description with a Copy button
   - safety, rights and confidence badges
   - **Approve**, **Reject**, **Regenerate** and **Download MP4**
4. **Adjust / details** shows the transcript with low-confidence words underlined and the original recording
   cued to the excerpt, so you can verify it. You can also:
   - enter new start and end times and regenerate. This reuses the stored transcript, does not re-transcribe,
     and snaps the boundaries so words are never cut.
   - edit the description
   - record the rights basis
   - publish, where connected

### Phone access on your private network

In `.env`, set `CLIPENGINE_HOST=0.0.0.0` and a `DASHBOARD_PASSWORD`. The engine refuses to listen on the network
without a password. Restart and allow access when Windows Firewall asks (choose *Private networks*). The console
prints the address to open on your phone, for example `http://192.168.1.20:8765`.

## 3. How excerpts are chosen

1. Whisper word timestamps are grouped into sentences, splitting on punctuation and on pauses longer than 1.6 s.
2. Every window of whole sentences between `CLIP_MIN_SECONDS` and `CLIP_MAX_SECONDS` that ends on a full stop
   is scored on:
   - **substance**: content-word density, plus markers of claims, figures, reasoning and policy
   - **questions answered** within the window
   - **transcription clarity** (word confidence)
   - **natural pauses** at both ends
   - **closeness to the target length**

   Penalties apply to filler words and to excerpts that start mid-thought ("And…", "But…", "Because…").
3. The top windows are chosen greedily, with no overlap and limited word overlap (Jaccard), so they are not
   repetitive.
4. **Optional local LLM.** If [Ollama](https://ollama.com) is running (for example `ollama pull llama3.2:3b`),
   it may re-rank the candidates. It only receives index numbers and can only return index numbers, so it can
   never invent text or timestamps. Its one-line summaries are stored separately, stripped of quotation marks and
   labelled *machine interpretation*. Invalid output falls back to deterministic ranking.

The selection prompt explicitly excludes outrage, insults and emotional manipulation. Short recordings get
relaxed length bounds automatically.

## 4. Editorial safeguards

- **Quotes vs. interpretation.** The description separates *Direct quotation* from any *Automated summary*.
  Titles are factual ("Excerpt (12:04–12:58) from …"). The engine never writes headlines.
- **Context.** Every clip stores the transcript before and after it. Rendered clips carry a small provenance
  label (source title and timestamp). Each recording folder keeps `provenance.json`, with the SHA-256 hash,
  source URL, original filename and import time.
- **Safety screen.** Clips are screened for advocacy of violence, dehumanising language and extremist
  terminology. You can extend the term list in `data/safety_terms.txt`, one regex per line. A flagged clip:
  - is marked **⚑ editorial review**
  - gets no promotional or summary text
  - cannot be published until you record an editorial decision note
  - is never auto-deleted

  The screen is a prompt for human judgement, not a classifier.
- **Low-confidence transcription** is underlined in the UI and counted in the description.
- **Rights.** Publishing requires a recorded rights basis: owned, licensed, public domain, or fair dealing /
  fair use reviewed. Approval alone is not enough.

## 5. URL ingestion policy

URL retrieval (via yt-dlp) is attempted only when **all three** of these hold:

1. the domain is listed in `ALLOWED_URL_DOMAINS`.
2. the domain is not one of the restricted platforms below.
3. you tick the per-import authorisation confirmation.

**YouTube, Rumble, TikTok, Instagram, Facebook, X, Twitch, Kick and BitChute are always refused**, with an
explanation, because their terms prohibit automated downloading. For those, get the file through an authorised
route, such as YouTube Studio for your own uploads, the platform's own download option, or the rights holder, and
use **Upload**.

No cookies, logins, DRM circumvention, geo-bypass or rate-limit evasion are used. Duplicate files are detected by
SHA-256, and duplicate URLs by their normalised form.

## 6. Publishing (optional; needs your developer accounts)

Until a platform is connected, use **Download MP4** and **Copy description** on each clip. Publishing always
requires the clip to be approved, a rights basis to be recorded, and any safety flag to have a recorded decision.

### TikTok: upload to inbox (draft)

The engine uses the official Content Posting API **upload** flow:
`/v2/post/publish/inbox/video/init/` → chunked PUT → `/v2/post/publish/status/fetch/`. It reports success only
when TikTok returns `SEND_TO_USER_INBOX` or `PUBLISH_COMPLETE`. You then finish and post the video in the TikTok
app.

**What you need to do:**

1. Sign in at <https://developers.tiktok.com>, then go to **Manage apps → Connect an app**.
2. Add the **Login Kit** and **Content Posting API** products. Request the scopes `user.info.basic` and
   `video.upload`.
3. Register this redirect URI exactly: `http://localhost:8765/oauth/tiktok/callback`. If TikTok requires HTTPS
   for your app type, use an HTTPS tunnel address you control, and set the same value in `.env`.
4. Submit the app for review if required. Until it is approved, TikTok limits who can authorise it, so add
   yourself as a target user or tester.
5. Put `TIKTOK_CLIENT_KEY` and `TIKTOK_CLIENT_SECRET` in your local `.env`. Never paste them into chat or
   commit them.
6. Restart, open **Settings → Connect TikTok**, and approve the request in the browser.

Tokens are stored in Windows Credential Manager via `keyring`. Uploads are rate-limited locally to the platform's
6 per minute.

### Instagram: publish a Reel

The official API **cannot create drafts**: a Reel published through it goes public immediately. The dashboard
therefore asks you to type `PUBLISH`. The flow is: `POST /{ig-user-id}/media` (REELS, resumable) → upload to
`rupload.facebook.com` → wait until the status is `FINISHED` → `media_publish`. Success is reported only with the
media ID and permalink returned by the API.

**What you need to do:**

1. Use an Instagram **Business or Creator** account linked to a Facebook Page.
2. At <https://developers.facebook.com>, create an app of the *Business* type and add **Facebook Login for
   Business** and the **Instagram Graph API**.
3. Add the redirect URI `http://localhost:8765/oauth/instagram/callback`. Meta allows localhost in development
   mode; otherwise use an HTTPS address you control.
4. Request the permissions `instagram_basic`, `instagram_content_publish`, `pages_show_list`,
   `pages_read_engagement` and `business_management`. Use the app in development mode with your own account, or
   complete App Review.
5. Put `META_APP_ID` and `META_APP_SECRET` in `.env`, restart, and click **Settings → Connect Instagram**.

Meta's limit is 100 API-published posts per 24 hours.

## 7. Reliability and storage

- **Jobs live in SQLite.** If the app or PC stops mid-job, running jobs are re-queued at the next start.
  Transcripts are reused, so work resumes instead of starting over.
- **Retries.** Transient errors retry with backoff, up to `JOB_MAX_ATTEMPTS`. Permanent errors (unreadable
  file, no speech, missing FFmpeg) fail immediately with a clear message, and **Retry** is available on the Jobs
  page.
- **Re-running a job never duplicates clips.** Renders are keyed by clip and revision. *Re-select excerpts*
  replaces only unreviewed candidates.
- **Disposable files.** The temporary 16 kHz audio is deleted right after transcription. Stale work files are
  removed after 24 hours. Rejected renders are removed after `DELETE_REJECTED_RENDERS_AFTER_DAYS`; their
  database records are kept.
- **Originals are never deleted** unless you set `DELETE_ORIGINALS_AFTER_DAYS` > 0. Even then, a recording is
  kept while any of its clips is awaiting review. Manual deletion requires typing the recording title.
- **Logs.** Structured JSON logs go to `data/logs/clipengine.jsonl`. Each job's log is viewable in the UI.

Data layout:

- `data/clipengine.sqlite3`
- `data/recordings/<date>_<title>/source.mp4` and `provenance.json`
- `data/clips/recNNNNN/clipNNNNN_rN/`: `clip.mp4`, `thumb.jpg`, `captions.srt`, `captions.ass`,
  `transcript.txt`, `description.txt`
- `data/models/`
- `data/secrets/`

## 8. Tests

```
python -m pytest -v                                  # unit + integration (offline)
CLIPENGINE_REAL_WHISPER=1 python -m pytest -v        # also runs the real Whisper transcription test
python scripts/acceptance_test.py                    # full end-to-end against the running app
```

`scripts/make_sample_video.py` generates a lawful 90-second test recording. The speech is original text written
for this project, synthesised with FFmpeg's built-in flite voice.

The acceptance test starts the real server and runs the whole workflow: upload, transcribe, select, render a
1080×1920 clip, preview it in a mobile browser, approve and reject, download, restart and confirm the records
persist.

Integration tests marked *stub* use a **test-only** transcript fixture so they can run offline. They never
stand in for transcription verification. Publishing tests use **mocked HTTP** and verify request construction
only. The GitHub Actions workflow (`.github/workflows/clipping-engine.yml`) runs the suite and the acceptance
test with **real faster-whisper** on Ubuntu and Windows, plus the Windows launcher check.

## 9. Troubleshooting

| Symptom | Fix |
|---|---|
| "ffmpeg not found" | Run `winget install Gyan.FFmpeg`, or put `ffmpeg.exe` and `ffprobe.exe` in `tools\ffmpeg\bin`, then relaunch. |
| First transcription seems stuck at "Loading model" | The model is downloading once from Hugging Face. Check your connection; the job retries. |
| Very slow transcription | Set a smaller `WHISPER_MODEL` (for example `base`), set `TRANSCRIBE_LANGUAGE=en`, or enable the GPU (`INSTALL_CUDA_LIBS=1`). |
| Phone cannot connect | Check `CLIPENGINE_HOST=0.0.0.0`, that the password is set, that the firewall allows *Private networks*, and that both devices are on the same network. |
| Video preview will not play on iPhone | Use Safari or Chrome. Range requests are supported, and clips are H.264 high profile with faststart. |
