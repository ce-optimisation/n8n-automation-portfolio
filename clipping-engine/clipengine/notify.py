"""Email notifications: one consolidated email per completed recording (plus optional failure alerts).

Safety model
------------
* **Nothing is ever sent until the owner approves** the recipient AND the template version in
  Settings → Notifications. Before that, notifications are created with status `held` and can be
  previewed in the dashboard. Changing NOTIFY_TO or the template version revokes the approval.
* Duplicate protection: every notification has a unique dedupe key (e.g. `ready:<recording id>`).
* Sending runs as a persistent `send_notification` job (retries transient errors; restart-safe).
* No email is created for a recording whose processing failed before clips were ready.
* Links point to the private dashboard (DASHBOARD_URL), which requires sign-in. No MP4s are
  attached and no unauthenticated media links are created. Small thumbnails are embedded inline.

Transports
----------
* `graph` (default): Microsoft Graph `POST /me/sendMail` with delegated `Mail.Send`, signed in once
  with the OAuth device-code flow (works with personal Outlook.com accounts; tenant `consumers`).
  Tokens are kept in the OS credential store (TokenStore). Graph answers 202 = accepted for delivery.
* `smtp`: any SMTP server that allows authenticated submission (credentials in the local .env).
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import logging
import subprocess
import time
from email.message import EmailMessage
from pathlib import Path

import httpx

from . import media, render
from .jobs import JobContext, PermanentError, TransientError

log = logging.getLogger("clipengine.notify")

TEMPLATE_VERSION = "1"
GRAPH = "https://graph.microsoft.com/v1.0"
SCOPES = "offline_access Mail.Send User.Read"


class NotifyNotConfigured(Exception):
    pass


# ---- approval -----------------------------------------------------------------------

def approval(db) -> dict | None:
    raw = db.get_kv("notify.approval")
    return json.loads(raw) if raw else None


def is_approved(settings, db) -> bool:
    a = approval(db)
    return bool(a and settings.notify_to and a.get("to", "").lower() == settings.notify_to.lower()
                and a.get("template_version") == TEMPLATE_VERSION)


def approve(settings, db) -> None:
    if not settings.notify_to:
        raise NotifyNotConfigured("Set NOTIFY_TO in .env first.")
    db.set_kv("notify.approval", json.dumps({"to": settings.notify_to, "template_version": TEMPLATE_VERSION,
                                             "transport": settings.notify_transport, "approved_at": time.time()}))


def revoke(db) -> None:
    db.execute("DELETE FROM meta WHERE key='notify.approval'")


# ---- content ------------------------------------------------------------------------

def _link(settings, path: str) -> str:
    return (settings.dashboard_url or settings.base_url()) + path


def _short(text: str | None, n: int = 160) -> str:
    t = " ".join((text or "").split())
    return t if len(t) <= n else t[:n - 1].rstrip() + "…"


def build_ready(settings, db, rid: int) -> dict | None:
    """Subject/HTML/text + inline thumbnails for a recording's reviewable clips. None if no clips."""
    rec = db.recording(rid)
    clips = db.query("SELECT * FROM clips WHERE recording_id=? AND status IN ('ready','approved') ORDER BY start", (rid,))
    if not rec or not clips:
        return None
    date = time.strftime("%d %b %Y", time.localtime(rec["created_at"]))
    n = len(clips)
    subject = f"[Clip review] {n} clip{'s' if n != 1 else ''} ready — {_short(rec['title'], 70)} ({date})"
    rows_h, rows_t, images = [], [], []
    for c in clips:
        dur = c["end"] - c["start"]
        cid = f"clip{c['id']}@clipengine"
        flag = (" <b style='color:#b42318'>⚑ flagged for editorial review</b>" if c["safety_status"] == "flagged"
                else "")
        quote = _short(c["transcript_text"], 180)
        if c["thumb_path"] and Path(c["thumb_path"]).is_file():
            images.append({"cid": cid, "path": c["thumb_path"], "clip_id": c["id"]})
            img = (f"<img src='cid:{cid}' width='96' style='border-radius:6px;display:block' "
                   f"alt='Clip {c['id']} thumbnail'>")
        else:
            img = ""
        rows_h.append(
            f"<tr><td style='padding:8px 12px 8px 0;vertical-align:top'>{img}</td>"
            f"<td style='padding:8px 0;vertical-align:top;font:14px/1.45 Segoe UI,Arial,sans-serif;color:#1d2330'>"
            f"<b>Clip #{c['id']}</b> · {render.mmss(c['start'])}–{render.mmss(c['end'])} · {dur:.0f}s{flag}<br>"
            f"<span style='color:#555'>Direct quote (opening): “{html.escape(quote)}”</span><br>"
            f"<a href='{html.escape(_link(settings, f'/clips/{c['id']}'))}'>Review clip #{c['id']}</a></td></tr>")
        rows_t.append(f"- Clip #{c['id']} {render.mmss(c['start'])}-{render.mmss(c['end'])} ({dur:.0f}s)"
                      f"{' [flagged for editorial review]' if c['safety_status'] == 'flagged' else ''}\n"
                      f"  \"{quote}\"\n  {_link(settings, f'/clips/{c['id']}')}")
    source = rec["source_url"] or rec["original_filename"] or ""
    review = _link(settings, f"/review?status=ready")
    body_html = (
        "<div style='font:15px/1.5 Segoe UI,Arial,sans-serif;color:#1d2330;max-width:640px'>"
        f"<p><b>{html.escape(rec['title'])}</b><br><span style='color:#555'>Recorded/imported {date} · "
        f"Source: {html.escape(_short(source, 90))}</span></p>"
        f"<p>{n} clip{'s are' if n != 1 else ' is'} awaiting your review.</p>"
        f"<table cellspacing='0' cellpadding='0'>{''.join(rows_h)}</table>"
        f"<p><a href='{html.escape(review)}' style='background:#2856d8;color:#fff;padding:9px 14px;"
        f"border-radius:8px;text-decoration:none'>Open the review queue</a></p>"
        "<p style='color:#777;font-size:12px'>Private links: they open your Clipping Engine dashboard and require "
        "sign-in. Quotes are direct transcript excerpts; nothing has been published. "
        "Turn these emails off in Settings → Notifications.</p></div>")
    body_text = (f"{rec['title']}\nRecorded/imported {date}. Source: {source}\n\n{n} clip(s) awaiting review:\n\n"
                 + "\n".join(rows_t) + f"\n\nOpen the review queue: {review}\n\n"
                 "Private links (sign-in required). Nothing has been published.")
    return {"subject": subject, "html": body_html, "text": body_text, "images": images}


