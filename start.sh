#!/usr/bin/env bash
# One command to start ReliOps Lab: creates the virtualenv on first use, installs requirements,
# starts the server and opens the browser. Run it from anywhere:  bash start.sh
set -euo pipefail
cd "$(dirname "$0")"
PORT="${PORT:-8765}"

# A half-made virtualenv (for example one interrupted earlier, with no bin/activate) is rebuilt.
if [ ! -f .venv/bin/activate ]; then
  echo "[setup] creating the virtualenv..."
  rm -rf .venv
  if command -v uv >/dev/null 2>&1; then
    uv venv -q .venv
  else
    python3 -m venv .venv || { echo "python3 -m venv failed. On Ubuntu or WSL run: sudo apt install python3-venv"; exit 1; }
  fi
fi
if [ ! -x .venv/bin/uvicorn ]; then
  echo "[setup] installing requirements..."
  if command -v uv >/dev/null 2>&1; then
    uv pip install -q --python .venv/bin/python -r requirements.txt
  else
    .venv/bin/python -m pip install -q -r requirements.txt
  fi
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
