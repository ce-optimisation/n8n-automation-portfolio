"""Source platform adapters: Rumble, Twitch, Kick, YouTube (Live) behind one normalised interface.

Each adapter DECLARES what it can do (`capabilities`) instead of pretending every platform supports the
same actions, and keeps DISCOVERY (public metadata through official APIs) strictly apart from
ACQUISITION (downloading or recording footage). Discovery never authorises retrieval: acquisition still
goes through `ingest.classify_url`, the source registry and the recorded-permission rules.

Capabilities
  identity        resolve a channel URL/handle to a stable id and name
  live_status     is the channel live now (title, viewers, started)
  vods            recent past broadcasts / uploads with duration
  clips           community clips with view counts
  schedule        upcoming scheduled streams
  stats           follower/subscriber or viewer figures (as the platform reports them)
  acquire_vod     a video can be retrieved by the engine (method stated in `acquisition`)
  acquire_live    a live broadcast can be recorded by the engine
  acquire_clip_official  an OFFICIAL download endpoint exists (Twitch Get Clips Download: broadcaster/
                  editor authorisation required)

Rate limits: each adapter keeps a minimum interval between calls; monitoring stays lightweight.
Credentials (all free developer registrations, owner action): TWITCH_CLIENT_ID/SECRET, KICK_CLIENT_ID/
SECRET, YOUTUBE_API_KEY. Without them an adapter reports `needs_credentials` and the engine falls
back to its existing yt-dlp listing where permitted.
"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

USER_AGENT = "LocalClippingEngine/1.1 (self-hosted editorial review tool)"


# Specific states every integration can report (never a bare "failed").
ERROR_CODES = {
    "needs_credentials": "Missing credentials (owner must register a free developer app / key).",
    "not_found": "Invalid channel identity (the platform has no such channel).",
    "unauthorised": "API authentication failed (credentials rejected, expired or lacking access).",
    "rate_limited": "Rate limit or quota reached; the engine waits and retries.",
    "offline": "Source offline (the site or channel is not reachable).",
    "inaccessible": "Content inaccessible (removed, private, geo-blocked or login-only).",
    "permission_missing": "Acquisition permission missing (discovery still works).",
    "temporary_failure": "Temporary service or network failure; retried with back-off.",
}


class AdapterError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class ChannelRef:
    platform: str
    handle: str            # login/slug/handle as used in URLs
    url: str


@dataclass
class LiveStatus:
    live: bool
    title: str = ""
    viewers: int | None = None
    started_at: str = ""
    category: str = ""
    stream_url: str = ""   # the page to record (acquisition still needs permission)


@dataclass
class MediaItem:
    key: str
    title: str
    url: str
    kind: str = "vod"      # vod | clip | upload | live | upcoming
    published: str = ""
    duration_s: float | None = None
    views: int | None = None


@dataclass
class Report:
    """What an adapter call established, with honest provenance."""
    platform: str
    capabilities: dict
    identity: dict = field(default_factory=dict)
    live: LiveStatus | None = None
    items: list[MediaItem] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    source: str = ""       # e.g. "Twitch Helix API (official)", "rumble.com channel page"


def parse_channel_url(url: str) -> ChannelRef | None:
    """Recognise a channel address on a supported platform. Never invents handles."""
    p = urlparse(url.strip())
    host = (p.hostname or "").lower().removeprefix("www.").removeprefix("m.")
    path = [x for x in (p.path or "").split("/") if x]
    if host == "twitch.tv" and path and path[0] not in ("videos", "directory", "settings", "p"):
        return ChannelRef("twitch", path[0].lower(), f"https://www.twitch.tv/{path[0].lower()}")
    if host == "kick.com" and path and path[0] not in ("categories", "browse", "video"):
        return ChannelRef("kick", path[0].lower(), f"https://kick.com/{path[0].lower()}")
    if host in ("youtube.com", "youtu.be") and path:
        if path[0].startswith("@"):
            return ChannelRef("youtube", path[0], f"https://www.youtube.com/{path[0]}")
        if path[0] in ("channel", "c", "user") and len(path) > 1:
            return ChannelRef("youtube", path[1], f"https://www.youtube.com/{path[0]}/{path[1]}")
    if host == "rumble.com" and len(path) > 1 and path[0] in ("c", "user"):
        return ChannelRef("rumble", path[1], f"https://rumble.com/{path[0]}/{path[1]}")
    return None


class _Throttle:
    def __init__(self, min_interval: float):
        self.min_interval, self.last, self.lock = min_interval, 0.0, threading.Lock()

    def wait(self):
        with self.lock:
            delay = self.last + self.min_interval - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self.last = time.monotonic()


class Adapter:
    platform = ""
    official_api = ""
    # Acquisition method per kind, stated plainly for the dashboard and the handoff.
    acquisition = {}

    def __init__(self, settings, client=None):
        self.settings = settings
        self.client = client            # an httpx.Client (tests pass one with a MOCK transport)
        self._throttle = _Throttle(1.0)

    def configured(self) -> bool:
        return True

    def capabilities(self) -> dict:
        raise NotImplementedError

    def _http(self):
        if self.client is None:
            import httpx
            self.client = httpx.Client(timeout=20, headers={"User-Agent": USER_AGENT})
        return self.client

    def _get(self, url: str, **kw):
        """GET with the engine's error taxonomy (see ERROR_CODES). Network trouble and 5xx are TEMPORARY."""
        import httpx
        self._throttle.wait()
        try:
            r = self._http().get(url, **kw)
        except httpx.TransportError as e:
            raise AdapterError("temporary_failure", f"{self.platform}: network problem ({type(e).__name__}); "
                                                    "will retry") from e
        return self._check(r)

    def _check(self, r):
        code = r.status_code
        if code == 429:
            raise AdapterError("rate_limited", f"{self.platform}: HTTP 429 (rate limited); back off and retry later")
        if code == 403:
            body = (r.text or "")[:2000].lower() if hasattr(r, "text") else ""
            if "quota" in body or "ratelimit" in body:
                raise AdapterError("rate_limited", f"{self.platform}: API quota exhausted; waiting for it to reset")
            raise AdapterError("unauthorised", f"{self.platform}: HTTP 403 — credentials missing, expired or "
                                               "lacking access")
        if code == 401:
            raise AdapterError("unauthorised", f"{self.platform}: HTTP 401 — credentials missing, expired or "
                                               "lacking access")
        if code == 404:
            raise AdapterError("not_found", f"{self.platform}: HTTP 404 — the channel or item does not exist")
        if code in (400,) and "api key" in ((r.text or "").lower() if hasattr(r, "text") else ""):
            raise AdapterError("unauthorised", f"{self.platform}: the API key was rejected")
        if code in (410, 451):
            raise AdapterError("inaccessible", f"{self.platform}: HTTP {code} — content unavailable to this client")
        if code >= 500:
            raise AdapterError("temporary_failure", f"{self.platform}: HTTP {code} (service problem); will retry")
        if code >= 400:
            raise AdapterError("http_error", f"{self.platform}: HTTP {code}")
        return r

    def identity(self, ref: ChannelRef) -> dict:
        """Cheap identity lookup (stable id + official name). Never searches by display name."""
        raise AdapterError("unsupported", f"{self.platform}: identity lookup not available")

    def report(self, ref: ChannelRef) -> Report:
        raise NotImplementedError


