"""Install/refresh Python dependencies only when requirements.txt changes.

Run with the virtual-environment interpreter (the launcher does this).
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REQ = ROOT / "requirements.txt"
STAMP = ROOT / ".venv" / ".requirements.sha256"


def main() -> int:
    sys.path.insert(0, str(ROOT))
    from clipengine.config import load_dotenv
    load_dotenv()
    want = hashlib.sha256(REQ.read_bytes()).hexdigest()
    cuda = os.environ.get("INSTALL_CUDA_LIBS", "0") == "1" and shutil.which("nvidia-smi")
    want += "+cuda" if cuda else ""
    if STAMP.is_file() and STAMP.read_text().strip() == want:
        return 0
    print("Installing Python packages (first run can take a few minutes)...", flush=True)
    pip = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check"]
    subprocess.run(pip + ["--upgrade", "pip"], check=False)
    rc = subprocess.run(pip + ["-r", str(REQ)]).returncode
    if rc == 0 and cuda:
        print("Installing NVIDIA CUDA 12 runtime libraries for GPU transcription...", flush=True)
        rc = subprocess.run(pip + ["nvidia-cublas-cu12", "nvidia-cudnn-cu12==9.*"]).returncode
    if rc != 0:
        print("\nPackage installation failed. Check your internet connection and try again.")
        return rc
    STAMP.write_text(want)
    return 0


if __name__ == "__main__":
    sys.exit(main())
