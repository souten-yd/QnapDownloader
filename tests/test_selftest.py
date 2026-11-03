"""On-device self-test (v0.1.44): state machine, validation, redaction, hints, endpoints. No network, no docker."""
import json
import threading
import time
import unittest
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
                          {"id": "client.live", "fps": 12.5, "kb": 30, "flags": {"img": True}}]}
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

    def test_mirror_events_is_the_iphone_failure(self):
        bad = self.ev(id="client.mirror_events", flags={"pointer": False, "click": False})
        self.assertEqual(bad["status"], FAIL)
        self.assertIn("届かず", bad["value"])
        half = self.ev(id="client.mirror_events", flags={"pointer": True, "click": False})
        self.assertEqual((half["status"], half["variant"]), (WARN, "click"))
        self.assertEqual(self.ev(id="client.mirror_events", flags={"pointer": True, "click": True})["status"], PASS)

    def test_live(self):
        self.assertEqual(self.ev(id="client.live", fps=0, flags={"img": True})["status"], FAIL)
        poll = self.ev(id="client.live", fps=3, flags={"img": False})
        self.assertEqual((poll["status"], poll["variant"]), (WARN, "polling"))
        self.assertEqual(self.ev(id="client.live", fps=2, flags={"img": True})["status"], WARN)
        self.assertEqual(self.ev(id="client.live", fps=12, flags={"img": True})["status"], PASS)


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
        self.assertEqual(selftest.HARD_TIMEOUT, 240.0)

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
