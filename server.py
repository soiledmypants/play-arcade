#!/usr/bin/env python3
"""Free-play arcade - wallet holdings queue + agent-computer stream. Stdlib HTTP API + static frontend."""

from __future__ import annotations

import atexit
import base64
import hashlib
import http.client
import json
import os
import queue
import re
import select
import socket
import struct
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
PUBLIC = ROOT / "public"
STATE_FILE = ROOT / "data" / "state.json"
LOG_DIR = ROOT / "logs"

# ---------------------------------------------------------------------------
# Config / .env
# ---------------------------------------------------------------------------


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip("'").strip('"')
        if key and key not in os.environ:
            os.environ[key] = val


load_dotenv(ROOT / ".env")

PORT = int(os.environ.get("PORT", "8787"))

# Prefer SESSION_SECONDS; migrate legacy SESSION_MINUTES if needed.
# 0 / negative / "UNLIMITED" = no timer expiry (testing). Live target is 15.
_raw_session = (os.environ.get("SESSION_SECONDS") or "").strip()
if _raw_session.upper() == "UNLIMITED":
    SESSION_SECONDS = 0
elif _raw_session != "":
    SESSION_SECONDS = max(0, int(_raw_session))
elif os.environ.get("SESSION_MINUTES"):
    SESSION_SECONDS = max(0, int(os.environ["SESSION_MINUTES"]) * 60)
else:
    SESSION_SECONDS = 15

SESSION_UNLIMITED = SESSION_SECONDS <= 0

DEMO_MODE = (os.environ.get("DEMO_MODE") or "false").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
TOKEN_MINT = (
    os.environ.get("TOKEN_MINT")
    or "8j3VdEjQW1Wch6nQHueVu9A1DeihKXiq3qS6eSPWpump"
).strip()
SOLANA_RPC_URL = (
    os.environ.get("SOLANA_RPC_URL") or "https://api.mainnet-beta.solana.com"
).strip()

# Localhost-only noVNC/websockify upstream (never bind VNC/noVNC to 0.0.0.0 here).
STREAM_UPSTREAM = os.environ.get("STREAM_UPSTREAM", "127.0.0.1:6088")
_stream_host, _stream_port_s = STREAM_UPSTREAM.rsplit(":", 1)
STREAM_UPSTREAM_HOST = _stream_host
STREAM_UPSTREAM_PORT = int(_stream_port_s)
STREAM_PREFIX = "/stream"
STREAM_VIEW_URL = os.environ.get(
    "STREAM_VIEW_URL",
    f"http://{STREAM_UPSTREAM}/vnc.html?autoconnect=1&resize=scale&view_only=1",
)
STREAM_CONTROL_URL = os.environ.get(
    "STREAM_CONTROL_URL",
    f"http://{STREAM_UPSTREAM}/vnc.html?autoconnect=1&resize=scale",
)
STREAM_VIEW_PATH = (
    f"{STREAM_PREFIX}/vnc.html?autoconnect=1&resize=scale&view_only=1&path=stream/"
)
STREAM_CONTROL_PATH = (
    f"{STREAM_PREFIX}/vnc.html?autoconnect=1&resize=scale&path=stream/"
)

# Desktop audio side channel (Pulse null-sink -> ffmpeg PCM over WebSocket).
# HTTP progressive MP3 is buffered to empty by Cloudflare tunnels; use WS instead.
AUDIO_PREFIX = "/audio"
AUDIO_WS_PATH = f"{AUDIO_PREFIX}/ws"
AUDIO_STREAM_PATH = f"{AUDIO_PREFIX}/stream.pcm"  # local debug raw PCM (not for CF)
AUDIO_CONTENT_TYPE = "application/octet-stream"
AUDIO_SAMPLE_RATE = 24000
AUDIO_CHANNELS = 1
AUDIO_FORMAT = "s16le"
AUDIO_PULSE_SERVER = os.environ.get(
    "PULSE_SERVER", "unix:/tmp/xdg-runtime-box-8/pulse/native"
)
AUDIO_PULSE_SINK = os.environ.get("PLAY_PULSE_SINK", "playdesk")
WS_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

CREW = [
    {
        "id": "live-computer",
        "name": "live computer",
        "role": "agent computer desk / stream",
    },
    {
        "id": "twitter",
        "name": "twitter",
        "role": "posts",
        "url": "https://x.com/botcomputerxai",
        "handle": "@botcomputerxai",
    },
]

STREAM_OFFLINE_MSG = "stream offline - agent computer not linked"