# ---- Twitch (Helix, official) -----------------------------------------------------------------

class TwitchAdapter(Adapter):
    platform = "twitch"
    official_api = "Twitch Helix API (app access token; free developer application)"
    acquisition = {
        "acquire_clip_official": "Official 'Get Clips Download' endpoint — only with the broadcaster's or an "
                                 "editor's authorisation (editor:manage:clips / channel:manage:clips).",
        "acquire_vod": "No official VOD download API. yt-dlp can read public VODs; used ONLY under a recorded "
                       "written permission for twitch.tv (same rule as Rumble).",
        "acquire_live": "No official recording API. yt-dlp/HLS capture ONLY under a recorded permission.",
    }
    API = "https://api.twitch.tv/helix"
    TOKEN_URL = "https://id.twitch.tv/oauth2/token"

    def __init__(self, settings, client=None):
        super().__init__(settings, client)
        self._token: tuple[str, float] | None = None

    def configured(self) -> bool:
        return bool(getattr(self.settings, "twitch_client_id", "") and getattr(self.settings, "twitch_client_secret", ""))

    def capabilities(self) -> dict:
        ok = self.configured()
        return {"identity": ok, "live_status": ok, "vods": ok, "clips": ok, "schedule": ok, "stats": ok,
                "acquire_vod": "permission_required", "acquire_live": "permission_required",
                "acquire_clip_official": "broadcaster_authorisation_required",
                "state": "ready" if ok else "needs_credentials"}

    def _headers(self) -> dict:
        if not self.configured():
            raise AdapterError("needs_credentials", "Twitch: add TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET "
                                                    "(free application at dev.twitch.tv/console)")
        if not self._token or self._token[1] < time.time() + 60:
            r = self._http().post(self.TOKEN_URL, data={"client_id": self.settings.twitch_client_id,
                                                         "client_secret": self.settings.twitch_client_secret,
                                                         "grant_type": "client_credentials"})
            if r.status_code != 200:
                raise AdapterError("unauthorised", f"Twitch token request failed (HTTP {r.status_code})")
            d = r.json()
            self._token = (d["access_token"], time.time() + float(d.get("expires_in", 3600)))
        return {"Client-Id": self.settings.twitch_client_id, "Authorization": f"Bearer {self._token[0]}"}

    def identity(self, ref: ChannelRef) -> dict:
        h = self._headers()
        users = self._get(f"{self.API}/users", params={"login": ref.handle}, headers=h).json().get("data", [])
        if not users:
            raise AdapterError("not_found", f"Twitch: no channel '{ref.handle}'")
        u = users[0]
        return {"id": u["id"], "login": u["login"], "name": u.get("display_name"),
                "url": f"https://www.twitch.tv/{u['login']}", "broadcaster_type": u.get("broadcaster_type") or "",
                "platform_verified": u.get("broadcaster_type") == "partner"}

    def report(self, ref: ChannelRef, clips: bool = True) -> Report:
        rep = Report("twitch", self.capabilities(), source=self.official_api)
        h = self._headers()
        users = self._get(f"{self.API}/users", params={"login": ref.handle}, headers=h).json().get("data", [])
        if not users:
            raise AdapterError("not_found", f"Twitch: no channel '{ref.handle}'")
        u = users[0]
        rep.identity = {"id": u["id"], "login": u["login"], "name": u.get("display_name"),
                        "created_at": u.get("created_at"), "url": f"https://www.twitch.tv/{u['login']}"}
        try:
            f = self._get(f"{self.API}/channels/followers", params={"broadcaster_id": u["id"]}, headers=h).json()
            rep.identity["followers"] = f.get("total")
        except AdapterError as e:
            rep.errors.append(str(e))
        s = self._get(f"{self.API}/streams", params={"user_id": u["id"]}, headers=h).json().get("data", [])
        rep.live = LiveStatus(bool(s), title=s[0].get("title", "") if s else "",
                              viewers=s[0].get("viewer_count") if s else None,
                              started_at=s[0].get("started_at", "") if s else "",
                              category=s[0].get("game_name", "") if s else "",
                              stream_url=rep.identity["url"] if s else "")
        for v in self._get(f"{self.API}/videos", params={"user_id": u["id"], "type": "archive", "first": 20},
                           headers=h).json().get("data", []):
            rep.items.append(MediaItem(key=f"twitch:v{v['id']}", title=v.get("title", ""), url=v.get("url", ""),
                                       kind="vod", published=v.get("created_at", ""),
                                       duration_s=parse_twitch_duration(v.get("duration", "")),
                                       views=v.get("view_count")))
        if clips:
            for c in self._get(f"{self.API}/clips", params={"broadcaster_id": u["id"], "first": 20},
                               headers=h).json().get("data", []):
                rep.items.append(MediaItem(key=f"twitch:c{c['id']}", title=c.get("title", ""), url=c.get("url", ""),
                                           kind="clip", published=c.get("created_at", ""),
                                           duration_s=c.get("duration"), views=c.get("view_count")))
        try:
            sch = self._get(f"{self.API}/schedule", params={"broadcaster_id": u["id"], "first": 5}, headers=h).json()
            for seg in (sch.get("data") or {}).get("segments") or []:
                rep.items.append(MediaItem(key=f"twitch:s{seg['id']}", title=seg.get("title", ""),
                                           url=rep.identity["url"], kind="upcoming", published=seg.get("start_time", "")))
        except AdapterError as e:
            if e.code not in ("http_error", "not_found"):      # 404 = no schedule set; not an error worth showing
                rep.errors.append(str(e))
        return rep


