# SECURITY.md — isolation guarantees

Hard rule: the controllable / streamed machine is **only this agent computer**
(`DISPLAY=:8` → `x11vnc :5908` → `noVNC :6088`). It is **never** the site
owner's personal PC and **never** a visitor's device.

## Trust boundary

| Inside the boundary (agent desk) | Outside the boundary |
|---|---|
| Agent Xvfb desktop (`DISPLAY=:8`) | User's Windows / personal PC |
| Localhost VNC / noVNC on this box | User home directories, wallets, repos |
| Timed keyboard/mouse while now-playing | GitHub deploy keys / SSH keys on user machine |
| Browser tabs opened on the agent desk | Secrets, `.env` on the owner's laptop |

Players may watch or briefly drive the **agent computer**. They must not be able
to reach the site owner's files, wallets, repos, or work machine.

## What is exposed vs not

**Exposed (intentionally):**

- Live video of the agent desk via `/stream/` (same-origin proxy → loopback noVNC)
- Timed control of that agent desk for the now-playing wallet (`SESSION_SECONDS`, default 10s)
- Public queue / health APIs (wallet address + holdings UI only)

**Not exposed:**

- The site owner's personal computer (no mounts, no Remote Desktop to user PC)
- Visitor devices (the iframe is not the visitor's screen)
- Secrets, private keys, deploy keys, or credential stores living on the owner's machine
- Raw VNC on a public interface

## Required deploy rules

1. **VNC localhost-only** — `x11vnc` must stay on `127.0.0.1:5908` (`-localhost`). Do not bind RFB to `0.0.0.0`.
2. **noVNC loopback** — websockify/noVNC for play-site binds `127.0.0.1:6088` only (`scripts/start-novnc-localhost.sh`).
3. **Public access only via authenticated tunnel/proxy** — frontend may be public; the stream gateway lives on the agent host and is reached through TLS + auth (or an authenticated tunnel). Never publish raw `:5908` / `:6088`.
4. **Control requires now-playing ticket** — only the seated wallet gets the control embed; everyone else gets `view_only`. Prefer a server-side input gate (strip pointer/key events unless seat token matches); client `view_only=1` alone is not a hard boundary.
5. **View-only default** — watchers always start view-only; control URL is swapped only while seated.
6. **No secrets in git** — keep `.env`, keys, cookies, and wallet seeds out of the repo (see `.gitignore`).

## Session hygiene

- Short seats (default **10 seconds** via `SESSION_SECONDS`).
- On seat expiry / leave: drop the controller back to view-only (kill input path); do not leave an open control websocket attached to a finished seat.
- Do **not** store visitor credentials, Phantom private keys, or site-owner secrets on the agent desk.
- Treat the agent browser profile as disposable arcade state; clear sessions if anything sensitive was typed during a seat.
- One controller at a time; enforce in the queue API.

## Operator checklist

```bash
# loopback stream only
ss -ltnp | rg '5908|6088'

# start localhost noVNC if needed
./scripts/start-novnc-localhost.sh

# play-site proxies /stream/ → 127.0.0.1:6088
python3 server.py   # :8787
```

See also: [PLAY.md](./PLAY.md), [STREAM.md](./STREAM.md), [README.md](./README.md).

## Host security keys / WebAuthn (critical)

The public desk must NEVER reach the operator laptop security key.

- Always DENY any Grok Bot "security key request" / passkey / WebAuthn prompt that names telegram.org or any other site from the arcade Chrome.
- Keep Sand WebAuthn proxy OFF for this product: delete `/home/box/.sand-webauthn-proxy-enabled` and keep `/etc/opt/chrome/policies/managed/sand-webauthn.json` as `{}`.
- Do not set `SAND_WEBAUTHN_PROXY=1` on this agent.
- Players can close tabs and browse (that is intended). They must not get a path to host authenticators.

## Downloads and file manager (critical)

Public players must not download binaries or browse the box filesystem.

- Chrome managed policy `zz-play-arcade-lockdown.json`: `DownloadRestrictions=3` (block all downloads), file chooser dialogs disabled, download dir is unwritable `/home/box/BlockedDownloads`.
- Thunar / file managers hidden (`NoDisplay`) and dock launchers removed; dock is Chrome-only.
- Always kill Thunar if it appears on the public stream.

## Terminals blocked (critical)

Public players must not get a shell on the box.

- Terminal apps hidden (`NoDisplay`) and stubbed in `~/bin` (exit 1).
- Dock is Chrome-only (no terminal launcher).
- XFCE TerminalEmulator helper set to blocked; kill any terminal if it appears.