# Solana base58 pubkey (loose; Phantom addresses are typically 32-44 chars).
_WALLET_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
_NAME_RE = re.compile(r"^[A-Za-z0-9 _.\-]{1,32}$")

# ---------------------------------------------------------------------------
# In-memory state
# ---------------------------------------------------------------------------

_lock = threading.RLock()
_state: dict[str, Any] = {
    "nowPlaying": None,
    "queue": [],
    "updatedAt": 0,
}


def _default_state() -> dict[str, Any]:
    return {"nowPlaying": None, "queue": [], "updatedAt": int(time.time())}


def sanitize_name(name: Any, wallet: str) -> str:
    raw = (str(name) if name is not None else "").strip()
    if not raw:
        suffix = wallet[-4:] if len(wallet) >= 4 else wallet
        return f"guest-{suffix}"
    raw = re.sub(r"\s+", " ", raw)
    if not _NAME_RE.match(raw):
        cleaned = re.sub(r"[^A-Za-z0-9 _.\-]", "", raw).strip()
        if not cleaned:
            suffix = wallet[-4:] if len(wallet) >= 4 else wallet
            return f"guest-{suffix}"
        raw = cleaned[:32]
    return raw[:32]


def validate_wallet(wallet: str) -> str:
    wallet = (wallet or "").strip()
    if not _WALLET_RE.match(wallet):
        raise ValueError("wallet required (base58 solana address)")
    return wallet


def short_wallet(wallet: str) -> str:
    if len(wallet) <= 10:
        return wallet
    return f"{wallet[:4]}…{wallet[-4:]}"


