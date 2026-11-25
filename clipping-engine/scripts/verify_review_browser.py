"""Optional Windows acceptance test with a fresh Edge profile and four disposable copies of a supplied MP4."""
import os
import sys
import shutil
import socket
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

import uvicorn
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from clipengine.config import get_settings
from clipengine.web.app import create_app


def run(media):
    with tempfile.TemporaryDirectory(prefix="review-uat-") as tmp:
        os.environ["CLIPENGINE_DATA_DIR"] = tmp
        os.environ["CLIPENGINE_ENV_FILE"] = str(Path(tmp) / "fixture.env")
        settings = get_settings(open_browser=False, dashboard_password="")
        with patch("clipengine.publishing.tokens._keyring", return_value=None):
            app = create_app(settings, start_worker=False)
        db = app.state.core.db
        for key in ("recording.enabled", "delivery.enabled"):
            db.set_kv(key, "0")
        cr = db.insert("creators", name="Acceptance fixture", brand_key="afterlive",
                       permission_json='{"granted_by":"Fixture","reference":"Disposable test only"}')
        rid = db.insert("recordings", title="Fixture", source_type="upload", status="ready", creator_id=cr)
        paths = {}
        for idx in range(1, 5):
            cid = db.insert("clips", recording_id=rid, idx=idx, start=0, end=59, status="ready",
                            title=f"Disposable {idx}", rights_status="unreviewed", safety_status="clear",
                            selection_method="manual", description="Test", transcript_text="Test")
            folder = settings.clips_dir / f"rec{rid:05d}" / f"clip{cid:05d}_r0"
            folder.mkdir(parents=True)
            paths[cid] = folder / "clip.mp4"
            shutil.copyfile(media, paths[cid])
            db.update("clips", cid, video_path=str(paths[cid]))
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
            server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
            worker = threading.Thread(target=server.run, kwargs={"sockets":[sock]}, daemon=True)
            worker.start()
            try:
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(channel="msedge", headless=True)
                    for mobile, cid in ((True, 1), (False, 4)):
                        ctx = browser.new_context(viewport={"width":390 if mobile else 1440,"height":900})
                        page = ctx.new_page()
                        url = f"http://127.0.0.1:{port}" + ("/m" if mobile else "/review?status=ready")
                        page.goto(url)
                        card = page.locator("main" if mobile else "article").first
                        video = card.locator("video")
                        source = video.get_attribute("src")
                        video.evaluate("(v)=>{v.muted=true;return v.play()}")
                        origin = page.evaluate("performance.timeOrigin")
                        card.get_by_role("button", name="Reject & delete").click()
                        expect(page.get_by_text(f"{media.stat().st_size:,} bytes freed",exact=False)).to_be_visible(timeout=15000)
                        assert not paths[cid].exists() and db.clip(cid)["status"] == "rejected"
                        assert page.url == url and page.evaluate("performance.timeOrigin") == origin
                        assert page.locator("video").first.get_attribute("src") != source
                        page.screenshot(path=str(ROOT / ("mobile-acceptance.png" if mobile else "desktop-acceptance.png")))
                        print("PASS", "mobile" if mobile else "desktop", flush=True)
                        ctx.close()
                    browser.close()
            finally:
                server.should_exit = True
                worker.join(15)


if __name__ == "__main__":
    run(Path(sys.argv[1]).resolve())
