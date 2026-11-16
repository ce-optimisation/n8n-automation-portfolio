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
| Selection | Content-led scorer (profile v4): clip length follows natural topic boundaries, and a clip must begin where a thought begins; optional local Ollama re-ranking among real candidates only |
| Captions | Approved minimalist style: bundled **Anton** font, sentence case, white with a black outline, compact, lower-middle |
| Automation | Authorised source monitoring (feeds, live HLS streams, permitted channels) → automatic import, clipping and an email when clips are ready |
| Publishing | Official TikTok Content Posting API (upload to inbox) and Instagram Graph API (Reels), run as resumable background jobs |

There is no Docker, Redis, Postgres, cloud service, paid API or subscription.

---

## 1. Install and launch (Windows)

**Easiest: the one-click installer.**

1. Open <https://github.com/ce-optimisation/n8n-automation-portfolio/tree/claude/optimistic-noether-k52qjf/clipping-engine>,
   click **`Install-Clipping-Engine.cmd`**, then the **Download raw file** button (top right of the file view).
2. Double-click the downloaded file. If Windows SmartScreen appears, choose **More info → Run anyway**.
   No administrator rights are needed. The installer:
   - downloads the project and copies it to **`%LOCALAPPDATA%\ClippingEngine`** (a local folder, not OneDrive);
   - creates a **Clipping Engine** shortcut on the Desktop and in the Start menu;
   - starts the launcher with the setup assistant (below).
