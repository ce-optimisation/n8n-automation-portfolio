"""Local speech transcription with faster-whisper (CTranslate2).

Model weights are downloaded once from Hugging Face into data/models and are
then used fully offline.
"""
from __future__ import annotations

import logging
import threading
import time
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


SAMPLE_RATE = 16000


def _check_wav(w) -> None:
    if w.getframerate() != SAMPLE_RATE or w.getnchannels() != 1 or w.getsampwidth() != 2:
        raise ValueError(f"expected 16 kHz mono 16-bit WAV, got {w.getframerate()} Hz/"
                         f"{w.getnchannels()} ch/{w.getsampwidth() * 8} bit")


def wav_duration(path: Path) -> float:
    import wave
    with wave.open(str(path), "rb") as w:
        _check_wav(w)
        return w.getnframes() / SAMPLE_RATE


def read_wav_range(path: Path, start: float = 0.0, end: float | None = None):
    """Read [start, end) seconds of the 16 kHz mono PCM WAV produced by media.extract_audio
    as float32 samples. Only that range is loaded, so memory stays bounded for long recordings.

    Passing samples directly also avoids faster-whisper's PyAV decoding path, whose
    keyword arguments are not stable across PyAV releases.
    """
    import wave

    import numpy as np
    with wave.open(str(path), "rb") as w:
        _check_wav(w)
        n = w.getnframes()
        a = max(0, min(n, int(round(start * SAMPLE_RATE))))
        b = n if end is None else max(a, min(n, int(round(end * SAMPLE_RATE))))
        w.setpos(a)
        data = w.readframes(b - a)
    return np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0


def load_wav_16k(path: Path):
    return read_wav_range(path)


def plan_chunks(path: Path, chunk_seconds: float, search_seconds: float = 20.0) -> list[tuple[float, float]]:
    """Split a recording into ~chunk_seconds pieces, cutting at the quietest 0.25 s
    frame within +/- search_seconds of each target so words are not split."""
    import numpy as np
    total = wav_duration(path)
    if chunk_seconds <= 0 or total <= chunk_seconds * 1.25:
        return [(0.0, round(total, 3))]
    cuts = [0.0]
    t = chunk_seconds
    while total - t > chunk_seconds * 0.25:
        a, b = max(cuts[-1] + 1.0, t - search_seconds), min(total, t + search_seconds)
        x = read_wav_range(path, a, b)
        frame = SAMPLE_RATE // 4
        nfr = len(x) // frame
        if nfr >= 1:
            energy = (x[: nfr * frame].reshape(nfr, frame) ** 2).mean(axis=1)
            cut = a + (int(np.argmin(energy)) + 0.5) * 0.25
        else:
            cut = t
        cuts.append(round(cut, 3))
        t = cut + chunk_seconds
    cuts.append(round(total, 3))
    return list(zip(cuts[:-1], cuts[1:]))


class Transcriber:
    name = "base"
    model_label = "unknown"

    def prepare(self) -> float:
        """Load models; return seconds spent."""
        return 0.0

    def transcribe_range(self, audio: Path, start: float, end: float, language: str | None = None,
                         on_progress: Callable[[float], None] | None = None) -> tuple[list[dict], str]:
        raise NotImplementedError

    def transcribe(self, audio: Path, duration: float,
                   on_progress: Callable[[float], None] | None = None) -> tuple[list[dict], str]:
        return self.transcribe_range(audio, 0.0, duration, None, on_progress)


