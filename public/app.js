const $ = (id) => document.getElementById(id);

const API_BASE = (window.PLAY_API_BASE || "").replace(/\/$/, "");

function apiUrl(path) {
  const p = path.startsWith("/") ? path : `/${path}`;
  return `${API_BASE}${p}`;
}

function absolutizePath(path) {
  if (!path) return path;
  if (/^https?:\/\//i.test(path)) return path;
  return apiUrl(path);
}


const els = {
  sessionLabel: $("session-label"),
  sessionPill: $("session-pill"),
  modeLabel: $("mode-label"),
  youStatus: $("you-status"),
  youHoldings: $("you-holdings"),
  youPosition: $("you-position"),
  youEta: $("you-eta"),
  btnJoin: $("btn-join"),
  btnLeave: $("btn-leave"),
  joinStatus: $("join-status"),
  btnConnect: $("btn-connect"),
  walletChip: $("wallet-chip"),
  walletLabel: $("wallet-label"),
  npWallet: $("np-wallet"),
  npTime: $("np-time"),
  npStatus: $("np-status"),
  liveDot: $("live-dot"),
  queueCount: $("queue-count"),
  queueBody: $("queue-body"),
  streamScreen: $("stream-screen"),
  streamFrame: $("stream-frame"),
  streamBadge: $("stream-badge"),
  streamNote: $("stream-note"),
  streamOverlay: $("stream-overlay"),
  streamOverlayTitle: $("stream-overlay-title"),
  streamOverlaySub: $("stream-overlay-sub"),
  streamTip: $("stream-tip"),
  crewList: $("crew-list"),
};

let config = {
  sessionSeconds: 10,
  demoMode: true,
  stream: null,
  crew: null,
};
let wallet = null;
let lastState = null;
let busy = false;
/** @type {"view"|"control"|null} */
let streamMode = null;
let streamOnline = null;
let streamCheckBusy = false;

function shortAddr(addr) {
  if (!addr || addr.length < 10) return addr || "—";
  return `${addr.slice(0, 4)}…${addr.slice(-4)}`;
}

function setStatus(msg, kind = "") {
  els.joinStatus.textContent = msg || "";
  els.joinStatus.className = `status-line mono${kind ? " " + kind : ""}`;
}

function formatHoldings(ui) {
  const n = Number(ui);
  if (!Number.isFinite(n)) return "—";
  if (n === 0) return "0";
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}m`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(2)}k`;
  if (n >= 1) return n.toLocaleString(undefined, { maximumFractionDigits: 2 });
  return n.toPrecision(3);
}

function formatEta(seconds) {
  if (sessionIsUnlimited()) return "—";
  if (seconds == null || !Number.isFinite(Number(seconds))) return "—";
  const s = Math.max(0, Math.floor(Number(seconds) || 0));
  if (s === 0) return "~now";
  if (s < 60) return `~${s}s`;
  const m = Math.floor(s / 60);
  const r = s % 60;
  return r ? `~${m}m ${r}s` : `~${m}m`;
}

function formatCountdown(seconds) {
  if (sessionIsUnlimited() || seconds == null || Number(seconds) < 0) return "∞";
  const s = Math.max(0, Math.floor(Number(seconds) || 0));
  return `${s}s`;
}

function sessionIsUnlimited() {
  const n = Number(config.sessionSeconds);
  return !Number.isFinite(n) || n <= 0;
}

function sessionLabelText() {
  if (sessionIsUnlimited()) return "no limit (test)";
  return `${Number(config.sessionSeconds)}s sessions`;
}

function sessionPillText() {
  if (sessionIsUnlimited()) return "no limit (test)";
  return `${Number(config.sessionSeconds)}s`;
}


function offlineMessage() {
  return config.stream?.offlineMessage || "stream offline — agent computer not linked";
}

