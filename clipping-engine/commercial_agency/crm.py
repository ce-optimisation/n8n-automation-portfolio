"""Maintain a local agency prospect register without performing outreach."""

from __future__ import annotations

import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from urllib.parse import urlsplit


STAGES = ("researched", "qualified", "contacted", "discovery", "proposal", "won", "closed")
APPLICATION_ID = 0x4147434D

SCHEMA = """
CREATE TABLE IF NOT EXISTS prospects (
    id INTEGER PRIMARY KEY,
    organization TEXT NOT NULL,
    organization_key TEXT NOT NULL UNIQUE,
    source_url TEXT NOT NULL,
    public_contact_url TEXT,
    contact_basis TEXT,
    approval_reference TEXT,
    stage TEXT NOT NULL DEFAULT 'researched',
    next_action TEXT,
    suppression_reference TEXT,
    CHECK (suppression_reference IS NULL OR approval_reference IS NULL)
);
"""


def connect(path: Path, *, initialize: bool = False) -> sqlite3.Connection:
    """Open an explicit database path. Only initialise creates a file."""
    path = path.resolve()
    connection = sqlite3.connect(f"{path.as_uri()}?mode={'rwc' if initialize else 'rw'}", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        application_id = connection.execute("PRAGMA application_id").fetchone()[0]
        if initialize and application_id == 0:
            if connection.execute("SELECT 1 FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchone():
                raise ValueError("refusing to initialise a database that already contains other data")
            connection.executescript(f"BEGIN IMMEDIATE;{SCHEMA}PRAGMA application_id = {APPLICATION_ID};COMMIT;")
        elif application_id != APPLICATION_ID:
            raise ValueError("path does not point to an agency CRM database")
    except Exception:
        connection.close()
        raise
    return connection


def _required(value: str, field: str) -> str:
    if not value.strip():
        raise ValueError(f"{field} is required")
    return value.strip()


def _public_url(value: str, field: str) -> str:
    value = _required(value, field)
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username is not None:
        raise ValueError(f"{field} must be an HTTPS link without credentials")
    if parsed.query or parsed.fragment:
        raise ValueError(f"{field} must omit query strings and fragments")
    return value


def get(db: sqlite3.Connection, prospect_id: int) -> dict:
    row = db.execute("SELECT * FROM prospects WHERE id=?", (prospect_id,)).fetchone()
    if row is None:
        raise ValueError(f"unknown prospect ID {prospect_id}")
    return _record(row)


def _record(row: sqlite3.Row) -> dict:
    values = dict(row)
    del values["organization_key"]
    values["outreach_gate"] = _gate(values)
    return values


def _gate(record: dict) -> dict:
    if record["suppression_reference"]:
        return {"ready": False, "reasons": ["do_not_contact"]}
    reasons = []
    if record["stage"] != "qualified":
        reasons.append("not_at_qualified_stage")
    for field in ("public_contact_url", "contact_basis", "approval_reference"):
        if not record[field]:
            reasons.append(f"missing_{field}")
    return {"ready": not reasons, "reasons": reasons}


def add(db: sqlite3.Connection, organization: str, source_url: str, next_action: str | None = None) -> dict:
    organization = " ".join(_required(organization, "organization").split())
    with db:
        cursor = db.execute(
            "INSERT INTO prospects(organization,organization_key,source_url,next_action) VALUES(?,?,?,?)",
            (organization, organization.casefold(), _public_url(source_url, "source_url"), next_action),
        )
    return get(db, cursor.lastrowid)


def _change(db: sqlite3.Connection, prospect_id: int, sql: str, parameters: tuple) -> dict:
    with db:
        cursor = db.execute(sql, (*parameters, prospect_id))
        if cursor.rowcount != 1:
            raise ValueError("unknown prospect or change blocked by its current state")
    return get(db, prospect_id)


def stage(db: sqlite3.Connection, prospect_id: int, stage: str, next_action: str | None = None) -> dict:
    if stage not in STAGES:
        raise ValueError(f"unknown stage {stage}")
    return _change(
        db, prospect_id,
        """UPDATE prospects SET approval_reference=CASE WHEN stage=? THEN approval_reference END,
                                 stage=?,next_action=? WHERE id=?""",
        (stage, stage, next_action),
    )


def record_contact(db: sqlite3.Connection, prospect_id: int, contact_url: str, basis: str) -> dict:
    return _change(
        db, prospect_id,
        """UPDATE prospects SET public_contact_url=?, contact_basis=?, approval_reference=NULL
           WHERE id=? AND suppression_reference IS NULL""",
        (_public_url(contact_url, "contact_url"), _required(basis, "contact basis")),
    )


def approve(db: sqlite3.Connection, prospect_id: int, reference: str) -> dict:
    return _change(
        db, prospect_id,
        """UPDATE prospects SET approval_reference=?
           WHERE id=? AND suppression_reference IS NULL AND stage='qualified'
             AND public_contact_url IS NOT NULL AND contact_basis IS NOT NULL""",
        (_required(reference, "approval reference"),),
    )


def suppress(db: sqlite3.Connection, prospect_id: int, reference: str) -> dict:
    return _change(
        db, prospect_id,
        """UPDATE prospects SET suppression_reference=COALESCE(suppression_reference,?),
                                 approval_reference=NULL WHERE id=?""",
        (_required(reference, "opt-out reference"),),
    )


def export(db: sqlite3.Connection) -> list[dict]:
    return [_record(row) for row in db.execute("SELECT * FROM prospects ORDER BY id")]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True, help="Explicit SQLite file path")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
    commands.add_parser("export")
    create = commands.add_parser("add")
    create.add_argument("organization")
    create.add_argument("source_url")
    create.add_argument("--next-action")
    status = commands.add_parser("stage")
    status.add_argument("id", type=int)
    status.add_argument("stage", choices=STAGES)
    status.add_argument("--next-action")
    contact = commands.add_parser("contact")
    contact.add_argument("id", type=int)
    contact.add_argument("contact_url")
    contact.add_argument("basis")
    for name in ("approve", "suppress"):
        command = commands.add_parser(name)
        command.add_argument("id", type=int)
        command.add_argument("reference")
    show = commands.add_parser("show")
    show.add_argument("id", type=int)
    args = parser.parse_args()

    handlers = {
        "init": lambda db: {"initialized": True, "path": str(args.db.resolve())},
        "export": export,
        "add": lambda db: add(db, args.organization, args.source_url, args.next_action),
        "stage": lambda db: stage(db, args.id, args.stage, args.next_action),
        "contact": lambda db: record_contact(db, args.id, args.contact_url, args.basis),
        "approve": lambda db: approve(db, args.id, args.reference),
        "suppress": lambda db: suppress(db, args.id, args.reference),
        "show": lambda db: get(db, args.id),
    }
    try:
        with closing(connect(args.db, initialize=args.command == "init")) as db:
            result = handlers[args.command](db)
    except (sqlite3.Error, ValueError) as exc:
        parser.exit(2, json.dumps({"error": str(exc)}) + "\n")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
