import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from commercial_intelligence.brief_engine import analyse
from commercial_intelligence import campaign_production as cp

ROOT = Path(__file__).resolve().parents[1]
FONT = ROOT / "clipengine" / "assets" / "fonts" / "Anton-Regular.ttf"
EVIDENCE_DIR = os.environ.get("CAMPAIGN_RENDER_EVIDENCE")

BRIEF_A = b"""TravelCo vertical campaign
Use footage from https://example.test/owned-fixture between 00:00:00 - 00:00:40.
Clips must be 12-20 seconds, vertical 9:16 at 1080x1920.
Focus on moments about travel and hotels. No politics.
Captions are required. Only original audio, no background music.
Add the TravelCo logo in the top right corner.
Post to TikTok and YouTube Shorts. Include #ad #TravelCo and tag @travelco.
Deadline 2026-12-15.
"""
BRIEF_B = b"""GearCo square campaign
Use footage from https://example.test/owned-fixture.
Clips must be 8-12 seconds in square 1:1 format at 1080p.
Do not add captions. Use only the provided approved track for music.
Show the sponsor banner across the bottom.
Post to Instagram. Disclose with #sponsored. Deadline 2026-12-20.
"""

SEGMENTS = [
    {"start": 0.0, "end": 4.0, "text": "Welcome back everyone to the stream."},
    {"start": 4.2, "end": 9.0, "text": "Why would you never book a hotel at the airport?"},
    {"start": 9.1, "end": 14.0, "text": "Because the travel price is insane, literally double!"},
    {"start": 14.1, "end": 19.0, "text": "So we found a cheaper hotel in Tokyo instead."},
    {"start": 19.5, "end": 24.0, "text": "Anyway the election news today was wild."},
    {"start": 24.2, "end": 30.0, "text": "Let me read some chat messages now."},
]


def _ffmpeg(*args):
    subprocess.run([shutil.which("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", *args],
                   check=True, capture_output=True, timeout=300)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
class CampaignProductionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="campaign-prod-"))
        cls.source = cls.tmp / "owned-fixture.mp4"
        _ffmpeg("-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=30",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=30", "-c:v", "libx264", "-preset", "ultrafast",
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(cls.source))
        cls.logo = cls.tmp / "logo.png"
        _ffmpeg("-f", "lavfi", "-i", "color=c=0xD02020:size=256x256", "-frames:v", "1", str(cls.logo))
        cls.track = cls.tmp / "approved.m4a"
        _ffmpeg("-f", "lavfi", "-i", "sine=frequency=880:duration=30", "-c:a", "aac", str(cls.track))

    @classmethod
    def tearDownClass(cls):
        if EVIDENCE_DIR:
            shutil.copytree(cls.tmp, Path(EVIDENCE_DIR), dirs_exist_ok=True)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _cut(self, moment, name):
        out = self.tmp / name
        _ffmpeg("-ss", f"{moment.start:.3f}", "-to", f"{moment.end:.3f}", "-i", str(self.source),
                "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(out))
        return out

    def _rules(self, analysis):
        rules = analysis["proposed_selection_rules"]
        self.assertTrue(rules["requires_owner_confirmation"])
        return rules  # test stands in for the owner's confirmation

    def test_two_briefs_produce_measurably_different_compliant_clips(self):
        a, b = (analyse(x, source_ref=n) for x, n in ((BRIEF_A, "fixture/a"), (BRIEF_B, "fixture/b")))
        moments_a = cp.select_moments(SEGMENTS, a, self._rules(a))
        self.assertTrue(moments_a)
        top = moments_a[0]
        self.assertTrue(12 <= top.duration <= 20)
        self.assertNotIn("election", top.text)              # banned topic excluded
        self.assertTrue(any("travel" in r or "hotel" in r for r in top.reasons))
        moments_b = cp.select_moments(SEGMENTS, b, self._rules(b))
        self.assertTrue(all(8 <= m.duration <= 12 for m in moments_b))

        (pa,) = cp.build_profiles(a, logo_path=str(self.logo))
        (pb,) = cp.build_profiles(b, sponsor_text="Sponsored by GearCo", approved_track=str(self.track))
        self.assertEqual((pa.width, pa.height, pa.captions), (1080, 1920, True))
        self.assertEqual((pb.width, pb.height, pb.captions), (1080, 1080, False))

        evidence = {}
        for analysis, profile, moment, title in ((a, pa, top, "Never book an airport hotel"),
                                                 (b, pb, moments_b[0], "Gear pick")):
            cut = self._cut(moment, f"{profile.name}-cut.mp4")
            out = self.tmp / f"{profile.name}.mp4"
            manifest = cp.apply_campaign_layers(cut, out, profile, FONT)
            meta = cp.build_metadata(analysis, title, "Clip from an owned fixture.")
            report = cp.validate(out, profile, analysis, manifest, meta)
            failed = [r for r in report["results"] if r["passed"] is False]
            self.assertEqual(failed, [], json.dumps(failed, indent=1))
            self.assertTrue(report["automatic_passed"])
            evidence[profile.name] = {"moment": [moment.start, moment.end], "probe": report["probe"],
                                      "checks": [(r["check"], r["passed"], r["method"]) for r in report["results"]],
                                      "manifest": manifest, "metadata": meta}
        self.assertNotEqual(evidence[pa.name]["probe"]["height"], evidence[pb.name]["probe"]["height"])
        checks_a = {c for c, *_ in evidence[pa.name]["checks"]}
        self.assertTrue({"brand_overlay_present", "no_added_music", "captions", "hashtag_present",
                         "mention_present", "disclosure_present"} <= checks_a)
        (self.tmp / "evidence.json").write_text(json.dumps(evidence, indent=2, default=str), encoding="utf-8")

    def test_validation_detects_noncompliant_render(self):
        a = analyse(BRIEF_A, source_ref="fixture/a")
        (pa,) = cp.build_profiles(a, logo_path=str(self.logo))
        long_cut = self.tmp / "too-long.mp4"
        _ffmpeg("-t", "25", "-i", str(self.source), "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
                str(long_cut))
        out = self.tmp / "too-long-out.mp4"
        manifest = cp.apply_campaign_layers(long_cut, out, pa, FONT)
        manifest["layers"] = [l for l in manifest["layers"] if l["type"] != "logo"]  # pretend logo was omitted
        meta = cp.build_metadata(a, "t")
        meta["description"] = "missing tags"
        report = cp.validate(out, pa, a, manifest, meta)
        failed = {r["check"] for r in report["results"] if r["passed"] is False}
        self.assertIn("duration", failed)
        self.assertIn("brand_overlay_present", failed)
        self.assertIn("hashtag_present", failed)
        self.assertFalse(report["automatic_passed"])

    def test_profile_refuses_missing_or_prohibited_assets(self):
        a = analyse(BRIEF_A, source_ref="fixture/a")
        with self.assertRaises(ValueError):
            cp.build_profiles(a)  # logo required
        with self.assertRaises(ValueError):
            cp.build_profiles(a, logo_path=str(self.logo), approved_track=str(self.track))  # original audio only
        b = analyse(BRIEF_B, source_ref="fixture/b")
        with self.assertRaises(ValueError):
            cp.build_profiles(b, sponsor_text="x")  # approved track required


if __name__ == "__main__":
    unittest.main()
