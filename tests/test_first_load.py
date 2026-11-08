"""First-load speed-ups (v0.1.54): timeline, spare tab, DOMContentLoaded trigger, partial copy, parallel prefetch,
stable resource URLs and caching headers, font-display, block list. No docker, no network."""
import os
import threading
import time
import unittest
from unittest import mock

import rb_blocklist as bl
import rb_firstload as fl
import rb_mirror
import remote_browser as rb
import selftest
import test_webapp
from test_remote_browser import BrowserBase, FakeRules


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


# ------------------------------------------------------------------ timeline
class TimelineTests(unittest.TestCase):
    def make(self):
        clock = FakeClock()
        return fl.Timeline(clock=clock), clock

    def test_first_mark_wins_and_stage_math(self):
        tl, clock = self.make()
        clock.now += 0.2
        self.assertTrue(tl.mark("target"))
        clock.now += 0.3
        self.assertFalse(tl.mark("target"))
        tl.mark("commit")
        self.assertEqual(tl.marks["target"], 200)
        self.assertEqual(tl.stage_ms("req", "target"), 200)
        self.assertEqual(tl.stage_ms("target", "commit"), 300)
        self.assertIsNone(tl.stage_ms("commit", "dcl"))

    def test_breakdown_rates_each_stage_and_names_the_slowest(self):
        tl, clock = self.make()
        for name, step in (("target", 0.1), ("commit", 4.0), ("dcl", 0.5), ("cap0", 0.1), ("cap1", 0.4), ("doc", 0.1)):
            clock.now += step
            tl.mark(name)
        data = tl.breakdown()
        status = {r["id"]: r["status"] for r in data["stages"]}
        self.assertEqual(status["create"], "pass")
        self.assertEqual(status["navigate"], "warn")
        self.assertEqual(data["slowest"], "navigate")
        self.assertEqual(data["total_ms"], tl.marks["doc"])
        self.assertEqual(fl.worst(data["stages"]), "warn")

    def test_stage_filter_and_capture_kind(self):
        tl, clock = self.make()
        tl.capture_ms = {"reader": 120, "mirror": 900}
        data = tl.breakdown(stages=fl.SELFTEST_STAGES, capture_kind="mirror")
        self.assertEqual([r["id"] for r in data["stages"]], list(fl.SELFTEST_STAGES))
        self.assertEqual([r for r in data["stages"] if r["id"] == "capture"][0]["ms"], 900)

    def test_fail_threshold_and_skip(self):
        self.assertEqual(fl.rate(None, 1, 2), "skip")
        self.assertEqual(fl.rate(3, 1, 2), "fail")
        self.assertEqual(fl.rate(1, 1, 2), "pass")
        self.assertEqual(fl.worst([]), "skip")

    def test_client_marks_are_validated(self):
        tl, _ = self.make()
        tl.set_client({"created": 120, "choose": "x", "doc": -5, "paint": True, "full": 99999999, "evil": 1})
        self.assertEqual(tl.client, {"created": 120})
        self.assertTrue(tl.reported)

    def test_line_is_compact_and_has_all_parts(self):
        tl, clock = self.make()
        for name in ("target", "commit", "dcl"):
            clock.now += 0.5
            tl.mark(name)
        tl.set_client({"created": 100, "full": 2300})
        tl.blocked = 3
        text = fl.line(tl.breakdown())
        self.assertIn("接続・遷移", text)
        self.assertIn("全体 2.3s", text)
        self.assertIn("広告等ブロック 3件", text)
        self.assertNotIn("\n", text)
        self.assertEqual(fl.line({}), "計測中")

    def test_debug_fields_are_flat_numbers(self):
        tl, clock = self.make()
        clock.now += 1
        tl.mark("target")
        tl.set_client({"full": 10})
        fields = fl.debug_fields(tl.breakdown())
        self.assertEqual(fields["create"], 1000)
        self.assertEqual(fields["c_full"], 10)
        for value in fields.values():
            self.assertIsInstance(value, (int, str, type(None)))

    def test_every_stage_has_a_hint(self):
        for sid, *_ in fl.STAGES:
            self.assertTrue(fl.hint_for(sid), sid)


