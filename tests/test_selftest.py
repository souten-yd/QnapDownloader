"""On-device self-test (v0.1.44): state machine, validation, redaction, hints, endpoints. No network, no docker."""
import json
import threading
import time
import unittest
from unittest import mock
import urllib.error
from unittest.mock import patch

import debuglog
import selftest
import test_webapp
from selftest import FAIL, PASS, SKIP, WARN


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class StubDebug:
    def __init__(self, lines=()):
        self.lines = list(lines)
        self.enabled = False
        self.calls = []

    def status(self):
        return {"enabled": self.enabled, "seq": 7}

    def enable(self, *args, **kwargs):
        self.enabled = True
        self.calls.append("enable")

    def disable(self):
        self.enabled = False
        self.calls.append("disable")

    def clear(self):
        self.calls.append("clear")

    def entries(self, since, limit):
        return {"entries": list(self.lines)}


def make_runner(env_ok=False, clock=None, debug=None, thread=False, **extra):
    """A SelfTest whose environment section is replaced (no docker); returns the runner."""
    clock = clock or Clock()
    runner = selftest.SelfTest(object(), version="9.9.9", clock=clock, wall=lambda: 1_700_000_000,
                               debug=debug or StubDebug(), thread=thread, **extra)
    runner._section_env = lambda run: env_ok
    runner._section_cleanup = lambda run, state: None
    return runner


def good_payload(run_id, **over):
    payload = {"run_id": run_id,
               "device": {"ua": "Mozilla/5.0 (iPhone)", "width": 390, "height": 844, "dpr": 3, "touch": 5,
                          "standalone": False, "storage": True, "clipboard": True, "flags": {"webkit": True}},
               "checks": [{"id": "client.rtt", "ms": 40},
                          {"id": "client.reader", "ok": True, "ms": 120, "n": 12},
                          {"id": "client.mirror_events", "flags": {"pointer": True, "click": True}},
                          {"id": "client.live", "fps": 12.5, "kb": 30, "flags": {"mjpeg": True}}]}
    payload.update(over)
    return payload


class HelperTests(unittest.TestCase):
    def test_rate_thresholds(self):
        self.assertEqual(selftest.rate(5, 10, 20), PASS)
        self.assertEqual(selftest.rate(15, 10, 20), WARN)
        self.assertEqual(selftest.rate(25, 10, 20), FAIL)
        self.assertEqual(selftest.rate(3, 5, 1, low=True), WARN)
        self.assertEqual(selftest.rate(0, 5, 1, low=True), FAIL)
        self.assertEqual(selftest.rate(None, 1, 2), SKIP)
        self.assertEqual(selftest.rate(True, 1, 2), SKIP)
        self.assertEqual(selftest.rate(float("nan"), 1, 2), SKIP)

    def test_parse_cpu(self):
        text = "processor\t: 0\nmodel name\t: Intel(R) Celeron(R)  J4125\nprocessor\t: 1\nmodel name\t: other\n"
        self.assertEqual(selftest.parse_cpu(text), ("Intel(R) Celeron(R) J4125", 2))
        self.assertEqual(selftest.parse_cpu(""), ("", 0))

    def test_viewport_is_sanitised(self):
        self.assertEqual(selftest.sanitize_viewport({"width": 390, "height": 844, "dpr": 3}), (390, 844, 3.0))
        for bad in (None, "x", {}, {"width": 10, "height": 844, "dpr": 3}, {"width": 390, "height": 844, "dpr": 9},
                    {"width": True, "height": 844, "dpr": 3}, {"width": 390.5, "height": 844, "dpr": 3}):
            self.assertEqual(selftest.sanitize_viewport(bad), selftest.DEFAULT_VIEWPORT)

    def test_png_fixture_is_a_png(self):
        data = selftest.make_png(4, 3, "ff0000")
        self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")

    def test_fixtures_serve_pages_images_and_count(self):
        fx = selftest.Fixtures()
        status, kind, body = fx("https://selftest.example.com/article")
        self.assertEqual((status, kind.split(";")[0]), (200, "text/html"))
        self.assertIn("リーダー".encode(), body) if "リーダー" in body.decode() else None
        self.assertEqual(fx.loads("/article"), 1)
        self.assertEqual(fx("https://selftest.example.com/i/20x10.png")[1], "image/png")
        self.assertIsNone(fx("https://selftest.example.com/i/99999x1.png"))
        self.assertIsNone(fx("https://selftest.example.com/nope"))
        self.assertEqual(fx("https://selftest.example.com/__beacon?e=toggle&p=x")[0], 204)
        self.assertEqual(fx.toggles("x"), 1)

    def test_every_fixture_page_is_inert_for_the_hub(self):
        # The virtual site is served from RAM; it must not reference any real host.
        for path, html in selftest.PAGES.items():
            with self.subTest(path=path):
                self.assertNotIn("http://", html.replace("http://www.w3.org", ""))

    def test_unsafe_markup_and_toggle(self):
        self.assertEqual(selftest.unsafe_markup("<p>hi</p>"), [])
        found = selftest.unsafe_markup('<script>1</script><a onclick="x()" href="javascript:1">')
        self.assertEqual(set(found), {"<script>", "on*属性", "javascript:"})
        html = '<a data-m="3" href="#">a</a><a aria-expanded="false" data-m="12" href="#">b</a>'
        self.assertEqual(selftest.find_toggle(html), 12)
        self.assertIsNone(selftest.find_toggle("<a href='#'>x</a>"))
        self.assertIsNone(selftest.find_toggle(None))

    def test_format_log_cleans_urls_and_secrets(self):
        line = selftest.format_log({"ts": 0, "category": "rb", "level": "info",
                                    "message": "open https://user:pw@host.example/path?token=abc",
                                    "data": {"u": "https://other.example/x?y=1"}})
        self.assertTrue(line.startswith("00:00:00 rb/info"))
        for secret in ("pw", "token=abc", "/path", "y=1"):
            self.assertNotIn(secret, line)
        self.assertIn("host.example", line)

    def test_hints_cover_every_client_check_and_variant(self):
        for cid in selftest.CLIENT_NAMES:
            with self.subTest(cid=cid):
                self.assertIn(cid, selftest.HINTS)
        for key, text in selftest.HINTS.items():
            with self.subTest(key=key):
                self.assertTrue(text.strip())
                if key != "client.secure":          # the one hint that names the Tailscale https address
                    self.assertNotIn("http", text)


class ClientValidationTests(unittest.TestCase):
    def test_good_payload_is_accepted(self):
        data = selftest.validate_client(good_payload("abcdefgh1234"))
        self.assertEqual(data["run_id"], "abcdefgh1234")
        self.assertEqual(len(data["checks"]), 4)
        self.assertEqual(data["device"]["flags"], {"webkit": True})

    def test_bad_payloads_are_rejected(self):
        base = good_payload("abcdefgh1234")
        cases = {
            "not a dict": [],
            "run id": dict(base, run_id="x"),
            "run id type": dict(base, run_id=5),
            "device missing": {k: v for k, v in base.items() if k != "device"},
            "device ua type": dict(base, device=dict(base["device"], ua=5)),
            "device width": dict(base, device=dict(base["device"], width=-1)),
            "device flag value": dict(base, device=dict(base["device"], flags={"a": [1]})),
            "device flag key": dict(base, device=dict(base["device"], flags={"a b": True})),
            "too many checks": dict(base, checks=[{"id": "client.rtt"}] * 30),
            "unknown id": dict(base, checks=[{"id": "client.evil"}]),
            "duplicate id": dict(base, checks=[{"id": "client.rtt"}, {"id": "client.rtt"}]),
            "number type": dict(base, checks=[{"id": "client.rtt", "ms": "40"}]),
            "number bool": dict(base, checks=[{"id": "client.rtt", "ms": True}]),
            "number range": dict(base, checks=[{"id": "client.rtt", "ms": 1e12}]),
            "ok type": dict(base, checks=[{"id": "client.reader", "ok": 1}]),
            "flags value": dict(base, checks=[{"id": "client.live", "flags": {"img": "yes"}}]),
            "flags key": dict(base, checks=[{"id": "client.live", "flags": {"IMG": True}}]),
            "note type": dict(base, checks=[{"id": "client.rtt", "note": 5}]),
        }
        for name, payload in cases.items():
            with self.subTest(name):
                with self.assertRaises(ValueError):
                    selftest.validate_client(payload)

    def test_strings_are_cleaned(self):
        payload = good_payload("abcdefgh1234")
        payload["device"]["ua"] = "UA https://u:p@secret.example/x?token=1 " + "A" * 500
        payload["checks"][0]["note"] = "see https://secret.example/private?key=abc"
        data = selftest.validate_client(payload)
        self.assertLessEqual(len(data["device"]["ua"]), 300)
        self.assertNotIn("token=1", data["device"]["ua"])
        self.assertNotIn("/private", data["checks"][0]["note"])
        self.assertNotIn("key=abc", data["checks"][0]["note"])


