"""Static checks for the browser-style shell (web/). Stdlib only."""
import re
import shutil
import subprocess
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"
FORBIDDEN_SANDBOX = ("allow-popups", "allow-top-navigation",
                     "allow-downloads", "allow-modals")
ORIGIN_TOKEN = "allow-same-origin"
# v0.1.57: the passthrough display's one frame. Its content is the cross-origin passthrough listener (never the Hub's own
# origin), so scripts run there but without top navigation, popups or the Hub's cookies. This exact attribute, once, in
# index.html, is the only sandbox allowed; every other use of these tokens still fails.
PT_SANDBOX = 'sandbox="allow-scripts allow-same-origin allow-forms allow-modals allow-downloads"'


def without_pt_sandbox(path, body):
    if path.name == "index.html":
        assert body.count(PT_SANDBOX) == 1, "the passthrough frame's sandbox must appear exactly once"
        return body.replace(PT_SANDBOX, "")
    return body


def text_files():
    return [p for p in WEB.iterdir() if p.suffix in {".html", ".js", ".css", ".svg"}]


class WebShellStaticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = (WEB / "app.js").read_text(encoding="utf-8")
        cls.html = (WEB / "index.html").read_text(encoding="utf-8")

    def test_engine_section_shows_mode_migration_and_container_station_hint(self):
        for ident in ("engineMode", "engineMigrate", "engineNoContainer", "engineSetupNative"):
            self.assertIn('id="%s"' % ident, self.html)
        self.assertIn("QTSのApp CenterでContainer Stationをインストールしてください", self.html)
        self.assertNotIn("engineNoContainerLink", self.html + self.js)
        self.assertNotIn(":8080", self.js)
        for text in ("コンテナ (Container Station)", "QTS直接実行", "コンテナへ移行",
                     "初期設定を開始"):
            self.assertIn(text, self.js + self.html)
        self.assertIn('mode: "container"', self.js)
        self.assertIn("startEngineSetup(\"native\")", self.js)

    def test_forbidden_sandbox_tokens_absent_everywhere(self):
        for path in text_files():
            body = without_pt_sandbox(path, path.read_text(encoding="utf-8"))
            for token in FORBIDDEN_SANDBOX:
                with self.subTest(file=path.name, token=token):
                    self.assertNotIn(token, body)

    def test_v0135_iframe_modes_are_gone(self):
        # The page itself is never framed. Since v0.1.39 the only iframe is the Hub-made mirror copy:
        # sandbox exactly allow-same-origin (no script, form, popup or navigation permission).
        for path in text_files():
            body = without_pt_sandbox(path, path.read_text(encoding="utf-8"))
            for token in ("SANDBOX_", "sandboxFor", "mountFrame", "compatAllowedFor",
                          "COMPAT_CONFIRM", "MODE_LABELS", "LEGACY_EXTERNAL_MODE", "enforceLiveLimit",
                          "MAX_LIVE_FRAMES", "setMode(", "normalizeMode", "applyHostMode", "openInTab",
                          "leaveRemote", "updateCfHint", "syncFrameVisibility", "allow-scripts"):
                with self.subTest(file=path.name, token=token):
                    self.assertNotIn(token, body)
        self.assertEqual(self.html.count("<iframe"), 2)             # v0.1.57: + the passthrough frame (ptFrame)
        # v0.1.41: no sandbox attribute (WebKit would drop the app's listeners); the document's own
        # CSP (default-src 'none') keeps every script out instead
        self.assertIn('<iframe id="mirrorFrame" class="mirror-frame" title=', self.html)
        self.assertEqual(len(re.findall(r'sandbox="', self.html)), 1)          # v0.1.57: only the passthrough frame
        self.assertIn('<iframe id="ptFrame" class="mirror-frame" title="パススルー表示のページ" referrerpolicy="no-referrer" ' + PT_SANDBOX, self.html)
        self.assertNotRegex(self.js, r"createElement\([\"']iframe[\"']\)")
        self.assertNotIn('setAttribute("sandbox"', self.js)
        self.assertNotIn(".sandbox", self.js)
        self.assertNotIn("srcdoc", self.js)
        self.assertNotRegex(self.js, r"const MODES\b")
        for ident in ("cfHint", "cfCompat", "cfRemote", "cfDismiss", "frames", "menuMode", "menuCompat",
                      "menuRemote", "remoteReopen"):
            with self.subTest(ident=ident):
                self.assertNotIn('id="%s"' % ident, self.html)
                self.assertNotIn('$("%s")' % ident, self.js)
        for text in ("強い制限", "限定操作", "互換モード", "互換で再読み込み", "表示されない場合"):
            for path in text_files():
                with self.subTest(file=path.name, text=text):
                    self.assertNotIn(text, path.read_text(encoding="utf-8"))
        css = (WEB / "style.css").read_text(encoding="utf-8")
        for selector in (".cf-hint", ".web-frame", ".frames", ".mode-badge.interactive", ".mode-badge.compat"):
            self.assertNotIn(selector, css)
        self.assertRegex(self.js, r"MAX_HOST_MODES\s*=\s*100\b")
        self.assertIn('name="mobile-web-app-capable"', self.html)

    def test_v0139_mirror_static_contract(self):
        self.assertIn('data-action="display:mirror"', self.html)
        self.assertIn(">ミラー</button>", self.html)
        self.assertIn("ミラー", self.html[self.html.index("rbDefaultMode"):])
        self.assertIn('content="', self.html)
        start = self.js.index("// ---- mirror display (v0.1.39) ----")
        block = self.js[start:start + 60000]
        for bad in ("innerHTML", "outerHTML", "document.write", "eval(", "srcdoc", "insertAdjacentHTML"):
            with self.subTest(bad=bad):
                self.assertNotIn(bad, block)
        self.assertIn("要素 ", self.js)
        self.assertIn("/api/rb/mirror/doc", self.js)

    def test_v0135_every_url_opens_in_the_nas_browser(self):
        # typed / bookmarked / restored URLs all go through openRemote after /api/browser/validate
        self.assertIn("validateUrl", self.js)
        self.assertIn("/api/browser/validate", self.js)
        self.assertIn("const navigateTab = (tab, value) => openRemote(tab, value,", self.js)
        body = self.js[self.js.index("async function openWebUrl(value)"):self.js.index("// -------------------------------------------------------------- omnibox")]
        self.assertIn("openRemote(fresh", body)
        self.assertNotIn("tab.remote", self.js)
        self.assertNotIn("isRemoteHost", self.js)

    def test_v0135_not_ready_state_has_one_button_to_the_browser_settings(self):
        panel = re.search(r'<div id="rbNotReady".*?</div>', self.html, re.S).group(0)
        self.assertIn("NASブラウザが準備できていません", panel)
        self.assertEqual(len(re.findall(r"<button", panel)), 1)
        self.assertIn('id="rbNotReadyBtn"', panel)
        self.assertIn('navigate("#/settings/browser/nas")', self.js)
        # shown when the image is missing (state.rbReady === false) without any request to the Hub
        self.assertIn("state.rbReady === false", self.js)
        gate = self.js[self.js.index("if (state.rbReady === false) {"):]
        self.assertLess(gate.index("tab.notReady = true"), gate.index('api("/api/rb/tabs"'))
        self.assertIn("setRbReady(d.rb.ready)", self.js)

    def test_update_watch_and_stale_page_notice(self):
        self.assertIn("/api/health", self.js)
        self.assertIn("更新中… 完了後に自動で再読み込みします", self.js)
        self.assertIn("location.reload()", self.js)
        self.assertRegex(self.js, r"UPDATE_POLL_MS\s*=\s*2000\b")
        self.assertRegex(self.js, r"UPDATE_WATCH_MS\s*=\s*5 \* 60 \* 1000\b")

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_is_tailscale_host(self):
        match = re.search(r"function isTailscaleHost\(hostname\) \{.*?\n\}\n", self.js, re.S)
        self.assertIsNotNone(match)
        script = match.group(0) + """
const yes = ["100.64.0.1", "100.100.1.2", "100.127.255.254", "myhub.tail1234.ts.net",
  "MyHub.TS.NET", "[fd7a:115c:a1e0::1]", "fd7a:115c:a1e0:ab12::5"];
const no = ["100.63.255.255", "100.128.0.1", "192.168.1.5", "10.0.0.1", "example.com",
  "nots.net", "evilts.net.example.com", "[fd7a:115c:a1e1::1]", "[fd00::1]", "100.64.0", "", "999.64.0.1"];
const bad = yes.filter(h => !isTailscaleHost(h)).concat(no.filter(h => isTailscaleHost(h)));
if (bad.length) { console.error("mismatch: " + bad.join(",")); process.exit(1); }
"""
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_no_dynamic_html_or_code_execution(self):
        for token in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write",
                      "new Function"):
            with self.subTest(token=token):
                self.assertNotIn(token, self.js)
        self.assertIsNone(re.search(r"\beval\s*\(", self.js))
        self.assertIsNone(re.search(r"set(Timeout|Interval)\s*\(\s*[\"'`]", self.js))

    def test_html_has_no_inline_script_style_or_handlers(self):
        for match in re.finditer(r"<script\b([^>]*)>(.*?)</script>", self.html, re.S):
            self.assertIn("src=", match.group(1))
            self.assertEqual(match.group(2).strip(), "")
        self.assertIsNone(re.search(r"<style\b", self.html))
        self.assertIsNone(re.search(r"\sstyle\s*=", self.html))
        self.assertIsNone(re.search(r"\son\w+\s*=", self.html, re.I))

    def test_image_src_assignments_are_bounded(self):
        self.assertIn("/api/browser/validate", self.js)
        # Exactly eight assignments (v0.1.35: the iframe one is gone; v0.1.37: + the image viewer's <img>): the remote-browser
        # screencast (blob: object URLs made from /api/rb/frame.jpg bytes: an
        # off-screen probe image for decoding, then the visible image), since v0.1.28
        # one tile <img> (blob: object URL of /api/rb/tile.jpg bytes) and, since v0.1.30,
        # one reader <img> (blob: object URL of /api/rb/reader/img bytes) and the two places
        # that point the live <img> at the same-origin MJPEG stream URL.
        # v0.1.44 self-test: + the reader image and the first tile (blob: object URLs of the same Hub bytes, to
        # decode them on this device) and the stream <img> (same-origin MJPEG URL): 12. Its mirror frame is
        # navigated with location.replace of the validated /api/rb/mirror/doc URL, never with a src assignment.
        # v0.1.62: + the open-tab card thumbnail on the browse home (`img.src = src`: a blob: URL of a tile the phone
        # already holds, see tabThumb(); it fetches nothing): 13.
        self.assertEqual(len(re.findall(r"\.src\s*=", self.js)), 13)
        self.assertNotIn("frame.src", self.js)
        self.assertIn("img.src = next;", self.js)

    def test_routes_and_limits(self):
        for route in ("#/downloads", "#/links", "#/settings/"):
            with self.subTest(route=route):
                self.assertIn(route, self.js)
        self.assertRegex(self.js, r"MAX_WEB_TABS\s*=\s*6\b")
        self.assertRegex(self.js, r"MAX_CLOSED_TABS\s*=\s*10\b")

    def test_storage_is_guarded_and_holds_no_secrets(self):
        self.assertIn("sessionStorage", self.js)
        self.assertIn("try", self.js)
        for call in re.finditer(r"(sessionStorage|localStorage)\.(get|set|remove)Item", self.js):
            before = self.js[max(0, call.start() - 80):call.start()]
            self.assertIn("try", before)
        self.assertNotRegex(self.js, r"(?i)(session|local)Storage\.setItem\([^)]*csrf")

    def test_v0130_reader_renderer_builds_dom_without_remote_html(self):
        start = self.js.index("// ---- reader mode (v0.1.30) ----")
        end = self.js.index("// ---- long press: follow the link in a hidden background page")
        reader = self.js[start:end]
        for token in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function",
                      "DOMParser", "createContextualFragment", "location.", "window.open", "srcdoc"):
            self.assertNotIn(token, reader)
        for token in ("document.createElement", "textContent", "createTextNode", "dataset.href"):
            self.assertIn(token, reader)
        self.assertNotRegex(reader, r"(?<!dataset)\.href\s*=(?!=)")       # links never get a real href
        self.assertIn("e.url = URL.createObjectURL(blob);", reader)
        self.assertIn("/api/rb/reader/img?tab=", reader)
        self.assertIn('id="readerScroll"', self.html)
        css = (WEB / "style.css").read_text(encoding="utf-8")
        self.assertRegex(css, r"\.reader-scroll\{[^}]*touch-action:pan-x pan-y pinch-zoom")
        self.assertRegex(css, r"\.reader-scroll\{[^}]*user-select:text")
        self.assertRegex(css, r"\.link-boxes\{[^}]*pointer-events:none")
        for text in ("既定の表示方法", "リンクの枠を表示", "元のページ表示に切替", 'data-display="reader">リーダー'):
            self.assertIn(text, self.html)
        self.assertIn('name="rbDefaultMode"', self.html)

    def test_safari_open_options_are_gone(self):
        for path in text_files():
            if path.parent.name != "web":
                continue
            text = path.read_text(encoding="utf-8")
            with self.subTest(file=path.name):
                self.assertNotIn("Safariで開く", text)
                self.assertNotIn("常にSafari", text)
                self.assertNotIn("externalHosts", text)
                self.assertNotIn("cfSafari", text)
        for token in ("alwaysExternal", "openExternal", "tryExternalOpen", "showExternalToast",
                      "externalSanityUrl", "isExternalHost"):
            self.assertNotIn(token, self.js)
        self.assertNotRegex(self.js, r"\bEXTERNAL_MODE\b")
        self.assertNotIn('data-action="external"', self.html)
        # the iPhone share sheet / bookmarklet settings stay
        self.assertIn('data-sub="share"', self.html)

    def test_stored_host_modes_keep_only_the_display_choices(self):
        if shutil.which("node") is None:
            self.skipTest("node missing")
        pieces = [self.js[self.js.index("function readHostModes() {"):self.js.index("function rememberedMode(")]]
        script = """
const MAX_HOST_MODES = 100, HOST_MODE_KEY = "k";
const REMOTE_FAST_MODE = "remote-fast", REMOTE_LIVE_MODE = "remote-live", REMOTE_READER_MODE = "remote-reader";
const REMOTE_MODES = [REMOTE_FAST_MODE, REMOTE_LIVE_MODE, REMOTE_READER_MODE];
const REMOTE_PT_MODE = "remote-pt";
let store = null, broken = false;
const readLocal = () => { if (broken) throw new Error("x"); return store; };
const writeLocal = (k, v) => { store = v; };
%s
const eq = (a, b, m) => { if (JSON.stringify(a) !== JSON.stringify(b)) { console.error(m + ": " + JSON.stringify(a)); process.exit(1); } };
store = JSON.stringify([["a.example", "external"], ["b.example", "compat"], ["c.example", "remote-live"],
  ["d.example", "bogus"], ["e.example", "strict"], ["f.example", "interactive"], ["g.example", "remote"],
  ["h.example", "remote-reader"], [5, "remote-fast"], "x"]);
eq(readHostModes(), [["c.example", "remote-live"], ["h.example", "remote-reader"]], "old modes are dropped");
migrateHostModes();
eq(JSON.parse(store), [["c.example", "remote-live"], ["h.example", "remote-reader"]], "stored copy rewritten silently");
rememberHostMode("c.example", "");
eq(JSON.parse(store), [["h.example", "remote-reader"]], "an empty mode removes the entry");
rememberHostMode("z.example", "remote-fast");
eq(JSON.parse(store), [["h.example", "remote-reader"], ["z.example", "remote-fast"]], "display choice stored");
rememberHostMode("z.example", "compat");
eq(JSON.parse(store), [["h.example", "remote-reader"]], "a non-display mode is never stored");
// v0.1.62: one entry per site (registrable domain); passthrough is stored too
rememberHostMode("www.shop.example.co.jp", "remote-live");
rememberHostMode("a.shop.example.co.jp", "remote-pt");
eq(JSON.parse(store), [["h.example", "remote-reader"], ["example.co.jp", "remote-pt"]], "one entry per site; passthrough kept");
rememberHostMode("x.example.co.jp", "");
eq(JSON.parse(store), [["h.example", "remote-reader"]], "forgetting a sub-domain removes the site entry");
store = "not json";
eq(readHostModes(), [], "garbage is harmless");
broken = true;
migrateHostModes();
eq(readHostModes(), [], "unreadable storage is harmless");
""" % "\n".join(pieces)
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_v0135_debug_log_client_wiring(self):
        for needle in ("function dlog(category, level, message, data)", "function dlogRate(", "DEBUG_FLUSH_MS = 2000",
                       "DEBUG_BATCH = 100", '"/api/debug/log"', '"/api/debug/config"', '"/api/debug/clear"',
                       '"/api/diag/diskio"', 'window.addEventListener("error"', '"unhandledrejection"',
                       "new Blob([debugJsonl()]", "setDebugState(d.debug)"):
            with self.subTest(needle=needle):
                self.assertIn(needle, self.js)
        # instrumented places
        for needle in ('dlog("ui", "info", "route"', '"display mode"', '"stream open"', '"stream first frame"',
                       '"stream fallback"', '"reader render"', '"tile mount"', '"polling frame rate"'):
            with self.subTest(needle=needle):
                self.assertIn(needle, self.js)
        # nothing is stored on the device and nothing is written by the Hub: the file is a client-side Blob
        self.assertIsNone(re.search(r"(local|session)Storage[^\n]*debug", self.js, re.I))
        self.assertIn("application/x-ndjson", self.js)
        self.assertIn("ログを見る", self.html)
        # the disk table is refreshed by the scheduler only while the diagnostics section is visible
        self.assertIn('sched.add("disk", DISK_REFRESH_MS, diagVisible, loadDisk)', self.js)
        for text in ("1分平均", "10分平均", "合計", "取得できません"):
            self.assertIn(text, self.html + self.js)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_v0135_dlog_is_silent_when_off_filters_and_batches(self):
        block = self.js[self.js.index("const dbgQueue = [];"):self.js.index("function setDebugState(")]
        script = """
const DEBUG_CATEGORIES = ["ui", "render", "net", "rb", "jd2", "engine", "disk", "http", "app"];
const DEBUG_LEVELS = ["debug", "info", "warn", "error"];
const DEBUG_FLUSH_MS = 2000, DEBUG_BATCH = 100, DEBUG_QUEUE_MAX = 300, DEBUG_RATE_MS = 1000;
const state = {authed: true, csrf: "c", debug: {enabled: false, level: "debug", categories: []}};
const timers = []; const posts = [];
global.setTimeout = (fn, ms) => { timers.push({fn, ms}); return timers.length; };
global.clearTimeout = () => {};
global.fetch = async (url, opts) => { posts.push({url, opts, body: JSON.parse(opts.body)}); return {}; };
%s
const eq = (a, b, m) => { if (JSON.stringify(a) !== JSON.stringify(b)) { console.error(m + ": " + JSON.stringify(a)); process.exit(1); } };
(async () => {
  dlog("ui", "info", "off");
  eq([dbgQueue.length, timers.length], [0, 0], "nothing while off");
  state.debug = {enabled: true, level: "info", categories: ["ui", "net"]};
  dlog("ui", "debug", "below level"); dlog("http", "error", "other category"); dlog("bogus", "info", "x");
  eq(dbgQueue.length, 0, "filtered");
  for (let i = 0; i < 250; i++) dlog("ui", "info", "m" + i);
  eq(dbgQueue.length, 250, "queued");
  eq(timers.length, 1, "one flush timer");
  eq(timers[0].ms, 2000, "flush at most every 2 s");
  await flushDebug();
  eq(posts.length, 1, "one post");
  eq(posts[0].url, "/api/debug/log", "url");
  eq(posts[0].body.entries.length, 100, "at most 100 per post");
  eq(posts[0].opts.headers["X-CSRF-Token"], "c", "csrf");
  eq(timers.length, 2, "the rest waits for the next interval");
  dlog("ui", "info", "y".repeat(900));
  eq(dbgQueue[dbgQueue.length - 1].message.length, 500, "message capped");
  dbgQueue.length = 0;
  dlogRate("k", "ui", "info", "first"); dlogRate("k", "ui", "info", "second");
  eq(dbgQueue.map(e => e.message), ["first"], "same key is rate limited");
  for (let i = 0; i < 1000; i++) dlog("ui", "info", "z");
  eq(dbgQueue.length, 300, "queue is bounded");
  state.debug.enabled = false;
  await flushDebug();
  eq(dbgQueue.length, 0, "dropped when switched off");
})().catch(e => { console.error(e.stack); process.exit(1); });
""" % block
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_engine_section_and_alias(self):
        for ident in ("engineSetup", "engineStart", "engineStop", "engineAutostart",
                      "engineSteps", "engineLog", "homeEngineSetup", "downloadsEngineAction",
                      "engineTestResult"):
            self.assertIn('id="%s"' % ident, self.html)
        self.assertIn('data-sub="engine"', self.html)
        self.assertIn("SETTINGS_REDIRECTS", self.js)
        self.assertIn("jd2/engine", self.js)
        self.assertNotIn("ENGINE_POLL_MS", self.js)       # v0.1.33: the state loop replaced the engine poll
        # Log text only via textContent; confirm names sources and sizes.
        self.assertIn('$("engineLog").textContent', self.js)
        for needle in ("installer.jdownloader.org", "github.com/adoptium", "自動開始しません"):
            self.assertIn(needle, self.js)
        self.assertIn("JD2エンジンが起動していません", self.html)

    def test_v0112_removed_controls_and_copy(self):
        # Removed ids: auto-refresh switch, manual connection test, home quick actions.
        for ident in ("downloadsAuto", "engineTest", "homeEngine", "homeOpenWeb", "downloadsEngineNotice",
                      "linksEngineNotice", "queueUrl", "queueAdd", "rapidgatorText", "folderRoots",
                      "engineLogButton", "updateLogButton"):
            with self.subTest(ident=ident):
                self.assertNotIn('id="%s"' % ident, self.html)
        self.assertNotIn("downloadsAuto", self.js)
        # The v0.1.11 localStorage key is only ever removed, never written or read.
        self.assertIn("removeLocal(LEGACY_AUTO_KEY)", self.js)
        self.assertNotIn("writeLocal(LEGACY_AUTO_KEY", self.js)
        self.assertNotIn("readLocal(LEGACY_AUTO_KEY", self.js)
        # Home has no quick-action row / "よく使う操作" / dev-preview notice.
        home = re.search(r'<section id="view-home".*?</section>\s*\n\s*<section id="view-downloads"',
                         self.html, re.S).group(0)
        self.assertNotIn("よく使う操作", home)
        self.assertNotIn("初期開発版", home)
        self.assertIsNone(re.search(r'class="actions"', home))
        self.assertIsNone(re.search(r'href="#/(downloads|links)"[^>]*class="btn', home))
        self.assertIn("URLを入力または貼り付け", self.html)
        # The old queue name is gone from every shipped file.
        for path in text_files():
            with self.subTest(file=path.name):
                self.assertNotIn("保留キュー", path.read_text(encoding="utf-8"))
        self.assertIn("追加したリンク", self.html)

    def test_settings_order_and_auto_refresh_interval(self):
        order = re.findall(r'<a href="#/settings/(\w+)" data-section', self.html)
        self.assertEqual(order, ["jd2", "browser", "connection", "system", "diag"])
        subs = re.findall(r'data-sub="(\w+)"', self.html)
        self.assertEqual(subs, ["engine", "accounts", "storage", "send", "nas", "pt", "rules", "bookmarks",
                                "https", "share", "updates", "login", "about", "selftest", "tools", "disk", "debug"])
        self.assertRegex(self.js, r"AUTO_REFRESH_MS\s*=\s*5000\b")
        self.assertNotIn("BADGE_REFRESH_MS", self.js)
        self.assertRegex(self.js, r"PASTE_DEBOUNCE_MS\s*=\s*400\b")
        # Delete APIs are wired, and sending to JD2 still asks for confirmation.
        self.assertIn("/api/links/delete", self.js)
        self.assertIn("/api/bookmarks/delete", self.js)
        self.assertIn("confirmMany", self.js)

    def test_v0113_https_and_share_sections(self):
        for ident in ("httpsState", "httpsCmd", "httpsCmdCopy", "shareDevices", "shareForm", "shareName",
                      "shareNew", "shareToken", "shareTokenCopy", "shareEndpoint", "shareEndpointCopy",
                      "shareJs", "shareJsCopy", "shareBookmarklet", "shareBookmarkletCopy",
                      "shareHttpWarning", "cfHttps"):
            with self.subTest(ident=ident):
                self.assertIn('id="%s"' % ident, self.html)
        self.assertIn('data-sub="https"', self.html)
        self.assertIn('data-sub="share"', self.html)
        self.assertIn("HTTPS（Tailscale）", self.html)
        self.assertIn("iPhoneから送る", self.html)
        self.assertIn('href="#/settings/connection/https"', self.html)
        self.assertIn("HTTPSで開くと、パスワードやトークンを暗号化して送れます", self.html)
        self.assertIn("which tailscale", self.html)
        self.assertIn("tailscale serve --bg --https=443 http://127.0.0.1:17864", self.js)
        self.assertIn("/api/share/tokens", self.js)
        self.assertIn("/api/share/tokens/delete", self.js)
        self.assertIn('location.origin + "/api/share"', self.js)
        self.assertIn("completion(Array.from(document.querySelectorAll('a[href]')", self.js)
        self.assertIn("encodeURIComponent(location.href)", self.js)
        self.assertIn("session.https", self.js)
        # The token is shown via textContent only and never persisted.
        self.assertIn('$("shareToken").textContent = made.token', self.js)
        self.assertNotRegex(self.js, r"(?i)(session|local)Storage\.setItem\([^)]*token")
        self.assertNotIn("shareToken\").value", self.js)

    def test_links_paste_route_prefills_without_saving(self):
        self.assertIn('new URLSearchParams(full.slice(cut + 1)).get("paste")', self.js)
        self.assertIn('history.replaceState(null, "", "#/links")', self.js)
        match = re.search(r'if \(route\.view === "links" && route\.paste\) \{.*?\n  \}\n', self.js, re.S)
        self.assertIsNotNone(match)
        self.assertIn("schedulePaste(0)", match.group(0))
        self.assertNotIn("addPasted", match.group(0))
        self.assertNotIn('"/api/links"', match.group(0))

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_share_js_snippet_extracts_links(self):
        line = re.search(r"const SHARE_JS = .*?;\n", self.js, re.S).group(0)
        script = line + """
const hrefs = ["https://rapidgator.net/file/1", "https://example.com/x", "https://rg.to/file/2"];
global.document = {querySelectorAll: () => hrefs.map(h => ({href: h}))};
global.location = {href: "https://page.example/"};
let out = null;
global.completion = value => { out = value; };
eval(SHARE_JS);
const want = "https://rapidgator.net/file/1\\nhttps://rg.to/file/2\\nhttps://page.example/";
if (out !== want) { console.error(JSON.stringify(out)); process.exit(1); }
"""
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_v0114_host_mode_storage_is_guarded_and_capped(self):
        for fn in ("readLocal", "writeLocal", "removeLocal"):
            body = re.search(r"function %s\(.*?\n\}\n" % fn, self.js, re.S).group(0)
            self.assertIn("try", body)
            self.assertIn("catch", body)
        self.assertRegex(self.js, r"\.slice\(-MAX_HOST_MODES\)")

    def test_v0114_long_tab_copy_removed(self):
        for gone in ("隔離表示中", "枠内の遷移先URLは", "Cloudflareの確認が進まない場合",
                     "互換モードで再読み込み"):
            for path in text_files():
                with self.subTest(file=path.name, text=gone):
                    self.assertNotIn(gone, path.read_text(encoding="utf-8"))
        # The HTTPS hint now lives only in the HTTPS settings section.
        https = re.search(r'<div class="sub" data-sub="https".*?<div class="sub" data-sub="share"', self.html, re.S).group(0)
        self.assertIn("HTTPSで開くと", https)
        view_web = re.search(r'<section id="view-web".*?</section>', self.html, re.S).group(0)
        self.assertNotIn("HTTPSで開くと", view_web)

    def test_v0116_remote_display_is_a_host_preference(self):
        self.assertNotIn('REMOTE_MODE = "remote"', self.js)
        self.assertIn('REMOTE_MODES = [REMOTE_FAST_MODE, REMOTE_LIVE_MODE, REMOTE_READER_MODE, REMOTE_MIRROR_MODE]', self.js)
        for ident in ("remoteView", "remoteImg", "remoteStage", "remoteKbd", "remoteKbdBtn", "remoteLinks",
                      "remoteSheet", "remoteAdd", "remoteSend", "remoteHosts"):
            self.assertIn('id="%s"' % ident, self.html)
        for text in ("リンク", "件を追加", "JD2へ送る"):
            self.assertIn(text, self.html + self.js)
        for route in ("/api/rb/frame", "/api/rb/input", "/api/rb/tabs", "/api/rb/tabs/close",
                      "/api/rb/links", "/api/rb/navigate", "/api/rb/status", "/api/rb/setup"):
            self.assertIn(route, self.js)
        self.assertIn('"/api/rapidgator/extract"', self.js)

    def test_v0122_image_source_is_only_a_blob_url_from_the_binary_api(self):
        assignments = re.findall(r"(\w+)\.src\s*=\s*(.+?);", self.js)
        self.assertEqual(sorted(a[0] for a in assignments),
                         ["img", "img", "img", "img", "img", "img", "img", "img", "img", "img", "mf", "probe"])
        # the only other <img> sources are the same-origin MJPEG stream URLs (v0.1.30)
        self.assertEqual(sorted(v for k, v in assignments if k == "img"),
                         ["e.url", "next", "src", "streamUrl(rb)", "streamUrl(rb)", "streamUrl({id: tab})", "t.url", "url",
                          "url", "url"])
        self.assertIn("t.url = URL.createObjectURL(blob);", self.js)
        self.assertIn("e.url = URL.createObjectURL(blob);", self.js)       # reader image (v0.1.30)
        self.assertIn("/api/rb/tile.jpg?tab=", self.js)
        self.assertEqual(dict(assignments)["probe"], "next")
        self.assertIn("const next = URL.createObjectURL(blob);", self.js)
        self.assertIn("/api/rb/frame.jpg?tab=", self.js)
        self.assertNotIn("data:image", self.js)
        self.assertNotIn("jpeg_base64", self.js)
        self.assertNotRegex(self.js, r"setAttribute\(\"src\"")
        # double buffering and no object URL leak
        self.assertIn("await probe.decode()", self.js)
        self.assertGreaterEqual(self.js.count("URL.revokeObjectURL("), 4)
        self.assertIn('blob.type === "image/jpeg"', self.js)
        # still no remote-controlled markup and no forbidden sandbox tokens
        for token in ("innerHTML", "outerHTML", "insertAdjacentHTML"):
            self.assertNotIn(token, self.js)
        for token in FORBIDDEN_SANDBOX:
            self.assertNotIn(token, self.js + self.html.replace(PT_SANDBOX, "", 1))     # v0.1.57: the passthrough frame's own attribute

    def test_v0122_viewport_scroll_coalescing_and_intercept_sheet(self):
        # viewport is reported from the stage, validated ranges mirror the server
        self.assertIn("/api/rb/viewport", self.js)
        self.assertIn("devicePixelRatio", self.js)
        self.assertIn("ResizeObserver", self.js)
        # one scroll input per animation frame, deltas summed
        self.assertIn("requestAnimationFrame(flushScroll)", self.js)
        self.assertIn("rbScroll.dx += dx", self.js)
        self.assertNotIn("RB_SCROLL_MS", self.js)
        # intercept bottom sheet
        for ident in ("interceptSheet", "interceptUrl", "interceptSend", "interceptAdd",
                      "interceptOpen", "interceptClose"):
            self.assertIn('id="%s"' % ident, self.html)
        for text in ("リンクを検出しました", "JD2へ送る", "リンクに追加", "ページを開く", "閉じる"):
            self.assertIn(text, self.html)
        self.assertRegex(self.html, r'<button[^>]*id="interceptSend"[^>]*class="btn primary"[^>]*hidden')
        for route in ("/api/rb/intercepts?tab=", "/api/rb/intercepts/send", "/api/rb/intercepts/add",
                      "/api/rb/intercepts/open"):
            self.assertIn(route, self.js)
        self.assertIn('setShown("interceptSend", canSend)', self.js)
        self.assertIn('setShown("interceptOpen", cur.item.kind === "hoster")', self.js)
        self.assertIn("confirm(", self.js[self.js.index("async function interceptAction"):])
        self.assertIn('"リンク " + count + "件"', self.js)       # the badge stays
        # popups: a non-blocking toast (latest wins, 8 s), never the sheet
        for ident in ("popupToast", "popupToastText", "popupToastOpen"):
            self.assertIn('id="%s"' % ident, self.html)
        self.assertIn("RB_POPUP_TOAST_MS = 8000", self.js)
        self.assertIn('item.kind === "popup"', self.js)
        self.assertIn('"ポップアップ: " + hostLabel(item.url) + " を開く"', self.js)
        self.assertIn("clearTimeout(rbPopup.timer);\n  rbPopup.item = item;", self.js)
        self.assertIn('api("/api/rb/intercepts/open", {tab: item.tab, id: item.id})', self.js)
        self.assertRegex(self.html, r'<div id="popupToast"[^>]*hidden')
        css = (WEB / "style.css").read_text(encoding="utf-8")
        self.assertIn(".intercept-url", css)
        self.assertIn("text-overflow:ellipsis", css)

    def test_v0116_remote_tab_lifecycle_and_input_wiring(self):
        # closing the web tab frees the NAS tab; polling only for the visible tab
        self.assertRegex(self.js, r"releaseRemote\(tab\);\s*//[^\n]*frees memory")
        self.assertIn('document.visibilityState === "visible" && currentViewName() === "web"', self.js)
        self.assertIn('"pointerdown"', self.js)
        self.assertIn('"wheel"', self.js)
        self.assertIn('$("remoteKbd")', self.js)
        for key in ("Enter", "Backspace", "Tab"):
            self.assertIn('"%s"' % key, self.js)
        self.assertIn("compositionend", self.js)
        # restored tabs are opened by autoReopen when shown (and never while not ready / after an error)
        self.assertIn("function autoReopen(tab)", self.js)
        self.assertIn("if (tab.notReady || tab.error || state.rbReady === false) return false;", self.js)
        self.assertIn("rb.width", self.js)    # taps are scaled to the reported frame size

    def test_v0116_settings_browser_section(self):
        section = re.search(r'<section class="settings-section" data-section="browser".*?</section>',
                            self.html, re.S).group(0)
        for ident in ("rbState", "rbSetup", "rbStop", "rbMemory", "rbSteps", "rbNoContainer"):
            self.assertIn('id="%s"' % ident, section)
        for text in ("初期設定（イメージ作成）", "停止", "2GiB", "80%", "5分", "10分", "Container Station"):
            self.assertIn(text, section)
        self.assertIn('SETTINGS_SECTIONS = ["jd2", "browser", "connection", "system", "diag"]', self.js)

    def test_v0116_native_leftover_row(self):
        self.assertRegex(self.html, r'<div id="engineNativeRow"[^>]*hidden')
        self.assertIn('id="engineNativeRemove"', self.html)
        self.assertIn("native_leftovers", self.js)
        self.assertIn('setShown("engineNativeRow", canClean)', self.js)
        self.assertIn("/api/engine/native/remove", self.js)
        self.assertIn("JD2の設定・キュー・ダウンロード途中のファイル", self.js)
        self.assertIn("QTS直接実行には戻せなくなります", self.js)

    def test_icons_use_symbol_sprite(self):
        self.assertIn("<symbol", self.html)
        self.assertIn('id="notice"', self.html)
        self.assertIn('role="status"', self.html)

    def test_v0123_accounts_section_ui_and_secret_handling(self):
        section = re.search(r'<div class="sub" data-sub="accounts".*?<div class="sub" data-sub="storage"',
                            self.html, re.S).group(0)
        for ident in ("accountsList", "accountForm", "accountHost", "accountHostList", "accountUser",
                      "accountPass", "accountAdd", "accountsSecure", "accountEditPass", "accountResult"):
            self.assertIn('id="%s"' % ident, section)
        self.assertIn('href="#/settings/connection/https"', section)
        # Password inputs: masked, never autofilled/saved by the browser.
        for ident in ("accountPass", "accountEditPass"):
            tag = re.search(r'<input id="%s"[^>]*>' % ident, section).group(0)
            self.assertIn('type="password"', tag)
            self.assertIn('autocomplete="new-password"', tag)
        self.assertIn('list="accountHostList"', section)
        # Settings order: アカウント directly after エンジン (subsections of the JD2 section).
        self.assertRegex(self.html, r'(?s)data-sub="engine".*?data-sub="accounts"')
        # Onboarding: optional third item, hidden by default.
        self.assertRegex(self.html, r'<li id="obAccounts"[^>]*hidden')
        self.assertIn("プレミアムアカウントを登録（任意）", self.html)
        # The two password inputs are read only to build a request body and are
        # emptied; they never reach storage, URLs, the DOM text or the console.
        for ident in ("accountPass", "accountEditPass"):
            for call in re.finditer(r'\$\("%s"\)\.value' % ident, self.js):
                line = self.js[self.js.rfind("\n", 0, call.start()) + 1:self.js.find("\n", call.end())]
                self.assertNotRegex(line, r"(?i)storage|textContent|location|console|setAttribute|toast|flash")
        self.assertNotRegex(self.js, r"(?i)(session|local)Storage\.setItem\([^)]*(pass|account)")
        self.assertNotRegex(self.js, r"(?i)write(Session|Local)\([^)]*(pass|account)")
        self.assertNotRegex(self.js, r"console\.\w+\([^)]*(?i:pass)")
        # Writes go to the dedicated endpoints; delete needs the typed host name.
        for path in ("/api/accounts/add", "/api/accounts/update", "/api/accounts/enable",
                     "/api/accounts/refresh", "/api/accounts/delete", "/api/accounts/hosts"):
            self.assertIn(path, self.js)
        self.assertIn("confirm_host", self.js)
        self.assertIn("/api/accounts/schema", self.js)
        self.assertIn("アカウント情報の項目を調べる", self.html)
        self.assertIn("一覧に出ないアカウントがある場合は、この結果を開発者に送ってください", self.html)
        self.assertIn("state.https === true || state.tailnet === true", self.js)
        self.assertIn("パスワードはHTTPSまたはTailscale経由でのみ送信できます", self.js)
        self.assertNotIn("innerHTML", self.js)


    def test_v0124_every_element_id_used_by_the_script_exists(self):
        used = set(re.findall(r'\$\("([A-Za-z0-9_-]+)"\)', self.js))
        have = set(re.findall(r'\sid="([^"]+)"', self.html))
        self.assertEqual(sorted(used - have), [])

    def test_v0124_send_and_start_ui(self):
        for ident in ("sendAutostart", "waitingBox", "waitingList", "waitingStart", "downloadsToggle"):
            self.assertIn('id="%s"' % ident, self.html)
        for text in ("JD2へ送ったらすぐダウンロード開始", "JD2で待機中のリンク", "すべて開始"):
            self.assertIn(text, self.html + self.js)
        for path in ("/api/jd2/linkgrabber", "/api/jd2/linkgrabber/start", "/api/jd2/downloads/start",
                     "/api/jd2/downloads/stop", "/api/settings/options"):
            self.assertIn(path, self.js)
        self.assertRegex(self.js, r"WAITING_MAX\s*=\s*50\b")
        # start-all and stop are confirmed in the UI as well as on the Hub
        self.assertRegex(self.js, r"async function startWaiting\(\)[\s\S]*?confirm\(")
        self.assertRegex(self.js, r"async function toggleDownloads\(\)[\s\S]*?confirm\(")
        # the old "auto start is OFF" wording only appears when the setting is off
        self.assertIn("function sendStartText()", self.js)

    def test_v0124_site_rules_and_popup_ui(self):
        for ident in ("blockAds", "siteRules", "interceptAuto", "interceptAutoRow",
                      "popupToastAllow", "popupToastBlock", "remotePopups"):
            self.assertIn('id="%s"' % ident, self.html)
        for text in ("広告をブロック", "このサイトでは今後自動で送る", "常に許可", "常にブロック",
                     "JD2へ自動送信しました", "ポップアップ", "件ブロック"):
            self.assertIn(text, self.html + self.js)
        self.assertIn("/api/rb/site-rules", self.js)
        self.assertIn("/api/rb/site-rules/delete", self.js)
        self.assertRegex(self.js, r'POPUP_LABELS = \{ask: "[^"]+", allow: "[^"]+", block: "[^"]+"\}')
        self.assertIn('popupRule("allow")', self.js)
        self.assertIn('popupRule("block")', self.js)

    def test_v0124_long_press_probe_ui(self):
        self.assertRegex(self.js, r"RB_LONGPRESS_MS\s*=\s*550\b")
        self.assertRegex(self.js, r"RB_LONGPRESS_MOVE_PX\s*=\s*10\b")
        for ident in ("probeSheet", "probeList", "probeSendAll", "probeAddAll", "probeClose",
                      "remoteRing"):
            self.assertIn('id="%s"' % ident, self.html)
        for text in ("リンク解析", "すべてJD2へ送る", "リンクに追加", "失敗を再試行", "待ちを取り消す"):
            self.assertIn(text, self.html + self.js)
        self.assertIn("/api/rb/probe", self.js)
        # a long press suppresses the tap, moving cancels it, the OS context menu is off
        self.assertIn("const longPressed = rbPress.fired;", self.js)
        self.assertRegex(self.js, r"rbPointer\.moved = true;\s*cancelPress\(\);")
        self.assertIn('stage.addEventListener("contextmenu"', self.js)
        # the probe sheet is built from text nodes only
        self.assertNotIn("innerHTML", self.js)

    def test_v0125_probe_autosend_has_no_confirm_and_packagizer_text_is_gone(self):
        self.assertNotIn("Packagizer", self.js)
        self.assertIn('id="probeAutosend"', self.html)
        self.assertIn("長押しで見つかったリンクを自動でJD2へ送る", self.html)
        self.assertIn("/api/rb/probe/send", self.js)
        start = self.js.index("async function autoSendProbe")
        auto = self.js[start:self.js.index("async function startProbe")]
        self.assertNotIn("confirm(", auto)
        self.assertIn("JD2へ送信しました", auto)
        # no confirm on the NAS-browser intercept send; the Links page asks only for 5+ links
        send = self.js[self.js.index('if (action === "send")'):self.js.index('} else if (action === "add")')]
        self.assertNotIn("confirm(", send)
        self.assertIn("opts.confirmMany && links.length >= 5", self.js)

    def test_v0164_long_press_queues_a_job_and_the_chip_shows_progress(self):
        # v0.1.65 (was: parallel probes with held-open requests): a long press only adds a job on the Hub
        self.assertNotIn("probeOpen", self.html + self.js)
        self.assertNotIn("このタブで開く", self.html + self.js)
        self.assertNotIn("probeSeq", self.js)
        self.assertNotIn("probeFlight", self.html + self.js)             # no request is held open and listed any more
        self.assertIn('id="probeChip"', self.html)
        self.assertIn('id="menuProbes"', self.html)
        self.assertIn("リンク解析 ", self.js)
        self.assertNotIn("リンク先を確認しています", self.html + self.js)
        start = self.js.index("async function startProbe")
        body = self.js[start:self.js.index("// Pointer to page coordinates")]
        self.assertNotIn("probeFlight", body)
        self.assertIn('await api("/api/rb/probe", body)', body)
        self.assertIn("解析待ちに追加（", body)
        self.assertIn("同じリンクは解析済みです", body)
        self.assertIn("リンクは見つかりませんでした", body)         # a Rapidgator link is still answered at once
        self.assertIn("（最終: ", body)
        auto = self.js[self.js.index("async function autoSendProbe"):start]
        self.assertIn("JD2へ送信しました（", auto)
        self.assertNotIn("probeSheet", auto)
        # the counts ride on every meta answer; the list is read only on demand; no timer polls it
        self.assertIn("noteProbes(tab, f.probes)", self.js)
        self.assertEqual(self.js.count("probeQuery(rb)"), 5)               # every meta long-poll passes the revision it holds
        self.assertIn('"/api/rb/probe/jobs?tab="', self.js.replace("'", '"'))
        self.assertNotIn("setInterval", self.js)
        for action in ("retry", "cancel", "dismiss"):
            self.assertIn('probeAction("%s"' % action, self.js)
        css = (WEB / "style.css").read_text(encoding="utf-8")
        self.assertRegex(css, r"\.probe-chip\{[^}]*height:32px")
        self.assertRegex(css, r"\.probe-chip\{[^}]*safe-area-inset-bottom")
        self.assertRegex(css, r"\.probe-sheet\{[^}]*max-height:60%")

    def test_v0127_key_bar_is_hidden_until_the_page_focuses_an_editable_element(self):
        self.assertRegex(self.html, r'id="remoteTools"[^>]*\bhidden\b')
        self.assertRegex(self.html.split('id="remoteKbd"')[0][-200:], r"<input $")
        self.assertIn("X-Frame-Editable", self.js)
        self.assertIn("setEditable(tab, r.editable, true)", self.js)
        self.assertIn('setShown("remoteTools", !!(tab && tab.rb && tab.rb.editable))', self.js)
        self.assertIn('$("remoteKbd").focus()', self.js)
        css = (WEB / "style.css").read_text(encoding="utf-8")
        self.assertIn(".remote-tools[hidden]{display:none}", css)
        self.assertRegex(css, r"\.remote-tools\{[^}]*min-height:36px")

    def test_v0125_history_clear_and_duplicate_skips(self):
        for ident in ("downloadsClear",):
            self.assertIn('id="%s"' % ident, self.html)
        # v0.1.33: the history deletion moved from the page body into the header ⋯ menu
        self.assertIn('id="linksMore"', self.html)
        self.assertNotIn('id="clearHistory"', self.html)
        self.assertNotIn('id="clearStaged"', self.html)
        self.assertIn("送信済み・要確認の履歴を削除", self.js)
        self.assertIn("未送信のリンクも含めてすべて削除", self.js)
        self.assertIn("完了・スキップを一覧から消す", self.html)
        self.assertIn("/api/links/clear", self.js)
        self.assertIn("/api/jd2/downloads/cleanup", self.js)
        self.assertIn("保存されたファイルは削除されません", self.js)
        self.assertIn("ダウンロード済みファイルやJD2のダウンロードは削除しません", self.js)
        # duplicates are skipped and counted, never confirmed
        self.assertIn("件は重複のためスキップ", self.js)
        for fn in ("async function sendUrls", "async function autoSendProbe", "async function stageLinks"):
            start = self.js.index(fn)
            body = self.js[start:self.js.index("\n}\n", start)]
            self.assertNotIn("confirm(", body)

    def test_v0124_quality_menu_and_perf_overlay(self):
        for quality in ("speed", "standard", "high"):
            self.assertIn('data-quality="%s"' % quality, self.html)
        self.assertIn('id="menuPerf"', self.html)
        self.assertIn('id="perfOverlay"', self.html)
        self.assertRegex(self.js, r'RB_QUALITIES\s*=\s*\["speed", "standard", "high"\]')
        self.assertIn('qnapdl.rbquality.v1', self.js)
        self.assertIn('return RB_QUALITIES.includes(value) ? value : "speed";', self.js)
        self.assertIn("quality: state.rbQuality", self.js)
        # remembered through the guarded storage wrappers only
        self.assertIn("writeLocal(RB_QUALITY_KEY, mode)", self.js)
        self.assertNotRegex(self.js, r"localStorage\.\w+Item\([^)]*RB_QUALITY_KEY")

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_v0124_perf_overlay_math(self):
        record = re.search(r"function recordPerf\(bytes, latency\) \{.*?\n\}\n", self.js, re.S).group(0)
        text = re.search(r"function perfText\(tab = remoteTab\(\)\) \{.*?\n\}\n", self.js, re.S).group(0)
        script = """
(function (performance) {
const RB_PERF_WINDOW_MS = 5000;
const state = {rbPerf: true, rbPerfSamples: [], rbPerfSince: 0, rbQuality: "standard"};
const RB_QUALITY_LABELS = {speed: "速度優先", standard: "標準", high: "高画質"};
const remoteTab = () => null;
const tabDisplay = tab => tab.display;
const LIVE_KIND_LABEL = {ws: "WebSocket", sse: "SSE", mjpeg: "MJPEG", poll: "ポーリング"};
""" + record + text + """
let t = 0;
const mk = () => ({now: () => t});
performance = mk();
for (let i = 0; i < 10; i++) { t = 500 * (i + 1); recordPerf(20480, 100); }
t = 5000;
const out = perfText();
if (out !== "ライブ(ポーリング)・標準 · 2.0 fps · 20 KB/枚 · 応答 100 ms") { console.error("bad: " + out); process.exit(1); }
// samples older than 5 s drop out; an idle page reports 0 fps
t = 20000; state.rbPerfSince = 15000;
const idle = perfText();
if (!idle.startsWith("ライブ(ポーリング)・標準 · 0.0 fps")) { console.error("idle: " + idle); process.exit(1); }
// 204 responses (no frame) count for latency but not for fps or bytes
t = 21000; state.rbPerfSamples = []; state.rbPerfSince = 20500;
recordPerf(0, 300); recordPerf(1024, 100);
t = 21500;
const mixed = perfText();
if (mixed !== "ライブ(ポーリング)・標準 · 1.0 fps · 1 KB/枚 · 応答 200 ms") { console.error("mixed: " + mixed); process.exit(1); }
// per display kind and transport (v0.1.30)
state.rbQuality = "speed";
const live = {display: "live", rb: {stream: {}, streamFailed: false,
  perf: {fps: 9.84, frameBytes: 22528, streamMs: 120}}};
let o = perfText(live);
if (o !== "ライブ(MJPEG)・速度優先 · 9.8 fps · 22 KB/枚 · 応答 120 ms") { console.error("stream: " + o); process.exit(1); }
live.rb.stream = {kind: "sse"};
live.rb.perf.fallback = "WebSocketの最初の画面が届かない";
o = perfText(live);
if (o !== "ライブ(SSE: WebSocketの最初の画面が届かない)・速度優先 · 9.8 fps · 22 KB/枚 · 応答 120 ms") { console.error("sse: " + o); process.exit(1); }
live.rb.stream = {kind: "ws"};      // v0.1.68: the transport and its speed in the existing speed line
live.rb.perf.fallback = "";
o = perfText(live);
if (o !== "ライブ(WebSocket)・速度優先 · 9.8 fps · 22 KB/枚 · 応答 120 ms") { console.error("ws: " + o); process.exit(1); }
live.rb.perf.fallback = "x";
live.rb.perf.fallback = "";
live.rb.streamFailed = true;
if (!perfText(live).startsWith("ライブ(ポーリング)・速度優先 · ")) { console.error("fallback: " + perfText(live)); process.exit(1); }
const fast = {display: "tiles", rb: {view: {tiles: [{url: "a"}, {url: "b"}, {url: ""}, {url: "c"}]},
  perf: {snapMs: 850, tiles: 2, tileBytes: 2 * 143360}}};
o = perfText(fast);
if (o !== "高速(全体表示)・速度優先 · タイル 3/4 · 撮影 850 ms · 1枚 140 KB") { console.error("fast: " + o); process.exit(1); }
const reader = {display: "reader", rb: {reader: {data: {blocks: [{t: "p"}, {t: "img"}, {t: "img"}]}},
  perf: {extractMs: 600, imgs: 1, imgMs: 80}}};
o = perfText(reader);
if (o !== "リーダー · ブロック 3 · 画像 1/2 · 取得 600 ms · 画像取得 80 ms") { console.error("reader: " + o); process.exit(1); }
})();
"""
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_v0146_sse_parser_transport_memory_and_copy_helpers(self):
        def fn(name):
            return re.search(r"(?:async )?function %s\(.*?\n\}\n" % name, self.js, re.S).group(0)
        script = """
(async function () {
const RB_LIVE_PREF_KEY = "k"; const RB_LIVE_RETRY_MS = 24 * 60 * 60 * 1000;
const LIVE_ORDER = ["ws", "sse", "mjpeg"];
const store = {};
const readLocal = key => (key in store ? store[key] : null);
const writeLocal = (key, value) => { store[key] = value; };
let sseOn = true, wsOn = true;
const sseSupported = () => sseOn;
const wsSupported = () => wsOn;
""" + fn("liveLoadPref") + fn("liveSavePref") + fn("liveKindSupported") + fn("liveFailedRecently") + fn("liveMarkFailed") + fn("liveMarkWorked") + fn("liveNextKind") + fn("liveStartKind") + fn("sseParser") + """
const eq = (a, b, what) => { if (JSON.stringify(a) !== JSON.stringify(b)) { console.error(what + ": " + JSON.stringify(a) + " != " + JSON.stringify(b)); process.exit(1); } };
// event stream parsing: comments and pings are ignored, events may be split anywhere
const seen = [];
const feed = sseParser((name, data) => seen.push([name, data]));
feed(":" + " ".repeat(2047) + "\\n\\n");
feed("event: frame\\ndata: QUJ");
feed("D\\n\\n: ping\\n\\nevent: frame\\ndata: WFla\\n\\n");
eq(seen, [["frame", "QUJD"], ["frame", "WFla"]], "sse events");
// v0.1.68 transport order WebSocket -> SSE -> MJPEG -> polling; a transport that failed is skipped for a day
eq(liveStartKind(), "ws", "default is WebSocket");
eq(liveNextKind("ws"), "sse", "after ws");
eq(liveNextKind("sse"), "mjpeg", "after sse");
eq(liveNextKind("mjpeg"), "poll", "after mjpeg");
liveMarkFailed("ws");
eq(liveStartKind(), "sse", "ws failed today");
eq(liveNextKind("ws"), "sse", "next after the failed ws");
liveMarkFailed("sse");
eq(liveStartKind(), "mjpeg", "ws and sse failed today");
liveMarkFailed("mjpeg");
eq(liveStartKind(), "poll", "all failed today");
writeLocal(RB_LIVE_PREF_KEY, JSON.stringify({fail: {ws: Date.now() - RB_LIVE_RETRY_MS - 1000, sse: Date.now() - 1000}}));
eq(liveStartKind(), "ws", "a day later WebSocket is tried again");
liveMarkWorked("ws");
eq(JSON.parse(store[RB_LIVE_PREF_KEY]).t, "ws", "remembered");
eq(Object.keys(JSON.parse(store[RB_LIVE_PREF_KEY]).fail), ["sse"], "a transport that worked is no longer failed");
wsOn = false;
eq(liveStartKind(), "mjpeg", "no WebSocket here, sse failed today");
sseOn = false;
writeLocal(RB_LIVE_PREF_KEY, "{}");
eq(liveStartKind(), "mjpeg", "neither ws nor sse here");
writeLocal(RB_LIVE_PREF_KEY, "not json");
eq(liveStartKind(), "mjpeg", "garbage is ignored");
writeLocal(RB_LIVE_PREF_KEY, JSON.stringify({fail: "x", t: 5}));
eq(liveStartKind(), "mjpeg", "a malformed fail map is ignored");
wsOn = true;
writeLocal(RB_LIVE_PREF_KEY, "{}");
eq(liveStartKind(), "ws", "ws again");
liveSavePref({t: "sse"}); liveSavePref({fail: {ws: 5}});
eq(JSON.parse(store[RB_LIVE_PREF_KEY]), {t: "sse", fail: {ws: 5}}, "merge");
})();
"""
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_v0146_every_copy_button_has_the_textarea_fallback(self):
        copy = re.search(r"function execCopy\(text\) \{.*?\n\}\n", self.js, re.S).group(0)
        for token in ('document.createElement("textarea")', "setSelectionRange", 'document.execCommand("copy")', "area.remove()"):
            self.assertIn(token, copy)
        helper = re.search(r"async function copyToClipboard\(text\) \{.*?\n\}\n", self.js, re.S).group(0)
        self.assertIn("navigator.clipboard && navigator.clipboard.writeText", helper)
        self.assertIn("return execCopy(text);", helper)
        # no copy button calls navigator.clipboard directly any more
        self.assertEqual(len(re.findall(r"await navigator\.clipboard\.writeText\(", self.js)), 1)
        self.assertEqual(len(re.findall(r"= document\.execCommand\(", self.js)), 1)
        selftest = re.search(r"async function selftestCopy\(\) \{.*?\n\}\n", self.js, re.S).group(0)
        self.assertIn("copyToClipboard(", selftest)
        self.assertIn("selftestUi.cache", selftest)           # the report is prefetched: the tap copies inside the gesture
        self.assertIn("下のテキストを長押し", selftest)
        for button in ("accountSchemaCopy", "debugCopy", "httpsCmdCopy", "shareTokenCopy", "shareEndpointCopy",
                       "shareJsCopy", "shareBookmarkletCopy", "engineKeysCopy"):
            line = re.search(r'\$\("%s"\)\.addEventListener\("click".*?\n(?:.*\n)?' % button, self.js).group(0)
            self.assertIn("copyText(", line, button)

    def test_v0146_selftest_client_measures_each_live_transport_in_the_apps_order(self):
        step = re.search(r"async function selftestLiveStep\(tab, holder\) \{.*?\n\}\n", self.js, re.S).group(0)
        self.assertLess(step.index("selftestLiveWs("), step.index("selftestLiveSse("))      # v0.1.68: WebSocket first
        self.assertLess(step.index("selftestLiveSse("), step.index("selftestLiveMjpeg("))
        self.assertLess(step.index("selftestLiveMjpeg("), step.index("selftestLivePoll("))
        ws = re.search(r"async function selftestLiveWs\(tab, out\) \{.*?\n\}\n", self.js, re.S).group(0)
        self.assertIn("wsUrl({id: tab})", ws)
        self.assertIn("RB_WS_FIRST_MS", ws)
        self.assertIn("conn.close()", ws)
        self.assertIn('"fps_ws"', step)
        self.assertIn("RB_MJPEG_FIRST_MS", self.js[self.js.index("async function selftestLiveMjpeg"):])
        poll = re.search(r"async function selftestLivePoll\(tab, out\) \{.*?\n\}\n", self.js, re.S).group(0)
        self.assertIn('"&since=" + seq', poll)               # exactly like remoteLoop: since = last seq, no rev
        self.assertNotIn("&rev=", poll)
        self.assertIn("ctl.abort()", poll)                   # a long poll waiting for a frame cannot stretch the window
        self.assertIn("client.secure", self.js)
        self.assertIn("queryCommandSupported", self.js)

    def test_v0130_overlay_names_the_mode_and_live_uses_the_stream(self):
        text = re.search(r"function perfText\(tab = remoteTab\(\)\) \{.*?\n\}\n", self.js, re.S).group(0)
        for label in ('"ライブ("', "LIVE_KIND_LABEL", "ライブ(ポーリング", "高速(全体表示)", "リーダー"):
            self.assertIn(label, text)
        self.assertIn('{ws: "WebSocket", sse: "SSE", mjpeg: "MJPEG", poll: "ポーリング"}', self.js)
        # live display: the <img> points at the stream; metadata still comes from the meta long-poll
        self.assertIn("/api/rb/stream.mjpg?tab=", self.js)
        self.assertIn("/api/rb/stream.sse?tab=", self.js)
        loop = re.search(r"async function streamLoop\(.*?\n\}\n", self.js, re.S).group(0)
        self.assertIn("openStream(tab, tab.rb, kind)", loop)
        self.assertIn("/api/rb/meta?tab=", loop)
        self.assertNotIn("frame.jpg", loop)
        # live prefers the stream unless this tab already fell back to frame.jpg polling (v0.1.46: MJPEG -> SSE -> polling)
        self.assertRegex(self.js, r'const kind = rb\.streamFailed \|\| typeof Image !== "function" \? "poll" : liveStartKind\(\);')
        self.assertIn('if (kind === "poll") {', self.js)
        self.assertIn("remoteLoop(tab, state.rbGen);", self.js)
        self.assertIn("streamLoop(tab, state.rbGen, kind)", self.js)
        # fallback: two errors within 10 s, or no picture within 2.5 s (MJPEG, then SSE 3 s) before polling
        self.assertIn("RB_STREAM_FIRST_MS = 5000", self.js)
        self.assertIn("RB_WS_FIRST_MS = 2500", self.js)
        self.assertIn("RB_MJPEG_FIRST_MS = 2500", self.js)
        self.assertIn("RB_SSE_FIRST_MS = 3000", self.js)
        self.assertIn("RB_STREAM_ERR_WINDOW_MS = 10000", self.js)
        self.assertIn("s.errors.length >= 2", self.js)
        advance = re.search(r"function streamAdvance\(tab, rb, reason\) \{.*?\n\}\n", self.js, re.S).group(0)
        self.assertIn("liveNextKind(kind)", advance)              # v0.1.68: WebSocket -> SSE -> MJPEG -> polling
        self.assertIn("openStream(tab, rb, next)", advance)
        self.assertIn("rb.streamFailed = true;", advance)
        # closed when hidden / switched / mode changes / the picture is dropped / logout
        sync = re.search(r"function syncRemotePoll\(\) \{.*?\n\}\n", self.js, re.S).group(0)
        self.assertIn("closeStream();", sync)
        self.assertIn("document.addEventListener(\"visibilitychange\", syncRemotePoll)", self.js)
        drop = re.search(r"function dropRemoteImage\(rb\) \{.*?\n\}\n", self.js, re.S).group(0)
        self.assertIn("closeStream()", drop)
        close = re.search(r"function closeStream\(\) \{.*?\n\}\n", self.js, re.S).group(0)
        self.assertIn('img.removeAttribute("src")', close)
        # proactive reconnect: every 9 min while visible, and after 6 s of 0 fps on a settled page
        self.assertIn("RB_STREAM_RECONNECT_MS = 9 * 60 * 1000", self.js)
        self.assertIn("RB_STREAM_STALL_MS = 6000", self.js)
        self.assertIn('"&n=" + (++streamNonce)', self.js)
        check = re.search(r"function checkStream\(rb, meta\) \{.*?\n\}\n", self.js, re.S).group(0)
        for token in ('document.visibilityState !== "visible"', "RB_STREAM_RECONNECT_MS",
                      "meta.loading === true", "meta.fps > 0", "RB_STREAM_STALL_MS"):
            self.assertIn(token, check)
        self.assertIn("checkStream(rb, meta);", loop)
        # the stream is same-origin: the CSP needs no new source
        self.assertNotIn("stream.mjpg", (WEB / "style.css").read_text(encoding="utf-8"))

    # ---- v0.1.28: fast full-page display ------------------------------------
    def test_v0128_tiles_are_lazy_and_released(self):
        css = (WEB / "style.css").read_text(encoding="utf-8")
        for ident in ("tileScroll", "snapProgress"):
            self.assertIn('id="%s"' % ident, self.html)
        self.assertRegex(self.html, r'id="tileScroll"[^>]*\bhidden\b')
        # load near / release far: tiles (2) and, since v0.1.30, reader images (2)
        self.assertEqual(self.js.count("new IntersectionObserver("), 4)
        self.assertIn('rootMargin: "100% 0px 100% 0px"', self.js)
        self.assertIn('rootMargin: "300% 0px 300% 0px"', self.js)
        self.assertIn("releaseTile(t)", self.js)
        self.assertRegex(self.js, r"function releaseTile\(t\) \{[^}]*URL\.revokeObjectURL\(t\.url\)")
        self.assertIn("RB_TILE_CONCURRENCY", self.js)
        self.assertIn(".tile-scroll{position:absolute;inset:0;overflow-x:hidden;overflow-y:auto", css)
        self.assertIn("touch-action:pan-y", css)
        # tiles are images only: no remote markup or script reaches the page
        for token in ("innerHTML", "outerHTML", "insertAdjacentHTML", "eval(", "new Function"):
            self.assertNotIn(token, self.js)
        self.assertIn("img-src 'self' data: blob:", (WEB.parent / "webapp.py").read_text(encoding="utf-8"))

    def test_v0128_display_menu_and_default(self):
        self.assertIn('data-action="display:tiles"', self.html)
        self.assertIn('data-action="display:live"', self.html)
        # v0.1.36: a segmented 表示 control in the grouped menu
        for text in ('aria-label="表示方法"', '>高速</button>', '>ライブ</button>'):
            self.assertIn(text, self.html)
        self.assertIn('RB_DISPLAY_LABELS = {reader: "リーダー", mirror: "ミラー", tiles: "高速（全体表示）", live: "ライブ"}', self.js)
        self.assertIn('const REMOTE_LIVE_MODE = "remote-live"', self.js)
        # remembered in the existing per-host map; no entry follows the Settings default (v0.1.30)
        self.assertIn("rememberHostMode(host, MODE_OF_DISPLAY[mode])", self.js)
        self.assertIn("DISPLAY_OF_SETTING[setting] || \"tiles\"", self.js)
        self.assertIn("action.startsWith(\"display:\")", self.js)
        self.assertNotIn("modes.includes(REMOTE_LIVE_MODE)", self.js)

    def test_v0128_hit_testing_taps_and_long_press_use_the_link_map(self):
        for route in ("/api/rb/snapshot", "/api/rb/snapshot/release", "/api/rb/tap-page", "/api/rb/meta",
                      "/api/rb/probe"):
            self.assertIn(route, self.js)
        self.assertIn("function linkAt(snap, x, y)", self.js)
        self.assertIn("{tab: rb.id, href: point.href}", self.js)
        self.assertIn("startProbe(tab, link ? {href: link.href} :", self.js)
        self.assertIn("RB_PULL_PX", self.js)
        self.assertIn("RB_INPUT_SNAP_MS = 800", self.js)
        self.assertIn("RB_META_WAIT_S = 10", self.js)

    def test_v0128_cloudflare_challenge_switches_to_live_and_back(self):
        # v0.1.52: no automatic live switch any more: the check page is held (not rendered) in the chosen display.
        self.assertIn("function syncChallenge(tab, challenge)", self.js)
        self.assertIn("X-Frame-Challenge", self.js)
        self.assertIn("function challengeHold(tab)", self.js)
        self.assertIn('if (rb.chal && rb.chal.live) return "live";', self.js)
        for gone in ("function challengeStep", "autoPollTick", "returnToFast", "rb-auto", "autoLiveChip", "AUTO_CLEAN_NEEDED"):
            self.assertNotIn(gone, self.js)

    def test_v0131_node_behaviour_of_early_read_errors_and_fallback(self):
        if shutil.which("node") is None:
            self.skipTest("node missing")
        display = self.js[self.js.index("// The display the user chose"):self.js.index("// 「表示: リーダー / 高速 / ライブ」 from the menu")]
        logic = self.js[self.js.index("// A page that is still \"loading\" after this long"):
                        self.js.index("// Metadata long-poll like the fast display")]
        loop = self.js[self.js.index("async function readerLoop("):self.js.index("// Tap on a link: the NAS browser goes there")]
        self.assertIn('readerAction(rb, meta, Date.now()) === "run"', loop)
        script = """
const state = {settings: {rb_default_mode: "reader"}};
const dlog = () => {};
const REMOTE_MODE = "remote", REMOTE_FAST_MODE = "remote-fast", REMOTE_LIVE_MODE = "remote-live",
  REMOTE_READER_MODE = "remote-reader";
const DISPLAY_OF_MODE = {[REMOTE_FAST_MODE]: "tiles", [REMOTE_LIVE_MODE]: "live", [REMOTE_READER_MODE]: "reader"};
const DISPLAY_OF_SETTING = {reader: "reader", fast: "tiles", live: "live"};
const hostOf = u => u ? new URL(u).hostname : "";
const rememberedMode = h => null;
const toasts = [], logs = [];
console.error = (...a) => logs.push(a.join(" "));
const toast = (m, k) => toasts.push([m, k || "success"]);
let applied = 0, apiResult = null, apiError = null;
const applyDisplay = () => { applied++; };
const challengeHold = () => false;
const updateRemoteView = () => {};
const remoteLost = () => {};
const snapBusy = () => {};
const $ = () => ({scrollTop: 0});
const readerHost = {owner: null};
const renderReaderBody = () => {};
const readerSavedTop = () => 0;
const syncReaderProgress = () => {};
const cleanReaderData = raw => raw && Array.isArray(raw.blocks) ? raw : null;
const api = async () => { if (apiError) throw apiError; return apiResult; };
const performance = {now: () => 0};
%s
%s
const eq = (a, b, m) => { if (a !== b) { process.stdout.write(m + ": " + a + " != " + b + "\\n"); process.exit(1); } };
const mk = () => ({id: "b1", url: "https://e.com/a", reader: null, readerPending: false, readerEarly: "", readerFail: null,
  readerErr: false, tilesFor: "", snapEarly: "", loadUrl: "", loadSince: 0, snap: null, snapKey: "", perf: {},
  readerBusy: false, readerAgain: false});
const A = "https://e.com/a", B = "https://e.com/b";
// --- 3 s rule: not before 3 s, then once, then again when loading turns false
let rb = mk();
eq(readerAction(rb, {url: A, loading: true}, 1000), "", "loading, 0 s");
eq(readerAction(rb, {url: A, loading: true}, 3999), "", "loading, 2.999 s");
eq(readerAction(rb, {url: A, loading: true}, 4000), "run", "loading, 3 s");
eq(readerAction(rb, {url: A, loading: true}, 9000), "", "only once while loading");
rb.reader = {url: A};
rb.readerPending = false;
eq(readerAction(rb, {url: A, loading: false}, 9500), "run", "re-run once loading is false");
eq(readerAction(rb, {url: A, loading: false}, 9600), "", "...and only once");
// a new URL restarts the 3 s clock; a finished page is read at once
rb = mk(); rb.reader = {url: A};
eq(readerAction(rb, {url: B, loading: true}, 100), "", "new URL starts the clock");
eq(readerAction(rb, {url: B, loading: true}, 3100), "run", "3 s after the URL change");
rb = mk();
eq(readerAction(rb, {url: A, loading: false}, 0), "run", "not loading: run at once");
// --- fast display uses the same rule
rb = mk(); rb.snap = {}; rb.snapKey = A + "\\nT";
eq(tilesAction(rb, {url: A, title: "T", loading: true}, false, 0), "", "tiles: loading 0 s");
eq(tilesAction(rb, {url: A, title: "T", loading: true}, false, 3000), "snap", "tiles: loading 3 s");
eq(tilesAction(rb, {url: A, title: "T", loading: true}, false, 6000), "", "tiles: once while loading");
eq(tilesAction(rb, {url: A, title: "T", loading: false}, false, 7000), "snap-force", "tiles: again when loaded");
eq(tilesAction(rb, {url: A, title: "T", loading: false}, true, 8000), "snap-changed", "tiles: usual if-changed");
eq(tilesAction(rb, {url: A, title: "T2", loading: false}, false, 9000), "snap", "tiles: title change");
// --- errors are surfaced; the second failure falls back to the fast display
(async () => {
  const tab = {rb: mk(), error: ""};
  tab.rb.id = "b1";
  tab.rb.url = A;
  apiError = new Error("ブラウザの操作に失敗しました");
  await runReader(tab, {quiet: true});
  eq(tab.error, "リーダー表示を作れませんでした: ブラウザの操作に失敗しました", "error text");
  eq(logs.length, 1, "logged");
  eq(tabDisplay(tab), "reader", "one failure keeps the reader");
  eq(toasts.length, 0, "quiet: no toast for the first failure");
  await runReader(tab, {quiet: true});
  eq(tabDisplay(tab), "tiles", "second failure -> fast");
  eq(tab.error, "", "error cleared on fallback");
  eq(toasts.length, 1, "one fallback toast");
  eq(toasts[0][0], "このページはリーダー表示に対応していないため高速表示にしました", "toast text");
  // only for this page
  tab.rb.url = B;
  eq(tabDisplay(tab), "reader", "other page: reader again");
  // 0 blocks -> fast at once
  apiError = null; apiResult = {blocks: [], url: B};
  await runReader(tab, {quiet: true});
  eq(tabDisplay(tab), "tiles", "0 blocks -> fast");
  eq(toasts.length, 2, "toast for 0 blocks");
  // a success resets the failure count and the error
  const t2 = {rb: mk(), error: ""};
  t2.rb.url = A;
  apiError = new Error("x");
  await runReader(t2, {quiet: true});
  apiError = null; apiResult = {blocks: [{t: "p"}], url: A};
  await runReader(t2, {quiet: true});
  eq(t2.error, "", "error cleared on success");
  apiError = new Error("y");
  await runReader(t2, {quiet: true});
  eq(tabDisplay(t2), "reader", "failures must be consecutive");
  // a non-quiet failure also toasts the message
  const t3 = {rb: mk(), error: ""};
  t3.rb.url = A;
  const before = toasts.length;
  await runReader(t3, {});
  eq(toasts.length, before + 1, "loud failure toasts");
})().catch(e => { process.stdout.write("EXC " + e.stack + "\\n"); process.exit(1); });
""" % (display, logic)
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_v0128_node_behaviour_of_display_choice_and_hit_testing(self):
        if shutil.which("node") is None:
            self.skipTest("node missing")
        pieces = [
            self.js[self.js.index("// The display the user chose"):self.js.index("// 「表示: リーダー / 高速 / ライブ」 from the menu")],
            self.js[self.js.index("// Link hit testing (all displays"):self.js.index("async function tileTap(")],
        ]
        script = """
let host = {mode: null};
const state = {settings: null};
const REMOTE_MODE = "remote", REMOTE_FAST_MODE = "remote-fast", REMOTE_LIVE_MODE = "remote-live",
  REMOTE_READER_MODE = "remote-reader", RB_LINK_NEAR_PX = 28;
const DISPLAY_OF_MODE = {[REMOTE_FAST_MODE]: "tiles", [REMOTE_LIVE_MODE]: "live", [REMOTE_READER_MODE]: "reader"};
const DISPLAY_OF_SETTING = {reader: "reader", fast: "tiles", live: "live"};
const hostOf = u => u ? new URL(u).hostname : "";
const rememberedMode = h => host.mode;
%s
const tab = rb => ({rb, current: "https://example.com/a"});
const eq = (a, b, m) => { if (a !== b) { console.error(m + ": " + a + " != " + b); process.exit(1); } };
eq(tabDisplay(null), "tiles", "no rb");
eq(tabDisplay(tab({})), "tiles", "default is the fast display");
// the Settings default applies where the site has no explicit choice (plain "remote" or none)
state.settings = {rb_default_mode: "reader"};
eq(tabDisplay(tab({})), "reader", "settings: reader");
host.mode = "remote";
eq(tabDisplay(tab({})), "reader", "plain remote follows the setting");
state.settings = {rb_default_mode: "live"};
eq(tabDisplay(tab({})), "live", "settings: live");
state.settings = {rb_default_mode: "bogus"};
eq(tabDisplay(tab({})), "tiles", "unknown setting -> fast");
// a per-host choice overrides the setting
state.settings = {rb_default_mode: "reader"};
host.mode = "remote-fast";
eq(tabDisplay(tab({})), "tiles", "host: fast beats reader");
host.mode = "remote-live";
eq(tabDisplay(tab({})), "live", "host: live");
state.settings = {rb_default_mode: "fast"};
host.mode = "remote-reader";
eq(tabDisplay(tab({})), "reader", "host: reader beats fast");
eq(userDisplay(tab({chal: {live: true}})), "reader", "userDisplay ignores the check's live switch");
// a Cloudflare check forces live meanwhile, whatever the choice
eq(tabDisplay(tab({chal: {live: true}})), "live", "the user's 確認画面を操作する shows live");
eq(tabDisplay(tab({chal: {live: false}})), tabDisplay(tab({})), "a held check keeps the chosen display");
// hit testing: inside, near (<= 28), outside, ties
const links = [{x: 10, y: 100, w: 50, h: 20, href: "a"}, {x: 30, y: 105, w: 50, h: 20, href: "b"}];
eq(pickLink(links, 11, 101).href, "a", "inside the first box only");
eq(pickLink(links, 40, 110).href, "a", "inside both: equal area -> the first");
eq(pickLink([{x: 0, y: 0, w: 200, h: 100, href: "big"}, {x: 50, y: 40, w: 20, h: 10, href: "small"}], 55, 45).href,
  "small", "inside both: smaller area wins");
eq(pickLink(links, 62, 110).href, "b", "right of b by 0 is inside b");
eq(pickLink(links, 80 + 28, 110).href, "b", "exactly 28 px away is near");
eq(pickLink(links, 80 + 29, 110), null, "29 px away is outside");
eq(pickLink([links[0]], 20, 120 + 20).href, "a", "20 px below a is near a");
eq(pickLink([links[0]], 20, 120 + 28).href, "a", "28 px below a is near a");
eq(pickLink([links[0]], 20, 120 + 29), null, "29 px below a is outside");
eq(pickLink([links[0]], 20, 100 - 28).href, "a", "28 px above a is near a");
eq(pickLink([links[0]], 10 - 28, 110).href, "a", "28 px left of a is near a");
eq(pickLink(links, 200, 300), null, "far away -> none (server resolves by coordinates)");
// nearest wins over a farther one
eq(pickLink([{x: 0, y: 0, w: 10, h: 10, href: "far"}, {x: 40, y: 0, w: 10, h: 10, href: "near"}], 30, 5).href,
  "near", "nearest box");
// equal distance: the smaller box wins
eq(pickLink([{x: 0, y: 0, w: 20, h: 20, href: "big"}, {x: 60, y: 0, w: 10, h: 10, href: "small"}], 40, 5).href,
  "small", "tie -> smaller area");
// corner distance is euclidean, not per axis
eq(pickLink([{x: 0, y: 0, w: 10, h: 10, href: "c"}], 10 + 25, 10 + 25), null, "corner at ~35 px is outside");
eq(pickLink([{x: 0, y: 0, w: 10, h: 10, href: "c"}], 10 + 15, 10 + 15).href, "c", "corner at ~21 px is near");
eq(pickLink(null, 1, 1), null, "no links");
eq(linkAt({links}, 11, 101).href, "a", "linkAt wraps pickLink");
eq(linkAt(null, 1, 1), null, "no snapshot");
"""  % "\n".join(pieces)
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)


