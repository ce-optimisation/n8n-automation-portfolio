"""Diagnostics: `python -m clipengine --doctor [--selftest]`.

`--doctor` inspects the installation and prints, for every item, whether it is OK, needs
attention or is broken, with the exact fix. `--selftest` additionally proves the whole local
workflow on THIS computer with genuine speech: a bundled 32-second NASA briefing excerpt
(see assets/selftest/README.txt) is imported, transcribed with the real Whisper model, a clip
is selected and rendered at 1080×1920 with the approved Anton captions, and the result is
checked. The self-test uses its own temporary data folder and never touches the owner's
recordings; the rendered clip is copied to data/selftest/ so it can be watched.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .config import PROJECT_ROOT, Settings, get_settings

OK, WARN, FAIL = "ok", "attention", "problem"
SELFTEST_AUDIO = Path(__file__).resolve().parent / "assets" / "selftest" / "nasa_landsat9_excerpt.m4a"
SELFTEST_WORDS = ("swaths", "kilometers", "coverage", "satellites", "landsat", "days", "temperature", "calibrated")
SELFTEST_SOURCE = "https://images.nasa.gov/details/GSFC_20210831_Landsat_9_Briefing"


def _run(cmd: list[str], timeout: float = 30) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)


def checks(settings: Settings) -> list[dict]:
    """Read-only inspection. Each item: {key, title, level, detail, fix}."""
    from . import autostart, captions, hardware, power, tailscale
    from .setup_status import _model_downloaded
    out: list[dict] = []

    def add(key, title, level, detail, fix=""):
        out.append({"key": key, "title": title, "level": level, "detail": detail, "fix": fix})

    v = sys.version_info
    add("python", "Python", OK if (3, 10) <= v[:2] <= (3, 13) else FAIL, f"{v.major}.{v.minor}.{v.micro} ({sys.executable})",
        "" if (3, 10) <= v[:2] <= (3, 13) else "Install Python 3.12 and run the launcher again.")
    hw = hardware.detect()
    if hw.ffmpeg and hw.ffprobe:
        rc, text = _run([hw.ffmpeg, "-hide_banner", "-version"])
        ver = text.splitlines()[0] if text else "?"
        rc2, filters = _run([hw.ffmpeg, "-hide_banner", "-filters"])
        rc3, encoders = _run([hw.ffmpeg, "-hide_banner", "-encoders"])
        missing = [n for n, ok_ in (("libass subtitles filter", " ass " in filters or " subtitles " in filters),
                                    ("libx264 encoder", "libx264" in encoders), ("AAC encoder", " aac " in encoders),
                                    ("loudnorm filter", "loudnorm" in filters)) if not ok_]
        if missing:
            add("ffmpeg", "FFmpeg", FAIL, f"{ver} — missing: {', '.join(missing)}",
                "This FFmpeg build cannot burn captions. Install a full build: winget install Gyan.FFmpeg "
                "(or run scripts\\get_ffmpeg.py), then start the launcher again.")
        else:
            add("ffmpeg", "FFmpeg", OK, f"{ver} at {hw.ffmpeg}")
    else:
        add("ffmpeg", "FFmpeg", FAIL, "ffmpeg/ffprobe not found.",
            "Run Start-Clipping-Engine.cmd again and accept the FFmpeg installation, or run "
            "“.venv\\Scripts\\python scripts\\get_ffmpeg.py”.")
    missing_pkgs = []
    for mod, pkg in (("fastapi", "fastapi"), ("uvicorn", "uvicorn"), ("jinja2", "jinja2"), ("multipart", "python-multipart"),
                     ("faster_whisper", "faster-whisper"), ("httpx", "httpx"), ("yt_dlp", "yt-dlp"), ("keyring", "keyring")):
        try:
            __import__(mod)
        except ImportError:
            missing_pkgs.append(pkg)
    add("packages", "Python packages", FAIL if missing_pkgs else OK,
        "missing: " + ", ".join(missing_pkgs) if missing_pkgs else "all required packages import correctly",
        "Delete the .venv folder and run the launcher again." if missing_pkgs else "")
    model, device, ctype = hardware.choose_whisper(hw, settings.whisper_model, settings.whisper_device,
                                                   settings.whisper_compute_type, settings.language)
    gpu = f"GPU {hw.gpu_name or 'CUDA'} {hw.vram_gb} GB" if hw.cuda_devices else (
        f"NVIDIA GPU present ({hw.gpu_name}) but CUDA libraries are not installed" if hw.gpu_name else "no CUDA GPU")
    add("hardware", "Computer", OK, f"{hw.os}, {hw.cpu_cores} CPU threads, {hw.ram_gb} GB RAM, {gpu}",
        "Optional: set INSTALL_CUDA_LIBS=1 in .env for much faster transcription on the NVIDIA GPU."
        if (hw.gpu_name and not hw.cuda_devices) else "")
    if settings.transcriber_backend == "stub":
        add("model", "Speech model", FAIL, "TEST-ONLY stub transcriber is active.", "Remove CLIPENGINE_TRANSCRIBER from .env.")
    elif _model_downloaded(settings, model):
        add("model", "Speech model", OK, f"{model} on {device} ({ctype}) is downloaded")
    else:
        add("model", "Speech model", WARN, f"{model} on {device} ({ctype}) is not downloaded yet",
            "It downloads automatically on first use (needs internet once): run the setup assistant, or click "
            "Settings → Download Whisper model now.")
    try:
        settings.ensure_dirs()
        probe = settings.data_dir / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        free = shutil.disk_usage(settings.data_dir).free / 1e9
        lvl = OK if free >= 20 else WARN if free >= 5 else FAIL
        detail = f"{settings.data_dir} is writable; {free:.0f} GB free"
        fix = "" if lvl == OK else "Long recordings need space (about 1–2 GB per hour). Free some disk space, or set CLIPENGINE_DATA_DIR to a larger drive."
        if "onedrive" in str(settings.data_dir).lower():
            lvl, fix = WARN, "The data folder is inside OneDrive, which would upload every recording. Move the engine to a local folder such as %LOCALAPPDATA%\\ClippingEngine (the installer does this)."
        add("data", "Data folder", lvl, detail, fix)
    except OSError as e:
        add("data", "Data folder", FAIL, f"{settings.data_dir} is not writable: {e}", "Choose another folder with CLIPENGINE_DATA_DIR in .env.")
    try:
        from .db import Database
        db = Database(settings.db_path)
        n = db.one("SELECT COUNT(*) n FROM recordings")["n"]
        add("database", "Database", OK, f"{settings.db_path.name} opens; {n} recording(s)")
    except Exception as e:  # noqa: BLE001
        db = None
        add("database", "Database", FAIL, f"cannot open {settings.db_path}: {e}", "Restore the file from a backup or move it aside to start fresh.")
    if hw.ffmpeg:
        try:
            res = captions.verify_font(settings.work_dir / "fontcheck", captions.load_style(db))
            if res.get("ok"):
                add("font", "Caption font", OK, f"{res.get('postscript')} loaded from the bundled font file")
            else:
                add("font", "Caption font", FAIL, f"requested {res.get('requested')}, FFmpeg used {res.get('postscript') or 'nothing'}",
                    "The bundled Anton font was not picked up. Re-download the project (assets/fonts/Anton-Regular.ttf must exist) "
                    "and use a full FFmpeg build with libass.")
        except Exception as e:  # noqa: BLE001
            add("font", "Caption font", FAIL, f"font check could not run: {e}", "See the FFmpeg item above.")
    from .__main__ import is_our_engine, port_free
    if port_free(settings.host, settings.port):
        add("port", "Dashboard address", OK, f"{settings.base_url()} is free (the engine is not running right now)")
    elif is_our_engine(settings.port):
        add("port", "Dashboard address", OK, f"the engine is running at {settings.base_url()}")
    else:
        add("port", "Dashboard address", WARN, f"port {settings.port} is used by another program",
            "The engine will move to the next free port automatically; set CLIPENGINE_PORT in .env to pin one.")
    if settings.host not in ("127.0.0.1", "localhost", "::1") and not settings.dashboard_password:
        add("exposure", "Network exposure", FAIL, "CLIPENGINE_HOST opens the dashboard to the network without a password",
            "Set DASHBOARD_PASSWORD, or set CLIPENGINE_HOST=127.0.0.1 and use Tailscale for phone access.")
    ts = tailscale.status()
    if not ts["installed"]:
        add("tailscale", "Phone access (Tailscale)", WARN, "Tailscale is not installed on this PC",
            f"Optional. Install it ({tailscale.INSTALL_URL}), sign in, then Settings → Phone access → Set up.")
    elif not ts["running"]:
        add("tailscale", "Phone access (Tailscale)", WARN, f"Tailscale is installed but not connected ({ts['state']})",
            "Click the Tailscale icon near the clock and sign in, then Settings → Phone access → Set up.")
    else:
        sv = tailscale.serve_status(settings.port)
        if sv["funnel"]:
            add("tailscale", "Phone access (Tailscale)", FAIL, f"{sv['url']} is exposed publicly through Tailscale FUNNEL",
                "Turn Funnel off: tailscale funnel --https=443 off")
        elif sv["serving"] and not settings.dashboard_password:
            add("tailscale", "Phone access (Tailscale)", FAIL, f"published at {sv['url']} but DASHBOARD_PASSWORD is empty",
                "Settings → Phone access → Set up (creates a password).")
        elif sv["serving"]:
            match = settings.dashboard_url.rstrip("/") == sv["url"].rstrip("/")
            add("tailscale", "Phone access (Tailscale)", OK if match else WARN,
                f"private address {sv['url']}" + ("" if match else f" (DASHBOARD_URL is “{settings.dashboard_url or 'empty'}”)"),
                "" if match else "Settings → Phone access → Set up, so email links use this address.")
        else:
            add("tailscale", "Phone access (Tailscale)", WARN, f"signed in as {ts['dns_name']}, dashboard not published yet",
                "Settings → Phone access → Set up.")
    if autostart.supported():
        add("autostart", "Start at Windows sign-in", OK if autostart.is_installed() else WARN,
            "enabled" if autostart.is_installed() else "not enabled — the engine only runs after you start it",
            "" if autostart.is_installed() else "Settings → Start at sign-in, or double-click Enable-Autostart.cmd.")
    if power.supported():
        add("sleep", "Sleep", OK if settings.prevent_sleep != "off" else WARN,
            {"off": "PREVENT_SLEEP=off: Windows may sleep during a recording",
             "active": "the PC is kept awake while recording or processing; it may sleep when idle (live streams "
                       "that start while it sleeps are missed)",
             "monitoring": "the PC is kept awake whenever an enabled source is being watched"}.get(settings.prevent_sleep, settings.prevent_sleep),
            "" if settings.prevent_sleep == "monitoring" else
            "For unattended live monitoring choose “Keep awake while monitoring” in Settings → Running unattended.")
    if settings.notify_to:
        from . import notify
        if db is not None:
            add("notify", "Email notifications", OK if notify.is_approved(settings, db) else WARN,
                f"recipient set; {'approved for sending' if notify.is_approved(settings, db) else 'held until you approve recipient and wording'}",
                "" if notify.is_approved(settings, db) else "Settings → Notifications.")
    else:
        add("notify", "Email notifications", WARN, "no recipient set (emails are off)", "Optional: Settings → Notifications.")
    return out


def print_report(items: list[dict]) -> int:
    marks = {OK: "[ ok ]", WARN: "[ !! ]", FAIL: "[FAIL]"}
    for it in items:
        print(f"  {marks[it['level']]} {it['title']}: {it['detail']}")
        if it["fix"]:
            print(f"         → {it['fix']}")
    bad = sum(1 for it in items if it["level"] == FAIL)
    warn = sum(1 for it in items if it["level"] == WARN)
    print(f"\n  {len(items) - bad - warn} OK, {warn} need attention, {bad} problem(s).")
    return 1 if bad else 0


def make_selftest_video(out: Path) -> Path:
    """A real-speech test video: bundled NASA audio over FFmpeg's moving test picture."""
    from . import media
    out.parent.mkdir(parents=True, exist_ok=True)
    media.run_ffmpeg(["-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30", "-i", str(SELFTEST_AUDIO),
                      "-shortest", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                      "-c:a", "aac", "-b:a", "96k", str(out)])
    return out


