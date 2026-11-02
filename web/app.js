"use strict";
/*
 * QnapDownloader browser-style shell (v0.1.16).
 * Spec: docs/browser-shell-ui.md. Single file, no build step.
 *
 * Security notes (see "Security invariants" in the spec):
 *  - DOM is built only with createElement/createElementNS + textContent.
 *  - External pages live in per-tab iframes whose sandbox is one of exactly
 *    three constants (SANDBOX_STRICT / _INTERACTIVE / _COMPAT, see below). The
 *    src is only ever set to a URL that /api/browser/validate returned
 *    (see mountFrame).
 *  - Storage keeps only {id, url history, index, mode} and the last route
 *    (session), one boolean preference and hostname->mode pairs (local).
 *    Never tokens, passwords or cookies.
 */

// ---------------------------------------------------------------- constants
const MAX_WEB_TABS = 6;
// iOS kills the WebContent process (and reloads the home-screen app) under
// memory pressure, so keep few live external iframes. Others re-load lazily.
const MAX_LIVE_FRAMES_NARROW = 1;
const MAX_LIVE_FRAMES_WIDE = 3;
const SWR_TTL_MS = 10000;
const MAX_HOST_MODES = 100;
const UPDATE_POLL_MS = 2000;
const UPDATE_WATCH_MS = 5 * 60 * 1000;
const STALE_CHECK_MS = 30000;
const MAX_CLOSED_TABS = 10;
const MAX_HISTORY = 50;
const AUTO_REFRESH_MS = 5000;     // downloads view: local data, refreshed while visible
const BADGE_REFRESH_MS = 30000;   // header badge: only while the engine is running
const FRAME_LOAD_TIMEOUT_MS = 12000;
const HINT_DELAY_MS = 6000;         // offer the "not showing?" bar after this long
const PAGE_SIZE = 30;
const WAITING_MAX = 50;
// Remote pages never get popups, downloads, modals or top navigation.
const SANDBOX_STRICT = "";
const SANDBOX_INTERACTIVE = "allow-scripts allow-forms";
// Compatibility mode (per tab, explicit confirm). SAFETY ARGUMENT: a sandboxed
// frame without the origin-sharing flag gets an opaque origin, so it has no
// cookies/storage and e.g. a Cloudflare check cannot complete. With the flag,
// the page keeps its OWN origin. External pages are always https on a public
// DNS name (browser_safety.validate), and the Hub is served from an http
// LAN/VPN address or a reverse-proxy host, so the two origins can never be
// equal; the backend (/api/browser/validate) additionally rejects the Hub's own
// hostname and *.ts.net. The page therefore gets only its own origin's
// cookies/storage and cannot reach the Hub's session or storage.
const SANDBOX_COMPAT = "allow-scripts allow-forms allow-same-origin";
const MODES = ["strict", "interactive", "compat"];
// NAS remote browser (v0.1.16): a host preference and a per-tab flag, never a
// sandbox value. The page is rendered by Chromium in a NAS container; this
// page only shows JPEG frames from /api/rb/frame.jpg (binary, shown through a
// blob: object URL) and posts validated input.
// NAS browser modes in the per-host map. "remote" = use the NAS browser with the display chosen in
// Settings (rb_default_mode); the other three are an explicit per-site display choice (v0.1.30).
const REMOTE_MODE = "remote";
const REMOTE_FAST_MODE = "remote-fast";      // fast full-page display (JPEG tiles)
const REMOTE_LIVE_MODE = "remote-live";      // live screencast (Cloudflare checks, logins)
const REMOTE_READER_MODE = "remote-reader";  // reader: text rebuilt from validated data
const REMOTE_MODES = [REMOTE_MODE, REMOTE_FAST_MODE, REMOTE_LIVE_MODE, REMOTE_READER_MODE];
const DISPLAY_OF_MODE = {[REMOTE_FAST_MODE]: "tiles", [REMOTE_LIVE_MODE]: "live", [REMOTE_READER_MODE]: "reader"};
const MODE_OF_DISPLAY = {tiles: REMOTE_FAST_MODE, live: REMOTE_LIVE_MODE, reader: REMOTE_READER_MODE};
const DISPLAY_OF_SETTING = {reader: "reader", fast: "tiles", live: "live"};
const RB_TILE_CONCURRENCY = 3;     // tile downloads in flight at once
const RB_META_WAIT_S = 10;         // long-poll of the tab metadata; its timeout also drives the DOM check
const RB_INPUT_SNAP_MS = 800;      // new snapshot this long after a key / text input
const RB_PULL_PX = 90;             // pull-to-refresh distance
const RB_SWAP_WAIT_MS = 6000;      // swap in a new snapshot after this long even if tiles are missing
const RB_LINK_NEAR_PX = 28;        // a press this close (remote CSS px) to a link box still hits it
const RB_VP_DEBOUNCE_MS = 200;     // live link map: refresh this long after the picture changed
const RB_VP_MIN_MS = 500;          // ... and at most this often
const RB_VP_WAIT_MS = 1500;        // a long press waits at most this long for a fresh live link map
const RB_BOX_CAP = 800;            // link outlines drawn at most
const RB_READER_IMG_CONCURRENCY = 3;
const RB_MAX_POLL_FAILS = 3;
const RB_TAP_MOVE_PX = 10;
const RB_TAP_MAX_MS = 600;
const RB_LINKS_DELAY_MS = 1200;
const RB_STATUS_POLL_MS = 1500;
const RB_VIEWPORT_DEBOUNCE_MS = 250;
const RB_INTERCEPT_QUEUE = 20;
const RB_POPUP_TOAST_MS = 8000;
const RB_STAGE_MIN_PX = 100;     // a hidden / not yet laid out stage reports ~0
const RB_LONGPRESS_MS = 550;        // long press on the remote page: follow the link in the background
const RB_LONGPRESS_MOVE_PX = 10;
const RB_RING_DELAY_MS = 150;       // the press ring appears after this (taps never show it)
const RB_QUALITY_KEY = "qnapdl.rbquality.v1";
const RB_QUALITIES = ["speed", "standard", "high"];
const RB_QUALITY_LABELS = {speed: "速度優先", standard: "標準", high: "高画質"};
const RB_DISPLAY_LABELS = {reader: "リーダー", tiles: "高速（全体表示）", live: "ライブ"};
const RB_PERF_WINDOW_MS = 5000;
const RB_STREAM_FIRST_MS = 5000;
const RB_STREAM_ERR_WINDOW_MS = 10000;
const RB_STREAM_META_S = 2;
const RB_STREAM_RECONNECT_MS = 9 * 60 * 1000;
const RB_STREAM_STALL_MS = 6000;
const RB_POPUP_NEW_TAB = "new";
const RB_KEYS = ["Enter", "Backspace", "Tab", "Escape", "Delete",
  "ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"];
const MODE_LABELS = {strict: "強い制限", interactive: "限定操作", compat: "互換"};
// v0.1.30: the old per-host "external" (always open in Safari) mode was removed; a stored entry
// is converted to the NAS browser default mode on load.
const LEGACY_EXTERNAL_MODE = "external";
const SHELL_KEY = "qnapdl.shell.v1";
const HOST_MODE_KEY = "qnapdl.hostmodes.v1";
const TAB_STORAGE_KEY = "qnapdl.webtabs.v1";
const LEGACY_AUTO_KEY = "qnapdl.downloads.auto";   // v0.1.11 switch; removed silently
const SETTINGS_SECTIONS = ["engine", "accounts", "browser", "storage", "https", "share", "bookmarks", "updates", "diagnostics", "about"];
const SECTION_ALIASES = {jd2: "engine"};   // old #/settings/jd2 links
const ENGINE_POLL_MS = 1500;
const SVG_NS = "http://www.w3.org/2000/svg";
const PASTE_LIMIT = 65536;
const PASTE_DEBOUNCE_MS = 400;
const HTTPS_SERVE_COMMAND = "tailscale serve --bg --https=443 http://127.0.0.1:17864";
// Shown for copying only; it runs in Shortcuts on the phone, never in this page.
const SHARE_JS = "completion(Array.from(document.querySelectorAll('a[href]'),a=>a.href)" +
  ".filter(h=>/rapidgator\\.net|rg\\.to/.test(h)).join('\\n')+'\\n'+location.href)";

const state = {
  csrf: "", authed: false, shellReady: false,
  route: {view: "home"}, lastSection: "",
  tabs: [], nextTabId: 1, closed: [], lastWebTabId: null,
  bookmarks: [], status: null, omniDisplay: "",
  snapshot: null, snapshotAt: 0, downloadsPage: 0, downloadsBusy: false,
  filter: "all", query: "", autoTimer: null, downloadsFails: 0,
  dlRunning: null, badgeTimer: null, badgeAt: 0, expanded: new Set(),
  settings: null, jd2: null, queueData: null, pasteSeq: 0, pasteTimer: null, paste: null,
  pickerOpen: false, connChecking: false, stepsMode: "",
  selectedVersion: "", selectedChannel: "rc",
  menuReturnFocus: null,
  snapshotJson: "", downloadsSig: "", folderPath: "",
  bootVersion: "", statusCheckedAt: 0, updateWatching: false,
  engine: null, engineTimer: null, engineBusy: false,
  https: false, tailnet: false,
  rbPolling: "", rbPollMode: "", rbGen: 0, rbStatus: null, rbTimer: null, rbBusy: false,
  rbIntercepts: [],
  rbQuality: loadRbQuality(), rbStream: null, rbPerf: false, rbPerfSamples: [], rbPerfTimer: 0,
  rbPerfSince: 0, probeResult: null, probeResults: [], siteRules: null, waiting: null,
  accounts: null, accountsTruncated: false, accountHosts: [], accountBusy: false, accountEditId: "",
};

// ------------------------------------------------------------------ helpers
const $ = id => document.getElementById(id);

function setShown(id, on) { $(id).hidden = !on; }

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = String(text);
  if (className) node.className = className;
  return node;
}

function icon(name) {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("class", "icon");
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS(SVG_NS, "use");
  use.setAttribute("href", "#i-" + name);
  svg.appendChild(use);
  return svg;
}

function readableSize(bytes) {
  if (typeof bytes !== "number" || !Number.isFinite(bytes) || bytes < 0) return "不明";
  if (bytes === 0) return "0 B";
  const exp = Math.min(4, Math.floor(Math.log(bytes) / Math.log(1024)));
  const num = bytes / Math.pow(1024, exp);
  return (num >= 10 ? num.toFixed(0) : num.toFixed(1)) + " " + ["B", "KiB", "MiB", "GiB", "TiB"][exp];
}

function readableEta(seconds) {
  if (typeof seconds !== "number" || !Number.isFinite(seconds) || seconds < 0) return "不明";
  const minutes = Math.ceil(seconds / 60);
  return minutes < 1 ? "1分未満" : minutes < 60 ? minutes + "分" :
    Math.floor(minutes / 60) + "時間" + (minutes % 60) + "分";
}

function hostOf(url) {
  try { return new URL(url).hostname.replace(/^www\./, ""); }
  catch (e) { return String(url).slice(0, 40); }
}

// Tailscale traffic is WireGuard-encrypted, so plain http:// to a tailnet host
// is not "unencrypted". Matches 100.64.0.0/10, *.ts.net and fd7a:115c:a1e0::/48.
function isTailscaleHost(hostname) {
  const host = String(hostname || "").toLowerCase().replace(/^\[|\]$/g, "").replace(/\.$/, "");
  if (host === "ts.net" || host.endsWith(".ts.net")) return true;
  const v4 = host.match(/^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/);
  if (v4) {
    const parts = v4.slice(1).map(Number);
    return parts.every(n => n <= 255) && parts[0] === 100 && parts[1] >= 64 && parts[1] <= 127;
  }
  return /^fd7a:115c:a1e0(:|$)/.test(host);
}

// Storage wrappers: every access is guarded; the UI works without storage.
function readSession(key) {
  try { return sessionStorage.getItem(key); } catch (e) { return null; }
}
function writeSession(key, value) {
  try {
    if (value === null) sessionStorage.removeItem(key);
    else sessionStorage.setItem(key, value);
  } catch (e) { /* private mode / blocked: ignore */ }
}
function loadRbQuality() {
  const value = readLocal(RB_QUALITY_KEY);
  return RB_QUALITIES.includes(value) ? value : "speed";
}
function readLocal(key) {
  try { return localStorage.getItem(key); } catch (e) { return null; }
}
function writeLocal(key, value) {
  try { localStorage.setItem(key, value); } catch (e) { /* ignore */ }
}
function removeLocal(key) {
  try { localStorage.removeItem(key); } catch (e) { /* ignore */ }
}

function normalizeMode(mode) { return MODES.includes(mode) ? mode : "strict"; }

function sandboxFor(mode) {
  if (mode === "compat") return SANDBOX_COMPAT;
  return mode === "interactive" ? SANDBOX_INTERACTIVE : SANDBOX_STRICT;
}

// Remembered mode per hostname: [[host, mode], ...] only, newest last, capped.
function readHostModes() {
  try {
    const data = JSON.parse(readLocal(HOST_MODE_KEY) || "[]");
    if (!Array.isArray(data)) return [];
    return data.map(e => (Array.isArray(e) && e[1] === LEGACY_EXTERNAL_MODE) ? [e[0], REMOTE_MODE] : e)
      .filter(e => Array.isArray(e) && typeof e[0] === "string" && e[0].length <= 253 &&
        ((MODES.includes(e[1]) && e[1] !== "strict") || REMOTE_MODES.includes(e[1]))).slice(-MAX_HOST_MODES);
  } catch (e) { return []; }
}

// One-time rewrite of stored "external" entries (readHostModes already maps them when reading).
function migrateHostModes() {
  try {
    const raw = readLocal(HOST_MODE_KEY);
    if (raw && raw.includes('"' + LEGACY_EXTERNAL_MODE + '"')) {
      writeLocal(HOST_MODE_KEY, JSON.stringify(readHostModes()));
    }
  } catch (e) { /* ignore: reads still map the old value */ }
}

function rememberHostMode(host, mode) {
  if (!host) return;
  const list = readHostModes().filter(e => e[0] !== host);
  if (mode !== "strict") list.push([host, mode]);   // strict is the default: store nothing
  writeLocal(HOST_MODE_KEY, JSON.stringify(list.slice(-MAX_HOST_MODES)));
}

function rememberedMode(host) {
  const hit = readHostModes().find(e => e[0] === host);
  return hit ? hit[1] : null;
}

function isRemoteHost(host) {
  const mode = host ? rememberedMode(host) : null;
  return REMOTE_MODES.includes(mode);
}

function remoteHosts() {
  return readHostModes().filter(e => REMOTE_MODES.includes(e[1])).map(e => e[0]);
}

migrateHostModes();

// ------------------------------------------------- stale-while-revalidate
// Views paint cached data immediately, refresh in the background when the
// entry is older than SWR_TTL_MS, never clear before new data arrives and
// touch the DOM only when the JSON actually changed.
const swrCache = new Map(), swrPainted = new Map(), swrPending = new Map();
let swrEpoch = 0;

function swrPaint(key, entry, render, slot) {
  const sig = key + "\n" + entry.json;
  if (swrPainted.get(slot) === sig) return;
  render(entry.data);
  swrPainted.set(slot, sig);
}

async function swr(key, path, render, opts = {}) {
  const slot = opts.slot || key;
  const hit = swrCache.get(key);
  if (hit) {
    swrPaint(key, hit, render, slot);
    if (!opts.force && Date.now() - hit.at < SWR_TTL_MS) return hit.data;
  }
  const epoch = swrEpoch;
  let pending = swrPending.get(key);
  if (!pending) {
    pending = api(path).then(data => {
      const entry = {data, json: JSON.stringify(data), at: Date.now()};
      if (epoch === swrEpoch) {
        swrCache.set(key, entry);
        while (swrCache.size > 60) swrCache.delete(swrCache.keys().next().value);
      }
      return entry;
    }).finally(() => swrPending.delete(key));
    swrPending.set(key, pending);
  }
  const entry = await pending;
  if (epoch === swrEpoch) swrPaint(key, entry, render, slot);
  return entry.data;
}

// After a write: forget the cache (the DOM stays until fresh data replaces it).
function swrDrop(...keys) { for (const key of keys) swrCache.delete(key); }

function swrReset() {
  swrEpoch++;
  swrCache.clear(); swrPainted.clear(); swrPending.clear();
}

