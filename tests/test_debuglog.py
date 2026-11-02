"""v0.1.35 debug log: ring caps, redaction, level/category filter, auto-off; HTTP API auth, CSRF, limits."""
import json
import unittest

import debuglog
import diskio
import test_webapp
import webapp


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class RingTests(unittest.TestCase):
    def make(self):
        self.clock = Clock()
        return debuglog.DebugLog(clock=self.clock, wall=lambda: 1700000000.0)

    def test_off_by_default_and_near_free(self):
        log = self.make()
        self.assertIsNone(log.log("http", "error", "x"))
        self.assertEqual(log.entries()["entries"], [])
        self.assertFalse(log.status()["enabled"])

    def test_entry_count_cap(self):
        log = self.make()
        log.enable()
        for i in range(debuglog.MAX_ENTRIES + 50):
            log.log("app", "info", "m%d" % i)
        entries = log.entries(limit=1000)["entries"]
        self.assertLessEqual(log.status()["count"], debuglog.MAX_ENTRIES)
        self.assertEqual(log.status()["count"], debuglog.MAX_ENTRIES)
        self.assertEqual(log.entries(1, 1000)["oldest"], 51)
        self.assertEqual(entries[-1]["message"], "m%d" % (debuglog.MAX_ENTRIES + 49))

    def test_byte_cap(self):
        log = self.make()
        log.enable()
        big = {"k": "x" * 150}
        for i in range(4000):
            log.log("app", "info", "y" * 490, big)
        status = log.status()
        self.assertLessEqual(status["bytes"], debuglog.MAX_BYTES)
        self.assertLess(status["count"], 4000)

    def test_message_and_data_limits(self):
        log = self.make()
        log.enable()
        log.log("app", "info", "z" * 2000, {"a": "b" * 5000, "c": ["d" * 1000] * 50})
        entry = log.entries()["entries"][0]
        self.assertLessEqual(len(entry["message"]), debuglog.MAX_MESSAGE)
        self.assertLessEqual(len(json.dumps(entry["data"])), debuglog.MAX_DATA_BYTES)
        log.log("app", "info", "big", {str(i): "v" * 100 for i in range(20)})
        self.assertTrue(log.entries()["entries"][-1]["data"].get("_truncated"))

    def test_redaction_of_keys_and_urls(self):
        log = self.make()
        log.enable()
        log.log("net", "info", "GET https://user:pw@host.example/path/x?token=abc&u=me failed password=hunter2 Bearer abcdef123456",
                {"password": "p", "Authorization": "a", "api_key": "k", "SessionId": "s", "my_cookie": "c",
                 "ok": 5, "nested": {"secret": "x", "url": "https://acct.example/u/1?sid=9", "list": ["https://h.example/a?b=c"]}})
        text = json.dumps(log.entries()["entries"][0])
        for leaked in ("hunter2", "abcdef123456", "token=abc", "user:pw", "sid=9", "/u/1", "p\"", "/a?b=c", "\"s\"", "\"k\""):
            self.assertNotIn(leaked, text)
        entry = log.entries()["entries"][0]
        self.assertIn("https://host.example", entry["message"])
        self.assertEqual(entry["data"]["password"], "[redacted]")
        self.assertEqual(entry["data"]["nested"]["secret"], "[redacted]")
        self.assertEqual(entry["data"]["nested"]["url"], "https://acct.example")
        self.assertEqual(entry["data"]["ok"], 5)

    def test_level_and_category_filter(self):
        log = self.make()
        log.enable("warn", ["http", "jd2"])
        self.assertIsNone(log.log("http", "info", "no"))
        self.assertIsNone(log.log("ui", "error", "no"))
        self.assertIsNotNone(log.log("jd2", "warn", "yes"))
        self.assertIsNotNone(log.log("http", "error", "yes"))
        self.assertEqual(log.status()["categories"], ["jd2", "http"])
        for bad in (("verbose", None), ("debug", ["nope"]), ("debug", "ui")):
            with self.assertRaises(ValueError):
                log.enable(*bad)

    def test_auto_off_after_60_minutes_and_remaining_time(self):
        log = self.make()
        log.enable()
        self.assertEqual(log.status()["remaining"], 3600)
        self.assertEqual(log.status()["until"], 1700000000 + 3600)
        self.clock.t += 3599
        self.assertTrue(log.is_enabled("app"))
        self.assertEqual(log.status()["remaining"], 1)
        self.clock.t += 2
        self.assertIsNone(log.log("app", "error", "late"))
        status = log.status()
        self.assertFalse(status["enabled"])
        self.assertIsNone(status["until"])
        self.assertEqual(log.entries()["entries"], [])
        log.enable(duration=999999)                      # the maximum is one hour
        self.assertEqual(log.status()["remaining"], 3600)

    def test_incremental_reads_and_clear(self):
        log = self.make()
        log.enable()
        for i in range(10):
            log.log("app", "info", "m%d" % i)
        self.assertEqual([e["seq"] for e in log.entries(0, 3)["entries"]], [8, 9, 10])
        self.assertEqual([e["seq"] for e in log.entries(7, 2)["entries"]], [8, 9])
        self.assertEqual(log.entries(10)["entries"], [])
        log.clear()
        self.assertEqual(log.entries()["entries"], [])
        self.assertEqual(log.status()["count"], 0)

    def test_client_batch_validation(self):
        log = self.make()
        self.assertEqual(log.log_client([{"category": "ui", "level": "info", "message": "x"}]), 0)   # off
        log.enable()
        batch = [{"category": "ui", "level": "info", "message": "ok", "data": {"a": 1}},
                 {"category": "nope", "level": "info", "message": "bad category"},
                 {"category": "ui", "level": "loud", "message": "bad level"},
                 {"category": "ui", "level": "info", "message": 5},
                 "text", None,
                 {"category": "ui", "level": "info", "message": "m" * 900, "data": "notadict"}]
        self.assertEqual(log.log_client(batch), 2)
        self.assertEqual(log.log_client([{"category": "ui", "level": "info", "message": "x"}] * 250), debuglog.MAX_BATCH)
        entry = log.entries(0, 1000)["entries"][1]
        self.assertEqual(entry["src"], "client")
        self.assertEqual(len(entry["message"]), debuglog.MAX_MESSAGE)
        self.assertNotIn("data", entry)


