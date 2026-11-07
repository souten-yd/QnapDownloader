"use strict";
// v0.1.57: injected by the Hub as the first script of every proxied HTML page (origin B). Deliberately small: the
// Service Worker rewrites requests; this keeps navigations inside the frame, reports the page to the app, takes the
// app's back / forward / reload / navigate, and switches off what would leak the phone's own address (WebSocket, WebRTC,
// preconnect hints) or break the setup (a site's own Service Worker).
(function () {
  var script = document.currentScript;
  var APP = (script && script.getAttribute("data-qd-app")) || "";   // the only origin we post to / accept messages from
  if (window.__qdPt) return;
  window.__qdPt = true;
  var B = location.origin;
  var PROXY = /^\/p\/([sh])\/([^\/?#]+)(\/[^?#]*)?/;
  var SCHEME = {s: "https:", h: "http:"};

  function toProxy(abs) {                       // absolute URL object -> "/p/s/host/path?query" or ""
    if ((abs.protocol !== "https:" && abs.protocol !== "http:") || abs.username || abs.password ||
        abs.hostname.indexOf(":") >= 0 || abs.hostname.charAt(0) === "[") return "";
    return "/p/" + (abs.protocol === "https:" ? "s" : "h") + "/" + abs.hostname.replace(/\.$/, "") + (abs.port ? ":" + abs.port : "") +
      (abs.pathname || "/") + abs.search;
  }
  function parsePage() {                        // the real page: {origin, url, host} for the current document
    var m = PROXY.exec(location.pathname + location.search);
    if (!m) return null;
    var hostport = m[2];
    try { hostport = decodeURIComponent(hostport); } catch (e) { return null; }
    var origin = SCHEME[m[1]] + "//" + hostport;
    return {origin: origin, host: hostport.split(":")[0], url: origin + (m[3] || "/") + location.search + location.hash};
  }
  function resolve(value) {
    try { return new URL(String(value), document.baseURI); } catch (e) { return null; }
  }
  function toProxyAbs(value) {                  // what a link / form / window.open target must be inside origin B
    var abs = resolve(value);
    if (!abs) return "";
    if (abs.origin === B) {
      if (PROXY.test(abs.pathname + abs.search) || abs.pathname.indexOf("/pt/") === 0) return abs.href;
      var page = parsePage();                    // "/foo": the page's own host
      return page ? B + toProxy(new URL(page.origin + abs.pathname + abs.search)) + abs.hash : abs.href;
    }
    var path = toProxy(abs);
    return path ? B + path + abs.hash : "";
  }

  // ---- reports to / commands from the app
  function post(message) {
    if (!APP || window.parent === window) return;
    message.qdPt = 1;
    try { window.parent.postMessage(message, APP); } catch (e) { /* the app left */ }
  }
  function report(kind) {
    var page = parsePage();
    post({type: "nav", kind: kind, url: page ? page.url : location.href, title: String(document.title || "").slice(0, 200)});
  }
  function go(url) { location.assign(url); }

  window.addEventListener("message", function (event) {
    var m = event.data;
    if (!APP || event.origin !== APP || event.source !== window.parent || !m || m.qdPtApp !== 1) return;
    if (m.cmd === "back") history.back();
    else if (m.cmd === "forward") history.forward();
    else if (m.cmd === "reload") location.reload();
    else if (m.cmd === "stop") { try { window.stop(); } catch (e) { /* ignore */ } }
    else if (m.cmd === "navigate" && typeof m.url === "string") {
      var abs = resolve(m.url), path = abs ? toProxy(abs) : "";
      if (path) go(B + path + abs.hash);
    }
  });

  var wrap = function (name, kind) {
    var original = history[name];
    history[name] = function (state, title, url) {
      var mapped = url;
      if (url !== undefined && url !== null) {
        var abs = resolve(url);
        var page = parsePage();
        if (abs && (abs.origin === B || (page && abs.origin === page.origin))) mapped = toProxyAbs(abs.href);
      }
      var result = original.call(this, state, title, mapped);
      setTimeout(function () { report(kind); }, 0);
      return result;
    };
  };
  wrap("pushState", "push");
  wrap("replaceState", "replace");
  window.addEventListener("popstate", function () { report("pop"); });
  window.addEventListener("hashchange", function () { report("hash"); });
  window.addEventListener("beforeunload", function () { post({type: "loading"}); });
  window.addEventListener("DOMContentLoaded", function () {
    report("load");
    var title = document.querySelector("title");
    if (title && window.MutationObserver) new MutationObserver(function () { report("title"); }).observe(title, {childList: true});
  });
  window.addEventListener("load", function () { report("load"); });
  post({type: "hello"});

  // ---- keep navigation inside the frame (new windows would escape the app)
  document.addEventListener("click", function (event) {
    if (event.defaultPrevented || event.button) return;
    var node = event.target;
    var a = node && node.closest ? node.closest("a[href],area[href]") : null;
    if (!a || a.hasAttribute("download")) return;
    var abs = resolve(a.href);
    if (!abs || (abs.protocol !== "http:" && abs.protocol !== "https:")) return;
    var mapped = toProxyAbs(abs.href);
    var outside = abs.origin !== B;
    var elsewhere = a.target && a.target !== "_self" && a.target !== "_top" && a.target !== "_parent";
    if (!mapped || (!outside && !elsewhere)) return;
    event.preventDefault();
    go(mapped);
  }, true);
  document.addEventListener("submit", function (event) {
    var form = event.target, submitter = event.submitter;
    if (!form || form.nodeType !== 1) return;
    var own = submitter && submitter.hasAttribute && submitter.hasAttribute("formaction");
    var mapped = toProxyAbs(own ? submitter.formAction : form.action);
    if (mapped) (own ? submitter : form).setAttribute(own ? "formaction" : "action", mapped);
    if (form.target && form.target !== "_self") form.target = "_self";
  }, true);
  var nativeSubmit = HTMLFormElement.prototype.submit;
  HTMLFormElement.prototype.submit = function () {
    var mapped = toProxyAbs(this.action);
    if (mapped) this.setAttribute("action", mapped);
    this.target = "_self";
    return nativeSubmit.call(this);
  };
  window.open = function (url) {
    var mapped = url ? toProxyAbs(url) : "";
    if (mapped) go(mapped);
    return null;
  };

  // ---- what would leak the phone's own address or break the setup
  var WS = function PassthroughWebSocket(url) {
    if (!(this instanceof PassthroughWebSocket)) throw new TypeError("Failed to construct 'WebSocket': Please use the 'new' operator");
    var socket = this;
    var target = new EventTarget();
    socket.addEventListener = target.addEventListener.bind(target);
    socket.removeEventListener = target.removeEventListener.bind(target);
    socket.dispatchEvent = target.dispatchEvent.bind(target);
    socket.url = String(url);
    socket.readyState = 0; socket.bufferedAmount = 0; socket.extensions = ""; socket.protocol = ""; socket.binaryType = "blob";
    try { console.warn("[パススルー] WebSocketは使えません（端末のIPが見えるため接続しません）:", String(url).split("?")[0]); } catch (e) { /* ignore */ }
    setTimeout(function () {
      socket.readyState = 3;
      var err = new Event("error");
      socket.dispatchEvent(err);
      if (typeof socket.onerror === "function") socket.onerror(err);
      var close = new CloseEvent("close", {code: 1006, reason: "passthrough", wasClean: false});
      socket.dispatchEvent(close);
      if (typeof socket.onclose === "function") socket.onclose(close);
    }, 0);
  };
  WS.CONNECTING = 0; WS.OPEN = 1; WS.CLOSING = 2; WS.CLOSED = 3;
  WS.prototype.send = function () { throw new DOMException("WebSocket is not open", "InvalidStateError"); };
  WS.prototype.close = function () { /* nothing is open */ };
  try { Object.defineProperty(window, "WebSocket", {value: WS, configurable: true, writable: true}); } catch (e) { /* ignore */ }
  ["RTCPeerConnection", "webkitRTCPeerConnection", "RTCDataChannel"].forEach(function (name) {
    try { Object.defineProperty(window, name, {value: undefined, configurable: true, writable: true}); } catch (e) { /* ignore */ }
  });
  if (navigator.serviceWorker && window.ServiceWorkerContainer) {
    var blocked = function () { return Promise.reject(new DOMException("Service Worker registration is not available in passthrough", "SecurityError")); };
    var P = ServiceWorkerContainer.prototype;
    try {
      Object.defineProperty(P, "register", {value: blocked, configurable: true, writable: true});
      Object.defineProperty(P, "getRegistrations", {value: function () { return Promise.resolve([]); }, configurable: true, writable: true});
      Object.defineProperty(P, "getRegistration", {value: function () { return Promise.resolve(undefined); }, configurable: true, writable: true});
      Object.defineProperty(ServiceWorkerRegistration.prototype, "unregister", {value: function () { return Promise.resolve(false); }, configurable: true, writable: true});
    } catch (e) { /* ignore */ }
  }
  // preconnect / dns-prefetch hints open connections without a fetch event: remove them as they appear
  function dropHints(root) {
    var list = root.querySelectorAll ? root.querySelectorAll("link[rel~=preconnect],link[rel~=dns-prefetch]") : [];
    for (var i = 0; i < list.length; i++) list[i].remove();
  }
  if (window.MutationObserver) {
    new MutationObserver(function (records) {
      for (var i = 0; i < records.length; i++) {
        var added = records[i].addedNodes;
        for (var j = 0; j < added.length; j++) {
          var n = added[j];
          if (n.nodeType !== 1) continue;
          if (n.matches && n.matches("link[rel~=preconnect],link[rel~=dns-prefetch]")) n.remove(); else dropHints(n);
        }
      }
    }).observe(document, {childList: true, subtree: true});
  }

  // ---- v0.1.60: tell the app how far the page is scrolled (it collapses / restores its bars from this). One small
  // message per animation frame while scrolling; nothing is sent while the page is still.
  var scrollTick = 0;
  window.addEventListener("scroll", function () {
    if (scrollTick || window.parent === window) return;
    scrollTick = requestAnimationFrame(function () {
      scrollTick = 0;
      var el = document.scrollingElement || document.documentElement;
      post({type: "scroll", top: Math.round(el.scrollTop), max: Math.round(el.scrollHeight - el.clientHeight)});
    });
  }, {passive: true});

  // ---- a quick sideways flick goes back / forward (the app has the history); same rules as the app's own
  var start = null, EDGE = 24;
  function scrollsSideways(node) {
    for (var el = node; el && el.nodeType === 1; el = el.parentElement) {
      if (el === document.body || el === document.documentElement) break;
      if (el.scrollWidth > el.clientWidth + 2) {
        var ox = getComputedStyle(el).overflowX;
        if (ox === "auto" || ox === "scroll" || ox === "overlay") return true;
      }
    }
    return false;
  }
  document.addEventListener("touchstart", function (event) {
    start = null;
    if (event.touches.length !== 1) return;
    var t = event.touches[0];
    if (t.clientX < EDGE || t.clientX > window.innerWidth - EDGE || scrollsSideways(event.target)) return;
    start = {x: t.clientX, y: t.clientY, at: Date.now()};
  }, {passive: true});
  document.addEventListener("touchmove", function (event) { if (event.touches.length !== 1) start = null; }, {passive: true});
  document.addEventListener("touchend", function (event) {
    var s = start; start = null;
    if (!s || event.changedTouches.length !== 1) return;
    var t = event.changedTouches[0], dx = t.clientX - s.x, dy = t.clientY - s.y;
    if (Date.now() - s.at > 450 || Math.abs(dx) < 90 || Math.abs(dx) < 2.5 * Math.abs(dy)) return;
    post({type: "flick", dir: dx > 0 ? -1 : 1});
  }, {passive: true});
})();
