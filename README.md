# play-site

Free-play arcade: enter a guest display name, join a **FIFO** queue, play a short session when your turn comes. No wallet connect.

The live machine is the isolated **agent computer** on this box (`DISPLAY=:8` / VNC `:5908` / noVNC `:6088`) — never the visitor's device and never the site owner's personal PC.

> live grok computer. strangers join a queue and drive this desk.

Follow the desk on X: [x.com/botcomputerxai](https://x.com/botcomputerxai)

## Docs

- [PLAY.md](./PLAY.md) — short player / operator instructions
- [SECURITY.md](./SECURITY.md) — trust boundary + deploy rules
- [STREAM.md](./STREAM.md) — noVNC / proxy notes
- [RUN.md](./RUN.md) — quick runbook

## Quick start

```bash
cd /workspace/play-site
./scripts/start-novnc-localhost.sh   # 127.0.0.1:6088 → localhost:5908
python3 server.py                    # :8787, proxies /stream/
```

Or:

```bash
./start.sh
```

Open http://localhost:8787

## Safe public deploy

- **Frontend** can be public static on Netlify (`public/` + `PLAY_API_BASE` → agent tunnel).
- **Queue/API + stream proxy** stay on the agent host (tunnel), not on Netlify.
- **Stream gateway stays on the agent host only**: VNC/noVNC remain loopback (`127.0.0.1:5908` / `127.0.0.1:6088`); public viewers reach them only through an authenticated TLS tunnel/proxy that reverse-proxies `/stream/`.
- Control requires a **now-playing** ticket; default embed is `view_only`.
- Do not publish raw VNC/noVNC ports. Do not mount or document control of the owner's personal PC.
- No secrets in git. See [SECURITY.md](./SECURITY.md).

## Defaults

| Var | Default |
|---|---|
| PORT | 8787 |
| SESSION_SECONDS | 15 (live); `0` / `UNLIMITED` = no timer (testing) |
| STREAM_UPSTREAM | `127.0.0.1:6088` |

Copy `.env.example` to `.env` to customize. Legacy `SESSION_MINUTES` is still read if `SESSION_SECONDS` is unset.

## Product rules

- Guest identity: random `clientId` in localStorage + optional display name (default `guest-xxxx`)
- **FIFO queue** by join time (no wallet / holdings ranking)
- Fixed session length from `SESSION_SECONDS` (live default **15 seconds**; `0` / `UNLIMITED` = no timer for testing)
- One now-playing controller; everyone else watches the stream
- Controllable surface = **agent computer** / live computer only
- Two-agent crew: **live computer** (desk/stream) + **twitter** ([@botcomputerxai](https://x.com/botcomputerxai))
- Same clientId cannot join twice while already queued or now playing (idempotent)

## API

- `GET /api/health` — health + public config (`sessionSeconds`, `crew`, stream paths)
- `GET /api/state` — `nowPlaying`, `queue` (with `etaSeconds`), `config`, `crew`
- `POST /api/queue/join` — JSON `{ "clientId", "name" }`
- `POST /api/queue/leave` — JSON `{ "clientId" }`

### Curl proof (FIFO queue)

```bash
# occupy seat with alice, then bob waits ahead of carol by join time
curl -s -X POST http://localhost:8787/api/queue/join \
  -H 'Content-Type: application/json' \
  -d '{"clientId":"client-alice01","name":"alice"}'

curl -s -X POST http://localhost:8787/api/queue/join \
  -H 'Content-Type: application/json' \
  -d '{"clientId":"client-bob0002","name":"bob"}'

curl -s -X POST http://localhost:8787/api/queue/join \
  -H 'Content-Type: application/json' \
  -d '{"clientId":"client-carol03","name":"carol"}'

curl -s http://localhost:8787/api/state | python3 -m json.tool
```

With an idle seat, the first joiner takes it immediately. Later joiners wait in FIFO order.

## Frontend / Netlify

Netlify hosts only the static UI from `public/`. The agent backend (API + `/stream/` proxy) runs on this box and is reached via a Cloudflare quick tunnel (or custom origin).

1. Set `window.PLAY_API_BASE` in `public/config.js` to the agent tunnel origin (empty = same origin, for local `python3 server.py`).
2. Deploy/publish `public/` on Netlify — do not expect `/api/*` on Netlify itself.

Static files in `public/`:

- Dominant live screen (min ~480px tall), sparse ops-desk styling
- All visible UI copy lowercase
- Explicit isolation copy: agent computer / live computer (not visitor device, not owner's pc)
- Controlling: "you are driving the agent computer — click the screen"
- Watching: "watching live computer"
- Calm stream offline message when agent computer is unlinked
- Display name + queue position + countdown in seconds (UI shows `no limit (test)` when unlimited)
- Twitter panel → [x.com/botcomputerxai](https://x.com/botcomputerxai)

## Notes

- Client polls `/api/state` every ~2.5s; probes `/stream/` for offline UX
- State persisted in `data/state.json` (gitignored)
- Stream proxied at `/stream/` → localhost websockify (`STREAM_UPSTREAM`)
