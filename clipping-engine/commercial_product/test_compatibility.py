import unittest

from clipengine.hardware import HardwareInfo
from commercial_product.compatibility import evaluate


def hw(cores, ram, cuda=0, vram=0.0, ffmpeg="ffmpeg"):
    return HardwareInfo(os="Windows 11", cpu="x", cpu_cores=cores, ram_gb=ram, cuda_devices=cuda,
                        gpu_name="RTX" if cuda else "", vram_gb=vram, ffmpeg=ffmpeg, ffprobe=ffmpeg)


class CompatibilityTest(unittest.TestCase):
    def test_profiles(self):
        cases = {
            "budget laptop": (hw(2, 4), 50, "fail", "tiny"),
            "minimum": (hw(4, 8), 30, "warn", "base"),
            "mid desktop": (hw(8, 16), 200, "warn", "small"),       # no GPU -> warn
            "gpu desktop": (hw(12, 32, 1, 12.0), 500, "pass", "large-v3"),
        }
        for name, (info, free, overall, model) in cases.items():
            with self.subTest(name):
                r = evaluate(info, free_gb=free, python=(3, 12), windows_build=22631)
                self.assertEqual(r["overall"], overall)
                self.assertEqual(r["transcription"]["model"], model)

    def test_blocking_conditions_have_remedies(self):
        r = evaluate(hw(8, 16, ffmpeg=None), free_gb=5, python=(3, 14), windows_build=18363)
        failed = {c["check"]: c["remedy"] for c in r["checks"] if c["status"] == "fail"}
        self.assertEqual(set(failed), {"os", "python", "storage", "ffmpeg"})
        self.assertTrue(all(failed.values()))


if __name__ == "__main__":
    unittest.main()
