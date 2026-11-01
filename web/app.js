"use strict";
/*
 * QnapDownloader browser-style shell (v0.1.9).
 * Spec: docs/browser-shell-ui.md. Single file, no build step.
 *
 * Security notes (see "Security invariants" in the spec):
 *  - DOM is built only with createElement/createElementNS + textContent.
 *  - External pages live in per-tab iframes whose sandbox is either empty or
 *    exactly SANDBOX_INTERACTIVE. The src is only ever set to a URL that
 *    /api/browser/validate returned (see mountFrame).
 *  - Storage keeps only {id, url history, index, mode} (session) and one
 *    boolean preference (local). Never tokens, passwords or cookies.
 */

// ---------------------------------------------------------------- constants
const MAX_WEB_TABS = 6;
const MAX_CLOSED_TABS = 10;
const MAX_HISTORY = 50;
const AUTO_REFRESH_MS = 15000;
const FRAME_LOAD_TIMEOUT_MS = 12000;
const PAGE_SIZE = 30;
// Remote pages never get same-origin rights, popups, downloads or top navigation.
const SANDBOX_STRICT = "";
const SANDBOX_INTERACTIVE = "allow-scripts allow-forms";
const TAB_STORAGE_KEY = "qnapdl.webtabs.v1";
const AUTO_STORAGE_KEY = "qnapdl.downloads.auto";
const SETTINGS_SECTIONS = ["storage", "jd2", "bookmarks", "updates", "diagnostics", "about"];
const SVG_NS = "http://www.w3.org/2000/svg";
const RAPIDGATOR_LIMIT = 65536;

