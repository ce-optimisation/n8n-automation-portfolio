"""Evidence report for live captures (read-only).

    .venv\\Scripts\\python.exe scripts\\live_report.py [--since 2026-11-16] [--out data\\live_report.txt]

For every live broadcast the engine detected: detection and recording times, duration and size, interruptions
and restarts, transcription time, candidate/rendered clips, Google Drive object ids and verification, the
raw-deletion gates and outcome, and whether the clips are in the review queue. It reads the database and files
only; it changes nothing. If no broadcast has been detected, it says so (the test is then still pending).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def ts(t) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) if t else "-"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="", help="YYYY-MM-DD: only broadcasts detected on/after this date")
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()
    from clipengine.config import get_settings
    from clipengine.db import Database
    s = get_settings()
    db = Database(s.db_path)
    since = time.mktime(time.strptime(a.since, "%Y-%m-%d")) if a.since else 0
    lines = [f"Live capture report - generated {ts(time.time())} - data folder {s.data_dir}"]
    for src in db.query("SELECT * FROM sources WHERE record_live=1"):
        lines.append(f"\nSource #{src['id']} {src['name']} ({src['url']}): enabled={bool(src['enabled'])} status={src['status']} "
                     f"last check {ts(src['last_checked'])}, next {ts(src['next_check'])}")
        if src["status_detail"]:
            lines.append(f"  note: {src['status_detail']}")
        if src["last_error"]:
            lines.append(f"  last error: {src['last_error'][:300]}")
    items = db.query("SELECT i.*, s.name sname FROM source_items i JOIN sources s ON s.id=i.source_id "
                     "WHERE i.kind='live' AND i.created_at >= ? ORDER BY i.id", (since,))
    if not items:
        lines.append("\nNo live broadcast has been detected yet: the real end-to-end test is PENDING.")
    for it in items:
        lines.append(f"\n=== Live broadcast: {it['title'] or it['item_key']} ({it['sname']}) ===")
        lines.append(f"1. detected / recording started: {ts(it['started_at'] or it['created_at'])}")
        lines.append(f"2. recording ended: {ts(it['ended_at'])}  state={it['state']}")
        lines.append(f"4. segments={it['segments']} restarts (interruptions recovered)={it['restarts']}"
                     + (f"  error: {it['error'][:300]}" if it["error"] else ""))
        logs = db.query("SELECT ts, level, message FROM job_logs WHERE message LIKE ? ORDER BY id", (f"%item {it['id']}%",))
        for lg in logs[:10]:
            lines.append(f"   log {ts(lg['ts'])} {lg['level']}: {lg['message'][:160]}")
        rec = db.recording(it["recording_id"]) if it["recording_id"] else None
        if not rec:
            lines.append("   not yet imported as a recording")
            continue
        meta = json.loads(rec["metadata_json"] or "{}")
        proc = meta.get("processing", {})
        size = rec["size_bytes"] or 0
        lines.append(f"3. recording #{rec['id']}: duration {(rec['duration'] or 0) / 60:.1f} min, size {size / 1e9:.2f} GB, "
                     f"{rec['width']}x{rec['height']}, status {rec['status']}, lifecycle {rec['lifecycle'] or '-'}")
        lines.append(f"5. transcription: {proc.get('transcribe_s', '-')} s ({proc.get('speed_x_realtime', '-')}x real time), "
                     f"model {rec['whisper_model'] or '-'}")
        sel = proc.get("selection", {})
        clips = db.query("SELECT * FROM clips WHERE recording_id=? ORDER BY idx", (rec["id"],))
        by = {}
        for c in clips:
            by[c["status"]] = by.get(c["status"], 0) + 1
        lines.append(f"6. candidates scored {sel.get('windows_scored', '-')}, selected {sel.get('selected', len(clips))}, "
                     f"rejected by niche policy {len(sel.get('rejected', []))}; clips by status {by}")
        for c in clips:
            q = json.loads(c["quality_json"] or "{}").get("quality")
            lines.append(f"7. clip #{c['id']} {c['start']:.0f}-{c['end']:.0f}s quality={q if q is not None else '-'} "
                         f"status={c['status']} file={'yes' if c['video_path'] and Path(c['video_path']).is_file() else 'no'}")
            for o in db.query("SELECT kind, state, remote_id, verified_at FROM cloud_objects WHERE owner_kind='clip' AND owner_id=?",
                              (c["id"],)):
                lines.append(f"8.   drive {o['kind']}: {o['state']} id={o['remote_id'] or '-'} verified {ts(o['verified_at'])}")
        try:
            from clipengine import cloud, lifecycle
            from clipengine.jobs import JobQueue
            from clipengine.publishing.tokens import TokenStore
            q = JobQueue(db)
            lc = lifecycle.Lifecycle(s, db, q, cloud.CloudService(s, db, q, TokenStore(s.secrets_dir)))
            for g in lc.gates(rec["id"]):
                lines.append(f"9.   gate {g['gate']}: {'PASS' if g['ok'] else 'not yet'} - {g['detail']}")
        except Exception as e:  # noqa: BLE001
            lines.append(f"9.   gates could not be evaluated: {e}")
        lines.append(f"9. raw recording: {'DELETED ' + ts(rec['raw_deleted_at']) if rec['raw_deleted_at'] else 'kept'}"
                     + (f" (held: {rec['raw_hold_reason']})" if rec["raw_hold_reason"] else ""))
        lines.append(f"10. in review queue: {by.get('ready', 0)} clip(s) awaiting review, {by.get('approved', 0)} approved")
    text = "\n".join(lines)
    print(text)
    if a.out:
        a.out.write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
