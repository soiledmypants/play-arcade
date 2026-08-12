# play

short instructions for players and operators.

## players

1. connect wallet
2. join queue — ranked by holdings (more tokens = higher rank; ties by join time)
3. wait your turn
4. when **now playing**, you control the **agent computer** (live computer) for `sessionSeconds` (live default **15s**; testing may use `0` = no limit)
5. everyone else watches the same live computer stream

### what you can do

- browse and click on the **agent computer** desk while you hold the seat
- watch the live stream when you are not seated

### what you can't do

- touch the site owner's personal computer
- reach the owner's files, wallets, repos, or work machine
- control another visitor's device (the stream is not your laptop)

## operators

```bash
cd /workspace/play-site
./scripts/start-novnc-localhost.sh   # 127.0.0.1:6088 → vnc :5908 / display :8
python3 server.py                    # :8787, proxies /stream/
```

- frontend can be public
- stream gateway stays on the agent host (loopback vnc/novnc + authenticated edge)
- control only for the now-playing ticket; view_only for everyone else

isolation details: [SECURITY.md](./SECURITY.md)


## netlify / static ui

- Netlify serves `public/` only
- set `PLAY_API_BASE` in `public/config.js` to the agent Cloudflare tunnel (or custom backend origin)
- stream should stay embedded in the site iframe (wallet extensions like Phantom can crash top-level noVNC)
