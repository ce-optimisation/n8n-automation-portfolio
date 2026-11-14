"""Watch folder: video files saved into the inbox folder are imported automatically.

A file is imported once its size has been stable for two scans (so partially copied or
still-downloading files are left alone). Files named like ``src-12 anything.mp4`` are
attached to pending source #12 (keeping its original URL as provenance). Imported files
are moved into data/recordings; duplicates go to inbox/duplicates and failures to
inbox/failed with a .error.txt explanation. Owner files are never deleted.
"""
from __future__ import annotations

import logging
import re
import shutil
import threading
import time
from pathlib import Path

from . import ingest
from .jobs import PermanentError

log = logging.getLogger("clipengine.inbox")
PENDING_RE = re.compile(r"\bsrc-(\d+)\b", re.I)


class InboxWatcher:
    def __init__(self, settings, db, pipeline, interval: float = 10.0):
        self.settings, self.db, self.pipeline, self.interval = settings, db, pipeline, interval
        self._seen: dict[Path, tuple[int, float]] = {}
        self._stop = threading.Event()
        self._t: threading.Thread | None = None

    def _move_aside(self, f: Path, sub: str, note: str | None = None) -> None:
        d = self.settings.inbox / sub
        d.mkdir(parents=True, exist_ok=True)
        target = d / f.name
        if target.exists():
            target = d / f"{f.stem}_{int(time.time())}{f.suffix}"
        shutil.move(str(f), target)
        if note:
            target.with_suffix(target.suffix + ".error.txt").write_text(note, encoding="utf-8")

    def scan_once(self) -> list[tuple[str, str]]:
        inbox = self.settings.inbox
        inbox.mkdir(parents=True, exist_ok=True)
        results = []
        for f in sorted(inbox.iterdir()):
            if not f.is_file() or f.suffix.lower() not in ingest.VIDEO_EXTS:
                continue
            try:
                st = f.stat()
            except OSError:
                continue
            sig = (st.st_size, st.st_mtime)
            if self._seen.get(f) != sig or time.time() - st.st_mtime < 5:
                self._seen[f] = sig          # changed since last scan: wait for it to settle
                continue
            self._seen.pop(f, None)
            try:
                m = PENDING_RE.search(f.stem)
                pending = self.db.one("SELECT * FROM pending_sources WHERE id = ? AND status='awaiting_file'",
                                      (int(m.group(1)),)) if m else None
                if pending:
                    rid, dup = ingest.attach_file_to_pending(self.settings, self.db, pending["id"], f,
                                                             original_name=f.name,
                                                             attestation="Imported from the inbox folder.")
                else:
                    rid, dup = ingest.register_file(self.settings, self.db, f, original_name=f.name,
                                                    source_type="inbox", move=True,
                                                    rights_attestation="Imported from the inbox folder by the owner.")
                if dup:
                    if f.exists():
                        self._move_aside(f, "duplicates")
                    results.append((f.name, f"duplicate of recording #{rid}"))
                    continue
                self.pipeline.queue.enqueue("process_recording", recording_id=rid, dedupe_key=f"process:{rid}",
                                            payload={"settings": {}})
                results.append((f.name, f"imported as recording #{rid}"))
                log.info("inbox: %s imported as recording #%s", f.name, rid)
            except (PermanentError, OSError, ValueError) as e:
                if f.exists():
                    self._move_aside(f, "failed", f"{type(e).__name__}: {e}")
                results.append((f.name, f"failed: {e}"))
                log.warning("inbox: %s failed: %s", f.name, e)
        return results

    def start(self) -> None:
        def loop():
            while not self._stop.wait(self.interval):
                try:
                    self.scan_once()
                except Exception:  # noqa: BLE001 - keep watching
                    log.exception("inbox scan failed")
        self._t = threading.Thread(target=loop, name="clipengine-inbox", daemon=True)
        self._t.start()

    def stop(self) -> None:
        self._stop.set()
