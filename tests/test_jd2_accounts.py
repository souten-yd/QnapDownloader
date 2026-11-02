"""Premium account normalization, masking and the service rules (no network)."""
import json
import threading
import unittest

import jd2_accounts as ja
import jd2_control
import jd2_local

NOW = 1_800_000_000_000          # fixed "now" in ms
FUTURE = NOW + 40 * 86400 * 1000


def raw_account(**extra):
    entry = {"UUID": 1234567890123, "hostname": "Rapidgator.net", "username": "alice@example.com",
             "enabled": True, "valid": True, "validUntil": FUTURE,
             "trafficLeft": 120 * 1024 ** 3, "trafficMax": 200 * 1024 ** 3}
    entry.update(extra)
    return entry


class MaskTests(unittest.TestCase):
    def test_masks_email_and_plain_names(self):
        self.assertEqual(ja.mask_username("alice@example.com"), "al••••@example.com")
        self.assertEqual(ja.mask_username("bobsmith"), "bo••••")
        self.assertEqual(ja.mask_username("ab"), "a••••")
        self.assertEqual(ja.mask_username("x"), "••••")
        self.assertEqual(ja.mask_username("a@b.co"), "••••@b.co")
        for bad in (None, "", 5, ["x"]):
            self.assertIsNone(ja.mask_username(bad))

    def test_scrub_text_removes_names_emails_urls_tokens_and_limits_length(self):
        text = ("Login failed for alice@example.com / bobsmith at https://x.example/a?token=abc "
                "key=" + "A" * 40 + "\n\tbad\x00")
        out = ja.scrub_text(text, "bobsmith")
        for needle in ("alice", "bobsmith", "https://", "AAAA", "\n", "\x00"):
            self.assertNotIn(needle, out)
        self.assertLessEqual(len(ja.scrub_text("x" * 500)), 120)
        self.assertIsNone(ja.scrub_text("   "))
        self.assertIsNone(ja.scrub_text(5))


