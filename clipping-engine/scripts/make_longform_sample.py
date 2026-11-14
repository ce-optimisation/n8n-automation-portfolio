"""Generate a long, lawful, *labelled* test recording (e.g. 60–180 minutes).

All speech is original text produced here from templates and synthesised locally with
FFmpeg's built-in flite voice; the picture is FFmpeg's test pattern. Every paragraph is
labelled 'substantive' (with a topic) or 'procedural', which lets
scripts/evaluate_selection.py measure clip-selection quality reproducibly.

    python scripts/make_longform_sample.py --minutes 60 out.mp4
Writes out.mp4 and out.timing.json ({"paragraphs": [{start, end, text, label, topic}]}).
"""
from __future__ import annotations

import argparse
import json
import random
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

TOPICS = {
    "bridge": ("the river bridge", "two of the main supports have lost about thirty percent of their strength",
               "rebuild the bridge with a separate path for walking and cycling", "forty two million pounds"),
    "housing": ("affordable housing", "rents in the district rose by twelve percent last year",
                "build four hundred homes on the old depot site", "sixty million pounds"),
    "flooding": ("flood defences", "the east bank flooded three times in the last five years",
                 "raise the flood wall by one metre and restore the wetland upstream", "eighteen million pounds"),
    "schools": ("school places", "primary schools in the north ward are nine percent over capacity",
                "add two classrooms to each of the three largest schools", "eleven million pounds"),
    "buses": ("the bus network", "evening services were cut by a third in twenty twenty two",
              "restore the evening routes and add a night service on weekends", "three million pounds a year"),
    "libraries": ("the library service", "visits fell by a quarter but digital loans doubled",
                  "keep every branch open and extend opening hours on Saturdays", "nine hundred thousand pounds"),
    "waste": ("waste and recycling", "the recycling rate is stuck at forty one percent",
              "introduce weekly food waste collection for every household", "two million pounds a year"),
    "energy": ("energy costs", "the council spends six million pounds a year on electricity",
               "install solar panels on twenty public buildings", "seven million pounds"),
    "parks": ("parks and green spaces", "three playgrounds failed their safety inspection",
              "replace the equipment and plant five hundred trees", "one point two million pounds"),
    "clinics": ("community health clinics", "waiting times at the walk in clinic doubled this winter",
                "open a second clinic in the town centre with longer hours", "four million pounds"),
    "broadband": ("rural broadband", "one in five rural homes cannot get a reliable connection",
                  "work with providers to connect every village by twenty twenty eight", "five million pounds"),
    "policing": ("community safety", "reported burglaries rose by eight percent in the west ward",
                 "fund two extra neighbourhood officers and better street lighting", "seven hundred thousand pounds"),
}

SUBSTANTIVE = [
    "Let me turn to {name}. The evidence is clear: {fact}. That is the reason the proposal is on the agenda tonight.",
    "The plan is to {plan}. The estimated cost is {cost}, and roughly half of that would come from national funding.",
    "Why does this matter? Because {fact}, and if nothing changes the problem will get worse over the next decade.",
    "A resident asked me whether the plan for {name} has been properly costed. The answer is yes. The report sets out the full cost of {cost} and the assumptions behind it.",
    "There are reasonable objections to the plan for {name}. Some people argue the money should go elsewhere. The council should take that seriously, and the consultation will test that argument.",
    "On the question of {name}, the officers' report recommends that we {plan}. The final decision will be taken by a vote in March.",
    "Let me be clear about what is not yet decided on {name}. The design is not final, and the public consultation is open until the end of the month.",
    "Here is the key figure for {name}: {fact}. Independent research commissioned last year reached the same conclusion.",
]
CONTINUATION = [
    "And that is why the timetable matters so much.",
    "But the costs are only part of the picture.",
    "So the question is whether we act now or wait another year.",
]
PROCEDURAL = [
    "Good evening everyone, and welcome. Can everyone at the back hear me? Is the microphone on?",
    "Thank you. Thank you very much, councillor. Let's move on to the next item on the agenda.",
    "Before we continue, a little housekeeping. The fire exits are at the back, and please make sure your phones are on silent.",
    "We will take a short break of five minutes now. Thank you all.",
    "Okay, right, um, so, shall we begin again? Thank you for your patience.",
    "Apologies have been received from two members. The minutes of the last meeting were approved.",
    "Thank you. Is there anything else on this item? No? Then we move on.",
    "Um, sorry, can we just, uh, check the slides are working. Thank you.",
]