def build_failure(settings, db, job) -> dict:
    rec = db.recording(job["recording_id"]) if job["recording_id"] else None
    title = rec["title"] if rec else f"job #{job['id']}"
    subject = f"[Clip review] Processing failed — {_short(title, 70)}"
    link = _link(settings, f"/jobs/{job['id']}")
    err = _short(job["error"], 300)
    body_html = (f"<div style='font:15px/1.5 Segoe UI,Arial,sans-serif'><p><b>{html.escape(title)}</b></p>"
                 f"<p>The {html.escape(job['kind'].replace('_', ' '))} step failed: {html.escape(err)}</p>"
                 f"<p><a href='{html.escape(link)}'>Open the job log</a> (sign-in required). You can retry it there.</p></div>")
    body_text = f"{title}\nThe {job['kind']} step failed: {err}\nJob log: {link}"
    return {"subject": subject, "html": body_html, "text": body_text, "images": []}


def build_disk_low(settings, report: dict) -> dict:
    subject = "[Clip review] Disk space is running low"
    text = (f"Free space: {report['disk_free_gb']:.1f} GB. Clipping data uses {report['total_gb']:.1f} GB. "
            f"Review retention settings or remove old recordings: {_link(settings, '/settings')}")
    return {"subject": subject, "html": f"<p>{html.escape(text)}</p>", "text": text, "images": []}