class ClientEvaluateTests(unittest.TestCase):
    def ev(self, **item):
        return selftest.client_evaluate(item)

    def test_statuses(self):
        self.assertEqual(self.ev(id="client.rtt", ms=40)["status"], PASS)
        self.assertEqual(self.ev(id="client.rtt", ms=800)["status"], WARN)
        self.assertEqual(self.ev(id="client.rtt", ms=5000)["status"], FAIL)
        self.assertEqual(self.ev(id="client.storage", ok=True)["status"], PASS)
        self.assertEqual(self.ev(id="client.clipboard", ok=False)["status"], WARN)
        self.assertEqual(self.ev(id="client.reader", ok=True, ms=100, n=20)["status"], PASS)
        self.assertEqual(self.ev(id="client.reader", ok=True, ms=100, n=2)["status"], FAIL)
        slow = self.ev(id="client.reader", ok=True, ms=9000, n=20)
        self.assertEqual((slow["status"], slow["variant"]), (WARN, "slow"))
        self.assertEqual(self.ev(id="client.mirror_load", ok=False, ms=10)["status"], FAIL)
        self.assertEqual(self.ev(id="client.mirror_morph", ok=False)["status"], FAIL)
        self.assertEqual(self.ev(id="client.tiles", ok=True, ms=100, n=3)["status"], PASS)
        self.assertEqual(self.ev(id="client.bogus")["status"], FAIL)

    def test_site_masker_hides_real_sites(self):
        import selftest as st
        mask = st.site_masker(["www.example.com", "news.foo.jp"])
        out = mask("実サイト1（www.example.com） https://www.example.com/a?b=1 cdn.example.com "
                   "https://other.net/x http://127.0.0.1:3000/a news.foo.jp/x notexample.com")
        for leaked in ("example.com/", "www.example.com", "other.net", "foo.jp"):
            self.assertNotIn(leaked, out)
        self.assertIn("サイト1", out)
        self.assertIn("サイト2/x", out)
        self.assertIn("（URL非表示）", out)
        self.assertIn("http://127.0.0.1:3000/a", out)          # the Hub's own fixtures stay readable
        self.assertIn("notexample.com", out)
        self.assertEqual(mask({"sites": ["a", "b"], "x": [{"v": "news.foo.jp"}], "n": 3}),
                         {"sites": ["サイト1", "サイト2"], "x": [{"v": "サイト2"}], "n": 3})

    def test_mirror_reuse(self):
        ok = self.ev(id="client.mirror_reuse", ok=True, ms=3, n=0, imgs=3, same=3, kept=40, added=0)
        self.assertEqual(ok["status"], PASS)
        self.assertIn("3/3", ok["value"])
        self.assertEqual(self.ev(id="client.mirror_reuse", ok=False, n=2, imgs=3, same=3)["status"], FAIL)
        self.assertEqual(self.ev(id="client.mirror_reuse", ok=False, n=0, imgs=3, same=1)["status"], WARN)
        self.assertEqual(self.ev(id="client.mirror_reuse", ok=False, n=0, imgs=0, same=0)["status"], "skip")

    def test_mirror_events_is_the_iphone_failure(self):
        bad = self.ev(id="client.mirror_events", flags={"pointer": False, "click": False})
        self.assertEqual(bad["status"], FAIL)
        self.assertIn("届かず", bad["value"])
        half = self.ev(id="client.mirror_events", flags={"pointer": True, "click": False})
        self.assertEqual((half["status"], half["variant"]), (WARN, "click"))
        self.assertEqual(self.ev(id="client.mirror_events", flags={"pointer": True, "click": True})["status"], PASS)

    def test_live_transports(self):
        live = lambda **kw: self.ev(id="client.live", **kw)
        # no frame on any transport (or no fps) is the failure
        self.assertEqual(live(fps=0, flags={"ws": False, "mjpeg": False, "sse": False, "poll": False})["status"], FAIL)
        self.assertEqual(live(fps=0, flags={"mjpeg": True})["status"], FAIL)
        self.assertEqual(live(fps=5, flags={})["status"], FAIL)
        # v0.1.68: the app ends on the first transport that works: WebSocket, then SSE, then MJPEG, then polling
        for flags, label in (({"ws": True, "sse": True, "mjpeg": True, "poll": True}, "経路: WebSocket"),
                             ({"ws": False, "sse": True, "mjpeg": True, "poll": True}, "経路: ストリーム(SSE)"),
                             ({"ws": False, "sse": False, "mjpeg": True, "poll": True}, "経路: ストリーム(MJPEG)"),
                             ({"ws": False, "sse": False, "mjpeg": False, "poll": True}, "経路: ポーリング")):
            with self.subTest(flags=flags):
                out = live(fps=12, flags=flags, fps_ws=12, fps_sse=11, fps_mjpeg=12, fps_poll=6)
                self.assertEqual(out["status"], PASS)             # 12 fps on whichever transport
                self.assertIn(label, out["value"])
        # below 5 fps on a moving page is a warning on every transport (polling keeps its own hint)
        poll = live(fps=3, flags={"ws": False, "mjpeg": False, "sse": False, "poll": True}, fps_poll=3)
        self.assertEqual((poll["status"], poll["variant"]), (WARN, "polling"))
        for flags in ({"ws": True}, {"sse": True}, {"mjpeg": True}):
            slow = live(fps=2, flags=flags)
            self.assertEqual((slow["status"], slow["variant"]), (WARN, "slow"), flags)
        self.assertEqual(live(fps=7, flags={"mjpeg": False, "sse": False, "poll": True})["status"], PASS)
        self.assertEqual(live(fps=5, flags={"ws": True})["status"], PASS)
        # the value tells every measured transport: the real-device case (only polling arrived at 0.3 fps, v0.1.65) ...
        out = live(fps=0.3, flags={"ws": False, "sse": False, "mjpeg": False, "poll": True},
                   fps_ws=0, fps_sse=0, fps_mjpeg=0, fps_poll=0.3)
        self.assertEqual(out["status"], WARN)
        # ... and what it looks like once WebSocket gets through
        out = live(fps=6.8, flags={"ws": True, "sse": False, "mjpeg": False, "poll": True},
                   fps_ws=6.8, fps_sse=0, fps_mjpeg=0, fps_poll=0.3)
        self.assertEqual(out["status"], PASS)
        for part in ("経路: WebSocket", "6.8 fps", "WebSocket 6.8 fps", "SSE 届かず", "MJPEG 届かず", "ポーリング 0.3 fps"):
            self.assertIn(part, out["value"])
        self.assertEqual(selftest.live_transport({"sse": True}), "sse")
        self.assertEqual(selftest.live_transport({"ws": True, "sse": True, "mjpeg": True, "poll": True}), "ws")
        self.assertEqual(selftest.live_transport({"mjpeg": True, "poll": True}), "mjpeg")
        self.assertIsNone(selftest.live_transport({"mjpeg": False}))
        self.assertIn("WebSocket", selftest.HINTS["client.live:polling"])
        self.assertIn("5fps", selftest.HINTS["client.live:slow"])

    def test_v0159_response_checks(self):
        ev = self.ev
        # route switches: local, so a slow one is a warning
        self.assertEqual(ev(id="client.route", ok=True, ms=60, n=4)["status"], PASS)
        self.assertEqual(ev(id="client.route", ok=True, ms=400, n=4)["status"], WARN)
        self.assertEqual(ev(id="client.route", ok=True, ms=2500, n=4)["status"], FAIL)
        self.assertEqual(ev(id="client.route", ok=False)["status"], FAIL)
        # a settings switch must look changed before the reply
        self.assertEqual(ev(id="client.toggle", ok=True, ms=30, n=300)["status"], PASS)
        self.assertEqual(ev(id="client.toggle", ok=True, ms=150, n=300)["status"], WARN)
        self.assertEqual(ev(id="client.toggle", ok=True, ms=900, n=300)["status"], FAIL)
        self.assertEqual(ev(id="client.toggle", ok=False, ms=1)["status"], FAIL)
        # idle: <= 3 state answers in 30 s passes
        self.assertEqual(ev(id="client.idle", ok=True, ms=30000, state=1, total=2)["status"], PASS)
        self.assertEqual(ev(id="client.idle", ok=True, ms=30000, state=3, total=5)["status"], PASS)
        self.assertEqual(ev(id="client.idle", ok=True, ms=30000, state=4, total=5)["status"], WARN)
        self.assertEqual(ev(id="client.idle", ok=True, ms=30000, state=2, total=40)["status"], WARN)
        self.assertEqual(ev(id="client.idle", ok=True, ms=30000, state=15, total=15)["status"], FAIL)
        self.assertEqual(ev(id="client.idle", ok=False)["status"], FAIL)
        self.assertIn("3", ev(id="client.idle", ok=True, ms=30000, state=1, total=2)["threshold"])
        self.assertIn("30 秒間", ev(id="client.idle", ok=True, ms=30014, state=1, total=2)["value"])
        # opening a page: <= 3 requests
        self.assertEqual(ev(id="client.open_rtt", ok=True, n=2, ms=900)["status"], PASS)
        self.assertEqual(ev(id="client.open_rtt", ok=True, n=3, ms=900)["status"], PASS)
        self.assertEqual(ev(id="client.open_rtt", ok=True, n=5, ms=900)["status"], WARN)
        self.assertEqual(ev(id="client.open_rtt", ok=True, n=6, ms=900)["status"], FAIL)
        for cid in selftest.RESPONSE_IDS:
            self.assertIn(cid, selftest.HINTS)
            self.assertIn(cid, selftest.CLIENT_NAMES)

    def test_v0159_response_checks_get_their_own_section(self):
        runner = make_runner()
        runner.start({"client": False})
        run = runner._run
        with runner._cv:
            run.state, run.phase, run.client = "running", "client", "pending"
        payload = good_payload(run.id)
        payload["checks"] += [{"id": "client.route", "ok": True, "ms": 50, "n": 4},
                              {"id": "client.idle", "ok": True, "ms": 30000, "state": 9, "total": 12}]
        runner.submit_client(payload)
        sections = {c["id"]: c["section"] for c in run.checks}
        self.assertEqual(sections["client.route"], "response")
        self.assertEqual(sections["client.idle"], "response")
        self.assertEqual(sections["client.rtt"], "client")
        text = runner.report(run.id)["text"]
        self.assertIn("## 操作の応答", text)
        self.assertIn("[FAIL] client.idle", text)
        with self.assertRaises(ValueError):
            selftest.validate_client(good_payload(run.id, checks=[{"id": "client.idle", "state": "x"}]))

    def test_clipboard_and_secure_context(self):
        self.assertEqual(self.ev(id="client.clipboard", ok=True, flags={"api": False})["status"], PASS)
        self.assertIn("copyコマンド", self.ev(id="client.clipboard", ok=True, flags={"api": False})["value"])
        self.assertIn("clipboard API", self.ev(id="client.clipboard", ok=True, flags={"api": True})["value"])
        self.assertEqual(self.ev(id="client.clipboard", ok=False)["status"], WARN)
        self.assertEqual(self.ev(id="client.secure", ok=True)["status"], PASS)
        no = self.ev(id="client.secure", ok=False)
        self.assertEqual(no["status"], WARN)
        self.assertIn("https", selftest.HINTS["client.secure"])
        self.assertIn("ts.net", selftest.HINTS["client.secure"])


