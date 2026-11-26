import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from licence import Access, resolve


VERIFIER = os.environ.get("CLIPENGINE_TEST_OPENSSL")


@unittest.skipUnless(VERIFIER, "Set CLIPENGINE_TEST_OPENSSL to a trusted OpenSSL 3 binary.")
class LicenceTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.openssl = Path(VERIFIER)
        self.private = self.root / "private.pem"
        self.public = self.root / "public.pem"
        self.run_openssl("genpkey", "-algorithm", "ED25519", "-out", self.private)
        self.run_openssl("pkey", "-in", self.private, "-pubout", "-out", self.public)

    def run_openssl(self, *args):
        subprocess.run([str(self.openssl), *map(str, args)], check=True, capture_output=True, timeout=8)

    def signed(self, state, **overrides):
        payload = json.dumps(dict(
            {"version": 1, "state": state, "account": "customer-1", "issued_at": 100,
             "valid_until": 200, "offline_until": 300}, **overrides
        )).encode()
        (self.root / "claims").write_bytes(payload)
        self.run_openssl("pkeyutl", "-sign", "-inkey", self.private, "-rawin",
                         "-in", self.root / "claims", "-out", self.root / "signature")
        return payload, (self.root / "signature").read_bytes()

    def access(self, bundle, now, **kwargs):
        return resolve(*bundle, public_key=self.public, openssl=self.openssl,
                       account="customer-1", now=now, **kwargs)

    def test_trial_expires_without_grace(self):
        bundle = self.signed("trial")
        self.assertEqual(self.access(bundle, 199), Access(True, "trial"))
        self.assertEqual(self.access(bundle, 200), Access(False, "expired"))

    def test_active_offline_grace_and_final_expiry(self):
        bundle = self.signed("active")
        self.assertEqual(self.access(bundle, 199), Access(True, "active"))
        self.assertEqual(self.access(bundle, 200), Access(True, "offline_grace"))
        self.assertEqual(self.access(bundle, 300), Access(False, "expired"))

    def test_past_due_cancelled_refunded_deny_access(self):
        for state in ("past_due", "cancelled", "refunded"):
            with self.subTest(state=state):
                self.assertEqual(self.access(self.signed(state), 150), Access(False, state))

    def test_tampering_other_account_future_and_rollback_fail_closed(self):
        payload, signature = self.signed("active")
        with self.assertRaisesRegex(ValueError, "signature"):
            self.access((payload + b" ", signature), 140)
        with self.assertRaisesRegex(ValueError, "claims"):
            self.access(self.signed("active", account="other"), 140)
        with self.assertRaisesRegex(ValueError, "dates"):
            self.access((payload, signature), 99)
        with self.assertRaisesRegex(ValueError, "outdated"):
            self.access((payload, signature), 140, minimum_issued_at=101)
        with self.assertRaisesRegex(ValueError, "dates"):
            self.access(self.signed("active", offline_until=999999), 140)
