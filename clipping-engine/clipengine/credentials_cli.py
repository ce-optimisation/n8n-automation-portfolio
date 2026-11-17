"""Secure local entry and live testing of the monitoring credentials.

``Set-Credentials.cmd`` (or ``python -m clipengine --set-credentials``) asks for each missing value with a HIDDEN prompt
(nothing is echoed, nothing goes through chat), writes it to the local .env (only that key; every other line is kept),
and then makes ONE real authentication request per platform and reports pass/fail by HTTP status only. Values are never
printed, logged or put in any exception text. Press Enter to skip a value; existing values are only replaced if you type a new one.

``authentication_test`` is also used by Check-Portfolio.
"""
from __future__ import annotations

import getpass
import sys

from .config import get_settings, update_env_file
from .platforms import USER_AGENT

FIELDS = {
    "youtube": [("YOUTUBE_API_KEY", "youtube_api_key", "YouTube Data API v3 key (Google Cloud console > Credentials)")],
    "twitch": [("TWITCH_CLIENT_ID", "twitch_client_id", "Twitch Client ID (dev.twitch.tv/console)"),
               ("TWITCH_CLIENT_SECRET", "twitch_client_secret", "Twitch Client Secret (New Secret)")],
    "kick": [("KICK_CLIENT_ID", "kick_client_id", "Kick Client ID (Kick > Settings > Developer)"),
             ("KICK_CLIENT_SECRET", "kick_client_secret", "Kick Client Secret")],
}
# A well-known, stable public channel used only to prove the YouTube key is accepted (1 quota unit).
YT_PROBE_CHANNEL = "UC_x5XG1OV2P6uZZ5FSM9Ttw"


def authentication_test(settings, client=None) -> dict:
    """{platform: {'state': 'ok'|'failed'|'skipped', 'detail': str}} - one real request each; no secrets in any text."""
    import httpx
    own = client is None
    client = client or httpx.Client(timeout=20, headers={"User-Agent": USER_AGENT})
    out = {}
    try:
        def attempt(name, fn):
            try:
                out[name] = fn()
            except httpx.HTTPError as e:
                out[name] = {"state": "failed", "detail": f"network problem ({type(e).__name__})"}
            except Exception as e:  # noqa: BLE001
                out[name] = {"state": "failed", "detail": f"unexpected error ({type(e).__name__})"}

        def token(url, cid, secret, label):
            r = client.post(url, data={"client_id": cid, "client_secret": secret, "grant_type": "client_credentials"})
            if r.status_code == 200 and "access_token" in r.text:
                return {"state": "ok", "detail": f"{label}: app access token issued (HTTP 200)"}
            return {"state": "failed", "detail": f"{label}: token request refused (HTTP {r.status_code}) - check the id/secret"}

        def yt():
            r = client.get("https://www.googleapis.com/youtube/v3/channels",
                           params={"part": "id", "id": YT_PROBE_CHANNEL, "key": settings.youtube_api_key})
            if r.status_code == 200 and r.json().get("items"):
                return {"state": "ok", "detail": "YouTube: key accepted, channels.list answered (HTTP 200, 1 quota unit)"}
            if r.status_code == 403:
                return {"state": "failed", "detail": "YouTube: HTTP 403 - API not enabled for the key's project, key restricted away from "
                                                     "YouTube Data API v3, or quota exhausted"}
            return {"state": "failed", "detail": f"YouTube: HTTP {r.status_code} - key rejected or API not enabled"}
        if settings.youtube_api_key:
            attempt("youtube", yt)
        else:
            out["youtube"] = {"state": "skipped", "detail": "YOUTUBE_API_KEY not set"}
        if settings.twitch_client_id and settings.twitch_client_secret:
            attempt("twitch", lambda: token("https://id.twitch.tv/oauth2/token", settings.twitch_client_id, settings.twitch_client_secret, "Twitch"))
        else:
            out["twitch"] = {"state": "skipped", "detail": "TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET not both set"}
        if settings.kick_client_id and settings.kick_client_secret:
            attempt("kick", lambda: token("https://id.kick.com/oauth/token", settings.kick_client_id, settings.kick_client_secret, "Kick"))
        else:
            out["kick"] = {"state": "skipped", "detail": "KICK_CLIENT_ID / KICK_CLIENT_SECRET not both set"}
    finally:
        if own:
            client.close()
    return out


def main(settings=None, prompt=getpass.getpass, out=sys.stdout, client=None) -> int:
    settings = settings or get_settings()
    print("Secure credential entry. Typing is hidden. Press Enter to skip a value. Nothing is shown, logged or sent anywhere "
          "except the platform's own login/token address.\n", file=out)
    new: dict[str, str] = {}
    for plat, fields in FIELDS.items():
        for env, attr, label in fields:
            have = bool(getattr(settings, attr, ""))
            v = prompt(f"{label}{' [already set - Enter keeps it]' if have else ''}: ").strip()
            if v:
                new[env] = v
    if new:
        path = update_env_file(new, settings.env_file)
        print(f"\nSaved {len(new)} value(s) to {path} (names only: {', '.join(sorted(new))}).", file=out)
        settings = get_settings()
    else:
        print("\nNothing entered; .env unchanged.", file=out)
    print("\nLive authentication test (one request per platform):", file=out)
    for plat, r in authentication_test(settings, client).items():
        print(f"  {plat:8} {r['state'].upper():8} {r['detail']}", file=out)
    print("\nRestart the engine for new values to take effect, then run Check-Portfolio.cmd.", file=out)
    return 0
