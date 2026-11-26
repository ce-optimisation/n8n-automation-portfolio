"""Customer install compatibility gate built on ``clipengine.hardware``.

Returns pass / warn / fail per check with a plain-English remedy, plus the Whisper
model the engine would choose, so first-run onboarding can stop early on
unsupported machines instead of failing during a live capture.
"""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import sys
from pathlib import Path

from clipengine.hardware import HardwareInfo, choose_whisper, detect

MIN = {"cores": 4, "ram_gb": 7.5, "free_gb": 20.0}  # "8 GB" machines report ~7.8 GB usable
REC = {"cores": 8, "ram_gb": 16.0, "free_gb": 100.0}
PY_SUPPORTED = ((3, 10), (3, 13))


def evaluate(hw: HardwareInfo, *, free_gb: float, python: tuple[int, int], windows_build: int | None) -> dict:
    checks = []

    def add(name, status, detail, remedy=""):
        checks.append({"check": name, "status": status, "detail": detail, "remedy": remedy})

    if windows_build is None:
        add("os", "warn", hw.os, "Only Windows 10/11 is supported for customer installs.")
    elif windows_build < 19041:
        add("os", "fail", f"Windows build {windows_build}", "Update to Windows 10 2004 (build 19041) or later.")
    else:
        add("os", "pass", f"Windows build {windows_build}")
    lo, hi = PY_SUPPORTED
    add("python", "pass" if lo <= python <= hi else "fail", f"{python[0]}.{python[1]}",
        "" if lo <= python <= hi else "Install Python 3.10–3.13 (the installer can provide one).")
    for key, label, unit in (("cores", "cpu_cores", ""), ("ram_gb", "ram", " GB")):
        value = hw.cpu_cores if key == "cores" else hw.ram_gb
        status = "fail" if value < MIN[key] else "warn" if value < REC[key] else "pass"
        add(label, status, f"{value}{unit}",
            {"fail": f"At least {MIN[key]}{unit} is required.",
             "warn": f"Works, but transcription is slower below {REC[key]}{unit}."}.get(status, ""))
    status = "fail" if free_gb < MIN["free_gb"] else "warn" if free_gb < REC["free_gb"] else "pass"
    add("storage", status, f"{free_gb:.1f} GB free",
        {"fail": f"Free at least {MIN['free_gb']:.0f} GB; a 3-hour stream can need 5–10 GB.",
         "warn": "Live recordings fill disks quickly; keep 100 GB free or set a separate data drive."}.get(status, ""))
    add("ffmpeg", "pass" if hw.ffmpeg and hw.ffprobe else "fail", "found" if hw.ffmpeg else "missing",
        "" if hw.ffmpeg and hw.ffprobe else "Install FFmpeg: winget install Gyan.FFmpeg, then restart.")
    add("gpu", "pass" if hw.cuda_devices else "warn", hw.gpu_name or "no CUDA GPU",
        "" if hw.cuda_devices else "CPU transcription is supported; a CUDA GPU makes it much faster.")
    model, device, compute = choose_whisper(hw)
    overall = "fail" if any(c["status"] == "fail" for c in checks) else \
              "warn" if any(c["status"] == "warn" for c in checks) else "pass"
    return {"overall": overall, "checks": checks,
            "transcription": {"model": model, "device": device, "compute_type": compute}}


def main() -> int:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--data-dir", type=Path, default=Path.cwd())
    a = cli.parse_args()
    build = None
    if platform.system() == "Windows":
        try:
            build = int(platform.version().split(".")[-1])
        except ValueError:
            build = None
    target = a.data_dir if a.data_dir.exists() else Path.cwd()
    result = evaluate(detect(), free_gb=shutil.disk_usage(target).free / 1e9,
                      python=sys.version_info[:2], windows_build=build)
    print(json.dumps(result, indent=2))
    return 2 if result["overall"] == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
