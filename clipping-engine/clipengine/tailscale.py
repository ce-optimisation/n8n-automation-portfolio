"""Private phone access through Tailscale Serve.

Tailscale puts the owner's PC and phone on a private network (a "tailnet"). `tailscale serve`
then publishes the dashboard at ``https://<pc-name>.<tailnet>.ts.net`` with a real HTTPS
certificate, reachable ONLY from devices signed in to the same tailnet. The engine itself stays
bound to 127.0.0.1; nothing is port-forwarded and Tailscale Funnel (public internet exposure) is
never enabled here.

This module only ever runs the official `tailscale` command-line client that the owner
installed and signed in to. It reads the real device name from `tailscale status --json`;
no address is invented. Commands used (Tailscale ≥ 1.52 syntax):

    tailscale status --json
    tailscale serve --bg --yes <port>          publish http://127.0.0.1:<port> at https://<name>/
    tailscale serve status --json
    tailscale serve --https=443 off            stop publishing

TAILSCALE_CLI overrides the executable (used by tests with a labelled fake).
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
from pathlib import Path

from . import media
from .config import apply_settings

INSTALL_URL = "https://tailscale.com/download/windows"
WINGET_ID = "Tailscale.Tailscale"
IOS_APP = "https://apps.apple.com/app/tailscale/id1470499037"


def find_cli() -> str | None:
    override = os.environ.get("TAILSCALE_CLI")
    if override:
        return override if Path(override).exists() else None
    found = shutil.which("tailscale")
    if found:
        return found
    if os.name == "nt":
        for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                     os.environ.get("ProgramW6432")):
            if base and (Path(base) / "Tailscale" / "tailscale.exe").exists():
                return str(Path(base) / "Tailscale" / "tailscale.exe")
    return None


def _run(args: list[str], timeout: float = 20.0) -> tuple[int | None, str]:
    """Run the CLI. Returns (exit code or None if it was still waiting at the timeout, output)."""
    cli = find_cli()
    if not cli:
        raise FileNotFoundError("Tailscale is not installed.")
    cmd = [cli, *args]
    if cli.endswith(".py"):                     # labelled fake CLI used by the test-suite
        import sys
        cmd = [sys.executable, *cmd]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                            text=True, encoding="utf-8", errors="replace", **media._popen_kwargs())
    try:
        out, _ = proc.communicate(timeout=timeout)
        return proc.returncode, out or ""
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()
        return None, out or ""


def status() -> dict:
    """What is true right now: installed? signed in? this PC's real tailnet name?"""
    st = {"installed": False, "running": False, "state": "not installed", "dns_name": "", "tailnet": "",
          "https_enabled": False, "version": "", "auth_url": "", "error": ""}
    cli = find_cli()
    if not cli:
        return st
    st["installed"] = True
    try:
        rc, out = _run(["status", "--json"])
    except OSError as e:
        st.update(state="error", error=str(e))
        return st
    try:
        data = json.loads(out[out.index("{"):])
    except (ValueError, json.JSONDecodeError):
        st.update(state="not running", error=(out.strip().splitlines() or ["The Tailscale service is not running."])[0])
        return st
    me = data.get("Self") or {}
    st.update(state=data.get("BackendState") or "unknown", version=(data.get("Version") or "").split("-")[0],
              dns_name=(me.get("DNSName") or "").rstrip("."), auth_url=data.get("AuthURL") or "",
              tailnet=(data.get("CurrentTailnet") or {}).get("Name") or "",
              https_enabled=bool(data.get("CertDomains")))
    st["running"] = st["state"] == "Running" and bool(st["dns_name"])
    return st


def serve_status(port: int) -> dict:
    """Is this engine's port published by Tailscale Serve, and at which address?"""
    res = {"serving": False, "url": "", "funnel": False, "other_targets": []}
    try:
        rc, out = _run(["serve", "status", "--json"])
        data = json.loads(out[out.index("{"):]) if "{" in out else {}
    except (OSError, ValueError, json.JSONDecodeError):
        return res
    funnel = data.get("AllowFunnel") or {}
    for hostport, web in (data.get("Web") or {}).items():
        for path, handler in (web.get("Handlers") or {}).items():
            proxy = (handler or {}).get("Proxy") or ""
            if re.search(rf"^https?://(127\.0\.0\.1|localhost):{port}(/|$)", proxy):
                host, _, hp = hostport.rpartition(":")
                res.update(serving=True, url=f"https://{host}" + ("" if hp == "443" else f":{hp}")
                           + ("" if path == "/" else path.rstrip("/")), funnel=bool(funnel.get(hostport)))
            elif proxy:
                res["other_targets"].append(f"{hostport}{path} → {proxy}")
    return res


