# play-site

Free-play arcade: connect a Solana wallet (Phantom via `window.solana`), join a **holdings-ranked** queue, play a short session when your turn comes.

The live machine is the isolated **agent computer** on this box (`DISPLAY=:8` / VNC `:5908` / noVNC `:6088`) - never the visitor's device and never the site owner's personal PC.

> grok bots come with a built-in computer. this one's public. queue up and use it. a little blockchain experiment.

Follow the desk on X: [x.com/botcomputerxai](https://x.com/botcomputerxai)

## Docs

- [PLAY.md](./PLAY.md) - short player / operator instructions
- [SECURITY.md](./SECURITY.md) - trust boundary + deploy rules
- [STREAM.md](./STREAM.md) - noVNC / proxy notes
- [RUN.md](./RUN.md) - quick runbook

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
- Control requires a **now-playing** ticket (connected wallet === `nowPlaying.wallet`); default embed is `view_only`.
- Do not publish raw VNC/noVNC ports. Do not mount or document control of the owner's personal PC.
- Do not load wallet-adapter scripts into the stream iframe (blob loader on parent page only).
- No secrets in git. See [SECURITY.md](./SECURITY.md).

## Defaults

| Var | Default |
|---|---|
| PORT | 8787 |
| SESSION_SECONDS | 15 (live); `0` / `UNLIMITED` = no timer (testing) |
| DEMO_MODE | false (accepts `holdings` override in join body when true) |
| TOKEN_MINT | `8j3VdEjQW1Wch6nQHueVu9A1DeihKXiq3qS6eSPWpump` |
| SOLANA_RPC_URL | `https://api.mainnet-beta.solana.com` |
| STREAM_UPSTREAM | `127.0.0.1:6088` |

Copy `.env.example` to `.env` to customize. Legacy `SESSION_MINUTES` is still read if `SESSION_SECONDS` is unset.

## Product rules

- Identity: Solana wallet address (Phantom `window.solana`); optional display name label
- **Holdings queue**: SPL balance for `TOKEN_MINT` desc, ties by join time
- Fixed session length from `SESSION_SECONDS` (live default **15 seconds**; `0` / `UNLIMITED` = no timer for testing)
- One now-playing controller; everyone else watches the stream
- Controllable surface = **agent computer** / live computer only
- Two-agent crew: **live computer** (desk/stream) + **twitter** ([@botcomputerxai](https://x.com/botcomputerxai))
- Same wallet cannot join twice while already queued or now playing (idempotent)

## API

- `GET /api/health` - health + public config (`sessionSeconds`, `crew`, stream paths, `tokenMint`, `demoMode`)
- `GET /api/state` - `nowPlaying`, `queue` (with `holdings`, `etaSeconds`), `config`, `crew`
- `POST /api/queue/join` - JSON `{ "wallet", "name?" }` (+ optional `holdings` when `DEMO_MODE`)
- `POST /api/queue/leave` - JSON `{ "wallet" }`

### Curl proof (holdings sort)

```bash
curl -s -X POST http://localhost:8787/api/queue/join \
  -H 'Content-Type: application/json' \
  -d '{"wallet":"Aaa1111111111111111111111111111111111111","name":"alice","holdings":10}'

curl -s -X POST http://localhost:8787/api/queue/join \
  -H 'Content-Type: application/json' \
  -d '{"wallet":"Bbb2222222222222222222222222222222222222","name":"bob","holdings":50}'

curl -s -X POST http://localhost:8787/api/queue/join \
  -H 'Content-Type: application/json' \
  -d '{"wallet":"Ccc3333333333333333333333333333333333333","name":"carol","holdings":25}'

curl -s http://localhost:8787/api/state | python3 -m json.tool
```

With an idle seat, the first joiner takes it immediately. Waiting queue sorts by holdings desc (bob ahead of carol).

## Frontend / Netlify

Netlify hosts only the static UI from `public/`. The agent backend (API + `/stream/` proxy) runs on this box and is reached via a Cloudflare quick tunnel (or custom origin).

1. Set `window.PLAY_API_BASE` in `public/config.js` to the agent tunnel origin (empty = same origin, for local `python3 server.py`).
2. Deploy/publish `public/` on Netlify - do not expect `/api/*` on Netlify itself.

Static files in `public/`:

- Dominant sticky live screen (~76vh / min 560px), sparse ops-desk styling
- Join / queue / twitter below the stream (no tall side rail)
- Blob-based noVNC loader (`createObjectURL` / `loadStreamFrame`) so Phantom does not inject into the stream
- Wallet connect only on the parent page via `window.solana`
