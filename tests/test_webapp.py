"""HTTP integration tests: zero network requests beyond local test server."""
import http.cookiejar
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

import self_update
import storage
import engine_supervisor
import jd2_control
import jd2_local
import container_runtime
import remote_browser
import webapp
from fake_docker import FakeDocker
from test_remote_browser import FakeWS


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

    def req(self, path, body=None, origin=True, csrf=True):
        headers = {"Host": self.host}
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

    RB_GET = ("/api/rb/status", "/api/rb/log", "/api/rb/frame?tab=b1&since=0", "/api/rb/links?tab=b1")
    RB_POST = (("/api/rb/setup", {"confirmed": True}), ("/api/rb/stop", {"confirmed": True}),
               ("/api/rb/tabs", {"url": "https://example.com/"}), ("/api/rb/tabs/close", {"id": "b1"}),
               ("/api/rb/navigate", {"tab": "b1", "url": "https://example.com/"}),
               ("/api/rb/input", {"tab": "b1", "type": "reload"}),
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

    def test_rb_tab_frame_input_links_close_flow(self):
        self.login()
        code, tab = self.req("/api/rb/tabs", {"url": "https://example.com/a"})
        self.assertEqual((code, tab["id"]), (201, "b1"))
        code, frame = self.req("/api/rb/frame?tab=b1&since=0&wait=0")
        self.assertEqual(code, 200)
        self.assertEqual(set(frame), {"seq", "jpeg_base64", "width", "height", "url", "title",
                                      "loading", "blocked"})
        self.assertIsNone(frame["jpeg_base64"])
        self.rb_ws.emit("Page.screencastFrame", {"data": "QUJD", "sessionId": 1, "metadata": {
            "deviceWidth": 1280, "deviceHeight": 2000}}, "SESSION1")
        for _ in range(100):
            code, frame = self.req("/api/rb/frame?tab=b1&since=0&wait=0")
            if frame["jpeg_base64"]:
                break
        self.assertEqual(frame["jpeg_base64"], "QUJD")
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
        self.assertEqual(self.req("/api/rb/frame?tab=b1&since=0&wait=0")[0], 404)
        self.assertEqual(self.req("/api/rb/tabs/close", {"id": "b1"})[0], 404)
        self.assertEqual(self.req("/api/rb/frame?tab=b1&since=abc")[0], 400)
        code, body = self.req("/api/rb/stop", {"confirmed": True})
        self.assertEqual(code, 200)
        self.assertNotIn("qnapdl-browser", self.rb_fake.containers)

    def test_rb_tab_limit_and_memory_refusals_are_japanese_400s(self):
        self.login()
        for i in range(3):
            self.assertEqual(self.req("/api/rb/tabs", {"url": "https://example.com/%d" % i})[0], 201)
        code, body = self.req("/api/rb/tabs", {"url": "https://example.com/x"})
        self.assertEqual(code, 400)
        self.assertIn("最大3枚", body["error"])

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

    def test_icons_and_private_ip_host_are_served_without_login(self):
        request = urllib.request.Request(self.url + "/api/health",
                                         headers={"Host": "192.168.68.57:17864"})
        with self.client.open(request, timeout=4) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(json.load(response)["version"], "0.1.19")
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
            self.assertIn("img-src 'self' data:;", csp)
            self.assertNotIn("blob:", csp)
            self.assertIn("default-src 'none'", csp)
            self.assertIn("frame-ancestors 'none'", csp)
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
        self.assertFalse(data["server_fetched"])
        for url in ("http://example.com", "https://192.168.68.57:17864",
                    "https://localhost/", "javascript:alert(1)"):
            with self.subTest(url=url):
                path="/api/browser/validate?url="+urllib.parse.quote(url,safe="")
                self.assertEqual(self.req(path)[0],400)
        with self.client.open(self.url,timeout=4) as response:
            html=response.read().decode()
            csp=response.headers.get("Content-Security-Policy")
        # Per-tab iframes are created by app.js; the sandbox values live there.
        script = (Path(webapp.__file__).parent / "web/app.js").read_text()
        self.assertIn('SANDBOX_STRICT = ""', script)
        self.assertIn('"allow-scripts allow-forms"', script)
        self.assertIn('referrerpolicy', script)
        self.assertIn("frame-src https:",csp)
        self.assertIn("frame-ancestors 'none'",csp)
        self.assertNotIn("allow-same-origin",html)
        self.assertNotIn("allow-downloads",html)

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

    def test_low_io_and_local_jd2_preflight_are_login_protected(self):
        self.assertEqual(self.req("/api/diagnostics/io")[0], 401)
        self.assertEqual(self.req("/api/jd2/local-preflight")[0], 401)
        self.login()
        status, data = self.req("/api/diagnostics/io")
        self.assertEqual(status, 200)
        self.assertIn("process_io", data)
        self.assertIn("events", data)
        self.assertTrue(data["local_only"])
        self.assertFalse(data["log_persistence"])
        with patch.object(webapp.runtime_diagnostics, "local_jd2_preflight",
                          return_value={"tcp_reachable": True,
                                        "operations_enabled": False}) as probe:
            status, result = self.req("/api/jd2/local-preflight")
        self.assertEqual(status, 200)
        self.assertTrue(result["tcp_reachable"])
        self.assertFalse(result["operations_enabled"])
        probe.assert_called_once_with()

    def test_update_apply_fails_closed_when_not_qpkg(self):
        self.login()
        info = self.req("/api/update/status")[1]
        self.assertFalse(info["supported"])
        code, _ = self.req("/api/update/apply", {"version": "v0.1.8"})
        self.assertEqual(code, 400)
        self.assertEqual(self.req("/api/health")[1]["version"], "0.1.19")
        self.assertFalse(self.req("/api/jd2/status")[1]["connected"])
        self.assertEqual(self.req("/api/jd2/update/apply", {"confirmed": True})[0], 400)
        self.assertEqual(self.req("/api/jd2/preset/apply", {"confirmed": True})[0], 400)


if __name__ == "__main__":
    unittest.main()
