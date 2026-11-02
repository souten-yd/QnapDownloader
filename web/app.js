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
const EXTERNAL_TOAST_MS = 8000;
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
const REMOTE_MODE = "remote";
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
const RB_PERF_WINDOW_MS = 5000;
const RB_POPUP_NEW_TAB = "new";
const RB_KEYS = ["Enter", "Backspace", "Tab", "Escape", "Delete",
  "ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"];
const MODE_LABELS = {strict: "強い制限", interactive: "限定操作", compat: "互換"};
// Per-host "always open in Safari". Stored in the same hostname->mode map as the
// isolation modes, but it is NOT a tab mode (never in MODES / tab.mode / sandbox).
const EXTERNAL_MODE = "external";
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
  rbPolling: "", rbGen: 0, rbStatus: null, rbTimer: null, rbBusy: false,
  rbIntercepts: [],
  rbQuality: loadRbQuality(), rbPerf: false, rbPerfSamples: [], rbPerfTimer: 0,
  rbPerfSince: 0, probeSeq: 0, probeResult: null, siteRules: null, waiting: null,
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
    return data.filter(e => Array.isArray(e) && typeof e[0] === "string" && e[0].length <= 253 &&
      ((MODES.includes(e[1]) && e[1] !== "strict") || e[1] === EXTERNAL_MODE ||
        e[1] === REMOTE_MODE)).slice(-MAX_HOST_MODES);
  } catch (e) { return []; }
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

function isRemoteHost(host) { return Boolean(host) && rememberedMode(host) === REMOTE_MODE; }

function remoteHosts() {
  return readHostModes().filter(e => e[1] === REMOTE_MODE).map(e => e[0]);
}

function isExternalHost(host) { return Boolean(host) && rememberedMode(host) === EXTERNAL_MODE; }

function externalHosts() {
  return readHostModes().filter(e => e[1] === EXTERNAL_MODE).map(e => e[0]);
}