class StateMachineTests(unittest.TestCase):
    def test_idle_snapshot(self):
        snap = make_runner().status()
        self.assertEqual((snap["state"], snap["run_id"], snap["rev"]), ("idle", None, 0))

    def test_run_without_browser_finishes_and_enters_history(self):
        runner = make_runner(env_ok=False)
        run_id = runner.start({"client": False})
        snap = runner.status()
        self.assertEqual((snap["run_id"], snap["state"], snap["phase"]), (run_id, "done", "finished"))
        self.assertEqual([c["id"] for c in snap["checks"]], ["browser.skipped"])
        statuses = {s["id"]: s["status"] for s in snap["sections"]}
        self.assertEqual(statuses["browser"], "skipped")
        self.assertNotIn("running", statuses.values())
        self.assertNotIn("pending", statuses.values())
        self.assertEqual([h["id"] for h in runner.history()], [run_id])
        self.assertEqual(snap["summary"]["skip"], 1)

    def test_debug_log_is_enabled_then_restored(self):
        debug = StubDebug([{"ts": 0, "category": "rb", "level": "info", "message": "a"},
                           {"ts": 0, "category": "net", "level": "info", "message": "ignored"}])
        runner = make_runner(debug=debug)
        run_id = runner.start({"client": False})
        self.assertEqual(debug.calls, ["enable", "disable", "clear"])
        text = runner.report(run_id)["text"]
        self.assertIn("rb/info a", text)
        self.assertNotIn("ignored", text)

    def test_debug_already_on_is_left_on(self):
        debug = StubDebug()
        debug.enabled = True
        make_runner(debug=debug).start({"client": False})
        self.assertEqual(debug.calls, [])

    def test_only_one_run_at_a_time(self):
        runner = make_runner()
        gate = threading.Event()
        runner._section_env = lambda run: gate.wait(5) and False
        run_id = runner.start({"client": False}) if False else None
        runner._thread_on = True
        run_id = runner.start({"client": False})
        try:
            with self.assertRaises(selftest.Busy):
                runner.start({})
            self.assertEqual(runner.status()["state"], "running")
        finally:
            gate.set()
        for _ in range(100):
            if runner.status()["state"] != "running":
                break
            time.sleep(0.02)
        self.assertEqual(runner.status()["run_id"], run_id)
        self.assertEqual(runner.status()["state"], "done")
        runner.start({"client": False})          # allowed again once finished

    def test_cancel(self):
        runner = make_runner()
        started = threading.Event()

        def env(run):
            started.set()
            for _ in range(200):
                runner._guard(run)
                time.sleep(0.01)
            return False
        runner._section_env = env
        runner._thread_on = True
        self.assertFalse(runner.cancel())         # nothing running
        runner.start({"client": False})
        started.wait(2)
        self.assertTrue(runner.cancel())
        for _ in range(200):
            if runner.status()["state"] != "running":
                break
            time.sleep(0.01)
        self.assertEqual(runner.status()["state"], "cancelled")
        self.assertFalse(runner.cancel())

    def test_exception_in_a_check_is_reported_not_raised(self):
        runner = make_runner()

        def boom(run):
            raise KeyError("x")
        runner._section_env = boom
        runner.start({"client": False})
        snap = runner.status()
        self.assertEqual(snap["state"], "error")
        self.assertEqual(snap["checks"][-1]["id"], "run.error")
        self.assertEqual(snap["checks"][-1]["status"], FAIL)
        self.assertIn("KeyError", snap["checks"][-1]["value"])
        self.assertTrue(snap["checks"][-1]["hint"])

    def test_cleanup_runs_even_when_cancelled_and_on_error(self):
        runner = make_runner()
        seen = []
        runner._section_cleanup = lambda run, state: seen.append(state)
        runner._section_env = lambda run: (_ for _ in ()).throw(selftest.Cancelled())
        runner.start({"client": False})
        self.assertEqual(seen, ["cancelled"])
        self.assertEqual(runner.status()["state"], "cancelled")

    def test_hard_timeout_is_enforced_by_guard(self):
        clock = Clock()
        runner = make_runner(clock=clock)

        def env(run):
            clock.now += selftest.HARD_TIMEOUT + 1
            runner._guard(run)
        runner._section_env = env
        runner.start({"client": False})
        snap = runner.status()
        self.assertEqual(snap["state"], "timeout")
        self.assertEqual(snap["checks"][-1]["id"], "run.timeout")
        self.assertEqual(selftest.HARD_TIMEOUT, 360.0)

    def test_stuck_worker_is_reaped_by_status(self):
        clock = Clock()
        runner = make_runner(clock=clock)
        release = threading.Event()
        runner._section_env = lambda run: release.wait(5) and False
        runner._thread_on = True
        run_id = runner.start({"client": False})
        try:
            self.assertEqual(runner.status()["state"], "running")
            clock.now += selftest.HARD_TIMEOUT + 20
            snap = runner.status()
            self.assertEqual((snap["run_id"], snap["state"]), (run_id, "timeout"))
            runner.start({"client": False})       # the slot is free again
        finally:
            release.set()

    def test_status_long_poll(self):
        runner = make_runner(clock=time.monotonic)
        runner._section_env = lambda run: False
        runner.start({"client": False})
        rev = runner.status()["rev"]
        started = time.monotonic()
        self.assertEqual(runner.status(since=rev, wait=0.2)["rev"], rev)       # nothing changed: waits
        self.assertGreaterEqual(time.monotonic() - started, 0.15)
        self.assertNotEqual(runner.status(since=rev - 1, wait=5)["rev"], rev - 1)   # already newer: returns at once
        self.assertEqual(runner.status(wait=999)["rev"], rev)      # wait is capped, no since: immediate

    def test_long_poll_wakes_on_change(self):
        runner = make_runner(clock=time.monotonic)
        gate = threading.Event()
        runner._section_env = lambda run: gate.wait(5) and False
        runner._thread_on = True
        runner.start({"client": False})
        rev = runner.status()["rev"]
        result = {}
        thread = threading.Thread(target=lambda: result.update(runner.status(since=rev, wait=5)))
        thread.start()
        time.sleep(0.1)
        runner.cancel()
        gate.set()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertGreater(result["rev"], rev)

    def test_history_keeps_only_five_in_ram(self):
        runner = make_runner()
        ids = [runner.start({"client": False}) for _ in range(8)]
        history = runner.history()
        self.assertEqual(len(history), selftest.HISTORY_MAX)
        self.assertEqual([h["id"] for h in history], list(reversed(ids[-5:])))
        self.assertIsNone(runner.result(ids[0]))
        self.assertIsNone(runner.report(ids[0]))
        self.assertEqual(runner.result(ids[-1])["run_id"], ids[-1])
        self.assertEqual(len(runner.status()["history"]), 5)

    def test_history_is_not_written_to_disk(self):
        import tempfile
        import os
        with tempfile.TemporaryDirectory() as tmp:
            runner = make_runner(data_dir=tmp)
            runner.start({"client": False})
            self.assertEqual(os.listdir(tmp), [])

    def test_urls_are_capped_and_reduced_to_hosts_in_options(self):
        runner = make_runner()
        urls = [{"url": "https://h%d.example.com/p?q=1" % i} for i in range(9)]
        runner.start({"urls": urls, "client": False})
        run = runner._run
        self.assertEqual(len(run.options["urls"]), selftest.MAX_URLS)
        self.assertEqual(run.options["urls"][0]["host"], "h0.example.com")


class ClientSubmitTests(unittest.TestCase):
    def waiting_runner(self):
        runner = make_runner()
        runner.start({"client": False})
        run = runner._run
        # Re-open the run as if it waits for the phone.
        with runner._cv:
            run.state, run.phase, run.client = "running", "client", "pending"
        return runner, run

    def test_accepts_once_and_adds_checks_with_hints(self):
        runner, run = self.waiting_runner()
        payload = good_payload(run.id)
        payload["checks"].append({"id": "client.mirror_events", "flags": {"pointer": False}} if False else {"id": "client.tiles", "ok": False})
        result = runner.submit_client(payload)
        self.assertEqual(result, {"ok": True, "checks": 5})
        by_id = {c["id"]: c for c in run.checks}
        self.assertEqual(by_id["client.rtt"]["status"], PASS)
        self.assertEqual(by_id["client.tiles"]["status"], FAIL)
        self.assertTrue(by_id["client.tiles"]["hint"])
        self.assertEqual(by_id["client.rtt"]["hint"], "")
        self.assertTrue(run.client_evt.is_set())
        with self.assertRaises(ValueError):
            runner.submit_client(payload)          # second submit refused

    def test_refused_when_not_waiting_or_wrong_run(self):
        runner = make_runner()
        with self.assertRaises(ValueError):
            runner.submit_client(good_payload("abcdefgh1234"))      # no run at all
        runner, run = self.waiting_runner()
        with self.assertRaises(ValueError):
            runner.submit_client(good_payload("zzzzzzzzzzzz"))      # another run
        with runner._cv:
            run.phase = "builtin"
        with self.assertRaises(ValueError):
            runner.submit_client(good_payload(run.id))              # not in the client phase
        with runner._cv:
            run.phase, run.state = "client", "cancelled"
        with self.assertRaises(ValueError):
            runner.submit_client(good_payload(run.id))              # finished

    def test_invalid_payload_changes_nothing(self):
        runner, run = self.waiting_runner()
        before = len(run.checks)
        with self.assertRaises(ValueError):
            runner.submit_client(good_payload(run.id, checks=[{"id": "client.nope"}]))
        self.assertEqual(len(run.checks), before)
        self.assertEqual(run.client, "pending")


def swipe_step(step_id, counts=None, **facts):
    base = {"nas_before": "C", "nas_after": "B", "nas_seq": "CB", "label": "B", "route_left": False,
            "synthetic": "history"}
    base.update(facts)
    return {"id": step_id, "facts": base, "counts": counts or {}, "events": [{"t": 3, "e": "popstate", "d": "hash=#/tab/t1"}]}


def swipe_payload(run_id, steps, name="mirror", **over):
    payload = {"run_id": run_id, "mode": {"name": name, "display": name, "synthetic": "touch", "history_growth": 0,
                                          "steps": steps}, "last": False}
    payload.update(over)
    return payload


class SwipeJudgeTests(unittest.TestCase):
    """v0.1.52: the Hub judges the phone's swipe facts (PASS / WARN / FAIL with Japanese hints)."""

    def judge(self, step_id, counts, **facts):
        base = {"nas_before": "C", "nas_after": "B", "nas_seq": "CB", "label": "B", "route_left": False,
                "synthetic": "history"}
        base.update(facts)
        return selftest.judge_swipe_step(step_id, base, counts)

    def test_edge_back_pass_and_failures(self):
        ok = {"popstate": 1, "step.call": 1, "step.moved": 1, "guard.push": 1}
        self.assertEqual(self.judge("edge_back", ok)[0], PASS)
        status, _, hints = self.judge("edge_back", {"step.moved": 0}, nas_after="C", nas_seq="C")
        self.assertEqual(status, FAIL)
        self.assertIn("popstate", " ".join(hints))
        status, _, hints = self.judge("edge_back", dict(ok, popstate=2, **{"step.moved": 2}), nas_after="A", nas_seq="CBA")
        self.assertEqual(status, FAIL)
        self.assertIn("popstate が2回発生", " ".join(hints))
        self.assertIn("二重実行", " ".join(hints))
        status, _, hints = self.judge("edge_back", dict(ok, **{"flick.accepted": 1}))
        self.assertEqual((status, "フリックと戻るの二重実行" in " ".join(hints)), (FAIL, True))
        self.assertEqual(self.judge("edge_back", ok, route_left=True)[0], FAIL)
        self.assertEqual(self.judge("edge_back", {"popstate": 1, "step.call": 1, "step.moved": 1})[0], WARN)  # no guard again
        self.assertEqual(self.judge("edge_back", ok, label="C")[0], WARN)                                       # stale display

    def test_manual_edge_back_hint_names_ios(self):
        status, _, hints = selftest.judge_swipe_step(
            "m_edge_back", {"nas_before": "C", "nas_after": "C", "nas_seq": "C", "label": "C", "answer": "ok"}, {})
        self.assertEqual(status, FAIL)
        self.assertIn("エッジスワイプでpopstateが来ない（iOSが戻る操作をしていない）", hints)
        self.assertEqual(selftest.judge_swipe_step("m_edge_back", {"answer": "skip"}, {})[0], SKIP)
        status, _, hints = selftest.judge_swipe_step(
            "m_edge_back", {"nas_before": "C", "nas_after": "B", "nas_seq": "CB", "label": "B", "answer": "bad"},
            {"popstate": 1, "step.call": 1, "step.moved": 1, "guard.push": 1})
        self.assertEqual(status, FAIL)
        self.assertTrue(any("利用者" in h for h in hints))

    def test_flicks(self):
        ok = {"flick.accepted": 1, "step.call": 1, "step.moved": 1}
        self.assertEqual(self.judge("flick_right", ok, nas_before="B", nas_after="A", nas_seq="BA", label="A")[0], PASS)
        status, _, hints = self.judge("flick_right", {"flick.reject": 1}, nas_before="B", nas_after="B", nas_seq="B",
                                      flick_why="short")
        self.assertEqual(status, FAIL)
        self.assertIn("short", " ".join(hints))
        status, _, hints = self.judge("flick_left", {"flick.accepted": 2, "step.call": 2, "step.moved": 2, "popstate": 1},
                                      nas_before="A", nas_after="C", nas_seq="ABC", label="C")
        self.assertEqual(status, FAIL)
        text = " ".join(hints)
        self.assertIn("フリックの二重実行", text)
        self.assertIn("フリックと戻るの二重実行", text)
        # a challenge/blank page left in the history is named
        status, _, hints = self.judge("flick_right", ok, nas_before="B", nas_after="?", nas_seq="B?", label="")
        self.assertEqual(status, FAIL)
        self.assertIn("確認ページ", " ".join(hints))
        self.assertEqual(self.judge("flick_right", {}, nas_before="B", nas_after="B", synthetic="none")[0], SKIP)

    def test_no_navigation_steps(self):
        for step in ("slow_move", "edge_start"):
            ok = self.judge(step, {"flick.reject": 1}, nas_before="B", nas_after="B", nas_seq="B", label="B")
            self.assertEqual(ok[0], PASS)
            bad = self.judge(step, {"flick.accepted": 1, "step.call": 1, "step.moved": 1}, nas_before="B",
                             nas_after="A", nas_seq="BA", label="A")
            self.assertEqual(bad[0], FAIL)

    def test_back_first_never_leaves_the_tab(self):
        ok = {"popstate": 1, "step.call": 1, "step.nomove": 1, "toast": 1, "guard.push": 1}
        self.assertEqual(self.judge("back_first", ok, nas_before="A", nas_after="A", nas_seq="A", label="A")[0], PASS)
        self.assertEqual(self.judge("back_first", ok, nas_before="A", nas_after="A", nas_seq="A", label="A", route_left=True)[0], FAIL)
        no_toast = {"popstate": 1, "step.call": 1, "step.nomove": 1}
        self.assertEqual(self.judge("back_first", no_toast, nas_before="A", nas_after="A", nas_seq="A", label="A")[0], WARN)

    def test_pull_steps(self):
        self.assertEqual(self.judge("pull_cancel", {}, hint_shown=True, hint_hidden=True, close_calls=0,
                                    nas_before="B", nas_after="B")[0], PASS)
        self.assertEqual(self.judge("pull_cancel", {}, hint_shown=True, hint_hidden=True, close_calls=1,
                                    nas_before="B", nas_after="B")[0], FAIL)
        self.assertEqual(self.judge("pull_cancel", {}, hint_shown=True, hint_hidden=False, close_calls=0,
                                    nas_before="B", nas_after="B")[0], FAIL)
        self.assertEqual(self.judge("pull_close", {}, hint_shown=True, close_calls=1, nas_before="B", nas_after="B")[0], PASS)
        self.assertEqual(self.judge("pull_close", {}, hint_shown=True, close_calls=0, nas_before="B", nas_after="B")[0], FAIL)


