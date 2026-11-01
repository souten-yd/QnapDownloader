"""Static checks for the browser-style shell (web/). Stdlib only."""
import re
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"
FORBIDDEN_SANDBOX = ("allow-same-origin", "allow-popups", "allow-top-navigation",
                     "allow-downloads", "allow-modals")


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
        self.assertIn("allow-scripts allow-forms", self.js)
        self.assertIn('SANDBOX_STRICT = ""', self.js)
        values = re.findall(r'SANDBOX_\w+\s*=\s*"([^"]*)"', self.js)
        self.assertEqual(sorted(values), ["", "allow-scripts allow-forms"])
        self.assertIn('"referrerpolicy", "no-referrer"', self.js)

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

    def test_icons_use_symbol_sprite(self):
        self.assertIn("<symbol", self.html)
        self.assertIn('id="notice"', self.html)
        self.assertIn('role="status"', self.html)


if __name__ == "__main__":
    unittest.main()