function clearExternalHost(host) {
  if (isExternalHost(host)) rememberHostMode(host, "strict");   // strict = store nothing
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

// One compact row per web tab (single line, ellipsis): [mode chip] host ... [Safari] [...]
function updateWebStatus(tab) {
  const box = $("webStatus");
  box.replaceChildren();
  if (!tab || !tab.history.length) return;
  const external = Boolean(tab.external);
  const remote = Boolean(tab.remote);
  const chip = element("button", remote ? "NASブラウザ" : external ? "Safari固定" : (MODE_LABELS[tab.mode] || MODE_LABELS.strict),
    "mode-badge " + (remote ? "remote" : external ? "external" : tab.mode));
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
  const safari = element("button", "Safari", "btn small");
  safari.type = "button";
  safari.title = "Safariで開く（今回のみ）";
  safari.setAttribute("aria-label", "Safariで開く（今回のみ）");
  safari.disabled = !tab.current;
  safari.addEventListener("click", () => openExternal(tab));
  box.appendChild(safari);
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
  const show = Boolean(tab && tab.current && !tab.external && !tab.remote && tab.showHint && !tab.hintDismissed);
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
    lastUsed: 0, hintDismissed: false, showHint: false, hintTimer: 0, external: false,
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

// ---- always-open-in-Safari (external mode) ----
// iOS only allows window.open() inside the user-gesture call stack, and
// /api/browser/validate is async, so we cannot validate first and open after
// (nor open a blank window first: that leaves an opener-linked popup). A host
// is only in external mode because the user chose it for a page that already
// passed server validation, so for such a host we run a cheap client-side
// sanity check (https, no credentials/port, public-looking hostname equal to
// the stored host) and call window.open synchronously with noopener. The
// server stays authoritative: /api/browser/validate is still called right
// afterwards and a rejection is surfaced (and the host's external mode cleared).
function externalSanityUrl(value) {
  let parsed;
  try { parsed = new URL(value); } catch (e) { return ""; }
  const host = parsed.hostname.toLowerCase();
  const hub = location.hostname.toLowerCase().replace(/^\[|\]$/g, "");
  const ok = parsed.protocol === "https:" && !parsed.username && !parsed.password && !parsed.port &&
    host.length <= 253 && host.includes(".") && host !== hub &&
    !/^[\d.]+$/.test(host) && !host.includes(":") && !host.startsWith("[") &&
    !/(^|\.)(localhost|local|internal|lan|home|ts\.net)$/.test(host) &&
    parsed.href.length <= 2048 && isExternalHost(hostOf(parsed.href));
  return ok ? parsed.href : "";
}

function showExternalToast(host, href) {
  stickyToast("external", "Safariで開きました（このサイトは常に外部で開きます）", "success", [{
    label: "タブで開く",
    run: () => {
      clearExternalHost(host);
      dropExternalToast();
      renderExternalHosts();
      perform(() => openWebUrl(href));
    },
  }]);
  setTimeout(dropExternalToast, EXTERNAL_TOAST_MS);
}

function dropExternalToast() {
  for (const old of Array.from($("notice").children)) {
    if (old.dataset.sticky === "external") old.remove();
  }
}

// Returns true when the navigation was handed to Safari (no iframe is created).
function tryExternalOpen(value, tab) {
  const href = externalSanityUrl(value);
  if (!href) return false;
  const host = hostOf(href);
  window.open(href, "_blank", "noopener,noreferrer");
  validateUrl(href).catch(e => {
    clearExternalHost(host);
    renderExternalHosts();
    toast("このサイトは開けません: " + e.message, "error");
  });
  showExternalToast(host, href);
  // A blank/new tab would only show an empty page: close it. A tab showing
  // another page is left as it is.
  if (tab && state.tabs.includes(tab) && !tab.history.length) closeTab(tab.id);
  return true;
}

// Validate `value`, let `commit` update the history, then (re)load the iframe.
// opts.restore: not a user gesture (lazy load of a restored tab), so an
// external host is shown without a frame instead of calling window.open.
async function openInTab(tab, value, commit, opts = {}) {
  if (tab.remote || (!opts.restore && isRemoteHost(hostOf(value)))) {
    if (opts.restore) { tab.loaded = true; refreshAfterTabChange(); return; }   // never auto-open on restore
    await openRemote(tab, value, commit);
    return;
  }
  if (!opts.restore && isExternalHost(hostOf(value)) && tryExternalOpen(value, tab)) return;
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
  if (isExternalHost(hostOf(url))) {
    // No iframe for an external host; the compact row offers the Safari button.
    if (tab.frame) tab.frame.remove();
    tab.frame = null;
    tab.loading = false;
    tab.external = true;
  } else {
    tab.external = false;
    mountFrame(tab, url);
  }
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
        rev: null, icptSeen: 0, present: 0, vp: "", popups: 0};
    }
  } catch (e) {
    if (token === tab.token) { tab.loading = false; tab.error = e.message; updateChrome(); }
    throw e;
  }
  if (tab.frame) { tab.frame.remove(); tab.frame = null; }
  commit(url);
  tab.remote = true;
  tab.external = false;
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
  if (state.rbPolling === rb.id) { state.rbPolling = ""; state.rbGen++; }
  dropRemoteImage(rb);
  state.rbIntercepts = state.rbIntercepts.filter(item => item.tab !== rb.id);
  if (rbPopup.item && rbPopup.item.tab === rb.id) hidePopupToast();
  closeProbeSheet();
  renderIntercept();
  api("/api/rb/tabs/close", {id: rb.id}).catch(() => {});
}

