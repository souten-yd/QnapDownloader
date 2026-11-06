"""Passkey endpoints, persistent sessions, revocation and rate limits (v0.1.53)."""
import hashlib
import http.client
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import debuglog
import storage
import webapp
import webauthn
import webauthn_testkit as kit

TS_HOST = "nas.tail1234.ts.net"
PASSWORD = "correct horse staple 123"


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.servers = []
        self.start()
        for table in (webapp.FAILED, webapp.PASSKEY_BEGIN, webapp.SHARE_FAILED):
            table.clear()
        self.auth = kit.Authenticator()

    def start(self):
        store = storage.Store(self.root)
        server = ThreadingHTTPServer(("127.0.0.1", 0), webapp.Handler)
        server.store = store
        server.secure_cookies = False
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.store, self.server, self.port = store, server, server.server_port
        self.servers.append((server, thread, store))
        self.addCleanup(self.stop_all)

    def stop_all(self):
        while self.servers:
            self.stop_one()

    def stop_one(self):
        server, thread, store = self.servers.pop()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        store.close()

    def restart(self):
        self.stop_one()
        self.start()

    def call(self, path, body=None, cookie=None, csrf=None, host=TS_HOST, origin="auto", extra=None, method=None):
        headers = {"Host": host}
        if cookie:
            headers["Cookie"] = "qdl_session=" + cookie
        if body is not None:
            headers["Content-Type"] = "application/json"
            if origin == "auto":
                origin = ("https://" if host.endswith(".ts.net") else "http://") + host
            if origin:
                headers["Origin"] = origin
            if csrf:
                headers["X-CSRF-Token"] = csrf
        headers.update(extra or {})
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(method or ("POST" if body is not None else "GET"), path,
                         json.dumps(body) if body is not None else None, headers)
            response = conn.getresponse()
            raw = response.read()
            return response.status, response, (json.loads(raw) if raw else {})
        finally:
            conn.close()

    @staticmethod
    def cookie_of(response):
        header = response.getheader("Set-Cookie") or ""
        return header.split(";")[0].split("=", 1)[1] if "qdl_session=" in header else None

    def setup_admin(self):
        self.assertEqual(self.call("/api/setup", {"password": PASSWORD})[0], 201)

    def password_login(self, remember=None, host=TS_HOST):
        body = {"password": PASSWORD}
        if remember is not None:
            body["remember"] = remember
        status, response, data = self.call("/api/login", body, host=host)
        self.assertEqual(status, 200, data)
        return self.cookie_of(response), data["csrf"], response

    def register(self, cookie, csrf, auth=None):
        auth = auth or self.auth
        status, _, options = self.call("/api/passkeys/register/begin", {}, cookie, csrf)
        self.assertEqual(status, 200, options)
        fields = auth.create(options, "https://" + TS_HOST)
        status, _, data = self.call("/api/passkeys/register/finish", fields, cookie, csrf)
        self.assertEqual(status, 201, data)
        return options, data

    def login_options(self):
        status, _, options = self.call("/api/passkeys/login/begin", {})
        self.assertEqual(status, 200, options)
        return options


