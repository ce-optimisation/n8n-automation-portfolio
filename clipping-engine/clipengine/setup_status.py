"""Setup checklist: accurate, actionable status for every part of the installation.

Statuses: Ready, Connected, Untested, Not configured, Owner action required,
Awaiting platform approval, Failed. A mocked integration is never shown as Connected:
'Connected' requires real stored OAuth tokens, and 'Ready' requires a confirmed real
platform transaction (or, for local steps, a real successful run).
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

READY, CONNECTED, UNTESTED = "Ready", "Connected", "Untested"
NOT_CONFIGURED, OWNER, AWAITING, FAILED = "Not configured", "Owner action required", "Awaiting platform approval", "Failed"


def _model_downloaded(settings, model: str) -> bool:
    d = Path(settings.models_dir)
    return any(d.glob(f"models--*faster-whisper-{model}*")) or any(d.glob(f"*{model}*/model.bin"))


def checklist(core) -> list[dict]:
    from . import hardware
    s, db = core.settings, core.db
    items: list[dict] = []

    def add(key, title, status, detail, action=""):
        items.append({"key": key, "title": title, "status": status, "detail": detail, "action": action})

    # 1. Python & FFmpeg
    hw = hardware.detect()
    if hw.ffmpeg and hw.ffprobe:
        add("ffmpeg", "Python and FFmpeg", READY, f"FFmpeg found at {hw.ffmpeg}.")
    else:
        add("ffmpeg", "Python and FFmpeg", FAILED, "FFmpeg/FFprobe not found on PATH.",
            "Close the app, run 'winget install Gyan.FFmpeg' (or place ffmpeg.exe and ffprobe.exe in "
            "tools\\ffmpeg\\bin), then start the launcher again.")
    # 2. Whisper configured
    model, device, ctype = hardware.choose_whisper(hw, s.whisper_model, s.whisper_device, s.whisper_compute_type,
                                                   s.language)
    if s.transcriber_backend == "stub":
        add("whisper", "Whisper speech model", FAILED, "TEST-ONLY stub transcriber is active (CLIPENGINE_TRANSCRIBER=stub).",
            "Remove CLIPENGINE_TRANSCRIBER from .env.")
    elif _model_downloaded(s, model):
        add("whisper", "Whisper speech model", READY, f"{model} on {device} ({ctype}) is downloaded.")
    else:
        add("whisper", "Whisper speech model", UNTESTED, f"{model} on {device} ({ctype}) will download on first use.",
            "Click 'Download model now' (needs internet once), or just import a recording.")
    # 3. Real transcription succeeded
    v = db.get_kv("verified.transcription")
    if v:
        info = json.loads(v)
        add("transcription", "A real transcription has succeeded", READY,
            f"Recording #{info['recording_id']} with {info['model']} on {time.strftime('%Y-%m-%d', time.localtime(info['at']))}.")
    else:
        failed = db.one("SELECT COUNT(*) n FROM jobs WHERE kind='process_recording' AND status='failed'")["n"]
        add("transcription", "A real transcription has succeeded", FAILED if failed else UNTESTED,
            "No recording has been transcribed with Whisper yet." + (f" {failed} analysis job(s) failed." if failed else ""),
            "Import a short authorised recording (Import → Upload) and wait for it to finish.")
    # 4. Source importing
    url_ok = db.one("SELECT COUNT(*) n FROM recordings WHERE source_type IN ('url','linked_file')")["n"]
    up_ok = db.one("SELECT COUNT(*) n FROM recordings WHERE source_type IN ('upload','inbox','linked_file')")["n"]
    add("import", "Source importing", READY if (url_ok and up_ok) else UNTESTED if not up_ok else READY,
        f"{up_ok} file import(s), {url_ok} URL/linked import(s).",
        "" if up_ok else "Upload a recording, or save a video into the inbox folder: " + str(s.inbox))
    # 5–6 TikTok
    tt = core.tiktok.status()
    tt_err = db.get_kv("oauth_error.tiktok")
    if not tt["configured"]:
        add("tiktok_creds", "TikTok developer credentials", OWNER, "TIKTOK_CLIENT_KEY / SECRET / REDIRECT_URI not set.",
            "Register a TikTok developer app (README → TikTok setup) and put the keys in .env.")
    else:
        add("tiktok_creds", "TikTok developer credentials", READY, f"Configured ({core.settings.tiktok_app_type} app).")
    if tt["connected"]:
        add("tiktok_oauth", "TikTok account connected", CONNECTED, f"Scopes: {tt['scope']}.")
    elif tt_err:
        add("tiktok_oauth", "TikTok account connected", AWAITING if "approv" in tt_err.lower() or "scope" in tt_err.lower()
            else FAILED, f"Last sign-in error: {tt_err}", "Check the app's review status and scopes, then reconnect.")
    else:
        add("tiktok_oauth", "TikTok account connected", OWNER if tt["configured"] else NOT_CONFIGURED,
            "Not connected.", "Settings → Connect TikTok." if tt["configured"] else "Configure credentials first.")
    # 7–8 Instagram
    ig = core.instagram.status()
    ig_err = db.get_kv("oauth_error.instagram")
    if not ig["configured"]:
        add("ig_creds", "Instagram developer credentials", OWNER,
            "Missing: " + ", ".join(core.instagram.missing_config()) + f" (mode: {ig['mode']}).",
            "Create a Meta app (README → Instagram setup) and put the values in .env.")
    else:
        add("ig_creds", "Instagram developer credentials", READY, f"Configured (mode: {ig['mode']}).")
    if ig["connected"]:
        add("ig_oauth", "Instagram account connected", CONNECTED,
            f"@{ig['username']}" + (" — token expires within 7 days; reconnect soon." if ig["expiring_soon"] else ""))
    elif ig_err:
        add("ig_oauth", "Instagram account connected", AWAITING if "review" in ig_err.lower() or "permission" in ig_err.lower()
            else FAILED, f"Last sign-in error: {ig_err}", "Check the Meta app's permissions/roles, then reconnect.")
    else:
        add("ig_oauth", "Instagram account connected", OWNER if ig["configured"] else NOT_CONFIGURED, "Not connected.",
            "Settings → Connect Instagram." if ig["configured"] else "Configure credentials first.")
    # 9. Dashboard ready for review
    alive = bool(core.queue._thread and core.queue._thread.is_alive())
    ready_clips = db.one("SELECT COUNT(*) n FROM clips WHERE status IN ('ready','approved')")["n"]
    if not alive:
        add("review", "Dashboard ready for review", FAILED, "Background worker is not running.", "Restart the app.")
    elif ready_clips:
        add("review", "Dashboard ready for review", READY, f"{ready_clips} clip(s) rendered and reviewable.")
    else:
        add("review", "Dashboard ready for review", UNTESTED, "Worker running; no rendered clips yet.",
            "Import a recording to produce the first clips.")
    # Captions (approved Anton style)
    from . import captions
    style = captions.load_style(db)
    fc = json.loads(db.get_kv("caption_font_check") or "null")
    if not style.enabled:
        add("captions", "Captions", UNTESTED, "Burned-in captions are turned off (SRT files are still produced).",
            "Turn captions on in Settings → Captions.")
    elif fc and fc.get("ok"):
        add("captions", "Caption font", READY, f"{fc.get('requested')} verified: libass selected "
            f"{fc.get('postscript')} from the bundled font file.")
    elif fc:
        add("captions", "Caption font", FAILED, f"Requested {fc.get('requested')}, libass used "
            f"{fc.get('substituted_by') or fc.get('selected') or 'nothing'} {fc.get('error', '')}".strip(),
            "Choose the Anton preset (bundled) or install the requested font.")
    else:
        add("captions", "Caption font", UNTESTED, "Font check runs at start-up.", "Reload this page in a moment.")
    # Source monitoring
    srcs = db.query("SELECT * FROM sources")
    live = [x for x in srcs if x["enabled"] and x["last_success"]]
    if live:
        add("sources", "Automatic source monitoring", READY, f"{len(live)} source(s) checked successfully.")
    elif any(x["status"] == "blocked" for x in srcs):
        add("sources", "Automatic source monitoring", OWNER,
            "Configured sources are disabled pending authorisation (see Sources).",
            "Record the platform's written permission, or use an authorised source/feed.")
    elif srcs:
        add("sources", "Automatic source monitoring", UNTESTED, "Sources configured; not yet checked successfully.",
            "Open Sources and click Check now.")
    else:
        add("sources", "Automatic source monitoring", NOT_CONFIGURED, "No sources configured (optional).",
            "Add an authorised feed or live stream in Sources, or keep using upload/inbox.")
    # Email notifications
    from . import notify
    mailer = core.notifier.mailer.status()
    sent = db.one("SELECT COUNT(*) n FROM notifications WHERE status='sent'")["n"]
    if not s.notify_enabled:
        add("email", "Email notifications", NOT_CONFIGURED, "Disabled (NOTIFY_ENABLED=0).")
    elif not s.notify_to or not mailer["configured"]:
        add("email", "Email notifications", OWNER, "Recipient or sending account not configured.",
            "Set NOTIFY_TO and OUTLOOK_CLIENT_ID in .env (README → Email notifications).")
    elif not mailer["connected"]:
        add("email", "Email notifications", OWNER, "Outlook account not connected.",
            "Settings → Notifications → Connect Outlook.")
    elif not notify.is_approved(s, db):
        add("email", "Email notifications", OWNER, "Connected; awaiting your approval of recipient and wording "
            "(notifications are held until then).", "Settings → Notifications → review the sample and approve.")
    elif sent:
        add("email", "Email notifications", READY, f"{sent} email(s) accepted for delivery.")
    else:
        add("email", "Email notifications", CONNECTED, "Approved and connected; no email sent yet.")
    # 10. Platform publishing readiness
    for plat, label, ok_state in (("tiktok", "TikTok upload-to-inbox", "delivered_inbox"),
                                  ("instagram", "Instagram Reels publishing", "published")):
        st = tt if plat == "tiktok" else ig
        last = db.one("SELECT * FROM publish_attempts WHERE platform=? ORDER BY id DESC LIMIT 1", (plat,))
        good = db.one("SELECT COUNT(*) n FROM publish_attempts WHERE platform=? AND status IN ('delivered_inbox','published')",
                      (plat,))["n"]
        if good:
            add(f"{plat}_ready", label, READY, f"{good} confirmed transaction(s) with the real platform API.")
        elif not st["configured"]:
            add(f"{plat}_ready", label, NOT_CONFIGURED, "Credentials not configured.", "See items above.")
        elif not st["connected"]:
            add(f"{plat}_ready", label, OWNER, "Account not connected.", "Connect the account in Settings.")
        elif last and last["status"] == "failed":
            add(f"{plat}_ready", label, FAILED, f"Last attempt failed: {last['detail']}", "Fix the cause and retry.")
        else:
            add(f"{plat}_ready", label, UNTESTED, "Connected, but no confirmed real upload yet.",
                "Approve a clip, record its rights basis, and send it" +
                (" to your TikTok inbox." if plat == "tiktok" else " (this posts publicly)."))
    return items


def platform_label(core, plat: str) -> tuple[str, str]:
    """Account status shown in Settings: never reports a mocked or unverified success.
    Returns (label, explanation)."""
    client = core.tiktok if plat == "tiktok" else core.instagram
    st = client.status()
    err = core.db.get_kv(f"oauth_error.{plat}")
    last = core.db.one("SELECT * FROM publish_attempts WHERE platform=? ORDER BY id DESC LIMIT 1", (plat,))
    confirmed = core.db.one("SELECT COUNT(*) n FROM publish_attempts WHERE platform=? AND status IN "
                            "('delivered_inbox','published')", (plat,))["n"]
    if not st["configured"]:
        return "Not connected", "Developer credentials are not configured (optional; see README)."
    if not st["connected"]:
        if err and any(k in err.lower() for k in ("approv", "review", "scope", "permission", "unaudited")):
            return "Awaiting developer approval", err
        return "Not connected", (f"Last sign-in error: {err}" if err else "Use Connect to sign in.")
    if last and last["status"] == "failed":
        return "Upload failed", last["detail"] or ""
    if confirmed:
        what = "delivery to your TikTok inbox" if plat == "tiktok" else "publication"
        return f"Platform-confirmed {what}", f"{confirmed} confirmed transaction(s) reported by the platform API."
    return "Upload available", "Connected. Approved, rights-cleared clips can be sent (nothing is sent automatically)."