class ItemActionStaticTests(unittest.TestCase):
    """v0.1.29: swipe rows, action sheet, multi-select; automatic live display."""

    @classmethod
    def setUpClass(cls):
        cls.js = (WEB / "app.js").read_text(encoding="utf-8")
        cls.html = (WEB / "index.html").read_text(encoding="utf-8")
        cls.css = (WEB / "style.css").read_text(encoding="utf-8")

    def test_swipe_uses_pointer_events_with_an_axis_lock(self):
        for token in ('"pointerdown"', '"pointermove"', '"pointerup"', '"pointercancel"',
                      "setPointerCapture", "SWIPE_AXIS_PX = 10", "SWIPE_FULL = 0.6"):
            self.assertIn(token, self.js)
        self.assertRegex(self.css, r"touch-action:\s*pan-y")
        self.assertIn('axis = "y"', self.js)
        self.assertIn("prefers-reduced-motion", self.css)
        self.assertIn("reducedMotion()", self.js)

    def test_rows_have_the_menu_fallback_and_the_safe_default(self):
        self.assertIn('"row-menu"', self.js)
        self.assertIn("操作メニュー", self.js)
        self.assertIn("aria-haspopup", self.js)
        for label in ("削除", "開始", "停止", "ファイルも削除", "キャンセル", "元に戻せません"):
            self.assertIn(label, self.js)
        # The default (primary, focused) option keeps the files; the destructive one is separate.
        self.assertRegex(self.js, r'一覧から削除（ファイルは残す）", primary: true')
        self.assertIn("delete_files: false", self.js)
        self.assertIn("delete_files: true, confirm_name: item.name", self.js)
        self.assertIn("confirm(", self.js)
        for ident in ("actionSheet", "selectBar", "selectStart", "selectStop", "selectDelete"):
            self.assertIn('id="%s"' % ident, self.html)
        self.assertIn('role="dialog"', self.html)

    def test_long_press_selects_and_waiting_rows_swipe(self):
        self.assertIn("LONG_PRESS_MS", self.js)
        self.assertIn("enterSelect(", self.js)
        self.assertIn('"/api/jd2/waiting/" + action', self.js)
        self.assertIn('"/api/jd2/items/" + action', self.js)
        self.assertIn("holdRender()", self.js)          # auto-refresh never rebuilds a held row

    def test_no_innerhtml_in_the_new_code(self):
        for token in ("innerHTML", "outerHTML", "insertAdjacentHTML"):
            self.assertNotIn(token, self.js)

    def test_challenge_hold_is_never_persisted_and_live_is_user_initiated(self):
        a = self.js.index("const CHAL_BUTTON_MS")
        b = self.js.index("// The stage is the viewer")
        region = self.js[a:b]
        self.assertNotIn("rememberHostMode", region)
        self.assertNotIn("history.", region)                 # no history side effects at all
        self.assertNotIn("pushState", region)
        self.assertIn("chalGoLive", region)
        self.assertIn('id="chalLiveBtn"', self.html)
        self.assertIn("確認画面を操作する", self.html)
        self.assertIn("安全性の確認中…（自動で進みます）", self.html)
        self.assertIn("CHAL_BUTTON_MS = 25000", region)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_challenge_hold_logic(self):
        a = self.js.index("const CHAL_BUTTON_MS")
        b = self.js.index("// The stage is the viewer")
        script = """
const timers = [], calls = [];
const setTimeout = (fn, ms) => { timers.push([fn, ms]); return timers.length; };
const clearTimeout = () => {};
const dlog = () => {}, gestureTrace = () => {};
const shown = {};
const setShown = (id, on) => { shown[id] = on; };
const activeWebTab = () => TAB;
const applyDisplay = () => calls.push("applyDisplay");
const tabDisplay = () => MODE;
const runMirror = async () => { calls.push("mirror"); }, runReader = async () => { calls.push("reader"); };
const takeSnapshot = async () => { calls.push("tiles"); };
let MODE = "mirror";
""" + self.js[a:b] + """
const assert = require("assert");
const TAB = {rb: {chal: null, mirrorPending: false}};
syncChallenge(TAB, false);
assert.strictEqual(TAB.rb.chal, null);
syncChallenge(TAB, true);                              // held: the chosen display stays, status line on
assert.ok(challengeHold(TAB));
assert.strictEqual(shown.chalStatus, true);
assert.strictEqual(shown.chalLiveBtn, false);
assert.strictEqual(timers[0][1], 25000);
timers[0][0]();                                        // 25 s later: the button appears
assert.strictEqual(shown.chalLiveBtn, true);
syncChallenge(TAB, false);                             // gone: the real page is rendered at once, in mirror
assert.ok(!challengeHold(TAB));
assert.deepStrictEqual(calls, ["mirror"]);
assert.strictEqual(shown.chalStatus, false);
for (const mode of ["reader", "tiles"]) {
  calls.length = 0; MODE = mode;
  syncChallenge(TAB, true); syncChallenge(TAB, false);
  assert.deepStrictEqual(calls, [mode]);
}
// the user's live switch: back through applyDisplay (the chosen display), nothing rendered by hand
calls.length = 0; MODE = "mirror";
syncChallenge(TAB, true);
chalGoLive(TAB);
assert.ok(TAB.rb.chal.live && !challengeHold(TAB));
assert.deepStrictEqual(calls, ["applyDisplay"]);
syncChallenge(TAB, false);
assert.deepStrictEqual(calls, ["applyDisplay", "applyDisplay"]);
"""
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)


