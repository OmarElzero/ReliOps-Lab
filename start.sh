#!/usr/bin/env bash
# One command to start ReliOps Lab: creates the virtualenv on first use, installs requirements,
# starts the server and opens the browser. Run it from anywhere:  bash start.sh
set -euo pipefail
cd "$(dirname "$0")"
PORT="${PORT:-8765}"

if [ ! -d .venv ]; then
  echo "[first run] creating the virtualenv and installing requirements..."
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi

URL="http://localhost:${PORT}"
open_browser() {
  sleep 2
  if [ "${LAB_NO_BROWSER:-}" = "1" ]; then return; fi
  if command -v wslview >/dev/null 2>&1; then wslview "$URL" >/dev/null 2>&1
  elif command -v explorer.exe >/dev/null 2>&1; then explorer.exe "$URL" >/dev/null 2>&1 || true
  elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$URL" >/dev/null 2>&1
  elif command -v open >/dev/null 2>&1; then open "$URL"
  else echo "open $URL in your browser"; fi
}
open_browser &
echo "ReliOps Lab: $URL   (Ctrl+C to stop)"
cd backend && exec ../.venv/bin/uvicorn app:app --host 127.0.0.1 --port "$PORT"
