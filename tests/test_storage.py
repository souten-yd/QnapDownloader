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

    def test_reject_missing_database_after_initialization(self):
        self.s.close()
        self.s = None
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


if __name__ == "__main__":
    unittest.main()
