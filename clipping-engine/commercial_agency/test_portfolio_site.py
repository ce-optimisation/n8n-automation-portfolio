import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from commercial_agency.portfolio_site import MARKER, build, verify

IDENTITY = {"name": "Pillarcut", "tagline": "Short-form clips from long-form shows.", "email": "hello@example.test",
            "owner": "James Callaghan"}


@unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg required")
class PortfolioSiteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pf-"))
        self.src = self.tmp / "clip.mp4"
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=1080x1920:rate=30:duration=6",
                        "-f", "lavfi", "-i", "sine=frequency=440:duration=6", "-c:v", "libx264", "-preset", "ultrafast",
                        "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(self.src)], check=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def manifest(self, n=2):
        return {"items": [{"clip_id": i, "title": f"Founder hook {i}", "brand": "Operator Notes", "collection": "business",
                           "featured": i == 1, "order": i, "video_path": str(self.src),
                           "rights_evidence": "Email 2026-11-28 ref X"} for i in range(1, n + 1)]}

    def test_build_is_small_linked_and_leak_free(self):
        out = self.tmp / "site"
        r = build(self.manifest(), IDENTITY, out)
        self.assertTrue(r["verification"]["ok"], r["verification"]["problems"])
        self.assertEqual(len(r["items"]), 2)
        self.assertTrue(all(i["bytes"] <= 10 * 1024 * 1024 for i in r["items"]))
        page = (out / "index.html").read_text(encoding="utf-8")
        self.assertIn("have not hired or endorsed us", page)
        self.assertIn('content="noindex"', page)
        self.assertNotIn(str(self.src), page)
        release = json.loads((out / "release.json").read_text(encoding="utf-8"))
        self.assertNotIn("rights_evidence", json.dumps(release))          # internal evidence stays private
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=width,height", "-of", "csv=p=0",
                                str(out / release["items"][0]["file"])], capture_output=True, text=True).stdout
        self.assertIn("720,1280", probe)

    def test_removal_on_rebuild_and_refuses_foreign_folders(self):
        out = self.tmp / "site"
        build(self.manifest(2), IDENTITY, out)
        r = build(self.manifest(1), IDENTITY, out)
        self.assertEqual(len(list((out / "media").glob("*.mp4"))), 1)
        foreign = self.tmp / "docs"
        foreign.mkdir()
        (foreign / "important.txt").write_text("keep")
        with self.assertRaises(ValueError):
            build(self.manifest(1), IDENTITY, foreign)
        self.assertTrue((foreign / "important.txt").exists())

    def test_verify_detects_leaks_and_broken_links(self):
        out = self.tmp / "site"
        build(self.manifest(1), IDENTITY, out)
        (out / "index.html").write_text('<a href="missing.html">x</a> C:\\Users\\James\\AppData', encoding="utf-8")
        report = verify(out)
        self.assertFalse(report["ok"])
        self.assertTrue(any("missing.html" in p for p in report["problems"]))
        self.assertTrue(any("AppData" in p or "Users" in p for p in report["problems"]))


if __name__ == "__main__":
    unittest.main()
