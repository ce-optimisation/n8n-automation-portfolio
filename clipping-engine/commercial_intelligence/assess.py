"""Assess an evidence-labelled campaign brief from local JSON."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


REQUIRED_CHECKS = (
    "campaign_open", "account_eligible", "region_eligible", "footage_authorized",
    "destination_authorized", "editing_rules_satisfied", "submission_deadline_met",
)


def _object(value: Any, name: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _amount(value: Any, name: str, *, maximum: int | None = None) -> int:
    if type(value) is not int or value < 0 or (maximum is not None and value > maximum):
        limit = f" no greater than {maximum}" if maximum is not None else ""
        raise ValueError(f"{name} must be a non-negative integer{limit}")
    return value


def _optional_amount(values: dict, name: str) -> int | None:
    value = values.get(name)
    return None if value is None else _amount(value, name)


def _checks(values: Any) -> list[dict]:
    supplied = _object(values, "checks")
    results = []
    for name in dict.fromkeys((*REQUIRED_CHECKS, *sorted(supplied))):
        entry = _object(supplied.get(name, {"status": "unknown"}), f"checks.{name}")
        status = entry.get("status")
        if status not in ("met", "unmet", "unknown"):
            raise ValueError(f"checks.{name}.status must be met, unmet or unknown")
        evidence = entry.get("evidence")
        if evidence is not None or status != "unknown":
            evidence = _text(evidence, f"checks.{name}.evidence")
        results.append({"name": name, "status": status, "evidence": evidence})
    return results


def _estimate(economics: dict, scenarios: Any) -> dict:
    if not isinstance(scenarios, list) or not scenarios:
        raise ValueError("approved_view_scenarios must be a non-empty array")
    costs = _amount(economics.get("incremental_cost_minor"), "incremental_cost_minor")
    currency = _text(economics.get("currency"), "currency")
    if len(currency) != 3 or not currency.isascii() or not currency.isalpha() or currency != currency.upper():
        raise ValueError("currency must be an uppercase three-letter code")
    model = economics.get("model")
    if model not in ("fixed_cpm", "shared_pool"):
        raise ValueError("model must be fixed_cpm or shared_pool")
    views = [_amount(value, "approved view scenario") for value in scenarios]
    evidence = _text(economics.get("evidence"), "economics.evidence")
    result = {"model": model, "currency": currency, "incremental_cost_minor": costs, "evidence": evidence}
    if model == "shared_pool":
        return {
            **result,
            "status": "not_estimable",
            "reason": "Approved views alone do not determine a shared-pool payout.",
            "scenarios": [{"approved_views": value, "estimated_gross_minor": None,
                           "estimated_fee_minor": None, "estimated_contribution_minor": None}
                          for value in views],
        }

    rate = _amount(economics.get("cpm_minor"), "cpm_minor")
    fee_bps = _amount(economics.get("fee_basis_points"), "fee_basis_points", maximum=10_000)
    minimum = _optional_amount(economics, "minimum_payout_minor")
    maximum = _optional_amount(economics, "maximum_payout_minor")
    remaining = _optional_amount(economics, "remaining_budget_minor")
    if minimum is not None and maximum is not None and minimum > maximum:
        raise ValueError("minimum payout exceeds maximum payout")

    estimates = []
    for value in views:
        gross = value * rate // 1_000
        for cap in (maximum, remaining):
            if cap is not None:
                gross = min(gross, cap)
        if minimum is not None and gross < minimum:
            gross = 0
        fee = (gross * fee_bps + 9_999) // 10_000
        estimates.append({"approved_views": value, "estimated_gross_minor": gross,
                          "estimated_fee_minor": fee, "estimated_contribution_minor": gross - fee - costs})
    terms = {"minimum_payout_minor": minimum, "maximum_payout_minor": maximum, "remaining_budget_minor": remaining}
    return {
        **result,
        "status": "scenario_only",
        "cpm_minor": rate,
        "fee_basis_points": fee_bps,
        **terms,
        "unknown_terms": [name for name, value in terms.items() if value is None],
        "scenarios": estimates,
    }


def assess(brief: Any) -> dict:
    """Evaluate a structured brief without granting authority or executing a submission."""
    brief = _object(brief, "brief")
    campaign = _text(brief.get("campaign"), "campaign")
    checks = _checks(brief.get("checks", {}))
    unmet = [item["name"] for item in checks if item["status"] == "unmet"]
    unknown = [item["name"] for item in checks if item["status"] == "unknown"]
    state = "ineligible" if unmet else "needs_evidence" if unknown else "meets_recorded_checks"
    return {
        "campaign": campaign,
        "eligibility": {"status": state, "checks": checks, "unmet": unmet, "unknown": unknown},
        "profitability_estimate": _estimate(_object(brief.get("economics"), "economics"),
                                            brief.get("approved_view_scenarios")),
        "actual_earnings_minor": None,
        "publication_authorized": False,
    }


def main() -> int:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("brief", type=Path)
    args = cli.parse_args()
    try:
        result = assess(json.loads(args.brief.read_text(encoding="utf-8-sig")))
    except (OSError, ValueError) as exc:
        cli.exit(2, json.dumps({"error": str(exc)}) + "\n")
    print(json.dumps(result, indent=2, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
