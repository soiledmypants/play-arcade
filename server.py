#!/usr/bin/env python3
"""Solana free-play arcade — wallet verify + hold-to-rank queue. Stdlib HTTP API + static frontend."""

from __future__ import annotations

import http.client
import json
import os
import select
import socket
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

DEMO_MODE = os.environ.get("DEMO_MODE", "true").lower() in ("1", "true", "yes", "on")
TOKEN_MINT = os.environ.get("TOKEN_MINT") or "8j3VdEjQW1Wch6nQHueVu9A1DeihKXiq3qS6eSPWpump"
SOLANA_RPC_URL = os.environ.get("SOLANA_RPC_URL") or "https://api.mainnet-beta.solana.com"
HOLDINGS_REFRESH_SECONDS = max(15, int(os.environ.get("HOLDINGS_REFRESH_SECONDS", "60")))

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
    },
]

STREAM_OFFLINE_MSG = "stream offline — agent computer not linked"

# ---------------------------------------------------------------------------
# In-memory state
# ---------------------------------------------------------------------------

_lock = threading.RLock()
_state: dict[str, Any] = {
    "nowPlaying": None,
    "queue": [],
    "updatedAt": 0,
}
_last_holdings_refresh = 0.0


def _default_state() -> dict[str, Any]:
    return {"nowPlaying": None, "queue": [], "updatedAt": int(time.time())}


def truncate_wallet(wallet: str) -> str:
    if not wallet or len(wallet) < 10:
        return wallet or ""
    return f"{wallet[:4]}…{wallet[-4:]}"


