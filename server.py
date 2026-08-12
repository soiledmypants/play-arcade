#!/usr/bin/env python3
"""Free-play arcade — guest FIFO queue + agent-computer stream. Stdlib HTTP API + static frontend."""

from __future__ import annotations

import http.client
import json
import os
import re
import select
import socket
import threading
import time
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
        "url": "https://x.com/botcomputerxai",
        "handle": "@botcomputerxai",
    },
]

STREAM_OFFLINE_MSG = "stream offline - agent computer not linked"

_CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
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


def sanitize_name(name: Any, client_id: str) -> str:
    raw = (str(name) if name is not None else "").strip()
    if not raw:
        suffix = client_id[-4:] if len(client_id) >= 4 else client_id
        return f"guest-{suffix}"
    raw = re.sub(r"\s+", " ", raw)
    if not _NAME_RE.match(raw):
        # strip illegal chars rather than reject soft guest names
        cleaned = re.sub(r"[^A-Za-z0-9 _.\-]", "", raw).strip()
        if not cleaned:
            suffix = client_id[-4:] if len(client_id) >= 4 else client_id
            return f"guest-{suffix}"
        raw = cleaned[:32]
    return raw[:32]


def validate_client_id(client_id: str) -> str:
    client_id = (client_id or "").strip()
    if not _CLIENT_ID_RE.match(client_id):
        raise ValueError("clientId required (8-64 chars, alnum/_/-)")
    return client_id


def load_state() -> None:
    global _state
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                np = data.get("nowPlaying")
                if isinstance(np, dict) and np.get("clientId"):
                    seconds = int(
                        np.get("seconds")
                        or np.get("sessionSeconds")
                        or (
                            int(np.get("minutes") or 0) * 60
                            if np.get("minutes")
                            else SESSION_SECONDS
                        )
                    )
                    cid = str(np["clientId"])
                    np = {
                        "clientId": cid,
                        "name": sanitize_name(np.get("name"), cid),
                        "seconds": seconds,
                        "startedAt": float(np.get("startedAt") or 0),
                        "endsAt": float(np.get("endsAt") or 0),
                        "remainingSeconds": int(np.get("remainingSeconds") or 0)
                        if np.get("remainingSeconds") is not None
                        else 0,
                        "joinedAt": np.get("joinedAt"),
                    }
                else:
                    # Drop legacy wallet-based seats
                    np = None
                queue = []
                for q in data.get("queue") or []:
                    if not isinstance(q, dict) or not q.get("clientId"):
                        continue
                    cid = str(q["clientId"])
                    queue.append(
                        {
                            "clientId": cid,
                            "name": sanitize_name(q.get("name"), cid),
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
    """FIFO by join time."""
    _state["queue"].sort(key=lambda q: float(q.get("joinedAt") or 0))


def client_in_play(client_id: str) -> str | None:
    """Return 'playing' | 'queued' | None."""
    np = _state.get("nowPlaying")
    if np and np.get("clientId") == client_id:
        return "playing"
    for q in _state["queue"]:
        if q.get("clientId") == client_id:
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
            "clientId": nxt["clientId"],
            "name": nxt.get("name") or sanitize_name(None, nxt["clientId"]),
            "seconds": seconds,
            "startedAt": now,
            "endsAt": ends,
            "remainingSeconds": None if unlimited else seconds,
            "unlimited": unlimited,
            "joinedAt": nxt.get("joinedAt"),
        }

    _state["updatedAt"] = int(now)


def public_config() -> dict[str, Any]:
    return {
        "sessionSeconds": SESSION_SECONDS,
        "sessionUnlimited": SESSION_UNLIMITED,
        "queueMode": "fifo",
        "identityMode": "guest",
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
        queue.append(
            {
                "position": i + 1,
                "clientId": q["clientId"],
                "name": q.get("name") or sanitize_name(None, q["clientId"]),
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
        np_out = {
            "clientId": np["clientId"],
            "name": np.get("name") or sanitize_name(None, np["clientId"]),
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


def join_queue(client_id: str, name: Any = None) -> dict[str, Any]:
    client_id = validate_client_id(client_id)
    display = sanitize_name(name, client_id)

    with _lock:
        tick_sessions()
        status = client_in_play(client_id)
        if status == "playing":
            np = _state.get("nowPlaying")
            if np and np.get("clientId") == client_id:
                np["name"] = display
            save_state()
            return {
                "ok": True,
                "status": "playing",
                "message": "already now playing",
                "name": display,
                "state": public_state_unlocked(),
            }
        if status == "queued":
            for q in _state["queue"]:
                if q["clientId"] == client_id:
                    q["name"] = display
                    break
            sort_queue()
            pos = next(
                (
                    i + 1
                    for i, q in enumerate(_state["queue"])
                    if q["clientId"] == client_id
                ),
                None,
            )
            save_state()
            return {
                "ok": True,
                "status": "queued",
                "position": pos,
                "message": "already in queue",
                "name": display,
                "state": public_state_unlocked(),
            }

        entry = {
            "clientId": client_id,
            "name": display,
            "joinedAt": time.time(),
            "seconds": SESSION_SECONDS,
        }
        _state["queue"].append(entry)
        sort_queue()
        tick_sessions()
        save_state()
        np = _state.get("nowPlaying")
        if np and np.get("clientId") == client_id:
            return {
                "ok": True,
                "status": "playing",
                "message": "joined - you have the seat",
                "name": display,
                "state": public_state_unlocked(),
            }
        pos = next(
            (
                i + 1
                for i, q in enumerate(_state["queue"])
                if q["clientId"] == client_id
            ),
            None,
        )
        return {
            "ok": True,
            "status": "queued",
            "position": pos,
            "message": "joined the queue",
            "name": display,
            "state": public_state_unlocked(),
        }


def leave_queue(client_id: str) -> dict[str, Any]:
    client_id = validate_client_id(client_id)

    with _lock:
        tick_sessions()
        np = _state.get("nowPlaying")
        if np and np.get("clientId") == client_id:
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
        _state["queue"] = [
            q for q in _state["queue"] if q.get("clientId") != client_id
        ]
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
            client_id = (body.get("clientId") or "").strip()
            name = body.get("name")
            try:
                result = join_queue(client_id, name=name)
            except ValueError as e:
                self._json(400, {"error": str(e)})
                return
            self._json(200, result)
            return

        if path == "/api/queue/leave":
            client_id = (body.get("clientId") or "").strip()
            try:
                result = leave_queue(client_id)
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
    print(
        f"  SESSION_SECONDS={cfg['sessionSeconds']}  "
        f"queueMode={cfg['queueMode']}  identityMode={cfg['identityMode']}"
    )
    print(f"  stream proxy {STREAM_PREFIX}/ -> {STREAM_UPSTREAM} (loopback only)")
    print(f"  static={PUBLIC}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
