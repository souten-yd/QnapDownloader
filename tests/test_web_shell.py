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
        for ident in ("cfHint", "cfCompat", "cfSafari", "cfDismiss"):
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
        # Exactly four assignments: the validated iframe URL, the remote-browser
        # screencast (blob: object URLs made from /api/rb/frame.jpg bytes: an
        # off-screen probe image for decoding, then the visible image) and, since v0.1.28,
        # one tile <img> (blob: object URL of /api/rb/tile.jpg bytes).
        self.assertEqual(len(re.findall(r"\.src\s*=", self.js)), 4)
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

    def test_external_open_uses_noopener(self):
        self.assertIn('window.open(tab.current, "_blank", "noopener,noreferrer")', self.js)

    def test_engine_section_and_alias(self):
        for ident in ("engineSetup", "engineStart", "engineStop", "engineAutostart",
                      "engineSteps", "engineLog", "homeEngineSetup", "downloadsEngineAction",
                      "engineTestResult"):
            self.assertIn('id="%s"' % ident, self.html)
        self.assertIn('data-section="engine"', self.html)
        self.assertNotIn('data-section="jd2"', self.html)
        self.assertIn("SECTION_ALIASES", self.js)
        self.assertIn("#/settings/engine", self.js)
        self.assertRegex(self.js, r"ENGINE_POLL_MS\s*=\s*1500\b")
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
        self.assertEqual(order, ["engine", "accounts", "browser", "storage", "https", "share", "bookmarks", "updates", "diagnostics", "about"])
        self.assertRegex(self.js, r"AUTO_REFRESH_MS\s*=\s*5000\b")
        self.assertRegex(self.js, r"BADGE_REFRESH_MS\s*=\s*30000\b")
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
        self.assertIn('data-section="https"', self.html)
        self.assertIn('data-section="share"', self.html)
        self.assertIn("HTTPS接続", self.html)
        self.assertIn("iPhoneから送る", self.html)
        self.assertIn('href="#/settings/https"', self.html)
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

    def test_v0114_external_mode_storage(self):
        self.assertIn('EXTERNAL_MODE = "external"', self.js)
        self.assertRegex(self.js, r"e\[1\] === EXTERNAL_MODE")
        self.assertIn("rememberHostMode(host, EXTERNAL_MODE)", self.js)
        # external is a host preference, never a tab mode / sandbox value.
        modes = re.search(r"const MODES = \[(.*?)\];", self.js).group(1)
        self.assertNotIn("external", modes)
        # Reads/writes of the map stay guarded and capped.
        for fn in ("readLocal", "writeLocal", "removeLocal"):
            body = re.search(r"function %s\(.*?\n\}\n" % fn, self.js, re.S).group(0)
            self.assertIn("try", body)
            self.assertIn("catch", body)
        self.assertRegex(self.js, r"\.slice\(-MAX_HOST_MODES\)")
        # Opens externally with noopener, synchronously, then validates.
        self.assertIn('window.open(href, "_blank", "noopener,noreferrer")', self.js)
        self.assertIn("externalSanityUrl", self.js)
        self.assertIn("renderExternalHosts", self.js)
        self.assertIn('id="externalHosts"', self.html)
        self.assertIn("常にSafariで開くサイト", self.html)
        self.assertRegex(self.js, r"HINT_DELAY_MS\s*=\s*6000\b")
        self.assertIn("Safariで開きました（このサイトは常に外部で開きます）", self.js)

    def test_v0114_long_tab_copy_removed(self):
        for gone in ("隔離表示中", "枠内の遷移先URLは", "Cloudflareの確認が進まない場合",
                     "互換モードで再読み込み"):
            for path in text_files():
                with self.subTest(file=path.name, text=gone):
                    self.assertNotIn(gone, path.read_text(encoding="utf-8"))
        # The HTTPS hint now lives only in the HTTPS settings section.
        https = re.search(r'<section class="settings-section" data-section="https".*?</section>', self.html, re.S).group(0)
        self.assertIn("HTTPSで開くと通過しやすくなります", https)
        view_web = re.search(r'<section id="view-web".*?</section>', self.html, re.S).group(0)
        self.assertNotIn("HTTPSで開くと", view_web)
        self.assertIn("表示されない場合", view_web)
        self.assertIn("Safariで開く（次回から自動）", view_web)
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

    def test_v0122_image_source_is_only_a_blob_url_from_the_binary_api(self):
        assignments = re.findall(r"(\w+)\.src\s*=\s*(.+?);", self.js)
        self.assertEqual(sorted(a[0] for a in assignments), ["frame", "img", "img", "probe"])
        self.assertEqual(sorted(v for k, v in assignments if k == "img"), ["next", "t.url"])
        self.assertIn("t.url = URL.createObjectURL(blob);", self.js)
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
        self.assertIn('SETTINGS_SECTIONS = ["engine", "accounts", "browser"', self.js)

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
        section = re.search(r'<section class="settings-section" data-section="accounts".*?</section>',
                            self.html, re.S).group(0)
        for ident in ("accountsList", "accountForm", "accountHost", "accountHostList", "accountUser",
                      "accountPass", "accountAdd", "accountsSecure", "accountEditPass", "accountResult"):
            self.assertIn('id="%s"' % ident, section)
        self.assertIn('href="#/settings/https"', section)
        # Password inputs: masked, never autofilled/saved by the browser.
        for ident in ("accountPass", "accountEditPass"):
            tag = re.search(r'<input id="%s"[^>]*>' % ident, section).group(0)
            self.assertIn('type="password"', tag)
            self.assertIn('autocomplete="new-password"', tag)
        self.assertIn('list="accountHostList"', section)
        # Settings order: アカウント directly after エンジン.
        self.assertRegex(self.html, r'data-section="engine">エンジン</a>\s*<a href="#/settings/accounts" data-section="accounts">アカウント</a>')
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
        for ident in ("clearHistory", "clearStaged", "downloadsClear"):
            self.assertIn('id="%s"' % ident, self.html)
        self.assertIn("履歴を削除", self.html)
        self.assertIn("未送信のリンクも削除", self.html)
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
        text = re.search(r"function perfText\(\) \{.*?\n\}\n", self.js, re.S).group(0)
        script = """
(function (performance) {
const RB_PERF_WINDOW_MS = 5000;
const state = {rbPerf: true, rbPerfSamples: [], rbPerfSince: 0};
""" + record + text + """
let t = 0;
const mk = () => ({now: () => t});
performance = mk();
for (let i = 0; i < 10; i++) { t = 500 * (i + 1); recordPerf(20480, 100); }
t = 5000;
const out = perfText();
if (out !== "2.0 fps · 20 KB/枚 · 応答 100 ms") { console.error("bad: " + out); process.exit(1); }
// samples older than 5 s drop out; an idle page reports 0 fps
t = 20000; state.rbPerfSince = 15000;
const idle = perfText();
if (!idle.startsWith("0.0 fps")) { console.error("idle: " + idle); process.exit(1); }
// 204 responses (no frame) count for latency but not for fps or bytes
t = 21000; state.rbPerfSamples = []; state.rbPerfSince = 20500;
recordPerf(0, 300); recordPerf(1024, 100);
t = 21500;
const mixed = perfText();
if (mixed !== "1.0 fps · 1 KB/枚 · 応答 200 ms") { console.error("mixed: " + mixed); process.exit(1); }
})();
"""
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    # ---- v0.1.28: fast full-page display ------------------------------------
    def test_v0128_tiles_are_lazy_and_released(self):
        css = (WEB / "style.css").read_text(encoding="utf-8")
        for ident in ("tileScroll", "tileRefresh", "snapProgress"):
            self.assertIn('id="%s"' % ident, self.html)
        self.assertRegex(self.html, r'id="tileScroll"[^>]*\bhidden\b')
        self.assertEqual(self.js.count("new IntersectionObserver("), 2)       # load near / release far
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
        self.assertIn('RB_DISPLAY_LABELS = {tiles: "高速（全体表示）", live: "ライブ"}', self.js)
        self.assertIn('const REMOTE_LIVE_MODE = "remote-live"', self.js)
        # remembered in the existing per-host map; no entry means the fast display
        self.assertIn("rememberHostMode(host, mode === \"live\" ? REMOTE_LIVE_MODE : REMOTE_MODE)", self.js)
        self.assertRegex(self.js, r'return rememberedMode\(hostOf\(tab\.current \|\| ""\)\) === REMOTE_LIVE_MODE \? "live" : "tiles";')
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
        self.assertIn("startProbe(tab, {href: link.href})", self.js)
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

    def test_v0128_node_behaviour_of_display_choice_and_hit_testing(self):
        if shutil.which("node") is None:
            self.skipTest("node missing")
        start = self.js.index("const delay = ms =>")
        pieces = [
            self.js[self.js.index("function tabDisplay(tab) {"):self.js.index("// 「表示: 高速 / ライブ」 from the menu")],
            self.js[self.js.index("// The link box under a page point"):self.js.index("async function tileTap(")],
        ]
        script = """
let host = {mode: null};
const REMOTE_LIVE_MODE = "remote-live", RB_LINK_SLOP_PX = 3;
const hostOf = u => u ? new URL(u).hostname : "";
const rememberedMode = h => host.mode;
%s
const tab = rb => ({rb, current: "https://example.com/a"});
const eq = (a, b, m) => { if (a !== b) { console.error(m + ": " + a + " != " + b); process.exit(1); } };
eq(tabDisplay(null), "tiles", "no rb");
eq(tabDisplay(tab({autoLive: false})), "tiles", "default is the fast display");
host.mode = "remote";
eq(tabDisplay(tab({autoLive: false})), "tiles", "remote");
host.mode = "remote-live";
eq(tabDisplay(tab({autoLive: false})), "live", "remembered live");
host.mode = "remote";
eq(tabDisplay(tab({autoLive: true})), "live", "challenge forces live");
const snap = {links: [{x: 10, y: 100, w: 50, h: 20, href: "a"}, {x: 30, y: 105, w: 50, h: 20, href: "b"}]};
eq(linkAt(snap, 11, 101).href, "a", "first");
eq(linkAt(snap, 40, 110).href, "b", "later box is on top");
eq(linkAt(snap, 62, 110).href, "b", "slop");
eq(linkAt(snap, 200, 110), null, "miss");
eq(linkAt(null, 1, 1), null, "no snapshot");
""" % "\n".join(pieces)
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
        self.assertIn("高速表示に戻す", self.html)
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
