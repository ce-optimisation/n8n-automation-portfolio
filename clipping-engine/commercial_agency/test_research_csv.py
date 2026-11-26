import unittest

from commercial_agency.crm import _score_criteria
from commercial_agency.research_csv import convert_row

ROW = {"name": "Example Ltd", "website": "https://example.co.uk/", "segment": "podcast",
       "longform_url": "https://youtube.com/@example", "demand_evidence_url": "https://jobs.example/123 ; https://x",
       "contact_route": "hello@example.co.uk as published on https://example.co.uk/contact",
       "subscriber_type": "Corporate subscriber: EXAMPLE LTD", "confidence": "high",
       "demand_evidence": "Shorts editor role", "shortform_observation": ""}


class ResearchCsvTest(unittest.TestCase):
    def test_scores_are_valid_for_crm_and_conservative(self):
        item = convert_row(ROW)
        total, _ = _score_criteria(item["criteria"])
        self.assertEqual(total, 2 + 2 + 1 + 2 + 1)
        self.assertEqual(item["source_url"], "https://jobs.example/123")
        self.assertIn("owner approval", item["next_action"])

    def test_sole_trader_and_gambling_are_downgraded(self):
        item = convert_row({**ROW, "subscriber_type": "appears to be a sole trader",
                            "demand_evidence": "Editor for betting brand"})
        self.assertEqual(item["criteria"]["corporate_route"], {"points": 0, "evidence": None})
        self.assertEqual(item["criteria"]["editorial_fit"]["points"], 0)
        self.assertIn("ASA", item["next_action"])
        _score_criteria(item["criteria"])

    def test_unverified_route_scores_at_most_one(self):
        item = convert_row({**ROW, "contact_route": "contact form (not verified)", "subscriber_type": "Likely limited company"})
        self.assertEqual(item["criteria"]["corporate_route"]["points"], 1)


if __name__ == "__main__":
    unittest.main()
