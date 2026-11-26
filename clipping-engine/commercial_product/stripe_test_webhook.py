"""Verify Stripe sandbox snapshot events at the boundary of a future billing service."""

import hashlib
import hmac
import json
import re


def verify_test_event(body: bytes, header: str, secret: bytes, *, now: int) -> dict:
    """Return a signed sandbox event. The caller provides the unmodified request body."""
    if not secret or len(header) > 4096 or len(body) > 1024 * 1024:
        raise ValueError("Invalid webhook inputs.")
    parts = [part.strip().partition("=") for part in header.split(",")]
    timestamps = [value for name, separator, value in parts if name == "t" and separator]
    signatures = [value for name, separator, value in parts if name == "v1" and separator
                  and re.fullmatch("[0-9a-f]{64}", value)]
    if (len(timestamps) != 1 or not re.fullmatch("[0-9]{1,12}", timestamps[0])
            or abs(now - int(timestamps[0])) > 300):
        raise ValueError("Invalid or stale signing timestamp.")
    expected = hmac.new(secret, timestamps[0].encode("ascii") + b"." + body, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, signature) for signature in signatures):
        raise ValueError("Invalid webhook signature.")
    event = json.loads(body)
    if (not isinstance(event, dict) or event.get("object") != "event" or event.get("livemode") is not False
            or not isinstance(event.get("id"), str) or not event["id"].startswith("evt_")
            or not isinstance(event.get("type"), str) or not event["type"]):
        raise ValueError("Expected a Stripe sandbox snapshot event.")
    return event
