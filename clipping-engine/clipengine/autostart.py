"""Optional start at Windows sign-in, using the per-user Startup folder (no administrator rights,
no Task Scheduler policy issues). The entry launches Start-Clipping-Engine.cmd minimised with
--no-browser --supervise, so the engine (and its crash supervisor) runs in the background.

CLIPENGINE_STARTUP_DIR overrides the folder (used by tests and for non-standard setups).
"""
from __future__ import annotations

import os
from pathlib import Path

from .config import PROJECT_ROOT

ENTRY_NAME = "ClippingEngine.cmd"


class AutostartUnsupported(Exception):
    pass


def startup_dir() -> Path | None:
    if os.environ.get("CLIPENGINE_STARTUP_DIR"):
        return Path(os.environ["CLIPENGINE_STARTUP_DIR"])
    if os.name == "nt" and os.environ.get("APPDATA"):
        return Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    return None


def supported() -> bool:
    return startup_dir() is not None


def entry_path() -> Path | None:
    d = startup_dir()
    return d / ENTRY_NAME if d else None


def is_installed() -> bool:
    p = entry_path()
    return bool(p and p.exists())


def install() -> Path:
    p = entry_path()
    if not p:
        raise AutostartUnsupported("Automatic start at sign-in is available on Windows only.")
    p.parent.mkdir(parents=True, exist_ok=True)
    launcher = PROJECT_ROOT / "Start-Clipping-Engine.cmd"
    content = ("@echo off\r\n"
               "rem Created by the Local Clipping Engine (Settings → Start at sign-in). Delete to disable.\r\n"
               f'start "Clipping Engine" /min cmd /c ""{launcher}" --no-browser --supervise"\r\n')
    p.write_text(content, encoding="utf-8", newline="")
    return p


def remove() -> None:
    p = entry_path()
    if p and p.exists():
        p.unlink()
