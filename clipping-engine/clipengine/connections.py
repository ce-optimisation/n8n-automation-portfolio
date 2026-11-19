"""Per-destination connection state, strict account-identity verification and the brand-isolation guard (Session 13).

Why this exists: the YouTube/Twitch/Kick credentials are *monitoring* keys. They prove nothing about the ability to deliver to
a publishing account. Each of the owner's destinations (six brands x YouTube/Facebook/Instagram, plus TikTok for Afterlive) is
tracked separately through ten states, and a destination is only ever "verified" when the platform's own API reports the
exact stable account id we expect. Unknown is never reported as complete, and ``publishing_enabled`` is OFF unless the owner
turns it on for that one destination.

Nothing here uploads, publishes, or stores a secret. The read-only identity adapters take an access token as an argument and
never persist it; tokens stay in ``publishing.tokens.TokenStore`` under a per-account key (e.g. ``youtube:afterlive``).
"""
from __future__ import annotations

import json
import time
from pathlib import Path

ASSET = Path(__file__).parent / "assets" / "social_account_directory.json"

# The ten states tracked for every destination, in the order they must be satisfied.
STATE_KEYS = ("account_created", "profile_branded", "email_verified", "developer_app_configured", "oauth_connected",
              "api_identity_verified", "scopes_granted", "draft_supported", "draft_delivery_verified", "publishing_enabled")

VERIFIED, RECORDED, READY, BLOCKED = "VERIFIED", "RECORDED", "READY FOR OWNER", "BLOCKED"
UNSUPPORTED, NOT_TESTED, UNKNOWN, OFF, PARTIAL = "UNSUPPORTED", "NOT TESTED", "UNKNOWN", "OFF", "PARTIAL"

# What each platform's OFFICIAL API actually offers for "prepare but do not publish" (checked against the documentation
# on 19 Nov 2026). Never claim more than this.
DRAFT_WORKFLOW = {
    "youtube": (PARTIAL, "No native draft in the API. Closest: a PRIVATE upload (videos.insert with status.privacyStatus="
                         "'private' set explicitly, because an omitted value defaults to PUBLIC). Needs owner approval per upload."),
    "facebook": (NOT_TESTED, "Reels API supports video_state=DRAFT (/{page-id}/video_reels, upload_phase=finish). Whether the "
                             "draft is editable and publishable in Meta Business Suite is not documented: verify with one "
                             "authorised test."),
    "instagram": (UNSUPPORTED, "The API has no drafts: media_publish is PUBLIC. Use the local staging/export package and post "
                               "in the Instagram app. Automatic publishing stays disabled (INSTAGRAM_PUBLIC_PUBLISH off)."),
    "tiktok": (NOT_TESTED, "Content Posting API inbox upload (scope video.upload) delivers to the creator's TikTok inbox to "
                           "finish in the app; max 5 pending shares per 24 h. Developer-app audit status is unconfirmed."),
}
# Scopes needed to read the identity and (later, only if approved) deliver the closest draft. Publishing scopes for Instagram
# are intentionally NOT requested.
REQUIRED_SCOPES = {
    "youtube": ("https://www.googleapis.com/auth/youtube.readonly", "https://www.googleapis.com/auth/youtube.upload"),
    "facebook": ("pages_show_list", "pages_read_engagement", "pages_manage_posts"),
    "instagram": ("instagram_basic", "pages_show_list", "pages_read_engagement"),
    "tiktok": ("user.info.basic", "video.upload"),
}


def load_directory(path: Path | None = None) -> dict:
    return json.loads(Path(path or ASSET).read_text(encoding="utf-8"))


def connection_key_for(row) -> str:
    """TokenStore key for ONE destination, so a shared administrator login can never stand in for several brands."""
    return row["connection_key"] or f"{row['platform']}:{row['brand']}"


# ---- recording the owner's directory (evidence only) ---------------------------------------------------------------

