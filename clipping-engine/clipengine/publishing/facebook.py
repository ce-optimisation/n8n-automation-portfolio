"""Facebook Pages (and their linked Instagram professional accounts) for the six brands (Session 13).

One Facebook login administers all six Pages, so authorising once must NOT mean "any brand may use any Page". After the
login, every brand's expected Page id (and linked Instagram id) is matched EXACTLY against what Meta reports, and a
per-brand Page access token is stored under ``facebook:<brand>`` only for a match. Tokens are never shown or logged.

Closest official "prepare, never publish" workflow: Reels API with ``video_state=DRAFT`` (documented; whether the draft can
be edited in Meta Business Suite is NOT documented, so it is reported as NOT TESTED until one authorised delivery proves it).
Nothing in this module uploads unless ``create_draft_reel`` is called, which the delivery layer only does after the routing
guard passes and the owner has issued an explicit command.
"""
from __future__ import annotations

import secrets
import urllib.parse
from pathlib import Path

import httpx

from .. import connections as cx
from .base import NotConfigured, PublishError

# Instagram permissions must first be enabled in the Meta app. Only the Instagram
# connection entry point requests them; the Pages-only entry point remains available.
LOGIN_SCOPES = "pages_show_list,pages_read_engagement,pages_manage_posts,business_management"
INSTAGRAM_SCOPES = LOGIN_SCOPES + ",instagram_basic,instagram_content_publish"
USER_KEY = "facebook:user"
ANALYTICS_SCOPES = "pages_show_list,pages_read_engagement,read_insights,instagram_basic,instagram_manage_insights"


