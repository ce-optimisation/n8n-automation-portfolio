#!/usr/bin/env bash
# macOS/Linux launcher (the Windows launcher is Start-Clipping-Engine.cmd).
set -euo pipefail
cd "$(dirname "$0")"
command -v ffmpeg >/dev/null || { echo "Install FFmpeg first (e.g. brew install ffmpeg / apt install ffmpeg)"; exit 1; }
PY=${PYTHON:-python3}
[ -x .venv/bin/python ] || "$PY" -m venv .venv
.venv/bin/python scripts/ensure_deps.py
[ -f .env ] || cp .env.example .env
exec .venv/bin/python -m clipengine "$@"