def rpc_token_holdings(wallet: str) -> float:
    """Read SPL token uiAmount for TOKEN_MINT owned by wallet. 0 on miss/error."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTokenAccountsByOwner",
        "params": [
            wallet,
            {"mint": TOKEN_MINT},
            {"encoding": "jsonParsed"},
        ],
    }
    req = urllib.request.Request(
        SOLANA_RPC_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return 0.0
    if data.get("error"):
        return 0.0
    value = ((data.get("result") or {}).get("value")) or []
    total = 0.0
    for acct in value:
        try:
            info = acct["account"]["data"]["parsed"]["info"]["tokenAmount"]
            ui = info.get("uiAmount")
            if ui is None:
                amount = float(info.get("amount") or 0)
                decimals = int(info.get("decimals") or 0)
                ui = amount / (10**decimals) if decimals >= 0 else amount
            total += float(ui or 0)
        except (KeyError, TypeError, ValueError):
            continue
    return max(0.0, total)


def resolve_holdings(wallet: str, demo_holdings: Any = None) -> float:
    if DEMO_MODE and demo_holdings is not None:
        try:
            return max(0.0, float(demo_holdings))
        except (TypeError, ValueError) as e:
            raise ValueError("holdings must be a number") from e
    return rpc_token_holdings(wallet)


def load_state() -> None:
    global _state
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                np = data.get("nowPlaying")
                if isinstance(np, dict) and np.get("wallet"):
                    seconds = int(
                        np.get("seconds")
                        or np.get("sessionSeconds")
                        or (
                            int(np.get("minutes") or 0) * 60
                            if np.get("minutes")
                            else SESSION_SECONDS
                        )
                    )
                    wallet = str(np["wallet"])
                    np = {
                        "wallet": wallet,
                        "name": sanitize_name(np.get("name"), wallet),
                        "holdings": float(np.get("holdings") or 0),
                        "seconds": seconds,
                        "startedAt": float(np.get("startedAt") or 0),
                        "endsAt": float(np.get("endsAt") or 0),
                        "remainingSeconds": int(np.get("remainingSeconds") or 0)
                        if np.get("remainingSeconds") is not None
                        else 0,
                        "joinedAt": np.get("joinedAt"),
                    }
                else:
                    # Drop legacy guest/clientId seats
                    np = None
                queue = []
                for q in data.get("queue") or []:
                    if not isinstance(q, dict) or not q.get("wallet"):
                        continue
                    wallet = str(q["wallet"])
                    queue.append(
                        {
                            "wallet": wallet,
                            "name": sanitize_name(q.get("name"), wallet),
                            "holdings": float(q.get("holdings") or 0),
                            "joinedAt": float(q.get("joinedAt") or time.time()),
                            "seconds": SESSION_SECONDS,
                        }
                    )
                _state = {
                    "nowPlaying": np,
                    "queue": queue,
                    "updatedAt": data.get("updatedAt") or int(time.time()),
                }
                sort_queue()
                return
        except (OSError, json.JSONDecodeError):
            pass
    _state = _default_state()


def save_state() -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    payload = {
        "nowPlaying": _state["nowPlaying"],
        "queue": _state["queue"],
        "updatedAt": _state["updatedAt"],
    }
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


def sort_queue() -> None:
    """Holdings desc, then earlier join time wins ties."""
    _state["queue"].sort(
        key=lambda q: (-float(q.get("holdings") or 0), float(q.get("joinedAt") or 0))
    )


def wallet_in_play(wallet: str) -> str | None:
    """Return 'playing' | 'queued' | None."""
    np = _state.get("nowPlaying")
    if np and np.get("wallet") == wallet:
        return "playing"
    for q in _state["queue"]:
        if q.get("wallet") == wallet:
            return "queued"
    return None


def tick_sessions() -> None:
    """Advance session clock; promote next player when seat is free/expired.

    SESSION_SECONDS=0 means unlimited (testing): seat never auto-expires.
    """
    now = time.time()
    np = _state.get("nowPlaying")
    if np:
        unlimited = SESSION_UNLIMITED or int(np.get("seconds") or 0) == 0
        ends_at = float(np.get("endsAt") or 0)
        if unlimited:
            np["seconds"] = 0
            np["endsAt"] = 0
            np["remainingSeconds"] = None
            np["unlimited"] = True
        elif ends_at <= now:
            _state["nowPlaying"] = None
            np = None
        else:
            remaining = max(0, int(ends_at - now))
            np["remainingSeconds"] = remaining
            np["unlimited"] = False

    sort_queue()

    if np is None and _state["queue"]:
        nxt = _state["queue"].pop(0)
        seconds = SESSION_SECONDS
        unlimited = SESSION_UNLIMITED or seconds == 0
        ends = 0 if unlimited else (now + seconds)
        _state["nowPlaying"] = {
            "wallet": nxt["wallet"],
            "name": nxt.get("name") or sanitize_name(None, nxt["wallet"]),
            "holdings": float(nxt.get("holdings") or 0),
            "seconds": seconds,
            "startedAt": now,
            "endsAt": ends,
            "remainingSeconds": None if unlimited else seconds,
            "unlimited": unlimited,
            "joinedAt": nxt.get("joinedAt"),
        }

    _state["updatedAt"] = int(now)



# ---------------------------------------------------------------------------
# Desktop audio bridge (Pulse monitor -> ffmpeg raw PCM fanout)
# ---------------------------------------------------------------------------


def _ws_accept_key(key: str) -> str:
    dig = hashlib.sha1(key.encode("utf-8") + WS_GUID).digest()
    return base64.b64encode(dig).decode("ascii")


def _ws_send_frame(sock: socket.socket, opcode: int, payload: bytes) -> None:
    header = bytearray()
    header.append(0x80 | (opcode & 0x0F))
    n = len(payload)
    if n < 126:
        header.append(n)
    elif n < 65536:
        header.append(126)
        header.extend(struct.pack("!H", n))
    else:
        header.append(127)
        header.extend(struct.pack("!Q", n))
    sock.sendall(bytes(header) + payload)


def _recvexact(sock: socket.socket, n: int) -> bytes | None:
    buf = bytearray()
    while len(buf) < n:
        try:
            chunk = sock.recv(n - len(buf))
        except BlockingIOError:
            return None if not buf else None
        except OSError:
            return None
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def _ws_try_read_frame(sock: socket.socket) -> tuple[int, bytes] | None:
    """Non-blocking-ish: return (opcode, payload) or None if no complete frame yet / closed."""
    try:
        readable, _, _ = select.select([sock], [], [], 0)
    except (OSError, ValueError):
        return (-1, b"")
    if not readable:
        return None
    hdr = _recvexact(sock, 2)
    if hdr is None:
        return (-1, b"")
    b0, b1 = hdr[0], hdr[1]
    opcode = b0 & 0x0F
    masked = (b1 & 0x80) != 0
    length = b1 & 0x7F
    if length == 126:
        ext = _recvexact(sock, 2)
        if ext is None:
            return (-1, b"")
        length = struct.unpack("!H", ext)[0]
    elif length == 127:
        ext = _recvexact(sock, 8)
        if ext is None:
            return (-1, b"")
        length = struct.unpack("!Q", ext)[0]
    mask = b""
    if masked:
        mask = _recvexact(sock, 4)
        if mask is None:
            return (-1, b"")
    payload = b""
    if length:
        payload = _recvexact(sock, length)
        if payload is None:
            return (-1, b"")
        if masked:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return opcode, payload


class AudioBridge:
    """Supervise ffmpeg capturing a Pulse sink monitor; fan out PCM to WS/HTTP clients."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._clients: list[queue.Queue[bytes | None]] = []
        self._proc: subprocess.Popen[bytes] | None = None
        self._stop = False
        self._thread: threading.Thread | None = None
        self._started = False
        self._restarts = 0
        self._last_error = ""

    @property
    def running(self) -> bool:
        proc = self._proc
        return bool(proc and proc.poll() is None)

    def status(self) -> dict[str, Any]:
        return {
            "wsPath": AUDIO_WS_PATH,
            "sampleRate": AUDIO_SAMPLE_RATE,
            "channels": AUDIO_CHANNELS,
            "format": AUDIO_FORMAT,
            "path": AUDIO_STREAM_PATH,
            "contentType": AUDIO_CONTENT_TYPE,
            "pulseServer": AUDIO_PULSE_SERVER,
            "pulseSink": AUDIO_PULSE_SINK,
            "running": self.running,
            "clients": len(self._clients),
            "restarts": self._restarts,
            "lastError": self._last_error or None,
            "note": (
                "WebSocket PCM via /audio/ws. "
                "HTTP progressive MP3 is broken through Cloudflare (buffered empty body)."
            ),
        }

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
            self._stop = False
            self._thread = threading.Thread(
                target=self._supervise, name="audio-bridge", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop = True
        proc = self._proc
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
            try:
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except OSError:
                    pass
        with self._lock:
            for q in list(self._clients):
                try:
                    q.put_nowait(None)
                except Exception:
                    pass
            self._clients.clear()

    def subscribe(self) -> queue.Queue[bytes | None]:
        q: queue.Queue[bytes | None] = queue.Queue(maxsize=64)
        with self._lock:
            self._clients.append(q)
        return q

    def unsubscribe(self, q: queue.Queue[bytes | None]) -> None:
        with self._lock:
            if q in self._clients:
                self._clients.remove(q)

    def _broadcast(self, chunk: bytes) -> None:
        with self._lock:
            clients = list(self._clients)
        dead: list[queue.Queue[bytes | None]] = []
        for q in clients:
            try:
                q.put_nowait(chunk)
            except queue.Full:
                # Slow client: drop oldest then retry once; else detach.
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    q.put_nowait(chunk)
                except queue.Full:
                    dead.append(q)
        if dead:
            with self._lock:
                for q in dead:
                    if q in self._clients:
                        self._clients.remove(q)

    def _spawn(self) -> subprocess.Popen[bytes]:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_path = LOG_DIR / "audio-ffmpeg.log"
        log_f = open(log_path, "a", encoding="utf-8")
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        log_f.write(
            f"\n--- spawn {stamp} sink={AUDIO_PULSE_SINK} "
            f"pcm={AUDIO_FORMAT} {AUDIO_CHANNELS}ch {AUDIO_SAMPLE_RATE}Hz ---\n"
        )
        log_f.flush()
        env = os.environ.copy()
        env["PULSE_SERVER"] = AUDIO_PULSE_SERVER
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-f",
            "pulse",
            "-i",
            f"{AUDIO_PULSE_SINK}.monitor",
            "-ac",
            str(AUDIO_CHANNELS),
            "-ar",
            str(AUDIO_SAMPLE_RATE),
            "-f",
            AUDIO_FORMAT,
            "-",
        ]
        return subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=log_f,
            env=env,
            bufsize=0,
        )

    def _supervise(self) -> None:
        while not self._stop:
            proc = None
            try:
                proc = self._spawn()
                self._proc = proc
                assert proc.stdout is not None
                # ~85ms of mono s16le @ 24kHz per read
                while not self._stop:
                    chunk = proc.stdout.read(4096)
                    if not chunk:
                        break
                    self._broadcast(chunk)
                rc = proc.poll()
                if rc is None:
                    try:
                        proc.terminate()
                        proc.wait(timeout=2)
                    except Exception:
                        try:
                            proc.kill()
                        except OSError:
                            pass
                        proc.wait(timeout=1)
                    rc = proc.poll()
                if not self._stop:
                    self._restarts += 1
                    self._last_error = f"ffmpeg exited rc={rc}"
                    print(f"[play-site] audio bridge restart: {self._last_error}")
            except Exception as exc:
                self._last_error = str(exc)
                self._restarts += 1
                print(f"[play-site] audio bridge error: {exc}")
            finally:
                self._proc = None
                if proc is not None:
                    try:
                        if proc.stdout:
                            proc.stdout.close()
                    except Exception:
                        pass
            if self._stop:
                break
            time.sleep(1.0)


