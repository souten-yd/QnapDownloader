"""Self-update regressions: no external GitHub/QNAP requests, no installer execution."""
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

import self_update as update
import storage
import updater


class QpkgSelfUpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = storage.Store(self.root)
        self.addCleanup(self.store.close)
        self.manager = update.UpdateManager(self.root, self.root, "0.1.2", "x86_64")
        self.job = self.manager.directory / ("a" * 32)
        self.job.mkdir()
        update.write_json(self.job / "request.json", {
            "id": self.job.name, "current": "0.1.2", "target": "v0.1.3",
            "arch": "x86_64", "data_root": str(self.root),
            "install_root": str(self.root), "port": 17864})
        update.write_json(self.manager.state_file, {
            "id": self.job.name, "phase": "queued", "target": "v0.1.3"})

    def test_sqlite_online_backup_captures_settings(self):
        self.store.set_download_dir("/share/Download")
        (self.root / "config").mkdir()
        (self.root / "config" / "example.txt").write_text("persistent config")
        path = Path(update.backup_data(self.root, self.job))
        with sqlite3.connect(path / "downloader.sqlite3") as db:
            selected = json.loads(db.execute(
                "SELECT value FROM settings WHERE key='download_dir'").fetchone()[0])
        self.assertEqual(selected, "/share/Download")
        self.assertEqual((path / "config" / "example.txt").read_text(),
                         "persistent config")

    def test_missing_sqlite_db_prevents_backup(self):
        with patch.object(update, "snapshot", side_effect=ValueError("corrupt")) as snapshot:
            with self.assertRaisesRegex(ValueError, "corrupt"):
                update.backup_data(self.root, self.job)
            snapshot.assert_called_once()

    def test_corrupt_download_never_stops_nas_service(self):
        good_payload = b"#!/bin/sh\nexit 0\n"
        asset = {
            "version": "v0.1.3", "name": "QnapDownloader_0.1.3_x86_64.qpkg",
            "size": len(good_payload),
            "sha256": hashlib.sha256(good_payload).hexdigest(),
            "url": "https://github.com/souten-yd/QnapDownloader/releases/download/"
                   "v0.1.3/QnapDownloader_0.1.3_x86_64.qpkg"}
        fd = self.manager.lock()
        try:
            with patch.object(update, "check_environment"), \
                 patch.object(updater, "latest", return_value=asset), \
                 patch.object(updater.urllib.request, "urlopen",
                              return_value=io.BytesIO(b"invalid")), \
                 patch.object(update.subprocess, "run") as stop, \
                 patch.object(update.subprocess, "Popen") as install:
                code = update.run_job(self.job, fd)
            self.assertEqual(code, 1)
            stop.assert_not_called()
            install.assert_not_called()
            self.assertEqual(update.read_json(self.manager.state_file)["phase"], "failed")
        finally:
            os.close(fd)

    def test_log_tail_is_bounded_and_never_uses_arbitrary_path(self):
        logfile = self.job / "update.log"
        logfile.write_text("A" * 18000, encoding="utf-8")
        self.assertEqual(len(self.manager.log_tail()), 16000)
        update.write_json(self.manager.state_file, {
            "id": "../../etc/shadow", "phase": "failed"})
        self.assertEqual(self.manager.log_tail(), "")

    def test_update_lock_prevents_parallel_jobs(self):
        fd = self.manager.lock()
        try:
            with self.assertRaises(update.UpdateConflict):
                self.manager.lock()
        finally:
            os.close(fd)

    def test_lost_worker_is_reported_failed_not_retried(self):
        with patch.object(update, "check_environment"), \
             patch.object(update.subprocess, "Popen") as popen:
            status = self.manager.status()
            self.assertEqual(status["phase"], "failed")
            self.assertFalse(status["busy"])
            popen.assert_not_called()

    def test_reject_non_qpkg_environment_and_downgrade(self):
        with patch.object(update.os, "geteuid", return_value=1000):
            with self.assertRaises(ValueError):
                self.manager.apply("v0.1.3")
        for version in ("v0.1.2", "v0.0.9", "javascript:123", None):
            with self.subTest(version=version), self.assertRaises(ValueError):
                self.manager.apply(version)

    def test_post_install_health_requires_matching_qpkg_and_web(self):
        good = io.BytesIO(b'{"version":"0.1.3","settings_revision":1}')
        client = Mock()
        client.open.return_value = good
        with patch.object(update, "getcfg", return_value="0.1.3"), \
             patch.object(update.urllib.request, "build_opener", return_value=client):
            update.wait_for_version("v0.1.3", 17864)
        self.assertEqual(client.open.call_args.args[0],
                         "http://127.0.0.1:17864/api/health")


if __name__ == "__main__":
    unittest.main()