def record_directory(db, directory: dict | None = None) -> dict:
    """Write the 19 recorded accounts into ``social_accounts`` (idempotent). Fills the placeholder rows; never overwrites a
    verified identity, never touches ``publishing_enabled``, never invents an account that was not created."""
    d = directory or load_directory()
    now = time.time()
    updated = created = 0
    for a in d["accounts"]:
        row = db.one("SELECT * FROM social_accounts WHERE platform=? AND brand=?", (a["platform"], a["brand"]))
        evidence = json.dumps({"source": "owner-recorded 19 Nov 2026 handoff", "url": a.get("url"), "note": a.get("note"),
                               "admin_login": d.get("youtube_admin_login") if a["platform"] == "youtube" else None,
                               "verified": False})
        fields = dict(handle=a["handle"], display_name=f"{a['brand']} ({a['platform']})", platform_id=a.get("expected_id"),
                      alt_ids_json=json.dumps(a.get("alt_ids") or []), status="awaiting_connection",
                      connection_key=f"{a['platform']}:{a['brand']}", updated_at=now,
                      notes="Account created (owner-recorded). Identity NOT yet verified through the official API.")
        if row is None:
            db.insert("social_accounts", platform=a["platform"], brand=a["brand"], created_at=now, evidence_json=evidence,
                      **fields)
            created += 1
            continue
        if row["identity_verified_at"]:
            continue                                   # never disturb a verified identity
        db.update("social_accounts", row["id"], evidence_json=evidence, **fields)
        updated += 1
    return {"updated": updated, "created": created, "total": len(d["accounts"])}


# ---- strict identity verification -----------------------------------------------------------------------------------

def identity_match(expected_id: str | None, alt_ids: list[str], observed: list[dict], expected_handle: str | None = None) -> dict:
    """Compare what the platform reports with what we expect. Exact id equality only; names are never fuzzy-matched.

    Returns {state: VERIFIED | ALT_ID_ONLY | HANDLE_ONLY | MISMATCH | NONE_OBSERVED, reason}."""
    if not observed:
        return {"state": "NONE_OBSERVED", "reason": "The platform returned no account for this authorisation."}
    ids = {str(o.get("id")) for o in observed if o.get("id")}
    if expected_id and str(expected_id) in ids:
        return {"state": "VERIFIED", "reason": "The platform reports the expected stable id."}
    if any(str(x) in ids for x in (alt_ids or [])):
        return {"state": "ALT_ID_ONLY", "reason": "Only an alternate public id matched; confirm which id the API uses before trusting it."}
    if not expected_id and expected_handle:
        handles = {str(o.get("username") or o.get("handle") or "").lower().lstrip("@") for o in observed}
        if expected_handle.lower().lstrip("@") in handles:
            return {"state": "HANDLE_ONLY", "reason": "Matched on handle only (no stable id was recorded); lower assurance."}
    return {"state": "MISMATCH", "reason": f"Expected id {expected_id!r} is not among the {len(ids)} account(s) the platform reported."}


def verify_identity(db, account_id: int, observed: list[dict], *, now: float | None = None) -> dict:
    """Apply ``identity_match`` to one destination and record the outcome. Only an exact id match sets VERIFIED. If the
    observed id belongs to a DIFFERENT brand's destination, that is reported as a cross-brand error and nothing is verified."""
    row = db.one("SELECT * FROM social_accounts WHERE id=?", (account_id,))
    if row is None:
        raise KeyError(f"social account {account_id} not found")
    alt = json.loads(row["alt_ids_json"] or "[]")
    res = identity_match(row["platform_id"], alt, observed, row["handle"])
    ids = {str(o.get("id")) for o in observed if o.get("id")}
    others = [r for r in db.query("SELECT * FROM social_accounts WHERE platform=? AND id!=? AND platform_id IS NOT NULL",
                                  (row["platform"], account_id)) if str(r["platform_id"]) in ids]
    if others and res["state"] != "VERIFIED":
        res = {"state": "CROSS_BRAND", "reason": "The authorised account is the destination of another brand: "
                                                  + ", ".join(sorted(r["brand"] for r in others))}
    t = now or time.time()
    fields = {"updated_at": t}
    if res["state"] == "VERIFIED":
        fields.update(verified_platform_id=str(row["platform_id"]), identity_verified_at=t, status="connected")
    ev = json.loads(row["evidence_json"] or "{}")
    ev.setdefault("identity_checks", []).append({"at": t, "result": res["state"], "reason": res["reason"],
                                                  "observed_ids": sorted(ids)[:10]})
    db.update("social_accounts", account_id, evidence_json=json.dumps(ev), **fields)
    return res


