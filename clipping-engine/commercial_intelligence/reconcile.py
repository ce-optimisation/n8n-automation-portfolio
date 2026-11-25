"""Reconcile recorded campaign earnings with actual settlement entries."""

from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re


def amount(value):
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("Amounts must be finite, nonnegative decimal values") from exc
    if not result.is_finite() or result < 0:
        raise ValueError("Amounts must be finite, nonnegative decimal values")
    return result


def indexed(rows, key):
    result = {}
    for row in rows:
        identifier = row.get(key)
        if not isinstance(identifier, str) or not identifier.strip() or identifier in result:
            raise ValueError(f"Every {key} must be present and unique")
        result[identifier] = row
    return result


def reconcile(earnings, settlements):
    """Match actual receipts to provider earning IDs, without netting currencies."""
    by_id = indexed(earnings, "earning_id")
    received = {}
    unmatched = []
    for receipt_id, receipt in indexed(settlements, "receipt_id").items():
        earning_id = receipt.get("earning_id")
        net = amount(receipt.get("net_received"))
        currency = receipt.get("currency")
        if not isinstance(currency, str) or not re.fullmatch("[A-Z]{3}", currency):
            raise ValueError("Currency must be a three-letter uppercase code")
        earning = by_id.get(earning_id)
        if earning is None or currency != earning.get("currency"):
            unmatched.append(receipt_id)
            continue
        received[earning_id] = received.get(earning_id, Decimal(0)) + net

    report = []
    for earning_id, earning in by_id.items():
        currency = earning.get("currency")
        if not isinstance(currency, str) or not re.fullmatch("[A-Z]{3}", currency):
            raise ValueError("Currency must be a three-letter uppercase code")
        expected = amount(earning.get("net_due"))
        settled = received.get(earning_id, Decimal(0))
        status = "matched" if settled == expected else "shortfall" if settled < expected else "overpayment"
        report.append({"earning_id": earning_id, "currency": currency, "net_due": str(expected),
                       "net_received": str(settled), "status": status})
    return {"earnings": report, "unmatched_receipt_ids": unmatched}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ledger", type=Path)
    args = parser.parse_args()
    try:
        data = json.loads(args.ledger.read_text(encoding="utf-8-sig"), parse_float=Decimal)
        output = reconcile(data["earnings"], data["settlements"])
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        parser.exit(2, f"{exc}\n")
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