_audio_bridge = AudioBridge()


def ensure_audio_bridge() -> AudioBridge:
    _audio_bridge.start()
    return _audio_bridge


def public_config() -> dict[str, Any]:
    return {
        "sessionSeconds": SESSION_SECONDS,
        "sessionUnlimited": SESSION_UNLIMITED,
        "queueMode": "holdings",
        "identityMode": "wallet",
        "demoMode": DEMO_MODE,
        "tokenMint": TOKEN_MINT,
        "port": PORT,
        "crew": CREW,
        "stream": {
            "upstream": STREAM_UPSTREAM,
            "proxyPrefix": STREAM_PREFIX,
            "viewPath": STREAM_VIEW_PATH,
            "controlPath": STREAM_CONTROL_PATH,
            "viewUrl": STREAM_VIEW_URL,
            "controlUrl": STREAM_CONTROL_URL,
            "embedMode": "same-origin-proxy",
            "offlineMessage": STREAM_OFFLINE_MSG,
            "note": (
                "stream is the isolated agent computer only "
                "(never the site owner's pc). localhost websockify via /stream/. "
                "public deploy: tls + authenticated proxied wss; "
                "control requires now-playing ticket "
                "(view_only is client-side only without a gate)."
            ),
            "audio": {
                "wsPath": AUDIO_WS_PATH,
                "sampleRate": AUDIO_SAMPLE_RATE,
                "channels": AUDIO_CHANNELS,
                "format": AUDIO_FORMAT,
                "path": AUDIO_STREAM_PATH,
                "contentType": AUDIO_CONTENT_TYPE,
                "note": (
                    "desktop audio side channel (not inside noVNC): "
                    "WebSocket PCM at /audio/ws. "
                    "HTTP progressive MP3 is broken via Cloudflare "
                    "(infinite body buffered to 0 bytes). "
                    "browsers block autoplay with sound; unmute in the UI."
                ),
            },
        },
    }


