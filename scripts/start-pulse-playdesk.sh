#!/usr/bin/env bash
# Start a headless PulseAudio daemon with a null sink for DISPLAY :8 Chrome audio.
# No /dev/snd required. Socket lands in the same XDG_RUNTIME_DIR box-chrome uses.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="$ROOT/logs"
mkdir -p "$LOG_DIR"

DISPLAY_NUM="${PLAY_DISPLAY_NUM:-8}"
XDG_RUNTIME_DIR="${PLAY_PULSE_XDG:-/tmp/xdg-runtime-box-${DISPLAY_NUM}}"
SINK_NAME="${PLAY_PULSE_SINK:-playdesk}"
PID_FILE="$LOG_DIR/pulseaudio.pid"
LOG_FILE="$LOG_DIR/pulseaudio.log"
export XDG_RUNTIME_DIR HOME="${HOME:-/home/box}"

mkdir -p "$XDG_RUNTIME_DIR/pulse"
chmod 700 "$XDG_RUNTIME_DIR" 2>/dev/null || true

SOCKET="$XDG_RUNTIME_DIR/pulse/native"

if [[ -S "$SOCKET" ]] && PULSE_SERVER="unix:$SOCKET" pactl info >/dev/null 2>&1; then
  # Ensure our sink exists even if pulse was started earlier.
  if ! PULSE_SERVER="unix:$SOCKET" pactl list short sinks 2>/dev/null | rg -q "^[0-9]+\s+${SINK_NAME}\b"; then
    PULSE_SERVER="unix:$SOCKET" pactl load-module module-null-sink \
      sink_name="$SINK_NAME" \
      sink_properties=device.description=PlayDesk >/dev/null
  fi
  PULSE_SERVER="unix:$SOCKET" pactl set-default-sink "$SINK_NAME" >/dev/null || true
  echo "pulse already up socket=$SOCKET sink=$SINK_NAME"
  PULSE_SERVER="unix:$SOCKET" pactl info | rg -i 'server string|default sink|server name' || true
  exit 0
fi

# Fresh daemon: native unix + null sink only (no ALSA/udev).
# exit-idle-time=-1 keeps it alive with no clients.
pulseaudio -n --daemonize=yes --exit-idle-time=-1 \
  --log-target="file:${LOG_FILE}" \
  --load="module-native-protocol-unix auth-anonymous=1" \
  --load="module-null-sink sink_name=${SINK_NAME} sink_properties=device.description=PlayDesk" \
  --load="module-always-sink" \
  || {
    echo "pulseaudio failed to start; see $LOG_FILE" >&2
    exit 1
  }

# Resolve daemon pid for the pid file (best effort).
sleep 0.2
PA_PID="$(pgrep -u "$(id -u)" -f "pulseaudio -n --daemonize" | head -1 || true)"
if [[ -n "${PA_PID:-}" ]]; then
  echo "$PA_PID" >"$PID_FILE"
fi

export PULSE_SERVER="unix:$SOCKET"
for _ in 1 2 3 4 5 6 7 8 9 10; do
  if pactl info >/dev/null 2>&1; then
    break
  fi
  sleep 0.15
done

pactl set-default-sink "$SINK_NAME" >/dev/null || true
pactl set-sink-mute "$SINK_NAME" 0 >/dev/null || true
pactl set-sink-volume "$SINK_NAME" 100% >/dev/null || true

echo "pulse ready socket=$SOCKET sink=$SINK_NAME pid=${PA_PID:-unknown}"
echo "PULSE_SERVER=$PULSE_SERVER"
pactl list short sinks || true
pactl info | rg -i 'server string|default sink|server name' || true