def selftest(settings: Settings, keep_dir: Path | None = None, log=print) -> dict:
    """End-to-end functional check with genuine speech and the REAL Whisper model."""
    from . import media
    from .db import Database
    from .jobs import JobQueue
    from .pipeline import Pipeline
    rep: dict = {"ok": False, "steps": [], "started_at": time.time(), "source": SELFTEST_SOURCE}

    def step(name, ok, detail):
        rep["steps"].append({"name": name, "ok": bool(ok), "detail": detail})
        log(f"  {'[ ok ]' if ok else '[FAIL]'} {name}: {detail}")
        return ok
    if settings.transcriber_backend != "whisper":
        step("Real speech model", False, "the TEST-ONLY stub transcriber is configured; the self-test needs real Whisper")
        return rep
    tmp = settings.work_dir / f"selftest_{int(time.time())}"
    try:
        s2 = get_settings(llm_provider="none", open_browser=False)
        s2.data_dir = tmp / "data"
        s2.models_path = settings.models_dir           # share the downloaded model
        s2.env_file = tmp / "selftest.env"
        s2.inbox_dir = None
        s2.notify_enabled = False
        s2.transcriber_backend = "whisper"
        s2.ensure_dirs()
        t0 = time.time()
        video = make_selftest_video(tmp / "nasa_landsat9_selftest.mp4")
        info = media.probe(video)
        step("Test recording", info["has_video"] and info["has_audio"],
             f"{info['duration']:.0f}s of genuine speech (NASA Landsat 9 briefing excerpt) at {info['width']}x{info['height']}")
        db = Database(s2.db_path)
        q = JobQueue(db)
        p = Pipeline(s2, db, q)
        rid, dup, _ = p.import_upload(video, video.name, "Self-test: NASA Landsat 9 briefing excerpt", {},
                                      "NASA-produced material (NASA Media Usage Guidelines); self-test only")
        step("Import", rid and not dup, f"recording #{rid} registered with provenance")
        q.drain(1800)
        rec = db.recording(rid)
        segs = db.segments(rid)
        text = " ".join(x["text"] for x in segs).lower()
        hits = [w for w in SELFTEST_WORDS if w in text]
        proc = json.loads(rec["metadata_json"] or "{}").get("processing", {})
        rep["model"] = rec["whisper_model"]
        rep["transcribe_seconds"] = proc.get("transcribe_s")
        rep["speed_x_realtime"] = proc.get("speed_x_realtime")
        if not step("Real transcription", len(hits) >= 4,
                    f"{rec['whisper_model']}: recognised {len(hits)}/{len(SELFTEST_WORDS)} expected words "
                    f"({', '.join(hits)}) in {proc.get('transcribe_s', '?')}s "
                    f"({proc.get('speed_x_realtime', '?')}× real time)"):
            failed = db.one("SELECT error FROM jobs WHERE status='failed' ORDER BY id DESC LIMIT 1")
            rep["error"] = failed["error"] if failed else (rec["error"] or "transcript did not contain the expected words")
            return rep
        clips = db.query("SELECT * FROM clips WHERE recording_id=? ORDER BY start", (rid,))
        ready = [c for c in clips if c["status"] == "ready" and c["video_path"] and Path(c["video_path"]).is_file()]
        if not step("Clip selection and rendering", bool(ready), f"{len(ready)} of {len(clips)} clip(s) rendered"):
            failed = db.one("SELECT error FROM jobs WHERE status='failed' ORDER BY id DESC LIMIT 1")
            rep["error"] = failed["error"] if failed else "no clip was rendered"
            return rep
        c = ready[0]
        out = media.probe(Path(c["video_path"]))
        step("Output video", out["width"] == 1080 and out["height"] == 1920 and out["has_audio"],
             f"{out['width']}x{out['height']}, {out['duration']:.1f}s, video+audio")
        ass = Path(c["ass_path"]).read_text(encoding="utf-8") if c["ass_path"] else ""
        step("Captions", "Anton" in ass and "Dialogue:" in ass,
             f"{ass.count('Dialogue:')} caption line(s) in the Anton style; opening: “{(c['transcript_text'] or '')[:70]}…”")
        rep["clip"] = {"start": c["start"], "end": c["end"], "duration": round(c["end"] - c["start"], 1),
                       "text": c["transcript_text"]}
        keep = keep_dir or (settings.data_dir / "selftest")
        keep.mkdir(parents=True, exist_ok=True)
        dst = keep / "selftest_clip.mp4"
        shutil.copy2(c["video_path"], dst)
        if c["thumb_path"] and Path(c["thumb_path"]).is_file():
            shutil.copy2(c["thumb_path"], keep / "selftest_clip.jpg")
        rep["clip_file"] = str(dst)
        rep["seconds"] = round(time.time() - t0, 1)
        rep["ok"] = all(s_["ok"] for s_ in rep["steps"])
        (keep / "selftest_report.json").write_text(json.dumps(rep, indent=1), encoding="utf-8")
        return rep
    except Exception as e:  # noqa: BLE001 - report, never crash the assistant
        rep["error"] = f"{type(e).__name__}: {e}"
        step("Self-test", False, rep["error"])
        return rep
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(settings: Settings, run_selftest: bool = False) -> int:
    print("\n  Clipping Engine — diagnostics\n")
    rc = print_report(checks(settings))
    if run_selftest:
        print("\n  Functional self-test (genuine speech, real Whisper, real render) — this can take a few minutes "
              "the first time while the speech model downloads…\n")
        rep = selftest(settings)
        if rep["ok"]:
            print(f"\n  Self-test PASSED in {rep.get('seconds')}s. Watch the result: {rep.get('clip_file')}")
        else:
            print(f"\n  Self-test FAILED: {rep.get('error', 'see the steps above')}")
            print(f"  Log: {settings.data_dir / 'logs' / 'clipengine.jsonl'}")
            rc = 1
    print()
    return rc


if __name__ == "__main__":      # pragma: no cover
    sys.exit(main(get_settings(), "--selftest" in sys.argv))