class SessionTests(Fixture):
    def test_short_and_remembered_sessions_have_matching_max_age(self):
        self.setup_admin()
        cookie, csrf, response = self.password_login()
        header = response.getheader("Set-Cookie")
        self.assertIn("Max-Age=28800", header)
        for token in ("HttpOnly", "SameSite=Strict", "Secure"):
            self.assertIn(token, header)
        cookie2, csrf2, response2 = self.password_login(remember=True)
        self.assertIn("Max-Age=%d" % storage.SESSION_IDLE, response2.getheader("Set-Cookie"))
        self.assertNotEqual(csrf, csrf2)
        self.assertEqual(self.call("/api/login", {"password": PASSWORD, "remember": "yes"})[0], 400)

    def test_session_endpoint_and_per_session_csrf(self):
        self.setup_admin()
        cookie, csrf, _ = self.password_login()
        status, _, data = self.call("/api/session", cookie=cookie)
        self.assertEqual((data["authenticated"], data["csrf"]), (True, csrf))
        self.assertEqual(self.call("/api/settings/options", {"block_ads": True}, cookie, "wrong")[0], 403)
        self.assertEqual(self.call("/api/settings/options", {"block_ads": True}, cookie, csrf)[0], 200)
        other, other_csrf, _ = self.password_login()
        self.assertEqual(self.call("/api/settings/options", {"block_ads": True}, cookie, other_csrf)[0], 403)

    def test_only_hashes_are_stored_never_the_cookie_value(self):
        self.setup_admin()
        cookie, csrf, _ = self.password_login(remember=True)
        digest = hashlib.sha256(cookie.encode()).hexdigest()
        with sqlite3.connect(self.store.path) as db:
            rows = db.execute("SELECT id_hash FROM sessions").fetchall()
        self.assertEqual(rows, [(digest,)])
        blob = b"".join(p.read_bytes() for p in self.store.directory.glob("downloader.sqlite3*"))
        self.assertNotIn(cookie.encode(), blob)
        self.assertNotIn(csrf.encode(), blob)

    def test_sessions_survive_a_restart(self):
        self.setup_admin()
        cookie, csrf, _ = self.password_login(remember=True)
        short, short_csrf, _ = self.password_login()
        self.restart()
        for value, token in ((cookie, csrf), (short, short_csrf)):
            status, _, data = self.call("/api/session", cookie=value)
            self.assertEqual((data["authenticated"], data["csrf"]), (True, token))
            self.assertEqual(self.call("/api/settings/options", {"block_ads": True}, value, token)[0], 200)

    def test_lifetimes_idle_absolute_and_touch(self):
        day = 86400
        raw, csrf, age = self.store.create_session("password", remember=False, now=1000)
        self.assertEqual(age, 8 * 3600)
        self.assertIsNotNone(self.store.lookup_session(raw, now=1000 + 8 * 3600 - 1))
        self.assertIsNone(self.store.lookup_session(raw, now=1000 + 8 * 3600 + 1))
        raw, csrf, age = self.store.create_session("passkey", remember=True, now=1000)
        self.assertEqual(age, 30 * day)
        found = self.store.lookup_session(raw, now=1000 + 10)
        self.assertFalse(found["touched"])                            # no write within the hour
        found = self.store.lookup_session(raw, now=1000 + 29 * day)
        self.assertTrue(found["touched"])
        self.assertEqual(found["max_age"], 30 * day)                  # idle window slid forward
        for step in range(2, 4):                                      # keep it busy every 29 days
            found = self.store.lookup_session(raw, now=1000 + 29 * day * step)
        self.assertEqual(found["max_age"], 90 * day - 29 * day * 3)   # capped by the absolute lifetime
        self.assertIsNone(self.store.lookup_session(raw, now=1000 + 90 * day + 1))
        raw, _, _ = self.store.create_session("passkey", remember=True, now=5000)
        self.assertIsNone(self.store.lookup_session(raw, now=5000 + 30 * day + 1))     # idle timeout
        self.assertIsNone(self.store.lookup_session("", now=1))
        self.assertIsNone(self.store.lookup_session("x" * 500, now=1))

    def test_touch_reissues_the_cookie_and_survives_restart(self):
        self.setup_admin()
        cookie, _, _ = self.password_login(remember=True)
        with sqlite3.connect(self.store.path) as db:
            db.execute("UPDATE sessions SET last_seen=last_seen-7200,created_at=created_at-7200")
        self.restart()
        status, response, data = self.call("/api/session", cookie=cookie)
        self.assertTrue(data["authenticated"])
        self.assertIn("Max-Age=", response.getheader("Set-Cookie") or "")

    def test_expired_sessions_are_rejected_and_purged(self):
        self.setup_admin()
        cookie, csrf, _ = self.password_login()
        with sqlite3.connect(self.store.path) as db:
            db.execute("UPDATE sessions SET created_at=created_at-30000,last_seen=last_seen-30000")
        self.restart()
        self.assertEqual(self.call("/api/state", cookie=cookie)[0], 401)
        self.assertEqual(self.store.session_count(), 0)

    def test_logout_and_revoke_all(self):
        self.setup_admin()
        first, csrf1, _ = self.password_login(remember=True)
        second, csrf2, _ = self.password_login(remember=True)
        status, response, _ = self.call("/api/logout", {}, first, csrf1)
        self.assertEqual(status, 200)
        self.assertIn("Max-Age=0", response.getheader("Set-Cookie"))
        self.assertFalse(self.call("/api/session", cookie=first)[2]["authenticated"])
        self.assertTrue(self.call("/api/session", cookie=second)[2]["authenticated"])
        third, csrf3, _ = self.password_login()
        self.assertEqual(self.call("/api/sessions/revoke-all", {}, second, "bad")[0], 403)
        self.assertEqual(self.call("/api/sessions/revoke-all", {}, None, None)[0], 401)
        status, response, data = self.call("/api/sessions/revoke-all", {}, second, csrf2)
        self.assertEqual((status, data["revoked"]), (200, 2))
        for value in (second, third):
            self.assertFalse(self.call("/api/session", cookie=value)[2]["authenticated"])
        self.restart()
        self.assertFalse(self.call("/api/session", cookie=second)[2]["authenticated"])

    def test_session_count_is_bounded(self):
        for _ in range(storage.SESSION_LIMIT + 5):
            self.store.create_session("password", False)
        self.assertEqual(self.store.session_count(), storage.SESSION_LIMIT)

    def test_session_ids_never_reach_the_debug_log(self):
        self.setup_admin()
        debuglog.LOG.enable("debug")
        self.addCleanup(debuglog.LOG.disable)
        cookie, csrf, _ = self.password_login(remember=True)
        self.call("/api/session", cookie=cookie)
        self.call("/api/logout", {}, cookie, csrf)
        dump = json.dumps(debuglog.LOG.entries(0, 1000))
        self.assertNotIn(cookie, dump)
        self.assertNotIn(csrf, dump)


