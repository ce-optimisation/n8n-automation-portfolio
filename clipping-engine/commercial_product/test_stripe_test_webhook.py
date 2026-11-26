import hashlib
import hmac
import json
import unittest

from stripe_test_webhook import verify_test_event


SECRET = b"whsec_fixture_only"
NOW = 1760000000


def event(live=False):
    return json.dumps({"id": "evt_synthetic", "type": "customer.subscription.updated",
                       "object": "event", "livemode": live}).encode()


def header(body, timestamp=NOW, secret=SECRET):
    message = str(timestamp).encode() + b"." + body
    signature = hmac.new(secret, message, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={signature}"


class StripeWebhookTests(unittest.TestCase):
    def verify(self, body, signed):
        return verify_test_event(body, signed, SECRET, now=NOW)

    def test_accepts_sandbox_event_with_multiple_signatures(self):
        body = event()
        signed = header(body, NOW - 300) + ",v1=" + "0" * 64 + ",v0=ignore"
        self.assertEqual(self.verify(body, signed)["id"], "evt_synthetic")

    def test_rejects_tampering_wrong_secret_and_legacy_only_signature(self):
        body = event()
        for incoming, signed in ((body + b" ", header(body)), (body, header(body, secret=b"other")),
                                 (body, header(body).replace("v1=", "v0="))):
            with self.subTest(signed=signed), self.assertRaisesRegex(ValueError, "signature"):
                self.verify(incoming, signed)

    def test_rejects_old_future_and_duplicate_timestamps(self):
        body = event()
        for signed in (header(body, NOW - 301), header(body, NOW + 301), header(body) + f",t={NOW}"):
            with self.subTest(signed=signed), self.assertRaisesRegex(ValueError, "timestamp"):
                self.verify(body, signed)

    def test_rejects_signed_live_event(self):
        body = event(live=True)
        with self.assertRaisesRegex(ValueError, "sandbox"):
            self.verify(body, header(body))