def parse_twitch_duration(s: str) -> float | None:
    m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", s or "")
    if not m or not any(m.groups()):
        return None
    h, mi, se = (int(x or 0) for x in m.groups())
    return h * 3600 + mi * 60 + se


# ---- Kick (public API v1, official) -----------------------------------------------------------

class KickAdapter(Adapter):
    platform = "kick"
    official_api = "Kick public API v1 (app access token; free developer application)"
    acquisition = {
        "acquire_vod": "The public API documents no VOD or clip export. Retrieval only under a recorded "
                       "written permission for kick.com, through the engine's existing yt-dlp path; unverified.",
        "acquire_live": "No official recording API. Capture only under a recorded permission; unverified.",
    }
    API = "https://api.kick.com/public/v1"
    TOKEN_URL = "https://id.kick.com/oauth/token"

    def __init__(self, settings, client=None):
        super().__init__(settings, client)
        self._token: tuple[str, float] | None = None

    def configured(self) -> bool:
        return bool(getattr(self.settings, "kick_client_id", "") and getattr(self.settings, "kick_client_secret", ""))

    def capabilities(self) -> dict:
        ok = self.configured()
        return {"identity": ok, "live_status": ok, "vods": False, "clips": False, "schedule": False,
                "stats": ok, "acquire_vod": "permission_required_unverified",
                "acquire_live": "permission_required_unverified", "state": "ready" if ok else "needs_credentials"}

    def _headers(self) -> dict:
        if not self.configured():
            raise AdapterError("needs_credentials", "Kick: add KICK_CLIENT_ID and KICK_CLIENT_SECRET "
                                                    "(free application in Kick's developer settings)")
        if not self._token or self._token[1] < time.time() + 60:
            r = self._http().post(self.TOKEN_URL, data={"client_id": self.settings.kick_client_id,
                                                         "client_secret": self.settings.kick_client_secret,
                                                         "grant_type": "client_credentials"})
            if r.status_code != 200:
                raise AdapterError("unauthorised", f"Kick token request failed (HTTP {r.status_code})")
            d = r.json()
            self._token = (d["access_token"], time.time() + float(d.get("expires_in", 3600)))
        return {"Authorization": f"Bearer {self._token[0]}", "Accept": "application/json"}

    def identity(self, ref: ChannelRef) -> dict:
        h = self._headers()
        chans = self._get(f"{self.API}/channels", params={"slug": ref.handle}, headers=h).json().get("data", [])
        if not chans:
            raise AdapterError("not_found", f"Kick: no channel '{ref.handle}'")
        c = chans[0]
        return {"id": c.get("broadcaster_user_id"), "slug": c.get("slug"), "name": c.get("slug"),
                "url": f"https://kick.com/{c.get('slug', ref.handle)}", "platform_verified": False}

    def report(self, ref: ChannelRef) -> Report:
        rep = Report("kick", self.capabilities(), source=self.official_api)
        h = self._headers()
        chans = self._get(f"{self.API}/channels", params={"slug": ref.handle}, headers=h).json().get("data", [])
        if not chans:
            raise AdapterError("not_found", f"Kick: no channel '{ref.handle}'")
        c = chans[0]
        uid = c.get("broadcaster_user_id")
        rep.identity = {"id": uid, "slug": c.get("slug"), "url": f"https://kick.com/{c.get('slug', ref.handle)}",
                        "category": (c.get("category") or {}).get("name")}
        st = c.get("stream") or {}
        live = bool(st.get("is_live"))
        if uid is not None:
            ls = self._get(f"{self.API}/livestreams", params={"broadcaster_user_id": uid}, headers=h).json().get("data", [])
            if ls:
                live = True
                st = {**st, **ls[0]}
        rep.live = LiveStatus(live, title=c.get("stream_title") or st.get("stream_title", ""),
                              viewers=st.get("viewer_count"), started_at=st.get("start_time") or st.get("started_at", ""),
                              category=rep.identity.get("category") or "", stream_url=rep.identity["url"] if live else "")
        return rep