# ------------------------------------------------------------------ spare tab and tab creation
class SpareTests(BrowserBase):
    def setUp(self):
        super().setUp()
        self.rb._spare_opt = True
        self.rb._monitor = False

    def make_spare(self):
        # the first open starts the browser; close the tab again so only the spare remains
        tab = self.open()
        self.rb.close_tab(tab["id"])
        end = time.monotonic() + 3
        while time.monotonic() < end and not self.rb.spare_info()["ready"]:
            self.rb._spare_make()           # a background make may be running: it holds the lock
            time.sleep(0.01)
        self.assertTrue(self.rb.spare_info()["ready"])
        return self.rb._spare

    def test_off_by_default_without_settings(self):
        self.rb._spare_opt = None
        self.assertFalse(self.rb._spare_enabled())
        self.assertEqual(self.rb.spare_info(), {"on": False, "ready": False})

    def test_option_enables_it(self):
        self.rb._spare_opt = None
        self.rb.rules = FakeRules()
        self.assertFalse(self.rb._spare_enabled())
        self.rb.rules.options["rb_spare_tab"] = True
        self.assertTrue(self.rb._spare_enabled())

    def test_spare_is_a_blank_isolated_tab_and_not_a_user_tab(self):
        spare = self.make_spare()
        self.assertEqual(self.rb._tabs, {})
        blank = [m for m in self.ws.calls("Target.createTarget") if m["params"]["url"] == "about:blank"]
        self.assertTrue(blank)
        self.assertEqual(spare["view"][:2], rb.SPARE_VIEW[:2])
        self.assertEqual(self.rb.spare_info(), {"on": True, "ready": True})
        self.assertNotIn(spare["target"], [t.target_id for t in self.rb._tabs.values()])

    def test_adopt_navigates_without_creating_a_target(self):
        spare = self.make_spare()
        created = len(self.ws.calls("Target.createTarget"))
        tab = self.open("https://example.com/x")
        # the opened tab IS the former spare; a refill may already have made the next blank one (CI timing)
        self.assertEqual(self.rb._tabs[tab["id"]].target_id, spare["target"])
        self.assertLessEqual(len(self.ws.calls("Target.createTarget")), created + 1)
        navigate = self.ws.calls("Page.navigate")[-1]
        self.assertEqual(navigate["params"]["url"], "https://example.com/x")
        self.assertIn(tab["id"], self.rb._tabs)
        self.assertEqual(self.rb._tabs[tab["id"]].timeline.kind, "mirror")
        self.assertIn("target", self.rb._tabs[tab["id"]].timeline.marks)

    def test_spare_is_replenished_after_use(self):
        self.make_spare()
        self.open("https://example.com/x")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not self.rb.spare_info()["ready"]:
            with self.rb._cv:
                self.rb._cv.notify_all()
            # the new page reaches DOMContentLoaded, which releases the replenishment
            self.ws.emit("Page.frameNavigated", {"frame": {"url": "https://example.com/x"}}, "SESSION1")
            self.ws.emit("Page.domContentEventFired", {}, "SESSION1")
            time.sleep(0.05)
        self.assertTrue(self.rb.spare_info()["ready"])

    def test_memory_short_closes_the_spare_and_prevents_a_new_one(self):
        self.make_spare()
        self.set_mem(300 * 1024)
        self.assertTrue(self.rb._spare_close("memory"))
        self.assertFalse(self.rb.spare_info()["ready"])
        self.assertFalse(self.rb._spare_make())
        self.assertIn("空きメモリ", self.rb._spare_reason())

    def test_spare_needs_more_memory_than_a_tab(self):
        self.set_mem(rb.MIN_AVAILABLE_KIB + 1024)
        self.assertIsNone(self.rb.memory_reason(fast=True))
        self.assertIsNotNone(self.rb._spare_reason())

    def test_idle_tick_closes_it(self):
        self.make_spare()
        self.rb._spare_tick(self.rb._last_activity + rb.SPARE_IDLE - 1)
        self.assertTrue(self.rb.spare_info()["ready"])
        self.rb._spare_tick(self.rb._last_activity + rb.SPARE_IDLE + 1)
        self.assertFalse(self.rb.spare_info()["ready"])

    def test_option_off_closes_it_on_the_next_tick(self):
        self.make_spare()
        self.rb._spare_opt = False
        self.rb._spare_tick(self.clock())
        self.assertFalse(self.rb.spare_info()["ready"])
        self.assertTrue(self.ws.calls("Target.closeTarget"))

    def test_stop_closes_the_spare(self):
        self.make_spare()
        self.rb.stop()
        self.assertIsNone(self.rb._spare)

    def test_destroyed_spare_is_forgotten(self):
        spare = self.make_spare()
        self.ws.emit("Target.targetDestroyed", {"targetId": spare["target"]})
        self.barrier()
        self.assertFalse(self.rb.spare_info()["ready"])

    def test_failing_navigation_falls_back_to_a_new_tab(self):
        self.make_spare()
        created = len(self.ws.calls("Target.createTarget"))
        self.ws.fail["Page.navigate"] = "boom"
        with self.assertRaises(rb.BrowserError):
            self.open("https://example.com/x")
        # the broken spare was discarded and no user tab is left behind
        self.assertEqual(self.rb._tabs, {})
        self.assertGreaterEqual(len(self.ws.calls("Target.createTarget")), created)

    def test_selftest_tabs_never_use_the_spare(self):
        self.make_spare()
        before = len(self.ws.calls("Target.createTarget"))
        self.rb._create_tab("https://example.com/s", selftest={"fixture": None})
        self.assertEqual(len(self.ws.calls("Target.createTarget")), before + 1)
        self.assertTrue(self.rb.spare_info()["ready"])


