"""Preflight and rank local clip metadata against a structured campaign brief."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from .assess import assess, _amount, _object, _text


def _array(value: Any, name: str, *, allow_empty=False) -> list:
    if not isinstance(value, list) or (not allow_empty and not value):
        raise ValueError(f"{name} must be {'an' if allow_empty else 'a non-empty'} array")
    return value


def _utc(value: Any, name: str) -> datetime:
    value = _text(value, name)
    try:
        dt = datetime.fromisoformat(value.removesuffix("Z") + ("+00:00" if value.endswith("Z") else ""))
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO 8601 UTC timestamp") from exc
    if dt.utcoffset() != timezone.utc.utcoffset(dt):
        raise ValueError(f"{name} must use UTC")
    return dt


def _interval(value: dict, name: str, *, open_end=False) -> tuple[int, int | None]:
    start = _amount(value.get("start_ms"), f"{name}.start_ms")
    if "end_ms" not in value:
        raise ValueError(f"{name}.end_ms must be supplied")
    supplied = value.get("end_ms")
    end = None if open_end and supplied is None else _amount(supplied, f"{name}.end_ms")
    if end is not None and end <= start:
        raise ValueError(f"{name}.end_ms must be greater than start_ms")
    return start, end


def _rules(value: Any) -> dict:
    value = _object(value, "selection_rules")
    rules = {
        "evidence": _text(value.get("evidence"), "selection_rules.evidence"),
        "minimum_duration_ms": _amount(value.get("minimum_duration_ms"), "minimum_duration_ms"),
        "maximum_duration_ms": _amount(value.get("maximum_duration_ms"), "maximum_duration_ms"),
        "submission_deadline": _utc(value.get("submission_deadline"), "submission_deadline"),
        "destinations": {},
        "source_windows": {},
    }
    if not 0 < rules["minimum_duration_ms"] <= rules["maximum_duration_ms"]:
        raise ValueError("expected 0 < minimum_duration_ms <= maximum_duration_ms")
    for name, reason in _object(value.get("destinations"), "destinations").items():
        rules["destinations"][_text(name, "destination")] = _text(reason, "destination evidence")
    if not rules["destinations"]:
        raise ValueError("destinations must contain at least one destination")
    for item in _array(value.get("source_windows"), "source_windows"):
        item = _object(item, "source window")
        source_id = _text(item.get("source_id"), "source_id")
        start, end = _interval(item, "source window", open_end=True)
        evidence = _text(item.get("evidence"), "source window evidence")
        rules["source_windows"].setdefault(source_id, []).append((start, end, evidence))
    return rules


def _rights(value: Any, campaign: str, source: str, destination: str, start: int, end: int,
            now: datetime) -> dict:
    if value is None:
        return {"check": "clip_redistribution_rights", "status": "unknown", "evidence": None}
    record = _object(value, "rights")
    status = record.get("status")
    if status not in ("met", "unmet", "unknown"):
        raise ValueError("rights.status must be met, unmet or unknown")
    if status == "unknown":
        return {"check": "clip_redistribution_rights", "status": status, "evidence": None}
    evidence = _text(record.get("evidence"), "rights.evidence")
    if record.get("campaign") != campaign or record.get("source_id") != source or record.get("destination") != destination:
        return {"check": "clip_redistribution_rights", "status": "unknown", "evidence": evidence,
                "reason": "Rights evidence does not name this campaign, source and exact destination."}
    if record.get("start_ms") is None or "end_ms" not in record:
        return {"check": "clip_redistribution_rights", "status": "unknown", "evidence": evidence,
                "reason": "Rights evidence does not record a permitted time window."}
    permitted_start, permitted_end = _interval(record, "rights", open_end=True)
    if start < permitted_start or (permitted_end is not None and end > permitted_end):
        return {"check": "clip_redistribution_rights", "status": "unknown", "evidence": evidence,
                "reason": "The clip extends outside the rights evidence window."}
    expiry = record.get("expires_at")
    if expiry is not None and now >= _utc(expiry, "rights.expires_at"):
        return {"check": "clip_redistribution_rights", "status": "unknown", "evidence": evidence,
                "reason": "The rights evidence has expired."}
    return {"check": "clip_redistribution_rights", "status": status, "evidence": evidence}


def select(brief: Any, clips: Any, *, as_of: datetime) -> dict:
    """Return evidence and shortlist for human review. Never approve a clip or schedule a post."""
    if as_of.utcoffset() != timezone.utc.utcoffset(as_of):
        raise ValueError("as_of must use UTC")
    assessment = assess(brief)
    rules = _rules(brief.get("selection_rules"))
    campaign = assessment["campaign"]
    eligibility = assessment["eligibility"]
    candidates = []
    seen = set()

    for entry in _array(clips, "clips", allow_empty=True):
        item = _object(entry, "clip")
        clip_id = _amount(item.get("clip_id"), "clip_id")
        if clip_id == 0 or clip_id in seen:
            raise ValueError("clip IDs must be positive and unique")
        seen.add(clip_id)
        source = _text(item.get("source_id"), "clip.source_id")
        destination = _text(item.get("destination"), "clip.destination")
        start, end = _interval(item, "clip")
        score = item.get("quality_score")
        if score is not None:
            score = _amount(score, "quality_score", maximum=100)
        clip_status = _text(item.get("status"), "clip.status")
        safety = _text(item.get("safety_status"), "clip.safety_status")
        rights = _rights(item.get("rights"), campaign, source, destination, start, end, as_of)
        allowed = rules["source_windows"].get(source, [])
        windows = [{"start_ms": lo, "end_ms": hi, "evidence": evidence} for lo, hi, evidence in allowed
                   if start >= lo and (hi is None or end <= hi)]
        destination_evidence = rules["destinations"].get(destination)
        gates = {
            "clip_status": clip_status in ("ready", "approved"),
            "safety_status": safety == "clear",
            "source_window": bool(windows),
            "destination": destination_evidence is not None,
            "duration": rules["minimum_duration_ms"] <= end - start <= rules["maximum_duration_ms"],
            "submission_deadline": as_of < rules["submission_deadline"],
        }
        failed = [name for name, ok in gates.items() if not ok]
        if rights["status"] == "unmet":
            failed.append(rights["check"])
        unknown = [rights["check"]] if rights["status"] == "unknown" else []
        if failed or eligibility["unmet"]:
            decision = "ineligible"
        elif unknown or eligibility["unknown"]:
            decision = "needs_evidence"
        else:
            decision = "shortlisted_for_review"
        candidates.append({
            "clip_id": clip_id, "source_id": source, "destination": destination, "status": decision,
            "clip_status": clip_status, "safety_status": safety,
            "start_ms": start, "end_ms": end, "duration_ms": end - start, "quality_score": score,
            "gate_results": gates, "failed_clip_checks": failed, "unknown_clip_checks": unknown,
            "matched_source_windows": windows, "destination_evidence": destination_evidence, "rights": rights,
        })

    candidates.sort(key=lambda row: (row["quality_score"] is None, -(row["quality_score"] or 0), row["clip_id"]))
    return {
        **assessment,
        "as_of": as_of.isoformat(),
        "selection_rules_evidence": rules["evidence"],
        "submission_deadline": rules["submission_deadline"].isoformat(),
        "candidates": candidates,
        "shortlisted_clip_ids": [item["clip_id"] for item in candidates if item["status"] == "shortlisted_for_review"],
    }


def main() -> int:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("brief", type=Path)
    cli.add_argument("clips", type=Path)
    cli.add_argument("--as-of", help="ISO 8601 UTC time, defaults to current UTC")
    args = cli.parse_args()
    try:
        when = datetime.now(timezone.utc) if args.as_of is None else _utc(args.as_of, "as_of")
        brief = json.loads(args.brief.read_text(encoding="utf-8-sig"))
        clips = json.loads(args.clips.read_text(encoding="utf-8-sig"))
        result = select(brief, clips, as_of=when)
    except (OSError, ValueError) as exc:
        cli.exit(2, json.dumps({"error": str(exc)}) + "\n")
    print(json.dumps(result, indent=2, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