class SwipeDiagnosticStaticTests(unittest.TestCase):
    """v0.1.52: the swipe self-test (automatic and manual) and the gesture hooks."""

    root = Path(__file__).resolve().parents[1] / "web"
    js = (root / "app.js").read_text(encoding="utf-8")
    html = (root / "index.html").read_text(encoding="utf-8")

    def test_buttons_and_banner_exist(self):
        for ident, text in (("selftestSwipe", "スワイプだけ確認"), ("selftestManual", "手動で確認"), ("swipeOk", "できた"),
                            ("swipeBad", "できない/違う動き"), ("swipeSkip", "スキップ"), ("swipeBanner", "")):
            self.assertIn('id="%s"' % ident, self.html)
            if text:
                self.assertIn(text, self.html)
        # the banner sits in the web view, above the page and below the toolbar (it is not an overlay)
        view = self.html[self.html.index('id="view-web"'):]
        self.assertLess(view.index('id="swipeBanner"'), view.index('id="remoteView"'))
        self.assertIn("swipe: true", self.js)                 # the normal run includes the automatic section
        self.assertIn('only: "swipe"', self.js)
        self.assertIn('only: "swipe_manual"', self.js)

    def test_hooks_are_present_where_the_behaviour_happens(self):
        def body(name):
            start = self.js.index(name)
            return self.js[start:start + 4500]
        for func, events in (("function attachFlick(", ("flick.accepted", "flick.reject")),
                             ("function wireGestures()", ("pull.arm", "pull.end", "pull.start")),
                             ("function onPopState()", ("popstate", "pop.ignore")),
                             ("function armBackGuard()", ("guard.push",)),
                             ("async function stepHistory(", ("step.call", "step.moved", "step.nomove")),
                             ("function handleTap(", ("mirror.tap",)),
                             ("function applyRoute()", ("route",))):
            if func == "function handleTap(":
                text = self.js[self.js.index(func):self.js.index(func) + 300]
            else:
                text = body(func)
            for event in events:
                self.assertIn('gestureTrace("%s"' % event, text, func + " lacks " + event)

    def test_trace_is_a_noop_unless_recording(self):
        if shutil.which("node") is None:
            self.skipTest("node missing")
        a = self.js.index("let swipeRec = null;")
        b = self.js.index("// A flick on the page and a browser back")
        script = self.js[a:b] + """
const assert = require("assert");
let hits = 0;
gestureTrace("popstate", {a: 1});                       // nothing recording: must not throw or record
swipeRec = {note: (e, d) => { hits++; assert.strictEqual(e, "popstate"); }};
gestureTrace("popstate", {a: 1});
assert.strictEqual(hits, 1);
swipeRec = null;
gestureTrace("popstate");
assert.strictEqual(hits, 1);
"""
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_flick_and_popstate_are_one_step_and_back_never_leaves_the_tab(self):
        a = self.js.index("function onPopState()")
        pop = self.js[a:a + 1800]
        self.assertNotIn("history.back()", pop)               # never a fall back to the previous app screen
        self.assertIn('claimStep("pop")', pop)
        self.assertIn('claimStep("flick")', self.js)
        self.assertIn("STEP_DEDUP_MS = 400", self.js)
        step = self.js[self.js.index("async function stepHistory("):][:1400]
        self.assertIn("これより前のページはありません", step)
        self.assertIn("これより先のページはありません", step)

    def test_claim_step_logic(self):
        if shutil.which("node") is None:
            self.skipTest("node missing")
        a = self.js.index("const STEP_DEDUP_MS")
        b = self.js.index("// The handlers are kept so that")
        script = "let now = 0; const performance = {now: () => now};\n" + self.js[a:b] + """
const assert = require("assert");
assert.ok(claimStep("flick"));
now = 200; assert.ok(!claimStep("pop"), "a popstate right after a flick is the same gesture");
now = 300; assert.ok(claimStep("flick"), "the same kind is not suppressed");
now = 800; assert.ok(claimStep("pop"), "later it is a new gesture");
"""
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_diagnostic_always_cleans_up_and_never_persists_its_tab(self):
        a = self.js.index("async function swipeEnd(d)")
        end = self.js[a:a + 1700]
        for needle in ("swipeRec.stop()", 'setShown("swipeBanner", false)', "releaseRemote(tab)", "state.tabs.splice",
                       "history.replaceState(null, \"\", d.prevHash)", "applyRoute()", "swipeDiag = null"):
            self.assertIn(needle, end)
        self.assertEqual(self.js.count("await swipeEnd(d)"), 2)           # automatic and manual: in finally
        self.assertIn("filter(t => !t.diag)", self.js)                    # saveTabs
        self.assertIn("tab.diag.stubClose", self.js)                       # the pull never closes a real tab