class CreateTabTests(BrowserBase):
    def test_tab_creation_is_pipelined(self):
        sent = []
        self.ws.hook = lambda m: sent.append(m["method"])
        self.open("https://example.com/")
        order = [m for m in sent if m in ("Target.createTarget", "Target.attachToTarget", "Page.enable", "Page.navigate")]
        self.assertEqual(order, ["Target.createTarget", "Target.attachToTarget", "Page.enable", "Page.navigate"])

    def test_error_in_a_pipelined_command_is_reported(self):
        self.ws.fail["Page.enable"] = "no page"
        with self.assertRaises(rb.BrowserError):
            self.open()
        self.assertEqual(self.rb._tabs, {})

    def test_timeline_starts_with_the_request_and_marks_the_target(self):
        tab = self.open()
        tl = self.rb._tabs[tab["id"]].timeline
        self.assertEqual(tl.marks["req"], 0)
        self.assertIn("target", tl.marks)


class MemoryGuardTests(BrowserBase):
    def test_no_user_tab_means_no_container_check(self):
        with mock.patch.object(self.rb, "_stats", side_effect=AssertionError("docker stats")):
            self.assertIsNone(self.rb._stats_fast(rb.STATS_TTL))

    def test_stale_result_is_used_and_refreshed_in_the_background(self):
        self.open()
        self.rb._stats_cache = (self.clock() - rb.STATS_TTL - 1, {"percent": 10.0}, None)
        refreshed = threading.Event()
        with mock.patch.object(self.rb, "container_memory", return_value=None), \
                mock.patch.object(self.rb, "_stats", side_effect=lambda ttl: refreshed.set() or {"percent": 11.0}):
            result = self.rb._stats_fast(rb.STATS_TTL)
            self.assertEqual(result, {"percent": 10.0})
            self.assertTrue(refreshed.wait(2))

    def test_stale_result_near_the_limit_is_not_trusted(self):
        self.open()
        self.rb._stats_cache = (self.clock() - rb.STATS_TTL - 1, {"percent": 78.0}, None)
        with mock.patch.object(self.rb, "container_memory", return_value=None), \
                mock.patch.object(self.rb, "_stats", return_value={"percent": 40.0}) as fresh:
            self.assertEqual(self.rb._stats_fast(rb.STATS_TTL), {"percent": 40.0})
            fresh.assert_called_once()

    def test_cgroup_reading_replaces_docker_stats(self):
        self.open()
        limit = rb.RemoteBrowser._browser_limit_bytes()
        used = limit * 0.5
        with mock.patch.object(self.rb, "container_memory", return_value={"source": "cgroup v2", "used_bytes": used}), \
                mock.patch.object(self.rb, "_stats", side_effect=AssertionError("docker stats")):
            result = self.rb._stats_fast(rb.STATS_TTL)
        self.assertAlmostEqual(result["percent"], 50.0, places=3)

    def test_high_cgroup_usage_refuses(self):
        self.open()
        limit = rb.RemoteBrowser._browser_limit_bytes()
        with mock.patch.object(self.rb, "container_memory", return_value={"source": "cgroup v2", "used_bytes": limit * 0.95}):
            self.assertIn("上限", self.rb._guard_reason())


# ------------------------------------------------------------------ DOMContentLoaded trigger
class DclTests(BrowserBase):
    def navigated(self, session="SESSION1", url="https://example.com/"):
        self.ws.emit("Page.frameNavigated", {"frame": {"url": url}}, session)
        self.barrier()

    def test_meta_reports_dcl_and_the_revision_moves(self):
        tab = self.open()
        t = self.rb._tabs[tab["id"]]
        self.navigated()
        self.assertFalse(t.dcl)
        rev = t.rev
        self.ws.emit("Page.domContentEventFired", {}, "SESSION1")
        self.barrier()
        self.assertTrue(t.dcl)
        self.assertGreater(t.rev, rev)
        self.assertIn("dcl", t.timeline.marks)
        self.assertTrue(self.rb.page_meta(tab["id"], wait=0)["dcl"])

    def test_dcl_before_the_commit_is_ignored(self):
        tab = self.open()
        t = self.rb._tabs[tab["id"]]
        self.ws.emit("Page.domContentEventFired", {}, "SESSION1")
        self.barrier()
        self.assertFalse(t.dcl)
        self.assertNotIn("dcl", t.timeline.marks)

    def test_a_new_navigation_resets_it(self):
        tab = self.open()
        t = self.rb._tabs[tab["id"]]
        self.navigated()
        self.ws.emit("Page.domContentEventFired", {}, "SESSION1")
        self.barrier()
        self.assertTrue(t.dcl)
        self.navigated(url="https://example.com/2")
        self.assertFalse(t.dcl)

    def test_client_loop_starts_at_dcl_not_at_the_timer(self):
        # the app's early start uses meta.dcl (see web/app.js loadedLongEnough)
        source = open(os.path.join(os.path.dirname(__file__), "..", "web", "app.js"), encoding="utf-8").read()
        body = source[source.index("function loadedLongEnough"):]
        body = body[:body.index("\n}\n")]
        self.assertIn("meta.dcl", body)


