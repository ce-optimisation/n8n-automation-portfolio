"""Verify a trusted SHA-256 manifest against a local release directory."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path, PureWindowsPath


SHA256 = re.compile(r"[0-9a-f]{64}")
FORBIDDEN = frozenset('\\:*?"<>|')


def _require_sha256(value: object) -> str:
    if not isinstance(value, str) or not SHA256.fullmatch(value):
        raise ValueError("Expected a lowercase SHA-256 digest.")
    return value


def _require_relative_name(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("Release paths must be nonempty relative names.")
    parts = value.split("/")
    if any(
        not part or part in (".", "..") or part.endswith((".", " "))
        or PureWindowsPath(part).is_reserved()
        or any(char in FORBIDDEN or ord(char) < 32 for char in part)
        for part in parts
    ):
        raise ValueError(f"Invalid relative release path: {value!r}")
    return value


def _entry(path: Path, directory: bool = False) -> os.stat_result:
    info = path.lstat()
    reparse = getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    valid = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode) and info.st_nlink == 1
    if reparse or not valid:
        raise ValueError(f"Expected an ordinary {'directory' if directory else 'unaliased file'}: {path}")
    return info


def _manifest(raw: bytes) -> dict[str, str]:
    doc = json.loads(raw)
    if not isinstance(doc, dict) or set(doc) != {"version", "files"} or type(doc["version"]) is not int or doc["version"] != 1:
        raise ValueError("Expected a version 1 release manifest.")
    if not isinstance(doc["files"], list) or not doc["files"]:
        raise ValueError("The manifest must list at least one file.")
    expected: dict[str, str] = {}
    aliases: set[str] = set()
    for item in doc["files"]:
        if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
            raise ValueError("Each file must specify exactly path and sha256.")
        name = _require_relative_name(item["path"])
        alias = name.casefold()
        if alias in aliases:
            raise ValueError(f"Duplicate or case-aliased manifest path: {name}")
        aliases.add(alias)
        expected[name] = _require_sha256(item["sha256"])
    return expected


def _tree(root: Path) -> dict[str, Path]:
    actual: dict[str, Path] = {}
    aliases: set[str] = set()

    def raise_walk_error(error: OSError) -> None:
        raise error

    for current, directories, files in os.walk(root, onerror=raise_walk_error):
        for entry in directories + files:
            path = Path(current) / entry
            directory = entry in directories
            _entry(path, directory=directory)
            name = _require_relative_name(path.relative_to(root).as_posix())
            alias = name.casefold()
            if alias in aliases:
                raise ValueError(f"Case-aliased release path: {name}")
            aliases.add(alias)
            if not directory:
                actual[name] = path
    return actual


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify(root: Path, manifest: Path, manifest_sha256: str) -> int:
    """Return the number of fully covered release files after successful checks."""
    expected_pin = _require_sha256(manifest_sha256)
    _entry(root, directory=True)
    _entry(manifest)
    canonical_root = root.resolve(strict=True)
    canonical_manifest = manifest.resolve(strict=True)
    if root.absolute() != canonical_root or manifest.absolute() != canonical_manifest:
        raise ValueError("Use canonical paths for the release directory and manifest.")
    root, manifest = canonical_root, canonical_manifest
    if manifest.is_relative_to(root):
        raise ValueError("Keep the manifest outside the release directory.")
    raw = manifest.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_pin:
        raise ValueError("Manifest SHA-256 does not match the trusted digest.")

    expected = _manifest(raw)
    actual = _tree(root)
    if actual.keys() != expected.keys():
        missing = sorted(expected.keys() - actual.keys())
        unlisted = sorted(actual.keys() - expected.keys())
        raise ValueError(f"Release files differ from manifest. Missing: {missing!r}. Unlisted: {unlisted!r}.")
    for name, digest in expected.items():
        if _file_digest(actual[name]) != digest:
            raise ValueError(f"SHA-256 mismatch: {name}")
    return len(expected)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--manifest-sha256", required=True)
    args = parser.parse_args(argv)
    try:
        count = verify(args.root, args.manifest, args.manifest_sha256)
    except (OSError, RuntimeError, ValueError) as error:
        print(f"Release verification failed: {error}", file=sys.stderr)
        return 1
    print(f"Verified SHA-256 for {count} release files.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
