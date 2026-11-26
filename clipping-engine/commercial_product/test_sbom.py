from pathlib import Path
import tempfile
import unittest

from commercial_product.sbom import build, classify, risks


class SbomTest(unittest.TestCase):
    def test_classification(self):
        self.assertEqual(classify("MIT License"), "permissive")
        self.assertEqual(classify("GNU Lesser General Public License v3 (LGPLv3)"), "weak_copyleft")
        self.assertEqual(classify("GPL-3.0-or-later"), "copyleft")
        self.assertEqual(classify("AGPL-3.0"), "strong_copyleft_network")
        self.assertEqual(classify(""), "unknown")

    def test_build_inventories_environment_and_flags_missing_font_notice(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "Anton-Regular.ttf").write_bytes(b"font")
            bom = build(Path(d))
        names = {c["name"].lower() for c in bom["components"]}
        self.assertIn("pip", names)
        self.assertEqual(bom["bomFormat"], "CycloneDX")
        self.assertIn("Anton-Regular.ttf", risks(bom)["needs_review"]["missing_notice"])


if __name__ == "__main__":
    unittest.main()
