const $ = (id) => document.getElementById(id);

const API_BASE = (window.PLAY_API_BASE || "").replace(/\/$/, "");
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
  youHoldings: $("you-holdings"),
  btnJoin: $("btn-join"),
  btnLeave: $("btn-leave"),
  btnWallet: $("btn-wallet"),
  walletAddr: $("wallet-addr"),
  joinStatus: $("join-status"),
  displayName: $("display-name"),
  youChip: $("you-chip"),
  youLabel: $("you-label"),
  npName: $("np-name"),
  npHoldings: $("np-holdings"),
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
  btnAudio: $("btn-audio"),
  crewList: $("crew-list"),
};

let config = {
  sessionSeconds: 15,
  sessionUnlimited: false,
  stream: null,
  crew: null,
  demoMode: false,
  tokenMint: "",
};
/** @type {string|null} */
let wallet = null;
let lastState = null;
let busy = false;
/** @type {"view"|"control"|null} */
let streamMode = null;
let streamOnline = null;
let streamCheckBusy = false;
let streamBlobUrl = null;
let streamLoadToken = 0;
let audioWanted = false;
let audioBound = false;
/** @type {AudioContext|null} */
let audioCtx = null;
/** @type {WebSocket|null} */
let audioWs = null;
/** @type {ScriptProcessorNode|null} */
let audioProcessor = null;
/** @type {Float32Array[]} */
let audioPcmQueue = [];
let audioPcmQueuedSamples = 0;
let audioBridgeRate = 24000;
let audioChannels = 1;
const AUDIO_PCM_MAX_SAMPLES = 24000 * 3; // ~3s cap


function shortWallet(addr) {
  if (!addr) return "-";
  if (addr.length <= 10) return addr;
  return `${addr.slice(0, 4)}…${addr.slice(-4)}`;
}

function formatHoldings(n) {
  if (n == null || !Number.isFinite(Number(n))) return "-";
  const v = Number(n);
  if (v >= 1_000_000) return `${(v / 1_000_000).toFixed(2)}m`;
  if (v >= 10_000) return `${Math.round(v).toLocaleString("en-US")}`;
  if (v >= 100) return v.toFixed(1);
  if (v >= 1) return v.toFixed(2);
  if (v === 0) return "0";
  return v.toFixed(4);
}

function defaultGuestName(addr) {
  const suffix = (addr || "guest").slice(-4);
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
  if (wallet) return defaultGuestName(wallet);
  return "";
}

function setStatus(msg, kind = "") {
  els.joinStatus.textContent = msg || "";
  els.joinStatus.className = `status-line mono${kind ? " " + kind : ""}`;
}

function formatEta(seconds) {
  if (sessionIsUnlimited()) return "-";
  if (seconds == null || !Number.isFinite(Number(seconds))) return "-";
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
  return config.stream?.offlineMessage || "stream offline - agent computer not linked";
}

function getProvider() {
  const sol = window.solana;
  if (sol?.isPhantom) return sol;
  if (sol) return sol;
  return null;
}

function refreshWalletUi() {
  if (!els.walletAddr || !els.btnWallet) return;
  if (wallet) {
    els.walletAddr.textContent = shortWallet(wallet);
    els.walletAddr.title = wallet;
    els.walletAddr.classList.add("connected");
    els.btnWallet.textContent = "disconnect";
  } else {
    els.walletAddr.textContent = "not connected";
    els.walletAddr.removeAttribute("title");
    els.walletAddr.classList.remove("connected");
    els.btnWallet.textContent = "connect wallet";
  }
  if (els.displayName && wallet && !els.displayName.value.trim()) {
    els.displayName.placeholder = defaultGuestName(wallet);
  }
}

async function connectWallet() {
  const provider = getProvider();
  if (!provider) {
    setStatus("phantom not found - install phantom or open in a wallet browser", "err");
    return;
  }
  try {
    const res = await provider.connect();
    const key = res?.publicKey?.toString?.() || provider.publicKey?.toString?.();
    if (!key) throw new Error("no public key");
    wallet = key;
    refreshWalletUi();
    refreshYouUi();
    setStatus("wallet connected", "ok");
  } catch (err) {
    setStatus(String(err.message || err || "wallet connect failed"), "err");
  }
}

