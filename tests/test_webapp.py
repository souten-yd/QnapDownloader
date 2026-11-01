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
import jd2_control
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
        self.server.jd2 = jd2_control.JD2Controller()
        self.server.secure_cookies = False
        self.server.bootstrap_token = "unit-test-bootstrap"
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
        self.assertEqual(self.req("/api/bootstrap", {
            "token": "unit-test-bootstrap", "password": "correct horse staple 123"})[0], 200)
        code, body = self.req("/api/login", {"password": "correct horse staple 123"})
        self.assertEqual(code, 200)
        self.csrf = body["csrf"]

    def test_host_filter_allows_private_and_tailscale_not_public(self):
        self.assertTrue(webapp.public_host_allowed("192.168.0.5:17864"))
        self.assertTrue(webapp.public_host_allowed("100.64.1.2:17864"))
        self.assertTrue(webapp.public_host_allowed("[::1]:17864"))
        self.assertFalse(webapp.public_host_allowed("8.8.8.8:17864"))
        self.assertFalse(webapp.public_host_allowed("evil.example:17864"))

    def test_setup_login_csrf_and_private_api(self):
        self.assertEqual(self.req("/api/settings")[0], 401)
        self.assertEqual(self.req("/api/bootstrap", {
            "token": "wrong", "password": "correct horse staple 123"})[0], 403)
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
        self.assertEqual(self.req("/api/logout", {})[0], 200)
        self.assertEqual(self.req("/api/settings")[0], 401)

    def test_folder_picker_persists_allowed_path_only(self):
        folder = self.allowed / "subdir"
        folder.mkdir()
        self.login()
        self.assertEqual(self.req("/api/settings/folder", {"path": "/etc"})[0], 400)
        self.assertEqual(self.req("/api/settings/folder", {"path": str(folder)})[0], 200)
        self.assertEqual(self.req("/api/settings")[1]["download_dir"], str(folder))
        self.assertEqual(self.req("/api/folders")[0], 200)
        self.assertEqual(self.req("/api/folders?path="+str(self.allowed))[0], 200)

    def test_update_apply_fails_closed_when_not_qpkg(self):
        self.login()
        info = self.req("/api/update/status")[1]
        self.assertFalse(info["supported"])
        code, _ = self.req("/api/update/apply", {"version": "v0.1.1"})
        self.assertEqual(code, 400)
        self.assertEqual(self.req("/api/health")[1]["version"], "0.1.0")
        self.assertFalse(self.req("/api/jd2/status")[1]["connected"])
        self.assertEqual(self.req("/api/jd2/update/apply", {"confirmed": True})[0], 400)
        self.assertEqual(self.req("/api/jd2/preset/apply", {"confirmed": True})[0], 400)


if __name__ == "__main__":
    unittest.main()