const state = {
  csrf: "", authed: false, shellReady: false,
  route: {view: "home"}, lastSection: "storage",
  tabs: [], nextTabId: 1, closed: [], lastWebTabId: null,
  bookmarks: [], status: null, omniDisplay: "",
  snapshot: null, snapshotAt: 0, downloadsPage: 0, downloadsBusy: false,
  filter: "all", query: "", autoRefresh: false, autoTimer: null,
  rapidgatorSnapshot: null, selectedVersion: "", selectedChannel: "rc",
  menuReturnFocus: null,
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
function readLocal(key) {
  try { return localStorage.getItem(key); } catch (e) { return null; }
}
function writeLocal(key, value) {
  try { localStorage.setItem(key, value); } catch (e) { /* ignore */ }
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
  if (!res.ok) throw new Error(data.error || "HTTP " + res.status);
  return data;
}

async function perform(fn) {
  try { await fn(); } catch (e) { toast(e.message, "error"); }
}

// ------------------------------------------------------------------- toasts
function toast(message, kind = "success") {
  const box = $("notice");
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
  while (box.children.length > 4) box.firstChild.remove();
}
function flash(message) { toast(message, "success"); }

// ------------------------------------------------------------------ routing
function parseRoute() {
  const raw = (location.hash || "").replace(/^#\/?/, "");
  const parts = raw.split("/").filter(Boolean);
  const head = parts[0] || "home";
  if (head === "downloads" || head === "links") return {view: head};
  if (head === "settings") {
    return {view: "settings",
      section: SETTINGS_SECTIONS.includes(parts[1]) ? parts[1] : "storage"};
  }
  if (head === "tab" && /^t\d+$/.test(parts[1] || "")) return {view: "tab", tabId: parts[1]};
  return {view: "home"};
}

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
  if (route.view === "tab" && !findTab(route.tabId)) { replaceRoute("#/home"); return; }
  state.route = route;
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
  perform(() => enterView(route));
}

async function enterView(route) {
  if (route.view === "home" || (route.view === "tab" && currentViewName() === "home")) {
    await loadStatus();
  } else if (route.view === "downloads") {
    renderDownloads();
  } else if (route.view === "links") {
    await loadQueue();
  } else if (route.view === "settings") {
    await enterSettings(route.section);
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
    back = tab.index > 0;
    forward = tab.index >= 0 && tab.index < tab.history.length - 1;
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
  $("starBtn").disabled = !marked;
  $("starBtn").classList.toggle("is-on", on);
  $("starBtn").setAttribute("aria-pressed", String(on));
  $("starBtn").setAttribute("aria-label", on ? "ブックマーク済み" : "ブックマークに追加");

  const bar = (shown === "web" || shown === "home") && state.bookmarks.length > 0;
  setShown("bookmarkBar", bar);
  $("webLoading").hidden = !(tab && tab.loading);
  updateWebStatus(tab);
  updatePinnedTabs();
  updateMobileBar();
}

function updatePinnedTabs() {
  const route = state.route;
  document.querySelectorAll("[data-tab]").forEach(a => {
    a.setAttribute("aria-selected", String(route.view === a.dataset.tab));
    if (a.dataset.tab === "settings") a.setAttribute("href", "#/settings/" + state.lastSection);
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

function updateWebStatus(tab) {
  const box = $("webStatus");
  box.replaceChildren();
  if (!tab || !tab.history.length) return;
  const label = tab.mode === "interactive" ? "限定操作" : "強い制限";
  box.appendChild(element("span", label, "mode-badge " + tab.mode));
  let text;
  if (tab.error) text = "読み込めませんでした: " + tab.error;
  else if (!tab.current) text = "検証して読み込み中です…";
  else text = "隔離表示中: " + tab.current +
    " — サイトが埋め込みを禁止している場合は空白になります。⋮ メニューからSafariで開けます。" +
    "枠内の遷移先URLは読み取れません。";
  box.appendChild(element("span", text, "web-status-text"));
}

// ------------------------------------------------------------- status/home
async function loadStatus() {
  const s = await api("/api/status");
  state.status = s;
  document.querySelectorAll(".js-version").forEach(n => { n.textContent = "v" + s.version; });
  renderStatusCards();
}

function statusCard(title, value, sub, ok, href) {
  const card = element(href ? "a" : "div", undefined, "card status-card");
  if (href) card.href = href;
  const head = element("div", undefined, "status-head");
  head.appendChild(element("span", undefined, "dot " + (ok === null ? "idle" : ok ? "ok" : "off")));
  head.appendChild(element("span", title, "status-title"));
  card.appendChild(head);
  card.appendChild(element("strong", value, "status-value"));
  card.appendChild(element("span", sub, "status-sub"));
  return card;
}

function renderStatusCards() {
  const box = $("status");
  box.replaceChildren();
  const s = state.status;
  if (!s) return;
  box.appendChild(statusCard("Web管理画面", "稼働中",
    "version " + s.version + " / 設定改訂 " + s.settings_revision, true));
  box.appendChild(statusCard("JDownloader 2", s.jd2.connected ? "接続中" : "未接続",
    s.jd2.connected ? "Core revision " + s.jd2.revision : (s.jd2.reason || "接続設定を確認してください"),
    Boolean(s.jd2.connected), "#/settings/jd2"));
  const running = runningCount();
  box.appendChild(statusCard("ダウンロード", running === null ? "未取得" : running + "件 実行中",
    running === null ? "ダウンロード画面で取得します" : "最後に取得した一覧より",
    running === null ? null : running > 0, "#/downloads"));
}

// --------------------------------------------------------------- bookmarks
async function loadBookmarks() {
  const b = await api("/api/bookmarks");
  state.bookmarks = Array.isArray(b.items) ? b.items : [];
  renderBookmarkBar();
  renderSpeedDial();
  renderBookmarkList();
  updateChrome();
}

function renderBookmarkBar() {
  const bar = $("bookmarkBar");
  bar.replaceChildren();
  for (const book of state.bookmarks.slice(0, 30)) {
    const btn = element("button", undefined, "bookmark-chip");
    btn.type = "button";
    btn.title = book.url;
    btn.appendChild(icon("star"));
    btn.appendChild(element("span", book.title || hostOf(book.url)));
    btn.addEventListener("click", () => perform(() => openWebUrl(book.url)));
    bar.appendChild(btn);
  }
}

function renderSpeedDial() {
  const dial = $("speedDial");
  dial.replaceChildren();
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
    dial.appendChild(tile);
  }
  setShown("speedDialEmpty", state.bookmarks.length === 0);
}

function renderBookmarkList() {
  const list = $("bookmarkItems");
  list.replaceChildren();
  if (!state.bookmarks.length) {
    list.appendChild(element("p", "ブックマークはまだありません。", "empty-state"));
    return;
  }
  for (const book of state.bookmarks) {
    const box = element("div", undefined, "item");
    const open = element("button", book.title || hostOf(book.url), "linklike");
    open.type = "button";
    open.addEventListener("click", () => perform(() => openWebUrl(book.url)));
    box.appendChild(open);
    box.appendChild(element("span", book.url));
    list.appendChild(box);
  }
}

async function addBookmark(title, url) {
  await api("/api/bookmarks", {title, url});
  await loadBookmarks();
}

// -------------------------------------------------------------- web tabs
function newTabRecord(id) {
  return {id, history: [], index: -1, mode: "strict", current: "",
    loaded: false, loading: false, token: 0, frame: null, error: ""};
}

function saveTabs() {
  // Only id / url history / index / mode. No tokens, cookies or page data.
  const data = {v: 1, next: state.nextTabId, tabs: state.tabs.map(t => ({
    id: t.id, history: t.history.slice(), index: t.index, mode: t.mode}))};
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
    tab.mode = raw.mode === "interactive" ? "interactive" : "strict";
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
  if (tab.frame) tab.frame.remove();
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

function mountFrame(tab, validatedUrl) {
  // SECURITY: validatedUrl must come from validateUrl(). The iframe is rebuilt
  // each time so the sandbox attribute is in force before navigation starts.
  if (tab.frame) tab.frame.remove();
  const frame = document.createElement("iframe");
  frame.setAttribute("sandbox", tab.mode === "interactive" ? SANDBOX_INTERACTIVE : SANDBOX_STRICT);
  frame.setAttribute("referrerpolicy", "no-referrer");
  frame.title = "外部サイトの隔離プレビュー（" + hostOf(validatedUrl) + "）";
  frame.className = "web-frame";
  frame.hidden = !(state.route.view === "tab" && state.route.tabId === tab.id);
  const done = () => {
    if (tab.frame === frame && tab.loading) { tab.loading = false; updateChrome(); }
  };
  frame.addEventListener("load", done);
  setTimeout(done, FRAME_LOAD_TIMEOUT_MS);
  $("frames").appendChild(frame);
  tab.frame = frame;
  tab.loading = true;
  frame.src = validatedUrl;
}

function syncFrameVisibility() {
  for (const tab of state.tabs) {
    if (tab.frame) tab.frame.hidden = !(state.route.view === "tab" && state.route.tabId === tab.id);
  }
}

// Validate `value`, let `commit` update the history, then (re)load the iframe.
async function openInTab(tab, value, commit) {
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
  commit(url);
  tab.current = url;
  tab.loaded = true;
  mountFrame(tab, url);
  saveTabs();
  refreshAfterTabChange();
}

function refreshAfterTabChange() {
  if (state.route.view === "tab") {
    const shown = currentViewName();
    for (const name of ["home", "web"]) $("view-" + name).hidden = name !== shown;
  }
  syncFrameVisibility();
  renderWebTabs();
  updateChrome();
}

function pushHistory(tab, url) {
  tab.history = tab.history.slice(0, tab.index + 1);
  tab.history.push(url);
  while (tab.history.length > MAX_HISTORY) tab.history.shift();
  tab.index = tab.history.length - 1;
}

const navigateTab = (tab, value) => openInTab(tab, value, url => pushHistory(tab, url));
const reloadTab = tab => openInTab(tab, tab.history[tab.index],
  url => { tab.history[tab.index] = url; });

function ensureLoaded(tab) {
  if (!tab || !tab.history.length || tab.loaded || tab.loading) return Promise.resolve();
  return reloadTab(tab);
}

async function stepHistory(tab, delta) {
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
  fresh.current = url;
  fresh.loaded = true;
  mountFrame(fresh, url);
  saveTabs();
  navigate("#/tab/" + fresh.id);
}

async function toggleMode(tab) {
  if (tab.mode === "strict") {
    if (!confirm("外部サイトのスクリプトとフォームを隔離枠内で実行します。ログインやCloudflareの動作保証はありません。続けますか？")) return;
    tab.mode = "interactive";
  } else {
    tab.mode = "strict";
  }
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
      const section = path[1] || "storage";
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

function resetExtraction() {
  state.rapidgatorSnapshot = null;
  $("rapidgatorStage").disabled = true;
  $("rapidgatorResult").replaceChildren();
}

function prefillExtraction(text) {
  // Prefill only. Nothing is saved or sent until the user previews and confirms.
  $("rapidgatorText").value = text.slice(0, RAPIDGATOR_LIMIT);
  resetExtraction();
  navigate("#/links");
  toast("Rapidgator抽出欄に転記しました。内容を確認して「リンクを抽出して確認」を押してください（自動保存はしません）。");
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
  for (const action of ["external", "stageUrl", "bookmark"]) {
    $("menu").querySelector("[data-action='" + action + "']").disabled = !usable;
  }
  $("menu").querySelector("[data-action='reopen']").disabled = state.closed.length === 0;
  $("menuMode").textContent = tab && tab.mode === "interactive" ?
    "強い制限モードに戻す" : "限定操作モードに切り替え";
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
  else if (action === "toggleMode" && tab) await toggleMode(tab);
  else if (action === "external" && tab) openExternal(tab);
  else if (action === "stageUrl" && tab) await stageTabUrl(tab);
  else if (action === "bookmark" && tab) await bookmarkTab(tab);
  else if (action === "closeTab" && tab) closeTab(tab.id);
  else if (action === "bookmarks") navigate("#/settings/bookmarks");
  else if (action === "settings") navigate("#/settings/" + state.lastSection);
  else if (action === "logout") await logout();
}

function openExternal(tab) {
  if (!tab.current) return;   // only a URL that passed /api/browser/validate
  window.open(tab.current, "_blank", "noopener,noreferrer");
}

async function stageTabUrl(tab) {
  if (!tab.current) return;
  if (!confirm("現在のアドレス欄のURLを保留キューに登録します。サイト内リンクは自動解析せず、JD2へも送信しません。よろしいですか？")) return;
  await api("/api/links", {url: tab.current});
  flash("保留キューに登録しました。ダウンロードは開始しません。");
}

async function bookmarkTab(tab) {
  if (!tab.current) return;
  if (state.bookmarks.some(b => b.url === tab.current)) { flash("すでにブックマークに登録されています"); return; }
  await addBookmark(hostOf(tab.current), tab.current);
  flash("ブックマークに追加しました");
}

async function logout() {
  await api("/api/logout", {});
  writeSession(TAB_STORAGE_KEY, null);
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

function runningCount() {
  if (!state.snapshot) return null;
  return state.snapshot.links.filter(x => x.running === true).length;
}

function updateBadge() {
  const running = runningCount();
  document.querySelectorAll(".js-dl-badge").forEach(b => {
    b.hidden = !running;
    b.textContent = running ? String(running) : "";
  });
  const label = running ? "ダウンロード（実行中 " + running + "件）" : "ダウンロード";
  $("dlButton").setAttribute("aria-label", label);
  $("dlButton").title = running ? label + " ※最後に取得した一覧の件数" : "ダウンロード";
}

function downloadCard(item, packageItem) {
  const card = element("article", undefined, "download-card " + itemState(item));
  card.appendChild(element("strong", item.name, "download-title"));
  const parts = [];
  if (!packageItem && item.host) parts.push("ホスト: " + item.host);
  if (packageItem && item.child_count !== null && item.child_count !== undefined) {
    parts.push("ファイル: " + item.child_count + "件");
  }
  const label = item.finished === true ? "完了" : item.running === true ? "ダウンロード中" :
    item.enabled === false ? "無効" : "待機/確認中";
  parts.push(label);
  if (item.status) parts.push(item.status);
  card.appendChild(element("p", parts.join(" / "), "download-detail"));
  if (item.progress !== null && item.progress !== undefined) {
    const track = element("div", undefined, "progress-track");
    track.setAttribute("role", "progressbar");
    track.setAttribute("aria-valuemin", "0");
    track.setAttribute("aria-valuemax", "100");
    track.setAttribute("aria-valuenow", String(item.progress));
    track.setAttribute("aria-label", item.name + " 進捗");
    const fill = element("div", undefined, "progress-fill");
    fill.style.width = item.progress + "%";
    track.appendChild(fill);
    card.appendChild(track);
  }
  card.appendChild(element("p",
    "転送: " + readableSize(item.bytes_loaded) + " / " + readableSize(item.bytes_total) +
    " · 速度: " + readableSize(item.speed_bytes) + "/秒 · 残り: " + readableEta(item.eta_seconds),
    "download-detail"));
  return card;
}

function matchesFilter(item) {
  if (state.filter !== "all" && itemState(item) !== state.filter) return false;
  const q = state.query.trim().toLowerCase();
  return !q || String(item.name).toLowerCase().includes(q);
}

// Pure client-side rendering of the last fetched snapshot (filter + search).
function renderDownloads() {
  const snap = state.snapshot;
  const packages = $("downloadPackages"), links = $("downloadLinks"), metrics = $("downloadsSummary");
  packages.replaceChildren(); links.replaceChildren(); metrics.replaceChildren();
  if (!snap) return;
  const shownPackages = snap.packages.filter(matchesFilter);
  const shownLinks = snap.links.filter(matchesFilter);
  for (const item of shownPackages) packages.appendChild(downloadCard(item, true));
  for (const item of shownLinks) links.appendChild(downloadCard(item, false));
  const filtering = state.filter !== "all" || state.query.trim() !== "";
  if (!shownPackages.length) {
    packages.appendChild(element("p", filtering && snap.packages.length ?
      "条件に一致するパッケージはありません。" : "パッケージが見つかりません。", "empty-state"));
  }
  if (!shownLinks.length) {
    links.appendChild(element("p", filtering && snap.links.length ?
      "条件に一致するファイルはありません。" : "ファイルが見つかりません。", "empty-state"));
  }
  const counts = {running: 0, waiting: 0, finished: 0};
  for (const item of snap.links) counts[itemState(item)]++;
  for (const [title, value] of [["表示ファイル", snap.links.length + "件"], ["実行中", counts.running + "件"],
      ["待機", counts.waiting + "件"], ["完了", counts.finished + "件"]]) {
    const box = element("div", undefined, "metric");
    box.appendChild(element("span", title));
    box.appendChild(element("strong", value));
    metrics.appendChild(box);
  }
}

async function loadDownloads(page, options = {}) {
  if (state.downloadsBusy) return;   // never overlap requests
  state.downloadsBusy = true;
  $("downloadsRefresh").disabled = true;
  $("downloadsMessage").textContent = "JD2から状態を取得中… ダウンロード操作は実行しません。";
  try {
    const snap = await api("/api/jd2/downloads?page=" + page);
    state.snapshot = snap;
    state.snapshotAt = Date.now();
    state.downloadsPage = snap.page;
    renderDownloads();
    updateBadge();
    $("downloadsMessage").textContent = "JD2から取得しました（読み取り専用）。一覧は最大" + PAGE_SIZE +
      "件ずつ表示します。" + (options.auto ? "（自動更新）" : "");
    $("downloadsPage").textContent = (snap.page + 1) + "ページ目";
    $("downloadsPrev").disabled = snap.page === 0;
    $("downloadsNext").disabled = !snap.has_more || snap.page >= 20;
    if (state.route.view === "home" || currentViewName() === "home") renderStatusCards();
  } catch (error) {
    let message = "取得できませんでした: " + error.message + "。設定でMyJDownloader接続を確認してください。";
    if (state.autoRefresh) {
      setAutoRefresh(false);
      message += " 自動更新を停止しました。";
    }
    $("downloadsMessage").textContent = message;
    $("downloadsPrev").disabled = true;
    $("downloadsNext").disabled = true;
  } finally {
    state.downloadsBusy = false;
    $("downloadsRefresh").disabled = false;
    syncAutoRefresh();
  }
}

function autoRefreshAllowed() {
  return state.autoRefresh && state.authed && state.route.view === "downloads" &&
    document.visibilityState === "visible";
}

// Opt-in browser-side refresh: only while the downloads view is shown and the
// page is visible. One request at a time; stops itself on any error.
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

function setAutoRefresh(on) {
  state.autoRefresh = on;
  $("downloadsAuto").checked = on;
  writeLocal(AUTO_STORAGE_KEY, on ? "1" : "0");
  syncAutoRefresh();
}

function setFilter(filter) {
  state.filter = filter;
  document.querySelectorAll(".chip").forEach(c =>
    c.setAttribute("aria-pressed", String(c.dataset.filter === filter)));
  renderDownloads();
}

// ----------------------------------------------------------------- links
async function loadQueue() {
  const list = $("queueItems");
  list.replaceChildren();
  const q = await api("/api/links");
  if (!q.items.length) list.appendChild(element("p", "保留中のリンクはありません。", "empty-state"));
  for (const link of q.items) {
    const container = element("div", undefined, "item");
    container.appendChild(element("span", link.url));
    container.appendChild(element("span", link.state === "staged" ? "保留中（自動送信なし）" :
      link.state === "sending" || link.state === "uncertain" ? "送信結果未確認・JD2で確認してください" :
      link.state === "sent" ? "JD2へ送信済み" : "状態: " + link.state));
    if (link.state === "staged") {
      const send = element("button", "JD2に手動で送る", "btn");
      send.type = "button";
      send.addEventListener("click", () => perform(async () => {
        if (!confirm("このリンクをJD2のLinkGrabberへ送ります。自動開始はOFFです。既存のPackagizerルールによりフォルダが変わる可能性があります。続けますか？")) return;
        await api("/api/links/send", {id: link.id});
        await loadQueue();
        flash("JD2のLinkGrabberへ登録しました。処理はまだ開始していません");
      }));
      container.appendChild(send);
    }
    list.appendChild(container);
  }
}

async function previewRapidgator() {
  const content = $("rapidgatorText").value;
  const preview = await api("/api/rapidgator/extract", {content, limit: 20, commit: false});
  state.rapidgatorSnapshot = {content, digest: preview.digest, limit: 20, count: preview.count};
  $("rapidgatorStage").disabled = preview.count === 0;
  const list = $("rapidgatorResult");
  list.replaceChildren();
  list.appendChild(element("p", "Rapidgatorリンク: " + preview.count + "件（未保存・未送信）"));
  for (const url of preview.items) list.appendChild(element("div", url, "item"));
}

async function stageRapidgator() {
  const snapshot = state.rapidgatorSnapshot;
  if (!snapshot || $("rapidgatorText").value !== snapshot.content) {
    flash("貼り付け内容が変化しました。再抽出してください");
    return;
  }
  if (!confirm(snapshot.count + "件のリンクをHubの保留キューに保存します。JD2への送信・ダウンロードは開始しません。よろしいですか？")) return;
  $("rapidgatorStage").disabled = true;
  state.rapidgatorSnapshot = null;
  const staged = await api("/api/rapidgator/extract",
    {content: snapshot.content, limit: snapshot.limit, digest: snapshot.digest, commit: true});
  $("rapidgatorResult").replaceChildren(element("p",
    "保留キューへ新規 " + staged.staged + "件を登録しました。JD2へは未送信です。"));
  await loadQueue();
  flash("保存完了。必要なら保留キューから個別にJD2へ送ってください");
}

// -------------------------------------------------------------- settings
async function enterSettings(section) {
  document.querySelectorAll(".settings-section").forEach(n => { n.hidden = n.dataset.section !== section; });
  document.querySelectorAll(".settings-nav a").forEach(a => {
    if (a.dataset.section === section) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
  if (section === "storage") {
    const s = await api("/api/settings");
    $("selectedFolder").textContent = s.download_dir ? "保存先：" + s.download_dir : "保存先：未選択";
    await folders("");
  } else if (section === "jd2") {
    const cfg = await api("/api/jd2/config");
    $("jd2ConfigState").textContent = cfg.configured ?
      "MyJD設定済み / デバイス: " + cfg.device_name +
        " / JD2更新: " + (cfg.can_update ? "明示許可済み" : "無効") +
        " / リンク送信: " + (cfg.can_send ? "明示許可済み" : "無効") :
      "MyJD未設定: " + (cfg.reason || "接続設定を作成してください");
  } else if (section === "bookmarks") {
    await loadBookmarks();
  } else if (section === "updates") {
    await updateStatus();
  } else if (section === "about") {
    await loadStatus();
  }
}

async function folders(path) {
  const data = await api("/api/folders" + (path ? "?path=" + encodeURIComponent(path) : ""));
  const picker = $("folderPicker");
  picker.replaceChildren();
  if (path) {
    picker.appendChild(element("p", "表示フォルダ: " + data.path, "path"));
    const select = element("button", "このフォルダを保存する", "btn primary");
    select.type = "button";
    select.disabled = !data.writable;
    select.addEventListener("click", () => perform(async () => {
      const v = await api("/api/settings/folder", {path: data.path});
      $("selectedFolder").textContent = "保存先：" + v.download_dir;
      flash("保存先を永続保存しました");
    }));
    picker.appendChild(select);
    const back = element("button", "← 共有フォルダ一覧", "btn");
    back.type = "button";
    back.addEventListener("click", () => perform(() => folders("")));
    picker.appendChild(back);
  }
  for (const child of data.items) {
    const btn = element("button", undefined, "folder");
    btn.type = "button";
    btn.appendChild(icon("folder"));
    btn.appendChild(element("span", child.name + " " + (child.writable ? "" : "(読取のみ)")));
    btn.addEventListener("click", () => perform(() => folders(child.path)));
    picker.appendChild(btn);
  }
}

async function updateStatus() {
  const s = await api("/api/update/status");
  $("updateStatus").textContent = "更新状態: " + s.phase + " — " + (s.message || s.unsupported_reason || "待機中");
  $("updateApply").disabled = Boolean(s.busy) || !s.supported;
  const jd = await api("/api/jd2/status");
  $("jd2Status").textContent = jd.connected ?
    "JD2接続中 / Core revision: " + (jd.revision || "不明") + " / 更新: " + (jd.update_available ? "あり" : "なし") :
    "JD2未接続: " + (jd.reason || "接続アダプタの構成待ち");
  $("jd2Check").disabled = !jd.connected || !jd.can_update;
  $("jd2Update").disabled = !jd.connected || !jd.can_update || !jd.update_available;
  $("jd2Preset").disabled = !jd.connected || !jd.can_preset;
}

async function checkUpdate() {
  const channel = $("updateChannel").value;
  const u = await api("/api/update/check?channel=" + encodeURIComponent(channel));
  state.selectedVersion = u.available ? u.release.version : "";
  state.selectedChannel = channel;
  $("updateResult").textContent = u.available ?
    "更新あり: " + u.release.version + (u.release.version.includes("-rc") ? "（RC版）" : "（正式版）") :
    (channel === "rc" ? "新しい正式版・RC版のQPKGはありません" : "新しい正式版のQPKGはありません");
  setShown("updateApply", Boolean(u.available));
  await updateStatus();
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
  await updateStatus();
  flash("推奨設定を再適用し、再読込で確認しました");
}

// ------------------------------------------------------------ session / boot
function teardownSession() {
  state.authed = false;
  state.shellReady = false;
  clearTimeout(state.autoTimer);
  state.autoTimer = null;
  for (const tab of state.tabs) tab.token++;
  $("frames").replaceChildren();   // unload every external page
  state.tabs = [];
  state.closed = [];
  state.lastWebTabId = null;
  state.snapshot = null;
  state.snapshotAt = 0;
  state.bookmarks = [];
  state.status = null;
  resetExtraction();
  $("rapidgatorText").value = "";
  closeMenu(false);
  closeSwitcher(false);
  updateBadge();
}

async function go() {
  try {
    const session = await api("/api/session");
    state.csrf = session.csrf || "";
    if (session.authenticated) {
      state.authed = true;
      setShown("authArea", false);
      setShown("app", true);
      if (!state.shellReady) {
        state.shellReady = true;
        restoreTabs();
        state.autoRefresh = readLocal(AUTO_STORAGE_KEY) === "1";
        $("downloadsAuto").checked = state.autoRefresh;
      }
      loadBookmarks().catch(() => {});
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
  document.addEventListener("keydown", onKeydown);
  document.addEventListener("click", event => {
    if (!$("menu").hidden && !event.target.closest(".menu-wrap")) closeMenu(false);
  });
  $("tabstrip").addEventListener("keydown", onTabstripKeydown);
  $("newTab").addEventListener("click", createBlankTab);
  $("homeOpenWeb").addEventListener("click", focusOmnibox);

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
    else if (state.route.view === "downloads") loadDownloads(state.downloadsPage);
    else perform(() => enterView(state.route));
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
  $("downloadsRefresh").addEventListener("click", () => loadDownloads(state.downloadsPage));
  $("downloadsPrev").addEventListener("click", () => loadDownloads(state.downloadsPage - 1));
  $("downloadsNext").addEventListener("click", () => loadDownloads(state.downloadsPage + 1));
  $("downloadsAuto").addEventListener("change", () => setAutoRefresh($("downloadsAuto").checked));
  document.querySelectorAll(".chip").forEach(c => c.addEventListener("click", () => setFilter(c.dataset.filter)));
  $("downloadsSearch").addEventListener("input", () => {
    state.query = $("downloadsSearch").value;
    renderDownloads();
  });
}

function wireLinks() {
  $("queueAdd").addEventListener("click", () => perform(async () => {
    await api("/api/links", {url: $("queueUrl").value.trim()});
    $("queueUrl").value = "";
    await loadQueue();
  }));
  $("rapidgatorText").addEventListener("input", resetExtraction);
  $("rapidgatorPreview").addEventListener("click", () => perform(previewRapidgator));
  $("rapidgatorStage").addEventListener("click", () => perform(stageRapidgator));
}

function wireSettings() {
  $("folderRoots").addEventListener("click", () => perform(() => folders("")));
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
      "。これは累積値です。JD2や他QPKGのI/Oは含みません。" :
      "この環境では/proc/self/ioを読み取れません。";
  }));
  $("diagnosticJd2").addEventListener("click", () => perform(async () => {
    const data = await api("/api/jd2/local-preflight");
    $("diagnosticResult").textContent = (data.tcp_reachable ?
      "NASの127.0.0.1:3128にTCP接続できました。" : "NASの127.0.0.1:3128に接続できません。") +
      " JD2本体の確認・LAN隔離検証・操作の許可はまだ行っていません。";
  }));
  $("jd2TestConnection").addEventListener("click", () => perform(async () => {
    $("jd2TestResult").textContent = "JD2の認証済み接続を確認しています。操作や更新は行いません。";
    try {
      const response = await api("/api/jd2/connect/test", {});
      $("jd2TestResult").textContent = "接続できました。Core revision " + response.revision +
        " / 更新: " + (response.update_available ? "あり" : "なし");
    } catch (err) {
      $("jd2TestResult").textContent = "接続できませんでした: " + err.message;
    }
  }));
  $("updateLogButton").addEventListener("click", () => perform(async () => {
    const result = await api("/api/update/log");
    $("updateLog").textContent = result.log || "まだ更新ログはありません";
    setShown("updateLog", true);
  }));
  $("updateChannel").addEventListener("change", () => {
    state.selectedVersion = "";
    state.selectedChannel = $("updateChannel").value;
    $("updateResult").textContent = "チャンネルを変更しました。更新を再確認してください。";
    setShown("updateApply", false);
  });
  $("updateCheck").addEventListener("click", () => perform(checkUpdate));
  $("jd2Check").addEventListener("click", () => perform(async () => {
    const result = await api("/api/jd2/update/check", {});
    await updateStatus();
    flash(result.update_available ? "JD2の更新があります" : "JD2に新しい更新はありません");
  }));
  $("jd2Update").addEventListener("click", () => perform(async () => {
    if (!confirm("JDownloader 2本体だけを更新します。JD2のダウンロードは一時中断される場合があります。よろしいですか？")) return;
    await api("/api/jd2/update/apply", {confirmed: true});
    await updateStatus();
  }));
  $("jd2Preset").addEventListener("click", () => perform(applyPreset));
  $("updateApply").addEventListener("click", () => perform(async () => {
    if (!state.selectedVersion || !confirm(state.selectedVersion + " にQPKGを更新します。サービスが再起動されます。続けますか？")) return;
    await api("/api/update/apply", {version: state.selectedVersion, channel: state.selectedChannel});
    await updateStatus();
  }));
}

document.addEventListener("DOMContentLoaded", () => {
  const plainLan = location.protocol === "http:" &&
    !["localhost", "127.0.0.1", "::1", "[::1]"].includes(location.hostname);
  setShown("lanSecurityWarning", plainLan);
  wireAuth();
  wireShell();
  wireDownloads();
  wireLinks();
  wireSettings();
  go();
});