def build_script(minutes: float, seed: int = 7) -> list[dict]:
    rng = random.Random(seed)
    topics = list(TOPICS)
    rng.shuffle(topics)
    paras, est, t_idx = [], 0.0, 0
    paras.append({"text": PROCEDURAL[0], "label": "procedural", "topic": None})
    while est < minutes * 60:
        topic = topics[t_idx % len(topics)]
        t_idx += 1
        name, fact, plan, cost = TOPICS[topic]
        block = rng.sample(SUBSTANTIVE, k=rng.randint(3, 5))
        for tmpl in block:
            text = tmpl.format(name=name, fact=fact, plan=plan, cost=cost)
            if rng.random() < 0.25:
                text += " " + rng.choice(CONTINUATION)
            paras.append({"text": text, "label": "substantive", "topic": topic})
        for _ in range(rng.randint(1, 2)):
            paras.append({"text": rng.choice(PROCEDURAL), "label": "procedural", "topic": None})
        est = sum(len(p["text"].split()) for p in paras) / 2.95 + len(paras) * 1.0   # ~156 wpm + pauses
    return paras


def make(out: Path, minutes: float, seed: int = 7, size: str = "640x360", fps: int = 15) -> Path:
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg:
        raise SystemExit("ffmpeg not found")
    out.parent.mkdir(parents=True, exist_ok=True)
    paras = build_script(minutes, seed)
    rng = random.Random(seed + 1)
    t0 = time.time()
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        listing, t = [], 0.0
        sil_cache: dict[float, str] = {}
        for i, p in enumerate(paras):
            (td / f"p{i}.txt").write_text(p["text"], encoding="utf-8")
            wav = f"p{i}.wav"
            subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                            f"flite=textfile=p{i}.txt:voice=slt", "-ar", "16000", "-ac", "1", wav],
                           check=True, cwd=td)
            dur = float(subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration", "-of",
                                        "csv=p=0", wav], capture_output=True, text=True, cwd=td).stdout.strip())
            p.update(start=round(t, 3), end=round(t + dur, 3))
            gap = round(rng.choice([0.6, 0.8, 1.0, 1.4]), 1)
            if gap not in sil_cache:
                name = f"sil{int(gap * 10)}.wav"
                subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                                "anullsrc=r=16000:cl=mono", "-t", str(gap), name], check=True, cwd=td)
                sil_cache[gap] = name
            listing += [wav, sil_cache[gap]]
            t += dur + gap
        (td / "list.txt").write_text("".join(f"file '{x}'\n" for x in listing), encoding="utf-8")
        subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i",
                        "list.txt", "-c", "copy", "speech.wav"], check=True, cwd=td)
        subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                        f"testsrc2=size={size}:rate={fps}", "-i", "speech.wav", "-shortest", "-c:v", "libx264",
                        "-preset", "ultrafast", "-crf", "32", "-g", str(fps * 10), "-pix_fmt", "yuv420p",
                        "-c:a", "aac", "-b:a", "64k", str(out.resolve())], check=True, cwd=td)
    out.with_suffix(".timing.json").write_text(json.dumps({"paragraphs": paras, "seed": seed}, indent=1),
                                               encoding="utf-8")
    print(f"generated {t / 60:.1f} min in {time.time() - t0:.0f}s: {out}", file=sys.stderr)
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("out", type=Path)
    ap.add_argument("--minutes", type=float, default=60)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    print(make(a.out, a.minutes, a.seed))