async function openInRemote(tab) {
  const url = tab.current || (tab.history.length ? tab.history[tab.index] : "");
  if (!url) return;
  rememberHostMode(hostOf(url), REMOTE_MODE);
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

async function rbInput(event) {
  const tab = remoteTab();
  if (!tab) return;
  try {
    await api("/api/rb/input", Object.assign({tab: tab.rb.id}, event));
  } catch (e) {
    if (e.status === 404) remoteLost(tab);
    else toast(e.message, "error");
  }
}

function remoteLost(tab) {
  if (tab.rb) {
    dropRemoteImage(tab.rb);
    state.rbIntercepts = state.rbIntercepts.filter(item => item.tab !== tab.rb.id);
    renderIntercept();
  }
  tab.rb = null;
  tab.error = "NASブラウザのタブが閉じられました";
  state.rbPolling = "";
  state.rbGen++;
  updateChrome();
}

// Free the object URL and clear the picture (nothing is kept once a tab is gone).
// One <img> is shared by all remote tabs, so only its current owner clears it.
const rbShown = {url: "", owner: null};

function dropRemoteImage(rb) {
  rb.present++;
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
  if (Number.isFinite(f.popups) && f.popups !== rb.popups) { rb.popups = f.popups; changed = true; }
  if (f.blocked && f.blocked !== rb.blocked) toast(String(f.blocked), "error");
  rb.blocked = f.blocked || null;
  tab.loading = loading;
  if (Number.isFinite(f.intercepts) && f.intercepts > rb.icptSeen) {
    fetchIntercepts(tab).catch(() => {});
  }
  if (changed) updateChrome();
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
    await api("/api/rb/viewport", Object.assign({tab: rb.id, quality: state.rbQuality}, view));
  } catch (e) {
    rb.vp = "";
    if (e.status === 404) remoteLost(tab);
    else throw e;
  }
}

const nextPaint = () => new Promise(resolve => requestAnimationFrame(() => resolve()));

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
  if (id === state.rbPolling) return;
  state.rbPolling = id;
  state.rbGen++;
  if (want) remoteLoop(tab, state.rbGen);
}

