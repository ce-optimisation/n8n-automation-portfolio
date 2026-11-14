"""LIVE public-internet URL import (runs only with CLIPENGINE_LIVE_NET=1, e.g. in CI).

Imports a genuine public-domain video (NASA works, hosted on Wikimedia Commons) through
the real download job with every network protection ON (no test-only switches): direct
link → redirect to upload.wikimedia.org → resumable download → probe → recording with
provenance. Candidates are tried in order; the test passes if one imports.
"""
import json
import os

import pytest

from clipengine import ingest
from clipengine.db import Database
from clipengine.jobs import JobQueue
from clipengine.pipeline import Pipeline

pytestmark = pytest.mark.skipif(os.environ.get("CLIPENGINE_LIVE_NET") != "1",
                                reason="set CLIPENGINE_LIVE_NET=1 to run the live internet import")

CANDIDATES = [  # public domain (NASA); Special:FilePath redirects to the file on upload.wikimedia.org
    "https://commons.wikimedia.org/wiki/Special:FilePath/NASA-_60_Years_in_60_Seconds.webm",
    "https://commons.wikimedia.org/wiki/Special:FilePath/Launch_Recap_NASA_and_SpaceX_fly_Astronauts_to_the_Space_Station.webm",
]


def test_live_public_domain_url_import(settings):
    assert not settings.allow_private_urls
    db = Database(settings.db_path)
    q = JobQueue(db)
    Pipeline(settings, db, q)
    errors = []
    for url in CANDIDATES:
        d = ingest.classify_url(url, settings, db)
        assert d.method == "direct", d
        jid = q.enqueue("download_url", payload={"url": url, "rights_attestation": "Public domain (NASA)"},
                        max_attempts=3)
        while q.run_one():
            if db.one("SELECT COUNT(*) n FROM recordings")["n"]:
                break
            db.execute("UPDATE jobs SET run_after = 0 WHERE kind='download_url'")
        rec = db.one("SELECT * FROM recordings")
        if rec:
            meta = json.loads(rec["metadata_json"])
            assert rec["source_type"] == "url" and rec["source_url"] == ingest.normalize_url(url)
            assert "upload.wikimedia.org" in meta["retrieval"]["final_url"]
            assert rec["duration"] > 10 and rec["has_audio"] and rec["sha256"]
            print("LIVE import OK:", url, rec["duration"], meta["retrieval"]["bytes"])
            return
        errors.append(db.one("SELECT error FROM jobs WHERE id=?", (jid,))["error"])
    if errors and all(e and "HTTP 403" in e for e in errors):
        # The remote host refused this machine (seen from some CI runner IP ranges). The code path
        # itself is unaffected; report it honestly instead of passing or failing opaquely.
        pytest.xfail(f"Remote host refused this runner with HTTP 403: {errors[0][:200]}")
    pytest.fail(f"No live candidate could be imported: {errors}")
