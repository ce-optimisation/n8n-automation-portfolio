"""Application settings.

Settings are read from environment variables, optionally loaded from a local
``.env`` file in the project root. No third-party dotenv dependency is used.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def env_file_path() -> Path:
    """The local settings file. CLIPENGINE_ENV_FILE overrides it (used by tests)."""
    return Path(os.environ.get("CLIPENGINE_ENV_FILE") or PROJECT_ROOT / ".env")


# Settings that the dashboard and the setup assistant may change without the owner editing .env by
# hand: .env key → (Settings attribute, converter). Anything else still needs a deliberate edit.
EDITABLE = {
    "DASHBOARD_PASSWORD": ("dashboard_password", str),
    "DASHBOARD_URL": ("dashboard_url", lambda v: v.strip().rstrip("/")),
    "NOTIFY_TO": ("notify_to", lambda v: v.strip()),
    "NOTIFY_TRANSPORT": ("notify_transport", lambda v: (v or "graph").strip().lower()),
    "OUTLOOK_CLIENT_ID": ("outlook_client_id", lambda v: v.strip()),
    "SMTP_HOST": ("smtp_host", lambda v: v.strip()),
    "SMTP_PORT": ("smtp_port", lambda v: int(v or 587)),
    "SMTP_USER": ("smtp_user", lambda v: v.strip()),
    "SMTP_FROM": ("smtp_from", lambda v: v.strip()),
    "SMTP_SSL": ("smtp_ssl", lambda v: str(v).strip().lower() in {"1", "true", "yes", "on"}),
    "WHISPER_MODEL": ("whisper_model", lambda v: (v or "auto").strip()),
    "PREVENT_SLEEP": ("prevent_sleep", lambda v: (v or "active").strip().lower()),
    "ALLOWED_URL_DOMAINS": ("allowed_url_domains", lambda v: [x.strip().lower() for x in v.split(",") if x.strip()]),
    # Client IDs and the YouTube API key are not passwords; client SECRETS still go in .env by hand.
    "TWITCH_CLIENT_ID": ("twitch_client_id", lambda v: v.strip()),
    "KICK_CLIENT_ID": ("kick_client_id", lambda v: v.strip()),
    "YOUTUBE_API_KEY": ("youtube_api_key", lambda v: v.strip()),
    "CLIPENGINE_GOOGLE_CLIENT_ID": ("google_client_id", lambda v: v.strip()),
    "RAW_DELETE_AFTER_CLOUD": ("raw_delete_after_cloud", lambda v: str(v).strip().lower() in {"1", "true", "yes", "on"}),
    "REJECTED_CLIP_HOURS": ("rejected_clip_hours", lambda v: float(v or 24)),
    "PUBLISHED_CLIP_DAYS": ("published_clip_days", lambda v: float(v or 7)),
    "DEFER_HEAVY_WHILE_RECORDING": ("defer_heavy_while_recording",
                                    lambda v: str(v).strip().lower() in {"1", "true", "yes", "on"}),
    "CLOUD_MIN_FREE_GB": ("cloud_min_free_gb", lambda v: float(v or 2)),
}


def update_env_file(values: dict[str, str], path: Path | None = None) -> Path:
    """Set KEY=VALUE lines in the local .env, keeping every other line and comment as it is.
    Creates the file from .env.example when it does not exist yet. Never touches other keys."""
    path = path or env_file_path()
    for k, v in values.items():
        if not k.replace("_", "").isalnum() or "\n" in str(v) or "\r" in str(v):
            raise ValueError(f"Refusing to write an unsafe setting: {k}")
    if path.is_file():
        lines = path.read_text(encoding="utf-8").splitlines()
    else:
        example = PROJECT_ROOT / ".env.example"
        lines = example.read_text(encoding="utf-8").splitlines() if example.is_file() else []
    todo = {k: str(v) for k, v in values.items()}
    out = []
    for raw in lines:
        key = raw.split("=", 1)[0].strip().lstrip("#").strip() if "=" in raw else ""
        active = "=" in raw and not raw.lstrip().startswith("#")
        if active and key in todo:
            out.append(f"{key}={todo.pop(key)}")
        else:
            out.append(raw)
    if todo:
        if out and out[-1].strip():
            out.append("")
        out.append("# --- Set by the setup assistant / dashboard --------------------------------")
        out += [f"{k}={v}" for k, v in todo.items()]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def apply_settings(settings: "Settings", values: dict[str, str], persist: bool = True) -> list[str]:
    """Change editable settings now (running app) and in .env (next start). Returns the keys set."""
    done = []
    for k, v in values.items():
        if k not in EDITABLE:
            raise ValueError(f"{k} cannot be changed from here; edit .env instead.")
        attr, conv = EDITABLE[k]
        setattr(settings, attr, conv(v))
        os.environ[k] = str(v)
        done.append(k)
    if persist and done:
        update_env_file({k: values[k] for k in done}, settings.env_file)
    return done


def load_dotenv(path: Path | None = None) -> None:
    """Load KEY=VALUE pairs from a .env file without overriding real env vars."""
    path = path or env_file_path()
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    v = os.environ.get(name, "").strip()
    return int(v) if v else default


def _float(name: str, default: float) -> float:
    v = os.environ.get(name, "").strip()
    return float(v) if v else default


def _list(name: str, default: str = "") -> list[str]:
    v = os.environ.get(name, default)
    return [x.strip().lower() for x in v.split(",") if x.strip()]


@dataclass
class Settings:
    data_dir: Path
    host: str = "127.0.0.1"
    port: int = 8765
    dashboard_password: str = ""
    open_browser: bool = True

    # Transcription
    whisper_model: str = "auto"          # auto | tiny | base | small | medium | large-v3 | *.en
    whisper_device: str = "auto"         # auto | cpu | cuda
    whisper_compute_type: str = "auto"
    language: str = ""                   # "" = auto-detect, e.g. "en"
    vad_filter: bool = True
    whisper_cpu_threads: int = 0         # 0 = automatic
    whisper_beam_size: int = 5
    transcribe_chunk_seconds: int = 1200 # long recordings are transcribed in checkpointed chunks
    whisper_prompt: str = ""             # per-recording vocabulary hint (set from creator/niche profiles)

    # Candidate selection
    clips_per_recording: int = 6
    clip_min_seconds: float = 15.0
    clip_max_seconds: float = 150.0
    clip_target_seconds: float = 0.0     # 0 = none. Only used by legacy profiles v1/v2.
    selection_profile: str = "v6"        # v6 = context-complete (default since session 9); v4/v5 and v1–v3 kept
    context_seconds: float = 25.0
    llm_provider: str = "auto"           # auto | ollama | none
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "llama3.2:3b"

    # Rendering
    out_width: int = 1080
    out_height: int = 1920
    video_crf: int = 21
    x264_preset: str = "veryfast"
    audio_bitrate: str = "160k"
    caption_font: str = "Arial"
    caption_font_size: int = 64

    # Ingestion
    allowed_url_domains: list[str] = field(default_factory=list)
    max_upload_mb: int = 8192
    inbox_dir: Path | None = None        # watch folder; default data/inbox
    monitor_enabled: bool = True         # poll enabled sources (sources are individually enabled/authorised)
    max_recording_hours: float = 8.0
    live_segment_max_gb: float = 1.0     # a recorder is rotated to a new file once its segment reaches this size
    live_segment_max_minutes: float = 30.0
    live_buffer_gb: float = 20.0         # total local space live captures may hold (all concurrent captures together)
    min_recording_seconds: float = 60.0  # shorter live captures are treated as incomplete
    # Session 13: AUTOMATICALLY found videos shorter than this are skipped (they are already clips, e.g. YouTube Shorts).
    # 0 = off. Videos the owner imports by hand are never skipped.
    min_source_seconds: float = 0.0
    # Session 13: phone alerts (ntfy) and approve -> auto-upload to the closest DRAFT workflow (never public).
    ntfy_topic: str = ""                 # private topic name; acts like a password for the alert feed (alerts carry no titles)
    ntfy_server: str = "https://ntfy.sh"
    push_min_gap_minutes: float = 10.0   # batch new clips into one alert at most this often (5+ clips alert at once)
    push_quiet_hours: str = ""           # e.g. "23:00-07:00": no alerts then; they are sent when quiet hours end
    delivery_daily_cap: int = 6          # max automatic uploads per channel per local day; the rest wait for the next day
    live_restart_limit: int = 120        # restarts happen only while the video page confirms the stream is live
    allow_private_urls: bool = False     # TEST-ONLY: permits localhost/private URLs
    http_user_agent: str = "LocalClippingEngine/1.1 (self-hosted editorial review tool)"

    # Jobs / retention
    max_attempts: int = 3
    delete_rejected_renders_after_days: int = 14
    delete_originals_after_days: int = 0     # 0 = never delete originals
    storage_warning_gb: float = 50.0
    # Keep Windows awake: off | active (while recording or processing — default) | monitoring
    # (whenever an enabled source is being watched). Per-process request; no system setting is changed.
    prevent_sleep: str = "active"
    env_file: Path | None = None             # the local .env this instance reads and writes
    models_path: Path | None = None          # share downloaded speech models with another data folder

    # Publishing (client credentials only; user tokens go to the secure store)
    tiktok_client_key: str = ""
    tiktok_client_secret: str = ""
    tiktok_redirect_uri: str = ""
    tiktok_app_type: str = "desktop"     # desktop (PKCE, hex S256) | web (no PKCE)
    meta_app_id: str = ""
    meta_app_secret: str = ""
    meta_redirect_uri: str = ""
    meta_graph_version: str = "v23.0"
    instagram_auth_mode: str = "facebook"   # facebook (Facebook Login for Business) | instagram (Instagram Login)
    instagram_app_id: str = ""
    instagram_app_secret: str = ""
    instagram_redirect_uri: str = ""
    public_media_base_url: str = ""         # https base reachable by Meta, required for instagram mode
    # Session 13: the Instagram API cannot create drafts; media_publish is PUBLIC. Off unless the owner opts in.
    instagram_public_publish: bool = False
    title_semantic: bool = False            # Session 13: local semantic reranking of VERIFIED title candidates (off by default)

    # Email notifications (nothing is ever sent until the owner approves recipient + template in Settings)
    notify_enabled: bool = True
    notify_to: str = ""
    notify_transport: str = "graph"      # graph (Microsoft Graph, Outlook.com) | smtp
    notify_on_failure: bool = True
    notify_inline_thumbnails: bool = True
    dashboard_url: str = ""              # link base used in emails (e.g. your Tailscale https address)
    outlook_client_id: str = ""
    outlook_tenant: str = "consumers"    # consumers = personal Microsoft accounts (outlook.com/hotmail)
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_ssl: bool = False

    # Source platforms (free developer registrations; discovery only — never retrieval rights)
    twitch_client_id: str = ""
    twitch_client_secret: str = ""
    kick_client_id: str = ""
    kick_client_secret: str = ""
    youtube_api_key: str = ""

    # Google (Drive storage and YouTube private uploads): the owner's own OAuth "Desktop app" client
    google_client_id: str = ""
    google_client_secret: str = ""
    # Session 13: a SEPARATE Google OAuth client (own project/consent screen) used only for the six YouTube channels.
    # Falls back to the Drive client if unset.
    youtube_oauth_client_id: str = ""
    youtube_oauth_client_secret: str = ""
    cloud_provider: str = "gdrive"          # gdrive | none
    cloud_folder_name: str = "Clipping Engine"
    cloud_min_free_gb: float = 2.0          # hold uploads (and warn) below this much free Drive space

    # Lifecycle and retention
    raw_delete_after_cloud: bool = True     # delete ENGINE-OWNED raw recordings once every gate passes
    rejected_clip_hours: float = 24.0
    published_clip_days: float = 7.0
    keep_drive_master: bool = True          # retention never deletes the Google Drive copy of a kept/published clip
    cleanup_grace_hours: float = 24.0       # default recovery window before a verified local clip is removed

    # Queue and storage
    max_concurrent_jobs: int = 1            # heavy jobs run one at a time (only 1 is supported)
    defer_heavy_while_recording: bool = True  # hold transcription/rendering while a live recording runs
    disk_reserve_gb: float = 1.0
    record_gb_per_hour: float = 1.5         # planning estimate for live capture
    live_expected_hours: float = 4.5        # plan live resolution so a broadcast this long fits on disk
    live_min_height: int = 1080             # owner requirement (16 Nov 2026): never record or download below 1080p
    live_max_height: int = 1080

    # Testing hook: name of a transcriber backend ("whisper" or "stub").
    transcriber_backend: str = "whisper"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "clipengine.sqlite3"

    @property
    def recordings_dir(self) -> Path:
        return self.data_dir / "recordings"

    @property
    def clips_dir(self) -> Path:
        return self.data_dir / "clips"

    @property
    def work_dir(self) -> Path:
        return self.data_dir / "work"

    @property
    def models_dir(self) -> Path:
        return Path(self.models_path) if self.models_path else self.data_dir / "models"

    @property
    def secrets_dir(self) -> Path:
        return self.data_dir / "secrets"

    @property
    def inbox(self) -> Path:
        return Path(self.inbox_dir) if self.inbox_dir else self.data_dir / "inbox"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.recordings_dir, self.clips_dir, self.work_dir,
                  self.models_dir, self.secrets_dir, self.data_dir / "logs", self.inbox):
            d.mkdir(parents=True, exist_ok=True)

    def base_url(self) -> str:
        host = "127.0.0.1" if self.host in ("0.0.0.0", "::") else self.host
        return f"http://{host}:{self.port}"


def get_settings(**overrides) -> Settings:
    load_dotenv()
    data_dir = Path(os.environ.get("CLIPENGINE_DATA_DIR", PROJECT_ROOT / "data")).resolve()
    s = Settings(
        data_dir=data_dir,
        host=os.environ.get("CLIPENGINE_HOST", "127.0.0.1"),
        port=_int("CLIPENGINE_PORT", 8765),
        dashboard_password=os.environ.get("DASHBOARD_PASSWORD", ""),
        open_browser=_bool("OPEN_BROWSER", True),
        whisper_model=os.environ.get("WHISPER_MODEL", "auto") or "auto",
        whisper_device=os.environ.get("WHISPER_DEVICE", "auto") or "auto",
        whisper_compute_type=os.environ.get("WHISPER_COMPUTE_TYPE", "auto") or "auto",
        language=os.environ.get("TRANSCRIBE_LANGUAGE", ""),
        vad_filter=_bool("VAD_FILTER", True),
        whisper_cpu_threads=_int("WHISPER_CPU_THREADS", 0),
        whisper_beam_size=_int("WHISPER_BEAM_SIZE", 5),
        transcribe_chunk_seconds=_int("TRANSCRIBE_CHUNK_SECONDS", 1200),
        clips_per_recording=_int("CLIPS_PER_RECORDING", 6),
        clip_min_seconds=_float("CLIP_MIN_SECONDS", 15.0),
        clip_max_seconds=_float("CLIP_MAX_SECONDS", 150.0),
        clip_target_seconds=_float("CLIP_TARGET_SECONDS", 0.0),
        selection_profile=(os.environ.get("SELECTION_PROFILE", "v6") or "v6").lower(),
        context_seconds=_float("CONTEXT_SECONDS", 25.0),
        llm_provider=(os.environ.get("LLM_PROVIDER", "auto") or "auto").lower(),
        ollama_url=os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434"),
        ollama_model=os.environ.get("OLLAMA_MODEL", "llama3.2:3b"),
        video_crf=_int("VIDEO_CRF", 21),
        x264_preset=os.environ.get("X264_PRESET", "veryfast"),
        caption_font=os.environ.get("CAPTION_FONT", "Arial"),
        caption_font_size=_int("CAPTION_FONT_SIZE", 64),
        allowed_url_domains=_list("ALLOWED_URL_DOMAINS", ""),
        max_upload_mb=_int("MAX_UPLOAD_MB", 8192),
        inbox_dir=Path(os.environ["INBOX_DIR"]) if os.environ.get("INBOX_DIR") else None,
        monitor_enabled=_bool("MONITOR_ENABLED", True),
        max_recording_hours=_float("MAX_RECORDING_HOURS", 8.0),
        min_recording_seconds=_float("MIN_RECORDING_SECONDS", 60.0),
        min_source_seconds=_float("MIN_SOURCE_SECONDS", 0.0),
        ntfy_topic=os.environ.get("NTFY_TOPIC", "").strip(),
        ntfy_server=(os.environ.get("NTFY_SERVER", "https://ntfy.sh") or "https://ntfy.sh").strip().rstrip("/"),
        push_min_gap_minutes=_float("PUSH_MIN_GAP_MINUTES", 10.0),
        push_quiet_hours=os.environ.get("PUSH_QUIET_HOURS", "").strip(),
        delivery_daily_cap=max(1, _int("DELIVERY_DAILY_CAP", 6)),
        live_segment_max_gb=_float("LIVE_SEGMENT_MAX_GB", 1.0),
        live_segment_max_minutes=_float("LIVE_SEGMENT_MAX_MINUTES", 30.0),
        live_buffer_gb=_float("LIVE_BUFFER_GB", 20.0),
        live_restart_limit=_int("LIVE_RESTART_LIMIT", 120),
        allow_private_urls=_bool("ALLOW_PRIVATE_URLS", False),
        http_user_agent=os.environ.get("HTTP_USER_AGENT") or "LocalClippingEngine/1.1 (self-hosted editorial review tool)",
        max_attempts=_int("JOB_MAX_ATTEMPTS", 3),
        delete_rejected_renders_after_days=_int("DELETE_REJECTED_RENDERS_AFTER_DAYS", 14),
        delete_originals_after_days=_int("DELETE_ORIGINALS_AFTER_DAYS", 0),
        storage_warning_gb=_float("STORAGE_WARNING_GB", 50.0),
        prevent_sleep=(os.environ.get("PREVENT_SLEEP", "active") or "active").lower(),
        env_file=env_file_path(),
        tiktok_client_key=os.environ.get("TIKTOK_CLIENT_KEY", ""),
        tiktok_client_secret=os.environ.get("TIKTOK_CLIENT_SECRET", ""),
        tiktok_redirect_uri=os.environ.get("TIKTOK_REDIRECT_URI", ""),
        tiktok_app_type=(os.environ.get("TIKTOK_APP_TYPE", "desktop") or "desktop").lower(),
        meta_app_id=os.environ.get("META_APP_ID", ""),
        meta_app_secret=os.environ.get("META_APP_SECRET", ""),
        meta_redirect_uri=os.environ.get("META_REDIRECT_URI", ""),
        meta_graph_version=os.environ.get("META_GRAPH_VERSION", "v23.0"),
        instagram_auth_mode=(os.environ.get("INSTAGRAM_AUTH_MODE", "facebook") or "facebook").lower(),
        instagram_app_id=os.environ.get("INSTAGRAM_APP_ID", ""),
        instagram_app_secret=os.environ.get("INSTAGRAM_APP_SECRET", ""),
        instagram_redirect_uri=os.environ.get("INSTAGRAM_REDIRECT_URI", ""),
        public_media_base_url=os.environ.get("PUBLIC_MEDIA_BASE_URL", "").rstrip("/"),
        instagram_public_publish=_bool("INSTAGRAM_PUBLIC_PUBLISH", False),
        title_semantic=_bool("TITLE_SEMANTIC", False),
        transcriber_backend=os.environ.get("CLIPENGINE_TRANSCRIBER", "whisper"),
        notify_enabled=_bool("NOTIFY_ENABLED", True),
        notify_to=os.environ.get("NOTIFY_TO", "").strip(),
        notify_transport=(os.environ.get("NOTIFY_TRANSPORT", "graph") or "graph").lower(),
        notify_on_failure=_bool("NOTIFY_ON_FAILURE", True),
        notify_inline_thumbnails=_bool("NOTIFY_INLINE_THUMBNAILS", True),
        dashboard_url=os.environ.get("DASHBOARD_URL", "").rstrip("/"),
        outlook_client_id=os.environ.get("OUTLOOK_CLIENT_ID", "").strip(),
        outlook_tenant=os.environ.get("OUTLOOK_TENANT", "consumers").strip() or "consumers",
        smtp_host=os.environ.get("SMTP_HOST", ""),
        smtp_port=_int("SMTP_PORT", 587),
        smtp_user=os.environ.get("SMTP_USER", ""),
        smtp_password=os.environ.get("SMTP_PASSWORD", ""),
        smtp_from=os.environ.get("SMTP_FROM", ""),
        smtp_ssl=_bool("SMTP_SSL", False),
        twitch_client_id=os.environ.get("TWITCH_CLIENT_ID", "").strip(),
        twitch_client_secret=os.environ.get("TWITCH_CLIENT_SECRET", "").strip(),
        kick_client_id=os.environ.get("KICK_CLIENT_ID", "").strip(),
        kick_client_secret=os.environ.get("KICK_CLIENT_SECRET", "").strip(),
        youtube_api_key=os.environ.get("YOUTUBE_API_KEY", "").strip(),
        google_client_id=os.environ.get("CLIPENGINE_GOOGLE_CLIENT_ID", "").strip(),
        google_client_secret=os.environ.get("CLIPENGINE_GOOGLE_CLIENT_SECRET", "").strip(),
        youtube_oauth_client_id=os.environ.get("YOUTUBE_OAUTH_CLIENT_ID", "").strip(),
        youtube_oauth_client_secret=os.environ.get("YOUTUBE_OAUTH_CLIENT_SECRET", "").strip(),
        cloud_provider=(os.environ.get("CLOUD_PROVIDER", "gdrive") or "gdrive").lower(),
        cloud_folder_name=os.environ.get("CLOUD_FOLDER_NAME", "Clipping Engine") or "Clipping Engine",
        cloud_min_free_gb=_float("CLOUD_MIN_FREE_GB", 2.0),
        raw_delete_after_cloud=_bool("RAW_DELETE_AFTER_CLOUD", True),
        rejected_clip_hours=_float("REJECTED_CLIP_HOURS", 24.0),
        published_clip_days=_float("PUBLISHED_CLIP_DAYS", 7.0),
        keep_drive_master=_bool("KEEP_DRIVE_MASTER", True),
        cleanup_grace_hours=_float("LOCAL_CLEANUP_GRACE_HOURS", 24.0),
        max_concurrent_jobs=max(1, min(1, _int("MAX_CONCURRENT_JOBS", 1))),
        defer_heavy_while_recording=_bool("DEFER_HEAVY_WHILE_RECORDING", True),
        disk_reserve_gb=_float("DISK_RESERVE_GB", 1.0),
        record_gb_per_hour=_float("RECORD_GB_PER_HOUR", 1.5),
        live_expected_hours=_float("LIVE_EXPECTED_HOURS", 4.5),
        live_min_height=_int("LIVE_MIN_HEIGHT", 1080),
        live_max_height=_int("LIVE_MAX_HEIGHT", 1080),
    )
    for k, v in overrides.items():
        setattr(s, k, v)
    return s
