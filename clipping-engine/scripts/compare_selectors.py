"""Compare the legacy selectors (v4, v5) with the context-complete selector (v6).

    python scripts/compare_selectors.py                       built-in fixtures (what the regression tests use)
    python scripts/compare_selectors.py --recording 12        a REAL recording from the engine's database (read-only):
                                                              run it on the owner's two example recordings to see whether
                                                              the new selector fixes what the owner judged by eye
    python scripts/compare_selectors.py --recording 12 --kind political_monologue --min 40 --max 180

It only reads; it never writes the database or renders anything. A table of unit-test fixtures proves the rules work;
it does NOT prove the clips are better for the owner: that needs the real recordings and a human look.
"""
import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from clipengine import complete, segment  # noqa: E402


def row(label, profile, segs, kind, pref, n=4):
    rep = {}
    kw = {"preferred": pref, "content_type": kind} if profile == "v6" else {}
    cands = segment.select(segs, n=n, min_s=pref[0], max_s=pref[1], profile=profile, report=rep, **kw)
    if not cands:
        print(f"{label:30} {profile}: no clips" + (f"  ({rep.get('note', '')[:110]})" if profile == "v6" else ""))
    for c in cands:
        a = c.assessment or {}
        verdict = a.get("verdict", "-")
        print(f"{label:30} {profile}: {c.start:7.1f}-{c.end:7.1f} ({c.duration:5.0f}s) {verdict:6} "
              f"{a.get('duration_class', ''):6} \"{c.text[:60]}...\"")


def fixtures():
    import test_complete_selection as t
    return [
        ("monologue with setup+qualifier", [t.SETUP, 0.4, t.ARGUMENT, 0.4, t.QUALIFY, 7.0, t.OTHER], "political_monologue", t.PREF_POL),
        ("4+ minute monologue", ["Let me explain the housing crisis and what the evidence says about rent and planning."]
         + t.long_monologue() + [8.0, t.OTHER], "political_monologue", t.PREF_POL),
        ("short gaming moment", ["Welcome back everyone, thanks for watching.", 4.0, t.GAME, 6.0,
                                 "Okay so now let me check the chat for a moment."], "gaming", (12.0, 60.0)),
        ("no worthwhile passage", ["Hey welcome back everyone.", "Thanks for joining us today, um, let me just check the chat.",
                                   "Uh okay it looks fine.", "Alright that's it, see you next time, thanks for watching."],
         "political_monologue", t.PREF_POL),
    ], t.transcript


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--recording", type=int)
    ap.add_argument("--db")
    ap.add_argument("--kind", default=None, choices=list(complete.PREFERRED))
    ap.add_argument("--min", type=float)
    ap.add_argument("--max", type=float)
    a = ap.parse_args()
    if a.recording:
        from clipengine.config import get_settings, load_dotenv
        load_dotenv()
        path = a.db or str(get_settings().db_path)
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        import json
        segs = [{"start": r["start"], "end": r["end"], "text": r["text"], "words": json.loads(r["words_json"] or "[]")}
                for r in con.execute("SELECT * FROM transcript_segments WHERE recording_id=? ORDER BY start", (a.recording,))]
        if not segs:
            print("No transcript stored for that recording.")
            return 1
        lo, hi = complete.PREFERRED.get(a.kind, (a.min or 30.0, a.max or 120.0))
        for prof in ("v4", "v5", "v6"):
            row(f"recording {a.recording}", prof, segs, a.kind, (a.min or lo, a.max or hi))
        return 0
    fx, tr = fixtures()
    for label, parts, kind, pref in fx:
        segs = tr(parts)
        for prof in ("v4", "v5", "v6"):
            row(label, prof, segs, kind, pref)
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