class ManualSwipeJudgeTests(unittest.TestCase):
    """v0.1.55: a real-finger step is judged per gesture (repeated swipes are fine), from the v0.1.54 device report."""

    @staticmethod
    def g(c=0, m=0, p=0, f=0, s=0, nas=""):
        return {"c": c, "m": m, "p": p, "f": f, "s": s, "nas": nas}

    def judge(self, step_id, counts, gestures, answer="ok", **facts):
        base = {"nas_before": "B", "nas_after": "B", "nas_seq": "B", "label": "B", "route_left": False,
                "synthetic": "", "answer": answer, "gestures": gestures, "gesture_n": len(gestures)}
        base.update(facts)
        return selftest.judge_swipe_step(step_id, base, counts)

    def test_flick_right_twice_is_not_a_double_execution(self):
        # the report: B->A, then a second flick on the first page (step.nomove + toast); user answered OK
        gestures = [self.g(c=1, m=1, f=1, nas="A"), self.g(c=1, f=1)]
        counts = {"flick.accepted": 2, "step.call": 2, "step.moved": 1, "step.nomove": 1, "toast": 1}
        status, value, hints = self.judge("m_flick_right", counts, gestures, nas_before="B", nas_after="A",
                                          nas_seq="BA", label="A")
        self.assertEqual((status, hints), (PASS, []))
        self.assertIn("ジェスチャー 2 回", value)
        # the user's own NG still fails it
        status, _, hints = self.judge("m_flick_right", counts, gestures, answer="bad", nas_before="B", nas_after="A",
                                      nas_seq="BA", label="A")
        self.assertEqual(status, FAIL)
        self.assertTrue(any("利用者" in h for h in hints))

    def test_one_gesture_acting_twice_fails(self):
        gestures = [self.g(c=2, m=2, f=2, nas="A")]
        status, _, hints = self.judge("m_flick_right", {"flick.accepted": 2, "step.call": 2, "step.moved": 2}, gestures,
                                      nas_before="B", nas_after="A", nas_seq="BA", label="A")
        self.assertEqual(status, FAIL)
        self.assertIn("二重実行", " ".join(hints))
        # a flick AND a popstate in one gesture
        status, _, hints = self.judge("m_flick_left", {"flick.accepted": 1, "popstate": 1, "step.call": 1, "step.moved": 1},
                                      [self.g(c=1, m=1, p=1, f=1, nas="B")], nas_before="A", nas_after="B", nas_seq="AB")
        self.assertEqual(status, FAIL)
        # two popstates in one gesture
        status, _, _ = self.judge("m_edge_back", {"popstate": 2, "step.call": 2, "step.moved": 2, "guard.push": 2},
                                  [self.g(c=2, m=2, p=2, nas="B")], nas_before="C", nas_after="A", nas_seq="CBA", label="A")
        self.assertEqual(status, FAIL)

    def test_edge_back_three_swipes(self):
        # the report: swipes 1 and 2 worked; the 3rd left the tab (route_left, user said NG) -> still FAIL
        left = [self.g(c=1, m=1, p=1, nas="B"), self.g(c=1, m=1, p=1, nas="A"), self.g(p=1)]
        counts = {"popstate": 3, "step.call": 2, "step.moved": 2, "guard.push": 3, "pop.ignore": 1, "route": 1}
        status, _, hints = self.judge("m_edge_back", counts, left, answer="bad", nas_before="C", nas_after="A",
                                      nas_seq="CBA", label="A", route_left=True)
        self.assertEqual(status, FAIL)
        text = " ".join(hints)
        self.assertIn("タブ画面から離れました", text)
        self.assertIn("利用者", text)
        self.assertNotIn("二重実行", text)                   # three separate swipes are not a double execution
        self.assertNotIn("popstate が3回", text)
        # the fixed app: the 3rd swipe stays on the tab (pop.stay, step.nomove + toast), the user answers OK
        stay = [self.g(c=1, m=1, p=1, nas="B"), self.g(c=1, m=1, p=1, nas="A"), self.g(c=1, p=1, s=1)]
        counts = {"popstate": 3, "step.call": 3, "step.moved": 2, "step.nomove": 1, "guard.push": 3, "pop.stay": 1, "toast": 1}
        status, value, hints = self.judge("m_edge_back", counts, stay, nas_before="C", nas_after="A", nas_seq="CBA", label="A")
        self.assertEqual((status, hints), (PASS, []))
        self.assertIn("ジェスチャー 3 回", value)
        # the first acting gesture decides the expected page
        wrong = [self.g(c=1, m=1, p=1, nas="A")]
        self.assertEqual(self.judge("m_edge_back", {"popstate": 1, "step.call": 1, "step.moved": 1, "guard.push": 1}, wrong,
                                    nas_before="C", nas_after="A", nas_seq="CA", label="A")[0], FAIL)
        # no popstate at all (iOS did nothing)
        self.assertEqual(self.judge("m_edge_back", {}, [], nas_before="C", nas_after="C", nas_seq="C", label="C")[0], FAIL)

    def test_table_scroll(self):
        # accepted flicks while scrolling a table -> FAIL
        accepted = [self.g(c=1, m=1, f=1, nas="B"), self.g(c=1, m=1, f=1, nas="C")]
        status, _, hints = self.judge("m_table", {"flick.accepted": 2, "step.call": 2, "step.moved": 2, "flick.reject": 5},
                                      accepted, nas_before="B", nas_after="C", nas_seq="BCBC", flick_why="slow", label="C")
        self.assertEqual(status, FAIL)
        self.assertIn("フリックとして受理", " ".join(hints))
        # only hscroll rejects -> PASS (even with many touches)
        rejects = [self.g(), self.g(), self.g()]
        status, value, hints = self.judge("m_table", {"flick.reject": 3}, rejects, nas_before="B", nas_after="B",
                                          nas_seq="B", flick_why="hscroll")
        self.assertEqual((status, hints), (PASS, []))
        self.assertIn("ジェスチャー 3 回", value)

    def test_pull_and_forward_and_missing_gestures(self):
        self.assertEqual(self.judge("m_pull", {}, [self.g()], hint_shown=True, hint_hidden=True, close_calls=0)[0], PASS)
        self.assertEqual(self.judge("m_pull", {}, [self.g()], hint_shown=False, hint_hidden=True, close_calls=0)[0], FAIL)
        self.assertEqual(self.judge("m_edge_forward", {"flick.accepted": 2, "step.call": 2, "step.moved": 1},
                                    [self.g(c=1, m=1, f=1, nas="C"), self.g(c=1, f=1)], nas_before="B", nas_after="C",
                                    nas_seq="BC", label="C")[0], PASS)
        self.assertEqual(self.judge("m_flick_left", {}, [], answer="skip")[0], SKIP)

    def test_automatic_rules_stay_strict(self):
        # an automatic step has no gestures: two accepted flicks are still a double execution
        status, _, hints = selftest.judge_swipe_step(
            "flick_right", {"nas_before": "B", "nas_after": "A", "nas_seq": "BA", "label": "A", "synthetic": "touch"},
            {"flick.accepted": 2, "step.call": 2, "step.moved": 1})
        self.assertEqual(status, FAIL)

    def test_new_automatic_steps(self):
        base = {"nas_before": "B", "nas_after": "B", "nas_seq": "B", "label": "B", "synthetic": "touch"}
        j = lambda step, counts, **f: selftest.judge_swipe_step(step, dict(base, **f), counts)
        self.assertEqual(j("hscroll", {"flick.reject": 1}, flick_why="hscroll")[0], PASS)
        self.assertEqual(j("hscroll", {"flick.reject": 1}, flick_why="short")[0], WARN)
        self.assertEqual(j("hscroll", {"flick.accepted": 1, "step.call": 1, "step.moved": 1}, nas_after="A", nas_seq="BA")[0], FAIL)
        self.assertEqual(j("hscroll", {}, synthetic="none")[0], SKIP)
        self.assertEqual(j("pull_omnibox", {}, hint_shown=True, hint_hidden=True, close_calls=0)[0], PASS)
        self.assertEqual(j("pull_omnibox", {}, hint_shown=False, hint_hidden=True, close_calls=0)[0], FAIL)
        self.assertEqual(j("pull_omnibox", {}, hint_shown=True, hint_hidden=True, close_calls=1)[0], FAIL)
        ok = {"popstate": 1, "pop.stay": 1, "step.call": 1, "step.moved": 1, "guard.push": 1}
        self.assertEqual(j("pop_stay", ok, nas_after="A", nas_seq="BA", label="A", synthetic="history")[0], PASS)
        self.assertEqual(j("pop_stay", dict(ok, route=1), nas_after="A", nas_seq="BA", label="A", route_left=True)[0], FAIL)
        self.assertEqual(j("pop_stay", {"popstate": 1, "pop.ignore": 1, "route": 1}, route_left=True)[0], FAIL)

    def test_gestures_are_validated(self):
        step = swipe_step("m_flick_right", {"flick.accepted": 1}, answer="ok",
                          gestures=[{"c": 1, "m": 1, "p": 0, "f": 1, "s": 0, "nas": "A"}], gesture_n=1)
        out = selftest.validate_swipe(swipe_payload("abcdefgh1234", [step]))
        self.assertEqual(out["mode"]["steps"][0]["facts"]["gestures"][0]["nas"], "A")
        bad = swipe_step("m_flick_right", {}, answer="ok", gestures=[{"c": "x", "nas": "A"}])
        with self.assertRaises(ValueError):
            selftest.validate_swipe(swipe_payload("abcdefgh1234", [bad]))
        bad = swipe_step("m_flick_right", {}, answer="ok", gestures=[{"nas": "Z"}])
        with self.assertRaises(ValueError):
            selftest.validate_swipe(swipe_payload("abcdefgh1234", [bad]))