# ---- YouTube (Data API v3, official; API key) -------------------------------------------------

class YouTubeAdapter(Adapter):
    platform = "youtube"
    official_api = "YouTube Data API v3 (API key; free daily quota)"
    acquisition = {
        "acquire_vod": "yt-dlp (existing path) only for an authorised domain/recorded permission.",
        "acquire_live": "yt-dlp live capture only under a recorded permission.",
    }
    API = "https://www.googleapis.com/youtube/v3"

    def configured(self) -> bool:
        return bool(getattr(self.settings, "youtube_api_key", ""))

    def capabilities(self) -> dict:
        ok = self.configured()
        return {"identity": ok, "live_status": ok, "vods": ok, "clips": False, "schedule": ok, "stats": ok,
                "acquire_vod": "permission_required", "acquire_live": "permission_required",
                "state": "ready" if ok else "needs_credentials"}

    def identity(self, ref: ChannelRef) -> dict:
        """channels.list only (1 quota unit)."""
        if not self.configured():
            raise AdapterError("needs_credentials", "YouTube: add YOUTUBE_API_KEY (free, Google Cloud console)")
        params = {"part": "id,snippet", "key": self.settings.youtube_api_key}
        if ref.handle.startswith("@"):
            params["forHandle"] = ref.handle
        elif ref.handle.startswith("UC"):
            params["id"] = ref.handle
        else:
            params["forUsername"] = ref.handle
        items = self._get(f"{self.API}/channels", params=params).json().get("items", [])
        if not items:
            raise AdapterError("not_found", f"YouTube: no channel '{ref.handle}'")
        ch = items[0]
        return {"id": ch["id"], "name": ch["snippet"].get("title"), "url": f"https://www.youtube.com/channel/{ch['id']}",
                "handle": (ch["snippet"].get("customUrl") or ""), "platform_verified": False}

    def report(self, ref: ChannelRef, max_items: int = 15) -> Report:
        """Quota-frugal: channels.list (1 unit) + playlistItems.list (1) + videos.list (1); never search.list
        (100 units). Live and upcoming broadcasts come from each video's liveStreamingDetails."""
        if not self.configured():
            raise AdapterError("needs_credentials", "YouTube: add YOUTUBE_API_KEY (free, Google Cloud console)")
        key = self.settings.youtube_api_key
        rep = Report("youtube", self.capabilities(), source=self.official_api)
        params = {"part": "id,snippet,statistics,contentDetails", "key": key}
        if ref.handle.startswith("@"):
            params["forHandle"] = ref.handle
        elif ref.handle.startswith("UC"):
            params["id"] = ref.handle
        else:
            params["forUsername"] = ref.handle
        items = self._get(f"{self.API}/channels", params=params).json().get("items", [])
        if not items:
            raise AdapterError("not_found", f"YouTube: no channel '{ref.handle}'")
        ch = items[0]
        stats = ch.get("statistics", {})
        rep.identity = {"id": ch["id"], "name": ch["snippet"].get("title"), "url": f"https://www.youtube.com/channel/{ch['id']}",
                        "subscribers": None if stats.get("hiddenSubscriberCount") else _int(stats.get("subscriberCount")),
                        "videos": _int(stats.get("videoCount")), "views": _int(stats.get("viewCount"))}
        uploads = ch.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
        if not uploads:
            return rep
        pl = self._get(f"{self.API}/playlistItems", params={"part": "contentDetails", "playlistId": uploads,
                                                             "maxResults": max_items, "key": key}).json().get("items", [])
        ids = [i["contentDetails"]["videoId"] for i in pl]
        if not ids:
            rep.live = LiveStatus(False)
            return rep
        vids = self._get(f"{self.API}/videos", params={"part": "snippet,contentDetails,liveStreamingDetails,statistics",
                                                        "id": ",".join(ids), "key": key}).json().get("items", [])
        live = None
        for v in vids:
            sn, lsd = v.get("snippet", {}), v.get("liveStreamingDetails") or {}
            state = sn.get("liveBroadcastContent", "none")
            url = f"https://www.youtube.com/watch?v={v['id']}"
            kind = "live" if state == "live" else "upcoming" if state == "upcoming" else ("vod" if lsd else "upload")
            rep.items.append(MediaItem(key=f"youtube:{v['id']}", title=sn.get("title", ""), url=url, kind=kind,
                                       published=sn.get("publishedAt", "") if kind != "upcoming" else lsd.get("scheduledStartTime", ""),
                                       duration_s=parse_iso_duration(v.get("contentDetails", {}).get("duration", "")),
                                       views=_int(v.get("statistics", {}).get("viewCount"))))
            if state == "live" and live is None:
                live = LiveStatus(True, title=sn.get("title", ""), viewers=_int(lsd.get("concurrentViewers")),
                                  started_at=lsd.get("actualStartTime", ""), stream_url=url)
        rep.live = live or LiveStatus(False)
        return rep


