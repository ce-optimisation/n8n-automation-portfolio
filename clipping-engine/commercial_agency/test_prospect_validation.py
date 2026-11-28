import csv
from datetime import date
from pathlib import Path
import tempfile
import unittest

from commercial_agency.prospect_validation import validate

FIELDS = ["name", "entity_status", "country", "opportunity_status", "status_checked_at", "route", "contact_basis",
          "caption_requirement", "portfolio_category"]
BASE = {"name": "Good Media Ltd", "entity_status": "Active; London", "country": "UK",
        "opportunity_status": "open (closes 11 Dec 2026)", "status_checked_at": "2026-11-28", "route": "careers page",
        "contact_basis": "respond-to-posting", "caption_requirement": "n/a", "portfolio_category": "business podcast"}


class ProspectValidationTest(unittest.TestCase):
    def run_rows(self, rows, cleared=frozenset({"business podcast"})):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "p.csv"
            with p.open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=FIELDS)
                w.writeheader()
                w.writerows(rows)
            return {r["name"]: r for r in validate(p, today=date(2026, 11, 28), cleared_collections=set(cleared))["prospects"]}

    def test_ready_only_needs_owner_approval(self):
        r = self.run_rows([BASE])["Good Media Ltd"]
        self.assertEqual(r["decision"], "ready_after_owner_approval")

    def test_exclusions_holds_and_flags(self):
        rows = [
            {**BASE, "name": "Closed Co", "opportunity_status": "closed (Podnews: expired)"},
            {**BASE, "name": "Dnc Co", "contact_basis": "do-not-contact (recruitment only)"},
            {**BASE, "name": "Unverified Co", "contact_basis": "corporate-subscriber marketing", "entity_status": "not found"},
            {**BASE, "name": "Solo Studio", "entity_status": "treat as sole trader", "contact_basis": "corporate-subscriber marketing"},
            {**BASE, "name": "Us Restorations", "country": "US", "caption_requirement": "Captions required"},
            {**BASE, "name": "Stale Co", "status_checked_at": "2026-11-20"},
            {**BASE, "name": "Good Media Limited"},
        ]
        r = self.run_rows([BASE] + rows)
        self.assertEqual(r["Closed Co"]["decision"], "excluded")
        self.assertEqual(r["Dnc Co"]["decision"], "excluded")
        self.assertIn("verified active company", " ".join(r["Unverified Co"]["blockers"]))
        self.assertEqual(r["Solo Studio"]["decision"], "blocked")
        self.assertTrue(any("CAN-SPAM" in n for n in r["Us Restorations"]["notes"]))
        self.assertTrue(any("reviewed-caption" in n for n in r["Us Restorations"]["notes"]))
        self.assertTrue(any("recheck" in b for b in r["Stale Co"]["blockers"]))
        self.assertIn("duplicate", r["Good Media Limited"]["blockers"][0])

    def test_no_cleared_portfolio_blocks_sending(self):
        r = self.run_rows([BASE], cleared=frozenset())["Good Media Ltd"]
        self.assertEqual(r["decision"], "blocked")
        self.assertTrue(any("portfolio" in b for b in r["blockers"]))


if __name__ == "__main__":
    unittest.main()
