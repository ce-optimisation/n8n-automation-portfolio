from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from commercial_agency import crm


class CRMTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "agency.sqlite"
        self.db = crm.connect(self.path, initialize=True)
        self.addCleanup(self.db.close)
        self.prospect = crm.add(self.db, "Example Studio", "https://example.test/about")["id"]

    def qualify(self):
        crm.stage(self.db, self.prospect, "qualified")
        crm.record_contact(self.db, self.prospect, "https://example.test/contact", "fixture review")
        return crm.approve(self.db, self.prospect, "fixture owner approval")

    def test_gate_needs_qualification_contact_and_approval(self):
        self.assertFalse(crm.get(self.db, self.prospect)["outreach_gate"]["ready"])
        with self.assertRaises(ValueError):
            crm.approve(self.db, self.prospect, "unqualified")
        self.assertTrue(self.qualify()["outreach_gate"]["ready"])

    def test_suppression_survives_reopen_and_blocks_approval(self):
        self.qualify()
        suppressed = crm.suppress(self.db, self.prospect, "fixture opt-out")
        self.assertEqual(suppressed["outreach_gate"], {"ready": False, "reasons": ["do_not_contact"]})
        self.assertIsNone(suppressed["approval_reference"])
        with closing(crm.connect(self.path)) as db:
            with self.assertRaises(ValueError):
                crm.approve(db, self.prospect, "another approval")
            with self.assertRaises(ValueError):
                crm.record_contact(db, self.prospect, "https://example.test/contact", "new basis")
            with self.assertRaises(sqlite3.IntegrityError):
                crm.add(db, "example   studio", "https://example.test/alt")
            self.assertEqual(crm.export(db)[0]["suppression_reference"], "fixture opt-out")

    def test_material_contact_and_stage_changes_clear_approval(self):
        self.qualify()
        updated = crm.record_contact(self.db, self.prospect, "https://example.test/alt", "changed basis")
        self.assertFalse(updated["outreach_gate"]["ready"])
        crm.approve(self.db, self.prospect, "fixture approval")
        crm.stage(self.db, self.prospect, "closed")
        reopened = crm.stage(self.db, self.prospect, "qualified")
        self.assertFalse(reopened["outreach_gate"]["ready"])

    def test_external_database_and_secret_bearing_url_rejected(self):
        other = self.path.with_name("other.sqlite")
        with closing(sqlite3.connect(other)) as db:
            db.execute("CREATE TABLE unrelated (id INTEGER)")
        with self.assertRaisesRegex(ValueError, "other data"):
            crm.connect(other, initialize=True)
        with self.assertRaises(ValueError):
            crm.add(self.db, "Invalid", "https://secret@example.test/path")

    def test_cli_uses_explicit_db_and_exports_json(self):
        new_path = self.path.with_name("with spaces.sqlite")
        command = [sys.executable, "-m", "commercial_agency.crm", "--db", str(new_path)]
        missing = subprocess.run([*command, "export"], capture_output=True, text=True)
        self.assertEqual(missing.returncode, 2)
        self.assertFalse(new_path.exists())
        subprocess.run([*command, "init"], capture_output=True, text=True, check=True)
        subprocess.run([*command, "add", "CLI Fixture", "https://example.test"], capture_output=True,
                       text=True, check=True)
        result = subprocess.run([*command, "export"], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout)[0]["organization"], "CLI Fixture")

    @staticmethod
    def fixture_criteria(points):
        return {name: {"points": points, "evidence": f"fixture:{name}"}
                for name in crm.SCORE_CRITERIA}

    def test_ranking_uses_evidence_without_authorizing_outreach(self):
        another = crm.add(self.db, "Another Fixture", "https://example.test/about")["id"]
        first = self.fixture_criteria(1)
        first["editing_gap"] = {"points": 0, "evidence": None}
        self.assertEqual(crm.score_prospect(self.db, self.prospect, first)["score"], 4)
        crm.score_prospect(self.db, another, self.fixture_criteria(2))
        with closing(crm.connect(self.path)) as db:
            ranked = crm.rank_prospects(db)
            self.assertEqual([p["id"] for p in ranked], [another, self.prospect])
            self.assertEqual(ranked[1]["criteria"], first)
            self.assertFalse(any(p["outreach_gate"]["ready"] for p in ranked))
            self.assertEqual(crm.export(db)[0]["score"], 4)
        crm.suppress(self.db, another, "fixture opt-out")
        self.assertEqual([p["id"] for p in crm.rank_prospects(self.db)], [self.prospect])
        with self.assertRaisesRegex(ValueError, "suppressed"):
            crm.score_prospect(self.db, another, self.fixture_criteria(2))
        self.assertEqual(len(crm.export(self.db)), 2)

    def test_positive_points_need_evidence_and_all_criteria(self):
        invalid = self.fixture_criteria(1)
        invalid["editing_gap"] = {"points": 2, "evidence": None}
        with self.assertRaisesRegex(ValueError, "requires evidence"):
            crm.score_prospect(self.db, self.prospect, invalid)
        del invalid["editing_gap"]
        with self.assertRaisesRegex(ValueError, "exactly"):
            crm.score_prospect(self.db, self.prospect, invalid)
        self.assertEqual(crm.rank_prospects(self.db), [])

    def test_repeat_init_migrates_existing_database_without_losing_data(self):
        with self.db:
            self.db.execute("DROP TABLE prospect_scores")
        with closing(crm.connect(self.path, initialize=True)) as db:
            self.assertEqual(crm.get(db, self.prospect)["organization"], "Example Studio")
            self.assertEqual(crm.score_prospect(db, self.prospect, self.fixture_criteria(1))["score"], 5)

    def research_entry(self, name):
        return {"organization": name, "source_url": "https://example.test",
                "next_action": "Review synthetic evidence", "criteria": self.fixture_criteria(1)}

    def test_research_import_is_idempotent_and_never_changes_suppression(self):
        crm.suppress(self.db, self.prospect, "fixture opt-out")
        batch = [self.research_entry("Example Studio"), self.research_entry("New Fixture")]
        result = crm.import_research(self.db, batch)
        self.assertEqual(result["skipped_existing"], ["Example Studio"])
        self.assertEqual(result["added"][0]["organization"], "New Fixture")
        self.assertEqual(crm.import_research(self.db, batch)["added"], [])
        ranked = crm.rank_prospects(self.db)
        self.assertEqual([item["organization"] for item in ranked], ["New Fixture"])
        self.assertEqual(ranked[0]["stage"], "researched")
        self.assertFalse(ranked[0]["outreach_gate"]["ready"])
        self.assertIsNone(ranked[0]["public_contact_url"])
        self.assertEqual(crm.export(self.db)[0]["suppression_reference"], "fixture opt-out")

    def test_invalid_research_batch_creates_no_records(self):
        first, second = self.research_entry("First Fixture"), self.research_entry("Second Fixture")
        second["criteria"]["editorial_fit"]["points"] = 3
        with self.assertRaises(ValueError):
            crm.import_research(self.db, [first, second])
        self.assertEqual(len(crm.export(self.db)), 1)


if __name__ == "__main__":
    unittest.main()
