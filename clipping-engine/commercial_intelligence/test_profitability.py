import sqlite3
import unittest

from commercial_intelligence.profitability import COSTS_SCHEMA, report, validate_cost

BASE = """
CREATE TABLE creators (id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE recordings (id INTEGER PRIMARY KEY, creator_id INTEGER);
CREATE TABLE clips (id INTEGER PRIMARY KEY, recording_id INTEGER);
CREATE TABLE campaign_briefs (id INTEGER PRIMARY KEY);
CREATE TABLE campaign_submissions (id INTEGER PRIMARY KEY, brief_id INTEGER, clip_id INTEGER, status TEXT);
CREATE TABLE campaign_receipts (id INTEGER PRIMARY KEY, submission_id INTEGER, currency TEXT,
    gross_minor INTEGER, fee_minor INTEGER);
"""


class ProfitabilityTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.executescript(BASE + COSTS_SCHEMA)
        self.db.executescript("""
            INSERT INTO creators VALUES (1,'IShowSpeed');
            INSERT INTO recordings VALUES (1,1);
            INSERT INTO clips VALUES (10,1),(11,1);
            INSERT INTO campaign_briefs VALUES (1);
            INSERT INTO campaign_submissions VALUES (1,1,10,'approved'),(2,1,11,'approved'),(3,1,11,'rejected');
            INSERT INTO campaign_receipts VALUES (1,1,'USD',5000,500);
            INSERT INTO campaign_costs (brief_id,clip_id,kind,currency,amount_minor,minutes,note,recorded_at)
                VALUES (1,NULL,'expense','USD',300,NULL,'caption font licence',0),
                       (1,10,'owner_minutes',NULL,NULL,12,'review',0);
        """)

    def test_cash_pending_and_time_kept_separate(self):
        r = report(self.db, hourly_rate_minor=2000, rate_currency="GBP")
        c = r["campaigns"][1]
        self.assertEqual(c["by_currency"]["USD"], {"gross_minor": 5000, "fees_minor": 500, "net_receipts_minor": 4500,
                                                   "expenses_minor": 300, "cash_profit_minor": 4200})
        self.assertEqual(c["approved_pending_payment"], 1)   # counted, never valued
        self.assertEqual(c["rejected"], 1)
        self.assertEqual(c["owner_minutes"], 12)
        self.assertEqual(c["owner_time_valuation"], {"currency": "GBP", "owner_time_value_minor": 400})
        self.assertEqual(r["clips"][10]["by_currency"]["USD"]["net_receipts_minor"], 4500)
        self.assertEqual(r["creators"]["IShowSpeed"]["USD"], {"gross_minor": 5000, "net_minor": 4500})

    def test_no_valuation_without_rate_and_cost_validation(self):
        self.assertIsNone(report(self.db)["campaigns"][1]["owner_time_valuation"])
        with self.assertRaises(ValueError):
            validate_cost("expense", currency="US", amount_minor=1, minutes=None, note="x")
        with self.assertRaises(ValueError):
            validate_cost("owner_minutes", currency=None, amount_minor=5, minutes=3, note="x")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("INSERT INTO campaign_costs (kind,currency,amount_minor,minutes,note,recorded_at) "
                            "VALUES ('expense','USD',NULL,NULL,'bad',0)")


if __name__ == "__main__":
    unittest.main()