# ---- queueing ---------------------------------------------------------------------------

def create(settings, db, queue, *, kind: str, dedupe_key: str, content: dict, recording_id: int | None = None) -> int | None:
    """Insert once per dedupe key. Status is 'queued' only if sending is enabled and approved."""
    if not settings.notify_enabled:
        return None
    if db.one("SELECT id FROM notifications WHERE dedupe_key=?", (dedupe_key,)):
        return None
    approved = is_approved(settings, db)
    status = "queued" if approved else "held"
    try:
        nid = db.insert("notifications", kind=kind, dedupe_key=dedupe_key, recording_id=recording_id,
                        to_addr=settings.notify_to or None, subject=content["subject"], body_html=content["html"],
                        body_text=content["text"], status=status, transport=settings.notify_transport)
    except Exception:  # noqa: BLE001 - unique dedupe_key raced
        return None
    db.set_kv(f"notify.images.{nid}", json.dumps(content.get("images", [])))
    if approved:
        queue.enqueue("send_notification", payload={"notification_id": nid}, dedupe_key=f"notify:{nid}",
                      max_attempts=6)
    return nid


def on_recording_ready(settings, db, queue, rid: int) -> int | None:
    content = build_ready(settings, db, rid)
    if not content:
        return None
    return create(settings, db, queue, kind="recording_ready", dedupe_key=f"ready:{rid}", content=content,
                  recording_id=rid)


def on_job_failed(settings, db, queue, job) -> int | None:
    if not settings.notify_on_failure or job["kind"] not in ("process_recording", "download_url", "render_clip"):
        return None
    key = f"failed:rec{job['recording_id']}" if job["recording_id"] else f"failed:job{job['id']}"
    return create(settings, db, queue, kind="job_failed", dedupe_key=key, content=build_failure(settings, db, job),
                  recording_id=job["recording_id"])


def release_held(settings, db, queue) -> int:
    """Owner action: queue notifications that were held before approval."""
    if not is_approved(settings, db):
        return 0
    n = 0
    for r in db.query("SELECT id FROM notifications WHERE status='held'"):
        db.update("notifications", r["id"], status="queued", to_addr=settings.notify_to)
        queue.enqueue("send_notification", payload={"notification_id": r["id"]}, dedupe_key=f"notify:{r['id']}",
                      max_attempts=6)
        n += 1
    return n


# ---- transports -----------------------------------------------------------------------------

def _thumb_bytes(path: str) -> bytes:
    """Small JPEG (240 px wide) for inline embedding."""
    ffmpeg = media.require("ffmpeg")
    p = subprocess.run([ffmpeg, "-v", "error", "-i", path, "-vf", "scale=240:-2", "-q:v", "5", "-f", "image2pipe",
                        "-vcodec", "mjpeg", "-"], capture_output=True, **media._popen_kwargs())
    return p.stdout if p.returncode == 0 else Path(path).read_bytes()


