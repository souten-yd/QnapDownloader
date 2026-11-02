"""v0.1.33 UI/UX overhaul: static and node-logic checks (docs/ux-review-0131.md)."""
import re
import shutil
import subprocess
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"
HTML = (WEB / "index.html").read_text(encoding="utf-8")
JS = (WEB / "app.js").read_text(encoding="utf-8")
CSS = (WEB / "style.css").read_text(encoding="utf-8")
NODE = shutil.which("node")


def node(script):
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


def settings_html():
    start = HTML.index('id="view-settings"')
    return HTML[start:HTML.index('id="view-web"')]


class SettingsStructureTests(unittest.TestCase):
    def test_four_sections_with_subsections_in_the_specified_order(self):
        self.assertEqual(re.findall(r'<a href="#/settings/(\w+)" data-section', HTML),
                         ["jd2", "browser", "connection", "system"])
        body = settings_html()
        for section, subs in (("jd2", ["engine", "accounts", "storage", "send"]),
                              ("browser", ["nas", "rules", "bookmarks"]),
                              ("connection", ["https", "share"]),
                              ("system", ["updates", "diagnostics", "about"])):
            block = re.search(r'data-section="%s" hidden>(.*?)</section>' % section, body, re.S).group(1)
            self.assertEqual(re.findall(r'data-sub="(\w+)"', block), subs)

    def test_every_checkbox_is_a_switch_in_a_label_left_control_right_row(self):
        for tag in re.findall(r"<input[^>]*type=\"checkbox\"[^>]*>", HTML):
            self.assertIn('role="switch"', tag)
        body = settings_html()
        for ident in ("sendAutostart", "probeAutosend", "blockAds", "rbLinkBoxes", "engineAutostart"):
            row = re.search(r'<div class="set-row"[^>]*>(?:(?!<div class="set-row").)*?id="%s"' % ident, body, re.S)
            self.assertIsNotNone(row, ident)
        self.assertNotIn("rule-toggle", JS + HTML)
        self.assertIn('input.setAttribute("role", "switch")', JS)
        self.assertRegex(CSS, r"\.switch input:checked\+\.switch-track")

    def test_descriptions_are_one_line_and_long_help_is_behind_an_info_toggle(self):
        body = re.sub(r"<details class=\"hint\">.*?</details>", "", settings_html(), flags=re.S)
        body = re.sub(r"<details[^>]*class=\"fold\".*?</details>", "", body, flags=re.S)
        body = re.sub(r"<pre.*?</pre>|<code.*?</code>", "", body, flags=re.S)
        texts = re.findall(r'<(?:span|p) class="[^"]*set-text[^"]*"[^>]*>([^<]*)<', body)
        texts += re.findall(r'<p class="muted"[^>]*>([^<]{1,400})</p>', body)
        self.assertGreater(len(texts), 8)
        for text in texts:
            self.assertLessEqual(len(text), 60, text)
        self.assertRegex(CSS, r"\.set-text\{[^}]*white-space:nowrap")
        hints = re.findall(r'<details class="hint"><summary aria-label="詳しい説明">\s*ⓘ</summary>', HTML)
        self.assertGreaterEqual(len(hints), 8)
        self.assertNotRegex(HTML, r'<details class="hint"[^>]*\sopen')           # closed by default

    def test_old_settings_routes_redirect_to_the_new_places(self):
        if not NODE:
            self.skipTest("node missing")
        block = JS[JS.index("const SETTINGS_SECTIONS"):JS.index("const ENGINE_PLACEHOLDER")] if "ENGINE_PLACEHOLDER" in JS else \
            JS[JS.index("const SETTINGS_SECTIONS"):JS.index("const SVG_NS")]
        fn = JS[JS.index("function defaultSection()"):JS.index("function settingsHash()")]
        node(block + fn + """
const assert = require("assert");
const r = (a, b) => settingsRoute(a, b);
const want = {engine: "jd2/engine", accounts: "jd2/accounts", storage: "jd2/storage", https: "connection/https",
  share: "connection/share", bookmarks: "browser/bookmarks", updates: "system/updates",
  diagnostics: "system/diagnostics", about: "system/about"};
for (const [old, target] of Object.entries(want)) {
  const route = r(old);
  assert.strictEqual(route.redirect, "#/settings/" + target, old);
  assert.strictEqual(route.section + "/" + route.sub, target);
}
assert.deepStrictEqual(r("browser"), {view: "settings", section: "browser", sub: ""});   // old = new top
assert.deepStrictEqual(r("jd2", "accounts"), {view: "settings", section: "jd2", sub: "accounts"});
assert.deepStrictEqual(r("system", "nope"), {view: "settings", section: "system", sub: ""});
assert.strictEqual(r(undefined).section, "jd2");
assert.strictEqual(r("bogus").redirect, undefined);
""")

    def test_folder_picker_is_a_bottom_sheet_that_closes_after_selection(self):
        self.assertRegex(HTML, r'<div id="folderSheet" class="sheet-backdrop" hidden>')
        self.assertIn('id="folderPicker"', HTML[HTML.index('id="folderSheet"'):])
        self.assertNotIn("folderPickerBox", HTML + JS)
        self.assertIn('setShown("folderSheet", open)', JS)
        select = JS[JS.index('"このフォルダにする"'):JS.index('"共有フォルダ一覧へ"')]
        self.assertIn("setPicker(false)", select)
        self.assertRegex(CSS, r"\.sheet-backdrop\{[^}]*align-items:flex-end")

    def test_lazy_initialisation_of_settings_and_nas_browser_handlers(self):
        boot = JS[JS.index('document.addEventListener("DOMContentLoaded"'):]
        for call in ("wireSettings()", "wireBrowser()", "wireRemote()"):
            self.assertNotIn(call, boot)
        self.assertIn("ensureSettings();", JS[JS.index("async function enterSettings"):])
        self.assertIn("ensureRemote();", JS[JS.index("async function openRemote"):])
        self.assertIn("if (remote) ensureRemote();", JS)


