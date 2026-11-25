import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from commercial_intelligence.assess import REQUIRED_CHECKS, assess


def brief():
    return {
        "campaign": "Synthetic fixture",
        "checks": {name: {"status": "met", "evidence": f"fixture:{name}"} for name in REQUIRED_CHECKS},
        "economics": {
            "model": "fixed_cpm",
            "currency": "USD",
            "cpm_minor": 100,
            "fee_basis_points": 1_000,
            "incremental_cost_minor": 100,
            "minimum_payout_minor": 300,
            "maximum_payout_minor": 1_000,
            "remaining_budget_minor": 800,
            "evidence": "fixture:terms",
        },
        "approved_view_scenarios": [1_000, 3_000, 10_000],
    }


class AssessmentTests(unittest.TestCase):
    def test_estimates_have_threshold_cap_fee_and_cost(self):
        result = assess(brief())
        self.assertEqual(result["eligibility"]["status"], "meets_recorded_checks")
        amounts = result["profitability_estimate"]["scenarios"]
        self.assertEqual([s["estimated_gross_minor"] for s in amounts], [0, 300, 800])
        self.assertEqual([s["estimated_fee_minor"] for s in amounts], [0, 30, 80])
        self.assertEqual([s["estimated_contribution_minor"] for s in amounts], [-100, 170, 620])
        self.assertIsNone(result["actual_earnings_minor"])
        self.assertFalse(result["publication_authorized"])

    def test_missing_checks_stay_unknown(self):
        value = brief()
        del value["checks"]["footage_authorized"]
        result = assess(value)
        self.assertEqual(result["eligibility"]["status"], "needs_evidence")
        self.assertEqual(result["eligibility"]["unknown"], ["footage_authorized"])

    def test_explicit_unmet_takes_precedence(self):
        value = brief()
        del value["checks"]["footage_authorized"]
        value["checks"]["region_eligible"] = {"status": "unmet", "evidence": "fixture:region condition"}
        value["checks"]["branding"] = {"status": "unmet", "evidence": "fixture:branding condition"}
        result = assess(value)
        self.assertEqual(result["eligibility"]["status"], "ineligible")
        self.assertEqual(result["eligibility"]["unmet"], ["region_eligible", "branding"])

    def test_evidence_is_required_for_definite_status(self):
        value = brief()
        del value["checks"]["destination_authorized"]["evidence"]
        with self.assertRaisesRegex(ValueError, "evidence"):
            assess(value)

    def test_shared_pool_does_not_become_cpm(self):
        value = brief()
        value["economics"]["model"] = "shared_pool"
        estimate = assess(value)["profitability_estimate"]
        self.assertEqual(estimate["status"], "not_estimable")
        self.assertTrue(all(x["estimated_gross_minor"] is None for x in estimate["scenarios"]))

    def test_unknown_budget_is_distinct_from_zero(self):
        value = brief()
        value["economics"]["remaining_budget_minor"] = None
        unknown = assess(value)["profitability_estimate"]
        self.assertEqual(unknown["unknown_terms"], ["remaining_budget_minor"])
        self.assertEqual(unknown["scenarios"][-1]["estimated_gross_minor"], 1_000)
        value["economics"]["remaining_budget_minor"] = 0
        depleted = assess(value)["profitability_estimate"]
        self.assertEqual(depleted["unknown_terms"], [])
        self.assertEqual(depleted["scenarios"][-1]["estimated_gross_minor"], 0)

    def test_invalid_numeric_inputs_fail(self):
        for name, invalid in (("cpm_minor", True), ("fee_basis_points", 10_001),
                              ("minimum_payout_minor", 1_001), ("incremental_cost_minor", -1)):
            with self.subTest(name=name):
                value = brief()
                value["economics"][name] = invalid
                with self.assertRaises(ValueError):
                    assess(value)

    def test_cli_json_and_error_exit(self):
        command = [sys.executable, "-m", "commercial_intelligence.assess"]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "brief.json"
            path.write_text(json.dumps(brief()), encoding="utf-8")
            result = subprocess.run([*command, str(path)], capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(result.stdout), assess(brief()))
            path.write_text("{}", encoding="utf-8")
            result = subprocess.run([*command, str(path)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn("error", json.loads(result.stderr))


if __name__ == "__main__":
    unittest.main()