class NormalizeTests(unittest.TestCase):
    def test_public_model_fields_only(self):
        item = ja.normalize_account(raw_account(), NOW)
        self.assertEqual(item, {
            "id": "1234567890123", "host": "rapidgator.net", "display_username": "al••••@example.com",
            "account_type": "premium", "enabled": True, "status": "valid",
            "valid_until": ja.iso_from_millis(FUTURE), "traffic_left_bytes": 120 * 1024 ** 3,
            "traffic_max_bytes": 200 * 1024 ** 3, "traffic_unlimited": False, "error_summary": None})
        self.assertRegex(item["valid_until"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")

    def test_secret_like_extra_fields_never_come_back(self):
        poison = "PWD-s3cr3t-COOKIE-abc123"
        entry = raw_account(password=poison, cookies={"a": poison}, token=poison, apikey=poison,
                            passwordHash=poison, properties={"pass": poison},
                            errorString="login failed for alice@example.com " + poison + poison,
                            errorType="INVALID", valid=False,
                            newField=poison, infoProperties={"x": poison})
        item = ja.normalize_account(entry, NOW)
        self.assertEqual(set(item), {"id", "host", "display_username", "account_type", "enabled",
                                     "status", "valid_until", "traffic_left_bytes", "traffic_max_bytes",
                                     "traffic_unlimited", "error_summary"})
        text = json.dumps(item, ensure_ascii=False)
        for needle in (poison, "alice@example.com", "s3cr3t", "COOKIE"):
            self.assertNotIn(needle, text)
        self.assertEqual(item["status"], "invalid")
        self.assertLessEqual(len(item["error_summary"]), 120)

    def test_status_derivation(self):
        cases = [
            (dict(enabled=False), "disabled"),
            (dict(valid=False, errorType="EXPIRED"), "expired"),
            (dict(valid=False, errorType="TEMP_DISABLED"), "temporarily_unavailable"),
            (dict(valid=False, errorType="INVALID"), "invalid"),
            (dict(valid=False), "invalid"),
            (dict(valid=True), "valid"),
            (dict(valid=None, enabled=True), "unknown"),
            (dict(valid=True, errorType="PLUGIN_ERROR"), "unknown"),
        ]
        for extra, status in cases:
            with self.subTest(extra=extra):
                entry = raw_account(**extra)
                if extra.get("valid", 1) is None:
                    entry.pop("valid")
                self.assertEqual(ja.normalize_account(entry, NOW)["status"], status)
                self.assertIn(status, ja.STATUSES)

    def test_account_type_is_derived_never_guessed(self):
        self.assertEqual(ja.normalize_account(raw_account(accountType="Premium"), NOW)["account_type"], "premium")
        self.assertEqual(ja.normalize_account(raw_account(type="FREE", validUntil=-1), NOW)["account_type"], "free")
        self.assertEqual(ja.normalize_account(raw_account(validUntil=-1), NOW)["account_type"], "unknown")
        self.assertEqual(ja.normalize_account(raw_account(validUntil=NOW - 1), NOW)["account_type"], "unknown")
        self.assertEqual(ja.normalize_account(raw_account(valid=False, errorType="EXPIRED"), NOW)["account_type"], "unknown")

    def test_dates_and_traffic(self):
        item = ja.normalize_account(raw_account(validUntil=-1, trafficLeft=-1, trafficMax=-1), NOW)
        self.assertIsNone(item["valid_until"])
        self.assertIsNone(item["traffic_left_bytes"])
        self.assertIsNone(item["traffic_max_bytes"])
        self.assertTrue(item["traffic_unlimited"])
        item = ja.normalize_account(raw_account(validUntil="x", trafficLeft="5", trafficMax=True), NOW)
        self.assertIsNone(item["valid_until"])
        self.assertIsNone(item["traffic_left_bytes"])
        self.assertIsNone(item["traffic_max_bytes"])
        self.assertFalse(item["traffic_unlimited"])
        self.assertIsNone(ja.iso_from_millis(10 ** 20))
        self.assertEqual(ja.iso_from_millis(86400000), "1970-01-02T00:00:00Z")

    def test_ids_hosts_and_garbage(self):
        self.assertEqual(ja.normalize_account(raw_account(UUID="42"), NOW)["id"], "42")
        self.assertEqual(ja.normalize_account({"uuid": 7, "host": "a.b", "username": "n"}, NOW)["id"], "7")
        for bad in (raw_account(UUID=-1), raw_account(UUID=True), raw_account(UUID="4 2"),
                    raw_account(UUID=None), raw_account(hostname="bad host"), raw_account(hostname=""),
                    raw_account(hostname="x/y"), raw_account(hostname=5), "text", None, 5):
            with self.subTest(bad=bad):
                self.assertIsNone(ja.normalize_account(bad, NOW))
        item = ja.normalize_account(raw_account(enabled="yes", valid="no"), NOW)
        self.assertIsNone(item["enabled"])
        self.assertEqual(item["status"], "unknown")

    def test_list_cap_and_shape(self):
        raw = [raw_account(UUID=i) for i in range(250)] + ["junk"]
        items, truncated = ja.normalize_accounts(raw, NOW)
        self.assertEqual((len(items), truncated), (200, True))
        self.assertEqual(ja.normalize_accounts([raw_account()], NOW)[1], False)
        with self.assertRaises(ValueError):
            ja.normalize_accounts({"a": 1})

    def test_hosts_rapidgator_first_validated_deduped(self):
        out = ja.normalize_hosts(["zippyshare.com", "Rapidgator.net", "rapidgator.net", "alpha.example",
                                  "bad host", "x/y", 5, None, "ünicode.de", "alpha.example"])
        self.assertEqual(out, ["rapidgator.net", "alpha.example", "zippyshare.com"])
        self.assertEqual(ja.normalize_hosts(["b.com"]), ["b.com"])
        with self.assertRaises(ValueError):
            ja.normalize_hosts("rapidgator.net")


class FakeAdapter:
    """Stands in for LocalAdapter: JD2-side state with raw (secret-bearing) objects."""
    engine_running = True

    def __init__(self):
        self.accounts = {}
        self.next_id = 100
        self.calls = []
        self.hosts = ["zz.example", "rapidgator.net", "other.example"]
        self.add_creates = True
        self.remove_works = True
        self.enable_works = True
        self.fail = {}

    def _maybe_fail(self, name):
        if self.fail.get(name):
            raise jd2_local.LocalUnavailable("JD2アカウントAPI（%s）の呼び出しに失敗しました" % name)

    def list_accounts(self):
        self.calls.append(("list",))
        self._maybe_fail("list")
        return [dict(a) for a in self.accounts.values()]

    def list_premium_hosters(self):
        self.calls.append(("hosters",))
        return list(self.hosts)

    def add_account(self, host, username, password):
        self.calls.append(("add", host, username, "<pw>"))
        self._maybe_fail("add")
        if self.add_creates:
            self.next_id += 1
            self.accounts[self.next_id] = {
                "UUID": self.next_id, "hostname": host, "username": username, "enabled": True,
                "valid": None, "password": password, "cookies": "secret-cookie"}

    def refresh_accounts(self, ids):
        self.calls.append(("refresh", tuple(ids)))
        self._maybe_fail("refresh")
        for i in ids:
            if i in self.accounts:
                self.accounts[i].update(valid=True, validUntil=FUTURE, trafficLeft=-1, trafficMax=-1)

    def enable_accounts(self, ids):
        self.calls.append(("enable", tuple(ids)))
        if self.enable_works:
            for i in ids:
                self.accounts[i]["enabled"] = True

    def disable_accounts(self, ids):
        self.calls.append(("disable", tuple(ids)))
        if self.enable_works:
            for i in ids:
                self.accounts[i]["enabled"] = False

    def set_credentials(self, ident, username, password):
        self.calls.append(("set", ident, username, "<pw>"))
        self.accounts[ident].update(username=username, password=password, valid=False, errorType="INVALID",
                                    errorString="Login failed for " + username)

    def remove_accounts(self, ids):
        self.calls.append(("remove", tuple(ids)))
        if self.remove_works:
            for i in ids:
                self.accounts.pop(i, None)


def service_with(adapter=None, **kwargs):
    adapter = adapter or FakeAdapter()
    clock = kwargs.pop("clock", None)
    service = ja.AccountService(clock=clock or (lambda: 1000.0), sleep=lambda s: None)
    return service, jd2_control.JD2Controller(adapter), adapter


class ServiceTests(unittest.TestCase):
    def test_engine_not_running_and_no_adapter_are_503(self):
        service, jd2, adapter = service_with()
        adapter.engine_running = False
        for call in (lambda: service.list(jd2), lambda: service.hosts(jd2),
                     lambda: service.add(jd2, {"host": "rapidgator.net", "username": "u", "password": "p"})):
            with self.assertRaises(ja.ServiceError) as ctx:
                call()
            self.assertEqual(ctx.exception.status, 503)
        self.assertEqual(adapter.calls, [])
        with self.assertRaises(ja.ServiceError) as ctx:
            ja.AccountService().list(jd2_control.JD2Controller(None))
        self.assertEqual(ctx.exception.status, 503)

    def test_list_cache_30s_and_refresh_bypass(self):
        now = [1000.0]
        service, jd2, adapter = service_with(clock=lambda: now[0])
        adapter.accounts[1] = raw_account(UUID=1)
        self.assertEqual(len(service.list(jd2)["items"]), 1)
        adapter.accounts[2] = raw_account(UUID=2)
        now[0] += 29
        self.assertEqual(len(service.list(jd2)["items"]), 1)
        self.assertEqual(len(service.list(jd2, refresh=True)["items"]), 2)
        adapter.accounts[3] = raw_account(UUID=3)
        now[0] += 31
        self.assertEqual(len(service.list(jd2)["items"]), 3)

    def test_hosts_cache_10_minutes_and_order(self):
        now = [1000.0]
        service, jd2, adapter = service_with(clock=lambda: now[0])
        self.assertEqual(service.hosts(jd2)["items"], ["rapidgator.net", "other.example", "zz.example"])
        adapter.hosts = ["new.example"]
        now[0] += 599
        self.assertEqual(len(service.hosts(jd2)["items"]), 3)
        now[0] += 2
        self.assertEqual(service.hosts(jd2)["items"], ["new.example"])

    def test_add_validation(self):
        service, jd2, adapter = service_with()
        good = {"host": "rapidgator.net", "username": "alice", "password": "pw"}
        cases = [dict(good, host="evil.example"), dict(good, host=5), dict(good, host=None),
                 dict(good, username=""), dict(good, username="   "), dict(good, username="a" * 201),
                 dict(good, username="a\nb"), dict(good, username=5),
                 dict(good, password=""), dict(good, password="p" * 501), dict(good, password="a\x00b"),
                 dict(good, password="a\tb"), dict(good, password="\ud800"), dict(good, password=None)]
        for body in cases:
            with self.subTest(body={k: v for k, v in body.items() if k != "password"}):
                with self.assertRaises((ja.ServiceError, ValueError)) as ctx:
                    service.add(jd2, dict(body))
                password = body.get("password")
                if isinstance(password, str) and len(password) > 3 and password.isprintable():
                    self.assertNotIn(password, str(ctx.exception))
        self.assertFalse([c for c in adapter.calls if c[0] == "add"])
        edge = dict(good, username="u" * 200, password="p" * 500)
        self.assertIsNotNone(service.add(jd2, edge)["item"])

    def test_add_returns_masked_refreshed_item_and_never_the_password(self):
        service, jd2, adapter = service_with()
        body = {"host": "RAPIDGATOR.net ", "username": "alice@example.com", "password": "TopSecret1"}
        result = service.add(jd2, body)
        self.assertEqual(body, {})      # best-effort wipe of the caller's dict
        item = result["item"]
        self.assertEqual((item["host"], item["display_username"], item["status"], item["account_type"]),
                         ("rapidgator.net", "al••••@example.com", "valid", "premium"))
        self.assertTrue(result["refreshed"])
        self.assertNotIn("TopSecret1", json.dumps(result))
        self.assertNotIn("secret-cookie", json.dumps(result))
        self.assertIn(("refresh", (adapter.next_id,)), adapter.calls)

    def test_add_when_new_id_cannot_be_found(self):
        service, jd2, adapter = service_with()
        adapter.add_creates = False
        result = service.add(jd2, {"host": "rapidgator.net", "username": "alice", "password": "pw"})
        self.assertIsNone(result["item"])
        self.assertFalse(result["refreshed"])
        self.assertFalse([c for c in adapter.calls if c[0] == "refresh"])

    def test_failed_add_is_not_retried_and_message_has_no_secret(self):
        service, jd2, adapter = service_with()
        adapter.fail["add"] = True
        with self.assertRaises(ja.ServiceError) as ctx:
            service.add(jd2, {"host": "rapidgator.net", "username": "alice", "password": "TopSecret1"})
        self.assertNotIn("TopSecret1", str(ctx.exception))
        self.assertEqual(len([c for c in adapter.calls if c[0] == "add"]), 1)
        self.assertEqual(ctx.exception.status, 503)

    def test_rate_limit_ten_per_ten_minutes_for_add_and_update(self):
        now = [1000.0]
        service, jd2, adapter = service_with(clock=lambda: now[0])
        body = lambda: {"host": "rapidgator.net", "username": "alice", "password": "pw"}
        for _ in range(5):
            service.add(jd2, body())
        ident = str(next(iter(adapter.accounts)))
        for _ in range(5):
            service.update(jd2, {"id": ident, "username": "alice", "password": "pw"})
        with self.assertRaises(ja.ServiceError) as ctx:
            service.add(jd2, body())
        self.assertEqual(ctx.exception.status, 429)
        with self.assertRaises(ja.ServiceError) as ctx:
            service.update(jd2, {"id": ident, "username": "alice", "password": "pw"})
        self.assertEqual(ctx.exception.status, 429)
        # Validation failures and other operations do not consume or bypass it.
        now[0] += 601
        service.add(jd2, body())

    def test_update_reports_the_new_status_without_echoing_credentials(self):
        service, jd2, adapter = service_with()
        service.add(jd2, {"host": "rapidgator.net", "username": "alice", "password": "pw"})
        ident = str(adapter.next_id)
        result = service.update(jd2, {"id": ident, "username": "bobsmith", "password": "WrongPw99"})
        self.assertEqual(result["item"]["status"], "invalid")
        text = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("WrongPw99", text)
        self.assertNotIn("bobsmith", text)
        self.assertIn("bo••••", text)
        with self.assertRaises(ja.ServiceError) as ctx:
            service.update(jd2, {"id": "999", "username": "a", "password": "b"})
        self.assertEqual(ctx.exception.status, 404)

    def test_enable_disable_refresh_validate_ids_and_read_back(self):
        service, jd2, adapter = service_with()
        service.add(jd2, {"host": "rapidgator.net", "username": "alice", "password": "pw"})
        ident = str(adapter.next_id)
        self.assertIs(service.set_enabled(jd2, {"id": ident, "enabled": False})["item"]["enabled"], False)
        self.assertEqual(service.set_enabled(jd2, {"id": ident, "enabled": True})["item"]["enabled"], True)
        for bad in ({"id": ident, "enabled": "false"}, {"id": ident, "enabled": 0}, {"id": ident},
                    {"id": 5, "enabled": True}, {"id": "../1", "enabled": True}, {"id": "9" * 20, "enabled": True}):
            with self.subTest(bad=bad):
                with self.assertRaises((ValueError, ja.ServiceError)):
                    service.set_enabled(jd2, dict(bad))
        adapter.enable_works = False
        with self.assertRaises(ja.ServiceError) as ctx:
            service.set_enabled(jd2, {"id": ident, "enabled": False})
        self.assertEqual(ctx.exception.status, 502)
        self.assertEqual(service.refresh(jd2, {"id": ident})["item"]["status"], "valid")
        with self.assertRaises(ja.ServiceError) as ctx:
            service.refresh(jd2, {"id": "12345"})
        self.assertEqual(ctx.exception.status, 404)
        # Only ids found in JD2's current list ever reach the adapter.
        self.assertFalse([c for c in adapter.calls if c[0] in ("refresh", "enable", "disable") and 12345 in c[1]])

    def test_delete_requires_matching_host_and_reads_back(self):
        service, jd2, adapter = service_with()
        service.add(jd2, {"host": "rapidgator.net", "username": "alice", "password": "pw"})
        ident = str(adapter.next_id)
        for bad in (None, "", "other.example", "rapidgator", 5, "rapidgator.net.evil"):
            with self.subTest(confirm=bad):
                with self.assertRaises(ja.ServiceError) as ctx:
                    service.delete(jd2, {"id": ident, "confirm_host": bad})
                self.assertEqual(ctx.exception.status, 400)
        self.assertFalse([c for c in adapter.calls if c[0] == "remove"])
        self.assertIn(adapter.next_id, adapter.accounts)
        adapter.remove_works = False
        with self.assertRaises(ja.ServiceError) as ctx:
            service.delete(jd2, {"id": ident, "confirm_host": "rapidgator.net"})
        self.assertEqual(ctx.exception.status, 502)
        adapter.remove_works = True
        self.assertEqual(service.delete(jd2, {"id": ident, "confirm_host": " Rapidgator.NET "}),
                         {"deleted": True, "id": ident})
        self.assertNotIn(adapter.next_id, adapter.accounts)
        with self.assertRaises(ja.ServiceError) as ctx:
            service.delete(jd2, {"id": ident, "confirm_host": "rapidgator.net"})
        self.assertEqual(ctx.exception.status, 404)

    def test_unexpected_adapter_errors_become_generic_messages(self):
        service, jd2, adapter = service_with()
        def boom():
            raise KeyError("password=TopSecret1")
        adapter.list_accounts = boom
        with self.assertRaises(ja.ServiceError) as ctx:
            service.list(jd2)
        self.assertNotIn("TopSecret1", str(ctx.exception))
        self.assertTrue(ctx.exception.__suppress_context__)


if __name__ == "__main__":
    unittest.main()