3. Running the installer again later **updates** the engine. Your `.env`, `data\` (recordings, clips, database),
   `.venv\` and `tools\` are never overwritten.

You can instead download the branch ZIP, extract it anywhere and double-click
`clipping-engine\Install-Clipping-Engine.cmd` (installs from the extracted copy) or
`clipping-engine\Start-Clipping-Engine.cmd` (runs in place).

**What the launcher (`Start-Clipping-Engine.cmd`) does every time:**

- finds Python 3.10–3.13. If none is found it offers to install Python 3.12 for your user account (winget, or
  the official python.org installer when winget is unavailable);
- creates a private `.venv` and installs the packages (first run, and again only when `requirements.txt` changes);
- finds FFmpeg in `tools\ffmpeg\bin`, on PATH or in the WinGet links folder. If it is missing it offers to
  install it (winget, falling back to a portable download into `tools\ffmpeg` — `scripts\get_ffmpeg.py`);
- creates `.env` from `.env.example`;
- **on the first start, runs the setup assistant**, then starts the engine under a crash supervisor and opens
  **http://127.0.0.1:8765**.

**The setup assistant** (`Start-Clipping-Engine.cmd --setup` to run it again) is safe to close and re-run; it
detects what is already done:

1. reports this computer's CPU, RAM and GPU and the speech model chosen for it;
2. downloads that speech model (once);
3. **proves the whole workflow on this PC**: a bundled 32-second NASA briefing excerpt (genuine speech) is
   transcribed with the real model, clipped and rendered with the approved captions; the result is saved to
   `data\selftest\selftest_clip.mp4`;
4. optional — phone access through Tailscale (§9);
5. optional — the address for email notifications (§7b);
6. optional — start at Windows sign-in and keep-awake while monitoring (§8).

**Diagnostics:** `Start-Clipping-Engine.cmd --doctor` prints every check with its exact fix;
add `--selftest` to repeat the real end-to-end run.

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

## 3b. Automatic source monitoring (Sources page)

The target flow is: a new recording becomes available, then it is imported, transcribed and clipped, it lands in
Ready to Review, and an email tells you it is there. This works only for sources you are **authorised** to retrieve
from automatically.

| Source type | What it watches | What happens |
|---|---|---|
| **Feed** | RSS/Atom feed whose items carry videos (councils, parliaments, podcasts) | Each new item is downloaded (resumable), imported and clipped |
| **Live stream (.m3u8)** | A direct HLS live stream | When it goes live, it is recorded. See below. |
| **Channel/page** | A channel listed by yt-dlp (permitted sites only) | New uploads are downloaded; live broadcasts are recorded |

**Live recordings:**

- The stream is recorded in crash-safe segments, and the recorder restarts automatically if it drops while the
  broadcast is still live.
- When the broadcast ends, the segments are joined and integrity-checked (streams, duration, and a decode test of
  the final 10 s).
- The result goes into the inbox with a provenance sidecar file, so the normal pipeline takes over.

**Safeguards:**

- On first check, existing items are recorded as a baseline and **not** downloaded, unless you set a backfill
  count.
- Each broadcast or item is recorded once, identified by item IDs and segment fingerprints.
- After a restart, recordings resume, or finalise if the broadcast has ended.

**Authorisation:** a source is enabled only if both of these hold:

- its site is in `ALLOWED_URL_DOMAINS`, or a written permission is recorded in Settings → Source permissions.
- the site resolves to a public address.

Each source also stores your note on who authorises the retrieval. Nothing logs in, uses cookies, or bypasses
paywalls, DRM or rate limits.

**Status of the sources you asked about:**

- **Rumble (America First / Nicholas J. Fuentes):**
  - Rumble's Terms of Use prohibit automated access without Rumble's *prior written permission*. A speaker
    inviting people to clip his show doesn't override the hosting platform's terms.
  - So a Rumble channel source is saved **disabled**, with this explanation, until you record Rumble's written
    permission.
  - Meanwhile, paste individual episode links on the Import page. They are saved as pending sources; attach the
    file you obtained from the rights holder, and it is processed automatically.
  - The exact Rumble channel address could not be verified from the build environment, because rumble.com isn't
    reachable there. Copy it from an episode page's channel link.
- **America First Plus (americafirst.plus):** couldn't be reached from the build environment either. If it is a
  subscription or login service, automated retrieval would bypass a paywall and is not supported. Use files you
  are authorised to download.

**Other recorders:** I evaluated [Multi-Stream-Recorder](https://github.com/ManletPride/Multi-Stream-Recorder):

- MIT licence; Python/Tkinter desktop app, Windows-first.
- Polls channels, records via yt-dlp/streamlink, and writes MP4 plus a JSON sidecar.
- It uses the same underlying tools as the built-in monitor, but it is a separate GUI with its own environment, and
  it doesn't enforce these permission checks. **The built-in monitor is the default.**
- If you prefer Multi-Stream-Recorder, point its output folder at the inbox: MP4s are imported automatically and
  its JSON sidecar is read for provenance. That path is untested against its real output.

Rekordly (rekordly.in) couldn't be reached or found during evaluation, so it is not recommended.

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
- **Measured with real Whisper** (`base.en`, CPU int8, GitHub-hosted runner with 4 vCPU and 16 GB) on a
  **114-minute** recording, with the worker **hard-killed** after the first checkpoint and then restarted:
  - The restarted run resumed at chunk 2 of 6 and reused the extracted audio. Nothing was lost or repeated.
  - Transcription ran at about **18× real time**, so roughly 3–4 minutes per recorded hour on that machine.
    Expect a similar order on a modern 4–8 core PC with `base`; `small` is about 3× slower.
  - Peak memory was **1.6 GB**, including the model. Peak temporary disk use was 0.22 GB, and all of it was
    cleaned up afterwards.
  - Selection took 3.2 s. Six clips rendered in 8–13 s each, and no jobs failed.
  - 97.7% of the spoken vocabulary appeared in the transcript.

## 5. How excerpts are chosen (profile v4, content-led with context boundaries)

There is **no preferred clip length.** A clip runs from a natural beginning to a natural end, which might be 15 s,
40 s or two minutes. `CLIP_MIN_SECONDS` (15) and `CLIP_MAX_SECONDS` (150) are only safety limits.

1. Whisper word timings are grouped into sentences, splitting on punctuation and on pauses over 1.6 s.
2. **Boundary detection** (TextTiling-style, adapted to each recording): every gap between sentences gets a
   strength score. It combines how much the vocabulary changes across the gap with the pause length, both ranked
   against that recording's own distribution. A gap followed by "And…" or "But…", or one that separates a
   question from its answer, counts as weak.
3. Every window of whole sentences ending on a full stop is scored. It gains points for:
   - substance: claims, figures and reasoning
   - questions *with* their answers
   - clarity
   - opening at the start of a topic
   - starting and ending at **strong boundaries**

   It loses points for:
   - cutting while the speaker's point continues
   - ending on an unanswered question
   - spanning a change of topic
   - starting mid-thought
   - filler and procedural talk
4. Clips are chosen greedily: no overlaps, a gap of at least 5 s, and limited repetition. **Quality comes before
   quantity:** weak passages are never used to reach a count. If only two passages qualify, you get two, and the
   recording page explains why.
5. Hateful or extremist advocacy is **flagged for contextual review**, never turned into promotional text (§6).

**Reproducible evaluation** (`scripts/make_longform_sample.py` + `scripts/evaluate_selection.py`):

| Sample | Profile | Substantive | Procedural | Ends mid-topic | Starts mid-topic | Topic diversity | Durations (s) |
|---|---|---|---|---|---|---|---|
| 58 min | v1 (55 s target) | 0.81 | 0.115 | 0.33 | 0.17 | 0.83 | 51–59 |
| 58 min | v2 (55 s target) | 0.94 | 0.00 | 0.17 | 0.00 | 0.83 | 45–58 |
| 58 min | **v3 (content-led)** | **0.94** | **0.00** | **0.00** | 0.33 | **1.0** | 33–55 |
| 9 min | v1 | 0.77 | 0.15 | 0.33 | 0.50 | 0.83 | 31–60 |
| 9 min | v2 | 0.92 | 0.02 | 0.00 | 0.33 | 1.0 | 32–58 |
| 9 min | **v3** | **0.92** | 0.02 | **0.00** | 0.50 | **1.0** | 20–113 |

These use the TEST-ONLY exact-timing transcript of a template-generated recording. In it, pauses don't vary with
topic changes, which handicaps v3's boundary signal. Compare the profiles rather than the absolute numbers. The
real-Whisper long-form CI job reports the same metrics on a genuine transcript.

**Measured:** v3 removes the duration target and never cut a topic's discussion short. It sometimes starts one
paragraph into a topic. `SELECTION_PROFILE=v2` restores the previous behaviour.

### Context boundaries (profile v4, the default)

Profile v3 measured breaks only from vocabulary change and pauses. On genuine speech that was not enough: tested
on a real 55-minute NASA/USGS briefing, v3 began a third of its clips in the middle of a sentence, ran half of them
across several topics, and ended some on a hand-over ("I'd like to introduce…"). Profile v4 adds discourse cues that
hold across formats (talks, panels, interviews, monologues):

- a clip **never starts mid-sentence**;
- a sentence that leans on earlier material is a poor first line and is scored down: "And/But/Because…", "also /
  another / finally / for example…", "as I said earlier", "that question", a reply ("Yes…", "Thanks for the
  question"), a short follow-up question ("Why does this matter?"), or an answer whose question would be cut off;
- a **question stays with its answer**; a change of speaker, a hand-over, an announced new subject or a long
  silence is a strong break that a clip should not straddle;
- a clip does not open or close on greetings, thanks, hand-overs, housekeeping or stray fragments, and does not
  end by announcing the next point;
- substance is measured per unit of time, so length alone earns nothing;
- after selection, each clip is checked against the one or two paragraphs around it: if a clean opening of the
  same point lies just before the chosen start, the start moves back to it; if the following sentences are still
  about what the clip is about (and no speaker change, hand-over or long silence intervenes), the end is extended
  to the close of the point — only when the enlarged clip still fits the limits and scores about as well. Clips
  are never padded.

Durations stay content-led (15 s minimum, 150 s maximum, no target). `SELECTION_PROFILE=v3` restores the previous
behaviour. Measured results, 6 clips per recording (each clip is 0.17 of a row, so one-step differences are noise):

| Transcript | Profile | Starts at a paragraph opening ↑ | Starts mid-sentence ↓ | Spans >1 topic ↓ | Housekeeping at an edge ↓ | Starts mid-topic ↓ | Ends mid-topic ↓ |
|---|---|---|---|---|---|---|---|
| **Real speech** — NASA briefing 55 min, Whisper base.en | v3 | 0.67 | 0.17 | 0.50 | 0.0 | 0.0 ¹ | 0.33 |
| | **v4** | **1.0** | **0.0** | 0.33 | 0.0 | 0.33 | **0.17** |
| **Real speech** — same recording, Whisper small.en | v3 | 0.50 | 0.33 | 0.33 | 0.0 | 0.0 ¹ | 0.33 |
| | **v4** | **0.83** | **0.0** | **0.17** | 0.0 | 0.33 | 0.50 |
| Synthetic 58 min, **real Whisper** (the session-3 benchmark) | v3 | 1.0 | 0.0 | 0.0 | 0.0 | 0.33 | 0.17 |
| | **v4** | 1.0 | 0.0 | 0.0 | 0.0 | **0.17** | 0.50 |
| Synthetic 58 min, exact timings | v3 | 1.0 | 0.0 | 0.0 | 0.0 | 0.33 | 0.0 |
| | **v4** | 1.0 | 0.0 | 0.0 | 0.0 | **0.17** | 0.0 |
| Synthetic 58 min, Whisper-like (TEST-ONLY perturbation) | v3 | 1.0 | 0.0 | 0.17 | 0.17 | 0.50 | 0.0 |
| | **v4** | 1.0 | 0.0 | **0.0** | **0.0** | **0.17** | 0.17 |
| **Mean of seven sets** (incl. two 9-minute sets) | v3 | 0.88 | 0.07 | 0.19 | 0.07 | 0.29 | 0.14 |
| | **v4** | **0.95** | **0.0** | **0.07** | **0.02** | **0.21** | 0.24 |

¹ v3's "0.0 starts mid-topic" on real speech is not a strength: those clips began mid-sentence or ran across whole
topics.

**What improved:** clips no longer start mid-sentence, far fewer run across several topics or open/close on
housekeeping, and starts part-way into a topic fell from 0.29 to 0.21 on average (0.33 → 0.17 on the session-3
benchmark). **What did not:** *endings.* v4 more often stops while the same discussion continues (0.14 → 0.24 on
average; 0.17 → 0.50 on the session-3 benchmark). Reading those clips, they end at the end of a complete paragraph
that is followed by another paragraph on the same subject — a complete sub-point rather than a cut-off sentence —
but by the strict label it is still "mid-topic", and a parameter sweep could not improve it without making other
measures worse. Question-and-answer exchanges longer than 150 s also cannot be kept whole. v4 is the default
because on genuine speech its clips are visibly more coherent; if you prefer clips that run to the end of a topic,
set `SELECTION_PROFILE=v3`. Reproduce:
`python scripts/evaluate_selection.py tests/fixtures/landsat9_topics.json --transcript tests/fixtures/landsat9_whisper_base_en.json.gz`.

## 5b. Captions (approved style)

**Approved default style:**

- **Anton** (official Google Fonts build, SIL OFL 1.1, bundled in `clipengine/assets/fonts`)
- normal sentence case, never forced to uppercase
- solid white with a modest solid black outline; no shadow, background, colour effects or animation
- 73 px on a 1080×1920 frame (enlarged 40% at the owner's request, Nov 2026); no burned-in source label by default
- centred, with the caption block at 72% of the height (lower-middle, clear of the TikTok/Instagram overlays)
- at most two lines, wrapped at word boundaries; any line wider than the 120 px side margins (real glyph widths) is shrunk for that caption only

**Verification:**

- The app renders a test frame at start-up and confirms that libass loaded the bundled Anton file and selected
  `Anton-Regular`.
- If a different font was substituted, the Setup checklist says so, by name.

Settings → Captions lets you change the size, outline thickness, vertical position, preset (Anton or the system's
classic sans), burn-in on/off, an optional subtle dark backing (off by default) and the small source label. A live
preview is shown.

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

## 6b. Ready to Review and Ready to Post

**Ready to Review** lists every new clip, each with:

- its preview and thumbnail
- source and timestamps
- duration
- direct transcript, with context before and after
- suggested neutral description
- content flags and rights status

From there you can approve, reject, adjust timestamps, regenerate, edit the description or download the MP4.

**Ready to Post** lists approved clips that have a recorded rights basis and no unresolved content flag, each with:

- one-click MP4 download and Copy description
- source information
- an **export check** against the documented TikTok and Instagram limits
- **manual posted/not-posted tracking** for each platform (an optional link and a note)

Neither page requires TikTok or Instagram to be connected.

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

## 7b. Email notifications

When all of a recording's clips are ready, **one** email lists them: the title, the original recording date, the
number of clips, and for each clip a small thumbnail, its duration, the opening words as a direct quote, and a link
into your private dashboard (sign-in required). No MP4s are attached and no public media links are created.
Optional alerts cover failed processing and low disk space.

**Nothing is sent until you approve it.** Notifications are prepared and *held* until you open **Settings →
Notifications**, read a real sample, type the recipient and approve that wording. Changing the recipient, or a new
version of the wording, cancels the approval. Each notification has a duplicate-proof key; sending is a retrying,
restart-safe job.

Everything is set up on the **Settings → Notifications** page (no file editing):

1. **Where to send** — your address and the dashboard address used in links (filled in automatically by Phone
   access, §9).
2. **Connect a sending account** — one of two supported ways:
   - **Option A (about 3 minutes): send from a Gmail account** with a Google *app password*
     (<https://myaccount.google.com/apppasswords>). The email still arrives in your Outlook inbox. The password is
     kept in Windows Credential Manager, not in `.env`.
   - **Option B: send from your Outlook.com account** through Microsoft Graph with a one-time device-code sign-in.
     Microsoft requires an *Application (client) ID* for this. A personal Outlook.com account can only create one
     after it has an Azure directory (<https://azure.microsoft.com/free>; Microsoft asks for a payment card to
     verify identity; app registrations themselves are free). Then: entra.microsoft.com → App registrations → New
     registration → **Personal Microsoft accounts only** → Authentication → **Allow public client flows: Yes** →
     copy the Application (client) ID into the page → **Connect Outlook**.

   Why two options: since 2024 Microsoft no longer lets an Outlook.com account register an application without a
   directory, and Outlook.com no longer accepts passwords or app passwords for SMTP, so "just enter your Outlook
   password" is not possible with any tool.
3. **Read the sample and approve it.**
4. **Optional test** (type `SEND`). "Sent" means the mail service accepted the message; check the inbox to
   confirm delivery.

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
- **Crash recovery:** the launcher runs the engine under a supervisor, which restarts it automatically after a
  crash (with back-off). Jobs, transcription chunks, downloads, live recordings and uploads all resume.
- **Start at sign-in (optional):** use Settings → *Start automatically when I sign in*, or run
  `Enable-Autostart.cmd` (undo with `Disable-Autostart.cmd`). This adds a per-user Startup entry that launches the
  engine minimised in the background. It needs no administrator rights or Task Scheduler. The PC must be on for
  monitoring and processing to happen.
- Disk space is checked hourly. Warnings appear on the dashboard, and in a daily email if approved.
- **Keep-awake:** Windows suspends every program when the PC sleeps. While a recording or a processing job is
  running the engine asks Windows not to sleep (a per-process request; no power setting is changed and the screen
  can still turn off). Settings → *Running unattended* can extend this to "whenever a source is being monitored".
  It cannot wake a sleeping PC, and a closed laptop lid still sleeps the machine unless you change that Windows
  setting yourself. **Nothing runs while the PC is off.**
- **The dashboard says what needs attention:** periods when the engine was not running (and that live broadcasts
  in them were not recorded), monitoring switched off, sources lacking permission or failing, failed recordings or
  jobs, expired email sign-in, and low disk space.
- **Live recording restarts:** if the recorder is interrupted mid-broadcast it restarts three media segments
  back, and the repeated footage is removed exactly when the recording is joined (verified frame-for-frame).

## 9. Phone access (private, through Tailscale)

The PC does all recording and processing; the phone is a window onto its dashboard: status, clip previews,
approve/reject, MP4 downloads and Ready to Post.

1. Install **Tailscale** on the PC (<https://tailscale.com/download/windows>, or let the setup assistant run
   `winget install Tailscale.Tailscale`) and sign in (free personal plan).
2. In the dashboard open **Settings → Phone access → Set up phone access**. The engine then:
   - creates a strong dashboard password if there is none (shown once; also saved in `.env`);
   - runs `tailscale serve --bg 8765`, which publishes the dashboard **only inside your tailnet** over HTTPS;
   - reads back this PC's real address (`https://<pc-name>.<tailnet>.ts.net`) from Tailscale, checks that it
     answers, and saves it as `DASHBOARD_URL` so email links point to it.
   If your tailnet has HTTPS switched off, the page shows Tailscale's own link to enable it; press the button again
   afterwards.
