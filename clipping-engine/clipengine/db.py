"""SQLite persistence layer (WAL mode, one short-lived connection per unit of work)."""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 7

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

CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,                    -- feed | channel | hls
    url TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 0,
    poll_minutes INTEGER NOT NULL DEFAULT 15,
    record_live INTEGER NOT NULL DEFAULT 1,
    backfill INTEGER NOT NULL DEFAULT 0,
    authorisation TEXT,                    -- owner's note: who permits this retrieval and how
    status TEXT NOT NULL DEFAULT 'new',     -- new | ok | blocked | error | disabled
    status_detail TEXT,
    last_checked REAL, next_check REAL, last_success REAL,
    consecutive_errors INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    settings_json TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS source_items (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    item_key TEXT NOT NULL,
    title TEXT,
    url TEXT,
    published TEXT,
    kind TEXT NOT NULL DEFAULT 'vod',      -- vod | live
    state TEXT NOT NULL,                   -- baseline | queued | recording | finalizing | handed_off | imported | duplicate | failed
    job_id INTEGER,
    recording_id INTEGER,
    work_dir TEXT,
    fingerprint TEXT,                      -- JSON list of media segment URIs seen (live broadcast identity)
    segments INTEGER NOT NULL DEFAULT 0,
    restarts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    started_at REAL, ended_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(source_id, item_key)
);

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,                    -- recording_ready | job_failed | disk_low | test
    dedupe_key TEXT NOT NULL UNIQUE,
    recording_id INTEGER,
    to_addr TEXT,
    subject TEXT NOT NULL,
    body_html TEXT NOT NULL,
    body_text TEXT NOT NULL,
    status TEXT NOT NULL,                  -- held | queued | sending | sent | failed | suppressed
    transport TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    sent_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS manual_posts (
    id INTEGER PRIMARY KEY,
    clip_id INTEGER NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
    platform TEXT NOT NULL,
    posted INTEGER NOT NULL DEFAULT 1,
    post_url TEXT,
    note TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(clip_id, platform)
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

-- ---- v5: multi-niche, multi-creator, storage, distribution and measurement ------------------
CREATE TABLE IF NOT EXISTS niches (
    key TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    grp TEXT, risk TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    active INTEGER NOT NULL DEFAULT 0,
    config_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS niche_history (
    id INTEGER PRIMARY KEY,
    key TEXT NOT NULL,
    version INTEGER NOT NULL,
    config_json TEXT NOT NULL,
    note TEXT,
    created_at REAL NOT NULL
);

-- Creators: operational (status active/paused/blocked/needs_authorisation) or research-only
-- (status 'research' / 'shortlisted'). Research data never authorises retrieval.
CREATE TABLE IF NOT EXISTS creators (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    platform TEXT,                          -- primary platform: rumble | twitch | kick | youtube | ...
    url TEXT,                               -- canonical channel URL (as verified; never guessed)
    status TEXT NOT NULL DEFAULT 'research',
    priority INTEGER NOT NULL DEFAULT 50,   -- higher runs first in the processing queue
    niches_json TEXT,                       -- ["ai_business", ...]
    profile_json TEXT,                      -- CreatorProfile
    permission_json TEXT,                   -- evidence, granted_by, reference, permitted_uses, expires
    schedule TEXT,
    expected_format TEXT,
    typical_minutes REAL,
    attribution TEXT,
    platforms_json TEXT,                    -- [{platform, url, primary, verified, observed}]
    stats_json TEXT,                        -- research figures with source, window and date
    sources_json TEXT,                      -- research citations
    commercial_score REAL,                  -- research assessment (0-100), not a fact
    notes TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_creators_url ON creators(url) WHERE url IS NOT NULL;

CREATE TABLE IF NOT EXISTS social_accounts (
    id INTEGER PRIMARY KEY,
    platform TEXT NOT NULL,                 -- youtube | tiktok | instagram | facebook | x | rumble
    handle TEXT NOT NULL,
    display_name TEXT,
    brand TEXT,                             -- the editorial identity this account represents
    niches_json TEXT,                       -- niches this account may receive (isolation)
    connection_key TEXT,                    -- TokenStore key when connected (never the token itself)
    status TEXT NOT NULL DEFAULT 'manual',  -- manual | awaiting_connection | connected | paused
    notes TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(platform, handle)
);

CREATE TABLE IF NOT EXISTS publication_packages (
    id INTEGER PRIMARY KEY,
    clip_id INTEGER NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
    platform TEXT NOT NULL,
    account_id INTEGER REFERENCES social_accounts(id),
    title TEXT, description TEXT, caption TEXT, hashtags TEXT, attribution TEXT,
    export_path TEXT,                       -- NULL = the master render is used as-is
    approval_state TEXT NOT NULL DEFAULT 'draft',   -- draft | approved | withdrawn
    upload_state TEXT NOT NULL DEFAULT 'not_started',
    status TEXT NOT NULL DEFAULT 'prepared', -- prepared | handoff_ready | uploaded_private | published | failed
    remote_id TEXT, remote_url TEXT,
    history_json TEXT,
    published_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(clip_id, platform, account_id)
);

CREATE TABLE IF NOT EXISTS cloud_objects (
    id INTEGER PRIMARY KEY,
    provider TEXT NOT NULL,                 -- gdrive | r2 (future)
    owner_kind TEXT NOT NULL,               -- clip | recording
    owner_id INTEGER NOT NULL,
    kind TEXT NOT NULL,                     -- video | thumb | transcript | metadata
    local_path TEXT,
    local_md5 TEXT, size INTEGER,
    remote_id TEXT, remote_folder TEXT,
    session_uri TEXT,                       -- resumable upload session (resume after a crash)
    state TEXT NOT NULL DEFAULT 'pending',  -- pending | uploading | uploaded | verified | failed | deleted
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    verified_at REAL, deleted_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(provider, owner_kind, owner_id, kind)
);

CREATE TABLE IF NOT EXISTS metrics (
    id INTEGER PRIMARY KEY,
    package_id INTEGER, clip_id INTEGER, account_id INTEGER, creator_id INTEGER,
    platform TEXT, niche_key TEXT,
    date TEXT NOT NULL,
    views INTEGER, likes INTEGER, comments INTEGER, shares INTEGER,
    watch_seconds REAL, avg_view_pct REAL, followers_delta INTEGER, incidents INTEGER,
    source TEXT NOT NULL DEFAULT 'manual',  -- manual | csv | api (never estimated)
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS commercial_entries (
    id INTEGER PRIMARY KEY,
    model TEXT NOT NULL DEFAULT 'B',        -- A = creator-authorised service, B = owner-operated channel
    kind TEXT NOT NULL,                     -- revenue | cost | affiliate_click | conversion | sponsor | review_minutes
    amount REAL NOT NULL DEFAULT 0,
    currency TEXT NOT NULL DEFAULT 'GBP',
    realised INTEGER NOT NULL DEFAULT 1,    -- 1 = realised, 0 = estimate (always shown apart)
    niche_key TEXT, creator_id INTEGER, account_id INTEGER, clip_id INTEGER,
    date TEXT NOT NULL,
    note TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS niche_scores (
    key TEXT PRIMARY KEY,
    scores_json TEXT NOT NULL,              -- criterion -> 0..10
    audience_score REAL, profit_score REAL, overall REAL,
    rationale TEXT, sources_json TEXT, observed TEXT, confidence TEXT,
    updated_at REAL NOT NULL
);
-- ---- v6: six-brand creator/source portfolio and discovery monitoring --------------------------
-- DISCOVERY (what exists / who is live) is kept apart from ACQUISITION (permission to retrieve).
CREATE TABLE IF NOT EXISTS brands (
    key TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    niche_key TEXT,
    description TEXT,
    editorial_note TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS monitor_sources (
    id INTEGER PRIMARY KEY,
    uid TEXT NOT NULL UNIQUE,              -- permanent internal id: <creator uid>/<platform>/<type>
    creator_id INTEGER NOT NULL REFERENCES creators(id) ON DELETE CASCADE,
    brand_key TEXT NOT NULL,
    platform TEXT NOT NULL,                -- youtube | twitch | kick | rumble | podcast
    source_type TEXT NOT NULL,             -- channel | podcast_feed
    channel_name TEXT,                     -- official name as the platform reports it (verified)
    candidate_handle TEXT,                 -- UNVERIFIED hint used only to look the identity up
    canonical_url TEXT,                    -- set from verified evidence, never guessed
    platform_id TEXT,                      -- stable platform id (UC..., twitch user id, kick user id, feed guid)
    verification TEXT NOT NULL DEFAULT 'NEEDS_VERIFICATION',   -- VERIFIED | NEEDS_VERIFICATION
    evidence TEXT, verified_at REAL,
    capabilities_json TEXT,                -- {"live":bool,"uploads":bool,"premieres":bool,"podcast":bool}
    monitoring_enabled INTEGER NOT NULL DEFAULT 1,
    owner_locked_json TEXT NOT NULL DEFAULT '[]',   -- fields the owner changed; the seeder never overwrites them
    acquisition TEXT NOT NULL DEFAULT 'PERMISSION_REQUIRED',   -- separate from monitoring
    legacy_source_id INTEGER,              -- existing `sources` row that handles recording (e.g. Rumble)
    priority INTEGER NOT NULL DEFAULT 50,
    dup_group TEXT,                        -- sources of the same show (simulcast) share a group
    live_state TEXT NOT NULL DEFAULT 'UNKNOWN',   -- LIVE | OFFLINE | UNKNOWN
    live_since TEXT, live_title TEXT, live_category TEXT,
    state TEXT NOT NULL DEFAULT 'NEEDS_VERIFICATION',  -- operational state shown to the owner
    error_code TEXT, last_error TEXT,
    consecutive_errors INTEGER NOT NULL DEFAULT 0,
    last_checked REAL, next_check REAL, last_success REAL,
    latest_media_id TEXT,
    requirement TEXT,                      -- what the owner must do (credentials, verification ...)
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_monitor_sources_due ON monitor_sources(monitoring_enabled, next_check);
CREATE TABLE IF NOT EXISTS discovered_items (
    id INTEGER PRIMARY KEY,
    monitor_source_id INTEGER NOT NULL REFERENCES monitor_sources(id) ON DELETE CASCADE,
    creator_id INTEGER NOT NULL,
    brand_key TEXT NOT NULL,
    platform TEXT NOT NULL,
    platform_item_id TEXT NOT NULL,
    kind TEXT NOT NULL,                    -- live | upcoming | upload | vod | episode
    title TEXT, url TEXT, published TEXT, scheduled_start TEXT, category TEXT,
    duration_s REAL,
    content_key TEXT,                      -- confirmed-identical content across platforms (simulcast)
    dup_of INTEGER,                        -- the first-discovered item for the same content
    state TEXT NOT NULL DEFAULT 'discovered',   -- discovered | baseline | queued | permission_required | duplicate | ignored | failed
    priority_tags TEXT,                    -- e.g. gta_vi_launch (a signal for review, never a classification)
    job_id INTEGER, detail TEXT,
    first_seen REAL NOT NULL, last_seen REAL NOT NULL,
    UNIQUE(monitor_source_id, platform_item_id)
);
CREATE TABLE IF NOT EXISTS monitor_events (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    monitor_source_id INTEGER,
    kind TEXT NOT NULL,                    -- state | live | discovered | error | recovered | priority | duplicate
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_monitor_events_ts ON monitor_events(ts);
-- ---- v7: verified-upload local cleanup audit trail ------------------------------------------------
CREATE TABLE IF NOT EXISTS cleanup_events (
    id INTEGER PRIMARY KEY,
    clip_id INTEGER NOT NULL,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,            -- evaluated | state | deleting | deleted | restored | restore_failed | held | cancelled | error | manual
    detail TEXT,
    evidence_json TEXT
);
CREATE INDEX IF NOT EXISTS ix_cleanup_events_clip ON cleanup_events(clip_id, ts);

CREATE TABLE IF NOT EXISTS priority_events (
    key TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    brand_key TEXT NOT NULL,
    starts_at TEXT NOT NULL,               -- ISO-8601 with offset
    ends_at TEXT NOT NULL,
    event_date TEXT,                       -- the launch/occurrence itself
    poll_seconds INTEGER NOT NULL DEFAULT 120,       -- elevated minimum interval for the brand's sources
    live_poll_seconds INTEGER NOT NULL DEFAULT 60,   -- while a source is live or a broadcast is imminent
    match_terms_json TEXT,                 -- words whose presence in source metadata tags an item
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL, updated_at REAL NOT NULL
);
"""


# Columns added after the first release: (table, column, declaration). Applied idempotently.
MIGRATIONS = [
    ("publish_attempts", "stage", "TEXT"),
    ("publish_attempts", "container_id", "TEXT"),
    ("publish_attempts", "job_id", "INTEGER"),
    ("publish_attempts", "confirmed_at", "REAL"),
    ("recordings", "selection_note", "TEXT"),
    ("source_items", "fingerprint", "TEXT"),
    # v4: when the recording was originally made/broadcast (may be years before it was imported)
    # and how it reached us: live_capture | archive | recent
    ("recordings", "recorded_at", "TEXT"),
    ("recordings", "origin", "TEXT"),
    # v5: creators, niches, lifecycle, retention, queue control
    ("sources", "creator_id", "INTEGER"),
    ("sources", "platform", "TEXT"),
    ("recordings", "creator_id", "INTEGER"),
    ("recordings", "niche_key", "TEXT"),
    ("recordings", "engine_owned", "INTEGER NOT NULL DEFAULT 0"),   # 1 = retrieved/recorded by the engine
    ("recordings", "lifecycle", "TEXT"),                           # see lifecycle.STAGES
    ("recordings", "raw_deleted_at", "REAL"),
    ("recordings", "raw_hold_reason", "TEXT"),
    ("clips", "niche_key", "TEXT"),
    ("clips", "quality_json", "TEXT"),
    ("clips", "title", "TEXT"),
    ("clips", "retain", "INTEGER NOT NULL DEFAULT 0"),             # owner 'keep': exempt from expiry
    ("clips", "published_at", "REAL"),
    ("clips", "review_opened_at", "REAL"),
    ("clips", "review_seconds", "REAL"),
    ("clips", "files_deleted_at", "REAL"),
    ("jobs", "priority", "INTEGER NOT NULL DEFAULT 0"),
    # v6: portfolio
    ("creators", "uid", "TEXT"),
    ("creators", "brand_key", "TEXT"),
    ("creators", "kind", "TEXT"),                                  # creator | show
    ("creators", "roster", "TEXT"),                                # 'approved' = owner-approved portfolio member
    ("creators", "acquisition_state", "TEXT"),                     # separate from monitoring
    ("monitor_sources", "notes", "TEXT"),                          # owner/roster notes (editable)
    # v7: context-complete selection and verified-upload local cleanup
    ("clips", "completeness_json", "TEXT"),                        # PASS/REVIEW/REJECT with per-dimension reasons
    ("clips", "duration_class", "TEXT"),                           # below | within | above (preferred range)
    ("clips", "keep_local", "INTEGER NOT NULL DEFAULT 0"),         # owner: never remove the local video
    ("clips", "local_deleted_at", "REAL"),                         # the local MP4 was removed after verified handoff
    ("clips", "cleanup_state", "TEXT"),                            # see cleanup.STATES
    ("clips", "cleanup_hold", "TEXT"),                             # owner hold / cancel reason
    ("clips", "cleanup_auto", "INTEGER"),                          # NULL = follow the global switch, 0 = never, 1 = allow
    ("clips", "restore_grace_until", "REAL"),                      # a restored copy is not cleaned up before this
    ("clips", "restored_at", "REAL"),
    ("clips", "local_bytes", "INTEGER"),
    ("publication_packages", "required", "INTEGER NOT NULL DEFAULT 1"),   # destination required before local cleanup
    ("publication_packages", "excluded_at", "REAL"),
    ("publication_packages", "exclude_reason", "TEXT"),
    ("publication_packages", "handoff_state", "TEXT"),             # see cleanup.HANDOFF_STATES
    ("publication_packages", "handoff_method", "TEXT"),            # api | manual
    ("publication_packages", "handoff_at", "REAL"),
    ("publication_packages", "handoff_evidence", "TEXT"),
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
        # Before upgrading an existing database to a newer schema, keep a consistent copy beside it
        # (data/backups). Owner data must survive an update even if a migration misbehaves.
        if self.path.is_file() and self.path.stat().st_size > 0:
            try:
                probe = sqlite3.connect(self.path)
                try:
                    row = probe.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
                finally:
                    probe.close()
                if row and int(row[0]) < SCHEMA_VERSION:
                    from .storage import backup_database
                    backup_database(self.path, self.path.parent / "backups", reason=f"before-schema-{SCHEMA_VERSION}")
            except sqlite3.Error:
                pass                      # no meta table yet: a brand-new database
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