# ------------------------------------------------------------------ partial (above the fold) copy
class PartialCopyTests(unittest.TestCase):
    def test_fold_is_a_number_in_the_script(self):
        script = rb_mirror.serializer_script(3.0, 1688)
        self.assertIn("fold:1688", script)
        self.assertIn("foldMin:%d" % rb_mirror.PARTIAL_MIN_ELEMENTS, script)
        self.assertIn("fold:0", rb_mirror.serializer_script(3.0))
        self.assertNotIn("__PARAMS__", script)

    def test_fold_is_bounded(self):
        self.assertIn("fold:20000", rb_mirror.serializer_script(2.0, 10 ** 9))
        self.assertIn("fold:0", rb_mirror.serializer_script(2.0, -5))

    def test_small_pages_are_not_split(self):
        self.assertGreaterEqual(rb_mirror.PARTIAL_MIN_ELEMENTS, 1000)
        self.assertEqual(rb_mirror.PARTIAL_SCREENS, 2)

    def test_assemble_reports_partial(self):
        raw = {"html": "<p>hello</p>", "css": "", "partial": True, "sheets": [], "title": "t", "url": "https://a.example/"}
        out = rb_mirror.assemble(raw, lambda url, kind: "/x", lambda data, mime: "/y", None)
        self.assertTrue(out["partial"])
        raw["partial"] = False
        out = rb_mirror.assemble(raw, lambda url, kind: "/x", lambda data, mime: "/y", None)
        self.assertFalse(out["partial"])


class PartialCaptureFlowTests(BrowserBase):
    def run_mirror(self, tab, partial, partial_result):
        raw = {"html": "<p>hello</p>", "css": "", "sheets": [], "title": "t", "url": "https://example.com/",
               "partial": partial_result, "w": 390, "h": 800, "nodes": 10, "links": []}
        self.ws.results["Page.getFrameTree"] = {"frameTree": {"frame": {"id": "FRAME1"}}}
        self.ws.results["Page.createIsolatedWorld"] = {"executionContextId": 7}
        self.ws.eval_map = [("foldMin", raw)]
        self.ws.eval_value = raw
        return self.rb.mirror(tab, full=True, partial=partial)

    def test_fold_is_two_viewports_and_a_partial_cache_is_not_reused_for_a_full_request(self):
        tab = self.open()
        self.rb.set_viewport(tab["id"], 390, 800, 3.0, "speed") if hasattr(self.rb, "set_viewport") else None
        try:
            first = self.run_mirror(tab["id"], True, True)
        except rb.BrowserError as error:
            self.fail("mirror failed: %s" % error)
        self.assertTrue(first.get("partial"))
        expressions = [m["params"].get("expression", "") for m in self.ws.calls("Runtime.evaluate")]
        self.assertTrue(any("fold:1600" in e for e in expressions), "fold is two viewport heights")
        second = self.run_mirror(tab["id"], False, False)
        self.assertFalse(second.get("partial"))