class AppShellCacheStaticTests(unittest.TestCase):
    """v0.1.60: the app-shell Service Worker caches the static shell only, never the API or anything authenticated."""

    @classmethod
    def setUpClass(cls):
        cls.sw = (WEB / "app-sw.js").read_text(encoding="utf-8")
        cls.js = (WEB / "app.js").read_text(encoding="utf-8")

    def test_shell_list_is_static_files_only(self):
        shell = re.search(r"var SHELL = \[(.*?)\];", self.sw, re.S).group(1)
        paths = re.findall(r'"([^"]+)"', shell)
        self.assertEqual(paths, ["/", "/app.js", "/style.css", "/icon.svg", "/apple-touch-icon.png", "/icon-192.png"])
        self.assertNotIn("/api", shell)

    def test_worker_handles_only_get_same_origin_shell_requests(self):
        match = re.search(r"function shellPath\(request\) \{.*?\n\}\n", self.sw, re.S).group(0)
        for token in ('request.method !== "GET"', "url.origin !== self.location.origin", "SHELL_SET[url.pathname]"):
            self.assertIn(token, match)
        handler = re.search(r'addEventListener\("fetch".*', self.sw, re.S).group(0)
        self.assertIn("if (!path) return;", handler)          # anything else goes to the network untouched
        self.assertNotIn("/api", self.sw.replace("/api/*", ""))
        self.assertNotIn("mirror/", self.sw)

    def test_cache_is_keyed_by_version_and_old_ones_are_deleted(self):
        self.assertIn('var CACHE = "qd-shell-" + VERSION;', self.sw)
        self.assertIn("__QD_VERSION__", self.sw)
        self.assertIn("caches.delete(n)", self.sw)

    def test_app_version_matches_hub_version(self):
        import sys
        sys.path.insert(0, str(WEB.parent))
        import webapp
        self.assertEqual(re.search(r'const APP_VERSION = "([^"]+)"', self.js).group(1), webapp.VERSION)

    def test_app_registers_only_the_app_worker_and_can_reset(self):
        self.assertEqual(self.js.count('register("/app-sw.js"'), 1)
        self.assertNotIn("pt-sw", self.js)
        self.assertIn('id="appCacheClear"', (WEB / "index.html").read_text(encoding="utf-8"))
        self.assertIn("async function dropShellCache()", self.js)
        self.assertIn("window.isSecureContext === true", self.js)       # plain http: no worker, as before


