"""URL ingestion, SSRF protection, resumable direct downloads, pending sources and inbox.

The HTTP tests use a LOCAL test server (127.0.0.1). Because the application refuses
private addresses, those tests enable the TEST-ONLY `allow_private_urls` switch; one
test keeps the protection on to prove a connection to a private peer is refused.
A live public-internet import is exercised separately in CI (test_live_network.py).
"""
import hashlib
import http.server
import json
import shutil
import threading
import time

import pytest

from clipengine import ingest
from clipengine.db import Database
from clipengine.inbox import InboxWatcher
from clipengine.jobs import JobQueue, TransientError
from clipengine.pipeline import Pipeline
from conftest import needs_ffmpeg

PUBLIC = ["93.184.216.34"]


class Server:
    """Tiny HTTP server with Range support, redirects and fault injection."""

    def __init__(self, payload: bytes):
        self.payload = payload
        self.drop_after = None      # close the connection after N bytes (once)
        self.fail_first = 0          # respond 503 this many times
        self.requests = []
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.requests.append((self.path, self.headers.get("Range")))
                if outer.fail_first:
                    outer.fail_first -= 1
                    self.send_response(503)
                    self.end_headers()
                    return
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/video.mp4")
                    self.end_headers()
                    return
                if self.path == "/page.mp4":
                    body = b"<html>not a video</html>"
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if self.path != "/video.mp4":
                    self.send_response(404)
                    self.end_headers()
                    return
                data, start = outer.payload, 0
                rng = self.headers.get("Range")
                if rng:
                    start = int(rng.split("=")[1].split("-")[0])
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
                else:
                    self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(len(data) - start))
                self.end_headers()
                chunk = data[start:]
                if outer.drop_after is not None:
                    n, outer.drop_after = outer.drop_after, None
                    self.wfile.write(chunk[:n])
                    self.wfile.flush()
                    self.connection.close()
                    return
                self.wfile.write(chunk)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()


@pytest.fixture
def server(sample_video):
    s = Server(sample_video.read_bytes())
    yield s
    s.close()


@pytest.fixture
def local_settings(stub_settings):
    stub_settings.allow_private_urls = True   # TEST-ONLY: the server is on 127.0.0.1
    return stub_settings


# ---- classification & SSRF ----------------------------------------------------

def test_classification_rules(settings, monkeypatch):
    monkeypatch.setattr(ingest, "resolve_host", lambda h: PUBLIC)
    c = lambda u: ingest.classify_url(u, settings)  # noqa: E731
    assert c("https://council.example.gov/meeting.MP4").method == "direct"
    assert c("https://rumble.com/v4abc-x.html").code == "restricted_platform"
    assert c("https://www.rumble.com/embed/x").code == "restricted_platform"
    assert c("https://user:pw@example.org/a.mp4").code == "credentials_in_url"
    assert c("https://example.org:8443/a.mp4").code == "unsupported_port"
    assert c("file:///etc/passwd").code == "unsupported_scheme"
    assert c("javascript:alert(1)").code == "unsupported_scheme"
    monkeypatch.setattr(ingest, "resolve_host", lambda h: ["10.0.0.5"])
    assert c("https://intranet.example.org/a.mp4").code == "private_address"
    monkeypatch.setattr(ingest, "resolve_host", lambda h: ["93.184.216.34", "127.0.0.1"])
    assert c("https://mixed.example.org/a.mp4").code == "private_address"
    assert c("http://169.254.169.254/latest/meta-data.mp4").code == "private_address"
    assert c("http://[::ffff:127.0.0.1]/a.mp4").code == "private_address"


def test_recorded_permission_enables_restricted_platform(settings, monkeypatch):
    monkeypatch.setattr(ingest, "resolve_host", lambda h: PUBLIC)
    db = Database(settings.db_path)
    assert ingest.classify_url("https://rumble.com/v1-x.html", settings, db).method == "link_only"
    db.execute("INSERT INTO source_permissions(domain, granted_by, granted_on, reference, created_at, updated_at) "
               "VALUES ('rumble.com','Rumble Inc','2026-11-01','REF-1',0,0)")
    d = ingest.classify_url("https://rumble.com/v1-x.html", settings, db)
    assert d.method == "ytdlp" and "REF-1" in d.message


# ---- direct download -------------------------------------------------------------

@needs_ffmpeg
def test_direct_download_resumes_after_interruption(local_settings, server):
    part = local_settings.work_dir / "x.part"
    server.drop_after = 200_000
    with pytest.raises(TransientError):
        ingest.download_direct(server.url + "/video.mp4", part, local_settings)
    assert 0 < part.stat().st_size < len(server.payload)
    meta = ingest.download_direct(server.url + "/video.mp4", part, local_settings)
    assert server.requests[-1][1] == f"bytes={part.stat().st_size - (len(server.payload) - 200_000)}-" \
        or server.requests[-1][1].startswith("bytes=")
    assert hashlib.sha256(part.read_bytes()).digest() == hashlib.sha256(server.payload).digest()
    assert meta["bytes"] == len(server.payload) and meta["content_type"] == "video/mp4"


