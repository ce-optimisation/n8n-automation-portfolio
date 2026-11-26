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
SCORE_CRITERIA = ("repeatable_source", "offer_fit", "editorial_fit", "corporate_route", "editing_gap")

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
CREATE TABLE IF NOT EXISTS prospect_scores (
    prospect_id INTEGER PRIMARY KEY REFERENCES prospects(id),
    score INTEGER NOT NULL CHECK (score BETWEEN 0 AND 10),
    evidence_json TEXT NOT NULL
);
"""


def connect(path: Path, *, initialize: bool = False) -> sqlite3.Connection:
    """Open an explicit database path. Only initialise creates a file."""
    path = path.resolve()
    connection = sqlite3.connect(f"{path.as_uri()}?mode={'rwc' if initialize else 'rw'}", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        application_id = connection.execute("PRAGMA application_id").fetchone()[0]
        if initialize and application_id == 0:
            if connection.execute("SELECT 1 FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchone():
                raise ValueError("refusing to initialise a database that already contains other data")
            connection.executescript(f"BEGIN IMMEDIATE;{SCHEMA}PRAGMA application_id = {APPLICATION_ID};COMMIT;")
        elif application_id != APPLICATION_ID:
            raise ValueError("path does not point to an agency CRM database")
        elif initialize:
            connection.executescript(f"BEGIN IMMEDIATE;{SCHEMA}COMMIT;")
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
    rows = db.execute(
        """SELECT p.*,s.score,s.evidence_json FROM prospects p LEFT JOIN prospect_scores s ON s.prospect_id=p.id
           ORDER BY p.id"""
    )
    return [_scored_record(row) for row in rows]


def _score_criteria(criteria: dict) -> tuple[int, dict]:
    if not isinstance(criteria, dict) or set(criteria) != set(SCORE_CRITERIA):
        raise ValueError("provide exactly these criteria: " + ", ".join(SCORE_CRITERIA))
    evidence = {}
    for name in SCORE_CRITERIA:
        entry = criteria[name]
        if not isinstance(entry, dict) or set(entry) != {"points", "evidence"}:
            raise ValueError(f"{name} requires points and evidence")
        points, reference = entry["points"], entry["evidence"]
        if type(points) is not int or not 0 <= points <= 2:
            raise ValueError(f"{name} points must be an integer from 0 to 2")
        if reference is not None and (not isinstance(reference, str) or not reference.strip()):
            raise ValueError(f"{name} evidence must be a non-empty reference or null")
        if points and reference is None:
            raise ValueError(f"{name} requires evidence for a positive score")
        evidence[name] = {"points": points, "evidence": reference.strip() if reference is not None else None}
    return sum(entry["points"] for entry in evidence.values()), evidence


def score_prospect(db: sqlite3.Connection, prospect_id: int, criteria: dict) -> dict:
    total, evidence = _score_criteria(criteria)
    with db:
        cursor = db.execute(
            """INSERT INTO prospect_scores(prospect_id,score,evidence_json)
               SELECT id,?,? FROM prospects WHERE id=? AND suppression_reference IS NULL
               ON CONFLICT(prospect_id) DO UPDATE SET score=excluded.score,evidence_json=excluded.evidence_json""",
            (total, json.dumps(evidence), prospect_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("unknown or suppressed prospect")
    return {"id": prospect_id, "score": total, "criteria": evidence}


def import_research(db: sqlite3.Connection, prospects: list) -> dict:
    """Add researched organisations and scores without changing any existing CRM records."""
    if not isinstance(prospects, list) or not prospects:
        raise ValueError("research import requires a non-empty array")
    records = []
    keys = set()
    for item in prospects:
        if not isinstance(item, dict) or set(item) != {"organization", "source_url", "next_action", "criteria"}:
            raise ValueError("each research entry requires organization, source_url, next_action and criteria")
        if any(not isinstance(item[name], str) for name in ("organization", "source_url", "next_action")):
            raise ValueError("organization, source_url and next_action must be strings")
        organization = " ".join(_required(item["organization"], "organization").split())
        key = organization.casefold()
        if key in keys:
            raise ValueError("research batch contains duplicate organisations")
        keys.add(key)
        source_url = _public_url(item["source_url"], "source_url")
        next_action = _required(item["next_action"], "next_action")
        total, evidence = _score_criteria(item["criteria"])
        records.append((organization, key, source_url, next_action, total, evidence))

    added, skipped = [], []
    with db:
        for organization, key, source_url, next_action, total, evidence in records:
            cursor = db.execute(
                """INSERT INTO prospects(organization,organization_key,source_url,next_action) VALUES(?,?,?,?)
                   ON CONFLICT(organization_key) DO NOTHING""",
                (organization, key, source_url, next_action),
            )
            if not cursor.rowcount:
                skipped.append(organization)
                continue
            prospect_id = cursor.lastrowid
            db.execute("INSERT INTO prospect_scores(prospect_id,score,evidence_json) VALUES(?,?,?)",
                       (prospect_id, total, json.dumps(evidence)))
            added.append({"id": prospect_id, "organization": organization, "score": total})
    return {"added": added, "skipped_existing": skipped}


def rank_prospects(db: sqlite3.Connection) -> list[dict]:
    """Rank scored, open prospects and retain their independent outreach gates."""
    rows = db.execute(
        """SELECT p.*, s.score, s.evidence_json FROM prospects p JOIN prospect_scores s ON s.prospect_id=p.id
           WHERE p.suppression_reference IS NULL AND p.stage IN ('researched','qualified')
           ORDER BY s.score DESC, p.id"""
    )
    return [_scored_record(row) for row in rows]


def _scored_record(row: sqlite3.Row) -> dict:
    prospect = _record(row)
    evidence = prospect.pop("evidence_json")
    prospect["criteria"] = json.loads(evidence) if evidence is not None else None
    return prospect


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True, help="Explicit SQLite file path")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
    commands.add_parser("export")
    commands.add_parser("rank")
    score = commands.add_parser("score")
    score.add_argument("id", type=int)
    score.add_argument("criteria_json", type=Path)
    research = commands.add_parser("import-research")
    research.add_argument("research_json", type=Path)
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
        "rank": rank_prospects,
        "score": lambda db: score_prospect(db, args.id, json.loads(args.criteria_json.read_text(encoding="utf-8-sig"))),
        "import-research": lambda db: import_research(db, json.loads(args.research_json.read_text(encoding="utf-8-sig"))),
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
    except (OSError, sqlite3.Error, ValueError) as exc:
        parser.exit(2, json.dumps({"error": str(exc)}) + "\n")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
