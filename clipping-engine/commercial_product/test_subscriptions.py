import hashlib
import hmac
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from commercial_product.licence import resolve
from commercial_product.stripe_test_webhook import verify_test_event
from commercial_product.subscriptions import DAY, Event, Ledger, from_stripe, sign

T0 = 1_800_000_000
OPENSSL = os.environ.get("CLIPENGINE_TEST_OPENSSL")


def ev(i, kind, created, **kw):
    return Event(id=f"evt_{i}", provider="test", kind=kind, subscription_id="sub_1", customer_id="cus_1",
                 created=created, **kw)


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "billing.sqlite"
        self.l = Ledger(self.path)
        self.l.apply(ev(1, "started", T0, period_end=T0 + 30 * DAY, plan="pro", workspaces=2), now=T0)

    def tearDown(self):
        self.l.db.close()
        self.dir.cleanup()

    def test_duplicate_and_out_of_order_events_are_safe(self):
        self.assertEqual(self.l.apply(ev(2, "paid", T0 + 30 * DAY, period_end=T0 + 60 * DAY)), "applied")
        self.assertEqual(self.l.apply(ev(2, "paid", T0 + 30 * DAY, period_end=T0 + 60 * DAY)), "duplicate")
        self.assertEqual(self.l.apply(ev(3, "payment_failed", T0 + 1 * DAY)), "stale")
        self.assertEqual(self.l.subscription("sub_1")["status"], "active")
        self.assertEqual(self.l.report()["stale_events"], 1)

    def test_failed_payment_grace_then_lockout_then_recovery(self):
        self.l.apply(ev(2, "payment_failed", T0 + 30 * DAY))
        self.assertEqual(self.l.snapshot("sub_1", now=T0 + 31 * DAY)["state"], "active")
        self.assertEqual(self.l.snapshot("sub_1", now=T0 + 38 * DAY)["state"], "past_due")
        self.l.apply(ev(3, "paid", T0 + 39 * DAY, period_end=T0 + 69 * DAY))
        snap = self.l.snapshot("sub_1", now=T0 + 40 * DAY)
        self.assertEqual(snap["state"], "active")
        self.assertLessEqual(snap["valid_until"] - snap["issued_at"], 2 * DAY)
        self.assertLessEqual(snap["offline_until"] - snap["valid_until"], 3 * DAY)

    def test_cancellation_keeps_paid_period_and_refund_is_immediate(self):
        self.l.apply(ev(2, "cancel_scheduled", T0 + 5 * DAY))
        self.l.apply(ev(3, "cancelled", T0 + 6 * DAY, period_end=T0 + 30 * DAY))
        self.assertEqual(self.l.snapshot("sub_1", now=T0 + 10 * DAY)["state"], "active")
        self.assertEqual(self.l.snapshot("sub_1", now=T0 + 31 * DAY)["state"], "cancelled")
        self.l.apply(ev(4, "refunded", T0 + 7 * DAY))
        self.assertEqual(self.l.snapshot("sub_1", now=T0 + 8 * DAY)["state"], "refunded")

    def test_activation_limit_and_persistence(self):
        self.l.activate("sub_1", "dev-a", now=T0)
        self.l.activate("sub_1", "dev-b", now=T0)
        with self.assertRaises(ValueError):
            self.l.activate("sub_1", "dev-c", now=T0)
        self.l.deactivate("sub_1", "dev-a", now=T0)
        self.l.activate("sub_1", "dev-c", now=T0)
        self.l.db.close()
        self.l = Ledger(self.path)  # durable across restarts
        self.assertEqual(self.l.subscription("sub_1")["plan"], "pro")
        self.assertEqual(self.l.report()["mrr_pence"], 2900)

    def test_rejects_foreign_customer_and_unknown_plan(self):
        bad = Event(id="evt_x", provider="t", kind="paid", subscription_id="sub_1", customer_id="cus_2", created=T0 + 1)
        with self.assertRaises(ValueError):
            self.l.apply(bad)
        with self.assertRaises(ValueError):
            self.l.apply(ev(9, "plan_changed", T0 + 2, plan="enterprise"))
        self.assertIsNone(self.l.db.execute("SELECT 1 FROM events WHERE id='evt_x'").fetchone())


class StripeMappingTest(unittest.TestCase):
    def test_signed_sandbox_event_flows_into_ledger_and_live_is_refused(self):
        secret = b"whsec_test_fixture"
        body = json.dumps({"id": "evt_s1", "object": "event", "livemode": False, "created": T0,
                           "type": "customer.subscription.created",
                           "data": {"object": {"id": "sub_s", "customer": "cus_s", "status": "trialing",
                                               "current_period_end": T0 + 14 * DAY,
                                               "metadata": {"plan": "solo"}}}}).encode()
        sig = hmac.new(secret, f"{T0}.".encode() + body, hashlib.sha256).hexdigest()
        event = verify_test_event(body, f"t={T0},v1={sig}", secret, now=T0 + 5)
        with tempfile.TemporaryDirectory() as d:
            ledger = Ledger(Path(d) / "b.sqlite")
            self.assertEqual(ledger.apply(from_stripe(event), now=T0), "applied")
            self.assertEqual(ledger.subscription("sub_s")["status"], "trial")
            ledger.db.close()
        with self.assertRaises(ValueError):
            from_stripe({**event, "livemode": True})
        self.assertIsNone(from_stripe({**event, "type": "product.created"}))


@unittest.skipUnless(OPENSSL, "Set CLIPENGINE_TEST_OPENSSL to a trusted OpenSSL 3 binary.")
class SignedSnapshotTest(unittest.TestCase):
    def test_ledger_snapshot_verifies_with_customer_resolver(self):
        openssl = Path(OPENSSL)
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            subprocess.run([str(openssl), "genpkey", "-algorithm", "ED25519", "-out", str(d / "k")], check=True, capture_output=True)
            subprocess.run([str(openssl), "pkey", "-in", str(d / "k"), "-pubout", "-out", str(d / "k.pub")], check=True, capture_output=True)
            ledger = Ledger(d / "b.sqlite")
            ledger.apply(ev(1, "started", T0, period_end=T0 + 30 * DAY), now=T0)
            payload, sig = sign(ledger.snapshot("sub_1", now=T0 + DAY), private_key=d / "k", openssl=openssl)
            access = resolve(payload, sig, public_key=d / "k.pub", openssl=openssl, account="cus_1", now=T0 + DAY + 60)
            self.assertTrue(access.allowed)
            offline = resolve(payload, sig, public_key=d / "k.pub", openssl=openssl, account="cus_1", now=T0 + 4 * DAY)
            self.assertEqual(offline.reason, "offline_grace")
            ledger.db.close()


if __name__ == "__main__":
    unittest.main()
