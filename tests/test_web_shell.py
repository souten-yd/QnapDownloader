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
        self.assertEqual(len(re.findall(r"\.src\s*=", self.js)), 1)
        self.assertIn("frame.src = validatedUrl", self.js)

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
        self.assertEqual(order, ["engine", "storage", "https", "share", "bookmarks", "updates", "diagnostics", "about"])
        self.assertRegex(self.js, r"AUTO_REFRESH_MS\s*=\s*5000\b")
        self.assertRegex(self.js, r"BADGE_REFRESH_MS\s*=\s*30000\b")
        self.assertRegex(self.js, r"PASTE_DEBOUNCE_MS\s*=\s*400\b")
        # Delete APIs are wired, and sending to JD2 still asks for confirmation.
        self.assertIn("/api/links/delete", self.js)
        self.assertIn("/api/bookmarks/delete", self.js)
        self.assertIn("confirm(many", self.js)

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

    def test_icons_use_symbol_sprite(self):
        self.assertIn("<symbol", self.html)
        self.assertIn('id="notice"', self.html)
        self.assertIn('role="status"', self.html)


if __name__ == "__main__":
    unittest.main()
