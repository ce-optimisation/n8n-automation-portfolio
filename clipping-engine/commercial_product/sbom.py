"""Generate a CycloneDX 1.5 SBOM for a Clipping Engine runtime with licence risk flags.

Run with the interpreter whose environment you want to inventory. Native tools
(FFmpeg/FFprobe), bundled fonts and cached models are added as components.
Licence labels come from package metadata and are evidence, not legal advice.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
from importlib import metadata
import json
from pathlib import Path
import re
import shutil
import subprocess
import uuid

PERMISSIVE = re.compile(r"\b(?:MIT|BSD|Apache|ISC|PSF|Python Software|Unlicense|Zlib|MPL|HPND|CC0|OFL|"
                        r"Public Domain|0BSD|Historical Permission)\b", re.I)
COPYLEFT = re.compile(r"\b(?:GPL|AGPL|LGPL|SSPL|EUPL|copyleft)|General Public License", re.I)


def classify(text: str) -> str:
    if not text:
        return "unknown"
    if re.search(r"\bAGPL|\bSSPL|Affero", text, re.I):
        return "strong_copyleft_network"
    if re.search(r"\bLGPL|Lesser General Public", text, re.I):
        return "weak_copyleft"
    if COPYLEFT.search(text):
        return "copyleft"
    if PERMISSIVE.search(text):
        return "permissive"
    return "unknown"


def _licence_of(dist) -> str:
    md = dist.metadata
    expr = md.get("License-Expression")
    if expr:
        return expr
    classifiers = [c.split("::")[-1].strip() for c in md.get_all("Classifier") or [] if c.startswith("License ::")]
    lic = (md.get("License") or "").strip()
    if classifiers:
        return "; ".join(classifiers)
    return lic.splitlines()[0][:120] if lic else ""


def python_components() -> list[dict]:
    out = []
    for dist in sorted(metadata.distributions(), key=lambda d: (d.metadata["Name"] or "").lower()):
        name = dist.metadata["Name"]
        if not name:
            continue
        lic = _licence_of(dist)
        out.append({"type": "library", "name": name, "version": dist.version,
                    "purl": f"pkg:pypi/{name.lower()}@{dist.version}",
                    "licenses": [{"license": {"name": lic or "UNKNOWN"}}],
                    "properties": [{"name": "clipengine:licence_class", "value": classify(lic)}]})
    return out


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def native_components() -> list[dict]:
    out = []
    for tool in ("ffmpeg", "ffprobe"):
        path = shutil.which(tool)
        if not path:
            continue
        banner = subprocess.run([path, "-hide_banner", "-buildconf"], capture_output=True, text=True, timeout=20).stdout
        version = subprocess.run([path, "-version"], capture_output=True, text=True, timeout=20).stdout.splitlines()[0]
        gpl = "--enable-gpl" in banner
        v3 = "--enable-version3" in banner
        nonfree = "--enable-nonfree" in banner
        lic = ("GPL-3.0-or-later" if gpl and v3 else "GPL-2.0-or-later" if gpl else
               "LGPL-3.0-or-later" if v3 else "LGPL-2.1-or-later")
        out.append({"type": "application", "name": tool, "version": version.split()[2] if len(version.split()) > 2 else version,
                    "hashes": [{"alg": "SHA-256", "content": _sha256(Path(path))}],
                    "licenses": [{"license": {"id": lic}}],
                    "properties": [{"name": "clipengine:licence_class", "value": "copyleft" if gpl else "weak_copyleft"},
                                   {"name": "clipengine:nonfree", "value": str(nonfree).lower()},
                                   {"name": "clipengine:distribution_note", "value":
                                    "Do not bundle; require customer-installed FFmpeg or ship an LGPL build with source offer."
                                    if gpl else "Ship with LGPL notice and source offer."}]})
    return out


def asset_components(root: Path, models_dir: Path | None) -> list[dict]:
    out = []
    for font in sorted(root.rglob("*.ttf")) + sorted(root.rglob("*.otf")):
        notice = any((font.parent / n).exists() for n in ("OFL.txt", "LICENSE", "LICENSE.txt"))
        out.append({"type": "file", "name": font.name, "hashes": [{"alg": "SHA-256", "content": _sha256(font)}],
                    "licenses": [{"license": {"name": "OFL-1.1 (expected)" if "Anton" in font.name else "UNKNOWN"}}],
                    "properties": [{"name": "clipengine:licence_class", "value": "permissive" if "Anton" in font.name else "unknown"},
                                   {"name": "clipengine:notice_present", "value": str(notice).lower()}]})
    if models_dir and models_dir.is_dir():
        for card in sorted(models_dir.rglob("README.md")):
            text = card.read_text(encoding="utf-8", errors="replace")[:2000]
            m = re.search(r"^license:\s*(\S+)", text, re.M | re.I)
            lic = m.group(1) if m else ""
            out.append({"type": "machine-learning-model", "name": card.parent.name,
                        "licenses": [{"license": {"name": lic or "UNKNOWN"}}],
                        "properties": [{"name": "clipengine:licence_class", "value": classify(lic)}]})
    return out


def build(root: Path, models_dir: Path | None = None) -> dict:
    comps = python_components() + native_components() + asset_components(root, models_dir)
    return {"bomFormat": "CycloneDX", "specVersion": "1.5", "serialNumber": f"urn:uuid:{uuid.uuid4()}", "version": 1,
            "metadata": {"timestamp": datetime.now(timezone.utc).isoformat(),
                         "component": {"type": "application", "name": "clipping-engine"}},
            "components": comps}


def risks(bom: dict) -> dict:
    groups: dict[str, list[str]] = {}
    for c in bom["components"]:
        cls = next(p["value"] for p in c["properties"] if p["name"] == "clipengine:licence_class")
        if cls != "permissive":
            groups.setdefault(cls, []).append(f"{c['name']} {c.get('version', '')}".strip())
        if any(p["name"] == "clipengine:notice_present" and p["value"] == "false" for p in c["properties"]):
            groups.setdefault("missing_notice", []).append(c["name"])
    return {"total": len(bom["components"]), "needs_review": groups}


def main() -> int:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    cli.add_argument("--models", type=Path)
    cli.add_argument("--out", type=Path, required=True)
    a = cli.parse_args()
    bom = build(a.root, a.models)
    a.out.write_text(json.dumps(bom, indent=2), encoding="utf-8")
    print(json.dumps(risks(bom), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
