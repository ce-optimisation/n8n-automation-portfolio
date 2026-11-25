import unittest

from commercial_intelligence.reconcile import reconcile


class ReconcileTest(unittest.TestCase):
    def test_partial_receipts_match_exact_decimal_due(self):
        report = reconcile(
            [{"earning_id": "A", "currency": "USD", "net_due": "0.30"}],
            [{"receipt_id": "one", "earning_id": "A", "currency": "USD", "net_received": "0.1"},
             {"receipt_id": "two", "earning_id": "A", "currency": "USD", "net_received": "0.2"}],
        )
        self.assertEqual(report["earnings"][0]["status"], "matched")

    def test_unmatched_payment_does_not_count_towards_due(self):
        report = reconcile(
            [{"earning_id": "A", "currency": "USD", "net_due": "20"}],
            [{"receipt_id": "one", "earning_id": "A", "currency": "GBP", "net_received": "20"}],
        )
        self.assertEqual(report["unmatched_receipt_ids"], ["one"])
        self.assertEqual(report["earnings"][0]["status"], "shortfall")

    def test_duplicate_receipt_is_rejected(self):
        receipt = {"receipt_id": "one", "earning_id": "A", "currency": "USD", "net_received": "10"}
        with self.assertRaisesRegex(ValueError, "unique"):
            reconcile([], [receipt, receipt])

    def test_non_finite_amount_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "finite"):
            reconcile([{"earning_id": "A", "currency": "USD", "net_due": "NaN"}], [])
