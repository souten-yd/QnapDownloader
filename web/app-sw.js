"use strict";
// v0.1.60: the APP shell cache (the app origin only; the passthrough origin has its own pt-sw.js).
// The Hub serves this file with __QD_VERSION__ replaced by its version, so every release is a new Service Worker
// with its own cache (qd-shell-<version>); the old ones are deleted on activation.
//
// What is cached: exactly the static shell below, nothing else. Never /api/*, mirror documents / resources, the
// passthrough listener or any other path: those requests are not handled here at all (the browser goes to the network).
// Strategy: stale-while-revalidate. The shell answers at once from the cache; a background fetch refreshes the entry
// (so the NEXT launch has the Hub's current file). A release changes this script, which installs a new worker with a
// complete new cache; the app also drops the caches itself when the Hub's version differs from its own (app.js).
var VERSION = "__QD_VERSION__";
var CACHE = "qd-shell-" + VERSION;
var SHELL = ["/", "/app.js", "/style.css", "/icon.svg", "/apple-touch-icon.png", "/icon-192.png"];
var SHELL_SET = {};
SHELL.forEach(function (p) { SHELL_SET[p] = true; });

self.addEventListener("install", function (event) {
  event.waitUntil(caches.open(CACHE).then(function (cache) {
    // "/" and the two bundles are required; the icons are best effort (a development tree has no PNG icons)
    var required = ["/", "/app.js", "/style.css"];
    return Promise.all(SHELL.map(function (path) {
      return fetch(path, {cache: "reload", credentials: "same-origin"}).then(function (res) {
        if (res.ok) return cache.put(path, res);
        if (required.indexOf(path) >= 0) throw new Error("shell file " + path + " answered " + res.status);
      }).catch(function (err) {
        if (required.indexOf(path) >= 0) throw err;
      });
    }));
  }).then(function () { return self.skipWaiting(); }));
});

self.addEventListener("activate", function (event) {
  event.waitUntil(caches.keys().then(function (names) {
    return Promise.all(names.filter(function (n) { return n.indexOf("qd-shell-") === 0 && n !== CACHE; })
      .map(function (n) { return caches.delete(n); }));
  }).then(function () { return self.clients.claim(); }));
});

function shellPath(request) {
  if (request.method !== "GET") return "";
  var url = new URL(request.url);
  if (url.origin !== self.location.origin) return "";
  if (url.pathname === "/") return request.mode === "navigate" || !url.search ? "/" : "";
  return SHELL_SET[url.pathname] && !url.search ? url.pathname : "";
}

self.addEventListener("fetch", function (event) {
  var path = shellPath(event.request);
  if (!path) return;                       // not the shell (/api/*, mirror, anything else): straight to the network
  event.respondWith(caches.open(CACHE).then(function (cache) {
    return cache.match(path).then(function (hit) {
      var refresh = fetch(path, {cache: "no-store", credentials: "same-origin"}).then(function (res) {
        if (res.ok && res.status === 200) cache.put(path, res.clone());
        return res;
      });
      if (hit) {
        event.waitUntil(refresh.catch(function () { /* offline: keep the cached copy */ }));
        return hit;
      }
      return refresh;                      // nothing cached for this file yet (e.g. a best-effort icon)
    });
  }));
});
