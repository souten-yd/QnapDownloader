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
        for ident in ("engineSetup", "engineStart", "engineStop", "engineTest", "engineAutostart",
                      "engineSteps", "engineLog", "homeEngineSetup", "downloadsEngineNotice",
                      "linksEngineNotice"):
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

    def test_icons_use_symbol_sprite(self):
        self.assertIn("<symbol", self.html)
        self.assertIn('id="notice"', self.html)
        self.assertIn('role="status"', self.html)


if __name__ == "__main__":
    unittest.main()
