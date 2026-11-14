"""Application settings.

Settings are read from environment variables, optionally loaded from a local
``.env`` file in the project root. No third-party dotenv dependency is used.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path | None = None) -> None:
    """Load KEY=VALUE pairs from a .env file without overriding real env vars."""
    path = path or PROJECT_ROOT / ".env"
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

    # Candidate selection
    clips_per_recording: int = 6
    clip_min_seconds: float = 30.0
    clip_max_seconds: float = 90.0
    clip_target_seconds: float = 55.0
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
    allow_private_urls: bool = False     # TEST-ONLY: permits localhost/private URLs

    # Jobs / retention
    max_attempts: int = 3
    delete_rejected_renders_after_days: int = 14
    delete_originals_after_days: int = 0     # 0 = never delete originals
    storage_warning_gb: float = 50.0

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
        return self.data_dir / "models"

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
        clip_min_seconds=_float("CLIP_MIN_SECONDS", 30.0),
        clip_max_seconds=_float("CLIP_MAX_SECONDS", 90.0),
        clip_target_seconds=_float("CLIP_TARGET_SECONDS", 55.0),
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
        allow_private_urls=_bool("ALLOW_PRIVATE_URLS", False),
        max_attempts=_int("JOB_MAX_ATTEMPTS", 3),
        delete_rejected_renders_after_days=_int("DELETE_REJECTED_RENDERS_AFTER_DAYS", 14),
        delete_originals_after_days=_int("DELETE_ORIGINALS_AFTER_DAYS", 0),
        storage_warning_gb=_float("STORAGE_WARNING_GB", 50.0),
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
        transcriber_backend=os.environ.get("CLIPENGINE_TRANSCRIBER", "whisper"),
    )
    for k, v in overrides.items():
        setattr(s, k, v)
    return s