def parse_iso_duration(s: str) -> float | None:
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s or "")
    if not m or not any(m.groups()):
        return None
    d, h, mi, se = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + se


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# ---- Rumble (channel page; no public API) -----------------------------------------------------

class RumbleAdapter(Adapter):
    platform = "rumble"
    official_api = "No public Rumble API; the channel page's embedded video grid (one request per check)"
    acquisition = {"acquire_vod": "yt-dlp, only under a recorded written permission for rumble.com (in place).",
                   "acquire_live": "yt-dlp live capture, only under a recorded permission (in place)."}

    def capabilities(self) -> dict:
        return {"identity": True, "live_status": True, "vods": True, "clips": False, "schedule": False,
                "stats": False, "acquire_vod": "permission_required", "acquire_live": "permission_required",
                "state": "ready"}

    def identity(self, ref: ChannelRef) -> dict:
        from . import monitor
        entries = monitor.list_channel(self.settings, ref.url)      # raises if the page is not a real channel
        return {"id": ref.handle, "name": ref.handle, "url": ref.url, "items_seen": len(entries),
                "platform_verified": False}

    def report(self, ref: ChannelRef) -> Report:
        from . import monitor
        rep = Report("rumble", self.capabilities(), source=self.official_api,
                     identity={"handle": ref.handle, "url": ref.url})
        entries = monitor.list_channel(self.settings, ref.url)
        live = next((e for e in entries if e.get("live")), None)
        rep.live = LiveStatus(bool(live), title=live["title"] if live else "", stream_url=live["url"] if live else "")
        rep.items = [MediaItem(key=f"rumble:{e['key']}", title=e["title"], url=e["url"],
                               kind="live" if e.get("live") else "vod", published=e.get("published", ""))
                     for e in entries]
        return rep


