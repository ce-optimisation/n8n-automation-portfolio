"""Create and verify local handoff packages for explicitly selected agency workspaces."""

from __future__ import annotations

import argparse
import hashlib
import json
import ntpath
import os
from pathlib import Path, PureWindowsPath
import re
import tempfile
from zipfile import BadZipFile, ZipFile, ZIP_DEFLATED


MANIFEST = "manifest.json"
_is_reserved = getattr(ntpath, "isreserved", lambda name: PureWindowsPath(name).is_reserved())


def _slug(value: str) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9-]{1,30}", value) or _is_reserved(value):
        raise ValueError("use a lowercase workspace or delivery ID of 2–31 letters, digits or hyphens")
    return value


def _filename(value: str) -> str:
    if (not value or value in (".", "..") or value.endswith((".", " "))
            or re.search(r'[<>:"/\\|?*\x00-\x1f]', value) or _is_reserved(value)
            or value.casefold() == MANIFEST):
        raise ValueError("asset must be a flat filename different from manifest.json")
    return value


def _reference(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be recorded")
    return value.strip()


def _local_child(parent: Path, name: str) -> Path:
    child = parent / name
    if child.resolve(strict=True) != child:
        raise ValueError(f"{name} resolves outside its expected path")
    return child


def onboard(root: Path, client_id: str, commercial_reference: str) -> dict:
    """Create a fresh workspace, with no path chosen by the client record."""
    root = root.resolve(strict=True)
    client_id = _slug(client_id)
    info = {"client_id": client_id, "commercial_reference": _reference(commercial_reference, "commercial reference")}
    workspace = root / client_id
    workspace.mkdir()
    (workspace / "inbox").mkdir()
    (workspace / "exports").mkdir()
    with (workspace / "client.json").open("x", encoding="utf-8") as handle:
        json.dump(info, handle, indent=2)
    return info


def _workspace(root: Path, client_id: str) -> Path:
    root = root.resolve(strict=True)
    folder = _local_child(root, _slug(client_id))
    info = json.loads(_local_child(folder, "client.json").read_text(encoding="utf-8"))
    if not isinstance(info, dict) or info.get("client_id") != client_id:
        raise ValueError("workspace identity mismatch")
    return folder


def _digest(stream, destination=None) -> tuple[int, str]:
    sha = hashlib.sha256()
    size = 0
    for chunk in iter(lambda: stream.read(256 * 1024), b""):
        sha.update(chunk)
        size += len(chunk)
        if destination is not None:
            destination.write(chunk)
    return size, sha.hexdigest()


def package(root: Path, client_id: str, delivery_id: str, files: list[str],
            rights_reference: str, approval_reference: str) -> dict:
    workspace = _workspace(root, client_id)
    delivery_id = _slug(delivery_id)
    rights_reference = _reference(rights_reference, "rights reference")
    approval_reference = _reference(approval_reference, "approval reference")
    names = [_filename(name) for name in files]
    if not names or len(set(name.casefold() for name in names)) != len(names):
        raise ValueError("provide at least one unique asset filename")
    inbox = _local_child(workspace, "inbox")
    exports = _local_child(workspace, "exports")
    sources = [_local_child(inbox, name) for name in names]
    if any(not source.is_file() for source in sources):
        raise ValueError("all assets must be files in the selected client's inbox")
    target = exports / f"{delivery_id}.zip"
    manifest = {"client_id": client_id, "delivery_id": delivery_id, "rights_reference": rights_reference,
                "approval_reference": approval_reference, "assets": []}
    with tempfile.TemporaryDirectory(prefix="handoff-", dir=exports) as scratch:
        temp = Path(scratch) / "delivery.zip"
        with ZipFile(temp, "x", ZIP_DEFLATED) as archive:
            for name, source in zip(names, sources, strict=True):
                with source.open("rb") as source_file, archive.open(name, "w", force_zip64=True) as member:
                    size, digest = _digest(source_file, member)
                manifest["assets"].append({"name": name, "bytes": size, "sha256": digest})
            archive.writestr(MANIFEST, json.dumps(manifest, indent=2))
        _verify_archive(temp, client_id, delivery_id)
        if os.name == "nt":
            os.rename(temp, target)  # Windows does not overwrite an existing target.
        else:
            os.link(temp, target)
    return manifest


def _verify_archive(path: Path, client_id: str, delivery_id: str) -> dict:
    with ZipFile(path) as archive:
        manifest = json.loads(archive.read(MANIFEST))
        if manifest["client_id"] != client_id or manifest["delivery_id"] != delivery_id:
            raise ValueError("handoff manifest identity mismatch")
        _reference(manifest["rights_reference"], "rights reference")
        _reference(manifest["approval_reference"], "approval reference")
        assets = manifest["assets"]
        names = [_filename(item["name"]) for item in assets]
        if not names or len(set(n.casefold() for n in names)) != len(names):
            raise ValueError("handoff has duplicate or missing assets")
        if sorted(archive.namelist()) != sorted([MANIFEST, *names]):
            raise ValueError("archive members do not match the manifest")
        for item in assets:
            with archive.open(item["name"]) as member:
                size, digest = _digest(member)
            if size != item["bytes"] or digest != item["sha256"]:
                raise ValueError("asset does not match the manifest")
        return manifest


def verify(root: Path, client_id: str, delivery_id: str) -> dict:
    workspace = _workspace(root, client_id)
    exports = _local_child(workspace, "exports")
    return _verify_archive(_local_child(exports, f"{_slug(delivery_id)}.zip"), client_id, delivery_id)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="Existing directory for local client workspaces")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("onboard")
    create.add_argument("client_id")
    create.add_argument("commercial_reference")
    seal = commands.add_parser("package")
    seal.add_argument("client_id")
    seal.add_argument("delivery_id")
    seal.add_argument("--rights-reference", required=True)
    seal.add_argument("--approval-reference", required=True)
    seal.add_argument("files", nargs="+")
    inspect = commands.add_parser("verify")
    inspect.add_argument("client_id")
    inspect.add_argument("delivery_id")
    args = parser.parse_args()
    try:
        if args.command == "onboard":
            result = onboard(args.root, args.client_id, args.commercial_reference)
        elif args.command == "package":
            result = package(args.root, args.client_id, args.delivery_id, args.files,
                             args.rights_reference, args.approval_reference)
        else:
            result = verify(args.root, args.client_id, args.delivery_id)
    except (OSError, ValueError, KeyError, TypeError, BadZipFile) as exc:
        parser.exit(2, json.dumps({"error": str(exc)}) + "\n")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
