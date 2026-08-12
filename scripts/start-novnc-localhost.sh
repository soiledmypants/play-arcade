#!/usr/bin/env bash
# Localhost-only noVNC bridge for THIS agent's desktop (DISPLAY :8 -> VNC 5908).
# Does NOT bind 0.0.0.0. Play-site should reverse-proxy / iframe later.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="$ROOT/logs"
PID_FILE="$LOG_DIR/novnc-6088.pid"
DISPLAY_NUM="${PLAY_DISPLAY_NUM:-8}"
VNC_PORT="${PLAY_VNC_PORT:-$((5900 + DISPLAY_NUM))}"
HTTP_PORT="${PLAY_NOVNC_PORT:-6088}"
BIND="${PLAY_NOVNC_BIND:-127.0.0.1}"
WEB_ROOT="${PLAY_NOVNC_WEB:-/usr/share/novnc}"

mkdir -p "$LOG_DIR"

if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "already running pid=$(cat "$PID_FILE") on ${BIND}:${HTTP_PORT}"
  exit 0
fi

# Prefer the platform x11vnc already bound to this display; do not start a second one.
if ! ss -tln | rg -q "127\\.0\\.0\\.1:${VNC_PORT}\\b"; then
  echo "ERROR: no x11vnc listening on 127.0.0.1:${VNC_PORT} for DISPLAY :${DISPLAY_NUM}" >&2
  echo "Expect platform helper: x11vnc -display :${DISPLAY_NUM} -localhost -rfbport ${VNC_PORT}" >&2
  exit 1
fi

nohup websockify \
  --web="$WEB_ROOT" \
  --heartbeat=30 \
  "${BIND}:${HTTP_PORT}" \
  "localhost:${VNC_PORT}" \
  >"$LOG_DIR/novnc-${HTTP_PORT}.log" 2>&1 &
echo $! >"$PID_FILE"
sleep 0.3
if ! kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "failed to start; see $LOG_DIR/novnc-${HTTP_PORT}.log" >&2
  exit 1
fi

echo "noVNC ready: http://${BIND}:${HTTP_PORT}/vnc.html?autoconnect=1&resize=scale"
echo "view-only:   http://${BIND}:${HTTP_PORT}/vnc.html?autoconnect=1&view_only=1&resize=scale"
echo "vnc target:  localhost:${VNC_PORT} (DISPLAY :${DISPLAY_NUM})"
echo "pid:         $(cat "$PID_FILE")"
