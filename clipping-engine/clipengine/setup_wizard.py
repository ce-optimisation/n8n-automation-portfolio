"""Guided first-time setup: `python -m clipengine --setup` (the launcher runs it on first start).

Every step inspects the real state of the computer first, so the assistant can be closed at any
point (for example while the owner signs in to Tailscale) and run again: finished steps are
recognised and skipped. Nothing here needs administrator rights, sends email, publishes
anything, or enables a source. `--yes` / CLIPENGINE_NONINTERACTIVE=1 accepts the safe defaults
(prepare the speech model, run the self-test, skip optional choices).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time

from .config import Settings, apply_settings

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class Console:
    def __init__(self, assume_yes: bool = False, input_fn=input, out=print):
        self.assume_yes, self.input_fn, self.out = assume_yes, input_fn, out

    def say(self, text: str = "") -> None:
        self.out(text)

    def head(self, n: int, total: int, title: str) -> None:
        self.out(f"\n  Step {n} of {total} — {title}\n  " + "-" * (len(title) + 14))

    def ask_yes(self, question: str, default: bool = True) -> bool:
        if self.assume_yes:
            return default
        try:
            ans = self.input_fn(f"  {question} [{'Y/n' if default else 'y/N'}] ").strip().lower()
        except EOFError:
            return default
        return default if not ans else ans in ("y", "yes")

    def ask_text(self, question: str, default: str = "") -> str:
        if self.assume_yes:
            return default
        try:
            return self.input_fn(f"  {question} ").strip() or default
        except EOFError:
            return default


def step_computer(settings: Settings, ui: Console) -> dict:
    from . import hardware
    from .transcribe import auto_cpu_threads
    hw = hardware.detect()
    model, device, ctype = hardware.choose_whisper(hw, settings.whisper_model, settings.whisper_device,
                                                   settings.whisper_compute_type, settings.language)
    ui.say(f"  Computer: {hw.os}, {hw.cpu_cores} CPU threads, {hw.ram_gb} GB RAM, "
           + (f"GPU {hw.gpu_name or 'CUDA'} ({hw.vram_gb} GB)" if hw.cuda_devices else
              (f"NVIDIA {hw.gpu_name} found, CUDA libraries not installed" if hw.gpu_name else "no NVIDIA GPU")))
    ui.say(f"  Speech model chosen for this computer: {model} on {device} ({ctype}), "
           f"{settings.whisper_cpu_threads or auto_cpu_threads()} threads.")
    if hw.gpu_name and not hw.cuda_devices:
        ui.say("  Tip: set INSTALL_CUDA_LIBS=1 in .env and restart the launcher to use the NVIDIA GPU (much faster).")
    return {"hardware": hw.as_dict(), "model": model, "device": device, "compute_type": ctype}


def step_model(settings: Settings, ui: Console) -> dict:
    from .transcribe import WhisperTranscriber
    t = WhisperTranscriber(settings)
    ui.say(f"  Preparing {t.model_label} in {settings.models_dir} (downloaded once; 75 MB–3 GB depending on the model)…")
    t0 = time.time()
    try:
        t.prepare()
    except Exception as e:  # noqa: BLE001
        ui.say(f"  [FAIL] The speech model could not be prepared: {e}")
        ui.say("         Check the internet connection (it comes from huggingface.co) and run the setup again.")
        return {"ok": False, "error": str(e)}
    ui.say(f"  [ ok ] {t.model_label} is ready ({time.time() - t0:.0f}s).")
    return {"ok": True, "model": t.model_label}


def step_selftest(settings: Settings, ui: Console) -> dict:
    from . import doctor
    ui.say("  Running the real workflow on a 32-second NASA briefing excerpt (genuine speech):")
    rep = doctor.selftest(settings, log=ui.say)
    if rep["ok"]:
        ui.say(f"  [ ok ] Everything works on this computer. Watch the test clip: {rep.get('clip_file')}")
    else:
        ui.say(f"  [FAIL] {rep.get('error', 'see the steps above')}")
    return rep


def step_phone(settings: Settings, ui: Console) -> dict:
    from . import tailscale
    st = tailscale.status()
    if not st["installed"]:
        ui.say("  Tailscale gives your iPhone a private, encrypted route to this PC's dashboard — no ports are "
               "opened to the internet.")
        if os.name == "nt" and ui.ask_yes("Install Tailscale now with winget? (Windows will ask you to approve it)", False):
            subprocess.run(["winget", "install", "--id", tailscale.WINGET_ID, "-e", "--accept-source-agreements",
                            "--accept-package-agreements"], check=False)
            st = tailscale.status()
        if not st["installed"]:
            ui.say(f"  Skipped. Later: install from {tailscale.INSTALL_URL}, sign in, then Settings → Phone access → Set up.")
            return {"ok": False, "skipped": True}
    if not st["running"]:
        ui.say("  Tailscale is installed but not signed in on this PC.")
        if ui.ask_yes("Open the Tailscale sign-in now? (a browser window opens; sign in, then come back here)", True) \
                and not ui.assume_yes:
            try:
                subprocess.Popen([tailscale.find_cli(), "up"], stdin=subprocess.DEVNULL)
            except OSError as e:
                ui.say(f"  Could not start the sign-in: {e}")
            deadline = time.time() + 300
            while time.time() < deadline and not st["running"]:
                time.sleep(4)
                st = tailscale.status()
                if st.get("auth_url"):
                    ui.say(f"  If no browser opened, visit: {st['auth_url']}")
                    st["auth_url"] = ""
        if not st["running"]:
            ui.say("  Not signed in yet. Run this setup again afterwards, or use Settings → Phone access → Set up.")
            return {"ok": False, "waiting_for": "tailscale sign-in"}
    if not ui.ask_yes(f"Publish the dashboard privately on your tailnet as https://{st['dns_name']} ?", True):
        return {"ok": False, "skipped": True}
    rep = tailscale.setup_phone_access(settings)
    for line in rep["steps"]:
        ui.say("  [ ok ] " + line)
    if rep.get("password_created"):
        ui.say("\n  Your dashboard password (also saved in .env as DASHBOARD_PASSWORD) — write it down now:")
        ui.say(f"\n        {rep['password_created']}\n")
    if rep["ok"]:
        ui.say("  On the iPhone:")
        for k, line in enumerate(tailscale.phone_instructions(rep["url"]), 1):
            ui.say(f"    {k}. {line}")
    else:
        ui.say("  [ !! ] " + rep["next_step"])
    return rep


def step_notifications(settings: Settings, ui: Console) -> dict:
    ui.say("  The engine can email you one summary per finished recording, with links to your private dashboard.")
    ui.say("  Nothing is sent until you approve the recipient and the wording inside the dashboard.")
    cur = settings.notify_to
    ans = ui.ask_text(f"Email address for notifications{f' [{cur}]' if cur else ' (Enter to skip)'}:", cur)
    if not ans:
        ui.say("  Skipped. Later: Settings → Notifications.")
        return {"ok": False, "skipped": True}
    if not EMAIL_RE.match(ans):
        ui.say("  That does not look like an email address; skipped. Later: Settings → Notifications.")
        return {"ok": False, "skipped": True}
    if ans != cur:
        apply_settings(settings, {"NOTIFY_TO": ans})
        ui.say("  [ ok ] Saved in your local .env only (never uploaded).")
    ui.say("  To finish: open the dashboard → Settings → Notifications → “Connect a sending account”. "
           "It shows the two supported ways to send and holds every email until you approve it.")
    return {"ok": True, "configured": True}


def step_unattended(settings: Settings, ui: Console) -> dict:
    from . import autostart, power
    res = {"autostart": autostart.is_installed(), "prevent_sleep": settings.prevent_sleep}
    if autostart.supported():
        if autostart.is_installed():
            ui.say("  [ ok ] The engine already starts when you sign in to Windows.")
        elif ui.ask_yes("Start the engine automatically (minimised) every time you sign in to Windows?", True):
            ui.say(f"  [ ok ] Enabled: {autostart.install()}")
            res["autostart"] = True
    else:
        ui.say("  Start at sign-in is a Windows feature (not available on this system).")
    if power.supported() and settings.prevent_sleep != "monitoring":
        ui.say("  A sleeping PC cannot notice a live stream. The engine already keeps the PC awake while it is "
               "recording or processing.")
        if ui.ask_yes("Also keep the PC awake whenever a source is being monitored? (the screen can still turn "
                      "off; no Windows setting is changed)", False):
            apply_settings(settings, {"PREVENT_SLEEP": "monitoring"})
            res["prevent_sleep"] = "monitoring"
            ui.say("  [ ok ] Saved (PREVENT_SLEEP=monitoring). Note: closing a laptop lid still sleeps the PC "
                   "unless you change “When I close the lid” in Windows power settings yourself.")
    return res


def main(settings: Settings, assume_yes: bool = False, ui: Console | None = None) -> int:
    from . import doctor
    ui = ui or Console(assume_yes)
    settings.ensure_dirs()
    from .db import Database
    Database(settings.db_path)            # creates/migrates the database
    total = 6
    ui.say("\n  Clipping Engine — setup assistant")
    ui.say("  (safe to close and run again: finished steps are detected and skipped)")
    state: dict = {"started_at": time.time()}

    ui.head(1, total, "This computer")
    state["computer"] = step_computer(settings, ui)
    problems = [c for c in doctor.checks(settings) if c["level"] == doctor.FAIL]
    for c in problems:
        ui.say(f"  [FAIL] {c['title']}: {c['detail']}\n         → {c['fix']}")
    if problems:
        ui.say("\n  Fix the item(s) above, then start the launcher again.")
        return 2

    ui.head(2, total, "Speech model")
    state["model"] = step_model(settings, ui)

    ui.head(3, total, "Proof that it works here")
    state["selftest"] = step_selftest(settings, ui) if state["model"].get("ok") else {"ok": False, "error": "no model"}

    ui.head(4, total, "Phone access (optional)")
    state["phone"] = step_phone(settings, ui) if (not assume_yes) else {"ok": False, "skipped": True}
    if assume_yes:
        ui.say("  Skipped in unattended mode. Later: Settings → Phone access → Set up.")

    ui.head(5, total, "Email notifications (optional)")
    state["notifications"] = step_notifications(settings, ui) if not assume_yes else {"ok": False, "skipped": True}
    if assume_yes:
        ui.say("  Skipped in unattended mode. Later: Settings → Notifications.")

    ui.head(6, total, "Running unattended")
    state["unattended"] = step_unattended(settings, ui) if not assume_yes else {"skipped": True}
    if assume_yes:
        ui.say("  Skipped in unattended mode. Later: Settings → Running unattended.")

    ok = bool(state["selftest"].get("ok"))
    state["finished_at"] = time.time()
    state["ok"] = ok
    slim = json.loads(json.dumps(state, default=str))
    slim.get("phone", {}).pop("password_created", None)       # never store the password in the report
    (settings.data_dir / "setup_state.json").write_text(json.dumps(slim, indent=1), encoding="utf-8")
    ui.say("\n  " + "=" * 60)
    if ok:
        ui.say("  Setup complete. The engine is ready.")
        ui.say(f"  • Dashboard on this PC:  {settings.base_url()}")
        if settings.dashboard_url:
            ui.say(f"  • Dashboard on your phone: {settings.dashboard_url}")
        ui.say(f"  • Drop videos here to process them: {settings.inbox}")
    else:
        ui.say("  Setup finished with a problem in the self-test (see above). The dashboard will still open, and "
               "Settings → Setup checklist shows what to fix. Run “Start-Clipping-Engine.cmd --setup” to retry.")
    ui.say("  " + "=" * 60 + "\n")
    return 0 if ok else 1