class TopBarStaticTests(unittest.TestCase):
    """v0.1.60: one top bar, no floating reload button, collapse on scroll."""

    @classmethod
    def setUpClass(cls):
        cls.js = (WEB / "app.js").read_text(encoding="utf-8")
        cls.html = (WEB / "index.html").read_text(encoding="utf-8")
        cls.css = (WEB / "style.css").read_text(encoding="utf-8")

    def test_no_floating_reload_and_one_menu(self):
        self.assertNotIn("tileRefresh", self.html + self.js + self.css)
        self.assertNotIn("tile-refresh", self.css)
        self.assertEqual(self.html.count('id="menu"'), 1)
        self.assertNotIn("メニューを開く", self.js)                  # the status row's second ⋮ is gone
        for action in ("forward", "reload"):
            self.assertIn('data-action="%s"' % action, self.html)

    def test_collapse_uses_transform_and_never_writes_scrolltop(self):
        block = re.search(r"function chromeSet\(.*?\n\}\n", self.js, re.S).group(0)
        self.assertNotIn("scrollTop =", block)
        self.assertIn("chrome-collapsed", self.css)
        self.assertIn("translateY(-100%)", self.css)
        self.assertIn("prefers-reduced-motion", self.css)
        self.assertIn("chromeExtra()", re.search(r"function stageViewport\(\) \{.*?\n\}\n", self.js, re.S).group(0))

    def test_passthrough_helper_reports_scroll(self):
        helper = (WEB / "pt-helper.js").read_text(encoding="utf-8")
        self.assertIn('type: "scroll"', helper)
        self.assertIn('m.type === "scroll"', self.js)

