"""SQLite persistence layer (WAL mode, one short-lived connection per unit of work)."""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS recordings (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    source_type TEXT NOT NULL,              -- upload | url | local_path
    source_url TEXT,
    source_site TEXT,
    source_media_id TEXT,
    original_filename TEXT,
    file_path TEXT,
    sha256 TEXT,
    size_bytes INTEGER,
    duration REAL,
    width INTEGER, height INTEGER, fps REAL,
    has_audio INTEGER,
    metadata_json TEXT,
    status TEXT NOT NULL DEFAULT 'imported',
    stage TEXT,
    error TEXT,
    language TEXT,
    whisper_model TEXT,
    settings_json TEXT,
    rights_attestation TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_recordings_sha ON recordings(sha256) WHERE sha256 IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_recordings_url ON recordings(source_url);

CREATE TABLE IF NOT EXISTS transcript_segments (
    id INTEGER PRIMARY KEY,
    recording_id INTEGER NOT NULL REFERENCES recordings(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL,
    start REAL NOT NULL,
    end REAL NOT NULL,
    text TEXT NOT NULL,
    avg_logprob REAL,
    no_speech_prob REAL,
    words_json TEXT,
    UNIQUE(recording_id, idx)
);

CREATE VIRTUAL TABLE IF NOT EXISTS transcript_fts USING fts5(
    text, recording_id UNINDEXED, segment_id UNINDEXED, start UNINDEXED
);

CREATE TABLE IF NOT EXISTS clips (
    id INTEGER PRIMARY KEY,
    recording_id INTEGER NOT NULL REFERENCES recordings(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL,
    start REAL NOT NULL,
    end REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending_render',
    transcript_text TEXT,
    context_before TEXT,
    context_after TEXT,
    quote TEXT,
    description TEXT,
    machine_summary TEXT,
    score REAL,
    score_reasons TEXT,
    selection_method TEXT,
    confidence REAL,
    low_conf_words TEXT,
    safety_flags TEXT,
    safety_status TEXT NOT NULL DEFAULT 'clear',     -- clear | flagged | acknowledged
    rights_status TEXT NOT NULL DEFAULT 'unreviewed',
    video_path TEXT, thumb_path TEXT, srt_path TEXT, ass_path TEXT,
    render_rev INTEGER NOT NULL DEFAULT 0,
    rendered_at REAL,
    reviewed_at REAL,
    review_note TEXT,
    error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(recording_id, idx)
);
CREATE INDEX IF NOT EXISTS ix_clips_status ON clips(status);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    recording_id INTEGER,
    clip_id INTEGER,
    payload_json TEXT,
    dedupe_key TEXT,
    status TEXT NOT NULL DEFAULT 'queued',      -- queued | running | done | failed | cancelled
    progress REAL NOT NULL DEFAULT 0,
    message TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    error TEXT,
    run_after REAL NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL,
    updated_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_jobs_active_dedupe ON jobs(dedupe_key)
    WHERE dedupe_key IS NOT NULL AND status IN ('queued', 'running');
CREATE INDEX IF NOT EXISTS ix_jobs_status ON jobs(status, run_after);

CREATE TABLE IF NOT EXISTS job_logs (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL,
    ts REAL NOT NULL,
    level TEXT NOT NULL,
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_job_logs_job ON job_logs(job_id);

CREATE TABLE IF NOT EXISTS transcript_chunks (
    recording_id INTEGER NOT NULL REFERENCES recordings(id) ON DELETE CASCADE,
    chunk_idx INTEGER NOT NULL,
    start REAL NOT NULL,
    end REAL NOT NULL,
    segments_json TEXT NOT NULL,
    language TEXT,
    model_label TEXT,
    created_at REAL NOT NULL,
    PRIMARY KEY (recording_id, chunk_idx)
);

CREATE TABLE IF NOT EXISTS pending_sources (
    id INTEGER PRIMARY KEY,
    url TEXT NOT NULL,
    title TEXT,
    note TEXT,
    reason TEXT,
    status TEXT NOT NULL DEFAULT 'awaiting_file',   -- awaiting_file | attached | cancelled
    recording_id INTEGER,
    settings_json TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS source_permissions (
    id INTEGER PRIMARY KEY,
    domain TEXT NOT NULL UNIQUE,
    granted_by TEXT NOT NULL,
    granted_on TEXT NOT NULL,
    reference TEXT NOT NULL,
    scope TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS public_media_tokens (
    token TEXT PRIMARY KEY,
    clip_id INTEGER NOT NULL,
    expires_at REAL NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS publish_attempts (
    id INTEGER PRIMARY KEY,
    clip_id INTEGER NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
    platform TEXT NOT NULL,
    status TEXT NOT NULL,          -- see publishing.base.STATES
    remote_id TEXT,
    detail TEXT,
    stage TEXT,
    container_id TEXT,
    job_id INTEGER,
    confirmed_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
"""


# Columns added after the first release: (table, column, declaration). Applied idempotently.
MIGRATIONS = [
    ("publish_attempts", "stage", "TEXT"),
    ("publish_attempts", "container_id", "TEXT"),
    ("publish_attempts", "job_id", "INTEGER"),
    ("publish_attempts", "confirmed_at", "REAL"),
    ("recordings", "selection_note", "TEXT"),
]


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Run statements in a single IMMEDIATE transaction."""
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def _columns(self, table: str) -> set[str]:
        cache = self.__dict__.setdefault("_colcache", {})
        if table not in cache:
            cache[table] = {r["name"] for r in self.query(f"PRAGMA table_info({table})")}
        return cache[table]

    def init(self) -> None:
        conn = self.connect()
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(SCHEMA)
            for table, col, decl in MIGRATIONS:
                cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
                if col not in cols:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
            conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                         (str(SCHEMA_VERSION),))
        finally:
            conn.close()

    # ---- generic helpers -------------------------------------------------
    def query(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        conn = self.connect()
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def execute(self, sql: str, params: tuple | dict = ()) -> int:
        conn = self.connect()
        try:
            cur = conn.execute(sql, params)
            return cur.lastrowid if cur.lastrowid else cur.rowcount
        finally:
            conn.close()

    def update(self, table: str, row_id: int, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.execute(f"UPDATE {table} SET {cols} WHERE id = ?", (*fields.values(), row_id))

    def insert(self, table: str, **fields: Any) -> int:
        now = time.time()
        cols = self._columns(table)
        for key in ("created_at", "updated_at"):
            if key in cols:
                fields.setdefault(key, now)
        cols = ", ".join(fields)
        qs = ", ".join("?" for _ in fields)
        return self.execute(f"INSERT INTO {table} ({cols}) VALUES ({qs})", tuple(fields.values()))

    # ---- key/value state (setup checklist, last OAuth errors, ...) --------
    def get_kv(self, key: str, default: str | None = None) -> str | None:
        row = self.one("SELECT value FROM meta WHERE key = ?", (key,))
        return row["value"] if row else default

    def set_kv(self, key: str, value: str) -> None:
        self.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, value))

    # ---- domain helpers --------------------------------------------------
    def recording(self, rid: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM recordings WHERE id = ?", (rid,))

    def clip(self, cid: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM clips WHERE id = ?", (cid,))

    def segments(self, rid: int) -> list[dict]:
        rows = self.query("SELECT * FROM transcript_segments WHERE recording_id = ? ORDER BY idx", (rid,))
        out = []
        for r in rows:
            d = dict(r)
            d["words"] = json.loads(r["words_json"] or "[]")
            out.append(d)
        return out

    def replace_transcript(self, rid: int, segments: list[dict]) -> None:
        with self.tx() as conn:
            conn.execute("DELETE FROM transcript_fts WHERE recording_id = ?", (rid,))
            conn.execute("DELETE FROM transcript_segments WHERE recording_id = ?", (rid,))
            for i, s in enumerate(segments):
                cur = conn.execute(
                    "INSERT INTO transcript_segments(recording_id, idx, start, end, text, avg_logprob,"
                    " no_speech_prob, words_json) VALUES (?,?,?,?,?,?,?,?)",
                    (rid, i, s["start"], s["end"], s["text"], s.get("avg_logprob"),
                     s.get("no_speech_prob"), json.dumps(s.get("words", []))))
                conn.execute("INSERT INTO transcript_fts(text, recording_id, segment_id, start) VALUES (?,?,?,?)",
                             (s["text"], rid, cur.lastrowid, s["start"]))

    def search_transcripts(self, q: str, limit: int = 50) -> list[sqlite3.Row]:
        # Quote each term so user input cannot inject FTS5 syntax.
        terms = [t.replace('"', '""') for t in q.split() if t.strip()]
        if not terms:
            return []
        match = " ".join(f'"{t}"' for t in terms)
        return self.query(
            "SELECT f.recording_id, f.segment_id, f.start, snippet(transcript_fts, 0, '[', ']', '…', 12) AS snip,"
            " r.title FROM transcript_fts f JOIN recordings r ON r.id = f.recording_id"
            " WHERE transcript_fts MATCH ? ORDER BY rank LIMIT ?", (match, limit))

    def counts(self) -> dict:
        c = {}
        c["recordings"] = self.one("SELECT COUNT(*) n FROM recordings")["n"]
        for st in ("ready", "approved", "rejected", "failed", "pending_render", "rendering"):
            c[f"clips_{st}"] = self.one("SELECT COUNT(*) n FROM clips WHERE status = ?", (st,))["n"]
        for st in ("queued", "running", "failed", "done"):
            c[f"jobs_{st}"] = self.one("SELECT COUNT(*) n FROM jobs WHERE status = ?", (st,))["n"]
        return c
