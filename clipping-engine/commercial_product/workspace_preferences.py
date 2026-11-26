"""Export explicitly allowlisted presentation preferences as portable JSON."""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
from pathlib import Path
import re
import sqlite3
from contextlib import closing


SCHEMA = "clipengine.presentation-preferences.v1"
MAX_BYTES = 64 * 1024
DATABASE = "clipengine.sqlite3"
FIELDS = {
    "caption_style": {
        "enabled": bool, "backing": bool, "bold": bool, "source_label": bool,
        "preset": ("anton", "classic"), "mode": ("phrase", "active_word"),
        "size": (32, 96), "outline": (0.0, 8.0), "position": (0.45, 0.85),
    },
    "title_style": {
        "enabled": bool, "uppercase": bool, "single_phrase": bool,
        "variant": ("classic", "afm_v2"), "scale": (0.1, 3.0),
        "max_lines": (1, 8), "side_margin": (0, 540),
        "centre_frac": (0.0, 1.0), "emphasis_scale": (0.1, 3.0),
    },
}


def _valid(value: object, rule: object) -> bool:
    if rule is bool:
        return type(value) is bool
    lower, upper = rule
    if isinstance(lower, str):
        return type(value) is str and value in rule
    types = (int,) if type(lower) is int else (int, float)
    return type(value) in types and lower <= value <= upper


def _style(key: str, value: object, *, strict: bool) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"Invalid saved {key}.")
    fields = FIELDS[key]
    selected = {name: setting for name, setting in value.items()
                if name in fields and _valid(setting, fields[name])}
    if strict and len(selected) != len(value):
        raise ValueError(f"Unexpected or invalid {key} field.")
    return selected


def export_preferences(database: Path, bundle: Path) -> str:
    with closing(sqlite3.connect(database.resolve(strict=True).as_uri() + "?mode=ro", uri=True)) as db:
        rows = db.execute("SELECT key, value FROM meta WHERE key IN (?, ?)", tuple(FIELDS))
        styles = {key: _style(key, json.loads(value), strict=False) for key, value in rows}
    payload = (json.dumps({"schema": SCHEMA, "styles": styles}, sort_keys=True, indent=2) + "\n").encode()
    with bundle.open("xb") as output:
        output.write(payload)
    return hashlib.sha256(payload).hexdigest()


def restore_preferences(bundle: Path, destination: Path, expected_sha256: str) -> None:
    if not re.fullmatch("[0-9a-fA-F]{64}", expected_sha256):
        raise ValueError("Expected a trusted SHA-256 digest.")
    with bundle.open("rb") as source:
        payload = source.read(MAX_BYTES + 1)
    if len(payload) > MAX_BYTES or not hmac.compare_digest(
        hashlib.sha256(payload).hexdigest(), expected_sha256.lower()
    ):
        raise ValueError("Preference bundle digest or size check failed.")
    document = json.loads(payload)
    if (not isinstance(document, dict) or document.keys() != {"schema", "styles"}
            or document["schema"] != SCHEMA or not isinstance(document["styles"], dict)
            or not document["styles"].keys() <= FIELDS.keys()):
        raise ValueError("Unsupported preference bundle.")
    styles = {key: _style(key, value, strict=True) for key, value in document["styles"].items()}
    rows = [(key, json.dumps(value, sort_keys=True)) for key, value in styles.items()]
    rows += [("recording.enabled", "0"), ("delivery.enabled", "0")]

    destination.mkdir(exist_ok=False)
    data = destination / "data"
    config = destination / "config"
    data.mkdir()
    config.mkdir()
    with closing(sqlite3.connect(data / DATABASE)) as db:
        with db:
            db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
            db.executemany("INSERT INTO meta VALUES (?, ?)", rows)
        if db.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise ValueError("Restored preference database failed integrity verification.")
    with (config / ".env").open("x", encoding="utf-8") as output:
        output.write("# Add this workspace's settings and connections before launching.\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export")
    export.add_argument("database", type=Path)
    export.add_argument("bundle", type=Path)
    restore = commands.add_parser("restore")
    restore.add_argument("bundle", type=Path)
    restore.add_argument("destination", type=Path)
    restore.add_argument("--sha256", required=True)
    args = parser.parse_args()
    if args.command == "export":
        print(export_preferences(args.database, args.bundle))
    else:
        restore_preferences(args.bundle, args.destination, args.sha256)


if __name__ == "__main__":
    main()
