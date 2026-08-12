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
| SESSION_SECONDS | 15 (live); `0` = unlimited testing |
| DEMO_MODE | false (local `.env` may set true) |
| TOKEN_MINT | `8j3VdEjQW1Wch6nQHueVu9A1DeihKXiq3qS6eSPWpump` |
| SOLANA_RPC_URL | `https://api.mainnet-beta.solana.com` |
| STREAM_UPSTREAM | `127.0.0.1:6088` |

Copy `.env.example` to `.env` to customize.

## API

- GET /api/health
- GET /api/state
- POST /api/queue/join `{ "wallet", "name?" }` (optional `holdings` when DEMO_MODE)
- POST /api/queue/leave `{ "wallet" }`

### Curl demo (holdings sort)

```bash
# seat filler
curl -s -X POST http://localhost:8787/api/queue/join \
  -H 'Content-Type: application/json' \
  -d '{"wallet":"Aaa1111111111111111111111111111111111111","name":"alice","holdings":10}'

# higher holdings should rank ahead of lower even if later join
curl -s -X POST http://localhost:8787/api/queue/join \
  -H 'Content-Type: application/json' \
  -d '{"wallet":"Bbb2222222222222222222222222222222222222","name":"bob","holdings":50}'

curl -s -X POST http://localhost:8787/api/queue/join \
  -H 'Content-Type: application/json' \
  -d '{"wallet":"Ccc3333333333333333333333333333333333333","name":"carol","holdings":25}'

curl -s http://localhost:8787/api/state | python3 -m json.tool
```
