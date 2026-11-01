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
import webapp


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

    def login(self):
        self.assertEqual(self.req("/api/setup", {
            "password": "correct horse staple 123"})[0], 201)
        code, body = self.req("/api/login", {"password": "correct horse staple 123"})
        self.assertEqual(code, 200)
        self.csrf = body["csrf"]

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
            self.assertEqual(json.load(response)["version"], "0.1.12")
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
            self.assertIn("img-src 'self'", csp)
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
        self.server.engine.start_setup = lambda: started.append(1)
        self.assertEqual(self.req("/api/engine/setup", {"confirmed": True})[0], 202)
        def busy():
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
        self.assertEqual(self.req("/api/health")[1]["version"], "0.1.12")
        self.assertFalse(self.req("/api/jd2/status")[1]["connected"])
        self.assertEqual(self.req("/api/jd2/update/apply", {"confirmed": True})[0], 400)
        self.assertEqual(self.req("/api/jd2/preset/apply", {"confirmed": True})[0], 400)


if __name__ == "__main__":
    unittest.main()
