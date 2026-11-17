"""Run pytest and print ONE concise, deterministic summary (counts, failures, skips with reasons) instead of a log.

    python scripts/test_report.py [pytest args...]      e.g.  python scripts/test_report.py tests/test_portfolio.py -k youtube

Writes test_report.json beside the repo's data (not committed). Exit code is pytest's.
"""
import json
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main(argv):
    xml = Path(tempfile.mkdtemp()) / "junit.xml"
    rc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={xml}", *argv],
                        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode
    if not xml.exists():
        print("pytest produced no report (collection error?)")
        return rc or 2
    root = ET.parse(xml).getroot()
    suite = root if root.tag == "testsuite" else root.find("testsuite")
    cases = list(root.iter("testcase"))
    out = {"passed": 0, "failed": [], "skipped": [], "errors": []}
    for c in cases:
        name = f"{c.get('classname', '').split('.')[-1]}::{c.get('name')}"
        if c.find("failure") is not None:
            out["failed"].append({"test": name, "why": (c.find("failure").get("message") or "")[:160]})
        elif c.find("error") is not None:
            out["errors"].append({"test": name, "why": (c.find("error").get("message") or "")[:160]})
        elif c.find("skipped") is not None:
            out["skipped"].append({"test": name, "why": (c.find("skipped").get("message") or "")[:100]})
        else:
            out["passed"] += 1
    out["seconds"] = round(float(suite.get("time", 0)), 1) if suite is not None else None
    print(f"passed={out['passed']} failed={len(out['failed'])} errors={len(out['errors'])} skipped={len(out['skipped'])} "
          f"in {out['seconds']}s")
    for k in ("failed", "errors", "skipped"):
        for item in out[k]:
            print(f"  {k[:-1] if k != 'errors' else 'error'}: {item['test']} - {item['why']}")
    (ROOT / "test_report.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
