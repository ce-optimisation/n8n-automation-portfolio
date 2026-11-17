"""The Portfolio page at an iPhone-sized viewport (real Chromium via Playwright; skipped where it is unavailable).
Set CLIPENGINE_SCREENSHOT_DIR to keep the screenshots."""
import os
import socket
import threading
import time
from pathlib import Path

import pytest

pw = pytest.importorskip("playwright.sync_api")

from clipengine import portfolio, roster_import  # noqa: E402
from clipengine.db import Database  # noqa: E402
from clipengine.web.app import create_app  # noqa: E402


@pytest.fixture
def server(settings):
    import uvicorn
    portfolio.seed(Database(settings.db_path), settings)
    roster_import.reconcile(settings, Database(settings.db_path))
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    app = create_app(settings, start_worker=False)
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    t.join(5)


def test_portfolio_page_is_usable_on_an_iphone(server, tmp_path):
    shots = Path(os.environ.get("CLIPENGINE_SCREENSHOT_DIR") or tmp_path)
    shots.mkdir(parents=True, exist_ok=True)
    with pw.sync_playwright() as p:
        browser = None
        for kw in ({}, {"executable_path": os.environ.get("CHROMIUM_PATH", "/opt/pw-browsers/chromium"),
                        "args": ["--no-sandbox"]}):
            try:
                browser = p.chromium.launch(**kw)
                break
            except Exception as e:  # noqa: BLE001
                err = e
        if browser is None:
            pytest.skip(f"Chromium not available: {err}")
        ctx = browser.new_context(**p.devices["iPhone 13"])
        page = ctx.new_page()
        resp = page.goto(server + "/portfolio")
        assert resp.status == 200
        width = page.evaluate("window.innerWidth")
        assert width <= 430
        assert page.evaluate("document.documentElement.scrollWidth") <= width + 1          # no sideways scrolling
        body = page.inner_text("body")
        for brand in ("America First Monitor", "Afterlive", "Operator Notes", "The Spec Edit", "Dial Study", "Open Floor"):
            assert brand in body
        for creator in ("Nick Fuentes", "Alex Jones", "N3on", "Joe Rogan Experience"):
            assert creator in body
        assert "NEEDS_VERIFICATION" in body and "PERMISSION_REQUIRED" in body and "NEEDS_CREDENTIALS" in body
        assert page.locator(".pf-src").count() == 27
        small = page.evaluate("""() => [...document.querySelectorAll('.pf-act button')].filter(b => b.getBoundingClientRect().height < 40).length""")
        assert small == 0                                                                    # comfortable tap targets
        page.screenshot(path=str(shots / "portfolio_top_iphone.png"))
        first = page.locator(".pf-edit summary").first
        first.click()
        form = page.locator(".pf-edit[open] form").first
        assert form.locator("label").count() >= 3 and form.locator("input[name=url]").count() == 1
        fsize = page.evaluate("""() => getComputedStyle(document.querySelector('.pf-edit[open] input')).fontSize""")
        assert float(fsize.replace("px", "")) >= 16                                          # iOS will not zoom the page on focus
        page.locator(".pf-edit[open]").first.scroll_into_view_if_needed()
        page.screenshot(path=str(shots / "portfolio_edit_iphone.png"))
        # the edit form works end to end
        form.locator("input[name=priority]").fill("77")
        form.locator("button").click()
        page.wait_for_load_state()
        assert "Saved priority" in page.inner_text("body")
        assert page.evaluate("document.documentElement.scrollWidth") <= width + 1
        browser.close()
