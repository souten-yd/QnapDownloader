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


if __name__ == "__main__":
    unittest.main()