async function disconnectWallet() {
  const provider = getProvider();
  try {
    if (provider?.disconnect) await provider.disconnect();
  } catch {
    /* ignore */
  }
  wallet = null;
  refreshWalletUi();
  refreshYouUi();
  setStatus("wallet disconnected");
}

async function toggleWallet() {
  if (wallet) await disconnectWallet();
  else await connectWallet();
}

function bindWalletEvents() {
  const provider = getProvider();
  if (!provider?.on) return;
  provider.on("accountChanged", (publicKey) => {
    if (publicKey) {
      wallet = publicKey.toString();
    } else {
      wallet = null;
    }
    refreshWalletUi();
    refreshYouUi();
    syncStreamFrame(true);
  });
  provider.on("disconnect", () => {
    wallet = null;
    refreshWalletUi();
    refreshYouUi();
    syncStreamFrame(true);
  });
}

async function trySilentWallet() {
  const provider = getProvider();
  if (!provider) return;
  try {
    if (provider.isConnected && provider.publicKey) {
      wallet = provider.publicKey.toString();
      refreshWalletUi();
      return;
    }
    // some providers support onlyIfTrusted
    const res = await provider.connect({ onlyIfTrusted: true });
    const key = res?.publicKey?.toString?.() || provider.publicKey?.toString?.();
    if (key) {
      wallet = key;
      refreshWalletUi();
    }
  } catch {
    /* user has not trusted yet */
  }
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
    els.modeLabel.textContent = "holdings queue";
  }
  if (els.streamNote && config.stream?.note) {
    els.streamNote.title = config.stream.note;
  }
  if (Array.isArray(config.crew)) renderCrew(config.crew);
  refreshYouUi();
  syncStreamFrame();
}

function findYou(state) {
  if (!wallet || !state) return { kind: "absent" };
  const np = state.nowPlaying;
  if (np && np.wallet === wallet) {
    return {
      kind: "playing",
      remainingSeconds: np.remainingSeconds,
      name: np.name,
      holdings: np.holdings,
    };
  }
  const q = (state.queue || []).find((row) => row.wallet === wallet);
  if (q) {
    return {
      kind: "queued",
      position: q.position,
      etaSeconds: q.etaSeconds,
      name: q.name,
      holdings: q.holdings,
    };
  }
  return { kind: "absent" };
}

function refreshYouUi() {
  const you = findYou(lastState);
  const label = wallet
    ? `${currentName() || defaultGuestName(wallet)} (${shortWallet(wallet)})`
    : "not connected";
  if (els.youLabel) els.youLabel.textContent = label;

  if (!wallet) {
    els.youStatus.textContent = "connect wallet";
    els.youPosition.textContent = "-";
    els.youEta.textContent = "-";
    if (els.youHoldings) els.youHoldings.textContent = "-";
    els.btnLeave.hidden = true;
    els.btnJoin.disabled = busy;
    els.btnJoin.textContent = "join";
    syncStreamFrame();
    return;
  }

  if (you.kind === "playing") {
    els.youStatus.textContent = "now playing";
    els.youPosition.textContent = "seat";
    els.youEta.textContent = formatCountdown(you.remainingSeconds);
    if (els.youHoldings) els.youHoldings.textContent = formatHoldings(you.holdings);
    els.btnLeave.hidden = false;
    els.btnLeave.textContent = "leave seat";
    els.btnJoin.disabled = busy;
    els.btnJoin.textContent = "you're playing";
  } else if (you.kind === "queued") {
    els.youStatus.textContent = "in queue";
    els.youPosition.textContent = `#${you.position}`;
    els.youEta.textContent = formatEta(you.etaSeconds);
    if (els.youHoldings) els.youHoldings.textContent = formatHoldings(you.holdings);
    els.btnLeave.hidden = false;
    els.btnLeave.textContent = "leave queue";
    els.btnJoin.disabled = busy;
    els.btnJoin.textContent = "already in queue";
  } else {
    els.youStatus.textContent = "ready";
    els.youPosition.textContent = "-";
    els.youEta.textContent = "-";
    if (els.youHoldings) els.youHoldings.textContent = "-";
    els.btnLeave.hidden = true;
    els.btnJoin.disabled = busy;
    els.btnJoin.textContent = "join";
  }
  syncStreamFrame();
}

