"""Genuine-recording acceptance test: a real, several-minute recording of real people speaking,
taken through the REAL running application with REAL faster-whisper.

Recording: "Landsat 9 Media Briefing", NASA / U.S. Geological Survey, 31 August 2021
           NASA Image and Video Library, id GSFC_20210831_Landsat_9_Briefing
           https://images.nasa.gov/details/GSFC_20210831_Landsat_9_Briefing
Rights:    NASA-produced material is generally not subject to copyright in the United States and
           may be used for educational/informational purposes (NASA Media Usage Guidelines).
           Used here only to test the software; no endorsement by NASA or USGS is implied.
Excerpt:   8 minutes (10:20–18:20): three speakers and several natural changes of topic.

What is verified (and written to report.json):
  1  the file arrives in the inbox with a provenance sidecar and is recognised
  2  provenance: original address, original date (2021-08-31), origin = archive
  3  no duplicate recording or job when the same file arrives again
  4  real faster-whisper transcription (expected vocabulary present; not the stub)
  5  excerpts are selected (profile, durations, opening words)
  6  every clip renders; 7 captions are present in the approved Anton style
  8  every MP4 is valid 1080x1920 H.264/AAC with audible audio; a frame of each is saved
  9  Ready to Review shows the clips; 10 preview (Range) and download work
  11 everything survives an application restart

    python scripts/real_recording_test.py --keep genuine-artifacts
    python scripts/real_recording_test.py --video path\\to\\already_downloaded.mp4 --keep out
"""
from __future__ import annotations

import argparse
import json
import os
import re
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

from acceptance_test import start_server, stop_server  # noqa: E402

NASA_ID = "GSFC_20210831_Landsat_9_Briefing"
MEDIA = f"https://images-assets.nasa.gov/video/{NASA_ID}/{NASA_ID}~{{variant}}.mp4"
DETAILS = f"https://images.nasa.gov/details/{NASA_ID}"
START, LENGTH = 620, 480
EXPECTED = ["landsat", "satellite", "wildfire", "usgs", "infrared", "agriculture", "calibrated", "glacier",
            "archive", "kilometers"]


def log(msg):
    print(f"[genuine] {msg}", flush=True)


def fetch_excerpt(dst: Path) -> tuple[Path, str]:
    """Download only the excerpt (HTTP range requests through FFmpeg, stream copy, no re-encode)."""
    errors = []
    for variant in ("small", "mobile", "medium"):
        url = MEDIA.format(variant=variant)
        log(f"downloading {LENGTH // 60} min excerpt from {url}")
        p = subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(START), "-t", str(LENGTH), "-i", url,
                            "-map", "0:v:0", "-map", "0:a:0", "-c", "copy", "-movflags", "+faststart", str(dst)],
                           capture_output=True, text=True)
        if p.returncode == 0 and dst.exists() and dst.stat().st_size > 2_000_000:
            return dst, url
        errors.append(f"{variant}: {p.stderr.strip()[-200:]}")
    # FFmpeg could not reach the server directly (for example behind a proxy): download the smallest
    # full file with the ordinary HTTP client, then cut the same excerpt locally.
    url = MEDIA.format(variant="mobile")
    full = dst.with_name("full_download.mp4")
    try:
        log(f"direct excerpt download failed; fetching the whole file instead: {url}")
        with httpx.stream("GET", url, timeout=120, follow_redirects=True) as r:
            r.raise_for_status()
            with open(full, "wb") as f:
                for chunk in r.iter_bytes(1024 * 1024):
                    f.write(chunk)
        p = subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(START), "-t", str(LENGTH), "-i", str(full),
                            "-map", "0:v:0", "-map", "0:a:0", "-c", "copy", "-movflags", "+faststart", str(dst)],
                           capture_output=True, text=True)
        full.unlink(missing_ok=True)
        if p.returncode == 0 and dst.exists() and dst.stat().st_size > 2_000_000:
            return dst, url
        errors.append(f"local cut: {p.stderr.strip()[-200:]}")
    except Exception as e:  # noqa: BLE001
        errors.append(f"full download: {type(e).__name__}: {e}")
    raise SystemExit("could not obtain the NASA recording: " + " | ".join(errors))


