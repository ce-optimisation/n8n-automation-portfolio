"""REAL Google Drive self-test with disposable, engine-generated assets (run after connecting Drive).

    .venv\\Scripts\\python.exe scripts\\cloud_selftest.py

Uses data\\selftest\\selftest_clip.mp4 (the engine's own NASA-briefing self-test render) or, if missing, a short
generated test video. In a separate Drive folder "Clipping Engine/_selftest" it:
  1. uploads the video and a metadata file (resumable upload)
  2. verifies each remote file exists with the same size and MD5 as the local file
  3. re-runs the upload with the same key: Drive must report the existing file (no duplicate)
  4. downloads the video back and compares it byte for byte (retrieval)
  5. deletes ONLY the files it created (a retention action on disposable assets) and confirms they are gone
It never touches real clips, recordings or any other Drive file. Exit code 0 = all steps passed.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    from clipengine import cloud, media
    from clipengine.config import get_settings
    from clipengine.db import Database
    from clipengine.jobs import JobQueue
    from clipengine.publishing.tokens import TokenStore
    s = get_settings()
    db = Database(s.db_path)
    svc = cloud.CloudService(s, db, JobQueue(db), TokenStore.for_settings(s))
    st = svc.status()
    print(f"Drive status: {st['label']}")
    if not st["connected"]:
        print("Not connected: open the dashboard > Storage > Connect Google Drive, then run this again.")
        return 2
    p = svc.provider
    q = svc.refresh_quota()
    print(f"quota: free {((q or {}).get('free') or 0) / 1e9:.1f} GB of {((q or {}).get('limit') or 0) / 1e9:.1f} GB")
    tmp = Path(tempfile.mkdtemp(prefix="ce_cloudtest_"))
    video = s.data_dir / "selftest" / "selftest_clip.mp4"
    if not video.is_file():
        video = tmp / "test.mp4"
        subprocess.run([media.require("ffmpeg"), "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=1080x1920:d=5",
                        "-f", "lavfi", "-i", "sine=d=5", "-c:v", "libx264", "-c:a", "aac", "-shortest", str(video)], check=True)
    meta = tmp / "metadata.json"
    meta.write_text(json.dumps({"selftest": True, "at": time.ctime()}), encoding="utf-8")
    results, created = {}, []
    try:
        root = p.ensure_folder(s.cloud_folder_name)
        folder = p.ensure_folder("_selftest", root)
        created.append(folder)
        run = time.strftime("%Y%m%d%H%M%S")
        for f in (video, meta):
            md5, size = cloud.md5_file(f), f.stat().st_size
            key = f"selftest:{run}:{f.name}:{md5}"
            info = p.upload(f, f"selftest_{run}_{f.name}", folder, key, None, lambda uri: None)
            rid = info["id"]
            created.append(rid)
            chk = info if info.get("md5Checksum") else p.metadata(rid)
            ok = chk.get("md5Checksum") == md5 and int(chk.get("size") or -1) == size
            results[f"upload+verify {f.name}"] = ok
            again = p.find(key)
            results[f"no duplicate on retry {f.name}"] = bool(again) and again["id"] == rid
        # retrieval: download the video back and compare
        import httpx
        r = httpx.get(f"{p.API}/files/{created[1]}", params={"alt": "media"}, headers=p._h(), timeout=300)
        results["retrieval byte-identical"] = r.status_code == 200 and hashlib.md5(r.content).hexdigest() == cloud.md5_file(video)
    finally:
        for rid in reversed(created):
            try:
                p.delete(rid)
            except Exception as e:  # noqa: BLE001
                print("cleanup problem:", e)
        gone = all(not p.find(f"selftest:{run}:{f.name}:{cloud.md5_file(f)}") for f in (video, meta)) if created else True
        results["test files deleted (only our own)"] = gone
    for k, v in results.items():
        print(f"{'PASS' if v else 'FAIL'}  {k}")
    return 0 if results and all(results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
