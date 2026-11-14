import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from clipengine.config import get_settings  # noqa: E402

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")


def stub_transcript_from_timing(timing_path: Path) -> dict:
    """TEST-ONLY: build a word-timed transcript from the sample generator's exact
    paragraph timings (words spread proportionally to their length). Used where the
    real Whisper model cannot be downloaded; never used by the application itself."""
    data = json.loads(Path(timing_path).read_text(encoding="utf-8"))
    segments = []
    for p in data["paragraphs"]:
        toks = p["text"].split()
        total = sum(len(t) + 1 for t in toks)
        t = p["start"]
        span = p["end"] - p["start"]
        words = []
        for tok in toks:
            d = span * (len(tok) + 1) / total
            words.append({"start": round(t, 3), "end": round(t + d * 0.92, 3), "word": " " + tok, "prob": 0.93})
            t += d
        segments.append({"start": p["start"], "end": p["end"], "text": p["text"], "avg_logprob": -0.2,
                         "no_speech_prob": 0.01, "words": words})
    return {"language": "en", "segments": segments}


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
    s = get_settings(llm_provider="none", open_browser=False)
    s.data_dir = tmp_path / "data"
    s.allowed_url_domains = []
    s.ensure_dirs()
    return s


@pytest.fixture
def stub_settings(settings, stub_transcript, monkeypatch):
    """Settings using the TEST-ONLY stub transcriber (explicitly labelled as such)."""
    monkeypatch.setenv("CLIPENGINE_STUB_TRANSCRIPT", str(stub_transcript))
    settings.transcriber_backend = "stub"
    return settings
