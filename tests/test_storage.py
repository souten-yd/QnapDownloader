import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import storage
import webapp


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.s = storage.Store(self.root)
        self.addCleanup(self.s.close)

    def test_defaults_are_safe_and_preserved_on_reopen(self):
        setting = self.s.settings()
        for field in ("auto_start", "auto_extract", "create_package_folders", "auto_delete"):
            self.assertIs(setting[field], False)
        target = self.root / "mydownloads"
        target.mkdir()
        self.s.set_download_dir(str(target))
        self.assertEqual(self.s.settings()["download_dir"], str(target))
        self.assertGreater(self.s.revision(), 0)
        again = storage.Store(self.root)
        try:
            self.assertEqual(again.settings()["download_dir"], str(target))
        finally:
            again.close()

    def test_delete_link_only_staged_and_delete_bookmark(self):
        keep = self.s.add_link("https://example.com/keep")["id"]
        gone = self.s.add_link("https://example.com/gone")["id"]
        before = self.s.revision()
        self.s.delete_link(gone)
        self.assertEqual([x["id"] for x in self.s.links()], [keep])
        self.assertGreater(self.s.revision(), before)
        self.s.begin_send(keep)
        for state in ("sending",):
            with self.assertRaises(ValueError):
                self.s.delete_link(keep)
        self.s.finish_send(keep, "uncertain")
        with self.assertRaises(ValueError):
            self.s.delete_link(keep)
        with self.assertRaises(ValueError):
            self.s.delete_link(gone)
        with self.assertRaises(ValueError):
            self.s.delete_link("1")
        # A URL can be added again after deleting its staged row.
        self.assertEqual(self.s.add_link("https://example.com/gone")["state"], "staged")
        mark = self.s.add_bookmark("Ex", "https://example.com/")
        self.s.delete_bookmark(mark)
        self.assertEqual(self.s.bookmarks(), [])
        with self.assertRaises(ValueError):
            self.s.delete_bookmark(mark)
        with self.assertRaises(ValueError):
            self.s.delete_bookmark(True)

    def test_short_lived_sqlite_connections_are_closed(self):
        with self.s.connect() as db:
            self.assertEqual(db.execute("PRAGMA quick_check").fetchone()[0], "ok")
        with self.assertRaises(sqlite3.ProgrammingError):
            db.execute("SELECT 1")

    def test_link_intents_are_inert_deduplicated(self):
        a = self.s.add_link("https://rapidgator.net/file/abc")
        b = self.s.add_link("https://rapidgator.net/file/abc")
        self.assertEqual(a, b)
        self.assertEqual(len(self.s.links()), 1)
        self.assertEqual(a["state"], "staged")
        for invalid in ("http://example.com", "file:///etc/passwd",
                        "https://user:pass@example.com/", "javascript:alert(1)", ""):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.s.add_link(invalid)

    def test_bookmark_duplicate_is_idempotent(self):
        a = self.s.add_bookmark("Example", "https://example.com")
        b = self.s.add_bookmark("Example again", "https://example.com")
        self.assertEqual(a, b)
        self.assertEqual(len(self.s.bookmarks()), 1)

    def test_admin_password_has_hash_and_no_plaintext(self):
        self.s.create_admin("correct horse staple 123")
        self.assertTrue(self.s.check_password("correct horse staple 123"))
        self.assertFalse(self.s.check_password("wrong"))
        with sqlite3.connect(self.s.path) as db:
            salt, secret = db.execute("SELECT salt,password_hash FROM accounts").fetchone()
            self.assertEqual(len(salt), 32)
            self.assertNotIn("correct horse", secret)
        with self.assertRaises(ValueError):
            self.s.create_admin("different password")

    def test_first_password_ownership_is_atomic_without_token(self):
        import concurrent.futures
        candidates = ["correct horse battery 123", "another long password 456"]
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.s.create_admin, x) for x in candidates]
            results = []
            for future in futures:
                try:
                    future.result()
                    results.append(True)
                except ValueError:
                    results.append(False)
        self.assertEqual(results.count(True), 1)
        self.assertEqual(sum(self.s.check_password(x) for x in candidates), 1)
        self.assertTrue(self.s.has_admin())

    def test_reject_missing_database_after_initialization(self):
        self.s.close()
        for p in self.root.joinpath("data").glob("downloader.sqlite3*"):
            p.unlink()
        with self.assertRaisesRegex(RuntimeError, "消失"):
            storage.Store(self.root)

    def test_shared_folder_only_and_no_symlink_escape(self):
        allowed = self.root / "allowed"
        allowed.mkdir()
        inside = allowed / "ok"
        inside.mkdir()
        outside = self.root / "private"
        outside.mkdir()
        (allowed / "escape").symlink_to(outside, target_is_directory=True)
        self.assertEqual(webapp.checked_directory(str(inside), [allowed]), inside)
        for invalid in (str(outside), str(allowed / "escape"), "../relative", "/etc"):
            with self.subTest(invalid=invalid), self.assertRaises((ValueError, OSError)):
                webapp.checked_directory(invalid, [allowed])


class ShareTokenStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.s = storage.Store(self.root)
        self.addCleanup(self.s.close)

    def test_token_is_returned_once_and_only_hash_is_stored(self):
        made = self.s.create_share_token("iPhone")
        self.assertEqual(set(made), {"id", "name", "token"})
        self.assertNotIn("token", self.s.share_tokens()[0])
        with self.s.connect() as db:
            rows = db.execute("SELECT * FROM share_tokens").fetchall()
        self.assertNotIn(made["token"], repr(rows))
        self.assertEqual(self.s.verify_share_token(made["token"]), made["id"])
        self.assertIsNone(self.s.verify_share_token(made["token"] + "x"))
        self.assertIsNone(self.s.verify_share_token(""))
        self.assertIsNone(self.s.verify_share_token(None))
        self.assertIsNone(self.s.verify_share_token("a" * 500))

    def test_name_and_count_limits_and_revocation(self):
        for bad in ("", "   ", "x" * 41, None, 3):
            with self.assertRaises(ValueError):
                self.s.create_share_token(bad)
        self.s.create_share_token("x" * 40)
        tokens = [self.s.create_share_token("d%d" % i) for i in range(9)]
        with self.assertRaises(ValueError):
            self.s.create_share_token("eleventh")
        self.s.delete_share_token(tokens[0]["id"])
        self.assertIsNone(self.s.verify_share_token(tokens[0]["token"]))
        self.assertIsNotNone(self.s.verify_share_token(tokens[1]["token"]))
        with self.assertRaises(ValueError):
            self.s.delete_share_token(tokens[0]["id"])
        with self.assertRaises(ValueError):
            self.s.delete_share_token(True)

    def test_last_used_is_written_at_most_once_per_hour(self):
        made = self.s.create_share_token("iPhone")
        self.assertIsNone(self.s.share_tokens()[0]["last_used_at"])
        self.s.verify_share_token(made["token"], now=10000.0)
        self.assertEqual(self.s.share_tokens()[0]["last_used_at"], 10000.0)
        self.s.verify_share_token(made["token"], now=10000.0 + 3599)
        self.assertEqual(self.s.share_tokens()[0]["last_used_at"], 10000.0)
        self.s.verify_share_token(made["token"], now=10000.0 + 3600)
        self.assertEqual(self.s.share_tokens()[0]["last_used_at"], 13600.0)
        # A failed attempt writes nothing.
        before = self.s.share_tokens()
        self.assertIsNone(self.s.verify_share_token("nope", now=99999.0))
        self.assertEqual(self.s.share_tokens(), before)

    def test_existing_database_gets_share_table_on_upgrade(self):
        with self.s.connect() as db:
            db.execute("DROP TABLE share_tokens")
        again = storage.Store(self.root)
        try:
            self.assertEqual(again.share_tokens(), [])
            self.assertTrue(again.create_share_token("iPhone")["token"])
        finally:
            again.close()