if __name__ == "__main__":
    unittest.main()


class ReaderTapTargetsTest(unittest.TestCase):
    """v0.1.34: reader text size switch and large buttons for rows of short links."""

    def test_reader_size_and_chips(self):
        root = Path(__file__).resolve().parents[1] / "web"
        app = (root / "app.js").read_text(encoding="utf-8")
        html = (root / "index.html").read_text(encoding="utf-8")
        css = (root / "style.css").read_text(encoding="utf-8")
        self.assertIn('id="readerAppearance"', html)       # v0.1.37: the cycle button became the Aa sheet
        for name in ("function readerLinkRow", "function readerChips", "function stepReaderSize"):
            self.assertIn(name, app)
        # storage is optional: every access is guarded
        self.assertIn("try { localStorage.setItem(READER_PREFS_KEY", app)
        self.assertIn("min-height:44px", css.split(".reader-chip{", 1)[1].split("}", 1)[0])


class ReaderLinkCardsTest(unittest.TestCase):
    """v0.1.35: long link texts become cards (block) or are shortened in the middle (inline)."""

    def test_link_cards(self):
        root = Path(__file__).resolve().parents[1] / "web"
        app = (root / "app.js").read_text(encoding="utf-8")
        css = (root / "style.css").read_text(encoding="utf-8")
        for name in ("function readerCardItems", "function readerCards", "function middleEllipsis",
                     "function readerLongText"):
            self.assertIn(name, app)
        # cards stay data-href anchors: taps and long presses keep going through the NAS browser
        self.assertIn('readerLink("", it.href)', app)
        self.assertIn(".reader-card{", css)


