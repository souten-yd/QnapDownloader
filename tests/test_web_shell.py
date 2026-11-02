"""Static checks for the browser-style shell (web/). Stdlib only."""
import re
import shutil
import subprocess
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"
FORBIDDEN_SANDBOX = ("allow-popups", "allow-top-navigation",
                     "allow-downloads", "allow-modals")
# The origin-sharing token is allowed in exactly one place: the compat constant.
ORIGIN_TOKEN = "allow-same-origin"
COMPAT_SANDBOX = "allow-scripts allow-forms allow-same-origin"


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
            body = path.read_text(encoding="utf-8")
            for token in FORBIDDEN_SANDBOX:
                with self.subTest(file=path.name, token=token):
                    self.assertNotIn(token, body)

    def test_only_allowed_sandbox_values(self):
        self.assertIn('SANDBOX_STRICT = ""', self.js)
        values = re.findall(r'SANDBOX_\w+\s*=\s*"([^"]*)"', self.js)
        self.assertEqual(sorted(values), sorted(["", "allow-scripts allow-forms", COMPAT_SANDBOX]))
        self.assertIn('"referrerpolicy", "no-referrer"', self.js)

    def test_origin_token_only_in_the_compat_constant(self):
        self.assertEqual(self.js.count(ORIGIN_TOKEN), 1)
        self.assertIn('SANDBOX_COMPAT = "%s";' % COMPAT_SANDBOX, self.js)
        for path in text_files():
            if path.name != "app.js":
                with self.subTest(file=path.name):
                    self.assertNotIn(ORIGIN_TOKEN, path.read_text(encoding="utf-8"))
        # The sandbox attribute is only ever set from sandboxFor(mode).
        self.assertEqual(len(re.findall(r'setAttribute\("sandbox"', self.js)), 1)
        self.assertIn('frame.setAttribute("sandbox", sandboxFor(tab.mode))', self.js)

    def test_compat_guard_and_remembered_modes(self):
        self.assertIn("compatAllowedFor", self.js)
        self.assertIn(".ts.net", self.js)
        self.assertRegex(self.js, r"MAX_HOST_MODES\s*=\s*100\b")
        self.assertRegex(self.js, r"MAX_LIVE_FRAMES_NARROW\s*=\s*1\b")
        self.assertRegex(self.js, r"MAX_LIVE_FRAMES_WIDE\s*=\s*3\b")
        for ident in ("cfHint", "cfCompat", "cfRemote", "cfDismiss"):
            self.assertIn('id="%s"' % ident, self.html)
        self.assertIn('name="mobile-web-app-capable"', self.html)

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

    def test_iframe_src_comes_from_validation(self):
        self.assertIn("/api/browser/validate", self.js)
        # Exactly five assignments: the validated iframe URL, the remote-browser
        # screencast (blob: object URLs made from /api/rb/frame.jpg bytes: an
        # off-screen probe image for decoding, then the visible image), since v0.1.28
        # one tile <img> (blob: object URL of /api/rb/tile.jpg bytes) and, since v0.1.30,
        # one reader <img> (blob: object URL of /api/rb/reader/img bytes) and the two places
        # that point the live <img> at the same-origin MJPEG stream URL.
        self.assertEqual(len(re.findall(r"\.src\s*=", self.js)), 8)
        self.assertIn("frame.src = validatedUrl", self.js)
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
        for text in ("既定の表示方法", "リンクの枠を表示", "元のページ表示に切替", "表示: リーダー"):
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

    def test_stored_external_host_mode_migrates_to_the_nas_browser_default(self):
        if shutil.which("node") is None:
            self.skipTest("node missing")
        pieces = [self.js[self.js.index("function readHostModes() {"):self.js.index("function rememberHostMode(")],
                  self.js[self.js.index("// One-time rewrite of stored"):self.js.index("migrateHostModes();\n\n// ----")]]
        script = """
const MODES = ["strict", "interactive", "compat"], MAX_HOST_MODES = 100, HOST_MODE_KEY = "k";
const REMOTE_MODE = "remote", REMOTE_FAST_MODE = "remote-fast", REMOTE_LIVE_MODE = "remote-live",
  REMOTE_READER_MODE = "remote-reader";
const REMOTE_MODES = [REMOTE_MODE, REMOTE_FAST_MODE, REMOTE_LIVE_MODE, REMOTE_READER_MODE];
const LEGACY_EXTERNAL_MODE = "external";
let store = null, broken = false;
const readLocal = () => { if (broken) throw new Error("x"); return store; };
const writeLocal = (k, v) => { store = v; };
%s
const eq = (a, b, m) => { if (JSON.stringify(a) !== JSON.stringify(b)) { console.error(m + ": " + JSON.stringify(a)); process.exit(1); } };
store = JSON.stringify([["a.example", "external"], ["b.example", "compat"], ["c.example", "remote-live"], ["d.example", "bogus"]]);
eq(readHostModes(), [["a.example", "remote"], ["b.example", "compat"], ["c.example", "remote-live"]], "read maps external");
migrateHostModes();
eq(JSON.parse(store), [["a.example", "remote"], ["b.example", "compat"], ["c.example", "remote-live"]], "stored copy rewritten");
broken = true;
migrateHostModes();
eq(readHostModes(), [], "unreadable storage is harmless");
""" % "\n".join(pieces)
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
        self.assertEqual(order, ["jd2", "browser", "connection", "system"])
        subs = re.findall(r'data-sub="(\w+)"', self.html)
        self.assertEqual(subs, ["engine", "accounts", "storage", "send", "nas", "rules", "bookmarks",
                                "https", "share", "updates", "diagnostics", "about"])
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
        self.assertIn("HTTPSで開くと通過しやすくなります", self.html)
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
        modes = re.search(r"const MODES = \[(.*?)\];", self.js).group(1)
        self.assertNotIn("external", modes)
        for fn in ("readLocal", "writeLocal", "removeLocal"):
            body = re.search(r"function %s\(.*?\n\}\n" % fn, self.js, re.S).group(0)
            self.assertIn("try", body)
            self.assertIn("catch", body)
        self.assertRegex(self.js, r"\.slice\(-MAX_HOST_MODES\)")
        self.assertRegex(self.js, r"HINT_DELAY_MS\s*=\s*6000\b")

    def test_v0114_long_tab_copy_removed(self):
        for gone in ("隔離表示中", "枠内の遷移先URLは", "Cloudflareの確認が進まない場合",
                     "互換モードで再読み込み"):
            for path in text_files():
                with self.subTest(file=path.name, text=gone):
                    self.assertNotIn(gone, path.read_text(encoding="utf-8"))
        # The HTTPS hint now lives only in the HTTPS settings section.
        https = re.search(r'<div class="sub" data-sub="https".*?<div class="sub" data-sub="share"', self.html, re.S).group(0)
        self.assertIn("HTTPSで開くと通過しやすくなります", https)
        view_web = re.search(r'<section id="view-web".*?</section>', self.html, re.S).group(0)
        self.assertNotIn("HTTPSで開くと", view_web)
        self.assertIn("表示されない場合", view_web)
        self.assertIn("互換で再読み込み", view_web)

    def test_v0116_remote_mode_is_a_host_preference_not_a_sandbox_mode(self):
        self.assertIn('REMOTE_MODE = "remote"', self.js)
        modes = re.search(r"const MODES = \[(.*?)\];", self.js).group(1)
        self.assertNotIn("remote", modes)
        for ident in ("remoteView", "remoteImg", "remoteStage", "remoteKbd", "remoteKbdBtn", "remoteLinks",
                      "remoteSheet", "remoteAdd", "remoteSend", "cfRemote", "menuRemote", "remoteHosts"):
            self.assertIn('id="%s"' % ident, self.html)
        for text in ("NASブラウザで開く", "リンク", "件を追加", "JD2へ送る"):
            self.assertIn(text, self.html + self.js)
        for route in ("/api/rb/frame", "/api/rb/input", "/api/rb/tabs", "/api/rb/tabs/close",
                      "/api/rb/links", "/api/rb/navigate", "/api/rb/status", "/api/rb/setup"):
            self.assertIn(route, self.js)
        self.assertIn('"/api/rapidgator/extract"', self.js)

    def test_v0130_fallback_bar_wraps_and_leads_with_the_nas_browser(self):
        css = (WEB / "style.css").read_text(encoding="utf-8")
        bar = self.html[self.html.index('id="cfHint"'):self.html.index('id="frames"')]
        ids = re.findall(r'<button[^>]*id="(cf\w+)"', bar)
        self.assertEqual(ids[:2], ["cfRemote", "cfCompat"])       # NASブラウザ first, then 互換
        for label in ("NASブラウザ</button>", "互換</button>"):
            self.assertIn(label, bar)
        self.assertRegex(bar, r'id="cfRemote" class="btn small primary" title="NASブラウザで開く"')
        self.assertIn('title="互換で再読み込み"', bar)
        rule = re.search(r"\.cf-hint\{([^}]*)\}", css).group(1)
        self.assertIn("flex-wrap:wrap", rule)
        self.assertNotIn("overflow:hidden", rule)
        self.assertIn(".cf-hint .btn.small{flex:none;min-height:36px}", css)
        self.assertRegex(css, r"@media \(max-width:600px\)\{\.cf-hint-text\{display:none\}\}")

    def test_v0122_image_source_is_only_a_blob_url_from_the_binary_api(self):
        assignments = re.findall(r"(\w+)\.src\s*=\s*(.+?);", self.js)
        self.assertEqual(sorted(a[0] for a in assignments), ["frame", "img", "img", "img", "img", "img", "probe"])
        # the only other <img> sources are the same-origin MJPEG stream URLs (v0.1.30)
        self.assertEqual(sorted(v for k, v in assignments if k == "img"),
                         ["e.url", "next", "streamUrl(rb)", "streamUrl(rb)", "t.url"])
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
            self.assertNotIn(token, self.js + self.html)

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
        # restored remote tabs are never opened automatically
        self.assertIn("remote tabs are opened on request only", self.js)
        self.assertIn("rb.width", self.js)    # taps are scaled to the reported frame size

    def test_v0116_settings_browser_section(self):
        section = re.search(r'<section class="settings-section" data-section="browser".*?</section>',
                            self.html, re.S).group(0)
        for ident in ("rbState", "rbSetup", "rbStop", "rbMemory", "rbSteps", "rbNoContainer"):
            self.assertIn('id="%s"' % ident, section)
        for text in ("初期設定（イメージ作成）", "停止", "2GiB", "80%", "5分", "10分", "Container Station"):
            self.assertIn(text, section)
        self.assertIn('SETTINGS_SECTIONS = ["jd2", "browser", "connection", "system"]', self.js)

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
        for text in ("リンク先を確認しました", "すべてJD2へ送る", "リンクに追加"):
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

    def test_v0127_parallel_probes_use_a_chip_and_never_a_blocking_sheet(self):
        self.assertNotIn("probeOpen", self.html + self.js)
        self.assertNotIn("このタブで開く", self.html + self.js)
        self.assertNotIn("probeSeq", self.js)
        self.assertIn('id="probeChip"', self.html)
        self.assertIn('id="probeFlight"', self.html)
        self.assertIn("確認中 ", self.js)
        self.assertNotIn("リンク先を確認しています", self.html + self.js)
        start = self.js.index("async function startProbe")
        body = self.js[start:self.js.index("// Pointer to page coordinates")]
        # startProbe never shows the sheet while probing; it starts its own request at once
        self.assertNotIn('setShown("probeSheet", true)', body)
        self.assertNotIn("probeSheet", body)
        self.assertNotIn("rb.probing", body)
        self.assertNotIn("cancel", body.lower())
        self.assertIn('await api("/api/rb/probe"', body)
        self.assertIn("リンクは見つかりませんでした", body)
        self.assertIn("（最終: ", body)
        # results that cannot be auto-sent are appended to one sheet
        self.assertIn("state.probeResults = (state.probeResults || []).concat(", body)
        auto = self.js[self.js.index("async function autoSendProbe"):start]
        self.assertIn("JD2へ送信しました（", auto)
        self.assertNotIn("probeSheet", auto)
        self.assertNotIn("state.probeSeq", auto)
        self.assertNotIn("stageLinks", auto)
        # the only place the sheet is shown is the post-result renderer
        self.assertEqual(self.js.count('setShown("probeSheet", groups.length > 0)'), 1)
        self.assertNotIn('setShown("probeSheet", true)', self.js)
        css = (WEB / "style.css").read_text(encoding="utf-8")
        self.assertRegex(css, r"\.probe-chip\{[^}]*height:32px")
        self.assertRegex(css, r"\.probe-chip\{[^}]*safe-area-inset-bottom")
        self.assertRegex(css, r"\.probe-sheet\{[^}]*max-height:40%")

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
        self.assertIn("完了したものを一覧から消す", self.html)
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
if (o !== "ライブ(ストリーム)・速度優先 · 9.8 fps · 22 KB/枚 · 応答 120 ms") { console.error("stream: " + o); process.exit(1); }
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

    def test_v0130_overlay_names_the_mode_and_live_uses_the_stream(self):
        text = re.search(r"function perfText\(tab = remoteTab\(\)\) \{.*?\n\}\n", self.js, re.S).group(0)
        for label in ("ライブ(ストリーム)", "ライブ(ポーリング", "高速(全体表示)", "リーダー"):
            self.assertIn(label, text)
        # live display: the <img> points at the stream; metadata still comes from the meta long-poll
        self.assertIn("/api/rb/stream.mjpg?tab=", self.js)
        loop = re.search(r"async function streamLoop\(.*?\n\}\n", self.js, re.S).group(0)
        self.assertIn("openStream(tab, tab.rb)", loop)
        self.assertIn("/api/rb/meta?tab=", loop)
        self.assertNotIn("frame.jpg", loop)
        # live prefers the stream unless this tab already fell back to frame.jpg polling
        self.assertRegex(self.js, r"if \(rb\.streamFailed \|\| typeof Image !== \"function\"\) remoteLoop\(tab, state\.rbGen\);\s+else streamLoop")
        # fallback: two errors within 10 s, or no picture within 5 s
        self.assertIn("RB_STREAM_FIRST_MS = 5000", self.js)
        self.assertIn("RB_STREAM_ERR_WINDOW_MS = 10000", self.js)
        self.assertIn("s.errors.length >= 2", self.js)
        self.assertRegex(self.js, r"function streamFallback\(tab, rb\) \{\s+rb\.streamFailed = true;")
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
        for ident in ("tileScroll", "tileRefresh", "snapProgress"):
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
        for text in ("表示: 高速（全体表示）", "表示: ライブ"):
            self.assertIn(text, self.html)
        self.assertIn('RB_DISPLAY_LABELS = {reader: "リーダー", tiles: "高速（全体表示）", live: "ライブ"}', self.js)
        self.assertIn('const REMOTE_LIVE_MODE = "remote-live"', self.js)
        # remembered in the existing per-host map; no entry follows the Settings default (v0.1.30)
        self.assertIn("rememberHostMode(host, MODE_OF_DISPLAY[mode])", self.js)
        self.assertIn("DISPLAY_OF_SETTING[setting] || \"tiles\"", self.js)
        self.assertIn("action.startsWith(\"display:\")", self.js)
        self.assertNotIn("modes.includes(REMOTE_LIVE_MODE)", self.js)
        modes = re.search(r"const MODES = \[([^\]]*)\]", self.js).group(1)
        self.assertNotIn("remote", modes)

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
        self.assertIn("function syncChallenge(tab, challenge, loading)", self.js)
        self.assertIn("X-Frame-Challenge", self.js)
        # v0.1.29: the way back is a dedicated poll with two clean checks (challengeStep).
        self.assertIn("function challengeStep(rb, challenge, loading, now, userLive)", self.js)
        self.assertIn("AUTO_CLEAN_NEEDED = 2", self.js)
        self.assertIn("if (rb.autoLive) return \"live\";", self.js)

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
const updateRemoteView = () => {};
const remoteLost = () => {};
const snapBusy = () => {};
const $ = () => ({scrollTop: 0});
const readerHost = {owner: null};
const renderReaderBody = () => {};
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
eq(tabDisplay(tab({autoLive: false})), "tiles", "default is the fast display");
// the Settings default applies where the site has no explicit choice (plain "remote" or none)
state.settings = {rb_default_mode: "reader"};
eq(tabDisplay(tab({autoLive: false})), "reader", "settings: reader");
host.mode = "remote";
eq(tabDisplay(tab({autoLive: false})), "reader", "plain remote follows the setting");
state.settings = {rb_default_mode: "live"};
eq(tabDisplay(tab({autoLive: false})), "live", "settings: live");
state.settings = {rb_default_mode: "bogus"};
eq(tabDisplay(tab({autoLive: false})), "tiles", "unknown setting -> fast");
// a per-host choice overrides the setting
state.settings = {rb_default_mode: "reader"};
host.mode = "remote-fast";
eq(tabDisplay(tab({autoLive: false})), "tiles", "host: fast beats reader");
host.mode = "remote-live";
eq(tabDisplay(tab({autoLive: false})), "live", "host: live");
state.settings = {rb_default_mode: "fast"};
host.mode = "remote-reader";
eq(tabDisplay(tab({autoLive: false})), "reader", "host: reader beats fast");
eq(userDisplay(tab({autoLive: true})), "reader", "userDisplay ignores the automatic switch");
// a Cloudflare check forces live meanwhile, whatever the choice
eq(tabDisplay(tab({autoLive: true})), "live", "challenge forces live");
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

    def test_auto_live_is_never_persisted_and_has_a_manual_way_back(self):
        a = self.js.index("function challengeStep")
        b = self.js.index("// The stage is the viewer")
        region = self.js[a:b]
        self.assertNotIn("rememberHostMode", region)
        self.assertIn("returnToFast", region)
        self.assertIn('id="autoLiveChip"', self.html)
        self.assertIn("元の表示に戻す", self.html)
        self.assertIn("wait=0", region)
        self.assertIn("document.visibilityState", region)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_challenge_step_logic(self):
        a = self.js.index("const AUTO_POLL_MS")
        b = self.js.index("function userPrefersLive")
        script = self.js[a:b] + """
const assert = require("assert");
const mk = () => ({autoLive: false, autoSince: 0, autoClean: 0, autoChip: false});
let rb = mk();
assert.strictEqual(challengeStep(rb, false, false, 0, false), null);
assert.strictEqual(challengeStep(rb, true, false, 1000, false), "enter");
assert.ok(rb.autoLive);
// one clean check is not enough; a challenge in between resets the count
assert.strictEqual(challengeStep(rb, false, false, 2500, false), null);
assert.strictEqual(challengeStep(rb, true, false, 4000, false), null);
assert.strictEqual(challengeStep(rb, false, false, 5500, false), null);
// clean but still loading does not count
assert.strictEqual(challengeStep(rb, false, true, 7000, false), null);
assert.strictEqual(rb.autoClean, 0);
assert.strictEqual(challengeStep(rb, false, false, 8500, false), null);
assert.strictEqual(challengeStep(rb, false, false, 10000, false), "return");
assert.ok(!rb.autoLive && !rb.autoChip);
// a manual live choice is never taken over
rb = mk();
assert.strictEqual(challengeStep(rb, true, false, 0, true), null);
assert.ok(!rb.autoLive);
// chip after the 90 s safety timeout, once; live stays; a later clean page still returns
rb = mk();
challengeStep(rb, true, false, 0, false);
assert.strictEqual(challengeStep(rb, true, false, 89000, false), null);
assert.strictEqual(challengeStep(rb, true, false, 90000, false), "chip");
assert.strictEqual(challengeStep(rb, true, false, 95000, false), null);
assert.ok(rb.autoLive && rb.autoChip);
challengeStep(rb, false, false, 96000, false);
assert.strictEqual(challengeStep(rb, false, false, 97500, false), "return");
"""
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()


class ReaderTapTargetsTest(unittest.TestCase):
    """v0.1.34: reader text size switch and large buttons for rows of short links."""

    def test_reader_size_and_chips(self):
        root = Path(__file__).resolve().parents[1] / "web"
        app = (root / "app.js").read_text(encoding="utf-8")
        html = (root / "index.html").read_text(encoding="utf-8")
        css = (root / "style.css").read_text(encoding="utf-8")
        self.assertIn('id="readerSize"', html)
        for name in ("function readerLinkRow", "function readerChips", "function cycleReaderSize"):
            self.assertIn(name, app)
        # storage is optional: every access is guarded
        self.assertIn("try { localStorage.setItem(READER_SIZE_KEY", app)
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