def _entry_holdings(entry: dict[str, Any] | None) -> dict[str, Any]:
    if not entry:
        return {"holdingsRaw": "0", "holdingsUi": 0.0}
    return {
        "holdingsRaw": str(entry.get("holdingsRaw") or "0"),
        "holdingsUi": float(entry.get("holdingsUi") or 0),
    }


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
                    holdings = _entry_holdings(np)
                    np = {
                        "wallet": np["wallet"],
                        "walletShort": np.get("walletShort")
                        or truncate_wallet(np["wallet"]),
                        "seconds": seconds,
                        "startedAt": float(np.get("startedAt") or 0),
                        "endsAt": float(np.get("endsAt") or 0),
                        "remainingSeconds": int(np.get("remainingSeconds") or 0),
                        "joinedAt": np.get("joinedAt"),
                        "holdingsRaw": holdings["holdingsRaw"],
                        "holdingsUi": holdings["holdingsUi"],
                    }
                else:
                    np = None
                queue = []
                for q in data.get("queue") or []:
                    if not isinstance(q, dict) or not q.get("wallet"):
                        continue
                    holdings = _entry_holdings(q)
                    queue.append(
                        {
                            "wallet": q["wallet"],
                            "joinedAt": float(q.get("joinedAt") or time.time()),
                            "seconds": SESSION_SECONDS,
                            "holdingsRaw": holdings["holdingsRaw"],
                            "holdingsUi": holdings["holdingsUi"],
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
    """More tokens held = higher rank; ties by earlier join time."""
    _state["queue"].sort(
        key=lambda q: (
            -float(q.get("holdingsUi") or 0),
            float(q.get("joinedAt") or 0),
        )
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


def fetch_token_balance(wallet: str) -> tuple[str, float]:
    """Return (raw_amount_str, ui_amount) for TOKEN_MINT via Solana RPC."""
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
    raw_total = 0
    ui_total = 0.0
    try:
        req = urllib.request.Request(
            SOLANA_RPC_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        if body.get("error"):
            return "0", 0.0
        value = ((body.get("result") or {}).get("value")) or []
        for acct in value:
            info = (
                ((acct.get("account") or {}).get("data") or {})
                .get("parsed", {})
                .get("info", {})
            )
            tok = info.get("tokenAmount") or {}
            try:
                raw_total += int(tok.get("amount") or 0)
            except (TypeError, ValueError):
                pass
            try:
                if tok.get("uiAmount") is not None:
                    ui_total += float(tok["uiAmount"])
                else:
                    decimals = int(tok.get("decimals") or 0)
                    ui_total += int(tok.get("amount") or 0) / (10**decimals if decimals else 1)
            except (TypeError, ValueError):
                pass
        return str(raw_total), float(ui_total)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
        return "0", 0.0


def resolve_holdings(
    wallet: str, demo_holdings: Any = None
) -> tuple[str, float]:
    """Resolve holdings; demoMode may override with a fake ui amount."""
    if DEMO_MODE and demo_holdings is not None:
        try:
            ui = float(demo_holdings)
            if ui < 0:
                ui = 0.0
            # Store as integer-ish raw for demos (assume 6 decimals like many SPL mints)
            raw = str(int(ui * 1_000_000))
            return raw, ui
        except (TypeError, ValueError):
            pass
    return fetch_token_balance(wallet)


def refresh_queue_holdings(force: bool = False) -> None:
    """Periodically re-fetch balances and re-sort (caller holds lock)."""
    global _last_holdings_refresh
    now = time.time()
    if not force and (now - _last_holdings_refresh) < HOLDINGS_REFRESH_SECONDS:
        return
    if not _state["queue"] and not _state.get("nowPlaying"):
        _last_holdings_refresh = now
        return
    for q in _state["queue"]:
        # Skip demo-faked entries that have no real wallet on-chain refresh need
        # when DEMO_MODE and holdings were injected — still allow refresh for real wallets.
        raw, ui = fetch_token_balance(q["wallet"])
        # Keep demo overrides if RPC returns 0 and entry already has positive holdings
        # only when DEMO_MODE — avoids wiping curl-proof fake balances every refresh.
        if DEMO_MODE and ui == 0 and float(q.get("holdingsUi") or 0) > 0:
            continue
        q["holdingsRaw"] = raw
        q["holdingsUi"] = ui
    np = _state.get("nowPlaying")
    if np and np.get("wallet"):
        raw, ui = fetch_token_balance(np["wallet"])
        if not (DEMO_MODE and ui == 0 and float(np.get("holdingsUi") or 0) > 0):
            np["holdingsRaw"] = raw
            np["holdingsUi"] = ui
    sort_queue()
    _last_holdings_refresh = now


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
        holdings = _entry_holdings(nxt)
        _state["nowPlaying"] = {
            "wallet": nxt["wallet"],
            "walletShort": truncate_wallet(nxt["wallet"]),
            "seconds": seconds,
            "startedAt": now,
            "endsAt": ends,
            "remainingSeconds": None if unlimited else seconds,
            "unlimited": unlimited,
            "joinedAt": nxt.get("joinedAt"),
            "holdingsRaw": holdings["holdingsRaw"],
            "holdingsUi": holdings["holdingsUi"],
        }

    _state["updatedAt"] = int(now)


def public_config() -> dict[str, Any]:
    return {
        "sessionSeconds": SESSION_SECONDS,
        "sessionUnlimited": SESSION_UNLIMITED,
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
        holdings = _entry_holdings(q)
        queue.append(
            {
                "position": i + 1,
                "wallet": q["wallet"],
                "walletShort": truncate_wallet(q["wallet"]),
                "seconds": session,
                "joinedAt": q.get("joinedAt"),
                "etaSeconds": eta_seconds,
                "holdingsRaw": holdings["holdingsRaw"],
                "holdingsUi": holdings["holdingsUi"],
            }
        )
    return queue


def public_state_unlocked() -> dict[str, Any]:
    """Build public state; caller must hold _lock."""
    np = _state["nowPlaying"]
    np_out = None
    if np:
        holdings = _entry_holdings(np)
        np_out = {
            **np,
            "walletShort": np.get("walletShort") or truncate_wallet(np["wallet"]),
            "holdingsRaw": holdings["holdingsRaw"],
            "holdingsUi": holdings["holdingsUi"],
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
        refresh_queue_holdings(force=False)
        sort_queue()
        save_state()
        return public_state_unlocked()


def join_queue(wallet: str, demo_holdings: Any = None) -> dict[str, Any]:
    wallet = wallet.strip()
    if not wallet:
        raise ValueError("wallet required")
    if len(wallet) < 32 or len(wallet) > 64:
        raise ValueError("wallet looks invalid")

    raw, ui = resolve_holdings(wallet, demo_holdings)

    with _lock:
        tick_sessions()
        status = wallet_in_play(wallet)
        if status == "playing":
            np = _state.get("nowPlaying")
            if np and np.get("wallet") == wallet:
                np["holdingsRaw"] = raw
                np["holdingsUi"] = ui
            save_state()
            return {
                "ok": True,
                "status": "playing",
                "message": "already now playing",
                "holdingsRaw": raw,
                "holdingsUi": ui,
                "state": public_state_unlocked(),
            }
        if status == "queued":
            for q in _state["queue"]:
                if q["wallet"] == wallet:
                    q["holdingsRaw"] = raw
                    q["holdingsUi"] = ui
                    break
            sort_queue()
            pos = next(
                (i + 1 for i, q in enumerate(_state["queue"]) if q["wallet"] == wallet),
                None,
            )
            save_state()
            return {
                "ok": True,
                "status": "queued",
                "position": pos,
                "message": "already in queue",
                "holdingsRaw": raw,
                "holdingsUi": ui,
                "state": public_state_unlocked(),
            }

        entry = {
            "wallet": wallet,
            "joinedAt": time.time(),
            "seconds": SESSION_SECONDS,
            "holdingsRaw": raw,
            "holdingsUi": ui,
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
                "message": "joined — you have the seat",
                "holdingsRaw": raw,
                "holdingsUi": ui,
                "state": public_state_unlocked(),
            }
        pos = next(
            (i + 1 for i, q in enumerate(_state["queue"]) if q["wallet"] == wallet),
            None,
        )
        return {
            "ok": True,
            "status": "queued",
            "position": pos,
            "message": "joined the queue",
            "holdingsRaw": raw,
            "holdingsUi": ui,
            "state": public_state_unlocked(),
        }


def leave_queue(wallet: str) -> dict[str, Any]:
    wallet = wallet.strip()
    if not wallet:
        raise ValueError("wallet required")

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
            self._json(200, {"ok": True, "config": public_config()})
            return
        if path == STREAM_PREFIX or path.startswith(STREAM_PREFIX + "/"):
            self._handle_stream_proxy()
            return
        if path == "/":
            self.path = "/index.html"
        return super().do_GET()

    def do_HEAD(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
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
            demo_holdings = body.get("holdings") if DEMO_MODE else None
            try:
                result = join_queue(wallet, demo_holdings=demo_holdings)
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

    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    cfg = public_config()
    print(f"play-site listening on http://0.0.0.0:{PORT}")
    print(f"  SESSION_SECONDS={cfg['sessionSeconds']}  demoMode={cfg['demoMode']}")
    print(f"  TOKEN_MINT={TOKEN_MINT}")
    print(f"  stream proxy {STREAM_PREFIX}/ -> {STREAM_UPSTREAM} (loopback only)")
    print(f"  static={PUBLIC}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