def auto_cpu_threads() -> int:
    import os
    n = os.cpu_count() or 4
    return max(1, n // 2 if n >= 8 else n)   # logical CPUs >= 8 usually means hyper-threading


class WhisperTranscriber(Transcriber):
    name = "whisper"

    def __init__(self, settings: Settings):
        self.settings = settings
        hw = hardware.detect()
        self.model_size, self.device, self.compute_type = hardware.choose_whisper(
            hw, settings.whisper_model, settings.whisper_device, settings.whisper_compute_type, settings.language)
        self.cpu_threads = settings.whisper_cpu_threads or auto_cpu_threads()
        self.model_label = f"faster-whisper {self.model_size} ({self.device}/{self.compute_type})"

    def _load(self):
        _add_nvidia_dll_dirs()
        from faster_whisper import WhisperModel
        key = (self.model_size, self.device, self.compute_type)
        with _model_lock:
            if key not in _model_cache:
                log.info("Loading Whisper model %s", self.model_label)
                kw = dict(download_root=str(self.settings.models_dir), cpu_threads=self.cpu_threads)
                try:
                    _model_cache[key] = WhisperModel(self.model_size, device=self.device,
                                                     compute_type=self.compute_type, **kw)
                except Exception as e:
                    if self.device == "cuda":
                        log.warning("CUDA load failed (%s); falling back to CPU int8", e)
                        self._to_cpu()
                        _model_cache[key] = WhisperModel(self.model_size, device="cpu", compute_type="int8", **kw)
                    else:
                        raise
            return _model_cache[key]

    def _to_cpu(self):
        self.device, self.compute_type = "cpu", "int8"
        self.model_label = f"faster-whisper {self.model_size} (cpu/int8, GPU fallback)"

    def prepare(self) -> float:
        t = time.time()
        self._load()
        return time.time() - t

    def transcribe_range(self, audio, start, end, language=None, on_progress=None):
        try:
            return self._transcribe_range(audio, start, end, language, on_progress)
        except RuntimeError as e:
            # Missing CUDA runtime DLLs (cuBLAS/cuDNN) surface only at inference time.
            if self.device != "cuda":
                raise
            log.warning("GPU transcription failed (%s); retrying on CPU", e)
            key = (self.model_size, self.device, self.compute_type)
            self._to_cpu()
            with _model_lock:
                _model_cache.pop(key, None)
            return self._transcribe_range(audio, start, end, language, on_progress)

    def _transcribe_range(self, audio, start, end, language, on_progress):
        model = self._load()
        samples = read_wav_range(audio, start, end)
        segments, info = model.transcribe(
            samples, language=language or self.settings.language or None, word_timestamps=True,
            vad_filter=self.settings.vad_filter, beam_size=self.settings.whisper_beam_size,
            condition_on_previous_text=False)
        out = []
        span = max(1e-6, end - start)
        for seg in segments:  # generator: transcription happens while iterating
            words = [{"start": round(w.start + start, 3), "end": round(w.end + start, 3), "word": w.word,
                      "prob": round(w.probability, 3)} for w in (seg.words or [])]
            out.append({"start": round(seg.start + start, 3), "end": round(seg.end + start, 3),
                        "text": seg.text.strip(), "avg_logprob": seg.avg_logprob,
                        "no_speech_prob": seg.no_speech_prob, "words": words})
            if on_progress:
                on_progress(min(1.0, seg.end / span))
        return out, info.language


class StubTranscriber(Transcriber):
    """TEST-ONLY transcriber. Returns segments from a JSON fixture
    (CLIPENGINE_STUB_TRANSCRIPT) that fall inside the requested range.

    It never produces speech that is not supplied by a test fixture and is
    only selectable with CLIPENGINE_TRANSCRIBER=stub.
    """
    name = "stub"
    model_label = "TEST STUB (not real transcription)"

    def __init__(self, settings: Settings, fixture: Path | None = None):
        self.fixture = fixture

    def transcribe_range(self, audio, start, end, language=None, on_progress=None):
        import json
        import os
        fixture = self.fixture or Path(os.environ["CLIPENGINE_STUB_TRANSCRIPT"])
        data = json.loads(Path(fixture).read_text(encoding="utf-8"))
        segs = [sg for sg in data["segments"] if start <= sg["start"] < end]
        if on_progress:
            on_progress(1.0)
        return segs, data.get("language", "en")


def get_transcriber(settings: Settings) -> Transcriber:
    if settings.transcriber_backend == "stub":
        return StubTranscriber(settings)
    return WhisperTranscriber(settings)
