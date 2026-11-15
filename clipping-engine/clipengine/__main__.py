"""Entry point: ``python -m clipengine`` (used by Start-Clipping-Engine.cmd).

Flags:
  --check       validate dependencies and exit (0 = ready)
  --no-browser  do not open the dashboard automatically
  --doctor      diagnostic report (add --selftest for a real end-to-end run with genuine speech)
  --setup       guided first-time setup (add --yes to accept defaults)
"""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import shutil
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

from .config import PROJECT_ROOT, get_settings


class JsonFormatter(logging.Formatter):
    def format(self, record):
        d = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created)), "level": record.levelname,
             "logger": record.name, "msg": record.getMessage()}
        if record.exc_info:
            d["exc"] = self.formatException(record.exc_info)
        return json.dumps(d)


def setup_logging(data_dir: Path) -> None:
    (data_dir / "logs").mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = logging.handlers.RotatingFileHandler(data_dir / "logs" / "clipengine.jsonl", maxBytes=5_000_000,
                                              backupCount=5, encoding="utf-8")
    fh.setFormatter(JsonFormatter())
    sh = logging.StreamHandler()
    sh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(fh)
    root.addHandler(sh)


def add_bundled_ffmpeg_to_path() -> None:
    cands = [PROJECT_ROOT / "tools" / "ffmpeg" / "bin", PROJECT_ROOT / "tools" / "ffmpeg"]
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        cands.append(Path(os.environ["LOCALAPPDATA"]) / "Microsoft" / "WinGet" / "Links")  # winget installs
    for cand in cands:
        if (cand / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")).exists():
            os.environ["PATH"] = str(cand) + os.pathsep + os.environ.get("PATH", "")
            return


def check(settings) -> list[str]:
    problems = []
    if sys.version_info < (3, 10):
        problems.append(f"Python 3.10+ required (found {sys.version.split()[0]}).")
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            problems.append(f"{tool} not found. Windows: run 'winget install Gyan.FFmpeg' then reopen the launcher, "
                            f"or extract an FFmpeg build into clipping-engine\\tools\\ffmpeg\\bin.")
    for mod, pkg in (("fastapi", "fastapi"), ("uvicorn", "uvicorn"), ("jinja2", "jinja2"),
                     ("multipart", "python-multipart"), ("faster_whisper", "faster-whisper"), ("httpx", "httpx")):
        try:
            __import__(mod)
        except ImportError:
            problems.append(f"Python package '{pkg}' missing: run pip install -r requirements.txt")
    if settings.host not in ("127.0.0.1", "localhost", "::1") and not settings.dashboard_password:
        problems.append("CLIPENGINE_HOST exposes the dashboard to your network but DASHBOARD_PASSWORD is empty. "
                        "Set a password in .env.")
    try:
        settings.ensure_dirs()
        probe = settings.data_dir / ".write_test"
        probe.write_text("ok")
        probe.unlink()
    except OSError as e:
        problems.append(f"Data directory {settings.data_dir} is not writable: {e}")
    return problems


def port_free(host: str, port: int) -> bool:
    """True unless something is already listening on the port."""
    target = "127.0.0.1" if host in ("0.0.0.0", "localhost") else host
    with socket.socket() as s:
        s.settimeout(1.0)
        return s.connect_ex((target, port)) != 0


def is_our_engine(port: int) -> bool:
    try:
        import httpx
        r = httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=2)
        return r.status_code == 200 and r.json().get("app") == "clipengine"
    except Exception:  # noqa: BLE001
        return False


def hardware_report(settings) -> list[str]:
    from . import hardware
    from .transcribe import auto_cpu_threads
    hw = hardware.detect()
    model, device, ctype = hardware.choose_whisper(hw, settings.whisper_model, settings.whisper_device,
                                                   settings.whisper_compute_type, settings.language)
    lines = [f"Computer: {hw.os}, {hw.cpu_cores} CPU threads, {hw.ram_gb} GB RAM, "
             + (f"GPU {hw.gpu_name or 'CUDA'} {hw.vram_gb} GB" if hw.cuda_devices else "no CUDA GPU detected"),
             f"Speech model: {model} on {device} ({ctype}), {settings.whisper_cpu_threads or auto_cpu_threads()} threads"]
    if not hw.cuda_devices and hw.gpu_name:
        lines.append("Tip: an NVIDIA GPU is present but not usable yet — set INSTALL_CUDA_LIBS=1 in .env and "
                     "restart the launcher for much faster transcription.")
    if hw.ram_gb and hw.ram_gb < 6:
        lines.append("Tip: less than 6 GB RAM — WHISPER_MODEL=tiny.en or base.en is recommended.")
    if "onedrive" in str(settings.data_dir).lower():
        lines.append("Warning: the data folder is inside OneDrive. Large videos will be uploaded to the cloud "
                     "by OneDrive; set CLIPENGINE_DATA_DIR to a local folder (e.g. C:\\ClippingEngineData).")
    return lines