def _build_public_queue(np: dict[str, Any] | None) -> list[dict[str, Any]]:
    session = SESSION_SECONDS
    queue = []
    for i, q in enumerate(_state["queue"]):
        if SESSION_UNLIMITED:
            eta_seconds = None if np else 0
        else:
            eta_seconds = i * session
            if np:
                rem = max(0, int(np.get("remainingSeconds") or 0))
                eta_seconds = rem + i * session
        wallet = q["wallet"]
        queue.append(
            {
                "position": i + 1,
                "wallet": wallet,
                "walletShort": short_wallet(wallet),
                "name": q.get("name") or sanitize_name(None, wallet),
                "holdings": float(q.get("holdings") or 0),
                "seconds": session,
                "joinedAt": q.get("joinedAt"),
                "etaSeconds": eta_seconds,
            }
        )
    return queue


def public_state_unlocked() -> dict[str, Any]:
    """Build public state; caller must hold _lock."""
    np = _state["nowPlaying"]
    np_out = None
    if np:
        wallet = np["wallet"]
        np_out = {
            "wallet": wallet,
            "walletShort": short_wallet(wallet),
            "name": np.get("name") or sanitize_name(None, wallet),
            "holdings": float(np.get("holdings") or 0),
            "seconds": np.get("seconds"),
            "startedAt": np.get("startedAt"),
            "endsAt": np.get("endsAt"),
            "remainingSeconds": np.get("remainingSeconds"),
            "unlimited": np.get("unlimited"),
            "joinedAt": np.get("joinedAt"),
        }
    return {
        "nowPlaying": np_out,
        "queue": _build_public_queue(np),
        "config": public_config(),
        "crew": CREW,
        "updatedAt": _state["updatedAt"],
    }


def public_state() -> dict[str, Any]:
    with _lock:
        tick_sessions()
        sort_queue()
        save_state()
        return public_state_unlocked()


