"use strict";
// v0.1.57: the download page of the passthrough origin. The JD2 button asks the APP (parent window, exact origin) to
// confirm and hand the link over; this page never talks to the Hub's API itself.
(function () {
  var script = document.currentScript;
  var d = script.dataset;
  var app = d.app || "";
  var button = document.getElementById("jd2");
  var message = document.getElementById("msg");
  function say(text) { if (message) message.textContent = text; }
  button.addEventListener("click", function () {
    if (!app || window.parent === window) { say("アプリのパススルー表示から開いたときだけ使えます。"); return; }
    window.parent.postMessage({qdPt: 1, type: "download", id: d.id, name: d.name, size: Number(d.size) || 0, host: d.host}, app);
    say("アプリで確認してください…");
  });
  window.addEventListener("message", function (event) {
    var m = event.data;
    if (event.origin !== app || event.source !== window.parent || !m || m.qdPtApp !== 1) return;
    if (m.cmd === "download-result" && m.id === d.id) say(String(m.text || "").slice(0, 200));
  });
})();
