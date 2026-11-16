import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from clipengine.config import get_settings  # noqa: E402
from clipengine.testsupport import stub_transcript_from_timing  # noqa: E402,F401

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")


@pytest.fixture(scope="session")
def sample_video(tmp_path_factory) -> Path:
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    from make_sample_video import make
    out = tmp_path_factory.mktemp("sample") / "sample_council_briefing.mp4"
    make(out)
    return out


@pytest.fixture(scope="session")
def stub_transcript(sample_video, tmp_path_factory) -> Path:
    f = tmp_path_factory.mktemp("stub") / "stub_transcript.json"
    f.write_text(json.dumps(stub_transcript_from_timing(sample_video.with_suffix(".timing.json"))))
    return f


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPENGINE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CLIPENGINE_ENV_FILE", str(tmp_path / "test.env"))   # never touch the owner's .env
    s = get_settings(llm_provider="none", open_browser=False)
    s.data_dir = tmp_path / "data"
    s.allowed_url_domains = []
    # Test media are tiny: plan with tiny figures so tests never depend on the host's free disk space
    # (the planning logic itself is tested with explicit values in test_expansion.py).
    s.record_gb_per_hour = 0.001
    s.disk_reserve_gb = 0.0
    # Never pick up real platform credentials from the machine running the tests.
    for attr in ("twitch_client_id", "twitch_client_secret", "kick_client_id", "kick_client_secret",
                 "youtube_api_key", "google_client_id", "google_client_secret"):
        setattr(s, attr, "")
    s.ensure_dirs()
    return s


@pytest.fixture
def stub_settings(settings, stub_transcript, monkeypatch):
    """Settings using the TEST-ONLY stub transcriber (explicitly labelled as such)."""
    monkeypatch.setenv("CLIPENGINE_STUB_TRANSCRIPT", str(stub_transcript))
    settings.transcriber_backend = "stub"
    return settings