3. On the iPhone: install Tailscale from the App Store, sign in with the **same account**, leave it switched on,
   open the address in Safari and sign in with the dashboard password. *Share → Add to Home Screen* gives an
   app-like icon.

The engine stays bound to 127.0.0.1. Nothing is port-forwarded, and Tailscale *Funnel* (public exposure) is never
enabled — if it is found switched on, the page says so and refuses to continue. Without a password the dashboard
only answers on the PC itself. Sign-in attempts are rate-limited; cookies are HttpOnly, SameSite=strict and Secure
over HTTPS; cross-origin POSTs are refused. Never port-forward the dashboard.

## 9b. Older recordings and historical backfill

- **A file you already have:** Import → Upload → *Older recording? Keep its history* — enter the original source
  address and date. The address is stored as provenance only (nothing is downloaded from it), and the same file or
  the same address is never imported twice.
- **Many or large files:** copy them into the inbox folder. A date in the file name (`2024-05-03` or `20240503`)
  becomes the original recording date; for full history add `<video name>.json` containing
  `{"source_url": "https://…", "recorded_at": "2024-05-03", "title": "…"}`.
- **A restricted platform's episode** (e.g. Rumble): paste the episode link on the Import page. It is saved as a
  *pending source*; attach the file supplied by the rights holder. The link is kept as provenance.
- **An authorised feed or channel:** the first check only *notes* what is already published (the baseline). Use
  **Import older items** on the Sources page (or "Import existing items on first check" when adding the source) to
  bring in earlier recordings. Every item is imported at most once.
- Recordings are labelled **live capture** (recorded by the engine as broadcast), **archive** (an older recording
  brought in later) or **recent**, with both the original date and the import date. Correct either on the
  recording's page.

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
