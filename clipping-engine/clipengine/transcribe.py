"""Local speech transcription with faster-whisper (CTranslate2).

Model weights are downloaded once from Hugging Face into data/models and are
then used fully offline.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Callable

from .config import Settings
from . import hardware

log = logging.getLogger("clipengine.transcribe")

_model_cache: dict[tuple, object] = {}


def _add_nvidia_dll_dirs() -> None:
    """On Windows, expose CUDA DLLs installed by the nvidia-* pip wheels to CTranslate2."""
    import os
    import sys
    if os.name != "nt":
        return
    for base in map(Path, sys.path):
        nv = base / "nvidia"
        if nv.is_dir():
            for bin_dir in nv.glob("*/bin"):
                os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")
                try:
                    os.add_dll_directory(str(bin_dir))
                except (OSError, AttributeError):
                    pass
_model_lock = threading.Lock()


class Transcriber:
    name = "base"
    model_label = "unknown"

    def transcribe(self, audio: Path, duration: float,
                   on_progress: Callable[[float], None] | None = None) -> tuple[list[dict], str]:
        raise NotImplementedError


class WhisperTranscriber(Transcriber):
    name = "whisper"

    def __init__(self, settings: Settings):
        self.settings = settings
        hw = hardware.detect()
        self.model_size, self.device, self.compute_type = hardware.choose_whisper(
            hw, settings.whisper_model, settings.whisper_device, settings.whisper_compute_type, settings.language)
        self.model_label = f"faster-whisper {self.model_size} ({self.device}/{self.compute_type})"

    def _load(self):
        _add_nvidia_dll_dirs()
        from faster_whisper import WhisperModel
        key = (self.model_size, self.device, self.compute_type)
        with _model_lock:
            if key not in _model_cache:
                log.info("Loading Whisper model %s", self.model_label)
                try:
                    _model_cache[key] = WhisperModel(self.model_size, device=self.device,
                                                     compute_type=self.compute_type,
                                                     download_root=str(self.settings.models_dir))
                except Exception as e:
                    if self.device == "cuda":
                        log.warning("CUDA load failed (%s); falling back to CPU int8", e)
                        self.device, self.compute_type = "cpu", "int8"
                        self.model_label = f"faster-whisper {self.model_size} (cpu/int8, GPU fallback)"
                        _model_cache[key] = WhisperModel(self.model_size, device="cpu", compute_type="int8",
                                                         download_root=str(self.settings.models_dir))
                    else:
                        raise
            return _model_cache[key]

    def transcribe(self, audio, duration, on_progress=None):
        try:
            return self._transcribe(audio, duration, on_progress)
        except RuntimeError as e:
            # Missing CUDA runtime DLLs (cuBLAS/cuDNN) surface only at inference time.
            if self.device != "cuda":
                raise
            log.warning("GPU transcription failed (%s); retrying on CPU", e)
            self.device, self.compute_type = "cpu", "int8"
            self.model_label = f"faster-whisper {self.model_size} (cpu/int8, GPU fallback)"
            return self._transcribe(audio, duration, on_progress)

    def _transcribe(self, audio, duration, on_progress=None):
        model = self._load()
        segments, info = model.transcribe(
            str(audio), language=self.settings.language or None, word_timestamps=True,
            vad_filter=self.settings.vad_filter, beam_size=5, condition_on_previous_text=False)
        out = []
        for seg in segments:  # generator: transcription happens while iterating
            words = [{"start": round(w.start, 3), "end": round(w.end, 3), "word": w.word,
                      "prob": round(w.probability, 3)} for w in (seg.words or [])]
            out.append({"start": round(seg.start, 3), "end": round(seg.end, 3), "text": seg.text.strip(),
                        "avg_logprob": seg.avg_logprob, "no_speech_prob": seg.no_speech_prob, "words": words})
            if on_progress and duration:
                on_progress(min(1.0, seg.end / duration))
        return out, info.language


class StubTranscriber(Transcriber):
    """TEST-ONLY transcriber. Reads a sidecar '<audio>.stub.json' transcript.

    It never produces speech that is not supplied by a test fixture and is
    only selectable with CLIPENGINE_TRANSCRIBER=stub.
    """
    name = "stub"
    model_label = "TEST STUB (not real transcription)"

    def __init__(self, settings: Settings, fixture: Path | None = None):
        self.fixture = fixture

    def transcribe(self, audio, duration, on_progress=None):
        import json
        import os
        fixture = self.fixture or Path(os.environ["CLIPENGINE_STUB_TRANSCRIPT"])
        data = json.loads(Path(fixture).read_text(encoding="utf-8"))
        if on_progress:
            on_progress(1.0)
        return data["segments"], data.get("language", "en")


def get_transcriber(settings: Settings) -> Transcriber:
    if settings.transcriber_backend == "stub":
        return StubTranscriber(settings)
    return WhisperTranscriber(settings)