function streamApiOrigin() {
  try {
    return new URL(API_BASE || window.location.origin);
  } catch {
    return new URL(window.location.origin);
  }
}

function streamAssetBase() {
  return `${streamApiOrigin().origin}/stream`;
}


function audioWsUrl() {
  const path = config.stream?.audio?.wsPath || "/audio/ws";
  const abs = absolutizePath(path);
  try {
    const u = new URL(abs, window.location.href);
    u.protocol = u.protocol === "https:" ? "wss:" : "ws:";
    return u.toString();
  } catch {
    const origin = streamApiOrigin();
    const proto = origin.protocol === "https:" ? "wss:" : "ws:";
    const p = path.startsWith("/") ? path : `/${path}`;
    return `${proto}//${origin.host}${p}`;
  }
}

function syncAudioButton() {
  if (!els.btnAudio) return;
  const on = Boolean(audioWanted);
  els.btnAudio.textContent = on ? "mute" : "unmute";
  els.btnAudio.setAttribute("aria-pressed", on ? "true" : "false");
  els.btnAudio.classList.toggle("on", on);
  els.btnAudio.title = on
    ? "mute desktop audio"
    : "desktop audio starts muted; click to unmute";
}

function clearAudioPcmQueue() {
  audioPcmQueue = [];
  audioPcmQueuedSamples = 0;
}

function enqueuePcmInt16(int16) {
  if (!int16 || !int16.length) return;
  const f32 = new Float32Array(int16.length);
  for (let i = 0; i < int16.length; i++) {
    f32[i] = int16[i] / 32768;
  }
  audioPcmQueue.push(f32);
  audioPcmQueuedSamples += f32.length;
  while (audioPcmQueuedSamples > AUDIO_PCM_MAX_SAMPLES && audioPcmQueue.length > 1) {
    const dropped = audioPcmQueue.shift();
    audioPcmQueuedSamples -= dropped.length;
  }
}

function pullPcmSamples(out) {
  let offset = 0;
  while (offset < out.length && audioPcmQueue.length) {
    const head = audioPcmQueue[0];
    const need = out.length - offset;
    if (head.length <= need) {
      out.set(head, offset);
      offset += head.length;
      audioPcmQueuedSamples -= head.length;
      audioPcmQueue.shift();
    } else {
      out.set(head.subarray(0, need), offset);
      audioPcmQueue[0] = head.subarray(need);
      audioPcmQueuedSamples -= need;
      offset += need;
    }
  }
  if (offset < out.length) {
    out.fill(0, offset);
  }
}

function stopDeskAudio() {
  if (audioWs) {
    try {
      audioWs.onopen = null;
      audioWs.onmessage = null;
      audioWs.onerror = null;
      audioWs.onclose = null;
      audioWs.close();
    } catch {
      /* ignore */
    }
    audioWs = null;
  }
  if (audioProcessor) {
    try {
      audioProcessor.disconnect();
    } catch {
      /* ignore */
    }
    audioProcessor.onaudioprocess = null;
    audioProcessor = null;
  }
  if (audioCtx) {
    const ctx = audioCtx;
    audioCtx = null;
    try {
      ctx.close();
    } catch {
      try {
        ctx.suspend();
      } catch {
        /* ignore */
      }
    }
  }
  clearAudioPcmQueue();
}