def routing_problems(db, clip_brand: str | None, account_id: int, *, explicit_owner_command: bool = False) -> list[str]:
    """Reasons a clip must NOT be routed to this destination. Empty = allowed. Brand isolation is absolute: a shared
    administrator login never lets one brand's clip reach another brand's account."""
    row = db.one("SELECT * FROM social_accounts WHERE id=?", (account_id,))
    if row is None:
        return ["Unknown destination."]
    out = []
    if not clip_brand or row["brand"] != clip_brand:
        out.append(f"Brand mismatch: clip brand {clip_brand!r} cannot go to the {row['brand']!r} account.")
    if not row["identity_verified_at"] or not row["verified_platform_id"]:
        out.append("The account's identity has not been verified through the platform's API.")
    elif row["platform_id"] and str(row["verified_platform_id"]) != str(row["platform_id"]):
        out.append("The verified id does not match the expected account id.")
    if not row["publishing_enabled"]:
        out.append("Publishing is not enabled for this destination (it is OFF by default).")
    if not explicit_owner_command:
        out.append("An explicit owner command is required for every delivery.")
    return out


# ---- read-only identity adapters (GET only; the token is an argument and is never stored) ----------------------------

def _get(http, url, token, params=None):
    r = http.get(url, params=params or {}, headers={"Authorization": f"Bearer {token}"})
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code} from {url.split('?')[0]}")
    return r.json()


def youtube_identity(http, access_token: str) -> list[dict]:
    """The channel(s) this authorisation acts for. For a Brand Account channel the token is bound to the channel the owner
    selected on the consent screen, so a shared Google login must be authorised ONCE PER CHANNEL."""
    j = _get(http, "https://www.googleapis.com/youtube/v3/channels", access_token, {"part": "id,snippet", "mine": "true"})
    return [{"id": i["id"], "name": i.get("snippet", {}).get("title"), "username": i.get("snippet", {}).get("customUrl"),
             "kind": "youtube_channel"} for i in j.get("items", [])]


def facebook_identity(http, access_token: str, version: str = "v23.0") -> list[dict]:
    """Pages this user token can manage, each with its linked Instagram professional account (if any)."""
    j = _get(http, f"https://graph.facebook.com/{version}/me/accounts", access_token,
             {"fields": "id,name,username,instagram_business_account{id,username}", "limit": "100"})
    out = []
    for p in j.get("data", []):
        out.append({"id": p["id"], "name": p.get("name"), "username": p.get("username"), "kind": "facebook_page"})
        ig = p.get("instagram_business_account")
        if ig:
            out.append({"id": ig["id"], "name": None, "username": ig.get("username"), "kind": "instagram_account",
                        "page_id": p["id"]})
    return out


def tiktok_identity(http, access_token: str) -> list[dict]:
    j = _get(http, "https://open.tiktokapis.com/v2/user/info/", access_token, {"fields": "open_id,display_name,username"})
    u = (j.get("data") or {}).get("user") or {}
    return [{"id": u["open_id"], "name": u.get("display_name"), "username": u.get("username"), "kind": "tiktok_user"}] if u.get("open_id") else []


# ---- the matrix ------------------------------------------------------------------------------------------------------

