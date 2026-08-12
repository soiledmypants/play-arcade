# STREAM.md - browser remote desktop for play-site arcade

Date: 2026-08-12 (America/New_York) - audio side channel via WebSocket PCM
Agent desktop under test: DISPLAY=:8 (1280x800 Xvfb)

## Inventory (what is already on this box)

| Component | Status | Notes |
|---|---|---|
| X11 / Wayland | X11 via Xvfb | Displays :1-:9 present; this agent is :8. No Wayland. |
| x11vnc | Installed + running | Per-display, localhost only. :8 -> 127.0.0.1:5908 (-nopw -shared -forever). |
| TigerVNC / TightVNC | Not installed | Not needed. |
| noVNC | Installed (novnc 1.6.0) | /usr/share/novnc (vnc.html, vnc_lite.html). |
| websockify | Installed + running | Platform :6080 for :1; token mux :6081 via /tmp/sand-novnc-tokens.d (8 -> localhost:5908). |
| ffmpeg | Installed | HLS fallback possible; worse interactive UX. |
| pulseaudio | Installed + running (user) | Null sink `playdesk` on XDG for :8; side-channel PCM via WebSocket `/audio/ws`. |
| Google Chrome | Installed + running on :8 | Full internet browsing works in-session. |
| Selkies-GStreamer | Not installed | Heavier than needed for today. |
| gstreamer CLI | Missing | Blocks easy Selkies path without apt. |

Platform helpers already wire each forked desktop: box-xvfb + box-x11vnc + token registration.

## Ranked options (practicality for today)

1. Chosen: noVNC + existing x11vnc - already installed; localhost VNC exists; one websockify gets a browser stream in minutes.
2. Selkies-GStreamer - better WebRTC for many watchers later; not installed; overkill for prototype.
3. ffmpeg HLS + custom input injection - high latency; worse arcade UX.

## Chosen approach (prototype)

noVNC over websockify -> localhost x11vnc on this agent display.

- Video + input: RFB through noVNC in the browser.
- Watchers: noVNC with view_only=1 (client-side; see security caveats).
- Controller (now-playing): same stream without view_only, or ticketed control from play-site.
- Session browsing: Chrome already on DISPLAY=:8.

### Localhost stream status

Running now (loopback only):

- URL: http://127.0.0.1:6088/vnc.html?autoconnect=1&resize=scale
- View-only: http://127.0.0.1:6088/vnc.html?autoconnect=1&view_only=1&resize=scale
- Lite UI: http://127.0.0.1:6088/vnc_lite.html?autoconnect=1
- Upstream VNC: localhost:5908 (DISPLAY=:8)
- WebSocket smoke test: HTTP/1.1 101 Switching Protocols
- Log/pid: logs/novnc-6088.log , logs/novnc-6088.pid

Also available via platform token mux on port 6081 (token 8 -> localhost:5908). Prefer 127.0.0.1:6088 for play-site until an authenticated proxy exists.

## Commands

```bash
# start (idempotent; localhost only)
/workspace/play-site/scripts/start-novnc-localhost.sh

# stop
/workspace/play-site/scripts/stop-novnc-localhost.sh

# override ports/display if needed
PLAY_DISPLAY_NUM=8 PLAY_NOVNC_PORT=6088 PLAY_NOVNC_BIND=127.0.0.1 \
  /workspace/play-site/scripts/start-novnc-localhost.sh

# play-site itself
cd /workspace/play-site && python3 server.py   # :8787
# optional: STREAM_UPSTREAM=127.0.0.1:6088
```

Do not start a second x11vnc on :8 unless the platform one dies; the start script checks 127.0.0.1:5908.

## Security notes (must-read)

