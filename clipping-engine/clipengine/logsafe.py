"""Keep credentials out of the logs (Session 13).

The HTTP client logs every request URL at INFO, and the YouTube Data API takes its key as a ``?key=`` query parameter, so
the full API key was written to ``data/logs/clipengine.jsonl`` (and the console) on every poll. ``redact`` removes secret
query parameters, bearer tokens and Google API keys from any text; it is applied to every log record before it is written.
"""
from __future__ import annotations

import logging
import re

# Any parameter whose NAME contains token/secret/key/password/sig (so fb_exchange_token, client_secret, x-amz-signature...)
# or is exactly "code".
_PARAM = re.compile(r"(?i)([?&;\s](?:[a-z0-9_\-]*(?:token|secret|key|password|passwd|sig|signature)[a-z0-9_\-]*|code)=)"
                    r"[^&\s\"'<>]+")
_FB_TOKEN = re.compile(r"\bEA[A-Za-z0-9]{3,}[A-Za-z0-9_\-]{40,}")
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=\-]{8,}")
_GOOGLE_KEY = re.compile(r"AIza[0-9A-Za-z_\-]{30,}")
_JSON_SECRET = re.compile(r"(?i)(\"(?:access_token|refresh_token|client_secret|api_key|password|id_token)\"\s*:\s*\")[^\"]+")


def redact(text: str) -> str:
    if not text:
        return text
    t = _PARAM.sub(lambda m: m.group(1) + "<redacted>", text)
    t = _BEARER.sub(lambda m: m.group(1) + " <redacted>", t)
    t = _GOOGLE_KEY.sub("<redacted-google-key>", t)
    t = _FB_TOKEN.sub("<redacted-meta-token>", t)
    return _JSON_SECRET.sub(lambda m: m.group(1) + "<redacted>", t)


class RedactingFilter(logging.Filter):
    """Rewrite the record's message (and any exception text) with secrets removed. Never drops a record."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = redact(record.getMessage())
            record.args = ()
            if record.exc_text:
                record.exc_text = redact(record.exc_text)
        except Exception:      # noqa: BLE001  (logging must never fail because of redaction)
            pass
        return True
