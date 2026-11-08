"use strict";
/*
 * QnapDownloader browser-style shell (v0.1.35).
 * Spec: docs/browser-shell-ui.md. Single file, no build step.
 *
 * Security notes (see "Security invariants" in the spec):
 *  - DOM is built only with createElement/createElementNS + textContent.
 *  - v0.1.57: the one exception is the passthrough frame (#ptFrame): a sandboxed iframe on a separate origin that the
 *    app only talks to by origin-checked postMessage (see "passthrough display" below).
 *  - There are no iframes: since v0.1.35 every http(s) web tab is shown by the NAS browser
 *    (Chromium in a NAS container); this page only displays JPEG frames / reader data the Hub
 *    produced. The URL is validated by /api/browser/validate before it is handed to the Hub.
 *  - Storage keeps only {id, url history, index, NAS tab id} and the last route (session), one boolean
 *    preference and hostname->display pairs (local). Never tokens, passwords or cookies.
 */

// ---------------------------------------------------------------- constants
const MAX_WEB_TABS = 6;            // v0.1.65: the least web tabs the app allows; the setting rb_max_tabs (1-20) can raise it
const SWR_TTL_MS = 10000;
const MAX_HOST_MODES = 100;
const UPDATE_POLL_MS = 2000;
const STATE_WAIT_S = 25;           // /api/state long-poll
const LOCAL_WRITE_QUIET_MS = 3000; // a revision change this soon after our own write is ignored
const TOAST_MS = 6000;
const UPDATE_WATCH_MS = 5 * 60 * 1000;
const MAX_CLOSED_TABS = 10;
const MAX_HISTORY = 50;
const AUTO_REFRESH_MS = 5000;     // downloads view: local data, refreshed while visible
const PAGE_SIZE = 30;
const WAITING_MAX = 50;
// NAS remote browser (v0.1.16): the page is rendered by Chromium in a NAS container; this
// page only shows JPEG frames from /api/rb/frame.jpg (binary, shown through a
// blob: object URL) and posts validated input.
// Per-host map (localStorage): only an explicit per-site display choice is stored (v0.1.30);
// without an entry the display chosen in Settings (rb_default_mode) applies.
const REMOTE_FAST_MODE = "remote-fast";      // fast full-page display (JPEG tiles)
const REMOTE_LIVE_MODE = "remote-live";      // live screencast (Cloudflare checks, logins)
const REMOTE_READER_MODE = "remote-reader";  // reader: text rebuilt from validated data
const REMOTE_MIRROR_MODE = "remote-mirror";  // mirror: sanitized HTML + CSS copy in a script-less sandboxed iframe
const REMOTE_MODES = [REMOTE_FAST_MODE, REMOTE_LIVE_MODE, REMOTE_READER_MODE, REMOTE_MIRROR_MODE];
const DISPLAY_OF_MODE = {[REMOTE_FAST_MODE]: "tiles", [REMOTE_LIVE_MODE]: "live", [REMOTE_READER_MODE]: "reader",
  [REMOTE_MIRROR_MODE]: "mirror"};
const MODE_OF_DISPLAY = {tiles: REMOTE_FAST_MODE, live: REMOTE_LIVE_MODE, reader: REMOTE_READER_MODE,
  mirror: REMOTE_MIRROR_MODE};
const DISPLAY_OF_SETTING = {reader: "reader", mirror: "mirror", fast: "tiles", live: "live"};
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
const RB_DISPLAY_LABELS = {reader: "リーダー", mirror: "ミラー", tiles: "高速（全体表示）", live: "ライブ"};
// v0.1.60: a short name of the display inside the omnibox (the chip opens the display chooser).
const DISPLAY_CHIP = {reader: "リーダー", mirror: "ミラー", tiles: "高速", live: "ライブ", pt: "パス"};
const DISPLAY_CHIP_TITLE = {reader: "リーダー", mirror: "ミラー", tiles: "高速", live: "ライブ", pt: "パススルー"};
// v0.1.60: this file's own version. The Hub's version (from /api/state) is compared with it: a shell cache that is
// older than the Hub (the app was updated while the home-screen app was closed) is dropped and the page reloaded.
// tests/test_web_shell.py checks that it equals webapp.VERSION.
const APP_VERSION = "0.1.73";
const PT_DISPLAY_LABEL = "パススルー";      // v0.1.57: not a NAS-browser display, so not in RB_DISPLAY_LABELS
const RB_PERF_WINDOW_MS = 5000;
const RB_STREAM_FIRST_MS = 5000;     // MJPEG as the last stream (v0.1.68: after WebSocket and SSE): first picture within this, else polling
const RB_MJPEG_FIRST_MS = 2500;      // v0.1.46: the self-test's MJPEG first-picture limit
const RB_SSE_FIRST_MS = 3000;        // v0.1.46: SSE first frame within this, else the next transport (MJPEG)
const RB_WS_FIRST_MS = 2500;         // v0.1.68: WebSocket first frame within this, else SSE
const RB_WS_ACK_MS = 25000;          // v0.1.68: the phone sends a tiny "ack" text this often (keeps proxies from idling the socket)
const RB_LIVE_PREF_KEY = "qnapdl.livetransport.v1";
const RB_LIVE_RETRY_MS = 24 * 60 * 60 * 1000;   // a transport that failed on this device is retried once a day (v0.1.68: per transport)
const RB_STREAM_ERR_WINDOW_MS = 10000;
const RB_STREAM_META_S = 2;
const RB_STREAM_RECONNECT_MS = 9 * 60 * 1000;
const RB_STREAM_STALL_MS = 6000;
const RB_POPUP_NEW_TAB = "new";
const RB_KEYS = ["Enter", "Backspace", "Tab", "Escape", "Delete",
  "ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"];
const SHELL_KEY = "qnapdl.shell.v1";
const HOST_MODE_KEY = "qnapdl.hostmodes.v1";
const RECENT_KEY = "qnapdl.recent.v1";        // v0.1.62: recent pages (this device only, never sent anywhere)
const RECENT_MAX = 20;
const REMOTE_PT_MODE = "remote-pt";           // v0.1.62: passthrough remembered for a site (stored only; not a NAS display)
const TAB_STORAGE_KEY = "qnapdl.webtabs.v1";
const LEGACY_AUTO_KEY = "qnapdl.downloads.auto";   // v0.1.11 switch; removed silently
// v0.1.33: four sections, each with in-page subsections (#/settings/jd2/accounts scrolls to one).
// v0.1.35 debug log (server ring, RAM only). The page batches entries while the server says it is on.
const DEBUG_CATEGORIES = ["ui", "render", "net", "rb", "jd2", "engine", "disk", "http", "app", "pt"];
const DEBUG_CATEGORY_LABELS = {ui: "画面", render: "描画", net: "通信", rb: "NASブラウザ", jd2: "JD2",
  engine: "エンジン", disk: "ディスク", http: "HTTP", app: "アプリ"};
const DEBUG_LEVELS = ["debug", "info", "warn", "error"];
const DEBUG_LEVEL_LABELS = {debug: "詳細", info: "情報", warn: "警告", error: "エラー"};
const DEBUG_FLUSH_MS = 2000;       // at most one POST per this long
const DEBUG_BATCH = 100;           // entries per POST (the server also enforces this)
const DEBUG_QUEUE_MAX = 300;       // entries waiting for the next POST; the oldest are dropped
const DEBUG_RATE_MS = 1000;        // the same key is logged at most this often
const DEBUG_VIEW_MS = 2000;        // the log sheet follows the server this often
const DEBUG_VIEW_MAX = 1000;
const DISK_REFRESH_MS = 10000;     // disk write table (the Hub samples every 10 s)
const SETTINGS_SECTIONS = ["jd2", "browser", "connection", "system", "diag"];
const SETTINGS_SUBS = {jd2: ["engine", "accounts", "storage", "send"], browser: ["nas", "pt", "rules", "bookmarks"],
  connection: ["https", "share"], system: ["updates", "login", "about"], diag: ["selftest", "tools", "disk", "debug"]};
// Old one-section routes land on their new place.
const SETTINGS_REDIRECTS = {engine: "jd2/engine", accounts: "jd2/accounts", storage: "jd2/storage",
  https: "connection/https", share: "connection/share", bookmarks: "browser/bookmarks",
  updates: "system/updates", diagnostics: "diag/tools", about: "system/about"};
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
  bookmarks: [], status: null, omniDisplay: "", omniShort: "",
  snapshot: null, snapshotAt: 0, downloadsPage: 0, downloadsBusy: false,
  filter: "all", query: "", downloadsFails: 0,
  dlRunning: null, expanded: new Set(),
  live: null, stateRev: null, stateGen: 0, stateRunning: false, stateAbort: null, stateFails: 0,
  settingsRev: null, engineSig: "", rbSig: "", localWriteAt: 0, dlKey: null, lastStage: null,
  settings: null, jd2: null, queueData: null, pasteSeq: 0, pasteTimer: null, paste: null,
  pickerOpen: false, stepsMode: "",
  selectedVersion: "", selectedChannel: "rc",
  menuReturnFocus: null,
  snapshotJson: "", downloadsSig: "", folderPath: "",
  bootVersion: "", updateWatching: false,
  engine: null,
  https: false, tailnet: false,
  rbPolling: "", rbPollMode: "", rbGen: 0, rbStatus: null, rbReady: null,
  rbRestarts: null, rbLost: null, rbCrashAt: 0,
  debug: {enabled: false, level: "debug", categories: [], until: 0, endsAt: 0},
  rbIntercepts: [],
  rbQuality: loadRbQuality(), rbStream: null, rbPerf: false, rbPerfSamples: [],
  rbPerfSince: 0, probeResult: null, probeResults: [], siteRules: null, waiting: null,
  accounts: null, accountsTruncated: false, accountHosts: [], accountBusy: false, accountEditId: "",
  pt: null,   // v0.1.57: /api/pt/status
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

// v0.1.62: one shape for "nothing here": what is empty, then (muted) what to do about it.
function emptyState(message, hint) {
  const node = element("p", message, "empty-state");
  if (hint) { node.appendChild(document.createElement("br")); node.appendChild(element("span", hint, "empty-hint")); }
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

// Remembered display per hostname: [[host, mode], ...] only, newest last, capped. v0.1.35: the
// iframe modes (strict / interactive / compat), the old "external" mode and the plain "remote"
// entry no longer exist; such stored entries are dropped so the default display applies.
function readHostModes() {
  try {
    const data = JSON.parse(readLocal(HOST_MODE_KEY) || "[]");
    if (!Array.isArray(data)) return [];
    return data.filter(e => Array.isArray(e) && typeof e[0] === "string" && e[0].length <= 253 &&
      (REMOTE_MODES.includes(e[1]) || e[1] === REMOTE_PT_MODE)).map(e => [e[0], e[1]]).slice(-MAX_HOST_MODES);
  } catch (e) { return []; }
}

// One-time silent rewrite of the stored map (readHostModes already ignores the old values).
function migrateHostModes() {
  try {
    const raw = readLocal(HOST_MODE_KEY);
    if (raw === null) return;
    const clean = JSON.stringify(readHostModes());
    if (clean !== raw) writeLocal(HOST_MODE_KEY, clean);
  } catch (e) { /* ignore: reads still skip the old values */ }
}

// v0.1.62: the display is remembered per SITE (registrable domain: www.example.com, a.example.com and example.com
// share one entry). There is no public-suffix list here: two-letter country codes with a generic second level
// (co.jp, or.jp, com.au, ...) keep three labels, everything else keeps two. Old exact-host entries still match.
const SLD_GENERIC = new Set(["co", "com", "net", "org", "ne", "or", "ac", "go", "gr", "ed", "gov", "edu", "ad", "lg"]);
function siteKey(hostOrUrl) {
  let host = String(hostOrUrl || "").trim().toLowerCase();
  if (/^[a-z][a-z0-9+.-]*:\/\//.test(host)) { try { host = new URL(host).hostname; } catch (e) { /* keep as is */ } }
  host = host.replace(/^www\./, "").replace(/\.$/, "");
  if (!host || host.includes(":") || /^[\d.]+$/.test(host)) return host;
  const labels = host.split(".");
  if (labels.length <= 2) return host;
  const tld = labels[labels.length - 1], second = labels[labels.length - 2];
  if (tld.length === 2 && SLD_GENERIC.has(second)) return labels.slice(-3).join(".");
  return labels.slice(-2).join(".");
}

function rememberHostMode(host, mode) {
  if (!host) return;
  const key = siteKey(host);
  const list = readHostModes().filter(e => siteKey(e[0]) !== key);
  if (REMOTE_MODES.includes(mode) || mode === REMOTE_PT_MODE) list.push([key, mode]);   // no entry = the default display
  writeLocal(HOST_MODE_KEY, JSON.stringify(list.slice(-MAX_HOST_MODES)));
}

function rememberedMode(host) {
  if (!host) return null;
  const all = readHostModes();
  const hit = all.find(e => e[0] === host) || all.find(e => siteKey(e[0]) === siteKey(host));
  return hit ? hit[1] : null;
}

// v0.1.62: does the site of this address have a remembered display (any, including passthrough)?
function hasSiteDisplay(url) {
  return Boolean(rememberedMode(hostOf(url)));
}

function displayHosts() {
  return readHostModes().map(e => e[0]);
}

migrateHostModes();

// ------------------------------------------------- app progress line (v0.1.62)
// A data load of an app screen that is still running after 300 ms shows the thin line at the top of the content (the
// same look as the web tab's line). Fast loads never show it, and nothing is held back for it: it only reports.
const LOAD_LINE_DELAY_MS = 300;
const appLoad = {n: 0, timer: 0};
function trackLoad(promise) {
  appLoad.n++;
  if (appLoad.n === 1) {
    clearTimeout(appLoad.timer);
    appLoad.timer = setTimeout(() => {
      if (appLoad.n > 0 && state.route.view !== "tab") { const bar = document.getElementById("appLoading"); if (bar) bar.hidden = false; }
    }, LOAD_LINE_DELAY_MS);
  }
  const done = () => {
    appLoad.n = Math.max(0, appLoad.n - 1);
    if (appLoad.n === 0) {
      clearTimeout(appLoad.timer);
      const bar = document.getElementById("appLoading");
      if (bar) bar.hidden = true;
    }
  };
  promise.then(done, done);
  return promise;
}

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
    trackLoad(pending);
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
// v0.1.59: HTTP requests to /api/ that the app made (long-polls of the state and background reports excluded):
// the first-load line shows how many were needed between the tap and the first paint.
const RTT_PASSIVE = /^\/api\/(state|debug\/log|rb\/firstload|rb\/mirror\/res|rb\/mirror\/release|rb\/snapshot\/release|selftest\/)/;
let rttCount = 0;
const reqStats = {state: 0, all: 0};          // v0.1.59: idle-traffic probe of the self-test
if (typeof window.fetch === "function") {
  const nativeFetch = window.fetch.bind(window);
  window.fetch = function (input, init) {
    try {
      const path = new URL(typeof input === "string" ? input : input.url, location.href).pathname;
      if (path.startsWith("/api/")) {
        if (!path.startsWith("/api/selftest/") && path !== "/api/debug/log") reqStats.all++;
        if (path === "/api/state") reqStats.state++;
        else if (!RTT_PASSIVE.test(path)) rttCount++;
      }
    } catch (e) { /* counting only */ }
    return nativeFetch(input, init);
  };
}

async function api(path, body = null, extra = null) {
  const headers = {};
  const opts = Object.assign({credentials: "same-origin", headers}, extra || {});
  if (body !== null) {
    opts.method = "POST";
    headers["Content-Type"] = "application/json";
    headers["X-CSRF-Token"] = state.csrf;
    opts.body = JSON.stringify(body);
    if (!path.startsWith("/api/rb/")) state.localWriteAt = Date.now();
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

// ------------------------------------------------------------------ debug log
// dlog(category, level, message, data): does nothing unless the Hub says the debug log is on.
// Entries are queued and POSTed to /api/debug/log at most every DEBUG_FLUSH_MS (<= DEBUG_BATCH each);
// the Hub applies its own size limits and redaction. Nothing is stored on this device.
const dbgQueue = [];
const dbgGate = new Map();
let dbgTimer = 0;

function dlogWanted(category, level) {
  const d = state.debug;
  if (!d.enabled || !DEBUG_CATEGORIES.includes(category)) return false;
  if (d.categories.length && !d.categories.includes(category)) return false;
  return DEBUG_LEVELS.indexOf(level) >= Math.max(0, DEBUG_LEVELS.indexOf(d.level));
}

function dlog(category, level, message, data) {
  if (!dlogWanted(category, level)) return;
  dbgQueue.push({category, level, message: String(message).slice(0, 500), data: data === undefined ? null : data});
  while (dbgQueue.length > DEBUG_QUEUE_MAX) dbgQueue.shift();
  if (!dbgTimer) dbgTimer = setTimeout(flushDebug, DEBUG_FLUSH_MS);
}

// Rate-limited dlog: the same key is logged at most once per `every` ms.
function dlogRate(key, category, level, message, data, every = DEBUG_RATE_MS) {
  if (!state.debug.enabled) return;
  const now = Date.now();
  if (now - (dbgGate.get(key) || 0) < every) return;
  if (dbgGate.size > 200) dbgGate.clear();
  dbgGate.set(key, now);
  dlog(category, level, message, data);
}

async function flushDebug() {
  dbgTimer = 0;
  if (!state.authed || !state.debug.enabled || !dbgQueue.length) { dbgQueue.length = 0; return; }
  const entries = dbgQueue.splice(0, DEBUG_BATCH);
  try {
    await fetch("/api/debug/log", {method: "POST", credentials: "same-origin",
      headers: {"Content-Type": "application/json", "X-CSRF-Token": state.csrf},
      body: JSON.stringify({entries})});
  } catch (e) { /* the log is best effort */ }
  if (dbgQueue.length && !dbgTimer) dbgTimer = setTimeout(flushDebug, DEBUG_FLUSH_MS);
}

function setDebugState(d) {
  const next = d && typeof d === "object" ? d : {};
  const was = state.debug.enabled;
  state.debug = {
    enabled: next.enabled === true,
    level: DEBUG_LEVELS.includes(next.level) ? next.level : "debug",
    categories: Array.isArray(next.categories) ? next.categories.filter(c => DEBUG_CATEGORIES.includes(c)) : [],
    until: Number.isFinite(next.until) ? next.until : 0,
    endsAt: next.enabled === true && Number.isFinite(next.remaining) ? Date.now() + next.remaining * 1000 : 0,
  };
  if (!state.debug.enabled) { dbgQueue.length = 0; clearTimeout(dbgTimer); dbgTimer = 0; }
  if (state.debug.enabled !== was) {
    if (state.debug.enabled) dlog("app", "info", "client debug on");
    renderDebugCard();
    sched.kick();
  }
}

window.addEventListener("error", event => {
  const where = String(event.filename || "").split("/").pop();
  dlogRate("err:" + event.message, "app", "error", "JS error: " + event.message,
    {at: where + ":" + event.lineno}, 5000);
});
window.addEventListener("unhandledrejection", event => {
  const reason = event.reason && event.reason.message ? event.reason.message : String(event.reason);
  dlogRate("rej:" + reason, "app", "error", "unhandled rejection: " + reason, null, 5000);
});

// ------------------------------------------------------------------- toasts
// One transient toast at a time (v0.1.33). Toasts of the same group and kind merge into one with
// a running count ("JD2へ送信しました（3件）"). An error stays until it is dismissed and replaces an
// older error; a success toast never hides a visible error. Sticky banners (update progress, stale
// page) are separate and keep their own key.
function planToast(cur, next) {
  if (cur && next.group && cur.group === next.group && cur.kind === next.kind) {
    const count = (cur.count || 1) + (next.count || 1);
    return Object.assign({}, next, {count, message: next.format ? next.format(count) : next.message});
  }
  return next;
}

const toastSlot = {cur: null, node: null, timer: 0};

function clearToast() {
  clearTimeout(toastSlot.timer);
  if (toastSlot.node) toastSlot.node.remove();
  toastSlot.cur = null; toastSlot.node = null; toastSlot.timer = 0;
}

function toast(message, kind = "success", opts = {}) {
  dlogRate("toast:" + message, "ui", kind === "error" ? "warn" : "info", "toast", {kind, text: String(message).slice(0, 200)});
  if (kind !== "error" && toastSlot.cur && toastSlot.cur.kind === "error") return;
  const next = planToast(toastSlot.cur, {kind, message, group: opts.group || "", count: opts.count || 1,
    format: opts.format || null});
  clearToast();
  const node = element("div", undefined, "toast " + kind);
  node.appendChild(element("span", next.message, "toast-text"));
  if (kind === "error") {
    const close = element("button", undefined, "toast-close");
    close.type = "button";
    close.setAttribute("aria-label", "通知を閉じる");
    close.title = "閉じる";
    close.appendChild(icon("x"));
    close.addEventListener("click", clearToast);
    node.appendChild(close);
  } else {
    toastSlot.timer = setTimeout(clearToast, TOAST_MS);
  }
  toastSlot.cur = next; toastSlot.node = node;
  $("notice").appendChild(node);
}
function flash(message, opts) { toast(message, "success", opts); }
// "N件を..." style messages that add up while they keep coming.
function flashCount(group, count, format) { toast(format(count), "success", {group, count, format}); }

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
  if (head === "settings") return settingsRoute(parts[1], parts[2]);
  if (head === "tab" && /^t\d+$/.test(parts[1] || "")) return {view: "tab", tabId: parts[1]};
  return {view: "home"};
}

function defaultSection() { return "jd2"; }

// #/settings/<section>[/<sub>]; an old one-section route carries `redirect` (its new hash).
function settingsRoute(first, second) {
  if (first === "system" && second === "diagnostics") {     // v0.1.40: 診断 is its own section
    return {view: "settings", section: "diag", sub: "", redirect: "#/settings/diag"};
  }
  if (SETTINGS_SECTIONS.includes(first)) {
    const sub = (SETTINGS_SUBS[first] || []).includes(second) ? second : "";
    return {view: "settings", section: first, sub};
  }
  const moved = SETTINGS_REDIRECTS[first];
  if (moved) {
    const [section, sub] = moved.split("/");
    return {view: "settings", section, sub, redirect: "#/settings/" + moved};
  }
  return {view: "settings", section: defaultSection(), sub: ""};
}

function settingsHash() { return "#/settings/" + (state.lastSection || defaultSection()); }

// v0.1.55: when the app itself changes the location (navigate, a #-link click, the nav buttons) a popstate that
// lands on another route is a real route change; without a recent app navigation it is a page back (onPopState).
let appNavAt = -1e9;
function noteAppNav() { appNavAt = performance.now(); }
const APP_NAV_MS = 700;

function navigate(hash) {
  noteAppNav();
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
  if (!state.authed || (swipeDiag && swipeDiag.cleaning)) return;
  const route = parseRoute();
  if (route.redirect) { replaceRoute(route.redirect); return; }
  if (route.view === "tab" && !findTab(route.tabId)) { replaceRoute("#/home"); return; }
  state.route = route;
  // The folder sheet belongs to Settings › JD2 › 保存先: leaving it closes the sheet.
  if (state.pickerOpen && !(route.view === "settings" && route.section === "jd2")) setPicker(false);
  if (route.view === "links" && route.paste) {
    // Drop the pasted text from the address bar and history state at once.
    history.replaceState(null, "", "#/links");
    $("pasteBox").value = route.paste.slice(0, PASTE_LIMIT);
    schedulePaste(0);
    state.route = {view: "links"};
  }
  gestureTrace("route", {hash: location.hash, view: route.view});
  if (route.view === "tab") { state.lastWebTabId = route.tabId; armBackGuard(); }
  if (route.view === "settings") state.lastSection = route.section;
  closeMenu(false);
  closeSwitcher(false);
  const shown = currentViewName();
  dlog("ui", "info", "route", {view: route.view, shown, section: route.section || null, sub: route.sub || null});
  for (const name of ["home", "downloads", "links", "settings", "web"]) {
    $("view-" + name).hidden = name !== shown;
  }
  renderWebTabs();
  updateChrome();
  syncRemotePoll();
  sched.kick();
  // Last route (a hash, never a secret) lets a reloaded iOS web app resume.
  writeSession(SHELL_KEY, JSON.stringify({v: 1, hash: location.hash || "#/home"}));
  perform(() => enterView(route));
}

async function enterView(route, force = false) {
  if (route.view === "home" || (route.view === "tab" && currentViewName() === "home")) {
    await loadHome(force);
  } else if (route.view === "downloads") {
    renderDownloads();
    refreshDownloadsNow();
  } else if (route.view === "links") {
    await loadQueue(force);
  } else if (route.view === "settings") {
    await enterSettings(route, force);
  }
}

// -------------------------------------------------------------- chrome / UI
function tabTitle(tab) {
  return tab.history.length ? hostOf(tab.history[tab.index]) : "新しいタブ";
}

function setOmniIcon(name) { $("omniIcon").setAttribute("href", "#i-" + name); }

// v0.1.60: the omnibox shows the domain (like Safari) while it is not being edited; the full URL appears on focus.
function omniDomain(url) {
  try {
    const u = new URL(url);
    if (u.protocol !== "https:" && u.protocol !== "http:") return String(url);
    return u.host.replace(/^www\./, "") || String(url);
  } catch (e) { return String(url); }
}

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
    back = Boolean(tab.rb || tab.pt);       // history lives in the NAS browser tab (v0.1.57: or in the app for passthrough)
    forward = Boolean(tab.rb || tab.pt);
  } else if (route.view === "downloads") {
    display = "qnap://downloads"; iconName = "download";
  } else if (route.view === "links") {
    display = "qnap://links"; iconName = "link";
  } else if (route.view === "settings") {
    display = "qnap://settings/" + route.section + (route.sub ? "/" + route.sub : ""); iconName = "gear";
  } else {
    iconName = "home";
  }
  state.omniDisplay = display;
  state.omniShort = tab && tab.history.length ? omniDomain(display) : display;      // v0.1.60: domain only when idle
  if (document.activeElement !== $("omnibox")) $("omnibox").value = state.omniShort;
  setOmniIcon(iconName);
  updateModeChip(tab);
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
  updateRemoteView(tab);
  updatePinnedTabs();
  updateMobileBar();
  chromeSync();
}


// ---------------------------------------------------- v0.1.60: collapse the bars while reading
// v0.1.63: phone widths AND touch devices of any width (iPad; `(pointer: coarse)`), never a mouse. On a wide screen the
// tab strip goes with the toolbar. Scrolling the page down hides the top bar (and the bottom tab bar);
// scrolling up, reaching the top, opening the menu or focusing the address bar shows them again, like Safari.
// The toolbar and the tab bar move with transform only. The content area grows into the freed space in ONE layout
// step: the content is moved with a compensating transform first and then slides, so the page never jumps and no
// scroll position is written (writing scrollTop would stop an iOS fling). The size the NAS browser is told about
// does not change (stageViewport subtracts chromeExtra()), so collapsing never makes the NAS re-layout.
const CHROME_HIDE_PX = 36, CHROME_SHOW_PX = 24, CHROME_MIN_TOP = 90, CHROME_MUTE_MS = 150;
const chrome = {collapsed: false, key: "", last: 0, down: 0, up: 0, mute: 0, h: 0, pad: 0, settle: 0};
const chromeNarrow = typeof window.matchMedia === "function" ? window.matchMedia("(max-width: 759px), (pointer: coarse)") : null;
// v0.1.63: height of everything that slides away: the toolbar and, on a wide screen, the tab strip above it
function chromeBarHeight() {
  const bar = document.querySelector(".toolbar"), strip = $("tabstrip");
  return Math.round(bar.offsetHeight + (strip && strip.offsetParent !== null ? strip.offsetHeight : 0));
}
const chromeReduced = typeof window.matchMedia === "function" ? window.matchMedia("(prefers-reduced-motion: reduce)") : null;

function chromeExtra() { return chrome.collapsed ? chrome.h + chrome.pad : 0; }

function chromeCan() {
  if (!chromeNarrow || !chromeNarrow.matches || document.body.dataset.view !== "web") return false;
  if (document.activeElement === $("omnibox") || !$("menu").hidden || !$("tabSwitcher").hidden || !$("swipeBanner").hidden) return false;
  const tab = activeWebTab();
  if (!tab || !tab.history.length) return false;
  if (tab.pt) return true;
  const display = tab.rb ? tabDisplay(tab) : "";
  return display === "mirror" || display === "reader" || display === "tiles";     // the live picture does not scroll here
}

function chromeSet(on, instant = false) {
  if (chrome.collapsed === on) return;
  const body = document.body, content = $("content");
  chrome.h = chromeBarHeight();
  const padBefore = parseFloat(getComputedStyle(content).paddingBottom) || 0;
  document.documentElement.style.setProperty("--bar-h", chrome.h + "px");
  const slide = !instant && !(chromeReduced && chromeReduced.matches);
  content.style.transition = "none";
  if (slide) content.style.transform = "translateY(" + (on ? chrome.h : -chrome.h) + "px)";
  body.classList.toggle("chrome-collapsed", on);
  chrome.collapsed = on;
  chrome.pad = on ? Math.max(0, padBefore - (parseFloat(getComputedStyle(content).paddingBottom) || 0)) : 0;
  void content.offsetHeight;                      // the start position is laid out; now slide to the end position
  if (slide) { content.style.transition = "transform .2s ease"; content.style.transform = ""; }
  else { content.style.transition = ""; content.style.transform = ""; }
  clearTimeout(chrome.settle);
  chrome.settle = setTimeout(() => { content.style.transition = ""; content.style.transform = ""; }, 280);
  chrome.mute = performance.now() + CHROME_MUTE_MS;       // the resize itself may move scrollTop: not a user scroll
  chrome.down = chrome.up = 0;
  dlog("ui", "debug", "chrome", {collapsed: on, h: chrome.h});
}

function chromeShow(instant = false) { if (chrome.collapsed) chromeSet(false, instant); }

// Called from every scroller (mirror document, reader, tiles, passthrough report) with its scrollTop and the
// largest scrollTop; `key` names the scroller so that a switch between them does not count as a scroll.
function chromeScrolled(top, max, key) {
  if (!chromeCan()) { chrome.last = top; chrome.key = key; chromeShow(true); return; }
  if (key !== chrome.key) { chrome.key = key; chrome.last = top; chrome.down = chrome.up = 0; if (top <= 2) chromeShow(); return; }
  const d = top - chrome.last;
  chrome.last = top;
  if (top <= 2) { chrome.down = chrome.up = 0; chromeShow(); return; }
  if (performance.now() < chrome.mute) return;
  if (Number.isFinite(max) && top > max + 1) return;              // rubber band below the page end
  if (d > 0) {
    chrome.up = 0; chrome.down += d;
    if (!chrome.collapsed && chrome.down > CHROME_HIDE_PX && top > CHROME_MIN_TOP) chromeSet(true);
  } else if (d < 0) {
    chrome.down = 0; chrome.up -= d;
    if (chrome.collapsed && chrome.up > CHROME_SHOW_PX) chromeSet(false);
  }
}

// updateChrome() calls this: leaving the reading situation (other tab / display / menu / editing) shows the bars.
function chromeSync() {
  if (chrome.collapsed && !chromeCan()) chromeSet(false, true);
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
  const shownTabs = state.tabs.filter(t => !t.diag).length;
  count.textContent = String(shownTabs);
  count.hidden = shownTabs === 0;
  $("mobileBrowse").setAttribute("aria-label", "ブラウズ（Webタブ " + shownTabs + "枚）");
}

function renderWebTabs() {
  const list = $("webTabs");
  list.replaceChildren();
  for (const tab of state.tabs) {
    if (tab.diag) continue;
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
    close.title = "タブを閉じる";                // v0.1.63: tooltip for the mouse
    close.appendChild(icon("x"));
    close.addEventListener("click", () => closeTab(tab.id));
    wrap.appendChild(close);
    list.appendChild(wrap);
  }
  updateMobileBar();
  renderHomeTabs();      // v0.1.62
}

// v0.1.60: the second row is gone. What it showed now lives in: the mode chip inside the omnibox (NAS browser /
// passthrough and the display), the thin progress line under the bar (loading), the menu's "このページ" line (page
// title) and - only while there is a problem - this inline banner (load errors), which dismisses itself.
let webStatusTimer = 0;
function updateWebStatus(tab) {
  const box = $("webStatus");
  const text = tab && tab.history.length && tab.error && tab.errorSeen !== tab.error
    ? hostOf(tab.history[tab.index]) + " — 読み込めませんでした: " + tab.error : "";
  if (box.dataset.text === text) return;
  box.dataset.text = text;
  box.replaceChildren();
  clearTimeout(webStatusTimer);
  if (!text) return;
  box.setAttribute("role", "status");
  const label = element("span", text, "web-status-text");
  label.title = tab.current || text;
  box.appendChild(label);
  const close = element("button", undefined, "icon-btn small");
  close.type = "button";
  close.setAttribute("aria-label", "閉じる");
  close.title = "閉じる";
  close.appendChild(icon("x"));
  const dismiss = () => { tab.errorSeen = tab.error; updateWebStatus(tab); };
  close.addEventListener("click", dismiss);
  box.appendChild(close);
  webStatusTimer = setTimeout(dismiss, 8000);
}

// v0.1.60: the display chip in the omnibox.
function updateModeChip(tab) {
  const chip = $("modeBtn");
  const on = Boolean(tab && tab.history.length);
  chip.hidden = !on;
  if (!on) return;
  const display = tab.pt ? "pt" : tab.rb ? tabDisplay(tab) : "tiles";
  chip.textContent = DISPLAY_CHIP[display] || "?";
  chip.classList.toggle("is-pt", display === "pt");
  const where = tab.pt ? "パススルー" : "NASブラウザ";
  chip.title = "表示方法を選ぶ（" + where + " · " + (DISPLAY_CHIP_TITLE[display] || display) + "）";
  chip.setAttribute("aria-label", chip.title);
}

// ------------------------------------------------------------- status/home
async function loadSettings(force = false) {
  await swr("settings", "/api/settings", renderDownloadDir, {force});
}

// Settings and bookmarks arrive with the first /api/state call (seeded into the swr cache), so
// entering the home view costs no request.
async function loadHome(force = false) {
  renderHomeTabs(); renderHomeRecent();      // v0.1.62: from what is already here, before any request answers
  await Promise.all([loadSettings(force), loadBookmarks(force)]);
}

// The Hub may have been updated (from this or another device) since this page
// loaded. Offer a reload; never reload automatically here.
function checkBootVersion(version) {
  if (typeof version !== "string" || !version) return;
  if (!state.bootVersion) state.bootVersion = APP_VERSION;      // v0.1.60: this file's version, not the first one the Hub reports
  if (version === state.bootVersion || state.updateWatching) return;
  // v0.1.60: this page came out of the app-shell cache and is older than the Hub: drop the cache and load the new
  // one now (once per Hub version, so a Hub whose files disagree with each other cannot cause a reload loop).
  if (shellServedFromCache() && readSession(SHELL_RELOAD_KEY) !== version) {
    writeSession(SHELL_RELOAD_KEY, version);
    reloadFresh();
    return;
  }
  stickyToast("stale", "Hubがv" + version + "に更新されています。再読み込みしてください" +
    "（再読み込み後に再ログインが必要です）", "info",
    [{label: "再読み込み", run: reloadFresh}]);
}

// ---- v0.1.60: app shell cache (Service Worker /app-sw.js, secure contexts only: https / localhost) ----
const SHELL_RELOAD_KEY = "qd.shellReload";
// https only (the home-screen app over tailscale serve). A loopback http page is a secure context too, but WebKit
// refuses workers there ("access control checks"); Chromium-based browsers keep it for local tests.
function swSupported() {
  if (!("serviceWorker" in navigator) || window.isSecureContext !== true) return false;
  if (location.protocol === "https:") return true;
  return /^(127\.0\.0\.1|localhost)$/.test(location.hostname) && /Chrome\//.test(navigator.userAgent);
}
function shellServedFromCache() {
  try { return swSupported() && Boolean(navigator.serviceWorker.controller); } catch (e) { return false; }
}
async function registerShell() {
  if (!swSupported()) return;
  try {
    await navigator.serviceWorker.register("/app-sw.js", {scope: "/", updateViaCache: "none"});
  } catch (e) {
    dlog("ui", "warn", "app shell worker not registered", {error: String(e && e.message || e).slice(0, 80)});
  }
}
// Unregisters this origin's worker and deletes its shell caches (never the API: nothing else is ever cached).
async function dropShellCache() {
  try {
    if (swSupported()) {
      const regs = await navigator.serviceWorker.getRegistrations();
      await Promise.all(regs.map(r => r.unregister()));
    }
  } catch (e) { /* nothing to drop */ }
  try {
    if (window.caches) {
      const names = await caches.keys();
      await Promise.all(names.filter(n => n.indexOf("qd-shell-") === 0).map(n => caches.delete(n)));
    }
  } catch (e) { /* nothing to drop */ }
}
async function reloadFresh() {
  await dropShellCache();
  location.reload();
}

function setCheck(rowId, done) {
  $(rowId).classList.toggle("is-done", done);
}

// One "はじめに" checklist until a save folder is chosen and the engine is set
// up; afterwards a single status line. Nothing renders before both are known.
function renderHome() {
  renderHomeTabs();      // v0.1.62: guarded by a signature; the lists are rebuilt only when something changed
  renderHomeRecent();
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
        "Container Stationが必要です（設定 › JD2）" : "JDownloader 2を導入します") :
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


// ----------------------------------------------- browse home (v0.1.62)
// v0.1.62: open tabs as cards (tap = switch, x = close), the recent pages list (localStorage only, never sent
// anywhere) and a "copied URL" chip that reads the clipboard only when it is tapped.
function readRecent() {
  try {
    const data = JSON.parse(readLocal(RECENT_KEY) || "[]");
    if (!Array.isArray(data)) return [];
    return data.filter(e => e && typeof e.u === "string" && /^https?:\/\//i.test(e.u) && e.u.length <= 2048)
      .map(e => ({u: e.u, t: typeof e.t === "string" ? e.t.slice(0, 200) : "", at: Number(e.at) || 0})).slice(0, RECENT_MAX);
  } catch (e) { return []; }
}

// Remember a page the NAS browser / passthrough finished loading: newest first, one row per address.
function recentOn() { return !(state.settings && state.settings.home_recent === false); }    // v0.1.65

function noteVisit(url, title) {
  if (!recentOn()) return;                // v0.1.65: switched off: nothing is recorded
  if (typeof url !== "string" || !/^https?:\/\//i.test(url) || url.length > 2048) return;
  if (/__cf_chl|\/cdn-cgi\/challenge/i.test(url)) return;      // Cloudflare check pages are not worth remembering
  const list = readRecent();
  const hit = list.find(e => e.u === url);
  if (hit && hit.t === (title || hit.t) && list[0] === hit) return;     // nothing new
  const next = [{u: url, t: String(title || (hit && hit.t) || "").slice(0, 200), at: Date.now()}]
    .concat(list.filter(e => e.u !== url)).slice(0, RECENT_MAX);
  writeLocal(RECENT_KEY, JSON.stringify(next));
  renderHomeRecent();
}

function clearRecent() {
  removeLocal(RECENT_KEY);
  renderHomeRecent();
  flash("最近見たページを消しました");
}

// A cheap thumbnail only: the first tile the phone already holds for a "fast" tab. Nothing is fetched for this.
function tabThumb(tab) {
  const view = tab.rb && tab.rb.view;
  const first = view && view.tiles && view.tiles[0];
  return first && typeof first.url === "string" && first.url.startsWith("blob:") ? first.url : "";
}

function tabCardInfo(tab) {
  const url = tab.current || tab.history[tab.index] || "";
  const title = (tab.pt ? tab.ptTitle : tab.rb && tab.rb.title) || "";
  const display = tab.pt ? PT_DISPLAY_LABEL : tab.rb ? RB_DISPLAY_LABELS[tabDisplay(tab)] || "" : "";
  return {url, title: title || hostOf(url), host: hostOf(url), display};
}

function renderHomeTabs() {
  const tabs = state.tabs.filter(t => !t.diag && t.history.length);
  const infos = tabs.map(t => [t, tabCardInfo(t)]);
  const sig = infos.map(([t, i]) => [t.id, i.url, i.title, i.display, tabThumb(t) ? 1 : 0].join("|")).join("\n");
  if (sig === state.homeTabsSig) return;
  state.homeTabsSig = sig;
  setShown("homeTabs", tabs.length > 0);
  const nodes = infos.map(([tab, info]) => {
    const card = element("div", undefined, "tab-card");
    card.dataset.tab = tab.id;
    const main = element("button", undefined, "tab-card-main");
    main.type = "button";
    main.setAttribute("aria-label", info.title + "（" + info.host + "）のタブへ切り替え");
    const thumb = element("span", undefined, "tab-card-thumb");
    const src = tabThumb(tab);
    if (src) {
      const img = document.createElement("img");
      img.alt = ""; img.decoding = "async"; img.src = src;      // v0.1.62: a blob: URL of a tile the phone already holds (tabThumb)
      img.addEventListener("error", () => img.remove());
      thumb.appendChild(img);
    }
    thumb.appendChild(element("span", info.host.slice(0, 1).toUpperCase(), "tab-card-letter"));
    main.appendChild(thumb);
    main.appendChild(element("strong", info.title, "tab-card-title"));
    const meta = element("span", undefined, "tab-card-meta");
    meta.appendChild(element("span", info.host, "tab-card-host"));
    if (info.display) meta.appendChild(element("span", info.display, "tab-card-mode"));
    main.appendChild(meta);
    main.addEventListener("click", () => navigate("#/tab/" + tab.id));
    const close = element("button", undefined, "tab-card-close");
    close.type = "button";
    close.setAttribute("aria-label", "タブを閉じる: " + info.title);
    close.title = "タブを閉じる";
    close.appendChild(icon("x"));
    close.addEventListener("click", () => closeTab(tab.id));
    card.append(main, close);
    return card;
  });
  $("homeTabGrid").replaceChildren(...nodes);
}

function renderHomeRecent() {
  const list = recentOn() ? readRecent() : [];
  const sig = JSON.stringify(list);
  if (sig === state.homeRecentSig) return;
  state.homeRecentSig = sig;
  setShown("homeRecent", list.length > 0);
  $("homeRecentList").replaceChildren(...list.map(entry => {
    const row = element("button", undefined, "recent-row");
    row.type = "button";
    row.dataset.url = entry.u;
    row.title = entry.u;
    row.appendChild(element("strong", entry.t || hostOf(entry.u), "recent-title"));
    row.appendChild(element("span", hostOf(entry.u), "recent-host"));
    row.addEventListener("click", () => perform(() => openWebUrl(entry.u)));
    return row;
  }));
}

// The first https address in the copied text (a single plain address is passed on as it is: the Hub validates it).
function clipboardUrl(text) {
  const t = String(text || "").trim();
  if (!t) return "";
  if (t.length <= 2048 && !/\s/.test(t)) return t;
  const m = /https?:\/\/[^\s<>"']+/i.exec(t);
  return m ? m[0].replace(/[.,;:)\]}]+$/, "") : "";
}

// Clipboard access happens here only, on the tap. Without the API (or when it is refused) the address bar gets the focus.
async function openClipboardUrl() {
  let text = "";
  if (navigator.clipboard && typeof navigator.clipboard.readText === "function") {
    try { text = await navigator.clipboard.readText(); } catch (e) { text = ""; }
  }
  const url = clipboardUrl(text);
  if (url) { await openWebUrl(url); return; }
  if (text) flash("コピーした内容にURLが見つかりません");
  const box = $("omnibox");
  box.focus();
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
    list.replaceChildren(emptyState("ブックマークはありません", "Webタブの ☆ から追加できます"));
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
// Every web tab is a NAS browser tab (v0.1.35): there is no in-phone iframe any more.
function newTabRecord(id) {
  return {id, history: [], index: -1, current: "", loading: false, token: 0, error: "",
    notReady: false, rb: null, pt: false, ptTitle: "", ptAwait: false, ptNative: {back: 0, fwd: 0}};   // v0.1.57: pt = passthrough display
}

function saveTabs() {
  // Only id / url history / index. No tokens, cookies or page data.
  const data = {v: 1, next: state.nextTabId, tabs: state.tabs.filter(t => !t.diag).map(t => ({
    id: t.id, history: t.history.slice(), index: t.index,
    pt: t.pt === true,      // v0.1.57: passthrough display (the page lives in the phone's frame; no NAS tab)
    nas: t.rb ? t.rb.id : (t.savedNas || "")}))};   // v0.1.43: the NAS tab is reattached after a reload
  writeSession(TAB_STORAGE_KEY, JSON.stringify(data));
}

function restoreTabs() {
  let data = null;
  try { data = JSON.parse(readSession(TAB_STORAGE_KEY) || "null"); } catch (e) { data = null; }
  if (!data || data.v !== 1 || !Array.isArray(data.tabs)) return;
  let maxId = 0;
  for (const raw of data.tabs.slice(0, Math.max(MAX_WEB_TABS, 20))) {   // v0.1.65: the setting may allow more than 6
    if (!raw || typeof raw.id !== "string" || !/^t\d+$/.test(raw.id) || findTab(raw.id)) continue;
    const tab = newTabRecord(raw.id);
    const history = Array.isArray(raw.history) ? raw.history : [];
    tab.pt = raw.pt === true;
    tab.history = history.filter(u => typeof u === "string" && u.length <= 2048 &&
      (u.startsWith("https://") || (tab.pt && u.startsWith("http://")))).slice(-MAX_HISTORY);
    const idx = Number.isInteger(raw.index) ? raw.index : tab.history.length - 1;
    tab.index = tab.history.length ? Math.min(Math.max(idx, 0), tab.history.length - 1) : -1;
    if (typeof raw.nas === "string" && /^b\d{1,9}$/.test(raw.nas)) tab.savedNas = raw.nas;
    state.tabs.push(tab);   // the NAS browser tab is reattached or opened when the tab is shown (autoReopen)
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

// v0.1.65: the number of web tabs follows the Hub setting rb_max_tabs (NAS browser tabs, 1-20), but never goes below
// MAX_WEB_TABS so tabs that are not loaded (restored, passthrough) keep working as before.
function webTabLimit() {
  const n = state.settings && Number(state.settings.rb_max_tabs);
  return Number.isInteger(n) && n >= 1 && n <= 20 ? Math.max(MAX_WEB_TABS, n) : MAX_WEB_TABS;
}

function tabLimitReached() {
  if (state.tabs.length < webTabLimit()) return false;
  toast("Webタブは最大" + webTabLimit() + "枚です（設定›ブラウザで変更できます）。端末とNASのメモリを考慮した上限です。不要なタブを閉じてください。", "error");
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
  if (tab.diag && tab.diag.stubClose) {       // v0.1.52: the swipe diagnostic only checks that a close was asked for
    tab.diag.closeCalls++;
    gestureTrace("pull.close", {stub: true});
    return;
  }
  if (tab.history.length) {
    state.closed.push({history: tab.history.slice(), index: tab.index});
    while (state.closed.length > MAX_CLOSED_TABS) state.closed.shift();
  }
  tab.token++;
  if (ptHost.tab === tab) ptDrop();     // v0.1.57
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
  saveTabs();
  navigate("#/tab/" + tab.id);   // the NAS browser tab is opened when the tab is shown
}

// Ask the Hub to validate; the returned URL is the only value handed to the NAS browser.
async function validateUrl(value) {
  const result = await api("/api/browser/validate?url=" + encodeURIComponent(value));
  return result.url;
}

// ---------------------------------------------------------- NAS remote browser
// The Hub validates every URL (/api/browser/validate rules) and forwards a fixed
// set of inputs. The only image source is a blob: object URL made from
// the Hub's /api/rb/frame.jpg bytes; it is never a remote URL.
// opts.validated: `value` already came from validateUrl().
function newRbRecord(id, tab) {
  return {id, seq: 0, url: "", title: "", width: 0, height: 0,
    loading: true, links: [], linksTimer: 0, blocked: null,
    rev: null, icptSeen: 0, present: 0, vp: "", popups: 0,
    shown: "", chal: null, view: null, snap: null, snapKey: "", snapBusy: false,
    snapAgain: null, snapTimer: 0, busy: 0, scrollTop: 0, owner: tab,
    vmap: null, vmapRev: -1, vmapTimer: 0, vmapBusy: false, vmapAt: 0, linksRev: -1,
    reader: null, readerBusy: false, readerPending: false, readerAgain: false,
    readerEarly: "", readerFail: null, readerErr: false, tilesFor: "", snapEarly: "", loadUrl: "", loadSince: 0,
    mirror: null, mirrorBusy: false, mirrorAgain: false, mirrorPending: false, mirrorEarly: "", mirrorFail: null, mirrorFollow: false, tl: null,
    mirrorAt: 0, mirrorMs: 0, mirrorTimer: 0, mirrorTouch: false, mirrorDeferred: false, mirrorPos: {},
    stream: null, streamFailed: false, perf: {}, seed: null, firstPending: false, heldMeta: null, vpSent: null};
}

// v0.1.43: after a reload of this page the NAS tab usually still exists: use it again instead of
// opening (and loading) a new one. Returns true when it was reattached.
async function reattachRemote(tab) {
  const id = tab.savedNas;
  tab.savedNas = "";
  if (!id) return false;
  try {
    const res = await fetch("/api/rb/meta?tab=" + encodeURIComponent(id) + "&wait=0",
      {credentials: "same-origin", cache: "no-store"});
    if (res.status !== 200 || tab.rb) return false;
    const meta = await res.json();
    const want = tab.current || (tab.history.length ? tab.history[tab.index] : "");
    if (state.tabs.some(t => t !== tab && t.rb && t.rb.id === id)) return false;
    if (typeof meta.url !== "string" || !meta.url) return false;
    tab.rb = newRbRecord(id, tab);
    tab.current = want || meta.url;
    dlog("rb", "info", "tab reattached", {tab: id});
    return true;
  } catch (e) {
    return false;
  }
}

// ---- first-load timeline (v0.1.54): milliseconds since the tap, sent once to the Hub (POST
// /api/rb/firstload), which merges them with its own stages, logs them (category render) and answers the
// breakdown line shown in the speed overlay.
function tlStart(rb, t0, createdAt, r0) {
  rb.tl = {t0, marks: {}, sent: false, result: null, r0: Number.isFinite(r0) ? r0 : rttCount, rtts: null};
  if (createdAt) tlMark(rb, "created", createdAt);
}

function tlMark(rb, name, at) {
  const tl = rb && rb.tl;
  if (!tl || tl.sent || name in tl.marks) return;
  tl.marks[name] = Math.max(0, Math.round((at || performance.now()) - tl.t0));
}

function tlFlush(rb) {
  const tl = rb && rb.tl;
  if (!tl || tl.sent) return;
  tl.sent = true;
  api("/api/rb/firstload", {tab: rb.id, marks: tl.marks, rtts: tl.rtts}).then(data => {
    if (rb.tl !== tl || !data || typeof data.line !== "string") return;
    tl.result = data;
    dlog("render", "info", "first load line", {line: data.line.slice(0, 200)});
    if (state.rbPerf) renderPerf();
  }).catch(() => {});
}

// Mirror: the document of the copy was parsed (`doc`), the images in the first screen are in (`paint`, or 1 s
// later at the latest), the images up to three screens down are in (`full`, 15 s cap).
function tlWatchMirror(rb, doc) {
  const tl = rb && rb.tl;
  if (!tl || tl.sent || tl.watching) return;
  tl.watching = true;
  tlMark(rb, "doc");
  tl.rtts = rttCount - tl.r0;
  const view = doc.defaultView;
  const height = (view && view.innerHeight) || 800;
  const began = performance.now();
  const waiting = limit => Array.from(doc.images).filter(img => {
    if (img.complete) return false;
    const r = img.getBoundingClientRect();
    return r.bottom > 0 && r.top < limit;
  }).length;
  const painted = () => {
    if (rb.tl !== tl || tl.sent || mirrorDoc() !== doc) return;
    if (!waiting(height) || performance.now() - began >= 1000) { tlMark(rb, "paint"); loaded(); return; }
    setTimeout(painted, 40);
  };
  const loaded = () => {
    if (rb.tl !== tl || tl.sent || mirrorDoc() !== doc) return;
    if (!waiting(height * 3) || performance.now() - began >= 15000) { tlMark(rb, "full"); tlFlush(rb); return; }
    setTimeout(loaded, 150);
  };
  painted();
}

// Reader: the text is on screen (`doc`); the pictures in the first screen are in, or 1 s (`paint`); ...(`full`).
function tlWatchReader(rb) {
  const tl = rb && rb.tl;
  if (!tl || tl.sent || tl.watching) return;
  tl.watching = true;
  tlMark(rb, "doc");
  tl.rtts = rttCount - tl.r0;
  const scroller = $("readerScroll");
  const height = scroller.clientHeight || 800;
  const began = performance.now();
  const waiting = limit => Array.from($("readerBody").querySelectorAll(".reader-fig")).filter(f => {
    if (f.classList.contains("is-loaded") || f.classList.contains("is-failed")) return false;
    const r = f.getBoundingClientRect(), top = scroller.getBoundingClientRect().top;
    return r.bottom > top && r.top < top + limit;
  }).length;
  const painted = () => {
    if (rb.tl !== tl || tl.sent) return;
    if (!waiting(height) || performance.now() - began >= 1000) { tlMark(rb, "paint"); loaded(); return; }
    setTimeout(painted, 40);
  };
  const loaded = () => {
    if (rb.tl !== tl || tl.sent) return;
    if (!waiting(height * 3) || performance.now() - began >= 15000) { tlMark(rb, "full"); tlFlush(rb); return; }
    setTimeout(loaded, 150);
  };
  painted();
}

function firstLoadLine(rb) {
  const tl = rb && rb.tl;
  if (tl && tl.result) return tl.result.line;
  if (tl && !tl.sent) return "計測中";
  return "";
}

// v0.1.59: the viewer's size sent along with a tab-open / navigate request (the Hub applies it before the page
// loads, so no separate /api/rb/viewport round trip). When the stage cannot be measured (the tab view is not
// shown yet) the last measured size, else the window size, is used; the viewport loop corrects it afterwards.
const STAGE_KEY = "qd.stage";
function rememberStage(view) {
  const same = state.lastStage && state.lastStage.width === view.width && state.lastStage.height === view.height &&
    state.lastStage.dpr === view.dpr;
  state.lastStage = view;
  if (!same) writeLocal(STAGE_KEY, JSON.stringify(view));
}
function recallStage() {
  try {
    const v = JSON.parse(readLocal(STAGE_KEY) || "null");
    if (v && Number.isInteger(v.width) && Number.isInteger(v.height) && Number(v.dpr) >= 1 &&
        v.width >= 320 && v.width <= 1600 && v.height >= 320 && v.height <= 2400) return {width: v.width, height: v.height, dpr: Number(v.dpr)};
  } catch (e) { /* none */ }
  return null;
}

function rbViewBody() {
  let view = stageViewport();
  if (view) rememberStage(view);
  else view = state.lastStage || recallStage();
  if (!view) {
    const dpr = Math.min(3, Math.max(1, Number(window.devicePixelRatio) || 1));
    view = {width: Math.min(1600, Math.max(320, document.documentElement.clientWidth || 390)),
      height: Math.min(2400, Math.max(320, (window.innerHeight || 800) - 130)), dpr: Math.round(dpr * 100) / 100};
  }
  return Object.assign({quality: state.rbQuality}, view);
}

function rbViewKey(view) {
  return view.width + "x" + view.height + "@" + view.dpr + "/" + view.quality;
}

// The display a page opened in `tab` will be shown with, decided before the tab exists (v0.1.59).
function plannedDisplay(tab, url) {
  if (tab.diag && tab.diag.mode) return tab.diag.mode;
  const remembered = DISPLAY_OF_MODE[rememberedMode(hostOf(url))];
  if (remembered) return remembered;
  return DISPLAY_OF_SETTING[state.settings && state.settings.rb_default_mode] || "tiles";
}

// opts.validated: `value` already came from validateUrl(); otherwise the Hub validates it inside the tabs /
// navigate request itself (v0.1.59: no separate /api/browser/validate round trip) and answers the canonical URL.
async function openRemote(tab, value, commit, opts = {}) {
  ensureRemote();
  const tapAt = performance.now();
  const r0 = rttCount;
  let madeAt = 0;
  const token = ++tab.token;
  tab.error = "";
  tab.notReady = false;
  tab.loading = true;
  updateChrome();
  let url = value, reply = null, view = null, first = null;
  try {
    if (state.rbReady === false) {
      // Not set up (or the image is outdated): one clear state in the tab, nothing is started.
      url = opts.validated ? value : await validateUrl(value);
      if (token !== tab.token || !state.tabs.includes(tab)) return;
      commit(url);
      tab.current = url;
      tab.notReady = true;
      tab.loading = false;
      saveTabs();
      refreshAfterTabChange();
      return;
    }
    if (!state.settings) await loadSettings().catch(() => {});   // the default display comes from Settings
    if (token !== tab.token || !state.tabs.includes(tab)) return;
    view = rbViewBody();
    const planned = plannedDisplay(tab, value);
    first = planned === "mirror" || planned === "reader" ? planned : null;
    const body = {url: value, view};
    if (first) body.first = first;
    if (tab.rb) {
      const rb = tab.rb;
      rb.firstPending = Boolean(first);       // the loop must not fetch the same first copy a second time
      try {
        reply = await api("/api/rb/navigate", Object.assign({tab: rb.id}, body));
      } catch (e) {
        firstDone(tab, rb, null);
        if (e.status !== 404) throw e;
        tab.rb = null;   // closed on the NAS meanwhile (idle stop): open a fresh one
      }
    }
    if (!tab.rb) {
      // v0.1.60: a dropped answer (WebKit "Load failed") is asked once more with the same nonce, so the NAS
      // answers with the tab it already made instead of opening another one nobody would close.
      body.nonce = rbNonce();
      try {
        reply = await api("/api/rb/tabs", body);
      } catch (e) {
        if (!(e instanceof TypeError) || e.status) throw e;
        if (token !== tab.token || !state.tabs.includes(tab)) return;
        reply = await api("/api/rb/tabs", body);
      }
      madeAt = performance.now();
      if (token !== tab.token || !state.tabs.includes(tab)) {
        api("/api/rb/tabs/close", {id: reply.id}).catch(() => {});
        return;
      }
      tab.rb = newRbRecord(reply.id, tab);
      tab.rb.vp = reply.view === true ? rbViewKey(view) : "";
      tab.rb.vpSent = reply.view === true ? view : null;
    }
    if (typeof reply.url === "string") url = reply.url;
  } catch (e) {
    if (tab.rb) firstDone(tab, tab.rb, null);
    if (token === tab.token) { tab.loading = false; tab.error = e.message; updateChrome(); }
    throw e;
  }
  if (tab.rb) { tab.rb.firstPending = false; tab.rb.heldMeta = null; }
  // (before commit(): updateChrome() may already start the display loop, which takes the seed at its start)
  if (first && reply.first && typeof reply.first === "object") tab.rb.seed = {kind: first, first: reply.first};
  commit(url);
  tab.current = url;
  tab.loading = false;
  tab.rb.seq = 0;
  tab.rb.rev = null;
  tlStart(tab.rb, tapAt, madeAt, r0);
  saveTabs();
  swrDrop("rb-status");
  refreshAfterTabChange();
  if (tab.rb.seed) applySeed(tab);        // a loop that was already running takes the first copy now
}

function rbNonce() {
  const bytes = new Uint8Array(12);
  try { crypto.getRandomValues(bytes); } catch (e) { for (let i = 0; i < bytes.length; i++) bytes[i] = Math.random() * 256 | 0; }
  return Array.from(bytes, b => b.toString(16).padStart(2, "0")).join("");
}

// v0.1.59: the request that carried (or should have carried) the first copy is over. With a copy: use it. Without
// one: the metadata the loop had to hold back meanwhile is handled now, as the loop would have done.
function firstDone(tab, rb, seed) {
  rb.firstPending = false;
  const held = rb.heldMeta;
  rb.heldMeta = null;
  if (!tab || tab.rb !== rb) return;
  if (seed) { applySeed(tab, seed); return; }
  if (!held) return;
  const display = tabDisplay(tab);
  if (display === "mirror" && mirrorAction(rb, held, Date.now()) === "run") runMirror(tab, {quiet: true}).catch(() => {});
  else if (display === "reader" && readerAction(rb, held, Date.now()) === "run") runReader(tab, {quiet: true}).catch(() => {});
}

// v0.1.59: the first copy / extraction that came with the tab-open / navigate answer, handled exactly like the
// loop would handle the metadata and the capture it asks for next (without the requests).
function takeSeed(rb, kind) {
  const seed = rb && rb.seed;
  if (!seed) return null;
  rb.seed = null;
  return seed.kind === kind ? seed : null;
}

function applySeed(tab, seed = null) {
  const rb = tab && tab.rb;
  if (!rb) return;
  if (!seed) seed = takeSeed(rb, tabDisplay(tab));
  if (!seed || !seed.first || !seed.first.meta) return;
  rb.seed = null;
  const meta = seed.first.meta;
  applyRemoteMeta(tab, Object.assign({}, meta, {seq: rb.seq}));
  if (tab.rb !== rb || tabDisplay(tab) !== seed.kind) return;
  if (seed.kind === "mirror") {
    if (mirrorAction(rb, meta, Date.now()) === "run") runMirror(tab, {quiet: true, prefetched: seed.first.mirror}).catch(() => {});
  } else if (readerAction(rb, meta, Date.now()) === "run") {
    runReader(tab, {quiet: true, prefetched: seed.first.reader}).catch(() => {});
  }
}

// Free the NAS-side tab (renderer memory). Fire-and-forget; the Hub also
// stops the whole container after a few idle minutes.
function releaseRemote(tab) {
  const rb = tab.rb;
  tab.rb = null;
  if (!rb) return;
  clearTimeout(rb.linksTimer);
  if (rb.chal) clearTimeout(rb.chal.timer);
  if (state.rbPolling === rb.id) { state.rbPolling = ""; state.rbPollMode = ""; state.rbGen++; }
  dropRemoteImage(rb);
  dropTiles(rb);
  dropReader(rb);
  dropMirror(rb);
  clearTimeout(rb.vmapTimer);
  state.rbIntercepts = state.rbIntercepts.filter(item => item.tab !== rb.id);
  if (rbPopup.item && rbPopup.item.tab === rb.id) hidePopupToast();
  closeProbeSheet();
  renderIntercept();
  api("/api/rb/tabs/close", {id: rb.id}).catch(() => {});
}

// Registers an openRemote() promise as the tab's open in flight, so that a second caller asking for the same URL
// (restore, autoReopen, route changes) gets this promise instead of opening again (v0.1.43, v0.1.54).
function trackOpening(tab, url, promise) {
  tab.opening = {url, promise};
  const clear = () => { if (tab.opening && tab.opening.promise === promise) tab.opening = null; };
  promise.then(clear, clear);
  return promise;
}

// (Re)open the current URL of the tab in the NAS browser.
async function openInRemote(tab) {
  const url = tab.current || (tab.history.length ? tab.history[tab.index] : "");
  if (!url) return;
  // v0.1.43: several callers (restore, autoReopen, route changes) may ask at once: one open only.
  if (tab.opening && tab.opening.url === url) return tab.opening.promise;
  await trackOpening(tab, url, openRemote(tab, url, u => { tab.history[tab.index] = u; }));
}

function remoteTab() {
  const tab = activeWebTab();
  return tab && tab.rb ? tab : null;
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
    } else if ((event.type === "text" || event.type === "key") && tabDisplay(tab) === "mirror") {
      scheduleMirror(tab, RB_INPUT_SNAP_MS, true);
    }
  } catch (e) {
    if (e.status === 404) remoteLost(tab);
    else toast(e.message, "error");
  }
}

const RB_LOST_TEXT = "NASブラウザのタブが閉じられました";
const RB_CRASH_QUIET_MS = 120000;

// The NAS browser stopped by itself (v0.1.38): its tabs are gone on the NAS side. Forget them
// quietly so that autoReopen opens the current URL again as soon as the tab is shown.
function resetTabsAfterCrash() {
  state.rbCrashAt = Date.now();
  for (const tab of state.tabs) {
    if (tab.rb) remoteLost(tab);
    if (tab.error === RB_LOST_TEXT) tab.error = "";
    tab.reopenAt = 0;
  }
  if (state.authed) updateChrome();
}

function remoteLost(tab) {
  if (tab.rb) {
    if (tab.rb.chal) clearTimeout(tab.rb.chal.timer);
    dropRemoteImage(tab.rb);
    dropTiles(tab.rb);
    dropReader(tab.rb);
    dropMirror(tab.rb);
    clearTimeout(tab.rb.vmapTimer);
    state.rbIntercepts = state.rbIntercepts.filter(item => item.tab !== tab.rb.id);
    renderIntercept();
  }
  tab.rb = null;
  // v0.1.42: a tab the NAS side closed (idle timeout, restart, memory) simply reopens its current
  // address when shown; the panel appears only when that reopen fails.
  tab.error = "";
  tab.reopenAt = 0;
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
  if (!loading && url !== "" && changed && !tab.diag) noteVisit(url, title);     // v0.1.62: recent pages (idempotent for the same page)
  if (typeof f.editable === "boolean") setEditable(tab, f.editable, false);
  if (Number.isFinite(f.popups) && f.popups !== rb.popups) { rb.popups = f.popups; changed = true; }
  if (f.blocked && f.blocked !== rb.blocked) toast(String(f.blocked), "error");
  rb.blocked = f.blocked || null;
  tab.loading = loading || Date.now() < (rb.tapUntil || 0);      // v0.1.60: a tap's progress line is not hidden early
  if (Number.isFinite(f.intercepts) && f.intercepts > rb.icptSeen) {
    fetchIntercepts(tab).catch(() => {});
  }
  if (typeof f.challenge === "boolean") syncChallenge(tab, f.challenge);
  if (f.probes) noteProbes(tab, f.probes);          // v0.1.65: the long-press analysis queue of this tab
  if (Number.isFinite(f.linksRev)) noteLinksRev(tab, f.linksRev);
  if (url !== "" && changed) applyDisplay(tab);
  if (changed) updateChrome();
}

// v0.1.52: a Cloudflare check page is not shown (no automatic switch to the live screencast, no spinner
// to watch). The tab keeps the display the user chose, a small status line says that the check is running,
// and the real page is rendered as soon as the check is gone. Only when the check is still there after
// CHAL_BUTTON_MS a button lets the user operate the check page in the live display, for that page only;
// the way back is applyDisplay (no history entry is ever added).
const CHAL_BUTTON_MS = 25000;

// True while the NAS page is a check page that must not be rendered.
function challengeHold(tab) {
  const rb = tab && tab.rb;
  return Boolean(rb && rb.chal && !rb.chal.live);
}

function syncChallenge(tab, challenge) {
  const rb = tab.rb;
  if (!rb) return;
  if (challenge) {
    if (!rb.chal) {
      rb.chal = {since: Date.now(), live: false, button: false, timer: 0};
      rb.chal.timer = setTimeout(() => {
        if (tab.rb !== rb || !rb.chal) return;
        rb.chal.button = true;
        updateChallengeUi(tab);
      }, CHAL_BUTTON_MS);
      dlog("render", "info", "challenge hold", {});
      gestureTrace("challenge.hold", {});
      updateChallengeUi(tab);
    }
    return;
  }
  if (!rb.chal) return;
  const was = rb.chal;
  clearTimeout(was.timer);
  rb.chal = null;
  dlog("render", "info", "challenge gone", {live: was.live});
  gestureTrace("challenge.gone", {live: was.live});
  updateChallengeUi(tab);
  if (was.live) { applyDisplay(tab); return; }      // back to the chosen display
  renderNow(tab);
}

// The real page, in the chosen display, right away (the loops' own early-start rules take over after this).
function renderNow(tab) {
  const rb = tab && tab.rb;
  if (!rb) return;
  const mode = tabDisplay(tab);
  if (mode === "mirror") { rb.mirrorPending = true; runMirror(tab, {quiet: true, force: true, immediate: true}).catch(() => {}); }
  else if (mode === "reader") runReader(tab, {quiet: true}).catch(() => {});
  else if (mode === "tiles") takeSnapshot(tab, {force: true, keepScroll: true, quiet: true}).catch(() => {});
}

function updateChallengeUi(tab) {
  const rb = tab && tab.rb;
  const on = Boolean(rb && rb.chal && !rb.chal.live && activeWebTab() === tab);
  setShown("chalStatus", on);
  setShown("chalLiveBtn", on && rb.chal.button === true);
}

// 「確認画面を操作する」: the live display for this check only; leaving it is automatic (syncChallenge).
function chalGoLive(tab) {
  const rb = tab && tab.rb;
  if (!rb || !rb.chal) return;
  rb.chal.live = true;
  updateChallengeUi(tab);
  applyDisplay(tab);
}

// The stage is the viewer's CSS viewport for the remote page.
function stageViewport() {
  const stage = $("remoteStage");
  const width = Math.round(stage.clientWidth), height = Math.round(stage.clientHeight) - chromeExtra();   // v0.1.60: not the collapsed size
  if (width < RB_STAGE_MIN_PX || height < RB_STAGE_MIN_PX) return null;
  const dpr = Math.min(3, Math.max(1, Number(window.devicePixelRatio) || 1));
  return {width: Math.min(1600, Math.max(320, width)), height: Math.min(2400, Math.max(320, height)),
    dpr: Math.round(dpr * 100) / 100};
}

async function sendViewport(tab) {
  const rb = tab.rb;
  const view = stageViewport();
  if (!rb || !view) return;
  rememberStage(view);
  const key = view.width + "x" + view.height + "@" + view.dpr + "/" + state.rbQuality;
  if (key === rb.vp) return;
  // v0.1.59: the size sent with the tab-open request was an estimate (the stage was not on screen yet): a height
  // that is a few pixels off does not matter for a copy or the reader text, so no extra request for it.
  const sent = rb.vpSent;
  rb.vpSent = null;
  const display = tabDisplay(tab);
  if (sent && sent.width === view.width && sent.dpr === view.dpr && sent.quality === state.rbQuality &&
      Math.abs(sent.height - view.height) <= 80 && (display === "mirror" || display === "reader")) {
    rb.vp = key;
    return;
  }
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

// ---- live display: WebSocket (v0.1.68), event stream (SSE, v0.1.46), MJPEG push (v0.1.30), polling ----
// 0. v0.1.68: /api/rb/stream.ws (wss:// on https): one binary message (a JPEG) per frame, decoded like the SSE frames.
//    A real iPhone behind `tailscale serve` got neither MJPEG nor SSE (the proxy buffers long responses) but reverse
//    proxies pass WebSocket upgrades through. No frame in RB_WS_FIRST_MS: the next transport.
// 1. SSE: /api/rb/stream.sse (fetch + ReadableStream, else EventSource), frames drawn into the same <img>.
// 2. MJPEG: the <img> points at /api/rb/stream.mjpg (same origin, cookie sent); the Hub pushes the newest frame.
// 3. Nothing delivers: frame.jpg polling as before.
// Metadata (address, title, loading, link-map revision, fps) still comes from the /api/rb/meta long-poll.
// A transport that failed on this device is skipped for a day (RB_LIVE_RETRY_MS), the one that worked is remembered.
// The stream is closed whenever the tab is hidden, switched or the mode changes.
let streamNonce = 0;
function streamUrl(rb) {
  return "/api/rb/stream.mjpg?tab=" + encodeURIComponent(rb.id) + "&n=" + (++streamNonce);
}
function sseUrl(rb) {
  return "/api/rb/stream.sse?tab=" + encodeURIComponent(rb.id) + "&n=" + (++streamNonce);
}

const LIVE_KIND_LABEL = {ws: "WebSocket", sse: "SSE", mjpeg: "MJPEG", poll: "ポーリング"};
// v0.1.68: the transports in the order they are tried. A reverse proxy such as `tailscale serve` buffered MJPEG and SSE
// on a real iPhone, but passes WebSocket upgrades through unbuffered, so WebSocket goes first.
const LIVE_ORDER = ["ws", "sse", "mjpeg"];

function wsUrl(rb) {
  return (location.protocol === "https:" ? "wss://" : "ws://") + location.host +
    "/api/rb/stream.ws?tab=" + encodeURIComponent(rb.id) + "&n=" + (++streamNonce);
}
function wsSupported() { return typeof WebSocket === "function" && typeof Blob === "function"; }

function sseFetchOk() {
  return typeof fetch === "function" && typeof AbortController === "function" && typeof TextDecoder === "function" &&
    typeof Response === "function" && "body" in Response.prototype;
}
function sseSupported() { return sseFetchOk() || typeof EventSource === "function"; }

function liveLoadPref() {
  try {
    const value = JSON.parse(readLocal(RB_LIVE_PREF_KEY) || "{}");
    return value && typeof value === "object" ? value : {};
  } catch (e) { return {}; }
}
function liveSavePref(patch) {
  writeLocal(RB_LIVE_PREF_KEY, JSON.stringify(Object.assign(liveLoadPref(), patch)));
}

function liveKindSupported(kind) {
  return kind === "ws" ? wsSupported() : kind === "sse" ? sseSupported() : kind === "mjpeg";
}
// A transport that did not deliver on this device is skipped for a day (then retried once). pref.fail = {kind: time}.
function liveFailedRecently(kind, pref = liveLoadPref()) {
  const at = Number(pref.fail && typeof pref.fail === "object" ? pref.fail[kind] : 0) || 0;
  return at > 0 && Date.now() - at <= RB_LIVE_RETRY_MS;
}
function liveMarkFailed(kind) {
  const pref = liveLoadPref();
  liveSavePref({fail: Object.assign({}, pref.fail && typeof pref.fail === "object" ? pref.fail : {}, {[kind]: Date.now()})});
}
function liveMarkWorked(kind) {
  const pref = liveLoadPref();
  const fail = Object.assign({}, pref.fail && typeof pref.fail === "object" ? pref.fail : {});
  delete fail[kind];
  liveSavePref({t: kind, fail});
}
// The first transport (WebSocket -> SSE -> MJPEG) that this browser can do and that did not fail today, else "poll".
function liveNextKind(after) {
  const pref = liveLoadPref();
  for (const kind of LIVE_ORDER.slice(after ? LIVE_ORDER.indexOf(after) + 1 : 0)) {
    if (liveKindSupported(kind) && !liveFailedRecently(kind, pref)) return kind;
  }
  return "poll";
}
// Which transport a live tab starts with.
function liveStartKind() { return liveNextKind(""); }

// Server-Sent Events text -> onEvent(name, data). Comments (": ping") are ignored.
function sseParser(onEvent) {
  let buffer = "";
  return chunk => {
    buffer += chunk;
    if (buffer.length > 8 * 1024 * 1024) buffer = "";
    for (;;) {
      const end = buffer.indexOf("\n\n");
      if (end < 0) return;
      const block = buffer.slice(0, end);
      buffer = buffer.slice(end + 2);
      let name = "message", data = "";
      for (const line of block.split("\n")) {
        if (line.startsWith("event:")) name = line.slice(6).trim();
        else if (line.startsWith("data:")) data += line.slice(5).trim();
      }
      if (data) onEvent(name, data);
    }
  };
}

function base64ToBlob(text) {
  const raw = atob(text);
  const bytes = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
  return new Blob([bytes], {type: "image/jpeg"});
}

// One connection to the event stream. onFrame(base64) per `frame` event; resolves when it ends
// (rejects on a network / HTTP error). `handle.close()` ends it. fetch + ReadableStream when the browser
// can, else EventSource.
function sseConnect(url, onFrame) {
  const handle = {close: () => {}};
  const done = new Promise((resolve, reject) => {
    if (sseFetchOk()) {
      const ctl = new AbortController();
      handle.close = () => ctl.abort();
      (async () => {
        try {
          const res = await fetch(url, {signal: ctl.signal, cache: "no-store", credentials: "same-origin"});
          if (!res.ok || !res.body) throw new Error("HTTP " + res.status);
          const reader = res.body.getReader();
          const decoder = new TextDecoder();
          const feed = sseParser((name, data) => { if (name === "frame") onFrame(data); });
          for (;;) {
            const {done: finished, value} = await reader.read();
            if (finished) break;
            feed(decoder.decode(value, {stream: true}));
          }
          resolve();
        } catch (e) {
          if (e && e.name === "AbortError") resolve(); else reject(e);
        }
      })();
    } else {
      const es = new EventSource(url, {withCredentials: true});
      handle.close = () => { es.close(); resolve(); };
      es.addEventListener("frame", event => onFrame(String(event.data || "")));
      es.onerror = () => { es.close(); reject(new Error("EventSource error")); };
    }
  });
  handle.done = done;
  return handle;
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
  if (s.kind === "ws") connectWs(s);
  else if (s.kind === "sse") connectSse(s);
  else $("remoteImg").src = streamUrl(rb);
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
  s.rb.present++;                   // a frame still decoding must not reappear after the stream is closed
  clearTimeout(s.timer);
  clearTimeout(s.retry);
  if (s.conn) { s.conn.close(); s.conn = null; }
  const img = $("remoteImg");
  img.onload = null;
  img.onerror = null;
  if (rbShown.owner === s.rb) {
    rbShown.owner = null;
    if (rbShown.url) URL.revokeObjectURL(rbShown.url);
    rbShown.url = "";
    img.removeAttribute("src");
    img.hidden = true;
  }
}

// The current transport did not deliver: WebSocket -> SSE -> MJPEG -> polling. `reason` is shown in the speed overlay.
function streamAdvance(tab, rb, reason) {
  const s = state.rbStream;
  const kind = s ? s.kind : "ws";
  rb.perf.fallback = reason;
  dlog("net", "warn", "stream fallback", {from: kind, reason});
  liveMarkFailed(kind);
  const next = liveNextKind(kind);
  if (next !== "poll") {
    openStream(tab, rb, next);
    return;
  }
  rb.streamFailed = true;
  liveSavePref({t: "poll"});
  closeStream();
  state.rbPollMode = "";             // make syncRemotePoll restart this tab with frame.jpg polling
  syncRemotePoll();
}

// SSE: (re)connect `s`; every frame is decoded off-screen and swapped in by presentRemoteFrame (at most two
// object URLs alive: the shown one and the one being decoded).
function connectSse(s) {
  if (s.conn) s.conn.close();
  const {rb} = s;
  const tab = remoteTabOf(rb);
  const conn = sseConnect(sseUrl(rb), data => {
    if (state.rbStream !== s || s.conn !== conn) return;
    let blob;
    try { blob = base64ToBlob(data); } catch (e) { return; }
    if (!s.loaded) {
      s.loaded = true;
      clearTimeout(s.timer);
      rb.perf.streamMs = performance.now() - s.opened;
      liveMarkWorked("sse");
      dlog("net", "info", "sse first frame", {ms: Math.round(rb.perf.streamMs)});
    }
    if (tab) presentRemoteFrame(tab, rb, blob).catch(() => {});
  });
  s.conn = conn;
  s.connAt = performance.now();
  conn.done.then(() => ended(false), () => ended(true));
  function ended(failed) {
    if (state.rbStream !== s || s.conn !== conn) return;
    const now = performance.now();
    s.errors = s.errors.filter(t => now - t < RB_STREAM_ERR_WINDOW_MS);
    if (failed || now - s.connAt < 2000) s.errors.push(now);      // an end right after the start is an error too
    dlog("net", failed ? "warn" : "info", "sse ended", {failed, count: s.errors.length});
    if (s.errors.length >= 2) { streamAdvance(remoteTabOf(rb), rb, "SSEのエラー"); return; }
    s.retry = setTimeout(() => { if (state.rbStream === s && s.conn === conn) connectSse(s); }, failed ? 500 : 50);
  }
}

// v0.1.68: one WebSocket to the live endpoint. onFrame(ArrayBuffer) per binary message. `done` resolves when it
// ends normally after it was open (the Hub's 10 minute limit, a close frame) and rejects when it never opened or
// ended abnormally. `close()` ends it quietly. Text messages from the Hub are ignored.
function wsConnect(url, onFrame) {
  const handle = {close: () => {}, opened: false};
  handle.done = new Promise((resolve, reject) => {
    let socket = null, ack = 0, finished = false;
    const finish = (ok, error) => {
      if (finished) return;
      finished = true;
      clearTimeout(ack);
      if (ok) resolve(); else reject(error);
    };
    try { socket = new WebSocket(url); } catch (e) { finish(false, e); return; }
    socket.binaryType = "arraybuffer";
    handle.close = () => {
      socket.onopen = socket.onmessage = socket.onerror = socket.onclose = null;
      try { socket.close(1000); } catch (e) { /* already closed */ }
      finish(true);
    };
    socket.onopen = () => {
      handle.opened = true;
      const beat = () => {
        try { if (socket.readyState === 1) socket.send("ack"); } catch (e) { /* closing */ }
        if (!finished) ack = setTimeout(beat, RB_WS_ACK_MS);
      };
      ack = setTimeout(beat, RB_WS_ACK_MS);
    };
    socket.onmessage = event => { if (event.data instanceof ArrayBuffer) onFrame(event.data); };
    socket.onerror = () => { /* the close event follows */ };
    socket.onclose = event => {
      // 4001: a newer socket replaced this one (the Hub's cap); 1006: never opened / dropped
      if (handle.opened && event.code !== 1006 && event.code !== 4001) finish(true);
      else finish(false, new Error("WebSocket " + (handle.opened ? "closed " : "failed ") + event.code));
    };
  });
  return handle;
}

// WebSocket: (re)connect `s`; frames are decoded off-screen like the SSE ones (presentRemoteFrame keeps at most two
// object URLs alive: the shown one and the one being decoded).
function connectWs(s) {
  if (s.conn) s.conn.close();
  const {rb} = s;
  const tab = remoteTabOf(rb);
  const conn = wsConnect(wsUrl(rb), buffer => {
    if (state.rbStream !== s || s.conn !== conn) return;
    if (!s.loaded) {
      s.loaded = true;
      clearTimeout(s.timer);
      rb.perf.streamMs = performance.now() - s.opened;
      liveMarkWorked("ws");
      dlog("net", "info", "ws first frame", {ms: Math.round(rb.perf.streamMs)});
    }
    s.frames = (s.frames || 0) + 1;
    if (tab) presentRemoteFrame(tab, rb, new Blob([buffer], {type: "image/jpeg"})).catch(() => {});
  });
  s.conn = conn;
  s.connAt = performance.now();
  conn.done.then(() => ended(false), () => ended(true));
  function ended(failed) {
    if (state.rbStream !== s || s.conn !== conn) return;
    const now = performance.now();
    s.errors = s.errors.filter(t => now - t < RB_STREAM_ERR_WINDOW_MS);
    if (failed || now - s.connAt < 2000) s.errors.push(now);      // an end right after the start is an error too
    dlog("net", failed ? "warn" : "info", "ws ended", {failed, count: s.errors.length});
    if (s.errors.length >= 2) { streamAdvance(remoteTabOf(rb), rb, "WebSocketのエラー"); return; }
    s.retry = setTimeout(() => { if (state.rbStream === s && s.conn === conn) connectWs(s); }, failed ? 500 : 50);
  }
}

function remoteTabOf(rb) {
  return state.tabs.find(t => t.rb === rb) || null;
}

function openStream(tab, rb, kind = "ws") {
  closeStream();
  dropRemoteImage(rb);
  const img = $("remoteImg");
  const s = {rb, kind, opened: performance.now(), loaded: false, errors: [], timer: 0, retry: 0, stalledSince: 0,
    arm: null, conn: null, connAt: 0};
  state.rbStream = s;
  rb.stream = s;
  rbShown.owner = rb;
  rbShown.url = "";
  const limit = kind === "ws" ? RB_WS_FIRST_MS : kind === "sse" ? RB_SSE_FIRST_MS : RB_STREAM_FIRST_MS;
  const shown = () => {
    if (state.rbStream !== s) return;
    if (!s.loaded) {
      s.loaded = true;
      clearTimeout(s.timer);
      rb.perf.streamMs = performance.now() - s.opened;
      liveMarkWorked("mjpeg");
      dlog("net", "info", "stream first frame", {ms: Math.round(rb.perf.streamMs)});
    }
    img.hidden = false;
    $("remoteIdle").hidden = true;
  };
  // iOS Safari draws each part of a multipart stream but may never fire "load" (the response
  // never ends), so the first frame is also detected by the decoded size (v0.1.34).
  const arm = () => {
    clearTimeout(s.timer);
    const deadline = performance.now() + limit;
    const check = () => {
      if (state.rbStream !== s || s.loaded) return;
      if (kind === "mjpeg" && img.naturalWidth > 0 && img.currentSrc.indexOf("/api/rb/stream.mjpg") >= 0) { shown(); return; }
      if (performance.now() >= deadline) {
        streamAdvance(tab, rb, kind === "ws" ? "WebSocketの最初の画面が届かない" : kind === "sse" ? "SSEの最初の画面が届かない" : "MJPEGの最初の画面が届かない");
        return;
      }
      s.timer = setTimeout(check, 250);
    };
    s.timer = setTimeout(check, 250);
  };
  s.arm = arm;
  if (kind === "ws") {
    dlog("net", "info", "ws open", {});
    arm();
    connectWs(s);
    return;
  }
  if (kind === "sse") {
    dlog("net", "info", "sse open", {});
    arm();
    connectSse(s);
    return;
  }
  img.onload = shown;
  img.onerror = () => {
    if (state.rbStream !== s) return;
    const now = performance.now();
    s.errors = s.errors.filter(t => now - t < RB_STREAM_ERR_WINDOW_MS);
    s.errors.push(now);
    dlog("net", "warn", "stream error", {count: s.errors.length});
    if (s.errors.length >= 2) { streamAdvance(tab, rb, "MJPEGのエラー"); return; }
    s.loaded = false;
    s.opened = now;
    arm();
    s.retry = setTimeout(() => { if (state.rbStream === s) img.src = streamUrl(rb); }, 500);
  };
  dlog("net", "info", "stream open", {});
  arm();
  img.src = streamUrl(rb);
}

async function streamLoop(tab, gen, kind = "ws") {
  let fails = 0, first = true;
  await nextPaint();
  await nextPaint();
  if (gen !== state.rbGen || !tab.rb) return;
  await sendViewport(tab).catch(() => {});
  if (gen !== state.rbGen || !tab.rb) return;
  openStream(tab, tab.rb, kind);
  while (gen === state.rbGen && tab.rb) {
    const rb = tab.rb;
    try {
      let target = "/api/rb/meta?tab=" + encodeURIComponent(rb.id) + "&wait=" + (first ? 0 : RB_STREAM_META_S);
      if (!first && rb.rev !== null) target += "&rev=" + rb.rev + probeQuery(rb);
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
      await delay(1000);
    }
  }
}

// One long-poll loop, only for the visible remote tab while the page is visible.
// Stopping the loop lets the Hub stop the screencast (5 s) and later the container.
function syncRemotePoll() {
  const tab = activeWebTab();
  const want = Boolean(state.authed && tab && tab.rb &&
    document.visibilityState === "visible" && currentViewName() === "web");
  const id = want ? tab.rb.id : "";
  const mode = want ? tabDisplay(tab) : "";
  if (id === state.rbPolling && mode === state.rbPollMode) return;
  if (want) dlog("render", "info", "display mode", {mode, why: displayReason(tab)});
  closeStream();
  state.rbPolling = id;
  state.rbPollMode = mode;
  state.rbGen++;
  if (!want) return;
  const rb = tab.rb;
  if (rb.shown && rb.shown !== mode) leaveDisplay(rb, rb.shown);
  rb.shown = mode;
  if (mode === "live") {
    const kind = rb.streamFailed || typeof Image !== "function" ? "poll" : liveStartKind();
    if (kind === "poll") {
      if (!rb.streamFailed) { rb.streamFailed = true; rb.perf.fallback = "前回ストリームが届かなかったため"; }
      remoteLoop(tab, state.rbGen);
    } else streamLoop(tab, state.rbGen, kind).catch(() => {});
    scheduleViewportLinks(tab, true);      // the link map of the live picture (v0.1.30)
  } else if (mode === "reader") {
    readerLoop(tab, state.rbGen).catch(() => {});
  } else if (mode === "mirror") {
    mirrorLoop(tab, state.rbGen).catch(() => {});
  } else {
    tileLoop(tab, state.rbGen).catch(() => {});
  }
}

function updateRemoteView(tab) {
  const remote = Boolean(tab && tab.history.length);
  if (remote) ensureRemote();
  setShown("remoteView", remote);
  if (!(remote && tab.pt)) { setShown("ptUnavailable", false); setShown("ptWrap", false); if (ptHost.tab) ptDrop(); }   // v0.1.57
  if (!remote) { closeRemoteSheet(); setShown("interceptSheet", false); return; }
  if (tab.pt) { updatePtView(tab); return; }
  const live = Boolean(tab.rb);
  const display = live ? tabDisplay(tab) : "tiles";
  const tiles = live && display === "tiles";
  const reader = live && display === "reader";
  const mirror = live && display === "mirror";
  setShown("tileScroll", tiles);
  setShown("readerScroll", reader);
  setShown("mirrorWrap", mirror && (mirrorHost.owner === tab.rb || tab.rb.mirrorBusy));
  syncReaderProgress();
  if (tiles) mountTiles(tab);
  if (reader) mountReader(tab);
  syncSnapProgress();
  $("remoteImg").hidden = tiles || reader || mirror || !live || !$("remoteImg").getAttribute("src");
  $("remoteIdle").hidden = live && (tiles ? Boolean(tab.rb.view) :
    reader ? Boolean(tab.rb.reader && tab.rb.reader.data) :
    mirror ? Boolean(mirrorHost.owner === tab.rb && tab.rb.mirror) : !$("remoteImg").hidden);
  if (!live || display !== "live") paintLiveBoxes(null);
  // A tab closed to save memory reopens by itself when it is shown again (v0.1.32);
  // the button only appears when that automatic reopen failed.
  const reopening = !live && autoReopen(tab);
  // v0.1.35: one clear state when the NAS browser cannot show the page (not set up, or it failed).
  const blocked = !live && !reopening && Boolean(tab.notReady || tab.error || state.rbReady === false);
  setShown("rbNotReady", blocked);
  if (blocked) {
    // Not set up → the way to Settings; set up but this page failed → just try again (v0.1.42).
    const setup = Boolean(tab.notReady || state.rbReady === false);
    $("rbNotReadyTitle").textContent = setup ? "NASブラウザが準備できていません" : "ページを開けませんでした";
    $("rbNotReadyText").textContent = tab.error ||
      (setup ? "初期設定（イメージの作成）が済んでいません。設定 › ブラウザで準備してください。" : "");
    $("rbNotReadyBtn").textContent = setup ? "設定 › ブラウザを開く" : "もう一度開く";
    $("rbNotReadyBtn").dataset.mode = setup ? "setup" : "retry";
  }
  if (!live) $("remoteIdle").hidden = blocked;
  $("remoteIdleText").textContent = "画面を待っています…";
  setShown("remoteLoading", Boolean(tab.loading));
  updateChallengeUi(tab);
  const count = live ? tab.rb.links.length : 0;
  setShown("remoteLinks", count > 0);
  $("remoteLinks").textContent = "リンク " + count + "件";
  const blockedPopups = live ? tab.rb.popups : 0;
  setShown("remotePopups", blockedPopups > 0);
  $("remotePopups").textContent = "ポップアップ " + blockedPopups + "件ブロック";
  renderIntercept();
}

const RB_AUTO_REOPEN_MS = 30000;
function autoReopen(tab) {
  if (tab.reopening) return true;
  if (document.hidden || activeWebTab() !== tab) return false;
  if (tab.notReady || tab.error || state.rbReady === false) return false;   // the tab shows why; reload retries
  const now = Date.now();
  if (tab.reopenAt && now - tab.reopenAt < RB_AUTO_REOPEN_MS) return false;   // failed recently: show the button
  tab.reopenAt = now;
  tab.reopening = true;
  tab.error = "";
  perform(async () => {
    if (await reattachRemote(tab)) { saveTabs(); refreshAfterTabChange(); return; }
    await openInRemote(tab);
  }).finally(() => {
    tab.reopening = false;
    if (tab.rb) tab.reopenAt = 0;
    if (activeWebTab() === tab) updateRemoteView(tab);
  });
  return true;
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
  if (state.tabs.length < webTabLimit()) {
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
let dbgFrames = 0, dbgFramesAt = performance.now();
function recordPerf(bytes, latency) {
  if (state.debug && state.debug.enabled && bytes > 0) dbgFrames++;
  if (!state.rbPerf) return;
  const now = performance.now();
  state.rbPerfSamples.push({t: now, bytes, latency});
  const cutoff = now - RB_PERF_WINDOW_MS;
  while (state.rbPerfSamples.length && state.rbPerfSamples[0].t < cutoff) state.rbPerfSamples.shift();
}

// The overlay text: the display's speed line, then (v0.1.54) the first-load breakdown of this tab's newest open.
function perfLine(tab = remoteTab()) {
  const first = firstLoadLine(tab && tab.rb);
  const text = perfText(tab);
  return first ? text + "\n初回: " + first : text;
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
  if (display === "mirror") {
    const m = p.mirror || {};
    const head = "ミラー · 要素 " + (m.nodes || 0) + " · HTML " + kb(m.htmlBytes || 0) + " · CSS " + kb(m.cssBytes || 0) +
      " · 画像 " + (m.images || 0) + " · 取得 " + Math.round(m.ms || 0) + " ms";
    const lines = mirrorDiagLines(rb);
    return lines.length ? head + "\n" + lines.join("\n") : head;
  }
  if (display === "reader") {
    const blocks = rb.reader && rb.reader.data ? rb.reader.data.blocks : [];
    const total = blocks.filter(b => b.t === "img").length;
    const each = p.imgs ? p.imgMs / p.imgs : 0;
    return "リーダー · ブロック " + blocks.length + " · 画像 " + (p.imgs || 0) + "/" + total +
      " · 取得 " + Math.round(p.extractMs || 0) + " ms" + (p.imgs ? " · 画像取得 " + Math.round(each) + " ms" : "");
  }
  if (rb && rb.stream && !rb.streamFailed) {
    return "ライブ(" + LIVE_KIND_LABEL[rb.stream.kind === "ws" || rb.stream.kind === "sse" ? rb.stream.kind : "mjpeg"] + (p.fallback ? ": " + p.fallback : "") + ")・" +
      quality + " · " + (p.fps || 0).toFixed(1) + " fps · " +
      kb(p.frameBytes || 0) + "/枚 · 応答 " + Math.round(p.streamMs || 0) + " ms";
  }
  const now = performance.now();
  const recent = state.rbPerfSamples.filter(x => x.t >= now - RB_PERF_WINDOW_MS);
  const frames = recent.filter(x => x.bytes > 0);
  const elapsed = Math.max(1, Math.min(RB_PERF_WINDOW_MS, now - state.rbPerfSince) / 1000);
  const fps = frames.length / elapsed;
  const bytes = frames.length ? frames.reduce((sum, x) => sum + x.bytes, 0) / frames.length : 0;
  const wait = recent.length ? recent.reduce((sum, x) => sum + x.latency, 0) / recent.length : 0;
  return "ライブ(ポーリング" + (p.fallback ? ": " + p.fallback : "") + ")・" + quality + " · " + fps.toFixed(1) + " fps · " + kb(bytes) +
    "/枚 · 応答 " + Math.round(wait) + " ms";
}

// v0.1.73: the mirror's detailed figures (speed line and self-test report)
function mirrorDiagSummary(rb) {
  const d = rb && rb.mirrorDiag;
  if (!d) return null;
  let doc = null;
  try { doc = mirrorHost.owner === rb ? mirrorDoc() : null; } catch (e) { doc = null; }
  const res = doc ? mirrorResEntries(doc) : {total: 0, tokens: 0, refetch: 0, cached: 0, kb: 0, slowest: 0};
  let pending = 0;
  if (doc) for (const img of Array.from(doc.images || [])) if (!img.complete) pending++;
  return {copies: d.copies, fresh: d.fresh, morphs: d.morphs, skipped: d.skipped, docs: d.docs,
    refetch: d.refetchBefore + res.refetch, res, pending, last: d.last, patch: d.patch || null, srv: d.srv};
}

function mirrorDiagLines(rb) {
  const s = mirrorDiagSummary(rb);
  if (!s || !s.last) return [];
  const l = s.last, p = l.patch || s.patch;
  const out = [];
  out.push("複製 " + (l.cap === null ? "?" : l.cap) + " ms（ページ内 " + (l.js === null ? "?" : l.js) + " ms）· 受信 " +
    (l.early ? "同送" : l.api + " ms") + " · " + (l.load ? "読込 " + l.load + " ms" : "反映 " + l.apply + " ms") +
    (l.partial ? " · 上部のみ" : "") + (l.reused ? " · 再利用" : ""));
  if (p) {
    out.push("差分: 保持 " + p.kept + " · 追加 " + p.added + " · 削除 " + p.removed + " · 属性 " + p.attrs + " · 文字 " + p.text +
      (p.css ? " · CSS更新" : "") + (p.fallback ? " · 全置換" : ""));
  }
  out.push("コピー " + s.copies + "回（文書 " + s.docs + " · 差分 " + s.morphs + " · 変化なし " + s.skipped + "）");
  const r = s.res;
  out.push("端末: 画像要求 " + r.total + "（" + r.tokens + "種）· キャッシュ " + r.cached + " · 再取得 " + s.refetch +
    " · 未完了 " + s.pending + " · " + r.kb + " KB · 最長 " + r.slowest + " ms");
  const v = s.srv;
  if (v) {
    out.push("Hub: 配信 " + (v.served || 0) + " · 保持から " + (v.hub_hits || 0) + " · 取得 " + (v.fetched || 0) +
      "（" + (v.fetch_ms || 0) + " ms）· 304 " + (v.not_modified || 0) + " · 失敗 " + (v.failed || 0) + " · " + (v.kb || 0) + " KB");
  }
  return out;
}

function renderPerf() {
  $("perfOverlay").textContent = perfLine();
}

function setPerf(on) {
  state.rbPerf = on;
  state.rbPerfSamples = [];
  state.rbPerfSince = performance.now();
  setShown("perfOverlay", on);
  if (on) renderPerf();
  sched.kick();
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
  if (tab.diag && tab.diag.mode) return tab.diag.mode;     // v0.1.52 swipe diagnostic: its own display mode
  const remembered = DISPLAY_OF_MODE[rememberedMode(hostOf(tab.current || ""))];
  if (remembered) return remembered;
  const setting = state.settings && state.settings.rb_default_mode;
  return DISPLAY_OF_SETTING[setting] || "tiles";
}

// Why the display is what it is (debug log only).
function displayReason(tab) {
  const rb = tab && tab.rb;
  if (!rb) return "no-rb";
  if (rb.chal && rb.chal.live) return "challenge-live(user)";
  if (rb.tilesFor && rb.tilesFor === rb.url) return "reader-or-mirror-failed->tiles";
  if (DISPLAY_OF_MODE[rememberedMode(hostOf(tab.current || ""))]) return "site-choice";
  return "setting";
}

// What is shown now: "tiles" (fast), "live", "reader" or "mirror". A Cloudflare check forces "live" meanwhile.
function tabDisplay(tab) {
  const rb = tab && tab.rb;
  if (!rb) return "tiles";
  if (rb.chal && rb.chal.live) return "live";
  if (rb.tilesFor && rb.tilesFor === rb.url) return "tiles";   // reader / mirror failed on this page (not persisted)
  return userDisplay(tab);
}

// 「表示: リーダー / 高速 / ライブ」 from the menu: remembered per host.
function setUserDisplay(tab, mode) {
  if (tab && mode === "pt") {                                                              // v0.1.57
    perform(async () => {
      await enterPt(tab);
      if (tab.pt) { const host = hostOf(tab.current || ""); if (host) rememberHostMode(host, REMOTE_PT_MODE); renderRemoteHosts(); }   // v0.1.62
    });
    return;
  }
  if (tab && tab.pt && MODE_OF_DISPLAY[mode]) { leavePt(tab, mode); return; }
  if (!tab || !tab.rb || !RB_DISPLAY_LABELS[mode]) return;
  const host = hostOf(tab.current || "");
  if (host) rememberHostMode(host, MODE_OF_DISPLAY[mode]);
  if (tab.rb.chal) tab.rb.chal.live = false;           // a manual choice is respected
  tab.rb.tilesFor = ""; tab.rb.readerFail = null; tab.rb.mirrorFail = null;
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
  } else if (shown === "mirror") {
    dropMirror(rb);
    api("/api/rb/mirror/release", {tab: rb.id}).catch(() => {});
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
  dlog("render", "info", "tile mount", {tiles: rb.view ? rb.view.tiles.length : 0});
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
  if (!rb || tabDisplay(tab) !== "tiles" || challengeHold(tab)) return;
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
    dlog("render", "debug", "tile snapshot (client)", {ms: Math.round(rb.perf.snapMs)});
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
      const wait = rb.loading === true && rb.snapEarly !== rb.url ? RB_EARLY_POLL_S : RB_META_WAIT_S;
      let target = "/api/rb/meta?tab=" + encodeURIComponent(rb.id) + "&wait=" + wait;
      if (rb.rev !== null) target += "&rev=" + rb.rev + probeQuery(rb);
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
      const act = tilesAction(rb, meta, unchanged, Date.now());
      if (act === "snap") takeSnapshot(tab, {keepScroll: true, quiet: true}).catch(() => {});
      else if (act === "snap-force") takeSnapshot(tab, {force: true, keepScroll: true, quiet: true}).catch(() => {});
      else if (act === "snap-changed") takeSnapshot(tab, {ifChanged: true, keepScroll: true, quiet: true}).catch(() => {});
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
// `view` is where the touches are heard and `top()` the scroll position (the mirror display listens inside its iframe).
function wirePull(scroller, onRefresh, view = scroller, top = () => scroller.scrollTop) {
  let startY = null;
  view.addEventListener("touchstart", event => {
    startY = top() <= 0 && event.touches.length === 1 ? event.touches[0].clientY : null;
  }, {passive: true});
  view.addEventListener("touchend", event => {
    const start = startY;
    startY = null;
    if (start === null || !event.changedTouches.length) return;
    if (event.changedTouches[0].clientY - start > RB_PULL_PX && top() <= 0) onRefresh();
  }, {passive: true});
}

function wireTiles() {
  const scroller = $("tileScroll");
  let chromeTick = 0;      // v0.1.60: the fast display scrolls natively: it drives the bar collapse too
  scroller.addEventListener("scroll", () => {
    if (chromeTick) return;
    chromeTick = requestAnimationFrame(() => {
      chromeTick = 0;
      chromeScrolled(scroller.scrollTop, scroller.scrollHeight - scroller.clientHeight, "tiles");
    });
  }, {passive: true});
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
  // v0.1.60: the floating re-capture button is gone (pull down at the top, or ⟳ in the bar / menu).
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
  }).slice(0, 5000).map(b => (b.t === "p" && b.q !== undefined) ? Object.assign({}, b, {q: b.q === true}) : b);
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
  if (!e.url || e.pinned) return;
  if (!e.fig.style.aspectRatio) e.fig.style.height = e.fig.offsetHeight + "px";   // keep the layout stable
  URL.revokeObjectURL(e.url);
  e.url = "";
  e.fig.classList.remove("is-loaded");
  e.img.removeAttribute("src");
}

function clearReaderBody() {
  closeReaderViewer();
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

// Image bytes of a reader picture through the Hub (never the page's own URL). null on any failure.
async function fetchReaderBlob(rb, index) {
  const res = await fetch("/api/rb/reader/img?tab=" + encodeURIComponent(rb.id) + "&i=" + index,
    {credentials: "same-origin", cache: "no-store"});
  if (res.status === 401 && state.authed) { state.authed = false; setTimeout(go, 0); return null; }
  if (res.status !== 200) return null;
  const blob = await res.blob();
  if (!blob.size || !/^image\/(jpeg|png|gif|webp|avif)$/.test(blob.type)) return null;
  return blob;
}

async function fetchReaderImage(e) {
  const began = performance.now();
  try {
    const blob = await fetchReaderBlob(e.rb, e.index);
    if (e.dead) return;
    if (!blob) {
      e.failedAt = Date.now();
      e.fig.classList.add("is-failed");
      return;
    }
    if (e.dead || !e.want || e.url) return;
    e.rb.perf.imgs = (e.rb.perf.imgs || 0) + 1;
    e.rb.perf.imgMs = (e.rb.perf.imgMs || 0) + (performance.now() - began);
    e.url = URL.createObjectURL(blob);
    e.fig.classList.remove("is-failed");
    e.fig.style.height = "";
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

// Long link texts (file names, raw URLs, ad-redirect addresses) break the reader's lines.
// v0.1.35: inline they are shortened in the middle (start and extension stay readable);
// a block that is only such links with short labels becomes a list of link cards.
const READER_LONG_TOKEN = 28;
const READER_SIZE_RE = /^\s*[(（［\[]?\s*(\d[\d.,]*\s*[KMGT]i?B)\s*[)）］\]]?/i;

// "Long" = a raw URL or a run of 24+ URL / file-name characters (Japanese prose has no spaces,
// so length alone would wrongly turn ordinary link sentences into cards).
const READER_LONG_RUN = /[A-Za-z0-9._~%+=&?#:\/\\@!$'*,;\[\]{}-]{24,}/;
const READER_TRAIL_SIZE = /\s*[(（［\[]\s*(\d[\d.,]*\s*[KMGT]i?B)\s*[)）］\]]\s*$/i;
const READER_ONLY_SIZE = /^\s*[(（［\[]?\s*(\d[\d.,]*\s*[KMGT]i?B)\s*[)）］\]]?\s*$/i;

function readerLongText(text) {
  const t = text.trim();
  return /^https?:\/\//i.test(t) || READER_LONG_RUN.test(t);
}

function middleEllipsis(text, max) {
  const t = text.trim();
  if (t.length <= max) return t;
  const tail = Math.min(18, Math.floor(max / 3));
  return t.slice(0, max - tail - 1) + "…" + t.slice(-tail);
}

function readerHostOf(href) {
  try { return new URL(href).hostname.replace(/^www\./, ""); } catch (e) { return ""; }
}

function readerLabel(text) {
  return text.replace(/[|｜:：・\-–—]+/g, " ").replace(/\s+/g, " ").trim();
}

// Splits a block into [{label, text, href, size}] when it is a set of long links with short
// labels ("RapidGator | name.rar (66.76 MB)"), else null (ordinary prose keeps inline links).
function readerCardItems(runs) {
  const links = runs.filter(r => r.href);
  if (!links.length || links.length > 30 || !links.every(r => readerLongText(r.text))) return null;
  const items = [];
  let pending = "";
  for (const r of runs) {
    if (r.href) {
      // The size is often part of the link text itself: "name.rar (66.76 MB)".
      const own = READER_TRAIL_SIZE.exec(r.text);
      const text = own ? r.text.slice(0, own.index) : r.text;
      items.push({label: readerLabel(pending), text: text.trim() || r.text.trim(), href: r.href,
        size: own ? own[1].replace(/\s+/g, " ") : ""});
      pending = "";
      continue;
    }
    let text = r.text;
    const last = items[items.length - 1];
    const m = last && !last.size && !pending ? READER_SIZE_RE.exec(text) : null;
    if (m) { last.size = m[1].replace(/\s+/g, " "); text = text.slice(m[0].length); }
    pending += text;
  }
  if (readerLabel(pending).length > 16) return null;          // trailing prose: not a link list
  if (items.some(it => it.label.length > 24)) return null;
  return items;
}

function readerCards(items) {
  const list = element("div", undefined, "reader-cards");
  for (const it of items) {
    const a = readerLink("", it.href);
    a.className = "reader-link reader-card";
    a.setAttribute("aria-label", (it.label ? it.label + ": " : "") + it.text);
    const top = element("span", undefined, "reader-card-top");
    const host = readerHostOf(it.href);
    top.appendChild(element("span", it.label || host, "reader-card-label"));
    if (it.size) top.appendChild(element("span", it.size, "reader-card-size"));
    a.appendChild(top);
    a.appendChild(element("span", middleEllipsis(it.text, 56), "reader-card-title"));
    if (it.label && host) a.appendChild(element("span", host, "reader-card-host"));
    list.appendChild(a);
  }
  return list;
}

function readerRuns(parent, runs) {
  for (const run of runs) {
    if (run.href && readerLongText(run.text)) {
      const a = readerLink(middleEllipsis(run.text, 40), run.href);
      a.title = run.text.trim();
      parent.appendChild(a);
    } else if (run.href) parent.appendChild(readerLink(run.text, run.href));
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
  const sized = Number.isFinite(block.w) && Number.isFinite(block.h) && block.w > 0 && block.h > 0;
  if (sized) {
    const w = Math.min(block.w, 20000), h = Math.min(block.h, 20000);
    figure.style.aspectRatio = w + " / " + h;           // the skeleton has the final shape: no layout shift
    figure.style.setProperty("--w", w + "px");
    if (w < 200) figure.classList.add("is-tiny");
  } else {
    figure.classList.add("is-unsized");
  }
  const entry = {rb, img, fig: figure, index: block.i, alt: img.alt, url: "", want: false, dead: false, queued: false,
    failedAt: 0, pinned: false};
  img.addEventListener("load", () => {
    if (!entry.url) return;
    figure.style.height = "";
    if (img.naturalWidth > 0 && img.naturalHeight > 0) figure.style.aspectRatio = img.naturalWidth + " / " + img.naturalHeight;
    figure.classList.add("is-loaded");
  });
  img.addEventListener("error", () => { if (entry.url) figure.classList.add("is-failed"); });
  readerHost.map.set(img, entry);
  readerHost.entries.add(entry);
  readerObservers();
  if (readerHost.near) { readerHost.near.observe(img); readerHost.far.observe(img); }
  else { entry.want = true; queueReaderImage(entry); }
  const linked = typeof block.href === "string" && /^https?:\/\//i.test(block.href);
  if (linked) {
    const a = readerLink("", block.href);
    a.classList.add("reader-imglink");
    a.appendChild(img);
    figure.appendChild(a);
  } else {
    figure.appendChild(img);
    figure.classList.add("can-zoom");
    img.tabIndex = 0;
    img.setAttribute("role", "button");
  }
  // "表示しない": a one-line placeholder that loads the picture when tapped.
  const hold = element("button", undefined, "reader-hold");
  hold.type = "button";
  hold.appendChild(icon("image"));
  const text = element("span", undefined, "reader-hold-text");
  text.appendChild(document.createTextNode(img.alt ? "画像: " + img.alt : "画像"));
  text.appendChild(element("small", "タップで読み込み"));
  hold.appendChild(text);
  figure.appendChild(hold);
  if (linked) {
    // a linked picture opens its link when tapped, so the viewer has its own button
    const zoom = element("button", undefined, "reader-zoom");
    zoom.type = "button";
    zoom.setAttribute("aria-label", "画像を拡大表示");
    zoom.appendChild(icon("search"));
    figure.appendChild(zoom);
  }
  return figure;
}

// Rows made almost only of short links (page numbers « 1 2 3 … », tags) are hard to tap as
// inline text: they are shown as large buttons instead (v0.1.34).
const READER_ROW_PUNCT = /[\s\u00a0…·|,.、。\/«»<>‹›\-–—:;()（）\[\]［］]+/g;
function readerLinkRow(runs) {
  let links = 0, plain = "";
  for (const r of runs) {
    if (r.href) {
      if (r.text.trim().length > 12) return false;
      links++;
    } else plain += r.text;
  }
  return links >= 3 && plain.replace(READER_ROW_PUNCT, "").length <= 8;
}

function readerChips(runs) {
  const row = element("div", undefined, "reader-chips");
  for (const r of runs) {
    if (r.href) {
      const a = readerLink(r.text.trim() || r.href, r.href);
      a.classList.add("reader-chip");
      row.appendChild(a);
      continue;
    }
    for (const token of r.text.split(/\s+/)) {
      if (token) row.appendChild(element("span", token, "reader-chip is-plain"));
    }
  }
  return row;
}

// ---- v0.1.36: reader appearance (per device; every storage access is guarded) ----
// One JSON object under qd.readerPrefs. Unknown / out-of-range values fall back to the default per key,
// so a damaged or older value never breaks the reader. The v0.1.34 four-step size (qd.readerSize) migrates.
const READER_PREFS_KEY = "qd.readerPrefs";
const READER_SIZE_KEY = "qd.readerSize";
const READER_SIZE_STEPS = [13, 14, 16, 18, 20, 22, 24, 26];
const READER_OLD_SIZES = [14, 16, 19, 23];
const READER_LEADING = [1.6, 1.85, 2.1];
const READER_WIDTHS = [30, 36, 44];
const READER_CHOICES = {font: ["gothic", "mincho"], theme: ["auto", "light", "sepia", "dark", "black"],
  images: ["full", "normal", "small", "none"]};
const READER_FLAGS = ["justify", "group", "headings", "progress"];
const READER_DEFAULTS = Object.freeze({size: 16, font: "gothic", theme: "auto", leading: 1, width: 1, justify: true,
  images: "normal", group: true, headings: true, progress: true});

function readerNearestStep(px) {
  let best = 16;
  for (const step of READER_SIZE_STEPS) if (Math.abs(step - px) < Math.abs(best - px)) best = step;
  return best;
}

function readerPrefsClean(raw) {
  const out = Object.assign({}, READER_DEFAULTS);
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return out;
  if (READER_SIZE_STEPS.includes(raw.size)) out.size = raw.size;
  for (const key of Object.keys(READER_CHOICES)) if (READER_CHOICES[key].includes(raw[key])) out[key] = raw[key];
  if (Number.isInteger(raw.leading) && raw.leading >= 0 && raw.leading < READER_LEADING.length) out.leading = raw.leading;
  if (Number.isInteger(raw.width) && raw.width >= 0 && raw.width < READER_WIDTHS.length) out.width = raw.width;
  for (const key of READER_FLAGS) if (typeof raw[key] === "boolean") out[key] = raw[key];
  return out;
}

function readerPrefsLoad() {
  let raw = null;
  try {
    const text = localStorage.getItem(READER_PREFS_KEY);
    if (text) raw = JSON.parse(text);
  } catch (e) { raw = null; }
  if (raw === null) {
    try {
      const old = localStorage.getItem(READER_SIZE_KEY);
      const index = old === null || old === "" ? NaN : Number(old);
      if (Number.isInteger(index) && index >= 0 && index < READER_OLD_SIZES.length) {
        raw = {size: readerNearestStep(READER_OLD_SIZES[index])};
      }
    } catch (e) { raw = null; }
  }
  return readerPrefsClean(raw);
}

function readerPrefsSave(prefs) {
  try { localStorage.setItem(READER_PREFS_KEY, JSON.stringify(prefs)); } catch (e) { /* keep for this page only */ }
}

let readerPrefs = readerPrefsClean(null);

function syncReaderProgress() {
  const on = readerPrefs.progress && !$("readerScroll").hidden;
  $("readerProgress").hidden = !on;
  if (on) updateReaderProgress();
}

function updateReaderProgress() {
  const s = $("readerScroll");
  const max = s.scrollHeight - s.clientHeight;
  const f = max > 0 ? Math.max(0, Math.min(1, s.scrollTop / max)) : 0;
  $("readerProgressFill").style.transform = "scaleX(" + f.toFixed(4) + ")";
  $("readerProgress").setAttribute("aria-valuenow", String(Math.round(f * 100)));
}

function applyReaderPrefs() {
  const p = readerPrefs, el = $("readerScroll");
  el.style.fontSize = p.size + "px";
  el.style.setProperty("--rd-lh", String(READER_LEADING[p.leading]));
  el.style.setProperty("--rd-w", READER_WIDTHS[p.width] + "em");
  el.dataset.font = p.font;
  el.dataset.theme = p.theme;
  el.dataset.images = p.images;
  el.classList.toggle("rd-justify", p.justify);
  el.classList.toggle("rd-heads", p.headings);
  syncReaderProgress();
}

// Layout-changing settings keep the reading place by position in the document.
const READER_RELAYOUT = ["size", "font", "leading", "width", "headings", "images", "justify"];
function setReaderPrefs(next) {
  const s = $("readerScroll");
  const before = readerPrefs;
  const ratio = s.scrollHeight > 0 ? s.scrollTop / s.scrollHeight : 0;
  readerPrefs = readerPrefsClean(next);
  readerPrefsSave(readerPrefs);
  applyReaderPrefs();
  syncReaderSheet();
  if (readerPrefs.group !== before.group) rerenderReader();
  else if (READER_RELAYOUT.some(k => readerPrefs[k] !== before[k]) && s.scrollTop > 0) s.scrollTop = ratio * s.scrollHeight;
}

function setReaderPref(key, value) { setReaderPrefs(Object.assign({}, readerPrefs, {[key]: value})); }

function stepReaderSize(delta) {
  const i = READER_SIZE_STEPS.indexOf(readerPrefs.size) + delta;
  if (i >= 0 && i < READER_SIZE_STEPS.length) setReaderPref("size", READER_SIZE_STEPS[i]);
}

function rerenderReader() {
  const rb = readerHost.owner;
  if (!rb) return;
  const s = $("readerScroll"), top = s.scrollTop;
  renderReaderBody(rb);
  s.scrollTop = top;
}

// Remembered reading place per tab and URL (RAM only): survives a re-extract and returning to a page.
const READER_POS_MAX = 40;
function readerSavedTop(rb, url) {
  return rb && rb.readerPos && url && Number.isFinite(rb.readerPos[url]) ? rb.readerPos[url] : 0;
}

function readerRememberTop(rb, top) {
  const url = rb && rb.reader && rb.reader.data ? rb.reader.data.url : "";
  if (!url) return;
  const pos = rb.readerPos || (rb.readerPos = {});
  delete pos[url];
  pos[url] = Math.max(0, Math.round(top));       // insertion order = recency
  const keys = Object.keys(pos);
  for (let i = 0; i < keys.length - READER_POS_MAX; i++) delete pos[keys[i]];
}

// ---- appearance sheet (reuses the app's bottom-sheet look) ----
const readerSheetRefs = {segs: {}, inputs: {}, value: null, minus: null, plus: null, opener: null};

function readerSegRow(key, label, options) {
  const row = element("div", undefined, "rs-row");
  row.appendChild(element("div", label, "rs-label"));
  const seg = element("div", undefined, "seg");
  seg.setAttribute("role", "radiogroup");
  seg.setAttribute("aria-label", label);
  readerSheetRefs.segs[key] = [];
  for (const [value, text, cls] of options) {
    const b = element("button", undefined, cls || "");
    b.type = "button";
    b.setAttribute("role", "radio");
    b.dataset.value = String(value);
    b.appendChild(element("span", text));
    b.addEventListener("click", () => setReaderPref(key, value));
    seg.appendChild(b);
    readerSheetRefs.segs[key].push(b);
  }
  row.appendChild(seg);
  return row;
}

function readerSwitchRow(key, label) {
  const row = element("label", undefined, "rs-row is-switch");
  row.appendChild(element("span", label, "rs-label"));
  const sw = element("span", undefined, "switch");
  const input = document.createElement("input");
  input.type = "checkbox";
  input.addEventListener("change", () => setReaderPref(key, input.checked));
  sw.append(input, element("span", undefined, "switch-track"));
  row.appendChild(sw);
  readerSheetRefs.inputs[key] = input;
  return row;
}

function buildReaderSheet() {
  const body = $("readerSheetBody");
  if (body.childElementCount) return;
  const size = element("div", undefined, "rs-row");
  size.appendChild(element("div", "文字サイズ", "rs-label"));
  const stepper = element("div", undefined, "rs-stepper");
  const minus = element("button", "A−", "btn");
  const plus = element("button", "A+", "btn");
  for (const [b, label, delta] of [[minus, "文字を小さく", -1], [plus, "文字を大きく", 1]]) {
    b.type = "button";
    b.setAttribute("aria-label", label);
    b.addEventListener("click", () => stepReaderSize(delta));
  }
  const value = element("span", "", "rs-value");
  value.setAttribute("aria-live", "polite");
  stepper.append(minus, value, plus);
  size.appendChild(stepper);
  Object.assign(readerSheetRefs, {value, minus, plus});
  body.append(size,
    readerSegRow("font", "書体", [["gothic", "ゴシック"], ["mincho", "明朝", "f-mincho"]]),
    readerSegRow("theme", "テーマ", [["auto", "自動", "sw-auto"], ["light", "ライト", "sw-light"], ["sepia", "セピア", "sw-sepia"],
      ["dark", "ダーク", "sw-dark"], ["black", "ブラック", "sw-black"]]),
    readerSegRow("leading", "行間", [[0, "狭い"], [1, "標準"], [2, "広い"]]),
    readerSegRow("width", "本文の幅", [[0, "狭い"], [1, "標準"], [2, "広い"]]),
    readerSwitchRow("justify", "両端揃え"),
    readerSegRow("images", "画像", [["full", "大きく"], ["normal", "標準"], ["small", "小さく"], ["none", "表示しない"]]),
    readerSwitchRow("group", "連続画像をまとめる"),
    readerSwitchRow("headings", "見出しを強調"),
    readerSwitchRow("progress", "進行状況バー"));
  const reset = element("button", "既定に戻す", "btn rs-reset");
  reset.type = "button";
  reset.addEventListener("click", () => setReaderPrefs(READER_DEFAULTS));
  body.appendChild(reset);
}

function syncReaderSheet() {
  const refs = readerSheetRefs;
  if (!refs.value) return;
  const p = readerPrefs;
  refs.value.textContent = p.size + "px";
  const at = READER_SIZE_STEPS.indexOf(p.size);
  refs.minus.disabled = at <= 0;
  refs.plus.disabled = at >= READER_SIZE_STEPS.length - 1;
  for (const key of Object.keys(refs.segs)) {
    for (const b of refs.segs[key]) b.setAttribute("aria-checked", String(b.dataset.value === String(p[key])));
  }
  for (const key of Object.keys(refs.inputs)) refs.inputs[key].checked = p[key] === true;
}

function openReaderSheet() {
  buildReaderSheet();
  syncReaderSheet();
  readerSheetRefs.opener = document.activeElement;
  setShown("readerSheet", true);
  const first = $("readerSheetBody").querySelector("button:not(:disabled)");
  if (first) first.focus({preventScroll: true});
}

function closeReaderSheet() {
  if ($("readerSheet").hidden) return;
  setShown("readerSheet", false);
  const back = readerSheetRefs.opener;
  readerSheetRefs.opener = null;
  if (back && back.isConnected && typeof back.focus === "function") back.focus({preventScroll: true});
}

function wireReaderSheet() {
  const sheet = $("readerSheet");
  $("readerAppearance").addEventListener("click", openReaderSheet);
  $("readerSheetClose").addEventListener("click", closeReaderSheet);
  sheet.addEventListener("click", e => { if (e.target === sheet) closeReaderSheet(); });
  sheet.addEventListener("keydown", e => {
    if (e.key === "Escape") { e.preventDefault(); closeReaderSheet(); return; }
    if (e.key !== "Tab") return;
    const items = Array.from(sheet.querySelectorAll("button:not(:disabled), input"));
    if (!items.length) return;
    const first = items[0], last = items[items.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  });
}

// ---- v0.1.36: full-screen image viewer (pinch / double-tap zoom, swipe between pictures, swipe down to close) ----
// Pointer events + a CSS transform on one <img>; the pictures are the same Hub bytes as in the page.
const rv = {open: false, list: [], i: 0, scale: 1, tx: 0, ty: 0, own: new Map(), ptrs: new Map(), g: null,
  tap: {t: 0, x: 0, y: 0}, opener: null, token: 0, busy: false, from: 0};
const RV_MAX_SCALE = 6;
const RV_DOUBLE_SCALE = 2.6;

function rvReduced() {
  try { return window.matchMedia("(prefers-reduced-motion: reduce)").matches; } catch (e) { return false; }
}

function rvStage() {
  const r = $("rvStage").getBoundingClientRect();
  return {w: r.width, h: r.height, cx: r.left + r.width / 2, cy: r.top + r.height / 2};
}

// The picture as drawn at scale 1 (object-fit: contain), to limit panning to what is really there.
function rvFit() {
  const img = $("rvImg"), st = rvStage();
  if (!img.naturalWidth || !img.naturalHeight) return {w: st.w, h: st.h};
  const k = Math.min(st.w / img.naturalWidth, st.h / img.naturalHeight);
  return {w: img.naturalWidth * k, h: img.naturalHeight * k};
}

function rvClamp() {
  const st = rvStage(), f = rvFit();
  const lx = Math.max(0, (f.w * rv.scale - st.w) / 2), ly = Math.max(0, (f.h * rv.scale - st.h) / 2);
  rv.tx = Math.max(-lx, Math.min(lx, rv.tx));
  rv.ty = Math.max(-ly, Math.min(ly, rv.ty));
}

function rvApply(ms) {
  const img = $("rvImg");
  const d = rvReduced() ? 0 : ms;
  img.style.transition = d ? "transform " + d + "ms ease-out" : "none";
  img.style.transform = "translate3d(" + rv.tx.toFixed(1) + "px," + rv.ty.toFixed(1) + "px,0) scale(" + rv.scale.toFixed(3) + ")";
}

function rvVeil(opacity) { $("readerViewer").style.background = "rgba(0,0,0," + opacity.toFixed(2) + ")"; }

function rvReset(ms) {
  rv.scale = 1; rv.tx = 0; rv.ty = 0;
  rvVeil(1);
  rvApply(ms);
}

// Zoom to `next` keeping the stage point (px, py) (relative to the stage centre) still.
function rvZoomAt(next, px, py) {
  next = Math.max(1, Math.min(RV_MAX_SCALE, next));
  const k = next / rv.scale;
  rv.tx = px - (px - rv.tx) * k;
  rv.ty = py - (py - rv.ty) * k;
  rv.scale = next;
  if (next <= 1) { rv.tx = 0; rv.ty = 0; } else rvClamp();
}

async function rvSource(e) {
  if (e.dead) return "";
  if (e.url) { e.pinned = true; return e.url; }
  const have = rv.own.get(e);
  if (have) return have;
  const blob = await fetchReaderBlob(e.rb, e.index).catch(() => null);
  if (!blob || !rv.open) return "";
  const url = URL.createObjectURL(blob);
  rv.own.set(e, url);
  return url;
}

function rvShow() {
  const e = rv.list[rv.i], img = $("rvImg"), token = ++rv.token;
  $("rvCount").textContent = (rv.i + 1) + " / " + rv.list.length;
  $("rvCaption").textContent = e.alt || "";
  $("rvPrev").hidden = rv.i === 0;
  $("rvNext").hidden = rv.i === rv.list.length - 1;
  $("rvMsg").textContent = "";
  img.classList.add("is-loading");
  rvSource(e).then(url => {
    if (token !== rv.token || !rv.open) return;
    if (!url) { $("rvMsg").textContent = "画像を読み込めません"; return; }
    if (img.getAttribute("src") === url && img.complete && img.naturalWidth) img.classList.remove("is-loading");
    else img.src = url;
    for (const n of [rv.i + 1, rv.i - 1]) if (rv.list[n]) rvSource(rv.list[n]).catch(() => {});   // neighbours
  }).catch(() => {});
}

function rvGo(dir) {
  const j = rv.i + dir;
  if (rv.busy || j < 0 || j >= rv.list.length) { rv.tx = 0; rvApply(150); return; }
  rv.busy = true;
  const w = rvStage().w, ms = rvReduced() ? 0 : 160;
  rv.tx = -dir * w;
  rvApply(ms);
  setTimeout(() => {
    rv.busy = false;
    if (!rv.open) return;
    rv.i = j;
    rv.scale = 1; rv.ty = 0; rv.tx = dir * w;
    rvApply(0);
    rvShow();
    void $("rvImg").offsetWidth;
    rv.tx = 0;
    rvApply(ms);
  }, ms);
}

function openReaderViewer(fig) {
  const list = [];
  let at = 0;
  for (const f of $("readerBody").querySelectorAll(".reader-fig")) {
    const img = f.querySelector(".reader-img");
    const e = img ? readerHost.map.get(img) : null;
    if (!e || e.dead) continue;
    if (f === fig) at = list.length;
    list.push(e);
  }
  if (!list.length) return;
  rv.list = list; rv.i = at; rv.from = at; rv.busy = false; rv.ptrs.clear(); rv.g = null;
  rv.opener = fig.querySelector(".reader-img, .reader-zoom") || fig;
  rv.open = true;
  rvReset(0);
  setShown("readerViewer", true);
  rvShow();
  $("rvClose").focus({preventScroll: true});
}

function closeReaderViewer() {
  if (!rv.open) return;
  rv.open = false;
  rv.token++;
  setShown("readerViewer", false);
  const img = $("rvImg");
  img.removeAttribute("src");
  img.classList.add("is-loading");
  for (const url of rv.own.values()) URL.revokeObjectURL(url);
  rv.own.clear();
  const last = rv.list[rv.i];
  for (const e of rv.list) if (e.pinned) { e.pinned = false; if (!e.want) releaseReaderImage(e); }
  rv.list = [];
  rv.ptrs.clear(); rv.g = null;
  if (last && !last.dead && rv.i !== rv.from) last.fig.scrollIntoView({block: "center"});   // leave the page where the viewer ended
  const back = rv.opener;
  rv.opener = null;
  if (back && back.isConnected) back.focus({preventScroll: true});
}

function rvBegin() {
  const pts = Array.from(rv.ptrs.values()), st = rvStage();
  if (pts.length >= 2) {
    const a = pts[0], b = pts[1];
    rv.g = {type: "pinch", d0: Math.hypot(a.x - b.x, a.y - b.y) || 1, s0: rv.scale, tx0: rv.tx, ty0: rv.ty,
      cx: (a.x + b.x) / 2 - st.cx, cy: (a.y + b.y) / 2 - st.cy};
  } else if (pts.length === 1) {
    rv.g = {type: "one", x0: pts[0].x, y0: pts[0].y, tx0: rv.tx, ty0: rv.ty, t0: performance.now(), axis: "",
      moved: false, dx: 0, dy: 0};
  } else rv.g = null;
}

function rvDown(e) {
  if (e.pointerType === "mouse" && e.button !== 0) return;
  try { $("rvStage").setPointerCapture(e.pointerId); } catch (err) { /* not capturable */ }
  rv.ptrs.set(e.pointerId, {x: e.clientX, y: e.clientY});
  if (rv.busy) return;
  rvBegin();
}

function rvMove(e) {
  const p = rv.ptrs.get(e.pointerId), g = rv.g;
  if (!p) return;
  p.x = e.clientX; p.y = e.clientY;
  if (!g || rv.busy) return;
  const st = rvStage();
  if (g.type === "pinch") {
    const pts = Array.from(rv.ptrs.values());
    if (pts.length < 2) return;
    const [a, b] = pts;
    const s1 = Math.max(0.8, Math.min(RV_MAX_SCALE, g.s0 * Math.hypot(a.x - b.x, a.y - b.y) / g.d0));
    const k = s1 / g.s0;
    rv.scale = s1;
    rv.tx = (a.x + b.x) / 2 - st.cx - (g.cx - g.tx0) * k;
    rv.ty = (a.y + b.y) / 2 - st.cy - (g.cy - g.ty0) * k;
    rvApply(0);
    return;
  }
  const dx = e.clientX - g.x0, dy = e.clientY - g.y0;
  g.dx = dx; g.dy = dy;
  if (Math.hypot(dx, dy) > 8) g.moved = true;
  if (rv.scale > 1.02) { rv.tx = g.tx0 + dx; rv.ty = g.ty0 + dy; rvClamp(); rvApply(0); return; }
  if (!g.axis && g.moved) g.axis = Math.abs(dx) > Math.abs(dy) ? "x" : "y";
  if (g.axis === "x") {
    const has = rv.list[rv.i + (dx < 0 ? 1 : -1)] !== undefined;
    rv.tx = has ? dx : dx * 0.3;
    rvApply(0);
  } else if (g.axis === "y") {
    rv.ty = dy > 0 ? dy : dy * 0.2;
    rvVeil(1 - Math.min(0.6, Math.abs(rv.ty) / 500));
    rvApply(0);
  }
}

function rvUp(e) {
  if (!rv.ptrs.has(e.pointerId)) return;
  const g = rv.g;
  rv.ptrs.delete(e.pointerId);
  try { $("rvStage").releasePointerCapture(e.pointerId); } catch (err) { /* already released */ }
  if (!g || rv.busy) { rvBegin(); return; }
  if (g.type === "pinch") {
    if (rv.scale < 1.05) rvReset(150); else { rvClamp(); rvApply(150); }
    rvBegin();
    if (rv.g) rv.g.moved = true;           // the remaining finger pans; it is not a tap
    return;
  }
  rvBegin();
  const dt = Math.max(1, performance.now() - g.t0);
  const cancelled = e.type === "pointercancel";
  if (!g.moved && !cancelled && dt < 350) {
    const now = performance.now(), t = rv.tap, st = rvStage();
    if (now - t.t < 320 && Math.hypot(e.clientX - t.x, e.clientY - t.y) < 30) {
      rv.tap.t = 0;
      if (rv.scale > 1.05) rvReset(160);
      else { rvZoomAt(RV_DOUBLE_SCALE, e.clientX - st.cx, e.clientY - st.cy); rvApply(160); }
    } else rv.tap = {t: now, x: e.clientX, y: e.clientY};
    return;
  }
  if (rv.scale > 1.02) return;
  if (g.axis === "x") {
    const dir = g.dx < 0 ? 1 : -1, w = rvStage().w;
    const fast = Math.abs(g.dx) / dt > 0.5 && Math.abs(g.dx) > 30;
    if (!cancelled && (Math.abs(g.dx) > w * 0.22 || fast) && rv.list[rv.i + dir]) rvGo(dir);
    else { rv.tx = 0; rvApply(150); }
  } else if (g.axis === "y") {
    if (!cancelled && (g.dy > 110 || (g.dy > 40 && g.dy / dt > 0.6))) closeReaderViewer();
    else { rv.ty = 0; rvVeil(1); rvApply(150); }
  }
}

function wireReaderViewer() {
  const stage = $("rvStage"), img = $("rvImg");
  stage.addEventListener("pointerdown", rvDown);
  stage.addEventListener("pointermove", rvMove);
  stage.addEventListener("pointerup", rvUp);
  stage.addEventListener("pointercancel", rvUp);
  stage.addEventListener("wheel", e => {
    if (!rv.open) return;
    e.preventDefault();
    const st = rvStage();
    rvZoomAt(rv.scale * Math.exp(-e.deltaY * 0.002), e.clientX - st.cx, e.clientY - st.cy);
    rvApply(0);
  }, {passive: false});
  img.addEventListener("load", () => { img.classList.remove("is-loading"); rvClamp(); rvApply(0); });
  img.addEventListener("error", () => { if (rv.open && img.getAttribute("src")) $("rvMsg").textContent = "画像を読み込めません"; });
  $("rvClose").addEventListener("click", closeReaderViewer);
  $("rvPrev").addEventListener("click", () => rvGo(-1));
  $("rvNext").addEventListener("click", () => rvGo(1));
  $("readerViewer").addEventListener("keydown", e => {
    if (!rv.open) return;
    if (e.key === "Escape") { e.preventDefault(); closeReaderViewer(); }
    else if (e.key === "ArrowLeft") { e.preventDefault(); rvGo(-1); }
    else if (e.key === "ArrowRight") { e.preventDefault(); rvGo(1); }
    else if (e.key === "+" || e.key === "=") { e.preventDefault(); rvZoomAt(rv.scale * 1.4, 0, 0); rvApply(120); }
    else if (e.key === "-") { e.preventDefault(); rvZoomAt(rv.scale / 1.4, 0, 0); rvApply(120); }
    else if (e.key === "Tab") {
      const items = Array.from($("readerViewer").querySelectorAll("button:not([hidden])"));
      if (!items.length) return;
      const first = items[0], last = items[items.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
  });
}

function readerNode(rb, block) {
  if (block.t === "h") {
    return element("h" + Math.min(6, Math.max(2, block.level + 1)), block.text, "reader-h");
  }
  if ((block.t === "p" || block.t === "li") && readerLinkRow(block.runs)) return readerChips(block.runs);
  const cards = (block.t === "p" || block.t === "li") ? readerCardItems(block.runs) : null;
  if (cards) return readerCards(cards);
  if (block.t === "p") {
    const p = element("p", undefined, block.q === true ? "reader-p reader-quote" : "reader-p");
    readerRuns(p, block.runs);
    return p;
  }
  if (block.t === "li") {
    const li = element("div", undefined, "reader-li");
    li.style.marginLeft = (1.4 * Math.max(0, Math.min(8, Number(block.depth) || 0))) + "em";
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
  const renderStart = performance.now();
  const frag = document.createDocumentFragment();
  // A label ("RapidGator") or a size ("(66.76 MB)") laid out in its own column comes as a
  // separate short block: it is folded into the neighbouring link card when it fits.
  let prev = null;
  const blocks = data.blocks;
  for (let k = 0; k < blocks.length; k++) {
    const block = blocks[k];
    if (readerPrefs.group && block.t === "img") {          // a run of 3+ pictures becomes a 2-column grid
      let j = k;
      while (j < blocks.length && blocks[j].t === "img") j++;
      if (j - k >= 3) {
        const grid = element("div", undefined, "reader-grid");
        for (let g = k; g < j; g++) grid.appendChild(readerImage(rb, blocks[g]));
        frag.appendChild(grid);
        k = j - 1;
        prev = null;
        continue;
      }
    }
    const runs = (block.t === "p" || block.t === "li") ? block.runs : null;
    const items = runs && !readerLinkRow(runs) ? readerCardItems(runs) : null;
    if (items) {
      if (!items[0].label && prev && prev.label) { items[0].label = prev.label; prev.node.remove(); }
      const next = blocks[k + 1];
      const last = items[items.length - 1];
      const nextText = next && next.t === "p" && next.runs.every(r => !r.href) ? next.runs.map(r => r.text).join("") : "";
      const m = nextText && !last.size ? READER_ONLY_SIZE.exec(nextText) : null;
      if (m) { last.size = m[1].replace(/\s+/g, " "); k++; }
      frag.appendChild(readerCards(items));
      prev = null;
      continue;
    }
    const node = readerNode(rb, block);
    frag.appendChild(node);
    let label = "";
    if (block.t === "p" && block.runs.every(r => !r.href)) {
      const text = readerLabel(block.runs.map(r => r.text).join(""));
      if (text && text.length <= 30) label = text;
    }
    prev = label ? {node, label} : null;
  }
  dlog("render", "info", "reader render", {ms: Math.round(performance.now() - renderStart), blocks: data.blocks.length});
  if (data.truncated) frag.appendChild(element("p", "長いページのため、途中までを表示しています。", "reader-empty"));
  body.appendChild(frag);
  tlWatchReader(rb);          // v0.1.54: first-load timeline (the text is on screen)
}

// The reader container is shared by all remote tabs; only its owner's content is built.
function mountReader(tab) {
  const rb = tab.rb;
  if (!rb) return;
  const scroller = $("readerScroll");
  const url = () => (rb.reader && rb.reader.data ? rb.reader.data.url : "");
  if (readerHost.owner === rb) {
    // a hidden scroller forgets its place: put it back
    if (scroller.scrollTop === 0 && readerSavedTop(rb, url()) > 0) scroller.scrollTop = readerSavedTop(rb, url());
    syncReaderProgress();
    return;
  }
  readerHost.owner = rb;
  renderReaderBody(rb);
  scroller.scrollTop = readerSavedTop(rb, url());
  syncReaderProgress();
}

// A page that is still "loading" after this long (ad-heavy pages never finish) is read anyway:
// its DOM is usually ready. Used by the reader and the fast display.
const RB_EARLY_LOAD_MS = 3000;
const RB_EARLY_POLL_S = 1;             // meta wait while a loading page waits for the 3 s mark

function noteLoadStart(rb, meta, now) {
  if (rb.loadUrl !== meta.url || meta.loading !== true) {
    rb.loadUrl = meta.url;
    rb.loadSince = now;
  }
}

// v0.1.54: the reader and the mirror start at DOMContentLoaded (meta.dcl, from the NAS browser's own event)
// instead of waiting for the 3 s timer; the timer stays for pages that never report it.
function loadedLongEnough(rb, meta, now, eventDriven) {
  noteLoadStart(rb, meta, now);
  if (meta.loading !== true) return false;
  return (eventDriven === true && meta.dcl === true) || now - rb.loadSince >= RB_EARLY_LOAD_MS;
}

// "run" when the reader should be fetched for this metadata, else "". When it ran while loading it
// runs once more when loading turns false (same URL).
function readerAction(rb, meta, now) {
  if (rb.firstPending) { rb.heldMeta = meta; return ""; }     // v0.1.59: see mirrorAction
  if (meta.loading === true) {
    rb.readerPending = true;
    if (rb.readerEarly !== meta.url && loadedLongEnough(rb, meta, now, true)) {
      rb.readerEarly = meta.url;
      return "run";
    }
    return "";
  }
  noteLoadStart(rb, meta, now);
  if (!rb.reader || rb.reader.url !== meta.url || rb.readerPending || rb.readerEarly === meta.url) {
    rb.readerEarly = "";
    return "run";
  }
  return "";
}

// "snap" / "snap-force" / "snap-changed" / "" for the fast display.
function tilesAction(rb, meta, unchanged, now) {
  if (meta.loading === true) {
    if (rb.snapEarly !== meta.url && loadedLongEnough(rb, meta, now)) {
      rb.snapEarly = meta.url;
      return "snap";
    }
    return "";
  }
  noteLoadStart(rb, meta, now);
  if (rb.snapEarly === meta.url) { rb.snapEarly = ""; return "snap-force"; }
  if (rb.snap !== null && rb.snapKey !== (meta.url + "\n" + meta.title)) return "snap";
  if (unchanged && rb.snap !== null) return "snap-changed";
  return "";
}

// Records a failed reader run; true when this page should be shown in the fast display instead
// (second failure in a row for the same URL, or an empty result).
function readerFailed(rb, url, empty) {
  const fail = rb.readerFail && rb.readerFail.url === url ? rb.readerFail : {url, n: 0};
  fail.n++;
  rb.readerFail = fail;
  return empty === true || fail.n >= 2;
}

function readerFallback(tab, rb) {
  rb.tilesFor = rb.url;
  rb.readerFail = null;
  tab.error = "";
  toast("このページはリーダー表示に対応していないため高速表示にしました");
  applyDisplay(tab);
}

async function runReader(tab, opts = {}) {
  const rb = tab && tab.rb;
  if (!rb || tabDisplay(tab) !== "reader" || challengeHold(tab)) return;
  if (rb.readerBusy) { rb.readerAgain = true; return; }
  rb.readerBusy = true;
  snapBusy(rb, 1);
  try {
    const started = performance.now();
    const raw = opts.prefetched || await api("/api/rb/reader", {tab: rb.id});     // v0.1.59: may come with the tab-open answer
    rb.perf.extractMs = performance.now() - started;
    dlog("render", "debug", "reader fetched", {ms: Math.round(rb.perf.extractMs)});
    rb.perf.imgs = 0; rb.perf.imgMs = 0;
    const data = cleanReaderData(raw);
    if (tab.rb !== rb || tabDisplay(tab) !== "reader") return;
    if (!data) throw new Error("本文を取り出せませんでした");
    if (data.blocks.length === 0 && rb.loading === true) {
      // v0.1.55: since v0.1.54 the extraction starts at DOMContentLoaded, when a page may not have its text yet.
      // Not a failure while it is still loading: the metadata poll runs the reader again when the load ends.
      dlog("render", "debug", "reader empty while loading", {url: data.url});
      updateRemoteView(tab);
      return;
    }
    if (data.blocks.length === 0) {
      console.error("reader: 0 blocks", data.url);
      readerFailed(rb, rb.url, true);
      readerFallback(tab, rb);
      return;
    }
    const scroller = $("readerScroll");
    const mounted = readerHost.owner === rb;
    const sameUrl = Boolean(rb.reader && rb.reader.data) && rb.reader.data.url === data.url;
    const prevUrl = rb.reader && rb.reader.data ? rb.reader.data.url : "";
    let keepTop = 0;
    if (mounted && sameUrl && opts.keepScroll !== false) keepTop = scroller.scrollTop;
    else if (!prevUrl || prevUrl === data.url) keepTop = readerSavedTop(rb, data.url);   // same page again: back to the reading place
    rb.reader = {data, url: data.url};
    rb.readerPending = false;
    rb.readerFail = null;
    if (rb.readerErr) { tab.error = ""; rb.readerErr = false; }
    if (mounted) { renderReaderBody(rb); scroller.scrollTop = keepTop; syncReaderProgress(); }
    updateRemoteView(tab);
  } catch (e) {
    if (tab.rb !== rb) return;
    if (e.status === 404) { remoteLost(tab); return; }
    console.error("reader failed:", e);
    if (readerFailed(rb, rb.url, false)) { readerFallback(tab, rb); return; }
    tab.error = "リーダー表示を作れませんでした: " + (e && e.message ? e.message : String(e));
    rb.readerErr = true;
    updateRemoteView(tab);
    if (!opts.quiet) toast(tab.error, "error");
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
  const seed = takeSeed(tab.rb, "reader");        // v0.1.59: see mirrorLoop
  tlMark(tab.rb, "choose");
  await nextPaint();
  await nextPaint();
  if (gen !== state.rbGen || !tab.rb) return;
  if (seed) {
    first = false;
    applySeed(tab, seed);
    sendViewport(tab).catch(() => {});
  } else {
    await sendViewport(tab).catch(() => {});
  }
  while (gen === state.rbGen && tab.rb) {
    const rb = tab.rb;
    try {
      const wait = first ? 0 : rb.loading === true && rb.readerEarly !== rb.url ? RB_EARLY_POLL_S : RB_META_WAIT_S;
      let target = "/api/rb/meta?tab=" + encodeURIComponent(rb.id) + "&wait=" + wait;
      if (!first && rb.rev !== null) target += "&rev=" + rb.rev + probeQuery(rb);
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
      if (readerAction(rb, meta, Date.now()) === "run") runReader(tab, {quiet: true}).catch(() => {});
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
  rb.firstPending = true;                          // v0.1.59: see mirrorNavigate
  try {
    const r = await api("/api/rb/navigate", {tab: rb.id, url: href, first: "reader"});
    rb.readerPending = true;
    firstDone(tab, rb, r.first ? {kind: "reader", first: r.first} : null);
  } catch (e) {
    firstDone(tab, rb, null);
    rb.tapUntil = 0; tab.loading = Boolean(rb.loading); syncLoadingBars(tab);       // v0.1.60
    if (e.status === 404) remoteLost(tab);
    else throw e;
  }
}

function wireReader() {
  const body = $("readerBody");
  readerPrefs = readerPrefsLoad();
  applyReaderPrefs();
  wireReaderSheet();
  wireReaderViewer();
  const scroller = $("readerScroll");
  let scrollTick = 0;
  scroller.addEventListener("scroll", () => {
    if (scrollTick) return;
    scrollTick = requestAnimationFrame(() => {
      scrollTick = 0;
      updateReaderProgress();
      chromeScrolled(scroller.scrollTop, scroller.scrollHeight - scroller.clientHeight, "reader");     // v0.1.60
      if (readerHost.owner && body.childElementCount) readerRememberTop(readerHost.owner, scroller.scrollTop);
    });
  }, {passive: true});
  const figureOf = event => (event.target && event.target.closest) ? event.target.closest(".reader-fig") : null;
  body.addEventListener("click", event => {
    const fig = figureOf(event);
    if (!fig || event.target.closest("a[data-href]")) return;
    if (event.target.closest(".reader-hold")) { fig.classList.add("is-open"); return; }
    if (scroller.dataset.images === "none" && !fig.classList.contains("is-open")) return;
    if (event.target.closest(".reader-zoom") || fig.classList.contains("can-zoom")) openReaderViewer(fig);
  });
  body.addEventListener("keydown", event => {
    if ((event.key === "Enter" || event.key === " ") && event.target.classList && event.target.classList.contains("reader-img") &&
        event.target.closest(".reader-fig.can-zoom")) { event.preventDefault(); openReaderViewer(event.target.closest(".reader-fig")); }
  });
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
      const label = (press.a.textContent || "").trim();          // v0.1.65: the link's text names the job in the analysis list
      perform(() => startProbe(tab, {href, label}));      // the exact address is known: no hit test needed
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
    if (tab && tabDisplay(tab) === "reader") {
      tapFeedback(tab, a, "is-tapped");              // v0.1.60
      perform(() => readerNavigate(tab, a.dataset.href));
    }
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

// ---- mirror display (v0.1.39) ----
// The NAS browser renders the page; the Hub sends a sanitized copy (HTML + CSS, no scripts, every image
// and font a Hub reference). It is shown in <iframe id="mirrorFrame"> (v0.1.41: without the sandbox
// attribute: WebKit drops every event listener on a script-less sandboxed document, so taps never
// arrived on iPhone). Nothing in it can run code: the Hub strips scripts and handlers, and the
// document's CSP (header and <meta>) is default-src 'none'; form-action 'none'; links carry no href. The iframe's document is /api/rb/mirror/doc (this origin,
// with its own strict CSP header); later versions of the same page are applied to the iframe's DOM
// (parsed with DOMParser) and its <style>. This code only listens (taps, long presses, touches): every
// action goes to the Hub, which validates it again. The page is never given an address of its own.
const MIRROR_TOGGLE = "[aria-haspopup],[aria-expanded],[aria-controls],[data-toggle],[data-bs-toggle],[role=button],summary,button";
const MIRROR_MIN_MS = 1000;           // recaptures are at least this far apart
const MIRROR_DOC_RE = /^\/api\/rb\/mirror\/doc\?tab=b[0-9]{1,6}&rev=[0-9]{1,12}$/;
const MIRROR_LOAD_TIMEOUT_MS = 12000;
const MIRROR_CLICK_REFRESH_MS = 350;  // after a tap that stayed on the page: look again
const MIRROR_POS_MAX = 40;
const MIRROR_TOUCH_RESUME_MS = 150;
const mirrorHost = {owner: null, pageUrl: "", last: null};
const mirrorWired = new WeakSet();

function mirrorDoc() {
  try {
    const doc = $("mirrorFrame").contentDocument;
    return doc && doc.body && doc.getElementById("mirrorCss") ? doc : null;
  } catch (e) { return null; }
}

function mirrorScroller(doc) { return doc.scrollingElement || doc.documentElement; }

function mirrorClosest(node, selector) {
  return node && node.closest ? node.closest(selector) : null;
}

// Defensive second check of what the Hub sent: nothing executable or external may be in it.
function mirrorUnsafe(data) {
  const markup = [data.html, "<x" + data.html_attrs + ">", "<x" + data.body_attrs + ">"];
  for (const t of markup) {
    if (typeof t !== "string") return true;
    if (/<\s*(script|iframe|object|embed|base|meta|link|form)\b/i.test(t) || /<[^>]*\son[a-z]+\s*=/i.test(t) ||
        /javascript\s*:/i.test(t)) return true;
  }
  const css = data.css;
  if (typeof css !== "string") return true;
  return /<\/style|<script|@import|expression\s*\(|javascript\s*:/i.test(css) ||
    /url\(\s*(?!["']?(?:\/api\/rb\/mirror\/res\?|data:i|#))/i.test(css);
}

function mirrorPosTop(rb, url) {
  return rb && rb.mirrorPos && url && Number.isFinite(rb.mirrorPos[url]) ? rb.mirrorPos[url] : 0;
}

function mirrorRememberTop(rb, url, top) {
  if (!rb || !url) return;
  delete rb.mirrorPos[url];
  rb.mirrorPos[url] = Math.max(0, Math.round(top));       // insertion order = recency
  const keys = Object.keys(rb.mirrorPos);
  for (let i = 0; i < keys.length - MIRROR_POS_MAX; i++) delete rb.mirrorPos[keys[i]];
}

function dropMirror(rb) {
  clearTimeout(rb.mirrorTimer);
  rb.mirrorTimer = 0;
  rb.mirror = null;
  rb.mirrorAgain = false;
  rb.mirrorPending = false;
  rb.mirrorTouch = false;
  rb.mirrorDeferred = false;
  rb.mirrorFollow = false;
  if (mirrorHost.owner !== rb) return;
  mirrorHost.owner = null; mirrorHost.pageUrl = ""; mirrorHost.last = null;
  const frame = $("mirrorFrame");
  $("mirrorWrap").hidden = true;
  frame.removeAttribute("src");
  try { frame.contentWindow.location.replace("about:blank"); } catch (e) { /* already gone */ }
}

// Loads the capture's document into the iframe; resolves when it is ready (the Hub's page, not the site's).
// `cover` keeps another tab's page out of sight while this one loads (the frame must be laid out to load).
function mirrorLoadDoc(docUrl, cover) {
  const mf = $("mirrorFrame"), wrap = $("mirrorWrap");
  wrap.style.visibility = cover ? "hidden" : "";
  wrap.hidden = false;
  return new Promise((resolve, reject) => {
    let done = false;
    const finish = ok => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      mf.removeEventListener("load", onLoad);
      if (ok) resolve(); else reject(new Error("ミラー表示を読み込めませんでした"));
    };
    // v0.1.70: a load event of another document (an earlier copy, about:blank during a quick back) is not ours:
    // keep waiting for this copy (until the timeout) instead of failing at once
    // v0.1.73: our own document that came back without the copy (the Hub answered an error) fails at once instead of
    // after the whole timeout
    const onLoad = () => {
      if (mirrorDoc()) { finish(true); return; }
      try {
        const here = mf.contentWindow.location.href;
        if (here === new URL(docUrl, location.href).href) finish(false);
      } catch (e) { /* not readable: keep waiting */ }
    };
    const timer = setTimeout(() => finish(false), MIRROR_LOAD_TIMEOUT_MS);
    mf.addEventListener("load", onLoad);
    // v0.1.50: replace, never add, the frame's history entry. A new src adds an entry to the
    // page's session history, so the browser back (the iOS edge swipe) stepped through earlier
    // copies of the mirror ("goes back to the same page").
    mf.dataset.doc = docUrl;
    rttCount++;                    // v0.1.59: the document is a request of its own
    try {
      mf.contentWindow.location.replace(new URL(docUrl, location.href).href);
    } catch (e) {
      mf.src = docUrl;
    }
  });
}

// The first visible element (below fixed headers) and where it is, to keep the reading place across a swap.
function mirrorAnchorOf(doc) {
  const view = doc.defaultView;
  const x = Math.min(24, Math.max(1, doc.documentElement.clientWidth / 2));
  for (const y of [6, 90, 180]) {
    const el = doc.elementFromPoint(x, y);
    const m = mirrorClosest(el, "[data-m]");
    if (!m) continue;
    const pos = view.getComputedStyle(m).position;
    if (pos === "fixed" || pos === "sticky") continue;
    return {id: m.getAttribute("data-m"), top: m.getBoundingClientRect().top};
  }
  return null;
}

// v0.1.73: only the attributes that differ are written (an <img> whose src stays is never asked again)
function mirrorCopyAttrs(from, to, st = null) {
  for (const a of Array.from(to.attributes)) {
    if (!from.hasAttribute(a.name)) { to.removeAttribute(a.name); if (st) st.attrs++; }
  }
  for (const a of Array.from(from.attributes)) {
    if (to.getAttribute(a.name) === a.value) continue;
    try {
      to.setAttribute(a.name, a.value);
      if (st) st.attrs++;
    } catch (e) { /* not a valid name here */ }
  }
}

// v0.1.73: brings the shown children of `cur` in line with the new copy `next`, keeping every element that is
// still there (matched by its data-m id, else by position and tag) and every text that did not change. The
// whole body was replaced before: every image was a new element (decoded and painted again, refetched when
// its URL changed) and the page flickered on each copy.
function mirrorPatchChildren(doc, cur, next, st) {
  const want = Array.from(next.childNodes);
  const ids = new Set();
  for (const n of want) if (n.nodeType === 1 && n.hasAttribute("data-m")) ids.add(n.getAttribute("data-m"));
  const byId = new Map();
  for (const n of Array.from(cur.childNodes)) {
    if (n.nodeType !== 1 || !n.hasAttribute("data-m")) continue;
    const id = n.getAttribute("data-m");
    if (ids.has(id) && !byId.has(id)) byId.set(id, n);
    else { cur.removeChild(n); st.removed++; }        // gone from the page: out before the walk
  }
  let ref = cur.firstChild;
  for (const nn of want) {
    let match = null;
    if (nn.nodeType === 1) {
      const id = nn.hasAttribute("data-m") ? nn.getAttribute("data-m") : null;
      if (id !== null) {
        const c = byId.get(id);
        if (c && c.nodeName === nn.nodeName) { match = c; byId.delete(id); }
      } else if (ref && ref.nodeType === 1 && !ref.hasAttribute("data-m") && ref.nodeName === nn.nodeName) {
        match = ref;
      }
    } else if (ref && ref.nodeType === nn.nodeType && ref.nodeType !== 1) {
      match = ref;
    }
    if (!match) {
      cur.insertBefore(doc.importNode(nn, true), ref);
      st.added++;
      continue;
    }
    if (match === ref) ref = ref.nextSibling;
    else { cur.insertBefore(match, ref); st.moved++; }
    if (match.nodeType === 1) {
      st.kept++;
      mirrorCopyAttrs(nn, match, st);
      const tag = match.nodeName;
      if (tag === "STYLE" || tag === "TEXTAREA" || tag === "TITLE") {
        if (match.textContent !== nn.textContent) { match.textContent = nn.textContent; st.text++; }
      } else {
        mirrorPatchChildren(doc, match, nn, st);
      }
    } else if (match.nodeValue !== nn.nodeValue) {
      match.nodeValue = nn.nodeValue;
      st.text++;
    }
  }
  while (ref) {
    const after = ref.nextSibling;
    cur.removeChild(ref);
    st.removed++;
    ref = after;
  }
}

// Replaces the page content of the iframe with a newer capture, keeping the reading position. Answers what
// was kept and changed (v0.1.73 diagnostics).
function mirrorMorph(doc, data, startTop = null) {
  const scroller = mirrorScroller(doc);
  const before = scroller.scrollTop;
  const anchor = startTop === null ? mirrorAnchorOf(doc) : null;
  const st = {kept: 0, added: 0, removed: 0, moved: 0, attrs: 0, text: 0, css: false};
  const sheet = doc.getElementById("mirrorCss");
  if (sheet.textContent !== data.css) { sheet.textContent = data.css; st.css = true; }
  // parsed by the iframe's own DOMParser so the iframe's policy (inline styles allowed), not the app's, applies
  const parsed = new (doc.defaultView.DOMParser)().parseFromString(
    "<!doctype html><html" + data.html_attrs + "><body" + data.body_attrs + ">" + data.html + "</body></html>",
    "text/html");
  mirrorCopyAttrs(parsed.documentElement, doc.documentElement, st);
  mirrorCopyAttrs(parsed.body, doc.body, st);
  try {
    mirrorPatchChildren(doc, doc.body, parsed.body, st);
  } catch (e) {
    // never leave a half-patched page: the plain swap of before
    const frag = doc.createDocumentFragment();
    for (const node of Array.from(parsed.body.childNodes)) frag.appendChild(doc.importNode(node, true));
    doc.body.replaceChildren(frag);
    st.fallback = true;
    dlog("render", "warn", "mirror patch failed", {message: String(e && e.message || e).slice(0, 120)});
  }
  let top = startTop === null ? before : startTop;
  if (anchor) {
    const again = doc.querySelector('[data-m="' + CSS.escape(anchor.id) + '"]');
    if (again) top = scroller.scrollTop + (again.getBoundingClientRect().top - anchor.top);
  }
  if (scroller.scrollTop !== top) scroller.scrollTop = top;
  return st;
}

// v0.1.73 diagnostics: the Hub resources this mirror document asked for (Resource Timing of the iframe). A token
// asked more than once is a refetch: the phone loaded the same image again.
function mirrorResEntries(doc) {
  const out = {total: 0, tokens: 0, refetch: 0, cached: 0, kb: 0, slowest: 0};
  let entries = [];
  try { entries = doc.defaultView.performance.getEntriesByType("resource"); } catch (e) { return out; }
  const seen = new Map();
  for (const e of entries) {
    if (!/\/api\/rb\/mirror\/res\?/.test(e.name)) continue;
    const m = /[?&]r=([0-9a-f]+)/.exec(e.name);
    if (!m) continue;
    out.total++;
    seen.set(m[1], (seen.get(m[1]) || 0) + 1);
    if (e.transferSize === 0 && e.decodedBodySize > 0) out.cached++;
    out.kb += (e.transferSize || 0) / 1024;
    out.slowest = Math.max(out.slowest, e.duration || 0);
  }
  out.tokens = seen.size;
  for (const n of seen.values()) out.refetch += Math.max(0, n - 1);
  out.kb = Math.round(out.kb);
  out.slowest = Math.round(out.slowest);
  return out;
}

function mirrorDiagFor(rb, url) {
  if (!rb.mirrorDiag || rb.mirrorDiag.url !== url) {
    rb.mirrorDiag = {url, copies: 0, fresh: 0, morphs: 0, skipped: 0, docs: 0, refetchBefore: 0, last: null, patch: null,
      srv: null};
  }
  return rb.mirrorDiag;
}

function mirrorFailed(rb, url, empty) {
  const fail = rb.mirrorFail && rb.mirrorFail.url === url ? rb.mirrorFail : {url, n: 0};
  fail.n++;
  rb.mirrorFail = fail;
  return empty === true || fail.n >= 2;
}

function mirrorFallback(tab, rb) {
  rb.tilesFor = rb.url;
  rb.mirrorFail = null;
  tab.error = "";
  dlog("render", "warn", "mirror fallback", {url: hostLabel(rb.url)});
  toast("このページはミラー表示に対応していないため高速表示にしました");
  applyDisplay(tab);
}

async function runMirror(tab, opts = {}) {
  const rb = tab && tab.rb;
  if (!rb || tabDisplay(tab) !== "mirror" || challengeHold(tab)) return;
  if (rb.mirrorBusy) { rb.mirrorAgain = true; return; }
  const wait = rb.mirrorAt + Math.max(MIRROR_MIN_MS, rb.mirrorMs * 2) - Date.now();
  const navigation = !rb.mirror || rb.mirror.pageUrl !== rb.url || mirrorHost.owner !== rb;
  if (wait > 0 && !opts.immediate && !navigation) { scheduleMirror(tab, wait, opts.force === true); return; }
  if (rb.mirrorTouch && !navigation) { rb.mirrorDeferred = true; return; }       // never swap under a finger
  rb.mirrorBusy = true;
  snapBusy(rb, 1);
  const url = rb.url;
  try {
    const started = performance.now();
    const doc0 = mirrorDoc();
    const fresh = navigation || !doc0;
    // v0.1.59: the first copy of a page may already have come with the tab-open / navigate answer
    const early = fresh && opts.force !== true ? opts.prefetched : null;
    const data = early || await api("/api/rb/mirror", {tab: rb.id, full: !fresh, force: opts.force === true, partial: fresh});
    const elapsed = performance.now() - started;
    if (tab.rb !== rb || tabDisplay(tab) !== "mirror") return;
    if (!data || !Number.isFinite(data.rev) || !data.stats || !Number.isFinite(data.stats.nodes)) {
      throw new Error("ミラーのデータが不正です");
    }
    if (data.stats.nodes === 0) {
      // An early copy (taken at DOMContentLoaded or with the tab-open answer since v0.1.54 / v0.1.59) or a copy of a
      // page that is still loading may be empty: look again. Only two empty copies of a loaded page mean that the
      // page cannot be mirrored (it was switched to the fast display after one early empty copy before).
      const settled = rb.loading !== true && data.partial !== true && !early && !rb.chal;   // v0.1.65: not during a check
      dlog("render", "debug", "mirror empty copy", {settled, partial: data.partial === true, early: Boolean(early)});
      if (!settled || !mirrorFailed(rb, url, false)) { scheduleMirror(tab, 1000, true); return; }
      console.error("mirror: 0 nodes", data.url);
      mirrorFallback(tab, rb);
      return;
    }
    let applied = 0, loadMs = 0, patch = null;
    const diag = mirrorDiagFor(rb, url);
    if (fresh) {
      if (typeof data.doc !== "string" || !MIRROR_DOC_RE.test(data.doc)) throw new Error("ミラーのデータが不正です");
      const keep = rb.mirror && rb.mirror.pageUrl === url ? mirrorPosTop(rb, url) : 0;
      const t0 = performance.now();
      if (doc0 && mirrorHost.owner === rb && rb.mirror && rb.mirror.pageUrl === url) {
        diag.refetchBefore += mirrorResEntries(doc0).refetch;   // the old document's counts go with it
      }
      await mirrorLoadDoc(data.doc, mirrorHost.owner !== rb);
      loadMs = performance.now() - t0;
      diag.docs++;
      if (tab.rb !== rb || tabDisplay(tab) !== "mirror") return;
      const doc = mirrorDoc();
      wireMirrorDoc(doc);
      mirrorScroller(doc).scrollTop = keep || mirrorPosTop(rb, url);
      mirrorHost.owner = rb; mirrorHost.pageUrl = url; mirrorHost.last = null;
      chromeScrolled(mirrorScroller(doc).scrollTop, Infinity, "mirror");        // v0.1.60: a new page starts at the top: bars back
      $("mirrorWrap").style.visibility = "";
      applied = performance.now() - t0;
      tlWatchMirror(rb, doc);
    } else {
      if (mirrorUnsafe(data)) {
        console.error("mirror: refused unsafe content");
        dlog("render", "error", "mirror unsafe content refused", {});
        throw new Error("ミラーの内容を安全に表示できません");
      }
      const last = mirrorHost.last;
      // v0.1.42: a capture of ANOTHER page (the address changed after this request started, or the
      // page navigated by script) starts at the top / its remembered place, never at the old offset.
      const noHash = u => String(u || "").split("#")[0];
      const pageChanged = Boolean(rb.mirror && data.url && noHash(data.url) !== noHash(rb.mirror.url));
      if (!last || last.html !== data.html || last.css !== data.css || last.ha !== data.html_attrs || last.ba !== data.body_attrs) {
        const t0 = performance.now();
        patch = mirrorMorph(doc0, data, pageChanged ? mirrorPosTop(rb, url) : null);
        if (pageChanged) mirrorHost.pageUrl = url;
        applied = performance.now() - t0;
        diag.morphs++;
        diag.patch = patch;
        dlog("render", "debug", "mirror morph", {ms: Math.round(applied), rev: data.rev, kept: patch.kept,
          added: patch.added, removed: patch.removed, attrs: patch.attrs, text: patch.text});
      } else {
        diag.skipped++;
      }
      mirrorHost.last = {html: data.html, css: data.css, ha: data.html_attrs, ba: data.body_attrs};
    }
    const st = data.stats;
    diag.copies++;
    if (fresh) diag.fresh++;
    diag.srv = data.diag && typeof data.diag === "object" ? data.diag : diag.srv;
    diag.last = {api: early ? 0 : Math.round(elapsed), early: Boolean(early), load: Math.round(loadMs),
      apply: Math.round(applied), partial: data.partial === true, reused: data.reused === true,
      cap: Number.isFinite(st.ms) ? st.ms : null, js: Number.isFinite(st.js_ms) ? st.js_ms : null, patch};
    rb.mirror = {rev: data.rev, pageUrl: url, url: data.url, title: data.title, truncated: data.truncated === true};
    rb.mirrorAt = Date.now();
    rb.mirrorMs = Math.min(elapsed, 8000);
    rb.mirrorPending = false;
    rb.mirrorFail = null;
    rb.mirrorFollow = data.partial === true;        // v0.1.54: a first copy of a big page is complete only near the top
    rb.perf.mirror = {nodes: st.nodes, htmlBytes: st.html_bytes, cssBytes: st.css_bytes, images: st.images,
      ms: elapsed};
    dlog("render", "info", "mirror shown", {partial: data.partial === true, ms: Math.round(elapsed), apply_ms: Math.round(applied), nodes: st.nodes,
      html_kb: Math.round(st.html_bytes / 1024), css_kb: Math.round(st.css_bytes / 1024), res: st.res,
      fresh, reused: data.reused === true});
    if (data.truncated) flash("長いページのため、途中までを表示しています。");
    if (tab.error) tab.error = "";
    updateRemoteView(tab);
  } catch (e) {
    if (tab.rb !== rb || pageLeaving) return;     // v0.1.57: WebKit fails in-flight fetches ("Load failed") while the page is left
    if (e.status === 404) { remoteLost(tab); return; }
    // a dropped connection (WebKit "Load failed", Chrome "Failed to fetch") is not a bug: logged and retried below
    if (!(e instanceof TypeError && !e.status)) console.error("mirror failed:", e);
    dlog("render", "warn", "mirror failed", {message: String(e && e.message || e).slice(0, 120), status: e.status || 0});
    // a dropped connection says nothing about the page: it never counts toward the fallback, the copy is asked again
    const dropped = e instanceof TypeError && !e.status;
    if (dropped) scheduleMirror(tab, 1500, true);
    else if (mirrorFailed(rb, url, false)) { mirrorFallback(tab, rb); return; }
    if (!mirrorDoc() || mirrorHost.owner !== rb) {
      tab.error = "ミラー表示を作れませんでした: " + (e && e.message ? e.message : String(e));
      updateRemoteView(tab);
    }
    if (!opts.quiet) toast("ミラー表示を更新できませんでした: " + (e && e.message ? e.message : String(e)), "error");
  } finally {
    rb.mirrorBusy = false;
    snapBusy(rb, -1);
    if (rb.mirrorAgain && tab.rb === rb) {
      rb.mirrorAgain = false;
      runMirror(tab, {quiet: true}).catch(() => {});
    }
    if (rb.mirrorFollow && tab.rb === rb) {
      rb.mirrorFollow = false;
      runMirror(tab, {quiet: true, force: true, immediate: true}).catch(() => {});
    }
  }
}

// Look again after `ms` (after a tap, typed text, or while the minimum gap between captures runs out).
function scheduleMirror(tab, ms, force) {
  const rb = tab && tab.rb;
  if (!rb) return;
  if (force) rb.mirrorForce = true;
  if (rb.mirrorTimer) return;
  rb.mirrorTimer = setTimeout(() => {
    rb.mirrorTimer = 0;
    const forced = rb.mirrorForce === true;
    rb.mirrorForce = false;
    if (tab.rb !== rb || tabDisplay(tab) !== "mirror" || document.visibilityState !== "visible") return;
    runMirror(tab, {quiet: true, force: forced, immediate: true}).catch(() => {});
  }, Math.max(0, ms));
}

// "run" when a capture is due for this metadata, else "".
function mirrorAction(rb, meta, now) {
  if (rb.firstPending) { rb.heldMeta = meta; return ""; }     // v0.1.59: the answer of the request in flight carries the copy
  const m = rb.mirror;
  const changed = Boolean(m) && Number.isFinite(meta.mirror_rev) && meta.mirror_rev !== m.rev;
  if (meta.loading === true) {
    rb.mirrorPending = true;
    if (rb.mirrorEarly !== meta.url && loadedLongEnough(rb, meta, now, true)) {
      rb.mirrorEarly = meta.url;
      return "run";
    }
    // an ad-heavy page that never finishes loading still changes: follow it after the first early capture
    return rb.mirrorEarly === meta.url && changed && mirrorHost.owner === rb ? "run" : "";
  }
  noteLoadStart(rb, meta, now);
  if (!m || mirrorHost.owner !== rb || m.pageUrl !== meta.url || rb.mirrorPending || rb.mirrorEarly === meta.url) {
    rb.mirrorEarly = "";
    return "run";
  }
  return changed ? "run" : "";
}

async function mirrorLoop(tab, gen) {
  let fails = 0, first = true;
  const seed = takeSeed(tab.rb, "mirror");        // v0.1.59: the first copy came with the tab-open answer
  tlMark(tab.rb, "choose");
  await nextPaint();
  await nextPaint();
  if (gen !== state.rbGen || !tab.rb) return;
  if (seed) {
    first = false;
    applySeed(tab, seed);
    sendViewport(tab).catch(() => {});             // only a correction when the size sent with the request differed
  } else {
    await sendViewport(tab).catch(() => {});
  }
  while (gen === state.rbGen && tab.rb) {
    const rb = tab.rb;
    try {
      const wait = first ? 0 : rb.loading === true && rb.mirrorEarly !== rb.url ? RB_EARLY_POLL_S : RB_META_WAIT_S;
      let target = "/api/rb/meta?tab=" + encodeURIComponent(rb.id) + "&wait=" + wait;
      if (!first && rb.rev !== null) target += "&rev=" + rb.rev + probeQuery(rb);
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
      if (gen !== state.rbGen || tab.rb !== rb || tabDisplay(tab) !== "mirror") return;
      if (mirrorAction(rb, meta, Date.now()) === "run") runMirror(tab, {quiet: true}).catch(() => {});
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

// A tap on a link: the NAS browser goes there (the Hub validates the address); the loop captures the new page.
// v0.1.60: instant feedback for a link tap (mirror and reader), before anything has come back: the tapped element is
// highlighted, the thin progress line under the bar starts, and the current page stays as it is until the new copy
// replaces it. The loading flag is held for a moment so a status report that still says "not loading" cannot hide it.
const TAP_HOLD_MS = 1500;
function syncLoadingBars(tab) {
  if (activeWebTab() !== tab) return;
  setShown("webLoading", Boolean(tab.loading));
  setShown("remoteLoading", Boolean(tab.loading));
}

function tapFeedback(tab, el, cls) {
  if (el && el.classList) {
    el.classList.add(cls);
    setTimeout(() => { try { el.classList.remove(cls); } catch (e) { /* the page was replaced */ } }, 4000);
  }
  const rb = tab && tab.rb;
  if (!rb) return;
  rb.tapUntil = Date.now() + TAP_HOLD_MS;
  tab.loading = true;
  syncLoadingBars(tab);
  setTimeout(() => {
    if (!tab.rb || tab.rb !== rb || Date.now() < rb.tapUntil) return;
    tab.loading = Boolean(rb.loading);
    syncLoadingBars(tab);
  }, TAP_HOLD_MS + 50);
}

async function mirrorNavigate(tab, href) {
  const rb = tab && tab.rb;
  if (!rb) return;
  rb.firstPending = true;                          // v0.1.59: the answer carries the first copy of the new page
  try {
    const r = await api("/api/rb/navigate", {tab: rb.id, url: href, first: "mirror"});
    rb.mirrorPending = true;
    firstDone(tab, rb, r.first ? {kind: "mirror", first: r.first} : null);
  } catch (e) {
    firstDone(tab, rb, null);
    rb.tapUntil = 0; tab.loading = Boolean(rb.loading); syncLoadingBars(tab);       // v0.1.60: the tap failed: no progress line
    if (e.status === 404) remoteLost(tab);
    else throw e;
  }
}

// A tap on anything else: the NAS browser clicks the same element (the Hub finds it by its number).
async function mirrorClick(tab, el, event) {
  const rb = tab.rb;
  const m = Number(el.getAttribute("data-m"));
  if (!rb || !Number.isInteger(m) || m < 0) return;
  const box = el.getBoundingClientRect();
  const unit = (v, lo, size) => size > 0 ? Math.min(1, Math.max(0, (v - lo) / size)) : 0.5;
  const fx = Math.round(unit(event.clientX, box.left, box.width) * 1000) / 1000;
  const fy = Math.round(unit(event.clientY, box.top, box.height) * 1000) / 1000;
  snapBusy(rb, 1);
  try {
    rb.firstPending = true;                        // v0.1.59: a tapped link answers with the new page's first copy
    const res = await api("/api/rb/mirror/click", {tab: rb.id, m, fx, fy, first: "mirror"});
    const seed = res.navigated === true && res.first ? {kind: "mirror", first: res.first} : null;
    if (seed) rb.mirrorPending = true;
    firstDone(tab, rb, seed);
    if (tab.rb !== rb) return;
    if (typeof res.editable === "boolean") setEditable(tab, res.editable, true);
    if (res.navigated !== true) scheduleMirror(tab, MIRROR_CLICK_REFRESH_MS, true);
  } catch (e) {
    firstDone(tab, rb, null);
    if (tab.rb !== rb) return;
    if (e.status === 410) {            // the element is gone: the page changed since this copy
      flash("表示を更新しました。もう一度タップしてください");
      scheduleMirror(tab, 0, true);
    } else if (e.status === 404) {
      remoteLost(tab);
    } else {
      toast(e.message, "error");
    }
  } finally {
    snapBusy(rb, -1);
  }
}

function mirrorScrollToAnchor(doc, id) {
  const target = id === "top" ? null : doc.getElementById(id) || doc.getElementsByName(id)[0];
  if (target) target.scrollIntoView({block: "start"});
  else if (id === "top" || id === "") mirrorScroller(doc).scrollTop = 0;
}

// Listeners inside the iframe's document (the parent runs them; the frame itself runs no script).
// A probe registered for `doc` in mirrorProbes (v0.1.44, self-test only) receives every tap the handlers
// recognise instead of the app acting on it, so the self-test can prove the listeners fire in THIS browser.
const mirrorProbes = new WeakMap();

function wireMirrorDoc(doc) {
  if (!doc || mirrorWired.has(doc)) return;
  const probe = mirrorProbes.get(doc) || null;
  mirrorWired.add(doc);
  const tabNow = () => {
    const t = remoteTab();
    return t && t.rb && tabDisplay(t) === "mirror" && mirrorHost.owner === t.rb ? t : null;
  };
  const press = {timer: 0, ring: 0, id: null, x: 0, y: 0, el: null, fired: false};
  const stopPress = () => {
    clearTimeout(press.timer); clearTimeout(press.ring);
    press.timer = press.ring = 0;
    if (!press.fired) hidePressRing();
  };
  const offset = () => $("mirrorFrame").getBoundingClientRect();
  doc.addEventListener("pointerdown", event => {
    stopPress();
    press.fired = false;
    const link = mirrorClosest(event.target, "[data-href]");
    if (!link || (event.pointerType === "mouse" && event.button !== 0) || !tabNow()) return;
    press.el = link; press.x = event.clientX; press.y = event.clientY; press.id = event.pointerId;
    const o = offset();
    const sx = event.clientX + o.left, sy = event.clientY + o.top;
    press.ring = setTimeout(() => showPressRing(sx, sy), RB_RING_DELAY_MS);
    press.timer = setTimeout(() => {
      press.timer = 0;
      const tab = tabNow();
      if (!tab || !press.el) return;
      press.fired = true;
      showPressRing(sx, sy);
      $("remoteRing").classList.add("is-done");
      setTimeout(() => { hidePressRing(); }, 350);
      const href = press.el.getAttribute("data-href");
      const label = (press.el.textContent || "").trim();          // v0.1.65: the link's text names the job in the analysis list
      perform(() => startProbe(tab, {href, label}));         // the exact address is known: no hit test needed
    }, RB_LONGPRESS_MS);
  });
  doc.addEventListener("pointermove", event => {
    if (event.pointerId === press.id && (press.timer || press.ring) &&
        Math.hypot(event.clientX - press.x, event.clientY - press.y) > RB_LONGPRESS_MOVE_PX) stopPress();
  });
  // iOS Safari sends no "click" for elements it does not consider clickable (our links have no
  // href, menus are plain divs), so a tap is recognised from the pointer itself (v0.1.40).
  const tap = {id: null, x: 0, y: 0, at: 0, moved: false, handledAt: 0};
  doc.addEventListener("pointerdown", event => {
    if (event.pointerType === "mouse" && event.button !== 0) { tap.id = null; return; }
    tap.id = event.pointerId; tap.x = event.clientX; tap.y = event.clientY; tap.at = performance.now(); tap.moved = false;
  }, true);
  doc.addEventListener("pointermove", event => {
    if (event.pointerId === tap.id && Math.hypot(event.clientX - tap.x, event.clientY - tap.y) > RB_TAP_MOVE_PX) tap.moved = true;
  }, true);
  doc.addEventListener("pointerup", event => {
    const quick = event.pointerId === tap.id && !tap.moved && performance.now() - tap.at < RB_LONGPRESS_MS;
    tap.id = null;
    stopPress();
    if (!quick || press.fired) return;
    tap.handledAt = performance.now();
    handleTap(event);
  });
  doc.addEventListener("pointercancel", () => { tap.id = null; stopPress(); });
  // v0.1.63: a right click (mouse) on a link opens the app's own menu; a long press (touch) keeps the link check
  doc.addEventListener("contextmenu", event => {
    event.preventDefault();
    if (probe || !(event.button === 2 || event.ctrlKey)) return;
    const tab = tabNow();
    const link = tab && mirrorClosest(event.target, "[data-href]");
    const url = link && link.getAttribute("data-href");
    if (!url || !/^https?:\/\/\S+$/i.test(url)) { closeLinkMenu(); return; }
    const o = offset();
    openLinkMenu(tab, url, event.clientX + o.left, event.clientY + o.top);
  });
  wireInputDoc(doc);                 // v0.1.63: keys, mouse buttons 4 / 5 and sideways trackpad swipes inside the frame
  doc.addEventListener("submit", event => { event.preventDefault(); });
  // The copy's controls are inert: no focus, no keyboard of their own (typing goes to the NAS browser).
  doc.addEventListener("mousedown", event => {
    if (mirrorClosest(event.target, "input,textarea,select")) event.preventDefault();
  });
  doc.addEventListener("focusin", event => {
    const el = event.target;
    if (el && el.blur && mirrorClosest(el, "input,textarea,select")) el.blur();
  });
  doc.addEventListener("click", event => {
    event.preventDefault();
    if (press.fired) { press.fired = false; return; }       // the long press already started a probe
    if (performance.now() - tap.handledAt < 800) return;    // the pointer already handled this tap
    handleTap(event);
  });
  function handleTap(event) {
    gestureTrace("mirror.tap", {type: event.type});
    if (probe) { probe.tap(event); return; }
    const tab = tabNow();
    if (!tab) return;
    const el = mirrorClosest(event.target, "[data-m]");
    // v0.1.42: menu toggles (ARIA/toggle markers, buttons, links to this page) are clicked on the NAS
    // so the page's own script opens them; navigating would only reload the page.
    const toggle = mirrorClosest(event.target, MIRROR_TOGGLE);
    const link = mirrorClosest(event.target, "[data-href]");
    if (link && !(toggle && (link.contains(toggle) || toggle.contains(link)))) {
      tapFeedback(tab, link, "qd-tap");              // v0.1.60: before the request, same task as the tap
      perform(() => mirrorNavigate(tab, link.getAttribute("data-href")));
      return;
    }
    const frag = !toggle && mirrorClosest(event.target, "[data-frag]");
    if (frag) mirrorScrollToAnchor(doc, frag.getAttribute("data-frag"));   // and the page may react too
    if (el) perform(() => mirrorClick(tab, el, event));
  }
  // Native scrolling: remember the place per page, and hold recaptures back while a finger is down.
  let scrollTick = 0;
  doc.addEventListener("scroll", () => {
    if (scrollTick) return;
    scrollTick = requestAnimationFrame(() => {
      scrollTick = 0;
      const owner = mirrorHost.owner;
      if (owner) mirrorRememberTop(owner, mirrorHost.pageUrl, mirrorScroller(doc).scrollTop);
      if (tabNow()) { const sc = mirrorScroller(doc); chromeScrolled(sc.scrollTop, sc.scrollHeight - sc.clientHeight, "mirror"); }   // v0.1.60
    });
  }, {passive: true});
  const touching = on => () => {
    const tab = remoteTab();
    const rb = tab && tab.rb;
    if (!rb) return;
    rb.mirrorTouch = on;
    if (!on && rb.mirrorDeferred) {
      rb.mirrorDeferred = false;
      setTimeout(() => { if (tab.rb === rb) runMirror(tab, {quiet: true}).catch(() => {}); }, MIRROR_TOUCH_RESUME_MS);
    }
  };
  doc.addEventListener("touchstart", touching(true), {passive: true});
  doc.addEventListener("touchend", touching(false), {passive: true});
  doc.addEventListener("touchcancel", touching(false), {passive: true});
  attachFlick(doc);
  if (swipeRec) swipeRec.hookDoc(doc);
  wirePull($("mirrorFrame"), () => {
    const tab = tabNow();
    if (tab) perform(() => runMirror(tab, {force: true, immediate: true}));
  }, doc, () => mirrorScroller(doc).scrollTop);
}

// ---- long-press link analysis queue (v0.1.65) ----
// A long press only ADDS A JOB on the Hub (POST /api/rb/probe answers at once with {job, position}); the Hub analyses
// the links one by one, waits for memory instead of failing, keeps the results for 30 minutes and (setting
// probe_autosend) sends what it finds to JD2 by itself. This side shows the counts (they ride on the tab's meta
// long-poll: rb.probes = {rev, queued, running, done, failed, total, review}), a pill while something is going on,
// and a sheet that lists the jobs (GET /api/rb/probe/jobs) only when the user opens it or a result needs a decision.
// A Rapidgator link needs no analysis: the Hub answers with the finished result and it is handled as before.
const probeUi = {open: false, jobs: [], local: [], tabId: "", notified: new Set(), loading: false, gen: 0};

function probeCounts(tab) {
  const p = tab && tab.rb && tab.rb.probes;
  return p && Number.isFinite(p.total) ? p : null;
}

// "&pr=<rev>": the meta long-poll also ends when one of the tab's analysis jobs changes (v0.1.65).
function probeQuery(rb) {
  return rb && rb.probes && Number.isFinite(rb.probes.rev) ? "&pr=" + rb.probes.rev : "";
}

function closeProbeSheet() {
  probeUi.open = false;
  probeUi.local = [];
  state.probeResult = null;
  setShown("probeSheet", false);
}

function renderProbeChip() {
  const tab = remoteTab();
  const p = probeCounts(tab);
  const active = p ? p.queued + p.running : 0;
  const show = Boolean(p && (active > 0 || p.failed > 0 || p.review > 0));
  if (show) {
    $("probeChip").textContent = "リンク解析 " + (p.done + p.failed) + "/" + p.total + (p.failed ? "・失敗" + p.failed : "") +
      (active === 0 && p.review ? "・要確認" : "");
  }
  setShown("probeChip", show);
  const item = $("menuProbes");
  if (item) {
    item.hidden = !(p && p.total > 0);
    if (p) item.textContent = "リンク解析の一覧（" + p.total + "件）";
  }
}

// Called with every meta answer of a tab: remembers the counts and refreshes the pill. The list is fetched (one request,
// after a short pause so a burst makes one) only while the sheet is open, or once the queue has run dry with results
// nobody was told about: then ONE toast says what happened (nothing is announced while jobs are still running).
const PROBE_FETCH_MS = 700;

function noteProbes(tab, p) {
  const rb = tab.rb;
  if (!rb || !p || !Number.isFinite(p.rev)) return;
  const before = rb.probes || null;
  rb.probes = p;
  const finished = (p.done || 0) + (p.failed || 0);
  if (rb.probeAnnounced === undefined) {
    rb.probeAnnounced = finished;          // results from before this page was loaded are listed, not announced
    if (finished) probeFetchSoon(tab, false);
  }
  if (tab === remoteTab()) renderProbeChip();
  if (before && before.rev === p.rev) return;
  const idle = p.queued + p.running === 0;
  const news = idle && finished > rb.probeAnnounced;
  if (news || (probeUi.open && probeUi.tabId === rb.id)) probeFetchSoon(tab, news);
}

function probeFetchSoon(tab, announce) {
  const rb = tab.rb;
  if (!rb) return;
  rb.probeFetchAnnounce = Boolean(rb.probeFetchAnnounce || announce);
  if (rb.probeFetch) return;
  rb.probeFetch = setTimeout(() => {
    rb.probeFetch = 0;
    const wanted = rb.probeFetchAnnounce;
    rb.probeFetchAnnounce = false;
    if (tab.rb !== rb) return;
    if (wanted && rb.probes) rb.probeAnnounced = (rb.probes.done || 0) + (rb.probes.failed || 0);
    loadProbeJobs(tab, {announce: wanted}).catch(() => {});
  }, PROBE_FETCH_MS);
}

async function loadProbeJobs(tab, opts = {}) {
  const rb = tab && tab.rb;
  if (!rb) return;
  const gen = ++probeUi.gen;
  const data = await api("/api/rb/probe/jobs?tab=" + encodeURIComponent(rb.id));
  if (tab.rb !== rb || gen !== probeUi.gen) return;
  probeUi.jobs = data.jobs || [];
  probeUi.tabId = rb.id;
  const fresh = probeUi.jobs.filter(j => (j.state === "done" || j.state === "failed") && !probeUi.notified.has(j.id));
  for (const j of fresh) probeUi.notified.add(j.id);
  if (probeUi.notified.size > 500) probeUi.notified = new Set(Array.from(probeUi.notified).slice(-250));
  if (probeUi.open) renderProbeSheet();
  if (opts.announce && fresh.length) announceProbes(tab, fresh);
}

// A result is ready: say so in one toast; open the sheet when links need a decision (not sent to JD2 by the Hub).
function announceProbes(tab, jobs) {
  const failed = jobs.filter(j => j.state === "failed");
  const done = jobs.filter(j => j.state === "done");
  const sentJobs = done.filter(j => j.sent && !j.sent.error && j.sent.sent > 0);
  const sentCount = sentJobs.reduce((n, j) => n + j.sent.sent, 0);
  const review = done.filter(j => j.links.length && !(j.sent && !j.sent.error));
  const empty = done.filter(j => !j.links.length);
  const parts = [];
  if (sentCount) parts.push("JD2へ送信 " + sentCount + "件");
  const sending = Boolean(state.settings && state.settings.probe_autosend !== false) && jd2CanSend();
  if (review.length) parts.push((sending ? "JD2へ送信中 " : "要確認 ") + review.length + "件");     // v0.1.67
  if (empty.length) parts.push("リンクなし " + empty.length + "件");
  if (done.length && !parts.length) parts.push("重複のため送信済み");
  if (failed.length) toast("リンク解析に失敗: " + failed.length + "件（" + (failed[0].label || failed[0].host) + "："
    + (failed[0].error || "不明") + "）。一覧から再試行できます", "error");
  if (done.length) flash("リンク解析が終わりました（" + parts.join("・") + "）");
  // v0.1.67: with automatic sending on, an unsent result is not a decision for the user yet: the Hub records its send a
  // moment after the job ends (this list may be read in between). The sheet opens only for failures, or for results
  // the fallback below could not send.
  const auto = Boolean(state.settings && state.settings.probe_autosend !== false) && jd2CanSend();
  if (failed.length || (review.length && !auto)) { probeUi.open = true; probeUi.tabId = tab.rb.id; renderProbeSheet(); }
  if (review.length && auto) probeAutosendFallback(tab, review.map(j => j.id));
}

// v0.1.66: the Hub sends finished results to JD2 by itself (probe_autosend). If one is still unsent a moment later (JD2
// was busy, or the Hub's attempt failed), this phone sends it, as before the queue; the Hub skips links already sent.
const PROBE_FALLBACK_MS = 4000;
function probeAutosendFallback(tab, ids) {
  const rb = tab && tab.rb;
  if (!rb || !ids.length) return;
  setTimeout(() => perform(async () => {
    if (tab.rb !== rb || !(state.settings && state.settings.probe_autosend !== false) || !jd2CanSend()) return;
    const data = await api("/api/rb/probe/jobs?tab=" + encodeURIComponent(rb.id));
    const still = (data.jobs || []).filter(j => ids.includes(j.id) && j.state === "done" && j.links.length && !(j.sent && !j.sent.error));
    let sent = 0, error = "";
    for (const job of still) {
      const r = await api("/api/rb/probe/send", {tab: rb.id, job: job.id});
      sent += r.sent || 0;
      if (r.error && !error) error = r.error;
    }
    if (still.length) {
      dlog("rb", "info", "probe autosend fallback", {jobs: still.length, sent, error: error.slice(0, 80)});
      if (error) toast("JD2へ送れませんでした: " + error, "error");
      else if (sent) flash("リンク解析の結果をJD2へ送りました（" + sent + "件）");
      await loadProbeJobs(tab);
      if (error) { probeUi.open = true; probeUi.tabId = rb.id; renderProbeSheet(); }    // now it needs the user
    }
  }), PROBE_FALLBACK_MS);
}

const PROBE_STATE_LABEL = {queued: "待機", running: "解析中", done: "完了", failed: "失敗", cancelled: "取り消し"};

function probeJobRows() {
  return probeUi.local.concat(probeUi.jobs.filter(j => j.state !== "cancelled"));
}

function renderProbeSheet() {
  const canSend = jd2CanSend();
  const rows = probeJobRows();
  const all = [];
  const groups = rows.map(job => {
    const group = element("div", undefined, "probe-group probe-job is-" + job.state);
    const head = element("div", undefined, "probe-job-head");
    head.appendChild(element("span", job.label || job.host || "リンク", "probe-job-label"));
    let status = PROBE_STATE_LABEL[job.state] || job.state;
    if (job.state === "queued") status = job.wait === "memory" ? "メモリ待ち" : "待機 " + job.position + "番目";
    if (job.state === "done" && job.sent && !job.sent.error && job.sent.sent) status = "送信済み " + job.sent.sent + "件";
    head.appendChild(element("span", status, "probe-job-state"));
    group.appendChild(head);
    const sub = [];
    if (job.host && job.label !== job.host) sub.push(job.host);
    if (job.state === "done") {
      sub.push("リダイレクト " + job.redirects + "回");
      if (job.final_url) sub.push("最終: " + (hostLabel(job.final_url) || "不明"));
    }
    if (job.state === "queued" && job.wait === "memory" && job.note) sub.push(job.note);
    if (job.state === "failed" && job.error) sub.push(job.error);
    else if (job.note && job.state !== "queued") sub.push(job.note);
    if (job.sent && job.sent.error) sub.push("送信失敗: " + job.sent.error);
    if (sub.length) group.appendChild(element("div", sub.join(" · "), "probe-group-head"));
    const jobSent = Boolean(job.sent && !job.sent.error);      // v0.1.67: already with JD2: not counted in 「すべて送る」
    for (const url of job.links || []) {
      if (!jobSent) all.push(url);
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
    if (job.state === "failed" && job.id && !job.local) {
      const retry = element("button", "再試行", "btn small");
      retry.type = "button";
      retry.addEventListener("click", () => perform(() => probeAction("retry", job.id)));
      group.appendChild(retry);
    }
    return group;
  });
  $("probeList").replaceChildren(...(groups.length ? groups : [element("div", "リンク解析はありません", "muted")]));
  const counts = {queued: 0, failed: 0};
  for (const job of rows) { if (job.state === "queued") counts.queued++; if (job.state === "failed" && !job.local) counts.failed++; }
  state.probeResult = {links: all};
  $("probeTitle").textContent = "リンク解析" + (rows.length ? "（" + rows.length + "件）" : "");
  $("probeSendAll").textContent = "すべてJD2へ送る（" + all.length + "件）";
  $("probeAddAll").textContent = "リンクに追加（" + all.length + "件）";
  $("probeRetryAll").textContent = "失敗を再試行（" + counts.failed + "件）";
  $("probeCancelAll").textContent = "待ちを取り消す（" + counts.queued + "件）";
  setShown("probeSendAll", canSend && all.length > 0);
  setShown("probeAddAll", all.length > 0);
  setShown("probeRetryAll", counts.failed > 0);
  setShown("probeCancelAll", counts.queued > 0);
  setShown("probeClear", rows.some(j => j.state === "done" || j.state === "failed"));
  setShown("probeSheet", true);
}

async function probeAction(kind, job) {
  const tab = remoteTab();
  if (!tab) return;
  const rb = tab.rb;
  await api("/api/rb/probe/" + kind, job ? {tab: rb.id, job} : {tab: rb.id});
  await loadProbeJobs(tab);
}

async function openProbeSheet(tab) {
  if (!tab || !tab.rb) return;
  probeUi.open = true;
  probeUi.tabId = tab.rb.id;
  renderProbeSheet();
  await loadProbeJobs(tab);
}

// Long press found hoster links of a RAPIDGATOR link (no analysis): hand them to JD2 without a dialog (setting probe_autosend).
// Returns true when handled (toast shown); false means the result goes to the sheet.
async function autoSendProbe(result, rb, item) {
  if (!result.links.length || !result.probe_id) return false;
  await loadSettings().catch(() => {});   // cached; can_send comes from the state loop
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
      const dupText = r.skipped ? "・" + r.skipped + "件は重複のためスキップ" : "";
      flashCount("sent", r.sent, n => "JD2へ送信しました（" + n + "件）" + dupText);
    }
    return true;
  } catch (e) {
    toast("JD2へ送信できませんでした: " + e.message, "error");
    return true;
  }
}

let probeLocalSerial = 0;

async function startProbe(tab, point) {
  const rb = tab.rb;
  if (!rb) return;
  const body = point.href ? {tab: rb.id, href: point.href} :
    point.snap ? {tab: rb.id, x: point.x, y: point.y, snap: point.snap} : {tab: rb.id, x: point.x, y: point.y};
  if (point.label) body.label = String(point.label).slice(0, 80);
  try {
    const result = await api("/api/rb/probe", body);
    if (tab.rb !== rb) return;
    if (!Array.isArray(result.links)) {
      // queued: answer at once. The counts follow with the next meta answer; show them now.
      const p = rb.probes || {rev: 0, queued: 0, running: 0, done: 0, failed: 0, total: 0, review: 0};
      if (!result.existing) rb.probes = Object.assign({}, p, {queued: p.queued + 1, total: p.total + 1});
      renderProbeChip();
      if (result.existing) {
        flash(result.state === "done" ? "同じリンクは解析済みです" : "同じリンクはすでに解析待ちです（" + result.position + "件目）");
        if (result.state === "done") openProbeSheet(tab).catch(() => {});
      } else {
        flash("解析待ちに追加（" + result.position + "件目）");
      }
      return;
    }
    if (!result.links.length) {
      const host = hostLabel(result.final_url);
      flash("リンクは見つかりませんでした" + (host ? "（最終: " + host + "）" : ""));
      return;
    }
    if (await autoSendProbe(result, rb, null)) return;
    if (tab.rb !== rb) return;
    probeUi.local.push({id: "L" + (++probeLocalSerial), local: true, state: "done", label: hostLabel(result.start_url) || "リンク",
      host: hostLabel(result.start_url), links: result.links, redirects: result.redirects || 0, final_url: result.final_url});
    probeUi.open = true;
    probeUi.tabId = rb.id;
    renderProbeSheet();
  } catch (e) {
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

// Lazy: the NAS browser handlers are attached the first time a remote tab is opened or shown.
let remoteWired = false;
function ensureRemote() {
  if (remoteWired) return;
  remoteWired = true;
  wireRemote();
}

// v0.1.37 gestures on the NAS browser:
// - a quick horizontal flick over the page goes back (finger to the right) / forward;
//   flicks starting at the screen edges are left to iOS.
// - dragging down from the toolbar closes the tab (a hint shows while armed).
const GESTURE_FLICK_PX = 90;
const GESTURE_FLICK_MS = 450;
const GESTURE_EDGE_PX = 24;
const GESTURE_CLOSE_PX = 110;

function gestureHint(text) {
  const hint = $("gestureHint");
  hint.textContent = text || "";
  hint.hidden = !text;
  gestureTrace("gesture.hint", {text: text || ""});
}

// v0.1.52 swipe diagnostic hook: a no-op unless a diagnostic is recording (swipeRec is set).
let swipeRec = null;
function gestureTrace(event, data) {
  if (swipeRec) swipeRec.note(event, data);
}

// A flick on the page and a browser back (popstate) that arrive together are one gesture: the first one
// to ask takes the step, the other within STEP_DEDUP_MS is dropped (v0.1.52).
const STEP_DEDUP_MS = 400;
const stepClaim = {at: 0, kind: ""};
function claimStep(kind) {
  const now = performance.now();
  if (stepClaim.kind && stepClaim.kind !== kind && now - stepClaim.at < STEP_DEDUP_MS) return false;
  stepClaim.at = now;
  stepClaim.kind = kind;
  return true;
}

// The handlers are kept so that the swipe diagnostic can feed them when synthetic TouchEvents cannot be built.
const flickHandlers = new WeakMap();
const pullHandlers = {};

// v0.1.55: the nearest ancestor (inside the touched element's own document) that scrolls sideways, or null.
// A swipe that starts on such an element (a wide table) scrolls it; it is never a back / forward flick.
function hScroller(target) {
  try {
    for (let el = target; el && el.nodeType === 1; el = el.parentElement) {
      const doc = el.ownerDocument;
      if (el === doc.body || el === doc.documentElement) break;
      if (el.scrollWidth > el.clientWidth + 2) {
        const ox = doc.defaultView.getComputedStyle(el).overflowX;
        if (ox === "auto" || ox === "scroll" || ox === "overlay") return el;
      }
    }
  } catch (e) { /* no layout: not a scroller */ }
  return null;
}

// The flick listener is attached to the remote view and, for the mirror display, inside its iframe
// (touches in a frame do not bubble to the parent).
function attachFlick(view) {
  let flick = null;
  const onStart = event => {
    flick = null;
    if (event.touches.length !== 1) { gestureTrace("flick.reject", {why: "multi"}); return; }
    const t = event.touches[0];
    if (t.clientX < GESTURE_EDGE_PX || t.clientX > window.innerWidth - GESTURE_EDGE_PX) {
      gestureTrace("flick.reject", {why: "edge", x: Math.round(t.clientX)});
      return;
    }
    if (hScroller(event.target)) { gestureTrace("flick.reject", {why: "hscroll", x: Math.round(t.clientX)}); return; }
    flick = {x: t.clientX, y: t.clientY, at: performance.now()};
  };
  const onMove = event => { if (event.touches.length !== 1) flick = null; };
  const onEnd = event => {
    const f = flick;
    flick = null;
    if (!f || event.changedTouches.length !== 1) return;
    const t = event.changedTouches[0];
    const dx = t.clientX - f.x, dy = t.clientY - f.y, dt = performance.now() - f.at;
    const info = {dx: Math.round(dx), dy: Math.round(dy), dt: Math.round(dt)};
    if (dt > GESTURE_FLICK_MS) { gestureTrace("flick.reject", Object.assign({why: "slow"}, info)); return; }
    if (Math.abs(dx) < GESTURE_FLICK_PX) { gestureTrace("flick.reject", Object.assign({why: "short"}, info)); return; }
    if (Math.abs(dx) < 2.5 * Math.abs(dy)) { gestureTrace("flick.reject", Object.assign({why: "diagonal"}, info)); return; }
    if (!$("readerViewer").hidden) { gestureTrace("flick.reject", Object.assign({why: "viewer"}, info)); return; }   // the image viewer swipes between images
    const tab = activeWebTab();
    if (!tab || !tab.rb) { gestureTrace("flick.reject", Object.assign({why: "no-tab"}, info)); return; }
    if (!claimStep("flick")) { gestureTrace("flick.reject", Object.assign({why: "dup-pop"}, info)); return; }
    state.gestureAt = Date.now();
    gestureTrace("flick.accepted", Object.assign({dir: dx > 0 ? "back" : "forward"}, info));
    dlog("ui", "info", "gesture", {kind: dx > 0 ? "back" : "forward"});
    gestureHint(dx > 0 ? "← 戻る" : "進む →");
    setTimeout(() => gestureHint(""), 600);
    perform(() => stepHistory(tab, dx > 0 ? -1 : 1));
  };
  flickHandlers.set(view, {start: onStart, move: onMove, end: onEnd});
  view.addEventListener("touchstart", onStart, {passive: true});
  view.addEventListener("touchmove", onMove, {passive: true});
  view.addEventListener("touchend", onEnd, {passive: true});
}

function wireGestures() {
  const view = $("remoteView");
  attachFlick(view);

  const bar = document.querySelector(".toolbar");
  let pull = null;
  const onStart = event => {
    pull = null;
    const tab = activeWebTab();
    if (!tab || event.touches.length !== 1 || !event.target.closest) return;
    // v0.1.55: on a phone the toolbar is mostly the address bar: a pull may start on it while it is not focused
    // (a tap still focuses it to type; a field that has the keyboard keeps its own touches).
    const field = event.target.closest("input,textarea");
    if (event.target.closest(".menu") || (field && (field.id !== "omnibox" || document.activeElement === field))) return;
    pull = {x: event.touches[0].clientX, y: event.touches[0].clientY, armed: false, id: tab.id};
    gestureTrace("pull.start", {});
  };
  const onMove = event => {
    if (!pull || event.touches.length !== 1) return;
    const t = event.touches[0];
    const armed = t.clientY - pull.y > GESTURE_CLOSE_PX && Math.abs(t.clientX - pull.x) < 80;
    if (armed !== pull.armed) {
      pull.armed = armed; gestureTrace("pull.arm", {armed}); gestureHint(armed ? "離すとタブを閉じます" : "");
      if (armed && document.activeElement === $("omnibox")) $("omnibox").blur();     // v0.1.55: no keyboard under a pull
    }
  };
  const end = () => {
    const p = pull;
    pull = null;
    gestureHint("");
    gestureTrace("pull.end", {armed: Boolean(p && p.armed)});
    if (!p || !p.armed) return;
    const tab = activeWebTab();
    if (tab && tab.id === p.id) { dlog("ui", "info", "gesture", {kind: "close"}); closeTab(tab.id); }
  };
  const cancel = () => { pull = null; gestureHint(""); };
  Object.assign(pullHandlers, {start: onStart, move: onMove, end, cancel});
  // v0.1.55: the status line right under the toolbar is a pull area too.
  for (const el of [bar, $("webStatus")]) {
    el.addEventListener("touchstart", onStart, {passive: true});
    el.addEventListener("touchmove", onMove, {passive: true});
    el.addEventListener("touchend", end, {passive: true});
    el.addEventListener("touchcancel", cancel, {passive: true});
  }
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
  $("probeChip").addEventListener("click", () => perform(() => openProbeSheet(remoteTab())));
  $("probeRetryAll").addEventListener("click", () => perform(() => probeAction("retry")));
  $("probeCancelAll").addEventListener("click", () => perform(() => probeAction("cancel")));
  $("probeClear").addEventListener("click", () => perform(async () => {
    probeUi.local = [];
    await probeAction("dismiss");
  }));
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
  $("rbNotReadyBtn").addEventListener("click", () => {
    if ($("rbNotReadyBtn").dataset.mode !== "retry") { navigate("#/settings/browser/nas"); return; }
    const tab = activeWebTab();
    if (!tab) return;
    tab.error = ""; tab.reopenAt = 0;
    updateRemoteView(tab);
  });
  $("chalLiveBtn").addEventListener("click", () => chalGoLive(activeWebTab()));
  document.addEventListener("visibilitychange", syncRemotePoll);
  wireTiles();
  wireReader();
  wireGestures();
}

function refreshAfterTabChange() {
  if (state.route.view === "tab") {
    const shown = currentViewName();
    for (const name of ["home", "web"]) $("view-" + name).hidden = name !== shown;
  }
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

const navigateTab = (tab, value) => openRemote(tab, value, url => pushHistory(tab, url));
const reloadTab = tab => tab.pt ? ptReload(tab) : tab.rb ? rbInputFor(tab, {type: "reload"}) : openInRemote(tab);

async function rbInputFor(tab, event) {
  try {
    return await api("/api/rb/input", Object.assign({tab: tab.rb.id}, event));
  } catch (e) {
    if (e.status === 404) remoteLost(tab);
    else throw e;
  }
}

// A browser back / forward of the NAS page (one page per gesture: the Hub skips redirects and check pages).
// Nothing to go back to is told in one short toast; it never leaves the tab (v0.1.52).
async function stepHistory(tab, delta) {
  if (tab.pt) return ptStep(tab, delta);      // v0.1.57
  if (!tab.rb) return null;
  gestureTrace("step.call", {delta});
  let res = null;
  try {
    res = await rbInputFor(tab, {type: delta < 0 ? "back" : "forward"});
  } catch (e) {
    gestureTrace("step.error", {msg: String(e && e.message || e).slice(0, 40)});
    throw e;
  }
  if (res && res.moved === false) {
    gestureTrace("step.nomove", {});
    gestureTrace("toast", {delta});
    flash(delta < 0 ? "これより前のページはありません" : "これより先のページはありません");
  } else if (res) {
    gestureTrace("step.moved", {skipped: res.skipped || 0});
  }
  return res;
}

// v0.1.50: on a web page, the browser's own back (the iOS edge swipe in the home-screen app) goes
// back in THAT page, like Safari. A guard entry with the same address sits on top of the tab's
// history entry; popping it means "back": the NAS tab steps back and the guard is put again.
// v0.1.52: when the page has nothing earlier the guard is just put again (a toast says so): the app
// never leaves the tab by a page back. Only the display switches (auto-live, mirror reloads) never push.
function armBackGuard() {
  const r = state.route;
  if (r.view !== "tab" || !findTab(r.tabId)) return;
  if (history.state && history.state.qdGuard === r.tabId) return;
  try {
    history.pushState({qdGuard: r.tabId}, "", location.hash);
    gestureTrace("guard.push", {len: history.length});
  } catch (e) { /* ignore */ }
}

// Session history also restores the frame's address of the entry being returned to; put the
// current copy back so the mirror never shows an older copy (or nothing) after a back.
function keepMirrorFrame() {
  const mf = $("mirrorFrame");
  const doc = mf && mf.dataset.doc;
  if (!doc || $("mirrorWrap").hidden) return;
  // v0.1.52: a document that is loaded again has none of the app's listeners: wire it when it is ready.
  const rewire = () => { try { const d = mirrorDoc(); if (d && d.body) wireMirrorDoc(d); } catch (e) { /* ignore */ } };
  try {
    const want = new URL(doc, location.href).href;
    if (mf.contentWindow.location.href !== want) {
      mf.addEventListener("load", rewire, {once: true});
      mf.contentWindow.location.replace(want);
      gestureTrace("mirror.reload", {});
    }
  } catch (e) {
    try {
      mf.addEventListener("load", rewire, {once: true});
      mf.contentWindow.location.replace(new URL(doc, location.href).href);
    } catch (e2) { /* ignore */ }
  }
}

// v0.1.55: a page back that landed on an OLDER APP ENTRY (WebKit skips history entries that were pushed without a
// user gesture, e.g. the guard put again after an async step) must still stay on the web tab: the tab is put back
// on top of the stack and the NAS page steps back. The hashchange that follows is dropped (nothing changed).
let skipHashChangeAt = -1e9;
function onHashChange() {
  if (performance.now() - skipHashChangeAt < 800 && state.route.view === "tab" && location.hash === "#/tab/" + state.route.tabId) {
    skipHashChangeAt = -1e9;
    gestureTrace("hashchange.skip", {});
    return;
  }
  applyRoute();
}

function onPopState() {
  // The traversal restores the frame's older document a little after popstate: look again a few times.
  for (const ms of [0, 120, 400, 1000]) setTimeout(keepMirrorFrame, ms);
  const r = state.route;
  gestureTrace("popstate", {hash: location.hash, guard: (history.state && history.state.qdGuard) || "-", len: history.length});
  if (swipeDiag && swipeDiag.cleaning) return;
  if (!state.authed || r.view !== "tab") { gestureTrace("pop.ignore", {why: "route"}); return; }
  if (history.state && history.state.qdGuard === r.tabId) { gestureTrace("pop.ignore", {why: "guard"}); return; }   // forward onto the guard
  const m = /^#\/tab\/(t\d+)/.exec(location.hash);
  if (!m || m[1] !== r.tabId) {
    const tab = findTab(r.tabId);
    if (tab && (tab.rb || tab.pt) && performance.now() - appNavAt > APP_NAV_MS) {
      // Not the app's own navigation: the person swiped back. Stay on the tab, step the NAS page back.
      gestureTrace("pop.stay", {from: location.hash});
      skipHashChangeAt = performance.now();
      try { history.pushState({qdGuard: r.tabId}, "", "#/tab/" + r.tabId); gestureTrace("guard.push", {len: history.length}); } catch (e) { /* ignore */ }
      if (!claimStep("pop")) { gestureTrace("pop.ignore", {why: "dup-flick"}); return; }
      perform(async () => {
        try { await stepHistory(tab, -1); } finally { armBackGuard(); }
      });
      return;
    }
    gestureTrace("pop.ignore", {why: "other-route"});   // a real route change: hashchange
    return;
  }
  const tab = findTab(r.tabId);
  if (!tab || !(tab.rb || tab.pt)) { armBackGuard(); return; }
  if (!claimStep("pop")) { gestureTrace("pop.ignore", {why: "dup-flick"}); armBackGuard(); return; }
  perform(async () => {
    try { await stepHistory(tab, -1); } finally { armBackGuard(); }
  });
}

// Open a URL typed/clicked by the user: in the active web tab, else a new tab. Always the NAS browser.
// v0.1.62: an address whose site has passthrough remembered, when passthrough is available now: the plain https
// address to open in the frame, else "" (the NAS browser opens it with the site's remembered or the default display).
// Decided on this device from what it already knows (no request, so opening a page costs nothing extra).
function rememberedPtUrl(value) {
  if (!ptAvailable() || typeof value !== "string") return "";
  const v = value.trim();
  const url = /^https:\/\/[^\s]+$/i.test(v) ? v : /^[a-z0-9-]+(\.[a-z0-9-]+)+(:\d+)?(\/\S*)?$/i.test(v) ? "https://" + v : "";
  return url && url.length <= 2048 && rememberedMode(hostOf(url)) === REMOTE_PT_MODE ? url : "";
}

async function openRememberedPt(tab, url) {
  if (tab.pt) { pushHistory(tab, url); tab.current = url; tab.ptTitle = ""; saveTabs(); await ptGo(tab, url); return; }
  pushHistory(tab, url);
  tab.current = url;
  saveTabs();
  await enterPt(tab);
}

async function openWebUrl(value) {
  const tab = activeWebTab();
  const ptUrl = rememberedPtUrl(value);
  if (ptUrl && (tab || !tabLimitReached())) {     // v0.1.62: the site's remembered display is passthrough
    const target = tab || createTab();
    if (!tab) navigate("#/tab/" + target.id);
    await openRememberedPt(target, ptUrl);
    return;
  }
  if (tab) { await (tab.pt ? ptNavigateTo(tab, value) : navigateTab(tab, value)); return; }   // v0.1.57: pt tabs stay in the frame
  await openInFreshTab(value);
}

// v0.1.63: always a NEW web tab (the omnibox opens in the active tab; the link menu's 新しいタブで開く opens here).
async function openInFreshTab(value) {
  if (tabLimitReached()) return;
  // v0.1.59: an address that looks right goes straight into the tabs request (the Hub validates it there and
  // answers the same message /api/browser/validate would); only odd input still takes the separate check first.
  const plain = value.length <= 2048 && /^https:\/\/[^\s]+$/i.test(value);
  const url = plain ? value : await validateUrl(value);
  const fresh = createTab();
  pushHistory(fresh, url);
  saveTabs();
  navigate("#/tab/" + fresh.id);
  // v0.1.54: the route change above makes autoReopen() ask for the same page a moment later; registering this
  // open as "in flight" (as openInRemote does) keeps it to ONE NAS tab instead of two (the second used to wait
  // behind the first and then be closed again).
  try {
    await trackOpening(fresh, url, openRemote(fresh, url, u => { fresh.history[fresh.index] = u; }, {validated: !plain}));
  } catch (e) {
    if (plain && e.status === 400 && !fresh.rb) {      // refused by the Hub's URL check: no tab is left behind
      const remembered = state.closed.length;
      closeTab(fresh.id);
      if (state.closed.length > remembered || state.closed.length === MAX_CLOSED_TABS) state.closed.pop();
      throw e;
    }
    throw e;
  }
}

// ------------------------------------------------------------- passthrough display (v0.1.57)
// The page is drawn by this phone inside one iframe on a second origin ("origin B", the Hub's passthrough listener,
// reached through its own `tailscale serve` port); a Service Worker there sends every request through the NAS. The
// app only talks to the frame by postMessage with an exact origin and source check, and never trusts its content.
const ptHost = {tab: null, origin: "", ready: false, seq: 0, loaded: false};
const PT_HIDE = ["tileScroll", "readerScroll", "mirrorWrap", "remoteImg", "remoteIdle", "rbNotReady",
  "chalStatus", "remoteLinks", "remotePopups"];

async function refreshPt() {
  try { state.pt = await api("/api/pt/status"); } catch (e) { state.pt = null; }
}

function ptAvailable() {
  const p = state.pt;
  return Boolean(p && p.enabled && p.listening && p.origin);
}

function ptReason() {
  const p = state.pt;
  if (!p || !p.enabled) return "設定 › ブラウザ › パススルー をオンにしてください";
  if (p.reason === "https" || p.reason === "tsnet") return "パススルーはHTTPSの *.ts.net で開いたアプリからだけ使えます";
  if (!p.listening) return p.error || "パススルーの待ち受けが動いていません";
  return "パススルーを使えません";
}

// The <iframe id="ptFrame"> is in index.html (with its fixed sandbox). A load swaps in a fresh copy of it, and removing
// the old one leaves no session history entries behind; the address is opened with location.replace, which adds none.
function ptFrameSet(url) {
  const fresh = $("ptFrame").cloneNode(false);
  $("ptWrap").replaceChildren(fresh);
  ptHost.loaded = Boolean(url);
  if (!url) return;
  try { fresh.contentWindow.location.replace(url); } catch (e) { ptHost.loaded = false; }
}

function ptDrop() {
  ptHost.seq++;
  ptHost.tab = null; ptHost.ready = false;
  if (ptHost.loaded) ptFrameSet("");
}

function ptCommand(cmd, extra, force) {
  const frame = $("ptFrame");
  if ((!ptHost.ready && !force) || !ptHost.origin || !frame || !frame.contentWindow) return false;
  try { frame.contentWindow.postMessage(Object.assign({qdPtApp: 1, cmd}, extra || {}), ptHost.origin); return true; }
  catch (e) { return false; }
}

// Load `url` in a fresh frame through a one-time ticket (a new frame never adds a history entry to the app).
async function ptLoad(tab, url) {
  if (!ptAvailable() || !url) return;
  const token = ++ptHost.seq;
  ptHost.tab = tab; ptHost.ready = false; ptHost.origin = state.pt.origin;
  tab.ptAwait = true; tab.ptNative = {back: 0, fwd: 0}; tab.error = ""; tab.loading = true;
  let data;
  try {
    data = await api("/api/pt/ticket", {url});
  } catch (e) {
    if (token === ptHost.seq) { ptHost.tab = null; tab.loading = false; tab.error = e.message; updateRemoteView(tab); }
    return;
  }
  if (token !== ptHost.seq || !state.tabs.includes(tab)) return;
  ptHost.origin = data.origin;
  ptFrameSet(data.enter);
}

function updatePtView(tab) {
  for (const id of PT_HIDE) setShown(id, false);
  closeRemoteSheet();
  setShown("interceptSheet", false);
  const ok = ptAvailable() && !tab.error;
  setShown("ptUnavailable", !ok);
  setShown("ptWrap", ok);
  if (!ok) {
    if (ptHost.tab === tab) ptDrop();
    $("ptUnavailableText").textContent = tab.error || ptReason();
  } else if (ptHost.tab !== tab) {
    ptLoad(tab, tab.current || tab.history[tab.index] || "");
  }
  setShown("remoteLoading", Boolean(tab.loading));
}

async function enterPt(tab) {
  if (state.pt === null) await refreshPt();
  if (!ptAvailable()) { toast(ptReason(), "error"); return; }
  if (!tab.history.length) return;
  tab.token++;                          // an NAS open still in flight is dropped
  releaseRemote(tab);
  tab.pt = true; tab.error = ""; tab.notReady = false; tab.ptAwait = true;
  tab.current = tab.history[tab.index] || tab.current;
  saveTabs();
  refreshAfterTabChange();
  if (activeWebTab() === tab) updateRemoteView(tab);
  flash("表示: " + PT_DISPLAY_LABEL);
}

function leavePt(tab, mode) {
  if (ptHost.tab === tab) ptDrop();
  tab.pt = false; tab.ptAwait = false; tab.error = ""; tab.loading = false; tab.reopenAt = 0;
  tab.current = tab.history[tab.index] || tab.current;
  const host = hostOf(tab.current || "");
  if (mode && host) { rememberHostMode(host, MODE_OF_DISPLAY[mode]); renderRemoteHosts(); }
  saveTabs();
  refreshAfterTabChange();
  if (activeWebTab() === tab) updateRemoteView(tab);   // the NAS tab reopens by itself (autoReopen)
  flash("表示: " + (mode ? RB_DISPLAY_LABELS[mode] : "NASブラウザ"));
}

async function ptNavigateTo(tab, value) {
  const url = await validateUrl(value);
  pushHistory(tab, url);
  tab.current = url; tab.ptTitle = "";
  saveTabs();
  await ptGo(tab, url);
}

async function ptGo(tab, url) {
  tab.ptAwait = true; tab.ptNative = {back: 0, fwd: 0}; tab.loading = true; tab.error = "";
  updateChrome();
  if (ptHost.tab === tab && ptHost.ready && ptCommand("navigate", {url})) { refreshAfterTabChange(); return; }
  await ptLoad(tab, url);
  refreshAfterTabChange();
}

async function ptReload(tab) {
  if (ptHost.tab === tab && ptCommand("reload")) { tab.loading = true; updateChrome(); return; }
  await ptLoad(tab, tab.current || tab.history[tab.index]);
}

// One page back / forward. A step the frame's own history can do (pages reached by clicking in it) is sent to the page;
// anything else is opened again from the app's list.
async function ptStep(tab, delta) {
  const target = tab.index + delta;
  if (target < 0) { flash("これより前のページはありません"); return null; }
  if (target >= tab.history.length) { flash("これより先のページはありません"); return null; }
  const n = tab.ptNative || {back: 0, fwd: 0};
  if ((delta < 0 ? n.back : n.fwd) > 0 && ptHost.tab === tab && ptCommand(delta < 0 ? "back" : "forward")) return null;
  tab.index = target;
  tab.current = tab.history[target];
  tab.ptTitle = "";
  saveTabs();
  await ptGo(tab, tab.current);
  return null;
}

// A report from the page: where it is now. The app keeps the list (the frame's own history only does the stepping).
function ptNav(tab, m) {
  if (m.kind === "title") {
    tab.ptTitle = String(m.title || "").slice(0, 200);
    if (tab.current) noteVisit(tab.current, tab.ptTitle);      // v0.1.62
    if (activeWebTab() === tab) updateWebStatus(tab);
    return;
  }
  const url = typeof m.url === "string" ? m.url : "";
  if (url.length > 2048 || !/^https?:\/\//i.test(url)) return;
  const n = tab.ptNative || (tab.ptNative = {back: 0, fwd: 0});
  tab.loading = false;
  tab.error = "";
  tab.ptTitle = String(m.title || "").slice(0, 200);
  const here = tab.history[tab.index];
  if (tab.ptAwait) {                      // the page we asked for (its address may differ after redirects)
    tab.ptAwait = false;
    tab.history[tab.index] = url;
  } else if (m.kind === "replace") {
    tab.history[tab.index] = url;
  } else if (url !== here) {
    if (tab.index > 0 && tab.history[tab.index - 1] === url) { tab.index--; n.back = Math.max(0, n.back - 1); n.fwd++; }
    else if (tab.history[tab.index + 1] === url) { tab.index++; n.fwd = Math.max(0, n.fwd - 1); n.back++; }
    else { pushHistory(tab, url); n.back++; n.fwd = 0; }
  }
  tab.current = url;
  saveTabs();
  noteVisit(url, tab.ptTitle);      // v0.1.62: recent pages
  if (activeWebTab() === tab) { updateChrome(); updateWebStatus(tab); setShown("remoteLoading", false); }
}

// The download page of a proxied site asks to hand the link to JD2; the confirmation is the app's own.
async function ptDownload(tab, m) {
  const id = typeof m.id === "string" && /^[A-Za-z0-9_-]{8,64}$/.test(m.id) ? m.id : "";
  if (!id) return;
  const name = String(m.name || "ファイル").slice(0, 120);
  const send = jd2CanSend();
  let text;
  if (!confirm("「" + name + "」を" + (send ? "JD2へ送ります" : "追加したリンクに入れます（JD2へは未送信）") +
      "。\nリンクのアドレスだけをNASが渡します（Cookieは渡せません）。よろしいですか？")) {
    text = "キャンセルしました";
  } else {
    try {
      const r = await api("/api/pt/jd2", {id, send});
      swrDrop("links");
      text = r.state === "sent" ? "JD2へ送りました" : r.state === "dup" ? "すでに送信済みです" : "追加したリンクに入れました";
    } catch (e) { text = e.message; }
  }
  ptCommand("download-result", {id, text}, true);    // the download page has no helper, so no "ready"
}

// v0.1.57: set when the app page is being left (reload, navigation), so aborted fetches are not reported as failures.
var pageLeaving = false;
window.addEventListener("beforeunload", () => { pageLeaving = true; });
window.addEventListener("pagehide", () => { pageLeaving = true; });
window.addEventListener("pageshow", ev => { if (ev.persisted) pageLeaving = false; });

window.addEventListener("message", ev => {
  const frame = $("ptFrame");
  if (!ptHost.origin || ev.origin !== ptHost.origin || !frame || ev.source !== frame.contentWindow) return;
  const m = ev.data;
  const tab = ptHost.tab;
  if (!m || m.qdPt !== 1 || typeof m.type !== "string" || !tab || !tab.pt || !state.tabs.includes(tab)) return;
  if (m.type === "hello") { ptHost.ready = true; tab.loading = false; if (activeWebTab() === tab) setShown("remoteLoading", false); }
  else if (m.type === "loading") { ptHost.ready = false; tab.loading = true; if (activeWebTab() === tab) setShown("remoteLoading", true); }
  else if (m.type === "nav") ptNav(tab, m);
  else if (m.type === "scroll") {      // v0.1.60: the page's own scrolling collapses / restores the bars
    if (activeWebTab() === tab && Number.isFinite(m.top) && Number.isFinite(m.max)) chromeScrolled(m.top, m.max, "pt");
  }
  else if (m.type === "flick") {
    if (activeWebTab() === tab && (m.dir === -1 || m.dir === 1) && claimStep("flick")) perform(() => stepHistory(tab, m.dir));
  } else if (m.type === "step") {           // v0.1.63: Alt+arrows / Cmd+[ ] / mouse buttons 4 5 / trackpad swipe inside the frame
    if (activeWebTab() === tab && (m.dir === -1 || m.dir === 1)) inputStep(m.dir, "input");
  } else if (m.type === "shortcut") {       // v0.1.63: Ctrl/Cmd+L, Ctrl/Cmd+R, F5 inside the frame
    if (activeWebTab() === tab && (m.name === "omnibox" || m.name === "reload")) runShortcut(m.name, false);
  } else if (m.type === "download") perform(() => ptDownload(tab, m));
});

// ---- settings
function renderPtSettings() {
  const s = state.settings || {};
  const port = Number.isInteger(s.pt_public_port) ? s.pt_public_port : 8443;
  $("ptCmd").textContent = "tailscale serve --bg --https=" + port + " http://127.0.0.1:17865";
  const p = state.pt;
  let line = "";
  if (!s.pt_enabled) line = "オフです。";
  else if (p && p.listening) line = "待ち受け中（127.0.0.1:17865）。" + (p.origin ? "" : ptReason());
  else line = (p && p.error) || "待ち受けを開始しています…";
  $("ptState").textContent = line;
}

function wirePt() {
  $("ptEnabled").addEventListener("change", () => perform(async () => {
    const on = $("ptEnabled").checked;
    try {
      await saveOption("pt_enabled", on);
    } catch (e) { $("ptEnabled").checked = !on; throw e; }
    await refreshPt();
    renderPtSettings();
    if (on) {
      flash("パススルーをオンにしました。画面を読み込み直します");
      setTimeout(reloadFresh, 700);     // the app's CSP only lists origin B once enabled (v0.1.60: the cached page has the old CSP)
    } else {
      for (const tab of state.tabs) if (tab.pt) tab.error = "";
      ptDrop();
      const active = activeWebTab();
      if (active) updateRemoteView(active);
      flash("パススルーをオフにしました");
    }
  }));
  $("ptCmdCopy").addEventListener("click", () => copyText($("ptCmd").textContent));
  $("ptCheck").addEventListener("click", () => perform(async () => {
    await refreshPt();
    renderPtSettings();
    const out = $("ptCheckResult");
    if (!ptAvailable()) { out.textContent = "確認できません: " + ptReason(); return; }
    out.textContent = "確認しています…";
    try {
      const res = await fetch(state.pt.origin + "/pt/ping", {mode: "cors", credentials: "omit", cache: "no-store"});
      const data = await res.json();
      out.textContent = data && data.ok === true ? "接続できました（" + state.pt.origin + "）" : "応答が正しくありません";
    } catch (e) {
      out.textContent = "接続できません。NASで上のコマンドを実行したか、アプリを読み込み直したか確認してください。";
    }
  }));
  $("ptClear").addEventListener("click", () => perform(async () => {
    await api("/api/pt/clear-cookies", {});
    flash("パススルーのCookieを消しました");
  }));
  $("ptUnavailableBtn").addEventListener("click", () => {
    const tab = activeWebTab();
    if (tab && tab.pt) leavePt(tab, null);
  });
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
      const section = path[1] || defaultSection();
      if (SETTINGS_SECTIONS.includes(section) || SETTINGS_REDIRECTS[section]) {
        return {kind: "route", value: "#/settings/" + section};
      }
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

function restoreOmnibox() { $("omnibox").value = document.activeElement === $("omnibox") ? state.omniDisplay : state.omniShort; }

function focusOmnibox() {
  const box = $("omnibox");
  box.focus();
  box.select();
}

// ---------------------------------------------------------------- menu
const MENU_ITEMS = "[role='menuitem'],[role='menuitemradio'],[role='menuitemcheckbox']";

function closeMenu(returnFocus = true) {
  if ($("menu").hidden) return;
  $("menu").hidden = true;
  $("menuButton").setAttribute("aria-expanded", "false");
  if (returnFocus) $("menuButton").focus();
}

function configureMenu() {
  const tab = activeWebTab();
  const web = Boolean(tab && tab.history.length);
  const menu = $("menu");
  menu.querySelectorAll("[data-scope='web']").forEach(n => { n.hidden = !web; });
  const usable = Boolean(tab && tab.current);
  for (const action of ["stageUrl", "bookmark"]) {
    menu.querySelector("[data-action='" + action + "']").disabled = !usable;
  }
  menu.querySelector("[data-action='reopen']").disabled = state.closed.length === 0;
  // v0.1.60: the second row's page title, and the actions that no longer have a button in the narrow bar
  const page = $("menuPage");
  const title = tab && (tab.pt ? tab.ptTitle : tab.rb && tab.rb.title) || "";
  page.hidden = !(web && (title || tab.current));
  page.textContent = web ? (title ? title + " · " : "") + hostOf(tab.current || tab.history[tab.index] || "") : "";
  $("menuForward").disabled = !(tab && (tab.rb || tab.pt));
  $("menuReload").disabled = !(tab && tab.history.length);
  const marked = Boolean(tab && tab.current && state.bookmarks.some(b => b.url === tab.current));
  const mark = menu.querySelector("[data-action='bookmark']");
  mark.textContent = marked ? "★ ブックマーク済み" : "ブックマークに追加";
  mark.classList.toggle("menu-item-on", marked);
  menu.querySelectorAll("[data-scope='remote']").forEach(n => { n.hidden = !web; });
  const display = tab && tab.pt ? "pt" : tab && tab.rb ? tabDisplay(tab) : "tiles";
  const ptOk = state.pt === null || ptAvailable() || Boolean(tab && tab.pt);       // unknown yet: not greyed out       // v0.1.57: the choice is greyed out with the reason as its tooltip
  $("menuPt").disabled = !ptOk;
  $("menuPt").title = ptOk ? "" : ptReason();
  // Picture quality only matters for the fast and live displays (v0.1.36).
  menu.querySelectorAll("[data-scope='quality']").forEach(n => { n.hidden = !web || display === "reader" || display === "mirror" || display === "pt"; });
  menu.querySelectorAll("[data-quality]").forEach(n => {
    n.setAttribute("aria-checked", String(n.dataset.quality === state.rbQuality));
  });
  menu.querySelectorAll("[data-display]").forEach(n => {
    n.setAttribute("aria-checked", String(n.dataset.display === display));
  });
  $("menuPerf").setAttribute("aria-checked", String(Boolean(state.rbPerf)));
  // v0.1.62: only offered while this site has a remembered display
  const forget = $("menuForgetSite");
  const site = web ? siteKey(hostOf(tab.current || tab.history[tab.index] || "")) : "";
  forget.hidden = !(web && hasSiteDisplay(tab.current || tab.history[tab.index] || ""));
  forget.textContent = "このサイトの表示を忘れる" + (site ? "（" + site + "）" : "");
  renderProbeChip();         // v0.1.65: 「リンク解析の一覧」 is offered while the tab has analysis jobs
}

function openMenu() {
  chromeShow(true);
  if (state.pt === null) refreshPt().then(() => { if (!$("menu").hidden) configureMenu(); });   // v0.1.57: first use only
  configureMenu();
  $("menu").hidden = false;
  $("menuButton").setAttribute("aria-expanded", "true");
  const first = Array.from($("menu").querySelectorAll(MENU_ITEMS))
    .find(n => !n.hidden && !n.disabled);
  if (first) first.focus();
}

async function menuAction(action) {
  const tab = activeWebTab();
  if (action === "newTab") createBlankTab();
  else if (action === "reopen") reopenClosedTab();
  else if (action.startsWith("quality:")) setRbQuality(action.slice(8));
  else if (action.startsWith("display:")) setUserDisplay(tab, action.slice(8));
  else if (action === "perf") setPerf(!state.rbPerf);
  else if (action === "forgetSite" && tab) forgetSiteDisplay(tab);
  else if (action === "probeList" && tab) await openProbeSheet(tab);          // v0.1.65
  else if (action === "stageUrl" && tab) await stageTabUrl(tab);
  else if (action === "bookmark" && tab) await bookmarkTab(tab);
  else if (action === "closeTab" && tab) closeTab(tab.id);
  else if (action === "forward" && tab) await stepHistory(tab, 1);       // v0.1.60: no ⟶ / ⟳ button in the narrow bar
  else if (action === "reload" && tab) await reloadTab(tab);
  else if (action === "bookmarks") navigate("#/settings/browser/bookmarks");
  else if (action === "settings") navigate(settingsHash());
  else if (action === "logout") await logout();
}

// v0.1.62: 「このサイトの表示を忘れる」: the site goes back to the default display (settings).
function forgetSiteDisplay(tab) {
  const host = hostOf(tab.current || tab.history[tab.index] || "");
  if (!host || !hasSiteDisplay(host)) return;
  rememberHostMode(host, "");
  renderRemoteHosts();
  if (tab.rb && !tab.pt) { tab.rb.tilesFor = ""; tab.rb.readerFail = null; tab.rb.mirrorFail = null; applyDisplay(tab); }
  updateChrome();
  flash("このサイトの表示を忘れました（" + siteKey(host) + "）");
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
  removeLocal(RECENT_KEY);      // v0.1.63: 最近見たページ is this device's history: a shared device must not keep it
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
    close.title = "タブを閉じる";
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

// Where a package that is still in a subfolder saves (name relative to the download folder only).
function folderNote(pkg) {
  const folder = pkg && pkg.folder;
  if (!folder || folder.flat !== false || typeof folder.name !== "string") return null;
  return element("p", "フォルダ: " + folder.name, "download-detail folder-note");
}

function singleRow(pkg, file) {
  const card = element("article", undefined, "download-card pkg single " + itemState(file));
  const top = element("div", undefined, "pkg-top");
  const line = element("div", undefined, "pkg-line");
  const head = element("div", undefined, "pkg-head");
  head.appendChild(element("span", file.name || pkg.name, "row-name"));
  head.appendChild(stateChip(file));
  line.appendChild(head);
  rowTools("package", pkg, line);           // the actions act on the package (= this one file)
  top.appendChild(line);
  const bar = progressBar(file) || progressBar(pkg);
  if (bar) top.appendChild(bar);
  const meta = metaText(file, file.host);
  if (meta) top.appendChild(element("p", meta, "download-detail"));
  const note = folderNote(pkg);
  if (note) top.appendChild(note);
  card.appendChild(itemSwipe("package", pkg, top));
  return card;
}

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
  const note = folderNote(pkg);
  if (note) top.appendChild(note);
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
  let startButton = null, pauseButton = null;
  if (cfg.startLabel) {
    startButton = element("button", cfg.startLabel, "swipe-btn " + (cfg.startLabel === "停止" ? "stop" : "start"));
    startButton.type = "button";
    left.appendChild(startButton);
    // v0.1.62: a swipe to the LEFT reveals both 一時停止／再開 and 一覧から消す (a swipe to the right keeps its 開始／停止)
    pauseButton = element("button", cfg.startLabel === "停止" ? "一時停止" : "再開",
      "swipe-btn " + (cfg.startLabel === "停止" ? "stop" : "start"));
    pauseButton.type = "button";
    right.appendChild(pauseButton);
  }
  const deleteButton = element("button", cfg.startLabel ? "消す" : "削除", "swipe-btn delete");
  deleteButton.type = "button";
  deleteButton.setAttribute("aria-label", "一覧から消す（ファイルは残す）");
  right.appendChild(deleteButton);
  const leftReveal = SWIPE_REVEAL * (pauseButton ? 2 : 1);
  body.classList.add("swipe-body");
  wrap.append(left, right, body);
  const buttons = [startButton, pauseButton, deleteButton].filter(Boolean);
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
      reveal(offset < 0 ? -leftReveal : SWIPE_REVEAL);
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
  if (pauseButton) pauseButton.addEventListener("click", () => { handle.close(); cfg.onStart(); });
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
  button.title = "操作メニュー";
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
  await afterItemAction(action === "start" ? "開始しました" : "LinkGrabberから消しました");
}

// v0.1.70: remove the waiting links that never start by themselves (offline / already in the list). List only.
async function cleanWaiting() {
  const w = state.waiting;
  const stuck = w && Number.isInteger(w.stuck) ? w.stuck : 0;
  if (!stuck || !confirm("オフラインのリンクと、ダウンロード一覧に同じファイルがあるリンク（" + stuck +
      "件）をLinkGrabberから消します。保存されたファイルは消えません。よろしいですか？")) return;
  const result = await api("/api/jd2/waiting/clean", {confirmed: true});
  flash(result.count + "件をLinkGrabberから消しました");
  await afterItemAction("");
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
    {label: "LinkGrabberから消す", primary: true, run: () => waitingCall("remove", [item])},
  ], opener);
}

function waitingRow(item) {
  const body = element("div", undefined, "waiting-item");
  body.appendChild(element("span", item.name, "waiting-name"));
  if (item.host) body.appendChild(element("span", item.host, "muted"));
  // v0.1.70: why this link waits (server text; offline / duplicate never start by themselves)
  if (typeof item.reason === "string" && item.reason) {
    body.appendChild(element("span", item.reason, "waiting-reason" +
      (item.state === "offline" || item.state === "duplicate" ? " is-stuck" : "")));
  }
  if (!item.id) return body;
  body.appendChild(menuButton(item.name, opener => waitingMenu(item, opener)));
  const wrap = swipeRow(body, {
    key: "waiting:" + item.id, startLabel: "開始",
    onStart: () => perform(() => waitingCall("start", [item])),
    onDelete: () => perform(() => waitingCall("remove", [item])),
    onDeleteSheet: () => openSheet(item.name, [
      {label: "LinkGrabberから消す", primary: true, run: () => waitingCall("remove", [item])}]),
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

// v0.1.62: JD2 has no "failed" flag in the snapshot: an item that is neither running nor finished and whose status
// text names a problem is counted as failed (and left out of 待機).
// v0.1.63: "Offline" / "オフライン" ALONE is the hoster's state of a link that is waiting (JD2 shows it before it has
// checked or while it waits for a slot): that is 待機, not 失敗. It only counts as a failure together with a real
// failure word ("File not found (offline)", "エラー: オフライン").
const FAILED_STATUS = /error|fail|invalid|incorrect|expired|not\s*found|エラー|失敗|見つかりません|期限|無効なリンク/i;
function isFailed(item) {
  return item.finished !== true && item.running !== true && typeof item.status === "string" && FAILED_STATUS.test(item.status);
}

// Totals for the summary at the top of the downloads tab; only what the fetched page holds (packages, else files).
function summarizeDownloads(snap) {
  const out = {running: 0, waiting: 0, finished: 0, failed: 0, speed: 0, eta: null, hasMore: false, total: 0};
  if (!snap) return out;
  const rows = snap.packages && snap.packages.length ? snap.packages : snap.links || [];
  let remaining = 0, remainingKnown = true, maxEta = null;
  for (const item of rows) {
    out.total++;
    if (item.finished === true) { out.finished++; continue; }
    if (item.running === true) {
      out.running++;
      if (typeof item.speed_bytes === "number") out.speed += item.speed_bytes;
      if (typeof item.eta_seconds === "number") maxEta = maxEta === null ? item.eta_seconds : Math.max(maxEta, item.eta_seconds);
    } else if (isFailed(item)) out.failed++;
    else out.waiting++;
    if (item.running === true || !isFailed(item)) {
      if (typeof item.bytes_total === "number" && item.bytes_total > 0) {
        remaining += Math.max(0, item.bytes_total - (typeof item.bytes_loaded === "number" ? item.bytes_loaded : 0));
      } else remainingKnown = false;
    }
  }
  // Remaining time of everything that still has to be downloaded at the current total speed; else the longest
  // per-item estimate; unknown when neither exists (nothing running).
  if (out.running > 0 && out.speed > 0 && remainingKnown) out.eta = Math.ceil(remaining / out.speed);
  else if (out.running > 0 && maxEta !== null) out.eta = maxEta;
  out.hasMore = Boolean(snap.has_more);
  return out;
}

function renderDownloadsSummary(snap, hasItems) {
  const box = $("downloadsSummary");
  box.hidden = !hasItems;
  if (!hasItems) return;
  const sum = summarizeDownloads(snap);
  $("dlSpeed").textContent = sum.running ? "合計 " + readableSize(sum.speed) + "/秒" : "停止中";
  $("dlEta").textContent = sum.running && sum.eta !== null ? "残り " + readableEta(sum.eta) : "";
  $("dlCountRunning").textContent = String(sum.running);
  $("dlCountWaiting").textContent = String(sum.waiting);
  $("dlCountFinished").textContent = String(sum.finished);
  $("dlCountFailed").textContent = String(sum.failed);
  box.classList.toggle("has-failed", sum.failed > 0);
  box.title = sum.hasMore ? "このページ（" + sum.total + "件）の集計です" : "";
}

function matchesFilter(item) {
  if (state.filter === "failed") { if (!isFailed(item)) return false; }
  else if (state.filter === "waiting") { if (itemState(item) !== "waiting" || isFailed(item)) return false; }    // v0.1.63: as the counts
  else if (state.filter !== "all" && itemState(item) !== state.filter) return false;
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
    renderDownloadsSummary(null, false);
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
    if (matchesFilter(pkg) || files.some(matchesFilter)) {
      // v0.1.46: a package of one file is one row (no nested card repeating the same name/size).
      nodes.push(files.length === 1 && (pkg.child_count === 1 || pkg.child_count === undefined) ?
        singleRow(pkg, files[0]) : packageRow(pkg, files));
    }
  }
  const looseShown = loose.filter(matchesFilter);
  // Without any package on the page, loose files are the list itself.
  if (!snap.packages.length) for (const file of looseShown) nodes.push(fileRow(file));
  if (!nodes.length) {
    const filtering = state.filter !== "all" || state.query.trim() !== "";
    nodes.push(filtering && hasItems ? emptyState("条件に一致するものはありません", "絞り込みを変えてください") :
      emptyState("ダウンロードはありません", "「リンク」から追加できます"));
  }
  list.replaceChildren(...nodes);
  const showLoose = snap.packages.length > 0 && looseShown.length > 0;
  setShown("orphanFiles", showLoose);
  orphans.replaceChildren(...(showLoose ? looseShown.map(fileRow) : []));
  $("orphanSummary").textContent = "他のファイル（" + looseShown.length + "件）";
  renderDownloadsSummary(snap, hasItems);       // v0.1.62
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
  // v0.1.70: say why links wait and whether they start by themselves
  $("waitingNote").textContent = "リンクの確認が終わると、「送信時に自動開始」がオンなら自動で開始します。" +
    (sendAutostartOn() ? "確認済みのものは約30秒以内に開始します。" : "いまはオフなので、「開始」で始めます。") +
    ((state.settings && state.settings.auto_clear_missing === false)
      ? "オフラインや同じファイルがあるものは開始されません。"
      : "オフライン（File not found）は自動で消します。同じファイルがあるものは開始されず、理由を表示します。");
  const stuck = Number.isInteger(w.stuck) ? w.stuck : 0;
  setShown("waitingClean", stuck > 0);
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
  if (!confirm("完了したダウンロードと、ファイルが既にあるためスキップされたものを一覧から削除します。保存されたファイルは削除されません。")) return;
  const r = await api("/api/jd2/downloads/cleanup", {confirmed: true});
  const skipped = r && Number.isInteger(r.skipped_removed) ? r.skipped_removed : 0;
  flash(skipped ? "完了したものと既存スキップ " + skipped + " 件を一覧から消しました" : "完了したものを一覧から消しました");
  state.downloadsFails = 0;
  await loadDownloads(state.downloadsPage);
}

// Links header ⋯ menu: only offers what exists.
function linksMenu() {
  const items = state.queueData ? state.queueData.items : [];
  const history = items.filter(l => l.state === "sent" || l.state === "uncertain").length;
  const staged = items.filter(l => l.state === "staged").length;
  const options = [];
  if (history) options.push({label: "送信済み・要確認の履歴を削除（" + history + "件）", run: () => clearHistory(false)});
  if (history + staged) {
    options.push({label: "未送信のリンクも含めてすべて削除（" + (history + staged) + "件）", destructive: true,
      run: () => clearHistory(true)});
  }
  if (options.length) openSheet("リンクの履歴", options, $("linksMore"));
}

async function clearHistory(includeStaged) {
  const items = state.queueData ? state.queueData.items : [];
  const count = items.filter(l => l.state === "sent" || l.state === "uncertain" ||
    (includeStaged && l.state === "staged")).length;
  if (!count) return;
  if (!confirm("送信履歴を" + count + "件削除します（ダウンロード済みファイルやJD2のダウンロードは削除しません）")) return;
  const r = await api("/api/links/clear", {include_staged: includeStaged});
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
    const request = api("/api/jd2/downloads?page=" + page);
    trackLoad(request);        // v0.1.62: the progress line when the list takes longer than 300 ms
    const snap = await request;
    state.snapshot = snap;
    state.snapshotJson = JSON.stringify(snap);
    state.snapshotAt = Date.now();
    state.downloadsPage = snap.page;
    state.downloadsFails = 0;
    if (snap.page === 0) state.dlRunning = countRunning(snap);
    // v0.1.70: the Hub removed entries whose file is gone from the hoster since the last look
    if (Number.isInteger(snap.missing_cleared) && snap.missing_cleared > 0) flash("ファイルがない" + snap.missing_cleared + "件を一覧から消しました");
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
    swrDrop("engine");   // the engine may have stopped meanwhile: the next state tick says so
  } finally {
    state.downloadsBusy = false;
    $("downloadsRefresh").disabled = false;
    sched.kick();
  }
}

function downloadsTaskWanted() {
  return state.route.view === "downloads" && engineRunning() && state.downloadsFails < 3 &&
    !state.downloadsBusy;
}

function setFilter(filter) {
  state.filter = filter;
  document.querySelectorAll(".chip").forEach(c =>
    c.setAttribute("aria-pressed", String(c.dataset.filter === filter)));
  renderDownloads();
}

// ----------------------------------------------------------------- links
function jd2CanSend() { return Boolean(state.live && state.live.engine && state.live.engine.can_send === true); }

const LINK_STATE_LABELS = {staged: "未送信", sending: "送信中", uncertain: "要確認", sent: "送信済み"};
// v0.1.62: what this page is doing with a link right now: id -> {s: "sending" | "failed", msg}. Shown over the Hub's
// state so 送信中 / 失敗 are visible while a hand-off runs and after it failed (the Hub keeps a failed link as 未送信).
const linkUi = new Map();
function setLinkUi(id, st, msg) {
  if (st) linkUi.set(id, {s: st, msg: msg || ""}); else linkUi.delete(id);
}
function repaintQueue() { if (state.queueData) renderQueue(state.queueData); }

function renderQueue(q) {
  state.queueData = q;
  const nodes = [];
  const canSend = jd2CanSend();
  const staged = q.items.filter(l => l.state === "staged");
  setShown("sendAll", canSend && staged.length >= 2);
  setShown("linksMore", q.items.length > 0);   // history deletion only exists when there is something
  if (!q.items.length) nodes.push(emptyState("追加したリンクはありません", "上の欄に貼り付けると追加できます"));
  for (const link of q.items) {
    const container = element("div", undefined, "item link-item");
    container.appendChild(element("span", link.url, "link-url"));
    const row = element("div", undefined, "link-actions");
    const ui = linkUi.get(link.id);
    const live = link.state === "staged" && ui ? ui.s : "";            // v0.1.62: this page's view of a staged link
    const chip = live === "sending" ? element("span", "送信中", "state-chip link-sending is-live") :
      live === "failed" ? element("span", "失敗", "state-chip link-failed") :
      element("span", LINK_STATE_LABELS[link.state] || link.state, "state-chip link-" + link.state);
    if (live === "failed") chip.title = ui.msg || "送信できませんでした";
    if (link.state === "uncertain" || link.state === "sending") chip.title = "送信結果が不明です。JD2で確認してください";
    row.appendChild(chip);
    container.dataset.state = live || link.state;
    if (live === "failed" && ui.msg) row.appendChild(element("span", ui.msg, "link-note"));
    if (link.state === "staged" && live !== "sending") {
      if (canSend) {
        const send = element("button", live === "failed" ? "再送信" : "JD2へ送る", "btn small");
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
  let sent = 0, current = null;
  for (const link of links) setLinkUi(link.id, "sending");     // v0.1.62: every link in this hand-off shows 送信中 at once
  repaintQueue();
  try {
    for (const link of links) {
      current = link;
      await api("/api/links/send", {id: link.id});
      setLinkUi(link.id, "");
      sent++;
    }
  } catch (e) {
    for (const link of links) setLinkUi(link.id, "");
    if (current) setLinkUi(current.id, "failed", e.message);    // the link that failed; the rest were not tried
    swrDrop("links");
    await loadQueue(true).catch(() => {});
    repaintQueue();
    throw new Error((many ? sent + "/" + links.length + "件送信後に停止: " : "") + e.message);
  }
  swrDrop("links");
  await loadQueue(true);
  // v0.1.56: the Hub forgets sent rows a few seconds later (auto_clear_links): refresh the list then
  if (state.settings && state.settings.auto_clear_links === true) {
    setTimeout(() => { swrDrop("links"); loadQueue(true).catch(() => {}); }, 7500);
  }
  const dupText = opts.skipped ? "・" + opts.skipped + "件は重複のためスキップ" : "";
  flashCount("sent", sent, n => n + "件を" + (sendAutostartOn() ? "JD2へ送り、ダウンロードを開始しました" :
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
  $("pasteSend").disabled = true;
  if (!$("pasteBox").value.trim()) { setShown("pasteResult", false); return; }
  state.pasteTimer = setTimeout(() => perform(runPastePreview), delay);
}

function isSingleHttpsUrl(text) { return /^https:\/\/[^\s<>"']+$/.test(text); }

function showPaste(summary, urls, buttonLabel) {
  $("pasteSummary").textContent = summary;
  $("pasteList").replaceChildren(...urls.map(u => element("div", u, "item")));
  $("pasteAdd").textContent = buttonLabel || "追加";
  $("pasteAdd").hidden = !buttonLabel;
  // v0.1.62: the send button is there at once (when JD2 can take links), next to the count
  const send = $("pasteSend");
  send.hidden = !(buttonLabel && jd2CanSend());
  send.textContent = urls.length > 1 ? urls.length + "件を追加してJD2へ送る" : "追加してJD2へ送る";
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
    $("pasteSend").disabled = false;
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
  state.paste = {kind: "extract", content, digest: preview.digest, limit: 20, count: preview.count, urls: preview.items};
  showPaste(preview.count + "件見つかりました", preview.items, preview.count + "件を追加");
  $("pasteAdd").disabled = false;
  $("pasteSend").disabled = false;
}

async function addPasted(thenSend = false) {
  const pasted = state.paste;
  if (!pasted) return;
  if (pasted.kind === "extract" && $("pasteBox").value !== pasted.content) {
    flash("貼り付け内容が変わりました。もう一度確認します");
    schedulePaste(0);
    return;
  }
  $("pasteAdd").disabled = true;
  $("pasteSend").disabled = true;
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
  if (thenSend && jd2CanSend()) {       // v0.1.62: 追加してJD2へ送る: only the links of this paste that are still 未送信
    const wanted = new Set(pasted.kind === "url" ? [pasted.url] : pasted.urls || []);
    const items = state.queueData ? state.queueData.items.filter(l => l.state === "staged" && wanted.has(l.url)) : [];
    if (items.length) await sendLinks(items);
  }
}

// v0.1.62: 貼り付け button. The clipboard is read only here (an explicit tap); without the API, or when it is
// refused, the box gets the focus so the long-press paste of the phone works.
async function pasteFromClipboard() {
  let text = "";
  if (navigator.clipboard && typeof navigator.clipboard.readText === "function") {
    try { text = await navigator.clipboard.readText(); } catch (e) { text = ""; }
  }
  const box = $("pasteBox");
  if (!text.trim()) {
    box.focus();
    flash("コピーした内容を読めませんでした。欄を長押しして「ペースト」してください");
    return;
  }
  box.value = text.slice(0, PASTE_LIMIT);
  schedulePaste(0);        // the count and the buttons appear at once
}

// -------------------------------------------------------------- settings
function showSettingsSection(section) {
  document.querySelectorAll(".settings-section").forEach(n => { n.hidden = n.dataset.section !== section; });
  document.querySelectorAll(".settings-nav a").forEach(a => {
    if (a.dataset.section === section) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
}

// #/settings/<section>/<sub>: scroll the subsection into view (the top of the page without one).
function scrollToSub(route) {
  const body = document.querySelector(".settings-section[data-section='" + route.section + "']");
  const target = route.sub && body ? body.querySelector("[data-sub='" + route.sub + "']") : null;
  try {
    if (target) target.scrollIntoView({block: "start"});
    else $("content").scrollTo(0, 0);
  } catch (e) { /* older browser: stay where we are */ }
}

function renderDownloadDir(s) {
  state.settings = s;
  $("sendAutostart").checked = s.send_autostart !== false;
  $("probeAutosend").checked = s.probe_autosend !== false;
  $("autoClearLinks").checked = s.auto_clear_links === true;           // v0.1.56
  $("autoClearDownloads").checked = s.auto_clear_downloads === true;   // v0.1.56
  $("autoClearMissing").checked = s.auto_clear_missing !== false;      // v0.1.70: default ON
  $("blockAds").checked = s.block_ads !== false;
  $("rbDefaultMode").value = DISPLAY_OF_SETTING[s.rb_default_mode] ? s.rb_default_mode : "fast";
  $("rbLinkBoxes").checked = s.rb_link_boxes === true;
  $("rbSpareTab").checked = s.rb_spare_tab !== false;
  $("homeRecentOn").checked = s.home_recent !== false;      // v0.1.65
  renderMaxTabs();
  $("rbNoWebfonts").checked = s.rb_no_webfonts !== false;
  $("rbResident").checked = s.rb_resident === true;
  $("ptEnabled").checked = s.pt_enabled === true;       // v0.1.57
  renderPtSettings();
  renderResidentQuick(s.rb_resident === true);
  $("selectedFolder").textContent = s.download_dir || "未選択";
  $("folderChange").textContent = s.download_dir ? "変更" : "選ぶ";
  renderHome();
  syncRemoteSettings();
}

// The folder picker is a bottom sheet; choosing a folder closes it.
function setPicker(open) {
  state.pickerOpen = open;
  setShown("folderSheet", open);
  if (open) $("folderCancel").focus();
}

// Lazy: the settings handlers are attached the first time the view is opened.
let settingsWired = false;
function ensureSettings() {
  if (settingsWired) return;
  settingsWired = true;
  wireSettings();
  wireBrowser();
}

async function enterSettings(route, force = false) {
  ensureSettings();
  const section = route.section;
  showSettingsSection(section);
  if (section !== "connection") hideShareToken();
  if (section !== "jd2") clearAccountForms();
  if (section === "jd2") {
    await Promise.all([loadEngine(force), loadSettings(force)]);
    if (engineRunning()) {
      // JD2 status (connected / update) and the account list: two requests, only while it runs.
      await Promise.all([loadJd2Status(force), loadAccounts(force)]);
    }
    if (route.sub === "storage" && state.settings && !state.settings.download_dir) {
      setPicker(true);
      await folders(state.folderPath, force).catch(() => folders("", force));
    }
  } else if (section === "browser") {
    renderRemoteHosts();
    await Promise.all([loadBrowserStatus(force), loadSettings(force), loadBookmarks(force),
      swr("site-rules", "/api/rb/site-rules", renderSiteRules, {force}), refreshPt()]);
    renderPtSettings();       // v0.1.57
  } else if (section === "connection") {
    renderHttps();
    renderShareStatic();
    await swr("share-tokens", "/api/share/tokens", renderShareDevices, {force});
  } else if (section === "system") {
    await updateStatus(force);
    await swr("passkeys", "/api/passkeys", renderPasskeys, {force});
  } else if (section === "diag") {
    renderDebugCard();
    loadDisk().catch(() => {});
    selftestAttach().catch(() => {});
  }
  scrollToSub(route);
}

// ---- Passkeys: Face ID / Touch ID login (v0.1.53)
const FACE_OFFER_KEY = "qdl.faceOffer";
const PASSKEY_RENEW_MS = 100000;      // server challenges live 120 s: renew the conditional request before that

function b64uToBuf(text) {
  const padded = String(text).replace(/-/g, "+").replace(/_/g, "/") + "===".slice((String(text).length + 3) % 4);
  const raw = atob(padded);
  const out = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
  return out.buffer;
}

function bufToB64u(buffer) {
  const bytes = new Uint8Array(buffer);
  let raw = "";
  for (let i = 0; i < bytes.length; i++) raw += String.fromCharCode(bytes[i]);
  return btoa(raw).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function passkeysSupported() {
  return window.isSecureContext === true && typeof window.PublicKeyCredential === "function" &&
    !!(navigator.credentials && navigator.credentials.get && navigator.credentials.create);
}

function passkeyError(e) {
  const name = e && e.name;
  if (name === "NotAllowedError" || name === "AbortError") return new Error("顔認証がキャンセルされたか、時間切れでした");
  if (name === "InvalidStateError") return new Error("この端末のパスキーはすでに登録されています");
  if (name === "NotSupportedError" || name === "SecurityError") {
    return new Error("この接続では顔認証を使えません。https://…ts.net で開いてください");
  }
  return e instanceof Error ? e : new Error("顔認証に失敗しました");
}

async function registerPasskey(label) {
  if (!passkeysSupported()) throw new Error("顔認証は https://…ts.net で開いたときだけ使えます");
  const opts = await api("/api/passkeys/register/begin", {});
  const publicKey = {
    challenge: b64uToBuf(opts.challenge), rp: opts.rp, timeout: opts.timeout,
    user: {id: b64uToBuf(opts.user.id), name: opts.user.name, displayName: opts.user.displayName},
    pubKeyCredParams: opts.pubKeyCredParams, authenticatorSelection: opts.authenticatorSelection,
    attestation: opts.attestation,
    excludeCredentials: (opts.excludeCredentials || []).map(c => ({type: c.type, id: b64uToBuf(c.id),
      transports: c.transports}))
  };
  let credential;
  try { credential = await navigator.credentials.create({publicKey}); } catch (e) { throw passkeyError(e); }
  if (!credential) throw new Error("顔認証を登録できませんでした");
  const r = credential.response;
  return api("/api/passkeys/register/finish", {
    id: credential.id, rawId: bufToB64u(credential.rawId), clientDataJSON: bufToB64u(r.clientDataJSON),
    attestationObject: bufToB64u(r.attestationObject),
    transports: typeof r.getTransports === "function" ? r.getTransports() : [], label});
}

let passkeyAbort = null;
let passkeyRenewTimer = 0;

function abortPasskeyLogin() {
  clearTimeout(passkeyRenewTimer);
  passkeyRenewTimer = 0;
  if (passkeyAbort) { try { passkeyAbort.abort(); } catch (e) { /* ignore */ } }
  passkeyAbort = null;
}

// mediation "optional": the Face ID sheet (button). "conditional": passkey autofill on the password field;
// it waits silently, so errors are swallowed and the request is renewed before the server challenge expires.
async function passkeyLogin(mediation) {
  if (!passkeysSupported()) throw new Error("顔認証は https://…ts.net で開いたときだけ使えます");
  abortPasskeyLogin();
  const controller = new AbortController();
  passkeyAbort = controller;
  const quiet = mediation === "conditional";
  if (quiet) schedulePasskeyRenew();
  try {
    const opts = await api("/api/passkeys/login/begin", {});
    const request = {publicKey: {challenge: b64uToBuf(opts.challenge), rpId: opts.rpId, timeout: opts.timeout,
      userVerification: opts.userVerification, allowCredentials: []}, signal: controller.signal};
    if (quiet) request.mediation = "conditional";
    let credential;
    try { credential = await navigator.credentials.get(request); } catch (e) {
      if (quiet) return;
      throw passkeyError(e);
    }
    if (!credential || controller.signal.aborted) return;
    const r = credential.response;
    let result;
    try {
      result = await api("/api/passkeys/login/finish", {
        id: credential.id, clientDataJSON: bufToB64u(r.clientDataJSON),
        authenticatorData: bufToB64u(r.authenticatorData), signature: bufToB64u(r.signature),
        userHandle: r.userHandle ? bufToB64u(r.userHandle) : null});
    } catch (e) {
      if (quiet) { schedulePasskeyRenew(); return; }
      throw e;
    }
    passkeyAbort = null;
    state.csrf = result.csrf;
    state.offerPasskey = false;
    $("password").value = "";
    await go();
  } catch (e) {
    if (quiet) return;
    throw e;
  } finally {
    if (passkeyAbort === controller && !quiet) passkeyAbort = null;
  }
}

function schedulePasskeyRenew() {
  clearTimeout(passkeyRenewTimer);
  passkeyRenewTimer = setTimeout(() => {
    if (state.authed || $("login").hidden) return;
    startConditionalLogin().catch(() => {});
  }, PASSKEY_RENEW_MS);
}

async function startConditionalLogin() {
  const pk = window.PublicKeyCredential;
  if (!passkeysSupported() || typeof pk.isConditionalMediationAvailable !== "function") return;
  if (!(await pk.isConditionalMediationAvailable())) return;
  // not awaited by the caller: this promise stays pending until the user picks a passkey or it is aborted
  await passkeyLogin("conditional");
}

function prepareLoginScreen() {
  abortPasskeyLogin();
  $("loginRemember").checked = state.https === true;
  const usable = passkeysSupported() && state.passkey && state.passkey.available && state.passkey.registered > 0;
  setShown("passkeyLogin", usable);
  const hint = $("passkeyLoginHint");
  const secureNote = !passkeysSupported() && state.https !== true && location.protocol === "http:" ? 
    "顔認証は https://…ts.net で開いたときだけ使えます" : "";
  hint.textContent = secureNote;
  hint.hidden = !secureNote;
  $("password").setAttribute("autocomplete", usable ? "username webauthn" : "current-password");
  if (usable) startConditionalLogin().catch(() => {});
}

function isIosDevice() {
  return /iPhone|iPad|iPod/.test(navigator.userAgent) ||
    (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
}

// After the first password login over HTTPS on an iPhone without a passkey: ask once.
function maybeOfferPasskey() {
  if (!state.offerPasskey) return;
  state.offerPasskey = false;
  if (!passkeysSupported() || !isIosDevice() || state.https !== true) return;
  if (!state.passkey || !state.passkey.available || state.passkey.registered > 0) return;
  if (readLocal(FACE_OFFER_KEY) === "done") return;
  setShown("passkeyOffer", true);
}

function renderPasskeys(data) {
  const secure = passkeysSupported() && data.available === true;
  setShown("passkeyUnavailable", !secure);
  $("passkeyRegister").disabled = !secure;
  const link = $("passkeyHttpsLink");
  if (!secure && typeof data.https_url === "string" && data.https_url.startsWith("https://") &&
      location.origin + "/" !== data.https_url) {
    link.href = data.https_url;
    link.hidden = false;
  } else {
    link.hidden = true;
  }
  const items = Array.isArray(data.items) ? data.items : [];
  const nodes = [];
  if (!items.length) nodes.push(element("p", "登録したパスキーはありません。", "empty-state"));
  for (const item of items) {
    const box = element("div", undefined, "item");
    box.appendChild(element("b", item.label));
    box.appendChild(element("span", "登録 " + formatTime(item.created_at) + " · 最終使用 " +
      (item.last_used_at ? formatTime(item.last_used_at) : "未使用")));
    const rename = element("button", "名前変更", "btn small");
    rename.type = "button";
    rename.addEventListener("click", () => perform(async () => {
      const label = prompt("パスキーの名前", item.label);
      if (label === null) return;
      await api("/api/passkeys/rename", {id: item.id, label});
      swrDrop("passkeys");
      await swr("passkeys", "/api/passkeys", renderPasskeys, {force: true});
    }));
    const del = element("button", "削除", "btn small");
    del.type = "button";
    del.addEventListener("click", () => perform(async () => {
      if (!confirm("「" + item.label + "」を削除します。この顔認証ではログインできなくなります。よろしいですか？")) return;
      await api("/api/passkeys/delete", {id: item.id});
      swrDrop("passkeys");
      await swr("passkeys", "/api/passkeys", renderPasskeys, {force: true});
    }));
    box.append(rename, del);
    nodes.push(box);
  }
  $("passkeyList").replaceChildren(...nodes);
  $("sessionCount").textContent = typeof data.sessions === "number" ?
    "ログイン中の端末（セッション）: " + data.sessions + "台" : "";
}

async function registerPasskeyFromSettings() {
  const input = $("passkeyLabel");
  const label = input.value.trim() || (isIosDevice() ? "iPhone" : "このデバイス");
  $("passkeyStatus").textContent = "顔認証の確認を待っています…";
  try {
    await registerPasskey(label);
    input.value = "";
    flash("顔認証を登録しました");
  } finally {
    $("passkeyStatus").textContent = "";
  }
  swrDrop("passkeys");
  await swr("passkeys", "/api/passkeys", renderPasskeys, {force: true});
}

async function revokeAllSessions() {
  if (!confirm("すべての端末（この端末を含む）からログアウトします。パスキーは残ります。よろしいですか？")) return;
  for (const tab of state.tabs) releaseRemote(tab);
  await api("/api/sessions/revoke-all", {});
  removeLocal(RECENT_KEY);      // v0.1.63: the same as a logout
  swrDrop("passkeys");
  writeSession(TAB_STORAGE_KEY, null);
  writeSession(SHELL_KEY, null);
  history.replaceState(null, "", "#/home");
  state.csrf = "";
  await go();
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

// Copies text. navigator.clipboard when there is one (secure contexts); otherwise, or when it refuses, a hidden
// textarea + document.execCommand("copy") (works on plain http in Safari). Call it directly from the tap
// handler with text that is already at hand: the copy command needs the user gesture. Resolves to true/false.
function execCopy(text) {
  const area = document.createElement("textarea");
  area.value = text;
  area.setAttribute("readonly", "");
  area.setAttribute("aria-hidden", "true");
  area.style.cssText = "position:fixed;left:0;top:0;width:1px;height:1px;padding:0;border:0;opacity:0;font-size:16px";
  const keep = document.activeElement;
  document.body.appendChild(area);
  let ok = false;
  try {
    area.focus({preventScroll: true});
    area.select();
    area.setSelectionRange(0, text.length);
    ok = document.execCommand("copy") === true;
  } catch (e) { ok = false; }
  area.remove();
  try { if (keep && keep.focus) keep.focus({preventScroll: true}); } catch (e) { /* ignore */ }
  return ok;
}

async function copyToClipboard(text) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (e) { /* refused (permissions, not focused): try the copy command */ }
  }
  return execCopy(text);
}

async function copyText(text) {
  const ok = await copyToClipboard(text);
  flash(ok ? "コピーしました" : "コピーできませんでした。長押しで選択してください");
  return ok;
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
  const folderKeys = (preview.package_folders || []).filter(x => !x.compliant);
  if (!changed.length && !folderKeys.length) { flash("推奨設定からの変更はありません"); return; }
  if (!confirm("次のJD2設定をOFFに変更します（既存キューは変更しません）。\n" +
      changed.map(x => "・" + x.label).join("\n") +
      folderKeys.map(x => "\n・パッケージ別フォルダ: " + x.key).join("") + "\n適用しますか？")) return;
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
    await delay(UPDATE_POLL_MS);
    const version = await fetchHealthVersion();
    if (version && before && version !== before) {
      reloadFresh();           // v0.1.60: the cached shell is the old version: drop it first
      return;
    }
  }
  state.updateWatching = false;
  stickyToast("update", "更新の完了を確認できませんでした。Hubの更新ログを確認し、手動で再読み込みしてください" +
    "（再読み込み後に再ログインが必要です）", "error",
    [{label: "再読み込み", run: reloadFresh}]);
}

// ------------------------------------------------------------ session / boot
function teardownSession() {
  state.authed = false;
  state.shellReady = false;
  stopStateLoop();
  state.live = null; state.stateRev = null; state.settingsRev = null; state.engineSig = ""; state.rbSig = ""; state.dlKey = null;
  clearTimeout(state.pasteTimer);
  clearToast();
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
  state.rbReady = null;
  setDebugState(null);
  setPerf(false);
  closeProbeSheet();
  hidePopupToast();
  state.siteRules = null;
  state.waiting = null;
  cancelPress();
  closeStream();
  $("remoteImg").removeAttribute("src");
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
  renderDownloadsSummary(null, false);
  state.homeTabsSig = ""; $("homeTabGrid").replaceChildren(); setShown("homeTabs", false);     // v0.1.62
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
    state.passkey = session.passkey || {available: false, registered: 0};
    updateLanWarning(session.tailnet === true, state.https);
    if (session.authenticated) {
      state.authed = true;
      abortPasskeyLogin();
      setShown("authArea", false);
      setShown("app", true);
      initShellState();
      await primeState();      // one request: engine, downloads, NAS browser, settings, bookmarks
      if (state.tabs.some(t => t.pt)) await refreshPt();       // v0.1.57: only for a restored passthrough tab (no extra request at home)
      applyRoute();
      syncStateLoop();
    } else {
      teardownSession();
      setShown("app", false);
      const boot = await api("/api/setup/status");
      setShown("authArea", true);
      setShown("setup", !boot.configured);
      setShown("login", boot.configured);
      if (boot.configured) prepareLoginScreen();
    }
    if (session.authenticated) maybeOfferPasskey();
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
    if (!$("linkMenu").hidden) { closeLinkMenu(); return; }          // v0.1.63
    if (!$("menu").hidden) { closeMenu(); return; }
    if (!$("tabSwitcher").hidden) { closeSwitcher(); return; }
    if (!$("probeSheet").hidden) { closeProbeSheet(); return; }       // v0.1.63: dialogs close with Esc too
    if (!$("remoteSheet").hidden) { closeRemoteSheet(); return; }
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

// ---------------------------------------------------------------- v0.1.63: keyboard, mouse buttons, trackpad
// PC (mouse + keyboard) and iPad with a hardware keyboard get the same actions as the touch gestures. They are
// callers of the same function the flick uses (stepHistory through inputStep), so the decisions stay in one place:
//   Alt+← / Alt+→, Cmd+[ / Cmd+], mouse buttons 4 / 5, a sideways two-finger swipe  = back / forward of the NAS page
//   Ctrl/Cmd+L = address bar     Ctrl/Cmd+R, F5 = reload the NAS page (inside a web tab only)
// A web tab is never left by any of them (the "nothing earlier" toast is the only effect at the first page).
function stepTab() {
  const tab = activeWebTab();
  return tab && (tab.rb || tab.pt) && tab.history.length ? tab : null;
}

// kind: "key" | "mouse" | "wheel" | "frame" (dedupes with a popstate / flick that arrives together)
function inputStep(dir, kind) {
  const tab = stepTab();
  if (!tab || !$("tabSwitcher").hidden || !$("readerViewer").hidden) return false;
  // a popstate that follows the browser's own handling of the same input within 400 ms is the same step: dropped
  if (!claimStep(kind === "frame" ? "flick" : "input")) { gestureTrace("input.reject", {why: "dup", kind}); return true; }
  state.gestureAt = Date.now();
  gestureTrace("input.step", {kind, dir});
  dlog("ui", "info", "input", {kind, dir: dir < 0 ? "back" : "forward"});
  gestureHint(dir < 0 ? "← 戻る" : "進む →");
  setTimeout(() => gestureHint(""), 600);
  perform(() => stepHistory(tab, dir));
  return true;
}

// Which app action a key press means ("" = none). Meta is the Mac command key.
function shortcutOf(event) {
  const key = String(event.key || ""), code = String(event.code || "");
  const mod = event.ctrlKey || event.metaKey;
  if (event.altKey && !mod && !event.shiftKey && (key === "ArrowLeft" || key === "ArrowRight")) return key === "ArrowLeft" ? "back" : "forward";
  if (event.metaKey && !event.ctrlKey && !event.altKey && !event.shiftKey) {
    if (key === "[" || code === "BracketLeft") return "back";
    if (key === "]" || code === "BracketRight") return "forward";
  }
  if (mod && !event.altKey && !event.shiftKey && key.toLowerCase() === "l") return "omnibox";
  if (mod && !event.altKey && key.toLowerCase() === "r") return "reload";
  if (key === "F5" && !mod && !event.altKey) return "reload";
  return "";
}

// Runs the action; true = it was the app's (the caller prevents the browser's own handling).
function runShortcut(name, typing) {
  if (!state.authed) return false;
  if (name === "omnibox") { focusOmnibox(); return true; }
  if (typing) return false;                       // a text field keeps its own Alt+arrows / Cmd+[ / reload
  const tab = stepTab();
  if (!tab || !$("readerViewer").hidden) return false;     // outside a web tab the browser's own action stays
  if (name === "back" || name === "forward") return inputStep(name === "back" ? -1 : 1, "key");
  if (name === "reload") { perform(() => reloadTab(tab)); return true; }
  return false;
}

function onShortcutKey(event) {
  const name = shortcutOf(event);
  if (!name || event.defaultPrevented) return;
  if (event.repeat && name !== "omnibox") {         // a held key is one step, not thirty
    if (stepTab() && !isTypingTarget(event.target)) event.preventDefault();
    return;
  }
  if (runShortcut(name, isTypingTarget(event.target))) event.preventDefault();
}

// v0.1.63: the tooltips of the bar buttons name their keys (the Mac wording on Apple devices)
function shortcutTitles() {
  let apple = false;
  try { apple = /Mac|iPhone|iPad|iPod/.test(navigator.platform || "") || /Mac OS X/.test(navigator.userAgent || ""); } catch (e) { apple = false; }
  const set = (id, text) => { const el = $(id); if (el) { el.title = text; } };
  set("navBack", apple ? "戻る（⌘[ / ⌥←）" : "戻る（Alt+←）");
  set("navForward", apple ? "進む（⌘] / ⌥→）" : "進む（Alt+→）");
  set("navReload", apple ? "再読み込み（⌘R）" : "再読み込み（Ctrl+R）");
  set("omnibox", apple ? "アドレスバー（⌘L または /）" : "アドレスバー（Ctrl+L または /）");
}

// Mouse buttons 4 / 5. preventDefault on all three events so that the browser does not also go back (that would
// leave the app); only a web tab takes them, elsewhere the browser's own back / forward stays.
function onMouseNav(event) {
  if (event.button !== 3 && event.button !== 4) return;
  if (!state.authed || !stepTab()) return;
  event.preventDefault();
  if (event.type === "mouseup") inputStep(event.button === 3 ? -1 : 1, "mouse");
}

// A sideways two-finger swipe on a trackpad (wheel events with a large deltaX) = back / forward. Not while the page
// itself can scroll sideways (a wide table, a horizontal scroller: v0.1.55 hScroller / a page wider than the window),
// and after one step the rest of the swipe's inertia is ignored until the wheel has been quiet for a moment.
const WHEEL_SWIPE_PX = 160, WHEEL_GAP_MS = 180, WHEEL_QUIET_MS = 450;
const wheelSwipe = {sum: 0, at: 0, quietUntil: 0};
function pageScrollsSideways(doc) {
  try {
    const el = doc.scrollingElement || doc.documentElement;
    if (!(el.scrollWidth > el.clientWidth + 2)) return false;
    const view = doc.defaultView;
    return view.getComputedStyle(doc.documentElement).overflowX !== "hidden" && view.getComputedStyle(doc.body).overflowX !== "hidden";
  } catch (e) { return false; }
}
// The last wheel decisions (RAM only, small): read by the E2E scenarios when a trackpad check fails.
const wheelLog = [];
function wheelNote(entry) { wheelLog.push(entry); if (wheelLog.length > 24) wheelLog.shift(); }
function onWheelSwipe(event, doc, target = event.target) {
  if (event.ctrlKey || event.metaKey || event.defaultPrevented || !state.authed) return;
  const now = performance.now();
  if (now < wheelSwipe.quietUntil) { wheelSwipe.quietUntil = now + WHEEL_QUIET_MS; return; }
  const dx = event.deltaX, dy = event.deltaY;
  if (Math.abs(dx) < 1 || Math.abs(dx) < 1.5 * Math.abs(dy)) { wheelSwipe.sum = 0; return; }
  if (now - wheelSwipe.at > WHEEL_GAP_MS || Math.sign(dx) !== Math.sign(wheelSwipe.sum)) wheelSwipe.sum = 0;
  wheelSwipe.at = now;
  // The page that would move is the one on screen: in the mirror display that is the mirror page, whichever
  // document the engine handed the wheel to (WebKit and Chromium differ for a wheel over a frame).
  let shown = null;
  try { const t = stepTab(); shown = t && tabDisplay(t) === "mirror" ? mirrorDoc() : null; } catch (e) { shown = null; }
  const sideways = pageScrollsSideways(doc || document) || Boolean(shown && pageScrollsSideways(shown));
  const scroller = Boolean(hScroller(target));
  wheelNote({doc: doc === document ? "app" : "frame", tgt: target && (target.id || target.nodeName), dx: Math.round(dx),
             sideways, scroller, sum: Math.round(wheelSwipe.sum)});
  if (!stepTab() || scroller || sideways) { wheelSwipe.sum = 0; return; }
  wheelSwipe.sum += dx;
  if (Math.abs(wheelSwipe.sum) < WHEEL_SWIPE_PX) return;
  const dir = wheelSwipe.sum < 0 ? -1 : 1;           // fingers to the right = deltaX < 0 = back
  wheelSwipe.sum = 0;
  wheelSwipe.quietUntil = now + WHEEL_QUIET_MS;
  inputStep(dir, "wheel");
}

// The same inputs for a document that does not bubble into the app (the mirror frame).
function wireInputDoc(doc) {
  doc.addEventListener("keydown", event => { onShortcutKey(event); if (!event.defaultPrevented) onKeydown(event); });
  for (const type of ["mousedown", "mouseup", "auxclick"]) doc.addEventListener(type, onMouseNav, true);
  doc.addEventListener("wheel", event => onWheelSwipe(event, doc), {passive: true});
  try { doc.documentElement.style.overscrollBehaviorX = "none"; } catch (e) { /* ignore */ }
}

// ---- right-click on a link in the mirror: the app's own small menu (never the browser's)
const linkMenu = {url: "", tab: null};
function closeLinkMenu() {
  linkMenu.url = ""; linkMenu.tab = null;
  setShown("linkMenu", false);
}
function openLinkMenu(tab, url, x, y) {
  linkMenu.url = url; linkMenu.tab = tab;
  const menu = $("linkMenu");
  menu.hidden = false;
  const w = menu.offsetWidth, h = menu.offsetHeight;
  menu.style.left = Math.max(4, Math.min(x, window.innerWidth - w - 4)) + "px";
  menu.style.top = Math.max(4, Math.min(y, window.innerHeight - h - 4)) + "px";
  const first = menu.querySelector("button");
  if (first) first.focus({preventScroll: true});
}
async function linkMenuAction(act) {
  const url = linkMenu.url;
  closeLinkMenu();
  if (!/^https?:\/\/\S+$/i.test(url) || url.length > 2048) return;
  if (act === "copy") await copyText(url);
  else if (act === "add") {
    await api("/api/links", {url});          // the same local, deletable staging as ⋮ › このURLをリンクに追加
    swrDrop("links");
    flash("追加したリンクに入れました（JD2へは未送信）");
  } else if (act === "open") await openInFreshTab(url);
}
function wireLinkMenu() {
  const menu = $("linkMenu");
  menu.addEventListener("click", event => {
    const b = event.target.closest("[data-link-act]");
    if (b) perform(() => linkMenuAction(b.dataset.linkAct));
  });
  menu.addEventListener("keydown", event => {
    if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
    const items = Array.from(menu.querySelectorAll("button"));
    const at = items.indexOf(document.activeElement);
    event.preventDefault();
    items[(at + (event.key === "ArrowDown" ? 1 : -1) + items.length) % items.length].focus();
  });
  document.addEventListener("pointerdown", event => { if (!menu.hidden && !menu.contains(event.target)) closeLinkMenu(); }, true);
  window.addEventListener("blur", closeLinkMenu);       // a click into the mirror frame is a window blur for this document
}

function onMenuKeydown(event) {
  if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
  const items = Array.from($("menu").querySelectorAll(MENU_ITEMS)).filter(n => !n.disabled && n.offsetParent !== null);
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

// v0.1.60: the bar collapse helpers and the cache reset button.
function wireChrome() {
  $("chromePeek").addEventListener("click", () => chromeShow());
  if (chromeNarrow && chromeNarrow.addEventListener) chromeNarrow.addEventListener("change", () => chromeShow(true));
  $("appCacheClear").addEventListener("click", () => perform(async () => {
    $("appCacheStatus").textContent = "消しています…";
    await dropShellCache();
    $("appCacheStatus").textContent = "消しました。再読み込みします…";
    setTimeout(() => location.reload(), 300);
  }));
}

function wireShell() {
  window.addEventListener("hashchange", onHashChange);
  window.addEventListener("popstate", onPopState);
  // v0.1.55: a click on an in-app #-link is the app's own navigation (not a page back).
  document.addEventListener("click", event => {
    const a = event.target && event.target.closest ? event.target.closest("a[href^='#']") : null;
    if (a) noteAppNav();
  }, true);
  // One visibility gate for everything periodic: the state long-poll and the scheduler.
  document.addEventListener("visibilitychange", () => {
    syncStateLoop();
    sched.kick();
  });
  document.addEventListener("keydown", onKeydown);
  // v0.1.63: PC / iPad keyboard, mouse buttons 4 / 5 and trackpad swipes (the mirror frame wires its own copy)
  document.addEventListener("keydown", onShortcutKey, true);
  for (const type of ["mousedown", "mouseup", "auxclick"]) window.addEventListener(type, onMouseNav, true);
  document.addEventListener("wheel", event => {
    // The passthrough page decides for itself (pt-helper.js): it knows whether it scrolls sideways.
    if (event.target && event.target.id === "ptFrame") return;
    // Some engines deliver a wheel over the mirror frame to this document: judge the mirror page, not the app.
    const frame = $("mirrorFrame");
    if (frame && event.target && (event.target === frame || event.target.id === "mirrorWrap")) {
      const doc = (() => { try { return mirrorDoc(); } catch (e) { return null; } })();
      if (doc) {
        const r = frame.getBoundingClientRect();
        let under = null;
        try { under = doc.elementFromPoint(event.clientX - r.left, event.clientY - r.top); } catch (e) { under = null; }
        onWheelSwipe(event, doc, under || doc.body);
        return;
      }
    }
    onWheelSwipe(event, document);
  }, {passive: true});
  wireLinkMenu();
  // v0.1.63: wider than the NAS page (> 1600 px) the page is centred; the wheel over the empty sides still scrolls it
  $("mirrorWrap").addEventListener("wheel", event => {
    if (event.target !== $("mirrorWrap")) return;
    const doc = mirrorDoc();
    if (!doc) return;
    const sc = mirrorScroller(doc);
    sc.scrollTop += event.deltaY; sc.scrollLeft += event.deltaX;
  }, {passive: true});
  document.addEventListener("click", event => {
    if (!$("menu").hidden && !event.target.closest(".menu-wrap, #modeBtn")) closeMenu(false);
  });
  $("tabstrip").addEventListener("keydown", onTabstripKeydown);
  shortcutTitles();
  $("newTab").addEventListener("click", createBlankTab);

  $("navBack").addEventListener("click", () => {
    const tab = activeWebTab();
    if (tab) perform(() => stepHistory(tab, -1));
    else { noteAppNav(); history.back(); }
  });
  $("navForward").addEventListener("click", () => {
    const tab = activeWebTab();
    if (tab) perform(() => stepHistory(tab, 1));
    else { noteAppNav(); history.forward(); }
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
  // v0.1.60: focus shows (and selects) the full URL; leaving the field shows the domain again.
  $("omnibox").addEventListener("focus", () => {
    const box = $("omnibox");
    chromeShow(true);
    if (box.value === state.omniShort) box.value = state.omniDisplay;
    box.select();
    requestAnimationFrame(() => { if (document.activeElement === box) box.select(); });   // iOS places the caret after focus
    state.omniSelectOnUp = true;
    setTimeout(() => { if (document.activeElement === box && state.omniSelectOnUp) box.select(); }, 60);
  });
  // WebKit puts the caret where the tap ended (after focus and the frame above): keep the whole URL selected once
  $("omnibox").addEventListener("mouseup", e => {
    if (!state.omniSelectOnUp) return;
    state.omniSelectOnUp = false;
    e.preventDefault();
    $("omnibox").select();
  });
  $("omnibox").addEventListener("blur", () => { state.omniSelectOnUp = false; $("omnibox").value = state.omniShort; });
  $("modeBtn").addEventListener("click", () => {      // v0.1.60: the display chooser is the "表示" group of the menu
    if (!$("menu").hidden) { closeMenu(false); return; }
    openMenu();
    const on = $("menu").querySelector("[data-display][aria-checked='true']:not([hidden])");
    if (on) on.focus();
  });
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
      abortPasskeyLogin();
      const r = await api("/api/login", {password: $("password").value, remember: $("loginRemember").checked});
      state.csrf = r.csrf;
      state.offerPasskey = true;       // a password login: maybe suggest a passkey once (maybeOfferPasskey)
      $("password").value = "";
      await go();
    });
  });
  $("passkeyLogin").addEventListener("click", () => perform(() => passkeyLogin("optional")));
  $("passkeyRegister").addEventListener("click", () => perform(() => registerPasskeyFromSettings()));
  $("revokeAll").addEventListener("click", () => perform(revokeAllSessions));
  $("passkeyOfferYes").addEventListener("click", () => perform(async () => {
    writeLocal(FACE_OFFER_KEY, "done");
    setShown("passkeyOffer", false);
    await registerPasskey("このデバイス");
    flash("顔認証を登録しました。次回から「顔認証でログイン」が使えます");
    swrDrop("passkeys");
  }));
  $("passkeyOfferLater").addEventListener("click", () => setShown("passkeyOffer", false));
  $("passkeyOfferNever").addEventListener("click", () => {
    writeLocal(FACE_OFFER_KEY, "done");
    setShown("passkeyOffer", false);
  });
}

function wireDownloads() {
  $("downloadsRefresh").addEventListener("click", () => {
    state.downloadsFails = 0;
    loadDownloads(state.downloadsPage);
  });
  wireSheet();
  $("linksMore").addEventListener("click", linksMenu);
  $("homeEngineSetup").addEventListener("click", () => perform(() => startEngineSetup()));
  $("homeEngineStart").addEventListener("click", () => perform(startEngine));
  $("homePasteOpen").addEventListener("click", () => perform(openClipboardUrl));     // v0.1.62: clipboard read on this tap only
  $("homeRecentClear").addEventListener("click", clearRecent);
  $("downloadsEngineAction").addEventListener("click", () => {
    if ($("downloadsEngineAction").dataset.action === "setup") navigate("#/settings/jd2/engine");
    else perform(startEngine);
  });
  $("waitingStart").addEventListener("click", () => perform(startWaiting));
  $("waitingClean").addEventListener("click", () => perform(cleanWaiting));
  $("downloadsToggle").addEventListener("click", () => perform(toggleDownloads));
  $("downloadsClear").addEventListener("click", () => perform(clearFinished));
  $("downloadsPrev").addEventListener("click", () => loadDownloads(state.downloadsPage - 1));
  $("downloadsNext").addEventListener("click", () => loadDownloads(state.downloadsPage + 1));
  document.querySelectorAll(".chip").forEach(c => c.addEventListener("click", () => setFilter(c.dataset.filter)));
  $("downloadsSearch").addEventListener("input", () => {
    state.query = $("downloadsSearch").value;
    renderDownloads();
  });
}

function wireLinks() {
  $("pasteBox").addEventListener("input", e => schedulePaste(e && e.inputType === "insertFromPaste" ? 0 : PASTE_DEBOUNCE_MS));
  $("pasteButton").addEventListener("click", () => perform(pasteFromClipboard));
  $("pasteSend").addEventListener("click", () => perform(() => addPasted(true)));
  $("pasteAdd").addEventListener("click", () => perform(addPasted));
  $("sendAll").addEventListener("click", () => perform(sendAllStaged));
}

// ------------------------------------------------- scheduler and unified state (v0.1.33)
// One scheduler runs every periodic task, and only while the page is visible: each task says
// when it is wanted (`when`), how often it runs (`every`) and what it does (`run`). Nothing else
// in this file starts an interval or a polling timeout.
const sched = {
  tasks: [], timer: 0,
  add(name, every, when, run) { this.tasks.push({name, every, when, run, last: 0, busy: false}); },
  kick() {
    clearTimeout(this.timer);
    this.timer = 0;
    if (document.visibilityState !== "visible" || !state.authed) return;
    const now = Date.now();
    let next = Infinity;
    for (const task of this.tasks) {
      if (task.busy || !task.when()) continue;
      const due = task.last + task.every;
      if (due <= now) {
        task.busy = true;
        Promise.resolve().then(task.run).catch(() => {}).finally(() => {
          task.busy = false;
          task.last = Date.now();
          this.kick();
        });
      } else {
        next = Math.min(next, due);
      }
    }
    if (next !== Infinity) this.timer = setTimeout(() => this.kick(), Math.max(50, next - now));
  },
};

function registerTasks() {
  sched.add("downloads", AUTO_REFRESH_MS, downloadsTaskWanted,
    () => loadDownloads(state.downloadsPage, {auto: true}));
  sched.add("disk", DISK_REFRESH_MS, diagVisible, loadDisk);
  sched.add("rb-mem", 5000, browserSectionVisible, () => loadBrowserStatus(true));
  sched.add("debug-view", DEBUG_VIEW_MS, () => dbgView.open && !dbgView.paused, () => pollDebugView(false));
  sched.add("debug-fps", 10000, () => state.debug.enabled, debugFps);
  sched.add("debug-remaining", 5000, () => state.debug.enabled && diagVisible(), renderDebugCard);
  sched.add("rb-perf", 1000, () => state.rbPerf, renderPerf);
}

function stateWanted() { return state.authed && document.visibilityState === "visible"; }

function stopStateLoop() {
  state.stateGen++;
  state.stateRunning = false;
  if (state.stateAbort) { try { state.stateAbort.abort(); } catch (e) { /* ignore */ } }
  state.stateAbort = null;
}

function syncStateLoop() {
  if (!stateWanted()) { if (state.stateRunning) stopStateLoop(); return; }
  if (!state.stateRunning && state.stateRev !== null) runStateLoop();
}

// The first call (no rev) answers at once and carries settings and bookmarks.
async function primeState() {
  try {
    applyState(await api("/api/state?full=1"));
  } catch (e) {
    /* the loop below retries; the views fetch what they need meanwhile */
  }
}

async function runStateLoop() {
  const gen = ++state.stateGen;
  state.stateRunning = true;
  while (gen === state.stateGen && stateWanted()) {
    try {
      const ctl = new AbortController();
      state.stateAbort = ctl;
      const data = await api("/api/state?rev=" + state.stateRev + "&wait=" + STATE_WAIT_S, null,
        {signal: ctl.signal, cache: "no-store"});
      if (gen !== state.stateGen) return;
      state.stateFails = 0;
      applyState(data);
    } catch (e) {
      if (gen !== state.stateGen) return;
      state.stateFails++;
      await delay(Math.min(15000, 1000 * Math.pow(2, state.stateFails)));
    }
  }
  if (gen === state.stateGen) state.stateRunning = false;
}

// Seed the swr cache so loadSettings()/loadBookmarks() paint without a request.
function swrSeed(key, data, render) {
  swrCache.set(key, {data, json: JSON.stringify(data), at: Date.now()});
  swrPaint(key, swrCache.get(key), render, key);
}

function applyState(d) {
  const first = state.stateRev === null;
  state.live = d;
  state.stateRev = d.rev;
  state.https = d.https === true;
  state.tailnet = d.tailnet === true;
  document.querySelectorAll(".js-version").forEach(n => { n.textContent = "v" + d.version; });
  document.querySelectorAll(".js-version-bare").forEach(n => { n.textContent = d.version; });
  checkBootVersion(d.version);
  if (d.settings) swrSeed("settings", d.settings, renderDownloadDir);
  if (d.bookmarks) swrSeed("bookmarks", {items: d.bookmarks}, renderBookmarks);
  const e = d.engine;
  // download counts for the badge / home line
  // v0.1.59: parts whose sub-signature did not change are not rendered again.
  const dlKey = JSON.stringify(d.downloads) + "|" + (e.can_send === true);
  const dlChanged = dlKey !== state.dlKey;
  state.dlKey = dlKey;
  state.dlRunning = d.downloads ? d.downloads.running : null;
  if (dlChanged || first) updateBadge();
  // engine: the full record only where it is shown, the compact block otherwise
  const engineChanged = e.sig !== state.engineSig;
  state.engineSig = e.sig;
  if (engineChanged || !state.engine) {
    if (engineDetailWanted() && !first) {
      swrDrop("engine");
      loadEngine(true).then(() => { if ($("engineAdvanced").open) return loadEngineLog(); }).catch(() => {});
      if (e.running) loadJd2Status(true).catch(() => {});
    } else {
      applyEngineLight(e);
    }
  } else if (dlChanged) {
    renderHome();
  }
  if ((engineChanged || dlChanged) && state.queueData && state.route.view === "links") renderQueue(state.queueData);
  // NAS browser: refetch the status only while its settings are on screen
  if (typeof d.rb.ready === "boolean") setRbReady(d.rb.ready);
  if (d.debug) setDebugState(d.debug);
  if (typeof d.rb.restarts === "number") {
    if (state.rbRestarts !== null && d.rb.restarts > state.rbRestarts) flash("NASブラウザを再起動しました");
    state.rbRestarts = d.rb.restarts;
  }
  if (typeof d.rb.lost === "number") {
    if (state.rbLost !== null && d.rb.lost > state.rbLost) resetTabsAfterCrash();
    state.rbLost = d.rb.lost;
  }
  if (d.rb.sig !== state.rbSig) {
    state.rbSig = d.rb.sig;
    if (browserSectionVisible()) { swrDrop("rb-status"); loadBrowserStatus(true).catch(() => {}); }
  }
  // settings revision: someone else changed data -> forget what is cached, refresh what is shown
  const rev = d.settings_rev;
  if (state.settingsRev !== null && rev !== state.settingsRev &&
      Date.now() - state.localWriteAt > LOCAL_WRITE_QUIET_MS) {
    swrDrop("settings", "bookmarks", "links", "share-tokens", "site-rules");
    if (state.route.view === "links") loadQueue(true).catch(() => {});
    else if (currentViewName() === "home") loadHome(true).catch(() => {});
  }
  state.settingsRev = rev;
  sched.kick();
}

// ------------------------------------------------------------------ engine
const PHASE_LABELS = {
  not_installed: "未導入", setup: "初期設定中", starting: "起動中…", running: "稼働中",
  stopped: "停止中", failed: "失敗",
};
const STEP_MARKS = {pending: "○", running: "…", done: "✓", skipped: "↷", warn: "!", failed: "✕"};
const STEP_TEXT = {pending: "待機", running: "実行中", done: "完了", skipped: "スキップ", warn: "警告", failed: "失敗"};

function engineSectionVisible() {
  return state.route.view === "settings" && state.route.section === "jd2";
}

// The full engine status (steps, components, log) is only needed where it is shown; the compact
// engine block of /api/state covers everything else.
function engineDetailWanted() {
  const e = state.engine;
  if (engineSectionVisible()) return true;
  return Boolean(e && (e.phase === "setup" || e.phase === "starting") &&
    (currentViewName() === "home" || state.route.view === "downloads"));
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
  } else if (before !== "running" && engineSectionVisible()) {
    loadJd2Status(true).catch(() => {});
  }
  sched.kick();
}

// Compact engine block of /api/state (no steps / components): enough for home, downloads, links.
function applyEngineLight(c) {
  const previous = state.engine;
  swrDrop("engine");
  if (previous && previous.light !== true && previous.phase === c.state && !engineDetailWanted()) {
    // keep the detailed record while the phase is unchanged
    state.engine = Object.assign({}, previous, {installed: c.installed, error: c.error,
      preset_applied: c.preset_applied, autostart: c.autostart});
  } else {
    state.engine = {light: true, phase: c.state, mode: c.mode, installed: c.installed, error: c.error,
      preset_applied: c.preset_applied, autostart: c.autostart, container: {available: c.cs}, steps: []};
  }
  renderHome();
  renderDownloadsShell();
  if (c.state !== "running") { state.dlRunning = null; updateBadge(); }
  sched.kick();
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
  // v0.1.62: the error is one line with a 詳しく toggle (the full text is one tap away)
  const errorText = e.error || "";
  if ($("engineError").textContent !== errorText) {
    $("engineError").classList.add("is-clamped");
    $("engineErrorMore").textContent = "詳しく";
    $("engineErrorMore").setAttribute("aria-expanded", "false");
  }
  $("engineError").hidden = !e.error;
  $("engineError").textContent = errorText;
  setShown("engineErrorRow", Boolean(e.error));
  setShown("engineErrorMore", Boolean(e.error));
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
  $("engineTestResult").textContent = jd ? (connected ? "JD2に接続できています" : "接続できません" +
    (jd.reason ? ": " + jd.reason : "")) : "";
  setShown("jd2Check", connected && jd.can_update === true);
  setShown("jd2Update", connected && jd.can_update === true && jd.update_available === true);
  setShown("jd2Preset", connected && jd.can_preset === true);
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
    $("accountsState").textContent = "JD2エンジンが起動していません";
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
      "登録されているアカウントはありません";
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
  if (!state.accountHosts.length) await loadAccountHosts();
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
  $("accountHost").addEventListener("focus", () => { if (engineRunning()) loadAccountHosts().catch(() => {}); });
}

// ---- Settings › システム › 診断: disk writes and the debug log (v0.1.35)
function diagVisible() {
  return state.route.view === "settings" && state.route.section === "diag";
}

function rateText(value) {
  return typeof value === "number" && Number.isFinite(value) ? readableSize(value) + "/s" : "—";
}

function renderDisk(data) {
  const rows = [];
  const measuring = data.measuring === true;
  $("diskToggle").textContent = measuring ? "計測を停止" : "計測を開始";
  $("diskToggle").classList.toggle("primary", !measuring);
  $("diskState").textContent = measuring ? "計測中" : data.stopped_at ? "停止しました（合計は停止時点）" : "未開始（平均は表示中だけ記録）";
  for (const source of data.sources || []) {
    const row = document.createElement("tr");
    const name = element("td", source.label + (source.kind === "array" ? "（RAID）" : source.kind === "disk" ? "（ディスク）" : ""));
    if (source.how) name.title = source.how;
    row.appendChild(name);
    if (!source.available) {
      const na = element("td", "取得できません", "na");
      na.colSpan = 3;
      if (source.reason) na.title = source.reason;
      row.appendChild(na);
    } else {
      const w = source.write || {};
      row.appendChild(element("td", rateText(w.rate_1m)));
      row.appendChild(element("td", rateText(w.rate_10m)));
      row.appendChild(element("td", typeof w.total === "number" ? readableSize(w.total) : "—"));
    }
    rows.push(row);
  }
  if (!rows.length) {
    const row = document.createElement("tr");
    const cell = element("td", "計測中…（最初の記録まで10秒ほどかかります）", "muted");
    cell.colSpan = 4;
    row.appendChild(cell);
    rows.push(row);
  }
  $("diskRows").replaceChildren(...rows);
}

async function loadDisk() {
  if (!diagVisible()) return;
  renderDisk(await api("/api/diag/diskio"));
}

function remainingText() {
  const left = Math.max(0, Math.round((state.debug.endsAt - Date.now()) / 1000));
  return "オン・残り " + Math.floor(left / 60) + "分" + String(left % 60).padStart(2, "0") + "秒で自動オフ";
}

function renderDebugCard() {
  if (!$("debugEnabled")) return;
  const d = state.debug;
  $("debugEnabled").checked = d.enabled;
  $("debugNote").textContent = d.enabled ? remainingText() : "オフ（RAMのみ・60分で自動オフ）";
  if (document.activeElement !== $("debugLevel")) $("debugLevel").value = d.level;
  const chosen = d.enabled ? d.categories : null;
  $("debugCats").querySelectorAll("input").forEach(box => {
    if (document.activeElement !== box && chosen) box.checked = chosen.includes(box.dataset.cat);
  });
  $("debugLevel").disabled = d.enabled;
  $("debugCats").querySelectorAll("input").forEach(box => { box.disabled = d.enabled; });
}

function buildDebugControls() {
  const levelOptions = DEBUG_LEVELS.map(l => {
    const o = element("option", DEBUG_LEVEL_LABELS[l]);
    o.value = l;
    return o;
  });
  $("debugLevel").replaceChildren(...levelOptions);
  $("debugLevel").value = "debug";
  const fl = [element("option", "すべてのレベル")].concat(DEBUG_LEVELS.map(l => {
    const o = element("option", DEBUG_LEVEL_LABELS[l] + "以上");
    o.value = l;
    return o;
  }));
  fl[0].value = "";
  $("debugFilterLevel").replaceChildren(...fl);
  const fc = [element("option", "すべてのカテゴリ")].concat(DEBUG_CATEGORIES.map(c => {
    const o = element("option", c + "（" + DEBUG_CATEGORY_LABELS[c] + "）");
    o.value = c;
    return o;
  }));
  fc[0].value = "";
  $("debugFilterCat").replaceChildren(...fc);
  $("debugCats").replaceChildren(...DEBUG_CATEGORIES.map(c => {
    const label = element("label", undefined, "switch compact");
    const input = document.createElement("input");
    input.type = "checkbox";
    input.setAttribute("role", "switch");
    input.dataset.cat = c;
    input.checked = true;
    label.appendChild(input);
    label.appendChild(element("span", undefined, "switch-track"));
    label.appendChild(element("span", c + " " + DEBUG_CATEGORY_LABELS[c]));
    return label;
  }));
}

const dbgView = {open: false, paused: false, entries: [], seq: 0, busy: false};

function debugFiltered() {
  const level = $("debugFilterLevel").value, cat = $("debugFilterCat").value;
  const needle = $("debugSearch").value.trim().toLowerCase();
  const min = level ? DEBUG_LEVELS.indexOf(level) : 0;
  return dbgView.entries.filter(e => DEBUG_LEVELS.indexOf(e.level) >= min && (!cat || e.category === cat) &&
    (!needle || (e.message + " " + (e.data ? JSON.stringify(e.data) : "")).toLowerCase().includes(needle)));
}

function pad(n, width = 2) { return String(n).padStart(width, "0"); }

function debugLine(e) {
  const t = new Date(e.ts * 1000);
  return pad(t.getHours()) + ":" + pad(t.getMinutes()) + ":" + pad(t.getSeconds()) + "." + pad(t.getMilliseconds(), 3);
}

function renderDebugView() {
  const shown = debugFiltered();
  const rows = shown.slice(-300).reverse().map(e => {
    const row = element("div", undefined, "debug-row " + e.level);
    row.appendChild(element("span", debugLine(e) + " " + e.src.slice(0, 1) + " " + e.category + " " + e.level + " ", "dbg-meta"));
    row.appendChild(element("span", e.message + (e.data ? " " + JSON.stringify(e.data) : "")));
    return row;
  });
  $("debugList").replaceChildren(...rows);
  $("debugMeta").textContent = shown.length + "件を表示（保持 " + dbgView.entries.length + "件）" +
    (state.debug.enabled ? "" : " · ログはオフです");
  $("debugFollow").textContent = dbgView.paused ? "一時停止中" : "追従中";
}

async function pollDebugView(initial = false) {
  if (dbgView.busy) return;
  dbgView.busy = true;
  try {
    const r = await api("/api/debug/log?since=" + (initial ? 0 : dbgView.seq) + "&limit=" + (initial ? 500 : 200));
    const list = Array.isArray(r.entries) ? r.entries : [];
    dbgView.entries = (initial ? list : dbgView.entries.concat(list)).slice(-DEBUG_VIEW_MAX);
    dbgView.seq = list.length ? list[list.length - 1].seq : (initial ? r.seq || 0 : dbgView.seq);
    renderDebugView();
  } finally { dbgView.busy = false; }
}

function debugJsonl() {
  return debugFiltered().map(e => JSON.stringify(e)).join("\n") + "\n";
}

function openDebugView() {
  dbgView.open = true;
  dbgView.paused = false;
  setShown("debugSheet", true);
  perform(() => pollDebugView(true));
  sched.kick();
}

function closeDebugView() {
  dbgView.open = false;
  setShown("debugSheet", false);
  sched.kick();
}

async function saveDebugConfig(enabled) {
  const body = {enabled};
  if (enabled) {
    body.level = $("debugLevel").value;
    body.categories = Array.from($("debugCats").querySelectorAll("input")).filter(b => b.checked).map(b => b.dataset.cat);
    if (!body.categories.length) throw new Error("カテゴリを1つ以上選んでください");
  }
  const status = await api("/api/debug/config", body);
  setDebugState(Object.assign({}, status, {remaining: status.remaining}));
  renderDebugCard();
}

function debugFps() {
  const now = performance.now();
  const frames = dbgFrames;
  const seconds = (now - dbgFramesAt) / 1000;
  dbgFrames = 0;
  dbgFramesAt = now;
  const tab = remoteTab();
  if (tab && seconds > 0 && tabDisplay(tab) === "live") {
    dlog("render", "debug", "polling frame rate", {fps: Math.round(frames / seconds * 10) / 10, seconds: Math.round(seconds)});
  }
}

function wireDiagnostics() {
  buildDebugControls();
  renderDebugCard();
  wireSelftest();
  $("diskToggle").addEventListener("click", () => perform(async () => {
    const measuring = $("diskToggle").textContent === "計測を停止";
    renderDisk(await api("/api/diag/diskio/" + (measuring ? "stop" : "start"), {}));
  }));
  $("debugEnabled").addEventListener("change", () => perform(async () => {
    const want = $("debugEnabled").checked;
    try { await saveDebugConfig(want); } catch (e) { $("debugEnabled").checked = !want; throw e; }
  }));
  $("debugView").addEventListener("click", openDebugView);
  $("debugClose").addEventListener("click", closeDebugView);
  $("debugSheet").addEventListener("click", e => { if (e.target === $("debugSheet")) closeDebugView(); });
  $("debugSheet").addEventListener("keydown", e => { if (e.key === "Escape") closeDebugView(); });
  for (const id of ["debugFilterLevel", "debugFilterCat"]) $(id).addEventListener("change", renderDebugView);
  $("debugSearch").addEventListener("input", renderDebugView);
  $("debugFollow").addEventListener("click", () => { dbgView.paused = !dbgView.paused; renderDebugView(); sched.kick(); });
  $("debugCopy").addEventListener("click", () => copyText(debugJsonl()));
  $("debugDownload").addEventListener("click", () => {
    // Client-side file: JSON Lines from the entries already on this page; the Hub writes nothing.
    const url = URL.createObjectURL(new Blob([debugJsonl()], {type: "application/x-ndjson"}));
    const link = document.createElement("a");
    link.href = url;
    link.download = "qnapdl-debug-" + new Date().toISOString().replace(/[:.]/g, "-") + ".jsonl";
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  $("debugClear").addEventListener("click", () => perform(async () => {
    if (!confirm("HubのRAM上のデバッグログをすべて消去します。よろしいですか？")) return;
    await api("/api/debug/clear", {});
    dbgView.entries = [];
    dbgView.seq = 0;
    await pollDebugView(true);
  }));
}

// ---- Settings › 診断 › 自己テスト (v0.1.44)
// ---------------------------------------------------------------------------------------------------
// v0.1.52 swipe diagnostic (Settings › 診断 › 自己テスト). The Hub holds an internal fixture tab at
// A → (check page) → A?token → B → C; this page shows it in the normal tab view and
//  - automatic: feeds the app's own handlers synthetic input (history.back/forward, TouchEvents on the
//    display, a toolbar pull) in each display mode and records what the app and the NAS did;
//  - manual: the person swipes with a real finger and answers できた / 違う / スキップ.
// Only measurements are sent (POST /api/selftest/swipe); the Hub judges. Everything is undone at the end.
// ---------------------------------------------------------------------------------------------------
const SWIPE_HOST = "selftest.example.com";
const SWIPE_EVENT_CAP = 60;
const SWIPE_PAYLOAD_MAX = 58000;
const SWIPE_AUTO_BUDGET_MS = 105000;     // no new display mode is started after this long
const SWIPE_MODES = ["mirror", "reader", "tiles"];
const SWIPE_MODE_NAME = {mirror: "ミラー", reader: "リーダー", tiles: "高速"};
let swipeDiag = null;                    // {tab, runId, prevHash, cleaning, aborted, manual, mode}

function swipeLetter(url) {
  try {
    const u = new URL(url);
    const m = u.hostname === SWIPE_HOST ? /^\/swipe\/([abc])$/.exec(u.pathname) : null;
    return m ? m[1].toUpperCase() : "?";
  } catch (e) { return "?"; }
}

function swipeTarget(el) {
  if (!el || !el.tagName) return "-";
  const holder = el.closest ? el.closest("[id]") : null;
  const cls = typeof el.className === "string" && el.className ? "." + el.className.split(/\s+/)[0] : "";
  return ((holder && holder.id ? "#" + holder.id + ">" : "") + el.tagName.toLowerCase() + cls).slice(0, 40);
}

function swipeCompact(events, cap) {
  let list = events;
  if (list.length > cap) list = list.filter(e => !/\.move$/.test(e.e));
  if (list.length > cap) {
    const head = Math.floor(cap / 3);
    list = list.slice(0, head).concat([{t: list[head].t, e: "trunc", d: "n=" + (list.length - cap)}], list.slice(list.length - (cap - head - 1)));
  }
  return list.map(e => ({t: e.t, e: e.e, d: e.d}));
}

// Records everything the app and the page saw during one step. gestureTrace() feeds note().
function makeSwipeRecorder() {
  const t0 = performance.now();
  const rec = {events: [], counts: {}, routeLeft: false, hintShown: false, flickWhy: "", pending: null, lastMove: -1000,
    gestures: [], off: [], docs: new WeakSet(), tabHash: swipeDiag ? "#/tab/" + swipeDiag.tab.id : ""};
  const fmt = data => !data ? "" : Object.keys(data).map(k => k + "=" + String(data[k]).replace(/\s+/g, " ")).join(" ").slice(0, 110);
  const push = (e, d) => { if (rec.events.length < 700) rec.events.push({t: Math.round(performance.now() - t0), e, d: fmt(d)}); };
  rec.note = (event, data) => {
    const key = String(event).replace(/[^a-z0-9_.]/g, "_").slice(0, 31);
    rec.counts[key] = (rec.counts[key] || 0) + 1;
    if (key === "gesture.hint" && data && data.text) rec.hintShown = true;
    if (key === "flick.reject" && data && data.why) rec.flickWhy = String(data.why);
    if (key === "route" && rec.tabHash && String((data && data.hash) || "") !== rec.tabHash) rec.routeLeft = true;
    // v0.1.55: a real touch outside the banner starts a gesture; what the app did until the next one belongs to it.
    const slot = {"step.call": "c", "step.moved": "m", popstate: "p", "flick.accepted": "f", "pop.stay": "s"}[key];
    if (key === "touch.start" && !(data && data.banner)) {
      if (rec.gestures.length < 40) rec.gestures.push({c: 0, m: 0, p: 0, f: 0, s: 0, nas: ""});
    } else if (slot) {
      // an action with no touch before it (history.back() from code) is its own gesture
      if (!rec.gestures.length) rec.gestures.push({c: 0, m: 0, p: 0, f: 0, s: 0, nas: ""});
      rec.gestures[rec.gestures.length - 1][slot]++;
    } else if (key === "nas" && data && data.page) {
      for (let i = rec.gestures.length - 1; i >= 0; i--) {         // the page a step led to
        const a = rec.gestures[i];
        if (a.c || a.p || a.f) { if (!a.nas) a.nas = String(data.page); break; }
      }
    }
    push(key, data);
  };
  const touch = kind => event => {
    const t = (event.changedTouches && event.changedTouches[0]) || (event.touches && event.touches[0]) || {};
    const x = Math.round(t.clientX), y = Math.round(t.clientY), now = performance.now();
    if (kind === "move") {
      if (now - rec.lastMove < 50) { rec.pending = [x, y, event]; return; }
      rec.lastMove = now; rec.pending = null;
    } else if (rec.pending) {
      const p = rec.pending; rec.pending = null;
      rec.note("touch.move", {x: p[0], y: p[1], last: 1});
    }
    const info = {x, y, tgt: swipeTarget(event.target), edge: x < GESTURE_EDGE_PX || x > window.innerWidth - GESTURE_EDGE_PX ? 1 : 0,
      trusted: event.isTrusted ? 1 : 0};
    if (kind === "start" && event.target && event.target.closest && event.target.closest("#swipeBanner")) info.banner = 1;   // the self-test's own buttons
    rec.note("touch." + kind, info);
  };
  const pointer = kind => event => {
    if (kind === "move") { const now = performance.now(); if (now - rec.lastMove < 50) return; rec.lastMove = now; }
    rec.note("pointer." + kind, {x: Math.round(event.clientX), y: Math.round(event.clientY), id: event.pointerId});
  };
  const listen = (target, type, fn) => { target.addEventListener(type, fn, {capture: true, passive: true}); rec.off.push(() => target.removeEventListener(type, fn, {capture: true})); };
  rec.hookDoc = doc => {
    if (!doc || rec.docs.has(doc)) return;
    rec.docs.add(doc);
    for (const k of ["start", "move", "end", "cancel"]) listen(doc, "touch" + k, touch(k));
    for (const k of ["down", "move", "up", "cancel"]) listen(doc, "pointer" + k, pointer(k));
  };
  for (const k of ["start", "move", "end", "cancel"]) listen(document, "touch" + k, touch(k));
  const onHash = () => rec.note("hashchange", {hash: location.hash, len: history.length});
  window.addEventListener("hashchange", onHash);
  rec.off.push(() => window.removeEventListener("hashchange", onHash));
  rec.stop = () => { for (const off of rec.off) off(); rec.off = []; if (swipeRec === rec) swipeRec = null; };
  rec.compact = cap => swipeCompact(rec.events, cap);
  swipeRec = rec;
  try { rec.hookDoc(mirrorDoc()); } catch (e) { /* no mirror document */ }
  return rec;
}

// NAS page letters seen while a step runs (the page the NAS browser is on, from /api/rb/meta).
function swipePoll(rbId) {
  const p = {seq: [], last: "", stop: false, changedAt: performance.now()};
  let firstDone;
  p.first = new Promise(resolve => { firstDone = resolve; });
  (async () => {
    while (!p.stop) {
      try {
        const res = await fetch("/api/rb/meta?tab=" + encodeURIComponent(rbId) + "&wait=0", {credentials: "same-origin", cache: "no-store"});
        if (res.ok) {
          const letter = swipeLetter((await res.json()).url);
          if (letter !== p.last) { p.last = letter; p.seq.push(letter); p.changedAt = performance.now(); gestureTrace("nas", {page: letter}); }
        }
      } catch (e) { /* the next sample */ }
      firstDone();
      await delay(100);
    }
  })();
  return p;
}

function swipeLabel(tab) {
  let text = "";
  try {
    const mode = tabDisplay(tab);
    if (mode === "mirror") { const d = mirrorDoc(); text = d && d.body ? (d.body.innerText || d.body.textContent || "") : ""; }
    else if (mode === "reader") text = ($("readerTitle").textContent || "") + " " + ($("readerBody").textContent || "");
    else text = (tab.rb && tab.rb.title) || "";
  } catch (e) { text = ""; }
  const m = /ページ\s*([ABC])/.exec(text);
  return m ? m[1] : "";
}

async function swipeWaitFor(test, ms) {
  const end = performance.now() + ms;
  for (;;) {
    let ok = false;
    try { ok = await test(); } catch (e) { ok = false; }
    if (ok) return true;
    if (performance.now() >= end || (swipeDiag && swipeDiag.aborted)) return false;
    await delay(100);
  }
}

function swipeAlive(d) {
  const snap = selftestUi.snap;
  return !d.aborted && Boolean(snap && snap.run_id === d.runId && snap.state === "running");
}

async function swipeNasLetter(d) {
  const meta = await api("/api/rb/meta?tab=" + encodeURIComponent(d.tab.rb.id) + "&wait=0");
  return swipeLetter(meta.url);
}

async function swipeEnsureRoute(d) {
  const tab = d.tab;
  if (!(state.route.view === "tab" && state.route.tabId === tab.id)) { navigate("#/tab/" + tab.id); await delay(300); }
  if (!(history.state && history.state.qdGuard === tab.id)) armBackGuard();
}

// The step's starting page: the Hub puts the NAS tab there, then the display must show it.
async function swipeEnsureStart(d, letter) {
  await swipeEnsureRoute(d);
  if ((await swipeNasLetter(d)) !== letter) {
    await api("/api/selftest/swipe/goto", {run_id: d.runId, page: letter});
    await swipeWaitFor(async () => (await swipeNasLetter(d)) === letter, 6000);
  }
  await swipeWaitFor(() => swipeLabel(d.tab) === letter, 8000);
  await delay(650);            // longer than the flick / popstate de-duplication window
}

async function swipeSetMode(d, mode) {
  const tab = d.tab;
  d.mode = mode;
  tab.diag.mode = mode;
  const rb = tab.rb;
  rb.tilesFor = ""; rb.readerFail = null; rb.mirrorFail = null;
  await swipeEnsureRoute(d);
  applyDisplay(tab);
  await swipeWaitFor(() => swipeLabel(tab) !== "", 10000);
  document.querySelectorAll("[data-swipe-mode]").forEach(b => b.classList.toggle("is-on", b.dataset.swipeMode === mode));
  await delay(400);
}

// ---- synthetic input -------------------------------------------------------------------------
function swipeEnv(d, kind) {
  const tab = d.tab, mode = tabDisplay(tab);
  if (kind === "toolbar") {
    const bar = document.querySelector(".toolbar"), r = bar.getBoundingClientRect();
    return {win: window, target: bar, handlers: pullHandlers, cx: window.innerWidth / 2, cy: r.top + r.height / 2, how: ""};
  }
  if (mode === "mirror") {
    const doc = mirrorDoc();
    if (!doc || !doc.body) return null;
    return {win: doc.defaultView, target: doc.body, handlers: flickHandlers.get(doc),
      cx: doc.documentElement.clientWidth / 2, cy: doc.documentElement.clientHeight / 2, how: ""};
  }
  const view = $("remoteView");
  const target = {reader: $("readerScroll"), tiles: $("tileScroll"), live: $("remoteStage")}[mode] || view;
  const r = target.getBoundingClientRect();
  return {win: window, target, handlers: flickHandlers.get(view), cx: r.left + r.width / 2, cy: r.top + r.height / 2, how: ""};
}

// A TouchEvent where the browser lets us build one; else the very same internal handler is called directly.
function swipeSend(env, type, x, y) {
  if (env.how !== "direct") {
    try {
      const w = env.win;
      const point = new w.Touch({identifier: 91, target: env.target, clientX: x, clientY: y, pageX: x, pageY: y, screenX: x, screenY: y});
      const list = type === "touchend" ? [] : [point];
      env.target.dispatchEvent(new w.TouchEvent(type, {bubbles: true, cancelable: true, composed: true,
        touches: list, targetTouches: list, changedTouches: [point], view: w}));
      env.how = "touch";
      return;
    } catch (e) {
      if (env.how === "touch") throw e;
      env.how = "direct";
    }
  }
  const h = env.handlers;
  const fn = h && (type === "touchstart" ? h.start : type === "touchmove" ? h.move : h.end);
  if (!fn) { env.how = "none"; return; }
  const point = {clientX: x, clientY: y};
  gestureTrace("touch." + type.slice(5), {x: Math.round(x), y: Math.round(y), direct: 1});
  fn({touches: type === "touchend" ? [] : [point], changedTouches: [point], target: env.target, isTrusted: false});
}

async function swipeDrag(env, x0, y0, x1, y1, ms, steps, begin = true) {
  if (begin) swipeSend(env, "touchstart", x0, y0);
  for (let i = 1; i <= steps; i++) {
    await delay(ms / steps);
    swipeSend(env, "touchmove", x0 + (x1 - x0) * i / steps, y0 + (y1 - y0) * i / steps);
  }
}

const SWIPE_AUTO_STEPS = [
  {id: "edge_back", start: "C", run: async () => { history.back(); return "history"; }},
  {id: "flick_right", start: "B", run: ctx => swipeFlick(ctx, {dx: 150, ms: 150})},
  {id: "back_first", start: "A", run: async () => { history.back(); return "history"; }},
  {id: "flick_left", start: "A", run: ctx => swipeFlick(ctx, {dx: -150, ms: 150})},
  {id: "slow_move", start: "B", run: ctx => swipeFlick(ctx, {dx: 150, ms: 1000, steps: 10})},
  {id: "edge_start", start: "B", run: ctx => swipeFlick(ctx, {dx: 150, ms: 150, x0: 8})},
  {id: "forward", start: "B", run: async () => { history.forward(); return "history"; }},
  {id: "pull_cancel", start: "B", run: ctx => swipePull(ctx, true)},
  {id: "pull_close", start: "B", run: ctx => swipePull(ctx, false)},
  // v0.1.55: a pull from the (unfocused) address bar, a sideways flick on a scrolling table, and a page back that
  // lands on an older app entry (the stack WebKit leaves after skipping entries made without a user gesture).
  {id: "pull_omnibox", start: "B", run: ctx => swipePull(ctx, true, "omnibox")},
  {id: "hscroll", start: "B", run: ctx => swipeFlick(ctx, {dx: -150, ms: 150, sel: "#wide"})},
  {id: "pop_stay", start: "B", run: swipePopStayRun},
];

// v0.1.55: the landing on an older app route (WebKit skipped our guard AND the tab's entry): the current entry is turned
// into another app route and the very popstate the browser would send is dispatched. (A real traversal is not used here:
// the joint session history also holds the mirror frame's entries, so entry counts are not reliable.) The app then
// pushes the tab back (history grows by one entry: d.histAdj) and steps the NAS page back.
async function swipePopStayRun(ctx) {
  const d = ctx.d, hash = "#/tab/" + d.tab.id;
  history.replaceState(null, "", d.prevHash === hash ? "#/home" : d.prevHash);
  d.histAdj = (d.histAdj || 0) + 1;
  window.dispatchEvent(new PopStateEvent("popstate", {state: null}));
  return "history";
}

async function swipeFlick(ctx, o) {
  let env = null;
  await swipeWaitFor(() => { env = swipeEnv(ctx.d, "view"); return Boolean(env && env.handlers); }, 4000);   // the mirror document may be swapping
  if (!env || !env.handlers) {
    let doc = null;
    try { doc = mirrorDoc(); } catch (e) { doc = null; }
    gestureTrace("input.none", {mode: tabDisplay(ctx.d.tab), doc: Boolean(doc), body: Boolean(doc && doc.body),
      wired: Boolean(doc && flickHandlers.get(doc)), wrap: !$("mirrorWrap").hidden, owner: mirrorHost.owner === ctx.d.tab.rb});
  }
  if (!env) return "none";
  let cx = env.cx;
  if (o.sel) {                      // start on this element (v0.1.55: a scrolling table); not there in this display: nothing to test
    // the page's copy may still be arriving: wait for the element (and the document holding it) a moment
    let el = null, asked = 0;
    await swipeWaitFor(() => {
      const e2 = swipeEnv(ctx.d, "view");
      if (e2 && e2.handlers) env = e2;
      const d0 = env.target.ownerDocument || env.target;       // the whole page, not the element under the centre
      el = d0.querySelector ? d0.querySelector(o.sel) : null;
      if (!el && tabDisplay(ctx.d.tab) === "mirror") {          // the event target may still be the app's element
        try { const md = mirrorDoc(); el = md ? md.querySelector(o.sel) : null; if (el) env.target = el; } catch (e) { el = null; }
      }
      // a first copy of a long page is partial (v0.1.54): the lower part may still be empty boxes; ask for the full one
      if (!el && tabDisplay(ctx.d.tab) === "mirror" && Date.now() - asked > 2000) {
        asked = Date.now();
        runMirror(ctx.d.tab, {force: true, immediate: true, quiet: true}).catch(() => {});
      }
      return Boolean(el);
    }, tabDisplay(ctx.d.tab) === "mirror" ? 4000 : 1500);    // longer waits pushed the whole check past its time limit
    if (!el) {
      let md = null;
      try { md = mirrorDoc(); } catch (e) { md = null; }
      gestureTrace("input.nosel", {sel: o.sel, mode: tabDisplay(ctx.d.tab), doc: Boolean(md),
        nodes: md ? md.getElementsByTagName("*").length : 0, partial: Boolean(md && md.querySelector(".qdm-ph")),
        url: md ? String(md.location.href).slice(-60) : ""});
      return "none";
    }
    const r = el.getBoundingClientRect();
    env.target = el;
    cx = r.left + r.width / 2;
    env.cy = r.top + r.height / 2;
  }
  const x0 = o.x0 !== undefined ? o.x0 : cx - o.dx / 2, y = env.cy;
  await swipeDrag(env, x0, y, x0 + o.dx, y, o.ms, o.steps || 6);
  swipeSend(env, "touchend", x0 + o.dx, y);
  return env.how;
}

async function swipePull(ctx, back, from) {
  const env = swipeEnv(ctx.d, "toolbar");
  if (from === "omnibox") {          // v0.1.55: the pull starts on the address bar (not focused)
    const box = $("omnibox");
    if (document.activeElement === box) box.blur();
    const r = box.getBoundingClientRect();
    env.target = box;
    env.cx = r.left + r.width / 2; env.cy = r.top + r.height / 2;
  }
  const x = env.cx, y = env.cy;
  await swipeDrag(env, x, y, x, y + 140, 150, 5);
  await delay(80);
  if (back) {
    await swipeDrag(env, x, y + 140, x, y + 20, 100, 3, false);       // the same finger goes back up
    await delay(60);
    ctx.facts.hint_hidden = $("gestureHint").hidden;
  }
  swipeSend(env, "touchend", x, back ? y + 20 : y + 140);
  return env.how;
}

// One step: starting page, recording, the action (automatic) or the person's answer (manual), settling, facts.
async function swipeRunStep(d, def, answerFn) {
  const tab = d.tab;
  await swipeEnsureStart(d, def.start);
  tab.diag.closeCalls = 0;
  const rec = makeSwipeRecorder();
  const poller = swipePoll(tab.rb.id);
  await poller.first;
  const t0 = performance.now();
  const ctx = {d, facts: {}};
  rec.note("step.begin", {id: def.id, mode: tabDisplay(tab)});
  let synthetic = "", answer = "";
  try {
    if (answerFn) answer = await answerFn(def);
    else synthetic = (await def.run(ctx)) || "";
    await delay(750);
    const end = performance.now() + 2500;
    while (performance.now() < end && performance.now() - poller.changedAt < 500) await delay(100);
    const want = poller.last;
    if (want === "A" || want === "B" || want === "C") await swipeWaitFor(() => swipeLabel(tab) === want, 2500);
  } finally {
    poller.stop = true;
    rec.stop();
  }
  const seq = poller.seq.join("").slice(0, 16);
  const facts = Object.assign({
    nas_before: poller.seq[0] || "?", nas_after: poller.last || "?", nas_seq: seq, label: swipeLabel(tab),
    route_left: rec.routeLeft || !(state.route.view === "tab" && state.route.tabId === tab.id),
    hint_shown: rec.hintShown, hint_hidden: $("gestureHint").hidden, close_calls: Math.min(100, tab.diag.closeCalls),
    synthetic: synthetic === "touch" || synthetic === "direct" || synthetic === "history" || synthetic === "none" ? synthetic : "",
    flick_why: rec.flickWhy, ms: Math.round(performance.now() - t0)}, ctx.facts);
  if (answer) {
    facts.answer = answer;
    facts.gestures = rec.gestures.slice(0, 12);      // v0.1.55: per-gesture results (manual judge)
    facts.gesture_n = rec.gestures.length;
  }
  return {id: def.id, facts, counts: rec.counts, raw: rec.events.slice(), display: tabDisplay(tab), synthetic: facts.synthetic};
}

function swipeBody(d, name, steps, growth, cap) {
  const last = steps[steps.length - 1];
  return {run_id: d.runId, mode: {name, display: last ? last.display : "", synthetic: swipeSyntheticOf(steps),
    history_growth: growth, steps: steps.map(s => ({id: s.id, facts: s.facts, counts: s.counts, events: swipeCompact(s.raw, cap)}))},
    last: false};
}

function swipeSyntheticOf(steps) {
  const kinds = steps.map(s => s.synthetic).filter(k => k === "touch" || k === "direct" || k === "none");
  return kinds.includes("none") ? "none" : kinds.includes("direct") ? "direct" : kinds.includes("touch") ? "touch" : "history";
}

async function swipePost(d, body) {
  for (const cap of [SWIPE_EVENT_CAP, 36, 20, 10]) {
    if (body.mode) for (const s of body.mode.steps) s.events = swipeCompact(s.events, cap);
    if (JSON.stringify(body).length <= SWIPE_PAYLOAD_MAX) break;
  }
  await api("/api/selftest/swipe", body);
}

// ---- tab, banner, clean-up --------------------------------------------------------------------
async function swipeBegin(snap, manual) {
  const prevHash = location.hash || "#/home";
  const tab = newTabRecord("t" + state.nextTabId++);
  const url = "https://" + SWIPE_HOST + "/swipe/c";
  tab.history = [url]; tab.index = 0; tab.current = url;
  tab.diag = {mode: null, closeCalls: 0, stubClose: true};
  tab.rb = newRbRecord(snap.tab, tab);
  state.tabs.push(tab);
  const d = {tab, runId: snap.run_id, prevHash, cleaning: false, aborted: false, manual, mode: ""};
  swipeDiag = d;
  history.replaceState(null, "", "#/tab/" + tab.id);       // the diagnostic tab takes the place of this screen's entry
  applyRoute();
  await delay(300);
  return d;
}

async function swipeEnd(d) {
  d.cleaning = true;
  if (swipeRec) swipeRec.stop();
  swipeRec = null;
  setShown("swipeBanner", false);
  const tab = d.tab, idx = state.tabs.indexOf(tab);
  tab.diag.stubClose = false;
  tab.token++;
  releaseRemote(tab);
  if (idx >= 0) state.tabs.splice(idx, 1);
  if (state.lastWebTabId === tab.id) state.lastWebTabId = null;
  gestureHint("");
  if (history.state && history.state.qdGuard === tab.id) {         // take the guard entry off the stack
    await new Promise(resolve => {
      const done = () => { window.removeEventListener("popstate", done); clearTimeout(timer); resolve(); };
      const timer = setTimeout(done, 800);
      window.addEventListener("popstate", done);
      history.back();
    });
  }
  history.replaceState(null, "", d.prevHash);
  d.cleaning = false;
  swipeDiag = null;
  applyRoute();
  renderWebTabs();
  updateChrome();
}

function swipeBanner(step, text) {
  $("swipeBannerStep").textContent = step;
  $("swipeBannerText").textContent = text;
  setShown("swipeBanner", true);
}

async function swipeFailPost(d, error) {
  try { await api("/api/selftest/swipe", {run_id: d.runId, last: true, error: String(error && error.message || error).slice(0, 150)}); } catch (e) { /* the Hub ends the wait */ }
}

// Automatic: every display mode, every step; one POST per mode and a final one.
async function swipeAuto(snap) {
  const d = await swipeBegin(snap, false);
  try {
    const started = performance.now();
    const device = selftestDevice();
    swipeBanner("スワイプの自動確認", "アプリのスワイプ処理へ合成した入力を流しています。画面を触らないでください。");
    for (const [i, name] of SWIPE_MODES.entries()) {
      if (!swipeAlive(d) || (i > 0 && performance.now() - started > SWIPE_AUTO_BUDGET_MS)) break;
      swipeBanner("スワイプの自動確認 " + (i + 1) + "/" + SWIPE_MODES.length, SWIPE_MODE_NAME[name] + "表示で確認しています。画面を触らないでください。");
      await swipeSetMode(d, name);
      const growth0 = history.length;
      d.histAdj = 0;
      const steps = [];
      for (const def of SWIPE_AUTO_STEPS) {
        if (!swipeAlive(d)) break;
        if (def.id === "pop_stay" && i !== SWIPE_MODES.length - 1) continue;   // v0.1.55: once, in the last display (it leaves one more history entry)
        steps.push(await swipeRunStep(d, def, null));
      }
      await swipePost(d, Object.assign(swipeBody(d, name, steps, history.length - growth0 - d.histAdj, SWIPE_EVENT_CAP), {device: i === 0 ? device : undefined}));
    }
    await api("/api/selftest/swipe", {run_id: d.runId, last: true});
  } catch (e) {
    await swipeFailPost(d, e);
  } finally {
    await swipeEnd(d).catch(() => {});
  }
}

const SWIPE_MANUAL_STEPS = [
  {id: "m_edge_back", start: "C", text: "画面の左端から右へスワイプ（iPhoneの戻る操作）", want: "期待: 1つ前のページ（ページB）になり、アプリは同じタブ画面のまま"},
  {id: "m_flick_right", start: "B", text: "ページの中央で右へすばやくフリック", want: "期待: 1つ前のページ（ページA）になる"},
  {id: "m_flick_left", start: "A", text: "ページの中央で左へすばやくフリック", want: "期待: 次のページ（ページB）になる"},
  {id: "m_table", start: "B", text: "表の部分を横にスクロール（ゆっくり）", want: "期待: 表が横に動くだけで、ページは戻らない・進まない"},
  {id: "m_edge_forward", start: "B", text: "画面の右端から左へスワイプ（iPhoneの進む操作、ある場合）", want: "期待: 進めるときだけ進む。何も起きなくても構いません"},
  {id: "m_pull", start: "B", text: "画面上部のアドレスバー（またはその下の細い表示行）に指を置いて、下へ引く（離さずに元へ戻す）", want: "期待: ヒントが出て、戻すと消える。タブは閉じない・キーボードは出ない"},
];

// Manual: the person's finger, the person's answer.
async function swipeManual(snap) {
  const d = await swipeBegin(snap, true);
  let pending = null;
  const wire = (id, value) => { $(id).onclick = () => { if (pending) { const r = pending; pending = null; r(value); } }; };
  wire("swipeOk", "ok"); wire("swipeBad", "bad"); wire("swipeSkip", "skip");
  $("swipeAbort").onclick = () => { d.aborted = true; if (pending) { const r = pending; pending = null; r("skip"); } api("/api/selftest/cancel", {}).catch(() => {}); };
  document.querySelectorAll("[data-swipe-mode]").forEach(b => {
    b.onclick = () => { if (!d.aborted) swipeSetMode(d, b.dataset.swipeMode).catch(() => {}); };
  });
  try {
    const device = selftestDevice();
    const first = SWIPE_MODES.includes(tabDisplay(d.tab)) ? tabDisplay(d.tab) : "mirror";
    swipeBanner("スワイプの確認（手動）", "準備しています…");
    $("swipeOk").disabled = $("swipeBad").disabled = $("swipeSkip").disabled = true;
    await swipeSetMode(d, first);
    const byMode = {};
    for (const [i, def] of SWIPE_MANUAL_STEPS.entries()) {
      if (!swipeAlive(d)) break;
      $("swipeOk").disabled = $("swipeBad").disabled = $("swipeSkip").disabled = true;
      swipeBanner("手順 " + (i + 1) + "/" + SWIPE_MANUAL_STEPS.length + "（準備中）", "ページを準備しています…");
      const result = await swipeRunStep(d, def, async () => {
        swipeBanner("手順 " + (i + 1) + "/" + SWIPE_MANUAL_STEPS.length, def.text + "　" + def.want);
        $("swipeOk").disabled = $("swipeBad").disabled = $("swipeSkip").disabled = false;
        return new Promise(resolve => { pending = resolve; });
      });
      (byMode[d.mode] = byMode[d.mode] || []).push(result);
    }
    let n = 0;
    for (const name of SWIPE_MODES) {
      if (byMode[name] && swipeAlive(d)) await swipePost(d, Object.assign(swipeBody(d, name, byMode[name], 0, SWIPE_EVENT_CAP), {device: n++ === 0 ? device : undefined}));
    }
    await api("/api/selftest/swipe", {run_id: d.runId, last: true});
  } catch (e) {
    await swipeFailPost(d, e);
  } finally {
    await swipeEnd(d).catch(() => {});
  }
}

// One button: the Hub measures the NAS, the NAS browser and built-in test pages (selftest.py), and while it
// waits this page measures ITS OWN side on the same internal fixture tab (reader / mirror / tiles / live)
// in an off-screen container that never touches the user's tabs or displays. Everything is RAM only; the
// Hub judges both halves and builds the report text.
const SELFTEST_URLS_KEY = "qdl.selftest.urls";
const SELFTEST_RUN_KEY = "qdl.selftest.run";
const SELFTEST_WAIT_S = 20;
const SELFTEST_LIVE_MS = 3000;
const SELFTEST_STATUS_LABEL = {pass: "合格", warn: "注意", fail: "失敗", skip: "省略"};
const SELFTEST_STATUS_ICON = {pass: "✓", warn: "!", fail: "×", skip: "–"};
const SELFTEST_SECTION_ICON = {pending: "・", running: "…", done: "✓", skipped: "–"};
const selftestUi = {runId: "", rev: -1, snap: null, watching: false, clientRun: "", shownId: "", text: "", cache: null, fetching: ""};

function selftestUrlLines() {
  return $("selftestUrls").value.split(/\r?\n/).map(line => line.trim()).filter(Boolean);
}

function selftestViewport() {
  const dpr = Math.min(3, Math.max(1, Number(window.devicePixelRatio) || 1));
  return {width: Math.min(1600, Math.max(320, Math.round(window.innerWidth))),
    height: Math.min(2400, Math.max(320, Math.round(window.innerHeight))), dpr: Math.round(dpr * 100) / 100};
}

function wireSelftest() {
  const saved = readLocal(SELFTEST_URLS_KEY);
  if (saved) $("selftestUrls").value = saved.slice(0, 2000);
  $("selftestUrls").addEventListener("input", () => writeLocal(SELFTEST_URLS_KEY, $("selftestUrls").value.slice(0, 2000)));
  $("selftestRun").addEventListener("click", () => perform(() => selftestStart()));
  $("selftestSwipe").addEventListener("click", () => perform(() => selftestStart({only: "swipe", client: false, urls: []})));
  $("selftestManual").addEventListener("click", () => perform(() => selftestStart({only: "swipe_manual", client: false, urls: []})));
  $("selftestCancel").addEventListener("click", () => perform(async () => {
    $("selftestCancel").disabled = true;
    await api("/api/selftest/cancel", {});
  }));
  $("selftestCopy").addEventListener("click", () => perform(selftestCopy));
  $("selftestIncludeUrls").addEventListener("change", () => { if (selftestUi.snap && selftestUi.snap.state !== "running") selftestPrefetch(); });
  // v0.1.73: real site addresses are hidden in the results by default (remembered on this device)
  $("selftestHideSites").checked = readLocal(SELFTEST_HIDE_KEY) !== "0";
  selftestHideSync();
  $("selftestHideSites").addEventListener("change", () => {
    writeLocal(SELFTEST_HIDE_KEY, $("selftestHideSites").checked ? "1" : "0");
    selftestHideSync();
    if (selftestUi.snap) renderSelftestResult(selftestUi.snap);
  });
  $("selftestSave").addEventListener("click", () => perform(selftestSave));
}

async function selftestStart(extra = {}) {
  const lines = selftestUrlLines();
  if (lines.length > 5) throw new Error("実サイトのURLは最大5件です");
  const urls = [];
  for (const [i, line] of lines.entries()) {      // same rules as the omnibox (the Hub checks again)
    try { urls.push(await validateUrl(line)); } catch (e) { throw new Error((i + 1) + "件目のURL: " + e.message); }
  }
  writeLocal(SELFTEST_URLS_KEY, $("selftestUrls").value.slice(0, 2000));
  $("selftestRun").disabled = true;
  try {
    const r = await api("/api/selftest/start", Object.assign({urls, viewport: selftestViewport(), client: true, swipe: true}, extra));
    selftestUi.runId = r.run_id;
    writeSession(SELFTEST_RUN_KEY, r.run_id);       // only the page that started a run measures its own side
    selftestUi.rev = -1;
    selftestUi.shownId = "";
    $("selftestText").hidden = true;
  } catch (e) {
    $("selftestRun").disabled = false;
    if (e.status === 409) { await selftestAttach(); return; }
    throw e;
  }
  selftestWatch().catch(() => {});
}

// Shows the newest run of the Hub (also after a reload) and follows it while it runs.
async function selftestAttach() {
  if (selftestUi.watching) return;
  const snap = await api("/api/selftest/status");
  selftestUi.runId = snap.run_id || "";
  selftestUi.rev = snap.rev;
  renderSelftest(snap);
  if (snap.state === "running") selftestWatch().catch(() => {});
}

async function selftestWatch() {
  if (selftestUi.watching) return;
  selftestUi.watching = true;
  let fails = 0;
  try {
    for (;;) {
      let snap;
      try {
        const since = selftestUi.rev >= 0 ? "&since=" + selftestUi.rev : "";
        snap = await api("/api/selftest/status?wait=" + SELFTEST_WAIT_S + since);
        fails = 0;
      } catch (e) {
        if (++fails >= 5) { $("selftestState").textContent = "進み具合を取得できません: " + e.message; return; }
        await delay(2000);
        continue;
      }
      selftestUi.rev = snap.rev;
      renderSelftest(snap);
      if (snap.state === "running" && snap.phase === "client" && snap.tab && selftestUi.clientRun !== snap.run_id &&
          readSession(SELFTEST_RUN_KEY) === snap.run_id) {
        selftestUi.clientRun = snap.run_id;
        selftestClient(snap).catch(() => {});
      }
      if (snap.state === "running" && snap.phase === "swipe" && snap.tab && selftestUi.swipeRun !== snap.run_id &&
          readSession(SELFTEST_RUN_KEY) === snap.run_id) {
        selftestUi.swipeRun = snap.run_id;
        (snap.swipe_kind === "manual" ? swipeManual : swipeAuto)(snap).catch(() => {});
      }
      if (snap.state !== "running") return;
    }
  } finally {
    selftestUi.watching = false;
  }
}

const SELFTEST_HIDE_KEY = "qd.selftestHideSites";

function selftestHide() { return $("selftestHideSites").checked; }

// the URL switch has no meaning while the addresses are hidden
function selftestHideSync() {
  const hide = selftestHide();
  $("selftestIncludeUrls").disabled = hide;
  if (hide) $("selftestIncludeUrls").checked = false;
}

// v0.1.73: the same rewriting as the Hub's report (selftest.site_masker) for what this screen shows
function selftestMasker(hosts) {
  if (!selftestHide()) return text => text;
  const labels = (hosts || []).map((h, i) => [String(h || "").toLowerCase().replace(/^www\./, ""), "サイト" + (i + 1)])
    .filter(p => p[0]).sort((a, b) => b[0].length - a[0].length);
  const esc = t => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const bare = labels.map(([h, label]) => [new RegExp("(^|[^\\w.-])(?:[\\w-]+\\.)*" + esc(h) + "(?![\\w-])", "gi"), label]);
  const labelOf = host => {
    for (const [h, label] of labels) if (host === h || host.endsWith("." + h)) return label;
    return null;
  };
  return text => {
    if (typeof text !== "string" || !text) return text;
    let out = text.replace(/(?:https?|wss?):\/\/[^\s"'<>|]+/gi, url => {
      let host = "";
      try { host = new URL(url).hostname.toLowerCase(); } catch (e) { host = ""; }
      if (/^(?:localhost|[0-9.]+|\[[0-9a-f:.]+\])$/i.test(host)) return url;
      return labelOf(host) || "（URL非表示）";
    });
    for (const [re, label] of bare) out = out.replace(re, (m, lead) => lead + label);
    return out;
  };
}

function selftestSecondsText(ms) { return Math.round((ms || 0) / 100) / 10 + "秒"; }

function renderSelftest(snap) {
  selftestUi.snap = snap;
  const running = snap.state === "running";
  $("selftestRun").disabled = running;
  for (const id of ["selftestSwipe", "selftestManual"]) $(id).disabled = running;
  setShown("selftestCancel", running);
  $("selftestCancel").disabled = false;
  const state = $("selftestState");
  if (!snap.run_id) state.textContent = "まだ実行していません";
  else if (running) state.textContent = (snap.current || "実行中") + "（" + selftestSecondsText(snap.elapsed_ms) + "）";
  else state.textContent = {done: "完了しました", cancelled: "中止しました", timeout: "6分の上限で終了しました",
    error: "途中でエラーになりました"}[snap.state] + "（" + selftestSecondsText(snap.elapsed_ms) + "）";
  const progress = $("selftestProgress");
  progress.hidden = !snap.run_id;
  progress.replaceChildren(...(snap.sections || []).map(sec => {
    const li = element("li", undefined, "selftest-step is-" + sec.status);
    li.appendChild(element("span", SELFTEST_SECTION_ICON[sec.status] || "・", "selftest-mark"));
    li.appendChild(element("span", sec.label + (sec.n ? "（" + sec.n + "件）" : ""), "selftest-step-label"));
    return li;
  }));
  if (selftestUi.shownId && selftestUi.shownId !== snap.run_id && running) selftestUi.shownId = "";
  renderSelftestResult(snap);
  renderSelftestHistory(snap.history || []);
}

function renderSelftestResult(snap) {
  const has = Boolean(snap.run_id) && (snap.checks || []).length > 0;
  setShown("selftestResult", has);
  if (!has) return;
  const sum = snap.summary || {};
  const running = snap.state === "running";
  $("selftestSummary").textContent = "合格 " + (sum.pass || 0) + " / 注意 " + (sum.warn || 0) + " / 失敗 " + (sum.fail || 0) +
    (sum.skip ? " / 省略 " + sum.skip : "") + "　所要 " + selftestSecondsText(snap.elapsed_ms) + (running ? "（実行中）" : "");
  $("selftestSummary").className = "selftest-summary " + (sum.fail ? "has-fail" : sum.warn ? "has-warn" : "is-ok");
  setShown("selftestCopy", !running);
  setShown("selftestSave", !running);
  if (!running) selftestPrefetch();
  const wrap = $("selftestTables");
  const nodes = [];
  const mask = selftestMasker(snap.sites);
  for (const sec of snap.sections || []) {
    const rows = snap.checks.filter(c => c.section === sec.id);
    if (!rows.length) continue;
    nodes.push(element("h3", sec.label, "selftest-h"));
    const table = element("table", undefined, "data-table selftest-table");
    const body = element("tbody");
    for (const c of rows) {
      const tr = element("tr", undefined, "selftest-row is-" + c.status);
      const mark = element("td", SELFTEST_STATUS_ICON[c.status] || "?", "selftest-st");
      mark.setAttribute("aria-label", SELFTEST_STATUS_LABEL[c.status] || c.status);
      tr.appendChild(mark);
      const cell = element("td", undefined, "selftest-cell");
      cell.appendChild(element("div", mask(c.name), "selftest-name"));
      if (c.value) cell.appendChild(element("div", mask(c.value), "selftest-val"));
      if (c.threshold) cell.appendChild(element("div", "基準: " + mask(c.threshold), "selftest-th"));
      if (c.hint) cell.appendChild(element("div", "改善のヒント: " + mask(c.hint), "selftest-hint"));
      tr.appendChild(cell);
      body.appendChild(tr);
    }
    table.appendChild(body);
    const holder = element("div", undefined, "table-wrap");
    holder.appendChild(table);
    nodes.push(holder);
  }
  wrap.replaceChildren(...nodes);
}

function renderSelftestHistory(items) {
  setShown("selftestHistory", items.length > 0);
  $("selftestHistoryList").replaceChildren(...items.map(item => {
    let when = "";
    try { when = new Date(item.started * 1000).toLocaleString("ja-JP", {month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit"}); } catch (e) { when = ""; }
    const button = element("button", when + " 合格" + item.pass + " 注意" + item.warn + " 失敗" + item.fail, "btn small");
    button.type = "button";
    if (item.id === (selftestUi.snap && selftestUi.snap.run_id)) button.classList.add("primary");
    button.addEventListener("click", () => perform(async () => {
      const full = await api("/api/selftest/results?id=" + encodeURIComponent(item.id));
      selftestUi.runId = full.run_id;
      selftestUi.rev = full.rev;
      full.history = items;
      renderSelftest(full);
    }));
    return button;
  }));
}

async function selftestReport() {
  const id = selftestUi.snap && selftestUi.snap.run_id;
  if (!id) throw new Error("結果がありません");
  const hide = selftestHide();
  const urls = !hide && $("selftestIncludeUrls").checked ? "1" : "0";
  return api("/api/selftest/report?id=" + encodeURIComponent(id) + "&urls=" + urls + "&hide=" + (hide ? "1" : "0"));
}

// Copies the report. The text is prefetched when the run ends (selftestPrefetch), so the tap copies it at once,
// inside the user gesture: navigator.clipboard when available, else a hidden textarea + execCommand("copy")
// (plain http in Safari). Only when both fail the text is shown selected for a long press.
async function selftestCopy() {
  const key = selftestCacheKey();
  const cached = selftestUi.cache && selftestUi.cache.key === key ? selftestUi.cache.text : null;
  let text = cached;
  let copied = false;
  if (text !== null) copied = await copyToClipboard(text);
  else {
    text = (await selftestReport()).text;
    copied = await copyToClipboard(text);
  }
  selftestUi.text = text;
  const area = $("selftestText");
  area.value = text;
  if (copied) {
    area.hidden = true;
    flash("結果をコピーしました。開発者へ貼り付けてください");
    return;
  }
  area.hidden = false;
  area.focus();
  area.select();
  try { area.setSelectionRange(0, area.value.length); } catch (e) { /* older browser */ }
  flash("下のテキストを長押しして「コピー」してください");
}

function selftestCacheKey() {
  const id = selftestUi.snap && selftestUi.snap.run_id;
  return id ? id + "|" + ($("selftestIncludeUrls").checked ? "1" : "0") + "|" + (selftestHide() ? "h" : "") : "";
}

async function selftestPrefetch() {
  const key = selftestCacheKey();
  if (!key || (selftestUi.cache && selftestUi.cache.key === key) || selftestUi.fetching === key) return;
  selftestUi.fetching = key;
  try {
    const report = await selftestReport();
    if (selftestCacheKey() === key) selftestUi.cache = {key, text: report.text};
  } catch (e) { /* the tap fetches it then */ }
  if (selftestUi.fetching === key) selftestUi.fetching = "";
}

// Client-side file (the Hub writes nothing): the readable report followed by the same data as JSON.
async function selftestSave() {
  const report = await selftestReport();
  const body = report.text + "\n\n=== JSON ===\n" + JSON.stringify(report.data, null, 1) + "\n";
  const url = URL.createObjectURL(new Blob([body], {type: "text/plain;charset=utf-8"}));
  const link = document.createElement("a");
  link.href = url;
  link.download = "qnapdl-selftest-" + new Date().toISOString().replace(/[:.]/g, "-") + ".txt";
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function selftestExecCopySupported() {
  try { return typeof document.queryCommandSupported === "function" && document.queryCommandSupported("copy") === true; } catch (e) { return false; }
}

function selftestDevice() {
  const flags = {
    pointer_events: typeof window.PointerEvent === "function",
    intersection_observer: typeof window.IntersectionObserver === "function",
    stream_body: typeof Response === "function" && "body" in Response.prototype,
    abort_controller: typeof window.AbortController === "function",
    img_decode: typeof HTMLImageElement === "function" && "decode" in HTMLImageElement.prototype,
    clipboard_api: Boolean(navigator.clipboard && navigator.clipboard.writeText),
    exec_copy: selftestExecCopySupported(),
    sse: sseSupported(),
    websocket: wsSupported(),
    live_pref: String(liveLoadPref().t || "-").slice(0, 8),
    secure_context: window.isSecureContext === true,
    https: location.protocol === "https:",
    stream_failed_before: Boolean(state.tabs.some(t => t.rb && t.rb.streamFailed)),
    rb_default_mode: String((state.settings && state.settings.rb_default_mode) || "?").slice(0, 20),
    rb_quality: String(state.rbQuality || "?").slice(0, 20),
    rb_resident: Boolean(state.settings && state.settings.rb_resident === true),
  };
  writeLocal("qdl.selftest.probe", "1");
  const storage = readLocal("qdl.selftest.probe") === "1";      // the wrappers swallow a blocked storage
  removeLocal("qdl.selftest.probe");
  let standalone = false;
  try { standalone = navigator.standalone === true || window.matchMedia("(display-mode: standalone)").matches; } catch (e) { standalone = false; }
  const view = selftestViewport();
  return {ua: String(navigator.userAgent || "").slice(0, 300), lang: String(navigator.language || "").slice(0, 20),
    platform: String(navigator.platform || "").slice(0, 60), width: view.width, height: view.height, dpr: view.dpr,
    touch: Number(navigator.maxTouchPoints) || 0, standalone, storage, clipboard: flags.clipboard_api || flags.exec_copy, flags};
}

const selftestMedian = values => {
  const sorted = values.slice().sort((a, b) => a - b);
  return sorted.length ? sorted[Math.floor(sorted.length / 2)] : null;
};

function selftestWaitFor(test, timeoutMs, stepMs = 50) {
  return new Promise(resolve => {
    const end = performance.now() + timeoutMs;
    const tick = () => {
      let ok = false;
      try { ok = test(); } catch (e) { ok = false; }
      if (ok) resolve(true);
      else if (performance.now() >= end) resolve(false);
      else setTimeout(tick, stepMs);
    };
    tick();
  });
}

// The phone's half. Always posts something (even after an error) and removes everything it created.
async function selftestClient(snap) {
  const runId = snap.run_id, tab = snap.tab;
  const view = snap.viewport || selftestViewport();
  const checks = [];
  const note = text => String(text || "").slice(0, 160);
  const step = async (id, fn) => {
    const now = selftestUi.snap;
    if (now && now.run_id === runId && now.state !== "running") return;     // cancelled / finished meanwhile
    $("selftestState").textContent = "この端末で測っています: " + (CLIENT_STEP_LABEL[id] || id);
    try { checks.push(Object.assign({id}, await fn())); }
    catch (e) { checks.push({id, ok: false, note: note("エラー: " + (e && e.message ? e.message : e))}); }
  };
  const holder = document.createElement("div");
  holder.setAttribute("aria-hidden", "true");
  holder.style.cssText = "position:fixed;left:-10000px;top:0;width:" + view.width + "px;height:" + view.height +
    "px;opacity:0;pointer-events:none;overflow:hidden";
  document.body.appendChild(holder);
  const created = {urls: [], frame: null};
  const device = selftestDevice();
  try {
    checks.push({id: "client.storage", ok: device.storage});
    checks.push({id: "client.clipboard", ok: device.clipboard, flags: {api: device.flags.clipboard_api}});
    checks.push({id: "client.secure", ok: device.flags.secure_context});
    await step("client.rtt", async () => {
      const times = [];
      for (let i = 0; i < 5; i++) {
        const t0 = performance.now();
        const res = await fetch("/api/health", {cache: "no-store", credentials: "same-origin"});
        await res.text();
        times.push(performance.now() - t0);
      }
      return {ms: Math.round(selftestMedian(times) * 10) / 10};
    });
    try { await api("/api/rb/viewport", {tab, width: view.width, height: view.height, dpr: view.dpr}); } catch (e) { /* the Hub set it already */ }
    await step("client.reader", () => selftestReaderStep(tab, holder));
    await step("client.mirror_load", async () => {
      const out = await selftestMirrorLoad(tab, holder, created);
      return out;
    });
    await step("client.mirror_events", () => selftestMirrorEvents(created));
    await step("client.mirror_morph", () => selftestMirrorMorph(tab, created));
    await step("client.mirror_reuse", () => selftestMirrorReuse(tab, created));
    await step("client.tiles", () => selftestTilesStep(tab, created));
    await step("client.live", () => selftestLiveStep(tab, holder));
    // v0.1.59: 「操作の応答」 - how the app itself responds on this phone (about 40 s in all)
    await step("client.open_rtt", () => selftestOpenRttStep(tab, view));
    await step("client.route", () => selftestRouteStep());
    await step("client.toggle", () => selftestToggleStep());
    await step("client.idle", () => selftestIdleStep());
  } catch (e) {
    checks.push({id: "client.error", ok: false, note: note(e && e.message ? e.message : e)});
  } finally {
    for (const url of created.urls) URL.revokeObjectURL(url);
    holder.remove();
    for (const path of ["/api/rb/mirror/release", "/api/rb/snapshot/release"]) {
      api(path, {tab}).catch(() => {});
    }
    try {
      await api("/api/selftest/client", {run_id: runId, device, checks});
    } catch (e) {
      $("selftestState").textContent = "端末側の結果を送れませんでした: " + (e && e.message ? e.message : e);
    }
  }
}

const CLIENT_STEP_LABEL = {"client.route": "画面の切り替え", "client.toggle": "スイッチの反応", "client.idle": "待機中の通信（30秒）",
  "client.open_rtt": "ページを開く通信回数", "client.rtt": "Hubとの通信", "client.reader": "リーダー表示", "client.mirror_load": "ミラーの読み込み",
  "client.mirror_events": "ミラー内のタップ", "client.mirror_morph": "ミラーの差し替え", "client.mirror_reuse": "ミラーの画像の再利用", "client.tiles": "高速表示",
  "client.live": "ライブ表示（WebSocket・SSE・MJPEG・ポーリング）"};

// ---- v0.1.59: 「操作の応答」 ----
const nextFrame2 = async () => { await nextPaint(); await nextPaint(); };

// Hash change -> the target view is on screen, for the main screens; the selftest screen is shown again afterwards.
async function selftestRouteStep() {
  const home = location.hash || "#/home";
  const targets = [["#/downloads", "downloads", "ダウンロード"], ["#/links", "links", "リンク"], ["#/home", "home", "ブラウズ"],
    ["#/settings/storage", "settings", "設定"]];
  const times = [], notes = [];
  try {
    for (const [hash, view, label] of targets) {
      if (location.hash === hash) { location.hash = "#/links"; await nextFrame2(); }
      const t0 = performance.now();
      location.hash = hash;
      const shown = await selftestWaitFor(() => state.route.view === view && !$("view-" + view).hidden, 4000, 8);
      await nextPaint();
      const ms = Math.round(performance.now() - t0);
      if (!shown) throw new Error(label + "の画面に切り替わりませんでした");
      times.push(ms);
      notes.push(label + " " + ms + "ms");
    }
  } finally {
    location.hash = home;
    await selftestWaitFor(() => location.hash === home, 2000, 20);
    await nextFrame2();
  }
  return {ok: true, ms: Math.max(...times), n: times.length, note: notes.join(" / ")};
}

// A tap on a settings switch (rb_link_boxes, put back afterwards): when does the change show, when does the Hub confirm?
async function selftestToggleStep() {
  const input = $("rbLinkBoxes");
  if (!input || !state.settings) throw new Error("スイッチを見つけられません");
  const key = "rb_link_boxes", initial = state.settings[key] === true;
  const t0 = performance.now();
  input.click();                                        // like a tap: the native change, then the app's handler
  const flipped = () => (state.settings[key] === true) !== initial && input.checked !== initial;
  const seen = await selftestWaitFor(flipped, 1500, 4);
  await nextPaint();
  const feedback = Math.round(performance.now() - t0);
  const busy = input.getAttribute("aria-busy") === "true";
  await selftestWaitFor(() => !input.hasAttribute("aria-busy"), 8000, 20);
  const confirm = Math.round(performance.now() - t0);
  const settled = (state.settings[key] === true) !== initial;
  try {
    input.click();                                       // back to how it was
    await selftestWaitFor(() => (state.settings[key] === true) === initial && !input.hasAttribute("aria-busy"), 8000, 20);
  } catch (e) { /* the final check below reports it */ }
  const restored = (state.settings[key] === true) === initial;
  return {ok: seen && settled && restored, ms: feedback, n: confirm,
    note: (busy ? "" : "保存中の印なし ") + (restored ? "" : "元に戻せませんでした")};
}

// The app idle on the Downloads screen for 30 s: how many /api/state answers and requests in all?
async function selftestIdleStep() {
  const home = location.hash || "#/home";
  location.hash = "#/downloads";
  const shown = await selftestWaitFor(() => state.route.view === "downloads", 3000, 20);
  await delay(2000);                                     // answers caused by the screen change itself
  const s0 = reqStats.state, a0 = reqStats.all, t0 = performance.now();
  while (performance.now() - t0 < 30000) {
    await delay(500);
    const now = selftestUi.snap;
    if (!now || now.state !== "running") break;          // cancelled meanwhile
  }
  const out = {ok: shown, ms: Math.round(performance.now() - t0), state: reqStats.state - s0, total: reqStats.all - a0};
  location.hash = home;
  await selftestWaitFor(() => location.hash === home, 2000, 20);
  return out;
}

// The virtual page of the self-test opened the way the app opens a page in the mirror display: requests until
// its copy is on this phone (navigate with viewport + first copy, then the copy document).
async function selftestOpenRttStep(tab, view) {
  const r0 = rttCount, t0 = performance.now();
  const reply = await api("/api/rb/navigate", {tab, url: "https://selftest.example.com/article",
    view: {width: view.width, height: view.height, dpr: view.dpr, quality: state.rbQuality}, first: "mirror"});
  const copy = reply.first && reply.first.mirror;
  if (!copy || typeof copy.doc !== "string" || !MIRROR_DOC_RE.test(copy.doc)) {
    throw new Error("最初のコピーが応答に含まれていません");
  }
  const res = await fetch(copy.doc, {cache: "no-store", credentials: "same-origin"});
  const text = await res.text();
  const ok = res.ok && text.length > 200;
  return {ok, ms: Math.round(performance.now() - t0), n: rttCount - r0};
}

async function selftestReaderStep(tab, holder) {
  const t0 = performance.now();
  const raw = await api("/api/rb/reader", {tab});
  const data = cleanReaderData(raw);
  if (!data || !data.blocks.length) return {ok: false, ms: Math.round(performance.now() - t0), n: 0};
  const box = element("div", undefined, "reader-body");
  holder.appendChild(box);
  const frag = document.createDocumentFragment();
  for (const b of data.blocks) {
    if (b.t === "h") frag.appendChild(element("h" + Math.min(6, Math.max(1, b.level)), b.text));
    else if (b.t === "p" || b.t === "li") {
      const p = element(b.t === "li" ? "li" : "p");
      for (const run of b.runs) p.appendChild(element(run.href ? "a" : "span", run.text));
      frag.appendChild(p);
    } else if (b.t === "pre") frag.appendChild(element("pre", b.text));
    else if (b.t === "img") frag.appendChild(element("div", "[画像" + b.i + "]", "reader-fig"));
    else frag.appendChild(element("hr"));
  }
  box.appendChild(frag);
  void box.offsetHeight;                      // layout once, like the real display
  const ms = Math.round(performance.now() - t0);
  const images = data.blocks.filter(b => b.t === "img");
  let imageNote = "";
  if (images.length) {                         // one real image through the Hub (decode on this device)
    const i0 = performance.now();
    const res = await fetch("/api/rb/reader/img?tab=" + encodeURIComponent(tab) + "&i=" + images[0].i,
      {cache: "no-store", credentials: "same-origin"});
    if (res.ok) {
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const img = new Image();
      img.src = url;
      try { await img.decode(); imageNote = "画像1枚 " + Math.round(performance.now() - i0) + " ms"; } catch (e) { imageNote = "画像のデコードに失敗"; }
      URL.revokeObjectURL(url);
    } else imageNote = "画像の取得に失敗 HTTP " + res.status;
  }
  return {ok: true, ms, n: data.blocks.length, note: imageNote};
}

async function selftestMirrorLoad(tab, holder, created) {
  const t0 = performance.now();
  const data = await api("/api/rb/mirror", {tab, full: false, force: true});
  if (!data || typeof data.doc !== "string" || !MIRROR_DOC_RE.test(data.doc)) throw new Error("ミラーのデータが不正です");
  const frame = $("mirrorFrame").cloneNode(false);      // the same element as the real display, without its id
  frame.removeAttribute("id");
  frame.removeAttribute("src");
  frame.title = "セルフテスト用ミラー（画面外）";
  frame.style.cssText = "position:absolute;left:0;top:0;width:100%;height:100%";
  holder.appendChild(frame);
  created.frame = frame;
  frame.contentWindow.location.replace(data.doc);        // validated above, same-origin Hub document
  const ready = () => {
    try { const d = frame.contentDocument; return Boolean(d && d.body && d.getElementById("mirrorCss")); } catch (e) { return false; }
  };
  const ok = await selftestWaitFor(ready, MIRROR_LOAD_TIMEOUT_MS);
  const doc = ok ? frame.contentDocument : null;
  return {ok, ms: Math.round(performance.now() - t0), n: data.stats ? data.stats.nodes : 0,
    note: ok ? "" : "ミラーのページを読み込めませんでした"};
}

// Dispatches pointer / click events inside the mirror document and checks the app's listeners saw them.
async function selftestMirrorEvents(created) {
  const frame = created.frame;
  const doc = frame && frame.contentDocument;
  if (!doc || !doc.body) throw new Error("ミラーのページがありません");
  const seen = [];
  mirrorProbes.set(doc, {tap: event => seen.push(event.type)});
  wireMirrorDoc(doc);
  const el = doc.querySelector("[data-m]");
  if (!el) throw new Error("ミラー内に操作できる要素がありません");
  const win = doc.defaultView;
  const box = el.getBoundingClientRect();
  const x = Math.round(box.left + Math.min(box.width, 40) / 2), y = Math.round(box.top + Math.min(box.height, 20) / 2);
  const fire = (type, Ctor, extra) => el.dispatchEvent(new Ctor(type, Object.assign(
    {bubbles: true, cancelable: true, view: win, clientX: x, clientY: y}, extra)));
  const Pointer = typeof win.PointerEvent === "function" ? win.PointerEvent : win.MouseEvent;
  const pointer = {pointerId: 7, pointerType: "touch", isPrimary: true, button: 0};
  fire("pointerdown", Pointer, pointer);
  fire("pointerup", Pointer, pointer);
  const pointerOk = seen.filter(t => t === "pointerup").length >= 1;
  await delay(900);                               // the app ignores a click that follows its own tap closely
  const before = seen.length;
  fire("click", win.MouseEvent, {button: 0});
  const clickOk = seen.length > before && seen[seen.length - 1] === "click";
  return {ok: pointerOk && clickOk, flags: {pointer: pointerOk, click: clickOk},
    note: Pointer === win.PointerEvent ? "" : "PointerEventなし（MouseEventで代用）"};
}

async function selftestMirrorMorph(tab, created) {
  const doc = created.frame && created.frame.contentDocument;
  if (!doc || !doc.body) throw new Error("ミラーのページがありません");
  const data = await api("/api/rb/mirror", {tab, full: true, force: true});
  if (mirrorUnsafe(data)) throw new Error("安全でないミラーのデータを受け取りました");
  const t0 = performance.now();
  mirrorMorph(doc, data, null);
  const ms = Math.round((performance.now() - t0) * 10) / 10;
  return {ok: doc.body.childNodes.length > 0, ms};
}

// v0.1.73: two more copies of the same page must keep every image (the same element, never asked again)
async function selftestMirrorReuse(tab, created) {
  const doc = created.frame && created.frame.contentDocument;
  if (!doc || !doc.body) throw new Error("ミラーのページがありません");
  await selftestWaitFor(() => Array.from(doc.images).every(img => img.complete), 4000);
  const before = Array.from(doc.images);
  const res0 = mirrorResEntries(doc);
  let kept = 0, added = 0, ms = 0;
  for (let i = 0; i < 2; i++) {
    await delay(800);                         // the Hub's resource prefetch of the first copy is done meanwhile
    const data = await api("/api/rb/mirror", {tab, full: true, force: true});
    if (mirrorUnsafe(data)) throw new Error("安全でないミラーのデータを受け取りました");
    const t0 = performance.now();
    const st = mirrorMorph(doc, data, null);
    ms = Math.max(ms, performance.now() - t0);
    kept += st.kept; added += st.added;
  }
  await delay(600);
  const after = new Set(Array.from(doc.images));
  const same = before.filter(img => after.has(img)).length;
  const res = mirrorResEntries(doc);
  const refetch = res.refetch - res0.refetch;
  return {ok: before.length > 0 && same === before.length && refetch === 0, ms: Math.round(ms * 10) / 10,
    n: refetch, imgs: before.length, same, kept, added, res: res.total,
    note: before.length ? "" : "画像がありません"};
}

async function selftestTilesStep(tab, created) {
  const t0 = performance.now();
  const meta = await api("/api/rb/snapshot", {tab, force: true});
  const res = await fetch("/api/rb/tile.jpg?tab=" + encodeURIComponent(tab) + "&snap=" + encodeURIComponent(meta.snap_id) + "&i=0",
    {cache: "no-store", credentials: "same-origin"});
  if (!res.ok) throw new Error("タイルを取得できません HTTP " + res.status);
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  created.urls.push(url);
  const img = new Image();
  img.src = url;
  await img.decode();
  return {ok: blob.type === "image/jpeg" && img.naturalWidth > 0, ms: Math.round(performance.now() - t0), n: meta.tiles,
    kb: Math.round(blob.size / 1024)};
}

// Live display the way the app does it, in the app's order, each transport measured on its own:
//   1. v0.1.68 WebSocket: stream.ws read for SELFTEST_LIVE_MS (first frame within RB_WS_FIRST_MS; frames per second);
//   2. SSE: stream.sse read for SELFTEST_LIVE_MS (frames per second);
//   3. MJPEG: an <img> on stream.mjpg must show a first frame within RB_MJPEG_FIRST_MS (then its frames are
//      counted for SELFTEST_LIVE_MS with a fetch reader, else the Hub's own fps figure is used);
//   4. polling: frame.jpg requested exactly like remoteLoop (since = last seq, no rev) for SELFTEST_LIVE_MS.
// The page under test is the animating one (/anim), so the figures are frames per second of a page that changes.
// The app ends on the first of them that works; the Hub judges (client_evaluate) from the flags.
async function selftestLiveMjpeg(tab, holder, out) {
  const img = new Image();
  img.style.cssText = "position:absolute;left:0;top:0;width:100%;height:auto";
  holder.appendChild(img);
  const t0 = performance.now();
  img.src = streamUrl({id: tab});
  const first = await selftestWaitFor(() => img.naturalWidth > 0 && img.currentSrc.indexOf("/api/rb/stream.mjpg") >= 0, RB_MJPEG_FIRST_MS, 100);
  out.flags.mjpeg = first;
  out.firstMs = Math.round(performance.now() - t0);
  if (!first) { img.removeAttribute("src"); img.remove(); return; }
  let frames = 0, bytes = 0, elapsed = SELFTEST_LIVE_MS;
  if (sseFetchOk()) {
    const ctl = new AbortController();
    const marker = Array.from("--qdlframe", c => c.charCodeAt(0));
    let tail = new Uint8Array(0);
    const started = performance.now();
    const stopTimer = setTimeout(() => ctl.abort(), SELFTEST_LIVE_MS);
    try {
      const res = await fetch(streamUrl({id: tab}), {signal: ctl.signal, cache: "no-store", credentials: "same-origin"});
      if (!res.ok || !res.body) throw new Error("HTTP " + res.status);
      const reader = res.body.getReader();
      for (;;) {
        const {done, value} = await reader.read();
        if (done) break;
        bytes += value.length;
        const joined = new Uint8Array(tail.length + value.length);
        joined.set(tail, 0);
        joined.set(value, tail.length);
        for (let i = 0; i + marker.length <= joined.length; i++) {
          if (joined[i] !== marker[0]) continue;
          let k = 1;
          while (k < marker.length && joined[i + k] === marker[k]) k++;
          if (k === marker.length) { frames++; i += marker.length - 1; }
        }
        tail = joined.slice(Math.max(0, joined.length - (marker.length - 1)));
      }
    } catch (e) { /* abort at the end of the window, or the reader cannot read it (WebKit "Load failed") */ }
    finally { clearTimeout(stopTimer); ctl.abort(); }
    elapsed = performance.now() - started;
  } else {
    await delay(SELFTEST_LIVE_MS);
  }
  if (frames === 0) {
    // The <img> showed it but fetch cannot read it: the Hub's own figure for the stream that is still open.
    try {
      const meta = await api("/api/rb/meta?tab=" + encodeURIComponent(tab) + "&wait=0");
      if (Number.isFinite(meta.fps) && meta.fps > 0) { out.fps_mjpeg = Math.round(meta.fps * 10) / 10; out.kb = Math.round((meta.frame_bytes || 0) / 1024); }
    } catch (e) { /* no figure */ }
  } else {
    out.fps_mjpeg = Math.round(frames / Math.max(elapsed / 1000, 0.1) * 10) / 10;
    out.kb = Math.round(bytes / frames / 1024);
  }
  img.removeAttribute("src");
  img.remove();
  if (!Number.isFinite(out.fps_mjpeg)) out.fps_mjpeg = 0;
}

async function selftestLiveWs(tab, out) {
  let frames = 0, bytes = 0, firstAt = 0;
  const started = performance.now();
  let conn = null;
  try {
    conn = wsConnect(wsUrl({id: tab}), buffer => {
      if (!frames) firstAt = performance.now();
      frames++;
      bytes += buffer.byteLength;
    });
    conn.done.catch(() => {});
    await selftestWaitFor(() => frames > 0, RB_WS_FIRST_MS, 50);
    if (frames > 0) await delay(Math.max(0, SELFTEST_LIVE_MS - (performance.now() - started)));
  } catch (e) { /* unsupported */ }
  if (conn) conn.close();
  const counted = Math.max(performance.now() - (firstAt || started), 100);
  out.flags.ws = frames > 0;
  // frames after the first one, per second since the first (a short window must not inflate the figure)
  out.fps_ws = frames > 1 ? Math.round((frames - 1) / (counted / 1000) * 10) / 10 : 0;
  out.ws_ms = firstAt ? Math.round(firstAt - started) : 0;
  if (frames > 0 && !out.kb) out.kb = Math.round(bytes / frames / 1024);
}

async function selftestLiveSse(tab, out) {
  let frames = 0, bytes = 0;
  const started = performance.now();
  let conn = null;
  try {
    conn = sseConnect(sseUrl({id: tab}), data => { frames++; bytes += Math.floor(data.length * 3 / 4); });
    conn.done.catch(() => {});
    await delay(SELFTEST_LIVE_MS);
  } catch (e) { /* unsupported */ }
  if (conn) conn.close();
  const elapsed = performance.now() - started;
  out.fps_sse = Math.round(frames / Math.max(elapsed / 1000, 0.1) * 10) / 10;
  out.flags.sse = frames > 0;
  if (frames > 0 && !out.kb) out.kb = Math.round(bytes / frames / 1024);
}

async function selftestLivePoll(tab, out) {
  let frames = 0, bytes = 0, seq = 0;
  const ctl = new AbortController();
  const started = performance.now();
  const stopTimer = setTimeout(() => ctl.abort(), SELFTEST_LIVE_MS);
  try {
    while (performance.now() - started < SELFTEST_LIVE_MS) {
      const res = await fetch("/api/rb/frame.jpg?tab=" + encodeURIComponent(tab) + "&since=" + seq,
        {signal: ctl.signal, cache: "no-store", credentials: "same-origin"});
      if (res.status !== 200 && res.status !== 204) break;
      const n = Number(res.headers.get("X-Frame-Seq"));
      if (res.status === 200) {
        const blob = await res.blob();
        if (blob.size > 0 && blob.type === "image/jpeg") { frames++; bytes += blob.size; }
      }
      if (Number.isFinite(n)) seq = Math.max(seq, n);
    }
  } catch (e) { /* the window ended while a long poll was waiting for a new frame */ }
  finally { clearTimeout(stopTimer); }
  const elapsed = performance.now() - started;
  out.fps_poll = Math.round(frames / Math.max(elapsed / 1000, 0.1) * 10) / 10;
  out.flags.poll = frames > 0;
  if (frames > 0 && !out.kb) out.kb = Math.round(bytes / frames / 1024);
}

async function selftestLiveStep(tab, holder) {
  const out = {flags: {}, kb: 0, firstMs: 0};
  if (wsSupported()) await selftestLiveWs(tab, out);
  await delay(300);
  if (sseSupported()) await selftestLiveSse(tab, out);
  await delay(300);
  await selftestLiveMjpeg(tab, holder, out);
  await delay(300);
  await selftestLivePoll(tab, out);
  const chosen = out.flags.ws ? "ws" : out.flags.sse ? "sse" : out.flags.mjpeg ? "mjpeg" : out.flags.poll ? "poll" : "";
  const fps = chosen === "ws" ? out.fps_ws : chosen === "sse" ? out.fps_sse : chosen === "mjpeg" ? out.fps_mjpeg :
    chosen === "poll" ? out.fps_poll : 0;
  const note = out.flags.ws ? "WebSocketの最初の画面 " + out.ws_ms + " ms" :
    wsSupported() ? "WebSocketの最初の画面が届かない（" + (RB_WS_FIRST_MS / 1000) + "秒）" : "WebSocket非対応";
  const result = {ok: Boolean(chosen), fps: fps || 0, kb: out.kb, ms: out.ws_ms || out.firstMs, flags: out.flags, note};
  for (const key of ["fps_ws", "fps_mjpeg", "fps_sse", "fps_poll"]) if (Number.isFinite(out[key])) result[key] = out[key];
  return result;
}

// ---- Settings › NASブラウザ (v0.1.16)
const RB_PHASE_LABELS = {stopped: "停止中", starting: "起動中…", running: "稼働中",
  stopping: "停止処理中…", setup: "初期設定中"};

function browserSectionVisible() {
  return state.route.view === "settings" && state.route.section === "browser";
}

function renderRemoteHosts() {
  const box = $("remoteHosts");
  if (!box) return;
  const hosts = displayHosts();
  if (!hosts.length) {
    box.replaceChildren(element("p", "設定されたサイトはありません（すべて既定の表示）", "empty-state"));
    return;
  }
  const nodes = [];
  for (const host of hosts) {
    const row = element("div", undefined, "item");
    const stored = rememberedMode(host);
    const shown = DISPLAY_OF_MODE[stored];
    row.appendChild(element("span", host + "（" + (stored === REMOTE_PT_MODE ? PT_DISPLAY_LABEL :
      shown ? RB_DISPLAY_LABELS[shown] : "既定の表示") + "）"));
    const off = element("button", "解除", "btn small");
    off.type = "button";
    off.addEventListener("click", () => { rememberHostMode(host, ""); renderRemoteHosts(); });
    row.appendChild(off);
    nodes.push(row);
  }
  box.replaceChildren(...nodes);
}

// Settings › NASブラウザ: per-site rules (top-level page host of the NAS browser tab).
const POPUP_LABELS = {ask: "確認する", allow: "許可（新しいタブ）", block: "ブロック"};

function ruleToggle(label, checked, onChange) {
  const wrap = element("label", undefined, "switch compact");
  const input = document.createElement("input");
  input.type = "checkbox";
  input.setAttribute("role", "switch");
  input.checked = checked;
  input.addEventListener("change", () => perform(async () => {
    const done = busyControl(input);          // v0.1.59: already shows the new state, dimmed until confirmed
    try { await onChange(input.checked); } catch (e) { input.checked = !input.checked; throw e; } finally { done(); }
  }));
  wrap.appendChild(input);
  wrap.appendChild(element("span", undefined, "switch-track"));
  wrap.appendChild(element("span", label));
  return wrap;
}

function renderSiteRules(data) {
  state.siteRules = data;
  const box = $("siteRules");
  if (!box) return;
  const items = Array.isArray(data.items) ? data.items : [];
  if (!items.length) {
    box.replaceChildren(element("p", "サイトごとの設定はありません", "empty-state"));
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

// v0.1.59: optimistic. The setting takes effect in the app (state, home line, NAS-browser sync) and the control
// shows a subtle busy mark at once; the Hub's answer only confirms it. A failure puts the old value back (state and
// control) and throws, so the caller's own toast / revert code still runs. Saves of one key are sent in order.
const optionChain = new Map();
const OPTION_CONTROL = {
  pt_enabled: "ptEnabled", rb_resident: "rbResident", block_ads: "blockAds", rb_default_mode: "rbDefaultMode",
  rb_max_tabs: "rbMaxTabs", rb_spare_tab: "rbSpareTab", home_recent: "homeRecentOn", rb_no_webfonts: "rbNoWebfonts", rb_link_boxes: "rbLinkBoxes", probe_autosend: "probeAutosend",
  auto_clear_links: "autoClearLinks", auto_clear_downloads: "autoClearDownloads", auto_clear_missing: "autoClearMissing", send_autostart: "sendAutostart",
};

// Marks a control (and its switch label) as saving; returns the function that clears the mark.
function busyControl(control) {
  const el = typeof control === "string" ? $(control) : control;
  if (!el) return () => {};
  const n = Number(el.dataset.saving || 0) + 1;
  el.dataset.saving = String(n);
  el.setAttribute("aria-busy", "true");
  const holder = el.closest("label, .set-row");
  if (holder) holder.classList.add("is-saving");
  return () => {
    const left = Math.max(0, Number(el.dataset.saving || 1) - 1);
    el.dataset.saving = String(left);
    if (left === 0) {
      el.removeAttribute("aria-busy");
      if (holder) holder.classList.remove("is-saving");
    }
  };
}

async function saveOption(key, value) {
  const before = state.settings ? state.settings[key] : undefined;
  state.settings = Object.assign({}, state.settings, {[key]: value});
  renderHome();
  syncRemoteSettings();
  const done = busyControl(OPTION_CONTROL[key]);
  const previous = optionChain.get(key) || Promise.resolve();
  const request = previous.catch(() => {}).then(() => api("/api/settings/options", {[key]: value}));
  optionChain.set(key, request);
  try {
    const saved = await request;
    swrDrop("settings", "status");
    if (optionChain.get(key) === request) state.settings = saved;     // a newer save of this key is still in flight
    else state.settings = Object.assign({}, saved, state.settings, {[key]: state.settings[key]});
    renderHome();
    syncRemoteSettings();
    return saved;
  } catch (e) {
    if (optionChain.get(key) === request) {
      state.settings = Object.assign({}, state.settings, {[key]: before});
      renderHome();
      syncRemoteSettings();
    }
    throw e;
  } finally {
    done();
    if (optionChain.get(key) === request) optionChain.delete(key);
  }
}

// Whether the NAS browser image exists (null = not known yet). A change updates the shown tab:
// "not ready" appears, or the tab opens by itself once the setup is done.
function setRbReady(ready) {
  if (state.rbReady === ready) return;
  state.rbReady = ready;
  for (const tab of state.tabs) {
    if (ready !== false && tab.notReady) { tab.notReady = false; tab.reopenAt = 0; }
  }
  if (state.authed) updateChrome();
}

// v0.1.65: the tab limit (1-20) and, once the self-test 「タブの容量」 has measured it, the recommended number.
function renderMaxTabs() {
  const select = $("rbMaxTabs");
  if (!select) return;
  if (!select.options.length) {
    for (let n = 1; n <= 20; n++) {
      const option = element("option", n + "枚");
      option.value = String(n);
      select.appendChild(option);
    }
  }
  const s = state.settings || {};
  if (Number.isInteger(s.rb_max_tabs)) select.value = String(s.rb_max_tabs);
  const reco = state.rbStatus && state.rbStatus.tab_reco;
  const row = $("rbMaxTabsRecoRow");
  const valid = reco && Number.isInteger(reco.tabs) && reco.tabs >= 1 && reco.tabs <= 20;
  row.hidden = !valid;
  if (!valid) return;
  $("rbMaxTabsReco").textContent = reco.tabs + "枚（1ページあたり 平均 " + Math.round(reco.avg_mib) + " MiB・最大 " + Math.round(reco.max_mib) +
    " MiB で自己テストが算出" + (reco.partial ? "・途中まで測定" : "") + "）";
  $("rbMaxTabsApply").disabled = Number(s.rb_max_tabs) === reco.tabs;
}

function renderBrowserStatus(r) {
  state.rbStatus = r;
  if (typeof r.image_ready === "boolean") setRbReady(r.image_ready);
  const cs = r.container_station || {};
  const setup = r.phase === "setup";
  const res = r.resident || {};
  const wd = res.watchdog || {};
  let label = r.update_needed && !setup ? "更新が必要" : !r.image_ready && !setup ? "未設定" : RB_PHASE_LABELS[r.phase] || r.phase;
  if (!setup && r.image_ready) {
    if (res.state === "resident") label = "常駐中";
    else if (res.state === "restarting") label = "再起動中（" + wd.restart_n + "回目）";
    else if (res.state === "gave_up") label = "自動再起動を停止";
    else if (res.state === "memory_wait") label = "メモリ不足のため待機中";
  }
  $("rbState").textContent = label;
  $("rbState").classList.toggle("is-ok", r.phase === "running");
  $("rbRetry").hidden = res.state !== "gave_up";
  const wdText = res.state === "gave_up" ? wd.message : res.state === "memory_wait" ? "メモリ不足のため待機中" : "";
  $("rbWatchdog").hidden = !wdText;
  $("rbWatchdog").textContent = wdText || "";
  if (typeof res.on === "boolean") {
    $("rbResident").checked = res.on;
    renderResidentQuick(res.on);
  }
  $("rbResidentLine").textContent = residentLines(r);
  $("rbWdLog").textContent = (wd.events || []).map(e => {
    const t = new Date(e.at * 1000);
    return t.toLocaleTimeString("ja-JP", {hour12: false}) + "  " + e.text;
  }).join("\n") || "まだありません";
  setShown("rbNoContainer", cs.available === false);
  $("rbSetup").hidden = setup || cs.available === false || r.phase !== "stopped";
  $("rbSetup").textContent = r.image_ready ? "イメージを再作成" :
    r.update_needed ? "初期設定を再実行" : "初期設定（イメージ作成）";
  $("rbStop").hidden = !(r.phase === "running" || r.phase === "starting");
  const parts = [];
  if (r.memory) parts.push("ブラウザのメモリ " + r.memory.used_mib + (Number.isFinite(r.memory.limit_mib) ?
    " / " + r.memory.limit_mib + " MiB（" + r.memory.percent + "%）" : " MiB"));
  if (typeof r.available_mib === "number") parts.push("NAS空きメモリ " + r.available_mib + " MiB");
  parts.push("タブ " + (r.tabs || []).length + " / " + r.max_tabs);
  $("rbMemory").textContent = parts.join(" · ");
  renderMaxTabs();
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
}

async function loadBrowserLog() {
  const data = await api("/api/rb/log");
  $("rbLog").textContent = (data.lines || []).join("\n") || "まだログはありません";
}

async function loadBrowserStatus(force = false) {
  await swr("rb-status", "/api/rb/status", renderBrowserStatus, {force});
}

// v0.1.38: the 常駐 switch sits right under the status (one control, no duplicate button).
function renderResidentQuick(on) {
  $("rbResident").checked = on;
}

async function setResident(on, revert) {
  try {
    await saveOption("rb_resident", on);
    flash(on ? "NASブラウザを常駐させます" : "NASブラウザの常駐をやめました（使わなければ自動で停止）");
  } catch (e) { revert(); throw e; }
  swrDrop("rb-status");
  renderBrowserStatus(await api("/api/rb/status"));
}

function fmtMib(value) { return typeof value === "number" ? value + " MiB" : "取得できません"; }

function residentLines(r) {
  const res = r.resident || {};
  const parts = [];
  const idle = res.idle;
  const total = typeof res.total_mib === "number" ? Math.round(res.total_mib / 1024) + " GB" : "";
  if (idle) {
    parts.push("待機時 約 " + idle.latest_mib + " MiB" +
      (typeof idle.percent_of_total === "number" && total ? "（" + total + "の" + idle.percent_of_total + "%）" : "") +
      (idle.min_mib < idle.latest_mib ? " 最小 " + idle.min_mib + " MiB" : ""));
  } else {
    parts.push(res.on ? "待機時 未測定（タブを閉じて60秒後に測ります）" : "待機時 未測定（常駐をオンにすると測ります）");
  }
  if (r.phase === "running") {
    const cur = res.memory;
    parts.push("現在 " + (cur ? cur.used_mib + " MiB" + (typeof cur.anon_mib === "number" ? "（匿名 " + cur.anon_mib + "）" : "") : "取得できません"));
  }
  parts.push("Hub " + fmtMib(res.hub_rss_mib));
  return parts.join(" · ");
}

function wireBrowser() {
  wirePt();      // v0.1.57
  $("rbResident").addEventListener("change", () => perform(() =>
    setResident($("rbResident").checked, () => { $("rbResident").checked = !$("rbResident").checked; })));
  $("rbRetry").addEventListener("click", () => perform(async () => {
    $("rbRetry").disabled = true;
    try { await api("/api/rb/retry", {}); } finally { $("rbRetry").disabled = false; }
    swrDrop("rb-status");
    renderBrowserStatus(await api("/api/rb/status"));
    flash("NASブラウザを再起動します");
  }));
  $("blockAds").addEventListener("change", () => perform(async () => {
    try {
      await saveOption("block_ads", $("blockAds").checked);
      flash($("blockAds").checked ? "広告をブロックします（次に開くタブから）" : "広告ブロックをやめました（次に開くタブから）");
    } catch (e) { $("blockAds").checked = !$("blockAds").checked; throw e; }
  }));
  $("rbDefaultMode").addEventListener("change", () => perform(async () => {
    const value = $("rbDefaultMode").value;
    try {
      await saveOption("rb_default_mode", value);
      flash("既定の表示方法: " + RB_DISPLAY_LABELS[DISPLAY_OF_SETTING[value]]);
    } catch (e) {
      renderDownloadDir(state.settings || {});
      throw e;
    }
  }));
  renderMaxTabs();
  $("rbMaxTabs").addEventListener("change", () => perform(async () => {
    const n = Number($("rbMaxTabs").value);
    try {
      await saveOption("rb_max_tabs", n);
      flash("NASブラウザのタブは最大" + n + "枚にしました");
      renderMaxTabs();
    } catch (e) { renderMaxTabs(); throw e; }
  }));
  $("rbMaxTabsApply").addEventListener("click", () => perform(async () => {
    const reco = state.rbStatus && state.rbStatus.tab_reco;
    if (!reco || !Number.isInteger(reco.tabs)) return;
    try {
      await saveOption("rb_max_tabs", reco.tabs);
      flash("タブの上限を推奨の" + reco.tabs + "枚にしました");
    } finally { renderMaxTabs(); }
  }));
  $("homeRecentOn").addEventListener("change", () => perform(async () => {      // v0.1.65
    const on = $("homeRecentOn").checked;
    try {
      await saveOption("home_recent", on);
      if (!on) removeLocal(RECENT_KEY);        // off also forgets what this device kept
      state.homeRecentSig = null;
      renderHomeRecent();
      flash(on ? "最近見たページを表示します" : "最近見たページを表示しません（一覧も消しました）");
    } catch (e) { $("homeRecentOn").checked = !on; throw e; }
  }));
  $("rbSpareTab").addEventListener("change", () => perform(async () => {
    try {
      await saveOption("rb_spare_tab", $("rbSpareTab").checked);
      flash($("rbSpareTab").checked ? "予備タブを用意します" : "予備タブをやめました");
    } catch (e) { $("rbSpareTab").checked = !$("rbSpareTab").checked; throw e; }
  }));
  $("rbNoWebfonts").addEventListener("change", () => perform(async () => {
    try {
      await saveOption("rb_no_webfonts", $("rbNoWebfonts").checked);
      flash($("rbNoWebfonts").checked ? "ミラー表示でWebフォントを使いません（次の取得から）" : "ミラー表示でWebフォントも取得します（次の取得から）");
    } catch (e) { $("rbNoWebfonts").checked = !$("rbNoWebfonts").checked; throw e; }
  }));
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
  // v0.1.56: both default OFF
  $("autoClearLinks").addEventListener("change", () => perform(async () => {
    try {
      await saveOption("auto_clear_links", $("autoClearLinks").checked);
      flash($("autoClearLinks").checked ? "JD2へ送れたリンクを自動で消します" : "送ったリンクは一覧に残します");
    } catch (e) { $("autoClearLinks").checked = !$("autoClearLinks").checked; throw e; }
  }));
  $("autoClearDownloads").addEventListener("change", () => perform(async () => {
    try {
      await saveOption("auto_clear_downloads", $("autoClearDownloads").checked);
      flash($("autoClearDownloads").checked ? "完了・スキップしたダウンロードを自動で消します（ファイルは残ります）" : "完了・スキップは手動で消します");
    } catch (e) { $("autoClearDownloads").checked = !$("autoClearDownloads").checked; throw e; }
  }));
  $("autoClearMissing").addEventListener("change", () => perform(async () => {
    try {
      await saveOption("auto_clear_missing", $("autoClearMissing").checked);
      flash($("autoClearMissing").checked ? "ファイルがないリンクを自動で一覧から消します（ファイルは残ります）" : "ファイルがないリンクは一覧に残します");
    } catch (e) { $("autoClearMissing").checked = !$("autoClearMissing").checked; throw e; }
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
}

function wireEngine() {
  $("engineSetup").addEventListener("click", () => perform(() => startEngineSetup()));
  $("engineResetup").addEventListener("click", () => perform(() => startEngineSetup()));
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
    const done = busyControl("engineAutostart");
    try {
      await api("/api/engine/autostart", {enabled: wanted});
    } catch (e) {
      $("engineAutostart").checked = !wanted;
      throw e;
    } finally { done(); }
    swrDrop("engine");
    flash(wanted ? "Hub起動時にエンジンも起動します" : "自動起動をOFFにしました");
  }));
  $("engineKeys").addEventListener("click", () => perform(loadEngineKeys));
  $("engineKeysCopy").addEventListener("click", async () => {
    copyText($("engineKeysOut").textContent);
  });
  $("engineErrorMore").addEventListener("click", () => {      // v0.1.62
    const clamped = $("engineError").classList.toggle("is-clamped");
    $("engineErrorMore").textContent = clamped ? "詳しく" : "閉じる";
    $("engineErrorMore").setAttribute("aria-expanded", String(!clamped));
  });
  $("engineAdvanced").addEventListener("toggle", () => {      // the log is read when 詳細・ログ is opened
    if ($("engineAdvanced").open) perform(loadEngineLog);
  });
  $("rbLogBox").addEventListener("toggle", () => {
    if ($("rbLogBox").open) perform(loadBrowserLog);
  });
}

function wireSettings() {
  $("folderChange").addEventListener("click", () => perform(async () => {
    setPicker(true);
    await folders("", true);
  }));
  $("folderCancel").addEventListener("click", () => setPicker(false));
  $("folderSheet").addEventListener("click", e => { if (e.target === $("folderSheet")) setPicker(false); });
  $("folderSheet").addEventListener("keydown", e => { if (e.key === "Escape") setPicker(false); });
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
  wireDiagnostics();
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
  $("flattenNow").addEventListener("click", () => perform(async () => {
    const out = await api("/api/jd2/flatten", {});
    const text = out.moved > 0 ? out.moved + "件のパッケージを保存先の直下へ移しました" : "移すパッケージはありません";
    $("flattenResult").textContent = text;
    flash(text);
    state.downloadsSig = "";
  }));
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
  registerTasks();
  wireAuth();
  wireShell();
  wireDownloads();
  wireLinks();
  paintShellEarly();
  wireChrome();
  go();
  // v0.1.60: after the first paint, never before it
  window.addEventListener("load", () => setTimeout(registerShell, 300));
});
