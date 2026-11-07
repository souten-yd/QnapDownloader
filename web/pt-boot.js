"use strict";
// v0.1.57: bootstrap page of the passthrough origin (/pt/enter). The ticket was redeemed and the cookie set by the
// server; this registers the Service Worker, waits until it is active, then goes to the proxied page. A navigation inside
// the worker's scope is handled by it even if this page is not controlled yet.
(function () {
  var script = document.currentScript;
  var target = script && script.getAttribute("data-target");
  var message = document.getElementById("m");
  function fail(text) { if (message) message.textContent = text; }
  function go() { location.replace(target); }
  if (!target || target.indexOf("/p/") !== 0) { fail("開く先が不正です"); return; }
  if (!("serviceWorker" in navigator)) { fail("このブラウザではService Workerが使えません（HTTPSで開いていますか？）"); return; }
  navigator.serviceWorker.register("/pt-sw.js", {scope: "/"}).then(function () {
    return navigator.serviceWorker.ready;
  }).then(function () {
    if (navigator.serviceWorker.controller) { go(); return; }
    var timer = setTimeout(go, 1500);
    navigator.serviceWorker.addEventListener("controllerchange", function () { clearTimeout(timer); go(); }, {once: true});
  }).catch(function (error) {
    fail("Service Workerを登録できません: " + (error && error.message ? error.message : error));
  });
})();