1. Never expose raw VNC unbound on all interfaces without auth. Platform x11vnc correctly uses -localhost. Keep it that way.
2. Prototype websockify binds 127.0.0.1:6088 only. Safe for local iframe/proxy experiments. Do not rebind to all interfaces for a public arcade.
3. Platform websockify already listens publicly on :6080 and :6081. Tokens for fork displays are low-entropy (display number). Treat as an infra risk if the box is network-reachable; play-site must not rely on that as public auth.
4. x11vnc runs -nopw -shared. Any RFB client that reaches it can inject input. noVNC view_only=1 is NOT a server-side security boundary - a crafted websocket client can still send pointer/key events. For production arcade:
   - Prefer a control gate in the proxy (strip input unless seat token matches now-playing wallet), or
   - Run a view-only VNC (x11vnc -viewonly) for spectators and a separate control channel toggled when the seat is claimed, or
   - Toggle x11vnc input policy from the queue API on seat grant/revoke.
5. One controller at a time: enforce in play-site (only now-playing wallet receives a short-lived control token). Watchers get view URL only.
6. Do not leave an open unauthenticated VNC/noVNC on the public internet. Put TLS + auth + seat checks in front (play-site reverse proxy or edge).

## Integration status (play-site UI) - DONE 2026-08-11

Embedded noVNC in `#stream-screen` via same-origin reverse proxy.

| Piece | Status |
|---|---|
| Same-origin proxy `/stream/` → `127.0.0.1:6088` | Done (HTTP + WebSocket upgrade in `server.py`) |
| Default iframe = view_only | Done (`WATCHING` badge) |
| nowPlaying wallet → control URL | Done (`CONTROLLING` badge; src swap only on mode flip) |
| Session end / wallet mismatch → view_only | Done (poll `/api/state` + wallet change handlers) |
| VNC/noVNC still loopback-only | Done (upstream remains `127.0.0.1:6088`) |
| UI note about public proxied WSS | Done (badge row + `config.stream.note`) |

### How URLs resolve

- Embed (preferred): `http://127.0.0.1:8787/stream/vnc.html?autoconnect=1&resize=scale&view_only=1&path=stream/`
- Control embed: same without `view_only=1`
- Direct localhost (also exposed in `/api/state` config): `http://127.0.0.1:6088/vnc.html?...`
- WebSocket through proxy: `ws://<play-site>/stream/` (noVNC `path=stream/`)

`config.stream` on `/api/state` and `/api/health` includes `viewPath`, `controlPath`, `viewUrl`, `controlUrl`, `upstream`, `note`.

### How to open and test

```bash
# ensure bridges
/workspace/play-site/scripts/start-novnc-localhost.sh
cd /workspace/play-site && python3 server.py   # :8787

# smoke
curl -sI http://127.0.0.1:8787/ | head -1          # expect 200
curl -sI http://127.0.0.1:8787/stream/vnc.html | head -1
curl -s http://127.0.0.1:8787/api/state | python3 -m json.tool | head

# browser
open http://127.0.0.1:8787/
# 1) page loads stream in WATCHING (view_only)
# 2) Connect wallet (or demo wallet) matching nowPlaying → badge CONTROLLING, iframe reloads once without view_only
# 3) Leave seat / wait for expiry / switch wallet → back to WATCHING (one reload)
```

### Control vs view switching

Works client-side: frontend compares connected `wallet` to `nowPlaying.wallet` on each `/api/state` poll (2.5s) and on wallet connect/change. Iframe `src` changes only when mode flips between `view` and `control`.

Security caveat unchanged: `view_only=1` is **not** a server-side gate. Production still needs proxy input filtering or dual VNC (viewonly spectator + control seat).

### Still TODO for production

1. Authenticated short-lived control tickets (`POST /api/stream/ticket`) enforced in the proxy.
2. Public TLS termination + WSS on `/stream/` (do not bind websockify to `0.0.0.0`).
3. Optional: reset Chrome/session on seat promote/expire.
4. Multi-watcher scale → Selkies/WebRTC if RFB fan-out hurts.

## Fallback plan

No new packages were required for this prototype.

If localhost bridge failed:
1. Use platform token mux on 6081 with token 8 (exposure risk).
2. Use python3 -m websockify if the binary wrapper is missing.
3. Vendor noVNC under play-site/public/novnc/ if needed.
4. Last resort: ffmpeg capture of display :8 to HLS under public/live/ (view-only).

