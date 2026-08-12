# play-site

Solana free-play arcade: connect a wallet, join the hold-to-rank queue, play a short session.

## Quick start

```bash
cd /workspace/play-site
python3 server.py
```

Or:

```bash
./start.sh
```

Open http://localhost:8787

## Defaults

| Var | Default |
|---|---|
| PORT | 8787 |
| SESSION_SECONDS | 10 |
| DEMO_MODE | true |

Copy `.env.example` to `.env` to customize.

## API

- GET /api/health
- GET /api/state
- POST /api/queue/join `{ "wallet" }` (optional `holdings` when DEMO_MODE)
- POST /api/queue/leave `{ "wallet" }`

### Curl demo

```bash
curl -s -X POST http://localhost:8787/api/queue/join \
  -H 'Content-Type: application/json' \
  -d '{"wallet":"WalletAAA111111111111111111111111111111111","holdings":100}'
```