class LinksAndTimersTests(unittest.TestCase):
    def test_links_history_deletion_lives_in_the_header_menu_and_only_with_history(self):
        links = HTML[HTML.index('id="view-links"'):HTML.index('id="view-settings"')]
        self.assertRegex(links, r'id="linksMore"[^>]*hidden')
        self.assertNotIn("履歴を削除", links)
        self.assertIn('setShown("linksMore", q.items.length > 0)', JS)
        menu = JS[JS.index("function linksMenu()"):JS.index("async function clearHistory")]
        self.assertIn("if (history)", menu)
        self.assertIn("if (options.length) openSheet", menu)

    def test_no_intervals_and_no_scattered_status_polling(self):
        self.assertNotIn("setInterval", JS)
        for gone in ("ENGINE_POLL_MS", "BADGE_REFRESH_MS", "RB_STATUS_POLL_MS", "autoTimer", "badgeTimer",
                     "engineTimer", "rbTimer", "rbPerfTimer", "probeTicker", "syncEnginePoll",
                     "syncBrowserPoll", "syncBadgeTimer", "syncAutoRefresh", "refreshBadge", "local-preflight",
                     "diagnosticJd2"):
            self.assertNotIn(gone, JS + HTML, gone)
        self.assertNotIn('"/api/rb/frame"', JS)
        self.assertNotRegex(JS, r"/api/rb/frame\?")
        # the two status endpoints are read on demand only, never from a timer
        for call in re.finditer(r'api\("/api/(engine|rb)/status"\)', JS):
            before = JS[max(0, call.start() - 400):call.start()]
            self.assertNotRegex(before, r"setTimeout\(\w*Tick")
        self.assertEqual(len(re.findall(r"sched\.add\(", JS)), 4)
        self.assertIn('document.visibilityState !== "visible"', JS[JS.index("const sched = {"):])
        self.assertIn("/api/state?rev=", JS)

    def test_dead_remnants_are_gone(self):
        for gone in ("data:image", "jpeg_base64", "tryExternalOpen", "MyJDownloaderで"):
            self.assertNotIn(gone, JS + HTML)

    def test_every_css_selector_refers_to_an_existing_id_or_class(self):
        hay = HTML + JS
        dynamic = ("is-", "link-", "mode-", "state-", "pkg", "running", "finished", "waiting", "strict",
                   "interactive", "compat", "remote", "success", "error", "info", "progress", "sticky")
        missing = set()
        for selector, _ in re.findall(r"([^{}]+)\{([^{}]*)\}", CSS):
            for name in re.findall(r"[.#]([A-Za-z_][\w-]*)", selector):
                if name not in hay and not name.startswith(dynamic) and name not in dynamic:
                    missing.add(name)
        self.assertEqual(sorted(missing), [])

    @unittest.skipUnless(NODE, "node missing")
    def test_toasts_coalesce_by_group_and_errors_replace_older_errors(self):
        plan = JS[JS.index("function planToast("):JS.index("const toastSlot")]
        node(plan + """
const assert = require("assert");
const fmt = n => "JD2へ送信しました（" + n + "件）";
let cur = null;
cur = planToast(cur, {kind: "success", message: fmt(1), group: "sent", count: 1, format: fmt});
assert.strictEqual(cur.message, "JD2へ送信しました（1件）");
cur = planToast(cur, {kind: "success", message: fmt(2), group: "sent", count: 2, format: fmt});
assert.strictEqual(cur.message, "JD2へ送信しました（3件）");
assert.strictEqual(cur.count, 3);
// a different group (or kind) starts over
const other = planToast(cur, {kind: "success", message: "保存しました", group: "", count: 1});
assert.strictEqual(other.message, "保存しました");
const err = planToast(cur, {kind: "error", message: "失敗", group: "sent", count: 1, format: fmt});
assert.strictEqual(err.message, "失敗");
// ungrouped toasts never merge
assert.strictEqual(planToast(other, {kind: "success", message: "保存しました", group: "", count: 1}).message, "保存しました");
""")
        slot = JS[JS.index("function toast(message"):JS.index("function flash(")]
        self.assertIn('if (kind !== "error" && toastSlot.cur && toastSlot.cur.kind === "error") return;', slot)
        self.assertIn("clearToast();", slot)                               # one toast at a time
        self.assertNotIn("pruneToasts", JS)


if __name__ == "__main__":
    unittest.main()