class RegistrationTests(Fixture):
    def setUp(self):
        super().setUp()
        self.setup_admin()
        self.cookie, self.csrf, _ = self.password_login()

    def test_register_begin_requires_login_csrf_origin_and_host(self):
        path = "/api/passkeys/register/begin"
        self.assertEqual(self.call(path, {})[0], 401)
        self.assertEqual(self.call(path, {}, self.cookie)[0], 403)                       # no CSRF
        self.assertEqual(self.call(path, {}, self.cookie, "bad")[0], 403)
        self.assertEqual(self.call(path, {}, self.cookie, self.csrf, origin=None)[0], 403)
        self.assertEqual(self.call(path, {}, self.cookie, self.csrf, origin="https://evil.example")[0], 403)
        self.assertEqual(self.call(path, {}, self.cookie, self.csrf, host="evil.example.com")[0], 403)
        self.assertEqual(self.call("/api/passkeys")[0], 401)
        for path, body in (("/api/passkeys/register/finish", {}), ("/api/passkeys/rename", {"id": 1, "label": "x"}),
                           ("/api/passkeys/delete", {"id": 1})):
            with self.subTest(path=path):
                self.assertEqual(self.call(path, body)[0], 401)
                self.assertEqual(self.call(path, body, self.cookie, "bad")[0], 403)

    def test_http_and_ip_hosts_cannot_use_passkeys(self):
        for host in ("127.0.0.1:%d" % self.port, "localhost:%d" % self.port):
            status, _, data = self.call("/api/passkeys/register/begin", {}, self.cookie, self.csrf, host=host)
            self.assertEqual(status, 400, host)
            self.assertIn("ts.net", data["error"])
        status, _, data = self.call("/api/session", host="127.0.0.1:%d" % self.port)
        self.assertEqual(data["passkey"], {"available": False, "registered": 0})
        # a forged X-Forwarded-Proto does not help a non-ts.net host; an IP host is never an rp id
        status, _, data = self.call("/api/passkeys/register/begin", {}, self.cookie, self.csrf,
                                    host="127.0.0.1:%d" % self.port, extra={"X-Forwarded-Proto": "https"})
        self.assertEqual(status, 400)

    def test_begin_options_shape(self):
        status, _, options = self.call("/api/passkeys/register/begin", {}, self.cookie, self.csrf)
        self.assertEqual(status, 200)
        self.assertEqual(len(webauthn.b64url_decode(options["challenge"])), 32)
        self.assertEqual(options["rp"], {"id": TS_HOST, "name": "QnapDownloader"})
        self.assertEqual(len(webauthn.b64url_decode(options["user"]["id"])), 16)
        self.assertEqual(options["user"]["name"], "admin")
        self.assertEqual([p["alg"] for p in options["pubKeyCredParams"]], [-7, -257])
        self.assertEqual(options["authenticatorSelection"], {
            "residentKey": "required", "requireResidentKey": True, "userVerification": "required",
            "authenticatorAttachment": "platform"})
        self.assertEqual((options["attestation"], options["excludeCredentials"]), ("none", []))
        again = self.call("/api/passkeys/register/begin", {}, self.cookie, self.csrf)[2]
        self.assertEqual(again["user"]["id"], options["user"]["id"])                     # stable handle
        self.assertNotEqual(again["challenge"], options["challenge"])
        self.restart()
        cookie, csrf, _ = self.password_login()
        self.assertEqual(self.call("/api/passkeys/register/begin", {}, cookie, csrf)[2]["user"]["id"],
                         options["user"]["id"])

    def test_register_list_exclude_rename_delete(self):
        options, data = self.register(self.cookie, self.csrf)
        status, _, listing = self.call("/api/passkeys", cookie=self.cookie)
        item = listing["items"][0]
        self.assertEqual((item["label"], item["rp_id"], item["id"]), ("テストiPhone", TS_HOST, data["id"]))
        self.assertIsNone(item["last_used_at"])
        self.assertNotIn("public_key", item)
        self.assertTrue(listing["available"])
        self.assertEqual(listing["https_url"], "https://" + TS_HOST + "/")
        again = self.call("/api/passkeys/register/begin", {}, self.cookie, self.csrf)[2]
        self.assertEqual([e["id"] for e in again["excludeCredentials"]],
                         [webauthn.b64url_encode(self.auth.credential_id)])
        fields = self.auth.create(again, "https://" + TS_HOST)
        status, _, error = self.call("/api/passkeys/register/finish", fields, self.cookie, self.csrf)
        self.assertEqual(status, 400)                                                    # same credential id
        self.assertEqual(self.call("/api/passkeys/rename", {"id": data["id"], "label": "仕事用"},
                                   self.cookie, self.csrf)[0], 200)
        self.assertEqual(self.call("/api/passkeys", cookie=self.cookie)[2]["items"][0]["label"], "仕事用")
        for bad in ("", "x" * 41, 5, "a\nb"):
            self.assertEqual(self.call("/api/passkeys/rename", {"id": data["id"], "label": bad},
                                       self.cookie, self.csrf)[0], 400)
        self.assertEqual(self.call("/api/passkeys/delete", {"id": 999}, self.cookie, self.csrf)[0], 400)
        self.assertEqual(self.call("/api/passkeys/delete", {"id": data["id"]}, self.cookie, self.csrf)[0], 200)
        self.assertEqual(self.call("/api/passkeys", cookie=self.cookie)[2]["items"], [])

    def test_only_public_material_is_stored(self):
        self.register(self.cookie, self.csrf)
        blob = b"".join(p.read_bytes() for p in self.store.directory.glob("downloader.sqlite3*"))
        self.assertIn(self.auth.cose, blob)
        self.assertNotIn(self.auth.private.to_bytes(32, "big"), blob)
        with sqlite3.connect(self.store.path) as db:
            columns = [r[1] for r in db.execute("PRAGMA table_info(passkeys)")]
        self.assertNotIn("private", " ".join(columns))

    def finish(self, options, **kwargs):
        fields = self.auth.create(options, kwargs.pop("origin", "https://" + TS_HOST), **kwargs)
        return self.call("/api/passkeys/register/finish", fields, self.cookie, self.csrf)

    def test_register_finish_rejections(self):
        begin = lambda: self.call("/api/passkeys/register/begin", {}, self.cookie, self.csrf)[2]
        for name, kwargs in (("origin", dict(origin="https://evil.ts.net")), ("rp hash", dict(rp_id="evil.ts.net")),
                             ("no UV", dict(flags=0x01)), ("no UP", dict(flags=0x04))):
            with self.subTest(name):
                self.assertEqual(self.finish(begin(), **kwargs)[0], 400)
        options = begin()
        fields = self.auth.create(options, "https://" + TS_HOST)
        for name, change in (("missing field", {"attestationObject": None}), ("bad base64", {"clientDataJSON": "***"}),
                             ("rawId differs", {"rawId": "AAAA"}), ("huge", {"attestationObject": "A" * 20000}),
                             ("transports", {"transports": "internal"})):
            with self.subTest(name):
                status, _, _ = self.call("/api/passkeys/register/finish", dict(fields, **change), self.cookie, self.csrf)
                self.assertIn(status, (400, 413))
        self.assertEqual(self.call("/api/passkeys", cookie=self.cookie)[2]["items"], [])

    def test_challenge_is_single_use_bound_to_session_and_expires(self):
        options = self.call("/api/passkeys/register/begin", {}, self.cookie, self.csrf)[2]
        fields = self.auth.create(options, "https://" + TS_HOST)
        other, other_csrf, _ = self.password_login()
        status, _, _ = self.call("/api/passkeys/register/finish", fields, other, other_csrf)   # another session
        self.assertEqual(status, 400)
        # that attempt burned it, so the rightful session cannot use it either
        self.assertEqual(self.call("/api/passkeys/register/finish", fields, self.cookie, self.csrf)[0], 400)
        options = self.call("/api/passkeys/register/begin", {}, self.cookie, self.csrf)[2]
        fields = self.auth.create(options, "https://" + TS_HOST)
        self.assertEqual(self.call("/api/passkeys/register/finish", fields, self.cookie, self.csrf)[0], 201)
        replay = self.auth.create(options, "https://" + TS_HOST)
        self.auth.credential_id = b"second-credential-0123456789ab"
        self.assertEqual(self.call("/api/passkeys/register/finish", replay, self.cookie, self.csrf)[0], 400)
        # expiry: a fresh challenge older than the TTL
        self.server.challenges = webauthn.Challenges(ttl=-1)
        options = self.call("/api/passkeys/register/begin", {}, self.cookie, self.csrf)[2]
        self.assertEqual(self.call("/api/passkeys/register/finish", self.auth.create(options, "https://" + TS_HOST),
                                   self.cookie, self.csrf)[0], 400)

    def test_passkey_limit(self):
        for index in range(storage.MAX_PASSKEYS):
            auth = kit.Authenticator(private=1000 + index, credential_id=b"credential-%02d-0123456789abcdef" % index)
            self.register(self.cookie, self.csrf, auth)
        options = self.call("/api/passkeys/register/begin", {}, self.cookie, self.csrf)[2]
        extra = kit.Authenticator(private=77, credential_id=b"one-too-many-0123456789abcdef")
        status, _, error = self.call("/api/passkeys/register/finish", extra.create(options, "https://" + TS_HOST),
                                     self.cookie, self.csrf)
        self.assertEqual(status, 400)