function applyConfig(cfg) {
  if (!cfg) return;
  config = { ...config, ...cfg };
  if (els.sessionLabel) els.sessionLabel.textContent = sessionLabelText();
  if (els.sessionPill) els.sessionPill.textContent = sessionPillText();
  if (els.modeLabel) {
    els.modeLabel.textContent = config.demoMode ? "demo mode" : "live";
  }
  if (els.streamNote && config.stream?.note) {
    els.streamNote.title = config.stream.note;
  }
  if (Array.isArray(config.crew) && els.crewList) {
    els.crewList.innerHTML = config.crew
      .map(
        (c) => `<li>
          <span class="crew-name">${c.name || c.id}</span>
          <span class="crew-role">${c.role || ""}</span>
        </li>`
      )
      .join("");
  }
  refreshConnectUi();
  refreshYouStatus();
  syncStreamFrame();
}

function findYou(state) {
  if (!wallet || !state) return { kind: "absent" };
  const np = state.nowPlaying;
  if (np && np.wallet === wallet) {
    return {
      kind: "playing",
      remainingSeconds: np.remainingSeconds,
      holdingsUi: np.holdingsUi,
    };
  }
  const q = (state.queue || []).find((row) => row.wallet === wallet);
  if (q) {
    return {
      kind: "queued",
      position: q.position,
      etaSeconds: q.etaSeconds,
      holdingsUi: q.holdingsUi,
    };
  }
  return { kind: "absent" };
}

function refreshYouStatus() {
  const you = findYou(lastState);
  if (!wallet) {
    els.youStatus.textContent = "not connected";
    els.youHoldings.textContent = "—";
    els.youPosition.textContent = "—";
    els.youEta.textContent = "—";
    els.btnLeave.hidden = true;
    return;
  }
  if (you.kind === "playing") {
    els.youStatus.textContent = "now playing";
    els.youHoldings.textContent = formatHoldings(you.holdingsUi);
    els.youPosition.textContent = "seat";
    els.youEta.textContent = formatCountdown(you.remainingSeconds);
    els.btnLeave.hidden = false;
    els.btnLeave.textContent = "leave seat";
  } else if (you.kind === "queued") {
    els.youStatus.textContent = "in queue";
    els.youHoldings.textContent = formatHoldings(you.holdingsUi);
    els.youPosition.textContent = `#${you.position}`;
    els.youEta.textContent = formatEta(you.etaSeconds);
    els.btnLeave.hidden = false;
    els.btnLeave.textContent = "leave queue";
  } else {
    els.youStatus.textContent = "ready";
    els.youHoldings.textContent = "—";
    els.youPosition.textContent = "—";
    els.youEta.textContent = "—";
    els.btnLeave.hidden = true;
  }
}

function refreshConnectUi() {
  const connected = Boolean(wallet);
  const you = findYou(lastState);
  syncStreamFrame();
  if (connected) {
    if (els.walletChip) els.walletChip.hidden = false;
    if (els.walletLabel) els.walletLabel.textContent = shortAddr(wallet);
    els.btnConnect.textContent = shortAddr(wallet);
    els.btnJoin.disabled = busy || you.kind === "playing" || you.kind === "queued";
    if (you.kind === "playing") {
      els.btnJoin.textContent = "you're playing";
    } else if (you.kind === "queued") {
      els.btnJoin.textContent = "already in queue";
    } else {
      els.btnJoin.textContent = "join queue";
    }
  } else {
    if (els.walletChip) els.walletChip.hidden = true;
    els.btnConnect.textContent = "connect";
    els.btnJoin.disabled = true;
    els.btnJoin.textContent = "connect to join";
  }
  refreshYouStatus();
}

function streamUrls() {
  const s = config.stream || {};
  let view =
    s.viewPath ||
    s.viewUrl ||
    "/stream/vnc.html?autoconnect=1&resize=scale&view_only=1&path=stream/";
  let control =
    s.controlPath ||
    s.controlUrl ||
    "/stream/vnc.html?autoconnect=1&resize=scale&path=stream/";
  // relative paths from config.stream must hit the agent backend, not Netlify
  view = absolutizePath(view);
  control = absolutizePath(control);
  return { view, control };
}

