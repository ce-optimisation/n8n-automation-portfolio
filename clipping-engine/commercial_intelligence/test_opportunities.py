from datetime import datetime, timezone
import math
import random
import unittest

from commercial_intelligence.opportunities import Assumptions, expected_payout, rank, score

NOW = datetime(2026, 11, 26, tzinfo=timezone.utc)
BASE = {"id": "a", "cpm_usd": 1.0, "min_payout_views": 10_000, "max_payout_usd": 500.0, "budget_remaining_usd": 7260,
        "deadline": "2026-12-31", "uk_eligible": "unknown", "platforms": ["tiktok", "instagram", "youtube"],
        "rights_basis": "campaign_licence"}


class OpportunityTest(unittest.TestCase):
    def test_closed_form_matches_simulation(self):
        rng = random.Random(7)
        mu, sigma = math.log(2000), 1.4
        sims = []
        for _ in range(200_000):
            v = math.exp(rng.gauss(mu, sigma))
            sims.append(0 if v < 10_000 else min(v / 1000 * 1.0, 50.0))
        self.assertAlmostEqual(expected_payout(1.0, 10_000, 50.0, 2000, 1.4), sum(sims) / len(sims), delta=0.05)

    def test_higher_cpm_can_rank_lower_after_threshold_and_cap(self):
        high_cpm = {**BASE, "id": "high", "cpm_usd": 3.0, "min_payout_views": 200_000, "max_payout_usd": 20.0}
        ranking = rank([high_cpm, BASE], now=NOW)
        self.assertEqual(ranking[0]["id"], "a")

    def test_rights_eligibility_and_deadline_gates(self):
        self.assertFalse(score({**BASE, "rights_basis": "unknown"}, now=NOW)["actionable"])
        self.assertFalse(score({**BASE, "uk_eligible": "no"}, now=NOW)["actionable"])
        self.assertEqual(score({**BASE, "deadline": "2026-11-01"}, now=NOW)["expected_net"], 0.0)
        yes = score({**BASE, "uk_eligible": "yes"}, now=NOW)["expected_revenue"]
        unk = score(BASE, now=NOW)["expected_revenue"]
        self.assertAlmostEqual(yes, unk * 2, delta=0.02)

    def test_budget_caps_revenue_and_costs_subtract(self):
        tiny = score({**BASE, "budget_remaining_usd": 5}, Assumptions(median_views=500_000), now=NOW)
        self.assertLessEqual(tiny["expected_revenue"], 5)
        self.assertIn("capped by remaining budget 5", tiny["reasons"])
        self.assertLess(tiny["expected_net"], tiny["expected_revenue"])


if __name__ == "__main__":
    unittest.main()
