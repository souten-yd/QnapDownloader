"""v0.1.74: passthrough sites through the NAS browser (storage option, pt_core helpers, rb_ptfetch, relay routing)."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import passthrough
import pt_core
import pt_proxy
import rb_ptfetch
import remote_browser
import storage
from test_passthrough import ListenerCase


class SettingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = storage.Store(Path(self.tmp.name))
        self.addCleanup(self.store.close)

    def test_default_is_empty(self):
        self.assertEqual(self.store.settings()["pt_nas_sites"], [])

    def test_list_is_normalised(self):
        self.store.set_option("pt_nas_sites", ["Example.COM", " b.example.net. ", "example.com", "日本.jp"])
        self.assertEqual(self.store.settings()["pt_nas_sites"], ["example.com", "b.example.net", "xn--wgv71a.jp"])

    def test_bad_values_are_refused(self):
        for bad in ("example.com", [1], ["a b"], ["http://x.com/"], ["x" * 300], ["a%d.example.com" % i for i in range(60)], None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                self.store.set_option("pt_nas_sites", bad)
        self.assertEqual(self.store.settings()["pt_nas_sites"], [])


class CoreHelperTests(unittest.TestCase):
    def test_site_match_covers_subdomains_only_of_listed_sites(self):
        sites = ["example.com"]
        self.assertTrue(pt_core.nas_site_match("example.com", sites))
        self.assertTrue(pt_core.nas_site_match("img.example.com", sites))
        self.assertFalse(pt_core.nas_site_match("example.org", sites))
        self.assertFalse(pt_core.nas_site_match("notexample.com", sites))
        self.assertFalse(pt_core.nas_site_match("example.com", []))

    def test_body_headers_are_dropped(self):
        items = [("Content-Encoding", "gzip"), ("Content-Length", "10"), ("Transfer-Encoding", "chunked"), ("Content-Type", "text/html")]
        self.assertEqual(pt_core.nas_response_headers(items), [("Content-Type", "text/html")])
        self.assertIn(("Content-Length", "10"), pt_core.nas_response_headers(items, keep_length=True))


class FetchHelperTests(unittest.TestCase):
    def test_url_key(self):
        self.assertEqual(rb_ptfetch.url_key("https://A.example.com/a%20b?x=%41#frag"), ("https", "a.example.com", 443, "/a b", "x=A"))
        self.assertEqual(rb_ptfetch.url_key("http://a.example.com"), ("http", "a.example.com", 80, "/", ""))
        self.assertIsNone(rb_ptfetch.url_key("file:///etc/passwd"))
        self.assertIsNone(rb_ptfetch.url_key("https://[bad"))

    def test_request_headers_allow_list(self):
        items = [("Cookie", "a=1"), ("Host", "x"), ("Origin", "https://x"), ("Referer", "https://x"), ("User-Agent", "u"),
                 ("Sec-Fetch-Mode", "cors"), ("Accept", "*/*"), ("Accept-Language", "ja"), ("X-Requested-With", "XMLHttpRequest"),
                 ("X-QD-PT-Top", "a"), ("Content-Type", "application/x-www-form-urlencoded"), ("Bad Name", "v"), ("X-A", "a\nb")]
        plain = rb_ptfetch.request_headers(items, cors=False)
        self.assertEqual(plain, {"accept": "*/*", "accept-language": "ja"})
        cors = rb_ptfetch.request_headers(items, cors=True)
        for forbidden in ("cookie", "host", "origin", "referer", "user-agent", "sec-fetch-mode", "x-qd-pt-top", "bad name", "x-a"):
            self.assertNotIn(forbidden, cors)
        self.assertEqual(cors["x-requested-with"], "XMLHttpRequest")
        self.assertEqual(cors["content-type"], "application/x-www-form-urlencoded")

    def test_request_mode(self):
        self.assertEqual(rb_ptfetch.request_mode("image", False, False), "no-cors")
        self.assertEqual(rb_ptfetch.request_mode("empty", False, False), "cors")
        self.assertEqual(rb_ptfetch.request_mode("image", True, False), "cors")
        self.assertEqual(rb_ptfetch.request_mode("script", False, True), "cors")

    def test_response_headers_split_repeated_values(self):
        self.assertEqual(rb_ptfetch.response_headers({"Set-Cookie": "a=1\nb=2", "X": "y", 3: "z"}),
                         [("Set-Cookie", "a=1"), ("Set-Cookie", "b=2"), ("X", "y")])
        self.assertEqual(rb_ptfetch.response_headers(None), [])

    def test_header_helpers(self):
        items = [("Content-Disposition", " Attachment; filename=a"), ("Content-Length", "123")]
        self.assertTrue(rb_ptfetch.is_attachment(items))
        self.assertEqual(rb_ptfetch.content_length(items), 123)
        self.assertEqual(rb_ptfetch.content_length([("Content-Length", "x")]), -1)

    def test_script_carries_the_job_and_no_cookie(self):
        script = rb_ptfetch.fetch_script(7, "https://a.example.com/x", "POST", {"accept": "*/*"}, "YQ==", "cors", "")
        self.assertIn("https://a.example.com/x", script)
        self.assertIn('"credentials": "include"', script)
        self.assertNotIn("cookie", script.lower())
        self.assertIn("__qdPt", rb_ptfetch.abort_script(7))

    def test_keep_store_expires_and_is_bounded(self):
        now = [100.0]
        keep = rb_ptfetch.KeepStore(lambda: now[0])
        keep.put("a", 1)
        self.assertEqual(keep.take("a"), 1)
        self.assertIsNone(keep.take("a"))                      # taken once
        keep.put("b", 2)
        now[0] += rb_ptfetch.KEEP_TTL + 1
        self.assertIsNone(keep.take("b"))
        for index in range(rb_ptfetch.KEEP_MAX + 5):
            keep.put(index, index)
        self.assertLessEqual(len(keep), rb_ptfetch.KEEP_MAX)
        keep.put(None, 1)
        keep.clear()
        self.assertEqual(len(keep), 0)

    def test_limits_match_the_spec(self):
        self.assertEqual(rb_ptfetch.FETCHER_MAX, 2)
        self.assertEqual(rb_ptfetch.FETCHER_IDLE, 300)
        self.assertEqual(rb_ptfetch.PER_SITE_MAX, 6)


class BrowserGuardTests(unittest.TestCase):
    def test_not_running_is_a_clear_503(self):
        browser = remote_browser.RemoteBrowser.__new__(remote_browser.RemoteBrowser)
        browser._lock = __import__("threading").RLock()
        browser._state = "stopped"
        browser._conn = None
        target = pt_core.parse_target("https://a.example.com/")
        with self.assertRaises(pt_proxy.UpstreamError) as ctx:
            browser.pt_fetch("GET", target, [], None, True)
        self.assertEqual(ctx.exception.status, 503)
        self.assertIn("NASブラウザ", str(ctx.exception))


class RelayTests(ListenerCase):
    """The Hub's routing: listed sites go to the NAS hook (after the SSRF guard), others stay on the Hub's own client."""

    def setUp(self):
        super().setUp()
        self.calls = []
        self.mode = "ok"

        def fake(method, target, headers, body, nav, dest=""):
            self.calls.append((method, target.host, dict((k.lower(), v) for k, v in headers), body, nav, dest))
            if self.mode == "challenge":
                raise pt_proxy.NasChallenge("https://%s/" % target.host)
            return pt_proxy.Response(200, "OK", [("Content-Type", "text/plain"), ("Content-Encoding", "gzip")], body=b"via-nas")

        self.mgr.hooks = dict(self.mgr.hooks, nas_fetch=fake)
        self.store.set_option("pt_nas_sites", ["a.example.com"])
        self.mgr.forget_sites()
        self.enter()

    def test_listed_host_goes_through_the_hook_without_hub_cookies(self):
        reply = self.proxy("/p/h/a.example.com/echo", extra={"Cookie": "qd_pt=%s" % self.cookie})
        self.assertEqual(reply.status, 200)
        self.assertEqual(reply.body, b"via-nas")
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn("cookie", self.calls[0][2])
        self.assertEqual(self.up.seen, [])                      # the Hub's own client never connected

    def test_unlisted_host_stays_direct(self):
        reply = self.proxy("/p/h/b.example.net/echo", top="b.example.net")
        self.assertEqual(reply.status, 200)
        self.assertEqual(self.calls, [])
        self.assertTrue(self.up.seen)

    def test_challenge_shows_the_apps_own_page(self):
        self.mode = "challenge"
        reply = self.proxy("/p/h/a.example.com/", dest="document", extra={"Sec-Fetch-Mode": "navigate"})
        self.assertEqual(reply.status, 200)
        self.assertIn("NASブラウザで確認する".encode(), reply.body)
        self.assertIn("確認が終わったので再読み込み".encode(), reply.body)
        self.assertIn("script-src 'self'", reply.h["content-security-policy"])
        self.assertIn("/pt/challenge.js", reply.body.decode())

    def test_hook_missing_is_a_503_page(self):
        self.mgr.hooks = {k: v for k, v in self.mgr.hooks.items() if k != "nas_fetch"}
        reply = self.proxy("/p/h/a.example.com/echo")
        self.assertEqual(reply.status, 503)
        self.assertIn("NASブラウザが起動していません".encode(), reply.body)

    def test_the_guard_runs_before_the_hook(self):
        self.store.set_option("pt_nas_sites", ["a.example.com"])
        self.mgr.forget_sites()
        with mock.patch.object(self.mgr.client, "addresses", side_effect=pt_core.Blocked("local")):
            reply = self.proxy("/p/h/a.example.com/echo")
        self.assertEqual(reply.status, 403)
        self.assertEqual(self.calls, [])

    def test_the_challenge_script_is_served(self):
        reply = self.get("/pt/challenge.js")
        self.assertEqual(reply.status, 200)
        self.assertIn(b"nas-check", reply.body)


if __name__ == "__main__":
    unittest.main()