def join_queue(
    wallet: str, name: Any = None, demo_holdings: Any = None
) -> dict[str, Any]:
    wallet = validate_wallet(wallet)
    display = sanitize_name(name, wallet)
    holdings = resolve_holdings(wallet, demo_holdings)

    with _lock:
        tick_sessions()
        status = wallet_in_play(wallet)
        if status == "playing":
            np = _state.get("nowPlaying")
            if np and np.get("wallet") == wallet:
                np["name"] = display
                np["holdings"] = holdings
            save_state()
            return {
                "ok": True,
                "status": "playing",
                "message": "already now playing",
                "wallet": wallet,
                "name": display,
                "holdings": holdings,
                "state": public_state_unlocked(),
            }
        if status == "queued":
            for q in _state["queue"]:
                if q["wallet"] == wallet:
                    q["name"] = display
                    q["holdings"] = holdings
                    break
            sort_queue()
            pos = next(
                (
                    i + 1
                    for i, q in enumerate(_state["queue"])
                    if q["wallet"] == wallet
                ),
                None,
            )
            save_state()
            return {
                "ok": True,
                "status": "queued",
                "position": pos,
                "message": "already in queue",
                "wallet": wallet,
                "name": display,
                "holdings": holdings,
                "state": public_state_unlocked(),
            }

        entry = {
            "wallet": wallet,
            "name": display,
            "holdings": holdings,
            "joinedAt": time.time(),
            "seconds": SESSION_SECONDS,
        }
        _state["queue"].append(entry)
        sort_queue()
        tick_sessions()
        save_state()
        np = _state.get("nowPlaying")
        if np and np.get("wallet") == wallet:
            return {
                "ok": True,
                "status": "playing",
                "message": "joined - you have the seat",
                "wallet": wallet,
                "name": display,
                "holdings": holdings,
                "state": public_state_unlocked(),
            }
        pos = next(
            (
                i + 1
                for i, q in enumerate(_state["queue"])
                if q["wallet"] == wallet
            ),
            None,
        )
        return {
            "ok": True,
            "status": "queued",
            "position": pos,
            "message": "joined the queue",
            "wallet": wallet,
            "name": display,
            "holdings": holdings,
            "state": public_state_unlocked(),
        }


def leave_queue(wallet: str) -> dict[str, Any]:
    wallet = validate_wallet(wallet)

    with _lock:
        tick_sessions()
        np = _state.get("nowPlaying")
        if np and np.get("wallet") == wallet:
            _state["nowPlaying"] = None
            tick_sessions()
            save_state()
            return {
                "ok": True,
                "status": "left",
                "message": "left the seat",
                "state": public_state_unlocked(),
            }

        before = len(_state["queue"])
        _state["queue"] = [q for q in _state["queue"] if q.get("wallet") != wallet]
        removed = before != len(_state["queue"])
        sort_queue()
        tick_sessions()
        save_state()
        if not removed:
            return {
                "ok": True,
                "status": "absent",
                "message": "not in queue or playing",
                "state": public_state_unlocked(),
            }
        return {
            "ok": True,
            "status": "left",
            "message": "left the queue",
            "state": public_state_unlocked(),
        }


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------


