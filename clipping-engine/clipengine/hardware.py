"""Hardware inspection and automatic Whisper model selection."""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
from dataclasses import dataclass, asdict


@dataclass
class HardwareInfo:
    os: str
    cpu: str
    cpu_cores: int
    ram_gb: float
    cuda_devices: int
    gpu_name: str
    vram_gb: float
    ffmpeg: str | None
    ffprobe: str | None

    def as_dict(self) -> dict:
        return asdict(self)


def _ram_gb() -> float:
    try:
        if os.name == "nt":
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("sullAvailExtendedVirtual", ctypes.c_ulonglong)]
            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
            return stat.ullTotalPhys / 1024**3
        if hasattr(os, "sysconf") and "SC_PHYS_PAGES" in os.sysconf_names:
            return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1024**3
    except Exception:
        pass
    return 0.0


def _gpu() -> tuple[int, str, float]:
    count = 0
    try:
        import ctranslate2
        count = ctranslate2.get_cuda_device_count()
    except Exception:
        count = 0
    name, vram = "", 0.0
    smi = shutil.which("nvidia-smi")
    if smi:
        try:
            out = subprocess.run([smi, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                                 capture_output=True, text=True, timeout=10).stdout.strip().splitlines()
            if out:
                n, mem = out[0].rsplit(",", 1)
                name, vram = n.strip(), float(mem) / 1024
        except Exception:
            pass
    return count, name, vram


def detect() -> HardwareInfo:
    cuda, gname, vram = _gpu()
    return HardwareInfo(
        os=f"{platform.system()} {platform.release()}",
        cpu=platform.processor() or platform.machine(),
        cpu_cores=os.cpu_count() or 1,
        ram_gb=round(_ram_gb(), 1),
        cuda_devices=cuda,
        gpu_name=gname,
        vram_gb=round(vram, 1),
        ffmpeg=shutil.which("ffmpeg"),
        ffprobe=shutil.which("ffprobe"),
    )


def choose_whisper(hw: HardwareInfo, model: str = "auto", device: str = "auto",
                   compute_type: str = "auto", language: str = "") -> tuple[str, str, str]:
    """Return (model_size, device, compute_type) suited to the hardware.

    Defaults favour reasonable accuracy while keeping a long recording's
    transcription time practical on the available hardware.
    """
    if device == "auto":
        device = "cuda" if hw.cuda_devices > 0 else "cpu"
    if model == "auto":
        if device == "cuda":
            vram = hw.vram_gb or 4.0
            model = "large-v3" if vram >= 10 else "medium" if vram >= 5 else "small"
        else:
            if hw.cpu_cores >= 8 and hw.ram_gb >= 15:
                model = "small"
            elif hw.cpu_cores >= 4 and hw.ram_gb >= 7:
                model = "base"
            else:
                model = "tiny"
        if language == "en" and model in ("tiny", "base", "small", "medium"):
            model += ".en"
    if compute_type == "auto":
        compute_type = "float16" if device == "cuda" else "int8"
    return model, device, compute_type