class GraphMailer:
    """Microsoft Graph delegated mail sending with device-code sign-in."""

    platform = "outlook"

    def __init__(self, settings, store, http: httpx.Client | None = None):
        self.settings, self.store = settings, store
        self.http = http or httpx.Client(timeout=30)
        self.auth = f"https://login.microsoftonline.com/{settings.outlook_tenant}/oauth2/v2.0"

    @property
    def configured(self) -> bool:
        return bool(self.settings.outlook_client_id)

    def status(self) -> dict:
        tok = self.store.get(self.platform)
        return {"configured": self.configured, "connected": bool(tok and tok.get("refresh_token")),
                "account": (tok or {}).get("account")}

    def start_device_code(self) -> dict:
        if not self.configured:
            raise NotifyNotConfigured("Set OUTLOOK_CLIENT_ID in .env (see README → Email notifications).")
        r = self.http.post(f"{self.auth}/devicecode", data={"client_id": self.settings.outlook_client_id,
                                                            "scope": SCOPES})
        body = r.json()
        if r.status_code != 200:
            raise NotifyNotConfigured(f"Microsoft sign-in could not start: {body.get('error_description') or body}")
        return body   # user_code, device_code, verification_uri, expires_in, interval, message

    def poll_device_code(self, device_code: str) -> str:
        """One poll. Returns 'pending', 'connected', or raises with Microsoft's message."""
        r = self.http.post(f"{self.auth}/token", data={
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code", "client_id": self.settings.outlook_client_id,
            "device_code": device_code})
        body = r.json()
        if r.status_code == 200:
            self._save(body)
            return "connected"
        if body.get("error") in ("authorization_pending", "slow_down"):
            return "pending"
        raise NotifyNotConfigured(f"Microsoft sign-in failed: {body.get('error_description') or body.get('error')}")

    def _save(self, body: dict) -> None:
        tok = {"access_token": body["access_token"], "refresh_token": body.get("refresh_token"),
               "expires_at": time.time() + int(body.get("expires_in", 3600)) - 120}
        try:
            me = self.http.get(f"{GRAPH}/me", headers={"Authorization": f"Bearer {tok['access_token']}"}).json()
            tok["account"] = me.get("mail") or me.get("userPrincipalName")
        except Exception:  # noqa: BLE001
            pass
        old = self.store.get(self.platform) or {}
        if not tok.get("refresh_token"):
            tok["refresh_token"] = old.get("refresh_token")
        tok.setdefault("account", old.get("account"))
        self.store.set(self.platform, tok)

    def _token(self, force: bool = False) -> str:
        tok = self.store.get(self.platform)
        if not tok or not tok.get("refresh_token"):
            raise NotifyNotConfigured("Outlook is not connected. Settings → Notifications → Connect Outlook.")
        if force or tok["expires_at"] < time.time():
            try:
                r = self.http.post(f"{self.auth}/token", data={"grant_type": "refresh_token", "scope": SCOPES,
                                                               "client_id": self.settings.outlook_client_id,
                                                               "refresh_token": tok["refresh_token"]})
            except httpx.TransportError as e:
                raise TransientError(f"Microsoft sign-in endpoint unreachable: {e}") from e
            if r.status_code != 200:
                raise NotifyNotConfigured("Outlook sign-in expired; reconnect in Settings → Notifications.")
            self._save(r.json())
            tok = self.store.get(self.platform)
        return tok["access_token"]

    def send(self, to: str, subject: str, html_body: str, images: list[dict], _retried: bool = False) -> str:
        msg = {"subject": subject, "body": {"contentType": "HTML", "content": html_body},
               "toRecipients": [{"emailAddress": {"address": to}}]}
        if images:
            msg["attachments"] = [{"@odata.type": "#microsoft.graph.fileAttachment", "name": f"{i['cid'].split('@')[0]}.jpg",
                                   "contentType": "image/jpeg", "isInline": True, "contentId": i["cid"],
                                   "contentBytes": base64.b64encode(_thumb_bytes(i["path"])).decode()}
                                  for i in images]
        try:
            r = self.http.post(f"{GRAPH}/me/sendMail", headers={"Authorization": f"Bearer {self._token()}"},
                               json={"message": msg, "saveToSentItems": True})
        except httpx.TransportError as e:
            raise TransientError(f"Microsoft Graph unreachable: {e}") from e
        if r.status_code == 202:
            return "Accepted by Microsoft Graph for delivery (HTTP 202)."
        if r.status_code == 401 and not _retried:
            self._token(force=True)
            return self.send(to, subject, html_body, images, _retried=True)
        if r.status_code == 429 or r.status_code >= 500:
            raise TransientError(f"Microsoft Graph temporarily refused the message (HTTP {r.status_code}).")
        detail = (r.json().get("error", {}) if r.content else {}).get("message", r.text[:200])
        raise PermanentError(f"Microsoft Graph rejected the message (HTTP {r.status_code}): {detail}")