def supervise(child_args: list[str], max_restarts_per_10min: int = 5, sleep=time.sleep, cmd_factory=None) -> int:
    """Keep the server running: restart it after a crash (non-zero exit), with back-off.
    A clean exit (0), Ctrl+C, or a configuration problem (exit 2) ends supervision."""
    import subprocess
    crashes: list[float] = []
    opened_browser = "--no-browser" in child_args
    while True:
        args = list(child_args)
        if opened_browser and "--no-browser" not in args:
            args.append("--no-browser")
        cmd = cmd_factory(args) if cmd_factory else [sys.executable, "-m", "clipengine", *args]
        proc = subprocess.Popen(cmd)
        opened_browser = True
        try:
            rc = proc.wait()
        except KeyboardInterrupt:
            proc.terminate()
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
            return 0
        if rc in (0, 2, 3):
            return rc
        now = time.time()
        crashes = [t for t in crashes if now - t < 600] + [now]
        delay = 5 if len(crashes) < max_restarts_per_10min else 600
        print(f"\n  The engine stopped unexpectedly (exit code {rc}). Restarting in {delay}s "
              f"(jobs resume automatically). Log: data/logs/clipengine.jsonl\n")
        sleep(delay)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="clipengine")
    ap.add_argument("--check", action="store_true", help="validate dependencies and exit")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--download-model", action="store_true", help="download the Whisper model now and exit")
    ap.add_argument("--supervise", action="store_true",
                    help="run the server in a child process and restart it automatically if it crashes")
    ap.add_argument("--autostart", choices=["on", "off"], help="add/remove start at Windows sign-in and exit")
    ap.add_argument("--doctor", action="store_true", help="print a diagnostic report and exit")
    ap.add_argument("--selftest", action="store_true",
                    help="with --doctor: also run the real end-to-end self-test (genuine speech, real Whisper)")
    ap.add_argument("--setup", action="store_true", help="run the setup assistant, then exit")
    ap.add_argument("--yes", action="store_true", help="with --setup: accept the defaults without asking")
    args = ap.parse_args(argv)
    if args.autostart:
        from . import autostart
        try:
            if args.autostart == "on":
                print(f"Start at sign-in enabled: {autostart.install()}")
            else:
                autostart.remove()
                print("Start at sign-in disabled.")
            return 0
        except autostart.AutostartUnsupported as e:
            print(e)
            return 2
    if args.supervise:
        return supervise([a for a in (argv if argv is not None else sys.argv[1:]) if a != "--supervise"])
    add_bundled_ffmpeg_to_path()
    settings = get_settings()
    if args.doctor:
        from . import doctor
        return doctor.main(settings, run_selftest=args.selftest)
    if args.setup:
        from . import setup_wizard
        return setup_wizard.main(settings, assume_yes=args.yes or os.environ.get("CLIPENGINE_NONINTERACTIVE") == "1")
    problems = check(settings)
    if problems:
        print("\nThe Clipping Engine cannot start yet:\n")
        for p in problems:
            print("  - " + p)
        return 2
    if args.check:
        from . import hardware
        hw = hardware.detect()
        print("All dependencies present.")
        for ln in hardware_report(settings):
            print("  " + ln)
        print(json.dumps({"hardware": hw.as_dict(), "whisper": hardware.choose_whisper(
            hw, settings.whisper_model, settings.whisper_device, settings.whisper_compute_type,
            settings.language)}, indent=2))
        return 0
    if args.download_model:
        from .transcribe import WhisperTranscriber
        t = WhisperTranscriber(settings)
        print(f"Downloading/loading {t.model_label} into {settings.models_dir} ...")
        print(f"Ready in {t.prepare():.0f}s")
        return 0
    setup_logging(settings.data_dir)
    if not port_free(settings.host, settings.port):
        if is_our_engine(settings.port):
            print(f"The Clipping Engine is already running. Opening {settings.base_url()}")
            if settings.open_browser and not args.no_browser:
                webbrowser.open(settings.base_url())
            return 0
        busy = settings.port
        for cand in range(busy + 1, busy + 20):
            if port_free(settings.host, cand):
                settings.port = cand
                break
        else:
            print(f"Port {busy} and the next 19 ports are in use by other programs. Set CLIPENGINE_PORT in .env.")
            return 3
        print(f"Port {busy} is used by another program; using port {settings.port} instead. "
              f"(TikTok/Instagram sign-in redirect addresses use port {busy}; set CLIPENGINE_PORT to a free port "
              "and update them if you rely on publishing.)")
    url = settings.base_url()
    import uvicorn
    from .web.app import create_app
    app = create_app(settings)
    if settings.open_browser and not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    print(f"\n  Clipping Engine running at {url}")
    for ln in hardware_report(settings):
        print("  " + ln)
    if settings.host == "0.0.0.0":
        try:
            lan = socket.gethostbyname(socket.gethostname())
            print(f"  On your phone (same network): http://{lan}:{settings.port}")
        except OSError:
            pass
    print(f"  Inbox folder (drop videos here to import): {settings.inbox}")
    print(f"  Logs: {settings.data_dir / 'logs' / 'clipengine.jsonl'}")
    print("  Close this window or press Ctrl+C to stop. Jobs resume automatically next time.\n")
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