class ReaderLinkCardsFoldTest(unittest.TestCase):
    """v0.1.36: size inside the link text, label/size in neighbouring blocks, Japanese prose stays text."""

    def test_card_detection(self):
        app = (Path(__file__).resolve().parents[1] / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn("READER_TRAIL_SIZE", app)
        self.assertIn("READER_ONLY_SIZE", app)
        self.assertIn("prev.node.remove()", app)
        # long = URL or an ASCII file-name run, never plain length (Japanese has no spaces)
        self.assertIn("READER_LONG_RUN.test(t)", app)


def css_theme(css, name):
    block = re.search(r'\.reader-scroll\[data-theme="%s"\]\{([^}]*)\}' % name, css).group(1)
    return dict(re.findall(r"--rd-([a-z-]+):(#[0-9a-fA-F]{6})", block))


def luminance(color):
    channels = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    r, g, b = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b):
    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


class ReaderLookTests(unittest.TestCase):
    """v0.1.37: reader appearance settings, themes, image viewer, progress, placeholders."""

    @classmethod
    def setUpClass(cls):
        cls.js = (WEB / "app.js").read_text(encoding="utf-8")
        cls.html = (WEB / "index.html").read_text(encoding="utf-8")
        cls.css = (WEB / "style.css").read_text(encoding="utf-8")

    def test_themes_keep_text_and_links_readable(self):
        for name in ("light", "sepia", "dark", "black"):
            tokens = css_theme(self.css, name)
            for fg in ("fg", "muted", "link"):
                for bg in ("bg", "surface"):
                    with self.subTest(theme=name, fg=fg, bg=bg):
                        self.assertGreaterEqual(contrast(tokens[fg], tokens[bg]), 4.5)
        self.assertEqual(css_theme(self.css, "black")["bg"].lower(), "#000000")

    def test_sheet_and_viewer_markup_and_controls(self):
        for ident in ("readerAppearance", "readerSheet", "readerSheetBody", "readerViewer", "rvStage", "rvImg",
                      "rvClose", "rvPrev", "rvNext", "readerProgress", "readerProgressFill"):
            self.assertIn('id="%s"' % ident, self.html)
        self.assertNotIn('id="readerSize"', self.html)
        for text in ("文字サイズ", "書体", "ゴシック", "明朝", "テーマ", "セピア", "ブラック", "行間", "本文の幅", "両端揃え",
                     "大きく", "小さく", "表示しない", "連続画像をまとめる", "見出しを強調", "進行状況バー", "既定に戻す", "自動"):
            self.assertIn(text, self.js)
        self.assertIn("Hiragino Mincho ProN", self.css)
        self.assertIn("Hiragino Kaku Gothic ProN", self.css)
        self.assertNotRegex(self.css, r"@font-face|@import|url\(")      # system fonts only, no downloads
        self.assertRegex(self.css, r"\.reader-viewer\{[^}]*touch-action:none")
        self.assertIn("prefers-reduced-motion", self.css)
        self.assertRegex(self.css, r"hanging-punctuation")
        self.assertRegex(self.css, r"text-spacing-trim")

    def test_viewer_and_sheet_build_dom_without_remote_html(self):
        start = self.js.index("// ---- v0.1.36: reader appearance")
        end = self.js.index("function readerNode(rb, block) {")
        text = self.js[start:end]
        for token in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function", "window.open", "srcdoc"):
            self.assertNotIn(token, text)
        self.assertNotRegex(text, r"(?<!dataset)\.href\s*=(?!=)")

    def test_prefs_clean_migrate_and_survive_broken_storage(self):
        if shutil.which("node") is None:
            self.skipTest("node missing")
        start = self.js.index("const READER_PREFS_KEY")
        end = self.js.index("function syncReaderProgress()")
        script = """
let store = {}, broken = false;
const localStorage = {getItem: k => { if (broken) throw new Error("x"); return k in store ? store[k] : null; },
  setItem: (k, v) => { if (broken) throw new Error("x"); store[k] = v; }};
%s
const eq = (a, b, m) => { if (JSON.stringify(a) !== JSON.stringify(b)) { console.error(m + ": " + JSON.stringify(a)); process.exit(1); } };
const D = {size: 16, font: "gothic", theme: "auto", leading: 1, width: 1, justify: true, images: "normal", group: true, headings: true, progress: true};
eq(readerPrefsLoad(), D, "defaults");
store["qd.readerSize"] = "3";
eq(readerPrefsLoad().size, 22, "old size index migrates to the nearest step (ties go down)");
store["qd.readerSize"] = "0";
eq(readerPrefsLoad().size, 14, "old small");
store["qd.readerSize"] = "9";
eq(readerPrefsLoad().size, 16, "bad old index");
readerPrefsSave(Object.assign({}, D, {theme: "sepia", size: 20}));
eq(readerPrefsLoad().theme, "sepia", "saved");
eq(readerPrefsLoad().size, 20, "saved prefs win over the old key");
store["qd.readerPrefs"] = JSON.stringify({size: 99, font: "comic", theme: "pink", leading: 7, width: -1, justify: "yes", images: "x", group: 0, headings: false, progress: false, extra: 1});
eq(readerPrefsLoad(), Object.assign({}, D, {headings: false, progress: false}), "invalid values fall back per key");
store["qd.readerPrefs"] = "not json";
eq(readerPrefsLoad(), D, "garbage");
store["qd.readerPrefs"] = "[1,2]";
eq(readerPrefsLoad(), D, "array");
broken = true;
eq(readerPrefsLoad(), D, "unreadable storage");
readerPrefsSave(D);
eq(READER_SIZE_STEPS.length >= 7 && READER_SIZE_STEPS.length <= 8, true, "steps");
eq([READER_SIZE_STEPS[0], READER_SIZE_STEPS[READER_SIZE_STEPS.length - 1]], [13, 26], "range");
""" % self.js[start:end]
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_reading_position_is_remembered_per_url(self):
        if shutil.which("node") is None:
            self.skipTest("node missing")
        start = self.js.index("const READER_POS_MAX")
        end = self.js.index("// ---- appearance sheet")
        script = """
%s
const eq = (a, b, m) => { if (JSON.stringify(a) !== JSON.stringify(b)) { console.error(m + ": " + JSON.stringify(a)); process.exit(1); } };
const rb = {reader: {data: {url: "https://a/1"}}};
readerRememberTop(rb, 120.4);
eq(readerSavedTop(rb, "https://a/1"), 120, "stored");
eq(readerSavedTop(rb, "https://a/2"), 0, "unknown url");
rb.reader = null;
eq(readerSavedTop(rb, "https://a/1"), 120, "survives dropReader");
readerRememberTop(rb, 5);
eq(readerSavedTop(rb, "https://a/1"), 120, "no data: nothing recorded");
rb.reader = {data: {url: "x"}};
for (let i = 0; i < 100; i++) { rb.reader.data.url = "u" + i; readerRememberTop(rb, i); }
eq(Object.keys(rb.readerPos).length, READER_POS_MAX, "bounded");
eq(readerSavedTop(rb, "u99"), 99, "newest kept");
""" % self.js[start:end]
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_images_have_placeholders_modes_and_a_zoom_button_for_linked_ones(self):
        for needle in ("reader-hold", "reader-zoom", "reader-grid", "is-loaded", "is-failed", "figure.style.aspectRatio",
                       "readerPrefs.group", "block.q === true", "openReaderViewer(fig)"):
            self.assertIn(needle, self.js)
        for needle in ('[data-images="full"]', '[data-images="small"]', '[data-images="none"]', ".rd-justify", ".rd-heads",
                       ".reader-quote", "rdShimmer"):
            self.assertIn(needle, self.css)
        self.assertNotRegex(self.css, r"\.rd-justify[^{]*\.reader-(h|card)")         # never justify headings or cards


