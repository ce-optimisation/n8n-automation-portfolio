"""Read-only storage audit: where the disk went, and what could be recovered under which rule.

Run on the laptop:  Storage-Audit.cmd   (or  python -m clipengine --storage-audit [--json])

It NEVER deletes or changes anything and never opens the engine database for writing (SQLite read-only URI, so no
migration or backup is triggered). It scans only the engine's own folders (data dir, install folder, .venv) - not
OneDrive, Documents or any other personal directory.

Classes, as in the owner's brief:
  A  safe disposable   - engine scratch/partials not referenced by an active job (maintenance removes these itself)
  B  eligible raw      - engine-owned recordings that pass ALL eight lifecycle gates (the existing policy deletes them)
  C  finished clips    - local MP4s with a verified Drive master (cleanup is governed by the Session 9 rules)
  D  protected         - owner uploads, unverified/unreviewed footage, active jobs, credentials, databases, logs, backups
Recordings blocked by a gate are listed with the FIRST gate that blocks them, so the blocker is visible.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

from .config import PROJECT_ROOT, Settings

GATE_ORDER = ["engine_owned", "finalised", "transcript", "rendered", "integrity", "cloud_verified", "no_jobs"]
PARTIAL = (".part", ".ytdl", ".tmp")


def _size(p: Path) -> int:
    try:
        if p.is_file():
            return p.stat().st_size
        return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
    except OSError:
        return 0


class _RO:
    """Just enough of Database for Lifecycle.gates, over a read-only connection."""

    def __init__(self, path: Path):
        self.c = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        self.c.row_factory = sqlite3.Row

    def query(self, sql, params=()):
        return self.c.execute(sql, params).fetchall()

    def one(self, sql, params=()):
        return self.c.execute(sql, params).fetchone()

    def recording(self, rid):
        return self.one("SELECT * FROM recordings WHERE id=?", (rid,))

    def clip(self, cid):
        return self.one("SELECT * FROM clips WHERE id=?", (cid,))


class _RoCloud:
    """Verified = video and metadata objects are in state 'verified' (same test as CloudService.clip_verified)."""

    def __init__(self, db: _RO):
        self.db = db
        self.provider = SimpleNamespace(connected=lambda: True)

    def clip_verified(self, clip_id, required=("video", "metadata")):
        rows = {r["kind"]: r["state"] for r in self.db.query(
            "SELECT kind, state FROM cloud_objects WHERE owner_kind='clip' AND owner_id=?", (clip_id,))}
        return all(rows.get(k) == "verified" for k in required)


def audit(settings: Settings, top: int = 12) -> dict:
    from .lifecycle import Lifecycle
    now = time.time()
    data = settings.data_dir
    usage = shutil.disk_usage(data if data.exists() else data.parent)
    rep: dict = {"generated": time.strftime("%Y-%m-%d %H:%M"), "data_dir": str(data),
                 "drive": {"total_gb": usage.total / 1e9, "free_gb": usage.free / 1e9, "used_gb": usage.used / 1e9,
                           "reserve_gb": settings.disk_reserve_gb}}
    cats = {
        "recordings (raw)": _size(settings.recordings_dir), "finished clips": _size(settings.clips_dir),
        "scratch (work)": _size(settings.work_dir), "models": _size(settings.models_dir),
        "inbox": _size(settings.inbox), "logs": _size(data / "logs"), "backups": _size(data / "backups"),
        "selftest": _size(data / "selftest"), "database": sum(_size(f) for f in data.glob("clipengine.sqlite3*")),
        "python venv (.venv)": _size(PROJECT_ROOT / ".venv"), "install folder (code)": 0,
    }
    cats["install folder (code)"] = max(0, _size(PROJECT_ROOT) - cats["python venv (.venv)"])
    rep["categories_gb"] = {k: round(v / 1e9, 3) for k, v in sorted(cats.items(), key=lambda kv: -kv[1])}
    rep["engine_total_gb"] = round(sum(cats.values()) / 1e9, 3)

    # -- scratch intermediates and partials (class A when old and no job owns them)
    jobs_busy = 0
    db = None
    if settings.db_path.is_file():
        try:
            db = _RO(settings.db_path)
            jobs_busy = db.one("SELECT COUNT(*) n FROM jobs WHERE status IN ('running','queued')")["n"]
        except sqlite3.Error as e:
            rep["database_error"] = str(e)
            db = None
    scratch = []
    for f in settings.work_dir.rglob("*") if settings.work_dir.exists() else []:
        if f.is_file():
            age = (now - f.stat().st_mtime) / 3600
            scratch.append({"path": str(f), "mb": round(f.stat().st_size / 1e6, 1), "age_h": round(age, 1),
                            "kind": "audio intermediate" if f.suffix == ".wav" else "partial" if f.suffix in PARTIAL else "other"})
    rep["class_A"] = {
        "scratch_files": len(scratch), "scratch_gb": round(sum(s["mb"] for s in scratch) / 1e3, 3),
        "older_than_24h_gb": round(sum(s["mb"] for s in scratch if s["age_h"] > 24) / 1e3, 3),
        "partial_downloads_gb": round(sum(s["mb"] for s in scratch if s["kind"] == "partial") / 1e3, 3),
        "note": "Removed by the hourly maintenance pass when older than 24 h and no job is running"
                + (f" ({jobs_busy} job(s) active now - nothing is removed while one runs)." if jobs_busy else "."),
    }

    if db is None:
        rep["note"] = "No readable engine database found: recordings/clips could not be classified."
        return rep

    # -- raw recordings: which of the eight gates blocks deletion?
    life = SimpleNamespace(settings=settings, db=db, cloud=_RoCloud(db))
    blockers: dict[str, dict] = {}
    recs = []
    eligible_bytes = protected_owner = 0
    for r in db.query("SELECT * FROM recordings WHERE file_path IS NOT NULL AND raw_deleted_at IS NULL"):
        p = Path(r["file_path"])
        size = _size(p)
        if not size:
            continue
        try:
            gates = Lifecycle.gates(life, r["id"])
        except Exception as e:  # noqa: BLE001
            gates = [{"gate": "error", "ok": False, "detail": str(e)}]
        failing = [g for g in gates if not g["ok"]]
        first = failing[0] if failing else None
        key = "ELIGIBLE (all gates pass)" if not failing else f"blocked: {first['gate']}"
        b = blockers.setdefault(key, {"recordings": 0, "gb": 0.0, "why": first["detail"] if first else "every deletion gate is satisfied"})
        b["recordings"] += 1
        b["gb"] = round(b["gb"] + size / 1e9, 3)
        if not failing:
            eligible_bytes += size
        elif first["gate"] == "engine_owned":
            protected_owner += size
        recs.append({"id": r["id"], "title": (r["title"] or "")[:60], "gb": round(size / 1e9, 3), "status": r["status"],
                     "blocker": first["gate"] if first else None, "detail": first["detail"] if first else "eligible"})
    rep["class_B_raw_recordings"] = {
        "eligible_gb": round(eligible_bytes / 1e9, 3), "owner_supplied_protected_gb": round(protected_owner / 1e9, 3),
        "by_outcome": blockers, "largest": sorted(recs, key=lambda x: -x["gb"])[:top],
        "auto_delete_enabled": bool(settings.raw_delete_after_cloud),
        "note": "Eligible recordings are removed by the existing lifecycle policy once its checks pass; this audit never deletes."}

    # -- finished clips
    c_rows = db.query("SELECT id, status, video_path, local_deleted_at, published_at, retain FROM clips WHERE video_path IS NOT NULL")
    cloud = _RoCloud(db)
    buckets = {"verified_in_drive": [0, 0], "not_verified": [0, 0], "rejected": [0, 0]}
    for c in c_rows:
        if c["local_deleted_at"]:
            continue
        sz = _size(Path(c["video_path"]).parent)
        k = "rejected" if c["status"] == "rejected" else "verified_in_drive" if cloud.clip_verified(c["id"]) else "not_verified"
        buckets[k][0] += 1
        buckets[k][1] += sz
    rep["class_C_finished_clips"] = {k: {"clips": v[0], "gb": round(v[1] / 1e9, 3)} for k, v in buckets.items()}
    rep["class_C_finished_clips"]["note"] = ("'verified_in_drive' clips can be removed locally once the owner enables "
                                             "local cleanup / Drive-only archive; 'not_verified' are protected.")

    # -- orphans: files in the engine folders that no database row refers to (reported, never auto-deleted)
    known = {str(Path(r["file_path"]).resolve()) for r in db.query("SELECT file_path FROM recordings WHERE file_path IS NOT NULL")}
    orphans = []
    if settings.recordings_dir.exists():
        for f in settings.recordings_dir.rglob("*"):
            if f.is_file() and str(f.resolve()) not in known and f.suffix not in PARTIAL:
                orphans.append({"path": str(f), "gb": round(f.stat().st_size / 1e9, 3)})
    live_dirs = {str(Path(c["video_path"]).parent.resolve()) for c in c_rows}
    stale_rev = []
    if settings.clips_dir.exists():
        for d in settings.clips_dir.glob("rec*/clip*_r*"):
            if d.is_dir() and str(d.resolve()) not in live_dirs:
                stale_rev.append({"path": str(d), "gb": round(_size(d) / 1e9, 3)})
    rep["orphans"] = {"recording_files_not_in_database": sorted(orphans, key=lambda x: -x["gb"])[:top],
                      "clip_folders_not_current": sorted(stale_rev, key=lambda x: -x["gb"])[:top],
                      "note": "Reported for the owner's decision. Not deleted automatically."}

    # -- duplicates (same sha256 in two recordings)
    dups = db.query("SELECT sha256, COUNT(*) n, GROUP_CONCAT(id) ids FROM recordings WHERE sha256 IS NOT NULL AND file_path IS NOT NULL "
                    "AND raw_deleted_at IS NULL GROUP BY sha256 HAVING n > 1")
    rep["duplicates"] = [{"ids": d["ids"], "copies": d["n"]} for d in dups]
    rep["jobs"] = {"active": jobs_busy, "failed": db.one("SELECT COUNT(*) n FROM jobs WHERE status='failed'")["n"]}
    rep["drive_backups"] = {"verified_clip_masters": db.one("SELECT COUNT(*) n FROM cloud_objects WHERE kind='video' AND state='verified'")["n"]}
    rep["recoverable_estimate_gb"] = round(rep["class_A"]["older_than_24h_gb"] + eligible_bytes / 1e9, 3)
    return rep


def format_report(rep: dict) -> str:
    d = rep["drive"]
    out = [f"STORAGE AUDIT  {rep['generated']}  (read-only: nothing was changed)",
           f"Drive holding the engine data: {d['free_gb']:.1f} GB free of {d['total_gb']:.0f} GB ({d['used_gb']:.0f} GB used)",
           f"Engine folders total: {rep['engine_total_gb']:.2f} GB", "", "Where the engine's space is:"]
    out += [f"  {v:8.2f} GB  {k}" for k, v in rep["categories_gb"].items()]
    a = rep["class_A"]
    out += ["", f"A. Safe disposable scratch: {a['scratch_files']} files, {a['scratch_gb']:.2f} GB "
            f"({a['older_than_24h_gb']:.2f} GB older than 24 h; partial downloads {a['partial_downloads_gb']:.2f} GB). {a['note']}"]
    if "class_B_raw_recordings" in rep:
        b = rep["class_B_raw_recordings"]
        out += ["", f"B. Raw recordings still on disk (automatic deletion setting: {'ON' if b['auto_delete_enabled'] else 'OFF'}; eligible now: {b['eligible_gb']:.2f} GB; owner-supplied/protected: "
                f"{b['owner_supplied_protected_gb']:.2f} GB):"]
        for k, v in b["by_outcome"].items():
            out.append(f"   {v['gb']:7.2f} GB  {v['recordings']:3d} recording(s)  {k} - {v['why']}")
        out += ["   Largest:"] + [f"   {r['gb']:7.2f} GB  #{r['id']} {r['title']} [{r['status']}] {r['blocker'] or 'eligible'}" for r in b["largest"]]
        c = rep["class_C_finished_clips"]
        out += ["", "C. Finished clips on disk: " + ", ".join(f"{k} {v['clips']} ({v['gb']:.2f} GB)" for k, v in c.items() if k != "note"), "   " + c["note"]]
        o = rep["orphans"]
        out += ["", f"Orphans: {len(o['recording_files_not_in_database'])} unreferenced recording file(s), "
                f"{len(o['clip_folders_not_current'])} non-current clip folder(s) (listed in the JSON; never auto-deleted)."]
        out += [f"Duplicate recordings (same checksum): {len(rep['duplicates'])} group(s)",
                f"Jobs: {rep['jobs']['active']} active, {rep['jobs']['failed']} failed.  Verified Drive clip masters: {rep['drive_backups']['verified_clip_masters']}",
                "", f"Estimated recoverable under existing rules: about {rep['recoverable_estimate_gb']:.2f} GB (class A + class B)."]
    else:
        out += ["", rep.get("note", "")]
    out += ["", "D. Protected and never touched: owner uploads, unverified or unreviewed footage, active jobs, credentials, databases, logs, backups, OneDrive and personal files."]
    return "\n".join(out)


def main(settings: Settings, as_json: bool = False) -> int:
    rep = audit(settings)
    if as_json:
        print(json.dumps(rep, indent=2))
    else:
        print(format_report(rep))
    try:
        logs = settings.data_dir / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        (logs / f"storage-audit-{time.strftime('%Y%m%d-%H%M')}.json").write_text(json.dumps(rep, indent=2), encoding="utf-8")
    except OSError:
        pass
    return 0
