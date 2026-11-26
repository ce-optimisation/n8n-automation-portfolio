"""Extract traceable selection rules from a local, explicitly labelled campaign brief."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re

from .assess import _text
from .select_candidates import _utc


_FIELDS = ("Campaign", "Duration", "Source window", "Destination", "Submission deadline")
_REPEATABLE = {"Source window", "Destination"}
_CLOCK = re.compile(r"([0-9]+):([0-5][0-9]):([0-5][0-9])(?:\.([0-9]{1,3}))?")
_DURATION = re.compile(r"([0-9]+)\s*(?:-|–|to)\s*([0-9]+)\s+seconds?", re.IGNORECASE)
_DESTINATION = re.compile(r"[A-Za-z][A-Za-z0-9_.-]*:[A-Za-z0-9_@.-]+")


def _clock(value: str) -> int:
    match = _CLOCK.fullmatch(value)
    if not match:
        raise ValueError("Use HH:MM:SS or HH:MM:SS.mmm for the source time.")
    hours, minutes, seconds, fractions = match.groups()
    return (int(hours) * 3_600 + int(minutes) * 60 + int(seconds)) * 1_000 + int((fractions or "0").ljust(3, "0"))


def _source_window(value: str, evidence: str) -> dict:
    columns = [part.strip() for part in value.split("|")]
    if len(columns) != 3:
        raise ValueError("Use source ID | start time | end time, with the literal 'end of source' if expressly permitted.")
    source, start, end = columns
    start_ms = _clock(start)
    end_ms = None if end == "end of source" else _clock(end)
    if end_ms is not None and end_ms <= start_ms:
        raise ValueError("The source window end must be after its start.")
    return {"source_id": _text(source, "source ID"), "start_ms": start_ms, "end_ms": end_ms, "evidence": evidence}


def _parse_field(label: str, value: str, evidence: str):
    value = _text(value, label)
    if label == "Duration":
        match = _DURATION.fullmatch(value)
        if not match:
            raise ValueError("Use an explicit minimum and maximum in whole seconds.")
        low, high = map(int, match.groups())
        if not 0 < low <= high:
            raise ValueError("Duration minimum must be positive and no greater than its maximum.")
        return low * 1_000, high * 1_000
    if label == "Source window":
        return _source_window(value, evidence)
    if label == "Destination":
        if not _DESTINATION.fullmatch(value):
            raise ValueError("Use an exact platform:account key.")
        return value
    if label == "Submission deadline":
        return _utc(value, label).isoformat()
    return value


def interpret(raw: bytes, *, source_ref: str) -> dict:
    """Preserve source lines as evidence. Unparsed or conflicting text holds the rules."""
    source_ref = _text(source_ref, "source_ref")
    document_hash = hashlib.sha256(raw).hexdigest()
    fields: dict[str, list[dict]] = {label: [] for label in _FIELDS}
    holds = []
    for line_no, original in enumerate(raw.decode("utf-8-sig").splitlines(), start=1):
        if not original.strip():
            continue
        label, sep, value = original.strip().partition(":")
        record = {"line": line_no, "source_text": original}
        if not sep or label.strip() not in fields:
            holds.append({**record, "reason": "Unrecognized brief clause. Review it before using these rules."})
            continue
        label = label.strip()
        evidence = f"{source_ref}#line={line_no};sha256={document_hash}"
        try:
            parsed = _parse_field(label, value, evidence)
        except ValueError as exc:
            holds.append({**record, "reason": str(exc)})
        else:
            fields[label].append({**record, "evidence": evidence, "value": parsed})

    for label, records in fields.items():
        if not records:
            holds.append({"field": label, "reason": "Missing a supported exact rule."})
        if label not in _REPEATABLE and len(records) > 1:
            holds.append({"field": label, "lines": [record["line"] for record in records],
                          "reason": "Multiple clauses for a single-valued rule. Resolve them explicitly."})
    destinations = {entry["value"]: entry["evidence"] for entry in fields["Destination"]}
    if len(destinations) != len(fields["Destination"]):
        holds.append({"field": "Destination", "reason": "Duplicate destination clauses need review."})

    result = {
        "schema_version": 1, "source_ref": source_ref, "source_sha256": document_hash,
        "status": "needs_review" if holds else "extracted_for_review",
        "evidence": fields, "holds": holds, "selection_rules": None,
        "publication_authorized": False,
    }
    if not holds:
        minimum, maximum = fields["Duration"][0]["value"]
        result["campaign"] = fields["Campaign"][0]["value"]
        result["selection_rules"] = {
            "evidence": f"{source_ref}#sha256={document_hash}",
            "minimum_duration_ms": minimum,
            "maximum_duration_ms": maximum,
            "submission_deadline": fields["Submission deadline"][0]["value"],
            "destinations": destinations,
            "source_windows": [entry["value"] for entry in fields["Source window"]],
        }
    return result


def main() -> int:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("brief", type=Path)
    cli.add_argument("--source-ref", required=True, help="Durable reference to the copied brief")
    args = cli.parse_args()
    try:
        result = interpret(args.brief.read_bytes(), source_ref=args.source_ref)
    except (OSError, ValueError) as exc:
        cli.exit(2, json.dumps({"error": str(exc)}) + "\n")
    print(json.dumps(result, indent=2, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
