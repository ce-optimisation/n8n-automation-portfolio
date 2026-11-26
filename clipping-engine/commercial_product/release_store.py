"""Stage and select verified local releases without modifying workspace data."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

from commercial_product.release_manifest import verify


RELEASE_ID = re.compile(r"[a-f0-9]{64}")


class ReleaseStore:
    def __init__(self, root: Path):
        self.root = root.resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("The release store must be an existing dedicated directory.")
        self.pointer = self.root / "selection.json"

    @contextmanager
    def _selection_lock(self):
        path = self.root / ".selection.lock"
        with path.open("a+b") as stream:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield

    def _release(self, release_id: str) -> Path:
        if not isinstance(release_id, str) or not RELEASE_ID.fullmatch(release_id):
            raise ValueError("Invalid release ID.")
        release = self.root / release_id
        if release.resolve() != release:
            raise ValueError("A stored release cannot be a filesystem alias.")
        return release

    def _check(self, release_id: str) -> Path:
        release = self._release(release_id)
        bundle = release / "bundle"
        verify(bundle, release / "manifest.json", release_id)
        return bundle

    def stage(self, bundle: Path, manifest: Path, manifest_sha256: str) -> str:
        """Copy and reverify a release before making it eligible for selection."""
        target = self._release(manifest_sha256)
        verify(bundle, manifest, manifest_sha256)
        bundle = bundle.resolve(strict=True)
        if bundle.is_relative_to(self.root) or self.root.is_relative_to(bundle):
            raise ValueError("The source bundle and release store must be separate.")
        if target.exists():
            self._check(manifest_sha256)
            return manifest_sha256
        with tempfile.TemporaryDirectory(prefix=".stage-", dir=self.root) as scratch:
            staged = Path(scratch) / "release"
            staged.mkdir()
            shutil.copytree(bundle, staged / "bundle", symlinks=True)
            shutil.copyfile(manifest, staged / "manifest.json", follow_symlinks=False)
            verify(staged / "bundle", staged / "manifest.json", manifest_sha256)
            staged.rename(target)
        return manifest_sha256

    def selection(self) -> dict[str, str | None]:
        if not self.pointer.exists():
            return {"current": None, "previous": None}
        if self.pointer.resolve() != self.pointer:
            raise ValueError("The selection file cannot be a filesystem alias.")
        saved = json.loads(self.pointer.read_text(encoding="utf-8"))
        if not isinstance(saved, dict) or saved.keys() != {"current", "previous"} or not saved["current"]:
            raise ValueError("Invalid release selection file.")
        for value in saved.values():
            if value is not None:
                self._release(value)
        return saved

    def activate(self, release_id: str) -> Path:
        with self._selection_lock():
            return self._activate(release_id, self.selection())

    def _activate(self, release_id: str, selected: dict[str, str | None]) -> Path:
        bundle = self._check(release_id)
        if selected["current"] == release_id:
            return bundle
        with tempfile.TemporaryDirectory(prefix=".select-", dir=self.root) as scratch:
            temporary = Path(scratch) / "selection.json"
            with temporary.open("w", encoding="utf-8") as stream:
                json.dump({"current": release_id, "previous": selected["current"]}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(self.pointer)
        return bundle

    def rollback(self) -> Path:
        with self._selection_lock():
            selected = self.selection()
            previous = selected["previous"]
            if previous is None:
                raise ValueError("No previous release has been selected.")
            return self._activate(previous, selected)

    def current(self) -> Path:
        current = self.selection()["current"]
        if current is None:
            raise ValueError("No current release has been selected.")
        return self._check(current)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("store", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    stage = commands.add_parser("stage")
    stage.add_argument("bundle", type=Path)
    stage.add_argument("manifest", type=Path)
    stage.add_argument("--manifest-sha256", required=True)
    commands.add_parser("activate").add_argument("release_id")
    commands.add_parser("rollback")
    commands.add_parser("current")
    args = parser.parse_args(argv)
    try:
        store = ReleaseStore(args.store)
        if args.command == "stage":
            result = store.stage(args.bundle, args.manifest, args.manifest_sha256)
        elif args.command == "activate":
            result = store.activate(args.release_id)
        elif args.command == "rollback":
            result = store.rollback()
        else:
            result = store.current()
    except (OSError, ValueError, RuntimeError) as error:
        print(f"Release store operation failed: {error}", file=sys.stderr)
        return 1
    print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