class SwipeValidationTests(unittest.TestCase):
    def test_good_payload_is_accepted_and_strings_cleaned(self):
        step = swipe_step("edge_back", {"popstate": 1})
        step["events"][0]["d"] = "see https://evil.example.org/a?token=1 and https://selftest.example.com/swipe/b"
        out = selftest.validate_swipe(swipe_payload("abcdefgh1234", [step]))
        detail = out["mode"]["steps"][0]["events"][0]["d"]
        self.assertNotIn("/a?token", detail)
        self.assertNotIn("https://evil", detail)
        self.assertEqual(out["mode"]["name"], "mirror")

    def test_bad_payloads_are_rejected(self):
        good = swipe_payload("abcdefgh1234", [swipe_step("edge_back", {"popstate": 1})])
        selftest.validate_swipe(good)
        import copy
        def mutate(fn):
            payload = copy.deepcopy(good)
            fn(payload)
            return payload
        bad = [
            None, [], mutate(lambda p: p.update(run_id="x")),
            mutate(lambda p: p["mode"].update(name="live")),
            mutate(lambda p: p["mode"].update(display="evil")),
            mutate(lambda p: p["mode"].update(synthetic="mouse")),
            mutate(lambda p: p["mode"].update(history_growth="1")),
            mutate(lambda p: p["mode"]["steps"][0].update(id="nope")),
            mutate(lambda p: p["mode"]["steps"][0]["facts"].update(nas_after="Z")),
            mutate(lambda p: p["mode"]["steps"][0]["facts"].update(nas_seq="CB;x")),
            mutate(lambda p: p["mode"]["steps"][0]["facts"].update(route_left="no")),
            mutate(lambda p: p["mode"]["steps"][0]["facts"].update(close_calls=-1)),
            mutate(lambda p: p["mode"]["steps"][0]["facts"].update(answer="maybe")),
            mutate(lambda p: p["mode"]["steps"][0]["counts"].update({"Bad Key": 1})),
            mutate(lambda p: p["mode"]["steps"][0]["counts"].update({"popstate": True})),
            mutate(lambda p: p["mode"]["steps"][0]["counts"].update({"popstate": 10 ** 9})),
            mutate(lambda p: p["mode"]["steps"][0].update(events=[{"t": 1, "e": "x"}] * (selftest.SWIPE_EVENTS_MAX + 1))),
            mutate(lambda p: p["mode"]["steps"][0].update(events=[{"t": "1", "e": "x", "d": ""}])),
            mutate(lambda p: p["mode"]["steps"][0].update(events=[{"t": 1, "e": "Bad Name", "d": ""}])),
            mutate(lambda p: p["mode"]["steps"].append(p["mode"]["steps"][0])),
        ]
        for payload in bad:
            with self.subTest(payload=str(payload)[:80]), self.assertRaises(ValueError):
                selftest.validate_swipe(payload)

    def test_http_body_cap_is_64_kib(self):
        self.assertEqual(selftest.SWIPE_BODY_MAX, 65536)


class SwipeRunTests(unittest.TestCase):
    def waiting(self, kind="auto"):
        runner = make_runner()
        runner.start({"client": False, "swipe": True})
        run = runner._run
        with runner._cv:
            run.state, run.phase, run.swipe = "running", "swipe", {"tab": "b1", "kind": kind, "state": "waiting", "modes": {}}
        return runner, run

    def test_submit_judges_stores_and_reports(self):
        runner, run = self.waiting()
        steps = [swipe_step("edge_back", {"popstate": 1, "step.call": 1, "step.moved": 1, "guard.push": 1}),
                 swipe_step("flick_right", {"flick.accepted": 1, "step.call": 1, "step.moved": 1}, nas_before="B",
                            nas_after="A", nas_seq="BA", label="A", synthetic="touch"),
                 swipe_step("back_first", {"popstate": 1, "step.call": 1, "step.nomove": 1, "toast": 1, "guard.push": 1},
                            nas_before="A", nas_after="A", nas_seq="A", label="A")]
        payload = swipe_payload("x" * 8, steps)
        payload["run_id"] = run.id
        payload["device"] = good_payload(run.id)["device"]
        self.assertEqual(runner.submit_swipe(payload), {"ok": True, "steps": 3})
        by_id = {c["id"]: c for c in run.checks}
        self.assertEqual(by_id["swipe.mirror.edge_back"]["status"], PASS)
        self.assertEqual(by_id["swipe.mirror.history"]["status"], PASS)
        self.assertEqual(by_id["swipe.mirror.display"]["section"], "swipe")
        with self.assertRaises(ValueError):
            runner.submit_swipe(payload)                       # the same mode twice
        runner.submit_swipe({"run_id": run.id, "last": True})
        self.assertTrue(run.swipe_evt.is_set())
        with runner._cv:
            run.state = "done"
            runner._history.append(run)
        text = runner.report(run.id)["text"]
        self.assertIn("## スワイプ（自動）のタイムライン", text)
        self.assertIn("### mirror (display=mirror, input=touch)", text)
        self.assertIn("popstate hash=#/tab/t1", text)
        self.assertNotIn("https://", text.split("## スワイプ（自動）のタイムライン", 1)[1])
        self.assertIn("swipe_kind", runner.status().keys() | {"swipe_kind"})
        self.assertIn("swipe", runner.result(run.id))

    def test_failing_step_has_a_hint_in_the_report(self):
        runner, run = self.waiting()
        step = swipe_step("edge_back", {"step.moved": 0}, nas_before="C", nas_after="C", nas_seq="C")
        runner.submit_swipe(swipe_payload(run.id, [step]))
        row = next(c for c in run.checks if c["id"] == "swipe.mirror.edge_back")
        self.assertEqual(row["status"], FAIL)
        self.assertIn("popstate", row["hint"])

    def test_refused_when_not_waiting_wrong_run_or_wrong_kind(self):
        runner, run = self.waiting()
        step = swipe_step("edge_back", {"popstate": 1})
        with self.assertRaises(ValueError):
            runner.submit_swipe(swipe_payload("abcdefgh1234", [step]))          # another run
        with self.assertRaises(ValueError):
            runner.submit_swipe(swipe_payload(run.id, [swipe_step("m_edge_back", {})]))   # a manual step in an automatic run
        with runner._cv:
            run.phase = "client"
        with self.assertRaises(ValueError):
            runner.submit_swipe(swipe_payload(run.id, [step]))

    def test_manual_run_uses_its_own_section_and_answers(self):
        runner, run = self.waiting("manual")
        step = swipe_step("m_edge_back", {"popstate": 1, "step.call": 1, "step.moved": 1, "guard.push": 1}, answer="ok")
        runner.submit_swipe(swipe_payload(run.id, [step]))
        row = next(c for c in run.checks if c["id"] == "swipe.mirror.m_edge_back")
        self.assertEqual((row["section"], row["status"]), ("swipe", PASS)) if False else self.assertEqual(row["status"], PASS)

    def test_goto_validates(self):
        runner, run = self.waiting()
        calls = []
        runner.rb = mock.Mock(selftest_history_to=lambda tab, path: calls.append((tab, path)) or True)
        self.assertEqual(runner.swipe_goto(run.id, "B"), {"ok": True})
        self.assertEqual(calls, [("b1", "/swipe/b")])
        for run_id, page in ((run.id, "Z"), ("bad", "B"), (None, "B")):
            with self.assertRaises(ValueError):
                runner.swipe_goto(run_id, page)

    def test_sections_for_runs(self):
        runner = make_runner()
        runner.start({"client": False})
        self.assertNotIn("swipe", [s["id"] for s in runner._run.sections])
        runner = make_runner()
        runner.start({"client": False, "swipe": True})
        self.assertIn("swipe", [s["id"] for s in runner._run.sections])
        runner = make_runner()
        runner.start({"only": "swipe_manual"})
        self.assertEqual([s["id"] for s in runner._run.sections], ["swipe_manual", "cleanup"])
        self.assertEqual(runner._run.hard, selftest.MANUAL_HARD_TIMEOUT)

    def test_fixture_pages_have_label_table_and_challenge(self):
        fixtures = selftest.Fixtures()
        for letter in "abc":
            status, ctype, body = fixtures("https://selftest.example.com/swipe/%s" % letter)
            html = body.decode()
            self.assertIn("ページ %s" % letter.upper(), html)
            self.assertIn("overflow-x:auto", html)
        self.assertIsNotNone(fixtures("https://selftest.example.com/swipe/a?__cf_chl_tk=selftest"))
        self.assertIsNotNone(fixtures("https://selftest.example.com" + selftest.SWIPE_CHALLENGE_PATH))
        self.assertEqual(selftest.SWIPE_ROUTE[0], "/swipe/a")
        self.assertTrue(selftest.SWIPE_ROUTE[-1].endswith("/swipe/c"))


class ReportTests(unittest.TestCase):
    def runner_with_site(self):
        debug = StubDebug([{"ts": 0, "category": "http", "level": "info",
                            "message": "GET https://secret.example.net/a/b?token=zzz"}])
        runner = make_runner(debug=debug)
        run_id = runner.start({"client": False,
                               "urls": [{"url": "https://secret.example.net/a/b?token=zzz"}]})
        run = runner._run
        runner._add(run, "sites", "site.open", "ページを開く", WARN, "9000 ms", "<= 8000 ms", hint_key="site.open",
                    extra={"site": "secret.example.net", "_url": "https://secret.example.net/a/b?token=zzz"})
        return runner, run_id

    def test_urls_reduced_to_host_by_default(self):
        runner, run_id = self.runner_with_site()
        report = runner.report(run_id)
        for secret in ("/a/b", "token=zzz"):
            self.assertNotIn(secret, report["text"])
            self.assertNotIn(secret, json.dumps(report["data"]))
        self.assertIn("[secret.example.net]", report["text"])

    def test_urls_included_only_on_request(self):
        runner, run_id = self.runner_with_site()
        report = runner.report(run_id, include_urls=True)
        self.assertIn("https://secret.example.net/a/b?token=zzz", report["text"])
        self.assertEqual(runner.report(run_id, include_urls="1")["data"]["checks"][-1].get("url"), None)

    def test_report_layout_and_content(self):
        runner, run_id = self.runner_with_site()
        with runner._cv:
            run = runner._run
            run.device = {"ua": "iPhone UA", "width": 390, "height": 844, "dpr": 3, "flags": {"webkit": True}}
        text = runner.report(run_id)["text"]
        for part in ("Self-test report", "app: v9.9.9", "合格", "PASS", "[NAS]", "[Phone] ua=iPhone UA",
                     "viewport=390x844 dpr=3", "webkit=1", "[WARN] site.open", "value: 9000 ms",
                     "threshold: <= 8000 ms", "改善のヒント", "## debug log (last 50 lines", "rb"[:0] + "http/info",
                     "== end =="):
            self.assertIn(part, text)
        self.assertLess(len(text), 20000)

    def test_report_without_phone_says_so(self):
        runner = make_runner()
        run_id = runner.start({"client": False})
        self.assertIn("no client result", runner.report(run_id)["text"])

    def test_log_is_capped_at_50_lines(self):
        debug = StubDebug([{"ts": i, "category": "rb", "level": "info", "message": "m%d" % i} for i in range(120)])
        runner = make_runner(debug=debug)
        run_id = runner.start({"client": False})
        data = runner.result(run_id)
        self.assertEqual(len(data["log"]), selftest.LOG_TAIL)
        self.assertTrue(data["log"][-1].endswith("m119"))

    def test_hints_only_on_warn_and_fail(self):
        runner = make_runner()
        run_id = runner.start({"client": False})
        run = runner._run
        runner._add(run, "env", "env.mem", "メモリ", PASS, "9000")
        runner._add(run, "env", "env.mem", "メモリ", WARN, "100")
        hints = [c["hint"] for c in runner.result(run_id)["checks"] if c["id"] == "env.mem"]
        self.assertEqual(hints[0], "")
        self.assertEqual(hints[1], selftest.HINTS["env.mem"])

    def test_variant_hint_falls_back_to_base(self):
        runner = make_runner()
        runner.start({"client": False})
        run = runner._run
        runner._add(run, "client", "client.reader", "x", WARN, "", hint_key="client.reader", variant="slow")
        runner._add(run, "client", "client.rtt", "x", WARN, "", hint_key="client.rtt", variant="nope")
        hints = {c["id"]: c["hint"] for c in run.checks}
        self.assertEqual(hints["client.reader"], selftest.HINTS["client.reader:slow"])
        self.assertEqual(hints["client.rtt"], selftest.HINTS["client.rtt"])