async function startDeskAudio() {
  stopDeskAudio();
  const AC = window.AudioContext || window.webkitAudioContext;
  if (!AC) {
    audioWanted = false;
    syncAudioButton();
    console.warn("desk audio: AudioContext unavailable");
    return;
  }
  audioBridgeRate = Number(config.stream?.audio?.sampleRate) || 24000;
  audioChannels = Number(config.stream?.audio?.channels) || 1;
  try {
    audioCtx = new AC({ sampleRate: audioBridgeRate });
  } catch {
    audioCtx = new AC();
  }
  try {
    if (audioCtx.state === "suspended") {
      await audioCtx.resume();
    }
  } catch (err) {
    audioWanted = false;
    syncAudioButton();
    stopDeskAudio();
    console.warn("desk audio AudioContext resume blocked", err);
    return;
  }

  const bufferSize = 4096;
  const proc = audioCtx.createScriptProcessor(bufferSize, 0, 1);
  proc.onaudioprocess = (ev) => {
    const out = ev.outputBuffer.getChannelData(0);
    const srcRate = audioBridgeRate || 24000;
    const dstRate = audioCtx?.sampleRate || srcRate;
    if (srcRate === dstRate) {
      pullPcmSamples(out);
      return;
    }
    // Simple linear resample from bridge rate into AudioContext rate.
    const needSrc = Math.ceil((out.length * srcRate) / dstRate) + 2;
    const src = new Float32Array(needSrc);
    pullPcmSamples(src);
    const ratio = srcRate / dstRate;
    for (let i = 0; i < out.length; i++) {
      const pos = i * ratio;
      const i0 = Math.floor(pos);
      const i1 = Math.min(i0 + 1, src.length - 1);
      const frac = pos - i0;
      out[i] = src[i0] * (1 - frac) + src[i1] * frac;
    }
  };
  proc.connect(audioCtx.destination);
  audioProcessor = proc;

  const wsUrl = audioWsUrl();
  let ws;
  try {
    ws = new WebSocket(wsUrl);
  } catch (err) {
    audioWanted = false;
    syncAudioButton();
    stopDeskAudio();
    console.warn("desk audio ws construct failed", err);
    return;
  }
  ws.binaryType = "arraybuffer";
  audioWs = ws;

  ws.onmessage = (ev) => {
    if (!audioWanted) return;
    if (typeof ev.data === "string") {
      try {
        const msg = JSON.parse(ev.data);
        if (msg && msg.type === "hello") {
          if (msg.sampleRate) {
            audioBridgeRate = Number(msg.sampleRate) || audioBridgeRate;
            if (config.stream?.audio) {
              config.stream.audio.sampleRate = audioBridgeRate;
            }
          }
          if (msg.channels) audioChannels = Number(msg.channels) || 1;
        }
      } catch {
        /* ignore non-json text */
      }
      return;
    }
    const buf = ev.data;
    if (!(buf instanceof ArrayBuffer)) return;
    enqueuePcmInt16(new Int16Array(buf));
  };

  ws.onerror = () => {
    console.warn("desk audio ws error");
  };

  ws.onclose = () => {
    if (audioWs === ws) audioWs = null;
    if (!audioWanted) return;
    // Retry shortly; bridge or tunnel may be restarting.
    setTimeout(() => {
      if (audioWanted) startDeskAudio();
    }, 1200);
  };
}

async function toggleDeskAudio() {
  audioWanted = !audioWanted;
  syncAudioButton();
  if (audioWanted) {
    if (streamOnline === false) {
      audioWanted = false;
      syncAudioButton();
      return;
    }
    await startDeskAudio();
  } else {
    stopDeskAudio();
  }
}

function bindAudioControls() {
  if (audioBound) return;
  audioBound = true;
  els.btnAudio?.addEventListener("click", () => {
    toggleDeskAudio();
  });
  syncAudioButton();
}

function revokeStreamBlob() {
  if (streamBlobUrl) {
    URL.revokeObjectURL(streamBlobUrl);
    streamBlobUrl = null;
  }
}

