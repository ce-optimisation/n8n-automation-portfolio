"""Session 13: the YouTube API key was being written to data/logs/clipengine.jsonl by the HTTP client's request log."""
import json
import logging

from clipengine import __main__ as entry
from clipengine.logsafe import RedactingFilter, redact

FAKE_KEY = "AIza" + "x" * 35        # shaped like a Google API key; not a real credential


def test_redacts_api_key_query_parameters_and_keeps_the_rest_of_the_url():
    url = f"HTTP Request: GET https://www.googleapis.com/youtube/v3/channels?part=id%2Csnippet&key={FAKE_KEY}&id=UC123"
    out = redact(url)
    assert FAKE_KEY not in out and "key=<redacted>" in out
    assert "part=id%2Csnippet" in out and "id=UC123" in out


def test_redacts_tokens_secrets_bearer_and_bare_google_keys():
    s = ("https://x.test/cb?code=abc123&state=ok&access_token=tok.en-1 Authorization: Bearer abcdefgh12345678 "
         f"raw {FAKE_KEY} " + json.dumps({"client_secret": "shh", "name": "keep"}))
    out = redact(s)
    for secret in ("abc123", "tok.en-1", "abcdefgh12345678", FAKE_KEY, "shh"):
        assert secret not in out, out
    assert "state=ok" in out and '"name": "keep"' in out


def test_ordinary_text_is_unchanged():
    msg = "Selected 4 candidate excerpts; render jobs queued (keyboard 12)"
    assert redact(msg) == msg


def test_file_log_never_contains_the_key(tmp_path):
    entry.setup_logging(tmp_path)
    try:
        logging.getLogger("httpx").info("HTTP Request: GET https://www.googleapis.com/youtube/v3/videos?key=%s \"HTTP/1.1 200 OK\"", FAKE_KEY)
        try:
            raise RuntimeError(f"failed calling https://api.test/x?key={FAKE_KEY}")
        except RuntimeError:
            logging.getLogger("t").exception("boom")
        for h in logging.getLogger().handlers:
            h.flush()
        text = (tmp_path / "logs" / "clipengine.jsonl").read_text(encoding="utf-8")
        assert FAKE_KEY not in text and "<redacted>" in text
    finally:
        root = logging.getLogger()
        for h in list(root.handlers):
            if any(isinstance(f, RedactingFilter) for f in h.filters):
                h.close()
                root.removeHandler(h)