class LoginTests(Fixture):
    def setUp(self):
        super().setUp()
        self.setup_admin()
        self.cookie, self.csrf, _ = self.password_login()
        self.register(self.cookie, self.csrf)
        self.origin = "https://" + TS_HOST

    def finish(self, options, auth=None, **kwargs):
        fields = (auth or self.auth).get(options, kwargs.pop("origin", self.origin), **kwargs)
        return self.call("/api/passkeys/login/finish", fields)

    def test_login_flow_creates_remembered_session(self):
        status, _, data = self.call("/api/session")
        self.assertEqual(data["passkey"], {"available": True, "registered": 1})
        options = self.login_options()
        self.assertEqual((options["rpId"], options["userVerification"], options["allowCredentials"]),
                         (TS_HOST, "required", []))
        status, response, data = self.finish(options)
        self.assertEqual(status, 200, data)
        header = response.getheader("Set-Cookie")
        self.assertIn("Max-Age=%d" % storage.SESSION_IDLE, header)
        self.assertIn("Secure", header)
        cookie = self.cookie_of(response)
        status, _, session = self.call("/api/session", cookie=cookie)
        self.assertEqual((session["authenticated"], session["csrf"]), (True, data["csrf"]))
        self.assertEqual(self.call("/api/settings/options", {"block_ads": True}, cookie, data["csrf"])[0], 200)
        item = self.call("/api/passkeys", cookie=cookie)[2]["items"][0]
        self.assertIsNotNone(item["last_used_at"])
        self.restart()
        self.assertTrue(self.call("/api/session", cookie=cookie)[2]["authenticated"])        # survives a restart

    def test_user_handle_is_checked_when_present(self):
        handle = self.store.passkey_user_handle()
        self.auth.user_handle = handle
        self.assertEqual(self.finish(self.login_options())[0], 200)
        self.auth.user_handle = b"x" * 16
        self.assertEqual(self.finish(self.login_options())[0], 401)

    def test_begin_needs_secure_host_origin_and_a_registered_key(self):
        self.assertEqual(self.call("/api/passkeys/login/begin", {}, origin=None)[0], 403)
        self.assertEqual(self.call("/api/passkeys/login/begin", {}, origin="https://evil.example")[0], 403)
        self.assertEqual(self.call("/api/passkeys/login/begin", {}, host="evil.example.com")[0], 403)
        self.assertEqual(self.call("/api/passkeys/login/begin", {}, host="127.0.0.1:%d" % self.port)[0], 400)
        self.assertEqual(self.call("/api/passkeys/login/begin", {}, host="other.tail1234.ts.net")[0], 404)
        self.assertEqual(self.call("/api/passkeys/login/finish", {}, host="127.0.0.1:%d" % self.port)[0], 400)

    def test_challenge_is_single_use(self):
        options = self.login_options()
        fields = self.auth.get(options, self.origin)
        self.assertEqual(self.call("/api/passkeys/login/finish", fields)[0], 200)
        self.assertEqual(self.call("/api/passkeys/login/finish", fields)[0], 401)          # replay
        self.assertEqual(self.call("/api/passkeys/login/finish", fields)[2]["error"], webapp.PASSKEY_LOGIN_FAILED)

    def test_challenge_expires(self):
        self.server.challenges = webauthn.Challenges(ttl=-1)
        self.assertEqual(self.finish(self.login_options())[0], 401)

    def test_login_failures_are_generic_and_rejected(self):
        message = webapp.PASSKEY_LOGIN_FAILED
        other = kit.Authenticator(private=9999)           # same credential id, different key
        unknown = kit.Authenticator(credential_id=b"unknown-credential-0123456789")
        cases = {
            "wrong key": dict(auth=other), "unknown credential": dict(auth=unknown),
            "wrong origin": dict(origin="https://evil.ts.net"), "wrong rp": dict(rp_id="evil.ts.net"),
            "no UV": dict(flags=0x01), "get/create": dict(kind="webauthn.create"),
        }
        for name, kwargs in cases.items():
            with self.subTest(name):
                status, response, data = self.finish(self.login_options(), **kwargs)
                self.assertEqual((status, data["error"]), (401, message))
                self.assertIsNone(self.cookie_of(response))
        status, _, data = self.call("/api/passkeys/login/finish", {"id": "AAAA"})
        self.assertEqual(status, 401)

    def test_sign_count_must_increase(self):
        self.assertEqual(self.finish(self.login_options())[0], 200)                         # counter 1
        self.assertEqual(self.finish(self.login_options())[0], 200)                         # counter 2
        self.assertEqual(self.finish(self.login_options(), counter=2)[0], 401)              # not increasing
        self.assertEqual(self.finish(self.login_options(), counter=1)[0], 401)
        self.assertEqual(self.finish(self.login_options(), counter=3)[0], 200)
        with sqlite3.connect(self.store.path) as db:
            self.assertEqual(db.execute("SELECT sign_count FROM passkeys").fetchone()[0], 3)

    def test_zero_counters_are_allowed(self):
        for _ in range(2):
            self.assertEqual(self.finish(self.login_options(), counter=0)[0], 200)

    def test_deleted_passkey_cannot_log_in(self):
        passkey = self.call("/api/passkeys", cookie=self.cookie)[2]["items"][0]
        self.call("/api/passkeys/delete", {"id": passkey["id"]}, self.cookie, self.csrf)
        self.assertEqual(self.call("/api/passkeys/login/begin", {})[0], 404)
        options = {"challenge": self.server.challenges.issue("get", "", TS_HOST, self.origin), "rpId": TS_HOST}
        self.assertEqual(self.finish(options)[0], 401)

    def test_failed_attempts_are_rate_limited(self):
        other = kit.Authenticator(private=4321)
        for _ in range(10):
            self.assertEqual(self.finish(self.login_options(), auth=other)[0], 401)
        status, _, data = self.call("/api/passkeys/login/begin", {})
        self.assertEqual(status, 429)
        options = {"challenge": self.server.challenges.issue("get", "", TS_HOST, self.origin), "rpId": TS_HOST}
        self.assertEqual(self.finish(options)[0], 429)               # even a good key is locked out meanwhile
        self.assertEqual(self.call("/api/login", {"password": PASSWORD}, host=TS_HOST)[0], 429)

    def test_failures_and_password_failures_share_the_budget(self):
        for _ in range(9):
            self.call("/api/login", {"password": "wrong password!!"})
        self.assertEqual(self.finish(self.login_options(), auth=kit.Authenticator(private=4321))[0], 401)
        self.assertEqual(self.call("/api/passkeys/login/begin", {})[0], 429)

    def test_begin_is_rate_limited(self):
        for _ in range(webapp.PASSKEY_BEGIN_LIMIT):
            self.assertEqual(self.call("/api/passkeys/login/begin", {})[0], 200)
        self.assertEqual(self.call("/api/passkeys/login/begin", {})[0], 429)

    def test_successful_login_clears_failures(self):
        for _ in range(5):
            self.finish(self.login_options(), auth=kit.Authenticator(private=4321))
        self.assertEqual(self.finish(self.login_options())[0], 200)
        self.assertEqual(webapp.FAILED.get("127.0.0.1"), None)

    def test_pending_challenges_are_bounded(self):
        for _ in range(webauthn.CHALLENGE_LIMIT + 20):
            self.server.challenges.issue("get", "", TS_HOST, self.origin)
        self.assertLessEqual(self.server.challenges.pending(), webauthn.CHALLENGE_LIMIT)

    def test_sessions_from_passkeys_can_be_revoked(self):
        status, response, data = self.finish(self.login_options())
        cookie = self.cookie_of(response)
        self.assertEqual(self.call("/api/sessions/revoke-all", {}, cookie, data["csrf"])[0], 200)
        self.assertFalse(self.call("/api/session", cookie=cookie)[2]["authenticated"])
        # the passkey itself still works for a new login
        self.assertEqual(self.finish(self.login_options())[0], 200)


