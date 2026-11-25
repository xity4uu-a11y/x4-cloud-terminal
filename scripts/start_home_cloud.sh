#!/usr/bin/env bash
# Zero-Cost 24/7 Self-Hosted Launcher for Linux / WSL2 / Raspberry Pi / Spare Laptop
set -euo pipefail

cd "$(dirname "$0")/.."
PORT="${PORT:-8501}"
WITH_TUNNEL="${1:-}"

if [ ! -f ".env" ]; then
  cp .env.example .env
  echo "[INFO] Created .env from .env.example — set X4_API_TOKEN and X4_BRIDGE_SECRET before public exposure!"
fi

if [ "${WITH_TUNNEL}" = "--tunnel" ]; then
  if command -v cloudflared >/dev/null 2>&1; then
    echo "[INFO] Starting free Cloudflare HTTPS Quick Tunnel -> http://127.0.0.1:${PORT}"
    cloudflared tunnel --url "http://127.0.0.1:${PORT}" &
  else
    echo "[WARN] cloudflared not installed. Get it free from https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/"
  fi
fi

echo "[INFO] Running X4 Cloud Terminal watchdog on 0.0.0.0:${PORT}..."
while true; do
  python3 run.py --host 0.0.0.0 --port "${PORT}" || true
  echo "[WARN] Process stopped; restarting in 3s..."
  sleep 3
done