def probe(path: Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)],
                         capture_output=True, text=True).stdout
    d = json.loads(out)
    v = next(s for s in d["streams"] if s["codec_type"] == "video")
    a = next((s for s in d["streams"] if s["codec_type"] == "audio"), None)
    return {"width": v["width"], "height": v["height"], "vcodec": v["codec_name"], "acodec": a["codec_name"] if a else None,
            "duration": round(float(d["format"]["duration"]), 2), "fps": v.get("r_frame_rate"),
            "size_mb": round(int(d["format"]["size"]) / 1e6, 2)}


def mean_volume(path: Path) -> float:
    err = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(path), "-vn", "-af", "volumedetect", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    m = re.search(r"mean_volume:\s*(-?[\d.]+) dB", err)
    return float(m.group(1)) if m else -99.0


def wait_until(fn, timeout, what, step=2.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        v = fn()
        if v:
            return v
        time.sleep(step)
    raise SystemExit(f"timed out waiting for {what}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", type=Path, help="use this already-downloaded excerpt instead of downloading")
    ap.add_argument("--port", type=int, default=8797)
    ap.add_argument("--keep", required=True, help="directory for the clips, frames, logs and report.json")
    ap.add_argument("--timeout", type=int, default=3600)
    args = ap.parse_args()
    work = Path(tempfile.mkdtemp(prefix="clipengine_genuine_"))
    keep = Path(args.keep)
    keep.mkdir(parents=True, exist_ok=True)
    report: dict = {"recording": {"title": "Landsat 9 Media Briefing (NASA/USGS, 31 Aug 2021) — 8 minute excerpt",
                                  "source": DETAILS, "nasa_id": NASA_ID, "excerpt_seconds": [START, START + LENGTH],
                                  "rights": "NASA-produced material; NASA Media Usage Guidelines; test use only"},
                    "checks": {}}
    ok = report["checks"]
    if args.video:
        video, media_url = work / "landsat9_briefing_excerpt.mp4", f"local file {args.video}"
        shutil.copy(args.video, video)
    else:
        video, media_url = fetch_excerpt(work / "landsat9_briefing_excerpt.mp4")
    src = probe(video)
    report["recording"].update(media_url=media_url, file=src)
    log(f"recording: {src}")
    assert src["duration"] >= 240 and src["acodec"], "the test recording must be several minutes long with audio"

    data = work / "data"
    inbox = data / "inbox"
    inbox.mkdir(parents=True)
    env = dict(os.environ, CLIPENGINE_DATA_DIR=str(data), CLIPENGINE_PORT=str(args.port), CLIPENGINE_HOST="127.0.0.1",
               OPEN_BROWSER="0", LLM_PROVIDER="none", DASHBOARD_PASSWORD="", TRANSCRIBE_LANGUAGE="en",
               PYTHONUNBUFFERED="1", CLIPENGINE_ENV_FILE=str(work / "test.env"), NOTIFY_TO="", MONITOR_ENABLED="0")
    env.pop("CLIPENGINE_TRANSCRIBER", None)                      # this test is only meaningful with real Whisper
    env.setdefault("WHISPER_MODEL", "base.en")
    logfile = open(keep / "server.log", "ab")
    proc, base = start_server(env, args.port, logfile)
    t_total = time.time()
    try:
        c = httpx.Client(base_url=base, timeout=120)
        # 1. arrives in the inbox with a sidecar (what a recorder or the owner would write)
        name = "Landsat 9 Media Briefing.mp4"
        (inbox / (name + ".json")).write_text(json.dumps(
            {"source_url": DETAILS, "recorded_at": "2021-08-31", "title": "Landsat 9 Media Briefing (NASA/USGS)",
             "source_name": "NASA Image and Video Library", "recorder": "owner-supplied archive file",
             "authorisation": "NASA Media Usage Guidelines (NASA-produced material)"}), encoding="utf-8")
        tmp = inbox / (name + ".copying")
        shutil.copy(video, tmp)
        tmp.replace(inbox / name)
        old = time.time() - 30
        os.utime(inbox / name, (old, old))
        rid = wait_until(lambda: next((r["id"] for r in [c.get("/api/recordings/1").json().get("recording")] if r), None)
                         if c.get("/api/recordings/1").status_code == 200 else None, 120, "the inbox import")
        ok["1_inbox_recognised"] = "PASS"
        log(f"inbox file recognised as recording #{rid}")

        st = wait_until(lambda: (lambda s: s if (s["clips"] and not [j for j in s["jobs"] if j["status"] in
                                                                     ("queued", "running")]) else
                                 (_ for _ in ()).throw(SystemExit(f"job failed: {[j for j in s['jobs'] if j['status'] == 'failed']}"))
                                 if [j for j in s["jobs"] if j["status"] == "failed"] else None)(
            c.get(f"/api/recordings/{rid}").json()), args.timeout, "processing")
        wall = time.time() - t_total
        page = c.get(f"/recordings/{rid}").text

        # 2. provenance
        assert DETAILS in page and "2021-08-31" in page and "archive recording" in page, "provenance not shown"
        assert st["recording"]["source_type"] == "recorder_import"
        ok["2_provenance_source_date_archive"] = "PASS"

        # 4. real transcription
        model = st["recording"]["whisper_model"] or ""
        assert "faster-whisper" in model and "STUB" not in model.upper(), f"not real Whisper: {model}"
        text = st["transcript_text"].lower()
        hits = [w for w in EXPECTED if w in text]
        assert len(hits) >= 6, f"transcript does not match the briefing: {hits} / {text[:300]}"
        m = re.search(r"transcription ([\d.]+)s \(([\d.]+)× real time", page)
        report["processing"] = {"model": model, "transcript_segments": st["transcript_segments"],
                                "transcript_words": len(text.split()), "expected_words_found": hits,
                                "transcribe_seconds": float(m.group(1)) if m else None,
                                "speed_x_realtime": float(m.group(2)) if m else None,
                                "total_wall_seconds_import_to_all_clips_ready": round(wall, 1)}
        ok["4_real_whisper_transcription"] = f"PASS ({model}; {len(hits)}/{len(EXPECTED)} expected words)"
        log(f"transcription: {report['processing']}")

        # 5–8. clips
        clips = st["clips"]
        assert clips and all(x["status"] == "ready" and x["rendered"] for x in clips), [x["status"] for x in clips]
        ok["5_excerpts_selected"] = f"PASS ({len(clips)} clip(s))"
        report["clips"] = []
        for x in clips:
            cid = x["id"]
            v = c.get(f"/clips/{cid}/video")
            assert v.status_code == 200
            f = keep / f"clip_{cid:02d}_{int(x['start']) + START}s.mp4"
            f.write_bytes(v.content)
            pr = probe(f)
            assert (pr["width"], pr["height"], pr["vcodec"], pr["acodec"]) == (1080, 1920, "h264", "aac"), pr
            assert abs(pr["duration"] - (x["end"] - x["start"])) < 1.5, (pr["duration"], x)
            vol = mean_volume(f)
            assert vol > -45, f"clip {cid} audio is silent ({vol} dB)"
            dec = subprocess.run(["ffmpeg", "-v", "error", "-i", str(f), "-f", "null", "-"], capture_output=True, text=True)
            assert dec.returncode == 0 and not dec.stderr.strip(), f"decode errors in clip {cid}: {dec.stderr[:200]}"
            frame = f.with_suffix(".jpg")
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(pr["duration"] / 2), "-i", str(f), "-frames:v", "1",
                            "-q:v", "3", str(frame)], check=True)
            srt = c.get(f"/clips/{cid}/captions.srt").text
            ass = (data / "clips").rglob("captions.ass")
            report["clips"].append({"id": cid, "source_time": [round(x["start"] + START, 1), round(x["end"] + START, 1)],
                                    "duration": pr["duration"], "size_mb": pr["size_mb"], "mean_volume_db": vol,
                                    "caption_cues": srt.count("-->"), "opening": x["transcript_text"][:140],
                                    "closing": x["transcript_text"][-110:], "file": f.name, "frame": frame.name})
            assert srt.count("-->") >= 2, f"clip {cid} has no captions"
        styles = [p.read_text(encoding="utf-8") for p in (data / "clips").rglob("captions.ass")]
        # owner-approved style: Anton 95 px (73 px + 30%, 17 Nov 2026), no burned-in source label
        assert styles and all("Style: Caption,Anton,95," in s_ for s_ in styles), "captions are not in the approved Anton style"
        assert not any("Dialogue: 1," in s_ for s_ in styles), "a source label was burned into a clip"
        ok["6_all_clips_rendered"] = "PASS"
        ok["7_captions_anton_95px_no_label"] = "PASS"
        ok["8_valid_1080x1920_h264_aac_with_audio"] = "PASS"
        for k in report["clips"]:
            log(f"clip #{k['id']}: {k['duration']}s  “{k['opening'][:80]}…”")

        # 9–10. review, preview, download
        review = c.get("/review").text
        assert all(f'src="/clips/{x["id"]}/video' in review for x in clips), "clips missing from Ready to Review"
        cid = clips[0]["id"]
        assert c.get(f"/clips/{cid}/video", headers={"Range": "bytes=0-1023"}).status_code == 206
        d = c.get(f"/clips/{cid}/download")
        assert d.status_code == 200 and "attachment" in d.headers.get("content-disposition", "")
        ok["9_ready_to_review_lists_clips"] = "PASS"
        ok["10_preview_and_download"] = "PASS"
        c.post(f"/clips/{cid}/approve", data={"note": "genuine-recording test"}, follow_redirects=False)

        # 3. the same file again → no duplicate recording, no new job
        jobs_before = len(c.get(f"/api/recordings/{rid}").json()["jobs"])
        shutil.copy(video, inbox / "same briefing again.mp4")
        os.utime(inbox / "same briefing again.mp4", (old, old))
        wait_until(lambda: (inbox / "duplicates" / "same briefing again.mp4").exists(), 120, "duplicate handling")
        assert c.get("/api/recordings/2").status_code == 404, "a duplicate recording was created"
        assert len(c.get(f"/api/recordings/{rid}").json()["jobs"]) == jobs_before
        ok["3_no_duplicate_recording_or_job"] = "PASS (file moved to inbox/duplicates, not deleted)"
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                b = p.chromium.launch(executable_path=os.environ.get("PW_CHROMIUM") or None)
                pg = b.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True)
                pg.goto(base + "/review")
                pg.wait_for_timeout(1500)
                pg.screenshot(path=str(keep / "review_phone.png"))
                b.close()
        except Exception as e:  # noqa: BLE001 - screenshot is a convenience only
            log(f"phone screenshot skipped ({type(e).__name__})")
    finally:
        stop_server(proc)

    # 11. restart
    proc, base = start_server(env, args.port, logfile)
    try:
        st2 = httpx.get(f"{base}/api/recordings/{rid}", timeout=30).json()
        assert st2["transcript_segments"] == st["transcript_segments"]
        status = {x["id"]: x["status"] for x in st2["clips"]}
        assert status[cid] == "approved" and len(status) == len(clips), status
        assert httpx.get(f"{base}/clips/{cid}/download", timeout=60).status_code == 200
        assert "2021-08-31" in httpx.get(f"{base}/recordings/{rid}", timeout=30).text
        ok["11_survives_restart"] = "PASS"
    finally:
        stop_server(proc)
        logfile.close()
    report["platform"] = f"{sys.platform} / Python {sys.version.split()[0]}"
    (keep / "report.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    log("RESULTS (REAL faster-whisper, genuine recording):")
    for k in sorted(ok, key=lambda s_: int(s_.split("_")[0])):
        log(f"  {k:44s} {ok[k]}")
    shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
