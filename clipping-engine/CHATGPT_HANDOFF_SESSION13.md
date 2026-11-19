# HANDOFF: Clipping Engine, end of Session 13 autonomous phase (19 Nov 2026)

Read all of this, then reply only to confirm you have absorbed it and wait for James (the owner). Do not run anything.
No secrets are in this file. Evidence labels: **WINDOWS-LIVE** (observed on James's install), **TESTED-DEV** (pytest on Windows against the
dev clone, HTTP mocked), **INSTALLED** (running in the engine now), **NOT DONE**.

## 1. Where everything is
- Repo `ce-optimisation/n8n-automation-portfolio`, app in `clipping-engine/`. Dev branch `claude/sharp-bell-utpc9o`: commits `1dc860c`, `dc221bd`,
  `487a6b3`, `80a81f3` (+ this handoff) on top of `de020c1`. Established branch `claude/optimistic-noether-k52qjf` is **untouched** at `de020c1`. No PR.
- Local dev clone: `C:\Users\James\ClippingEngine-dev\repo`. Installed app: `%LOCALAPPDATA%\ClippingEngine`, **now running commit `80a81f3`** (INSTALLED).
- Install done 19 Nov 12:23 after a verified backup: `data\backups\pre-session13-install-*.sqlite3` (integrity ok), `code-before-session13-*.zip`,
  `env-pre-session13-*.bak` (local only). `.env`, `data\`, `.venv`, tokens were not touched. Engine restarted; dashboard `http://127.0.0.1:8765`.
- Old log scrubbed in place (585 lines had the YouTube API key in a URL): 0 remain. New logs are redacted at write time.

## 2. Non-negotiable rules (unchanged)
Python/local-first, no paid AI APIs; human editorial approval; **never publish automatically**; no test uploads without explicit authorisation;
discovery != recording permission; no DRM/auth/paywall bypass; brand and account isolation; never silently re-render clips; preserve DB, recordings,
backups, credentials, Drive masters; global clip cleanup and Drive-only archive stay OFF; `RAW_DELETE_AFTER_CLOUD` stays ON (owner decision 19 Nov);
no force-push, no PR or promotion to the established branch without approval; back up the DB and check jobs before any install/restart;
secrets never in chat, logs or commits; Windows results kept separate from mocked/CI results.

## 3. What Session 13 did
**Verified on Windows (WINDOWS-LIVE)**
- Installed code was identical to `de020c1`. YouTube, Twitch, Kick authenticate (HTTP 200). 24 of 27 sources VERIFIED; acquisition PERMITTED for one source (Rumble, Nick Fuentes).
- First real proof of Session 11 live-capture safeguards: a 95-minute Rumble capture rotated into 6 segments (~1 GB each), joined to one 4.57 GB file, segments removed,
  imported as recording 6, 4 clips rendered and verified on Drive; the engine then deleted the raw automatically after all gates passed (free disk 6.2 -> 10.4 GB).
  Not yet proven: the `awaiting_space` resume path under genuine disk exhaustion.
- Creator-name `?` was a console display artifact; the database holds a proper em dash.
- Disk investigation: the engine's own growth was the capture; no other large recent file found; the ~10 GB loss of 18 Nov is **unattributed**. Disapproved clips (9, 10, 12) hold only 6 MB locally
  (masters verified on Drive) and were **not** deleted; recordings 1-5 raws were already gone. Nothing was deleted this session.
- Root cause of "Amazon AWS went" and the stale `brand_policy: default` found and fixed. The YouTube key was in the logs (fixed).

**Code (TESTED-DEV and now INSTALLED)**
- `hooks.py`: topic labels are noun phrases only; no dependent-clause or dangling-verb claims; no subject-less or context-free questions; no profanity/slurs (any brand); no repeated-word stumbles,
  contractions or conversational filler in topics; first-person lines never become political-brand titles; negated/hedged sources carry a reviewer warning. Unusable chatter -> "enter a title manually".
  Real recording-6 wording was used for regression tests (it produced "Fuentes on fucking bitch" before the fix).