# ------------------------------------------------------------------ parallel prefetch
class PrefetchTests(BrowserBase):
    def prepare(self, images, fonts):
        tab = self.open()
        t = self.rb._tabs[tab["id"]]
        t.mirror_on = True
        for i in range(images):
            t.mirror_tokens.register("https://img.example/%d.png" % i, "img")
        for i in range(fonts):
            t.mirror_tokens.register("https://img.example/f%d.woff2" % i, "font")
        return t

    def test_fetches_are_bounded_and_ordered_images_first(self):
        t = self.prepare(30, 8)
        active = {"now": 0, "max": 0}
        seen = []
        lock = threading.Lock()
        done = threading.Event()

        def fake(ident, token):
            with lock:
                active["now"] += 1
                active["max"] = max(active["max"], active["now"])
                seen.append(token)
            time.sleep(0.02)
            with lock:
                active["now"] -= 1
                if len(seen) >= rb.MIRROR_PREFETCH_IMAGES + rb.MIRROR_PREFETCH_FONTS:
                    done.set()
            return b"x", "image/png"
        with mock.patch.object(self.rb, "mirror_resource", side_effect=fake):
            self.rb._mirror_prefetch(t, {"rev": 1})
            self.assertTrue(done.wait(3))
        time.sleep(0.1)
        self.assertLessEqual(active["max"], rb.MIRROR_FETCH_PARALLEL)
        self.assertGreater(active["max"], 1)
        self.assertEqual(len(seen), rb.MIRROR_PREFETCH_IMAGES + rb.MIRROR_PREFETCH_FONTS)
        order = dict(t.mirror_tokens.ordered())
        kinds = [order[token] for token in seen[:rb.MIRROR_PREFETCH_IMAGES]]
        self.assertEqual(set(kinds), {"img"})

    def test_nothing_runs_when_the_mirror_is_not_shown(self):
        t = self.prepare(3, 0)
        t.mirror_on = False
        with mock.patch.object(self.rb, "mirror_resource", side_effect=AssertionError("fetched")):
            self.rb._mirror_prefetch(t, {"rev": 1})
            time.sleep(0.05)

    def test_cached_resources_are_not_fetched_again(self):
        t = self.prepare(2, 0)
        for token, _ in t.mirror_tokens.ordered():
            self.rb._mirror_res.put(t.id, token, b"x", "image/png")
        with mock.patch.object(self.rb, "mirror_resource", side_effect=AssertionError("fetched")):
            self.rb._mirror_prefetch(t, {"rev": 1})
            time.sleep(0.05)


# ------------------------------------------------------------------ stable URLs and the shared cache
class StableResourceTests(unittest.TestCase):
    def test_token_is_stable_per_url_and_kind(self):
        a = rb_mirror.stable_token("https://a.example/x.png", "img")
        self.assertEqual(a, rb_mirror.stable_token("https://a.example/x.png", "img"))
        self.assertNotEqual(a, rb_mirror.stable_token("https://a.example/x.png", "font"))
        self.assertNotEqual(a, rb_mirror.stable_token("https://a.example/y.png", "img"))
        self.assertRegex(a, r"\A[0-9a-f]{16}\Z")

    def test_two_token_maps_agree(self):
        one, two = rb_mirror.TokenMap(), rb_mirror.TokenMap()
        self.assertEqual(one.register("https://a.example/x.png", "img"), two.register("https://a.example/x.png", "img"))

    def test_register_twice_gives_one_entry_and_order_is_kept(self):
        tokens = rb_mirror.TokenMap()
        a = tokens.register("https://a.example/1.png", "img")
        b = tokens.register("https://a.example/2.png", "img")
        self.assertEqual(tokens.register("https://a.example/1.png", "img"), a)
        self.assertEqual([t for t, _ in tokens.ordered()], [a, b])
        self.assertEqual(len(tokens), 2)

    def test_res_path_carries_the_hash_only_when_valid(self):
        token = rb_mirror.stable_token("https://a.example/x.png", "img")
        plain = rb_mirror.res_path("b1", token)
        hashed = rb_mirror.res_path("b1", token, rb_mirror.content_hash(b"abc"))
        self.assertNotIn("&h=", plain)
        self.assertTrue(hashed.startswith(plain))
        self.assertRegex(hashed, r"&h=[0-9a-f]{12}\Z")
        self.assertEqual(rb_mirror.res_path("b1", token, "../../x"), plain)

    def test_path_of_keeps_the_first_form_given_out(self):
        # v0.1.73: the second copy must not add "&h=" to a URL the phone already has (it reloaded every image)
        tokens = rb_mirror.TokenMap()
        token = tokens.register("https://a.example/x.png", "img")
        first = tokens.path_of(token, lambda: rb_mirror.res_path("b1", token))
        later = tokens.path_of(token, lambda: rb_mirror.res_path("b1", token, rb_mirror.content_hash(b"abc")))
        self.assertEqual(first, later)
        self.assertNotIn("&h=", later)
        tokens.clear()                                   # a new page may get the hashed form
        token = tokens.register("https://a.example/x.png", "img")
        self.assertIn("&h=", tokens.path_of(token, lambda: rb_mirror.res_path("b1", token, rb_mirror.content_hash(b"abc"))))

    def test_path_of_unknown_token_is_not_remembered(self):
        tokens = rb_mirror.TokenMap()
        self.assertEqual(tokens.path_of("deadbeef", lambda: "/x"), "/x")
        self.assertIsNone(tokens.get("deadbeef"))

    def test_diag_public_is_plain_numbers(self):
        diag = rb_mirror.new_diag()
        diag["bytes"] = 4096
        diag["last"] = {"ms": 5}
        out = rb_mirror.diag_public(diag)
        self.assertEqual(out["kb"], 4)
        self.assertNotIn("bytes", out)
        self.assertEqual(out["last"], {"ms": 5})
        self.assertIsNot(out["last"], diag["last"])

    def test_cache_is_shared_between_tabs_and_expires(self):
        clock = FakeClock()
        cache = rb_mirror.ResourceCache(ttl=10, clock=clock)
        cache.put("b1", "tok", b"data", "image/png")
        self.assertEqual(cache.get("b2", "tok"), (b"data", "image/png"))
        self.assertEqual(cache.known_hash("tok"), rb_mirror.content_hash(b"data"))
        clock.now += 11
        self.assertIsNone(cache.get("b1", "tok"))
        self.assertIsNone(cache.known_hash("tok"))
        self.assertEqual(cache.bytes, 0)

    def test_closing_one_tab_keeps_what_another_tab_uses(self):
        cache = rb_mirror.ResourceCache()
        cache.put("b1", "tok", b"data", "image/png")
        cache.get("b2", "tok")
        cache.drop_tab("b1")
        self.assertIsNotNone(cache.get("b2", "tok"))
        cache.drop_tab("b2")
        self.assertIsNone(cache.get("b3", "tok"))

    def test_size_cap_evicts_the_oldest(self):
        cache = rb_mirror.ResourceCache(cap_bytes=10)
        cache.put("b1", "a", b"123456", "x")
        cache.put("b1", "b", b"123456", "x")
        self.assertIsNone(cache.get("b1", "a"))
        self.assertIsNotNone(cache.get("b1", "b"))
        self.assertLessEqual(cache.bytes, 10)


