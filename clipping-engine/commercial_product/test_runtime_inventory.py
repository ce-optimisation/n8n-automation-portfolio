import hashlib
import tempfile
import unittest
from pathlib import Path

from commercial_product.runtime_inventory import inventory


class RuntimeInventoryTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)

    def distribution(self, folder, headers):
        path = self.root / f"{folder}.dist-info"
        path.mkdir()
        (path / "METADATA").write_text(headers, encoding="utf-8")

    def test_distinguishes_declared_legacy_and_missing_evidence(self):
        self.distribution("missing-1", "Metadata-Version: 2.1\nName: missing\nVersion: 1\nLicense: UNKNOWN\n")
        self.distribution("legacy-1", "Metadata-Version: 2.1\nName: legacy\nVersion: 1\n"
                          "Classifier: License :: OSI Approved :: MIT License\nLicense: legacy detail\n")
        self.distribution("declared-1", "Metadata-Version: 2.4\nName: declared\nVersion: 1\n"
                          "License-Expression: MIT OR BSD-3-Clause\nLicense-File: LICENSE.txt\n")
        observed = inventory([self.root])["python_distributions"]
        self.assertEqual([item["name"] for item in observed], ["declared", "legacy", "missing"])
        self.assertEqual([item["license_evidence"] for item in observed], [
            "declared_expression", "legacy_metadata", "missing",
        ])
        self.assertEqual(observed[0]["license_expression"], "MIT OR BSD-3-Clause")
        self.assertEqual(observed[0]["declared_license_files"], ["LICENSE.txt"])
        self.assertEqual(len(observed[0]["metadata_text_sha256"]), 64)
        self.assertNotIn("legacy detail", str(observed))

    def test_binary_hashes_are_evidence_without_a_license_claim(self):
        fake = self.root / "runtime.exe"
        fake.write_bytes(b"fixture, never executed")
        self.assertEqual(inventory([], [fake])["binary_artifacts"], [{
            "name": "runtime.exe", "size": fake.stat().st_size, "sha256": hashlib.sha256(fake.read_bytes()).hexdigest(),
            "license_evidence": "not_assessed",
        }])
        with self.assertRaisesRegex(ValueError, "ordinary binary"):
            inventory([], [self.root])

    def test_duplicate_normalized_distribution_is_rejected(self):
        self.distribution("same-name-1", "Name: same-name\nVersion: 1\n")
        self.distribution("same_name-2", "Name: same_name\nVersion: 2\n")
        with self.assertRaisesRegex(ValueError, "Multiple installed"):
            inventory([self.root])
