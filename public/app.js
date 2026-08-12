const $ = (id) => document.getElementById(id);

const API_BASE = (window.PLAY_API_BASE || "").replace(/\/$/, "");
const STORAGE_CLIENT = "play-arcade.clientId";
const STORAGE_NAME = "play-arcade.displayName";
const TWITTER_URL = "https://x.com/botcomputerxai";

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
  youPosition: $("you-position"),
  youEta: $("you-eta"),
  btnJoin: $("btn-join"),
  btnLeave: $("btn-leave"),
  joinStatus: $("join-status"),
  displayName: $("display-name"),
  youChip: $("you-chip"),
  youLabel: $("you-label"),
  npName: $("np-name"),
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
  sessionUnlimited: false,
  stream: null,
  crew: null,
};
let clientId = null;
let lastState = null;
let busy = false;
/** @type {"view"|"control"|null} */
let streamMode = null;
let streamOnline = null;
let streamCheckBusy = false;

function randomId() {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

function ensureClientId() {
  try {
    let id = localStorage.getItem(STORAGE_CLIENT);
    if (!id || !/^[A-Za-z0-9_-]{8,64}$/.test(id)) {
      id = randomId();
      localStorage.setItem(STORAGE_CLIENT, id);
    }
    return id;
  } catch {
    return randomId();
  }
}

function defaultGuestName(id) {
  const suffix = (id || "guest").slice(-4);
  return `guest-${suffix}`;
}

function loadSavedName() {
  try {
    return (localStorage.getItem(STORAGE_NAME) || "").trim();
  } catch {
    return "";
  }
}

function saveName(name) {
  try {
    localStorage.setItem(STORAGE_NAME, name || "");
  } catch {
    /* ignore */
  }
}

function currentName() {
  const typed = (els.displayName?.value || "").trim();
  if (typed) return typed.slice(0, 32);
  return defaultGuestName(clientId);
}

function setStatus(msg, kind = "") {
  els.joinStatus.textContent = msg || "";
  els.joinStatus.className = `status-line mono${kind ? " " + kind : ""}`;
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
  if (config.sessionUnlimited) return true;
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

function renderCrew(crew) {
  if (!els.crewList || !Array.isArray(crew)) return;
  els.crewList.innerHTML = crew
    .map((c) => {
      const id = c.id || "";
      const name = c.name || id;
      const role = c.role || "";
      if (id === "twitter" || /twitter|x\.com/i.test(name + role)) {
        return `<li>
          <a class="crew-name" href="${TWITTER_URL}" target="_blank" rel="noopener noreferrer">${name}</a>
          <span class="crew-role">@botcomputerxai</span>
        </li>`;
      }
      return `<li>
        <span class="crew-name">${name}</span>
        <span class="crew-role">${role}</span>
      </li>`;
    })
    .join("");
}

function applyConfig(cfg) {
  if (!cfg) return;
  config = { ...config, ...cfg };
  if (els.sessionLabel) els.sessionLabel.textContent = sessionLabelText();
  if (els.sessionPill) els.sessionPill.textContent = sessionPillText();
  if (els.modeLabel) {
    els.modeLabel.textContent = "guest fifo";
  }
  if (els.streamNote && config.stream?.note) {
    els.streamNote.title = config.stream.note;
  }
  if (Array.isArray(config.crew)) renderCrew(config.crew);
  refreshYouUi();
  syncStreamFrame();
}

function findYou(state) {
  if (!clientId || !state) return { kind: "absent" };
  const np = state.nowPlaying;
  if (np && np.clientId === clientId) {
    return {
      kind: "playing",
      remainingSeconds: np.remainingSeconds,
      name: np.name,
    };
  }
  const q = (state.queue || []).find((row) => row.clientId === clientId);
  if (q) {
    return {
      kind: "queued",
      position: q.position,
      etaSeconds: q.etaSeconds,
      name: q.name,
    };
  }
  return { kind: "absent" };
}

function refreshYouUi() {
  const you = findYou(lastState);
  const name = currentName();
  if (els.youLabel) els.youLabel.textContent = name;
  if (you.kind === "playing") {
    els.youStatus.textContent = "now playing";
    els.youPosition.textContent = "seat";
    els.youEta.textContent = formatCountdown(you.remainingSeconds);
    els.btnLeave.hidden = false;
    els.btnLeave.textContent = "leave seat";
    els.btnJoin.disabled = busy;
    els.btnJoin.textContent = "you're playing";
  } else if (you.kind === "queued") {
    els.youStatus.textContent = "in queue";
    els.youPosition.textContent = `#${you.position}`;
    els.youEta.textContent = formatEta(you.etaSeconds);
    els.btnLeave.hidden = false;
    els.btnLeave.textContent = "leave queue";
    els.btnJoin.disabled = busy;
    els.btnJoin.textContent = "already in queue";
  } else {
    els.youStatus.textContent = "ready";
    els.youPosition.textContent = "—";
    els.youEta.textContent = "—";
    els.btnLeave.hidden = true;
    els.btnJoin.disabled = busy;
    els.btnJoin.textContent = "join";
  }
  syncStreamFrame();
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
  if (clientId && np && np.clientId === clientId) return "control";
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
  const sessionSecs = sessionIsUnlimited()
    ? null
    : Number(config.sessionSeconds) || 15;
  if (np) {
    els.npStatus.textContent = "live";
    els.liveDot?.classList.add("on");
    els.npName.textContent = np.name || "player";
    els.npTime.textContent = formatCountdown(
      sessionIsUnlimited() ? null : np.remainingSeconds ?? sessionSecs
    );
  } else {
    els.npStatus.textContent = "idle";
    els.liveDot?.classList.remove("on");
    els.npName.textContent = "empty";
    els.npTime.textContent = "—";
  }
  syncStreamFrame();

  const queue = state.queue || [];
  els.queueCount.textContent = `${queue.length} waiting`;
  if (!queue.length) {
    els.queueBody.innerHTML =
      '<tr class="empty-row"><td colspan="3">queue empty — join to play</td></tr>';
  } else {
    els.queueBody.innerHTML = queue
      .map(
        (q) => `<tr>
          <td>${q.position}</td>
          <td title="${q.clientId || ""}">${q.name || "guest"}</td>
          <td>${formatEta(q.etaSeconds)}</td>
        </tr>`
      )
      .join("");
  }
  refreshYouUi();
}

async function fetchState() {
  const res = await fetch(apiUrl("/api/state"), { cache: "no-store" });
  if (!res.ok) throw new Error(`state ${res.status}`);
  const data = await res.json();
  renderState(data);
  return data;
}

async function joinQueue() {
  if (!clientId || busy) return;
  busy = true;
  refreshYouUi();
  const name = currentName();
  saveName(name);
  if (els.displayName && !els.displayName.value.trim()) {
    els.displayName.value = name;
  }
  setStatus("joining queue…");
  try {
    const res = await fetch(apiUrl("/api/queue/join"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ clientId, name }),
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
    refreshYouUi();
  }
}

async function leaveQueue() {
  if (!clientId || busy) return;
  busy = true;
  refreshYouUi();
  setStatus("leaving…");
  try {
    const res = await fetch(apiUrl("/api/queue/leave"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ clientId }),
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
    refreshYouUi();
  }
}

function bind() {
  els.btnJoin.addEventListener("click", joinQueue);
  els.btnLeave.addEventListener("click", leaveQueue);
  els.displayName?.addEventListener("change", () => {
    saveName((els.displayName.value || "").trim());
    refreshYouUi();
  });
  els.displayName?.addEventListener("input", () => {
    refreshYouUi();
  });
  els.displayName?.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") {
      ev.preventDefault();
      joinQueue();
    }
  });
}

async function boot() {
  clientId = ensureClientId();
  const saved = loadSavedName();
  if (els.displayName) {
    els.displayName.value = saved || defaultGuestName(clientId);
    els.displayName.placeholder = defaultGuestName(clientId);
  }
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