// ---------------------------------------------------------------------- API
async function api(path, body = null) {
  const headers = {};
  const opts = {credentials: "same-origin", headers};
  if (body !== null) {
    opts.method = "POST";
    headers["Content-Type"] = "application/json";
    headers["X-CSRF-Token"] = state.csrf;
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  let data = {};
  try { data = await res.json(); } catch (e) { data = {}; }
  if (res.status === 401 && state.authed) {
    // Session ended (e.g. NAS service restart): fall back to the login form.
    state.authed = false;
    setTimeout(go, 0);
    throw new Error("セッションが切れました。再度ログインしてください");
  }
  if (!res.ok) {
    const failure = new Error(data.error || "HTTP " + res.status);
    failure.status = res.status;
    throw failure;
  }
  return data;
}

async function perform(fn) {
  try { await fn(); } catch (e) { toast(e.message, "error"); }
}

// ------------------------------------------------------------------- toasts
function pruneToasts(box) {
  const normal = Array.from(box.children).filter(n => !n.dataset.sticky);
  while (normal.length > 4) normal.shift().remove();
}

const recentErrorToasts = new Map();
function toast(message, kind = "success") {
  const box = $("notice");
  if (kind === "error") {
    // The same failure can surface from more than one handler for one click:
    // show it once per 5 s.
    const now = Date.now();
    const last = recentErrorToasts.get(message);
    if (last !== undefined && now - last < 5000) return;
    recentErrorToasts.set(message, now);
    for (const [key, at] of recentErrorToasts) if (now - at >= 5000) recentErrorToasts.delete(key);
  }
  for (const old of Array.from(box.children)) {
    if (old.dataset.message === message) old.remove();
  }
  const node = element("div", undefined, "toast " + kind);
  node.dataset.message = message;
  node.appendChild(element("span", message, "toast-text"));
  if (kind === "error") {
    const close = element("button", undefined, "toast-close");
    close.type = "button";
    close.setAttribute("aria-label", "通知を閉じる");
    close.appendChild(icon("x"));
    close.addEventListener("click", () => node.remove());
    node.appendChild(close);
  } else {
    setTimeout(() => node.remove(), 6000);
  }
  box.appendChild(node);
  pruneToasts(box);
}
function flash(message) { toast(message, "success"); }

// Persistent toast (no close button, no timeout) replaced by key. `actions`
// is [{label, run}]. Used for the update progress and stale-page notices.
function stickyToast(key, message, kind, actions = []) {
  const box = $("notice");
  for (const old of Array.from(box.children)) {
    if (old.dataset.sticky === key) old.remove();
  }
  const node = element("div", undefined, "toast sticky " + kind);
  node.dataset.sticky = key;
  node.appendChild(element("span", message, "toast-text"));
  for (const action of actions) {
    const btn = element("button", action.label, "btn toast-action");
    btn.type = "button";
    btn.addEventListener("click", action.run);
    node.appendChild(btn);
  }
  box.appendChild(node);
}

// ------------------------------------------------------------------ routing
function parseRoute() {
  const full = (location.hash || "").replace(/^#\/?/, "");
  const cut = full.indexOf("?");
  const raw = cut < 0 ? full : full.slice(0, cut);
  const parts = raw.split("/").filter(Boolean);
  const head = parts[0] || "home";
  if (head === "links") {
    // #/links?paste=<urlencoded>: prefill the paste box only (never auto-saves).
    let paste = null;
    if (cut >= 0) {
      try { paste = new URLSearchParams(full.slice(cut + 1)).get("paste"); } catch (e) { paste = null; }
    }
    return paste ? {view: "links", paste} : {view: "links"};
  }
  if (head === "downloads") return {view: head};
  if (head === "settings") {
    const wanted = SECTION_ALIASES[parts[1]] || parts[1];
    return {view: "settings",
      section: SETTINGS_SECTIONS.includes(wanted) ? wanted : defaultSection()};
  }
  if (head === "tab" && /^t\d+$/.test(parts[1] || "")) return {view: "tab", tabId: parts[1]};
  return {view: "home"};
}

// Engine first until it is running; then the save folder.
function defaultSection() {
  return state.engine && state.engine.phase === "running" ? "storage" : "engine";
}

function settingsHash() { return "#/settings/" + (state.lastSection || defaultSection()); }

function navigate(hash) {
  if (location.hash === hash) applyRoute();
  else location.hash = hash;
}

function replaceRoute(hash) {
  history.replaceState(null, "", hash);
  applyRoute();
}

function findTab(id) { return state.tabs.find(t => t.id === id) || null; }

function activeWebTab() {
  return state.route.view === "tab" ? findTab(state.route.tabId) : null;
}

// Which <section id="view-*"> is visible for the current route.
function currentViewName() {
  const tab = activeWebTab();
  if (state.route.view === "tab") return tab && tab.history.length ? "web" : "home";
  return state.route.view;
}

function applyRoute() {
  if (!state.authed) return;
  const route = parseRoute();
  if (/^#\/settings\/jd2\/?$/.test(location.hash)) { replaceRoute("#/settings/engine"); return; }
  if (route.view === "tab" && !findTab(route.tabId)) { replaceRoute("#/home"); return; }
  state.route = route;
  if (route.view === "links" && route.paste) {
    // Drop the pasted text from the address bar and history state at once.
    history.replaceState(null, "", "#/links");
    $("pasteBox").value = route.paste.slice(0, PASTE_LIMIT);
    schedulePaste(0);
    state.route = {view: "links"};
  }
  if (route.view === "tab") state.lastWebTabId = route.tabId;
  if (route.view === "settings") state.lastSection = route.section;
  closeMenu(false);
  closeSwitcher(false);
  const shown = currentViewName();
  for (const name of ["home", "downloads", "links", "settings", "web"]) {
    $("view-" + name).hidden = name !== shown;
  }
  syncFrameVisibility();
  renderWebTabs();
  updateChrome();
  syncAutoRefresh();
  syncEnginePoll();
  syncBadgeTimer();
  syncRemotePoll();
  syncBrowserPoll();
  // Last route (a hash, never a secret) lets a reloaded iOS web app resume.
  writeSession(SHELL_KEY, JSON.stringify({v: 1, hash: location.hash || "#/home"}));
  perform(() => enterView(route));
}

async function enterView(route, force = false) {
  if (route.view === "home" || (route.view === "tab" && currentViewName() === "home")) {
    await loadHome(force);
  } else if (route.view === "downloads") {
    renderDownloads();
    await loadEngine(force);
    refreshDownloadsNow();
  } else if (route.view === "links") {
    await Promise.all([loadQueue(force), loadJd2Status(force)]);
  } else if (route.view === "settings") {
    await enterSettings(route.section, force);
  } else if (route.view === "tab") {
    await ensureLoaded(findTab(route.tabId));
  }
}

// -------------------------------------------------------------- chrome / UI
function tabTitle(tab) {
  return tab.history.length ? hostOf(tab.history[tab.index]) : "新しいタブ";
}

function setOmniIcon(name) { $("omniIcon").setAttribute("href", "#i-" + name); }

function updateChrome() {
  if (!state.authed) return;
  syncKeyBar();
  const route = state.route, tab = activeWebTab(), shown = currentViewName();
  document.body.dataset.view = shown;
  let display = "", iconName = "globe";
  let back = true, forward = true;
  if (tab) {
    if (tab.history.length) {
      display = tab.current || tab.history[tab.index] || "";
      iconName = tab.current ? "lock" : "globe";
    }
    back = tab.remote ? Boolean(tab.rb) : tab.index > 0;
    forward = tab.remote ? Boolean(tab.rb) : (tab.index >= 0 && tab.index < tab.history.length - 1);
  } else if (route.view === "downloads") {
    display = "qnap://downloads"; iconName = "download";
  } else if (route.view === "links") {
    display = "qnap://links"; iconName = "link";
  } else if (route.view === "settings") {
    display = "qnap://settings/" + route.section; iconName = "gear";
  } else {
    iconName = "home";
  }
  state.omniDisplay = display;
  if (document.activeElement !== $("omnibox")) $("omnibox").value = display;
  setOmniIcon(iconName);
  $("navBack").disabled = !back;
  $("navForward").disabled = !forward;
  $("navReload").disabled = Boolean(tab) && !tab.history.length;

  const marked = Boolean(tab && tab.current);
  const on = marked && state.bookmarks.some(b => b.url === tab.current);
  $("starBtn").hidden = !(tab && tab.history.length);   // only meaningful for web tabs
  $("starBtn").disabled = !marked;
  $("starBtn").classList.toggle("is-on", on);
  $("starBtn").setAttribute("aria-pressed", String(on));
  $("starBtn").setAttribute("aria-label", on ? "ブックマーク済み" : "ブックマークに追加");

  const bar = (shown === "web" || shown === "home") && state.bookmarks.length > 0;
  setShown("bookmarkBar", bar);
  $("webLoading").hidden = !(tab && tab.loading);
  updateWebStatus(tab);
  updateCfHint(tab);
  updateRemoteView(tab);
  updatePinnedTabs();
  updateMobileBar();
}

function updatePinnedTabs() {
  const route = state.route;
  document.querySelectorAll("[data-tab]").forEach(a => {
    a.setAttribute("aria-selected", String(route.view === a.dataset.tab));
    if (a.dataset.tab === "settings") a.setAttribute("href", settingsHash());
  });
}

function updateMobileBar() {
  const route = state.route;
  document.querySelectorAll("#mobileBar [data-nav]").forEach(n => {
    const nav = n.dataset.nav;
    const current = nav === "browse" ? route.view === "tab" : route.view === nav;
    if (current) n.setAttribute("aria-current", "page");
    else n.removeAttribute("aria-current");
  });
  const count = $("tabCount");
  count.textContent = String(state.tabs.length);
  count.hidden = state.tabs.length === 0;
  $("mobileBrowse").setAttribute("aria-label", "ブラウズ（Webタブ " + state.tabs.length + "枚）");
}

function renderWebTabs() {
  const list = $("webTabs");
  list.replaceChildren();
  for (const tab of state.tabs) {
    const active = state.route.view === "tab" && state.route.tabId === tab.id;
    const wrap = element("div", undefined, "tab web-tab" + (active ? " active" : ""));
    wrap.setAttribute("role", "presentation");
    const link = element("a", undefined, "tab-link");
    link.href = "#/tab/" + tab.id;
    link.setAttribute("role", "tab");
    link.setAttribute("aria-selected", String(active));
    link.title = tab.current || tabTitle(tab);
    link.appendChild(icon("globe"));
    link.appendChild(element("span", tabTitle(tab), "tab-label"));
    wrap.appendChild(link);
    const close = element("button", undefined, "tab-close");
    close.type = "button";
    close.setAttribute("aria-label", "タブを閉じる: " + tabTitle(tab));
    close.appendChild(icon("x"));
    close.addEventListener("click", () => closeTab(tab.id));
    wrap.appendChild(close);
    list.appendChild(wrap);
  }
  updateMobileBar();
}

// One compact row per web tab (single line, ellipsis): [mode chip] host ... [...]
function updateWebStatus(tab) {
  const box = $("webStatus");
  box.replaceChildren();
  if (!tab || !tab.history.length) return;
  const remote = Boolean(tab.remote);
  const chip = element("button", remote ? "NASブラウザ" : (MODE_LABELS[tab.mode] || MODE_LABELS.strict),
    "mode-badge " + (remote ? "remote" : tab.mode));
  chip.type = "button";
  chip.title = "表示されない場合のヒントを表示";
  chip.addEventListener("click", () => {
    tab.showHint = true;
    tab.hintDismissed = false;
    updateChrome();
  });
  box.appendChild(chip);
  const host = hostOf(tab.history[tab.index]);
  let text = host;
  if (tab.remote && tab.rb && tab.rb.title) text = tab.rb.title;
  if (tab.error) text = host + " — 読み込めませんでした: " + tab.error;
  else if (!tab.current && !tab.remote) text = host + " — 読み込み中…";
  const label = element("span", text, "web-status-text");
  label.title = tab.current || text;
  box.appendChild(label);
  const more = element("button", undefined, "icon-btn small");
  more.type = "button";
  more.title = "メニュー";
  more.setAttribute("aria-label", "メニューを開く");
  more.appendChild(icon("more"));
  more.addEventListener("click", event => { event.stopPropagation(); openMenu(); });
  box.appendChild(more);
}

// Slim one-line bar: shown 6 s after a load starts (or when the chip is tapped).
// Cross-origin load failure cannot be detected, so this is a timing heuristic.
function updateCfHint(tab) {
  const show = Boolean(tab && tab.current && !tab.remote && tab.showHint && !tab.hintDismissed);
  $("cfHint").hidden = !show;
  $("cfCompat").hidden = !tab || tab.mode === "compat";
}

// ------------------------------------------------------------- status/home
function renderStatusData(s) {
  state.status = s;
  if (s.engine) applyEngine(s.engine);
  if (s.jd2) state.jd2 = s.jd2;
  document.querySelectorAll(".js-version").forEach(n => { n.textContent = "v" + s.version; });
  document.querySelectorAll(".js-version-bare").forEach(n => { n.textContent = s.version; });
  renderHome();
}

async function loadStatus(force = false) {
  const s = await swr("status", "/api/status", renderStatusData, {force});
  state.statusCheckedAt = Date.now();
  checkBootVersion(s.version);
}

async function loadSettings(force = false) {
  await swr("settings", "/api/settings", renderDownloadDir, {force});
}

async function loadHome(force = false) {
  await Promise.all([loadStatus(force), loadSettings(force)]);
  refreshBadge();
  // Quiet: only decides whether the optional "register an account" item shows.
  if (engineRunning() && state.accounts === null) loadAccounts().catch(() => {});
}

// The Hub may have been updated (from this or another device) since this page
// loaded. Offer a reload; never reload automatically here.
function checkBootVersion(version) {
  if (typeof version !== "string" || !version) return;
  if (!state.bootVersion) { state.bootVersion = version; return; }
  if (version === state.bootVersion || state.updateWatching) return;
  stickyToast("stale", "Hubがv" + version + "に更新されています。再読み込みしてください" +
    "（再読み込み後に再ログインが必要です）", "info",
    [{label: "再読み込み", run: () => location.reload()}]);
}

function setCheck(rowId, done) {
  $(rowId).classList.toggle("is-done", done);
}

// One "はじめに" checklist until a save folder is chosen and the engine is set
// up; afterwards a single status line. Nothing renders before both are known.
function renderHome() {
  const e = state.engine, cfg = state.settings;
  if (!e || !cfg) {
    setShown("onboarding", false);
    setShown("homeStatus", false);
    return;
  }
  const folderDone = Boolean(cfg.download_dir);
  const engineDone = e.installed === true;
  const onboarding = !folderDone || !engineDone;
  // Optional third item: the engine runs and JD2 has no premium account yet.
  const wantAccounts = !onboarding && e.phase === "running" &&
    Array.isArray(state.accounts) && state.accounts.length === 0;
  setShown("onboarding", onboarding || wantAccounts);
  setShown("homeStatus", !onboarding);
  setShown("obAccounts", wantAccounts);
  if (wantAccounts) {
    setCheck("obStorage", true);
    setCheck("obEngine", true);
    $("obStorageText").textContent = cfg.download_dir;
    setShown("obStorageBtn", false);
    setShown("homeEngineSetup", false);
    $("obEngineText").textContent = "準備できました";
  }
  if (onboarding) {
    setCheck("obStorage", folderDone);
    setCheck("obEngine", engineDone);
    $("obStorageText").textContent = folderDone ? cfg.download_dir : "ダウンロードの保存先フォルダ";
    setShown("obStorageBtn", !folderDone);
    const setupBtn = $("homeEngineSetup");
    const busy = e.phase === "setup";
    setupBtn.hidden = engineDone || busy;
    setupBtn.disabled = !folderDone;
    setupBtn.textContent = e.error ? "もう一度試す" : "初期設定を開始";
    $("obEngineText").textContent = engineDone ? "準備できました" :
      busy ? setupProgressText(e) :
      e.error ? "失敗しました: " + e.error :
      folderDone ? ((e.container || {}).available === false ?
        "Container Stationが必要です（設定 › エンジン）" : "JDownloader 2を導入します") :
      "先に保存先を選んでください";
    return;
  }
  const phase = e.phase;
  const running = phase === "running";
  $("homeStatusDot").className = "dot " + (running ? "ok" : phase === "failed" ? "off" : "idle");
  $("homeStatusText").textContent = running ? "エンジン稼働中" :
    phase === "starting" ? "エンジン起動中…" :
    phase === "failed" ? "エンジンを起動できません" : "エンジン停止中";
  const link = $("homeStatusLink");
  link.hidden = !running;
  if (running) {
    link.textContent = state.dlRunning === null ? "ダウンロードを見る" : "ダウンロード中 " + state.dlRunning + "件";
    link.setAttribute("href", "#/downloads");
  }
  setShown("homeEngineStart", phase === "stopped" || phase === "failed");
}

// --------------------------------------------------------------- bookmarks
function renderBookmarks(b) {
  state.bookmarks = Array.isArray(b.items) ? b.items : [];
  renderBookmarkBar();
  renderSpeedDial();
  renderBookmarkList();
  updateChrome();
}

async function loadBookmarks(force = false) {
  await swr("bookmarks", "/api/bookmarks", renderBookmarks, {force});
}

function renderBookmarkBar() {
  const bar = $("bookmarkBar");
  const nodes = [];
  for (const book of state.bookmarks.slice(0, 30)) {
    const btn = element("button", undefined, "bookmark-chip");
    btn.type = "button";
    btn.title = book.url;
    btn.appendChild(icon("star"));
    btn.appendChild(element("span", book.title || hostOf(book.url)));
    btn.addEventListener("click", () => perform(() => openWebUrl(book.url)));
    nodes.push(btn);
  }
  bar.replaceChildren(...nodes);
}

function renderSpeedDial() {
  const dial = $("speedDial");
  const nodes = [];
  for (const book of state.bookmarks.slice(0, 24)) {
    const tile = element("button", undefined, "dial-tile");
    tile.type = "button";
    tile.title = book.url;
    const letter = element("span", (book.title || hostOf(book.url)).slice(0, 1).toUpperCase(), "dial-letter");
    letter.setAttribute("aria-hidden", "true");
    tile.appendChild(letter);
    tile.appendChild(element("span", book.title || hostOf(book.url), "dial-title"));
    tile.appendChild(element("span", hostOf(book.url), "dial-host"));
    tile.addEventListener("click", () => perform(() => openWebUrl(book.url)));
    nodes.push(tile);
  }
  dial.replaceChildren(...nodes);
  setShown("speedDialEmpty", state.bookmarks.length === 0);
}

function renderBookmarkList() {
  const list = $("bookmarkItems");
  if (!state.bookmarks.length) {
    list.replaceChildren(element("p", "ブックマークはまだありません。", "empty-state"));
    return;
  }
  const nodes = [];
  for (const book of state.bookmarks) {
    const box = element("div", undefined, "item");
    const open = element("button", book.title || hostOf(book.url), "linklike");
    open.type = "button";
    open.addEventListener("click", () => perform(() => openWebUrl(book.url)));
    box.appendChild(open);
    box.appendChild(element("span", book.url));
    const del = element("button", "削除", "btn small");
    del.type = "button";
    del.addEventListener("click", () => perform(async () => {
      await api("/api/bookmarks/delete", {id: book.id});
      await loadBookmarks(true);
    }));
    box.appendChild(del);
    nodes.push(box);
  }
  list.replaceChildren(...nodes);
}

async function addBookmark(title, url) {
  await api("/api/bookmarks", {title, url});
  await loadBookmarks(true);
}

// -------------------------------------------------------------- web tabs
function newTabRecord(id) {
  return {id, history: [], index: -1, mode: "strict", current: "",
    loaded: false, loading: false, token: 0, frame: null, error: "",
    lastUsed: 0, hintDismissed: false, showHint: false, hintTimer: 0,
    remote: false, rb: null};
}

function saveTabs() {
  // Only id / url history / index / mode. No tokens, cookies or page data.
  const data = {v: 1, next: state.nextTabId, tabs: state.tabs.map(t => ({
    id: t.id, history: t.history.slice(), index: t.index, mode: t.mode,
    remote: t.remote === true}))};
  writeSession(TAB_STORAGE_KEY, JSON.stringify(data));
}

function restoreTabs() {
  let data = null;
  try { data = JSON.parse(readSession(TAB_STORAGE_KEY) || "null"); } catch (e) { data = null; }
  if (!data || data.v !== 1 || !Array.isArray(data.tabs)) return;
  let maxId = 0;
  for (const raw of data.tabs.slice(0, MAX_WEB_TABS)) {
    if (!raw || typeof raw.id !== "string" || !/^t\d+$/.test(raw.id) || findTab(raw.id)) continue;
    const tab = newTabRecord(raw.id);
    const history = Array.isArray(raw.history) ? raw.history : [];
    tab.history = history.filter(u => typeof u === "string" && u.length <= 2048 &&
      u.startsWith("https://")).slice(-MAX_HISTORY);
    const idx = Number.isInteger(raw.index) ? raw.index : tab.history.length - 1;
    tab.index = tab.history.length ? Math.min(Math.max(idx, 0), tab.history.length - 1) : -1;
    tab.mode = normalizeMode(raw.mode);
    tab.remote = raw.remote === true;   // the remote tab itself is NOT reopened automatically
    state.tabs.push(tab);   // loaded=false: validated and loaded lazily on selection
    maxId = Math.max(maxId, Number(raw.id.slice(1)));
  }
  const next = Number.isInteger(data.next) ? data.next : 1;
  state.nextTabId = Math.max(next, maxId + 1);
}

function createTab() {
  const tab = newTabRecord("t" + state.nextTabId++);
  state.tabs.push(tab);
  saveTabs();
  return tab;
}

function tabLimitReached() {
  if (state.tabs.length < MAX_WEB_TABS) return false;
  toast("Webタブは最大" + MAX_WEB_TABS + "枚です。端末のメモリと隔離iframeの負荷を考慮した上限です。不要なタブを閉じてください。", "error");
  return true;
}

function createBlankTab() {
  if (tabLimitReached()) return;
  const tab = createTab();
  navigate("#/tab/" + tab.id);
}

function closeTab(id) {
  const idx = state.tabs.findIndex(t => t.id === id);
  if (idx < 0) return;
  const tab = state.tabs[idx];
  if (tab.history.length) {
    state.closed.push({history: tab.history.slice(), index: tab.index, mode: tab.mode});
    while (state.closed.length > MAX_CLOSED_TABS) state.closed.shift();
  }
  tab.token++;
  clearTimeout(tab.hintTimer);
  if (tab.frame) tab.frame.remove();
  releaseRemote(tab);   // closing the web tab closes the NAS browser tab (frees memory)
  state.tabs.splice(idx, 1);
  saveTabs();
  if (state.lastWebTabId === id) state.lastWebTabId = null;
  if (state.route.view === "tab" && state.route.tabId === id) {
    const next = state.tabs[idx] || state.tabs[idx - 1];
    navigate(next ? "#/tab/" + next.id : "#/home");
  } else {
    renderWebTabs();
    updateChrome();
  }
  if (!$("tabSwitcher").hidden) renderSwitcher();
}

function reopenClosedTab() {
  if (!state.closed.length) { toast("開き直せる閉じたタブはありません。", "error"); return; }
  if (tabLimitReached()) return;
  const saved = state.closed.pop();
  const tab = createTab();
  tab.history = saved.history;
  tab.index = saved.index;
  tab.mode = saved.mode;
  saveTabs();
  navigate("#/tab/" + tab.id);   // loaded lazily (and re-validated) on selection
}

// Ask the Hub to validate; the returned URL is the only value used for iframes.
async function validateUrl(value) {
  const result = await api("/api/browser/validate?url=" + encodeURIComponent(value));
  return result.url;
}

// Defense in depth for compat mode (the backend enforces the same rule): the
// page's origin must differ from the Hub's and must not be a tailnet name.
function compatAllowedFor(url) {
  let host;
  try {
    const parsed = new URL(url);
    if (parsed.protocol !== "https:") return false;
    host = parsed.hostname.toLowerCase();
  } catch (e) { return false; }
  const hub = location.hostname.toLowerCase().replace(/^\[|\]$/g, "");
  return host !== hub && host !== "ts.net" && !host.endsWith(".ts.net");
}

function unloadTab(tab) {
  tab.token++;
  if (tab.frame) tab.frame.remove();
  tab.frame = null;
  tab.loaded = false;
  tab.loading = false;
  tab.current = "";   // history is kept; selecting the tab re-validates and reloads
}

function liveFrameLimit() {
  let narrow = false;
  try { narrow = window.matchMedia("(max-width: 759px)").matches; } catch (e) { narrow = false; }
  return narrow ? MAX_LIVE_FRAMES_NARROW : MAX_LIVE_FRAMES_WIDE;
}

// LRU: unload the least recently used frames beyond the limit (never `keep`).
function enforceLiveLimit(keep) {
  const limit = liveFrameLimit();
  const live = state.tabs.filter(t => t.frame);
  if (live.length <= limit) return;
  live.filter(t => t !== keep).sort((a, b) => a.lastUsed - b.lastUsed)
    .slice(0, live.length - limit).forEach(unloadTab);
}

function mountFrame(tab, validatedUrl) {
  // SECURITY: validatedUrl must come from validateUrl(). The iframe is rebuilt
  // each time so the sandbox attribute is in force before navigation starts.
  if (tab.frame) tab.frame.remove();
  if (tab.mode === "compat" && !compatAllowedFor(validatedUrl)) {
    tab.mode = "strict";
    toast("このアドレスでは互換モードを使えません。強い制限モードで表示します。", "error");
  }
  const frame = document.createElement("iframe");
  frame.setAttribute("sandbox", sandboxFor(tab.mode));
  frame.setAttribute("referrerpolicy", "no-referrer");
  frame.title = "外部サイトの隔離プレビュー（" + hostOf(validatedUrl) + "）";
  frame.className = "web-frame";
  frame.hidden = !(state.route.view === "tab" && state.route.tabId === tab.id);
  const done = () => {
    if (tab.frame === frame && tab.loading) { tab.loading = false; updateChrome(); }
  };
  frame.addEventListener("load", done);
  setTimeout(done, FRAME_LOAD_TIMEOUT_MS);
  // Heuristic only: whether or not 'load' fired, offer the bar after HINT_DELAY_MS.
  clearTimeout(tab.hintTimer);
  tab.showHint = false;
  tab.hintDismissed = false;
  tab.hintTimer = setTimeout(() => {
    if (tab.frame === frame && state.tabs.includes(tab)) { tab.showHint = true; updateChrome(); }
  }, HINT_DELAY_MS);
  $("frames").appendChild(frame);
  tab.frame = frame;
  tab.loading = true;
  tab.lastUsed = Date.now();
  frame.src = validatedUrl;
  enforceLiveLimit(tab);
}

function syncFrameVisibility() {
  for (const tab of state.tabs) {
    if (!tab.frame) continue;
    const active = state.route.view === "tab" && state.route.tabId === tab.id;
    tab.frame.hidden = !active;
    if (active) tab.lastUsed = Date.now();
  }
}

// The mode follows the hostname: a remembered mode applies when a tab opens
// that host; compat never carries over to a host the user did not choose it for.
function applyHostMode(tab, url) {
  const remembered = rememberedMode(hostOf(url));
  if (remembered && MODES.includes(remembered)) tab.mode = remembered;
  else if (tab.mode === "compat") tab.mode = "strict";
}

// Validate `value`, let `commit` update the history, then (re)load the iframe.
// opts.restore: lazy load of a restored tab (a remote tab is never opened automatically).
async function openInTab(tab, value, commit, opts = {}) {
  if (tab.remote || (!opts.restore && isRemoteHost(hostOf(value)))) {
    if (opts.restore) { tab.loaded = true; refreshAfterTabChange(); return; }   // never auto-open on restore
    await openRemote(tab, value, commit);
    return;
  }
  const token = ++tab.token;
  tab.error = "";
  tab.loading = true;
  updateChrome();
  let url;
  try {
    url = await validateUrl(value);
  } catch (e) {
    if (token === tab.token) { tab.loading = false; tab.error = e.message; updateChrome(); }
    throw e;
  }
  if (token !== tab.token || !state.tabs.includes(tab)) return;
  const previous = tab.history.length ? tab.history[tab.index] : "";
  commit(url);
  if (!previous || hostOf(previous) !== hostOf(url)) applyHostMode(tab, url);
  tab.current = url;
  tab.loaded = true;
  mountFrame(tab, url);
  saveTabs();
  refreshAfterTabChange();
}

// ---------------------------------------------------------- NAS remote browser
// The Hub validates every URL (/api/browser/validate rules) and forwards a fixed
// set of inputs. The only image source is a blob: object URL made from
// the Hub's /api/rb/frame.jpg bytes; it is never a remote URL.
async function openRemote(tab, value, commit) {
  const token = ++tab.token;
  tab.error = "";
  tab.loading = true;
  updateChrome();
  let url;
  try {
    url = await validateUrl(value);
    if (token !== tab.token || !state.tabs.includes(tab)) return;
    if (!state.settings) await loadSettings().catch(() => {});   // the default display comes from Settings
    if (token !== tab.token || !state.tabs.includes(tab)) return;
    if (tab.rb) {
      try {
        await api("/api/rb/navigate", {tab: tab.rb.id, url});
      } catch (e) {
        if (e.status !== 404) throw e;
        tab.rb = null;   // closed on the NAS meanwhile (idle stop): open a fresh one
      }
    }
    if (!tab.rb) {
      const made = await api("/api/rb/tabs", {url});
      if (token !== tab.token || !state.tabs.includes(tab)) {
        api("/api/rb/tabs/close", {id: made.id}).catch(() => {});
        return;
      }
      tab.rb = {id: made.id, seq: 0, url: "", title: "", width: 0, height: 0,
        loading: true, links: [], linksTimer: 0, blocked: null,
        rev: null, icptSeen: 0, present: 0, vp: "", popups: 0,
        shown: "", autoLive: false, autoSince: 0, autoClean: 0, autoChip: false,
        autoTimer: 0, autoBusy: false, autoOff: "", view: null, snap: null, snapKey: "", snapBusy: false,
        snapAgain: null, snapTimer: 0, busy: 0, scrollTop: 0, owner: tab,
        vmap: null, vmapRev: -1, vmapTimer: 0, vmapBusy: false, vmapAt: 0, linksRev: -1,
        reader: null, readerBusy: false, readerPending: false, readerAgain: false,
        stream: null, streamFailed: false, perf: {}};
    }
  } catch (e) {
    if (token === tab.token) { tab.loading = false; tab.error = e.message; updateChrome(); }
    throw e;
  }
  if (tab.frame) { tab.frame.remove(); tab.frame = null; }
  commit(url);
  tab.remote = true;
  tab.current = url;
  tab.loaded = true;
  tab.loading = false;
  tab.rb.seq = 0;
  tab.rb.rev = null;
  saveTabs();
  swrDrop("rb-status");
  refreshAfterTabChange();
}

// Free the NAS-side tab (renderer memory). Fire-and-forget; the Hub also
// stops the whole container after a few idle minutes.
function releaseRemote(tab) {
  const rb = tab.rb;
  tab.rb = null;
  if (!rb) return;
  clearTimeout(rb.linksTimer);
  if (state.rbPolling === rb.id) { state.rbPolling = ""; state.rbPollMode = ""; state.rbGen++; }
  dropRemoteImage(rb);
  dropTiles(rb);
  dropReader(rb);
  clearTimeout(rb.vmapTimer);
  state.rbIntercepts = state.rbIntercepts.filter(item => item.tab !== rb.id);
  if (rbPopup.item && rbPopup.item.tab === rb.id) hidePopupToast();
  closeProbeSheet();
  renderIntercept();
  api("/api/rb/tabs/close", {id: rb.id}).catch(() => {});
}

async function openInRemote(tab) {
  const url = tab.current || (tab.history.length ? tab.history[tab.index] : "");
  if (!url) return;
  if (!isRemoteHost(hostOf(url))) rememberHostMode(hostOf(url), REMOTE_MODE);   // default: fast display
  renderRemoteHosts();
  unloadTab(tab);
  tab.remote = true;
  await openRemote(tab, url, u => { tab.history[tab.index] = u; });
}

async function leaveRemote(tab) {
  const url = tab.current || (tab.history.length ? tab.history[tab.index] : "");
  const host = hostOf(url);
  if (isRemoteHost(host)) rememberHostMode(host, "strict");
  releaseRemote(tab);
  tab.remote = false;
  tab.loaded = false;
  renderRemoteHosts();
  if (url) await reloadTab(tab);
  else refreshAfterTabChange();
}

function remoteTab() {
  const tab = activeWebTab();
  return tab && tab.remote && tab.rb ? tab : null;
}

// The key bar (キーボード/Enter/⌫/Tab) is shown only while the remote page's focused element
// is editable (reported by the Hub after each input); when it turns on, focus the hidden
// text input so the iOS keyboard opens.
function setEditable(tab, editable, focus) {
  const rb = tab && tab.rb;
  if (!rb) return;
  const was = rb.editable === true;
  rb.editable = editable === true;
  syncKeyBar();
  if (focus && rb.editable && !was && remoteTab() === tab) $("remoteKbd").focus();
}

function syncKeyBar() {
  const tab = remoteTab();
  setShown("remoteTools", !!(tab && tab.rb && tab.rb.editable));
}

async function rbInput(event) {
  const tab = remoteTab();
  if (!tab) return;
  try {
    const r = await api("/api/rb/input", Object.assign({tab: tab.rb.id}, event));
    if (r && typeof r.editable === "boolean") setEditable(tab, r.editable, true);
    if ((event.type === "text" || event.type === "key") && tabDisplay(tab) === "tiles") {
      scheduleSnapshot(tab, RB_INPUT_SNAP_MS);
    }
  } catch (e) {
    if (e.status === 404) remoteLost(tab);
    else toast(e.message, "error");
  }
}

function remoteLost(tab) {
  if (tab.rb) {
    dropRemoteImage(tab.rb);
    dropTiles(tab.rb);
    dropReader(tab.rb);
    clearTimeout(tab.rb.vmapTimer);
    state.rbIntercepts = state.rbIntercepts.filter(item => item.tab !== tab.rb.id);
    renderIntercept();
  }
  tab.rb = null;
  tab.error = "NASブラウザのタブが閉じられました";
  state.rbPolling = "";
  state.rbPollMode = "";
  state.rbGen++;
  updateChrome();
}

// Free the object URL and clear the picture (nothing is kept once a tab is gone).
// One <img> is shared by all remote tabs, so only its current owner clears it.
const rbShown = {url: "", owner: null};

function dropRemoteImage(rb) {
  rb.present++;
  if (state.rbStream && state.rbStream.rb === rb) closeStream();
  if (rbShown.owner !== rb) return;
  if (rbShown.url) URL.revokeObjectURL(rbShown.url);
  rbShown.url = ""; rbShown.owner = null;
  const img = $("remoteImg");
  img.removeAttribute("src");
  img.hidden = true;
}

// Double buffer: the new JPEG is decoded off-screen first, then swapped in, and the
// previous object URL is revoked. A frame that arrives while an older one is still
// decoding wins; the older one is dropped.
async function presentRemoteFrame(tab, rb, blob) {
  const ticket = ++rb.present;
  const next = URL.createObjectURL(blob);
  const probe = new Image();
  probe.src = next;
  try {
    await probe.decode();
  } catch (e) {
    URL.revokeObjectURL(next);
    return;
  }
  if (ticket !== rb.present || tab.rb !== rb) { URL.revokeObjectURL(next); return; }
  const img = $("remoteImg");
  img.src = next;
  img.hidden = false;
  $("remoteIdle").hidden = true;
  const previous = rbShown.url;
  rbShown.url = next;
  rbShown.owner = rb;
  if (previous) URL.revokeObjectURL(previous);
}

function frameHeaders(res) {
  const text = name => {
    const raw = res.headers.get(name) || "";
    try { return decodeURIComponent(raw); } catch (e) { return ""; }
  };
  const number = name => Number(res.headers.get(name));
  return {
    seq: number("X-Frame-Seq"), width: number("X-Frame-Width"), height: number("X-Frame-Height"),
    url: text("X-Frame-Url"), title: text("X-Frame-Title"),
    loading: res.headers.get("X-Frame-Loading") === "1",
    intercepts: number("X-Frame-Intercepts"), rev: number("X-Frame-Rev"),
    blocked: text("X-Frame-Blocked") || null,
    popups: number("X-Frame-Popups"),
    editable: res.headers.get("X-Frame-Editable") === "1",
    challenge: res.headers.get("X-Frame-Challenge") === "1",
    linksRev: res.headers.has("X-Frame-Links-Rev") ? number("X-Frame-Links-Rev") : NaN,
  };
}

function applyRemoteMeta(tab, f) {
  const rb = tab.rb;
  if (!rb || !Number.isFinite(f.seq)) return;
  rb.seq = Math.max(rb.seq, f.seq);
  if (Number.isFinite(f.rev)) rb.rev = f.rev;
  let changed = false;
  if (Number.isFinite(f.width) && Number.isFinite(f.height) && f.width > 0 && f.height > 0) {
    rb.width = f.width; rb.height = f.height;
  }
  const url = typeof f.url === "string" ? f.url : "";
  const title = typeof f.title === "string" ? f.title : "";
  const loading = f.loading === true;
  if (url !== rb.url) {
    rb.url = url;
    rb.autoOff = "";
    changed = true;
    if (url.startsWith("https://") && url.length <= 2048) {
      tab.current = url;
      if (tab.history.length) tab.history[tab.index] = url;
      saveTabs();
    }
  }
  if (title !== rb.title) { rb.title = title; changed = true; }
  if (loading !== rb.loading) {
    rb.loading = loading;
    changed = true;
    if (!loading) scheduleRemoteLinks(tab);
  }
  if (typeof f.editable === "boolean") setEditable(tab, f.editable, false);
  if (Number.isFinite(f.popups) && f.popups !== rb.popups) { rb.popups = f.popups; changed = true; }
  if (f.blocked && f.blocked !== rb.blocked) toast(String(f.blocked), "error");
  rb.blocked = f.blocked || null;
  tab.loading = loading;
  if (Number.isFinite(f.intercepts) && f.intercepts > rb.icptSeen) {
    fetchIntercepts(tab).catch(() => {});
  }
  if (typeof f.challenge === "boolean") syncChallenge(tab, f.challenge, loading);
  if (Number.isFinite(f.linksRev)) noteLinksRev(tab, f.linksRev);
  if (url !== "" && changed) applyDisplay(tab);
  if (changed) updateChrome();
}

// Cloudflare checks and logins need the live screencast. The switch is automatic and
// temporary (rb.autoLive, never written to the per-host mode map); a dedicated poll of
// /api/rb/meta brings the tab back to 高速 after two clean checks of a finished page.
const AUTO_POLL_MS = 1500;
const AUTO_CLEAN_NEEDED = 2;
const AUTO_TIMEOUT_MS = 90000;

// Pure state step (unit-tested in node). Returns "enter" | "return" | "chip" | null.
function challengeStep(rb, challenge, loading, now, userLive) {
  if (!rb.autoLive) {
    if (challenge && !userLive) {
      rb.autoLive = true; rb.autoSince = now; rb.autoClean = 0; rb.autoChip = false;
      return "enter";
    }
    return null;
  }
  if (challenge) {
    rb.autoClean = 0;
    if (!rb.autoChip && now - rb.autoSince >= AUTO_TIMEOUT_MS) { rb.autoChip = true; return "chip"; }
    return null;
  }
  if (loading) { rb.autoClean = 0; return null; }
  rb.autoClean++;
  if (rb.autoClean >= AUTO_CLEAN_NEEDED) {
    rb.autoLive = false; rb.autoClean = 0; rb.autoChip = false;
    return "return";
  }
  return null;
}

function userPrefersLive(tab) {
  return userDisplay(tab) === "live";
}

function syncChallenge(tab, challenge, loading) {
  const rb = tab.rb;
  if (!rb || rb.autoLive || rb.autoOff === rb.url) return;   // the poll below owns the way back
  if (challengeStep(rb, challenge, loading, Date.now(), userPrefersLive(tab)) === "enter") {
    if (rb.shown === "tiles" || rb.shown === "reader") flash("確認画面のため一時的にライブ表示にしました");
    startAutoPoll(tab);
    applyDisplay(tab);
  }
}

function startAutoPoll(tab) {
  const rb = tab.rb;
  if (!rb || rb.autoTimer) return;
  rb.autoTimer = setInterval(() => autoPollTick(tab, rb), AUTO_POLL_MS);
}

function stopAutoPoll(rb) {
  if (rb && rb.autoTimer) { clearInterval(rb.autoTimer); rb.autoTimer = 0; }
}

async function autoPollTick(tab, rb) {
  if (tab.rb !== rb || !rb.autoLive) { stopAutoPoll(rb); return; }
  if (document.visibilityState !== "visible" || activeWebTab() !== tab || rb.autoBusy) return;
  rb.autoBusy = true;
  try {
    const res = await fetch("/api/rb/meta?tab=" + encodeURIComponent(rb.id) + "&wait=0",
      {credentials: "same-origin", cache: "no-store"});
    if (res.status !== 200 || tab.rb !== rb || !rb.autoLive) return;
    const meta = await res.json();
    if (tab.rb !== rb || !rb.autoLive) return;
    const step = challengeStep(rb, meta.challenge === true, meta.loading === true, Date.now(), false);
    if (step === "return") {
      stopAutoPoll(rb);
      applyDisplay(tab);   // tiles again: the tile loop takes a fresh snapshot on start
    } else if (step === "chip") {
      updateRemoteView(tab);
    }
  } catch (e) {
    /* transient: the next tick tries again */
  } finally {
    rb.autoBusy = false;
  }
}

// 「高速表示に戻す」 after the 90 s safety timeout: back to 高速, and no automatic
// switch again for this address.
function returnToFast(tab) {
  const rb = tab && tab.rb;
  if (!rb) return;
  stopAutoPoll(rb);
  rb.autoLive = false; rb.autoChip = false; rb.autoClean = 0; rb.autoOff = rb.url;
  applyDisplay(tab);
}

// The stage is the viewer's CSS viewport for the remote page.
function stageViewport() {
  const stage = $("remoteStage");
  const width = Math.round(stage.clientWidth), height = Math.round(stage.clientHeight);
  if (width < RB_STAGE_MIN_PX || height < RB_STAGE_MIN_PX) return null;
  const dpr = Math.min(3, Math.max(1, Number(window.devicePixelRatio) || 1));
  return {width: Math.min(1600, Math.max(320, width)), height: Math.min(2400, Math.max(320, height)),
    dpr: Math.round(dpr * 100) / 100};
}

async function sendViewport(tab) {
  const rb = tab.rb;
  const view = stageViewport();
  if (!rb || !view) return;
  const key = view.width + "x" + view.height + "@" + view.dpr + "/" + state.rbQuality;
  if (key === rb.vp) return;
  rb.vp = key;
  try {
    const r = await api("/api/rb/viewport", Object.assign({tab: rb.id, quality: state.rbQuality}, view));
    // The tiles of the old size are gone on the Hub: take a new snapshot.
    if (r && r.changed && rb.view && tab.rb === rb && tabDisplay(tab) === "tiles") {
      takeSnapshot(tab, {force: true, keepScroll: true, quiet: true}).catch(() => {});
    }
  } catch (e) {
    rb.vp = "";
    if (e.status === 404) remoteLost(tab);
    else throw e;
  }
}

const nextPaint = () => new Promise(resolve => requestAnimationFrame(() => resolve()));

// ---- live display over MJPEG push (v0.1.30) ----
// The <img> points at /api/rb/stream.mjpg (same origin, cookie sent); the Hub pushes the newest frame.
// Metadata (address, title, loading, link-map revision, fps) still comes from the /api/rb/meta
// long-poll. Two stream errors within 10 s, or no picture within 5 s, fall back to frame.jpg
// polling for that tab. The stream is closed whenever the tab is hidden, switched or the mode changes.
let streamNonce = 0;
function streamUrl(rb) {
  return "/api/rb/stream.mjpg?tab=" + encodeURIComponent(rb.id) + "&n=" + (++streamNonce);
}

// The Hub ends a stream after 10 min and an <img> may not report that: reconnect every 9 min
// while visible, and when the Hub says the stream delivers 0 fps for 6 s on a settled page.
// The old picture stays until the new stream's first frame replaces it.
function reconnectStream(rb) {
  const s = rb.stream;
  if (!s || state.rbStream !== s) return;
  s.opened = performance.now();
  s.loaded = false;
  s.stalledSince = 0;
  s.arm();
  $("remoteImg").src = streamUrl(rb);
}

function checkStream(rb, meta) {
  const s = rb.stream;
  if (!s || state.rbStream !== s || document.visibilityState !== "visible") return;
  const now = performance.now();
  if (now - s.opened >= RB_STREAM_RECONNECT_MS) { reconnectStream(rb); return; }
  if (!s.loaded || meta.loading === true || !Number.isFinite(meta.fps) || meta.fps > 0) { s.stalledSince = 0; return; }
  if (!s.stalledSince) s.stalledSince = now;
  else if (now - s.stalledSince >= RB_STREAM_STALL_MS) reconnectStream(rb);
}

function closeStream() {
  const s = state.rbStream;
  if (!s) return;
  state.rbStream = null;
  s.rb.stream = null;
  clearTimeout(s.timer);
  clearTimeout(s.retry);
  const img = $("remoteImg");
  img.onload = null;
  img.onerror = null;
  if (rbShown.owner === s.rb) {
    rbShown.owner = null;
    rbShown.url = "";
    img.removeAttribute("src");
    img.hidden = true;
  }
}

function streamFallback(tab, rb) {
  rb.streamFailed = true;
  closeStream();
  state.rbPollMode = "";             // make syncRemotePoll restart this tab with frame.jpg polling
  syncRemotePoll();
}

function openStream(tab, rb) {
  closeStream();
  dropRemoteImage(rb);
  const img = $("remoteImg");
  const s = {rb, opened: performance.now(), loaded: false, errors: [], timer: 0, retry: 0, stalledSince: 0, arm: null};
  state.rbStream = s;
  rb.stream = s;
  rbShown.owner = rb;
  rbShown.url = "";
  const arm = () => {
    clearTimeout(s.timer);
    s.timer = setTimeout(() => {
      if (state.rbStream === s && !s.loaded) streamFallback(tab, rb);
    }, RB_STREAM_FIRST_MS);
  };
  s.arm = arm;
  img.onload = () => {
    if (state.rbStream !== s) return;
    if (!s.loaded) {
      s.loaded = true;
      clearTimeout(s.timer);
      rb.perf.streamMs = performance.now() - s.opened;
    }
    img.hidden = false;
    $("remoteIdle").hidden = true;
  };
  img.onerror = () => {
    if (state.rbStream !== s) return;
    const now = performance.now();
    s.errors = s.errors.filter(t => now - t < RB_STREAM_ERR_WINDOW_MS);
    s.errors.push(now);
    if (s.errors.length >= 2) { streamFallback(tab, rb); return; }
    s.loaded = false;
    s.opened = now;
    arm();
    s.retry = setTimeout(() => { if (state.rbStream === s) img.src = streamUrl(rb); }, 500);
  };
  arm();
  img.src = streamUrl(rb);
}

async function streamLoop(tab, gen) {
  let fails = 0, first = true;
  await nextPaint();
  await nextPaint();
  if (gen !== state.rbGen || !tab.rb) return;
  await sendViewport(tab).catch(() => {});
  if (gen !== state.rbGen || !tab.rb) return;
  openStream(tab, tab.rb);
  while (gen === state.rbGen && tab.rb) {
    const rb = tab.rb;
    try {
      let target = "/api/rb/meta?tab=" + encodeURIComponent(rb.id) + "&wait=" + (first ? 0 : RB_STREAM_META_S);
      if (!first && rb.rev !== null) target += "&rev=" + rb.rev;
      first = false;
      const res = await fetch(target, {credentials: "same-origin", cache: "no-store"});
      if (gen !== state.rbGen || tab.rb !== rb) return;
      if (res.status === 401 && state.authed) {
        state.authed = false;
        setTimeout(go, 0);
        return;
      }
      if (res.status === 404) { remoteLost(tab); return; }
      if (res.status !== 200) throw new Error("HTTP " + res.status);
      const meta = await res.json();
      if (gen !== state.rbGen || tab.rb !== rb) return;
      fails = 0;
      if (Number.isFinite(meta.fps)) rb.perf.fps = meta.fps;
      if (Number.isFinite(meta.frame_bytes)) rb.perf.frameBytes = meta.frame_bytes;
      applyRemoteMeta(tab, Object.assign({}, meta, {seq: rb.seq, linksRev: meta.links_rev}));
      checkStream(rb, meta);
    } catch (e) {
      if (gen !== state.rbGen) return;
      fails++;
      if (fails >= RB_MAX_POLL_FAILS) {
        state.rbPolling = "";
        state.rbPollMode = "";
        toast("NASブラウザの画面を取得できません: " + e.message, "error");
        return;
      }
      await delay(1000);
    }
  }
}

async function remoteLoop(tab, gen) {
  let fails = 0;
  // Size the remote page to this device before the first frame is requested.
  await nextPaint();
  await nextPaint();
  if (gen !== state.rbGen || !tab.rb) return;
  await sendViewport(tab).catch(() => {});
  while (gen === state.rbGen && tab.rb) {
    const rb = tab.rb;
    try {
      let target = "/api/rb/frame.jpg?tab=" + encodeURIComponent(rb.id) + "&since=" + rb.seq;
      if (rb.rev !== null) target += "&rev=" + rb.rev;
      const requestedAt = performance.now();
      const res = await fetch(target, {credentials: "same-origin", cache: "no-store"});
      const latency = performance.now() - requestedAt;
      if (gen !== state.rbGen || tab.rb !== rb) return;
      if (res.status === 401 && state.authed) {
        state.authed = false;
        setTimeout(go, 0);
        return;
      }
      if (res.status === 404) { remoteLost(tab); return; }
      if (res.status !== 200 && res.status !== 204) {
        let message = "HTTP " + res.status;
        try { message = (await res.json()).error || message; } catch (e) { /* not JSON */ }
        throw new Error(message);
      }
      fails = 0;
      const meta = frameHeaders(res);
      if (res.status === 200) {
        const blob = await res.blob();
        if (gen !== state.rbGen || tab.rb !== rb) return;
        if (blob.size > 0 && blob.type === "image/jpeg") {
          presentRemoteFrame(tab, rb, blob).catch(() => {});
          recordPerf(blob.size, latency);
        }
      } else {
        recordPerf(0, latency);
      }
      applyRemoteMeta(tab, meta);
    } catch (e) {
      if (gen !== state.rbGen) return;
      fails++;
      if (fails >= RB_MAX_POLL_FAILS) {
        state.rbPolling = "";
        toast("NASブラウザの画面を取得できません: " + e.message, "error");
        return;
      }
      await new Promise(resolve => setTimeout(resolve, 1000));
    }
  }
}

// One long-poll loop, only for the visible remote tab while the page is visible.
// Stopping the loop lets the Hub stop the screencast (5 s) and later the container.
function syncRemotePoll() {
  const tab = activeWebTab();
  const want = Boolean(state.authed && tab && tab.remote && tab.rb &&
    document.visibilityState === "visible" && currentViewName() === "web");
  const id = want ? tab.rb.id : "";
  const mode = want ? tabDisplay(tab) : "";
  if (id === state.rbPolling && mode === state.rbPollMode) return;
  closeStream();
  state.rbPolling = id;
  state.rbPollMode = mode;
  state.rbGen++;
  if (!want) return;
  const rb = tab.rb;
  if (rb.shown && rb.shown !== mode) leaveDisplay(rb, rb.shown);
  rb.shown = mode;
  if (mode === "live") {
    if (rb.streamFailed || typeof Image !== "function") remoteLoop(tab, state.rbGen);
    else streamLoop(tab, state.rbGen).catch(() => {});
    scheduleViewportLinks(tab, true);      // the link map of the live picture (v0.1.30)
  } else if (mode === "reader") {
    readerLoop(tab, state.rbGen).catch(() => {});
  } else {
    tileLoop(tab, state.rbGen).catch(() => {});
  }
}

function updateRemoteView(tab) {
  const remote = Boolean(tab && tab.remote && tab.history.length);
  setShown("remoteView", remote);
  $("frames").hidden = remote;
  if (!remote) { closeRemoteSheet(); setShown("interceptSheet", false); return; }
  const live = Boolean(tab.rb);
  const display = live ? tabDisplay(tab) : "tiles";
  const tiles = live && display === "tiles";
  const reader = live && display === "reader";
  setShown("tileScroll", tiles);
  setShown("readerScroll", reader);
  setShown("tileRefresh", tiles || reader);
  if (tiles) mountTiles(tab);
  if (reader) mountReader(tab);
  syncSnapProgress();
  $("remoteImg").hidden = tiles || reader || !live || !$("remoteImg").getAttribute("src");
  $("remoteIdle").hidden = live && (tiles ? Boolean(tab.rb.view) :
    reader ? Boolean(tab.rb.reader && tab.rb.reader.data) : !$("remoteImg").hidden);
  if (!live || display !== "live") paintLiveBoxes(null);
  $("remoteIdleText").textContent = tab.error || (live ? "画面を待っています…" :
    "NASブラウザのタブは開かれていません（メモリ節約のため自動で閉じました）。");
  setShown("remoteReopen", !live);
  setShown("remoteLoading", Boolean(tab.loading));
  setShown("autoLiveChip", live && tab.rb.autoLive === true && tab.rb.autoChip === true);
  const count = live ? tab.rb.links.length : 0;
  setShown("remoteLinks", count > 0);
  $("remoteLinks").textContent = "リンク " + count + "件";
  const blockedPopups = live ? tab.rb.popups : 0;
  setShown("remotePopups", blockedPopups > 0);
  $("remotePopups").textContent = "ポップアップ " + blockedPopups + "件ブロック";
  renderIntercept();
}

function scheduleRemoteLinks(tab) {
  const rb = tab.rb;
  if (!rb) return;
  clearTimeout(rb.linksTimer);
  rb.linksTimer = setTimeout(() => { refreshRemoteLinks(tab).catch(() => {}); }, RB_LINKS_DELAY_MS);
}

async function refreshRemoteLinks(tab) {
  const rb = tab.rb;
  if (!rb || state.rbPolling !== rb.id) return;
  const data = await api("/api/rb/links?tab=" + encodeURIComponent(rb.id));
  if (!tab.rb || tab.rb !== rb) return;
  rb.links = Array.isArray(data.items) ? data.items.filter(u => typeof u === "string").slice(0, 100) : [];
  if (activeWebTab() === tab) updateRemoteView(tab);
  if (!$("remoteSheet").hidden) renderRemoteSheet(tab);
}

function closeRemoteSheet() { setShown("remoteSheet", false); }

function renderRemoteSheet(tab) {
  const links = tab.rb ? tab.rb.links : [];
  $("remoteSheetList").replaceChildren(...links.map(u => element("span", u, "link-url")));
  $("remoteAdd").textContent = links.length + "件を追加";
  $("remoteAdd").disabled = links.length === 0;
  setShown("remoteSend", jd2CanSend() && links.length > 0);
}

async function openRemoteSheet() {
  const tab = remoteTab();
  if (!tab) return;
  await refreshRemoteLinks(tab).catch(() => {});
  renderRemoteSheet(tab);
  setShown("remoteSheet", true);
  loadJd2Status().then(() => { if (!$("remoteSheet").hidden) renderRemoteSheet(tab); }).catch(() => {});
}

// Stage through the existing extract/commit path (the Hub re-validates every URL).
async function stageRemoteLinks(tab) {
  return stageLinks(tab.rb.links.slice());
}

async function stageLinks(items) {
  if (!items.length) return items;
  const content = items.join("\n");
  const preview = await api("/api/rapidgator/extract", {content, limit: items.length});
  await api("/api/rapidgator/extract", {content, limit: items.length, commit: true, digest: preview.digest});
  swrDrop("links");
  return items;
}

async function remoteAdd() {
  const tab = remoteTab();
  if (!tab) return;
  const items = await stageRemoteLinks(tab);
  flash(items.length + "件を追加したリンクに入れました（JD2へは未送信）");
}

async function remoteSend() {
  const tab = remoteTab();
  if (!tab) return;
  await sendUrls(tab.rb.links.slice());
}

// Stage (the Hub re-validates every URL), then hand the staged rows to JD2.
async function sendUrls(links) {
  const items = await stageLinks(links);
  await loadQueue(true);
  const wanted = new Set(items);
  const staged = state.queueData ? state.queueData.items.filter(l => l.state === "staged" && wanted.has(l.url)) : [];
  const dup = items.length - staged.length;
  if (!staged.length) { flash("すべて送信済み・追加済みのためスキップしました（" + dup + "件）"); return; }
  await sendLinks(staged, {skipped: dup});
}

// ---- detected links (hoster pages / downloads stopped on the NAS browser)
async function fetchIntercepts(tab) {
  const rb = tab.rb;
  if (!rb || rb.icptFetching) return;
  rb.icptFetching = true;
  try {
    const data = await api("/api/rb/intercepts?tab=" + encodeURIComponent(rb.id) + "&since=" + rb.icptSeen);
    if (tab.rb !== rb) return;
    for (const item of Array.isArray(data.items) ? data.items : []) {
      if (!Number.isInteger(item.id) || item.id <= rb.icptSeen) continue;
      rb.icptSeen = item.id;
      if (typeof item.url !== "string" || !item.url.startsWith("https://")) continue;
      if (item.kind === "popup") {
        showPopupToast({tab: rb.id, id: item.id, url: item.url, host: hostOf(tab.current)});
        continue;
      }
      if (item.kind === "auto_sent") {
        swrDrop("links");
        flash(item.dup ? "すでにJD2へ送信済みのリンクです" : "JD2へ自動送信しました");
        continue;
      }
      if (item.kind === "popup_opened") {
        handlePopupOpened(tab, item).catch(e => toast(e.message, "error"));
        continue;
      }
      if (item.kind !== "hoster" && item.kind !== "download") continue;
      state.rbIntercepts.push({tab: rb.id, id: item.id, kind: item.kind, url: item.url});
    }
    state.rbIntercepts = state.rbIntercepts.slice(-RB_INTERCEPT_QUEUE);
    renderIntercept();
    loadJd2Status().then(renderIntercept).catch(() => {});
  } finally {
    rb.icptFetching = false;
  }
}

// Popups never move the tab by themselves: a small toast offers to open the URL.
// One toast at a time (the latest popup wins); it disappears after 8 s.
const rbPopup = {item: null, timer: 0};

function hostLabel(url) {
  try { return new URL(url).hostname; } catch (e) { return ""; }
}

function hidePopupToast() {
  clearTimeout(rbPopup.timer);
  rbPopup.item = null;
  setShown("popupToast", false);
}

function showPopupToast(item) {
  clearTimeout(rbPopup.timer);
  rbPopup.item = item;
  $("popupToastText").textContent = "ポップアップ: " + hostLabel(item.url) + " を開く";
  $("popupToastText").title = item.url;
  setShown("popupToast", true);
  rbPopup.timer = setTimeout(hidePopupToast, RB_POPUP_TOAST_MS);
}

async function openPopupToast() {
  const item = rbPopup.item;
  if (!item) return;
  hidePopupToast();
  await api("/api/rb/intercepts/open", {tab: item.tab, id: item.id});
}

async function setSiteRule(host, rule) {
  const saved = await api("/api/rb/site-rules", Object.assign({host}, rule));
  state.siteRules = null;
  swrDrop("site-rules");
  return saved;
}

// 常に許可 / 常にブロック: remember the policy for the page's host (the Hub
// applies it from the next popup on); "allow" also opens this one now.
async function popupRule(policy) {
  const item = rbPopup.item;
  if (!item || !item.host) return;
  hidePopupToast();
  await setSiteRule(item.host, {popup: policy});
  if (policy === "allow") await api("/api/rb/intercepts/open", {tab: item.tab, id: item.id});
  flash(item.host + (policy === "allow" ? " のポップアップを今後は新しいタブで開きます" :
    " のポップアップを今後はブロックします"));
}

// A site set to "allow": the Hub tells us a popup should open in a NEW NAS tab
// (or already navigated the current tab when the tab/memory limits are reached).
async function handlePopupOpened(tab, item) {
  const host = hostLabel(item.url);
  if (item.where !== RB_POPUP_NEW_TAB) {
    flash("ポップアップ（" + host + "）をこのタブで開きました");
    return;
  }
  if (state.tabs.length < MAX_WEB_TABS) {
    const fresh = createTab();
    try {
      await openRemote(fresh, item.url, url => pushHistory(fresh, url));
      flash("ポップアップ（" + host + "）を新しいタブで開きました");
      return;
    } catch (e) {
      closeTab(fresh.id);
    }
  }
  if (tab.rb) {
    await api("/api/rb/intercepts/open", {tab: tab.rb.id, id: item.id});
    flash("ポップアップ（" + host + "）をこのタブで開きました");
  }
}

function currentIntercept() {
  const tab = remoteTab();
  if (!tab) return null;
  const list = state.rbIntercepts.filter(item => item.tab === tab.rb.id);
  return list.length ? {item: list[0], more: list.length - 1} : null;
}

function renderIntercept() {
  const cur = currentIntercept();
  setShown("interceptSheet", Boolean(cur) && !$("remoteView").hidden);
  const tab = remoteTab();
  if (rbPopup.item && (!tab || tab.rb.id !== rbPopup.item.tab || $("remoteView").hidden)) hidePopupToast();
  if (!cur) return;
  const canSend = jd2CanSend();
  $("interceptKind").textContent = cur.item.kind === "download" ?
    "ファイルのダウンロードを止めました" : "Rapidgatorのページを開かずに止めました";
  $("interceptUrl").textContent = cur.item.url;
  $("interceptUrl").title = cur.item.url;
  $("interceptMore").textContent = cur.more > 0 ? "ほか" + cur.more + "件が待機中" : "";
  setShown("interceptSend", canSend);
  setShown("interceptAutoRow", canSend && cur.item.kind === "hoster" && Boolean(hostOf(tab.current)));
  setShown("interceptOpen", cur.item.kind === "hoster");
  $("interceptAdd").classList.toggle("primary", !canSend);
}

function dropIntercept(item) {
  $("interceptAuto").checked = false;
  state.rbIntercepts = state.rbIntercepts.filter(other => other !== item);
  renderIntercept();
}

async function interceptAction(action) {
  const cur = currentIntercept();
  if (!cur) return;
  const item = cur.item;
  const body = {tab: item.tab, id: item.id};
  if (action === "send") {
    const autoHost = $("interceptAuto").checked ? hostOf(remoteTab().current) : "";
    const sentResult = await api("/api/rb/intercepts/send", body);
    swrDrop("links");
    await loadQueue(true).catch(() => {});
    flash(sentResult && sentResult.state === "dup" ? "送信済みのためスキップしました" : sentText());
    if (autoHost) {
      $("interceptAuto").checked = false;
      await setSiteRule(autoHost, {auto_send: true});
      flash(autoHost + " は今後自動でJD2へ送ります");
    }
  } else if (action === "add") {
    await api("/api/rb/intercepts/add", body);
    swrDrop("links");
    flash("追加したリンクに入れました（JD2へは未送信）");
  } else if (action === "open") {
    await api("/api/rb/intercepts/open", body);
  }
  dropIntercept(item);
}

// ---- speed overlay (client side only): frames/s and bytes over the last 5 s,
// and request-to-response time of the frame poll.
function recordPerf(bytes, latency) {
  if (!state.rbPerf) return;
  const now = performance.now();
  state.rbPerfSamples.push({t: now, bytes, latency});
  const cutoff = now - RB_PERF_WINDOW_MS;
  while (state.rbPerfSamples.length && state.rbPerfSamples[0].t < cutoff) state.rbPerfSamples.shift();
}

function perfText(tab = remoteTab()) {
  const rb = tab && tab.rb;
  const p = (rb && rb.perf) || {};
  const quality = RB_QUALITY_LABELS[state.rbQuality] || "";
  const kb = n => (n / 1024).toFixed(0) + " KB";
  const display = rb ? tabDisplay(tab) : "live";
  if (display === "tiles") {
    const tiles = rb.view ? rb.view.tiles : [];
    const loaded = tiles.filter(t => t.url).length;
    const each = p.tiles ? p.tileBytes / p.tiles : 0;
    return "高速(全体表示)・" + quality + " · タイル " + loaded + "/" + tiles.length +
      " · 撮影 " + Math.round(p.snapMs || 0) + " ms · 1枚 " + kb(each);
  }
  if (display === "reader") {
    const blocks = rb.reader && rb.reader.data ? rb.reader.data.blocks : [];
    const total = blocks.filter(b => b.t === "img").length;
    const each = p.imgs ? p.imgMs / p.imgs : 0;
    return "リーダー · ブロック " + blocks.length + " · 画像 " + (p.imgs || 0) + "/" + total +
      " · 取得 " + Math.round(p.extractMs || 0) + " ms" + (p.imgs ? " · 画像取得 " + Math.round(each) + " ms" : "");
  }
  if (rb && rb.stream && !rb.streamFailed) {
    return "ライブ(ストリーム)・" + quality + " · " + (p.fps || 0).toFixed(1) + " fps · " +
      kb(p.frameBytes || 0) + "/枚 · 応答 " + Math.round(p.streamMs || 0) + " ms";
  }
  const now = performance.now();
  const recent = state.rbPerfSamples.filter(x => x.t >= now - RB_PERF_WINDOW_MS);
  const frames = recent.filter(x => x.bytes > 0);
  const elapsed = Math.max(1, Math.min(RB_PERF_WINDOW_MS, now - state.rbPerfSince) / 1000);
  const fps = frames.length / elapsed;
  const bytes = frames.length ? frames.reduce((sum, x) => sum + x.bytes, 0) / frames.length : 0;
  const wait = recent.length ? recent.reduce((sum, x) => sum + x.latency, 0) / recent.length : 0;
  return "ライブ(ポーリング)・" + quality + " · " + fps.toFixed(1) + " fps · " + kb(bytes) +
    "/枚 · 応答 " + Math.round(wait) + " ms";
}

function renderPerf() {
  $("perfOverlay").textContent = perfText();
}

function setPerf(on) {
  state.rbPerf = on;
  state.rbPerfSamples = [];
  state.rbPerfSince = performance.now();
  clearInterval(state.rbPerfTimer);
  state.rbPerfTimer = 0;
  setShown("perfOverlay", on);
  if (on) {
    renderPerf();
    state.rbPerfTimer = setInterval(renderPerf, 1000);
  }
}

// ---- render quality (speed / standard / high), remembered on this device
function setRbQuality(mode) {
  if (!RB_QUALITIES.includes(mode)) return;
  state.rbQuality = mode;
  writeLocal(RB_QUALITY_KEY, mode);
  const tab = remoteTab();
  if (tab) {
    tab.rb.vp = "";
    sendViewport(tab).catch(e => toast(e.message, "error"));
  }
  flash("画質: " + RB_QUALITY_LABELS[mode]);
}

// ---- fast full-page display (v0.1.28) ----
// The Hub measures the page and renders it as JPEG tiles on demand. The phone scrolls them
// natively (no round trip per scroll). Tiles are only images: <img> elements fed with blob:
// object URLs of /api/rb/tile.jpg bytes. The link map is text used for hit testing; every
// href is validated again by the Hub before it is acted on.
const tileHost = {owner: null};
const tileOf = new WeakMap();
const tileQueue = [];
let tileActive = 0;
let tileNear = null, tileFar = null;

// The display the user chose: the site's remembered choice, else the Settings default (高速).
function userDisplay(tab) {
  const remembered = DISPLAY_OF_MODE[rememberedMode(hostOf(tab.current || ""))];
  if (remembered) return remembered;
  const setting = state.settings && state.settings.rb_default_mode;
  return DISPLAY_OF_SETTING[setting] || "tiles";
}

// What is shown now: "tiles" (fast), "live" or "reader". A Cloudflare check forces "live" meanwhile.
function tabDisplay(tab) {
  const rb = tab && tab.rb;
  if (!rb) return "tiles";
  if (rb.autoLive) return "live";
  return userDisplay(tab);
}

// 「表示: リーダー / 高速 / ライブ」 from the menu: remembered per host.
function setUserDisplay(tab, mode) {
  if (!tab || !tab.rb || !RB_DISPLAY_LABELS[mode]) return;
  const host = hostOf(tab.current || "");
  if (host) rememberHostMode(host, MODE_OF_DISPLAY[mode]);
  stopAutoPoll(tab.rb);
  tab.rb.autoLive = false; tab.rb.autoChip = false;   // a manual choice is respected
  renderRemoteHosts();
  applyDisplay(tab);
  flash("表示: " + RB_DISPLAY_LABELS[mode]);
}

function applyDisplay(tab) {
  if (!tab || !tab.rb) return;
  syncRemotePoll();
  if (activeWebTab() === tab) updateRemoteView(tab);
}

// Leaving a display frees what it held: tiles (phone and Hub) or the live picture.
function leaveDisplay(rb, shown) {
  if (shown === "tiles") {
    dropTiles(rb);
    api("/api/rb/snapshot/release", {tab: rb.id}).catch(() => {});
  } else if (shown === "reader") {
    dropReader(rb);
  } else {
    clearTimeout(rb.vmapTimer);
    rb.vmap = null; rb.vmapRev = -1; rb.linksRev = -1;
    paintLiveBoxes(rb.owner);
    dropRemoteImage(rb);
  }
}

function tileObservers() {
  if (tileNear || typeof IntersectionObserver !== "function") return;
  const root = $("tileScroll");
  // Load what is within one screen of the viewport ...
  tileNear = new IntersectionObserver(entries => {
    for (const entry of entries) {
      const t = tileOf.get(entry.target);
      if (!t || t.view.dead || !entry.isIntersecting) continue;
      t.want = true;
      loadTile(t);
    }
  }, {root, rootMargin: "100% 0px 100% 0px"});
  // ... and release the object URL of tiles that are three screens away.
  tileFar = new IntersectionObserver(entries => {
    for (const entry of entries) {
      const t = tileOf.get(entry.target);
      if (t && !entry.isIntersecting) { t.want = false; releaseTile(t); }
    }
  }, {root, rootMargin: "300% 0px 300% 0px"});
}

function buildView(rb, meta) {
  const canvas = element("div", undefined, "tile-canvas");
  const view = {rb, meta, canvas, tiles: [], dead: false, k: 1};
  for (let i = 0; i < meta.tiles; i++) {
    const img = document.createElement("img");
    img.className = "tile-img";
    img.alt = "";
    img.draggable = false;
    const t = {view, index: i, img, url: "", want: false, forced: false, promise: null, done: null};
    tileOf.set(img, t);
    view.tiles.push(t);
    canvas.appendChild(img);
  }
  view.boxes = element("div", undefined, "link-boxes");     // optional outlines (setting), no pointer events
  view.boxes.setAttribute("aria-hidden", "true");
  canvas.appendChild(view.boxes);
  layoutView(view);
  return view;
}

// Tile boxes in CSS pixels of the phone: page pixels times k (container width / page width).
function layoutView(view) {
  const width = $("tileScroll").clientWidth || view.meta.width;
  const k = width / view.meta.width;
  view.k = k;
  view.canvas.style.height = Math.round(view.meta.height * k) + "px";
  for (const t of view.tiles) {
    const top = t.index * view.meta.tile_height;
    const bottom = Math.min(view.meta.height, top + view.meta.tile_height);
    const a = Math.round(top * k), b = Math.round(bottom * k);
    t.img.style.top = a + "px";
    t.img.style.height = Math.max(1, b - a) + "px";
  }
  paintBoxes(view.boxes, view.meta.links, k, 0, 0);
}

// Lazy loading: tiles near the viewport load, tiles far away release their object URLs.
function observeView(view) {
  if (view.observed || view.dead) return;
  tileObservers();
  view.observed = true;
  if (tileNear) {
    for (const t of view.tiles) { tileNear.observe(t.img); tileFar.observe(t.img); }
  } else {
    for (const t of view.tiles) { t.want = true; loadTile(t); }   // no IntersectionObserver: load all
  }
}

function releaseTile(t) {
  if (!t.url) return;
  URL.revokeObjectURL(t.url);
  t.url = "";
  t.img.removeAttribute("src");
}

function killView(view) {
  if (!view || view.dead) return;
  view.dead = true;
  for (const t of view.tiles) {
    if (tileNear) tileNear.unobserve(t.img);
    if (tileFar) tileFar.unobserve(t.img);
    t.want = false;
    releaseTile(t);
  }
  view.canvas.remove();
}

function dropTiles(rb) {
  clearTimeout(rb.snapTimer);
  if (rb.view) killView(rb.view);
  rb.view = null;
  rb.snap = null;
  rb.snapKey = "";
  rb.snapAgain = null;
  if (tileHost.owner === rb) { tileHost.owner = null; $("tileScroll").replaceChildren(); }
  rb.busy = 0;
  syncSnapProgress();
}

// The scroll container is shared by all remote tabs: only its owner's canvas is mounted.
function mountTiles(tab) {
  const rb = tab.rb;
  const scroller = $("tileScroll");
  if (!rb || tileHost.owner === rb) return;
  if (tileHost.owner) tileHost.owner.scrollTop = scroller.scrollTop;
  tileHost.owner = rb;
  scroller.replaceChildren(...(rb.view ? [rb.view.canvas] : []));
  scroller.scrollTop = rb.scrollTop || 0;
  if (rb.view) { layoutView(rb.view); observeView(rb.view); }
}

function syncSnapProgress() {
  const tab = activeWebTab();
  setShown("snapProgress", Boolean(tab && tab.rb && tab.rb.busy > 0 && tabDisplay(tab) !== "live"));
}

function snapBusy(rb, delta) {
  rb.busy = Math.max(0, rb.busy + delta);
  syncSnapProgress();
}

function loadTile(t) {
  if (t.view.dead) return Promise.resolve();
  if (t.promise) return t.promise;
  if (t.url) return Promise.resolve();
  t.promise = new Promise(resolve => { t.done = resolve; });
  tileQueue.push(t);
  pumpTiles();
  return t.promise;
}

function settleTile(t) {
  const done = t.done;
  t.promise = null;
  t.done = null;
  if (done) done();
}

function pumpTiles() {
  while (tileActive < RB_TILE_CONCURRENCY && tileQueue.length) {
    const t = tileQueue.shift();
    if (t.view.dead || (!t.want && !t.forced)) { settleTile(t); continue; }
    tileActive++;
    fetchTile(t).then(ok => {
      if (!ok && !t.view.dead && t.want) setTimeout(() => { if (t.want) loadTile(t); }, 2000);
    }).catch(() => {}).finally(() => { tileActive--; settleTile(t); pumpTiles(); });
  }
}

// true when finished (or deliberately dropped); false to retry later.
async function fetchTile(t) {
  const rb = t.view.rb;
  const began = performance.now();
  try {
    const res = await fetch("/api/rb/tile.jpg?tab=" + encodeURIComponent(rb.id) +
      "&snap=" + encodeURIComponent(t.view.meta.snap_id) + "&i=" + t.index,
      {credentials: "same-origin", cache: "no-store"});
    if (t.view.dead) return true;
    if (res.status === 401 && state.authed) { state.authed = false; setTimeout(go, 0); return true; }
    if (res.status === 404) {          // the Hub dropped this snapshot: take a new one
      if (rb.owner) takeSnapshot(rb.owner, {force: true, keepScroll: true, quiet: true}).catch(() => {});
      return true;
    }
    if (res.status !== 200) return false;
    const blob = await res.blob();
    if (t.view.dead || !blob.size || blob.type !== "image/jpeg") return true;
    if (!t.want && !t.forced) return true;      // scrolled far away meanwhile
    rb.perf.tiles = (rb.perf.tiles || 0) + 1;
    rb.perf.tileBytes = (rb.perf.tileBytes || 0) + blob.size;
    rb.perf.tileMs = performance.now() - began;
    releaseTile(t);
    t.url = URL.createObjectURL(blob);
    t.img.src = t.url;
    try { await t.img.decode(); } catch (e) { /* shown anyway */ }
    return true;
  } catch (e) {
    return false;
  }
}

const delay = ms => new Promise(resolve => setTimeout(resolve, ms));

// Measure the page on the Hub and swap the tiles in. The old view stays until the first
// tiles of the new one are decoded, so the screen never goes blank.
async function takeSnapshot(tab, opts = {}) {
  const rb = tab && tab.rb;
  if (!rb || tabDisplay(tab) !== "tiles") return;
  if (rb.snapBusy) {
    const again = rb.snapAgain || {};
    rb.snapAgain = {force: again.force === true || opts.force === true,
      ifChanged: again.ifChanged !== false && opts.ifChanged === true,
      keepScroll: opts.keepScroll !== false, quiet: opts.quiet === true};
    return;
  }
  rb.snapBusy = true;
  snapBusy(rb, 1);
  try {
    const started = performance.now();
    const meta = await api("/api/rb/snapshot", {tab: rb.id, force: opts.force === true,
      if_changed: opts.ifChanged === true});
    rb.perf.snapMs = performance.now() - started;
    if (tab.rb !== rb || tabDisplay(tab) !== "tiles") return;
    if (rb.view && rb.view.meta.snap_id === meta.snap_id) { rb.view.meta = meta; rb.snap = meta; return; }
    await swapView(tab, rb, meta, opts);
  } catch (e) {
    if (e.status === 404) remoteLost(tab);
    else if (!opts.quiet) toast(e.message, "error");
  } finally {
    rb.snapBusy = false;
    snapBusy(rb, -1);
    const again = rb.snapAgain;
    rb.snapAgain = null;
    if (again && tab.rb === rb) takeSnapshot(tab, again).catch(() => {});
  }
}

async function swapView(tab, rb, meta, opts) {
  tileObservers();
  const scroller = $("tileScroll");
  const old = rb.view;
  const mounted = tileHost.owner === rb;
  const sameUrl = Boolean(old) && old.meta.url === meta.url;
  // Same page: keep the position. A different address starts at the top.
  const keepTop = mounted && sameUrl && opts.keepScroll !== false ? scroller.scrollTop : 0;
  const view = buildView(rb, meta);
  if (mounted) {
    view.canvas.classList.add("is-staging");
    scroller.appendChild(view.canvas);
    layoutView(view);
    const top = keepTop, bottom = keepTop + scroller.clientHeight;
    const need = view.tiles.filter(t => {
      const a = parseFloat(t.img.style.top), b = a + parseFloat(t.img.style.height);
      return b > top && a < bottom;
    });
    for (const t of need) t.forced = true;
    await Promise.race([Promise.all(need.map(loadTile)), delay(RB_SWAP_WAIT_MS)]);
    for (const t of need) t.forced = false;
    if (tab.rb !== rb || tileHost.owner !== rb) { killView(view); return; }
    view.canvas.classList.remove("is-staging");
  }
  if (old) killView(old);
  rb.view = view;
  rb.snap = meta;
  rb.snapKey = meta.url + "\n" + meta.title;
  if (mounted) {
    scroller.scrollTop = keepTop;
    observeView(view);
  }
  updateRemoteView(tab);
}

function scheduleSnapshot(tab, ms) {
  const rb = tab.rb;
  if (!rb) return;
  clearTimeout(rb.snapTimer);
  rb.snapTimer = setTimeout(() => {
    takeSnapshot(tab, {force: true, keepScroll: true, quiet: true}).catch(() => {});
  }, ms);
}

// Tab metadata long-poll (no screencast). Navigation settling triggers a snapshot; a poll that
// times out with no change checks for DOM changes about every 10 s.
async function tileLoop(tab, gen) {
  let fails = 0;
  await nextPaint();
  await nextPaint();
  if (gen !== state.rbGen || !tab.rb) return;
  await sendViewport(tab).catch(() => {});
  if (gen !== state.rbGen || !tab.rb) return;
  takeSnapshot(tab, {keepScroll: true, quiet: true}).catch(() => {});
  while (gen === state.rbGen && tab.rb) {
    const rb = tab.rb;
    try {
      let target = "/api/rb/meta?tab=" + encodeURIComponent(rb.id) + "&wait=" + RB_META_WAIT_S;
      if (rb.rev !== null) target += "&rev=" + rb.rev;
      const res = await fetch(target, {credentials: "same-origin", cache: "no-store"});
      if (gen !== state.rbGen || tab.rb !== rb) return;
      if (res.status === 401 && state.authed) {
        state.authed = false;
        setTimeout(go, 0);
        return;
      }
      if (res.status === 404) { remoteLost(tab); return; }
      if (res.status !== 200) throw new Error("HTTP " + res.status);
      const meta = await res.json();
      if (gen !== state.rbGen || tab.rb !== rb) return;
      fails = 0;
      const unchanged = meta.rev === rb.rev;
      applyRemoteMeta(tab, Object.assign({}, meta, {seq: rb.seq}));
      if (gen !== state.rbGen || tab.rb !== rb || tabDisplay(tab) !== "tiles") return;
      if (meta.loading !== true && rb.snap !== null && rb.snapKey !== (meta.url + "\n" + meta.title)) {
        takeSnapshot(tab, {keepScroll: true, quiet: true}).catch(() => {});
      } else if (meta.loading !== true && unchanged && rb.snap !== null) {
        takeSnapshot(tab, {ifChanged: true, keepScroll: true, quiet: true}).catch(() => {});
      }
    } catch (e) {
      if (gen !== state.rbGen) return;
      fails++;
      if (fails >= RB_MAX_POLL_FAILS) {
        state.rbPolling = "";
        state.rbPollMode = "";
        toast("NASブラウザの画面を取得できません: " + e.message, "error");
        return;
      }
      await delay(1000);
    }
  }
}

// Page coordinates of a pointer: tiles are scaled by k, the container scrolls natively.
function tilePoint(event) {
  const tab = remoteTab();
  const view = tab && tab.rb.view;
  if (!view) return null;
  const scroller = $("tileScroll");
  const box = scroller.getBoundingClientRect();
  const x = (event.clientX - box.left) / view.k;
  const y = (event.clientY - box.top + scroller.scrollTop) / view.k;
  if (!(x >= 0 && y >= 0 && x <= view.meta.width && y <= view.meta.height)) return null;
  return {x, y};
}

// Link hit testing (all displays, v0.1.30). Boxes are {x, y, w, h, href} in remote CSS pixels.
// Distance from a point to a box (0 inside): a press slightly off a link still counts.
function boxDistance(l, x, y) {
  const dx = Math.max(l.x - x, 0, x - (l.x + l.w));
  const dy = Math.max(l.y - y, 0, y - (l.y + l.h));
  return Math.hypot(dx, dy);
}

// The box that contains the point; else the nearest one within `near` px. Equal distance: the
// smaller box wins. null when nothing is that close (the Hub then resolves it by coordinates).
function pickLink(links, x, y, near = RB_LINK_NEAR_PX) {
  if (!Array.isArray(links)) return null;
  let best = null, bestD = Infinity, bestA = Infinity;
  for (const l of links) {
    const d = boxDistance(l, x, y);
    if (d > near) continue;
    const a = l.w * l.h;
    if (d < bestD - 1e-9 || (Math.abs(d - bestD) <= 1e-9 && a < bestA)) { best = l; bestD = d; bestA = a; }
  }
  return best;
}

function linkAt(snap, x, y) {
  return pickLink(snap && snap.links, x, y);
}

async function tileTap(tab, point) {
  const rb = tab.rb;
  if (!rb || !rb.snap) return;
  snapBusy(rb, 1);
  try {
    const r = await api("/api/rb/tap-page", {tab: rb.id, snap: rb.snap.snap_id,
      x: Math.round(point.x), y: Math.round(point.y)});
    if (tab.rb !== rb) return;
    if (typeof r.editable === "boolean") setEditable(tab, r.editable, true);
    await takeSnapshot(tab, {force: true, keepScroll: true, quiet: true});
  } catch (e) {
    if (tab.rb !== rb) return;
    if (e.status === 404) {         // snapshot replaced meanwhile (or the tab is gone)
      flash("表示を更新しました。もう一度タップしてください");
    } else {
      toast(e.message, "error");
    }
    await takeSnapshot(tab, {force: true, keepScroll: true, quiet: true});
  } finally {
    snapBusy(rb, -1);
  }
}

function tileLongPress(tab, clientX, clientY) {
  const point = tilePoint({clientX, clientY});
  const snap = tab.rb.snap;
  if (!point || !snap) { cancelPress(); flash("この位置にリンクがありません"); return; }
  const link = linkAt(snap, point.x, point.y);
  rbPress.fired = true;
  $("remoteRing").classList.add("is-done");
  setTimeout(() => { rbPress.fired = false; hidePressRing(); }, 350);
  // Known locally: the probe starts at once. Otherwise the Hub resolves the point itself (it also
  // looks for the nearest link within the same tolerance).
  perform(() => startProbe(tab, link ? {href: link.href} :
    {x: Math.round(point.x), y: Math.round(point.y), snap: snap.snap_id}));
}

// Pull down at the top of a scroller to refresh (tiles and reader).
function wirePull(scroller, onRefresh) {
  let startY = null;
  scroller.addEventListener("touchstart", event => {
    startY = scroller.scrollTop <= 0 && event.touches.length === 1 ? event.touches[0].clientY : null;
  }, {passive: true});
  scroller.addEventListener("touchend", event => {
    const start = startY;
    startY = null;
    if (start === null || !event.changedTouches.length) return;
    if (event.changedTouches[0].clientY - start > RB_PULL_PX && scroller.scrollTop <= 0) onRefresh();
  }, {passive: true});
}

function wireTiles() {
  const scroller = $("tileScroll");
  const press = {id: null, x0: 0, y0: 0, t0: 0, moved: false};
  const fast = () => {
    const tab = remoteTab();
    return tab && tabDisplay(tab) === "tiles" && tab.rb.view ? tab : null;
  };
  scroller.addEventListener("pointerdown", event => {
    const tab = fast();
    if (!tab || press.id !== null) return;
    press.id = event.pointerId;
    press.x0 = event.clientX; press.y0 = event.clientY;
    press.t0 = Date.now();
    press.moved = false;
    cancelPress();
    rbPress.fired = false;
    const x0 = event.clientX, y0 = event.clientY;
    rbPress.ringTimer = setTimeout(() => showPressRing(x0, y0), RB_RING_DELAY_MS);
    rbPress.timer = setTimeout(() => {
      const current = fast();
      if (press.id !== null && !press.moved && current) tileLongPress(current, x0, y0);
    }, RB_LONGPRESS_MS);
  });
  scroller.addEventListener("pointermove", event => {
    if (event.pointerId !== press.id) return;
    if (Math.hypot(event.clientX - press.x0, event.clientY - press.y0) > RB_TAP_MOVE_PX) {
      press.moved = true;
      cancelPress();
    }
  });
  scroller.addEventListener("pointerup", event => {
    if (event.pointerId !== press.id) return;
    press.id = null;
    const longPressed = rbPress.fired;
    cancelPress();
    const tab = fast();
    if (!tab || press.moved || longPressed) return;
    if (Date.now() - press.t0 > RB_TAP_MAX_MS) return;
    const point = tilePoint(event);
    if (point) perform(() => tileTap(tab, point));
  });
  // Scrolling starts a pointercancel: that is not a tap.
  scroller.addEventListener("pointercancel", event => {
    if (event.pointerId !== press.id) return;
    press.id = null;
    cancelPress();
  });
  // Pull down at the top to refresh.
  wirePull(scroller, () => {
    const tab = fast();
    if (tab) perform(() => takeSnapshot(tab, {force: true, keepScroll: true}));
  });
  $("tileRefresh").addEventListener("click", () => {
    const tab = remoteTab();
    if (!tab) return;
    if (tabDisplay(tab) === "reader") perform(() => runReader(tab, {keepScroll: true}));
    else if (fast()) perform(() => takeSnapshot(tab, {force: true, keepScroll: true}));
  });
  if (typeof ResizeObserver === "function") {
    new ResizeObserver(() => {
      const tab = remoteTab();
      if (tab && tab.rb.view && tileHost.owner === tab.rb) layoutView(tab.rb.view);
    }).observe(scroller);
  }
}

// ---- optional link outlines (setting rb_link_boxes, v0.1.30) ----
// A client overlay of thin outlines over the link boxes; pointer-events none, nothing from the page.
function boxesWanted() { return Boolean(state.settings && state.settings.rb_link_boxes === true); }

function paintBoxes(layer, links, k, dx, dy) {
  if (!layer) return;
  layer.replaceChildren();
  if (!boxesWanted() || !Array.isArray(links)) return;
  const frag = document.createDocumentFragment();
  for (let i = 0; i < links.length && i < RB_BOX_CAP; i++) {
    const l = links[i];
    const box = document.createElement("div");
    box.className = "link-box";
    box.style.left = Math.round(l.x * k + dx) + "px";
    box.style.top = Math.round(l.y * k + dy) + "px";
    box.style.width = Math.max(1, Math.round(l.w * k)) + "px";
    box.style.height = Math.max(1, Math.round(l.h * k)) + "px";
    frag.appendChild(box);
  }
  layer.appendChild(frag);
}

function paintLiveBoxes(tab) {
  const layer = $("liveBoxes");
  const rb = tab && tab.rb;
  if (!rb || !rb.vmap || !boxesWanted() || tabDisplay(tab) !== "live" || activeWebTab() !== tab) {
    layer.replaceChildren();
    return;
  }
  const img = $("remoteImg").getBoundingClientRect();
  const stage = $("remoteStage").getBoundingClientRect();
  if (!img.width || !rb.width) { layer.replaceChildren(); return; }
  paintBoxes(layer, rb.vmap.links, img.width / rb.width, img.left - stage.left, img.top - stage.top);
}

// The setting changed (this or another device): redraw the outlines and re-evaluate the display.
function syncRemoteSettings() {
  for (const tab of state.tabs) {
    if (tab.rb && tab.rb.view) paintBoxes(tab.rb.view.boxes, tab.rb.view.meta.links, tab.rb.view.k, 0, 0);
  }
  const tab = remoteTab();
  if (tab) { paintLiveBoxes(tab); applyDisplay(tab); }
}

// ---- live display link map (v0.1.30) ----
// The Hub reports the links of the visible viewport ({rev, scrollX, scrollY, links:[{x,y,w,h,href}]},
// CSS px relative to the viewport). Every live frame carries X-Frame-Links-Rev; when it differs from
// the revision held here the map is refreshed (throttled), and only while the live display is shown.
function cleanViewportMap(vl) {
  if (!vl || !Array.isArray(vl.links)) return null;
  const num = v => typeof v === "number" && Number.isFinite(v);
  const links = [];
  for (const l of vl.links) {
    if (links.length >= 600) break;
    if (l && num(l.x) && num(l.y) && num(l.w) && num(l.h) && l.w > 0 && l.h > 0 &&
        typeof l.href === "string" && /^https?:\/\//i.test(l.href)) {
      links.push({x: l.x, y: l.y, w: l.w, h: l.h, href: l.href});
    }
  }
  return {rev: vl.rev, scrollX: num(vl.scrollX) ? vl.scrollX : 0, scrollY: num(vl.scrollY) ? vl.scrollY : 0, links};
}

function noteLinksRev(tab, rev) {
  const rb = tab && tab.rb;
  if (!rb || tabDisplay(tab) !== "live") return;
  rb.linksRev = rev;
  if (rev !== rb.vmapRev) scheduleViewportLinks(tab, false);
}

function scheduleViewportLinks(tab, immediate) {
  const rb = tab && tab.rb;
  if (!rb || rb.vmapTimer || tabDisplay(tab) !== "live") return;
  // The page flags a scroll end 200 ms after the last scroll event: ask a little later than that.
  const wait = Math.max(immediate ? 0 : RB_VP_DEBOUNCE_MS, rb.vmapAt + RB_VP_MIN_MS - Date.now());
  rb.vmapTimer = setTimeout(() => {
    rb.vmapTimer = 0;
    if (tab.rb !== rb || tabDisplay(tab) !== "live" || state.rbPolling !== rb.id ||
        document.visibilityState !== "visible") return;
    fetchViewportLinks(tab, false).catch(() => {});
  }, wait);
}

function fetchViewportLinks(tab, fresh) {
  const rb = tab && tab.rb;
  if (!rb) return Promise.resolve(null);
  if (rb.vmapBusy && !fresh) return rb.vmapBusy;
  rb.vmapAt = Date.now();
  const job = (async () => {
    try {
      let target = "/api/rb/meta?tab=" + encodeURIComponent(rb.id) + "&wait=0&links=1";
      if (rb.vmap && rb.vmapRev >= 0) target += "&vp=" + rb.vmapRev;
      if (fresh) target += "&fresh=1";
      const res = await fetch(target, {credentials: "same-origin", cache: "no-store"});
      if (res.status !== 200 || tab.rb !== rb) return rb.vmap;
      const meta = await res.json();
      const vl = meta && meta.viewport_links;
      if (tab.rb !== rb || !vl || !Number.isFinite(vl.rev)) return rb.vmap;
      if (vl.unchanged === true) {
        rb.vmapRev = vl.rev;
      } else {
        const map = cleanViewportMap(vl);
        if (map) { rb.vmap = map; rb.vmapRev = vl.rev; paintLiveBoxes(tab); }
      }
      return rb.vmap;
    } catch (e) {
      return rb.vmap;       // transient: the next frame change asks again
    } finally {
      if (rb.vmapBusy === job) rb.vmapBusy = null;
    }
  })();
  rb.vmapBusy = job;
  return job;
}

// ---- reader mode (v0.1.30) ----
// The Hub runs a fixed extractor in the NAS browser and returns validated blocks (headings,
// paragraphs with link runs, list items, images, preformatted text). Everything is built here with
// createElement/textContent: no remote HTML, script or style reaches this page. Links are <a> elements
// carrying data-href (never a real href); images are blob: URLs of /api/rb/reader/img bytes.
const readerHost = {owner: null, entries: new Set(), near: null, far: null, map: new WeakMap()};
const readerQueue = [];
let readerActive = 0;
const READER_TYPES = ["h", "p", "li", "img", "pre", "hr"];

function readerRunsOk(runs) {
  return Array.isArray(runs) && runs.every(r => r && typeof r.text === "string" &&
    (r.href === undefined || (typeof r.href === "string" && /^https?:\/\//i.test(r.href) && r.href.length <= 2048)));
}

// The Hub validated these already; the page checks types again before building anything.
function cleanReaderData(data) {
  if (!data || !Array.isArray(data.blocks)) return null;
  const blocks = data.blocks.filter(b => {
    if (!b || !READER_TYPES.includes(b.t)) return false;
    if (b.t === "h") return typeof b.text === "string" && Number.isInteger(b.level);
    if (b.t === "p" || b.t === "li") return readerRunsOk(b.runs);
    if (b.t === "pre") return typeof b.text === "string";
    if (b.t === "img") return Number.isInteger(b.i) && b.i >= 0;
    return true;
  }).slice(0, 5000);
  return {title: typeof data.title === "string" ? data.title : "", host: typeof data.host === "string" ? data.host : "",
    url: typeof data.url === "string" ? data.url : "", blocks, truncated: data.truncated === true};
}

function readerObservers() {
  if (readerHost.near || typeof IntersectionObserver !== "function") return;
  const root = $("readerScroll");
  readerHost.near = new IntersectionObserver(entries => {
    for (const entry of entries) {
      const e = readerHost.map.get(entry.target);
      if (e && entry.isIntersecting) { e.want = true; queueReaderImage(e); }
    }
  }, {root, rootMargin: "100% 0px 100% 0px"});
  readerHost.far = new IntersectionObserver(entries => {
    for (const entry of entries) {
      const e = readerHost.map.get(entry.target);
      if (e && !entry.isIntersecting) { e.want = false; releaseReaderImage(e); }
    }
  }, {root, rootMargin: "300% 0px 300% 0px"});
}

function releaseReaderImage(e) {
  if (!e.url) return;
  if (!e.img.style.aspectRatio) e.img.style.height = e.img.offsetHeight + "px";   // keep the layout stable
  URL.revokeObjectURL(e.url);
  e.url = "";
  e.img.removeAttribute("src");
}

function clearReaderBody() {
  for (const e of readerHost.entries) {
    e.dead = true;
    e.want = false;
    if (readerHost.near) { readerHost.near.unobserve(e.img); readerHost.far.unobserve(e.img); }
    if (e.url) { URL.revokeObjectURL(e.url); e.url = ""; }
  }
  readerHost.entries.clear();
  readerQueue.length = 0;
  $("readerBody").replaceChildren();
}

function dropReader(rb) {
  rb.reader = null;
  rb.readerPending = false;
  rb.readerAgain = false;
  if (readerHost.owner === rb) {
    readerHost.owner = null;
    clearReaderBody();
    $("readerTitle").textContent = "";
    $("readerHost").textContent = "";
  }
  rb.busy = 0;
  syncSnapProgress();
}

function queueReaderImage(e) {
  if (e.dead || e.url || e.queued) return;
  if (e.failedAt && Date.now() - e.failedAt < 10000) return;
  e.queued = true;
  readerQueue.push(e);
  pumpReaderImages();
}

function pumpReaderImages() {
  while (readerActive < RB_READER_IMG_CONCURRENCY && readerQueue.length) {
    const e = readerQueue.shift();
    e.queued = false;
    if (e.dead || !e.want || e.url) continue;
    readerActive++;
    fetchReaderImage(e).catch(() => {}).finally(() => { readerActive--; pumpReaderImages(); });
  }
}

async function fetchReaderImage(e) {
  const began = performance.now();
  try {
    const res = await fetch("/api/rb/reader/img?tab=" + encodeURIComponent(e.rb.id) + "&i=" + e.index,
      {credentials: "same-origin", cache: "no-store"});
    if (e.dead) return;
    if (res.status === 401 && state.authed) { state.authed = false; setTimeout(go, 0); return; }
    if (res.status !== 200) { e.failedAt = Date.now(); e.img.classList.add("is-failed"); return; }
    const blob = await res.blob();
    if (e.dead || !e.want || e.url) return;
    if (!blob.size || !/^image\/(jpeg|png|gif|webp|avif)$/.test(blob.type)) {
      e.failedAt = Date.now();
      e.img.classList.add("is-failed");
      return;
    }
    e.rb.perf.imgs = (e.rb.perf.imgs || 0) + 1;
    e.rb.perf.imgMs = (e.rb.perf.imgMs || 0) + (performance.now() - began);
    e.url = URL.createObjectURL(blob);
    e.img.classList.remove("is-failed");
    e.img.style.height = "";
    e.img.src = e.url;
  } catch (err) {
    e.failedAt = Date.now();
  }
}

function readerLink(text, href) {
  const a = document.createElement("a");
  a.textContent = text;
  a.dataset.href = href;          // never a real href: a tap goes to the NAS browser, not this page
  a.className = "reader-link";
  a.tabIndex = 0;
  a.setAttribute("role", "link");
  return a;
}

function readerRuns(parent, runs) {
  for (const run of runs) {
    if (run.href) parent.appendChild(readerLink(run.text, run.href));
    else parent.appendChild(document.createTextNode(run.text));
  }
}

function readerImage(rb, block) {
  const figure = element("figure", undefined, "reader-fig");
  const img = document.createElement("img");
  img.className = "reader-img";
  img.alt = typeof block.alt === "string" ? block.alt : "";
  img.draggable = false;
  img.decoding = "async";
  if (Number.isFinite(block.w) && Number.isFinite(block.h) && block.w > 0 && block.h > 0) {
    img.style.aspectRatio = Math.min(block.w, 20000) + " / " + Math.min(block.h, 20000);
  }
  const entry = {rb, img, index: block.i, url: "", want: false, dead: false, queued: false, failedAt: 0};
  readerHost.map.set(img, entry);
  readerHost.entries.add(entry);
  readerObservers();
  if (readerHost.near) { readerHost.near.observe(img); readerHost.far.observe(img); }
  else { entry.want = true; queueReaderImage(entry); }
  if (typeof block.href === "string" && /^https?:\/\//i.test(block.href)) {
    const a = readerLink("", block.href);
    a.classList.add("reader-imglink");
    a.appendChild(img);
    figure.appendChild(a);
  } else {
    figure.appendChild(img);
  }
  return figure;
}

function readerNode(rb, block) {
  if (block.t === "h") {
    return element("h" + Math.min(6, Math.max(2, block.level + 1)), block.text, "reader-h");
  }
  if (block.t === "p") {
    const p = element("p", undefined, "reader-p");
    readerRuns(p, block.runs);
    return p;
  }
  if (block.t === "li") {
    const li = element("div", undefined, "reader-li");
    li.style.marginLeft = (1.2 + 1.4 * Math.max(0, Math.min(8, Number(block.depth) || 0))) + "em";
    readerRuns(li, block.runs);
    return li;
  }
  if (block.t === "pre") return element("pre", block.text, "reader-pre");
  if (block.t === "img") return readerImage(rb, block);
  return element("hr", undefined, "reader-hr");
}

function renderReaderBody(rb) {
  clearReaderBody();
  const data = rb.reader && rb.reader.data;
  $("readerTitle").textContent = data ? (data.title || data.host) : "";
  $("readerHost").textContent = data ? data.host : "";
  if (!data) return;
  const body = $("readerBody");
  if (!data.blocks.length) {
    body.appendChild(element("p", "本文を取り出せませんでした。「元のページ表示に切替」をお試しください。", "reader-empty"));
    return;
  }
  const frag = document.createDocumentFragment();
  for (const block of data.blocks) frag.appendChild(readerNode(rb, block));
  if (data.truncated) frag.appendChild(element("p", "長いページのため、途中までを表示しています。", "reader-empty"));
  body.appendChild(frag);
}

// The reader container is shared by all remote tabs; only its owner's content is built.
function mountReader(tab) {
  const rb = tab.rb;
  if (!rb || readerHost.owner === rb) return;
  const scroller = $("readerScroll");
  if (readerHost.owner) readerHost.owner.readerTop = scroller.scrollTop;
  readerHost.owner = rb;
  renderReaderBody(rb);
  scroller.scrollTop = rb.readerTop || 0;
}

async function runReader(tab, opts = {}) {
  const rb = tab && tab.rb;
  if (!rb || tabDisplay(tab) !== "reader") return;
  if (rb.readerBusy) { rb.readerAgain = true; return; }
  rb.readerBusy = true;
  snapBusy(rb, 1);
  try {
    const started = performance.now();
    const raw = await api("/api/rb/reader", {tab: rb.id});
    rb.perf.extractMs = performance.now() - started;
    rb.perf.imgs = 0; rb.perf.imgMs = 0;
    const data = cleanReaderData(raw);
    if (tab.rb !== rb || tabDisplay(tab) !== "reader") return;
    if (!data) throw new Error("本文を取り出せませんでした");
    const scroller = $("readerScroll");
    const mounted = readerHost.owner === rb;
    const sameUrl = Boolean(rb.reader && rb.reader.data) && rb.reader.data.url === data.url;
    const keepTop = mounted && sameUrl && opts.keepScroll !== false ? scroller.scrollTop : 0;
    rb.reader = {data, url: data.url};
    rb.readerPending = false;
    if (mounted) { renderReaderBody(rb); scroller.scrollTop = keepTop; }
    updateRemoteView(tab);
  } catch (e) {
    if (tab.rb !== rb) return;
    if (e.status === 404) remoteLost(tab);
    else if (!opts.quiet) toast(e.message, "error");
  } finally {
    rb.readerBusy = false;
    snapBusy(rb, -1);
    if (rb.readerAgain && tab.rb === rb) {
      rb.readerAgain = false;
      runReader(tab, {quiet: true}).catch(() => {});
    }
  }
}

// Metadata long-poll like the fast display's: when a page finishes loading (or the address changed)
// the reader data is fetched again.
async function readerLoop(tab, gen) {
  let fails = 0, first = true;
  await nextPaint();
  await nextPaint();
  if (gen !== state.rbGen || !tab.rb) return;
  await sendViewport(tab).catch(() => {});
  while (gen === state.rbGen && tab.rb) {
    const rb = tab.rb;
    try {
      let target = "/api/rb/meta?tab=" + encodeURIComponent(rb.id) + "&wait=" + (first ? 0 : RB_META_WAIT_S);
      if (!first && rb.rev !== null) target += "&rev=" + rb.rev;
      first = false;
      const res = await fetch(target, {credentials: "same-origin", cache: "no-store"});
      if (gen !== state.rbGen || tab.rb !== rb) return;
      if (res.status === 401 && state.authed) {
        state.authed = false;
        setTimeout(go, 0);
        return;
      }
      if (res.status === 404) { remoteLost(tab); return; }
      if (res.status !== 200) throw new Error("HTTP " + res.status);
      const meta = await res.json();
      if (gen !== state.rbGen || tab.rb !== rb) return;
      fails = 0;
      applyRemoteMeta(tab, Object.assign({}, meta, {seq: rb.seq}));
      if (gen !== state.rbGen || tab.rb !== rb || tabDisplay(tab) !== "reader") return;
      if (meta.loading === true) {
        rb.readerPending = true;
      } else if (!rb.reader || rb.reader.url !== meta.url || rb.readerPending) {
        runReader(tab, {quiet: true}).catch(() => {});
      }
    } catch (e) {
      if (gen !== state.rbGen) return;
      fails++;
      if (fails >= RB_MAX_POLL_FAILS) {
        state.rbPolling = "";
        state.rbPollMode = "";
        toast("NASブラウザの画面を取得できません: " + e.message, "error");
        return;
      }
      await delay(1000);
    }
  }
}

// Tap on a link: the NAS browser goes there (the Hub validates the address); the loop reads the
// new page when it finishes loading.
async function readerNavigate(tab, href) {
  const rb = tab && tab.rb;
  if (!rb) return;
  try {
    await api("/api/rb/navigate", {tab: rb.id, url: href});
    rb.readerPending = true;
  } catch (e) {
    if (e.status === 404) remoteLost(tab);
    else throw e;
  }
}

function wireReader() {
  const body = $("readerBody");
  const press = {timer: 0, a: null, x: 0, y: 0, id: null, fired: false};
  const stop = () => { clearTimeout(press.timer); press.timer = 0; };
  const anchor = event => (event.target && event.target.closest) ? event.target.closest("a[data-href]") : null;
  body.addEventListener("pointerdown", event => {
    stop();
    press.fired = false;
    const a = anchor(event);
    if (!a || (event.pointerType === "mouse" && event.button !== 0)) return;
    press.a = a; press.x = event.clientX; press.y = event.clientY; press.id = event.pointerId;
    press.timer = setTimeout(() => {
      press.timer = 0;
      const tab = remoteTab();
      if (!press.a || !tab || tabDisplay(tab) !== "reader") return;
      press.fired = true;
      showPressRing(press.x, press.y);
      $("remoteRing").classList.add("is-done");
      setTimeout(hidePressRing, 350);
      const href = press.a.dataset.href;
      perform(() => startProbe(tab, {href}));      // the exact address is known: no hit test needed
    }, RB_LONGPRESS_MS);
  });
  body.addEventListener("pointermove", event => {
    if (press.timer && event.pointerId === press.id &&
        Math.hypot(event.clientX - press.x, event.clientY - press.y) > RB_LONGPRESS_MOVE_PX) stop();
  });
  body.addEventListener("pointerup", stop);
  body.addEventListener("pointercancel", stop);
  body.addEventListener("contextmenu", event => { if (anchor(event)) event.preventDefault(); });
  const open = event => {
    const a = anchor(event);
    if (!a) return;
    event.preventDefault();
    if (press.fired) { press.fired = false; return; }       // the long press already started a probe
    const tab = remoteTab();
    if (tab && tabDisplay(tab) === "reader") perform(() => readerNavigate(tab, a.dataset.href));
  };
  body.addEventListener("click", open);
  body.addEventListener("keydown", event => { if (event.key === "Enter") open(event); });
  wirePull($("readerScroll"), () => {
    const tab = remoteTab();
    if (tab && tabDisplay(tab) === "reader") perform(() => runReader(tab, {keepScroll: true}));
  });
  $("readerOriginal").addEventListener("click", () => {
    const tab = remoteTab();
    if (tab) setUserDisplay(tab, "tiles");
  });
}

// ---- long press: follow the link in a hidden background page on the NAS
const rbPress = {timer: 0, ringTimer: 0, fired: false};

function showPressRing(clientX, clientY) {
  const box = $("remoteStage").getBoundingClientRect();
  const ring = $("remoteRing");
  ring.style.left = Math.round(clientX - box.left) + "px";
  ring.style.top = Math.round(clientY - box.top) + "px";
  ring.classList.remove("is-done");
  setShown("remoteRing", true);
}

function hidePressRing() {
  setShown("remoteRing", false);
  $("remoteRing").classList.remove("is-done");
}

function cancelPress() {
  clearTimeout(rbPress.timer);
  clearTimeout(rbPress.ringTimer);
  rbPress.timer = rbPress.ringTimer = 0;
  if (!rbPress.fired) hidePressRing();
}

function longPress(clientX, clientY) {
  const tab = remoteTab();
  const point = tab ? remotePoint({clientX, clientY}) : null;
  if (!tab || !point) { cancelPress(); return; }
  rbPress.fired = true;
  $("remoteRing").classList.add("is-done");
  setTimeout(() => { rbPress.fired = false; hidePressRing(); }, 350);
  // Live display: hit test against the viewport link map (refreshed first when the picture
  // changed since); no link near the point -> the Hub resolves the coordinates itself.
  perform(async () => {
    const rb = tab.rb;
    if (rb && (!rb.vmap || rb.vmapRev !== rb.linksRev)) {
      await Promise.race([fetchViewportLinks(tab, true), delay(RB_VP_WAIT_MS)]);
    }
    if (tab.rb !== rb || !rb) return;
    const link = rb.vmap ? pickLink(rb.vmap.links, point.x, point.y) : null;
    await startProbe(tab, link ? {href: link.href} : point);
  });
}

// ---- parallel long-press probes (v0.1.27) ----
// Every long press starts its own request at once (the server queues beyond its limits).
// While probing only the small chip is shown; the sheet appears solely when a finished
// probe cannot be auto-sent, and then it collects the results of all such probes.
let probeSerial = 0;
const probeFlight = new Map();   // id -> {n, started}
let probeFlightOpen = false;
let probeTicker = 0;

function closeProbeSheet() {
  state.probeResults = [];
  state.probeResult = null;
  setShown("probeSheet", false);
}

function renderProbeChip() {
  const count = probeFlight.size;
  $("probeChip").textContent = "確認中 " + count + "件";
  setShown("probeChip", count > 0);
  if (!count) { probeFlightOpen = false; clearInterval(probeTicker); probeTicker = 0; }
  else if (!probeTicker) probeTicker = setInterval(renderProbeFlight, 1000);
  renderProbeFlight();
}

function renderProbeFlight() {
  const box = $("probeFlight");
  box.hidden = !(probeFlightOpen && probeFlight.size);
  if (box.hidden) return;
  const now = Date.now();
  box.replaceChildren(...Array.from(probeFlight.values()).map(p =>
    element("div", "リンク" + p.n + " · " + Math.floor((now - p.started) / 1000) + "秒"
      + (p.url ? " · " + p.url : ""))));
}

function renderProbe() {
  const results = state.probeResults || [];
  const canSend = jd2CanSend();
  const all = [];
  const groups = results.map(item => {
    const group = element("div", undefined, "probe-group");
    group.appendChild(element("div", "リンク" + item.n + " · リダイレクト " + item.result.redirects +
      "回 · 最終: " + (hostLabel(item.result.final_url) || "不明") +
      (item.error ? " · 送信失敗: " + item.error : ""), "probe-group-head"));
    for (const url of item.result.links) {
      all.push(url);
      const row = element("div", undefined, "probe-link");
      row.appendChild(element("span", url, "link-url"));
      const actions = element("span", undefined, "probe-link-actions");
      if (canSend) {
        const send = element("button", "送る", "btn small");
        send.type = "button";
        send.addEventListener("click", () => perform(() => sendUrls([url])));
        actions.appendChild(send);
      }
      const add = element("button", "追加", "btn small");
      add.type = "button";
      add.addEventListener("click", () => perform(async () => {
        await stageLinks([url]);
        flash("追加したリンクに入れました（JD2へは未送信）");
      }));
      actions.appendChild(add);
      row.appendChild(actions);
      group.appendChild(row);
    }
    return group;
  });
  $("probeList").replaceChildren(...groups);
  state.probeResult = {links: all};
  $("probeSendAll").textContent = "すべてJD2へ送る（" + all.length + "件）";
  $("probeAddAll").textContent = "リンクに追加（" + all.length + "件）";
  setShown("probeSendAll", canSend && all.length > 0);
  setShown("probeAddAll", all.length > 0);
  setShown("probeSheet", groups.length > 0);
}

// Long press found hoster links: hand them to JD2 without a dialog (setting probe_autosend).
// Returns true when handled (toast shown); false means the result goes to the sheet.
async function autoSendProbe(result, rb, item) {
  if (!result.links.length || !result.probe_id) return false;
  await Promise.all([loadSettings().catch(() => {}), loadJd2Status().catch(() => {})]);
  if (!(state.settings && state.settings.probe_autosend !== false) || !jd2CanSend()) return false;
  try {
    const r = await api("/api/rb/probe/send", {tab: rb.id, probe_id: result.probe_id});
    swrDrop("links");
    loadQueue(true).catch(() => {});
    if (r.error) {
      toast((r.sent ? r.sent + "件送信後に停止: " : "") + "JD2へ送信できませんでした: " + r.error, "error");
    } else if (r.sent === 0) {
      flash("送信済みのためスキップしました（" + r.skipped + "件）");
    } else {
      flash("JD2へ送信しました（" + r.sent + "件）" +
            (r.skipped ? "・" + r.skipped + "件は重複のためスキップ" : ""));
    }
    return true;
  } catch (e) {
    toast("JD2へ送信できませんでした: " + e.message, "error");
    return true;
  }
}

async function startProbe(tab, point) {
  const rb = tab.rb;
  if (!rb) return;
  const item = {n: ++probeSerial, started: Date.now()};
  const id = item.n;
  probeFlight.set(id, item);
  renderProbeChip();
  try {
    const result = await api("/api/rb/probe", point.href ? {tab: rb.id, href: point.href} :
      point.snap ? {tab: rb.id, x: point.x, y: point.y, snap: point.snap} : {tab: rb.id, x: point.x, y: point.y});
    probeFlight.delete(id);
    renderProbeChip();
    if (tab.rb !== rb) return;
    if (!result.links.length) {
      const host = hostLabel(result.final_url);
      flash("リンクは見つかりませんでした" + (host ? "（最終: " + host + "）" : ""));
      return;
    }
    if (await autoSendProbe(result, rb, item)) return;
    if (tab.rb !== rb) return;
    state.probeResults = (state.probeResults || []).concat([{n: item.n, result}]);
    renderProbe();
  } catch (e) {
    probeFlight.delete(id);
    renderProbeChip();
    if (e.status === 404) remoteLost(tab);
    else throw e;
  }
}

// Pointer to page coordinates: scaled to the frame size reported by the Hub.
function remotePoint(event) {
  const tab = remoteTab();
  const img = $("remoteImg");
  const box = img.getBoundingClientRect();
  if (!tab || !box.width || !box.height || !tab.rb.width || !tab.rb.height) return null;
  const x = (event.clientX - box.left) / box.width * tab.rb.width;
  const y = (event.clientY - box.top) / box.height * tab.rb.height;
  if (!(x >= 0 && y >= 0 && x <= tab.rb.width && y <= tab.rb.height)) return null;
  return {x: Math.round(x), y: Math.round(y), scale: tab.rb.width / box.width};
}

const rbPointer = {id: null, startX: 0, startY: 0, lastY: 0, lastX: 0, t0: 0, moved: false};

// Scroll coalescing: deltas are summed and sent as ONE input per animation frame,
// and never while the previous scroll request is still in flight.
const rbScroll = {x: 0, y: 0, dx: 0, dy: 0, raf: 0, busy: false};

function flushScroll() {
  rbScroll.raf = 0;
  if (rbScroll.busy || (!rbScroll.dx && !rbScroll.dy)) return;
  const dx = Math.max(-5000, Math.min(5000, Math.round(rbScroll.dx)));
  const dy = Math.max(-5000, Math.min(5000, Math.round(rbScroll.dy)));
  const {x, y} = rbScroll;
  rbScroll.dx = 0; rbScroll.dy = 0;
  rbScroll.busy = true;
  rbInput({type: "scroll", x, y, dx, dy}).finally(() => {
    rbScroll.busy = false;
    if (rbScroll.dx || rbScroll.dy) queueScroll(rbScroll.x, rbScroll.y, 0, 0);
  });
}

function queueScroll(x, y, dx, dy) {
  rbScroll.x = x; rbScroll.y = y;
  rbScroll.dx += dx; rbScroll.dy += dy;
  if (!rbScroll.raf) rbScroll.raf = requestAnimationFrame(flushScroll);
}

function wireRemote() {
  const stage = $("remoteStage");
  stage.addEventListener("pointerdown", event => {
    const tab = remoteTab();
    if (!tab || rbPointer.id !== null || tabDisplay(tab) !== "live") return;
    rbPointer.id = event.pointerId;
    rbPointer.startX = rbPointer.lastX = event.clientX;
    rbPointer.startY = rbPointer.lastY = event.clientY;
    rbPointer.t0 = Date.now();
    rbPointer.moved = false;
    try { stage.setPointerCapture(event.pointerId); } catch (e) { /* not supported */ }
    cancelPress();
    rbPress.fired = false;
    const startX = event.clientX, startY = event.clientY;
    rbPress.ringTimer = setTimeout(() => showPressRing(startX, startY), RB_RING_DELAY_MS);
    rbPress.timer = setTimeout(() => {
      if (rbPointer.id !== null && !rbPointer.moved) longPress(startX, startY);
    }, RB_LONGPRESS_MS);
  });
  stage.addEventListener("contextmenu", event => {
    if (!event.target.closest || !event.target.closest("#readerScroll")) event.preventDefault();
  });
  stage.addEventListener("pointermove", event => {
    if (event.pointerId !== rbPointer.id) return;
    if (Math.hypot(event.clientX - rbPointer.startX, event.clientY - rbPointer.startY) >
        Math.min(RB_TAP_MOVE_PX, RB_LONGPRESS_MOVE_PX)) {
      rbPointer.moved = true;
      cancelPress();
    }
    if (!rbPointer.moved) return;
    const point = remotePoint(event);
    if (!point) return;
    // Dragging the finger up scrolls the page down (positive wheel delta).
    const dx = (rbPointer.lastX - event.clientX) * point.scale;
    const dy = (rbPointer.lastY - event.clientY) * point.scale;
    rbPointer.lastX = event.clientX;
    rbPointer.lastY = event.clientY;
    queueScroll(point.x, point.y, dx, dy);
  });
  const finish = event => {
    if (event.pointerId !== rbPointer.id) return;
    rbPointer.id = null;
    const longPressed = rbPress.fired;
    cancelPress();
    const point = remotePoint(event);
    if (event.type === "pointercancel" || !point || longPressed) return;
    if (!rbPointer.moved && Date.now() - rbPointer.t0 <= RB_TAP_MAX_MS) {
      rbInput({type: "tap", x: point.x, y: point.y});
    }
  };
  stage.addEventListener("pointerup", finish);
  stage.addEventListener("pointercancel", finish);
  stage.addEventListener("wheel", event => {
    const tab = remoteTab();
    if (!tab || tabDisplay(tab) !== "live") return;      // fast display scrolls natively
    const point = remotePoint(event);
    if (!point) return;
    event.preventDefault();
    queueScroll(point.x, point.y, event.deltaX, event.deltaY);
  }, {passive: false});

  // Hidden text input: iOS only shows its keyboard for a focused real input.
  const kbd = $("remoteKbd");
  let composing = false, skipInput = false;
  const sendText = text => {
    kbd.value = "";
    if (text) rbInput({type: "text", text: text.slice(0, 500)});
  };
  $("remoteKbdBtn").addEventListener("click", () => kbd.focus());
  kbd.addEventListener("compositionstart", () => { composing = true; });
  kbd.addEventListener("compositionend", event => {
    composing = false;
    skipInput = true;   // the input event that follows must not send the text twice
    setTimeout(() => { skipInput = false; }, 0);
    sendText(event.data || "");
  });
  kbd.addEventListener("input", event => {
    if (composing || event.isComposing || skipInput) { if (skipInput) kbd.value = ""; return; }
    sendText(kbd.value);
  });
  kbd.addEventListener("keydown", event => {
    if (composing || event.isComposing || !RB_KEYS.includes(event.key)) return;
    event.preventDefault();
    rbInput({type: "key", key: event.key});
  });
  $("remoteKeyEnter").addEventListener("click", () => rbInput({type: "key", key: "Enter"}));
  $("remoteKeyBack").addEventListener("click", () => rbInput({type: "key", key: "Backspace"}));
  $("remoteKeyTab").addEventListener("click", () => rbInput({type: "key", key: "Tab"}));
  $("remoteLinks").addEventListener("click", () => perform(openRemoteSheet));
  $("popupToastOpen").addEventListener("click", () => perform(openPopupToast));
  $("popupToastAllow").addEventListener("click", () => perform(() => popupRule("allow")));
  $("popupToastBlock").addEventListener("click", () => perform(() => popupRule("block")));
  $("probeClose").addEventListener("click", closeProbeSheet);
  $("probeChip").addEventListener("click", () => { probeFlightOpen = !probeFlightOpen; renderProbeFlight(); });
  $("probeSendAll").addEventListener("click", () => perform(async () => {
    if (state.probeResult) await sendUrls(state.probeResult.links.slice());
  }));
  $("probeAddAll").addEventListener("click", () => perform(async () => {
    const links = state.probeResult ? state.probeResult.links.slice() : [];
    await stageLinks(links);
    flash(links.length + "件を追加したリンクに入れました（JD2へは未送信）");
  }));
  $("interceptSend").addEventListener("click", () => perform(() => interceptAction("send")));
  $("interceptAdd").addEventListener("click", () => perform(() => interceptAction("add")));
  $("interceptOpen").addEventListener("click", () => perform(() => interceptAction("open")));
  $("interceptClose").addEventListener("click", () => {
    const cur = currentIntercept();
    if (cur) dropIntercept(cur.item);
  });
  // Re-size the remote page when the viewer's stage changes (rotation, keyboard, zoom).
  let viewportTimer = 0;
  const queueViewport = () => {
    clearTimeout(viewportTimer);
    viewportTimer = setTimeout(() => {
      const tab = remoteTab();
      if (tab && state.rbPolling === tab.rb.id) sendViewport(tab).catch(() => {});
    }, RB_VIEWPORT_DEBOUNCE_MS);
  };
  if (typeof ResizeObserver === "function") new ResizeObserver(queueViewport).observe(stage);
  window.addEventListener("resize", queueViewport);
  $("remoteSheetClose").addEventListener("click", closeRemoteSheet);
  $("remoteAdd").addEventListener("click", () => perform(remoteAdd));
  $("remoteSend").addEventListener("click", () => perform(remoteSend));
  $("remoteReopen").addEventListener("click", () => {
    const tab = activeWebTab();
    if (tab) perform(() => openInRemote(tab));
  });
  $("cfRemote").addEventListener("click", () => {
    const tab = activeWebTab();
    if (tab) perform(() => openInRemote(tab));
  });
  document.addEventListener("visibilitychange", syncRemotePoll);
  wireTiles();
  wireReader();
}

function refreshAfterTabChange() {
  if (state.route.view === "tab") {
    const shown = currentViewName();
    for (const name of ["home", "web"]) $("view-" + name).hidden = name !== shown;
  }
  syncFrameVisibility();
  renderWebTabs();
  updateChrome();
  syncRemotePoll();
}

function pushHistory(tab, url) {
  tab.history = tab.history.slice(0, tab.index + 1);
  tab.history.push(url);
  while (tab.history.length > MAX_HISTORY) tab.history.shift();
  tab.index = tab.history.length - 1;
}

const navigateTab = (tab, value) => openInTab(tab, value, url => pushHistory(tab, url));
const reloadTab = tab => (tab.remote && tab.rb) ?
  rbInputFor(tab, {type: "reload"}) :
  openInTab(tab, tab.history[tab.index], url => { tab.history[tab.index] = url; });

async function rbInputFor(tab, event) {
  try {
    await api("/api/rb/input", Object.assign({tab: tab.rb.id}, event));
  } catch (e) {
    if (e.status === 404) remoteLost(tab);
    else throw e;
  }
}

function ensureLoaded(tab) {
  if (!tab || !tab.history.length || tab.loaded || tab.loading) return Promise.resolve();
  if (tab.remote) { tab.loaded = true; return Promise.resolve(); }   // remote tabs are opened on request only
  return openInTab(tab, tab.history[tab.index], url => { tab.history[tab.index] = url; }, {restore: true});
}

async function stepHistory(tab, delta) {
  if (tab.remote) {
    if (tab.rb) await rbInputFor(tab, {type: delta < 0 ? "back" : "forward"});
    return;
  }
  const target = tab.index + delta;
  if (target < 0 || target >= tab.history.length) return;
  await openInTab(tab, tab.history[target], url => {
    tab.index = target;
    tab.history[target] = url;
  });
}

// Open a URL typed/clicked by the user: in the active web tab, else a new tab.
async function openWebUrl(value) {
  const tab = activeWebTab();
  if (tab) { await navigateTab(tab, value); return; }
  if (tabLimitReached()) return;
  const url = await validateUrl(value);
  const fresh = createTab();
  pushHistory(fresh, url);
  applyHostMode(fresh, url);
  fresh.current = url;
  fresh.loaded = true;
  mountFrame(fresh, url);
  saveTabs();
  navigate("#/tab/" + fresh.id);
}

const COMPAT_CONFIRM = "互換モードに切り替えます。\n\n" +
  "・このサイト自身のCookie・ストレージを枠内で使えるようにします（Hub自体のものではありません）。\n" +
  "・Cloudflareの確認を回避する機能ではありません。\n" +
  "・iPhoneのSafariではサードパーティCookieのブロックにより動作しない場合があります。その場合は「NASブラウザ」を使ってください。\n\n" +
  "続けますか？";

async function setMode(tab, target) {
  target = normalizeMode(target);
  if (tab.mode === target) return;
  if (target === "interactive" && tab.mode === "strict" &&
      !confirm("外部サイトのスクリプトとフォームを隔離枠内で実行します。ログインやCloudflareの動作保証はありません。続けますか？")) return;
  if (target === "compat" && !confirm(COMPAT_CONFIRM)) return;
  tab.mode = target;
  if (tab.history.length) rememberHostMode(hostOf(tab.history[tab.index]), target);
  saveTabs();
  if (tab.history.length) await reloadTab(tab);   // re-validates and rebuilds the iframe
  updateChrome();
}

// -------------------------------------------------------------- omnibox
function classifyInput(raw) {
  const text = raw.trim();
  if (!text) return {kind: "empty"};
  if (/[\r\n]/.test(text)) {
    return /rapidgator/i.test(text) ? {kind: "extract", value: text} : {kind: "hint"};
  }
  const qnap = text.match(/^qnap:\/\/(.*)$/i);
  if (qnap) {
    const path = qnap[1].replace(/\/+$/, "").toLowerCase().split("/");
    if (path[0] === "downloads" && path.length === 1) return {kind: "route", value: "#/downloads"};
    if (path[0] === "links" && path.length === 1) return {kind: "route", value: "#/links"};
    if (path[0] === "home" || path[0] === "") return {kind: "route", value: "#/home"};
    if (path[0] === "settings" && path.length <= 2) {
      const section = SECTION_ALIASES[path[1]] || path[1] || defaultSection();
      if (SETTINGS_SECTIONS.includes(section)) return {kind: "route", value: "#/settings/" + section};
    }
    return {kind: "unknown-qnap"};
  }
  if (/^https?:\/\/\S+$/i.test(text)) return {kind: "url", value: text};
  if (/^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+(:\d+)?([/?#]\S*)?$/i.test(text)) {
    return {kind: "url", value: "https://" + text};
  }
  return {kind: "hint"};
}

function prefillExtraction(text) {
  // Prefill only. Nothing is saved until the user presses the add button.
  $("pasteBox").value = text.slice(0, PASTE_LIMIT);
  navigate("#/links");
  schedulePaste(0);
}

async function handleOmnibox(raw) {
  const target = classifyInput(raw);
  if (target.kind === "empty") { restoreOmnibox(); return; }
  if (target.kind === "route") { navigate(target.value); return; }
  if (target.kind === "extract") { prefillExtraction(target.value); return; }
  if (target.kind === "url") { await openWebUrl(target.value); return; }
  if (target.kind === "unknown-qnap") { toast("不明な qnap:// アドレスです。downloads / links / settings/<節> が使えます。", "error"); return; }
  toast("URLを入力してください（検索エンジンは未対応）", "error");
}

function restoreOmnibox() { $("omnibox").value = state.omniDisplay; }

function focusOmnibox() {
  const box = $("omnibox");
  box.focus();
  box.select();
}

// ---------------------------------------------------------------- menu
function closeMenu(returnFocus = true) {
  if ($("menu").hidden) return;
  $("menu").hidden = true;
  $("menuButton").setAttribute("aria-expanded", "false");
  if (returnFocus) $("menuButton").focus();
}

function configureMenu() {
  const tab = activeWebTab();
  const web = Boolean(tab && tab.history.length);
  $("menu").querySelectorAll("[data-scope='web']").forEach(n => { n.hidden = !web; });
  const usable = Boolean(tab && tab.current);
  for (const action of ["remoteOpen", "stageUrl", "bookmark"]) {
    $("menu").querySelector("[data-action='" + action + "']").disabled = !usable;
  }
  $("menu").querySelector("[data-action='reopen']").disabled = state.closed.length === 0;
  $("menuMode").textContent = tab && tab.mode === "interactive" ?
    "強い制限モードに戻す" : "限定操作モードに切り替え";
  $("menuRemote").textContent = tab && tab.remote ? "NASブラウザをやめる（通常表示へ）" : "NASブラウザで開く";
  const remoteMenu = Boolean(tab && tab.remote && tab.history.length);
  $("menu").querySelectorAll("[data-scope='remote']").forEach(n => { n.hidden = !remoteMenu; });
  $("menu").querySelectorAll("[data-quality]").forEach(n => {
    const on = n.dataset.quality === state.rbQuality;
    n.textContent = (on ? "✓ " : "") + "画質: " + RB_QUALITY_LABELS[n.dataset.quality];
    n.setAttribute("aria-checked", String(on));
  });
  const display = tab && tab.rb ? tabDisplay(tab) : "tiles";
  $("menu").querySelectorAll("[data-display]").forEach(n => {
    const on = n.dataset.display === display;
    n.textContent = (on ? "✓ " : "") + "表示: " + RB_DISPLAY_LABELS[n.dataset.display];
    n.setAttribute("aria-checked", String(on));
  });
  $("menuPerf").textContent = state.rbPerf ? "速度表示をやめる" : "速度表示";
  $("menuMode").hidden = Boolean(tab && tab.remote) || !web;
  $("menuCompat").hidden = Boolean(tab && tab.remote) || !web;
  $("menuCompat").textContent = tab && tab.mode === "compat" ?
    "互換モードをやめる（強い制限へ）" : "互換モードに切り替え";
}

function openMenu() {
  configureMenu();
  $("menu").hidden = false;
  $("menuButton").setAttribute("aria-expanded", "true");
  const first = Array.from($("menu").querySelectorAll("[role='menuitem']"))
    .find(n => !n.hidden && !n.disabled);
  if (first) first.focus();
}

async function menuAction(action) {
  const tab = activeWebTab();
  if (action === "newTab") createBlankTab();
  else if (action === "reopen") reopenClosedTab();
  else if (action === "toggleMode" && tab) await setMode(tab, tab.mode === "interactive" ? "strict" : "interactive");
  else if (action === "compatMode" && tab) await setMode(tab, tab.mode === "compat" ? "strict" : "compat");
  else if (action === "remoteOpen" && tab) await (tab.remote ? leaveRemote(tab) : openInRemote(tab));
  else if (action.startsWith("quality:")) setRbQuality(action.slice(8));
  else if (action.startsWith("display:")) setUserDisplay(tab, action.slice(8));
  else if (action === "perf") setPerf(!state.rbPerf);
  else if (action === "stageUrl" && tab) await stageTabUrl(tab);
  else if (action === "bookmark" && tab) await bookmarkTab(tab);
  else if (action === "closeTab" && tab) closeTab(tab.id);
  else if (action === "bookmarks") navigate("#/settings/bookmarks");
  else if (action === "settings") navigate(settingsHash());
  else if (action === "logout") await logout();
}

async function stageTabUrl(tab) {
  if (!tab.current) return;
  await api("/api/links", {url: tab.current});   // local, deletable, never sent to JD2
  swrDrop("links");
  flash("追加したリンクに入れました（JD2へは未送信）");
}

async function bookmarkTab(tab) {
  if (!tab.current) return;
  if (state.bookmarks.some(b => b.url === tab.current)) { flash("すでにブックマークに登録されています"); return; }
  await addBookmark(hostOf(tab.current), tab.current);
  flash("ブックマークに追加しました");
}

async function logout() {
  for (const tab of state.tabs) releaseRemote(tab);
  await api("/api/logout", {});
  writeSession(TAB_STORAGE_KEY, null);
  writeSession(SHELL_KEY, null);
  history.replaceState(null, "", "#/home");
  state.csrf = "";
  await go();
}

// ------------------------------------------------------- mobile tab switcher
function openSwitcher() {
  renderSwitcher();
  setShown("tabSwitcher", true);
  $("switcherClose").focus();
}

function closeSwitcher(returnFocus = true) {
  if ($("tabSwitcher").hidden) return;
  setShown("tabSwitcher", false);
  if (returnFocus) $("mobileBrowse").focus();
}

function renderSwitcher() {
  const grid = $("switcherGrid");
  grid.replaceChildren();
  if (!state.tabs.length) grid.appendChild(element("p", "Webタブはありません。", "muted"));
  for (const tab of state.tabs) {
    const active = state.route.view === "tab" && state.route.tabId === tab.id;
    const card = element("div", undefined, "switch-card" + (active ? " active" : ""));
    const main = element("button", undefined, "switch-main");
    main.type = "button";
    main.appendChild(icon("globe"));
    main.appendChild(element("strong", tabTitle(tab)));
    main.appendChild(element("span", tab.current || (tab.history.length ? tab.history[tab.index] : "URL未入力")));
    main.addEventListener("click", () => { closeSwitcher(false); navigate("#/tab/" + tab.id); });
    const close = element("button", undefined, "switch-close");
    close.type = "button";
    close.setAttribute("aria-label", "タブを閉じる: " + tabTitle(tab));
    close.appendChild(icon("x"));
    close.addEventListener("click", () => closeTab(tab.id));
    card.appendChild(main);
    card.appendChild(close);
    grid.appendChild(card);
  }
}

function onMobileBrowse() {
  if (activeWebTab()) { openSwitcher(); return; }
  if (!state.tabs.length) { createBlankTab(); return; }
  const last = findTab(state.lastWebTabId) || state.tabs[state.tabs.length - 1];
  navigate("#/tab/" + last.id);
}

// ------------------------------------------------------------- downloads
function itemState(item) {
  if (item.finished === true) return "finished";
  if (item.running === true) return "running";
  return "waiting";
}

function engineRunning() { return Boolean(state.engine && state.engine.phase === "running"); }

function countRunning(snap) { return snap.links.filter(x => x.running === true).length; }

function runningCount() { return state.dlRunning; }

function updateBadge() {
  const running = runningCount();
  document.querySelectorAll(".js-dl-badge").forEach(b => {
    b.hidden = !running;
    b.textContent = running ? String(running) : "";
  });
  const label = running ? "ダウンロード（実行中 " + running + "件）" : "ダウンロード";
  $("dlButton").setAttribute("aria-label", label);
  $("dlButton").title = label;
}

const STATE_LABELS = {running: "ダウンロード中", finished: "完了", waiting: "待機"};

function stateChip(item) {
  const st = itemState(item);
  const label = st === "waiting" && item.enabled === false ? "無効" : STATE_LABELS[st];
  return element("span", label, "state-chip " + st);
}

function sizeText(item) {
  const loaded = typeof item.bytes_loaded === "number" ? item.bytes_loaded : null;
  if (typeof item.bytes_total === "number") return readableSize(loaded === null ? 0 : loaded) + " / " + readableSize(item.bytes_total);
  return loaded === null ? "" : readableSize(loaded);
}

function metaText(item, extra) {
  const parts = extra ? [extra] : [];
  const size = sizeText(item);
  if (size) parts.push(size);
  if (item.running === true) {
    if (typeof item.speed_bytes === "number") parts.push(readableSize(item.speed_bytes) + "/秒");
    if (typeof item.eta_seconds === "number") parts.push("残り " + readableEta(item.eta_seconds));
  }
  if (item.status) parts.push(item.status);
  return parts.join(" · ");
}

function progressBar(item) {
  if (item.progress === null || item.progress === undefined) return null;
  const track = element("div", undefined, "progress-track");
  track.setAttribute("role", "progressbar");
  track.setAttribute("aria-valuemin", "0");
  track.setAttribute("aria-valuemax", "100");
  track.setAttribute("aria-valuenow", String(item.progress));
  track.setAttribute("aria-label", item.name + " 進捗");
  const fill = element("div", undefined, "progress-fill");
  fill.style.width = item.progress + "%";
  track.appendChild(fill);
  return track;
}

function fileRow(item) {
  const row = element("article", undefined, "file-row " + itemState(item));
  const top = element("div", undefined, "row-top");
  top.appendChild(element("span", item.name, "row-name"));
  top.appendChild(stateChip(item));
  rowTools("link", item, top);
  row.appendChild(top);
  const bar = progressBar(item);
  if (bar) row.appendChild(bar);
  const meta = metaText(item, item.host);
  if (meta) row.appendChild(element("p", meta, "download-detail"));
  return itemSwipe("link", item, row);
}

function packageKey(item) { return item.id ? "id:" + item.id : "n:" + item.name; }

function packageRow(pkg, files) {
  const key = packageKey(pkg);
  const open = state.expanded.has(key);
  const card = element("article", undefined, "download-card pkg " + itemState(pkg));
  const top = element("div", undefined, "pkg-top");
  const line = element("div", undefined, "pkg-line");
  const head = element("button", undefined, "pkg-head");
  head.type = "button";
  head.setAttribute("aria-expanded", String(open));
  head.appendChild(element("span", open ? "▾" : "▸", "caret"));
  head.appendChild(element("span", pkg.name, "row-name"));
  head.appendChild(stateChip(pkg));
  head.addEventListener("click", () => {
    if (state.expanded.has(key)) state.expanded.delete(key); else state.expanded.add(key);
    state.downloadsSig = "";
    renderDownloads();
  });
  line.appendChild(head);
  rowTools("package", pkg, line);
  top.appendChild(line);
  const bar = progressBar(pkg);
  if (bar) top.appendChild(bar);
  const count = typeof pkg.child_count === "number" ? "ファイル " + pkg.child_count + "件" : "";
  const meta = metaText(pkg, count);
  if (meta) top.appendChild(element("p", meta, "download-detail"));
  card.appendChild(itemSwipe("package", pkg, top));
  if (open) {
    const box = element("div", undefined, "pkg-files");
    if (files.length) for (const f of files) box.appendChild(fileRow(f));
    else box.appendChild(element("p", "このページに表示できるファイルがありません", "empty-state"));
    card.appendChild(box);
  }
  return card;
}

// ---- per-item actions (v0.1.29): swipe rows, action sheet, multi-select ----
const SWIPE_REVEAL = 88;       // px of the revealed button
const SWIPE_AXIS_PX = 10;      // movement before the gesture picks an axis
const SWIPE_FULL = 0.6;        // share of the row width that commits the action
const LONG_PRESS_MS = 500;
const BULK_MAX = 100;

function reducedMotion() {
  return Boolean(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
}

const swipeState = {open: null, gesture: false, sheet: false, sheetReturn: null,
  pending: new Set(), select: {on: false, keys: new Set()}, index: new Map()};

// While a row is being dragged or held open, or a sheet is up, the 5 s
// auto-refresh must not rebuild the rows under the finger.
function holdRender() { return swipeState.gesture || swipeState.open !== null || swipeState.sheet; }

function flushRender() {
  if (holdRender()) return;
  state.downloadsSig = "";
  renderDownloads();
  renderWaiting();
}

function itemKey(kind, item) { return kind + ":" + item.id; }

// Wraps `body` (the visible row) with the buttons that a swipe reveals:
// swipe left shows 削除 (red), swipe right shows 開始／停止 (cfg.startLabel).
function swipeRow(body, cfg) {
  const wrap = element("div", undefined, "swipe");
  wrap.dataset.key = cfg.key;
  const left = element("div", undefined, "swipe-under swipe-under-left");
  const right = element("div", undefined, "swipe-under swipe-under-right");
  let startButton = null;
  if (cfg.startLabel) {
    startButton = element("button", cfg.startLabel, "swipe-btn " + (cfg.startLabel === "停止" ? "stop" : "start"));
    startButton.type = "button";
    left.appendChild(startButton);
  }
  const deleteButton = element("button", "削除", "swipe-btn delete");
  deleteButton.type = "button";
  right.appendChild(deleteButton);
  body.classList.add("swipe-body");
  wrap.append(left, right, body);
  const buttons = [startButton, deleteButton].filter(Boolean);
  let offset = 0, startX = 0, startY = 0, startOffset = 0, axis = null, pid = null;
  let width = 320, timer = null, endedAt = 0;

  const setButtons = on => buttons.forEach(b => {
    b.tabIndex = on ? 0 : -1;
    if (on) b.removeAttribute("aria-hidden"); else b.setAttribute("aria-hidden", "true");
  });
  setButtons(false);
  const clearTimer = () => { if (timer !== null) { clearTimeout(timer); timer = null; } };
  const setOffset = (px, animate) => {
    offset = px;
    body.style.transition = animate && !reducedMotion() ? "transform .3s cubic-bezier(.22,1,.36,1)" : "none";
    body.style.transform = px ? "translateX(" + px + "px)" : "";
  };
  const handle = {wrap, close() {
    setOffset(0, true); setButtons(false);
    if (swipeState.open === handle) { swipeState.open = null; setTimeout(flushRender, 320); }
  }};
  const reveal = px => {
    if (swipeState.open && swipeState.open !== handle) swipeState.open.close();
    setOffset(px, true); setButtons(true); swipeState.open = handle;
  };
  const settle = () => {
    const ratio = Math.abs(offset) / width;
    if (ratio > SWIPE_FULL) {
      const deleting = offset < 0;
      handle.close();
      // A full swipe runs the safe default: start/stop, or remove from the list only.
      if (deleting) cfg.onDelete(); else cfg.onStart();
    } else if (Math.abs(offset) >= SWIPE_REVEAL / 2) {
      reveal(offset < 0 ? -SWIPE_REVEAL : SWIPE_REVEAL);
    } else {
      handle.close();
    }
  };
  const blocked = () => swipeState.select.on || swipeState.pending.has(cfg.key);

  body.addEventListener("pointerdown", e => {
    if (e.pointerType === "mouse" && e.button !== 0) return;
    if (e.target.closest(".row-menu, input")) return;
    if (swipeState.select.on || pid !== null) return;
    pid = e.pointerId; startX = e.clientX; startY = e.clientY;
    startOffset = offset; axis = null; width = body.offsetWidth || 320;
    if (swipeState.open && swipeState.open !== handle) swipeState.open.close();
    clearTimer();
    timer = setTimeout(() => {
      timer = null;
      if (axis === null && pid !== null && !blocked()) {
        pid = null; endedAt = performance.now();
        cfg.onLongPress();
      }
    }, LONG_PRESS_MS);
  });
  body.addEventListener("pointermove", e => {
    if (e.pointerId !== pid || blocked()) return;
    const dx = e.clientX - startX, dy = e.clientY - startY;
    if (axis === null) {
      if (Math.abs(dx) < SWIPE_AXIS_PX && Math.abs(dy) < SWIPE_AXIS_PX) return;
      clearTimer();
      if (Math.abs(dy) > Math.abs(dx)) { axis = "y"; pid = null; return; }   // vertical scroll wins
      axis = "x"; swipeState.gesture = true;
      try { body.setPointerCapture(e.pointerId); } catch (err) { /* synthetic pointer */ }
    }
    if (axis !== "x") return;
    const max = startLabelAllowed() ? width : 0;
    setOffset(Math.max(-width, Math.min(max, startOffset + dx)), false);
  });
  const startLabelAllowed = () => Boolean(cfg.startLabel);
  const finish = (e, cancelled) => {
    if (e.pointerId !== pid && axis !== "x") return;
    clearTimer();
    const was = axis;
    pid = null; axis = null;
    if (was === "x") {
      swipeState.gesture = false; endedAt = performance.now();
      if (cancelled) handle.close(); else settle();
    }
  };
  body.addEventListener("pointerup", e => finish(e, false));
  body.addEventListener("pointercancel", e => finish(e, true));
  // A drag or long press must not also count as a tap (expand / toggle).
  wrap.addEventListener("click", e => {
    if (performance.now() - endedAt < 450) { e.preventDefault(); e.stopPropagation(); return; }
    if (swipeState.select.on && !e.target.closest(".row-menu")) {
      e.preventDefault(); e.stopPropagation(); toggleSelected(cfg.key);
    }
  }, true);
  if (startButton) startButton.addEventListener("click", () => { handle.close(); cfg.onStart(); });
  deleteButton.addEventListener("click", () => { handle.close(); cfg.onDeleteSheet(); });
  wrap.addEventListener("keydown", e => {
    if (e.key === "Escape" && swipeState.open === handle) handle.close();
  });
  return wrap;
}

function menuButton(name, onOpen) {
  const button = element("button", "⋯", "row-menu");
  button.type = "button";
  button.setAttribute("aria-label", name + " の操作メニュー");
  button.setAttribute("aria-haspopup", "dialog");
  button.addEventListener("click", e => { e.stopPropagation(); onOpen(button); });
  return button;
}

function selectBox(key, name) {
  const box = element("input", undefined, "sel-box");
  box.type = "checkbox";
  box.setAttribute("aria-label", name + " を選択");
  box.checked = swipeState.select.keys.has(key);
  box.addEventListener("click", e => { e.stopPropagation(); toggleSelected(key); });
  return box;
}

// ---- action sheet (also the ⋯ fallback): every option is a real button ----
function openSheet(title, options, returnTo) {
  swipeState.sheet = true;
  swipeState.sheetReturn = returnTo || document.activeElement;
  $("sheetTitle").textContent = title;
  const buttons = options.map(option => {
    const cls = "btn sheet-btn" + (option.destructive ? " danger" : "") + (option.primary ? " primary" : "");
    const button = element("button", option.label, cls);
    button.type = "button";
    if (option.primary) button.dataset.default = "true";
    button.addEventListener("click", () => {
      closeSheet();
      if (option.run) perform(option.run);
    });
    return button;
  });
  const cancel = element("button", "キャンセル", "btn sheet-btn");
  cancel.type = "button";
  cancel.addEventListener("click", closeSheet);
  $("sheetOptions").replaceChildren(...buttons, cancel);
  setShown("actionSheet", true);
  (buttons.find(b => b.dataset.default) || cancel).focus();
}

function closeSheet() {
  if (!swipeState.sheet) return;
  swipeState.sheet = false;
  setShown("actionSheet", false);
  const back = swipeState.sheetReturn;
  swipeState.sheetReturn = null;
  if (back && back.isConnected && typeof back.focus === "function") back.focus();
  flushRender();
}

function wireSheet() {
  const sheet = $("actionSheet");
  sheet.addEventListener("click", e => { if (e.target === sheet) closeSheet(); });
  sheet.addEventListener("keydown", e => {
    if (e.key === "Escape") { e.preventDefault(); closeSheet(); return; }
    if (e.key !== "Tab") return;
    const items = Array.from(sheet.querySelectorAll("button"));
    if (!items.length) return;
    const first = items[0], last = items[items.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  });
  document.addEventListener("pointerdown", e => {
    const open = swipeState.open;
    if (open && !open.wrap.contains(e.target)) open.close();
  }, true);
  $("selectStart").addEventListener("click", () => perform(() => bulkAction("start")));
  $("selectStop").addEventListener("click", () => perform(() => bulkAction("stop")));
  $("selectDelete").addEventListener("click", bulkDeleteSheet);
  $("selectDone").addEventListener("click", () => exitSelect());
}

// ---- calls ----
function setPending(keys, on) {
  for (const key of keys) if (on) swipeState.pending.add(key); else swipeState.pending.delete(key);
  document.querySelectorAll(".swipe[data-key]").forEach(node =>
    node.classList.toggle("pending", swipeState.pending.has(node.dataset.key)));
}

async function afterItemAction(message) {
  if (message) flash(message);
  state.downloadsFails = 0;
  state.downloadsSig = "";
  await loadDownloads(state.downloadsPage);
  flushRender();
}

// kind: package|link. items: [{id, name}]. Requests are chunked to the server cap.
async function callItems(action, kind, items, extra) {
  const keys = items.map(item => itemKey(kind, item));
  setPending(keys, true);
  try {
    for (let i = 0; i < items.length; i += BULK_MAX) {
      const body = Object.assign({kind, ids: items.slice(i, i + BULK_MAX).map(item => item.id),
        page: state.downloadsPage}, extra || {});
      await api("/api/jd2/items/" + action, body);
    }
  } finally {
    setPending(keys, false);
  }
}

async function startItems(kind, items) {
  await callItems("start", kind, items);
  await afterItemAction(items.length + "件を開始しました");
}

async function stopItems(kind, items) {
  await callItems("stop", kind, items);
  await afterItemAction(items.length + "件を停止しました（「開始」で再開できます）");
}

// Default delete: only JD2's list; the saved files stay on the NAS.
async function removeFromList(kind, items) {
  try {
    await callItems("remove", kind, items, {delete_files: false});
  } finally {
    await afterItemAction("");
  }
  flash(items.length + "件を一覧から削除しました（ファイルは残しています）");
}

async function removeWithFiles(kind, item) {
  if (!confirm("「" + item.name + "」のファイルをNASから削除します。\n一覧からも消え、元に戻せません。\nよろしいですか？")) return;
  try {
    await callItems("remove", kind, [item], {delete_files: true, confirm_name: item.name});
  } finally {
    await afterItemAction("");
  }
  flash("ファイルも削除しました");
}

async function waitingCall(action, items) {
  const keys = items.map(item => "waiting:" + item.id);
  setPending(keys, true);
  try {
    await api("/api/jd2/waiting/" + action, {ids: items.map(item => item.id)});
  } finally {
    setPending(keys, false);
  }
  await afterItemAction(action === "start" ? "開始しました" : "待機中の一覧から削除しました");
}

function deleteOptions(kind, item) {
  return [
    {label: "一覧から削除（ファイルは残す）", primary: true, run: () => removeFromList(kind, [item])},
    {label: "ファイルも削除", destructive: true, run: () => removeWithFiles(kind, item)},
  ];
}

function canStart(item) { return item.finished !== true; }

function startLabelFor(item) {
  if (item.finished === true) return null;
  return item.running === true ? "停止" : "開始";
}

function itemRun(kind, item) {
  return item.running === true ? stopItems(kind, [item]) : startItems(kind, [item]);
}

function itemMenu(kind, item, opener) {
  const key = itemKey(kind, item);
  const options = [];
  const label = startLabelFor(item);
  if (label) options.push({label, run: () => itemRun(kind, item)});
  options.push(...deleteOptions(kind, item));
  options.push({label: "複数選択", run: () => enterSelect(key)});
  openSheet(item.name, options, opener);
}

// One swipeable row for a package head or a file; items without an id are plain.
function itemSwipe(kind, item, body) {
  if (!item.id) return body;
  const key = itemKey(kind, item);
  swipeState.index.set(key, {kind, item});
  const wrap = swipeRow(body, {
    key, startLabel: startLabelFor(item),
    onStart: () => perform(() => itemRun(kind, item)),
    onDelete: () => perform(() => removeFromList(kind, [item])),
    onDeleteSheet: () => openSheet(item.name, deleteOptions(kind, item)),
    onLongPress: () => enterSelect(key),
  });
  wrap.classList.toggle("pending", swipeState.pending.has(key));
  wrap.classList.toggle("selected", swipeState.select.keys.has(key));
  return wrap;
}

function rowTools(kind, item, line) {
  if (!item.id) return;
  const key = itemKey(kind, item);
  line.prepend(selectBox(key, item.name));
  line.appendChild(menuButton(item.name, opener => itemMenu(kind, item, opener)));
}

// ---- waiting (LinkGrabber) rows ----
function waitingMenu(item, opener) {
  openSheet(item.name, [
    {label: "開始", run: () => waitingCall("start", [item])},
    {label: "一覧から削除", primary: true, run: () => waitingCall("remove", [item])},
  ], opener);
}

function waitingRow(item) {
  const body = element("div", undefined, "waiting-item");
  body.appendChild(element("span", item.name, "waiting-name"));
  if (item.host) body.appendChild(element("span", item.host, "muted"));
  if (!item.id) return body;
  body.appendChild(menuButton(item.name, opener => waitingMenu(item, opener)));
  const wrap = swipeRow(body, {
    key: "waiting:" + item.id, startLabel: "開始",
    onStart: () => perform(() => waitingCall("start", [item])),
    onDelete: () => perform(() => waitingCall("remove", [item])),
    onDeleteSheet: () => openSheet(item.name, [
      {label: "一覧から削除", primary: true, run: () => waitingCall("remove", [item])}]),
    onLongPress: () => {},
  });
  wrap.classList.toggle("pending", swipeState.pending.has("waiting:" + item.id));
  return wrap;
}

// ---- multi-select ----
function updateSelectBar() {
  const sel = swipeState.select;
  setShown("selectBar", sel.on);
  $("view-downloads").classList.toggle("selecting", sel.on);
  $("selectCount").textContent = sel.keys.size + "件選択";
  for (const id of ["selectStart", "selectStop", "selectDelete"]) $(id).disabled = sel.keys.size === 0;
}

function refreshSelection() {
  document.querySelectorAll(".swipe[data-key]").forEach(node => {
    const on = swipeState.select.keys.has(node.dataset.key);
    node.classList.toggle("selected", on);
    const box = node.querySelector(".sel-box");
    if (box) box.checked = on;
  });
  updateSelectBar();
}

function enterSelect(key) {
  if (swipeState.open) swipeState.open.close();
  swipeState.select.on = true;
  if (key) swipeState.select.keys.add(key);
  refreshSelection();
}

function exitSelect() {
  swipeState.select.on = false;
  swipeState.select.keys.clear();
  refreshSelection();
}

function toggleSelected(key) {
  const keys = swipeState.select.keys;
  if (keys.has(key)) keys.delete(key); else keys.add(key);
  refreshSelection();
}

function selectedGroups() {
  const groups = {package: [], link: []};
  for (const key of swipeState.select.keys) {
    const entry = swipeState.index.get(key);
    if (entry) groups[entry.kind].push(entry.item);
  }
  return groups;
}

async function bulkAction(action) {
  const groups = selectedGroups();
  let count = 0;
  try {
    for (const kind of ["package", "link"]) {
      if (!groups[kind].length) continue;
      await callItems(action, kind, groups[kind]);
      count += groups[kind].length;
    }
  } finally {
    await afterItemAction("");
  }
  flash(count + "件を" + (action === "start" ? "開始" : "停止") + "しました");
  exitSelect();
}

function bulkDeleteSheet() {
  const groups = selectedGroups();
  const total = groups.package.length + groups.link.length;
  if (!total) return;
  openSheet(total + "件を削除（ファイルを消すには1件ずつ操作してください）", [{
    label: "一覧から削除（ファイルは残す）", primary: true,
    run: async () => {
      try {
        for (const kind of ["package", "link"]) {
          if (groups[kind].length) await callItems("remove", kind, groups[kind], {delete_files: false});
        }
      } finally {
        await afterItemAction("");
      }
      flash(total + "件を一覧から削除しました（ファイルは残しています）");
      exitSelect();
    },
  }]);
}

function matchesFilter(item) {
  if (state.filter !== "all" && itemState(item) !== state.filter) return false;
  const q = state.query.trim().toLowerCase();
  return !q || String(item.name).toLowerCase().includes(q);
}

function clockText(ms) {
  const d = new Date(ms), two = n => String(n).padStart(2, "0");
  return two(d.getHours()) + ":" + two(d.getMinutes()) + ":" + two(d.getSeconds());
}

// Empty state (engine not running) vs. list. Called on engine changes too.
function renderDownloadsShell() {
  const e = state.engine;
  const running = engineRunning();
  setShown("downloadsEmpty", Boolean(e) && !running);
  setShown("downloadsBody", running);
  $("downloadsRefresh").hidden = !running;
  $("downloadsToggle").hidden = !running;
  $("downloadsClear").hidden = !running;
  $("downloadsUpdated").hidden = !running;
  if (!e || running) return;
  const needSetup = !e.installed || e.phase === "setup";
  const button = $("downloadsEngineAction");
  button.dataset.action = needSetup ? "setup" : "start";
  button.textContent = needSetup ? "初期設定へ" : "起動";
  button.hidden = e.phase === "starting";
  button.disabled = false;
  const note = e.phase === "starting" ? "起動しています…" : e.error ? e.error : "";
  $("downloadsEmptyNote").textContent = note;
  setShown("downloadsEmptyNote", Boolean(note));
}

// Packages are the rows; their files (same snapshot, matched by package id)
// expand on click. Files whose package is not on this page go to a fold.
function renderDownloads() {
  renderDownloadsShell();
  const snap = state.snapshot;
  const sig = snap ? state.snapshotJson + "|" + state.filter + "|" + state.query + "|" +
    Array.from(state.expanded).join(",") : "";
  if (sig === state.downloadsSig) return;
  if (holdRender()) return;   // keep the row under the finger; flushRender() catches up
  state.downloadsSig = sig;
  swipeState.index.clear();
  const list = $("downloadList"), orphans = $("downloadOrphans");
  if (!snap) {
    list.replaceChildren(); orphans.replaceChildren();
    setShown("orphanFiles", false); setShown("downloadsTools", false);
    $("downloadsSummary").textContent = "";
    return;
  }
  const hasItems = snap.packages.length > 0 || snap.links.length > 0;
  setShown("downloadsTools", hasItems);
  const byPackage = new Map();
  const known = new Set(snap.packages.map(p => p.id).filter(Boolean));
  const loose = [];
  for (const file of snap.links) {
    if (file.package_id && known.has(file.package_id)) {
      if (!byPackage.has(file.package_id)) byPackage.set(file.package_id, []);
      byPackage.get(file.package_id).push(file);
    } else {
      loose.push(file);
    }
  }
  const nodes = [];
  for (const pkg of snap.packages) {
    const files = pkg.id ? byPackage.get(pkg.id) || [] : [];
    if (matchesFilter(pkg) || files.some(matchesFilter)) nodes.push(packageRow(pkg, files));
  }
  const looseShown = loose.filter(matchesFilter);
  // Without any package on the page, loose files are the list itself.
  if (!snap.packages.length) for (const file of looseShown) nodes.push(fileRow(file));
  if (!nodes.length) {
    const filtering = state.filter !== "all" || state.query.trim() !== "";
    nodes.push(element("p", filtering && hasItems ? "条件に一致するものはありません" :
      "ダウンロードはありません。「リンク」から追加できます", "empty-state"));
  }
  list.replaceChildren(...nodes);
  const showLoose = snap.packages.length > 0 && looseShown.length > 0;
  setShown("orphanFiles", showLoose);
  orphans.replaceChildren(...(showLoose ? looseShown.map(fileRow) : []));
  $("orphanSummary").textContent = "他のファイル（" + looseShown.length + "件）";
  const rows = snap.packages.length ? snap.packages : snap.links;
  const counts = {running: 0, waiting: 0, finished: 0};
  for (const item of rows) counts[itemState(item)]++;
  $("downloadsSummary").textContent = hasItems ? "実行中 " + counts.running + " · 待機 " +
    counts.waiting + " · 完了 " + counts.finished : "";
  for (const key of Array.from(swipeState.select.keys)) {
    if (!swipeState.index.has(key)) swipeState.select.keys.delete(key);
  }
  updateSelectBar();
}

// Links JD2 holds in the LinkGrabber (names only, max 50) with a one-click start.
async function loadWaiting() {
  try {
    state.waiting = await api("/api/jd2/linkgrabber");
  } catch (e) {
    state.waiting = null;   // optional section: never blocks the download list
  }
  renderWaiting();
}

function renderWaiting() {
  const w = state.waiting;
  const count = w ? w.count : 0;
  setShown("waitingBox", count > 0);
  $("downloadsToggle").textContent = state.dlRunning > 0 ? "停止" : "開始";
  if (!count) { $("waitingList").replaceChildren(); return; }
  $("waitingCount").textContent = (w.has_more ? count + "件以上" : count + "件");
  if (holdRender()) return;
  $("waitingList").replaceChildren(...w.items.slice(0, WAITING_MAX).map(waitingRow));
}

async function startWaiting() {
  const count = state.waiting ? state.waiting.count : 0;
  if (!count || !confirm("JD2で待機中の" + (state.waiting.has_more ? count + "件以上" : count + "件") +
      "をダウンロードリストへ移して開始します。よろしいですか？")) return;
  const result = await api("/api/jd2/linkgrabber/start", {confirmed: true});
  flash(result.moved + "件を開始しました");
  state.downloadsFails = 0;
  await loadDownloads(state.downloadsPage);
}

async function clearFinished() {
  if (!confirm("完了したダウンロードを一覧から削除します。保存されたファイルは削除されません。")) return;
  await api("/api/jd2/downloads/cleanup", {confirmed: true});
  flash("完了したものを一覧から消しました");
  state.downloadsFails = 0;
  await loadDownloads(state.downloadsPage);
}

async function clearHistory() {
  const includeStaged = $("clearStaged").checked;
  const items = state.queueData ? state.queueData.items : [];
  const count = items.filter(l => l.state === "sent" || l.state === "uncertain" ||
    (includeStaged && l.state === "staged")).length;
  if (!count) { flash("削除できる履歴はありません"); return; }
  if (!confirm("送信履歴を" + count + "件削除します（ダウンロード済みファイルやJD2のダウンロードは削除しません）")) return;
  const r = await api("/api/links/clear", {include_staged: includeStaged});
  $("clearStaged").checked = false;
  swrDrop("links");
  await loadQueue(true);
  flash(r.removed + "件の履歴を削除しました");
}

async function toggleDownloads() {
  if (state.dlRunning > 0) {
    if (!confirm("JD2のダウンロードをすべて停止します。よろしいですか？")) return;
    await api("/api/jd2/downloads/stop", {confirmed: true});
    flash("ダウンロードを停止しました");
  } else {
    await api("/api/jd2/downloads/start", {});
    flash("ダウンロードを開始しました");
  }
  state.downloadsFails = 0;
  await loadDownloads(state.downloadsPage);
}

// Entering the downloads view: fetch immediately when the engine is running.
function refreshDownloadsNow() {
  if (!engineRunning()) return;
  state.downloadsFails = 0;
  loadDownloads(state.downloadsPage);
}

async function loadDownloads(page, options = {}) {
  if (state.downloadsBusy || !engineRunning()) return;   // never overlap requests
  state.downloadsBusy = true;
  $("downloadsRefresh").disabled = true;
  try {
    const snap = await api("/api/jd2/downloads?page=" + page);
    state.snapshot = snap;
    state.snapshotJson = JSON.stringify(snap);
    state.snapshotAt = Date.now();
    state.badgeAt = state.snapshotAt;
    state.downloadsPage = snap.page;
    state.downloadsFails = 0;
    if (snap.page === 0) state.dlRunning = countRunning(snap);
    if (snap.page === 0) await loadWaiting();
    renderDownloads();
    updateBadge();
    $("downloadsMessage").textContent = "";
    $("downloadsUpdated").textContent = "更新: " + clockText(state.snapshotAt);
    $("downloadsPage").textContent = (snap.page + 1) + "ページ目";
    $("downloadsPrev").disabled = snap.page === 0;
    $("downloadsNext").disabled = !snap.has_more || snap.page >= 20;
    setShown("downloadsPager", Boolean(snap.has_more) || snap.page > 0);
    renderHome();
  } catch (error) {
    state.downloadsFails++;
    $("downloadsMessage").textContent = "取得できませんでした: " + error.message;
    loadEngine(true).catch(() => {});   // the engine may have stopped meanwhile
  } finally {
    state.downloadsBusy = false;
    $("downloadsRefresh").disabled = false;
    syncAutoRefresh();
  }
}

function autoRefreshAllowed() {
  return state.authed && state.route.view === "downloads" && engineRunning() &&
    document.visibilityState === "visible" && state.downloadsFails < 3;
}

// Local data, so refresh every few seconds, but only while the downloads view is
// on screen and the page is visible. One request at a time; gives up after 3 errors.
function syncAutoRefresh() {
  clearTimeout(state.autoTimer);
  state.autoTimer = null;
  if (!autoRefreshAllowed() || state.downloadsBusy) return;
  const wait = state.snapshot ? Math.max(0, AUTO_REFRESH_MS - (Date.now() - state.snapshotAt)) : 0;
  state.autoTimer = setTimeout(autoTick, wait);
}

async function autoTick() {
  state.autoTimer = null;
  if (!autoRefreshAllowed() || state.downloadsBusy) return;
  await loadDownloads(state.downloadsPage, {auto: true});
}

// Header badge: light page-0 fetch every 30s while the engine runs and the page
// is visible. The downloads view refreshes the count itself.
async function refreshBadge() {
  if (!state.authed || document.visibilityState !== "visible" || !engineRunning() ||
      state.downloadsBusy || state.route.view === "downloads") return;
  if (Date.now() - state.badgeAt < 10000) return;
  state.downloadsBusy = true;
  try {
    const snap = await api("/api/jd2/downloads?page=0");
    state.dlRunning = countRunning(snap);
    state.badgeAt = Date.now();
    updateBadge();
    renderHome();
  } catch (e) { /* silent: the badge just keeps its last value */ }
  finally { state.downloadsBusy = false; }
}

function syncBadgeTimer() {
  const want = state.authed && engineRunning();
  if (want && !state.badgeTimer) state.badgeTimer = setInterval(refreshBadge, BADGE_REFRESH_MS);
  else if (!want && state.badgeTimer) { clearInterval(state.badgeTimer); state.badgeTimer = null; }
}

function setFilter(filter) {
  state.filter = filter;
  document.querySelectorAll(".chip").forEach(c =>
    c.setAttribute("aria-pressed", String(c.dataset.filter === filter)));
  renderDownloads();
}

// ----------------------------------------------------------------- links
function jd2CanSend() { return Boolean(state.jd2 && state.jd2.can_send === true); }

const LINK_STATE_LABELS = {staged: "未送信", sending: "送信中", uncertain: "要確認", sent: "送信済み"};

function renderQueue(q) {
  state.queueData = q;
  const nodes = [];
  const canSend = jd2CanSend();
  const staged = q.items.filter(l => l.state === "staged");
  setShown("sendAll", canSend && staged.length >= 2);
  if (!q.items.length) nodes.push(element("p", "追加したリンクはありません", "empty-state"));
  for (const link of q.items) {
    const container = element("div", undefined, "item link-item");
    container.appendChild(element("span", link.url, "link-url"));
    const row = element("div", undefined, "link-actions");
    const chip = element("span", LINK_STATE_LABELS[link.state] || link.state, "state-chip link-" + link.state);
    if (link.state === "uncertain" || link.state === "sending") chip.title = "送信結果が不明です。JD2で確認してください";
    row.appendChild(chip);
    if (link.state === "staged") {
      if (canSend) {
        const send = element("button", "JD2へ送る", "btn small");
        send.type = "button";
        send.addEventListener("click", () => perform(() => sendLinks([link])));
        row.appendChild(send);
      }
      const del = element("button", "削除", "btn small quiet");
      del.type = "button";
      del.addEventListener("click", () => perform(async () => {
        await api("/api/links/delete", {id: link.id});
        swrDrop("links");
        await loadQueue(true);
      }));
      row.appendChild(del);
    }
    container.appendChild(row);
    nodes.push(container);
  }
  $("queueItems").replaceChildren(...nodes);   // swapped in one step, never cleared first
}

async function loadQueue(force = false) {
  await swr("links", "/api/links", renderQueue, {force});
}

function renderJd2Status(jd) {
  state.jd2 = jd;
  if (state.queueData) renderQueue(state.queueData);
  renderEngineJd2();
}

async function loadJd2Status(force = false) {
  await swr("jd2-status", "/api/jd2/status", renderJd2Status, {force});
}

function sendAutostartOn() { return !state.settings || state.settings.send_autostart !== false; }
function sendStartText() {
  return sendAutostartOn() ? "送信後すぐにダウンロードを開始します。" : "自動開始はOFFです。";
}
function sentText() {
  return sendAutostartOn() ? "JD2へ送り、ダウンロードを開始しました" :
    "JD2のLinkGrabberへ送りました（ダウンロードは未開始）";
}

// Sequential hand-off with one confirmation; stops at the first error.
async function sendLinks(links, opts = {}) {
  const many = links.length > 1;
  // Only the Links page "send all" (5+ links) asks; every other send was asked for explicitly.
  if (opts.confirmMany && links.length >= 5 &&
      !confirm(links.length + "件のリンクをJD2へ順に送ります。" + sendStartText() + "続けますか？")) return;
  let sent = 0;
  try {
    for (const link of links) {
      await api("/api/links/send", {id: link.id});
      sent++;
    }
  } catch (e) {
    swrDrop("links");
    await loadQueue(true).catch(() => {});
    throw new Error((many ? sent + "/" + links.length + "件送信後に停止: " : "") + e.message);
  }
  swrDrop("links");
  await loadQueue(true);
  const dupText = opts.skipped ? "・" + opts.skipped + "件は重複のためスキップ" : "";
  flash(sent + "件を" + (sendAutostartOn() ? "JD2へ送り、ダウンロードを開始しました" :
    "JD2のLinkGrabberへ送りました（ダウンロードは未開始）") + dupText);
}

function sendAllStaged() {
  const items = state.queueData ? state.queueData.items.filter(l => l.state === "staged") : [];
  return items.length ? sendLinks(items, {confirmMany: true}) : Promise.resolve();
}

// ---- one paste box: single https URL, or preview of extracted links
function schedulePaste(delay = PASTE_DEBOUNCE_MS) {
  clearTimeout(state.pasteTimer);
  state.pasteSeq++;
  state.paste = null;
  $("pasteAdd").disabled = true;
  if (!$("pasteBox").value.trim()) { setShown("pasteResult", false); return; }
  state.pasteTimer = setTimeout(() => perform(runPastePreview), delay);
}

function isSingleHttpsUrl(text) { return /^https:\/\/[^\s<>"']+$/.test(text); }

function showPaste(summary, urls, buttonLabel) {
  $("pasteSummary").textContent = summary;
  $("pasteList").replaceChildren(...urls.map(u => element("div", u, "item")));
  $("pasteAdd").textContent = buttonLabel || "追加";
  $("pasteAdd").hidden = !buttonLabel;
  setShown("pasteResult", true);
}

async function runPastePreview() {
  const seq = state.pasteSeq;
  const content = $("pasteBox").value;
  const text = content.trim();
  if (!text) { setShown("pasteResult", false); return; }
  if (isSingleHttpsUrl(text)) {
    state.paste = {kind: "url", url: text};
    showPaste("追加するURL", [text], "このURLを追加");
    $("pasteAdd").disabled = false;
    return;
  }
  const preview = await api("/api/rapidgator/extract", {content, limit: 20, commit: false});
  if (seq !== state.pasteSeq) return;   // the box changed while waiting
  if (!preview.count) {
    state.paste = null;
    showPaste(/^http:\/\/\S+$/.test(text) ? "httpsのURLだけ追加できます" :
      "追加できるリンクが見つかりません", [], "");
    return;
  }
  state.paste = {kind: "extract", content, digest: preview.digest, limit: 20, count: preview.count};
  showPaste(preview.count + "件見つかりました", preview.items, preview.count + "件を追加");
  $("pasteAdd").disabled = false;
}

async function addPasted() {
  const pasted = state.paste;
  if (!pasted) return;
  if (pasted.kind === "extract" && $("pasteBox").value !== pasted.content) {
    flash("貼り付け内容が変わりました。もう一度確認します");
    schedulePaste(0);
    return;
  }
  $("pasteAdd").disabled = true;
  state.paste = null;
  let message;
  if (pasted.kind === "url") {
    await api("/api/links", {url: pasted.url});
    message = "追加しました";
  } else {
    const staged = await api("/api/rapidgator/extract",
      {content: pasted.content, limit: pasted.limit, digest: pasted.digest, commit: true});
    message = staged.staged === pasted.count ? pasted.count + "件を追加しました" :
      staged.staged + "件を追加しました（" + (pasted.count - staged.staged) + "件は追加済み）";
  }
  clearTimeout(state.pasteTimer);
  state.pasteSeq++;
  $("pasteBox").value = "";
  setShown("pasteResult", false);
  swrDrop("links");
  await loadQueue(true);
  flash(message);
}

// -------------------------------------------------------------- settings
function showSettingsSection(section) {
  document.querySelectorAll(".settings-section").forEach(n => { n.hidden = n.dataset.section !== section; });
  document.querySelectorAll(".settings-nav a").forEach(a => {
    if (a.dataset.section === section) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
}

function renderDownloadDir(s) {
  state.settings = s;
  $("sendAutostart").checked = s.send_autostart !== false;
  $("probeAutosend").checked = s.probe_autosend !== false;
  $("blockAds").checked = s.block_ads !== false;
  const displayMode = DISPLAY_OF_SETTING[s.rb_default_mode] ? s.rb_default_mode : "fast";
  document.querySelectorAll('input[name="rbDefaultMode"]').forEach(radio => { radio.checked = radio.value === displayMode; });
  $("rbLinkBoxes").checked = s.rb_link_boxes === true;
  $("selectedFolder").textContent = s.download_dir || "未選択";
  $("folderChange").textContent = s.download_dir ? "変更" : "選ぶ";
  renderHome();
  syncRemoteSettings();
}

function setPicker(open) {
  state.pickerOpen = open;
  setShown("folderPickerBox", open);
}

async function enterSettings(section, force = false) {
  showSettingsSection(section);
  if (section !== "share") hideShareToken();
  if (section !== "accounts") clearAccountForms();
  if (section === "storage") {
    await loadSettings(force);
    // Nothing chosen yet: go straight to the picker.
    if (!state.settings || !state.settings.download_dir) setPicker(true);
    if (state.pickerOpen) {
      await folders(state.folderPath, force).catch(e => {
        if (!state.folderPath) throw e;
        return folders("", force);   // remembered folder vanished: back to the shares
      });
    }
  } else if (section === "engine") {
    await loadEngine(force);
    if (engineRunning()) {
      await loadJd2Status(force);
      checkConnection();
    }
  } else if (section === "accounts") {
    await loadEngine(force);
    renderAccountsAvailability();
    if (engineRunning()) await Promise.all([loadAccounts(force), loadAccountHosts(force)]);
  } else if (section === "browser") {
    renderRemoteHosts();
    await Promise.all([loadBrowserStatus(force), loadSettings(force),
      swr("site-rules", "/api/rb/site-rules", renderSiteRules, {force})]);
  } else if (section === "https") {
    renderHttps();
  } else if (section === "share") {
    renderShareStatic();
    await swr("share-tokens", "/api/share/tokens", renderShareDevices, {force});
  } else if (section === "bookmarks") {
    await loadBookmarks(force);
  } else if (section === "updates") {
    await Promise.all([loadStatus(force), updateStatus(force)]);
  } else if (section === "about") {
    await loadStatus(force);
  }
}

// ---- HTTPS / iPhone share (v0.1.13)
function isHttpsNow() { return state.https === true || location.protocol === "https:"; }

function renderHttps() {
  const secure = isHttpsNow();
  const state_ = $("httpsState");
  state_.classList.toggle("is-ok", secure);
  state_.textContent = secure ?
    (isTailscaleHost(location.hostname) && location.hostname.endsWith(".ts.net") ?
      "HTTPS ✓（ts.net経由）" : "HTTPS ✓") :
    "HTTP（暗号化なし）";
  $("httpsDetail").textContent = secure ?
    "この接続は暗号化され、Cloudflare確認に必要な安全なコンテキストが使えます。" :
    "Cloudflare確認は安全なコンテキスト（HTTPS）でないと通りにくくなります。下の手順でHTTPSを有効にできます。";
  $("httpsCmd").textContent = HTTPS_SERVE_COMMAND;
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    flash("コピーしました");
  } catch (e) { flash("コピーできませんでした。長押しで選択してください"); }
}

function shareEndpoint() { return location.origin + "/api/share"; }

function shareBookmarklet() {
  return "javascript:location.href='" + location.origin + "/#/links?paste='+encodeURIComponent(location.href)";
}

function renderShareStatic() {
  $("shareEndpoint").textContent = shareEndpoint();
  $("shareJs").textContent = SHARE_JS;
  $("shareBookmarklet").textContent = shareBookmarklet();
  const clear = !isHttpsNow() && location.protocol === "http:" &&
    !isTailscaleHost(location.hostname) && state.tailnet !== true;
  setShown("shareHttpWarning", clear);
}

function hideShareToken() {
  $("shareToken").textContent = "";
  setShown("shareNew", false);
}

function formatTime(seconds) {
  if (typeof seconds !== "number") return "";
  try { return new Date(seconds * 1000).toLocaleString("ja-JP"); } catch (e) { return ""; }
}

function renderShareDevices(data) {
  const items = Array.isArray(data.items) ? data.items : [];
  if (!items.length) {
    $("shareDevices").replaceChildren(element("p", "登録した端末はありません。", "empty-state"));
    return;
  }
  const nodes = [];
  for (const device of items) {
    const box = element("div", undefined, "item");
    box.appendChild(element("b", device.name));
    box.appendChild(element("span", "登録 " + formatTime(device.created_at) + " · 最終使用 " +
      (device.last_used_at ? formatTime(device.last_used_at) : "未使用")));
    const del = element("button", "削除", "btn small");
    del.type = "button";
    del.addEventListener("click", () => perform(async () => {
      if (!confirm("「" + device.name + "」を削除します。この端末からは送れなくなります。よろしいですか？")) return;
      await api("/api/share/tokens/delete", {id: device.id});
      swrDrop("share-tokens");
      await swr("share-tokens", "/api/share/tokens", renderShareDevices, {force: true});
    }));
    box.appendChild(del);
    nodes.push(box);
  }
  $("shareDevices").replaceChildren(...nodes);
}

async function addShareDevice() {
  const name = $("shareName").value.trim();
  const made = await api("/api/share/tokens", {name});
  $("shareName").value = "";
  $("shareToken").textContent = made.token;   // shown once; cleared on leaving the section
  setShown("shareNew", true);
  swrDrop("share-tokens");
  await swr("share-tokens", "/api/share/tokens", renderShareDevices, {force: true});
}

function renderFolders(path, data) {
  const nodes = [];
  if (path) {
    nodes.push(element("p", data.path, "path"));
    const select = element("button", "このフォルダにする", "btn primary");
    select.type = "button";
    select.disabled = !data.writable;
    select.addEventListener("click", () => perform(async () => {
      await api("/api/settings/folder", {path: data.path});
      swrDrop("settings", "status");
      await loadSettings(true);
      setPicker(false);
      flash("保存先を変更しました");
    }));
    nodes.push(select);
    const back = element("button", "共有フォルダ一覧へ", "btn");
    back.type = "button";
    back.addEventListener("click", () => perform(() => folders("")));
    nodes.push(back);
  }
  for (const child of data.items) {
    const btn = element("button", undefined, "folder");
    btn.type = "button";
    btn.appendChild(icon("folder"));
    btn.appendChild(element("span", child.name + (child.writable ? "" : "（読取のみ）")));
    btn.addEventListener("click", () => perform(() => folders(child.path)));
    nodes.push(btn);
  }
  $("folderPicker").replaceChildren(...nodes);
}

// The picker is rebuilt only when the folder listing it shows actually changed (see swr).
async function folders(path, force = false) {
  const previous = state.folderPath;
  state.folderPath = path;
  try {
    await swr("folders:" + path, "/api/folders" + (path ? "?path=" + encodeURIComponent(path) : ""),
      data => renderFolders(path, data), {slot: "folderPicker", force});
  } catch (e) {
    state.folderPath = previous;
    throw e;
  }
}

const UPDATE_PHASES = {
  idle: "待機中", queued: "開始待ち", checking: "確認中", downloading: "取得中",
  backup: "バックアップ中", installing: "適用中", verifying: "検証中",
  success: "完了", failed: "失敗",
};

function syncUpdateApply() {
  const info = state.updateInfo;
  const ok = Boolean(state.selectedVersion && info && info.supported && !info.busy);
  setShown("updateApply", ok);
}

function renderUpdateStatus(s) {
  state.updateInfo = s;
  let text = "";
  if (!s.supported) {
    text = "この環境ではQPKGの更新を適用できません" + (s.unsupported_reason ? "（" + s.unsupported_reason + "）" : "");
  } else if (s.phase && s.phase !== "idle") {
    text = "更新: " + (UPDATE_PHASES[s.phase] || "処理中") + (s.message ? " — " + s.message : "");
  }
  $("updateStatus").textContent = text;
  syncUpdateApply();
}

async function updateStatus(force = false) {
  await swr("update-status", "/api/update/status", renderUpdateStatus, {force});
}

async function checkUpdate() {
  const channel = $("updateChannel").value;
  const u = await api("/api/update/check?channel=" + encodeURIComponent(channel));
  state.selectedVersion = u.available ? u.release.version : "";
  state.selectedChannel = channel;
  $("updateResult").textContent = u.available ? u.release.version + " があります" : "最新です";
  await updateStatus(true);
  syncUpdateApply();
}

async function applyPreset() {
  const preview = await api("/api/jd2/preset/preview");
  if (!preview.applicable) {
    flash("一部のJD2設定を特定できないため適用を停止しました: " + (preview.missing || []).join(", "));
    return;
  }
  const changed = preview.entries.filter(x => x.change);
  if (!changed.length) { flash("推奨設定からの変更はありません"); return; }
  if (!confirm("次のJD2設定をOFFに変更します（既存キューは変更しません）。\n" +
      changed.map(x => "・" + x.label).join("\n") + "\n適用しますか？")) return;
  await api("/api/jd2/preset/apply", {confirmed: true});
  swrDrop("engine", "status");
  await Promise.all([loadJd2Status(true), loadEngine(true)]);
  flash("推奨設定を再適用しました");
}

// ------------------------------------------------------- update auto reload
async function fetchHealthVersion() {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), 4000);
  try {
    const res = await fetch("/api/health", {cache: "no-store", credentials: "same-origin", signal: ctl.signal});
    if (!res.ok) return "";
    const data = await res.json();
    return typeof data.version === "string" ? data.version : "";
  } catch (e) {
    return "";   // service restarting / offline: not an error here
  } finally {
    clearTimeout(timer);
  }
}

async function serverVersion() {
  return (await fetchHealthVersion()) || (state.status && state.status.version) || state.bootVersion;
}

// After POST /api/update/apply: poll /api/health until it reports a different
// version, then reload. Sessions are in memory, so the login screen follows.
async function watchForUpdate(before) {
  if (state.updateWatching) return;
  state.updateWatching = true;
  document.querySelectorAll("#notice [data-sticky='stale']").forEach(n => n.remove());
  stickyToast("update", "更新中… 完了後に自動で再読み込みします（再読み込み後に再ログインが必要です）", "progress");
  const deadline = Date.now() + UPDATE_WATCH_MS;
  while (Date.now() < deadline) {
    await new Promise(resolve => setTimeout(resolve, UPDATE_POLL_MS));
    const version = await fetchHealthVersion();
    if (version && before && version !== before) {
      location.reload();
      return;
    }
  }
  state.updateWatching = false;
  stickyToast("update", "更新の完了を確認できませんでした。Hubの更新ログを確認し、手動で再読み込みしてください" +
    "（再読み込み後に再ログインが必要です）", "error",
    [{label: "再読み込み", run: () => location.reload()}]);
}

// ------------------------------------------------------------ session / boot
function teardownSession() {
  state.authed = false;
  state.shellReady = false;
  clearTimeout(state.autoTimer);
  state.autoTimer = null;
  clearTimeout(state.pasteTimer);
  clearInterval(state.badgeTimer);
  state.badgeTimer = null;
  state.pasteSeq++;
  state.paste = null;
  state.settings = null;
  state.jd2 = null;
  state.queueData = null;
  state.engine = null;
  state.dlRunning = null;
  state.expanded = new Set();
  state.pickerOpen = false;
  state.stepsMode = "";
  state.downloadsFails = 0;
  for (const tab of state.tabs) { tab.token++; clearTimeout(tab.rb && tab.rb.linksTimer); tab.rb = null; }
  state.rbPolling = "";
  state.rbGen++;
  state.rbStatus = null;
  clearTimeout(state.rbTimer);
  setPerf(false);
  closeProbeSheet();
  hidePopupToast();
  state.siteRules = null;
  state.waiting = null;
  cancelPress();
  closeStream();
  $("remoteImg").removeAttribute("src");
  $("frames").replaceChildren();   // unload every external page
  state.tabs = [];
  state.closed = [];
  state.lastWebTabId = null;
  state.snapshot = null;
  state.snapshotJson = "";
  state.downloadsSig = "";
  state.snapshotAt = 0;
  state.folderPath = "";
  state.bookmarks = [];
  state.status = null;
  state.accounts = null;
  state.accountHosts = [];
  state.accountEditId = "";
  clearAccountForms();
  swrReset();
  writeSession(SHELL_KEY, null);
  for (const id of ["downloadList", "downloadOrphans", "queueItems", "pasteList",
      "folderPicker", "bookmarkItems", "shareDevices", "accountsList", "speedDial", "bookmarkBar", "webTabs"]) {
    $(id).replaceChildren();
  }
  $("downloadsSummary").textContent = "";
  $("pasteBox").value = "";
  hideShareToken();
  setShown("pasteResult", false);
  setShown("onboarding", false);
  setShown("homeStatus", false);
  closeMenu(false);
  closeSwitcher(false);
  updateBadge();
}

function initShellState() {
  if (state.shellReady) return;
  state.shellReady = true;
  restoreTabs();
  removeLocal(LEGACY_AUTO_KEY);   // v0.1.11 auto-refresh switch no longer exists
}

// iOS may reload the home-screen app after killing its WebContent process.
// Paint the shell (tab strip, last route) from sessionStorage right away, then
// let go() verify the session. No external page is loaded and no API is called.
function paintShellEarly() {
  const raw = readSession(SHELL_KEY);
  if (!raw) return;
  let saved = null;
  try { saved = JSON.parse(raw); } catch (e) { saved = null; }
  if (!saved || saved.v !== 1) return;
  initShellState();
  if (!location.hash && typeof saved.hash === "string" && /^#\/[A-Za-z0-9\/_-]{0,80}$/.test(saved.hash)) {
    history.replaceState(null, "", saved.hash);
  }
  const route = parseRoute();
  state.route = route.view === "tab" && !findTab(route.tabId) ? {view: "home"} : route;
  setShown("authArea", false);
  setShown("app", true);
  const shown = currentViewName();
  for (const name of ["home", "downloads", "links", "settings", "web"]) {
    $("view-" + name).hidden = name !== shown;
  }
  if (state.route.view === "settings") showSettingsSection(state.route.section);
  renderWebTabs();
  updatePinnedTabs();
}

async function go() {
  try {
    const session = await api("/api/session");
    state.csrf = session.csrf || "";
    state.https = session.https === true;
    state.tailnet = session.tailnet === true;
    updateLanWarning(session.tailnet === true, state.https);
    if (session.authenticated) {
      state.authed = true;
      setShown("authArea", false);
      setShown("app", true);
      initShellState();
      loadBookmarks().catch(() => {});
      loadStatus().catch(() => {});   // also detects "Hub updated since this page loaded"
      applyRoute();
    } else {
      teardownSession();
      setShown("app", false);
      const boot = await api("/api/setup/status");
      setShown("authArea", true);
      setShown("setup", !boot.configured);
      setShown("login", boot.configured);
    }
  } catch (e) {
    toast(e.message, "error");
  }
}

// ----------------------------------------------------------- event wiring
function isTypingTarget(node) {
  if (!node || !node.tagName) return false;
  return ["INPUT", "TEXTAREA", "SELECT"].includes(node.tagName) || node.isContentEditable;
}

function onKeydown(event) {
  if (!state.authed) return;
  if (event.key === "Escape") {
    if (!$("menu").hidden) { closeMenu(); return; }
    if (!$("tabSwitcher").hidden) { closeSwitcher(); return; }
    if (event.target === $("omnibox")) { restoreOmnibox(); $("omnibox").blur(); }
    return;
  }
  if (isTypingTarget(event.target)) return;
  const slash = event.key === "/" && !event.ctrlKey && !event.metaKey && !event.altKey;
  const palette = (event.ctrlKey || event.metaKey) && !event.altKey && event.key.toLowerCase() === "k";
  if (slash || palette) {
    event.preventDefault();
    focusOmnibox();
  }
}

function onMenuKeydown(event) {
  if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
  const items = Array.from($("menu").querySelectorAll("[role='menuitem']")).filter(n => !n.hidden && !n.disabled);
  if (!items.length) return;
  event.preventDefault();
  const at = items.indexOf(document.activeElement);
  const step = event.key === "ArrowDown" ? 1 : -1;
  items[(at + step + items.length) % items.length].focus();
}

function onTabstripKeydown(event) {
  if (event.key !== "ArrowRight" && event.key !== "ArrowLeft") return;
  const tabs = Array.from(document.querySelectorAll("#tabstrip [role='tab']"));
  const at = tabs.indexOf(document.activeElement);
  if (at < 0) return;
  event.preventDefault();
  const step = event.key === "ArrowRight" ? 1 : -1;
  tabs[(at + step + tabs.length) % tabs.length].focus();
}

function wireShell() {
  window.addEventListener("hashchange", applyRoute);
  document.addEventListener("visibilitychange", syncAutoRefresh);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") refreshBadge();
    // Long-open page: notice a Hub update done elsewhere (toast only, no reload).
    if (document.visibilityState === "visible" && state.authed &&
        Date.now() - state.statusCheckedAt > STALE_CHECK_MS) {
      loadStatus(true).catch(() => {});
    }
  });
  $("cfCompat").addEventListener("click", () => {
    const tab = activeWebTab();
    if (tab) perform(() => setMode(tab, "compat"));
  });
  $("cfDismiss").addEventListener("click", () => {
    const tab = activeWebTab();
    if (tab) { tab.hintDismissed = true; updateChrome(); }
  });
  document.addEventListener("keydown", onKeydown);
  document.addEventListener("click", event => {
    if (!$("menu").hidden && !event.target.closest(".menu-wrap")) closeMenu(false);
  });
  $("tabstrip").addEventListener("keydown", onTabstripKeydown);
  $("newTab").addEventListener("click", createBlankTab);

  $("navBack").addEventListener("click", () => {
    const tab = activeWebTab();
    if (tab) perform(() => stepHistory(tab, -1));
    else history.back();
  });
  $("navForward").addEventListener("click", () => {
    const tab = activeWebTab();
    if (tab) perform(() => stepHistory(tab, 1));
    else history.forward();
  });
  $("navReload").addEventListener("click", () => {
    const tab = activeWebTab();
    if (tab && tab.history.length) perform(() => reloadTab(tab));
    else if (state.route.view === "downloads") { state.downloadsFails = 0; loadDownloads(state.downloadsPage); }
    else perform(() => enterView(state.route, true));
  });

  $("omniForm").addEventListener("submit", event => {
    event.preventDefault();
    const text = $("omnibox").value;
    $("omnibox").blur();
    perform(() => handleOmnibox(text));
  });
  $("omnibox").addEventListener("focus", () => $("omnibox").select());
  $("omnibox").addEventListener("paste", event => {
    // A single-line input flattens newlines; inspect the clipboard ourselves.
    const text = event.clipboardData ? event.clipboardData.getData("text") : "";
    if (/[\r\n]/.test(text.trim())) {
      event.preventDefault();
      $("omnibox").blur();
      perform(() => handleOmnibox(text));
    }
  });
  $("starBtn").addEventListener("click", () => {
    const tab = activeWebTab();
    if (tab) perform(() => bookmarkTab(tab));
  });

  $("menuButton").addEventListener("click", () => { if ($("menu").hidden) openMenu(); else closeMenu(); });
  $("menu").addEventListener("keydown", onMenuKeydown);
  $("menu").addEventListener("click", event => {
    const item = event.target.closest("[data-action]");
    if (!item || item.disabled) return;
    closeMenu(false);
    perform(() => menuAction(item.dataset.action));
  });

  $("mobileBrowse").addEventListener("click", onMobileBrowse);
  $("switcherNew").addEventListener("click", () => { closeSwitcher(false); createBlankTab(); });
  $("switcherClose").addEventListener("click", () => closeSwitcher());
}

function wireAuth() {
  $("setupForm").addEventListener("submit", event => {
    event.preventDefault();
    perform(async () => {
      await api("/api/setup", {password: $("setupPassword").value});
      $("setupPassword").value = "";
      await go();
    });
  });
  $("loginForm").addEventListener("submit", event => {
    event.preventDefault();
    perform(async () => {
      const r = await api("/api/login", {password: $("password").value});
      state.csrf = r.csrf;
      $("password").value = "";
      await go();
    });
  });
}

function wireDownloads() {
  $("downloadsRefresh").addEventListener("click", () => {
    state.downloadsFails = 0;
    loadDownloads(state.downloadsPage);
  });
  wireSheet();
  $("autoLiveChip").addEventListener("click", () => returnToFast(activeWebTab()));
  $("waitingStart").addEventListener("click", () => perform(startWaiting));
  $("downloadsToggle").addEventListener("click", () => perform(toggleDownloads));
  $("downloadsClear").addEventListener("click", () => perform(clearFinished));
  $("clearHistory").addEventListener("click", () => perform(clearHistory));
  $("downloadsPrev").addEventListener("click", () => loadDownloads(state.downloadsPage - 1));
  $("downloadsNext").addEventListener("click", () => loadDownloads(state.downloadsPage + 1));
  document.querySelectorAll(".chip").forEach(c => c.addEventListener("click", () => setFilter(c.dataset.filter)));
  $("downloadsSearch").addEventListener("input", () => {
    state.query = $("downloadsSearch").value;
    renderDownloads();
  });
}

function wireLinks() {
  $("pasteBox").addEventListener("input", () => schedulePaste());
  $("pasteAdd").addEventListener("click", () => perform(addPasted));
  $("sendAll").addEventListener("click", () => perform(sendAllStaged));
}

// ------------------------------------------------------------------ engine
const PHASE_LABELS = {
  not_installed: "未導入", setup: "初期設定中", starting: "起動中…", running: "稼働中",
  stopped: "停止中", failed: "失敗",
};
const STEP_MARKS = {pending: "○", running: "…", done: "✓", skipped: "↷", warn: "!", failed: "✕"};
const STEP_TEXT = {pending: "待機", running: "実行中", done: "完了", skipped: "スキップ", warn: "警告", failed: "失敗"};

function engineSectionVisible() {
  return state.route.view === "settings" && state.route.section === "engine";
}

// Poll only while a setup/start is in progress AND a view that shows it (engine
// section, home or downloads) is on screen and the page is visible.
function enginePollWanted() {
  const e = state.engine;
  return Boolean(state.authed && e && (e.phase === "setup" || e.phase === "starting") &&
    document.visibilityState === "visible" &&
    (engineSectionVisible() || currentViewName() === "home" || state.route.view === "downloads"));
}

function syncEnginePoll() {
  clearTimeout(state.engineTimer);
  state.engineTimer = null;
  if (!enginePollWanted() || state.engineBusy) return;
  state.engineTimer = setTimeout(engineTick, ENGINE_POLL_MS);
}

async function engineTick() {
  state.engineTimer = null;
  if (!enginePollWanted() || state.engineBusy) return;
  state.engineBusy = true;
  try {
    applyEngine(await api("/api/engine/status"));
    if ($("engineLogBox").open) await loadEngineLog();
  } catch (e) {
    toast("エンジンの状態を取得できませんでした: " + e.message, "error");
    state.engineBusy = false;
    return;   // polling stops until the next user action
  }
  state.engineBusy = false;
  syncEnginePoll();
}

function applyEngine(e) {
  const before = state.engine ? state.engine.phase : "";
  state.engine = e;
  renderEngine(e);
  renderEngineJd2();
  renderHome();
  renderDownloadsShell();
  if (e.phase !== "running") {
    state.dlRunning = null;
    updateBadge();
  } else if (before !== "running") {
    state.badgeAt = 0;
    refreshBadge();
    if (engineSectionVisible()) { loadJd2Status(true).catch(() => {}); checkConnection(); }
  }
  syncEnginePoll();
  syncBadgeTimer();
  syncAutoRefresh();
}

async function loadEngine(force = false) {
  await swr("engine", "/api/engine/status", applyEngine, {force});
}

// "3/9 Javaの展開と動作確認…" for the step that is running (the final marker step is not counted).
function setupProgressText(e) {
  const steps = (e.steps || []).filter(x => x.id !== "done");
  if (!steps.length) return "準備しています…";
  const at = steps.findIndex(x => x.status === "running");
  if (at < 0) {
    const done = steps.filter(x => x.status === "done" || x.status === "skipped").length;
    return done + "/" + steps.length + " 準備しています…";
  }
  return (at + 1) + "/" + steps.length + " " + steps[at].label + "…";
}

function renderEngine(e) {
  const phase = e.phase;
  const busy = phase === "setup" || phase === "starting";
  const running = phase === "running";
  const failedSetup = !e.installed && Boolean(e.error);
  const stateText = $("engineState");
  stateText.textContent = phase === "failed" && e.installed ? "起動できません" : PHASE_LABELS[phase] || phase;
  stateText.classList.toggle("is-ok", running);
  // One primary action per state; unusable buttons are hidden, not disabled.
  const noCs = !e.installed && !busy && (e.container || {}).available === false;
  $("engineSetup").hidden = e.installed || busy || noCs;
  $("engineSetup").textContent = failedSetup ? "初期設定をやり直す" : "初期設定を開始";
  // Execution mode: container (Container Station) is the default; native is the fallback.
  const cs = e.container || {};
  const modeText = e.mode === "container" ? "コンテナ (Container Station)" : "QTS直接実行";
  $("engineMode").textContent = "実行方式: " + modeText +
    (e.mode === "container" && cs.state === "running" ? "（" + cs.name + "）" : "");
  setShown("engineMigrate", Boolean(e.can_migrate) && !busy);
  setShown("engineNoContainer", noCs);
  setShown("engineSetupNative", noCs);
  $("engineStart").hidden = !(e.installed && !running && !busy);
  $("engineStop").hidden = !running || phase === "setup";
  setShown("engineResetup", e.installed && !running && !busy);
  // Leftover QTS-direct Java (container mode only); hidden when nothing is left.
  const left = e.native_leftovers || {};
  const canClean = left.present === true && e.mode === "container" && e.installed && !busy;
  setShown("engineNativeRow", canClean);
  if (canClean) {
    $("engineNativeRemove").textContent = "従来方式のJava（約" + Math.max(1, Math.round((left.bytes || 0) / 1048576)) + " MB）を削除";
  }
  setShown("engineKeysRow", running);
  const progress = $("engineProgress");
  const showProgress = busy;
  setShown("engineProgressText", showProgress);
  progress.hidden = phase !== "setup";
  if (showProgress) {
    $("engineProgressText").textContent = phase === "setup" ? setupProgressText(e) : "起動しています…";
    const current = (e.steps || []).find(x => x.status === "running");
    if (current && typeof current.progress === "number") progress.value = current.progress;
    else progress.removeAttribute("value");
  }
  $("engineError").hidden = !e.error;
  $("engineError").textContent = e.error || "";
  if (!running) $("engineTestResult").textContent = "";
  setShown("engineAutostartRow", e.installed === true);
  $("engineAutostart").checked = Boolean(e.autostart);

  // Details: components, version, hashes.
  const comps = (e.components || []).map(c => {
    const box = element("div", undefined, "item");
    box.appendChild(element("b", c.label));
    box.appendChild(element("span", (c.installed ? "導入済み" : "未導入") +
      " · 版 " + (c.version || c.pinned_version || "不明") + " · SHA-256 " + c.sha256 + "…" +
      (c.size ? " · " + readableSize(c.size) : "")));
    return box;
  });
  $("engineComponents").replaceChildren(...comps);
  const meta = [];
  if (e.core_revision && running) meta.push("Core revision " + e.core_revision);
  if (typeof e.restart_count === "number" && e.restart_count > 0) meta.push("自動再起動 " + e.restart_count + "回");
  $("engineMeta").textContent = meta.join(" · ");

  // Steps: open while running or after a failure; a collapsed record after success.
  const steps = e.steps || [];
  const active = steps.some(x => x.status !== "pending");
  const failed = Boolean(e.error) || steps.some(x => x.status === "failed");
  const mode = phase === "setup" ? "run" : failed && active ? "fail" : e.installed && active ? "done" : "none";
  const box = $("engineStepsBox");
  box.hidden = mode === "none";
  if (mode !== state.stepsMode) {
    state.stepsMode = mode;
    box.open = mode === "run" || mode === "fail";
    $("engineStepsSummary").textContent = mode === "done" ? "前回の初期設定の記録" : "初期設定の進行状況";
  }
  $("engineSteps").replaceChildren(...steps.map(step => {
    const li = element("li", undefined, "engine-step is-" + step.status);
    li.appendChild(element("span", STEP_MARKS[step.status] || "?", "step-mark"));
    const body = element("span", undefined, "step-body");
    body.appendChild(element("b", step.label));
    body.appendChild(element("span", " — " + (STEP_TEXT[step.status] || step.status) +
      (step.detail ? "：" + step.detail : ""), "step-detail"));
    if (typeof step.progress === "number" && step.status === "running") {
      const bar = element("progress");
      bar.max = 100; bar.value = step.progress;
      body.appendChild(bar);
    }
    li.appendChild(body);
    return li;
  }));
}

// JD2 update / preset row: only while the engine runs; hidden buttons instead of disabled ones.
function renderEngineJd2() {
  const e = state.engine, jd = state.jd2;
  const show = engineRunning();
  setShown("engineJd2", show);
  if (!show) return;
  const connected = Boolean(jd && jd.connected);
  const notes = [];
  if (connected && jd.update_available) notes.push("JD2の更新があります");
  if (e && e.preset_applied !== true) notes.push("推奨設定が未適用のため、リンクを送れません");
  $("jd2Status").textContent = notes.join(" · ");
  setShown("jd2Check", connected && jd.can_update === true);
  setShown("jd2Update", connected && jd.can_update === true && jd.update_available === true);
  setShown("jd2Preset", connected && jd.can_preset === true);
}

// Inline, automatic and read-only: runs when the engine section opens.
async function checkConnection() {
  if (state.connChecking || !engineRunning()) return;
  state.connChecking = true;
  const out = $("engineTestResult");
  out.textContent = "接続を確認しています…";
  try {
    await api("/api/jd2/connect/test", {});
    out.textContent = "接続OK";
  } catch (err) {
    out.textContent = "接続できません: " + err.message;
  } finally {
    state.connChecking = false;
  }
}

async function loadEngineKeys() {
  const data = await api("/api/engine/config-keys");
  const lines = (data.items || []).map(i => i.interface + "." + i.key + " = " +
    (i.value === null || i.value === undefined ? "-" : String(i.value)) +
    " (" + (i.type || "?") + ")");
  const out = $("engineKeysOut");
  out.textContent = lines.join("\n") || "該当する項目がありません";
  out.hidden = false;
  $("engineKeysCopy").hidden = lines.length === 0;
}

async function loadEngineLog() {
  const data = await api("/api/engine/log");
  $("engineLog").textContent = (data.lines || []).join("\n") || "まだログはありません";
}

function engineSetupConfirm(mode) {
  if (mode === "container") {
    return confirm("JD2エンジンの初期設定を開始します（コンテナ方式）。\n\n" +
      "・Container Station上に、Hub専用のコンテナ qnapdl-jd2 を作ります（公式イメージ eclipse-temurin:21-jre をダイジェスト固定で取得）\n" +
      "・ポートは公開せず、専用ネットワークのみ。非rootで読み取り専用、メモリ上限1GB、空きメモリ1.5GB未満では起動しません\n" +
      "・JDownloader.jar（公式インストーラ、SHA-256検証）を engine/jd2 に置きます\n" +
      "・コンテナに見えるのは engine/jd2 と選択済みの保存先だけです\n" +
      "取得後、JD2が公式アップデートを自己適用するため時間がかかります。\n" +
      "ダウンロードは自動開始しません。既存のDocker版JD2・他のコンテナには触れません。\n\nよろしいですか？");
  }
  return confirm("JD2エンジンの初期設定を開始します（QTS直接実行）。\n\n" +
    "取得元（公式の固定URL・SHA-256検証つき）:\n" +
    "・Java 21 JRE（Eclipse Temurin 21.0.13+9）github.com/adoptium 約52MB\n" +
    "・JDownloader.jar（公式インストーラ）installer.jdownloader.org 約5MB\n\n" +
    "配置先: Hubの永続データ領域の engine/ フォルダ（QPKGには同梱しません）\n" +
    "空き容量400MB以上が必要です。取得後、JD2が公式アップデートを自己適用するため時間がかかります。\n" +
    "ダウンロードは自動開始しません。既存のDocker版JD2には触れません。\n\nよろしいですか？");
}

// mode: "container" | "native" | undefined (the Hub picks: container when available).
async function startEngineSetup(mode) {
  const e = state.engine || {};
  const chosen = mode || (e.installed ? e.mode : e.preferred_mode) || "native";
  if (!engineSetupConfirm(chosen)) return;
  const body = {confirmed: true};
  if (mode) body.mode = mode;
  await api("/api/engine/setup", body);
  swrDrop("engine", "status");
  flash("初期設定を開始しました");
  applyEngine(await api("/api/engine/status"));
}

async function migrateEngine() {
  if (!confirm("JD2をコンテナ方式へ移行します。\n\n" +
      "・いまのJD2（QTS直接実行）を停止し、Container Stationのコンテナとして再起動します（実行中のダウンロードは中断されます）\n" +
      "・設定・キュー・アカウント・部分ファイルは engine/jd2 のまま引き継ぎます（データは消えません）\n" +
      "・保存先と推奨設定は移行後に再確認されます\n\nよろしいですか？")) return;
  await api("/api/engine/setup", {confirmed: true, mode: "container"});
  swrDrop("engine", "status");
  flash("コンテナへの移行を開始しました");
  applyEngine(await api("/api/engine/status"));
}

async function startEngine() {
  await api("/api/engine/start", {});
  swrDrop("engine", "status");
  applyEngine(await api("/api/engine/status"));
}

// ---- Settings › アカウント (v0.1.23)
// Credentials only ever live in the two password inputs below. They are
// never put into storage, a URL, a log or a DOM text node, and the inputs are
// emptied right after the request is built (and whenever the section closes).
const ACCOUNT_STATUS = {valid: "正常", invalid: "認証エラー", expired: "期限切れ",
  temporarily_unavailable: "一時的に利用不可", disabled: "無効", unknown: "不明"};
const ACCOUNT_SECURE_MESSAGE = "パスワードはHTTPSまたはTailscale経由でのみ送信できます。";

// The server decides (TCP peer + trusted TLS terminator); this only mirrors it.
function accountsSecureNow() { return state.https === true || state.tailnet === true; }

function clearAccountForms() {
  for (const id of ["accountPass", "accountEditPass"]) $(id).value = "";
  $("accountEditUser").value = "";
  setShown("accountEdit", false);
  state.accountEditId = "";
}

function accountDateText(iso) {
  if (typeof iso !== "string") return "不明";
  const moment = new Date(iso);
  return Number.isNaN(moment.getTime()) ? "不明" : moment.toLocaleDateString("ja-JP");
}

function accountTrafficText(item) {
  if (item.traffic_unlimited === true) return "無制限";
  if (typeof item.traffic_left_bytes !== "number") return "不明";
  const left = readableSize(item.traffic_left_bytes);
  return typeof item.traffic_max_bytes === "number" && item.traffic_max_bytes > 0 ?
    left + " / " + readableSize(item.traffic_max_bytes) : left;
}

function accountSummary(item) {
  const type = item.account_type === "premium" ? "Premium" :
    item.account_type === "free" ? "Free" : "種別不明";
  if (item.status === "valid") {
    return type + (item.valid_until ? "・有効期限 " + accountDateText(item.valid_until) : "") +
      (item.enabled === false ? "・無効" : "・有効");
  }
  return type + "・" + (ACCOUNT_STATUS[item.status] || "不明") +
    (item.error_summary ? "（" + item.error_summary + "）" : "");
}

function renderAccountsAvailability() {
  const secure = accountsSecureNow();
  const running = engineRunning();
  setShown("accountsSecure", !secure);
  $("accountsSecureText").textContent = ACCOUNT_SECURE_MESSAGE;
  for (const id of ["accountHost", "accountUser", "accountPass", "accountAdd",
      "accountEditUser", "accountEditPass", "accountEditSave"]) {
    $(id).disabled = !(secure && running);
  }
  $("accountsReload").disabled = !running;
  if (!running) {
    $("accountsState").textContent = "JD2エンジンが起動していません。設定 › エンジン で起動すると、アカウントを管理できます。";
    $("accountsList").replaceChildren();
  }
}

function accountButton(label, handler, item) {
  const button = element("button", label, "btn small");
  button.type = "button";
  button.disabled = state.accountBusy;
  button.addEventListener("click", () => perform(() => handler(item)));
  return button;
}

function accountCard(item) {
  const card = element("div", undefined, "item");
  const head = element("div", undefined, "account-head");
  head.appendChild(element("b", item.host));
  head.appendChild(element("span", item.display_username || "（名前不明）"));
  const type = element("span", item.account_type === "premium" ? "Premium" :
    item.account_type === "free" ? "Free" : "種別不明",
    "account-badge" + (item.account_type === "premium" ? " is-ok" : ""));
  head.appendChild(type);
  head.appendChild(element("span", item.enabled === true ? "有効" : item.enabled === false ? "無効" : "有効/無効 不明",
    "account-badge" + (item.enabled === false ? " is-bad" : "")));
  card.appendChild(head);
  const meta = element("div", undefined, "account-meta");
  const bad = ["invalid", "expired", "temporarily_unavailable"].includes(item.status);
  meta.appendChild(element("span", "状態: " + (ACCOUNT_STATUS[item.status] || "不明") +
    (item.error_summary ? "（" + item.error_summary + "）" : ""), bad ? "engine-error" : undefined));
  meta.appendChild(element("span", "有効期限: " + accountDateText(item.valid_until)));
  meta.appendChild(element("span", "残り容量: " + accountTrafficText(item)));
  card.appendChild(meta);
  const actions = element("div", undefined, "account-actions");
  actions.appendChild(accountButton("再確認", refreshAccount, item));
  actions.appendChild(accountButton(item.enabled === false ? "有効にする" : "無効にする", toggleAccount, item));
  const edit = accountButton("認証情報を更新", openAccountEdit, item);
  edit.disabled = state.accountBusy || !accountsSecureNow();
  actions.appendChild(edit);
  actions.appendChild(accountButton("削除", deleteAccount, item));
  card.appendChild(actions);
  return card;
}

function renderAccounts(data) {
  state.accounts = Array.isArray(data.items) ? data.items : [];
  state.accountsTruncated = data.truncated === true;
  renderAccountsAvailability();
  if (engineRunning()) {
    $("accountsState").textContent = state.accounts.length ?
      state.accounts.length + "件のアカウント" + (data.truncated ? "（上限200件まで表示）" : "") :
      "登録されているアカウントはありません。下のフォームから追加できます。";
    $("accountsList").replaceChildren(...state.accounts.map(accountCard));
  }
  renderHome();
}

async function loadAccounts(force = false) {
  await swr("accounts", force ? "/api/accounts?refresh=1" : "/api/accounts", renderAccounts, {force});
}

function renderAccountHosts(data) {
  state.accountHosts = Array.isArray(data.items) ? data.items : [];
  $("accountHostList").replaceChildren(...state.accountHosts.map(host => {
    const option = document.createElement("option");
    option.value = host;
    return option;
  }));
  if (!$("accountHost").value && state.accountHosts.includes("rapidgator.net")) {
    $("accountHost").value = "rapidgator.net";
  }
}

async function loadAccountHosts(force = false) {
  await swr("account-hosts", "/api/accounts/hosts", renderAccountHosts, {force});
}

// Every change goes through here: buttons are locked, the list is re-read after.
async function accountChange(path, body) {
  state.accountBusy = true;
  renderAccounts({items: state.accounts || [], truncated: state.accountsTruncated});
  try {
    return await api(path, body);
  } finally {
    state.accountBusy = false;
    swrDrop("accounts");
    await loadAccounts(true).catch(() => {});
    // The list may be unchanged (so swr would skip painting): unlock the buttons.
    renderAccounts({items: state.accounts || [], truncated: state.accountsTruncated});
  }
}

function showAccountResult(item, fallback) {
  const text = item ? item.host + " " + (item.display_username || "") + ": " + accountSummary(item) : fallback;
  $("accountResult").textContent = text;
  const bad = item && ["invalid", "expired", "temporarily_unavailable"].includes(item.status);
  toast(text, bad ? "error" : "success");
}

async function addAccount() {
  if (!accountsSecureNow()) throw new Error(ACCOUNT_SECURE_MESSAGE);
  const host = $("accountHost").value.trim().toLowerCase();
  if (!state.accountHosts.includes(host)) throw new Error("一覧にあるホストを選んでください");
  const body = {host, username: $("accountUser").value, password: $("accountPass").value};
  $("accountPass").value = "";
  $("accountResult").textContent = "追加して、JD2でログイン確認しています…";
  try {
    const result = await accountChange("/api/accounts/add", body);
    $("accountUser").value = "";
    showAccountResult(result.item, result.message || "追加しました");
  } catch (e) {
    $("accountResult").textContent = "";
    throw e;
  } finally {
    body.password = "";
  }
}

function openAccountEdit(item) {
  if (!accountsSecureNow()) throw new Error(ACCOUNT_SECURE_MESSAGE);
  state.accountEditId = item.id;
  $("accountEditTitle").textContent = item.host + " " + (item.display_username || "") + " の認証情報を更新";
  $("accountEditUser").value = "";
  $("accountEditPass").value = "";
  setShown("accountEdit", true);
  $("accountEditUser").focus();
}

async function saveAccountEdit() {
  if (!accountsSecureNow()) throw new Error(ACCOUNT_SECURE_MESSAGE);
  const body = {id: state.accountEditId, username: $("accountEditUser").value,
    password: $("accountEditPass").value};
  $("accountEditPass").value = "";
  try {
    const result = await accountChange("/api/accounts/update", body);
    setShown("accountEdit", false);
    $("accountEditUser").value = "";
    state.accountEditId = "";
    showAccountResult(result.item, "更新しました");
  } finally {
    body.password = "";
  }
}

async function refreshAccount(item) {
  const result = await accountChange("/api/accounts/refresh", {id: item.id});
  showAccountResult(result.item, "再確認しました");
}

async function toggleAccount(item) {
  const wanted = item.enabled === false;
  const result = await accountChange("/api/accounts/enable", {id: item.id, enabled: wanted});
  showAccountResult(result.item, wanted ? "有効にしました" : "無効にしました");
}

async function deleteAccount(item) {
  const typed = prompt("JD2からこのアカウントを削除します（ダウンロード済みのファイルやキューには触れません）。\n\n" +
    item.host + "  " + (item.display_username || "") + "\n\n続けるには、ホスト名「" + item.host + "」を入力してください。");
  if (typed === null) return;
  if (typed.trim().toLowerCase() !== item.host) throw new Error("ホスト名が一致しないため削除しませんでした");
  await accountChange("/api/accounts/delete", {id: item.id, confirm_host: typed.trim()});
  flash(item.host + " のアカウントを削除しました");
}

async function loadAccountSchema() {
  const data = await api("/api/accounts/schema");
  $("accountSchemaOut").textContent = JSON.stringify(data, null, 1);
  setShown("accountSchemaOut", true);
  setShown("accountSchemaCopy", true);
}

function wireAccounts() {
  $("accountSchema").addEventListener("click", () => perform(loadAccountSchema));
  $("accountSchemaCopy").addEventListener("click", () => copyText($("accountSchemaOut").textContent));
  $("accountForm").addEventListener("submit", event => {
    event.preventDefault();
    perform(addAccount);
  });
  $("accountEditForm").addEventListener("submit", event => {
    event.preventDefault();
    perform(saveAccountEdit);
  });
  $("accountEditCancel").addEventListener("click", () => {
    $("accountEditPass").value = "";
    $("accountEditUser").value = "";
    setShown("accountEdit", false);
    state.accountEditId = "";
  });
  $("accountsReload").addEventListener("click", () => perform(() => loadAccounts(true)));
}

// ---- Settings › NASブラウザ (v0.1.16)
const RB_PHASE_LABELS = {stopped: "停止中", starting: "起動中…", running: "稼働中",
  stopping: "停止処理中…", setup: "初期設定中"};

function browserSectionVisible() {
  return state.route.view === "settings" && state.route.section === "browser";
}

function rbPollWanted() {
  const e = state.rbStatus;
  return Boolean(state.authed && browserSectionVisible() && document.visibilityState === "visible" &&
    e && (e.phase === "setup" || e.phase === "starting" || e.phase === "stopping"));
}

function syncBrowserPoll() {
  clearTimeout(state.rbTimer);
  state.rbTimer = null;
  if (!rbPollWanted() || state.rbBusy) return;
  state.rbTimer = setTimeout(browserTick, RB_STATUS_POLL_MS);
}

async function browserTick() {
  state.rbTimer = null;
  if (!rbPollWanted() || state.rbBusy) return;
  state.rbBusy = true;
  try {
    renderBrowserStatus(await api("/api/rb/status"));
  } catch (e) {
    state.rbBusy = false;
    return;
  }
  state.rbBusy = false;
  syncBrowserPoll();
}

function renderRemoteHosts() {
  const box = $("remoteHosts");
  if (!box) return;
  const hosts = remoteHosts();
  if (!hosts.length) {
    box.replaceChildren(element("p", "設定されたサイトはありません。Webタブの表示がうまくいかないとき「NASブラウザで開く」を選ぶと追加されます。", "empty-state"));
    return;
  }
  const nodes = [];
  for (const host of hosts) {
    const row = element("div", undefined, "item");
    const shown = DISPLAY_OF_MODE[rememberedMode(host)];
    row.appendChild(element("span", host + "（" + (shown ? RB_DISPLAY_LABELS[shown] : "既定の表示") + "）"));
    const off = element("button", "解除", "btn small");
    off.type = "button";
    off.addEventListener("click", () => { rememberHostMode(host, "strict"); renderRemoteHosts(); });
    row.appendChild(off);
    nodes.push(row);
  }
  box.replaceChildren(...nodes);
}

// Settings › NASブラウザ: per-site rules (top-level page host of the NAS browser tab).
const POPUP_LABELS = {ask: "確認する", allow: "許可（新しいタブ）", block: "ブロック"};

function ruleToggle(label, checked, onChange) {
  const wrap = element("label", undefined, "rule-toggle");
  const input = document.createElement("input");
  input.type = "checkbox";
  input.checked = checked;
  input.addEventListener("change", () => perform(async () => {
    try { await onChange(input.checked); } catch (e) { input.checked = !input.checked; throw e; }
  }));
  wrap.appendChild(input);
  wrap.appendChild(document.createTextNode(" " + label));
  return wrap;
}

function renderSiteRules(data) {
  state.siteRules = data;
  const box = $("siteRules");
  if (!box) return;
  const items = Array.isArray(data.items) ? data.items : [];
  if (!items.length) {
    box.replaceChildren(element("p", "サイトごとの設定はありません。リンク検出シートの「このサイトでは今後自動で送る」やポップアップの「常に許可／常にブロック」で追加されます。", "empty-state"));
    return;
  }
  box.replaceChildren(...items.map(rule => {
    const row = element("div", undefined, "item rule-item");
    row.appendChild(element("span", rule.host, "rule-host"));
    const controls = element("span", undefined, "rule-controls");
    controls.appendChild(ruleToggle("自動送信", rule.auto_send === true,
      checked => setSiteRule(rule.host, {auto_send: checked})));
    const select = document.createElement("select");
    select.setAttribute("aria-label", rule.host + " のポップアップ");
    for (const [value, label] of Object.entries(POPUP_LABELS)) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = "ポップアップ: " + label;
      select.appendChild(option);
    }
    select.value = rule.popup;
    select.addEventListener("change", () => perform(() => setSiteRule(rule.host, {popup: select.value})));
    controls.appendChild(select);
    controls.appendChild(ruleToggle("広告ブロック", rule.block_ads !== false,
      checked => setSiteRule(rule.host, {block_ads: checked})));
    const del = element("button", "削除", "btn small quiet");
    del.type = "button";
    del.addEventListener("click", () => perform(async () => {
      await api("/api/rb/site-rules/delete", {host: rule.host});
      swrDrop("site-rules");
      renderSiteRules(await api("/api/rb/site-rules"));
    }));
    controls.appendChild(del);
    row.appendChild(controls);
    return row;
  }));
}

async function saveOption(key, value) {
  const saved = await api("/api/settings/options", {[key]: value});
  swrDrop("settings", "status");
  state.settings = saved;
  renderHome();
  syncRemoteSettings();
  return saved;
}

function renderBrowserStatus(r) {
  state.rbStatus = r;
  const cs = r.container_station || {};
  const setup = r.phase === "setup";
  $("rbState").textContent = r.update_needed && !setup ? "更新が必要" : !r.image_ready && !setup ? "未設定" : RB_PHASE_LABELS[r.phase] || r.phase;
  $("rbState").classList.toggle("is-ok", r.phase === "running");
  setShown("rbNoContainer", cs.available === false);
  $("rbSetup").hidden = setup || cs.available === false || r.phase !== "stopped";
  $("rbSetup").textContent = r.image_ready ? "イメージを再作成" :
    r.update_needed ? "初期設定を再実行" : "初期設定（イメージ作成）";
  $("rbStop").hidden = !(r.phase === "running" || r.phase === "starting");
  const parts = [];
  if (r.memory) parts.push("ブラウザのメモリ " + r.memory.used_mib + " / " + r.memory.limit_mib + " MiB（" + r.memory.percent + "%）");
  if (typeof r.available_mib === "number") parts.push("NAS空きメモリ " + r.available_mib + " MiB");
  parts.push("タブ " + (r.tabs || []).length + " / " + r.max_tabs);
  $("rbMemory").textContent = parts.join(" · ");
  const reason = (r.image_ready || r.update_needed) && r.reason ? r.reason : "";
  $("rbReason").hidden = !reason;
  $("rbReason").textContent = reason ? (r.update_needed ? reason : "いまは開けません: " + reason) : "";
  $("rbError").hidden = !r.error;
  $("rbError").textContent = r.error || "";
  const steps = r.steps || [];
  const active = steps.some(x => x.status !== "pending");
  setShown("rbStepsBox", active);
  if (setup || r.error) $("rbStepsBox").open = true;
  $("rbSteps").replaceChildren(...steps.map(step => {
    const li = element("li", undefined, "engine-step is-" + step.status);
    li.appendChild(element("span", STEP_MARKS[step.status] || "?", "step-mark"));
    const body = element("span", undefined, "step-body");
    body.appendChild(element("b", step.label));
    body.appendChild(element("span", " — " + (STEP_TEXT[step.status] || step.status) +
      (step.detail ? "：" + step.detail : ""), "step-detail"));
    li.appendChild(body);
    return li;
  }));
  syncBrowserPoll();
}

async function loadBrowserLog() {
  const data = await api("/api/rb/log");
  $("rbLog").textContent = (data.lines || []).join("\n") || "まだログはありません";
}

async function loadBrowserStatus(force = false) {
  await swr("rb-status", "/api/rb/status", renderBrowserStatus, {force});
}

function wireBrowser() {
  $("blockAds").addEventListener("change", () => perform(async () => {
    try {
      await saveOption("block_ads", $("blockAds").checked);
      flash($("blockAds").checked ? "広告をブロックします（次に開くタブから）" : "広告ブロックをやめました（次に開くタブから）");
    } catch (e) { $("blockAds").checked = !$("blockAds").checked; throw e; }
  }));
  document.querySelectorAll('input[name="rbDefaultMode"]').forEach(radio => {
    radio.addEventListener("change", () => perform(async () => {
      if (!radio.checked) return;
      try {
        await saveOption("rb_default_mode", radio.value);
        flash("既定の表示方法: " + RB_DISPLAY_LABELS[DISPLAY_OF_SETTING[radio.value]]);
      } catch (e) {
        renderDownloadDir(state.settings || {});
        throw e;
      }
    }));
  });
  $("rbLinkBoxes").addEventListener("change", () => perform(async () => {
    try {
      await saveOption("rb_link_boxes", $("rbLinkBoxes").checked);
      flash($("rbLinkBoxes").checked ? "リンクの枠を表示します" : "リンクの枠を隠しました");
    } catch (e) { $("rbLinkBoxes").checked = !$("rbLinkBoxes").checked; throw e; }
  }));
  $("probeAutosend").addEventListener("change", () => perform(async () => {
    try {
      await saveOption("probe_autosend", $("probeAutosend").checked);
      flash($("probeAutosend").checked ? "長押しで見つかったリンクを自動でJD2へ送ります" : "長押しの結果はシートで確認します");
    } catch (e) { $("probeAutosend").checked = !$("probeAutosend").checked; throw e; }
  }));
  $("sendAutostart").addEventListener("change", () => perform(async () => {
    try {
      await saveOption("send_autostart", $("sendAutostart").checked);
      flash($("sendAutostart").checked ? "JD2へ送ったらすぐダウンロードを開始します" : "JD2へ送るだけにします（開始は手動）");
    } catch (e) { $("sendAutostart").checked = !$("sendAutostart").checked; throw e; }
  }));
  $("rbSetup").addEventListener("click", () => perform(async () => {
    if (!confirm("NASブラウザの初期設定を始めます。\n\n" +
        "・Container Station上で、同梱のDockerfileからイメージ qnapdl-browser を作ります（Debian bookworm-slim をダイジェスト固定で取得し、Chromium・Xvfb・日本語フォントをaptで導入。数分かかり、約1GBのディスクを使います）\n" +
        "・Chromiumのサンドボックスは有効のままです。このNASのカーネルがユーザー名前空間を許可しない場合は、設定を中止します（サンドボックスを無効にはしません）\n" +
        "・ポートは公開せず、非root・読み取り専用・メモリ上限2GBで動かします。タブを開いたときだけ起動し、使わなければ自動で停止します\n\nよろしいですか？")) return;
    await api("/api/rb/setup", {confirmed: true});
    swrDrop("rb-status");
    flash("NASブラウザの初期設定を開始しました");
    renderBrowserStatus(await api("/api/rb/status"));
  }));
  $("rbStop").addEventListener("click", () => perform(async () => {
    if (!confirm("NASブラウザを停止します。開いているNASブラウザのタブはすべて閉じられます。よろしいですか？")) return;
    $("rbStop").disabled = true;
    try {
      await api("/api/rb/stop", {confirmed: true});
    } finally {
      $("rbStop").disabled = false;
    }
    for (const tab of state.tabs) { tab.rb = null; }
    state.rbPolling = ""; state.rbGen++;
    swrDrop("rb-status");
    renderBrowserStatus(await api("/api/rb/status"));
    updateChrome();
    flash("NASブラウザを停止しました");
  }));
  document.addEventListener("visibilitychange", syncBrowserPoll);
}

function wireEngine() {
  $("engineSetup").addEventListener("click", () => perform(() => startEngineSetup()));
  $("engineResetup").addEventListener("click", () => perform(() => startEngineSetup()));
  $("homeEngineSetup").addEventListener("click", () => perform(() => startEngineSetup()));
  $("engineSetupNative").addEventListener("click", () => perform(() => startEngineSetup("native")));
  $("engineMigrate").addEventListener("click", () => perform(migrateEngine));
  $("engineNativeRemove").addEventListener("click", () => perform(async () => {
    if (!confirm("QTS直接実行用のJava（engine/java）とダウンロードの残骸（engine/.download）を削除します。\n\n" +
        "・JD2の設定・キュー・ダウンロード途中のファイル（engine/jd2）は残ります。コンテナ方式のJD2はそのまま使えます\n" +
        "・削除後はQTS直接実行には戻せなくなります（戻したいときは、もう一度初期設定を実行すればJavaを取得し直せます）\n\nよろしいですか？")) return;
    const done = await api("/api/engine/native/remove", {confirmed: true});
    swrDrop("engine", "status");
    flash("従来方式のJavaを削除しました（" + Math.round((done.freed_bytes || 0) / 1048576) + " MB）");
    applyEngine(await api("/api/engine/status"));
  }));
  $("engineStart").addEventListener("click", () => perform(startEngine));
  $("homeEngineStart").addEventListener("click", () => perform(startEngine));
  $("downloadsEngineAction").addEventListener("click", () => {
    if ($("downloadsEngineAction").dataset.action === "setup") navigate("#/settings/engine");
    else perform(startEngine);
  });
  $("engineStop").addEventListener("click", () => perform(async () => {
    if (!confirm("JD2エンジンを停止します。実行中のダウンロードは中断されます（最大30秒かかります）。よろしいですか？")) return;
    $("engineStop").disabled = true;
    try {
      await api("/api/engine/stop", {confirmed: true});
    } finally {
      $("engineStop").disabled = false;
    }
    swrDrop("engine", "status", "jd2-status");
    applyEngine(await api("/api/engine/status"));
    flash("エンジンを停止しました");
  }));
  $("engineAutostart").addEventListener("change", () => perform(async () => {
    const wanted = $("engineAutostart").checked;
    try {
      await api("/api/engine/autostart", {enabled: wanted});
    } catch (e) {
      $("engineAutostart").checked = !wanted;
      throw e;
    }
    swrDrop("engine");
    flash(wanted ? "Hub起動時にエンジンも起動します" : "自動起動をOFFにしました");
  }));
  $("engineKeys").addEventListener("click", () => perform(loadEngineKeys));
  $("engineKeysCopy").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText($("engineKeysOut").textContent);
      flash("コピーしました");
    } catch (e) { flash("コピーできませんでした"); }
  });
  $("engineLogBox").addEventListener("toggle", () => {
    if ($("engineLogBox").open) perform(loadEngineLog);
  });
  $("rbLogBox").addEventListener("toggle", () => {
    if ($("rbLogBox").open) perform(loadBrowserLog);
  });
  document.addEventListener("visibilitychange", syncEnginePoll);
}

function wireSettings() {
  $("folderChange").addEventListener("click", () => perform(async () => {
    setPicker(true);
    await folders("", true);
  }));
  $("folderCancel").addEventListener("click", () => setPicker(false));
  $("bookmarkForm").addEventListener("submit", event => {
    event.preventDefault();
    perform(async () => {
      await addBookmark($("bookmarkName").value, $("bookmarkUrl").value.trim());
      $("bookmarkName").value = "";
      $("bookmarkUrl").value = "";
    });
  });
  $("diagnosticIo").addEventListener("click", () => perform(async () => {
    const io = (await api("/api/diagnostics/io")).process_io;
    $("diagnosticResult").textContent = io.available ?
      "Hub起動後の物理読込: " + readableSize(io.read_bytes) +
      " / 書込: " + readableSize(io.write_bytes) +
      " / 書込取消: " + readableSize(io.cancelled_write_bytes) +
      "（累積値。JD2や他アプリのI/Oは含みません）" :
      "この環境では/proc/self/ioを読み取れません。";
  }));
  $("diagnosticJd2").addEventListener("click", () => perform(async () => {
    const data = await api("/api/jd2/local-preflight");
    $("diagnosticResult").textContent = (data.tcp_reachable ?
      "127.0.0.1:3128にTCP接続できました。" : "127.0.0.1:3128に接続できません。") +
      "接続できてもJD2の認証や外部からの隔離は確認できません。";
  }));
  wireEngine();
  wireAccounts();
  $("httpsCmdCopy").addEventListener("click", () => copyText(HTTPS_SERVE_COMMAND));
  $("shareForm").addEventListener("submit", event => {
    event.preventDefault();
    perform(addShareDevice);
  });
  $("shareTokenCopy").addEventListener("click", () => copyText($("shareToken").textContent));
  $("shareEndpointCopy").addEventListener("click", () => copyText(shareEndpoint()));
  $("shareJsCopy").addEventListener("click", () => copyText(SHARE_JS));
  $("shareBookmarkletCopy").addEventListener("click", () => copyText(shareBookmarklet()));
  $("updateLogBox").addEventListener("toggle", () => {
    if (!$("updateLogBox").open) return;
    perform(async () => {
      const result = await api("/api/update/log");
      $("updateLog").textContent = result.log || "まだ更新ログはありません";
    });
  });
  $("updateChannel").addEventListener("change", () => {
    state.selectedVersion = "";
    state.selectedChannel = $("updateChannel").value;
    $("updateResult").textContent = "";
    syncUpdateApply();
  });
  $("updateCheck").addEventListener("click", () => perform(checkUpdate));
  $("jd2Check").addEventListener("click", () => perform(async () => {
    const result = await api("/api/jd2/update/check", {});
    swrDrop("jd2-status", "status");
    await loadJd2Status(true);
    flash(result.update_available ? "JD2の更新があります" : "JD2は最新です");
  }));
  $("jd2Update").addEventListener("click", () => perform(async () => {
    if (!confirm("JDownloader 2本体だけを更新します。JD2のダウンロードは一時中断される場合があります。よろしいですか？")) return;
    await api("/api/jd2/update/apply", {confirmed: true});
    swrDrop("jd2-status", "status");
    await loadJd2Status(true);
  }));
  $("jd2Preset").addEventListener("click", () => perform(applyPreset));
  $("updateApply").addEventListener("click", () => perform(async () => {
    if (!state.selectedVersion || !confirm(state.selectedVersion + " にQPKGを更新します。サービスが再起動されます。続けますか？")) return;
    const before = await serverVersion();
    await api("/api/update/apply", {version: state.selectedVersion, channel: state.selectedChannel});
    watchForUpdate(before);   // not awaited: polls until the new version answers
    try { await updateStatus(true); } catch (e) { /* the service may already be restarting */ }
  }));
}

// tailnet: server-side verdict from the TCP peer address (MagicDNS short names
// are invisible to hostname checks). WireGuard already encrypts the tunnel.
function updateLanWarning(tailnet, https) {
  const plainLan = location.protocol === "http:" && https !== true &&
    !["localhost", "127.0.0.1", "::1", "[::1]"].includes(location.hostname) &&
    !isTailscaleHost(location.hostname) && tailnet !== true;
  setShown("lanSecurityWarning", plainLan);
}

document.addEventListener("DOMContentLoaded", () => {
  updateLanWarning(false, false);
  wireAuth();
  wireShell();
  wireDownloads();
  wireLinks();
  wireSettings();
  wireRemote();
  wireBrowser();
  paintShellEarly();
  fetchHealthVersion().then(v => { if (v && !state.bootVersion) state.bootVersion = v; });
  go();
});