function desiredStreamMode(state) {
  const np = state?.nowPlaying;
  if (wallet && np && np.wallet === wallet) return "control";
  return "view";
}

function showStreamOffline(msg) {
  streamOnline = false;
  if (els.streamOverlay) {
    els.streamOverlay.hidden = false;
    if (els.streamOverlayTitle) {
      els.streamOverlayTitle.textContent = msg || offlineMessage();
    }
    if (els.streamOverlaySub) {
      els.streamOverlaySub.textContent = "waiting for live computer";
    }
  }
  if (els.streamFrame) {
    // Keep iframe blank so raw json/errors never flash
    if (els.streamFrame.getAttribute("src")) {
      els.streamFrame.removeAttribute("src");
    }
  }
}

function showStreamOnline() {
  streamOnline = true;
  if (els.streamOverlay) els.streamOverlay.hidden = true;
}

async function probeStream() {
  if (streamCheckBusy) return;
  streamCheckBusy = true;
  try {
    const controller = new AbortController();
    const t = setTimeout(() => controller.abort(), 2500);
    const res = await fetch(apiUrl("/stream/vnc.html"), {
      method: "HEAD",
      cache: "no-store",
      signal: controller.signal,
    });
    clearTimeout(t);
    if (!res.ok) {
      showStreamOffline(offlineMessage());
      return;
    }
    const wasOffline = streamOnline === false;
    showStreamOnline();
    if (wasOffline || !els.streamFrame?.getAttribute("src")) {
      streamMode = null; // force reload
      syncStreamFrame(true);
    }
  } catch {
    showStreamOffline(offlineMessage());
  } finally {
    streamCheckBusy = false;
  }
}

function syncStreamFrame(force = false) {
  if (!els.streamFrame) return;
  if (streamOnline === false) {
    showStreamOffline(offlineMessage());
    return;
  }
  const mode = desiredStreamMode(lastState);
  const { view, control } = streamUrls();
  const nextSrc = mode === "control" ? control : view;
  if (force || streamMode !== mode) {
    streamMode = mode;
    if (els.streamFrame.getAttribute("src") !== nextSrc) {
      els.streamFrame.setAttribute("src", nextSrc);
    }
  }
  if (els.streamBadge) {
    const controlling = mode === "control";
    els.streamBadge.textContent = controlling ? "controlling" : "watching";
    els.streamBadge.classList.toggle("controlling", controlling);
  }
  if (els.streamNote) {
    els.streamNote.textContent =
      mode === "control"
        ? "you are driving the agent computer — click the screen"
        : "watching live computer";
  }
}

function renderState(state) {
  lastState = state;
  if (state.config) applyConfig(state.config);
  else if (state.crew) applyConfig({ crew: state.crew });

  const np = state.nowPlaying;
  const sessionSecs = sessionIsUnlimited() ? null : (Number(config.sessionSeconds) || 15);
  if (np) {
    els.npStatus.textContent = "live";
    els.liveDot?.classList.add("on");
    els.npWallet.textContent = np.walletShort || shortAddr(np.wallet);
    els.npTime.textContent = formatCountdown(
      sessionIsUnlimited() ? null : (np.remainingSeconds ?? sessionSecs)
    );
  } else {
    els.npStatus.textContent = "idle";
    els.liveDot?.classList.remove("on");
    els.npWallet.textContent = "empty";
    els.npTime.textContent = "—";
  }
  syncStreamFrame();

  const queue = state.queue || [];
  els.queueCount.textContent = `${queue.length} waiting`;
  if (!queue.length) {
    els.queueBody.innerHTML =
      '<tr class="empty-row"><td colspan="4">queue empty — connect and join</td></tr>';
  } else {
    els.queueBody.innerHTML = queue
      .map(
        (q) => `<tr>
          <td>${q.position}</td>
          <td title="${q.wallet}">${q.walletShort || shortAddr(q.wallet)}</td>
          <td>${formatHoldings(q.holdingsUi)}</td>
          <td>${formatEta(q.etaSeconds)}</td>
        </tr>`
      )
      .join("");
  }
  refreshConnectUi();
}

