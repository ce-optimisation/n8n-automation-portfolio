import json
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from commercial_intelligence.select_candidates import select
from commercial_intelligence.test_assess import brief


NOW = datetime(2026, 11, 25, tzinfo=timezone.utc)


def campaign():
    value = brief()
    value["selection_rules"] = {
        "evidence": "fixture:brief",
        "minimum_duration_ms": 5_000,
        "maximum_duration_ms": 20_000,
        "submission_deadline": "2026-11-30T12:00:00Z",
        "destinations": {"Instagram:brand": "fixture:account rules"},
        "source_windows": [
            {"source_id": "fixture:recording-52", "start_ms": 10_000, "end_ms": 50_000,
             "evidence": "fixture:eligible excerpt"},
        ],
    }
    return value


def clip(clip_id=1, **changes):
    return {
        "clip_id": clip_id,
        "source_id": "fixture:recording-52",
        "destination": "Instagram:brand",
        "status": "ready",
        "safety_status": "clear",
        "start_ms": 10_000,
        "end_ms": 15_000,
        "quality_score": 90,
        "rights": {"status": "met", "campaign": "Synthetic fixture", "source_id": "fixture:recording-52",
                   "destination": "Instagram:brand", "start_ms": 10_000, "end_ms": 40_000,
                   "evidence": "fixture:permission"},
        **changes,
    }


class CandidateTests(unittest.TestCase):
    def test_shortlists_exact_scope_and_ranks_recorded_quality(self):
        result = select(campaign(), [clip(9), clip(3), clip(2, quality_score=91),
                                    clip(6, quality_score=None)], as_of=NOW)
        self.assertEqual(result["shortlisted_clip_ids"], [2, 3, 9, 6])
        self.assertFalse(result["publication_authorized"])
        self.assertIsNone(result["actual_earnings_minor"])
        candidate = result["candidates"][0]
        self.assertEqual(candidate["matched_source_windows"][0]["evidence"], "fixture:eligible excerpt")
        self.assertEqual(candidate["rights"]["evidence"], "fixture:permission")
        self.assertEqual(candidate["destination_evidence"], "fixture:account rules")

    def test_rejection_wrong_source_wrong_destination_and_duration_fail(self):
        cases = [
            (clip(status="rejected"), "clip_status"),
            (clip(safety_status="flagged"), "safety_status"),
            (clip(source_id="fixture:different-recording"), "source_window"),
            (clip(start_ms=9_999), "source_window"),
            (clip(start_ms=40_000, end_ms=50_001), "source_window"),
            (clip(destination="Instagram:other"), "destination"),
            (clip(end_ms=14_999), "duration"),
        ]
        for value, expected in cases:
            with self.subTest(check=expected):
                result = select(campaign(), [value], as_of=NOW)
                self.assertEqual(result["shortlisted_clip_ids"], [])
                self.assertIn(expected, result["candidates"][0]["failed_clip_checks"])

    def test_cannot_join_two_separated_authorised_windows(self):
        value = campaign()
        first = value["selection_rules"]["source_windows"][0]
        first["end_ms"] = 15_000
        value["selection_rules"]["source_windows"].append({**first, "start_ms": 16_000, "end_ms": None})
        result = select(value, [clip(end_ms=20_000)], as_of=NOW)
        self.assertEqual(result["candidates"][0]["matched_source_windows"], [])
        self.assertEqual(result["shortlisted_clip_ids"], [])

    def test_wrong_expired_or_missing_permission_is_held(self):
        rights = clip()["rights"]
        missing_end = {key: val for key, val in rights.items() if key != "end_ms"}
        cases = [
            clip(rights=None),
            clip(rights=missing_end),
            clip(rights={**rights, "campaign": "Different campaign"}),
            clip(rights={**rights, "source_id": "fixture:different-recording"}),
            clip(rights={**rights, "destination": "Instagram:other"}),
            clip(rights={**rights, "end_ms": 14_999}),
            clip(rights={**rights, "expires_at": "2026-11-25T00:00:00Z"}),
        ]
        for value in cases:
            with self.subTest(clip=value):
                result = select(campaign(), [value], as_of=NOW)
                self.assertEqual(result["candidates"][0]["status"], "needs_evidence")
                self.assertEqual(result["shortlisted_clip_ids"], [])

    def test_dates_and_brief_checks_fail_closed(self):
        value = campaign()
        value["selection_rules"]["submission_deadline"] = "2026-11-25T00:00:00Z"
        result = select(value, [clip()], as_of=NOW)
        self.assertEqual(result["candidates"][0]["failed_clip_checks"], ["submission_deadline"])
        value = campaign()
        del value["checks"]["campaign_open"]
        result = select(value, [clip()], as_of=NOW)
        self.assertEqual(result["candidates"][0]["status"], "needs_evidence")

    def test_invalid_inputs_fail(self):
        for values in ([clip(), clip()], [clip(0)], [clip(end_ms=10_000)], [clip(quality_score=True)]):
            with self.subTest(clips=values):
                with self.assertRaises(ValueError):
                    select(campaign(), values, as_of=NOW)
        missing_end = campaign()
        del missing_end["selection_rules"]["source_windows"][0]["end_ms"]
        with self.assertRaisesRegex(ValueError, "end_ms must be supplied"):
            select(missing_end, [clip()], as_of=NOW)
        with self.assertRaisesRegex(ValueError, "UTC"):
            select(campaign(), [clip()], as_of=datetime(2026, 11, 25))

    def test_cli_returns_expected_json_without_modifying_inputs(self):
        command = [sys.executable, "-m", "commercial_intelligence.select_candidates"]
        with tempfile.TemporaryDirectory() as directory:
            a = Path(directory) / "campaign.json"
            b = Path(directory) / "clips.json"
            a.write_text(json.dumps(campaign()), encoding="utf-8")
            b.write_text(json.dumps([clip()]), encoding="utf-8")
            result = subprocess.run([*command, str(a), str(b), "--as-of", "2026-11-25T00:00:00Z"],
                                    capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(result.stdout), select(campaign(), [clip()], as_of=NOW))
            self.assertEqual(json.loads(a.read_text(encoding="utf-8")), campaign())
            self.assertEqual(json.loads(b.read_text(encoding="utf-8")), [clip()])


if __name__ == "__main__":
    unittest.main()
