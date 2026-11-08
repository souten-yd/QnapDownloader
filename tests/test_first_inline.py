"""v0.1.59: the first copy / extraction rides on the tab-open / navigate answer (fewer round trips)."""
import os
import threading
import time
import unittest
from unittest import mock

import rb_firstload
import remote_browser as rb
from test_remote_browser import BrowserBase


class FirstInlineTests(BrowserBase):
    def navigated(self, session="SESSION1", url="https://example.com/a"):
        self.ws.emit("Page.frameStartedLoading", {"frameId": "F1"}, session)
        self.ws.emit("Page.frameNavigated", {"frame": {"url": url}}, session)
        self.barrier()

    def dcl(self, session="SESSION1"):
        self.ws.emit("Page.domContentEventFired", {}, session)
        self.barrier()

    def test_viewport_is_applied_before_the_navigation_in_the_same_batch(self):
        sent = []
        self.ws.hook = lambda m: sent.append(m["method"])
        tab = self.rb.open_tab("https://example.com/a", view={"width": 390, "height": 700, "dpr": 3, "quality": "high"})
        self.assertLess(sent.index("Emulation.setDeviceMetricsOverride"), sent.index("Page.navigate"))
        override = self.ws.calls("Emulation.setDeviceMetricsOverride")[0]["params"]
        self.assertEqual(override, {"width": 390, "height": 700, "deviceScaleFactor": 2.0, "mobile": True})
        self.assertEqual(len(self.ws.calls("Emulation.setTouchEmulationEnabled")), 1)
        # the same size afterwards is a no-op (no second CDP command)
        self.assertEqual(self.rb.set_viewport(tab["id"], 390, 700, 3, "high"), {"ok": True, "changed": False})
        self.assertEqual(len(self.ws.calls("Emulation.setDeviceMetricsOverride")), 1)

    def test_bad_view_is_refused_before_anything_is_created(self):
        for view in ({"width": 10, "height": 700, "dpr": 2}, {"width": 390, "height": 700, "dpr": 9},
                     {"width": 390, "height": 700, "dpr": 2, "quality": "x"}, "x"):
            with self.assertRaises(rb.BrowserError):
                self.rb.open_tab("https://example.com/a", view=view)
        self.assertEqual(self.ws.calls("Target.createTarget"), [])

    def test_failed_viewport_command_does_not_fail_the_open(self):
        self.ws.fail["Emulation.setDeviceMetricsOverride"] = "nope"
        tab = self.rb.open_tab("https://example.com/a", view={"width": 390, "height": 700, "dpr": 2})
        self.assertIsNone(self.rb._tabs[tab["id"]].viewport)      # set_viewport will try again later

    def test_navigate_sets_the_viewport_first(self):
        tab = self.rb.open_tab("https://example.com/a")
        self.rb.navigate(tab["id"], "https://example.com/b", view={"width": 390, "height": 700, "dpr": 2})
        methods = self.ws.methods()
        self.assertLess(methods.index("Emulation.setDeviceMetricsOverride"), len(methods) - 1)
        self.assertEqual(methods[-1], "Page.navigate")

    def test_wait_first_returns_the_mirror_copy_at_dcl(self):
        tab = self.rb.open_tab("https://example.com/a")
        self.navigated()
        copy = {"rev": 3, "doc": "/api/rb/mirror/doc?tab=b1&rev=3"}
        with mock.patch.object(self.rb, "mirror", return_value=copy) as mirror:
            timer = threading.Timer(0.1, self.dcl)
            timer.start()
            started = time.monotonic()
            out = self.rb.wait_first(tab["id"], "mirror", timeout=5)
            timer.join()
        self.assertEqual(out["mirror"], copy)
        self.assertLess(time.monotonic() - started, 3)
        mirror.assert_called_once_with("b1", full=False, partial=True)
        self.assertIn("rev", out["meta"])
        self.assertTrue(out["meta"]["dcl"])

    def test_wait_first_skips_a_check_page(self):
        # v0.1.65: a Cloudflare-like check page is not the page: no first copy is taken of it
        tab = self.rb.open_tab("https://example.com/a")
        self.navigated()
        self.dcl()
        self.rb._tabs[tab["id"]].title = "Just a moment..."
        with mock.patch.object(self.rb, "mirror") as mirror:
            out = self.rb.wait_first(tab["id"], "mirror", timeout=2)
        mirror.assert_not_called()
        self.assertIsNone(out["mirror"])
        self.assertIn("rev", out["meta"])

    def test_wait_first_bounds_the_page_side_check(self):
        # v0.1.72: the check before the first copy waits at most FIRST_CHALLENGE_BUDGET for the page's answer
        tab = self.rb.open_tab("https://example.com/a")
        self.navigated()
        self.dcl()
        budgets = []
        real = self.rb._challenge
        def spy(t, budget=None):
            budgets.append(budget)
            return False
        with mock.patch.object(self.rb, "_challenge", side_effect=spy), \
                mock.patch.object(self.rb, "mirror", return_value={"html": "<p>x</p>", "partial": False}):
            self.rb.wait_first(tab["id"], "mirror", timeout=2)
        self.assertIn(rb.FIRST_CHALLENGE_BUDGET, budgets)

    def test_wait_first_reader(self):
        tab = self.rb.open_tab("https://example.com/a")
        self.navigated()
        self.dcl()
        with mock.patch.object(self.rb, "reader", return_value={"blocks": []}) as reader:
            out = self.rb.wait_first(tab["id"], "reader", timeout=5)
        self.assertEqual(out["reader"], {"blocks": []})
        reader.assert_called_once_with("b1")

    def test_a_page_that_never_loads_times_out_with_meta_only(self):
        tab = self.rb.open_tab("https://example.com/a")
        with mock.patch.object(self.rb, "mirror", side_effect=AssertionError("no copy")):
            started = time.monotonic()
            out = self.rb.wait_first(tab["id"], "mirror", timeout=0.3)
        self.assertIsNone(out["mirror"])
        self.assertGreaterEqual(time.monotonic() - started, 0.25)
        self.assertLess(time.monotonic() - started, 3)
        self.assertIn("url", out["meta"])

    def test_a_failing_capture_returns_no_data_not_an_error(self):
        tab = self.rb.open_tab("https://example.com/a")
        self.navigated()
        self.dcl()
        with mock.patch.object(self.rb, "mirror", side_effect=rb.BrowserError("boom")):
            out = self.rb.wait_first(tab["id"], "mirror", timeout=2)
        self.assertIsNone(out["mirror"])

    def test_waiters_are_capped(self):
        tab = self.rb.open_tab("https://example.com/a")
        threads, results = [], []
        for _ in range(rb.FIRST_WAITERS_MAX):
            t = threading.Thread(target=lambda: results.append(self.rb.wait_first(tab["id"], "mirror", timeout=1.5)))
            t.start()
            threads.append(t)
        time.sleep(0.3)
        started = time.monotonic()
        extra = self.rb.wait_first(tab["id"], "mirror", timeout=5)      # no free slot: answers at once
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertIsNone(extra["mirror"])
        for t in threads:
            t.join(5)
        self.assertEqual(len(results), rb.FIRST_WAITERS_MAX)
        # the slots are free again afterwards
        for _ in range(rb.FIRST_WAITERS_MAX):
            self.assertTrue(self.rb._first_waiters.acquire(blocking=False))

    def test_navigate_resets_the_wait_for_the_new_document(self):
        tab = self.rb.open_tab("https://example.com/a")
        self.navigated()
        self.dcl()
        self.rb.navigate(tab["id"], "https://example.com/b")
        with mock.patch.object(self.rb, "mirror", side_effect=AssertionError("old page")):
            out = self.rb.wait_first(tab["id"], "mirror", timeout=0.3)        # the new page has not committed yet
        self.assertIsNone(out["mirror"])

    def test_a_fragment_only_navigation_does_not_wait(self):
        tab = self.rb.open_tab("https://example.com/a")
        self.navigated()
        self.rb._tabs[tab["id"]].url = "https://example.com/a"
        self.rb.navigate(tab["id"], "https://example.com/a#menu")
        started = time.monotonic()
        out = self.rb.wait_first(tab["id"], "mirror", timeout=5)
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertIsNone(out["mirror"])
        # a different page afterwards waits again
        self.rb.navigate(tab["id"], "https://example.com/b#x")
        self.assertFalse(self.rb._tabs[tab["id"]].nav_same_doc)

    def test_unknown_kind_is_refused(self):
        tab = self.rb.open_tab("https://example.com/a")
        with self.assertRaises(rb.BrowserError):
            self.rb.wait_first(tab["id"], "tiles")


