# Owner setup guide — Session 10 (do these in order; each step is on your Lenovo)

Nothing below needs you to paste a secret into chat. Secrets go only into the hidden prompts of `Set-Credentials.cmd`.

## 0. Update the installed engine
1. Run `Install-Clipping-Engine.cmd` from the updated folder (it backs up the database and keeps `.env`, media and credentials).
2. Start the engine. The first start migrates the database to **schema v8** (a `before-schema-8` backup is taken automatically).
3. Open http://127.0.0.1:8765 and confirm Settings shows `SELECTION_PROFILE=v6`.

## 1. Storage (do this first if the disk is nearly full)
1. Double-click **`Storage-Audit.cmd`**. It is read-only and prints where the space is (raw recordings by deletion-gate outcome,
   finished clips, scratch, venv, orphans, duplicates). A copy is saved to `data\logs\storage-audit-*.json`.
2. The audit's **B** section names the gate that blocks each raw recording (e.g. `engine_owned` = you supplied it, never auto-deleted;
   `cloud_verified` = not yet verified in Drive; `rendered` = clips still pending/failed).
3. Storage page: decide on **Automatic local cleanup** (after platform handoffs) and the optional **Drive-only archive**
   (reviewed + Drive-verified clips only; run the restore test first). Both are OFF until you switch them on.
4. Nothing outside the engine's folders is ever touched (not OneDrive, not Documents).

## 2. Monitoring credentials (YouTube, Twitch, Kick)
Double-click **`Set-Credentials.cmd`** and follow the prompts after creating each credential below.

**YouTube (monitoring key — not the upload authorisation)**
1. https://console.cloud.google.com/ → create/select a project.
2. https://console.cloud.google.com/apis/library/youtube.googleapis.com → **Enable** *YouTube Data API v3*.
3. https://console.cloud.google.com/apis/credentials → **Create credentials → API key**.
4. Edit the key → *API restrictions* → **Restrict key → YouTube Data API v3**. (Application restrictions: the engine calls from your
   home IP, so leave *None* unless your IP is fixed; the API restriction is the important one.)
5. Paste it into `Set-Credentials.cmd` (`YOUTUBE_API_KEY`). Uploading to your channels uses a separate OAuth sign-in later.

**Twitch**
1. https://dev.twitch.tv/console → sign in. Twitch requires two-factor authentication on the account before it lets you register an app.
2. *Register Your Application*: any name; Category *Application Integration*; Client Type *Confidential*; for the OAuth Redirect URL
   enter `http://localhost` (the engine uses the client-credentials flow, which never redirects).
3. *Manage* → copy **Client ID**, *New Secret* → copy the secret into `Set-Credentials.cmd`.

**Kick**
1. https://kick.com/settings/developer (Settings → Developer) → create an app (Kick requires two-factor authentication on the account).
2. Copy the Client ID and Client Secret into `Set-Credentials.cmd`.
3. Kick's public API is for discovery only (channel, livestream status). It does not provide footage; acquisition stays
   permission-gated.
NB: the field names above follow what Twitch/Kick showed when this was written — if a screen differs, follow the screen.

`Set-Credentials.cmd` makes one real request per platform and prints OK/FAILED by HTTP status only. Then run **`Check-Portfolio.cmd`**.

## 3. Hook titles
Open any clip → **Hook title** card: accept & lock, edit, pick a verified alternative, regenerate, or remove. Titles appear in the
video only for brands whose title style is on (America First Monitor is on by default; turn other brands on in Settings).
Accepting or editing re-renders an unapproved clip; approved clips are never silently changed.

## 4. Publishing accounts — 24 (do this last; it blocks nothing above)
Suggested order: create the central Google account → YouTube channels (all six, via *Create a channel* under that account) →
Meta Business portfolio → Facebook Pages (six) → Instagram professional accounts (six, linked to the Pages) → TikTok (six).
Pause for every verification/CAPTCHA/phone check; do not reuse a phone number past what each platform allows. Plus-aliases are
not accepted everywhere — if one is refused, use a separate alias/inbox and record it.

| Brand | YouTube | TikTok | Instagram | Facebook |
|---|---|---|---|---|
| Afterlive | ☐ url: | ☐ url: | ☐ url: | ☐ url: |
| America First Monitor | ☐ | ☐ | ☐ | ☐ |
| Operator Notes | ☐ | ☐ | ☐ | ☐ |
| The Spec Edit | ☐ | ☐ | ☐ | ☐ |
| Dial Study | ☐ | ☐ | ☐ | ☐ |
| Open Floor | ☐ | ☐ | ☐ | ☐ |

Official sign-up entry points: https://www.youtube.com/create_channel · https://business.facebook.com/ ·
https://www.facebook.com/pages/create · https://www.instagram.com/accounts/emailsignup/ · https://www.tiktok.com/signup.
Tick a box only after the platform shows the live profile page; paste its URL. Add each confirmed account on the engine's
Accounts page, mapped to exactly one brand (brands never share an account). Nothing is published automatically.