class DebugApiTests(test_webapp.HttpTests):
    def setUp(self):
        super().setUp()
        debuglog.LOG.disable()
        debuglog.LOG.clear()
        self.addCleanup(debuglog.LOG.disable)
        self.addCleanup(debuglog.LOG.clear)
        self.meter = diskio.DiskIO(None, (), proc_io=lambda: (0, 4096), disks=lambda: [], start_thread=False)
        self.server.diskio = self.meter

    def test_endpoints_require_login_csrf_and_origin(self):
        gets = ("/api/debug/log", "/api/diag/diskio")
        posts = (("/api/debug/log", {"entries": []}), ("/api/debug/config", {"enabled": False}),
                 ("/api/debug/clear", {}), ("/api/diag/diskio/start", {}), ("/api/diag/diskio/stop", {}))
        for path in gets:
            self.assertEqual(self.req(path)[0], 401, path)
        for path, body in posts:
            self.assertEqual(self.req(path, body)[0], 401, path)
        self.login()
        for path, body in posts:
            with self.subTest(path=path):
                self.assertEqual(self.req(path, body, csrf=False)[0], 403)
                self.assertEqual(self.req(path, body, origin=False)[0], 403)
                self.assertEqual(self.req(path, body)[0], 200)
        for path in gets:
            self.assertEqual(self.req(path)[0], 200)

    def test_state_reports_debug_and_auto_off_status(self):
        self.login()
        state = self.req("/api/state")[1]
        self.assertFalse(state["debug"]["enabled"])
        self.assertIsNone(state["debug"]["until"])
        self.assertNotIn("remaining", state["debug"])
        code, status = self.req("/api/debug/config", {"enabled": True, "level": "info", "categories": ["ui", "http"]})
        self.assertEqual(code, 200)
        self.assertTrue(status["enabled"])
        debug = self.req("/api/state")[1]["debug"]
        self.assertTrue(debug["enabled"])
        self.assertEqual((debug["level"], debug["categories"]), ("info", ["ui", "http"]))
        self.assertGreater(debug["remaining"], 3500)
        self.assertEqual(self.req("/api/debug/config", {"enabled": False})[1]["enabled"], False)

    def test_config_validation(self):
        self.login()
        for body in ({}, {"enabled": "yes"}, {"enabled": True, "level": "loud"},
                     {"enabled": True, "categories": ["nope"]}, {"enabled": True, "categories": "ui"}):
            with self.subTest(body=body):
                self.assertEqual(self.req("/api/debug/config", body)[0], 400)

    def test_client_entries_are_stored_only_while_on_and_read_incrementally(self):
        self.login()
        entry = {"category": "ui", "level": "info", "message": "route", "data": {"token": "t", "view": "home"}}
        self.assertEqual(self.req("/api/debug/log", {"entries": [entry]})[1]["accepted"], 0)
        self.req("/api/debug/config", {"enabled": True, "level": "debug", "categories": ["ui"]})
        self.assertEqual(self.req("/api/debug/log", {"entries": [entry, dict(entry, category="http")]})[1]["accepted"], 1)
        first = self.req("/api/debug/log?since=0&limit=50")[1]
        self.assertEqual([e["message"] for e in first["entries"]], ["route"])
        self.assertEqual(first["entries"][0]["data"]["token"], "[redacted]")
        self.assertEqual(first["entries"][0]["src"], "client")
        self.assertTrue(first["status"]["enabled"])
        self.req("/api/debug/log", {"entries": [dict(entry, message="second")]})
        newer = self.req("/api/debug/log?since=%d" % first["seq"])[1]
        self.assertEqual([e["message"] for e in newer["entries"]], ["second"])
        self.assertEqual(self.req("/api/debug/clear", {})[0], 200)
        self.assertEqual(self.req("/api/debug/log")[1]["entries"], [])

    def test_limits(self):
        self.login()
        self.req("/api/debug/config", {"enabled": True})
        many = [{"category": "app", "level": "info", "message": "m"}] * 130
        self.assertEqual(self.req("/api/debug/log", {"entries": many})[1]["accepted"], debuglog.MAX_BATCH)
        for query in ("since=x", "limit=0", "limit=1001", "limit=abc", "since=" + "9" * 13):
            with self.subTest(query=query):
                self.assertEqual(self.req("/api/debug/log?" + query)[0], 400)
        huge = [{"category": "app", "level": "info", "message": "x" * 600, "data": {"k": "v" * 400}}] * 100
        self.assertEqual(self.req("/api/debug/log", {"entries": huge})[0], 200)       # ~100 KB is within the batch limit
        self.assertEqual(self.req("/api/debug/log", {"entries": [], "pad": "x" * 400000})[0], 400)
        self.assertEqual(self.req("/api/debug/log", {"entries": "text"})[1]["accepted"], 0)

    def test_http_requests_are_logged_only_when_on_without_query_and_rate_limited(self):
        self.login()
        self.req("/api/settings?secret=1")
        self.assertEqual(self.req("/api/debug/log")[1]["entries"], [])
        self.req("/api/debug/config", {"enabled": True, "level": "debug", "categories": ["http"]})
        for _ in range(3):
            self.req("/api/settings?secret=1")
        entries = self.req("/api/debug/log")[1]["entries"]
        settings = [e for e in entries if e["data"]["path"] == "/api/settings"]
        self.assertEqual(len(settings), 1)               # the same path is logged once per second
        data = settings[0]["data"]
        self.assertEqual((data["method"], data["status"]), ("GET", 200))
        self.assertIsInstance(data["ms"], int)
        self.assertNotIn("secret", json.dumps(entries))
        self.assertFalse([e for e in entries if e["data"]["path"].startswith("/api/debug/")])

    def test_diskio_api_start_stop_and_unavailable_sources(self):
        self.login()
        code, data = self.req("/api/diag/diskio")
        self.assertEqual(code, 200)
        self.assertFalse(data["measuring"])
        self.meter.sample()
        started = self.req("/api/diag/diskio/start", {})[1]
        self.assertTrue(started["measuring"])
        hub = started["sources"][0]
        self.assertEqual((hub["id"], hub["available"], hub["write"]["total"]), ("hub", True, 0))
        stopped = self.req("/api/diag/diskio/stop", {})[1]
        self.assertFalse(stopped["measuring"])
        self.meter._proc_io = lambda: (_ for _ in ()).throw(OSError("x"))
        self.meter.sample.__self__._cache.invalidate()
        self.meter.sample()
        item = self.req("/api/diag/diskio")[1]["sources"][0]
        self.assertFalse(item["available"])
        self.assertTrue(item["reason"])

    def test_diagnostics_ui_is_wired_into_settings_system(self):
        page = self.client.open(self.url + "/", timeout=4).read().decode()
        for ident in ("diskToggle", "diskRows", "debugEnabled", "debugLevel", "debugCats", "debugView",
                      "debugSheet", "debugList", "debugCopy", "debugDownload", "debugClear", "debugFollow",
                      "debugSearch", "debugFilterLevel", "debugFilterCat"):
            self.assertIn('id="%s"' % ident, page)
        self.assertIn("ディスク書き込み", page)
        self.assertIn("ログを見る", page)


for _name in [n for n in dir(DebugApiTests) if n.startswith("test_") and n not in (
        "test_endpoints_require_login_csrf_and_origin", "test_state_reports_debug_and_auto_off_status",
        "test_config_validation", "test_client_entries_are_stored_only_while_on_and_read_incrementally",
        "test_limits", "test_http_requests_are_logged_only_when_on_without_query_and_rate_limited",
        "test_diskio_api_start_stop_and_unavailable_sources", "test_diagnostics_ui_is_wired_into_settings_system")]:
    setattr(DebugApiTests, _name, None)

if __name__ == "__main__":
    unittest.main()