class RttReportTests(unittest.TestCase):
    def test_rtts_are_validated_and_shown(self):
        tl = rb_firstload.Timeline()
        tl.set_client({"doc": 500}, 3)
        data = tl.breakdown()
        self.assertEqual(data["rtts"], 3)
        self.assertIn("往復 3回", rb_firstload.line(data))
        for bad in (True, -1, 1000, "2", 1.5, None):
            tl = rb_firstload.Timeline()
            tl.set_client({"doc": 500}, bad)
            self.assertIsNone(tl.breakdown()["rtts"])


APP_JS = os.path.join(os.path.dirname(__file__), "..", "web", "app.js")


def function_source(name):
    text = open(APP_JS, encoding="utf-8").read()
    start = text.index("function " + name + "(")
    end = text.index("\n}\n", start)
    return text[start:end]


class ClientSourceTests(unittest.TestCase):
    """The client side of v0.1.59 (no browser here: the E2E suite runs it for real)."""

    def test_open_remote_validates_inside_the_request(self):
        body = function_source("openRemote")
        before_rb = body[:body.index("/api/rb/tabs")]
        self.assertEqual(before_rb.count("validateUrl("), 1)          # only the 'not set up' branch
        self.assertIn("rbReady === false", before_rb[:before_rb.index("validateUrl(")])
        self.assertIn("view", body)
        self.assertIn("first", body)
        self.assertIn("rb.seed", body)

    def test_display_loops_use_the_seed_without_their_first_requests(self):
        for loop in ("mirrorLoop", "readerLoop"):
            body = function_source(loop)
            self.assertIn("takeSeed(", body)
            self.assertIn("applySeed(", body)

    def test_save_option_is_optimistic(self):
        body = function_source("saveOption")
        self.assertLess(body.index("state.settings = Object.assign"), body.index("api("))
        self.assertIn("busyControl(", body)
        self.assertIn("catch (e)", body)

    def test_state_update_skips_unchanged_parts(self):
        body = function_source("applyState")
        self.assertIn("dlChanged", body)
        self.assertIn("engineChanged", body)

    def test_request_counter_feeds_the_first_load_line(self):
        text = open(APP_JS, encoding="utf-8").read()
        self.assertIn("rtts: tl.rtts", text)
        self.assertIn("RTT_PASSIVE", text)


if __name__ == "__main__":
    unittest.main()


class StaleDocumentTests(BrowserBase):
    """v0.1.73: the document of a copy that a newer copy of the same page replaced is served as the newer one."""

    def test_older_rev_gets_the_newest_copy_and_a_newer_rev_is_gone(self):
        tab_id = self.rb.open_tab("https://example.com/a")["id"]
        tab = self.rb._tab(tab_id)
        capture = {"rev": 5, "css": "", "html": "<p>x</p>", "html_attrs": "", "body_attrs": "", "preload": ""}
        with self.rb._lock:
            tab.mirror_cache, tab.mirror_doc = capture, None
        text = self.rb.mirror_document(tab_id, 3)
        self.assertIn("<p>x</p>", text)
        self.assertEqual(tab.mirror_doc[0], 5)
        with self.assertRaises(rb.MirrorGone):
            self.rb.mirror_document(tab_id, 6)
        with self.rb._lock:
            tab.mirror_cache = None
        with self.assertRaises(rb.MirrorGone):
            self.rb.mirror_document(tab_id, 5)