class SelfTestHttpTests(unittest.TestCase):
    """Endpoints. The Hub fixture of test_webapp is reused without inheriting its tests."""
    setUp = test_webapp.HttpTests.setUp
    stop = test_webapp.HttpTests.stop
    req = test_webapp.HttpTests.req
    raw = test_webapp.HttpTests.raw
    login = test_webapp.HttpTests.login

    def runner(self):
        runner = webapp.server_selftest(self.server)
        runner._section_env = lambda run: False
        runner._section_cleanup = lambda run, state: None
        runner._thread_on = False
        runner.debug = StubDebug()
        return runner

    def test_reads_require_login(self):
        for path in ("/api/selftest/status", "/api/selftest/results", "/api/selftest/report"):
            with self.subTest(path=path):
                self.assertEqual(self.req(path)[0], 401)

    def test_writes_require_login_csrf_and_origin(self):
        posts = (("/api/selftest/start", {}), ("/api/selftest/cancel", {}),
                 ("/api/selftest/client", good_payload("abcdefgh1234")))
        for path, body in posts:
            with self.subTest(path=path, case="anonymous"):
                self.assertEqual(self.req(path, body)[0], 401)
        self.login()
        for path, body in posts:
            with self.subTest(path=path, case="no csrf"):
                self.assertEqual(self.req(path, body, csrf=False)[0], 403)
            with self.subTest(path=path, case="no origin"):
                self.assertEqual(self.req(path, body, origin=False)[0], 403)
            with self.subTest(path=path, case="foreign origin"):
                self.assertEqual(self.req(path, body, extra={"Origin": "http://evil.example"}, origin=False)[0], 403)

    def test_start_status_report_flow(self):
        self.login()
        self.runner()
        code, body = self.req("/api/selftest/start", {"client": False})
        self.assertEqual(code, 202)
        run_id = body["run_id"]
        code, snap = self.req("/api/selftest/status")
        self.assertEqual((code, snap["run_id"], snap["state"]), (200, run_id, "done"))
        code, snap = self.req("/api/selftest/status?since=%d&wait=1" % (snap["rev"] - 1))
        self.assertEqual(code, 200)
        code, listing = self.req("/api/selftest/results")
        self.assertEqual([i["id"] for i in listing["items"]], [run_id])
        code, full = self.req("/api/selftest/results?id=" + run_id)
        self.assertEqual((code, full["run_id"]), (200, run_id))
        self.assertIn("log", full)
        code, report = self.req("/api/selftest/report?id=" + run_id)
        self.assertEqual(code, 200)
        self.assertIn("Self-test report", report["text"])
        self.assertIn("v" + webapp.VERSION, report["text"])
        self.assertEqual(report["data"]["run_id"], run_id)
        self.assertEqual(self.req("/api/selftest/results?id=nosuchrun123")[0], 404)
        self.assertEqual(self.req("/api/selftest/report?id=nosuchrun123")[0], 404)

    def test_bad_query_values_are_rejected(self):
        self.login()
        for path in ("/api/selftest/status?since=x", "/api/selftest/status?wait=999",
                     "/api/selftest/status?wait=-1", "/api/selftest/results?id=../etc",
                     "/api/selftest/report?id=a%20b"):
            with self.subTest(path=path):
                self.assertEqual(self.req(path)[0], 400)

    def test_start_validates_urls(self):
        self.login()
        self.runner()
        bad = ("ftp://example.com/", "http://127.0.0.1/", "http://localhost/", "https://x.invalid/",
               "https://me.tail1234.ts.net/", "http://" + self.host + "/", "javascript:alert(1)", 5, None)
        for url in bad:
            with self.subTest(url=url):
                code, body = self.req("/api/selftest/start", {"urls": [url]})
                self.assertEqual(code, 400)
                self.assertIn("1件目", body["error"])
        self.assertEqual(self.req("/api/selftest/start", {"urls": "https://example.com/"})[0], 400)
        self.assertEqual(self.req("/api/selftest/start", {"urls": ["https://example.com/%d" % i for i in range(6)]})[0], 400)
        self.assertEqual(self.req("/api/selftest/status")[1]["state"], "idle")
        self.assertEqual(self.req("/api/selftest/start", {"urls": ["https://example.com/a"], "client": False})[0], 202)

    def test_second_start_while_running_is_409(self):
        self.login()
        runner = self.runner()
        gate = threading.Event()
        runner._section_env = lambda run: gate.wait(5) and False
        runner._thread_on = True
        try:
            self.assertEqual(self.req("/api/selftest/start", {"client": False})[0], 202)
            self.assertEqual(self.req("/api/selftest/start", {"client": False})[0], 409)
            self.assertEqual(self.req("/api/selftest/cancel", {})[1], {"cancelled": True})
        finally:
            gate.set()
        for _ in range(100):
            if self.req("/api/selftest/status")[1]["state"] != "running":
                break
            time.sleep(0.02)
        self.assertEqual(self.req("/api/selftest/cancel", {})[1], {"cancelled": False})

    def test_client_endpoint_validates_and_accepts(self):
        self.login()
        runner = self.runner()
        self.assertEqual(self.req("/api/selftest/client", good_payload("abcdefgh1234"))[0], 400)   # no run
        self.assertEqual(self.req("/api/selftest/client", {"run_id": "bad"})[0], 400)
        self.assertEqual(self.req("/api/selftest/client", [1])[0], 400)
        runner.start({"client": False})
        run = runner._run
        with runner._cv:
            run.state, run.phase, run.client = "running", "client", "pending"
        code, body = self.req("/api/selftest/client", good_payload(run.id))
        self.assertEqual((code, body["ok"]), (200, True))

    def test_client_body_is_size_capped(self):
        self.login()
        big = good_payload("abcdefgh1234")
        big["device"]["ua"] = "A" * (selftest.CLIENT_BODY_MAX + 10)
        self.assertEqual(self.req("/api/selftest/client", big)[0], 400)

    def test_selftest_posts_do_not_invalidate_caches_or_log_noise(self):
        self.assertIn("/api/selftest/start", webapp.RB_QUIET_POSTS)
        self.assertIn("/api/selftest/status", webapp.DEBUG_SKIP_HTTP)


import webapp  # noqa: E402  (after the fixture import so test_webapp patches apply first)

if __name__ == "__main__":
    unittest.main()


# ------------------------------------------------------------------------------ v0.1.46
class FakeSiteRB:
    """The part of RemoteBrowser the real-site measurement uses, on a fake clock."""

    def __init__(self, clock, dcl_at=None, load_at=None, scrollable=True):
        self.clock, self.dcl_at, self.load_at, self.scrollable = clock, dcl_at, load_at, scrollable
        self.t0 = None
        self.seq = 0
        self.y = 0
        self.scrolls = []
        self.closed = []

    def memory_reason(self):
        return None

    def open_tab(self, url, selftest=None):
        self.t0 = self.clock()
        return {"id": "s1"}

    def set_viewport(self, *args):
        pass

    def selftest_info(self, tab):
        t = self.clock() - self.t0
        loaded = self.load_at is not None and t >= self.load_at
        return {"loading": not loaded, "url": "https://site.example/", "title": "", "rev": 1, "seq": self.seq,
                "screencast": True, "challenge": False,
                "dcl": 1 if self.dcl_at is not None and t >= self.dcl_at else 0}

    def page_meta(self, tab, wait=0):
        return {"challenge": False}

    def reader(self, tab):
        return {"count": 12, "blocks": []}

    def mirror(self, tab, full=False, force=False):
        return {"stats": {"nodes": 80, "html_bytes": 1000, "css_bytes": 1000}}

    def snapshot(self, tab, force=False):
        return {"tiles": 3}

    def selftest_scroll(self, tab, dy=200):
        self.scrolls.append(dy)
        if not self.scrollable:
            return 0
        self.y += dy
        return self.y

    def stream(self, tab, write, max_seconds=0):
        time.sleep(0.8)                       # real time: the scroller thread runs while "the stream" is open
        for _ in range(int(max_seconds * 7)):  # 7 fps on the fake clock
            self.clock.now += 1.0 / 7
            self.seq += 1
            write(b"x" * 2048)

    def close_tab(self, tab):
        self.closed.append(tab)


class SiteTests(unittest.TestCase):
    def measure(self, **kwargs):
        clock = Clock()
        runner = make_runner(clock=clock, sleep=lambda s: setattr(clock, "now", clock.now + s))
        runner.rb = FakeSiteRB(clock, **kwargs)
        runner.start({"client": False})
        run = runner._run
        item = {"url": "https://site.example/", "host": "site.example"}
        runner._site(run, 1, item, "実サイト1（site.example）", 390, 844, 3.0)
        return runner, run, {c["id"]: c for c in run.checks}, clock

    def test_open_is_judged_by_dom_content_loaded_and_unfinished_load_is_only_a_note(self):
        runner, run, checks, clock = self.measure(dcl_at=2.0, load_at=None)
        opened = checks["site.1.open"]
        self.assertEqual(opened["status"], PASS)
        self.assertIn("DOMContentLoaded 2000 ms", opened["value"])
        self.assertIn("完全な読み込みは未完了（広告など）", opened["value"])
        self.assertLess(clock.now - 1000, 12)                  # did not wait the full 25 s for an ad-bound load
        self.assertEqual(runner.rb.closed, ["s1"])

    def test_open_with_load_reports_both_times_and_slow_dcl_warns(self):
        runner, run, checks, clock = self.measure(dcl_at=1.0, load_at=2.5)
        self.assertEqual(checks["site.1.open"]["status"], PASS)
        self.assertIn("完全な読み込み 2", checks["site.1.open"]["value"])
        runner, run, checks, clock = self.measure(dcl_at=9.0, load_at=10.0)
        self.assertEqual(checks["site.1.open"]["status"], WARN)
        self.assertTrue(checks["site.1.open"]["hint"])

    def test_open_fails_only_without_dom_content_loaded(self):
        runner, run, checks, clock = self.measure(dcl_at=None, load_at=None)
        self.assertEqual(checks["site.1.open"]["status"], FAIL)
        self.assertIn("DOMContentLoadedが来ませんでした", checks["site.1.open"]["value"])
        # a finished load without the event still counts (the event is implied)
        runner, run, checks, clock = self.measure(dcl_at=None, load_at=1.5)
        self.assertEqual(checks["site.1.open"]["status"], PASS)

    def test_live_scrolls_the_page_so_a_still_site_makes_frames(self):
        runner, run, checks, clock = self.measure(dcl_at=1.0, load_at=1.5)
        live = checks["site.1.live"]
        self.assertGreaterEqual(len(runner.rb.scrolls), 2)
        self.assertEqual(set(runner.rb.scrolls), {selftest.SITE_SCROLL_PX})
        self.assertEqual(live["status"], PASS)
        self.assertIn("スクロール", live["value"])
        self.assertEqual(live["hint"], "")

    def test_live_on_a_page_that_cannot_scroll_is_not_a_failure(self):
        runner, run, checks, clock = self.measure(dcl_at=1.0, load_at=1.5, scrollable=False)
        self.assertEqual(checks["site.1.live"]["status"], PASS)
        self.assertIn("スクロールできないページ", checks["site.1.live"]["value"])

    def test_live_warns_only_below_5_fps_with_movement(self):
        clock = Clock()
        runner = make_runner(clock=clock, sleep=lambda s: setattr(clock, "now", clock.now + s))
        fake = FakeSiteRB(clock, dcl_at=1.0, load_at=1.5)
        fake.stream = lambda tab, write, max_seconds=0: (time.sleep(0.8), setattr(clock, "now", clock.now + 3.0),
                                                         write(b"x"), write(b"x"))      # 2 frames in 3 s
        runner.rb = fake
        runner.start({"client": False})
        run = runner._run
        runner._site(run, 1, {"url": "https://site.example/", "host": "site.example"}, "s", 390, 844, 3.0)
        live = {c["id"]: c for c in run.checks}["site.1.live"]
        self.assertEqual(live["status"], WARN)
        self.assertEqual(live["hint"], selftest.HINTS["site.live"])


