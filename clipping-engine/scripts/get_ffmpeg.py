"""Download a portable FFmpeg for Windows into tools/ffmpeg (no installer, no administrator rights).

Used by the launcher when FFmpeg is missing and winget is unavailable or fails. The build is the
widely used "BtbN" GPL build published on GitHub (includes libx264, libass and loudnorm, which
the engine needs). The archive is checked to contain ffmpeg.exe and ffprobe.exe, extracted to
tools/ffmpeg/bin, and the result is executed once to prove it runs and can burn captions.

    .venv\\Scripts\\python scripts\\get_ffmpeg.py
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEST = ROOT / "tools" / "ffmpeg" / "bin"
URLS = [
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip",
    "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
]


def download(url: str, dst: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "LocalClippingEngine-setup"})
    with urllib.request.urlopen(req, timeout=60) as r, open(dst, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done, last = 0, -1
        while chunk := r.read(1024 * 1024):
            f.write(chunk)
            done += len(chunk)
            pct = int(done * 100 / total) if total else -1
            if pct // 10 != last:
                last = pct // 10
                print(f"    {done / 1e6:.0f} MB" + (f" ({pct}%)" if total else ""), flush=True)


def install_from(zip_path: Path) -> None:
    with zipfile.ZipFile(zip_path) as z:
        wanted = {}
        for name in z.namelist():
            base = name.replace("\\", "/").rsplit("/", 1)[-1].lower()
            if base in ("ffmpeg.exe", "ffprobe.exe") and "/bin/" in "/" + name.replace("\\", "/"):
                wanted[base] = name
        if set(wanted) != {"ffmpeg.exe", "ffprobe.exe"}:
            raise RuntimeError("the archive does not contain bin/ffmpeg.exe and bin/ffprobe.exe")
        DEST.mkdir(parents=True, exist_ok=True)
        for base, name in wanted.items():
            with z.open(name) as src, open(DEST / (base + ".tmp"), "wb") as out:
                shutil.copyfileobj(src, out)
            (DEST / (base + ".tmp")).replace(DEST / base)


def works() -> tuple[bool, str]:
    exe = DEST / "ffmpeg.exe"
    if not exe.exists() or not (DEST / "ffprobe.exe").exists():
        return False, "not installed"
    try:
        v = subprocess.run([str(exe), "-hide_banner", "-version"], capture_output=True, text=True, timeout=30)
        f = subprocess.run([str(exe), "-hide_banner", "-filters"], capture_output=True, text=True, timeout=30)
        e = subprocess.run([str(exe), "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as ex:
        return False, str(ex)
    if v.returncode != 0:
        return False, "ffmpeg.exe does not run on this computer"
    missing = [n for n, ok in (("libass", " ass " in f.stdout), ("libx264", "libx264" in e.stdout)) if not ok]
    if missing:
        return False, "build lacks " + ", ".join(missing)
    return True, v.stdout.splitlines()[0]


def main() -> int:
    ok, detail = works()
    if ok:
        print(f"  FFmpeg already present: {detail}")
        return 0
    if sys.platform != "win32" and "--force" not in sys.argv:
        print("  This helper is for Windows. On macOS/Linux install FFmpeg with your package manager.")
        return 1
    errors = []
    for url in URLS:
        print(f"  Downloading FFmpeg from {url.split('/')[2]} (about 100–200 MB) ...", flush=True)
        try:
            with tempfile.TemporaryDirectory() as td:
                z = Path(td) / "ffmpeg.zip"
                download(url, z)
                install_from(z)
            ok, detail = works()
            if ok:
                print(f"  [ok] {detail}\n       installed in {DEST}")
                return 0
            errors.append(f"{url}: {detail}")
        except Exception as e:  # noqa: BLE001
            errors.append(f"{url}: {type(e).__name__}: {e}")
    print("\n  FFmpeg could not be downloaded automatically:")
    for e in errors:
        print("   - " + e)
    print("  Check the internet connection, or extract an FFmpeg build so that\n"
          f"  {DEST / 'ffmpeg.exe'} exists, then start the launcher again.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
