"""Build the external clipping-service portfolio as a static site for Cloudflare Pages (free plan).

Input is ONLY the cleared export manifest (/showcase/manifest.json): favourites with written promotional-use
evidence that the owner selected. Each clip is re-encoded to a small web MP4 (Pages caps files at 25 MiB; we aim
for <= 10 MB), given a poster frame and a SHA-256, and listed on a mobile-friendly page. The output folder is
rebuilt from scratch every time, so a clip removed from the manifest disappears on the next deploy.

Deployment (owner-authenticated once):  npx wrangler pages deploy <out> --project-name <name>
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import html
import json
from pathlib import Path
import re
import shutil
import subprocess

MARKER = ".clipping-portfolio-build"
MAX_BYTES = 10 * 1024 * 1024
PAGES_LIMIT = 25 * 1024 * 1024
LEAK_PATTERNS = [re.compile(p, re.I) for p in (r"[A-Z]:\\\\?Users", r"AppData", r"\.sqlite", r"secrets?[/\\]",
                                                r"(?:api[_-]?key|access_token|refresh_token|client_secret)\s*[:=]",
                                                r"127\.0\.0\.1", r"ts\.net")]


def slug(text: str, n: int) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "clip").lower()).strip("-")[:48] or "clip"
    return f"{n:02d}-{s}"


def _ffmpeg(*args: str) -> None:
    subprocess.run([shutil.which("ffmpeg") or "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
                   check=True, capture_output=True, timeout=900)


def encode(src: Path, dst: Path) -> None:
    """720x1280 H.264/AAC, faststart; retries with a lower bitrate if over MAX_BYTES."""
    for vbit in ("1600k", "1000k", "650k"):
        _ffmpeg("-i", str(src), "-vf", "scale=720:1280:force_original_aspect_ratio=decrease,pad=720:1280:(ow-iw)/2:(oh-ih)/2",
                "-c:v", "libx264", "-preset", "medium", "-b:v", vbit, "-maxrate", vbit, "-bufsize", "2M",
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(dst))
        if dst.stat().st_size <= MAX_BYTES:
            return
    raise ValueError(f"{src.name} could not be encoded under {MAX_BYTES // 1_048_576} MB; trim the clip.")


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


PAGE = """<!doctype html><html lang="en-GB"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{name} — short-form clipping</title>
<meta name="description" content="{tagline}"><meta name="robots" content="{robots}">
<style>:root{{--ink:#15171b;--muted:#5d6370;--line:#e4e6eb;--bg:#fbfbfc;--card:#fff;--accent:#d8402b}}
@media(prefers-color-scheme:dark){{:root{{--ink:#eceef2;--muted:#a3a9b6;--line:#2a2e36;--bg:#111317;--card:#181b21}}}}
*{{box-sizing:border-box}}body{{margin:0;font:16px/1.6 system-ui,-apple-system,"Segoe UI",sans-serif;color:var(--ink);background:var(--bg)}}
.wrap{{max-width:1100px;margin:0 auto;padding:0 20px}}header{{padding:40px 0 16px}}h1{{font-size:clamp(30px,5vw,46px);letter-spacing:-.02em;margin:0}}
.lead{{color:var(--muted);font-size:18px;max-width:62ch}}.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:18px;padding:16px 0 40px}}
figure{{margin:0;background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden}}
video{{width:100%;aspect-ratio:9/16;display:block;background:#000}}figcaption{{padding:12px 14px}}figcaption b{{display:block}}
.tag{{font-size:13px;color:var(--muted)}}.note{{font-size:14px;color:var(--muted);border-top:1px solid var(--line);padding:20px 0 40px}}
a{{color:var(--accent)}}nav.filters a{{margin-right:12px;font-size:14px}}</style></head><body><div class="wrap">
<header><h1>{name}</h1><p class="lead">{tagline}</p><p><a href="mailto:{email}">{email}</a></p>{filters}</header>
<main class="grid">{cards}</main>
<p class="note">These are examples of our editing. Unless a clip says otherwise, the creators shown have not hired or endorsed us; footage appears with the rights holder's permission for portfolio use. <a href="privacy.html">Privacy</a> · Updated {updated}</p>
</div></body></html>"""

PRIVACY = """<!doctype html><html lang="en-GB"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Privacy — {name}</title></head><body style="font:16px/1.6 system-ui;max-width:720px;margin:40px auto;padding:0 20px">
<h1>Privacy</h1><p>{name} is operated by {owner}, a sole trader in the United Kingdom. This page is a static portfolio: it sets no cookies,
runs no analytics and collects no personal data. If you email us, we use your message only to reply and keep it for up to 24 months.
You can ask us to delete it at {email}. Hosting is provided by Cloudflare, which processes standard request logs.</p>
<p><a href="./">Back to the portfolio</a></p></body></html>"""


def build(manifest: dict, identity: dict, out: Path, *, robots: str = "noindex") -> dict:
    out = Path(out)
    if out.exists():
        if not (out / MARKER).exists():
            raise ValueError(f"{out} exists and was not created by this builder; refusing to overwrite.")
        shutil.rmtree(out)
    (out / "media").mkdir(parents=True)
    (out / MARKER).write_text("generated", encoding="utf-8")
    items = sorted(manifest.get("items", []), key=lambda i: (not i.get("featured"), i.get("order", 0)))
    published, cards, collections = [], [], set()
    for n, item in enumerate(items, start=1):
        src = Path(item["video_path"]) if item.get("video_path") else None
        if not src or not src.is_file():
            raise ValueError(f"Clip {item['clip_id']} has no local media; recover it from Drive in Showcase first.")
        name = slug(item["title"], n)
        mp4, jpg = out / "media" / f"{name}.mp4", out / "media" / f"{name}.jpg"
        encode(src, mp4)
        _ffmpeg("-ss", "1", "-i", str(mp4), "-frames:v", "1", "-vf", "scale=360:-2", str(jpg))
        col = (item.get("collection") or "").strip()
        if col:
            collections.add(col)
        published.append({"clip_id": item["clip_id"], "file": f"media/{name}.mp4", "sha256": _sha(mp4),
                          "bytes": mp4.stat().st_size, "collection": col or None,
                          "rights_evidence": item.get("rights_evidence")})
        label = " · ".join(x for x in (item.get("brand"), col.replace("-", " ") if col else None) if x)
        cards.append(f'<figure data-collection="{html.escape(col)}"><video src="media/{name}.mp4" poster="media/{name}.jpg" '
                     f'controls preload="none" playsinline></video><figcaption><b>{html.escape(item["title"])}</b>'
                     f'<span class="tag">{html.escape(label)}</span></figcaption></figure>')
    filters = ""
    if collections:
        filters = '<nav class="filters">' + "".join(
            f'<a href="#" data-c="{html.escape(c)}">{html.escape(c.replace("-", " "))}</a>' for c in sorted(collections)) + \
            '<a href="#" data-c="">all</a></nav><script>document.querySelectorAll("nav.filters a").forEach(a=>a.onclick=e=>{' \
            'e.preventDefault();const c=a.dataset.c;document.querySelectorAll("figure").forEach(f=>f.style.display=' \
            '!c||f.dataset.collection===c?"":"none")})</script>'
    esc = {k: html.escape(str(v)) for k, v in identity.items()}
    updated = datetime.now(timezone.utc).strftime("%d %B %Y")
    (out / "index.html").write_text(PAGE.format(**esc, robots=robots, filters=filters,
                                                cards="".join(cards) or "<p>Examples coming soon.</p>", updated=updated),
                                    encoding="utf-8")
    (out / "privacy.html").write_text(PRIVACY.format(**esc), encoding="utf-8")
    (out / "_headers").write_text("/*\n  X-Content-Type-Options: nosniff\n  Referrer-Policy: no-referrer\n"
                                  "  X-Frame-Options: DENY\n  Permissions-Policy: camera=(), microphone=(), geolocation=()\n",
                                  encoding="utf-8")
    release = {"built_at": datetime.now(timezone.utc).isoformat(), "items": published}
    (out / "release.json").write_text(json.dumps({"built_at": release["built_at"],
                                                  "items": [{k: v for k, v in i.items() if k != "rights_evidence"}
                                                            for i in published]}, indent=2), encoding="utf-8")
    report = verify(out)
    return {**release, "verification": report}


def verify(out: Path) -> dict:
    """Every local link resolves, every file is within the Pages limit, nothing private leaks."""
    problems = []
    for page in out.glob("*.html"):
        text = page.read_text(encoding="utf-8")
        for ref in re.findall(r'(?:src|href|poster)="([^"#:]+)"', text):
            if not ref.startswith(("mailto", "http")) and ref not in ("./",) and not (out / ref).exists():
                problems.append(f"{page.name}: missing {ref}")
    for f in out.rglob("*"):
        if f.is_file():
            if f.stat().st_size > PAGES_LIMIT:
                problems.append(f"{f.name} exceeds the 25 MiB Pages limit")
            if f.suffix in (".html", ".json", ".txt", "") or f.name == "_headers":
                text = f.read_text(encoding="utf-8", errors="replace")
                for p in LEAK_PATTERNS:
                    if p.search(text):
                        problems.append(f"{f.name}: private-looking content matches {p.pattern}")
    return {"ok": not problems, "problems": problems,
            "files": sum(1 for f in out.rglob("*") if f.is_file()),
            "bytes": sum(f.stat().st_size for f in out.rglob("*") if f.is_file())}


def main() -> int:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("manifest", type=Path, help="Export from /showcase/manifest.json")
    cli.add_argument("identity", type=Path, help='JSON: {"name","tagline","email","owner"}')
    cli.add_argument("out", type=Path)
    cli.add_argument("--indexable", action="store_true", help="Allow search engines (default noindex)")
    a = cli.parse_args()
    result = build(json.loads(a.manifest.read_text(encoding="utf-8")), json.loads(a.identity.read_text(encoding="utf-8")),
                   a.out, robots="index,follow" if a.indexable else "noindex")
    print(json.dumps(result["verification"], indent=2))
    return 0 if result["verification"]["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
