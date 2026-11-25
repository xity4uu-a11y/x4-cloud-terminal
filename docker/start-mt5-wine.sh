#!/usr/bin/env bash
set -euo pipefail

export DISPLAY=:99
export WINEPREFIX="${WINEPREFIX:-/mt5/.wine}"
export WINEDEBUG="-all"

echo "[mt5-wine] Starting virtual display Xvfb ${DISPLAY} (1280x800x24)..."
Xvfb "${DISPLAY}" -screen 0 1280x800x24 -ac +extension GLX +render -noreset &
sleep 2

echo "[mt5-wine] Starting Fluxbox window manager..."
fluxbox &
sleep 1

echo "[mt5-wine] Starting x11vnc + noVNC web desktop on port ${NOVNC_PORT:-6080}..."
if [ -n "${VNC_PASSWORD:-}" ]; then
  mkdir -p /root/.vnc
  x11vnc -storepasswd "${VNC_PASSWORD}" /root/.vnc/passwd
  x11vnc -display "${DISPLAY}" -forever -shared -rfbauth /root/.vnc/passwd -rfbport "${VNC_PORT:-5900}" -bg
else
  echo "[mt5-wine] WARNING: VNC_PASSWORD not set! Bind port 6080 to 127.0.0.1 only."
  x11vnc -display "${DISPLAY}" -forever -shared -nopw -rfbport "${VNC_PORT:-5900}" -bg
fi

websockify --web=/usr/share/novnc/ "${NOVNC_PORT:-6080}" "127.0.0.1:${VNC_PORT:-5900}" &

MT5_EXE="${WINEPREFIX}/drive_c/Program Files/MetaTrader 5/terminal64.exe"
if [ ! -f "${MT5_EXE}" ]; then
  echo "[mt5-wine] MetaTrader 5 not found yet. Downloading official MetaQuotes installer..."
  wget -qO /mt5/installers/mt5setup.exe "https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe"
  echo "[mt5-wine] Open http://127.0.0.1:${NOVNC_PORT:-6080}/vnc.html to complete MT5 login & attach /mt5/experts/X4_CloudBridge_EA.mq5"
  wine /mt5/installers/mt5setup.exe &
else
  # Sync latest X4_CloudBridge_EA.mq5 into MQL5/Experts automatically
  EXPERTS_DIR="${WINEPREFIX}/drive_c/Program Files/MetaTrader 5/MQL5/Experts"
  mkdir -p "${EXPERTS_DIR}"
  cp -f /mt5/experts/X4_CloudBridge_EA.mq5 "${EXPERTS_DIR}/X4_CloudBridge_EA.mq5" || true
  echo "[mt5-wine] Launching MetaTrader 5 terminal64.exe..."
  wine "${MT5_EXE}" /portable &
fi

wait -n
