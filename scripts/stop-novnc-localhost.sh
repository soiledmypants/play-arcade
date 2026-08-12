#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PID_FILE="$ROOT/logs/novnc-6088.pid"
HTTP_PORT="${PLAY_NOVNC_PORT:-6088}"

if [[ -f "$PID_FILE" ]]; then
  pid="$(cat "$PID_FILE")"
  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid" || true
    echo "stopped pid=$pid"
  else
    echo "stale pid file ($pid)"
  fi
  rm -f "$PID_FILE"
else
  # best-effort match our localhost binder only
  pids=$(ss -tlnp 2>/dev/null | rg "127\\.0\\.0\\.1:${HTTP_PORT}\\b" | rg -o 'pid=[0-9]+' | cut -d= -f2 | sort -u || true)
  if [[ -n "${pids:-}" ]]; then
    kill $pids || true
    echo "stopped pids: $pids"
  else
    echo "not running"
  fi
fi