async function fetchState() {
  const res = await fetch(apiUrl("/api/state"), { cache: "no-store" });
  if (!res.ok) throw new Error(`state ${res.status}`);
  const data = await res.json();
  renderState(data);
  return data;
}

function getProvider() {
  if (typeof window === "undefined") return null;
  if (window.solana?.isPhantom) return window.solana;
  if (window.solana) return window.solana;
  if (window.phantom?.solana?.isPhantom) return window.phantom.solana;
  return null;
}

async function connectWallet() {
  const provider = getProvider();
  if (!provider) {
    setStatus("phantom not found — using local demo wallet", "ok");
    if (!wallet) {
      wallet = "Demo1111111111111111111111111111111111111";
      refreshConnectUi();
    }
    return;
  }
  try {
    const resp = await provider.connect();
    wallet = resp.publicKey?.toString?.() || provider.publicKey?.toString?.();
    if (!wallet) throw new Error("no public key");
    refreshConnectUi();
    setStatus("wallet connected", "ok");
  } catch (err) {
    setStatus(`connect failed: ${err.message || err}`, "err");
  }
}

async function joinQueue() {
  if (!wallet || busy) return;
  busy = true;
  refreshConnectUi();
  setStatus("joining queue…");
  try {
    const res = await fetch(apiUrl("/api/queue/join"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ wallet }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "join failed");
    if (data.state) renderState(data.state);
    else await fetchState();
    if (data.status === "playing") {
      setStatus(data.message || "you have the seat", "ok");
    } else if (data.status === "queued") {
      const pos = data.position != null ? ` (#${data.position})` : "";
      setStatus((data.message || "joined the queue") + pos, "ok");
    } else {
      setStatus(data.message || "ok", "ok");
    }
  } catch (err) {
    setStatus(String(err.message || err), "err");
  } finally {
    busy = false;
    refreshConnectUi();
  }
}

async function leaveQueue() {
  if (!wallet || busy) return;
  busy = true;
  refreshConnectUi();
  setStatus("leaving…");
  try {
    const res = await fetch(apiUrl("/api/queue/leave"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ wallet }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "leave failed");
    if (data.state) renderState(data.state);
    else await fetchState();
    setStatus(data.message || "left", "ok");
  } catch (err) {
    setStatus(String(err.message || err), "err");
  } finally {
    busy = false;
    refreshConnectUi();
  }
}

function bind() {
  els.btnConnect.addEventListener("click", connectWallet);
  els.btnJoin.addEventListener("click", joinQueue);
  els.btnLeave.addEventListener("click", leaveQueue);

  const provider = getProvider();
  if (provider) {
    provider.on?.("accountChanged", (key) => {
      wallet = key ? key.toString() : null;
      refreshConnectUi();
    });
    provider.on?.("disconnect", () => {
      wallet = null;
      refreshConnectUi();
    });
    if (provider.isConnected && provider.publicKey) {
      wallet = provider.publicKey.toString();
      refreshConnectUi();
    }
  }
}

async function boot() {
  applyConfig(config);
  bind();
  showStreamOffline(offlineMessage());
  try {
    await fetchState();
  } catch (err) {
    setStatus(`failed to load state: ${err.message || err}`, "err");
  }
  probeStream();
  setInterval(() => {
    fetchState().catch(() => {});
  }, 2500);
  setInterval(() => {
    probeStream();
  }, 8000);
}

boot();
