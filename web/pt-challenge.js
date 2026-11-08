"use strict";
// v0.1.74: the page the passthrough frame shows when a site (listed as 「NASブラウザ経由」) asks for a bot check the NAS browser
// has not passed yet. Both buttons ask the APP (parent window, exact origin) to act; this page never talks to the Hub's API.
(function () {
  var script = document.currentScript;
  var app = script.dataset.app || "";
  var message = document.getElementById("msg");
  function say(text) { if (message) message.textContent = text; }
  function ask(type) {
    if (!app || window.parent === window) { say("アプリのパススルー表示から開いたときだけ使えます。"); return; }
    window.parent.postMessage({qdPt: 1, type: type}, app);
  }
  document.getElementById("check").addEventListener("click", function () { ask("nas-check"); });
  document.getElementById("again").addEventListener("click", function () { say("読み込み直しています…"); ask("reload"); });
  window.addEventListener("message", function (event) {
    var m = event.data;
    if (event.origin !== app || event.source !== window.parent || !m || m.qdPtApp !== 1) return;
    if (m.cmd === "challenge-result") say(String(m.text || "").slice(0, 200));
  });
})();
