#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DISPLAY_NUM="${PLAY_DISPLAY_NUM:-8}"
XDG_RUNTIME_DIR="${PLAY_PULSE_XDG:-/tmp/xdg-runtime-box-${DISPLAY_NUM}}"
export XDG_RUNTIME_DIR HOME="${HOME:-/home/box}"
PULSE_SERVER="unix:$XDG_RUNTIME_DIR/pulse/native" pulseaudio -k 2>/dev/null || true
rm -f "$ROOT/logs/pulseaudio.pid"
echo "pulse stopped (best effort)"