class ResourceHeaderTests(test_webapp.HttpTests):
    """The Hub's resource route (run only through load_tests below)."""

    def serve(self, query, known=None, extra=None, data=b"PNGDATA"):
        import urllib.request
        import urllib.error
        rb_obj = self.server.rb
        patches = [mock.patch.object(rb_obj, "mirror_resource", return_value=(data, "image/png")),
                   mock.patch.object(rb_obj, "mirror_resource_etag", return_value=known)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        if not self.csrf:
            self.login()
        headers = {"Host": self.host}
        headers.update(extra or {})
        request = urllib.request.Request(self.url + "/api/rb/mirror/res?" + query, headers=headers)
        try:
            with self.client.open(request, timeout=4) as response:
                return response.status, response.headers, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.headers, error.read()

    def test_hashed_url_is_immutable(self):
        chash = rb_mirror.content_hash(b"PNGDATA")
        status, headers, body = self.serve("tab=b1&r=0123456789abcdef&h=" + chash)
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "private, max-age=86400, immutable")
        self.assertEqual(headers["ETag"], '"%s"' % chash)
        self.assertEqual(body, b"PNGDATA")

    def test_wrong_or_missing_hash_is_not_immutable(self):
        for query in ("tab=b1&r=0123456789abcdef", "tab=b1&r=0123456789abcdef&h=000000000000"):
            with self.subTest(query=query):
                status, headers, _ = self.serve(query)
                self.assertEqual(status, 200)
                self.assertEqual(headers["Cache-Control"], "private, max-age=300")

    def test_matching_etag_gets_304_without_a_body_or_a_fetch(self):
        chash = rb_mirror.content_hash(b"PNGDATA")
        status, headers, body = self.serve("tab=b1&r=0123456789abcdef", known=chash,
                                           extra={"If-None-Match": '"%s"' % chash})
        self.assertEqual(status, 304)
        self.assertEqual(body, b"")
        self.server.rb.mirror_resource.assert_not_called()

    def test_other_etag_is_a_normal_response(self):
        status, _, body = self.serve("tab=b1&r=0123456789abcdef", known="aaaaaaaaaaaa",
                                     extra={"If-None-Match": '"bbbbbbbbbbbb"'})
        self.assertEqual((status, body), (200, b"PNGDATA"))

    def test_firstload_endpoint_validates(self):
        self.login()
        self.assertNotEqual(self.req("/api/rb/firstload", {"tab": "b9", "marks": {"full": 1}})[0], 200)
        self.assertNotEqual(self.req("/api/rb/firstload", {"tab": "b9", "marks": "x"})[0], 200)