def enable_serve(port: int, timeout: float = 25.0) -> dict:
    """Publish the dashboard on the tailnet. If the tailnet has HTTPS switched off, Tailscale
    prints a link where the owner enables it (an account setting only they can change)."""
    rc, out = _run(["serve", "--bg", "--yes", str(port)], timeout=timeout)
    sv = serve_status(port)
    link = re.search(r"https://login\.tailscale\.com/\S+", out)
    return {"ok": sv["serving"], "url": sv["url"], "output": out.strip()[-1500:],
            "enable_link": link.group(0) if link and not sv["serving"] else "",
            "waiting": rc is None and not sv["serving"]}


def disable_serve() -> tuple[bool, str]:
    rc, out = _run(["serve", "--https=443", "off"])
    return rc == 0, out.strip()


def verify(url: str, timeout: float = 15.0) -> tuple[bool, str]:
    """Fetch /healthz through the Tailscale address from this PC (proves the private HTTPS route
    and certificate work end to end; it cannot prove the phone is signed in)."""
    import httpx
    try:
        r = httpx.get(url.rstrip("/") + "/healthz", timeout=timeout)
        if r.status_code == 200 and r.json().get("app") == "clipengine":
            return True, "The dashboard answered through the private Tailscale address."
        return False, f"Unexpected answer through Tailscale (HTTP {r.status_code})."
    except Exception as e:  # noqa: BLE001
        return False, f"Could not reach {url} yet: {type(e).__name__}: {e}"


def new_password() -> str:
    """A strong dashboard password that is still typeable on a phone."""
    alphabet = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "-".join("".join(secrets.choice(alphabet) for _ in range(5)) for _ in range(4))


def setup_phone_access(settings, port: int | None = None) -> dict:
    """Everything that does not need the owner: make sure a dashboard password exists, publish
    the dashboard on the tailnet, read back the real address, check it, and save it as
    DASHBOARD_URL so email links point at it. Returns a report with `next_step` when the owner
    has to do something (install, sign in, enable HTTPS)."""
    port = port or settings.port
    rep: dict = {"ok": False, "steps": [], "next_step": "", "url": "", "password_created": ""}
    st = status()
    rep["tailscale"] = st
    if not st["installed"]:
        rep["next_step"] = (f"Install Tailscale on this PC ({INSTALL_URL}, or run: winget install {WINGET_ID}), "
                            "sign in, then press this button again.")
        return rep
    if not st["running"]:
        rep["next_step"] = ("Tailscale is installed but not signed in on this PC. Click the Tailscale icon near "
                            "the clock → Log in" + (f" (or open {st['auth_url']})" if st["auth_url"] else "")
                            + ", then press this button again.")
        return rep
    rep["steps"].append(f"Tailscale is running; this PC is {st['dns_name']}.")
    if not settings.dashboard_password:
        pw = new_password()
        apply_settings(settings, {"DASHBOARD_PASSWORD": pw})
        rep["password_created"] = pw
        rep["steps"].append("Created a dashboard password (required before the dashboard is reachable from "
                            "another device).")
    res = enable_serve(port)
    rep["serve_output"] = res["output"]
    if not res["ok"]:
        if res["enable_link"]:
            rep["next_step"] = ("Your tailnet has HTTPS switched off. Open this link, press Enable, then press this "
                                f"button again: {res['enable_link']}")
        else:
            rep["next_step"] = ("Tailscale Serve did not start. Message from Tailscale: "
                                + (res["output"].splitlines()[-1] if res["output"] else "no output"))
        return rep
    sv = serve_status(port)
    if sv["funnel"]:
        rep["next_step"] = ("Tailscale FUNNEL is switched on for this address, which would expose the dashboard to "
                            "the public internet. Turn it off with: tailscale funnel --https=443 off")
        return rep
    rep["url"] = res["url"]
    rep["steps"].append(f"Tailscale Serve publishes the dashboard privately at {res['url']}.")
    apply_settings(settings, {"DASHBOARD_URL": res["url"]})
    rep["steps"].append("Saved as DASHBOARD_URL, so notification emails link to this address.")
    ok, msg = verify(res["url"])
    rep["verified_from_pc"] = ok
    rep["steps"].append(msg)
    rep["ok"] = True
    return rep


def phone_instructions(url: str) -> list[str]:
    return ["On the iPhone: install “Tailscale” from the App Store and sign in with the SAME account "
            "you used on this PC.",
            "Leave Tailscale switched on (the VPN toggle in the app).",
            f"Open Safari and go to {url or 'the address shown above'}",
            "Sign in with the dashboard password. Tap Share → Add to Home Screen for an app-like icon."]