## Blockers encountered

- Broad UI-binary inventory shell probe auto-blocked; narrower inventory succeeded.
- Selkies / TigerVNC / gst-launch absent; skipped.
- No other blockers: stack already present and usable.

## Paths

| Path | Purpose |
|---|---|
| /workspace/play-site/STREAM.md | This document |
| /workspace/play-site/scripts/start-novnc-localhost.sh | Start localhost noVNC |
| /workspace/play-site/scripts/stop-novnc-localhost.sh | Stop it |
| /workspace/play-site/logs/novnc-6088.log | bridge log |
| /workspace/play-site/logs/novnc-6088.pid | pid file |
| /usr/share/novnc/ | noVNC static assets |
| /tmp/sand-novnc-tokens.d/8 | platform token -> localhost:5908 |

## Audio (side channel) - DONE 2026-08-12

noVNC still does not carry desktop sound. Audio is a **separate WebSocket PCM side channel**
on the play-site API origin (same place as `/stream/`), so Netlify can play it via
`PLAY_API_BASE`.

Why not progressive HTTP MP3: Cloudflare quick tunnels return HTTP 200 + `audio/mpeg`
headers for infinite MP3 bodies but deliver **0 body bytes** (buffering). WebSocket
binary frames pass through the same way noVNC does.

| Piece | Detail |
|---|---|
| Pulse | User daemon, null sink `playdesk` (no `/dev/snd` needed) |
| Socket | `unix:/tmp/xdg-runtime-box-8/pulse/native` (`XDG_RUNTIME_DIR` for box-chrome on :8) |
| Client | `/home/box/.config/pulse/client.conf` sets `default-server` (autospawn off) |
| Capture | `ffmpeg -f pulse -i playdesk.monitor` -> raw PCM mono s16le @ 24kHz |
| URL | WebSocket `/audio/ws` (hello JSON, then binary PCM frames) |
| Debug HTTP | `/audio/stream.pcm` raw PCM for local curl only (not Cloudflare-safe) |
| Supervise | `server.py` AudioBridge spawns ffmpeg, fans out to client queues, restarts on exit |
| UI | `unmute` / `mute` under the stream (default muted; browsers block autoplay with sound) |

### Start / verify

```bash
/workspace/play-site/scripts/start-pulse-playdesk.sh
# optional relaunch so Chrome picks up the pulse socket via XDG_RUNTIME_DIR
DISPLAY=:8 /usr/local/bin/box-chrome --start-maximized https://www.google.com

cd /workspace/play-site && python3 server.py   # supervises ffmpeg

# local WS: hello + non-zero PCM energy
python3 - <<'PY'
import json, struct, time
from websocket import create_connection
ws = create_connection("ws://127.0.0.1:8787/audio/ws", timeout=5)
hello = json.loads(ws.recv())
assert hello.get("type") == "hello" and hello.get("format") == "s16le"
raw = b""
deadline = time.time() + 3
while time.time() < deadline and len(raw) < 8000:
    frame = ws.recv()
    if isinstance(frame, bytes):
        raw += frame
ws.close()
samples = struct.unpack("<" + "h" * (len(raw)//2), raw[:len(raw)//2*2])
energy = sum(abs(s) for s in samples) / max(1, len(samples))
print(hello, "bytes", len(raw), "energy", round(energy, 2))
PY

PULSE_SERVER=unix:/tmp/xdg-runtime-box-8/pulse/native pactl list short sinks
```

Logs: `logs/pulseaudio.log`, `logs/audio-ffmpeg.log`, `logs/pulseaudio.pid`.

### Residual limits

- Autoplay: browsers require a user gesture; the UI stays muted until **unmute**.
- Latency: PCM WS is typically under ~1s behind the noVNC video (not lip-sync).
- Tunnel: use `wss://.../audio/ws`. Do not rely on HTTP progressive MP3 through Cloudflare.
- Sync: video (RFB) and audio (PCM WS) are independent clocks; expect drift under load.