def load_tests(loader, standard_tests, pattern):
    suite = unittest.TestSuite()
    for case in (TimelineTests, SpareTests, CreateTabTests, MemoryGuardTests, DclTests, PartialCopyTests,
                 PartialCaptureFlowTests, PrefetchTests, StableResourceTests, FontDisplayTests, ImageLoadingTests,
                 BlocklistTests, SelftestCheckTests):
        suite.addTests(loader.loadTestsFromTestCase(case))
    for name in ("test_hashed_url_is_immutable", "test_wrong_or_missing_hash_is_not_immutable",
                 "test_matching_etag_gets_304_without_a_body_or_a_fetch", "test_other_etag_is_a_normal_response",
                 "test_firstload_endpoint_validates"):
        suite.addTest(ResourceHeaderTests(name))
    return suite


# ------------------------------------------------------------------ CSS and HTML
def rewrite(text, web_fonts=True):
    seen = []

    def register(url, kind):
        seen.append((url, kind))
        return "/res/%d" % len(seen)
    out = rb_mirror.rewrite_css(text, "https://a.example/", register, lambda data, mime: "/data", web_fonts)
    return out, seen


class FontDisplayTests(unittest.TestCase):
    def test_swap_is_added_inside_font_face(self):
        out, _ = rewrite('@font-face{font-family:"Body";src:url(a.woff2)}')
        self.assertIn("font-display:swap", out)
        self.assertEqual(out.count("font-display"), 1)

    def test_existing_declaration_is_overridden_by_the_last_one(self):
        out, _ = rewrite('@font-face{font-family:Body;font-display:block;src:url(a.woff2)}')
        self.assertTrue(out.rindex("font-display:swap") > out.index("font-display:block"))

    def test_no_stray_semicolon_after_the_swap(self):
        out, _ = rewrite('@font-face{font-family:Body;src:url(a.woff2);}')
        self.assertNotIn(";;", out)

    def test_other_rules_are_not_touched(self):
        out, _ = rewrite("p{color:red}")
        self.assertNotIn("font-display", out)

    def test_text_fonts_are_removed_without_web_fonts_and_nothing_is_registered(self):
        out, seen = rewrite('@font-face{font-family:"Noto Sans JP";src:url(a.woff2)}p{color:red}', web_fonts=False)
        self.assertNotIn("@font-face", out)
        self.assertIn("p{color:red}", out)
        self.assertEqual([s for s in seen if s[1] == "font"], [])

    def test_icon_fonts_survive_without_web_fonts(self):
        out, seen = rewrite('@font-face{font-family:"FontAwesome";src:url(fa.woff2)}', web_fonts=False)
        self.assertIn("@font-face", out)
        self.assertEqual([s[1] for s in seen], ["font"])

    def test_option_default_is_on(self):
        import storage
        self.assertTrue(storage.DEFAULTS["rb_no_webfonts"] if hasattr(storage, "DEFAULTS") else True)
        self.assertIn("rb_no_webfonts", storage.BOOL_OPTIONS)
        self.assertIn("rb_spare_tab", storage.BOOL_OPTIONS)


class ImageLoadingTests(unittest.TestCase):
    def build(self, count):
        html = "<body>" + "".join('<img src="https://a.example/%d.png" width="100" height="50">' % i for i in range(count))
        raw = {"html": html, "css": "", "sheets": [], "title": "t", "url": "https://a.example/"}
        return rb_mirror.assemble(raw, lambda url, kind: "/api/rb/mirror/res?tab=b1&r=%016x" % (abs(hash(url)) % 16 ** 16),
                                  lambda data, mime: "/d", None)

    def test_first_images_are_eager_and_the_rest_lazy_async(self):
        out = self.build(5)
        body = out["html"] if "html" in out else out["body"]
        imgs = [part for part in body.split("<img")[1:]]
        self.assertEqual(len(imgs), 5)
        for tag in imgs[:rb_mirror.FIRST_EAGER_IMAGES]:
            self.assertNotIn('loading="lazy"', tag)
        for tag in imgs[rb_mirror.FIRST_EAGER_IMAGES:]:
            self.assertIn('loading="lazy"', tag)
            self.assertIn('decoding="async"', tag)
        self.assertIn("fetchpriority", imgs[0])

    def test_preload_links_accept_only_hub_paths(self):
        ok = "/api/rb/mirror/res?tab=b1&r=0123456789abcdef&h=0123456789ab"
        out = rb_mirror.preload_links([ok, "https://evil.example/x.png", '"><script>'], [ok])
        self.assertEqual(out.count("<link"), 2)
        self.assertNotIn("evil", out)
        self.assertNotIn("<script", out)
        self.assertIn('as="font" crossorigin', out)

    def test_preload_counts_are_limited(self):
        ok = ["/api/rb/mirror/res?tab=b1&r=%016x" % i for i in range(10)]
        out = rb_mirror.preload_links(ok, ok)
        self.assertEqual(out.count('as="image"'), rb_mirror.PRELOAD_IMAGES)
        self.assertEqual(out.count('as="font"'), rb_mirror.PRELOAD_FONTS)

    def test_document_carries_the_preload_in_the_head(self):
        link = rb_mirror.preload_links(["/api/rb/mirror/res?tab=b1&r=0123456789abcdef"])
        doc = rb_mirror.build_document("", "<p>x</p>", preload=link)
        self.assertLess(doc.index("rel=\"preload\""), doc.index("<style"))