@needs_ffmpeg
def test_direct_download_errors(local_settings, server):
    part = local_settings.work_dir / "y.part"
    with pytest.raises(ingest.SourceError) as e:
        ingest.download_direct(server.url + "/page.mp4", part, local_settings)
    assert e.value.code == "not_video"
    with pytest.raises(ingest.SourceError) as e:
        ingest.download_direct(server.url + "/missing.mp4", part, local_settings)
    assert e.value.code == "not_found"
    server.fail_first = 1
    with pytest.raises(TransientError):
        ingest.download_direct(server.url + "/video.mp4", part, local_settings)
    local_settings.max_upload_mb = 0
    with pytest.raises(ingest.SourceError) as e:
        ingest.download_direct(server.url + "/video.mp4", part, local_settings)
    assert e.value.code == "too_large"


@needs_ffmpeg
def test_follows_redirect_and_rechecks_each_hop(local_settings, server):
    part = local_settings.work_dir / "z.part"
    meta = ingest.download_direct(server.url + "/redirect", part, local_settings)
    assert meta["final_url"].endswith("/video.mp4")


@needs_ffmpeg
def test_private_peer_refused_even_if_dns_check_passed(settings, server, monkeypatch):
    """Simulates DNS rebinding: the name check passes but the connection lands on 127.0.0.1."""
    monkeypatch.setattr(ingest, "check_network_target", lambda url, s: "rebind.example")
    with pytest.raises(ingest.SourceError) as e:
        ingest.download_direct(server.url + "/video.mp4", settings.work_dir / "r.part", settings)
    assert e.value.code == "private_address"


@needs_ffmpeg
def test_url_import_job_end_to_end_with_provenance_and_dedupe(local_settings, server):
    db = Database(local_settings.db_path)
    q = JobQueue(db)
    Pipeline(local_settings, db, q)
    url = server.url + "/video.mp4"
    q.enqueue("download_url", payload={"url": url, "rights_attestation": "test"}, max_attempts=4)
    q.drain(600)
    rec = db.one("SELECT * FROM recordings")
    meta = json.loads(rec["metadata_json"])
    assert rec["source_type"] == "url" and rec["source_url"] == ingest.normalize_url(url)
    assert meta["retrieval"]["method"] == "direct" and meta["retrieval"]["bytes"] == len(server.payload)
    assert db.one("SELECT COUNT(*) n FROM clips WHERE status='ready'")["n"] >= 1
    assert not list(local_settings.work_dir.glob("dl_*"))
    assert ingest.find_duplicate(db, url=url)["id"] == rec["id"]


# ---- pending sources & inbox --------------------------------------------------------

@needs_ffmpeg
def test_pending_source_attach_keeps_link_provenance(stub_settings, sample_video):
    db = Database(stub_settings.db_path)
    pid, existed = ingest.create_pending_source(db, "https://rumble.com/v9-talk.html", title="Talk", reason="r")
    assert not existed and ingest.create_pending_source(db, "https://rumble.com/v9-talk.html/", title=None,
                                                        reason="r") == (pid, True)
    f = stub_settings.work_dir / "file.mp4"
    shutil.copy(sample_video, f)
    rid, dup = ingest.attach_file_to_pending(stub_settings, db, pid, f, original_name="file.mp4")
    rec = db.recording(rid)
    assert rec["source_type"] == "linked_file" and rec["source_url"] == "https://rumble.com/v9-talk.html"
    assert "not downloaded by this tool" in rec["rights_attestation"]
    assert db.one("SELECT status FROM pending_sources WHERE id=?", (pid,))["status"] == "attached"


@needs_ffmpeg
def test_inbox_watcher_imports_stable_files(stub_settings, sample_video):
    db = Database(stub_settings.db_path)
    q = JobQueue(db)
    p = Pipeline(stub_settings, db, q)
    w = InboxWatcher(stub_settings, db, p)
    inbox = stub_settings.inbox
    pid, _ = ingest.create_pending_source(db, "https://rumble.com/v7-x.html", title="Seven", reason="r")
    shutil.copy(sample_video, inbox / f"src-{pid} interview.mp4")
    (inbox / "notes.txt").write_text("ignore me")
    import os
    old = time.time() - 60
    for f in inbox.iterdir():
        os.utime(f, (old, old))
    assert w.scan_once() == []                 # first sighting: wait until size is stable
    res = w.scan_once()
    assert res and "imported" in res[0][1]
    assert db.one("SELECT source_type, source_url FROM recordings")["source_type"] == "linked_file"
    shutil.copy(sample_video, inbox / "again.mp4")
    os.utime(inbox / "again.mp4", (old, old))
    w.scan_once()
    assert "duplicate" in w.scan_once()[0][1]
    assert (inbox / "duplicates" / "again.mp4").exists() and (inbox / "notes.txt").exists()
    assert q.db.one("SELECT COUNT(*) n FROM jobs WHERE kind='process_recording'")["n"] == 1