# ---- Podcast RSS/Atom feeds (lightweight; discovery only) ---------------------------------------

class PodcastAdapter(Adapter):
    """Reads a public podcast feed. Episodes keep their own guid, so identity survives republishing.
    The enclosure link is RECORDED but never fetched here: audio/video is only retrieved through the
    permission-checked acquisition path."""
    platform = "podcast"
    official_api = "Public RSS/Atom feed (no credentials)"
    acquisition = {"acquire_vod": "Enclosure links are stored, never downloaded automatically. Retrieval only "
                                  "under a recorded permission; protected or login-only media is never fetched."}
    LOOKUP = "https://itunes.apple.com/search"

    def capabilities(self) -> dict:
        return {"identity": True, "live_status": False, "vods": True, "clips": False, "schedule": False,
                "stats": False, "acquire_vod": "permission_required", "acquire_live": False, "state": "ready"}

    def find_feed(self, names: list[str]) -> dict:
        """Look a show up in Apple's public podcast directory and accept it ONLY on an exact (normalised)
        title match. Returns {feed_url, title, evidence}; raises not_found otherwise (never guesses)."""
        wanted = {_norm(n) for n in names}
        for term in names:
            data = self._get(self.LOOKUP, params={"media": "podcast", "entity": "podcast", "limit": 10,
                                                   "term": term}).json()
            for r in data.get("results", []):
                if _norm(r.get("collectionName", "")) in wanted and r.get("feedUrl"):
                    return {"feed_url": r["feedUrl"], "title": r["collectionName"], "id": str(r.get("collectionId")),
                            "evidence": f"Apple Podcasts directory exact title match '{r['collectionName']}' "
                                        f"(collection {r.get('collectionId')})"}
        raise AdapterError("not_found", "Podcast: no directory entry with an exact title match; supply the feed URL")

    def fetch_feed(self, url: str) -> tuple[dict, list[MediaItem]]:
        r = self._get(url)
        return parse_podcast_feed(r.text)

    def identity(self, ref: ChannelRef) -> dict:
        meta, items = self.fetch_feed(ref.url)
        return {"id": meta.get("guid") or ref.url, "name": meta.get("title"), "url": ref.url, "items_seen": len(items),
                "platform_verified": False}

    def report(self, ref: ChannelRef) -> Report:
        meta, items = self.fetch_feed(ref.url)
        return Report("podcast", self.capabilities(), source=self.official_api,
                      identity={"id": meta.get("guid") or ref.url, "name": meta.get("title"), "url": ref.url},
                      items=items, live=LiveStatus(False))


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def parse_podcast_feed(text: str) -> tuple[dict, list[MediaItem]]:
    """RSS 2.0 (with iTunes/enclosure) or Atom. Stable key = <guid>, else the enclosure/link URL."""
    import xml.etree.ElementTree as ET
    from email.utils import parsedate_to_datetime
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise AdapterError("inaccessible", f"Podcast: the feed is not valid XML ({e})") from e
    ns = {"a": "http://www.w3.org/2005/Atom", "it": "http://www.itunes.com/dtds/podcast-1.0.dtd"}
    meta, items = {}, []

    def iso(v: str) -> str:
        try:
            return parsedate_to_datetime(v).isoformat()
        except (TypeError, ValueError):
            return (v or "").strip()

    def dur(v: str):
        v = (v or "").strip()
        if not v:
            return None
        try:
            parts = [float(x) for x in v.split(":")]
        except ValueError:
            return None
        sec = 0.0
        for x in parts:
            sec = sec * 60 + x
        return sec
    ch = root.find("channel")
    if ch is not None:
        meta = {"title": (ch.findtext("title") or "").strip(), "guid": (ch.findtext("it:guid", namespaces=ns) or
                                                                       ch.findtext("link") or "").strip()}
        for it in ch.findall("item"):
            enc = it.find("enclosure")
            link = (enc.get("url") if enc is not None else "") or (it.findtext("link") or "")
            key = (it.findtext("guid") or link or it.findtext("title") or "").strip()
            if not key:
                continue
            items.append(MediaItem(key=f"podcast:{key}", title=(it.findtext("title") or "").strip(), url=link.strip(),
                                   kind="episode", published=iso(it.findtext("pubDate") or ""),
                                   duration_s=dur(it.findtext("it:duration", namespaces=ns))))
    else:
        meta = {"title": (root.findtext("a:title", namespaces=ns) or "").strip(), "guid": (root.findtext("a:id", namespaces=ns) or "").strip()}
        for en in root.findall("a:entry", ns):
            link = ""
            for l in en.findall("a:link", ns):
                if l.get("rel") == "enclosure":
                    link = l.get("href", "")
                    break
                link = link or l.get("href", "")
            key = (en.findtext("a:id", namespaces=ns) or link or "").strip()
            if key:
                items.append(MediaItem(key=f"podcast:{key}", title=(en.findtext("a:title", namespaces=ns) or "").strip(),
                                       url=link, kind="episode",
                                       published=(en.findtext("a:published", namespaces=ns) or en.findtext("a:updated", namespaces=ns) or "").strip()))
    if not meta.get("title") and not items:
        raise AdapterError("inaccessible", "Podcast: the document is not a podcast feed")
    return meta, items


ADAPTERS = {"twitch": TwitchAdapter, "kick": KickAdapter, "youtube": YouTubeAdapter, "rumble": RumbleAdapter,
            "podcast": PodcastAdapter}


def adapter_for(settings, platform: str, client=None) -> Adapter:
    cls = ADAPTERS.get(platform)
    if not cls:
        raise AdapterError("unsupported", f"No adapter for platform '{platform}'")
    return cls(settings, client)


def capability_matrix(settings) -> dict:
    """For the dashboard: what each platform can do on this installation, and how footage is obtained."""
    return {name: {"capabilities": cls(settings).capabilities(), "api": cls.official_api,
                   "acquisition": cls.acquisition} for name, cls in ADAPTERS.items()}
