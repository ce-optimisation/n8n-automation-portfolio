"""REAL transcription test with faster-whisper.

Requires the model to be downloadable (Hugging Face) or already cached.
Enabled with CLIPENGINE_REAL_WHISPER=1 (set in CI); skipped otherwise so the
offline suite never reports a transcription test it did not run.
"""
import os
import shutil

import pytest

from clipengine import media, transcribe

pytestmark = pytest.mark.skipif(os.environ.get("CLIPENGINE_REAL_WHISPER") != "1",
                                reason="set CLIPENGINE_REAL_WHISPER=1 to run real Whisper transcription")


def test_real_whisper_transcribes_sample(settings, sample_video):
    settings.whisper_model = os.environ.get("WHISPER_MODEL", "base.en")
    settings.language = "en"
    wav = media.extract_audio(sample_video, settings.work_dir / "a.wav")
    t = transcribe.WhisperTranscriber(settings)
    segs, lang = t.transcribe(wav, media.probe(sample_video)["duration"])
    text = " ".join(s["text"] for s in segs).lower()
    assert lang == "en"
    assert sum(w in text for w in ("council", "bridge", "ferry", "consultation", "library")) >= 3, text
    assert all(s["words"] for s in segs) and all(0 <= w["prob"] <= 1 for s in segs for w in s["words"])
