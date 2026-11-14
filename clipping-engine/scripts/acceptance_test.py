"""End-to-end acceptance test against the real running application.

Steps (spec §12):
 1. start the application (python -m clipengine)       6. preview the clip in the dashboard
 2. import a local video (HTTP upload)                    7. approve it
 3. generate its transcript                               8. download the approved MP4
 4. detect candidate passages                             9. restart and confirm records persist
 5. render at least one vertical clip

Usage:
    python scripts/acceptance_test.py            # real faster-whisper transcription
    python scripts/acceptance_test.py --stub     # TEST-ONLY stub transcript (no model download)

The --stub mode exists only for sandboxes that cannot download the Whisper
model; its output is labelled as such and does not verify transcription.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))


def log(msg):
    print(f"[acceptance] {msg}", flush=True)


def start_server(env, port, logfile):
    proc = subprocess.Popen([sys.executable, "-m", "clipengine", "--no-browser"], cwd=ROOT, env=env,
                            stdout=logfile, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    for _ in range(120):
        if proc.poll() is not None:
            raise SystemExit(f"server exited early with code {proc.returncode}")
        try:
            if httpx.get(base + "/healthz", timeout=2).status_code == 200:
                return proc, base
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    proc.kill()
    raise SystemExit("server did not start")


def stop_server(proc):
    proc.terminate()
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stub", action="store_true", help="TEST-ONLY stub transcriber")
    ap.add_argument("--port", type=int, default=8799)
    ap.add_argument("--keep", help="directory to keep artifacts (screenshots, downloaded clip)")
    ap.add_argument("--timeout", type=int, default=1800)
    args = ap.parse_args()

    from make_sample_video import make
    work = Path(tempfile.mkdtemp(prefix="clipengine_accept_"))
    keep = Path(args.keep) if args.keep else work / "artifacts"
    keep.mkdir(parents=True, exist_ok=True)
    sample = make(work / "sample_council_briefing.mp4")
    log(f"sample video: {sample}")

    env = dict(os.environ, CLIPENGINE_DATA_DIR=str(work / "data"), CLIPENGINE_PORT=str(args.port),
               CLIPENGINE_HOST="127.0.0.1", OPEN_BROWSER="0", LLM_PROVIDER="none", DASHBOARD_PASSWORD="",
               TRANSCRIBE_LANGUAGE="en", PYTHONUNBUFFERED="1")
    env.setdefault("WHISPER_MODEL", "base.en")
    mode = "REAL faster-whisper transcription"
    if args.stub:
        from clipengine.testsupport import stub_transcript_from_timing
        stub = work / "stub.json"
        stub.write_text(json.dumps(stub_transcript_from_timing(sample.with_suffix(".timing.json"))))
        env.update(CLIPENGINE_TRANSCRIBER="stub", CLIPENGINE_STUB_TRANSCRIPT=str(stub))
        mode = "TEST-ONLY STUB transcript (transcription NOT verified)"
    log(f"mode: {mode}")
    logfile = open(keep / "server.log", "ab")
    results = {}

    # 1. start
    proc, base = start_server(env, args.port, logfile)
    results["1_start"] = "PASS"
    try:
        c = httpx.Client(base_url=base, timeout=120)
        # 2. import local video
        with open(sample, "rb") as f:
            r = c.post("/import/upload", files={"file": (sample.name, f, "video/mp4")},
                       data={"title": "Acceptance sample: council bridge briefing"},
                       headers={"X-Requested-With": "XMLHttpRequest"})
        r.raise_for_status()
        rid = r.json()["recording_id"]
        log(f"imported recording #{rid}: {r.json()['msg']}")
        results["2_import"] = "PASS"

        # wait for processing
        t0 = time.time()
        while True:
            st = c.get(f"/api/recordings/{rid}").json()
            active = [j for j in st["jobs"] if j["status"] in ("queued", "running")]
            failed = [j for j in st["jobs"] if j["status"] == "failed"]
            if failed:
                raise SystemExit(f"job failed: {failed}")
            if st["clips"] and not active:
                break
            if time.time() - t0 > args.timeout:
                raise SystemExit("timed out waiting for processing")
            time.sleep(2)
        log(f"processing finished in {time.time() - t0:.0f}s with {st['recording']['whisper_model']}")

        # 3. transcript
        text = st["transcript_text"].lower()
        assert st["transcript_segments"] > 0, "no transcript"
        expected = ["council", "bridge", "ferry", "consultation"]
        hits = [w for w in expected if w in text]
        log(f"transcript: {st['transcript_segments']} segments; expected words found: {hits}")
        assert len(hits) >= 3, f"transcript does not match the spoken content: {text[:300]}"
        results["3_transcript"] = "PASS" if not args.stub else "PASS (stub — not a transcription test)"

        # 4. candidates
        assert len(st["clips"]) >= 1
        log(f"candidates: {[(round(x['start'], 1), round(x['end'], 1), x['status']) for x in st['clips']]}")
        results["4_candidates"] = "PASS"

        # 5. rendered vertical clip
        clip = next(x for x in st["clips"] if x["status"] == "ready" and x["rendered"])
        cid = clip["id"]
        v = c.get(f"/clips/{cid}/video")
        assert v.status_code == 200 and v.headers["content-type"].startswith("video/mp4")
        rng = c.get(f"/clips/{cid}/video", headers={"Range": "bytes=0-1023"})
        assert rng.status_code == 206, f"range requests unsupported ({rng.status_code}); mobile Safari needs them"
        tmpclip = keep / "preview.mp4"
        tmpclip.write_bytes(v.content)
        pr = json.loads(subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams",
                                        str(tmpclip)], capture_output=True, text=True).stdout)
        vs = next(s for s in pr["streams"] if s["codec_type"] == "video")
        aus = next(s for s in pr["streams"] if s["codec_type"] == "audio")
        assert (vs["width"], vs["height"], vs["codec_name"], aus["codec_name"]) == (1080, 1920, "h264", "aac"), pr
        results["5_render_1080x1920_h264_aac"] = "PASS"

        # 6. preview in dashboard
        page = c.get("/review").text
        assert f'src="/clips/{cid}/video' in page, "clip video not embedded in review queue"
        results["6_dashboard_preview"] = "PASS (HTML embeds playable video)"
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                b = p.chromium.launch(executable_path=os.environ.get("PW_CHROMIUM") or None)
                pg = b.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True)
                pg.goto(base + "/review")
                pg.wait_for_function(
                    f"document.querySelector('#clip-{cid} video') && "
                    f"document.querySelector('#clip-{cid} video').readyState >= 1", timeout=30000)
                dur = pg.evaluate(f"document.querySelector('#clip-{cid} video').duration")
                pg.screenshot(path=str(keep / "review_mobile.png"), full_page=False)
                pg.goto(base + f"/clips/{cid}")
                pg.screenshot(path=str(keep / "clip_detail_mobile.png"), full_page=True)
                b.close()
            log(f"browser loaded clip metadata (duration {dur:.1f}s); screenshots saved")
            results["6_dashboard_preview"] = "PASS (browser loaded video in mobile viewport)"
        except ImportError:
            log("playwright not installed; browser preview check skipped (HTML check passed)")
        except Exception as e:  # noqa: BLE001 - browser availability varies; HTML check already passed
            log(f"browser preview check could not run ({type(e).__name__}: {str(e)[:200]}); HTML check passed")

        # 7. approve
        r = c.post(f"/clips/{cid}/approve", data={"note": "acceptance test"}, follow_redirects=False)
        assert r.status_code == 303
        rejected = next((x for x in st["clips"] if x["id"] != cid), None)
        if rejected:
            c.post(f"/clips/{rejected['id']}/reject", follow_redirects=False)
        results["7_approve_reject"] = "PASS"

        # 8. download approved MP4
        d = c.get(f"/clips/{cid}/download")
        assert d.status_code == 200 and "attachment" in d.headers.get("content-disposition", "")
        (keep / "approved_clip.mp4").write_bytes(d.content)
        results["8_download"] = f"PASS ({len(d.content) / 1e6:.1f} MB)"
    finally:
        stop_server(proc)

    # 9. restart and confirm persistence
    proc, base = start_server(env, args.port, logfile)
    try:
        st2 = httpx.get(f"{base}/api/recordings/{rid}", timeout=30).json()
        status = {x["id"]: x["status"] for x in st2["clips"]}
        assert status[cid] == "approved", status
        if rejected:
            assert status[rejected["id"]] == "rejected", status
        assert st2["transcript_segments"] == st["transcript_segments"]
        assert httpx.get(f"{base}/clips/{cid}/download", timeout=30).status_code == 200
        results["9_restart_persistence"] = "PASS"
    finally:
        stop_server(proc)
        logfile.close()

    log("RESULTS (" + mode + "):")
    for k, v in results.items():
        log(f"  {k:32s} {v}")
    (keep / "results.json").write_text(json.dumps({"mode": mode, "results": results,
                                                   "transcript_excerpt": st["transcript_text"][:600]}, indent=2))
    if not args.keep:
        shutil.rmtree(work / "data", ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