class FacebookClient:
    platform = "facebook"

    def __init__(self, app_id: str, app_secret: str, redirect_uri: str, store, version: str = "v23.0",
                 http: httpx.Client | None = None):
        self.app_id, self.app_secret, self.redirect_uri = app_id, app_secret, redirect_uri
        self.store, self.version = store, version
        self.graph = f"https://graph.facebook.com/{version}"
        self.http = http or httpx.Client(timeout=httpx.Timeout(60.0, read=600.0))

    # ---- login ---------------------------------------------------------------------------------------------------
    @property
    def configured(self) -> bool:
        return bool(self.app_id and self.app_secret and self.redirect_uri)

    def missing_config(self) -> list[str]:
        return [k for k, v in (("META_APP_ID", self.app_id), ("META_APP_SECRET", self.app_secret),
                               ("META_REDIRECT_URI", self.redirect_uri)) if not v]

    def authorize_url(self, include_instagram: bool = False, *, analytics: bool = False,
                      redirect_uri: str | None = None) -> tuple[str, dict]:
        if not self.configured:
            raise NotConfigured("Facebook is not configured: set " + ", ".join(self.missing_config()) + " in .env")
        state = secrets.token_urlsafe(24)
        redirect = redirect_uri or self.redirect_uri
        q = {"client_id": self.app_id, "redirect_uri": redirect, "state": state,
             "scope": ANALYTICS_SCOPES if analytics else (INSTAGRAM_SCOPES if include_instagram else LOGIN_SCOPES),
             "response_type": "code"}
        return f"https://www.facebook.com/{self.version}/dialog/oauth?" + urllib.parse.urlencode(q), {
            "state": state, "redirect": redirect}

    def _json(self, r: httpx.Response) -> dict:
        body = r.json() if r.content else {}
        if r.status_code == 200 and not (isinstance(body, dict) and body.get("error")):
            return body
        err = (body.get("error") if isinstance(body, dict) else None) or {}
        raise PublishError(f"Facebook API error ({err.get('code', r.status_code)}): "
                           f"{err.get('error_user_msg') or err.get('message') or 'HTTP ' + str(r.status_code)}")

    def exchange_code(self, code: str) -> None:
        """Code -> long-lived USER token (stored under facebook:user). Page tokens come later, per brand, after id checks."""
        self.store.set(USER_KEY, self._exchange_user(code))

    def _exchange_user(self, code: str, redirect_uri: str | None = None) -> dict:
        short = self._json(self.http.get(f"{self.graph}/oauth/access_token", params={
            "client_id": self.app_id, "redirect_uri": redirect_uri or self.redirect_uri,
            "client_secret": self.app_secret, "code": code}))
        long = self._json(self.http.get(f"{self.graph}/oauth/access_token", params={
            "grant_type": "fb_exchange_token", "client_id": self.app_id, "client_secret": self.app_secret,
            "fb_exchange_token": short["access_token"]}))
        permissions = self._json(self.http.get(f"{self.graph}/me/permissions", params={
            "access_token": long["access_token"]}))
        granted = sorted({p["permission"] for p in permissions.get("data", [])
                          if p.get("status") == "granted" and p.get("permission")})
        return {"access_token": long["access_token"], "scope": ",".join(granted)}

    def connect_analytics(self, db, code: str, redirect_uri: str | None = None) -> int:
        user = self._exchange_user(code, redirect_uri)
        scopes = set(user["scope"].split(","))
        pages = {str(p["id"]): p for p in self.managed_pages(user["access_token"])}
        saved = 0
        for account in db.query("SELECT * FROM social_accounts WHERE platform IN ('facebook','instagram') "
                                "AND identity_verified_at IS NOT NULL"):
            platform, brand = account["platform"], account["brand"]
            page_account = account if platform == "facebook" else db.one(
                "SELECT * FROM social_accounts WHERE platform='facebook' AND brand=? AND identity_verified_at IS NOT NULL",
                (brand,))
            page = pages.get(str(page_account["verified_platform_id"]), {}) if page_account else {}
            identity = page.get("id") if platform == "facebook" else (page.get("instagram_business_account") or {}).get("id")
            required = {"pages_show_list", "pages_read_engagement"}
            required.update({"read_insights"} if platform == "facebook" else {"instagram_basic", "instagram_manage_insights"})
            if str(identity) != str(account["verified_platform_id"]) or not page.get("access_token") or not required <= scopes:
                continue
            self.store.set(f"{platform}-analytics:{brand}", {"access_token": page["access_token"],
                           "scope": user["scope"], "identity": str(identity)})
            saved += 1
        return saved

    def user_connected(self) -> bool:
        return bool(self.store.get(USER_KEY))

    # ---- per-brand adoption (the isolation step) ---------------------------------------------------------------------
    def managed_pages(self, token: str | None = None) -> list[dict]:
        """Pages the login manages, WITH their page tokens (kept in memory only) and linked Instagram account."""
        tok = token or (self.store.get(USER_KEY) or {}).get("access_token")
        if not tok:
            raise NotConfigured("Facebook login has not been authorised yet.")
        j = self._json(self.http.get(f"{self.graph}/me/accounts", params={
            "fields": "id,name,username,access_token,instagram_business_account{id,username}", "limit": "100",
            "access_token": tok}))
        return j.get("data", [])

    def adopt_pages(self, db) -> list[dict]:
        """For each brand's Facebook and Instagram destination: exact-id match, then store that brand's Page token under
        ``facebook:<brand>`` / ``instagram:<brand>``. Returns one result dict per destination; nothing is stored on a mismatch."""
        pages = self.managed_pages()
        granted_scopes = (self.store.get(USER_KEY) or {}).get("scope", "")
        by_id = {str(p["id"]): p for p in pages}
        out = []
        for acct in db.query("SELECT * FROM social_accounts WHERE platform IN ('facebook','instagram') AND "
                             "handle NOT LIKE 'PENDING-%' ORDER BY brand, platform"):
            if acct["platform"] == "facebook":
                observed = [{"id": p["id"], "name": p.get("name"), "username": p.get("username")} for p in pages]
                res = cx.verify_identity(db, acct["id"], observed)
                page = by_id.get(str(acct["platform_id"]))
                if res["state"] == "VERIFIED" and page and page.get("access_token"):
                    self.store.set(f"facebook:{acct['brand']}", {"access_token": page["access_token"], "page_id": page["id"],
                                                                 "scope": granted_scopes})
            else:                                   # instagram: must be the account linked to THIS brand's Page
                fb = db.one("SELECT * FROM social_accounts WHERE platform='facebook' AND brand=?", (acct["brand"],))
                page = by_id.get(str(fb["platform_id"])) if fb and fb["platform_id"] else None
                ig = (page or {}).get("instagram_business_account")
                observed = [{"id": ig["id"], "username": ig.get("username")}] if ig else []
                res = cx.verify_identity(db, acct["id"], observed)
                if res["state"] == "VERIFIED" and page and page.get("access_token"):
                    self.store.set(f"instagram:{acct['brand']}", {"access_token": page["access_token"], "ig_user_id": ig["id"],
                                                                  "username": ig.get("username"), "scope": granted_scopes})
            out.append({"brand": acct["brand"], "platform": acct["platform"], "handle": acct["handle"], **res})
        return out

    # ---- the closest supported draft workflow (NEVER called by connecting; delivery layer only) -------------------------
    def create_draft_reel(self, db, account_id: int, video: Path, description: str) -> dict:
        return self.create_reel(db, account_id, video, description, publish=False)

    def create_reel(self, db, account_id: int, video: Path, description: str, publish: bool = False) -> dict:
        """Reels API: start -> upload -> finish with video_state=DRAFT (default) or PUBLISHED (owner chose public mode for this
        Page). The caller has already passed the routing guard; this re-checks that the stored token belongs to the verified Page
        so a stale token can never reach another brand."""
        acct = db.one("SELECT * FROM social_accounts WHERE id=?", (account_id,))
        tok = self.store.get(f"facebook:{acct['brand']}") if acct else None
        if not acct or acct["platform"] != "facebook" or not tok or str(tok.get("page_id")) != str(acct["verified_platform_id"]):
            raise NotConfigured("No verified Page token is stored for this brand; connect and verify its Page first.")
        video = Path(video)
        if not video.is_file():
            raise PublishError("Video file not found.")
        page, token, size = tok["page_id"], tok["access_token"], video.stat().st_size
        start = self._json(self.http.post(f"{self.graph}/{page}/video_reels", data={"upload_phase": "start", "access_token": token}))
        vid, url = start["video_id"], start["upload_url"]
        with video.open("rb") as fh:
            up = self.http.post(url, headers={"Authorization": f"OAuth {token}", "offset": "0", "file_size": str(size)}, content=fh.read())
        if up.status_code != 200:
            raise PublishError(f"Facebook upload failed (HTTP {up.status_code}).")
        fin = self._json(self.http.post(f"{self.graph}/{page}/video_reels", data={
            "upload_phase": "finish", "video_id": vid, "video_state": "PUBLISHED" if publish else "DRAFT",
            "description": description or "", "access_token": token}))
        return {"video_id": vid, "state": "PUBLISHED" if publish else "DRAFT", "success": bool(fin.get("success", True)),
                "response": {k: fin.get(k) for k in ("success", "message") if k in fin}}

    def reel_status(self, video_id: str, page_token: str) -> dict:
        return self._json(self.http.get(f"{self.graph}/{video_id}", params={"fields": "status", "access_token": page_token}))