class Handler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(PUBLIC), **kwargs)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[play-site] {self.address_string()} {fmt % args}")

    def _cors(self) -> None:
        # Allow Netlify (and any) static origin to call the agent API + stream probe.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, HEAD, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, code: int, payload: Any) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as e:
            raise ValueError("invalid JSON body") from e
        if not isinstance(data, dict):
            raise ValueError("JSON body must be an object")
        return data

    def _stream_upstream_path(self) -> str:
        parsed = urlparse(self.path)
        suffix = parsed.path[len(STREAM_PREFIX) :] or "/"
        if not suffix.startswith("/"):
            suffix = "/" + suffix
        if parsed.query:
            return f"{suffix}?{parsed.query}"
        return suffix

    def _proxy_stream_http(self) -> None:
        upstream_path = self._stream_upstream_path()
        try:
            conn = http.client.HTTPConnection(
                STREAM_UPSTREAM_HOST, STREAM_UPSTREAM_PORT, timeout=60
            )
            headers: dict[str, str] = {}
            for key, value in self.headers.items():
                lk = key.lower()
                if lk in ("host", "content-length"):
                    continue
                headers[key] = value
            headers["Host"] = f"{STREAM_UPSTREAM_HOST}:{STREAM_UPSTREAM_PORT}"
            conn.request(self.command, upstream_path, headers=headers)
            resp = conn.getresponse()
            body = resp.read()
            self.send_response(resp.status, resp.reason)
            upstream_len = None
            for key, value in resp.getheaders():
                lk = key.lower()
                if lk in (
                    "transfer-encoding",
                    "connection",
                    "content-length",
                    "content-encoding",
                    "x-frame-options",
                    "content-security-policy",
                    "server",
                    "date",
                ):
                    if lk == "content-length":
                        try:
                            upstream_len = int(value)
                        except ValueError:
                            pass
                    continue
                self.send_header(key, value)
            if self.command == "HEAD" and upstream_len is not None:
                self.send_header("Content-Length", str(upstream_len))
            else:
                self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self._cors()
            self.end_headers()
            if self.command != "HEAD" and body:
                self.wfile.write(body)
            conn.close()
        except OSError:
            self._json(502, {"error": STREAM_OFFLINE_MSG, "offline": True})

    def _proxy_stream_websocket(self) -> None:
        upstream_path = self._stream_upstream_path()
        try:
            upstream = socket.create_connection(
                (STREAM_UPSTREAM_HOST, STREAM_UPSTREAM_PORT), timeout=10
            )
        except OSError:
            self._json(502, {"error": STREAM_OFFLINE_MSG, "offline": True})
            return

        try:
            req_lines = [f"GET {upstream_path} HTTP/1.1"]
            host_sent = False
            for key, value in self.headers.items():
                if key.lower() == "host":
                    req_lines.append(
                        f"Host: {STREAM_UPSTREAM_HOST}:{STREAM_UPSTREAM_PORT}"
                    )
                    host_sent = True
                else:
                    req_lines.append(f"{key}: {value}")
            if not host_sent:
                req_lines.append(
                    f"Host: {STREAM_UPSTREAM_HOST}:{STREAM_UPSTREAM_PORT}"
                )
            req_lines.extend(["", ""])
            upstream.sendall("\r\n".join(req_lines).encode("latin-1"))

            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = upstream.recv(4096)
                if not chunk:
                    break
                buf += chunk
            if b"\r\n\r\n" not in buf:
                self._json(502, {"error": STREAM_OFFLINE_MSG, "offline": True})
                upstream.close()
                return
            header_blob, rest = buf.split(b"\r\n\r\n", 1)
            self.connection.sendall(header_blob + b"\r\n\r\n" + rest)
            status_line = header_blob.split(b"\r\n", 1)[0].decode("latin-1", "replace")
            if " 101 " not in status_line:
                upstream.close()
                self.close_connection = True
                return

            client = self.connection
            upstream.setblocking(False)
            client.setblocking(False)
            sockets = [client, upstream]
            try:
                while True:
                    readable, _, errored = select.select(sockets, [], sockets, 60.0)
                    if errored:
                        break
                    if not readable:
                        continue
                    if client in readable:
                        data = client.recv(65536)
                        if not data:
                            break
                        upstream.sendall(data)
                    if upstream in readable:
                        data = upstream.recv(65536)
                        if not data:
                            break
                        client.sendall(data)
            except OSError:
                pass
            finally:
                try:
                    upstream.close()
                except OSError:
                    pass
                self.close_connection = True
        except Exception:
            try:
                upstream.close()
            except OSError:
                pass
            self.close_connection = True

    def _handle_stream_proxy(self) -> None:
        upgrade = (self.headers.get("Upgrade") or "").lower()
        connection = (self.headers.get("Connection") or "").lower()
        if upgrade == "websocket" or "upgrade" in connection:
            self._proxy_stream_websocket()
            return
        self._proxy_stream_http()

    def _audio_headers(self, streaming: bool = True) -> None:
        self.send_header("Content-Type", AUDIO_CONTENT_TYPE)
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Accept-Ranges", "none")
        if streaming:
            # Infinite live body; discourage intermediaries from buffering the whole response.
            self.send_header("Connection", "close")
        self._cors()

    def _handle_audio_head(self) -> None:
        ensure_audio_bridge()
        self.send_response(200)
        self._audio_headers(streaming=False)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _handle_audio_stream(self) -> None:
        """Local debug: raw PCM over HTTP. Prefer /audio/ws (Cloudflare-safe)."""
        bridge = ensure_audio_bridge()
        self.send_response(200)
        self._audio_headers(streaming=True)
        self.end_headers()
        q = bridge.subscribe()
        try:
            while True:
                try:
                    chunk = q.get(timeout=60.0)
                except queue.Empty:
                    continue
                if chunk is None:
                    break
                self.wfile.write(chunk)
                try:
                    self.wfile.flush()
                except Exception:
                    break
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            bridge.unsubscribe(q)
            self.close_connection = True

    def _handle_audio_ws(self) -> None:
        key = (self.headers.get("Sec-WebSocket-Key") or "").strip()
        upgrade = (self.headers.get("Upgrade") or "").lower()
        if not key or upgrade != "websocket":
            self.send_response(400)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                b'{"error":"expected websocket upgrade to /audio/ws"}'
            )
            return

        bridge = ensure_audio_bridge()
        accept = _ws_accept_key(key)
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()

        sock = self.connection
        q = bridge.subscribe()
        try:
            hello = {
                "type": "hello",
                "sampleRate": AUDIO_SAMPLE_RATE,
                "channels": AUDIO_CHANNELS,
                "format": AUDIO_FORMAT,
            }
            _ws_send_frame(sock, 0x1, json.dumps(hello).encode("utf-8"))
            sock.setblocking(True)
            while True:
                # Drain client control frames (ping/close) without blocking the audio fanout.
                while True:
                    frame = _ws_try_read_frame(sock)
                    if frame is None:
                        break
                    opcode, payload = frame
                    if opcode < 0:
                        return
                    if opcode == 0x8:  # close
                        try:
                            _ws_send_frame(sock, 0x8, payload[:2] if payload else b"")
                        except OSError:
                            pass
                        return
                    if opcode == 0x9:  # ping -> pong
                        try:
                            _ws_send_frame(sock, 0xA, payload)
                        except OSError:
                            return
                    # ignore text/binary from client
                try:
                    chunk = q.get(timeout=1.0)
                except queue.Empty:
                    continue
                if chunk is None:
                    break
                try:
                    _ws_send_frame(sock, 0x2, chunk)
                except OSError:
                    break
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            bridge.unsubscribe(q)
            self.close_connection = True

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/state":
            self._json(200, public_state())
            return
        if path == "/api/health":
            cfg = public_config()
            cfg["stream"] = dict(cfg.get("stream") or {})
            cfg["stream"]["audioStatus"] = _audio_bridge.status()
            self._json(200, {"ok": True, "config": cfg})
            return
        if path == AUDIO_WS_PATH:
            self._handle_audio_ws()
            return
        if path == AUDIO_STREAM_PATH or path == AUDIO_PREFIX or path == AUDIO_PREFIX + "/":
            # Local raw PCM debug stream; browsers should use /audio/ws.
            if path != AUDIO_STREAM_PATH:
                self.send_response(302)
                self.send_header("Location", AUDIO_WS_PATH)
                self._cors()
                self.end_headers()
                return
            self._handle_audio_stream()
            return
        if path == STREAM_PREFIX or path.startswith(STREAM_PREFIX + "/"):
            self._handle_stream_proxy()
            return
        if path == "/":
            self.path = "/index.html"
        return super().do_GET()

    def do_HEAD(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == AUDIO_WS_PATH:
            self.send_response(405)
            self.send_header("Allow", "GET")
            self._cors()
            self.end_headers()
            return
        if path == AUDIO_STREAM_PATH or path == AUDIO_PREFIX or path == AUDIO_PREFIX + "/":
            self._handle_audio_head()
            return
        if path == STREAM_PREFIX or path.startswith(STREAM_PREFIX + "/"):
            self._proxy_stream_http()
            return
        if path == "/":
            self.path = "/index.html"
        return super().do_HEAD()

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            body = self._read_json()
        except ValueError as e:
            self._json(400, {"error": str(e)})
            return

        if path == "/api/queue/join":
            wallet = (body.get("wallet") or "").strip()
            name = body.get("name")
            demo_holdings = body.get("holdings") if DEMO_MODE else None
            try:
                result = join_queue(wallet, name=name, demo_holdings=demo_holdings)
            except ValueError as e:
                self._json(400, {"error": str(e)})
                return
            self._json(200, result)
            return

        if path == "/api/queue/leave":
            wallet = (body.get("wallet") or "").strip()
            try:
                result = leave_queue(wallet)
            except ValueError as e:
                self._json(400, {"error": str(e)})
                return
            self._json(200, result)
            return

        self._json(404, {"error": "not found"})


def main() -> None:
    load_state()
    with _lock:
        tick_sessions()
        save_state()

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    ensure_audio_bridge()
    atexit.register(_audio_bridge.stop)

    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    cfg = public_config()
    print(f"play-site listening on http://0.0.0.0:{PORT}")
    print(
        f"  SESSION_SECONDS={cfg['sessionSeconds']}  "
        f"queueMode={cfg['queueMode']}  identityMode={cfg['identityMode']}  "
        f"demoMode={cfg['demoMode']}"
    )
    print(f"  TOKEN_MINT={TOKEN_MINT}")
    print(f"  stream proxy {STREAM_PREFIX}/ -> {STREAM_UPSTREAM} (loopback only)")
    print(
        f"  audio {AUDIO_WS_PATH} <- pulse:{AUDIO_PULSE_SINK}.monitor "
        f"({AUDIO_FORMAT} {AUDIO_CHANNELS}ch {AUDIO_SAMPLE_RATE}Hz)"
    )
    print(f"  static={PUBLIC}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
        _audio_bridge.stop()
        server.shutdown()


if __name__ == "__main__":
    main()