class SiteRuleTests(unittest.TestCase):
    """v0.1.24: site_rules table, options, migration and write-on-change."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.s = storage.Store(self.root)
        self.addCleanup(self.s.close)

    def revision(self):
        return self.s.revision()

    def test_defaults_include_the_new_options_on(self):
        settings = self.s.settings()
        self.assertIs(settings["send_autostart"], True)
        self.assertIs(settings["block_ads"], True)

    def test_options_persist_and_are_written_only_on_change(self):
        before = self.revision()
        self.s.set_option("send_autostart", True)            # already the default: no write
        self.assertEqual(self.revision(), before)
        self.s.set_option("send_autostart", False)
        self.assertEqual(self.revision(), before + 1)
        self.s.set_option("send_autostart", False)
        self.assertEqual(self.revision(), before + 1)
        again = storage.Store(self.root)
        try:
            self.assertIs(again.settings()["send_autostart"], False)
            self.assertIs(again.settings()["block_ads"], True)
        finally:
            again.close()

    def test_options_reject_unknown_keys_and_non_booleans(self):
        for key, value in (("download_dir", True), ("nope", True), ("send_autostart", "yes"),
                           ("send_autostart", 1), ("block_ads", None)):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                self.s.set_option(key, value)

    def test_rules_roundtrip_with_defaults_and_partial_updates(self):
        rule = self.s.set_site_rule("Example.COM.", auto_send=True)
        self.assertEqual(rule, {"host": "example.com", "auto_send": True, "popup": "ask", "block_ads": True})
        self.s.set_site_rule("example.com", popup="block")
        self.s.set_site_rule("example.com", block_ads=False)
        self.assertEqual(self.s.site_rules(), [
            {"host": "example.com", "auto_send": True, "popup": "block", "block_ads": False}])
        again = storage.Store(self.root)
        try:
            self.assertEqual(again.site_rules()[0]["popup"], "block")
        finally:
            again.close()

    def test_unchanged_rule_writes_nothing(self):
        self.s.set_site_rule("example.com", auto_send=True, popup="allow")
        before = self.revision()
        for _ in range(3):
            self.s.set_site_rule("example.com", auto_send=True, popup="allow", block_ads=True)
            self.s.set_site_rule("example.com")
        self.assertEqual(self.revision(), before)
        self.s.set_site_rule("example.com", popup="ask")
        self.assertEqual(self.revision(), before + 1)

    def test_host_validation(self):
        good = ("example.com", "a.b.example.co.jp", "xn--r8jz45g.jp", "EXAMPLE.org")
        for host in good:
            with self.subTest(host=host):
                self.s.valid_host(host)
        for bad in ("", "localhost", "example", "http://example.com", "example.com/path", "example.com:8080",
                    "192.168.0.1", "1.2.3.4", "exa mple.com", "-a.example.com", "a..example.com", "ex_ample.com",
                    "example.c", "example.123", None, 5, "x" * 300 + ".com", "a@b.com"):
            with self.subTest(host=bad), self.assertRaises(ValueError):
                self.s.set_site_rule(bad, auto_send=True)
        self.assertEqual(self.s.site_rules(), [])

    def test_field_validation(self):
        for kwargs in ({"auto_send": 1}, {"auto_send": "true"}, {"block_ads": 0}, {"popup": "maybe"},
                       {"popup": ""}, {"popup": None, "auto_send": None, "block_ads": "x"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.s.set_site_rule("example.com", **kwargs)
        self.assertEqual(self.s.site_rules(), [])

    def test_popup_check_constraint_in_the_database(self):
        with self.assertRaises(sqlite3.IntegrityError), self.s.connect() as db:
            db.execute("INSERT INTO site_rules(host,auto_send,popup,block_ads) VALUES('x.example',0,'maybe',1)")

    def test_at_most_200_rules(self):
        for i in range(storage.MAX_SITE_RULES):
            self.s.set_site_rule("h%d.example.com" % i, auto_send=True)
        with self.assertRaises(ValueError):
            self.s.set_site_rule("one-too-many.example.com", auto_send=True)
        before = self.revision()
        self.s.set_site_rule("h5.example.com", popup="block")       # updating an existing one is fine
        self.assertEqual(len(self.s.site_rules()), storage.MAX_SITE_RULES)
        self.assertEqual(self.revision(), before + 1)

    def test_delete(self):
        self.s.set_site_rule("example.com", auto_send=True)
        self.s.delete_site_rule("example.com")
        self.assertEqual(self.s.site_rules(), [])
        with self.assertRaises(ValueError):
            self.s.delete_site_rule("example.com")
        with self.assertRaises(ValueError):
            self.s.delete_site_rule("not a host")

    def test_lookup_uses_the_host_then_parent_domains_but_never_the_tld(self):
        self.s.set_site_rule("example.com", auto_send=True)
        self.s.set_site_rule("deep.example.com", popup="block")
        self.assertTrue(self.s.site_rule_for("example.com")["auto_send"])
        self.assertTrue(self.s.site_rule_for("www.example.com")["auto_send"])
        self.assertEqual(self.s.site_rule_for("a.deep.example.com")["popup"], "block")
        self.assertIsNone(self.s.site_rule_for("example.org"))
        self.assertIsNone(self.s.site_rule_for("com"))
        self.assertIsNone(self.s.site_rule_for(""))
        self.assertIsNone(self.s.site_rule_for(None))
        self.assertIsNone(self.s.site_rule_for("notexample.com"))

    def test_cache_follows_writes_from_the_same_store(self):
        self.assertIsNone(self.s.site_rule_for("example.com"))
        self.s.set_site_rule("example.com", auto_send=True)
        self.assertIsNotNone(self.s.site_rule_for("example.com"))
        self.s.delete_site_rule("example.com")
        self.assertIsNone(self.s.site_rule_for("example.com"))

    def test_link_state_lookup(self):
        self.assertIsNone(self.s.link_state("https://example.com/x"))
        self.s.add_link("https://example.com/x")
        self.assertEqual(self.s.link_state("https://example.com/x"), "staged")

    def test_upgrade_from_a_database_without_the_table(self):
        with self.s.connect() as db:
            db.execute("DROP TABLE site_rules")
        again = storage.Store(self.root)
        try:
            self.assertEqual(again.site_rules(), [])
            again.set_site_rule("example.com", popup="allow")
        finally:
            again.close()

    def test_upgrade_adds_the_block_ads_column_and_keeps_rows(self):
        with self.s.connect() as db:
            db.execute("DROP TABLE site_rules")
            db.execute("CREATE TABLE site_rules(host TEXT PRIMARY KEY, auto_send INTEGER NOT NULL DEFAULT 0, "
                       "popup TEXT NOT NULL DEFAULT 'ask' CHECK(popup IN ('ask','allow','block')))")
            db.execute("INSERT INTO site_rules VALUES('old.example.com',1,'block')")
        again = storage.Store(self.root)
        try:
            self.assertEqual(again.site_rules(), [
                {"host": "old.example.com", "auto_send": True, "popup": "block", "block_ads": True}])
            again.set_site_rule("old.example.com", block_ads=False)
            self.assertIs(again.site_rules()[0]["block_ads"], False)
        finally:
            again.close()
        # opening it a third time (column exists) must not fail
        third = storage.Store(self.root)
        third.close()


if __name__ == "__main__":
    unittest.main()