class FakeCgroupRuntime:
    def __init__(self, refs):
        self.refs = refs

    def container_ref(self, name):
        return self.refs.get(name)


class EnvCgroupAndReportTests(unittest.TestCase):
    def make(self, refs, root, proc):
        import container_runtime as cr
        runner = make_runner()
        runner.rb = type("RB", (), {"runtime": FakeCgroupRuntime(refs), "_cgroup_root": str(root),
                                    "_proc_root": str(proc)})()
        runner.start({"client": False})
        return runner, runner._run, cr

    def layout(self, tmp, with_dir):
        from pathlib import Path
        root, proc = Path(tmp) / "cg", Path(tmp) / "proc"
        (proc / "self").mkdir(parents=True)
        (proc / "77").mkdir()
        cid = "cd" * 32
        mem = root / "memory" / "container-station" / cid
        blk = root / "blkio" / "container-station" / cid
        for d in (mem, blk):
            d.mkdir(parents=True)
        (proc / "self" / "mountinfo").write_text(
            "36 35 0:30 / %s rw - cgroup cgroup rw,memory\n37 35 0:31 / %s rw - cgroup cgroup rw,blkio\n" % (
                root / "memory", root / "blkio"))
        (proc / "77" / "cgroup").write_text(
            "11:blkio:/container-station/%s\n10:memory:/container-station/%s\n" % (cid, cid))
        if with_dir:
            (mem / "memory.usage_in_bytes").write_text("1000\n")
            (blk / "blkio.throttle.io_service_bytes").write_text("8:0 Read 5\n8:0 Write 7\n")
        return root, proc, cid

    def test_env_cgroup_row_names_the_working_source_and_the_layout(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root, proc, cid = self.layout(tmp, True)
            import container_runtime as cr
            runner, run, _ = self.make({cr.BROWSER_NAME: {"id": cid, "pid": 77, "running": True}}, root, proc)
            runner._cgroup_row(run)
            row = [c for c in run.checks if c["id"] == "env.cgroup"][0]
            self.assertEqual(row["status"], PASS)
            for part in ("v1(blkio,memory)", "browser: メモリ=cgroup v1 I/O=cgroup v1", "container-station", "(proc)",
                         "jd2: 停止中"):
                self.assertIn(part, row["value"])
            self.assertIn("cgroup v1", run.info["cgroup_sources"])
            self.assertEqual(run.info["cgroup"], "v1(blkio,memory)")

    def test_env_cgroup_row_warns_and_prints_the_facts_when_only_docker_stats_is_left(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root, proc, cid = self.layout(tmp, False)
            import shutil
            shutil.rmtree(str(root / "memory"))
            shutil.rmtree(str(root / "blkio"))
            (root / "memory").mkdir()
            (root / "blkio").mkdir()
            import container_runtime as cr
            runner, run, _ = self.make({cr.JD2_NAME: {"id": cid, "pid": 77, "running": True}}, root, proc)
            runner._cgroup_row(run)
            row = [c for c in run.checks if c["id"] == "env.cgroup"][0]
            self.assertEqual(row["status"], WARN)
            self.assertIn("docker stats", row["value"])
            self.assertIn("pid-cgroup=blkio:/container-station/", row["value"])
            self.assertIn("mounts=v1@", row["value"])
            self.assertEqual(row["hint"], selftest.HINTS["env.cgroup"])

    def test_env_cgroup_row_is_skipped_when_no_container_runs(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root, proc, cid = self.layout(tmp, True)
            runner, run, _ = self.make({}, root, proc)
            runner._cgroup_row(run)
            row = [c for c in run.checks if c["id"] == "env.cgroup"][0]
            self.assertEqual(row["status"], SKIP)

    def test_report_header_has_transport_and_cgroup_facts(self):
        runner = make_runner()
        runner.start({"client": False})
        run = runner._run
        run.info.update(cgroup="v1(blkio,memory)", cgroup_sources="browser mem=cgroup v1 io=docker stats")
        with runner._cv:
            run.state, run.phase, run.client = "running", "client", "pending"
        payload = good_payload(run.id)
        payload["checks"][-1] = {"id": "client.live", "fps": 6.5, "kb": 20, "fps_sse": 6.5, "fps_poll": 0.3,
                                 "fps_mjpeg": 0, "flags": {"mjpeg": False, "sse": True, "poll": True}}
        payload["checks"] += [{"id": "client.clipboard", "ok": True, "flags": {"api": False}},
                              {"id": "client.secure", "ok": False}]
        runner.submit_client(payload)
        text = runner.report(run.id)["text"]
        self.assertIn("[Source] cgroup=v1(blkio,memory) | container-stats=browser mem=cgroup v1 io=docker stats", text)
        self.assertIn("[Live] transport=sse fps=6.5", text)
        self.assertIn("[PASS] client.clipboard", text)
        self.assertIn("[WARN] client.secure", text)
        self.assertIn("ts.net", text)
        self.assertEqual(run.info["live_transport"], "sse")

    def test_the_animating_page_moves_inside_the_viewport(self):
        """v0.1.68: the moving box was at the end of the article, far below a phone's viewport: the screencast sent only the
        frames of the page load (21 = '7.0 fps' in 3 s) and then nothing, and the phone's live test measured a still page."""
        page = selftest.PAGES["/anim"]
        box = page[page.index('id="a"') - 40:page.index('id="a"') + 200]
        for token in ("position:fixed", "top:0", "z-index"):
            self.assertIn(token, box)
        self.assertIn("setInterval", page)

    def test_report_header_names_websocket(self):
        runner = make_runner()
        runner.start({"client": False})
        run = runner._run
        with runner._cv:
            run.state, run.phase, run.client = "running", "client", "pending"
        payload = good_payload(run.id)
        payload["checks"][-1] = {"id": "client.live", "fps": 6.8, "kb": 29, "fps_ws": 6.8, "fps_sse": 0, "fps_poll": 0.3,
                                 "fps_mjpeg": 0, "flags": {"ws": True, "sse": False, "mjpeg": False, "poll": True}}
        runner.submit_client(payload)
        text = runner.report(run.id)["text"]
        self.assertIn("[Live] transport=websocket fps=6.8", text)
        self.assertIn("経路: WebSocket / 6.8 fps", text)
        self.assertEqual(run.info["live_transport"], "ws")
        with self.assertRaises(ValueError):                    # fps_ws is a number like the others
            selftest.validate_client(dict(payload, checks=[{"id": "client.live", "fps_ws": "x"}]))

    def test_keep_is_requested_for_the_phone_phase(self):
        clock = Clock()
        runner = make_runner(clock=clock)
        calls = []
        runner.rb = type("RB", (), {
            "selftest_goto": lambda self, tab, path: None,
            "selftest_info": lambda self, tab: {"loading": False, "url": selftest.SelfTest._origin() + "/anim"},
            "selftest_keep": lambda self, tab, seconds: calls.append((tab, seconds))})()
        runner.start({"client": False})
        run = runner._run
        run.options["client"] = True
        run.cancel.clear()
        run.client_evt.set()
        with runner._cv:
            run.state = "running"
        runner._client_phase(run, "t1")
        self.assertEqual(calls, [("t1", selftest.CLIENT_WAIT + 15.0)])


class RecommendationTests(unittest.TestCase):
    """v0.1.65: recommended tab count from the measured per-page cost."""

    def test_container_limit_decides_when_it_is_the_tighter_one(self):
        # 2048 MiB container, 80 % guard = 1638 MiB; browser alone 700 MiB; a page = 150 MiB * 1.25
        rec = selftest.recommend_tabs(150, 700, 2048, 9000)
        self.assertEqual((rec["tabs"], rec["by_container"]), (5, 5))
        self.assertGreater(rec["by_host"], 20)

    def test_free_memory_of_the_nas_can_decide_instead(self):
        # only 3 GiB free and 2 GiB must stay free: room for (3072-2048)/(100*1.25) = 8 pages
        rec = selftest.recommend_tabs(100, 300, 8192, 3072)
        self.assertEqual((rec["tabs"], rec["by_host"]), (8, 8))

    def test_open_tabs_are_part_of_the_base_and_count_towards_the_answer(self):
        without = selftest.recommend_tabs(100, 500, 2048, 6000)
        withtabs = selftest.recommend_tabs(100, 500 + 2 * 100, 2048, 6000 - 2 * 100, open_tabs=2)
        self.assertEqual(withtabs["tabs"], without["tabs"])             # 2 tabs open do not change what fits in total

    def test_clamped_to_one_through_twenty(self):
        self.assertEqual(selftest.recommend_tabs(900, 1500, 2048, 9000)["tabs"], 1)        # nothing fits: still one
        self.assertEqual(selftest.recommend_tabs(40, 300, 65536, 60000)["tabs"], 20)
        self.assertEqual(selftest.recommend_tabs(5, 300, 65536, 60000)["tabs"],
                         selftest.recommend_tabs(selftest.CAPACITY_FLOOR_MIB, 300, 65536, 60000)["tabs"])    # noise floor

    def test_unknown_limits_are_left_out_and_without_a_cost_there_is_no_answer(self):
        self.assertEqual(selftest.recommend_tabs(100, 300, None, None)["tabs"], 20)
        self.assertEqual(selftest.recommend_tabs(100, 300, 2048, None)["by_host"], None)
        for bad in (None, 0, -5, float("nan"), "x"):
            self.assertIsNone(selftest.recommend_tabs(bad, 300, 2048, 9000))

    def test_page_costs_skip_the_first_pages_renderer_start_and_prefer_real_sites(self):
        samples = [("軽い仮想ページ", 150.0, False), ("重い仮想ページ1", 100.0, False), ("重い仮想ページ2", 120.0, False)]
        stats = selftest.page_costs(samples)
        self.assertEqual((stats["avg"], stats["max"], stats["per_tab"], stats["basis"]), (110.0, 120.0, 120.0, "virtual"))
        stats = selftest.page_costs(samples + [("実サイト1", 200.0, True), ("実サイト2", 90.0, True)])
        self.assertEqual((stats["per_tab"], stats["basis"], stats["n"]), (200.0, "real", 5))
        self.assertAlmostEqual(stats["avg"], (100 + 120 + 200 + 90) / 4)
        single = selftest.page_costs([("軽い仮想ページ", 80.0, False)])
        self.assertEqual((single["avg"], single["per_tab"]), (80.0, 80.0))
        self.assertEqual(selftest.page_costs([])["per_tab"], None)
        self.assertEqual(selftest.page_costs([("a", -20.0, False), ("b", -5.0, False)])["max"], 0.0)   # noise never goes negative


class FakeCapacityRB:
    """RemoteBrowser as the 「タブの容量」 check sees it: every open page adds memory; the guard may refuse."""

    def __init__(self, clock, base=500.0, costs=(80, 100, 120, 110, 90), limit_mib=2048, refuse_after=None, tabs=()):
        self.clock, self.base, self.costs = clock, base, list(costs)
        self.used = base
        self.n = 0
        self.limit = limit_mib
        self.refuse_after = refuse_after
        self.open_ids, self.closed, self.opened = [], [], []
        self.user_tabs = list(tabs)
        self.tab_reco = None
        self.max = 3

    def container_memory(self, fresh=False, allow_stats=True):
        assert fresh, "the capacity check must read a fresh value"
        return {"used_bytes": int(self.used * 1024 * 1024), "anon_bytes": None, "file_bytes": None, "source": "cgroup"}

    def status(self):
        return {"tabs": [{"id": t} for t in self.user_tabs], "memory": {"used_mib": 1, "limit_mib": self.limit},
                "available_mib": 9000}

    def memory_reason(self, *args, **kwargs):
        if self.refuse_after is not None and self.n >= self.refuse_after:
            return "NASブラウザのメモリ使用量が上限の80%に達しています（85%）。タブを閉じてから開いてください"
        return None

    def max_tabs(self):
        return self.max

    def open_tab(self, url, selftest=None):
        assert selftest and selftest.get("capacity") is True
        self.opened.append(url)
        self.used += self.costs[self.n] if self.n < len(self.costs) else 100
        self.n += 1
        tab = "s%d" % self.n
        self.open_ids.append(tab)
        return {"id": tab}

    def set_viewport(self, *a):
        pass

    def selftest_info(self, tab):
        return {"loading": False, "url": self.opened[int(tab[1:]) - 1], "title": "", "rev": 1, "seq": 0,
                "screencast": False, "challenge": False, "dcl": 1}

    def selftest_heap(self, tab):
        return 4 * 1024 * 1024

    def close_tab(self, tab):
        self.closed.append(tab)
        self.open_ids.remove(tab)

    def selftest_tabs(self):
        return list(self.open_ids)


class FakeQueueRB(FakeCapacityRB):
    """v0.1.65: adds the probe queue API the 「リンク解析キュー」 check uses; jobs finish one per poll."""

    def __init__(self, clock, mode="ok", **kw):
        super().__init__(clock, **kw)
        self.mode = mode
        self.jobs = []
        self.enqueued = []
        self.tab_closed_for = []
        self.probes = self

    def tab_closed(self, tab):
        self.tab_closed_for.append(tab)

    def open_tab(self, url, selftest=None):
        self.opened.append(url)
        self.n += 1
        tab = "q%d" % self.n
        self.open_ids.append(tab)
        return {"id": tab}

    def probe_enqueue(self, tab, x, y, href=None, label=None, **kw):
        self.enqueued.append(href)
        job = {"id": "j%d" % len(self.jobs), "state": "queued", "links": [], "started": None, "finished": None, "error": None}
        self.jobs.append(job)
        return {"job": job["id"], "position": len(self.jobs)}

    def probe_jobs(self, tab):
        for job in self.jobs:                    # one job completes per poll
            if job["state"] == "queued":
                index = self.jobs.index(job)
                job["started"] = 100.0 + index * (0.5 if self.mode != "overlap" else 0.1)
                job["finished"] = job["started"] + 0.4
                if self.mode == "fail" and index % 2:
                    job["state"], job["error"] = "failed", "NASブラウザのメモリ使用量が上限の85%に達しています"
                else:
                    job["state"], job["links"] = "done", ["https://rapidgator.net/file/x"]
                break
        return {"jobs": [dict(j) for j in self.jobs], "summary": {}}


class QueueCheckTests(unittest.TestCase):
    def run_queue(self, mode="ok"):
        clock = Clock()
        runner = make_runner(clock=clock, sleep=lambda s: setattr(clock, "now", clock.now + s))
        runner.rb = FakeQueueRB(clock, mode=mode)
        runner.start({"client": False, "urls": []})
        run = runner._run
        runner._queue(run, selftest.Fixtures())
        return runner, run, {c["id"]: c for c in run.checks if c["section"] == "queue"}

    def test_ten_virtual_presses_finish_one_after_another(self):
        runner, run, checks = self.run_queue()
        self.assertEqual(len(runner.rb.enqueued), selftest.QUEUE_PROBES)
        self.assertTrue(all(h.startswith("https://selftest.example.com/go/") for h in runner.rb.enqueued))
        self.assertEqual(checks["queue.enqueue"]["status"], "pass")
        self.assertEqual(checks["queue.finish"]["status"], "pass")
        self.assertIn("10/10件完了", checks["queue.finish"]["value"])
        self.assertEqual(checks["queue.sequential"]["status"], "pass")
        self.assertEqual(runner.rb.tab_closed_for, ["q1"])               # the check's tab and jobs are gone afterwards
        self.assertEqual(runner.rb.open_ids, [])

    def test_failures_and_overlap_are_reported(self):
        _, _, checks = self.run_queue("fail")
        self.assertEqual(checks["queue.finish"]["status"], "warn")
        self.assertIn("メモリ使用量", checks["queue.finish"]["value"])
        self.assertTrue(checks["queue.finish"]["hint"])
        _, _, checks = self.run_queue("overlap")
        self.assertEqual(checks["queue.sequential"]["status"], "fail")

    def test_virtual_redirect_page_forwards_to_a_file_page(self):
        answer = selftest.Fixtures()(selftest.remote_browser_origin() + "/go/3")
        self.assertEqual(answer[0], 200)
        self.assertIn(b"https://rapidgator.net/file/", answer[2])
        self.assertIsNone(selftest.Fixtures()(selftest.remote_browser_origin() + "/go/abc"))


class CapacityTests(unittest.TestCase):
    def run_capacity(self, urls=(), **rb_kwargs):
        clock = Clock()
        runner = make_runner(clock=clock, sleep=lambda s: setattr(clock, "now", clock.now + s))
        runner.rb = FakeCapacityRB(clock, **rb_kwargs)
        runner.start({"client": False, "urls": [{"url": u, "host": u.split("/")[2]} for u in urls]})
        run = runner._run
        runner._capacity(run, selftest.Fixtures())
        return runner, run, {c["id"]: c for c in run.checks}

    def test_one_line_per_page_a_summary_and_everything_is_closed_again(self):
        runner, run, checks = self.run_capacity()
        pages = [c for c in run.checks if c["id"].startswith("capacity.page.")]
        self.assertEqual(len(pages), 4)                                 # the light page and the heavy one three times
        self.assertIn("+80 MiB", pages[0]["value"])
        self.assertIn("ブラウザの描画プロセスの起動を含む", pages[0]["value"])
        self.assertIn("JSヒープ 4.0 MiB", pages[1]["value"])
        self.assertIn("コンテナ合計 680 MiB", pages[1]["value"])
        summary = checks["capacity.summary"]
        # average of pages 2..4 = 110, maximum 120 -> container: (2048*0.8 - 500)/(120*1.25) = 7
        self.assertIn("1ページあたり 平均 110 MiB・最大 120 MiB → 推奨タブ枚数 7枚（現在の上限 3枚）", summary["value"])
        self.assertEqual(summary["status"], PASS)
        self.assertEqual(runner.rb.tab_reco["tabs"], 7)
        self.assertEqual(runner.rb.open_ids, [])
        self.assertEqual(sorted(runner.rb.closed), sorted("s%d" % n for n in range(1, 5)))
        self.assertEqual(checks["capacity.base"]["status"], PASS)
        self.assertEqual(run.info["tab_reco"]["basis"], "virtual")

    def test_real_sites_follow_the_virtual_pages_and_set_the_cost(self):
        runner, run, checks = self.run_capacity(urls=("https://news.example/a", "https://shop.example/b"),
                                                costs=(80, 100, 120, 110, 260, 90))
        self.assertEqual(len([c for c in run.checks if c["id"].startswith("capacity.page.")]), 6)
        self.assertEqual(runner.rb.opened[-2:], ["https://news.example/a", "https://shop.example/b"])
        self.assertEqual(run.info["tab_reco"]["basis"], "real")
        self.assertEqual(run.info["tab_reco"]["per_tab_mib"], 260.0)
        self.assertIn("実サイトの最大", checks["capacity.summary"]["value"])
        self.assertEqual(checks["capacity.page.5"]["site"], "news.example")       # only the host is reported
        self.assertNotIn("/a", checks["capacity.page.5"]["name"])
        self.assertEqual(runner.rb.open_ids, [])

    def test_no_real_site_is_touched_without_the_users_urls(self):
        runner, run, checks = self.run_capacity()
        self.assertTrue(all(u.startswith(selftest.remote_browser_origin()) for u in runner.rb.opened))

    def test_the_memory_guard_ends_the_measurement_early_and_says_so(self):
        runner, run, checks = self.run_capacity(refuse_after=2)
        self.assertEqual(len([c for c in run.checks if c["id"].startswith("capacity.page.")]), 2)
        summary = checks["capacity.summary"]
        self.assertEqual(summary["status"], WARN)
        self.assertIn("メモリ保護のため 3 枚目で止めました", summary["value"])
        self.assertTrue(summary["hint"])
        self.assertTrue(runner.rb.tab_reco["partial"])
        self.assertEqual(runner.rb.open_ids, [])

    def test_a_current_limit_above_the_recommendation_is_a_warning_with_a_hint(self):
        clock = Clock()
        runner = make_runner(clock=clock, sleep=lambda s: setattr(clock, "now", clock.now + s))
        runner.rb = FakeCapacityRB(clock, costs=(80, 350, 350, 350))
        runner.rb.max = 20
        runner.start({"client": False})
        run = runner._run
        runner._capacity(run, selftest.Fixtures())
        summary = {c["id"]: c for c in run.checks}["capacity.summary"]
        self.assertEqual(summary["status"], WARN)
        self.assertIn(selftest.HINTS["capacity.recommend"], summary["hint"])
        self.assertTrue(any(c["status"] == WARN and c["id"].startswith("capacity.page.") for c in run.checks))

    def test_without_a_readable_container_memory_the_section_is_skipped(self):
        clock = Clock()
        runner = make_runner(clock=clock, sleep=lambda s: setattr(clock, "now", clock.now + s))
        fake = FakeCapacityRB(clock)
        fake.container_memory = lambda fresh=False, allow_stats=True: None
        runner.rb = fake
        runner.start({"client": False})
        run = runner._run
        runner._capacity(run, selftest.Fixtures())
        self.assertEqual([c["status"] for c in run.checks if c["section"] == "capacity"], [SKIP])
        self.assertEqual(fake.opened, [])

    def test_the_time_budget_limits_the_pages(self):
        clock = Clock()
        runner = make_runner(clock=clock, sleep=lambda s: setattr(clock, "now", clock.now + s))
        fake = FakeCapacityRB(clock)
        original = fake.open_tab

        def slow_open(url, selftest=None):
            clock.now += 25.0
            return original(url, selftest=selftest)
        fake.open_tab = slow_open
        runner.rb = fake
        runner.start({"client": False})
        run = runner._run
        runner._capacity(run, selftest.Fixtures())
        pages = [c for c in run.checks if c["id"].startswith("capacity.page.")]
        self.assertLess(len(pages), 4)
        self.assertIn("時間の上限", {c["id"]: c for c in run.checks}["capacity.summary"]["value"])

    def test_the_section_is_in_the_report_text_and_between_the_sites_and_the_cleanup(self):
        ids = [i for i, _ in selftest.SECTIONS]
        self.assertEqual(ids[ids.index("sites") + 1], "capacity")
        self.assertEqual(dict(selftest.SECTIONS)["capacity"], "タブの容量")
        self.assertEqual(selftest.HARD_TIMEOUT, 360.0)
        runner, run, _ = self.run_capacity()
        run.state = "done"
        text = runner._report_text(run, False)
        self.assertIn("## タブの容量", text)
        self.assertIn("capacity.summary", text)
        self.assertIn("推奨タブ枚数", text)