function buildStreamParams(mode) {
  const origin = streamApiOrigin();
  const isHttps = origin.protocol === "https:";
  const port = origin.port || (isHttps ? "443" : "80");
  const params = new URLSearchParams();
  params.set("autoconnect", "1");
  params.set("reconnect", "1");
  params.set("resize", "scale");
  params.set("show_dot", "0");
  params.set("view_clip", "0");
  params.set("host", origin.hostname);
  params.set("port", String(port));
  params.set("path", "stream/");
  params.set("encrypt", isHttps ? "1" : "0");
  if (mode === "view") params.set("view_only", "1");
  return params;
}

/** Rewrite relative noVNC asset URLs so a blob: document can load them from the API. */
function absolutizeStreamHtml(html, assetBase) {
  const base = assetBase.replace(/\/$/, "");
  html = html.replace(
    /\b(src|href)="(?!https?:\/\/|data:|blob:|\/\/|#)([^"]+)"/gi,
    (_m, attr, url) => {
      if (url.startsWith("/")) {
        return `${attr}="${streamApiOrigin().origin}${url}"`;
      }
      const cleaned = url.replace(/^\.\//, "");
      return `${attr}="${base}/${cleaned}"`;
    }
  );
  // Inline module imports: from "./app/ui.js" / from './core/...'
  html = html.replace(
    /(\bfrom\s*)(["'])\.\/([^"']+)\2/g,
    (_m, pref, q, rel) => `${pref}${q}${base}/${rel}${q}`
  );
  // fetch('./defaults.json') used by vnc.html bootstrap
  html = html.replace(
    /(fetch\s*\(\s*)(["'])\.\/([^"']+)\2/g,
    (_m, pref, q, rel) => `${pref}${q}${base}/${rel}${q}`
  );
  return html;
}

/** Force host/port/path/encrypt inside the fetched HTML (blob pages lose query params). */
function injectStreamConnectSettings(html, mode) {
  const origin = streamApiOrigin();
  const isHttps = origin.protocol === "https:";
  const port = origin.port || (isHttps ? "443" : "80");
  const viewOnly = mode === "view";
  const base = streamAssetBase().replace(/\/$/, "") + "/";
  const boot = `
    <base href="${base}">
    <script>
      // Play-site blob loader: wallet extensions skip blob: documents.
      // noVNC reads connect settings from defaults/mandatory (query is unavailable on blob:).
      window.PLAY_STREAM_WS = ${JSON.stringify(
        `${isHttps ? "wss" : "ws"}://${origin.hostname}:${port}/stream/`
      )};
    </script>
`;
  if (/<head[^>]*>/i.test(html)) {
    html = html.replace(/<head[^>]*>/i, (m) => m + boot);
  } else {
    html = boot + html;
  }
  const inject = `
        defaults['host'] = ${JSON.stringify(origin.hostname)};
        defaults['port'] = ${JSON.stringify(String(port))};
        defaults['path'] = 'stream/';
        defaults['encrypt'] = ${isHttps ? "true" : "false"};
        defaults['autoconnect'] = true;
        defaults['reconnect'] = true;
        defaults['resize'] = 'scale';
        defaults['show_dot'] = false;
        defaults['view_clip'] = false;
        defaults['view_only'] = ${viewOnly ? "true" : "false"};
        mandatory['host'] = defaults['host'];
        mandatory['port'] = defaults['port'];
        mandatory['path'] = defaults['path'];
        mandatory['encrypt'] = defaults['encrypt'];
        mandatory['resize'] = defaults['resize'];
        mandatory['show_dot'] = defaults['show_dot'];
        mandatory['view_clip'] = defaults['view_clip'];
        mandatory['view_only'] = defaults['view_only'];
`;
  if (html.includes("UI.start(")) {
    html = html.replace("UI.start(", `${inject}\n        UI.start(`);
  }
  html = injectStreamViewLock(html);
  return html;
}

/** Hide noVNC chrome; keep black letterbox bars (scale, no stretch). */
function injectStreamViewLock(html) {
  const lock = `
    <style id="play-stream-lock">
      html, body {
        margin: 0 !important;
        padding: 0 !important;
        overflow: hidden !important;
        width: 100% !important;
        height: 100% !important;
        background: #000 !important;
        overscroll-behavior: none !important;
        touch-action: none !important;
      }
      #noVNC_control_bar,
      #noVNC_control_bar_anchor,
      #noVNC_control_bar_handle,
      #noVNC_control_bar_hint,
      #noVNC_status,
      #noVNC_power_button,
      #noVNC_clipboard_button,
      #noVNC_settings_button,
      #noVNC_fullscreen_button,
      #noVNC_extra_keys_button,
      #noVNC_view_drag_button,
      #noVNC_mobile_buttons,
      #noVNC_modifiers,
      #noVNC_connect_button,
      #noVNC_disconnect_button,
      .noVNC_panel,
      #noVNC_credentials_dlg {
        display: none !important;
        visibility: hidden !important;
        pointer-events: none !important;
      }
      #noVNC_container,
      #noVNC_screen,
      #noVNC_canvas_area {
        margin: 0 !important;
        padding: 0 !important;
        overflow: hidden !important;
        width: 100% !important;
        height: 100% !important;
        max-width: 100% !important;
        max-height: 100% !important;
        border: 0 !important;
        border-radius: 0 !important;
        border-bottom-right-radius: 0 !important;
        border-bottom-left-radius: 0 !important;
        border-top-right-radius: 0 !important;
        border-top-left-radius: 0 !important;
        background: #000 !important;
      }
      #noVNC_container canvas,
      #noVNC_screen canvas,
      canvas {
        display: block !important;
        margin: 0 auto !important;
        /* let noVNC scale mode size the canvas; do not stretch */
        max-width: 100% !important;
        max-height: 100% !important;
        object-fit: contain !important;
      }
      #noVNC_transition,
      #noVNC_transition_text,
      #noVNC_bell,
      #noVNC_fallback_error {
        display: none !important;
        visibility: hidden !important;
      }
    </style>
    <script id="play-stream-lock-js">
      (function () {
        function harden() {
          try {
            document.documentElement.style.overflow = "hidden";
            if (document.body) document.body.style.overflow = "hidden";
            ["noVNC_control_bar","noVNC_control_bar_anchor","noVNC_status",
             "noVNC_control_bar_hint","noVNC_power_button","noVNC_clipboard_button",
             "noVNC_settings_button","noVNC_view_drag_button"].forEach(function (id) {
              var el = document.getElementById(id);
              if (el) { el.style.display = "none"; el.hidden = true; }
            });
          } catch (e) {}
        }
        function blockParentScroll(ev) {
          if (ev.cancelable && (ev.type === "wheel" || ev.type === "touchmove")) {
            ev.preventDefault();
          }
        }
        ["wheel","touchmove"].forEach(function (type) {
          window.addEventListener(type, blockParentScroll, { passive: false, capture: true });
        });
        if (document.readyState === "loading") {
          document.addEventListener("DOMContentLoaded", harden);
        } else {
          harden();
        }
        setInterval(harden, 1500);
      })();
    </script>
`;
  if (/<head[^>]*>/i.test(html)) {
    return html.replace(/<head[^>]*>/i, (m) => m + lock);
  }
  return lock + html;
}

async function loadStreamFrame(mode) {
  if (!els.streamFrame) return;
  const token = ++streamLoadToken;
  const params = buildStreamParams(mode);
  const fetchUrl = apiUrl(`/stream/vnc.html?${params.toString()}`);
  const assetBase = streamAssetBase();
  try {
    const res = await fetch(fetchUrl, { cache: "no-store" });
    if (!res.ok) throw new Error(`stream html ${res.status}`);
    let html = await res.text();
    html = absolutizeStreamHtml(html, assetBase);
    html = injectStreamConnectSettings(html, mode);
    if (token !== streamLoadToken) return;
    const blob = new Blob([html], { type: "text/html" });
    const blobUrl = URL.createObjectURL(blob);
    // Hash params: noVNC also reads connect settings from location.hash
    const nextSrc = `${blobUrl}#${params.toString()}`;
    revokeStreamBlob();
    streamBlobUrl = blobUrl;
    streamMode = mode;
    els.streamFrame.setAttribute("src", nextSrc);
  } catch {
    if (token !== streamLoadToken) return;
    showStreamOffline(offlineMessage());
  }
}