class GestureAndAutoLiveTest(unittest.TestCase):
    """v0.1.37: flick back/forward, drag down from the toolbar to close, auto-live returns on busy pages."""

    def test_gestures_and_auto_live(self):
        app = (Path(__file__).resolve().parents[1] / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn("function wireGestures()", app)
        self.assertIn("stepHistory(tab, dx > 0 ? -1 : 1)", app)
        self.assertIn("GESTURE_EDGE_PX", app)             # screen edges stay with iOS
        self.assertIn("離すとタブを閉じます", app)
        self.assertIn("function claimStep(kind)", app)          # v0.1.52: a flick and a popstate are one step
        rb = (Path(__file__).resolve().parents[1] / "remote_browser.py").read_text(encoding="utf-8")
        self.assertIn("innerText)||'').length<400", rb)


class MirrorTapTest(unittest.TestCase):
    """v0.1.40: taps in the mirror are recognised from pointer events (iOS sends no click there)."""

    def test_tap_without_click(self):
        app = (Path(__file__).resolve().parents[1] / "web" / "app.js").read_text(encoding="utf-8")
        start = app.index("function wireMirrorDoc(doc)")
        block = app[start:start + 8000]
        self.assertIn('doc.addEventListener("pointerup"', block)
        self.assertIn("handleTap(event)", block)
        self.assertIn("tap.handledAt", block)          # the later click (desktop) is not handled twice


class MirrorToggleAndLostTabTest(unittest.TestCase):
    """v0.1.42: menu toggles are clicked on the NAS (no reload); lost tabs reopen without a panel."""

    def test_toggles_and_reopen(self):
        root = Path(__file__).resolve().parents[1]
        app = (root / "web" / "app.js").read_text(encoding="utf-8")
        mirror = (root / "rb_mirror.py").read_text(encoding="utf-8")
        self.assertIn("const MIRROR_TOGGLE", app)
        self.assertIn("var same=u.href.split('#')[0]===cur.href.split('#')[0];", mirror)  # "#" and this page: click
        self.assertIn("[aria-haspopup],[aria-expanded]", mirror)
        start = app.index("function remoteLost(tab)")
        self.assertIn('tab.error = "";', app[start:start + 1200])
        self.assertIn('"もう一度開く"', app)


class ReturnFasterTest(unittest.TestCase):
    """v0.1.43: NAS tab reattached after a reload, one open per tab, faster way back from auto-live."""

    def test_reattach_dedupe_and_auto_live(self):
        root = Path(__file__).resolve().parents[1]
        app = (root / "web" / "app.js").read_text(encoding="utf-8")
        rb = (root / "remote_browser.py").read_text(encoding="utf-8")
        self.assertIn("async function reattachRemote(tab)", app)
        self.assertIn("nas: t.rb ? t.rb.id", app)
        self.assertIn("tab.opening && tab.opening.url === url", app)
        self.assertIn("function renderNow(tab)", app)           # v0.1.52: the real page right after a check
        self.assertIn("self._settle(tab, before_rev, wait_load=False)", rb)


class SingleFilePackageTest(unittest.TestCase):
    """v0.1.46: a package with one file is shown as one row, not a package card with a nested file card."""

    def test_single_row(self):
        app = (Path(__file__).resolve().parents[1] / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn("function singleRow(pkg, file)", app)
        self.assertIn("singleRow(pkg, files[0]) : packageRow(pkg, files)", app)


class BrowserBackTest(unittest.TestCase):
    """v0.1.50: the browser back on a web page is a page back; the mirror frame adds no history."""

    def test_back_guard_and_frame_replace(self):
        app = (Path(__file__).resolve().parents[1] / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn("function armBackGuard()", app)
        self.assertIn('window.addEventListener("popstate", onPopState)', app)
        self.assertIn("mf.contentWindow.location.replace(new URL(docUrl, location.href).href)", app)
        self.assertNotIn("    mf.src = docUrl;\n  });", app)