class RsaLoginTests(Fixture):
    def test_rs256_registration_and_login(self):
        with patch.object(webauthn, "MIN_RSA_BITS", 512):
            self.setup_admin()
            cookie, csrf, _ = self.password_login()
            rs = kit.Authenticator("rs256", credential_id=b"rsa-credential-0123456789abcdef")
            self.register(cookie, csrf, rs)
            options = self.login_options()
            status, _, data = self.call("/api/passkeys/login/finish", rs.get(options, "https://" + TS_HOST))
            self.assertEqual(status, 200, data)


class TestHostFlagTests(Fixture):
    def test_localhost_is_refused_unless_the_test_flag_is_set(self):
        self.setup_admin()
        host = "localhost:%d" % self.port
        self.assertEqual(self.call("/api/session", host=host)[2]["passkey"]["available"], False)
        self.server.passkey_test_hosts = frozenset(["localhost"])
        self.assertEqual(self.call("/api/session", host=host)[2]["passkey"]["available"], True)
        cookie, csrf, _ = self.password_login(host=host)
        status, _, options = self.call("/api/passkeys/register/begin", {}, cookie, csrf, host=host)
        self.assertEqual((status, options["rp"]["id"]), (200, "localhost"))
        fields = self.auth.create(options, "http://" + host)
        self.assertEqual(self.call("/api/passkeys/register/finish", fields, cookie, csrf, host=host)[0], 201)
        options = self.call("/api/passkeys/login/begin", {}, host=host)[2]
        status, _, data = self.call("/api/passkeys/login/finish", self.auth.get(options, "http://" + host), host=host)
        self.assertEqual(status, 200)

    def test_production_main_never_enables_it_by_default(self):
        self.assertEqual(getattr(self.server, "passkey_test_hosts", frozenset()), frozenset())
        source = (Path(webapp.__file__)).read_text()
        self.assertIn('choices=("localhost",)', source)
        self.assertNotIn("passkey_test_hosts = frozenset([\"localhost\"])", source)


class RpRulesTests(unittest.TestCase):
    def test_rp_name_rules(self):
        self.assertTrue(webapp.passkey_rp_name_ok("nas.tail1234.ts.net"))
        for name in ("127.0.0.1", "192.168.1.5", "nas", "localhost", "evil.example.com", "ts.net", "a_b.ts.net",
                     "::1", ""):
            self.assertFalse(webapp.passkey_rp_name_ok(name), name)
        with patch.dict(os.environ, {"DOWNLOADER_ALLOWED_HOSTS": "hub.example.org, nas"}):
            self.assertTrue(webapp.passkey_rp_name_ok("hub.example.org"))
            self.assertFalse(webapp.passkey_rp_name_ok("nas"))


if __name__ == "__main__":
    unittest.main()