function updateRemoteView(tab) {
  const remote = Boolean(tab && tab.remote && tab.history.length);
  setShown("remoteView", remote);
  $("frames").hidden = remote;
  if (!remote) { closeRemoteSheet(); setShown("interceptSheet", false); return; }
  const live = Boolean(tab.rb);
  $("remoteImg").hidden = !live || !$("remoteImg").getAttribute("src");
  $("remoteIdle").hidden = live && !$("remoteImg").hidden;
  $("remoteIdleText").textContent = tab.error || (live ? "画面を待っています…" :
    "NASブラウザのタブは開かれていません（メモリ節約のため自動で閉じました）。");
  setShown("remoteReopen", !live);
  setShown("remoteLoading", Boolean(tab.loading));
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
  if (!staged.length) { flash("送れる未送信のリンクはありません"); return; }
  await sendLinks(staged);
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
    if (!confirm("このリンクをJD2へ送ります。" + sendStartText() + "既存のPackagizerルールによりフォルダが変わる可能性があります。続けますか？")) return;
    const autoHost = $("interceptAuto").checked ? hostOf(remoteTab().current) : "";
    await api("/api/rb/intercepts/send", body);
    swrDrop("links");
    await loadQueue(true).catch(() => {});
    flash(sentText());
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

function perfText() {
  const now = performance.now();
  const recent = state.rbPerfSamples.filter(x => x.t >= now - RB_PERF_WINDOW_MS);
  const frames = recent.filter(x => x.bytes > 0);
  const elapsed = Math.max(1, Math.min(RB_PERF_WINDOW_MS, now - state.rbPerfSince) / 1000);
  const fps = frames.length / elapsed;
  const bytes = frames.length ? frames.reduce((sum, x) => sum + x.bytes, 0) / frames.length : 0;
  const wait = recent.length ? recent.reduce((sum, x) => sum + x.latency, 0) / recent.length : 0;
  return fps.toFixed(1) + " fps · " + (bytes / 1024).toFixed(0) + " KB/枚 · 応答 " + Math.round(wait) + " ms";
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
  perform(() => startProbe(tab, point));
}

function closeProbeSheet() {
  state.probeSeq++;
  setShown("probeSheet", false);
}

function renderProbe(view, tab) {
  const busy = view.busy === true;
  const result = view.result || null;
  const links = result ? result.links : [];
  $("probeTitle").textContent = busy ? "リンク先を確認しています…" : "リンク先を確認しました";
  let status = "最大15秒かかります。確認用のページは自動で閉じます";
  if (result) {
    status = "リダイレクト " + result.redirects + "回 · 最終: " + (hostLabel(result.final_url) || "不明") +
      (links.length ? "" : "（Rapidgatorのリンクは見つかりませんでした）");
  }
  $("probeStatus").textContent = status;
  $("probeStatus").title = result ? result.final_url : "";
  const canSend = jd2CanSend();
  const rows = links.map(url => {
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
    return row;
  });
  $("probeList").replaceChildren(...rows);
  $("probeSendAll").textContent = "すべてJD2へ送る（" + links.length + "件）";
  $("probeAddAll").textContent = "リンクに追加（" + links.length + "件）";
  setShown("probeSendAll", !busy && canSend && links.length > 0);
  setShown("probeAddAll", !busy && links.length > 0);
  setShown("probeOpen", !busy && Boolean(result) && links.length === 0 && Boolean(result.final_url));
  state.probeResult = result;
}

async function startProbe(tab, point) {
  const rb = tab.rb;
  if (!rb || rb.probing) { toast("リンク先を確認中です", "error"); return; }
  const seq = ++state.probeSeq;
  rb.probing = true;
  renderProbe({busy: true}, tab);
  setShown("probeSheet", true);
  loadJd2Status().then(() => { if (seq === state.probeSeq && state.probeResult) renderProbe({result: state.probeResult}, tab); }).catch(() => {});
  try {
    const result = await api("/api/rb/probe", {tab: rb.id, x: point.x, y: point.y});
    if (seq !== state.probeSeq || tab.rb !== rb) return;
    renderProbe({result}, tab);
  } catch (e) {
    if (seq === state.probeSeq) setShown("probeSheet", false);
    if (e.status === 404) remoteLost(tab);
    else throw e;
  } finally {
    rb.probing = false;
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
    if (!remoteTab() || rbPointer.id !== null) return;
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
  stage.addEventListener("contextmenu", event => event.preventDefault());
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
    const point = remotePoint(event);
    if (!point || !remoteTab()) return;
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
  $("probeSendAll").addEventListener("click", () => perform(async () => {
    if (state.probeResult) await sendUrls(state.probeResult.links.slice());
  }));
  $("probeAddAll").addEventListener("click", () => perform(async () => {
    const links = state.probeResult ? state.probeResult.links.slice() : [];
    await stageLinks(links);
    flash(links.length + "件を追加したリンクに入れました（JD2へは未送信）");
  }));
  $("probeOpen").addEventListener("click", () => perform(async () => {
    const tab = remoteTab();
    const result = state.probeResult;
    if (!tab || !result) return;
    closeProbeSheet();
    await api("/api/rb/navigate", {tab: tab.rb.id, url: result.final_url});
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
  if (isExternalHost(hostOf(value)) && tryExternalOpen(value, null)) return;
  if (tabLimitReached()) return;
  const url = await validateUrl(value);
  const fresh = createTab();
  pushHistory(fresh, url);
  applyHostMode(fresh, url);
  fresh.current = url;
  fresh.loaded = true;
  if (isExternalHost(hostOf(url))) fresh.external = true;
  else mountFrame(fresh, url);
  saveTabs();
  navigate("#/tab/" + fresh.id);
}

const COMPAT_CONFIRM = "互換モードに切り替えます。\n\n" +
  "・このサイト自身のCookie・ストレージを枠内で使えるようにします（Hub自体のものではありません）。\n" +
  "・Cloudflareの確認を回避する機能ではありません。\n" +
  "・iPhoneのSafariではサードパーティCookieのブロックにより動作しない場合があります。その場合は「Safariで開く」を使ってください。\n\n" +
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
  for (const action of ["remoteOpen", "external", "stageUrl", "bookmark"]) {
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
  else if (action === "perf") setPerf(!state.rbPerf);
  else if (action === "external" && tab) openExternal(tab);
  else if (action === "stageUrl" && tab) await stageTabUrl(tab);
  else if (action === "bookmark" && tab) await bookmarkTab(tab);
  else if (action === "closeTab" && tab) closeTab(tab.id);
  else if (action === "bookmarks") navigate("#/settings/bookmarks");
  else if (action === "settings") navigate(settingsHash());
  else if (action === "logout") await logout();
}

function openExternal(tab) {
  if (!tab.current) return;   // only a URL that passed /api/browser/validate
  window.open(tab.current, "_blank", "noopener,noreferrer");
}

// "Safariで開く（次回から自動）": open now (inside the click gesture) and remember the host.
function alwaysExternal(tab) {
  if (!tab.current) return;
  const host = hostOf(tab.current);
  rememberHostMode(host, EXTERNAL_MODE);
  window.open(tab.current, "_blank", "noopener,noreferrer");
  showExternalToast(host, tab.current);
  renderExternalHosts();
}

// Settings: hosts set to "always open in Safari", each with a release button.
function renderExternalHosts() {
  const box = $("externalHosts");
  if (!box) return;
  const hosts = externalHosts();
  if (!hosts.length) {
    box.replaceChildren(element("p", "設定されたサイトはありません。", "empty-state"));
    return;
  }
  const nodes = [];
  for (const host of hosts) {
    const row = element("div", undefined, "item");
    row.appendChild(element("span", host));
    const off = element("button", "解除", "btn small");
    off.type = "button";
    off.addEventListener("click", () => { clearExternalHost(host); renderExternalHosts(); });
    row.appendChild(off);
    nodes.push(row);
  }
  box.replaceChildren(...nodes);
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
  row.appendChild(top);
  const bar = progressBar(item);
  if (bar) row.appendChild(bar);
  const meta = metaText(item, item.host);
  if (meta) row.appendChild(element("p", meta, "download-detail"));
  return row;
}

function packageKey(item) { return item.id ? "id:" + item.id : "n:" + item.name; }

function packageRow(pkg, files) {
  const key = packageKey(pkg);
  const open = state.expanded.has(key);
  const card = element("article", undefined, "download-card pkg " + itemState(pkg));
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
  card.appendChild(head);
  const bar = progressBar(pkg);
  if (bar) card.appendChild(bar);
  const count = typeof pkg.child_count === "number" ? "ファイル " + pkg.child_count + "件" : "";
  const meta = metaText(pkg, count);
  if (meta) card.appendChild(element("p", meta, "download-detail"));
  if (open) {
    const box = element("div", undefined, "pkg-files");
    if (files.length) for (const f of files) box.appendChild(fileRow(f));
    else box.appendChild(element("p", "このページに表示できるファイルがありません", "empty-state"));
    card.appendChild(box);
  }
  return card;
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
  state.downloadsSig = sig;
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
  $("waitingList").replaceChildren(...w.items.slice(0, WAITING_MAX).map(item => {
    const row = element("div", undefined, "waiting-item");
    row.appendChild(element("span", item.name, "waiting-name"));
    if (item.host) row.appendChild(element("span", item.host, "muted"));
    return row;
  }));
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
async function sendLinks(links) {
  const many = links.length > 1;
  if (!confirm(many ? links.length + "件のリンクをJD2へ順に送ります。" + sendStartText() + "既存のPackagizerルールによりフォルダが変わる可能性があります。続けますか？" :
      "このリンクをJD2へ送ります。" + sendStartText() + "既存のPackagizerルールによりフォルダが変わる可能性があります。続けますか？")) return;
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
  flash(sent + "件を" + (sendAutostartOn() ? "JD2へ送り、ダウンロードを開始しました" :
    "JD2のLinkGrabberへ送りました（ダウンロードは未開始）"));
}

function sendAllStaged() {
  const items = state.queueData ? state.queueData.items.filter(l => l.state === "staged") : [];
  return items.length ? sendLinks(items) : Promise.resolve();
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
  $("blockAds").checked = s.block_ads !== false;
  $("selectedFolder").textContent = s.download_dir || "未選択";
  $("folderChange").textContent = s.download_dir ? "変更" : "選ぶ";
  renderHome();
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
    renderExternalHosts();
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
  $("cfSafari").addEventListener("click", () => {
    const tab = activeWebTab();
    if (tab) alwaysExternal(tab);
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
  $("waitingStart").addEventListener("click", () => perform(startWaiting));
  $("downloadsToggle").addEventListener("click", () => perform(toggleDownloads));
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
    row.appendChild(element("span", host));
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