def _dev_app(settings, platform: str) -> tuple[str, str]:
    if platform == "youtube":
        own = bool(getattr(settings, "youtube_oauth_client_id", "") and getattr(settings, "youtube_oauth_client_secret", ""))
        ok = own or bool(getattr(settings, "google_client_id", "") and getattr(settings, "google_client_secret", ""))
        if own:
            return (READY, "Dedicated YouTube OAuth client configured; authorise each channel separately.")
        return (READY, "Only the Drive OAuth client is configured; YouTube scopes must be on its consent screen and the "
                       "YouTube Data API enabled in its project (or add a dedicated YOUTUBE_OAUTH_CLIENT_ID/SECRET).") if ok \
            else (BLOCKED, "No Google OAuth client configured.")
    if platform in ("facebook", "instagram"):
        ok = bool(getattr(settings, "meta_app_id", "") and getattr(settings, "meta_app_secret", ""))
        return (READY, "Meta app credentials present; app roles/permissions (Advanced access) unconfirmed.") if ok \
            else (BLOCKED, "No Meta developer app configured (META_APP_ID/META_APP_SECRET).")
    ok = bool(getattr(settings, "tiktok_client_key", "") and getattr(settings, "tiktok_client_secret", ""))
    return (READY, "TikTok app keys present; app review/audit unconfirmed.") if ok else (BLOCKED, "No TikTok developer app configured.")


def build_matrix(db, settings, store=None) -> list[dict]:
    """One row per destination with all ten states. Honest about unknowns: nothing is VERIFIED without API evidence."""
    rows = []
    for r in db.query("SELECT * FROM social_accounts ORDER BY brand, platform"):
        plat = r["platform"]
        if plat not in DRAFT_WORKFLOW:
            continue
        created = not str(r["handle"]).startswith("PENDING-")
        tok = store.get(connection_key_for(r)) if (store and created) else None
        scope_text = (tok or {}).get("scope") or ""
        have = set(scope_text.replace(",", " ").split())
        need = REQUIRED_SCOPES[plat]
        app = _dev_app(settings, plat)
        draft = DRAFT_WORKFLOW[plat]
        pkg = db.one("SELECT COUNT(*) n FROM publication_packages WHERE platform=? AND handoff_state IS NOT NULL "
                     "AND handoff_method='api'", (plat,))
        states = {
            "account_created": (RECORDED if created else BLOCKED,
                                "Recorded by owner; not API-verified." if created else "Not created (excluded from this effort)."),
            "profile_branded": (UNKNOWN, "Awaiting approved brand-guidelines pack; logos approved, banners/bios not yet."),
            "email_verified": (UNKNOWN, "Not checked through any API."),
            "developer_app_configured": app if created else (BLOCKED, "No account."),
            "oauth_connected": ((VERIFIED if tok else READY), "Token stored." if tok else "Owner must authorise this destination."),
            "api_identity_verified": ((VERIFIED, f"id {r['verified_platform_id']}") if r["identity_verified_at"]
                                      else (NOT_TESTED, "No API read-back yet.")),
            "scopes_granted": ((VERIFIED if have and set(need) <= have else (PARTIAL if have else NOT_TESTED)),
                               "needs: " + ", ".join(need)),
            "draft_supported": draft,
            "draft_delivery_verified": ((VERIFIED, "API handoff recorded.") if pkg and pkg["n"] else (NOT_TESTED, "No delivery test authorised.")),
            "publishing_enabled": ((OFF, "Disabled (default).") if not r["publishing_enabled"] else ("ON", "Owner enabled.")),
        }
        rows.append({"id": r["id"], "brand": r["brand"], "platform": plat, "handle": r["handle"],
                     "expected_id": r["platform_id"], "states": states})
    return rows


def render_matrix_text(rows: list[dict]) -> str:
    out = ["Destination connection matrix (a state is VERIFIED only with API evidence; publishing stays OFF by default)", ""]
    for r in rows:
        out.append(f"{r['brand']:22} {r['platform']:10} @{r['handle']}")
        for k in STATE_KEYS:
            s, note = r["states"][k]
            out.append(f"    {k:26} {s:16} {note}")
        out.append("")
    return "\n".join(out)


def main(settings, record: bool = False, as_json: bool = False) -> int:
    from .db import Database
    from .publishing.tokens import TokenStore
    db = Database(settings.db_path)
    if record:
        print("Recorded:", record_directory(db))
    rows = build_matrix(db, settings, TokenStore(settings.secrets_dir))
    print(json.dumps(rows, indent=2) if as_json else render_matrix_text(rows))
    return 0
