"""HTTP integration tests: zero network requests beyond local test server."""
import base64
import http.cookiejar
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import time
import threading
import unittest
import urllib.error
import urllib.parse
import unittest.mock
import urllib.request
from unittest.mock import patch

import self_update
import storage
import engine_supervisor
import jd2_control
import jd2_local
import container_runtime
import remote_browser
import jd2_accounts
import state_hub
import webapp
from fake_docker import FakeDocker
from test_remote_browser import FakeWS
from test_jd2_accounts import FakeAdapter


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.allowed = self.root / "allowed"
        self.allowed.mkdir()
        self.env = patch.dict(os.environ, {"DOWNLOADER_ALLOWED_ROOTS": str(self.allowed)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.store = storage.Store(self.root)
        self.addCleanup(self.store.close)
        self.store.set_option("rb_spare_tab", False)     # v0.1.54: a background spare tab would take the fake target ids
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), webapp.Handler)
        self.server.store = self.store
        self.server.engine = engine_supervisor.EngineManager(self.root, self.store)
        self.server.jd2 = jd2_control.JD2Controller(self.server.engine.adapter)
        self.server.secure_cookies = False
        ctx = self.root / "browser-ctx"
        ctx.mkdir()
        (ctx / "Dockerfile").write_text("FROM x\n")
        (ctx / "entrypoint.sh").write_text("#!/bin/sh\n")
        meminfo = self.root / "meminfo"
        meminfo.write_text("MemTotal: 16000000 kB\nMemAvailable: 9000000 kB\n")
        self.rb_fake = FakeDocker()
        self.rb_ws = FakeWS()
        runtime = container_runtime.Runtime(self.rb_fake, isfile=self.rb_fake.isfile,
                                            access=lambda p, m: True, meminfo_path=str(meminfo))
        self.rb_fake.images.add(runtime.browser_tag(str(ctx)))
        self.server.rb = remote_browser.RemoteBrowser(
            runtime, ctx, meminfo_path=str(meminfo), connect=lambda ip: self.rb_ws,
            monitor=False, frame_interval=0.0, start_timeout=2.0)
        self.addCleanup(self.server.rb.shutdown)
        self.server.updates = self_update.UpdateManager(self.root, webapp.ROOT,
                                                       webapp.VERSION, "x86_64", self.server.server_port)
        self.port = self.server.server_port
        self.host = "127.0.0.1:" + str(self.port)
        self.url = "http://" + self.host
        self.cookie = http.cookiejar.CookieJar()
        self.client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookie),
                                                  urllib.request.ProxyHandler({}))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)
        self.csrf = ""
        for table in (webapp.SHARE_FAILED, webapp.SHARE_RATE, webapp.FAILED):
            table.clear()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def req(self, path, body=None, origin=True, csrf=True, extra=None):
        headers = {"Host": self.host}
        headers.update(extra or {})
        if body is not None:
            headers["Content-Type"] = "application/json"
            if origin:
                headers["Origin"] = self.url
            if csrf:
                headers["X-CSRF-Token"] = self.csrf
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.url+path, data=data, headers=headers)
        try:
            with self.client.open(request, timeout=4) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)

    def raw(self, path, body=None, headers=None, host=None, method=None):
        """Request without the cookie jar; returns (status, headers, json)."""
        sent = {"Host": host or self.host}
        sent.update(headers or {})
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            sent.setdefault("Content-Type", "application/json")
        request = urllib.request.Request(self.url + path, data=data, headers=sent, method=method)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=4) as response:
                return response.status, response.headers, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, error.headers, json.load(error)

    def login(self):
        self.assertEqual(self.req("/api/setup", {
            "password": "correct horse staple 123"})[0], 201)
        code, body = self.req("/api/login", {"password": "correct horse staple 123"})
        self.assertEqual(code, 200)
        self.csrf = body["csrf"]

    RB_GET = ("/api/rb/status", "/api/rb/log", "/api/rb/links?tab=b1",
              "/api/rb/frame.jpg?tab=b1&since=0&wait=0", "/api/rb/intercepts?tab=b1&since=0",
              "/api/rb/stream.mjpg?tab=b1", "/api/rb/stream.sse?tab=b1")
    RB_POST = (("/api/rb/setup", {"confirmed": True}), ("/api/rb/stop", {"confirmed": True}),
               ("/api/rb/tabs", {"url": "https://example.com/"}), ("/api/rb/tabs/close", {"id": "b1"}),
               ("/api/rb/navigate", {"tab": "b1", "url": "https://example.com/"}),
               ("/api/rb/input", {"tab": "b1", "type": "reload"}),
               ("/api/rb/viewport", {"tab": "b1", "width": 390, "height": 700, "dpr": 3}),
               ("/api/rb/intercepts/open", {"tab": "b1", "id": 1}),
               ("/api/rb/intercepts/add", {"tab": "b1", "id": 1}),
               ("/api/rb/intercepts/send", {"tab": "b1", "id": 1}),
               ("/api/engine/native/remove", {"confirmed": True}))

    def test_rb_endpoints_require_login_csrf_and_origin(self):
        for path in self.RB_GET:
            with self.subTest(path=path):
                self.assertEqual(self.req(path)[0], 401)
        for path, body in self.RB_POST:
            with self.subTest(path=path):
                self.assertIn(self.req(path, body)[0], (401, 403))
        self.login()
        for path, body in self.RB_POST:
            with self.subTest(path=path, case="no csrf"):
                self.assertEqual(self.req(path, body, csrf=False)[0], 403)
            with self.subTest(path=path, case="no origin"):
                self.assertEqual(self.req(path, body, origin=False)[0], 403)
        self.assertEqual([c for c in self.rb_fake.docker_calls() if c[0] in ("run", "build")], [])

    def test_rb_status_and_confirmations(self):
        self.login()
        code, body = self.req("/api/rb/status")
        self.assertEqual(code, 200)
        self.assertEqual(body["phase"], "stopped")
        self.assertTrue(body["image_ready"])
        self.assertEqual(body["max_tabs"], 3)
        self.assertEqual(self.req("/api/rb/setup", {})[0], 400)
        self.assertEqual(self.req("/api/rb/stop", {"confirmed": "yes"})[0], 400)
        self.assertEqual(self.req("/api/engine/native/remove", {})[0], 400)

    def test_rb_open_validates_urls_and_hub_origin(self):
        self.login()
        for url in ("http://example.com/", "https://127.0.0.1/", "file:///etc/passwd", "", None, 5,
                    "https://nas.tail1234.ts.net/"):
            with self.subTest(url=url):
                self.assertEqual(self.req("/api/rb/tabs", {"url": url})[0], 400)
        self.assertEqual([c for c in self.rb_fake.docker_calls() if c[0] == "run"], [])

    def test_v0159_tabs_validates_in_the_same_request_and_returns_the_first_copy(self):
        self.login()
        # validation is folded into the tabs request: the same 400 the validate endpoint gives
        _, expected = self.req("/api/browser/validate?url=" + urllib.parse.quote("http://example.com/"))
        code, body = self.req("/api/rb/tabs", {"url": "http://example.com/", "first": "mirror"})
        self.assertEqual(code, 400)
        self.assertEqual(body["error"], expected["error"])
        self.assertEqual(self.req("/api/rb/tabs", {"url": "https://example.com/", "first": "tiles"})[0], 400)
        self.assertEqual(self.req("/api/rb/tabs", {"url": "https://example.com/", "view": {"width": 5}})[0], 400)
        self.assertEqual(self.server.rb._tabs, {})                     # nothing was created by a refused request
        copy = {"rev": 4, "doc": "/api/rb/mirror/doc?tab=b1&rev=4", "stats": {"nodes": 9}}

        def page_loads():
            for _ in range(200):
                if self.server.rb._tabs:
                    break
                time.sleep(0.01)
            time.sleep(0.1)
            self.rb_ws.emit("Page.frameStartedLoading", {"frameId": "F"}, "SESSION1")
            self.rb_ws.emit("Page.frameNavigated", {"frame": {"url": "https://example.com/a"}}, "SESSION1")
            self.rb_ws.emit("Page.domContentEventFired", {}, "SESSION1")

        loader = threading.Thread(target=page_loads)
        loader.start()
        with patch.object(self.server.rb, "mirror", return_value=copy) as mirror:
            code, tab = self.req("/api/rb/tabs", {"url": "https://example.com/a", "first": "mirror",
                                                  "view": {"width": 390, "height": 700, "dpr": 3}})
        loader.join()
        self.assertEqual(code, 201)
        self.assertEqual((tab["id"], tab["url"]), ("b1", "https://example.com/a"))
        self.assertEqual(tab["first"]["mirror"], copy)
        self.assertTrue(tab["first"]["meta"]["dcl"])
        mirror.assert_called_once_with("b1", full=False, partial=True)
        self.assertEqual(self.rb_ws.calls("Emulation.setDeviceMetricsOverride")[0]["params"]["width"], 390)
        # without `first` the answer is the old one
        code, plain = self.req("/api/rb/tabs", {"url": "https://example.com/b"})
        self.assertEqual((code, sorted(plain)), (201, ["id", "url"]))
        # navigate takes the same two options
        self.assertEqual(self.req("/api/rb/navigate", {"tab": "b1", "url": "https://example.org/", "first": "x"})[0], 400)
        code, moved = self.req("/api/rb/navigate", {"tab": "b2", "url": "https://example.org/",
                                                    "view": {"width": 390, "height": 700, "dpr": 3}})
        self.assertEqual((code, sorted(moved)), (200, ["id", "url"]))

    def test_rb_tab_frame_input_links_close_flow(self):
        self.login()
        code, tab = self.req("/api/rb/tabs", {"url": "https://example.com/a"})
        self.assertEqual((code, tab["id"]), (201, "b1"))
        code, headers, data = self.get_raw("/api/rb/frame.jpg?tab=b1&since=0&wait=0")
        self.assertEqual(code, 204)                         # nothing newer yet: metadata headers only
        for name in ("X-Frame-Seq", "X-Frame-Width", "X-Frame-Height", "X-Frame-Url", "X-Frame-Title",
                     "X-Frame-Loading", "X-Frame-Intercepts", "X-Frame-Rev", "X-Frame-Blocked",
                     "X-Frame-Popups", "X-Frame-Editable", "X-Frame-Challenge", "X-Frame-Links-Rev"):
            self.assertIsNotNone(headers.get(name), name)
        self.rb_ws.emit("Page.screencastFrame", {"data": "QUJD", "sessionId": 1, "metadata": {
            "deviceWidth": 1280, "deviceHeight": 2000}}, "SESSION1")
        for _ in range(100):
            code, headers, data = self.get_raw("/api/rb/frame.jpg?tab=b1&since=0&wait=0")
            if code == 200:
                break
        self.assertEqual((code, data), (200, b"ABC"))
        # The web UI omits `wait`; the server default must be accepted (v0.1.20 NAS bug).
        code, headers, data = self.get_raw("/api/rb/frame.jpg?tab=b1&since=0")
        self.assertEqual((code, data), (200, b"ABC"))
        self.assertEqual(self.get_raw("/api/rb/frame.jpg?tab=b1&since=0&wait=11")[0], 400)
        # the JSON frame endpoint was removed in v0.1.33
        self.assertEqual(self.req("/api/rb/frame?tab=b1&since=0&wait=0")[0], 404)
        self.assertEqual(self.req("/api/rb/input", {"tab": "b1", "type": "tap", "x": 5, "y": 6})[0], 200)
        for bad in ({"tab": "b1", "type": "eval"}, {"tab": "b1", "type": "tap", "x": -5, "y": 1},
                    {"tab": "b1", "type": "key", "key": "F5"}, {"tab": "../x", "type": "reload"}):
            with self.subTest(bad=bad):
                self.assertEqual(self.req("/api/rb/input", bad)[0], 400)
        self.rb_ws.eval_value = ["https://rapidgator.net/file/12345"]
        self.assertEqual(self.req("/api/rb/links?tab=b1")[1]["items"], ["https://rapidgator.net/file/12345"])
        self.assertEqual(self.req("/api/rb/navigate", {"tab": "b1", "url": "http://x.example/"})[0], 400)
        self.assertEqual(self.req("/api/rb/navigate", {"tab": "b1", "url": "https://example.org/"})[0], 200)
        self.assertEqual(self.req("/api/rb/tabs/close", {"id": "b1"})[1], {"closed": True})
        self.assertEqual(self.get_raw("/api/rb/frame.jpg?tab=b1&since=0&wait=0")[0], 404)
        self.assertEqual(self.req("/api/rb/tabs/close", {"id": "b1"})[0], 404)
        self.assertEqual(self.get_raw("/api/rb/frame.jpg?tab=b1&since=abc")[0], 400)
        code, body = self.req("/api/rb/stop", {"confirmed": True})
        self.assertEqual(code, 200)
        self.assertNotIn("qnapdl-browser", self.rb_fake.containers)

    def get_raw(self, path):
        request = urllib.request.Request(self.url + path, headers={"Host": self.host})
        try:
            with self.client.open(request, timeout=6) as response:
                return response.status, response.headers, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.headers, error.read()

    def feed_rb(self, data="QUJD", ack=1, width=390, height=700):
        self.rb_ws.emit("Page.screencastFrame", {"data": data, "sessionId": ack, "metadata": {
            "deviceWidth": width, "deviceHeight": height}}, "SESSION1")
        for _ in range(200):
            if self.server.rb._tabs["b1"].frame == data:
                return
            time.sleep(0.01)

    V28_GET = ("/api/rb/tile.jpg?tab=b1&snap=s1&i=0", "/api/rb/meta?tab=b1&wait=0")
    V28_POST = (("/api/rb/snapshot", {"tab": "b1"}),
                ("/api/rb/snapshot/release", {"tab": "b1"}),
                ("/api/rb/tap-page", {"tab": "b1", "snap": "s1", "x": 1, "y": 1}),
                ("/api/rb/probe", {"tab": "b1", "href": "https://example.org/x"}))

    def test_v0128_snapshot_endpoints_require_login_csrf_and_origin(self):
        for path in self.V28_GET:
            with self.subTest(path=path):
                self.assertEqual(self.req(path)[0], 401)
        for path, body in self.V28_POST:
            with self.subTest(path=path):
                self.assertIn(self.req(path, body)[0], (401, 403))
        self.login()
        for path, body in self.V28_POST:
            with self.subTest(path=path, case="no csrf"):
                self.assertEqual(self.req(path, body, csrf=False)[0], 403)
            with self.subTest(path=path, case="no origin"):
                self.assertEqual(self.req(path, body, origin=False)[0], 403)
        self.assertEqual(self.rb_ws.calls("Page.captureScreenshot"), [])

    def test_v0128_snapshot_tile_tap_and_meta_flow(self):
        self.login()
        self.assertEqual(self.req("/api/rb/tabs", {"url": "https://example.com/a"})[0], 201)
        self.assertEqual(self.req("/api/rb/viewport", {"tab": "b1", "width": 390, "height": 700, "dpr": 2})[0], 200)
        self.rb_ws.eval_map = [("__qdlMut", {"links": [
            {"x": 5, "y": 6, "w": 50, "h": 20, "href": "https://example.org/a"},
            {"x": 5, "y": 6, "w": 50, "h": 20, "href": "javascript:1"}], "inputs": []}),
            ("scrollTo", [0, 0, 390, 700])]
        code, meta = self.req("/api/rb/snapshot", {"tab": "b1"})
        self.assertEqual(code, 200)
        self.assertEqual((meta["width"], meta["height"], meta["tiles"], meta["tile_height"]),
                         (390, 4001, 3, 1600))
        self.assertEqual([l["href"] for l in meta["links"]], ["https://example.org/a"])
        self.assertEqual(self.rb_ws.calls("Page.captureScreenshot"), [])
        code, headers, data = self.get_raw("/api/rb/tile.jpg?tab=b1&snap=%s&i=2" % meta["snap_id"])
        self.assertEqual((code, headers["Content-Type"]), (200, "image/jpeg"))
        self.assertEqual(len(data), 100)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(len(self.rb_ws.calls("Page.captureScreenshot")), 1)
        for bad in ("i=x", "i=-1", "i=123456", "i="):
            with self.subTest(bad=bad):
                self.assertEqual(self.get_raw("/api/rb/tile.jpg?tab=b1&snap=%s&%s" % (meta["snap_id"], bad))[0], 400)
        self.assertEqual(self.get_raw("/api/rb/tile.jpg?tab=b1&snap=%s&i=3" % meta["snap_id"])[0], 400)
        self.assertEqual(self.get_raw("/api/rb/tile.jpg?tab=b1&snap=s99&i=0")[0], 404)      # re-snapshot
        self.assertEqual(self.get_raw("/api/rb/tile.jpg?tab=b1&snap=../x&i=0")[0], 400)
        self.assertEqual(self.get_raw("/api/rb/tile.jpg?tab=b9&snap=s1&i=0")[0], 404)
        code, tapped = self.req("/api/rb/tap-page", {"tab": "b1", "snap": meta["snap_id"], "x": 10, "y": 20})
        self.assertEqual((code, tapped["snap_needed"], tapped["navigated"]), (200, True, False))
        for body in ({"tab": "b1", "snap": meta["snap_id"], "x": -1, "y": 1},
                     {"tab": "b1", "snap": meta["snap_id"], "x": 1},
                     {"tab": "b1", "snap": meta["snap_id"], "x": "1", "y": 1},
                     {"tab": "b1", "snap": "zz", "x": 1, "y": 1},
                     {"tab": "b1", "snap": meta["snap_id"], "x": 1e6, "y": 1}):
            with self.subTest(body=body):
                self.assertEqual(self.req("/api/rb/tap-page", body)[0], 400)
        self.assertEqual(self.req("/api/rb/tap-page", {"tab": "b1", "snap": "s99", "x": 1, "y": 1})[0], 404)
        code, state = self.req("/api/rb/meta?tab=b1&wait=0")
        self.assertEqual((code, state["snap"], state["challenge"]), (200, meta["snap_id"], False))
        for bad in ("wait=11", "wait=x", "rev=x"):
            self.assertEqual(self.req("/api/rb/meta?tab=b1&" + bad)[0], 400)
        self.assertEqual(self.req("/api/rb/meta?tab=b9&wait=0")[0], 404)
        self.assertEqual(self.req("/api/rb/snapshot/release", {"tab": "b1"}), (200, {"ok": True}))
        self.assertEqual(self.get_raw("/api/rb/tile.jpg?tab=b1&snap=%s&i=0" % meta["snap_id"])[0], 404)
        self.assertEqual(self.req("/api/rb/snapshot", {"tab": "b9"})[0], 404)
        self.assertEqual(self.req("/api/rb/snapshot", {})[0], 400)

    def test_v0128_frame_header_reports_the_challenge(self):
        self.login()
        self.req("/api/rb/tabs", {"url": "https://example.com/a"})
        self.feed_rb()
        code, headers, _ = self.get_raw("/api/rb/frame.jpg?tab=b1&since=0&wait=0")
        self.assertEqual(headers["X-Frame-Challenge"], "0")

    def test_v0128_probe_href_is_validated_by_the_server(self):
        self.login()
        self.req("/api/rb/tabs", {"url": "https://example.com/a"})
        for href in ("http://example.org/x", "https://127.0.0.1/x", "https://localhost/", "javascript:1",
                     "https://" + self.host.split(":")[0] + "/x", "https://nas.tail1234.ts.net/x", 5):
            with self.subTest(href=href):
                self.assertEqual(self.req("/api/rb/probe", {"tab": "b1", "href": href})[0], 400)
        hoster = "https://rapidgator.net/file/0123456789abcdef0123456789abcdef"
        code, body = self.req("/api/rb/probe", {"tab": "b1", "href": hoster})
        self.assertEqual((code, body["links"]), (200, [hoster]))
        self.assertTrue(body["probe_id"])

    def test_rb_viewport_validates_and_applies(self):
        self.login()
        self.assertEqual(self.req("/api/rb/tabs", {"url": "https://example.com/a"})[0], 201)
        code, body = self.req("/api/rb/viewport", {"tab": "b1", "width": 390, "height": 700, "dpr": 3})
        self.assertEqual((code, body), (200, {"ok": True, "changed": True}))
        sent = [m for m in self.rb_ws.sent if m["method"] == "Emulation.setDeviceMetricsOverride"]
        self.assertEqual(sent[0]["params"], {"width": 390, "height": 700, "deviceScaleFactor": 1.0, "mobile": True})
        before = len(self.rb_ws.sent)
        for bad in ({"width": 100, "height": 700, "dpr": 2}, {"width": 390, "height": 5000, "dpr": 2},
                    {"width": 390, "height": 700, "dpr": 4}, {"width": "390", "height": 700, "dpr": 2},
                    {"width": 390, "height": 700}, {"tab": "b9", "width": 390, "height": 700, "dpr": 2},
                    {"tab": "../x", "width": 390, "height": 700, "dpr": 2}):
            with self.subTest(bad=bad):
                body = dict({"tab": "b1"}, **bad) if "tab" not in bad else bad
                self.assertIn(self.req("/api/rb/viewport", body)[0], (400, 404))
        self.assertEqual(len(self.rb_ws.sent), before)

    def test_rb_frame_jpg_binary_headers_204_and_json_compat(self):
        self.login()
        self.req("/api/rb/tabs", {"url": "https://example.com/a"})
        code, headers, data = self.get_raw("/api/rb/frame.jpg?tab=b1&since=0&wait=0")
        self.assertEqual((code, data), (204, b""))
        self.assertEqual(headers["X-Frame-Seq"], "0")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIsNone(headers["Content-Length"])
        self.rb_ws.emit("Target.targetInfoChanged", {"targetInfo": {
            "targetId": "TARGET00001", "type": "page", "title": "日本語 タイトル " + "x" * 300,
            "url": "https://example.com/a b?q=日本&x=1"}})
        jpeg = b"\xff\xd8\xff\xe0JFIF-fake"
        self.feed_rb(base64.b64encode(jpeg).decode())
        code, headers, data = self.get_raw("/api/rb/frame.jpg?tab=b1&since=0&wait=0")
        self.assertEqual((code, data), (200, jpeg))
        self.assertEqual(headers["Content-Type"], "image/jpeg")
        self.assertEqual(headers["Content-Length"], str(len(jpeg)))
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual((headers["X-Frame-Seq"], headers["X-Frame-Width"], headers["X-Frame-Height"],
                          headers["X-Frame-Loading"], headers["X-Frame-Intercepts"]), ("1", "390", "700", "0", "0"))
        self.assertTrue(headers["X-Frame-Url"].isascii())
        self.assertEqual(urllib.parse.unquote(headers["X-Frame-Url"]), "https://example.com/a b?q=日本&x=1")
        title = urllib.parse.unquote(headers["X-Frame-Title"])
        self.assertTrue(title.startswith("日本語 タイトル ") and len(title) == 200)
        self.assertEqual(self.get_raw("/api/rb/frame.jpg?tab=b1&since=1&wait=0")[0], 204)
        self.assertEqual(self.get_raw("/api/rb/frame.jpg?tab=b1&since=x")[0], 400)
        self.assertEqual(self.get_raw("/api/rb/frame.jpg?tab=b1&since=0&wait=11")[0], 400)
        self.assertEqual(self.get_raw("/api/rb/frame.jpg?tab=b1&since=0&wait=0&rev=x")[0], 400)
        self.assertEqual(self.get_raw("/api/rb/frame.jpg?tab=b9&since=0&wait=0")[0], 404)
        self.assertEqual(self.get_raw("/api/rb/frame.jpg?tab=b1&since=0&wait=0")[2], jpeg)

    def test_rb_intercepts_list_open_add_flow(self):
        self.login()
        self.req("/api/rb/tabs", {"url": "https://example.com/a"})
        hoster = "https://rapidgator.net/file/0123456789abcdef0123456789abcdef"
        self.rb_ws.emit("Fetch.requestPaused", {"requestId": "r1", "resourceType": "Document",
                                                "request": {"url": hoster + "/x.rar"}}, "SESSION1")
        for _ in range(200):
            if self.req("/api/rb/intercepts?tab=b1&since=0")[1]["items"]:
                break
            time.sleep(0.01)
        self.assertEqual(self.req("/api/rb/intercepts?tab=b1&since=0")[1],
                         {"items": [{"id": 1, "kind": "hoster", "url": hoster}]})
        self.assertEqual(self.req("/api/rb/intercepts?tab=b1&since=1")[1], {"items": []})
        self.assertEqual(self.req("/api/rb/intercepts?tab=b1&since=z")[0], 400)
        code, headers, _ = self.get_raw("/api/rb/frame.jpg?tab=b1&since=0&wait=0")
        self.assertEqual(headers["X-Frame-Intercepts"], "1")
        # validation
        for bad in ({"tab": "b1", "id": 0}, {"tab": "b1", "id": "1"}, {"tab": "b1", "id": 99},
                    {"tab": "b1"}, {"tab": "../x", "id": 1}):
            for action in ("open", "add", "send"):
                with self.subTest(bad=bad, action=action):
                    self.assertEqual(self.req("/api/rb/intercepts/" + action, bad)[0], 400)
        self.assertEqual(self.req("/api/rb/intercepts/add", {"tab": "b9", "id": 1})[0], 404)
        # popup events can be opened too (plain navigation, opener was not moved before)
        self.rb_ws.emit("Target.targetCreated", {"targetInfo": {
            "targetId": "POPUP000001", "type": "page", "openerId": "TARGET00001",
            "url": "https://shop.example/p"}})
        for _ in range(200):
            if len(self.req("/api/rb/intercepts?tab=b1&since=1")[1]["items"]) == 1:
                break
            time.sleep(0.01)
        popup = self.req("/api/rb/intercepts?tab=b1&since=1")[1]["items"]
        self.assertEqual(popup, [{"id": 2, "kind": "popup", "url": "https://shop.example/p"}])
        navs = len([m for m in self.rb_ws.sent if m["method"] == "Page.navigate"])
        self.assertEqual(self.req("/api/rb/intercepts/open", {"tab": "b1", "id": 2})[0], 200)
        self.assertEqual([m for m in self.rb_ws.sent if m["method"] == "Page.navigate"][navs:][0]["params"],
                         {"url": "https://shop.example/p"})
        # add stages only (never sends)
        code, staged = self.req("/api/rb/intercepts/add", {"tab": "b1", "id": 1})
        self.assertEqual((code, staged["state"]), (201, "staged"))
        self.assertEqual([l["url"] for l in self.req("/api/links")[1]["items"]], [hoster])
        # open navigates the tab to exactly that URL and lets it through once
        self.assertEqual(self.req("/api/rb/intercepts/open", {"tab": "b1", "id": 1})[0], 200)
        self.assertEqual([m for m in self.rb_ws.sent if m["method"] == "Page.navigate"][-1]["params"],
                         {"url": hoster})
        self.rb_ws.emit("Fetch.requestPaused", {"requestId": "r2", "resourceType": "Document",
                                                "request": {"url": hoster}}, "SESSION1")
        for _ in range(200):
            if any(m["method"] == "Fetch.continueRequest" for m in self.rb_ws.sent):
                break
            time.sleep(0.01)
        self.assertTrue(any(m["method"] == "Fetch.continueRequest" for m in self.rb_ws.sent))

    def test_rb_intercept_send_uses_the_existing_jd2_path(self):
        self.login()
        self.assertEqual(self.req("/api/settings/folder", {"path": str(self.allowed)})[0], 200)
        self.req("/api/rb/tabs", {"url": "https://example.com/a"})
        self.rb_ws.emit("Browser.downloadWillBegin", {"frameId": "TARGET00001",
                                                      "url": "https://files.example/a.zip"})
        for _ in range(200):
            if self.req("/api/rb/intercepts?tab=b1&since=0")[1]["items"]:
                break
            time.sleep(0.01)
        # engine not running: refused, nothing staged
        self.assertEqual(self.req("/api/rb/intercepts/send", {"tab": "b1", "id": 1})[0], 400)
        self.assertEqual(self.req("/api/links")[1]["items"], [])
        adapter = unittest.mock.MagicMock()
        adapter.can_send = False
        with patch.object(self.server.jd2, "_require", return_value=adapter), \
                patch("jd2_transfer.prepare_transfer") as prepare, \
                patch("jd2_transfer.transfer", return_value={"state": "sent"}) as transfer:
            code, body = self.req("/api/rb/intercepts/send", {"tab": "b1", "id": 1})
            self.assertEqual(code, 400)
            self.assertIn("JD2", body["error"])
            transfer.assert_not_called()
            self.assertEqual(self.req("/api/links")[1]["items"], [])
            adapter.can_send = True
            code, body = self.req("/api/rb/intercepts/send", {"tab": "b1", "id": 1})
            self.assertEqual((code, body), (200, {"state": "sent"}))
            transfer.assert_called_once_with(adapter, "https://files.example/a.zip", str(self.allowed.resolve()),
                                             autostart=True)
            prepare.assert_called_with(adapter, str(self.allowed.resolve()))
            # sending the same event again must not re-send; it is skipped silently (v0.1.25)
            self.assertEqual(self.req("/api/rb/intercepts/send", {"tab": "b1", "id": 1}), (200, {"state": "dup"}))
            self.assertEqual(transfer.call_count, 1)
        links = self.req("/api/links")[1]["items"]
        self.assertEqual([(l["url"], l["state"]) for l in links], [("https://files.example/a.zip", "sent")])

    def test_rb_tab_limit_and_memory_refusals_are_japanese_400s(self):
        self.login()
        for i in range(3):
            self.assertEqual(self.req("/api/rb/tabs", {"url": "https://example.com/%d" % i})[0], 201)
        code, body = self.req("/api/rb/tabs", {"url": "https://example.com/x"})
        self.assertEqual(code, 400)
        self.assertIn("最大3枚", body["error"])

    def test_rb_tab_open_repeated_with_the_same_nonce_returns_the_same_tab(self):
        # v0.1.60: a lost answer is asked again with the same nonce: no second (orphan) tab
        self.login()
        code, first = self.req("/api/rb/tabs", {"url": "https://example.com/a", "nonce": "n0nce-1234abcd"})
        self.assertEqual(code, 201)
        code, again = self.req("/api/rb/tabs", {"url": "https://example.com/a", "nonce": "n0nce-1234abcd"})
        self.assertEqual(code, 201)
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(again["url"], "https://example.com/a")
        code, other = self.req("/api/rb/tabs", {"url": "https://example.com/b", "nonce": "n0nce-5678abcd"})
        self.assertNotEqual(other["id"], first["id"])
        self.assertEqual(len(self.req("/api/rb/status")[1]["tabs"]), 2)
        self.assertEqual(self.req("/api/rb/tabs", {"url": "https://example.com/c", "nonce": "bad nonce!"})[0], 400)

    def test_engine_native_remove_refused_in_native_mode(self):
        self.login()
        code, body = self.req("/api/engine/native/remove", {"confirmed": True})
        self.assertEqual(code, 400)
        self.assertIn("コンテナ方式", body["error"])
        self.assertIn("native_leftovers", self.req("/api/engine/status")[1])

    def test_host_filter_allows_private_and_tailscale_not_public(self):
        self.assertTrue(webapp.public_host_allowed("192.168.0.5:17864"))
        self.assertTrue(webapp.public_host_allowed("100.64.1.2:17864"))
        self.assertTrue(webapp.public_host_allowed("[::1]:17864"))
        self.assertFalse(webapp.public_host_allowed("8.8.8.8:17864"))
        self.assertFalse(webapp.public_host_allowed("evil.example:17864"))
        for malformed in ("[::1", "localhost:notaport", "localhost:99999",
                          "localhost:bad", "foo@localhost", "localhost/path",
                          "", "example.com#localhost", "abc def"):
            with self.subTest(host=malformed):
                self.assertFalse(webapp.public_host_allowed(malformed))

    def test_private_lan_peers_and_public_wan_are_distinguished(self):
        for address in ("127.0.0.1", "192.168.68.57", "10.3.4.5",
                        "172.23.2.4", "100.64.1.5", "::1", "fd00::5"):
            with self.subTest(address=address):
                self.assertTrue(webapp.private_peer_allowed(address))
        for address in ("8.8.8.8", "203.0.113.1", "169.254.1.1",
                        "2001:4860:4860::8888", "not-an-ip"):
            with self.subTest(address=address):
                self.assertFalse(webapp.private_peer_allowed(address))

    def test_app_shell_worker_is_served_with_the_version_and_csp_allows_it(self):
        """v0.1.60: /app-sw.js carries the Hub's version (its cache name), needs no login, is scoped to /, and the app
        CSP names worker-src 'self'."""
        with self.client.open(self.url + "/app-sw.js", timeout=4) as response:
            body = response.read().decode("utf-8")
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers.get_content_type(), "text/javascript")
            self.assertEqual(response.headers["Service-Worker-Allowed"], "/")
            self.assertIn("worker-src 'self'", response.headers["Content-Security-Policy"])
        self.assertIn('var VERSION = "%s";' % webapp.VERSION, body)
        self.assertNotIn("__QD_VERSION__", body)
        self.assertIn("worker-src 'self'", webapp.CSP)
        self.assertNotIn("/app-sw.js", webapp.FILES)          # not a plain static file: the version is substituted

    def test_icons_and_private_ip_host_are_served_without_login(self):
        request = urllib.request.Request(self.url + "/api/health",
                                         headers={"Host": "192.168.68.57:17864"})
        with self.client.open(request, timeout=4) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(json.load(response)["version"], "0.1.67")
        with self.client.open(self.url + "/icon.svg", timeout=4) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers.get_content_type(), "image/svg+xml")
            self.assertIn(b"<svg", response.read())
        with self.client.open(self.url, timeout=4) as response:
            html = response.read()
            self.assertIn(b"apple-touch-icon.png", html)
            self.assertIn(b"QnapDownloader", html)

    def test_setup_login_csrf_and_private_api(self):
        self.assertEqual(self.req("/api/settings")[0], 401)
        self.assertEqual(self.req("/api/update/log")[0], 401)
        self.login()
        self.assertEqual(self.req("/api/settings")[1]["auto_start"], False)
        self.assertEqual(self.req("/api/links", {"url": "https://rapidgator.net/file/1"},
                                  csrf=False)[0], 403)
        self.assertEqual(self.req("/api/links", {"url": "https://rapidgator.net/file/1"},
                                  origin=False)[0], 403)
        code, body = self.req("/api/links", {"url": "https://rapidgator.net/file/1"})
        self.assertEqual(code, 201)
        self.assertEqual(body["state"], "staged")
        self.assertEqual(self.req("/api/links")[1]["items"][0]["state"], "staged")
        self.assertEqual(self.req("/api/links/send", {"id": body["id"]})[0], 400)
        self.assertEqual(self.req("/api/links")[1]["items"][0]["state"], "staged")
        self.assertEqual(self.req("/api/logout", {})[0], 200)
        self.assertEqual(self.req("/api/settings")[0], 401)

    def test_tokenless_first_run_and_existing_admin_is_not_replaced(self):
        status, info = self.req("/api/setup/status")
        self.assertEqual(status, 200)
        self.assertFalse(info["configured"])
        self.assertEqual(self.req("/api/setup", {
            "password": "too-short"})[0], 400)
        self.assertFalse(self.store.has_admin())
        self.assertEqual(self.req("/api/setup", {
            "password": "correct horse staple 123"}, origin=False)[0], 403)
        status, info = self.req("/api/setup", {
            "password": "correct horse staple 123"})
        self.assertEqual(status, 201)
        self.assertTrue(info["ok"])
        self.assertTrue(self.req("/api/setup/status")[1]["configured"])
        self.assertEqual(self.req("/api/setup", {
            "password": "another strong password 987"})[0], 403)
        self.assertTrue(self.store.check_password("correct horse staple 123"))
        self.assertFalse(self.store.check_password("another strong password 987"))
        self.assertEqual(self.req("/api/bootstrap", {
            "token": "legacy-token", "password": "another strong password 987"})[0], 401)
        self.assertEqual(self.req("/api/settings")[0], 401)

    def test_no_admin_token_or_password_leaks_in_setup_status(self):
        self.assertEqual(self.req("/api/setup/status"), (200, {"configured": False}))
        self.login()
        status, info = self.req("/api/setup/status")
        self.assertEqual(status, 200)
        self.assertEqual(info, {"configured": True})

    def test_legacy_token_file_is_deleted_not_read(self):
        config = self.root / "config"
        config.mkdir()
        old = config / "bootstrap-token"
        old.write_text("obsolete legacy credential\n", encoding="utf-8")
        self.assertTrue(webapp.remove_legacy_setup_token(self.root))
        self.assertFalse(old.exists())
        self.assertTrue(webapp.remove_legacy_setup_token(self.root))
        self.assertEqual(self.req("/api/setup/status"), (200, {"configured": False}))

    def test_tokenless_setup_works_with_private_lan_host_header(self):
        host = self.host
        request = urllib.request.Request(self.url + "/api/setup",
            data=json.dumps({"password": "correct horse staple 123"}).encode(),
            headers={"Host": "192.168.68.57:17864",
                     "Origin": "http://192.168.68.57:17864",
                     "Content-Type": "application/json"})
        with self.client.open(request, timeout=4) as response:
            self.assertEqual(response.status, 201)
        self.assertTrue(self.store.has_admin())

    def test_csp_allows_first_party_icons_but_blocks_iframes(self):
        req = urllib.request.Request(self.url + "/icon.svg")
        with self.client.open(req, timeout=4) as response:
            csp = response.headers.get("Content-Security-Policy")
            # blob: is for the remote-browser screencast (object URL of /api/rb/frame.jpg)
            self.assertIn("img-src 'self' data: blob:;", csp)
            self.assertNotIn("blob:", csp.replace("img-src 'self' data: blob:;", ""))
            self.assertIn("default-src 'none'", csp)
            self.assertIn("frame-ancestors 'none'", csp)
            # v0.1.35: no iframe browser any more, so nothing may be framed at all
            self.assertIn("frame-src 'self'", csp)
            self.assertNotIn("frame-src https:", csp)
            self.assertNotIn("child-src", csp)
            self.assertEqual(response.headers.get("X-Frame-Options"), "DENY")

    def test_removed_myjd_config_and_probe_requires_running_engine(self):
        self.assertEqual(self.req("/api/jd2/config")[0], 401)
        self.assertEqual(self.req("/api/jd2/connect/test", {})[0], 401)
        self.login()
        self.assertEqual(self.req("/api/jd2/config")[0], 404)
        self.assertEqual(self.req("/api/jd2/connect/test", {}, csrf=False)[0], 403)
        # Engine not installed: fail closed.
        self.assertEqual(self.req("/api/jd2/connect/test", {})[0], 400)
        status = self.req("/api/jd2/status")[1]
        self.assertFalse(status["connected"])
        self.assertIn("エンジン未起動/未導入", status["reason"])
        self.assertFalse(status["can_send"])

    def test_tailnet_peer_helper_and_session_flag(self):
        for addr in ("100.64.0.1", "100.127.255.254", "fd7a:115c:a1e0::1",
                     "::ffff:100.100.1.2"):
            self.assertTrue(webapp.tailnet_peer(addr), addr)
        for addr in ("100.63.0.1", "100.128.0.1", "192.168.0.5", "127.0.0.1",
                     "fd7b::1", "garbage", None):
            self.assertFalse(webapp.tailnet_peer(addr), addr)
        self.assertFalse(self.req("/api/session")[1]["tailnet"])
        with patch.object(webapp, "tailnet_peer", return_value=True):
            self.assertTrue(self.req("/api/session")[1]["tailnet"])

    def test_config_keys_requires_auth_and_running_engine(self):
        self.assertEqual(self.req("/api/engine/config-keys")[0], 401)
        self.login()
        self.assertEqual(self.req("/api/engine/config-keys")[0], 400)
        entries = [
            {"interfaceName": "a.LinkgrabberSettings", "key": "AutoConfirm", "type": "BOOLEAN", "value": True},
            {"interfaceName": "a.GeneralSettings", "key": "DefaultDownloadFolder", "type": "STRING", "value": "/share/x"},
            {"interfaceName": "a.Foo", "key": "ProxyPassword", "type": "STRING", "value": "x"},
            {"interfaceName": "a.Extraction", "key": "ExtractPath", "type": "STRING", "value": "short"},
        ] + [{"interfaceName": "a.Foo", "key": "Extract%d" % i, "type": "INT", "value": i}
             for i in range(300)]
        with patch("jd2_local.LocalAdapter.config_keys",
                   lambda self: jd2_local.filter_config_keys(entries)):
            code, body = self.req("/api/engine/config-keys")
        self.assertEqual(code, 200)
        items = body["items"]
        self.assertEqual(len(items), 200)
        self.assertEqual(items[0]["value"], True)
        self.assertIsNone(items[1]["value"])
        self.assertFalse(any("password" in i["key"].lower() for i in items))

    def test_engine_api_auth_csrf_origin_and_confirmation(self):
        self.assertEqual(self.req("/api/engine/status")[0], 401)
        self.assertEqual(self.req("/api/engine/log")[0], 401)
        for name in ("setup", "start", "stop", "autostart"):
            self.assertEqual(self.req("/api/engine/" + name, {})[0], 401)
        self.login()
        code, status = self.req("/api/engine/status")
        self.assertEqual(code, 200)
        self.assertEqual(status["phase"], "not_installed")
        self.assertEqual([s["id"] for s in status["steps"]][0], "precheck")
        self.assertEqual(self.req("/api/engine/log")[1]["lines"], [])
        for name in ("setup", "start", "stop", "autostart"):
            self.assertEqual(self.req("/api/engine/" + name, {}, csrf=False)[0], 403)
            self.assertEqual(self.req("/api/engine/" + name, {}, origin=False)[0], 403)
        self.assertEqual(self.req("/api/engine/setup", {})[0], 400)
        self.assertEqual(self.req("/api/engine/setup", {"confirmed": "true"})[0], 400)
        self.assertEqual(self.req("/api/engine/stop", {})[0], 400)
        self.assertEqual(self.req("/api/engine/autostart", {"enabled": "yes"})[0], 400)
        self.assertEqual(self.req("/api/engine/start", {})[0], 400)
        # No download folder selected: setup is refused before any thread starts.
        self.assertEqual(self.req("/api/engine/setup", {"confirmed": True})[0], 400)
        self.assertFalse(self.server.engine.status()["setup_running"])
        self.assertEqual(self.req("/api/engine/autostart", {"enabled": True})[0], 200)
        self.assertTrue(self.req("/api/engine/status")[1]["autostart"])

    def test_engine_setup_busy_returns_409(self):
        self.login()
        folder = self.allowed / "dl"
        folder.mkdir()
        self.assertEqual(self.req("/api/settings/folder", {"path": str(folder)})[0], 200)
        started = []
        self.server.engine.start_setup = lambda mode=None: started.append(mode)
        self.assertEqual(self.req("/api/engine/setup", {"confirmed": True})[0], 202)
        self.assertEqual(started, [None])
        self.assertEqual(self.req("/api/engine/setup", {"confirmed": True, "mode": "container"})[0], 202)
        self.assertEqual(started, [None, "container"])
        for bad in ("docker", ["container"], 1):
            self.assertEqual(self.req("/api/engine/setup", {"confirmed": True, "mode": bad})[0], 400)
        def busy(mode=None):
            raise engine_supervisor.SetupBusy("実行中")
        self.server.engine.start_setup = busy
        self.assertEqual(self.req("/api/engine/setup", {"confirmed": True})[0], 409)

    def test_folder_picker_persists_allowed_path_only(self):
        folder = self.allowed / "subdir"
        folder.mkdir()
        self.login()
        self.assertEqual(self.req("/api/settings/folder", {"path": "/etc"})[0], 400)
        self.assertEqual(self.req("/api/settings/folder", {"path": str(folder)})[0], 200)
        self.assertEqual(self.req("/api/settings")[1]["download_dir"], str(folder))
        self.assertEqual(self.req("/api/folders")[0], 200)
        self.assertEqual(self.req("/api/folders?path="+str(self.allowed))[0], 200)

    def test_rapidgator_preview_and_digest_confirmed_staging(self):
        content=("https://rapidgator.net/file/1234?utm=1 "
                 "https://rg.to/file/1234 "
                 "https://rg.to/file/5678")
        self.assertEqual(self.req("/api/rapidgator/extract",
                                  {"content": content})[0], 401)
        self.login()
        self.assertEqual(self.req("/api/rapidgator/extract",
                                  {"content": content}, csrf=False)[0], 403)
        code, preview = self.req("/api/rapidgator/extract",
                                 {"content": content, "limit": 20})
        self.assertEqual(code, 200)
        self.assertEqual(preview["count"], 2)
        self.assertFalse(preview["saved"])
        self.assertEqual(self.req("/api/links")[1]["items"], [])
        self.assertEqual(self.req("/api/rapidgator/extract",
             {"content": content+"changed", "digest": preview["digest"],
              "commit": True})[0], 400)
        code, result = self.req("/api/rapidgator/extract",
             {"content": content, "digest": preview["digest"], "commit": True})
        self.assertEqual(code, 200)
        self.assertEqual(result["staged"], 2)
        self.assertTrue(result["saved"])
        self.assertEqual(len(self.req("/api/links")[1]["items"]), 2)
        code, repeated = self.req("/api/rapidgator/extract",
             {"content": content, "digest": preview["digest"], "commit": True})
        self.assertEqual(code, 200)
        self.assertEqual(repeated["staged"], 0)

    def test_rapidgator_large_paste_does_not_allow_unbounded_body(self):
        self.login()
        content="https://rapidgator.net/file/1234 " + "a" * 18000
        code, value = self.req("/api/rapidgator/extract", {"content": content})
        self.assertEqual(code, 200)
        self.assertEqual(value["count"], 1)
        oversized="a"*68000
        self.assertEqual(self.req("/api/rapidgator/extract",
                                 {"content": oversized})[0], 400)

    def test_browser_url_validation_requires_login_and_never_fetches_url(self):
        import urllib.parse
        valid="/api/browser/validate?url="+urllib.parse.quote(
            "https://rapidgator.net/file/1234",safe="")
        self.assertEqual(self.req(valid)[0], 401)
        self.login()
        code, data=self.req(valid)
        self.assertEqual(code,200)
        self.assertEqual(data["url"],"https://rapidgator.net/file/1234")
        self.assertEqual(sorted(data), ["url"])      # v0.1.35: no iframe-only fields
        for url in ("http://example.com", "https://192.168.68.57:17864",
                    "https://localhost/", "javascript:alert(1)"):
            with self.subTest(url=url):
                path="/api/browser/validate?url="+urllib.parse.quote(url,safe="")
                self.assertEqual(self.req(path)[0],400)
        with self.client.open(self.url,timeout=4) as response:
            html=response.read().decode()
            csp=response.headers.get("Content-Security-Policy")
        # v0.1.35: no iframes at all, so no sandbox values, and nothing may be framed.
        script = (Path(webapp.__file__).parent / "web/app.js").read_text()
        self.assertNotIn("SANDBOX_", script)
        self.assertNotIn("createElement(\"iframe\")", script)
        self.assertIn("frame-src 'self'",csp)
        self.assertNotIn("frame-src https:",csp)
        self.assertIn("frame-ancestors 'none'",csp)
        # v0.1.41: the mirror copy relies on its own CSP (WebKit). v0.1.57: the one sandbox is the passthrough frame's.
        self.assertEqual(html.count('sandbox="'), 1)
        self.assertEqual(html.count("allow-scripts"), 1)
        self.assertIn('id="ptFrame"', html[html.index('sandbox="') - 200:html.index('sandbox="')])
        self.assertEqual(html.count("allow-downloads"), 1)      # the same single attribute
        self.assertNotIn("allow-popups", html)
        self.assertNotIn("allow-top-navigation", html)

    def test_browser_validate_rejects_hub_own_host_and_tailnet_names(self):
        import urllib.parse
        self.login()
        with patch.dict(os.environ, {"DOWNLOADER_ALLOWED_HOSTS": "hub.example.com"}):
            def check(url, host):
                path = "/api/browser/validate?url=" + urllib.parse.quote(url, safe="")
                request = urllib.request.Request(self.url + path, headers={"Host": host})
                try:
                    with self.client.open(request, timeout=4) as response:
                        return response.status, json.load(response)
                except urllib.error.HTTPError as error:
                    return error.code, json.load(error)
            # Hub's own hostname (case-insensitive, any port) is refused.
            for host in ("hub.example.com:17864", "HUB.Example.com"):
                with self.subTest(host=host):
                    code, data = check("https://hub.example.com/page", host)
                    self.assertEqual(code, 400)
                    self.assertIn("Hub", data["error"])
            # Another public name is still fine for the same Host.
            code, data = check("https://rapidgator.net/file/1", "hub.example.com:17864")
            self.assertEqual(code, 200)
            # Tailscale MagicDNS names are always refused.
            code, data = check("https://myhub.tail1234.ts.net/", "hub.example.com")
            self.assertEqual(code, 400)
            self.assertIn("Tailscale", data["error"])

    # ---- v0.1.13: HTTPS behind Tailscale Serve ---------------------------
    def test_request_is_https_only_for_loopback_peers(self):
        https = {"X-Forwarded-Proto": "https", "Host": "192.168.1.5:17864"}
        self.assertTrue(webapp.request_is_https("127.0.0.1", https))
        self.assertTrue(webapp.request_is_https("::1", https))
        self.assertTrue(webapp.request_is_https("::ffff:127.0.0.1", https))
        self.assertTrue(webapp.request_is_https("127.0.0.1", {"Host": "nas.tail1234.ts.net"}))
        self.assertTrue(webapp.request_is_https("127.0.0.1", {"Host": "NAS.tail1234.TS.NET:443"}))
        # Forged header or ts.net Host from any non-loopback peer is ignored.
        for peer in ("192.168.1.5", "100.64.0.9", "fd7a:115c:a1e0::5", "8.8.8.8", "garbage", None):
            with self.subTest(peer=peer):
                self.assertFalse(webapp.request_is_https(peer, https))
                self.assertFalse(webapp.request_is_https(peer, {"Host": "nas.tail1234.ts.net"}))
        self.assertFalse(webapp.request_is_https("127.0.0.1", {"Host": "192.168.1.5"}))
        self.assertFalse(webapp.request_is_https("127.0.0.1", {"X-Forwarded-Proto": "http"}))
        self.assertFalse(webapp.request_is_https("127.0.0.1", {"Host": "evilts.net"}))
        self.assertFalse(webapp.request_is_https("127.0.0.1", {}))

    def test_ts_net_host_allowed_only_for_loopback_and_tailnet_peers(self):
        name = "nas.tail1234.ts.net:443"
        self.assertTrue(webapp.public_host_allowed(name, "127.0.0.1"))
        self.assertTrue(webapp.public_host_allowed(name, "::1"))
        self.assertTrue(webapp.public_host_allowed("nas.tail1234.ts.net", "100.100.1.2"))
        self.assertTrue(webapp.public_host_allowed("nas.tail1234.ts.net", "fd7a:115c:a1e0::7"))
        self.assertFalse(webapp.public_host_allowed(name, "192.168.1.5"))
        self.assertFalse(webapp.public_host_allowed(name, "8.8.8.8"))
        self.assertFalse(webapp.public_host_allowed(name))
        for bad in ("ts.net", "bad_name.tail1234.ts.net", "-a.tail1234.ts.net",
                    "nas.tail1234.ts.net.evil.com", "nas.tail1234.ts.net:99999", "a b.tail1234.ts.net",
                    "user@nas.tail1234.ts.net", "nas..tail1234.ts.net"):
            with self.subTest(host=bad):
                self.assertFalse(webapp.public_host_allowed(bad, "127.0.0.1"))

    def test_direct_tailnet_access_accepts_ts_net_and_short_names_only_from_tailnet(self):
        for peer in ("100.100.1.2", "fd7a:115c:a1e0::7", "127.0.0.1", "::1"):
            for host in ("nas.tail1234.ts.net", "nas.tail1234.ts.net:17864", "NAS", "nas:17864",
                         "my-nas:17864"):
                with self.subTest(peer=peer, host=host):
                    self.assertTrue(webapp.public_host_allowed(host, peer))
            for host in ("evil.example.com", "evil.example.com:17864", "nas.local", "a.b",
                         "bad_name", "-nas", "12345", "nas:99999", "nas/x", "u@nas"):
                with self.subTest(peer=peer, host=host):
                    self.assertFalse(webapp.public_host_allowed(host, peer))
        for peer in ("192.168.68.57", "10.0.0.5", "8.8.8.8"):
            for host in ("nas", "nas:17864", "nas.tail1234.ts.net:17864"):
                with self.subTest(peer=peer, host=host):
                    self.assertFalse(webapp.public_host_allowed(host, peer))
        self.assertFalse(webapp.public_host_allowed("nas"))
        handler = webapp.Handler.__new__(webapp.Handler)
        handler.headers = {"Host": "nas.tail1234.ts.net:17864"}
        handler.client_address = ("100.100.1.2", 1)
        self.assertTrue(handler.check_host())
        handler.headers = {"Host": "nas:17864"}
        self.assertTrue(handler.check_host())
        handler.client_address = ("192.168.68.57", 1)
        self.assertFalse(handler.check_host())
        handler.client_address = ("100.100.1.2", 1)
        handler.headers = {"Host": "evil.example.com"}
        self.assertFalse(handler.check_host())

    def test_ts_net_host_served_for_loopback_peer_and_session_reports_https(self):
        host = "nas.tail1234.ts.net"
        code, headers, data = self.raw("/api/session", host=host)
        self.assertEqual(code, 200)
        self.assertTrue(data["https"])
        code, headers, data = self.raw("/api/session")
        self.assertFalse(data["https"])
        code, headers, data = self.raw("/api/session", headers={"X-Forwarded-Proto": "https"})
        self.assertTrue(data["https"])
        # A ts.net Host is refused from a non-tailnet, non-loopback peer.
        handler = webapp.Handler.__new__(webapp.Handler)
        handler.client_address = ("192.168.1.5", 1)
        handler.headers = {"Host": host}
        self.assertFalse(handler.check_host())
        handler.client_address = ("127.0.0.1", 1)
        self.assertTrue(handler.check_host())

    def test_cookie_is_secure_when_request_is_https_and_never_hsts(self):
        self.assertEqual(self.req("/api/setup", {"password": "correct horse staple 123"})[0], 201)
        login = {"password": "correct horse staple 123"}
        plain = self.raw("/api/login", login, {"Origin": self.url})
        self.assertEqual(plain[0], 200)
        cookie = plain[1]["Set-Cookie"]
        self.assertNotIn("Secure", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        forwarded = self.raw("/api/login", login, {"Origin": self.url, "X-Forwarded-Proto": "https"})
        self.assertIn("; Secure", forwarded[1]["Set-Cookie"])
        self.assertIn("HttpOnly", forwarded[1]["Set-Cookie"])
        host = "nas.tail1234.ts.net"
        via_serve = self.raw("/api/login", login, {"Origin": "https://" + host}, host=host)
        self.assertEqual(via_serve[0], 200)
        self.assertIn("; Secure", via_serve[1]["Set-Cookie"])
        self.assertIsNone(via_serve[1].get("Strict-Transport-Security"))
        self.assertIsNone(forwarded[1].get("Strict-Transport-Security"))
        self.server.secure_cookies = True
        flagged = self.raw("/api/login", login, {"Origin": self.url})
        self.assertIn("; Secure", flagged[1]["Set-Cookie"])

    # ---- v0.1.13: iPhone share tokens ------------------------------------
    def make_token(self, name="iPhone"):
        self.login()
        code, body = self.req("/api/share/tokens", {"name": name})
        self.assertEqual(code, 201)
        return body

    def share(self, token, body=None, headers=None, host=None):
        sent = {"Authorization": "Bearer " + token} if token is not None else {}
        sent.update(headers or {})
        return self.raw("/api/share", {"text": "nothing to see"} if body is None else body, sent, host)

    def test_share_token_admin_api_auth_csrf_and_secrecy(self):
        for path, body in (("/api/share/tokens", {"name": "a"}), ("/api/share/tokens/delete", {"id": 1})):
            self.assertEqual(self.req(path, body)[0], 401)
        self.assertEqual(self.req("/api/share/tokens")[0], 401)
        self.login()
        self.assertEqual(self.req("/api/share/tokens", {"name": "a"}, csrf=False)[0], 403)
        self.assertEqual(self.req("/api/share/tokens", {"name": "a"}, origin=False)[0], 403)
        self.assertEqual(self.req("/api/share/tokens", {"name": ""})[0], 400)
        self.assertEqual(self.req("/api/share/tokens", {"name": "x" * 41})[0], 400)
        self.assertEqual(self.req("/api/share/tokens", {"name": 5})[0], 400)
        code, made = self.req("/api/share/tokens", {"name": " iPhone 15 "})
        self.assertEqual(code, 201)
        self.assertEqual(made["name"], "iPhone 15")
        self.assertGreaterEqual(len(made["token"]), 40)
        code, listing = self.req("/api/share/tokens")
        self.assertEqual(code, 200)
        self.assertEqual([i["name"] for i in listing["items"]], ["iPhone 15"])
        self.assertEqual(set(listing["items"][0]), {"id", "name", "created_at", "last_used_at"})
        self.assertNotIn(made["token"], json.dumps(listing))
        # Only the hash is stored.
        import hashlib
        with self.store.connect() as db:
            stored = db.execute("SELECT token_hash FROM share_tokens").fetchone()[0]
            self.assertEqual(stored, hashlib.sha256(made["token"].encode()).hexdigest())
            self.assertNotIn(made["token"], json.dumps(db.execute("SELECT * FROM share_tokens").fetchall()))
        self.assertEqual(self.req("/api/share/tokens/delete", {"id": made["id"]}, csrf=False)[0], 403)
        self.assertEqual(self.req("/api/share/tokens/delete", {"id": "1"})[0], 400)
        self.assertEqual(self.req("/api/share/tokens/delete", {"id": 999})[0], 400)
        self.assertEqual(self.req("/api/share/tokens/delete", {"id": made["id"]})[0], 200)
        self.assertEqual(self.req("/api/share/tokens")[1]["items"], [])

    def test_share_token_limit_is_ten(self):
        self.login()
        for index in range(10):
            self.assertEqual(self.req("/api/share/tokens", {"name": "d%d" % index})[0], 201)
        self.assertEqual(self.req("/api/share/tokens", {"name": "d10"})[0], 400)

    def test_share_stages_rapidgator_links_with_good_token(self):
        token = self.make_token()["token"]
        text = ("https://example.com/a https://rapidgator.net/file/1111?x=1\n"
                "https://rg.to/file/2222 https://rapidgator.net/file/1111")
        code, _, data = self.share(token, {"text": text})
        self.assertEqual((code, data), (200, {"added": 2, "found": 2}))
        # Dedupe: the second send adds nothing new.
        code, _, data = self.share(token, {"text": text})
        self.assertEqual((code, data), (200, {"added": 0, "found": 2}))
        urls = {i["url"] for i in self.req("/api/links")[1]["items"]}
        self.assertEqual(urls, {"https://rapidgator.net/file/1111", "https://rapidgator.net/file/2222"})
        # "url" is accepted too; a lone https URL is staged when no RG link exists.
        code, _, data = self.share(token, {"url": "https://example.com/page?q=1"})
        self.assertEqual((code, data), (200, {"added": 1, "found": 1}))
        # Text that is neither: nothing staged.
        code, _, data = self.share(token, {"text": "see https://example.com/a and more"})
        self.assertEqual((code, data), (200, {"added": 0, "found": 0}))
        code, _, data = self.share(token, {"text": "http://example.com/plain"})
        self.assertEqual(code, 400)
        for bad in ({"text": 5}, {}, {"text": "x" * 70000}):
            self.assertEqual(self.share(token, bad)[0], 400)
        links = self.req("/api/links")[1]["items"]
        self.assertTrue(all(i["state"] == "staged" for i in links))
        self.assertEqual(len(links), 3)

    def test_share_never_touches_jd2(self):
        token = self.make_token()["token"]
        with patch("jd2_transfer.transfer") as transfer, \
                patch("jd2_transfer.prepare_transfer") as prepare, \
                patch.object(self.server.jd2, "_require") as require, \
                patch.object(self.server.engine, "start") as start:
            code, _, data = self.share(token, {"text": "https://rapidgator.net/file/4444"})
        self.assertEqual(code, 200)
        self.assertEqual(data["added"], 1)
        for mock in (transfer, prepare, require, start):
            mock.assert_not_called()

    def test_share_rejects_bad_missing_and_revoked_tokens_uniformly(self):
        made = self.make_token()
        good = made["token"]
        results = []
        for supplied in (None, "", "wrong", good[:-1], good + "x", good.upper()):
            code, _, data = self.share(supplied)
            results.append((code, data))
        self.assertEqual(results, [(401, {"error": "認証に失敗しました"})] * 6)
        code, _, data = self.raw("/api/share", {"text": "nothing"}, {"Authorization": "Basic " + good})
        self.assertEqual(code, 401)
        # A logged-in browser session (cookie) is not enough on its own.
        code, body = self.req("/api/share", {"text": "https://rapidgator.net/file/4444"})
        self.assertEqual(code, 401)
        self.assertEqual(self.req("/api/links")[1]["items"], [])
        self.assertEqual(self.share(good)[0], 200)
        self.assertEqual(self.req("/api/share/tokens/delete", {"id": made["id"]})[0], 200)
        self.assertEqual(self.share(good)[0], 401)

    def test_share_rejects_foreign_origin_and_bad_host(self):
        token = self.make_token()["token"]
        for origin in ("https://evil.example", "null", "http://127.0.0.1:1"):
            with self.subTest(origin=origin):
                self.assertEqual(self.share(token, headers={"Origin": origin})[0], 403)
        self.assertEqual(self.share(token, headers={"Origin": self.url})[0], 200)
        self.assertEqual(self.share(token)[0], 200)   # Shortcuts send no Origin
        self.assertEqual(self.share(token, host="evil.example")[0], 403)
        self.assertEqual(self.share(token, host="8.8.8.8")[0], 403)
        # No CORS surface: preflight is not implemented.
        request = urllib.request.Request(self.url + "/api/share", method="OPTIONS",
                                         headers={"Host": self.host, "Origin": "https://evil.example"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with self.assertRaises(urllib.error.HTTPError) as caught:
            opener.open(request, timeout=4)
        self.assertGreaterEqual(caught.exception.code, 400)
        self.assertIsNone(caught.exception.headers.get("Access-Control-Allow-Origin"))

    def test_share_rate_limit_per_token_and_failure_lockout(self):
        first = self.make_token("a")["token"]
        second = self.req("/api/share/tokens", {"name": "b"})[1]["token"]
        for _ in range(30):
            self.assertEqual(self.share(first)[0], 200)
        self.assertEqual(self.share(first)[0], 429)
        self.assertEqual(self.share(second)[0], 200)   # limit is per token
        webapp.SHARE_RATE.clear()
        self.assertEqual(self.share(first)[0], 200)
        for _ in range(20):
            self.assertEqual(self.share("wrong")[0], 401)
        self.assertEqual(self.share("wrong")[0], 429)
        self.assertEqual(self.share(first)[0], 429)    # address locked out for 5 minutes
        webapp.SHARE_FAILED.clear()
        self.assertEqual(self.share(first)[0], 200)

    def test_share_requires_json_and_bounded_body(self):
        token = self.make_token()["token"]
        code, _, _ = self.raw("/api/share", b"text=x", {"Authorization": "Bearer " + token,
                                                       "Content-Type": "text/plain"})
        self.assertEqual(code, 400)
        code, _, _ = self.raw("/api/share", b"x" * 80000, {"Authorization": "Bearer " + token})
        self.assertEqual(code, 400)

    def test_update_channel_select_rc_releases_without_installer_execution(self):
        self.assertEqual(self.req("/api/update/check?channel=rc")[0],401)
        self.login()
        asset={"version":"v0.1.7-rc1","size":9000}
        with patch.object(webapp.updater,"latest",return_value=asset) as latest:
            code,result=self.req("/api/update/check?channel=rc")
        self.assertEqual(code,200)
        self.assertEqual(result["release"]["version"],"v0.1.7-rc1")
        self.assertEqual(result["channel"],"rc")
        latest.assert_called_once_with(webapp.VERSION,"x86_64","rc")
        with patch.object(webapp.updater,"latest",return_value=None) as latest:
            code,result=self.req("/api/update/check?channel=stable")
        self.assertEqual(code,200)
        self.assertFalse(result["available"])
        latest.assert_called_once_with(webapp.VERSION,"x86_64","stable")
        self.assertEqual(self.req("/api/update/check?channel=invalid")[0],400)
        # Applies still require a real QNAP package registration.
        self.assertEqual(self.req("/api/update/apply",
            {"version":"v0.1.7-rc1","channel":"rc"})[0],400)

    def test_downloads_page_is_auth_only_and_readonly(self):
        self.assertEqual(self.req("/api/jd2/downloads")[0], 401)
        self.login()
        self.assertEqual(self.req("/api/jd2/downloads")[0], 400)
        calls = []
        class ReadOnly:
            def list_downloads(self, page):
                calls.append(page)
                return {"source":"local-readonly","page":page,
                        "page_size":30,"packages":[],"links":[{
                            "name":"filename.zip","host":"rapidgator.net",
                            "progress":25,"status":"待機",
                            "bytes_loaded":5,"bytes_total":20,
                            "speed_bytes":0,"eta_seconds":None,
                            "running":False,"finished":False,"enabled":True}],
                        "has_more":False,"notice":"readonly"}
        self.server.jd2 = jd2_control.JD2Controller(ReadOnly())
        code,result = self.req("/api/jd2/downloads?page=2")
        self.assertEqual(code,200)
        self.assertEqual(calls,[2])
        self.assertEqual(result["links"][0]["progress"],25)
        self.assertNotIn("password",str(result))
        self.assertEqual(self.req("/api/jd2/downloads?page=999")[0],400)
        self.assertEqual(self.req("/api/jd2/downloads?page=-1")[0],400)
        self.assertEqual(calls,[2])
        with self.client.open(self.url,timeout=4) as response:
            html=response.read().decode()
        self.assertIn('data-tab="downloads"',html)
        self.assertIn('id="downloadsRefresh"',html)
        self.assertIn('id="downloadList"',html)

    # ---- v0.1.23: premium accounts -------------------------------------
    HTTPS = {"X-Forwarded-Proto": "https"}
    PW = "TopSecret-Pw-123"

    def accounts_setup(self):
        self.login()
        self.fake = FakeAdapter()
        self.server.jd2 = jd2_control.JD2Controller(self.fake)
        self.server.accounts = jd2_accounts.AccountService(sleep=lambda s: None)

    def add_account(self, user="alice@example.com", password=None):
        return self.req("/api/accounts/add", {"host": "rapidgator.net", "username": user,
                                             "password": password or self.PW}, extra=self.HTTPS)

    def test_accounts_apis_require_login_csrf_and_origin(self):
        posts = [("add", {"host": "rapidgator.net", "username": "u", "password": "p"}),
                 ("update", {"id": "1", "username": "u", "password": "p"}),
                 ("enable", {"id": "1", "enabled": True}), ("refresh", {"id": "1"}),
                 ("delete", {"id": "1", "confirm_host": "rapidgator.net"})]
        for path in ("/api/accounts", "/api/accounts/hosts"):
            self.assertEqual(self.req(path)[0], 401)
        for action, body in posts:
            self.assertEqual(self.req("/api/accounts/" + action, body, extra=self.HTTPS)[0], 401)
        self.accounts_setup()
        for action, body in posts:
            with self.subTest(action=action):
                self.assertEqual(self.req("/api/accounts/" + action, body, csrf=False, extra=self.HTTPS)[0], 403)
                self.assertEqual(self.req("/api/accounts/" + action, body, origin=False, extra=self.HTTPS)[0], 403)
        self.assertEqual(self.req("/api/accounts/nope", {}, extra=self.HTTPS)[0], 404)
        self.assertEqual([c for c in self.fake.calls if c[0] not in ("list", "hosters")], [])

    def test_accounts_need_a_running_engine(self):
        self.login()   # real (stopped) engine
        self.assertEqual(self.req("/api/accounts")[0], 503)
        self.assertEqual(self.req("/api/accounts/hosts")[0], 503)
        code, body = self.add_account()
        self.assertEqual(code, 503)
        self.assertNotIn(self.PW, json.dumps(body))
        self.assertIn("エンジン", body["error"])

    def test_list_and_hosts_are_masked_cached_and_leak_nothing(self):
        self.accounts_setup()
        self.fake.accounts[7] = {"UUID": 7, "hostname": "rapidgator.net", "username": "alice@example.com",
                                 "enabled": True, "valid": True, "validUntil": 4_000_000_000_000,
                                 "trafficLeft": 5 * 1024 ** 3, "trafficMax": 10 * 1024 ** 3,
                                 "password": "RAWPW", "cookies": "RAWCOOKIE", "token": "RAWTOKEN"}
        code, data = self.req("/api/accounts")   # reads work over plain HTTP too
        self.assertEqual(code, 200)
        text = json.dumps(data, ensure_ascii=False)
        for needle in ("RAWPW", "RAWCOOKIE", "RAWTOKEN", "alice@example.com"):
            self.assertNotIn(needle, text)
        self.assertEqual(data["items"][0]["display_username"], "al••••@example.com")
        self.assertEqual(data["items"][0]["id"], "7")
        self.fake.accounts[8] = dict(self.fake.accounts[7], UUID=8)
        self.assertEqual(len(self.req("/api/accounts")[1]["items"]), 1)            # 30 s cache
        self.assertEqual(len(self.req("/api/accounts?refresh=1")[1]["items"]), 2)  # bypass
        self.assertEqual(self.req("/api/accounts/hosts")[1]["items"][0], "rapidgator.net")
        self.req("/api/accounts/hosts")
        self.assertEqual(len([c for c in self.fake.calls if c[0] == "hosters"]), 1)  # 10 min cache

    def test_credential_writes_need_https_or_tailnet(self):
        self.accounts_setup()
        code, body = self.req("/api/accounts/add", {"host": "rapidgator.net", "username": "u",
                                                    "password": self.PW})       # plain-HTTP LAN peer
        self.assertEqual(code, 403)
        self.assertEqual(body["error"], "パスワードはHTTPSまたはTailscale経由でのみ送信できます")
        self.assertNotIn(self.PW, json.dumps(body))
        self.assertEqual(self.req("/api/accounts/update", {"id": "1", "username": "u", "password": self.PW})[0], 403)
        self.assertEqual([c for c in self.fake.calls if c[0] in ("add", "set")], [])
        with patch.object(webapp, "tailnet_peer", return_value=True):         # WireGuard peer
            self.assertEqual(self.add_account_plain()[0], 200)
        self.assertEqual(self.add_account()[0], 200)                           # HTTPS terminator
        # Non-credential changes are allowed on plain HTTP once logged in.
        ident = str(self.fake.next_id)
        self.assertEqual(self.req("/api/accounts/refresh", {"id": ident})[0], 200)
        self.assertEqual(self.req("/api/accounts/enable", {"id": ident, "enabled": False})[0], 200)

    def add_account_plain(self):
        return self.req("/api/accounts/add", {"host": "rapidgator.net", "username": "bob@example.com",
                                             "password": self.PW})

    def test_transport_rule_helper(self):
        https = {"X-Forwarded-Proto": "https", "Host": "192.168.1.5:17864"}
        self.assertTrue(webapp.credentials_transport_ok("127.0.0.1", https))
        self.assertTrue(webapp.credentials_transport_ok("127.0.0.1", {"Host": "nas.tail1.ts.net"}))
        self.assertTrue(webapp.credentials_transport_ok("100.100.1.2", {"Host": "nas:17864"}))
        self.assertTrue(webapp.credentials_transport_ok("fd7a:115c:a1e0::7", {}))
        for peer in ("192.168.1.5", "10.0.0.5", "8.8.8.8", "garbage", None):
            self.assertFalse(webapp.credentials_transport_ok(peer, https), peer)   # forged header
        self.assertFalse(webapp.credentials_transport_ok("127.0.0.1", {"Host": "192.168.1.5"}))
        self.assertFalse(webapp.credentials_transport_ok("192.168.1.5", {}))

    def test_add_flow_masks_validates_host_and_never_echoes_the_password(self):
        self.accounts_setup()
        code, data = self.add_account()
        self.assertEqual(code, 200)
        text = json.dumps(data, ensure_ascii=False)
        self.assertNotIn(self.PW, text)
        self.assertNotIn("alice@example.com", text)
        self.assertEqual(data["item"]["display_username"], "al••••@example.com")
        self.assertEqual(data["item"]["status"], "valid")
        # host must come from the hoster list
        for host in ("evil.example", "", None, 5, "rapidgator.net/../x"):
            with self.subTest(host=host):
                code, body = self.req("/api/accounts/add", {"host": host, "username": "u", "password": self.PW},
                                      extra=self.HTTPS)
                self.assertEqual(code, 400)
                self.assertNotIn(self.PW, json.dumps(body))
        for body in ({"host": "rapidgator.net", "username": "", "password": "p"},
                     {"host": "rapidgator.net", "username": "u" * 201, "password": "p"},
                     {"host": "rapidgator.net", "username": "u", "password": "p" * 501},
                     {"host": "rapidgator.net", "username": "u", "password": "a\nb"},
                     {"host": "rapidgator.net", "username": "u\x00", "password": "p"}):
            self.assertEqual(self.req("/api/accounts/add", body, extra=self.HTTPS)[0], 400)
        self.assertEqual(len([c for c in self.fake.calls if c[0] == "add"]), 1)

    def test_account_bodies_are_limited_to_4_kib(self):
        self.accounts_setup()
        big = {"host": "rapidgator.net", "username": "u", "password": "p", "pad": "x" * 4200}
        code, body = self.req("/api/accounts/add", big, extra=self.HTTPS)
        self.assertEqual(code, 400)
        self.assertEqual(body["error"], "リクエストが大きすぎます")
        self.assertEqual([c for c in self.fake.calls if c[0] == "add"], [])

    def test_add_and_update_rate_limit_is_ten_per_ten_minutes(self):
        self.accounts_setup()
        for _ in range(10):
            self.assertEqual(self.add_account()[0], 200)
        code, body = self.add_account()
        self.assertEqual(code, 429)
        ident = str(self.fake.next_id)
        code, _ = self.req("/api/accounts/update", {"id": ident, "username": "u", "password": self.PW},
                           extra=self.HTTPS)
        self.assertEqual(code, 429)
        self.assertEqual(self.req("/api/accounts")[0], 200)     # reads are not limited
        self.assertEqual(len([c for c in self.fake.calls if c[0] == "add"]), 10)

    def test_update_enable_refresh_delete_flow_and_confirm_host(self):
        self.accounts_setup()
        self.add_account()
        ident = str(self.fake.next_id)
        code, data = self.req("/api/accounts/update", {"id": ident, "username": "carol", "password": self.PW},
                              extra=self.HTTPS)
        self.assertEqual(code, 200)
        self.assertNotIn(self.PW, json.dumps(data))
        self.assertEqual(data["item"]["status"], "invalid")
        self.assertEqual(self.req("/api/accounts/enable", {"id": ident, "enabled": False})[1]["item"]["enabled"], False)
        self.assertEqual(self.req("/api/accounts/enable", {"id": ident, "enabled": "no"})[0], 400)
        self.assertEqual(self.req("/api/accounts/refresh", {"id": ident})[0], 200)
        self.assertEqual(self.req("/api/accounts/refresh", {"id": "424242"})[0], 404)
        self.assertEqual(self.req("/api/accounts/delete", {"id": ident, "confirm_host": "other.example"})[0], 400)
        self.assertEqual(self.req("/api/accounts/delete", {"id": ident})[0], 400)
        self.assertIn(self.fake.next_id, self.fake.accounts)
        code, data = self.req("/api/accounts/delete", {"id": ident, "confirm_host": "rapidgator.net"})
        self.assertEqual((code, data), (200, {"deleted": True, "id": ident}))
        self.assertEqual(self.req("/api/accounts?refresh=1")[1]["items"], [])

    def test_failed_account_call_leaves_no_secret_in_response_or_ram_logs(self):
        self.accounts_setup()
        self.fake.fail["add"] = True
        code, body = self.add_account()
        self.assertEqual(code, 503)
        self.assertNotIn(self.PW, json.dumps(body, ensure_ascii=False))
        self.assertNotIn("alice@example.com", json.dumps(body, ensure_ascii=False))
        self.assertNotIn(self.PW, "\n".join(self.server.engine.log_lines()))

    def test_schema_diagnostic_lists_keys_and_types_never_values(self):
        self.assertEqual(self.req("/api/accounts/schema")[0], 401)
        self.login()
        self.assertEqual(self.req("/api/accounts/schema")[0], 503)   # engine stopped
        self.fake = FakeAdapter()
        self.server.jd2 = jd2_control.JD2Controller(self.fake)
        self.server.accounts = jd2_accounts.AccountService(sleep=lambda s: None)
        self.fake.accounts[1] = {"UUID": 1, "hostname": "rapidgator.net", "username": "alice",
                                 "password": "LEAK-PW-1", "cookies": ["LEAK-C"], "valid": True}
        self.fake.accounts[2] = {"UUID": 2, "hostname": "bad host", "token": "LEAK-T"}   # dropped
        self.fake.accounts[3] = {"nothing": None}                                         # dropped
        self.fake.hosts = ["rapidgator.net", "x.example", "y y", "z.example", "w.example"]
        code, data = self.req("/api/accounts/schema")
        self.assertEqual(code, 200)
        text = json.dumps(data, ensure_ascii=False)
        for needle in ("LEAK", "alice"):
            self.assertNotIn(needle, text)
        self.assertEqual((data["count"], data["normalized"], data["dropped"]), (3, 1, 2))
        first = data["samples"][0]
        self.assertEqual(first, sorted(first, key=lambda e: e["key"]))
        self.assertIn({"key": "password", "type": "string"}, first)
        self.assertIn({"key": "cookies", "type": "array"}, first)
        self.assertEqual(data["hoster_item_type"], "string")
        self.assertEqual(data["hoster_first"], ["rapidgator.net", "x.example"])
        self.assertEqual(len(data["samples"]), 3)

    def test_accounts_in_version_and_static_files(self):
        with self.client.open(self.url + "/", timeout=4) as response:
            html = response.read().decode()
        self.assertIn('data-sub="accounts"', html)
        self.assertEqual(webapp.ACCOUNT_BODY_MAX, 4096)

    def test_delete_apis_require_auth_csrf_and_only_staged_links(self):
        link = {"id": 1}
        self.assertEqual(self.req("/api/links/delete", link)[0], 401)
        self.assertEqual(self.req("/api/bookmarks/delete", link)[0], 401)
        self.login()
        added = self.req("/api/links", {"url": "https://example.com/a"})[1]
        sent = self.req("/api/links", {"url": "https://example.com/b"})[1]
        mark = self.req("/api/bookmarks", {"title": "Ex", "url": "https://example.com/"})[1]
        # CSRF and Origin are enforced.
        self.assertEqual(self.req("/api/links/delete", {"id": added["id"]}, csrf=False)[0], 403)
        self.assertEqual(self.req("/api/links/delete", {"id": added["id"]}, origin=False)[0], 403)
        self.assertEqual(self.req("/api/bookmarks/delete", {"id": mark["id"]}, csrf=False)[0], 403)
        self.assertEqual(len(self.req("/api/links")[1]["items"]), 2)
        # Rows that left the "staged" state are never deleted.
        self.store.begin_send(sent["id"])
        self.assertEqual(self.req("/api/links/delete", {"id": sent["id"]})[0], 400)
        self.store.finish_send(sent["id"], "sent")
        self.assertEqual(self.req("/api/links/delete", {"id": sent["id"]})[0], 400)
        # Invalid ids are refused.
        for bad in (0, -1, "1", True, None, 999):
            self.assertEqual(self.req("/api/links/delete", {"id": bad})[0], 400, bad)
            self.assertEqual(self.req("/api/bookmarks/delete", {"id": bad})[0], 400, bad)
        before = self.req("/api/health")[1]["settings_revision"]
        self.assertEqual(self.req("/api/links/delete", {"id": added["id"]})[0], 200)
        self.assertGreater(self.req("/api/health")[1]["settings_revision"], before)
        self.assertEqual([i["id"] for i in self.req("/api/links")[1]["items"]], [sent["id"]])
        self.assertEqual(self.req("/api/bookmarks/delete", {"id": mark["id"]})[0], 200)
        self.assertEqual(self.req("/api/bookmarks")[1]["items"], [])
        self.assertEqual(self.req("/api/bookmarks/delete", {"id": mark["id"]})[0], 400)

    def test_low_io_diagnostic_is_login_protected_and_preflight_is_gone(self):
        self.assertEqual(self.req("/api/diagnostics/io")[0], 401)
        self.login()
        status, data = self.req("/api/diagnostics/io")
        self.assertEqual(status, 200)
        self.assertIn("process_io", data)
        self.assertIn("events", data)
        self.assertTrue(data["local_only"])
        self.assertFalse(data["log_persistence"])
        self.assertEqual(self.req("/api/jd2/local-preflight")[0], 404)

    # ---- v0.1.33: unified state, caches ------------------------------------------
    def test_state_requires_login(self):
        self.assertEqual(self.req("/api/state")[0], 401)

    def test_state_shape_and_first_load_bundle(self):
        self.login()
        code, data = self.req("/api/state?full=1")
        self.assertEqual(code, 200)
        self.assertEqual(data["version"], webapp.VERSION)
        for key in ("rev", "engine", "downloads", "rb", "settings_rev", "https", "tailnet", "settings", "bookmarks"):
            self.assertIn(key, data)
        for key in ("state", "mode", "running", "can_send", "preset_applied"):
            self.assertIn(key, data["engine"])
        self.assertFalse(data["engine"]["running"])
        self.assertIsNone(data["downloads"])                      # engine not running
        for key in ("state", "tabs", "mem"):
            self.assertIn(key, data["rb"])
        # no bundle unless asked for
        self.assertNotIn("settings", self.req("/api/state")[1])
        self.assertEqual(self.req("/api/state?rev=x")[0], 400)
        self.assertEqual(self.req("/api/state?rev=1&wait=26")[0], 400)

    def test_long_poll_returns_immediately_on_a_different_rev_and_waits_when_unchanged(self):
        self.login()
        rev = self.req("/api/state")[1]["rev"]
        started = time.monotonic()
        code, same = self.req("/api/state?rev=%d&wait=1" % rev)
        self.assertEqual((code, same["rev"]), (200, rev))
        self.assertGreaterEqual(time.monotonic() - started, 0.9)   # held until the wait ran out
        started = time.monotonic()
        self.assertEqual(self.req("/api/state?rev=%d&wait=3" % (rev + 99))[1]["rev"], rev)
        self.assertLess(time.monotonic() - started, 1.0)

    def test_rev_bumps_when_a_write_changes_the_state_and_wakes_the_poller(self):
        self.login()
        rev = self.req("/api/state")[1]["rev"]
        result = {}

        def poll():
            result["data"] = self.req("/api/state?rev=%d&wait=3" % rev)[1]

        t = threading.Thread(target=poll)
        t.start()
        time.sleep(0.3)
        self.assertEqual(self.req("/api/bookmarks", {"title": "x", "url": "https://example.com/"})[0], 201)
        t.join(5)
        self.assertGreater(result["data"]["rev"], rev)
        self.assertGreater(result["data"]["settings_rev"], 0)

    def test_sources_are_cached_between_requests(self):
        self.login()
        engine = self.server.engine
        real = engine.status
        with patch.object(engine, "status", side_effect=real) as spy:
            for _ in range(3):
                self.req("/api/state")
                self.req("/api/engine/status")
        self.assertEqual(spy.call_count, 1)                       # engine status cache (2 s)
        with patch.object(self.server.rb, "status", side_effect=self.server.rb.status) as rb_spy:
            for _ in range(3):
                self.req("/api/rb/status")
                self.req("/api/state")
        self.assertLessEqual(rb_spy.call_count, 1)

    def test_a_write_drops_the_cached_reads(self):
        self.login()
        engine = self.server.engine
        with patch.object(engine, "status", side_effect=engine.status) as spy:
            self.req("/api/engine/status")
            self.req("/api/bookmarks", {"title": "x", "url": "https://example.com/"})
            self.req("/api/engine/status")
        self.assertEqual(spy.call_count, 2)

    def test_download_counts_follow_the_list_rows(self):
        rows = {"packages": [{"running": True}, {"finished": True}, {}], "links": []}
        self.assertEqual(state_hub.download_counts(rows), {"running": 1, "waiting": 1, "finished": 1})
        self.assertEqual(state_hub.download_counts({"packages": [], "links": [{"running": True}]}),
                         {"running": 1, "waiting": 0, "finished": 0})

    def test_removed_endpoints(self):
        self.login()
        self.assertEqual(self.req("/api/jd2/local-preflight")[0], 404)
        self.assertEqual(self.req("/api/rb/frame?tab=b1&since=0&wait=0")[0], 404)

    def test_update_apply_fails_closed_when_not_qpkg(self):
        self.login()
        info = self.req("/api/update/status")[1]
        self.assertFalse(info["supported"])
        code, _ = self.req("/api/update/apply", {"version": "v0.1.8"})
        self.assertEqual(code, 400)
        self.assertEqual(self.req("/api/health")[1]["version"], "0.1.67")
        self.assertFalse(self.req("/api/jd2/status")[1]["connected"])
        self.assertEqual(self.req("/api/jd2/update/apply", {"confirmed": True})[0], 400)
        self.assertEqual(self.req("/api/jd2/preset/apply", {"confirmed": True})[0], 400)


    # ---- v0.1.24: send-and-start, site rules, probe, quality ------------------
    V24_GET = ("/api/rb/site-rules", "/api/jd2/linkgrabber")
    V24_POST = (("/api/rb/site-rules", {"host": "example.com", "auto_send": True}),
                ("/api/rb/site-rules/delete", {"host": "example.com"}),
                ("/api/rb/probe", {"tab": "b1", "x": 1, "y": 1}),
                ("/api/rb/probe/send", {"tab": "b1", "probe_id": "x"}),
                ("/api/links/clear", {"include_staged": False}),
                ("/api/jd2/downloads/cleanup", {"confirmed": True}),
                ("/api/settings/options", {"send_autostart": False}),
                ("/api/jd2/linkgrabber/start", {"confirmed": True}),
                ("/api/jd2/downloads/start", {}),
                ("/api/jd2/downloads/stop", {"confirmed": True}))

    def test_v0124_endpoints_require_login_csrf_and_origin(self):
        for path in self.V24_GET:
            with self.subTest(path=path):
                self.assertEqual(self.req(path)[0], 401)
        for path, body in self.V24_POST:
            with self.subTest(path=path):
                self.assertIn(self.req(path, body)[0], (401, 403))
        self.login()
        for path, body in self.V24_POST:
            with self.subTest(path=path, case="no csrf"):
                self.assertEqual(self.req(path, body, csrf=False)[0], 403)
            with self.subTest(path=path, case="no origin"):
                self.assertEqual(self.req(path, body, origin=False)[0], 403)
        self.assertEqual(self.store.site_rules(), [])
        self.assertIs(self.store.settings()["send_autostart"], True)

    def test_site_rules_api_validates_and_persists(self):
        self.login()
        code, body = self.req("/api/rb/site-rules")
        self.assertEqual((code, body["items"], body["block_ads"], body["max"]), (200, [], True, 200))
        for bad in ({"host": "localhost"}, {"host": "http://example.com"}, {"host": "192.168.0.1"},
                    {"host": "example.com/x"}, {"host": ""}, {"host": 5}, {},
                    {"host": "example.com", "popup": "maybe"}, {"host": "example.com", "auto_send": "yes"},
                    {"host": "example.com", "block_ads": 1}):
            with self.subTest(bad=bad):
                self.assertEqual(self.req("/api/rb/site-rules", bad)[0], 400)
        self.assertEqual(self.req("/api/rb/site-rules")[1]["items"], [])
        code, rule = self.req("/api/rb/site-rules", {"host": "Example.com", "auto_send": True})
        self.assertEqual((code, rule), (200, {"host": "example.com", "auto_send": True, "popup": "ask",
                                              "block_ads": True}))
        self.req("/api/rb/site-rules", {"host": "example.com", "popup": "block", "block_ads": False})
        items = self.req("/api/rb/site-rules")[1]["items"]
        self.assertEqual(items, [{"host": "example.com", "auto_send": True, "popup": "block", "block_ads": False}])
        self.assertEqual(self.req("/api/rb/site-rules/delete", {"host": "nope.example.com"})[0], 400)
        self.assertEqual(self.req("/api/rb/site-rules/delete", {"host": "example.com"})[1], {"deleted": True})
        self.assertEqual(self.req("/api/rb/site-rules")[1]["items"], [])

    def test_site_rules_api_enforces_the_200_cap(self):
        self.login()
        for i in range(200):
            self.store.set_site_rule("h%d.example.com" % i, auto_send=True)
        code, body = self.req("/api/rb/site-rules", {"host": "extra.example.com", "auto_send": True})
        self.assertEqual(code, 400)
        self.assertIn("200", body["error"])

    def test_auto_clear_options_default_off_and_round_trip(self):
        self.login()
        settings = self.req("/api/settings")[1]
        self.assertEqual((settings["auto_clear_links"], settings["auto_clear_downloads"]), (False, False))
        code, saved = self.req("/api/settings/options", {"auto_clear_links": True, "auto_clear_downloads": True})
        self.assertEqual((code, saved["auto_clear_links"], saved["auto_clear_downloads"]), (200, True, True))
        self.assertIs(storage.Store(self.root).settings()["auto_clear_downloads"], True)
        self.assertEqual(self.req("/api/settings/options", {"auto_clear_links": "yes"})[0], 400)
        self.assertIs(self.req("/api/settings/options", {"auto_clear_links": False})[1]["auto_clear_links"], False)

    def test_settings_options_toggle_persists_and_rejects_everything_else(self):
        self.login()
        settings = self.req("/api/settings")[1]
        self.assertEqual((settings["send_autostart"], settings["block_ads"]), (True, True))
        code, saved = self.req("/api/settings/options", {"send_autostart": False})
        self.assertEqual((code, saved["send_autostart"], saved["block_ads"]), (200, False, True))
        self.assertIs(self.req("/api/settings")[1]["send_autostart"], False)
        self.assertEqual(self.req("/api/settings/options", {"block_ads": False})[0], 200)
        reopened = storage.Store(self.root)
        try:
            self.assertEqual((reopened.settings()["send_autostart"], reopened.settings()["block_ads"]),
                             (False, False))
        finally:
            reopened.close()
        for bad in ({}, {"send_autostart": "no"}, {"send_autostart": 0}, {"download_dir": True},
                    {"send_autostart": True, "auto_delete": True}, {"nope": True}):
            with self.subTest(bad=bad):
                self.assertEqual(self.req("/api/settings/options", bad)[0], 400)
        self.assertEqual(self.req("/api/settings")[1]["auto_delete"], False)

    def jd2_ready(self, can_send=True):
        adapter = unittest.mock.MagicMock()
        adapter.can_send = can_send
        return adapter

    def test_manual_send_uses_autostart_by_default_and_not_when_the_setting_is_off(self):
        self.login()
        self.assertEqual(self.req("/api/settings/folder", {"path": str(self.allowed)})[0], 200)
        adapter = self.jd2_ready()
        with patch.object(self.server.jd2, "_require", return_value=adapter), \
                patch("jd2_transfer.prepare_transfer"), \
                patch("jd2_transfer.transfer", return_value={"state": "sent"}) as transfer:
            first = self.req("/api/links", {"url": "https://rapidgator.net/file/111"})[1]["id"]
            self.assertEqual(self.req("/api/links/send", {"id": first})[0], 200)
            transfer.assert_called_with(adapter, "https://rapidgator.net/file/111",
                                        str(self.allowed.resolve()), autostart=True)
            self.req("/api/settings/options", {"send_autostart": False})
            second = self.req("/api/links", {"url": "https://rapidgator.net/file/222"})[1]["id"]
            self.assertEqual(self.req("/api/links/send", {"id": second})[0], 200)
            transfer.assert_called_with(adapter, "https://rapidgator.net/file/222", str(self.allowed.resolve()))

    def open_tab_on(self, url="https://example.com/a"):
        self.assertEqual(self.req("/api/rb/tabs", {"url": url})[0], 201)

    def wait_items(self, since=0, count=1):
        items = []
        for _ in range(300):
            items = self.req("/api/rb/intercepts?tab=b1&since=%d" % since)[1]["items"]
            if len(items) >= count:
                break
            time.sleep(0.01)
        return items

    def hoster_paused(self, request_id="r1", url="https://rapidgator.net/file/0123456789abcdef0123456789abcdef"):
        self.rb_ws.emit("Fetch.requestPaused", {"requestId": request_id, "resourceType": "Document",
                                                "request": {"url": url}}, "SESSION1")

    def test_auto_send_rule_sends_through_the_existing_path_and_dedupes(self):
        self.login()
        webapp.wire_remote_browser(self.server)
        self.assertEqual(self.req("/api/settings/folder", {"path": str(self.allowed)})[0], 200)
        self.req("/api/rb/site-rules", {"host": "example.com", "auto_send": True})
        self.open_tab_on("https://www.example.com/a")
        adapter = self.jd2_ready()
        hoster = "https://rapidgator.net/file/0123456789abcdef0123456789abcdef"
        with patch.object(self.server.jd2, "_require", return_value=adapter), \
                patch("jd2_transfer.prepare_transfer") as prepare, \
                patch("jd2_transfer.transfer", return_value={"state": "sent"}) as transfer:
            self.hoster_paused()
            items = self.wait_items()
            self.assertEqual(items, [{"id": 1, "kind": "auto_sent", "url": hoster, "dup": False}])
            transfer.assert_called_once_with(adapter, hoster, str(self.allowed.resolve()), autostart=True)
            prepare.assert_called_with(adapter, str(self.allowed.resolve()))
            self.assertEqual([(l["url"], l["state"]) for l in self.req("/api/links")[1]["items"]],
                             [(hoster, "sent")])
            # the same link again (a different file name in the URL) is never sent twice
            self.hoster_paused("r2", hoster + "/again.rar.html")
            time.sleep(0.3)
            self.assertEqual(transfer.call_count, 1)
            self.assertEqual(len(self.req("/api/links")[1]["items"]), 1)

    def test_auto_send_falls_back_to_the_sheet_when_the_engine_cannot_send(self):
        self.login()
        webapp.wire_remote_browser(self.server)
        self.req("/api/settings/folder", {"path": str(self.allowed)})
        self.req("/api/rb/site-rules", {"host": "example.com", "auto_send": True})
        self.open_tab_on()
        with patch.object(self.server.jd2, "_require", return_value=self.jd2_ready(False)), \
                patch("jd2_transfer.transfer") as transfer:
            self.hoster_paused()
            items = self.wait_items()
        self.assertEqual([i["kind"] for i in items], ["hoster"])
        transfer.assert_not_called()
        self.assertEqual(self.req("/api/links")[1]["items"], [])

    def test_auto_send_without_a_rule_or_with_the_rule_off_shows_the_sheet(self):
        self.login()
        webapp.wire_remote_browser(self.server)
        self.req("/api/settings/folder", {"path": str(self.allowed)})
        self.req("/api/rb/site-rules", {"host": "example.com", "auto_send": False, "popup": "block"})
        self.open_tab_on()
        with patch.object(self.server.jd2, "_require", return_value=self.jd2_ready()), \
                patch("jd2_transfer.transfer") as transfer:
            self.hoster_paused()
            items = self.wait_items()
        self.assertEqual([i["kind"] for i in items], ["hoster"])
        transfer.assert_not_called()

    def test_auto_send_failure_is_reported_and_leaves_the_sheet(self):
        self.login()
        webapp.wire_remote_browser(self.server)
        self.req("/api/rb/site-rules", {"host": "example.com", "auto_send": True})
        self.open_tab_on()        # no download folder chosen: the send path refuses
        with patch.object(self.server.jd2, "_require", return_value=self.jd2_ready()):
            self.hoster_paused()
            items = self.wait_items()
        self.assertEqual([i["kind"] for i in items], ["hoster"])
        code, headers, _ = self.get_raw("/api/rb/frame.jpg?tab=b1&since=0&wait=0")
        self.assertIn("%E8%87%AA%E5%8B%95%E9%80%81%E4%BF%A1", headers["X-Frame-Blocked"])    # 自動送信

    def test_popup_block_rule_counts_in_the_frame_header(self):
        self.login()
        webapp.wire_remote_browser(self.server)
        self.req("/api/rb/site-rules", {"host": "example.com", "popup": "block"})
        self.open_tab_on()
        self.rb_ws.emit("Target.targetCreated", {"targetInfo": {
            "targetId": "POPUP000001", "type": "page", "openerId": "TARGET00001", "url": "https://shop.example/p"}})
        for _ in range(300):
            code, headers, _ = self.get_raw("/api/rb/frame.jpg?tab=b1&since=0&wait=0")
            if headers["X-Frame-Popups"] == "1":
                break
            time.sleep(0.01)
        self.assertEqual(headers["X-Frame-Popups"], "1")
        self.assertEqual(self.req("/api/rb/intercepts?tab=b1&since=0")[1]["items"], [])

    def test_ad_blocking_follows_the_hub_setting_and_the_site_rule(self):
        self.login()
        webapp.wire_remote_browser(self.server)
        self.open_tab_on("https://example.com/a")
        self.assertTrue([m for m in self.rb_ws.sent if m["method"] == "Network.setBlockedURLs"])
        self.req("/api/rb/tabs/close", {"id": "b1"})
        self.req("/api/settings/options", {"block_ads": False})
        count = len([m for m in self.rb_ws.sent if m["method"] == "Network.setBlockedURLs"])
        self.assertEqual(self.req("/api/rb/tabs", {"url": "https://example.com/a"})[0], 201)
        self.assertEqual(len([m for m in self.rb_ws.sent if m["method"] == "Network.setBlockedURLs"]), count)

    def test_probe_endpoint_returns_links_and_closes_its_target(self):
        self.login()
        webapp.wire_remote_browser(self.server)
        self.open_tab_on()
        hoster = "https://rapidgator.net/file/0123456789abcdef0123456789abcdef"
        self.rb_ws.eval_map = [("elementFromPoint", "https://blog.example.org/go?id=7")]

        def hook(message):
            if message["method"] == "Page.navigate" and message.get("sessionId") == "SESSION2":
                self.rb_ws.emit("Fetch.requestPaused", {"requestId": "rp", "resourceType": "Document",
                                                        "request": {"url": hoster}}, "SESSION2")
        self.rb_ws.hook = hook
        code, body = self.req("/api/rb/probe", {"tab": "b1", "x": 40, "y": 80, "label": "第1巻"})
        # v0.1.65: a long press only queues a job and answers at once
        self.assertEqual((code, body["state"], body["position"], body["label"], body["existing"]),
                         (202, "queued", 1, "第1巻", False))
        self.assertTrue(self.server.rb.probes.wait_idle(10))
        code, listing = self.req("/api/rb/probe/jobs?tab=b1")
        job = listing["jobs"][0]
        self.assertEqual((code, job["id"], job["state"], job["links"], job["start_url"] if "start_url" in job else None),
                         (200, body["job"], "done", [hoster], None))
        self.assertEqual((job["href"], job["final_url"], job["redirects"]), ("https://blog.example.org/go?id=7", hoster, 0))
        self.assertEqual(listing["summary"]["done"], 1)
        closed = [m["params"]["targetId"] for m in self.rb_ws.sent if m["method"] == "Target.closeTarget"]
        self.assertEqual(closed, ["TARGET00002"])
        for bad in ({"tab": "b1", "x": -1, "y": 0}, {"tab": "b1", "x": "a", "y": 0}, {"tab": "b1"},
                    {"tab": "../x", "x": 1, "y": 1}, {"x": 1, "y": 1}):
            with self.subTest(bad=bad):
                self.assertEqual(self.req("/api/rb/probe", bad)[0], 400)
        self.assertEqual(self.req("/api/rb/probe", {"tab": "b9", "x": 1, "y": 1})[0], 404)

    # ---- v0.1.65: the analysis queue over HTTP -------------------------------------------------
    def queue_setup(self):
        self.login()
        webapp.wire_remote_browser(self.server)
        self.open_tab_on()
        hoster = self.HOSTER_A

        def hook(message):
            if message["method"] == "Page.navigate" and message.get("sessionId") != "SESSION1":
                self.rb_ws.emit("Fetch.requestPaused", {"requestId": "rp", "resourceType": "Document",
                                                        "request": {"url": hoster}}, message["sessionId"])
        self.rb_ws.hook = hook

    def test_twelve_presses_are_twelve_quick_answers_and_every_job_finishes(self):
        self.queue_setup()
        started = time.time()
        answers = [self.req("/api/rb/probe", {"tab": "b1", "href": "https://blog.example.org/go/%d" % n}) for n in range(12)]
        self.assertTrue(all(code == 202 for code, _ in answers))
        self.assertLess(time.time() - started, 3.0)
        self.assertEqual([a["job"] for _, a in answers], [j["id"] for j in self.server.rb.probe_jobs("b1")["jobs"]])
        self.assertTrue(self.server.rb.probes.wait_idle(20))
        code, listing = self.req("/api/rb/probe/jobs?tab=b1")
        self.assertEqual([j["state"] for j in listing["jobs"]], ["done"] * 12)
        self.assertEqual(listing["summary"], dict(listing["summary"], done=12, failed=0, queued=0, running=0, total=12, review=12))

    def test_meta_has_the_probe_summary_and_accepts_pr(self):
        self.queue_setup()
        self.req("/api/rb/probe", {"tab": "b1", "href": "https://blog.example.org/go/1"})
        self.assertTrue(self.server.rb.probes.wait_idle(10))
        code, meta = self.req("/api/rb/meta?tab=b1&wait=0")
        self.assertEqual((code, meta["probes"]["done"], meta["probes"]["total"]), (200, 1, 1))
        code, meta2 = self.req("/api/rb/meta?tab=b1&wait=0&rev=%d&pr=%d" % (meta["rev"], meta["probes"]["rev"]))
        self.assertEqual(code, 200)
        self.assertEqual(self.req("/api/rb/meta?tab=b1&wait=0&pr=x")[0], 400)

    def test_cancel_retry_dismiss_endpoints(self):
        self.queue_setup()
        self.rb_ws.fail["Fetch.enable"] = "Target closed"
        self.req("/api/rb/probe", {"tab": "b1", "href": "https://blog.example.org/go/1"})
        self.assertTrue(self.server.rb.probes.wait_idle(10))
        code, listing = self.req("/api/rb/probe/jobs?tab=b1")
        self.assertEqual((listing["jobs"][0]["state"], "Target closed" in listing["jobs"][0]["error"]), ("failed", True))
        del self.rb_ws.fail["Fetch.enable"]
        self.assertEqual(self.req("/api/rb/probe/retry", {"tab": "b1"}), (200, {"retried": 1}))
        self.assertTrue(self.server.rb.probes.wait_idle(10))
        self.assertEqual(self.req("/api/rb/probe/jobs?tab=b1")[1]["jobs"][0]["state"], "done")
        self.assertEqual(self.req("/api/rb/probe/cancel", {"tab": "b1"}), (200, {"cancelled": 0}))
        self.assertEqual(self.req("/api/rb/probe/dismiss", {"tab": "b1"}), (200, {"dismissed": 1}))
        self.assertEqual(self.req("/api/rb/probe/jobs?tab=b1")[1]["jobs"], [])
        for path in ("/api/rb/probe/cancel", "/api/rb/probe/retry", "/api/rb/probe/dismiss"):
            self.assertEqual(self.req(path, {"tab": "b9"})[0], 404)
            self.assertEqual(self.req(path, {"tab": "b1"}, csrf=False)[0], 403)
        self.assertEqual(self.req("/api/rb/probe/jobs?tab=b9")[0], 404)

    def test_probe_endpoints_need_a_login(self):
        self.assertEqual(self.req("/api/rb/probe/jobs?tab=b1")[0], 401)
        for path in ("/api/rb/probe/cancel", "/api/rb/probe/retry", "/api/rb/probe/dismiss"):
            self.assertEqual(self.req(path, {"tab": "b1"})[0], 401)

    def test_finished_jobs_are_sent_to_jd2_by_the_hub_when_the_option_is_on(self):
        self.queue_setup()
        self.req("/api/settings/folder", {"path": str(self.allowed)})
        with patch.object(self.server.jd2, "_require", return_value=self.jd2_ready()), \
                patch("jd2_transfer.prepare_transfer"), \
                patch("jd2_transfer.transfer", return_value={"state": "sent"}) as transfer:
            self.req("/api/rb/probe", {"tab": "b1", "href": "https://blog.example.org/go/1"})
            self.assertTrue(self.server.rb.probes.wait_idle(10))
            job = self.req("/api/rb/probe/jobs?tab=b1")[1]["jobs"][0]
            self.assertEqual((job["state"], job["sent"]["sent"], job["sent"]["error"]), ("done", 1, None))
            self.assertEqual([c.args[1] for c in transfer.call_args_list], [self.HOSTER_A])
            self.assertEqual(self.req("/api/rb/probe/jobs?tab=b1")[1]["summary"]["review"], 0)
            # the option off: the result waits for the user, who sends it from the list (by job id, never by URL)
            self.req("/api/settings/options", {"probe_autosend": False})
            self.req("/api/rb/probe", {"tab": "b1", "href": "https://blog.example.org/go/2"})
            self.assertTrue(self.server.rb.probes.wait_idle(10))
            second = [j for j in self.req("/api/rb/probe/jobs?tab=b1")[1]["jobs"] if j["href"].endswith("/2")][0]
            self.assertIsNone(second["sent"])
            self.assertEqual(transfer.call_count, 1)
            code, body = self.req("/api/rb/probe/send", {"tab": "b1", "job": second["id"], "links": ["https://evil.example/x"]})
            self.assertEqual((code, body["sent"], body["skipped"]), (200, 0, 1))      # the same hoster link: already sent
            self.assertEqual(self.req("/api/rb/probe/send", {"tab": "b1", "job": "nope"})[0], 400)
            self.assertEqual(self.req("/api/rb/probe/send", {"tab": "b2", "job": second["id"]})[0], 400)

    def test_autosend_does_nothing_when_jd2_cannot_send(self):
        self.queue_setup()
        with patch.object(self.server.jd2, "_require", return_value=self.jd2_ready(False)), \
                patch("jd2_transfer.transfer") as transfer:
            self.req("/api/rb/probe", {"tab": "b1", "href": "https://blog.example.org/go/1"})
            self.assertTrue(self.server.rb.probes.wait_idle(10))
            transfer.assert_not_called()
        job = self.req("/api/rb/probe/jobs?tab=b1")[1]["jobs"][0]
        self.assertEqual((job["state"], job["sent"]), ("done", None))

    def test_autosend_that_cannot_happen_keeps_its_reason_on_the_job(self):
        # v0.1.66: no silent failure: after the retries the reason is on the job (the list shows it)
        self.queue_setup()
        with patch.object(webapp, "PROBE_AUTOSEND_RETRIES", 0), \
                patch.object(self.server.jd2, "_require", return_value=self.jd2_ready(False)), \
                patch("jd2_transfer.transfer") as transfer:
            self.req("/api/rb/probe", {"tab": "b1", "href": "https://blog.example.org/go/1"})
            self.assertTrue(self.server.rb.probes.wait_idle(10))
            transfer.assert_not_called()
        job = self.req("/api/rb/probe/jobs?tab=b1")[1]["jobs"][0]
        self.assertEqual(job["state"], "done")
        self.assertEqual(job["sent"]["sent"], 0)
        self.assertIn("自動送信できませんでした", job["sent"]["error"])

    def test_a_rapidgator_press_is_still_answered_with_the_result_itself(self):
        self.login()
        self.open_tab_on()
        code, body = self.req("/api/rb/probe", {"tab": "b1", "href": self.HOSTER_A + "/x.rar.html"})
        self.assertEqual((code, body["links"], body["redirects"]), (200, [self.HOSTER_A], 0))
        self.assertTrue(body["probe_id"])

    def test_the_tab_limit_is_a_setting(self):
        self.login()
        webapp.wire_remote_browser(self.server)
        self.assertEqual(self.req("/api/settings")[1]["rb_max_tabs"], 3)
        for bad in (0, 21, "5", True, 2.5, None):
            with self.subTest(value=bad):
                self.assertEqual(self.req("/api/settings/options", {"rb_max_tabs": bad})[0], 400)
        self.assertEqual(self.req("/api/settings/options", {"rb_max_tabs": 5})[1]["rb_max_tabs"], 5)
        self.assertEqual(self.req("/api/rb/status")[1]["max_tabs"], 5)         # the new limit counts at once
        for n in range(5):
            self.assertEqual(self.req("/api/rb/tabs", {"url": "https://example.com/%d" % n})[0], 201)
        code, body = self.req("/api/rb/tabs", {"url": "https://example.com/6"})
        self.assertEqual(code, 400)
        self.assertEqual(body["error"], "NASブラウザのタブは最大5枚です（設定で変更できます）。不要なタブを閉じてください")

    def test_the_self_test_recommendation_is_in_the_browser_status(self):
        self.login()
        self.assertIsNone(self.server.rb.status()["tab_reco"])
        self.server.rb.tab_reco = {"tabs": 7, "avg_mib": 80.0, "max_mib": 120.0}
        self.assertEqual(self.req("/api/rb/status")[1]["tab_reco"]["tabs"], 7)

    HOSTER_A = "https://rapidgator.net/file/0123456789abcdef0123456789abcdef"
    HOSTER_B = "https://rapidgator.net/file/fedcba9876543210fedcba9876543210"

    def stored_probe(self, links, now=None):
        return webapp.remember_probe(self.server, "b1", links, now)

    def test_probe_send_uses_the_stored_result_only_and_dedupes(self):
        self.login()
        self.req("/api/settings/folder", {"path": str(self.allowed)})
        pid = self.stored_probe([self.HOSTER_A, self.HOSTER_B])
        adapter = self.jd2_ready()
        evil = "https://evil.example/x"
        with patch.object(self.server.jd2, "_require", return_value=adapter), \
                patch("jd2_transfer.prepare_transfer"), \
                patch("jd2_transfer.transfer", return_value={"state": "sent"}) as transfer:
            self.assertEqual(self.req("/api/rb/probe/send", {"tab": "b1", "probe_id": "wrong"})[0], 400)
            self.assertEqual(self.req("/api/rb/probe/send", {"tab": "b2", "probe_id": pid})[0], 400)
            transfer.assert_not_called()
            code, body = self.req("/api/rb/probe/send", {"tab": "b1", "probe_id": pid,
                                                         "links": [evil], "url": evil})
            self.assertEqual((code, body["sent"], body["skipped"], body["error"]), (200, 2, 0, None))
            self.assertEqual([c.args[1] for c in transfer.call_args_list], [self.HOSTER_A, self.HOSTER_B])
            self.assertTrue(all(c.kwargs == {"autostart": True} for c in transfer.call_args_list))
            code, body = self.req("/api/rb/probe/send", {"tab": "b1", "probe_id": pid})
            self.assertEqual((code, body["sent"], body["skipped"]), (200, 0, 2))
            self.assertEqual(transfer.call_count, 2)
        self.assertNotIn(evil, [l["url"] for l in self.req("/api/links")[1]["items"]])

    def test_probe_send_respects_autostart_off_and_engine_state(self):
        self.login()
        self.req("/api/settings/folder", {"path": str(self.allowed)})
        self.req("/api/settings/options", {"send_autostart": False})
        pid = self.stored_probe([self.HOSTER_A])
        with patch.object(self.server.jd2, "_require", return_value=self.jd2_ready(False)), \
                patch("jd2_transfer.transfer") as transfer:
            code, body = self.req("/api/rb/probe/send", {"tab": "b1", "probe_id": pid})
            self.assertEqual((code, body["sent"]), (200, 0))
            self.assertIn("JD2", body["error"])
            transfer.assert_not_called()
        with patch.object(self.server.jd2, "_require", return_value=self.jd2_ready()), \
                patch("jd2_transfer.prepare_transfer"), \
                patch("jd2_transfer.transfer", return_value={"state": "sent"}) as transfer:
            code, body = self.req("/api/rb/probe/send", {"tab": "b1", "probe_id": pid})
            self.assertEqual((body["sent"], body["autostart"]), (1, False))
            self.assertEqual(transfer.call_args.args[1:], (self.HOSTER_A, str(self.allowed.resolve())))
            self.assertEqual(transfer.call_args.kwargs, {})

    def test_probe_results_are_stored_per_probe_id_expire_and_are_capped(self):
        self.login()
        now = time.time()
        pid = self.stored_probe([self.HOSTER_A], now - webapp.PROBE_TTL - 1)
        self.assertEqual(self.req("/api/rb/probe/send", {"tab": "b1", "probe_id": pid})[0], 400)
        many = ["https://rapidgator.net/file/%032x" % i for i in range(80)]
        pid = self.stored_probe(many)
        self.assertEqual(len(self.server.probe_results["b1"][pid]["links"]), webapp.PROBE_MAX_LINKS)
        self.assertIsNone(self.stored_probe([]))
        # an empty result does not discard the earlier probe of the tab
        self.assertIn(pid, self.server.probe_results["b1"])
        ids = [self.stored_probe([self.HOSTER_A]) for _ in range(webapp.PROBE_KEEP_PER_TAB + 5)]
        self.assertEqual(len(self.server.probe_results["b1"]), webapp.PROBE_KEEP_PER_TAB)
        self.assertEqual(list(self.server.probe_results["b1"]), ids[-webapp.PROBE_KEEP_PER_TAB:])
        self.assertEqual(self.req("/api/rb/probe/send", {"tab": "b1", "probe_id": pid})[0], 400)

    def test_probe_send_with_mixed_ids_sends_only_the_named_probe(self):
        self.login()
        self.req("/api/settings/folder", {"path": str(self.allowed)})
        first = self.stored_probe([self.HOSTER_A])
        second = self.stored_probe([self.HOSTER_B])
        other_tab = webapp.remember_probe(self.server, "b2", [self.HOSTER_A])
        with patch.object(self.server.jd2, "_require", return_value=self.jd2_ready()), \
                patch("jd2_transfer.prepare_transfer"), \
                patch("jd2_transfer.transfer", return_value={"state": "sent"}) as transfer:
            code, body = self.req("/api/rb/probe/send", {"tab": "b1", "probe_id": second})
            self.assertEqual((code, body["sent"], body["total"]), (200, 1, 1))
            self.assertEqual([c.args[1] for c in transfer.call_args_list], [self.HOSTER_B])
            self.assertEqual(self.req("/api/rb/probe/send", {"tab": "b1", "probe_id": other_tab})[0], 400)
            self.assertEqual(self.req("/api/rb/probe/send", {"tab": "b3", "probe_id": first})[0], 400)
            code, body = self.req("/api/rb/probe/send", {"tab": "b1", "probe_id": first})
            self.assertEqual((code, body["sent"]), (200, 1))
            self.assertEqual([c.args[1] for c in transfer.call_args_list], [self.HOSTER_B, self.HOSTER_A])

    def test_probe_queue_overflow_is_http_429(self):
        # v0.1.65: only when all 200 slots still wait or run (a held-open request can no longer pile up)
        self.queue_setup()
        gate = threading.Event()
        inner = self.rb_ws.hook
        self.rb_ws.hook = lambda message: (gate.wait(10) if message["method"] == "Page.navigate" and message.get("sessionId") != "SESSION1" else None, inner(message))
        with patch("rb_probe_queue.QUEUE_CAP", 5):
            for n in range(5):
                self.assertEqual(self.req("/api/rb/probe", {"tab": "b1", "href": "https://blog.example.org/go/%d" % n})[0], 202)
            code, body = self.req("/api/rb/probe", {"tab": "b1", "href": "https://blog.example.org/go/99"})
        self.assertEqual(code, 429)
        self.assertIn("解析待ちが多すぎます", body["error"])
        gate.set()
        self.assertTrue(self.server.rb.probes.wait_idle(20))

    def test_links_clear_endpoint_keeps_staged_by_default(self):
        self.login()
        a = self.req("/api/links", {"url": "https://example.com/a"})[1]["id"]
        self.req("/api/links", {"url": "https://example.com/b"})
        self.server.store.begin_send(a)
        self.server.store.finish_send(a, "sent")
        self.assertEqual(self.req("/api/links/clear", {"include_staged": "yes"})[0], 400)
        code, body = self.req("/api/links/clear", {})
        self.assertEqual((code, body), (200, {"removed": 1}))
        self.assertEqual([l["state"] for l in self.req("/api/links")[1]["items"]], ["staged"])
        self.assertEqual(self.req("/api/links/clear", {"include_staged": True})[1], {"removed": 1})
        self.assertEqual(self.req("/api/links")[1]["items"], [])

    def test_downloads_cleanup_needs_confirmation_and_calls_the_adapter(self):
        self.login()
        adapter = self.jd2_ready()
        with patch.object(self.server.jd2, "_require", return_value=adapter):
            self.assertEqual(self.req("/api/jd2/downloads/cleanup", {})[0], 400)
            adapter.cleanup_finished.assert_not_called()
            adapter.remove_skipped_existing.return_value = 2
            self.assertEqual(self.req("/api/jd2/downloads/cleanup", {"confirmed": True}),
                             (200, {"cleared": True, "skipped_removed": 2}))
            adapter.cleanup_finished.assert_called_once_with()
            adapter.remove_skipped_existing.assert_called_once_with()

    ITEM_POST = (("/api/jd2/items/start", {"kind": "package", "id": "1"}),
                 ("/api/jd2/items/stop", {"kind": "package", "id": "1"}),
                 ("/api/jd2/items/remove", {"kind": "package", "id": "1", "delete_files": False}),
                 ("/api/jd2/waiting/remove", {"ids": ["1"]}),
                 ("/api/jd2/waiting/start", {"ids": ["1"]}))

    def test_item_endpoints_need_login_csrf_and_origin(self):
        for path, body in self.ITEM_POST:
            with self.subTest(path=path):
                self.assertEqual(self.req(path, body)[0], 401)
        self.login()
        for path, body in self.ITEM_POST:
            with self.subTest(path=path):
                self.assertEqual(self.req(path, body, csrf=False)[0], 403)
                self.assertEqual(self.req(path, body, origin=False)[0], 403)
                self.assertEqual(self.req(path, body)[0], 400)   # engine not running: refused, no call

    def item_adapter(self):
        adapter = self.jd2_ready()
        adapter.list_downloads.return_value = {
            "packages": [{"id": "111", "name": "Pack"}],
            "links": [{"id": "333", "package_id": "111", "name": "a.bin"}]}
        adapter.list_linkgrabber.return_value = {"items": [{"id": "501", "package_id": None, "name": "w"}]}
        adapter._options = {"nas_root": str(self.allowed), "jd2_root": "/downloads"}
        adapter.package_save_path.return_value = "/downloads/sub"
        return adapter

    def test_flatten_endpoint_needs_login_csrf_and_origin_and_moves_only_via_the_adapter(self):
        body = {}
        self.assertEqual(self.req("/api/jd2/flatten", body)[0], 401)
        self.login()
        self.assertEqual(self.req("/api/jd2/flatten", body, csrf=False)[0], 403)
        self.assertEqual(self.req("/api/jd2/flatten", body, origin=False)[0], 403)
        self.assertEqual(self.req("/api/jd2/flatten", body)[0], 400)          # no download folder yet
        self.assertEqual(self.req("/api/settings/folder", {"path": str(self.allowed)})[0], 200)
        self.assertEqual(self.req("/api/jd2/flatten", body)[0], 400)          # engine not running
        adapter = self.jd2_ready()
        adapter._options = {"same_host": True}
        adapter.flatten_waiting_downloads.return_value = 2
        with patch.object(self.server.jd2, "_require", return_value=adapter):
            code, out = self.req("/api/jd2/flatten", body)
        self.assertEqual((code, out), (200, {"moved": 2}))
        adapter.flatten_waiting_downloads.assert_called_once_with(str(self.allowed.resolve()))
        self.assertNotIn(str(self.allowed), json.dumps(out))

    def test_downloads_api_returns_relative_folder_markers_and_no_absolute_paths(self):
        self.login()
        self.assertEqual(self.req("/api/settings/folder", {"path": str(self.allowed)})[0], 200)
        base = str(self.allowed.resolve())

        class Adapter:
            _options = {"same_host": True}

            def list_downloads(self, page, base_dir=None):
                import jd2_downloads
                raw = [{"uuid": 1, "name": "Pack", "saveTo": base_dir + "/Pack Folder"},
                       {"uuid": 2, "name": "Flat", "saveTo": base_dir},
                       {"uuid": 3, "name": "Out", "saveTo": "/volume1/private"}]
                return jd2_downloads.snapshot(raw, [], page, base_dir)
        self.server.jd2 = jd2_control.JD2Controller(Adapter())
        code, result = self.req("/api/jd2/downloads")
        self.assertEqual(code, 200)
        self.assertEqual([p["folder"] for p in result["packages"]],
                         [{"flat": False, "name": "Pack Folder"}, {"flat": True}, None])
        text = json.dumps(result, ensure_ascii=False)
        for secret in (base, "/volume1", "saveTo"):
            self.assertNotIn(secret, text)

    def test_item_endpoints_validate_ids_and_call_the_adapter(self):
        self.login()
        adapter = self.item_adapter()
        with patch.object(self.server.jd2, "_require", return_value=adapter):
            self.assertEqual(self.req("/api/jd2/items/start", {"kind": "package", "id": "111", "page": 1})[0], 200)
            adapter.list_downloads.assert_called_with(1)
            adapter.set_enabled.assert_called_with(True, [], [111])
            adapter.force_download.assert_called_with([], [111])
            self.assertEqual(self.req("/api/jd2/items/stop", {"kind": "link", "id": "333"})[0], 200)
            adapter.set_enabled.assert_called_with(False, [333], [])
            self.assertEqual(self.req("/api/jd2/items/remove", {"kind": "link", "ids": ["333"]})[0], 200)
            adapter.remove_links.assert_called_with([333], [])
            adapter.delete_with_files.assert_not_called()
            adapter.reset_mock()
            for body in ({"kind": "package", "id": 111}, {"kind": "package", "id": "999"},
                         {"kind": "package", "id": "1.5"}, {"kind": "package", "ids": ["1"] * 101},
                         {"kind": "nope", "id": "111"}, {"kind": "package"}):
                with self.subTest(body=body):
                    self.assertEqual(self.req("/api/jd2/items/start", body)[0], 400)
            adapter.set_enabled.assert_not_called()
            self.assertEqual(self.req("/api/jd2/waiting/remove", {"ids": ["501"]})[0], 200)
            adapter.remove_waiting.assert_called_with([501])
            self.assertEqual(self.req("/api/jd2/waiting/remove", {"ids": ["502"]})[0], 400)
            self.assertEqual(self.req("/api/jd2/waiting/start", {"id": "501"})[0], 200)
            adapter.move_to_downloadlist.assert_called_with([501], [])

    def test_item_remove_with_files_needs_name_and_the_chosen_folder(self):
        self.login()
        (self.allowed / "sub").mkdir()
        self.assertEqual(self.req("/api/settings/folder", {"path": str(self.allowed / "sub")})[0], 200)
        adapter = self.item_adapter()
        with patch.object(self.server.jd2, "_require", return_value=adapter):
            body = {"kind": "package", "id": "111", "delete_files": True}
            self.assertEqual(self.req("/api/jd2/items/remove", body)[0], 400)
            self.assertEqual(self.req("/api/jd2/items/remove", dict(body, confirm_name="pack"))[0], 400)
            self.assertEqual(self.req("/api/jd2/items/remove", dict(body, delete_files="yes", confirm_name="Pack"))[0], 400)
            adapter.delete_with_files.assert_not_called()
            adapter.package_save_path.return_value = "/downloads/elsewhere"
            self.assertEqual(self.req("/api/jd2/items/remove", dict(body, confirm_name="Pack"))[0], 400)
            adapter.delete_with_files.assert_not_called()
            adapter.package_save_path.return_value = "/downloads/sub/x"
            self.assertEqual(self.req("/api/jd2/items/remove", dict(body, confirm_name="Pack"))[0], 200)
            adapter.delete_with_files.assert_called_once_with([], [111])

    def test_intercept_send_of_a_known_link_is_skipped_without_error(self):
        self.login()
        pid = self.stored_probe([self.HOSTER_A])
        self.req("/api/settings/folder", {"path": str(self.allowed)})
        with patch.object(self.server.jd2, "_require", return_value=self.jd2_ready()), \
                patch("jd2_transfer.prepare_transfer"), \
                patch("jd2_transfer.transfer", return_value={"state": "sent"}) as transfer:
            self.req("/api/rb/probe/send", {"tab": "b1", "probe_id": pid})
            self.assertEqual(transfer.call_count, 1)
            code, body = self.req("/api/rb/probe/send", {"tab": "b1", "probe_id": pid})
            self.assertEqual((code, body["sent"], body["skipped"], body["error"]), (200, 0, 1, None))
            self.assertEqual(transfer.call_count, 1)

    # ---- v0.1.30: viewport link map, reader mode, display settings -----------------
    V30_GET = ("/api/rb/reader/img?tab=b1&i=0", "/api/rb/meta?tab=b1&wait=0&links=1&vp=1")
    V30_POST = (("/api/rb/reader", {"tab": "b1"}),
                ("/api/rb/probe", {"tab": "b1", "x": 1, "y": 1, "snap": "s1"}),
                ("/api/settings/options", {"rb_default_mode": "reader"}))

    def test_v0130_endpoints_require_login_csrf_and_origin(self):
        for path in self.V30_GET:
            with self.subTest(path=path):
                self.assertEqual(self.req(path)[0], 401)
        for path, body in self.V30_POST:
            with self.subTest(path=path):
                self.assertIn(self.req(path, body)[0], (401, 403))
        self.login()
        for path, body in self.V30_POST:
            with self.subTest(path=path, case="no csrf"):
                self.assertEqual(self.req(path, body, csrf=False)[0], 403)
            with self.subTest(path=path, case="no origin"):
                self.assertEqual(self.req(path, body, origin=False)[0], 403)
        self.assertEqual(self.rb_ws.calls("Runtime.evaluate"), [])

    def test_v0130_display_settings_default_persist_and_validate(self):
        self.login()
        settings = self.req("/api/settings")[1]
        self.assertEqual((settings["rb_default_mode"], settings["rb_link_boxes"]), ("fast", False))
        for mode in ("reader", "live", "fast"):
            code, body = self.req("/api/settings/options", {"rb_default_mode": mode})
            self.assertEqual((code, body["rb_default_mode"]), (200, mode))
            self.assertEqual(self.req("/api/settings")[1]["rb_default_mode"], mode)
        self.assertIs(self.req("/api/settings/options", {"rb_link_boxes": True})[1]["rb_link_boxes"], True)
        for bad in ({"rb_default_mode": "tiles"}, {"rb_default_mode": True}, {"rb_default_mode": 1},
                    {"rb_default_mode": None}, {"rb_link_boxes": "yes"}, {"rb_default_mode": "reader", "x": 1}):
            with self.subTest(bad=bad):
                self.assertEqual(self.req("/api/settings/options", bad)[0], 400)
        self.assertEqual(self.req("/api/settings")[1]["rb_default_mode"], "fast")
        # written only when it changes
        before = self.store.revision()
        self.req("/api/settings/options", {"rb_default_mode": "fast"})
        self.assertEqual(self.store.revision(), before)

    def test_v0130_meta_carries_the_viewport_link_map_and_frames_the_links_rev(self):
        self.login()
        self.req("/api/rb/tabs", {"url": "https://example.com/a"})
        self.req("/api/rb/viewport", {"tab": "b1", "width": 390, "height": 700, "dpr": 2})
        self.rb_ws.eval_map = [("__qdlVp", {"scrollX": 0, "scrollY": 40, "width": 390, "height": 700, "links": [
            {"x": 5, "y": 6, "w": 50, "h": 20, "href": "https://example.org/a"},
            {"x": 5, "y": 6, "w": 50, "h": 20, "href": "javascript:1"}]})]
        code, plain = self.req("/api/rb/meta?tab=b1&wait=0")
        self.assertEqual(code, 200)
        self.assertNotIn("viewport_links", plain)
        code, meta = self.req("/api/rb/meta?tab=b1&wait=0&links=1")
        vp = meta["viewport_links"]
        self.assertEqual((code, vp["scrollY"], [l["href"] for l in vp["links"]]), (200, 40, ["https://example.org/a"]))
        code, again = self.req("/api/rb/meta?tab=b1&wait=0&links=1&vp=%d" % vp["rev"])
        self.assertEqual(again["viewport_links"], {"rev": vp["rev"], "unchanged": True})
        self.assertEqual(self.req("/api/rb/meta?tab=b1&wait=0&links=1&fresh=1")[0], 200)
        for bad in ("vp=x", "vp=-1", "vp=" + "1" * 13):
            self.assertEqual(self.req("/api/rb/meta?tab=b1&wait=0&links=1&" + bad)[0], 400)
        self.feed_rb()
        code, headers, _ = self.get_raw("/api/rb/frame.jpg?tab=b1&since=0&wait=0")
        self.assertEqual(code, 200)
        self.assertGreater(int(headers["X-Frame-Links-Rev"]), vp["rev"] - 1)
        self.assertEqual(self.req("/api/rb/meta?tab=b9&wait=0&links=1")[0], 404)

    def test_v0130_probe_by_coordinates_in_the_fast_display_uses_the_snapshot(self):
        self.login()
        self.req("/api/rb/tabs", {"url": "https://example.com/a"})
        self.req("/api/rb/viewport", {"tab": "b1", "width": 390, "height": 700, "dpr": 2})
        code, meta = self.req("/api/rb/snapshot", {"tab": "b1"})
        hoster = "https://rapidgator.net/file/0123456789abcdef0123456789abcdef"
        self.rb_ws.eval_map = [("scrollTo", [0, 600, 390, 700]), ("elementFromPoint", hoster)]
        code, body = self.req("/api/rb/probe", {"tab": "b1", "x": 100, "y": 900, "snap": meta["snap_id"]})
        self.assertEqual((code, body["links"]), (200, [hoster]))
        self.assertEqual(self.req("/api/rb/probe", {"tab": "b1", "x": 1, "y": 1, "snap": "s99"})[0], 404)
        self.assertEqual(self.req("/api/rb/probe", {"tab": "b1", "x": 1, "y": 1, "snap": 5})[0], 400)

    def test_v0130_reader_flow_and_image_endpoint(self):
        self.login()
        self.req("/api/rb/tabs", {"url": "https://example.com/a"})
        secret = "https://cdn.example.org/private/pic.png"
        self.rb_ws.eval_map = [("document.baseURI", {
            "title": "Hello", "images": [secret], "hosters": ["https://rapidgator.net/file/0123456789abcdef0123456789abcdef"],
            "blocks": [{"t": "h", "level": 1, "text": "Head"},
                       {"t": "p", "runs": [{"text": "a", "href": "https://example.org/x"}, {"text": "b", "href": "javascript:1"}]},
                       {"t": "img", "i": 0, "alt": "", "w": 10, "h": 10}]})]
        code, data = self.req("/api/rb/reader", {"tab": "b1"})
        self.assertEqual(code, 200)
        self.assertEqual([b["t"] for b in data["blocks"]], ["h", "p", "img", "h", "li"])
        self.assertEqual(data["blocks"][3]["text"], "ダウンロードリンク")
        self.assertNotIn(secret, json.dumps(data))
        self.assertEqual(self.req("/api/rb/reader", {"tab": "b9"})[0], 404)
        self.assertEqual(self.req("/api/rb/reader", {})[0], 400)
        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 30
        self.rb_ws.results["Page.getResourceTree"] = {"frameTree": {"frame": {"id": "F"}, "resources": [
            {"url": secret, "type": "Image"}]}}
        self.rb_ws.results["Page.getResourceContent"] = {
            "content": base64.b64encode(png).decode(), "base64Encoded": True}
        code, headers, raw = self.get_raw("/api/rb/reader/img?tab=b1&i=0")
        self.assertEqual((code, raw, headers["Content-Type"]), (200, png, "image/png"))
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(self.get_raw("/api/rb/reader/img?tab=b1&i=5")[0], 404)
        self.assertEqual(self.get_raw("/api/rb/reader/img?tab=b9&i=0")[0], 404)
        for bad in ("i=x", "i=-1", "i=" + "1" * 5, "i="):
            self.assertEqual(self.get_raw("/api/rb/reader/img?tab=b1&" + bad)[0], 400)
        # SVG is refused; an unreadable image is a 502
        self.rb_ws.results["Page.getResourceContent"] = {"content": "<svg></svg>", "base64Encoded": False}
        self.assertEqual(self.get_raw("/api/rb/reader/img?tab=b1&i=0")[0], 415)
        self.rb_ws.results["Page.getResourceTree"] = {"frameTree": {"frame": {"id": "F"}, "resources": []}}
        self.rb_ws.eval_map = [("credentials:'include'", {"err": "fetch"})]
        self.assertEqual(self.get_raw("/api/rb/reader/img?tab=b1&i=0")[0], 502)

    def test_probe_autosend_setting_defaults_on_and_persists(self):
        self.login()
        self.assertIs(self.req("/api/settings")[1]["probe_autosend"], True)
        self.assertIs(self.req("/api/settings/options", {"probe_autosend": False})[1]["probe_autosend"], False)
        self.assertIs(self.req("/api/settings")[1]["probe_autosend"], False)

    def test_probe_refuses_the_hub_origin_and_non_https_without_opening_a_page(self):
        self.login()
        self.open_tab_on()
        created = len([m for m in self.rb_ws.sent if m["method"] == "Target.createTarget"])
        for value in ("https://nas.tail1234.ts.net/x", "http://blog.example.org/", None):
            with self.subTest(value=value):
                self.rb_ws.eval_map = [("elementFromPoint", value)]
                self.assertEqual(self.req("/api/rb/probe", {"tab": "b1", "x": 5, "y": 5})[0], 400)
        self.assertEqual(len([m for m in self.rb_ws.sent if m["method"] == "Target.createTarget"]), created)

    def test_viewport_quality_parameter(self):
        self.login()
        self.open_tab_on()
        self.assertEqual(self.req("/api/rb/viewport", {"tab": "b1", "width": 400, "height": 800, "dpr": 3,
                                                       "quality": "high"})[0], 200)
        override = [m for m in self.rb_ws.sent if m["method"] == "Emulation.setDeviceMetricsOverride"][-1]
        self.assertEqual(override["params"]["deviceScaleFactor"], 2.0)
        for bad in ("ultra", 3, "", ["high"]):
            with self.subTest(bad=bad):
                self.assertEqual(self.req("/api/rb/viewport", {"tab": "b1", "width": 401, "height": 800, "dpr": 3,
                                                               "quality": bad})[0], 400)

    def test_linkgrabber_listing_and_start_all(self):
        self.login()
        # engine not running: fails closed
        self.assertEqual(self.req("/api/jd2/linkgrabber")[0], 400)
        adapter = unittest.mock.MagicMock()
        adapter.list_linkgrabber.return_value = {
            "items": [{"id": "11", "package_id": "5", "name": "a.rar", "host": "rapidgator.net", "bytes_total": 1}],
            "count": 1, "has_more": False}
        with patch.object(self.server.jd2, "_require", return_value=adapter):
            code, body = self.req("/api/jd2/linkgrabber")
            self.assertEqual((code, body["count"], body["items"][0]["name"]), (200, 1, "a.rar"))
            self.assertEqual(self.req("/api/jd2/linkgrabber/start", {})[0], 400)
            self.assertEqual(self.req("/api/jd2/linkgrabber/start", {"confirmed": "yes"})[0], 400)
            adapter.move_to_downloadlist.assert_not_called()
            code, body = self.req("/api/jd2/linkgrabber/start", {"confirmed": True})
            self.assertEqual((code, body), (200, {"moved": 1, "started": True}))
            adapter.move_to_downloadlist.assert_called_once_with([11], [5])
            adapter.start_downloads.assert_called_once_with()
            self.assertEqual(self.req("/api/jd2/downloads/start", {})[1], {"started": True})
            self.assertEqual(adapter.start_downloads.call_count, 2)
            self.assertEqual(self.req("/api/jd2/downloads/stop", {})[0], 400)
            adapter.stop_downloads.assert_not_called()
            self.assertEqual(self.req("/api/jd2/downloads/stop", {"confirmed": True})[1], {"stopped": True})
            adapter.stop_downloads.assert_called_once_with()
        for path in ("/api/jd2/linkgrabber/start", "/api/jd2/downloads/start"):
            self.assertEqual(self.req(path, {"confirmed": True})[0], 400)     # no engine: refused


    def stream_open(self, path="/api/rb/stream.mjpg?tab=b1"):
        import http.client
        cookie = "; ".join("%s=%s" % (c.name, c.value) for c in self.cookie)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", path, headers={"Host": self.host, "Cookie": cookie})
        return conn, conn.getresponse()

    @staticmethod
    def stream_part(response):
        boundary = response.readline()
        kind = response.readline()
        length = response.readline()
        blank = response.readline()
        size = int(length.split(b":")[1])
        body = response.read(size)
        tail = response.readline()
        return boundary, kind, length, blank, body, tail

    def test_rb_stream_mjpg_parts_limits_auth_and_close(self):
        # auth first (a stream is never anonymous), then the usual Host check
        self.assertEqual(self.raw("/api/rb/stream.mjpg?tab=b1")[0], 401)
        self.login()
        self.assertEqual(self.get_raw("/api/rb/stream.mjpg?tab=b9")[0], 404)
        self.assertEqual(self.raw("/api/rb/stream.mjpg?tab=b1", host="evil.example")[0], 403)
        self.req("/api/rb/tabs", {"url": "https://example.com/a"})
        jpeg = b"\xff\xd8\xff\xe0JFIF-stream"
        # the response starts with the first frame, so one is fed while the request is open
        feeder = threading.Timer(0.3, lambda: self.rb_ws.emit("Page.screencastFrame", {
            "data": base64.b64encode(jpeg).decode(), "sessionId": 1,
            "metadata": {"deviceWidth": 390, "deviceHeight": 700}}, "SESSION1"))
        feeder.start()
        self.addCleanup(feeder.cancel)
        conn, response = self.stream_open()
        try:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.getheader("Content-Type"), "multipart/x-mixed-replace; boundary=qdlframe")
            self.assertEqual(response.getheader("Cache-Control"), "no-store")
            self.assertEqual(response.getheader("X-Content-Type-Options"), "nosniff")
            boundary, kind, length, blank, body, tail = self.stream_part(response)
            self.assertEqual((boundary, kind, blank, body, tail), (
                b"--qdlframe\r\n", b"Content-Type: image/jpeg\r\n", b"\r\n", jpeg, b"\r\n"))
            self.assertEqual(length, b"Content-Length: %d\r\n" % len(jpeg))
            meta = self.req("/api/rb/meta?tab=b1&wait=0")[1]
            self.assertEqual(meta["streams"], 1)
            self.assertIn("fps", meta)
            # per-tab limit (v0.1.46): a third stream replaces the oldest one instead of being refused
            conn2, second = self.stream_open()
            try:
                for _ in range(200):
                    if self.server.rb._tabs["b1"].streams == 2:
                        break
                    time.sleep(0.01)
                conn3, third = self.stream_open()
                try:
                    self.assertEqual(third.status, 200)
                    while response.read(65536):          # the oldest stream is ended by the Hub: EOF
                        pass
                    for _ in range(300):
                        if self.server.rb._tabs["b1"].streams == 2:
                            break
                        time.sleep(0.01)
                    self.assertEqual(self.server.rb._tabs["b1"].streams, 2)
                finally:
                    conn3.close()
            finally:
                conn2.close()
            # closing the tab ends the stream
            self.assertEqual(self.req("/api/rb/tabs/close", {"id": "b1"})[0], 200)
            for _ in range(300):
                if self.server.rb._streams == 0:
                    break
                time.sleep(0.01)
            self.assertEqual(self.server.rb._streams, 0)
        finally:
            conn.close()


    def test_rb_stream_sse_headers_events_auth_and_close(self):
        self.assertEqual(self.raw("/api/rb/stream.sse?tab=b1")[0], 401)
        self.login()
        self.assertEqual(self.get_raw("/api/rb/stream.sse?tab=b9")[0], 404)
        self.assertEqual(self.raw("/api/rb/stream.sse?tab=b1", host="evil.example")[0], 403)
        self.req("/api/rb/tabs", {"url": "https://example.com/a"})
        jpeg = b"\xff\xd8\xff\xe0JFIF-sse"
        conn, response = self.stream_open("/api/rb/stream.sse?tab=b1")
        try:
            self.assertEqual(response.status, 200)       # headers go out with the padding, before any frame
            self.assertEqual(response.getheader("Content-Type"), "text/event-stream")
            self.assertEqual(response.getheader("Cache-Control"), "no-store")
            self.assertEqual(response.getheader("X-Accel-Buffering"), "no")
            self.assertEqual(response.getheader("X-Content-Type-Options"), "nosniff")
            self.assertIsNone(response.getheader("Content-Length"))
            pad = response.readline()
            self.assertTrue(pad.startswith(b":") and len(pad) >= 2048)
            self.assertEqual(response.readline(), b"\n")
            self.rb_ws.emit("Page.screencastFrame", {
                "data": base64.b64encode(jpeg).decode(), "sessionId": 1,
                "metadata": {"deviceWidth": 390, "deviceHeight": 700}}, "SESSION1")
            self.assertEqual(response.readline(), b"event: frame\n")
            data = response.readline()
            self.assertEqual(data, b"data: " + base64.b64encode(jpeg) + b"\n")
            self.assertEqual(response.readline(), b"\n")
            self.assertEqual(self.req("/api/rb/meta?tab=b1&wait=0")[1]["streams"], 1)
            self.assertEqual(self.req("/api/rb/tabs/close", {"id": "b1"})[0], 200)
            for _ in range(300):
                if self.server.rb._streams == 0:
                    break
                time.sleep(0.01)
            self.assertEqual(self.server.rb._streams, 0)
        finally:
            conn.close()

    def test_state_polls_with_a_running_browser_never_start_docker_stats(self):
        self.login()
        self.req("/api/rb/tabs", {"url": "https://example.com/a"})

        def stats_calls():
            return len([c for c in self.rb_fake.docker_calls() if c[0] == "stats"])
        real, seen = self.server.rb.status, []

        def spy(*args, **kwargs):
            seen.append(kwargs)
            return real(*args, **kwargs)
        self.server.rb.status = spy
        self.addCleanup(lambda: setattr(self.server.rb, "status", real))
        baseline = stats_calls()
        for _ in range(5):
            webapp.server_cache(self.server).invalidate()           # every poll recomputes the browser part
            webapp.server_state_hub(self.server)._refresh(force=True)
            self.assertEqual(self.req("/api/state?full=1")[0], 200)
        self.assertGreaterEqual(len(seen), 5)                       # the state really asked rb.status() ...
        self.assertTrue(all(not kw.get("fresh_memory") for kw in seen))
        self.assertEqual(stats_calls(), baseline)                   # ... and docker stats never ran for it
        webapp.server_cache(self.server).invalidate()
        self.assertEqual(self.req("/api/rb/status")[0], 200)        # the settings view asks for fresh figures
        self.assertTrue(seen[-1].get("fresh_memory"))


    # ---- v0.1.38: resident NAS browser ------------------------------------------------
    def test_v0138_resident_setting_default_persist_validate_and_apply(self):
        self.login()
        self.assertIs(self.req("/api/settings")[1]["rb_resident"], False)
        code, body = self.req("/api/settings/options", {"rb_resident": True})
        self.assertEqual((code, body["rb_resident"]), (200, True))
        self.assertTrue(self.server.rb.resident())
        self.assertIs(self.store.settings()["rb_resident"], True)
        for bad in ({"rb_resident": "yes"}, {"rb_resident": 1}, {"rb_resident": None},
                    {"rb_resident": True, "x": 1}):
            with self.subTest(bad=bad):
                self.assertEqual(self.req("/api/settings/options", bad)[0], 400)
        self.assertIs(self.req("/api/settings/options", {"rb_resident": False})[1]["rb_resident"], False)
        self.assertFalse(self.server.rb.resident())
        before = self.store.revision()
        self.req("/api/settings/options", {"rb_resident": False})
        self.assertEqual(self.store.revision(), before)

    def test_v0138_boot_starts_resident_only_when_enabled(self):
        self.login()
        webapp.boot_remote_browser(self.server)
        self.assertFalse(self.server.rb.resident())
        self.store.set_option("rb_resident", True)
        webapp.boot_remote_browser(self.server)
        self.assertTrue(self.server.rb.resident())

    def test_v0138_status_state_and_retry_endpoint(self):
        self.assertEqual(self.req("/api/rb/retry")[0], 401)
        self.assertIn(self.req("/api/rb/retry", {})[0], (401, 403))
        self.login()
        self.assertEqual(self.req("/api/rb/retry", {}, csrf=False)[0], 403)
        self.assertEqual(self.req("/api/rb/retry", {}, origin=False)[0], 403)
        self.assertEqual(self.req("/api/rb/retry", {"x": 1})[0], 202)
        status = self.req("/api/rb/status")[1]
        for key in ("on", "state", "memory", "idle", "hub_rss_mib", "total_mib", "watchdog"):
            self.assertIn(key, status["resident"])
        for key in ("state", "restart_n", "reason", "message", "events", "restarts", "lost_seq"):
            self.assertIn(key, status["resident"]["watchdog"])
        rbstate = self.req("/api/state")[1]["rb"]
        for key in ("res", "rs", "idle_mib", "cur_mib", "restarts", "lost"):
            self.assertIn(key, rbstate)


if __name__ == "__main__":
    unittest.main()
