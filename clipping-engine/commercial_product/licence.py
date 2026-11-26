"""Verify signed local licence snapshots and resolve access at a supplied time."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
import tempfile


STATES = frozenset({"trial", "active", "past_due", "cancelled", "refunded"})
FIELDS = {"version", "account", "state", "issued_at", "valid_until", "offline_until"}
MAX_PAYLOAD = 8192
MAX_OFFLINE_SECONDS = 3 * 24 * 60 * 60


@dataclass(frozen=True)
class Access:
    allowed: bool
    reason: str


def resolve(
    payload: bytes,
    signature: bytes,
    *,
    public_key: Path,
    openssl: Path,
    account: str,
    now: int,
    minimum_issued_at: int = 0,
) -> Access:
    """Use the trusted publisher key and a trusted local time and issuance watermark."""
    if len(payload) > MAX_PAYLOAD or len(signature) != 64:
        raise ValueError("Invalid licence payload or signature length.")
    if not openssl.is_absolute() or not public_key.is_absolute():
        raise ValueError("The verifier and trusted key must use explicit absolute paths.")

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        claims_file = root / "claims.json"
        signature_file = root / "signature"
        claims_file.write_bytes(payload)
        signature_file.write_bytes(signature)
        result = subprocess.run(
            [str(openssl), "pkeyutl", "-verify", "-pubin", "-inkey", str(public_key),
             "-rawin", "-in", str(claims_file), "-sigfile", str(signature_file)],
            capture_output=True,
            timeout=5,
            cwd=root,
            check=False,
        )
    if result.returncode != 0:
        raise ValueError("Licence signature verification failed.")

    claims = json.loads(payload)
    if (
        not isinstance(claims, dict) or claims.keys() != FIELDS
        or type(claims["version"]) is not int or claims["version"] != 1
        or not account or claims["account"] != account
        or not isinstance(claims["state"], str) or claims["state"] not in STATES
    ):
        raise ValueError("Invalid licence claims or account.")
    issued, expires, offline = (claims[key] for key in ("issued_at", "valid_until", "offline_until"))
    if (
        any(type(value) is not int for value in (issued, expires, offline))
        or not 0 <= minimum_issued_at <= issued <= now
        or not issued <= expires <= offline <= expires + MAX_OFFLINE_SECONDS
    ):
        raise ValueError("Invalid licence dates or outdated snapshot.")

    state = claims["state"]
    if state in {"past_due", "cancelled", "refunded"}:
        return Access(False, state)
    if now < expires:
        return Access(True, state)
    if state == "active" and now < offline:
        return Access(True, "offline_grace")
    return Access(False, "expired")