function desiredStreamMode(state) {
  const np = state?.nowPlaying;
  if (wallet && np && np.wallet === wallet) return "control";
  return "view";
}

function showStreamOffline(msg) {
  streamOnline = false;
  streamMode = null;
  if (audioWanted) {
    // Keep wanted=true so returning online can resume after unmute gesture already granted.
    stopDeskAudio();
  }
  revokeStreamBlob();
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
  if (audioWanted && !audioWs) {
    startDeskAudio();
  }
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
  if (force || streamMode !== mode) {
    streamMode = mode;
    loadStreamFrame(mode);
  }
  if (els.streamBadge) {
    const controlling = mode === "control";
    els.streamBadge.textContent = controlling ? "controlling" : "watching";
    els.streamBadge.classList.toggle("controlling", controlling);
  }
  if (els.streamNote) {
    els.streamNote.textContent =
      mode === "control"
        ? "you are driving the agent computer - click the screen"
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
    els.npName.textContent = np.name || shortWallet(np.wallet) || "player";
    if (els.npHoldings) {
      els.npHoldings.textContent = formatHoldings(np.holdings);
      els.npHoldings.title = np.wallet || "";
    }
    els.npTime.textContent = formatCountdown(
      sessionIsUnlimited() ? null : np.remainingSeconds ?? sessionSecs
    );
  } else {
    els.npStatus.textContent = "idle";
    els.liveDot?.classList.remove("on");
    els.npName.textContent = "empty";
    if (els.npHoldings) {
      els.npHoldings.textContent = "-";
      els.npHoldings.removeAttribute("title");
    }
    els.npTime.textContent = "-";
  }
  syncStreamFrame();

  const queue = state.queue || [];
  els.queueCount.textContent = `${queue.length} waiting`;
  if (!queue.length) {
    els.queueBody.innerHTML =
      '<tr class="empty-row"><td colspan="4">queue empty - connect wallet and join</td></tr>';
  } else {
    els.queueBody.innerHTML = queue
      .map(
        (q) => `<tr>
          <td>${q.position}</td>
          <td title="${q.wallet || ""}">${q.name || shortWallet(q.wallet)}</td>
          <td>${formatHoldings(q.holdings)}</td>
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
  if (busy) return;
  if (!wallet) {
    setStatus("connect wallet to join", "err");
    await connectWallet();
    if (!wallet) return;
  }
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
      body: JSON.stringify({ wallet, name }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "join failed");
    if (data.state) renderState(data.state);
    else await fetchState();
    if (data.status === "playing") {
      setStatus(
        `${data.message || "you have the seat"} · holdings ${formatHoldings(data.holdings)}`,
        "ok"
      );
    } else if (data.status === "queued") {
      const pos = data.position != null ? ` (#${data.position})` : "";
      setStatus(
        `${data.message || "joined the queue"}${pos} · holdings ${formatHoldings(data.holdings)}`,
        "ok"
      );
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
  if (!wallet || busy) return;
  busy = true;
  refreshYouUi();
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
    refreshYouUi();
  }
}

function bind() {
  els.btnJoin.addEventListener("click", joinQueue);
  els.btnLeave.addEventListener("click", leaveQueue);
  els.btnWallet?.addEventListener("click", toggleWallet);
  bindAudioControls();
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
  bindWalletEvents();
}

async function boot() {
  const saved = loadSavedName();
  if (els.displayName) {
    els.displayName.value = saved;
    els.displayName.placeholder = "guest-xxxx";
  }
  applyConfig(config);
  refreshWalletUi();
  bind();
  showStreamOffline(offlineMessage());
  await trySilentWallet();
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