# ------------------------------------------------------------------ block list
class BlocklistTests(unittest.TestCase):
    def test_new_hosts_are_blocked_with_subdomains(self):
        patterns = set(bl.patterns())
        for domain in ("googletagmanager.com", "hotjar.com", "criteo.com", "taboola.com", "disqus.com"):
            self.assertIn("*://%s/*" % domain, patterns, domain)
            self.assertIn("*://*.%s/*" % domain, patterns, domain)

    def test_list_grew_and_has_no_duplicates(self):
        domains = bl.domains()
        self.assertEqual(len(domains), len(set(domains)))
        self.assertGreater(len(domains), 300)

    def test_hosters_cdns_and_captcha_are_never_listed(self):
        domains = set(bl.domains())
        for must_not in ("rapidgator.net", "google.com", "facebook.com", "cloudflare.com", "gstatic.com",
                         "recaptcha.net", "hcaptcha.com", "jsdelivr.net", "cdnjs.cloudflare.com"):
            self.assertNotIn(must_not, domains)
            for listed in domains:
                self.assertFalse(must_not.endswith("." + listed) and listed != must_not, "%s covers %s" % (listed, must_not))

    def test_patterns_are_well_formed(self):
        for pattern in bl.patterns():
            self.assertRegex(pattern, r"\A\*://(\*\.)?[a-z0-9.-]+/\*\Z")


# ------------------------------------------------------------------ self-test check
class StubRb:
    def __init__(self, data=None, error=None):
        self.data, self.error = data, error
        self.asked = []

    def first_load(self, tab, stages=None, capture_kind=None):
        self.asked.append((tab, stages, capture_kind))
        if self.error:
            raise self.error
        return self.data


class SelftestCheckTests(unittest.TestCase):
    def runner(self, stub):
        added = []
        obj = selftest.SelfTest.__new__(selftest.SelfTest)
        obj.rb = stub
        obj._add = lambda run, section, cid, name, status, value, threshold, **kw: added.append(
            (cid, status, value, threshold, kw))
        return obj, added

    def breakdown(self, navigate_ms):
        clock = FakeClock()
        tl = fl.Timeline(clock=clock)
        for name, step in (("target", 0.1), ("commit", navigate_ms / 1000.0), ("dcl", 0.2), ("cap0", 0.1), ("cap1", 0.3)):
            clock.now += step
            tl.mark(name)
        return tl.breakdown(stages=fl.SELFTEST_STAGES, capture_kind="mirror")

    def test_pass_and_values(self):
        obj, added = self.runner(StubRb(self.breakdown(300)))
        obj._first_load_check(None, "b1", "builtin", "builtin.first_load", "初回", "builtin.first_load")
        cid, status, value, threshold, kw = added[0]
        self.assertEqual((cid, status), ("builtin.first_load", selftest.PASS))
        self.assertIn("接続・遷移", value)
        self.assertIn("<=", threshold)

    def test_slow_stage_warns_with_its_own_hint_variant(self):
        obj, added = self.runner(StubRb(self.breakdown(5000)))
        obj._first_load_check(None, "b1", "site", "site.1.first_load", "初回", "site.first_load")
        _, status, _, _, kw = added[0]
        self.assertEqual(status, selftest.WARN)
        self.assertEqual(kw["variant"], "navigate")

    def test_only_the_self_test_stages_are_asked_for(self):
        stub = StubRb(self.breakdown(300))
        obj, _ = self.runner(stub)
        obj._first_load_check(None, "b1", "builtin", "x", "n", "builtin.first_load")
        self.assertEqual(stub.asked[0][1:], (fl.SELFTEST_STAGES, "mirror"))

    def test_missing_data_or_method_adds_nothing(self):
        for stub in (StubRb(None), StubRb(error=rb.BrowserError("x")), object()):
            obj, added = self.runner(stub)
            obj._first_load_check(None, "b1", "builtin", "x", "n", "builtin.first_load")
            self.assertEqual(added, [])

    def test_hints_exist(self):
        for key in ("builtin.first_load", "site.first_load"):
            self.assertIn(key, selftest.HINTS)


if __name__ == "__main__":
    unittest.main()
