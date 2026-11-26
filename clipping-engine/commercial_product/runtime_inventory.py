"""Collect installed Python distribution and binary provenance evidence locally."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import platform
import re
import stat
import sys
from pathlib import Path


def _sha256(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(chunk)
    return checksum.hexdigest()


def _distribution_evidence(distribution) -> dict:
    metadata = distribution.metadata
    name = metadata.get("Name")
    version = metadata.get("Version")
    if not name or not version:
        raise ValueError("An installed distribution lacks a name or version.")
    expression = metadata.get("License-Expression")
    classifiers = sorted(set(
        value for value in metadata.get_all("Classifier", []) if value.startswith("License ::")
    ))
    declared_files = sorted(set(metadata.get_all("License-File", [])))
    legacy = metadata.get("License")
    legacy_present = bool(legacy and legacy.strip().lower() not in {"unknown", "none"})
    record = distribution.read_text("METADATA") or distribution.read_text("PKG-INFO")
    return {
        "name": name,
        "version": version,
        "metadata_version": metadata.get("Metadata-Version"),
        "license_expression": expression,
        "license_classifiers": classifiers,
        "declared_license_files": declared_files,
        "legacy_license_field_present": legacy_present,
        "license_evidence": (
            "declared_expression" if expression else
            "legacy_metadata" if classifiers or declared_files or legacy_present else
            "missing"
        ),
        "metadata_text_sha256": hashlib.sha256(record.encode("utf-8")).hexdigest() if record else None,
    }


def inventory(search_paths: list[Path] | None = None, binaries: list[Path] | None = None) -> dict:
    options = {"path": [str(path) for path in search_paths]} if search_paths is not None else {}
    distributions = []
    seen = set()
    for distribution in importlib.metadata.distributions(**options):
        evidence = _distribution_evidence(distribution)
        name = re.sub(r"[-_.]+", "-", evidence["name"]).lower()
        if name in seen:
            raise ValueError(f"Multiple installed metadata records for distribution: {name}")
        seen.add(name)
        distributions.append(evidence)

    artifacts = []
    for path in binaries or []:
        details = path.lstat()
        if not stat.S_ISREG(details.st_mode) or getattr(details, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError(f"Expected an ordinary binary artifact: {path.name}")
        artifacts.append({"name": path.name, "size": details.st_size, "sha256": _sha256(path),
                          "license_evidence": "not_assessed"})
    return {
        "schema": "clipping-engine-runtime-evidence/1",
        "interpreter": {"implementation": platform.python_implementation(), "version": platform.python_version()},
        "python_distributions": sorted(distributions, key=lambda item: item["name"].lower()),
        "binary_artifacts": sorted(artifacts, key=lambda item: (item["name"].lower(), item["sha256"])),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--search-path", type=Path, action="append")
    parser.add_argument("--binary", type=Path, action="append")
    args = parser.parse_args(argv)
    try:
        result = inventory(args.search_path, args.binary)
    except (OSError, ValueError) as error:
        print(f"Runtime inventory failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