class SmtpMailer:
    platform = "smtp"

    def __init__(self, settings, smtp_factory=None):
        self.settings = settings
        self.smtp_factory = smtp_factory

    @property
    def configured(self) -> bool:
        s = self.settings
        return bool(s.smtp_host and s.smtp_user and s.smtp_password)

    def status(self) -> dict:
        return {"configured": self.configured, "connected": self.configured, "account": self.settings.smtp_user}

    def send(self, to: str, subject: str, html_body: str, images: list[dict]) -> str:
        import smtplib
        s = self.settings
        if not self.configured:
            raise NotifyNotConfigured("SMTP_HOST, SMTP_USER and SMTP_PASSWORD must be set in .env.")
        m = EmailMessage()
        m["Subject"], m["From"], m["To"] = subject, s.smtp_from or s.smtp_user, to
        m.set_content("This message is best viewed as HTML.")
        m.add_alternative(html_body, subtype="html")
        html_part = m.get_payload()[1]
        for i in images:
            html_part.add_related(_thumb_bytes(i["path"]), maintype="image", subtype="jpeg", cid=f"<{i['cid']}>")
        factory = self.smtp_factory or (smtplib.SMTP_SSL if s.smtp_ssl else smtplib.SMTP)
        try:
            with factory(s.smtp_host, s.smtp_port, timeout=30) as conn:
                if not s.smtp_ssl:
                    conn.starttls()
                conn.login(s.smtp_user, s.smtp_password)
                conn.send_message(m)
        except (OSError, smtplib.SMTPServerDisconnected) as e:
            raise TransientError(f"SMTP server unreachable: {e}") from e
        except smtplib.SMTPAuthenticationError as e:
            raise PermanentError(f"SMTP login failed: {e}") from e
        return "Accepted by the SMTP server."


class Notifier:
    def __init__(self, settings, db, queue, store, graph_http=None, smtp_factory=None):
        self.settings, self.db, self.queue = settings, db, queue
        self.graph = GraphMailer(settings, store, graph_http)
        self.smtp = SmtpMailer(settings, smtp_factory)
        queue.register("send_notification", self.run)

    @property
    def mailer(self):
        return self.smtp if self.settings.notify_transport == "smtp" else self.graph

    def run(self, ctx: JobContext) -> None:
        nid = ctx.payload["notification_id"]
        n = self.db.one("SELECT * FROM notifications WHERE id=?", (nid,))
        if not n or n["status"] in ("sent", "suppressed"):
            return
        if not self.settings.notify_enabled:
            self.db.update("notifications", nid, status="suppressed", error="Notifications disabled.")
            return
        if not is_approved(self.settings, self.db):
            self.db.update("notifications", nid, status="held", error="Awaiting owner approval of recipient/template.")
            return
        images = json.loads(self.db.get_kv(f"notify.images.{nid}") or "[]")
        self.db.update("notifications", nid, status="sending", attempts=n["attempts"] + 1)
        try:
            detail = self.mailer.send(self.settings.notify_to, n["subject"], n["body_html"], images)
        except NotifyNotConfigured as e:
            self.db.update("notifications", nid, status="failed", error=str(e))
            raise PermanentError(str(e)) from e
        except TransientError as e:
            self.db.update("notifications", nid, status="queued", error=str(e))
            raise
        except PermanentError as e:
            self.db.update("notifications", nid, status="failed", error=str(e))
            raise
        self.db.update("notifications", nid, status="sent", sent_at=time.time(), error=None,
                       to_addr=self.settings.notify_to, transport=self.settings.notify_transport)
        ctx.log(f"Notification #{nid} sent: {detail}")

    def preview_html(self, nid: int) -> str:
        """Dashboard preview: inline cid images are replaced with authenticated thumbnail URLs."""
        n = self.db.one("SELECT * FROM notifications WHERE id=?", (nid,))
        body = n["body_html"]
        for i in json.loads(self.db.get_kv(f"notify.images.{nid}") or "[]"):
            body = body.replace(f"cid:{i['cid']}", f"/clips/{i['clip_id']}/thumb")
        return body


def fingerprint(content: dict) -> str:
    return hashlib.sha1((content["subject"] + content["text"]).encode()).hexdigest()[:12]
