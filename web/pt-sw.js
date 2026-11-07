"use strict";
// v0.1.57: Service Worker of the passthrough origin (B). It sends EVERY network request of a controlled page through
// the Hub: same-origin paths that are not /p/ or /pt/ (a page's "/api/x") become /p/<scheme>/<page host>/api/x, and any
// other origin ("https://cdn.example.net/x.js") becomes /p/s/cdn.example.net/x.js. The Hub (passthrough.py) makes the
// request from the NAS. The mapping mirrors pt_core.py; tests/test_passthrough.py runs both on the same vectors.
// X-QD-PT-Top (the host of the top-level page) lets the Hub attach a site's cookies only to first-party requests.

const PROXY = /^\/p\/([sh])\/([^\/?#]+)(\/[^?#]*)?/;
const SCHEME = {s: "https", h: "http"};
const OWN = self.location.origin;
const tops = new Map();            // client id -> host of the top-level proxied page (best effort; this worker may be stopped)
const TOPS_MAX = 200;

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", event => event.waitUntil(self.clients.claim()));

// https://host:port/path?query -> /p/s/host:port/path?query, or null (not http(s), IPv6, credentials).
function toProxy(urlString) {
  let u;
  try { u = new URL(urlString); } catch (e) { return null; }
  if (u.protocol !== "https:" && u.protocol !== "http:") return null;
  if (u.username || u.password || u.hostname.indexOf(":") >= 0 || u.hostname.charAt(0) === "[") return null;
  return "/p/" + (u.protocol === "https:" ? "s" : "h") + "/" + u.hostname.replace(/\.$/, "") + (u.port ? ":" + u.port : "") +
    (u.pathname || "/") + u.search;
}

// /p/s/host/path?query -> {scheme, host, hostport, origin, url} or null
function fromProxy(pathAndQuery) {
  const m = PROXY.exec(pathAndQuery);
  if (!m) return null;
  let hostport;
  try { hostport = decodeURIComponent(m[2]); } catch (e) { return null; }
  const host = hostport.split(":")[0].toLowerCase();
  if (!/^[a-z0-9_.-]+$/.test(host)) return null;
  const origin = SCHEME[m[1]] + "://" + hostport;
  return {scheme: SCHEME[m[1]], host: host, hostport: hostport, origin: origin,
          url: origin + (m[3] || "/") + pathAndQuery.slice(m[0].length)};
}

function proxyOfUrl(urlString) {
  try {
    const u = new URL(urlString);
    return u.origin === OWN ? fromProxy(u.pathname + u.search) : null;
  } catch (e) { return null; }
}

function rememberTop(id, host) {
  if (!id || !host) return;
  if (tops.size >= TOPS_MAX) tops.delete(tops.keys().next().value);
  tops.set(id, host);
}

// The proxied page that issued this request: its client's URL, else (navigations) the referrer.
async function baseOf(event) {
  if (event.clientId) {
    const client = await self.clients.get(event.clientId);
    if (client) {
      const hit = proxyOfUrl(client.url);
      if (hit) return {page: hit, clientId: client.id};
    }
  }
  const hit = event.request.referrer ? proxyOfUrl(event.request.referrer) : null;
  return hit ? {page: hit, clientId: ""} : null;
}

function plain(status, text) {
  return new Response("<!doctype html><meta charset=utf-8><title>" + text + "</title><p style=\"font:16px sans-serif;padding:24px\">" +
    text + "</p>", {status: status, headers: {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store"}});
}

async function handle(event) {
  const req = event.request;
  const url = new URL(req.url);
  const nav = req.mode === "navigate";
  let path = null;          // the /p/ (or /pt/) URL to request, relative to origin B
  let own = false;          // already a path of origin B
  let base = null;
  if (url.origin === OWN) {
    if (url.pathname.indexOf("/pt/") === 0 || url.pathname === "/pt-sw.js") {
      path = url.pathname + url.search; own = true;
    } else if (fromProxy(url.pathname + url.search)) {
      path = url.pathname + url.search; own = true;
    } else {
      base = await baseOf(event);            // "/api/x": the page's own host
      if (!base) return nav ? plain(404, "このページの元のサイトを特定できませんでした。アプリから開き直してください。") : Response.error();
      path = "/p/" + (base.page.scheme === "https" ? "s" : "h") + "/" + base.page.hostport + url.pathname + url.search;
    }
  } else {
    path = toProxy(req.url);
    if (!path) return fetch(req);            // data:, blob:, ... never reach here for http(s)
  }
  // The top-level site of the requesting page, for the first-party cookie rule.
  const target = fromProxy(path);
  let top = target ? target.host : "";
  if (nav) {
    if (event.clientId) {                     // a frame inside a proxied page: its top is the parent's top
      const parent = await self.clients.get(event.clientId);
      const parentPage = parent ? proxyOfUrl(parent.url) : null;
      if (parentPage) top = tops.get(parent.id) || parentPage.host;
    }
    rememberTop(event.resultingClientId, top);
  } else if (event.clientId) {
    if (!base) base = await baseOf(event);
    top = tops.get(event.clientId) || (base ? base.page.host : top);
  }
  if (nav && !own) {
    // A navigation to "/foo" must end up at /p/.../foo so the document URL resolves its relative links correctly.
    return Response.redirect(path, req.method === "GET" || req.method === "HEAD" ? 302 : 307);
  }
  const headers = new Headers(req.headers);
  headers.set("X-QD-PT-Top", top);
  headers.set("X-QD-PT-Dest", req.destination || "");
  if (nav) {
    return fetch(new Request(path, {method: req.method, headers: headers, body: req.method === "GET" || req.method === "HEAD" ? undefined : await req.arrayBuffer(),
      credentials: "same-origin", redirect: "manual", referrer: req.referrer, referrerPolicy: req.referrerPolicy}));
  }
  const init = {method: req.method, headers: headers, credentials: "same-origin", mode: "same-origin",
    redirect: req.redirect === "manual" ? "manual" : "follow", cache: req.cache === "only-if-cached" ? "default" : req.cache,
    referrer: req.referrer, referrerPolicy: req.referrerPolicy, keepalive: req.keepalive};
  if (req.method !== "GET" && req.method !== "HEAD") init.body = await req.arrayBuffer();
  return fetch(new Request(path, init));
}

self.addEventListener("fetch", event => {
  let url;
  try { url = new URL(event.request.url); } catch (e) { return; }
  if (url.protocol !== "https:" && url.protocol !== "http:") return;          // blob:, data:, ...
  event.respondWith(handle(event).catch(() => plain(502, "パススルー経由の読み込みに失敗しました。")));
});