- `pipeline.py`: a creator with no brand takes the brand from the niche instead of the permissive default policy.
- `safety.py`: new narrow `group_targeting_review` category (human review only) + evasion normalisation. The three owner-rejected clips (recording 2: 9, 10, 12) had all been stored as "clear". 9 and 10 wording is now flagged; **12 is not caught**.
- `titles.py` (America First Monitor only, owner-approved, default ON for AFM; `meta title_afm_v2 = 0` switches it off): scale 0.60 (existing shrink loop handles long titles), centre at 18% of frame height, **one whole yellow phrase at one size**, shadow 35%, block >= 60 px clear of the picture. Other brands are byte-identical (golden generated from the old committed code). Mockups: `data\reports\session13\title_afm_v2_mockups.png` (disposable, no clip touched). **No existing clip was re-rendered.**
- `semantic.py`: optional local reranker, `TITLE_SEMANTIC=1`, **off**; can only reorder candidates the verifier already accepted. TF-IDF embedder by default; optional `fastembed` (not installed; nothing downloaded).
- `connections.py` + `publishing/facebook.py|youtube.py|delivery.py` + Connections dashboard page (`/connections`): ten tracked states per destination; exact-id identity verification; shared admin login can never verify one brand as another; per-brand tokens (`facebook:<brand>`, `youtube:<brand>`);
  per-destination `publishing_enabled` OFF by default; delivery needs verified identity + brand match + approved/rights-cleared clip + typed `DELIVER`. Facebook = Reels `video_state=DRAFT`; YouTube = **private** upload (`privacyStatus` always set); Instagram = refused (no draft API; `INSTAGRAM_PUBLIC_PUBLISH` off); TikTok = existing inbox path.
- Legacy Instagram connector now refuses to guess among several linked accounts. Log redaction (`logsafe.py`). Schema v9 columns on `social_accounts`.
- The 19 owner-recorded accounts are written to the live DB as **evidence only** (`--record-accounts`): 19 recorded, **0 verified, 0 delivery-enabled**. CLI: `--connections`.
- Tests: wider regression run 171 passed / 1 skipped; hooks+semantic+AFM+safety 69 passed / 10 skipped (render tests); connectors 8, connections 9, connections web 4, publishing 16, logsafe 4. All platform HTTP mocked.

## 4. Destination matrix (19 accounts)
| Brand | YouTube | Facebook Page | Instagram | TikTok |
|---|---|---|---|---|
| Afterlive | recorded | recorded | recorded | recorded |
| America First Monitor | recorded | recorded | recorded | not created |
| Operator Notes | recorded | recorded | recorded | not created |
| The Spec Edit | recorded | recorded | recorded | not created |
| Dial Study | recorded | recorded | recorded | not created |
| Open Floor | recorded | recorded | recorded | not created |
No OAuth, identity verification, scope grant, draft delivery or publishing exists for any of them. Blockers (`.env`, names only): **YouTube** - Google OAuth client present (Drive) but needs the YouTube Data API enabled and the youtube.readonly/upload scopes on its consent screen;
**Facebook/Instagram** - `META_APP_ID`/`META_APP_SECRET`/`META_REDIRECT_URI` empty (no Meta developer app); **TikTok** - `TIKTOK_CLIENT_KEY`/`SECRET` empty, app audit unknown. Do not create the five missing TikTok accounts.

## 5. Open items
**Claude can do (autonomous)**: re-check source states after the next polls; regenerate titles for recording 6's unreviewed clips 31-34 **only if James asks** (the installed generator now returns manual-needed for 32 and 34); consider a local generative title model comparison (size/CPU) before any download;
tune the safety screen for clip 12; extend the Connections page with per-clip delivery buttons; run the full test suite once on the installed copy; keep this handoff current.
**Needs James**: sign in and authorise (Facebook Pages first, then YouTube per channel, Instagram via the same Facebook login, TikTok Afterlive); create the Meta developer app and enable the YouTube Data API scopes (browser-guided; passwords, 2FA, consent are his);
press **Confirm official** for N3on (id 1269875) on the Portfolio page; record platform and creator **permissions/rights** (Settings > Source permissions) before any recording or download; Rumble rights for recording 5; Nick Fuentes's creator authorisation (his Rumble source is PERMITTED while his creator record is `needs_authorisation`, and the engine has recorded two of his streams);
whether recording 6 (a profane GTA gameplay stream from his channel) belongs under America First Monitor at all; which of clips 25-30 may be re-rendered with the new title style; any real draft-delivery test (one named clip, one named account); the ~10 GB unexplained disk loss.

## 6. First reply
Confirm you have absorbed this, restate the two piles in one line each, and wait for James.
