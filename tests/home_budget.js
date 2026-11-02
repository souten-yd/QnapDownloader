// Request-budget harness: runs web/app.js (or $APP_JS) against a stub DOM and a real Hub for 10 s of
// VIRTUAL time on #/home and prints the API requests it made as JSON.
// usage: node home_budget.js <app.js> <baseUrl> <cookie> [hash]
const fs = require("fs");
const vm = require("vm");
const [appJs, base, cookie, hash = "#/home"] = process.argv.slice(2);
const BUDGET_MS = 10000;
const nativeFetch = globalThis.fetch;
const realSetTimeout = setTimeout;

let vnow = 0;
const realNow = Date.now();
Date.now = () => realNow + vnow;
const timers = [];
let timerSeq = 0;
globalThis.setTimeout = (fn, ms = 0, ...args) => { const t = {id: ++timerSeq, at: vnow + Math.max(0, ms), fn, args}; timers.push(t); return t.id; };
globalThis.clearTimeout = id => { const i = timers.findIndex(t => t.id === id); if (i >= 0) timers.splice(i, 1); };
globalThis.setInterval = (fn, ms) => {
  const run = () => { fn(); const t = {id, at: vnow + ms, fn: run, args: []}; timers.push(t); };
  const id = ++timerSeq; timers.push({id, at: vnow + ms, fn: run, args: []}); return id;
};
globalThis.clearInterval = globalThis.clearTimeout;

function stub(store = {}) {
  const target = function () {};
  return new Proxy(target, {
    get(_, name) {
      if (name === Symbol.toPrimitive) return () => "";
      if (name in store) return store[name];
      if (name === "then") return undefined;
      if (name === "dataset" || name === "style") return (store[name] = {});
      if (name === "classList") return {toggle() {}, add() {}, remove() {}, contains: () => false};
      if (name === "children" || name === "childNodes") return [];
      if (name === "length") return 0;
      if (name === "hidden" || name === "open" || name === "checked" || name === "disabled") return false;
      if (name === "value" || name === "textContent" || name === "className") return "";
      if (name === "querySelectorAll") return () => [];
      if (name === "querySelector" || name === "closest") return () => null;
      if (name === "getAttribute") return () => null;
      if (name === "clientWidth" || name === "clientHeight") return 400;
      return stub();
    },
    set(_, name, value) { store[name] = value; return true; },
    apply() { return stub(); },
  });
}

const listeners = {};
const ids = new Map();
const documentStub = {
  visibilityState: "visible", activeElement: stub(), body: stub(),
  getElementById: id => { if (!ids.has(id)) ids.set(id, stub()); return ids.get(id); },
  createElement: () => stub(), createElementNS: () => stub(), createTextNode: () => stub(),
  querySelector: () => null, querySelectorAll: () => [],
  addEventListener: (name, fn) => { (listeners[name] = listeners[name] || []).push(fn); },
};
const locationStub = {hash, protocol: "http:", hostname: "127.0.0.1", origin: base, href: base + "/" + hash};
const memory = () => { const m = new Map(); return {getItem: k => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)), removeItem: k => m.delete(k)}; };
Object.assign(globalThis, {
  document: documentStub, location: locationStub, sessionStorage: memory(), localStorage: memory(),
  history: {replaceState(a, b, h) { if (h) locationStub.hash = h; }, back() {}, forward() {}},
  confirm: () => true, prompt: () => null, requestAnimationFrame: fn => setTimeout(fn, 16),
});
Object.defineProperty(globalThis, "navigator", {value: {}, configurable: true});
globalThis.window = Object.assign(globalThis, {addEventListener() {}, devicePixelRatio: 1});

const requests = [];
let inflight = 0;
globalThis.fetch = (path, opts = {}) => {
  const method = (opts.method || "GET").toUpperCase();
  const entry = {t: vnow, method, path: String(path)};
  requests.push(entry);
  if (/^\/api\/state\?rev=/.test(path) || /\/api\/rb\/(meta|frame)/.test(path)) {
    return new Promise((_, reject) => { if (opts.signal) opts.signal.addEventListener("abort", () => reject(new Error("aborted"))); });
  }
  inflight++;
  const headers = Object.assign({Cookie: cookie, Host: new URL(base).host}, opts.headers || {});
  return nativeFetch(base + path, Object.assign({}, opts, {headers})).finally(() => { inflight--; });
};

const tick = () => new Promise(r => setImmediate(r));
async function settle() {
  for (let i = 0; i < 400; i++) { await tick(); if (inflight === 0 && i > 5) break; if (i > 5) await new Promise(r => realSetTimeout(r, 5)); }
}

process.on("unhandledRejection", e => console.error("unhandled:", e && e.stack || e));
(async () => {
  vm.runInThisContext(fs.readFileSync(appJs, "utf8"), {filename: "app.js"});
  for (const fn of listeners.DOMContentLoaded || []) fn();
  while (true) {
    await settle();
    timers.sort((a, b) => a.at - b.at || a.id - b.id);
    const next = timers[0];
    if (!next || next.at > BUDGET_MS) break;
    timers.shift();
    vnow = Math.max(vnow, next.at);
    try { next.fn(...next.args); } catch (e) { if (!/undici/.test(String(e && e.stack))) console.error("timer error:", e && e.stack); }   // node's own fetch timers share setTimeout
  }
  await settle();
  const api = requests.filter(r => r.path.startsWith("/api/") && r.t <= BUDGET_MS);
  console.log(JSON.stringify({count: api.length, requests: api.map(r => r.method + " " + r.path)}));
  process.exit(0);
})();
