import hashlib
import io
import json
import os
import signal
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import engine_components as ec
import engine_supervisor as es
import jd2_local
from test_engine_components import FakeOpener, FakeResponse, make_tar


class NoContainerStation:
    """Runtime stand-in: Container Station missing (native fallback)."""

    def version(self):
        return None

    def available(self):
        return False


class Store:
    def __init__(self, folder):
        self.folder = folder

    def settings(self):
        return {"download_dir": self.folder}


class HeapAndLogTests(unittest.TestCase):
    def test_heap_selection(self):
        gib = 1024 * 1024
        self.assertEqual(es.select_heap(2 * gib), 256)
        self.assertEqual(es.select_heap(int(2.99 * gib)), 256)
        self.assertEqual(es.select_heap(3 * gib), 384)
        self.assertEqual(es.select_heap(int(5.9 * gib)), 384)
        self.assertEqual(es.select_heap(6 * gib), 512)
        self.assertEqual(es.select_heap(16 * gib), 512)

    def test_meminfo_parse(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "meminfo"
            path.write_text("MemTotal:        4046628 kB\nMemFree: 1 kB\n")
            self.assertEqual(es.read_mem_total_kib(str(path)), 4046628)
            self.assertIsNone(es.read_mem_total_kib(str(Path(temp) / "none")))

    def test_scrub_secrets(self):
        for text in ("password=hunter2 ok", 'login {"password":"hunter2"}',
                     "Cookie: SID=abc123", "Authorization: Bearer abc.def", "token: xyz"):
            with self.subTest(text=text):
                clean = es.scrub(text)
                for secret in ("hunter2", "abc123", "abc.def", "xyz"):
                    self.assertNotIn(secret, clean)
        self.assertEqual(es.scrub("Starting JD2 core"), "Starting JD2 core")

    def test_ring_is_bounded_and_scrubbed(self):
        with tempfile.TemporaryDirectory() as temp:
            manager = es.EngineManager(temp, Store(temp))
            for i in range(450):
                manager.log("line %d password=secret%d" % (i, i))
            lines = manager.log_lines()
            self.assertEqual(len(lines), 300)
            self.assertEqual(lines[-1], "line 449 password=***")
            self.assertFalse(any("secret" in line for line in lines))


def stat_line(pid, ticks, state="S"):
    rest = [state, "1", str(pid), str(pid), "0", "-1", "0", "0", "0", "0", "0", "0", "0",
            "0", "0", "20", "0", "1", "0", str(ticks), "0", "0"]
    return "%d (java) %s" % (pid, " ".join(rest))


class SupervisorIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.proc = self.root / "proc"
        self.proc.mkdir()
        self.kills = []
        self.manager = es.EngineManager(self.root, Store(str(self.root)), proc_root=str(self.proc),
                                        kill=self.fake_kill, sleep=lambda s: None)
        self.argv = [str(self.manager.java_bin), "-jar", str(self.manager.jar)]

    def fake_kill(self, pid, sig):
        self.kills.append((pid, sig))
        if sig in (signal.SIGTERM, signal.SIGKILL):
            self.remove(pid)

    def make(self, pid, ticks, argv):
        folder = self.proc / str(pid)
        folder.mkdir(exist_ok=True)
        (folder / "stat").write_text(stat_line(pid, ticks))
        (folder / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")

    def remove(self, pid):
        import shutil
        shutil.rmtree(str(self.proc / str(pid)), ignore_errors=True)

    def adopt(self, pid, ticks):
        self.manager._pid, self.manager._ticks = pid, ticks

    def test_matching_process_gets_sigterm(self):
        self.make(4242, 5555, self.argv)
        self.adopt(4242, 5555)
        self.assertTrue(self.manager.is_ours_running())
        result = self.manager.stop()
        self.assertEqual(self.kills, [(4242, signal.SIGTERM)])
        self.assertTrue(result["signalled"])
        self.assertFalse(self.manager.is_ours_running())

    def test_start_time_mismatch_refuses_to_signal(self):
        self.make(4242, 9999, self.argv)  # pid reused by another process
        self.adopt(4242, 5555)
        result = self.manager.stop()
        self.assertEqual(self.kills, [])
        self.assertFalse(result["signalled"])
        self.assertTrue(any("シグナルを送りません" in l for l in self.manager.log_lines()))
        self.assertTrue((self.proc / "4242").exists())

    def test_cmdline_mismatch_refuses_to_signal(self):
        for argv in (["/usr/bin/python3", "evil.py"],
                     [str(self.manager.java_bin), "-jar", "/tmp/other.jar"],
                     ["/usr/bin/java", "-jar", str(self.manager.jar)]):
            with self.subTest(argv=argv):
                self.make(4242, 5555, argv)
                self.adopt(4242, 5555)
                self.manager.stop()
                self.assertEqual(self.kills, [])

    def test_zombie_is_not_ours(self):
        self.make(4242, 5555, self.argv)
        (self.proc / "4242" / "stat").write_text(stat_line(4242, 5555, state="Z"))
        self.adopt(4242, 5555)
        self.assertFalse(self.manager.is_ours_running())

    def test_sigkill_after_term_timeout(self):
        self.make(4242, 5555, self.argv)
        self.adopt(4242, 5555)
        clock = {"t": 0.0}

        def sleep(seconds):
            clock["t"] += 20.0
        self.manager._sleep = sleep
        self.manager._now = lambda: clock["t"]

        def stubborn(pid, sig):
            self.kills.append((pid, sig))
            if sig == signal.SIGKILL:
                self.remove(pid)
        self.manager._kill = stubborn
        self.manager.stop()
        self.assertEqual(self.kills, [(4242, signal.SIGTERM), (4242, signal.SIGKILL)])

    def test_connection_target_requires_running_engine(self):
        with self.assertRaises(jd2_local.LocalUnavailable) as ctx:
            self.manager.connection_target()
        self.assertIn("エンジン未起動/未導入", str(ctx.exception))
        self.make(4242, 5555, self.argv)
        self.adopt(4242, 5555)
        with patch.object(jd2_local, "verify_listener", return_value=True) as verify:
            self.assertEqual(self.manager.connection_target(), 3128)
        verify.assert_called_once_with(3128, 4242, str(self.proc))


class StateTests(unittest.TestCase):
    def test_state_written_only_on_change(self):
        with tempfile.TemporaryDirectory() as temp:
            manager = es.EngineManager(temp, Store(temp))
            with patch.object(es.durability, "write_json", wraps=es.durability.write_json) as write:
                manager.set_autostart(True)
                manager.set_autostart(True)
                manager.note_preset_applied()
                manager.note_preset_applied()
                self.assertEqual(write.call_count, 2)
            saved = json.loads((Path(temp) / "engine" / "state.json").read_text())
            self.assertTrue(saved["autostart"])
            self.assertTrue(saved["preset_applied"])
            again = es.EngineManager(temp, Store(temp))
            self.assertTrue(again.autostart)
            with self.assertRaises(ValueError):
                manager.set_autostart("yes")


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.folder = self.root / "dl"
        self.folder.mkdir()
        tar_path = self.root / "jre.tar.gz"
        make_tar(str(tar_path), [("jdk-t-jre/", "dir", None), ("jdk-t-jre/bin/", "dir", None),
                                 ("jdk-t-jre/bin/java", "file", b"#!/bin/sh\n")])
        self.java_data = tar_path.read_bytes()
        self.jar_data = b"PK-fake-jar" * 50
        self.manifest = {
            "java": {"label": "j", "version": "21.0.1", "url": "https://x.test/java.tar.gz",
                     "sha256": hashlib.sha256(self.java_data).hexdigest(),
                     "size": len(self.java_data), "filename": "java.tar.gz", "dirname": "jdk-t-jre"},
            "jd2": {"label": "d", "version": "installer", "url": "https://x.test/JDownloader.jar",
                    "sha256": hashlib.sha256(self.jar_data).hexdigest(),
                    "size": len(self.jar_data), "filename": "JDownloader.jar"},
        }
        self.fetched = []
        self.data = self.root / "data"
        self.data.mkdir()
        self.runs = []

    def opener(self):
        outer = self

        class Opener:
            def open(self, request, timeout=None):
                outer.fetched.append(request.full_url)
                data = outer.java_data if "java" in request.full_url else outer.jar_data
                return FakeResponse(data, url=request.full_url, length=len(data))
        return Opener()

    def run_fake(self, command, **kwargs):
        self.runs.append(command)
        return SimpleNamespace(returncode=0, stdout=b'openjdk version "21.0.1"\n')

    def manager(self, **extra):
        manager = es.EngineManager(self.data, Store(str(self.folder)), manifest=self.manifest, runtime=NoContainerStation(),
                                   opener=self.opener(), run=self.run_fake, machine="x86_64",
                                   disk_usage=lambda p: SimpleNamespace(free=10 ** 12),
                                   meminfo_path=str(self.root / "nomem"), **extra)
        done = lambda: ("done", "fake")
        for name in ("_s_first_run", "_s_resident", "_s_verify", "_s_preset"):
            setattr(manager, name, done)
        return manager

    def statuses(self, manager):
        return {s["id"]: s["status"] for s in manager.status()["steps"]}

    def test_full_setup_then_resume_skips_verified_artifacts(self):
        manager = self.manager()
        manager.run_setup("native")
        status = manager.status()
        self.assertIsNone(status["error"], status)
        self.assertEqual(len(self.fetched), 2)
        self.assertTrue(status["installed"])
        self.assertTrue(manager.autostart)
        self.assertEqual(self.statuses(manager)["done"], "done")
        self.assertTrue(manager.java_bin.is_file())
        self.assertTrue(manager.jar.is_file())
        cfg = json.loads((manager.jd2_dir / "cfg" / es.API_CONFIG_NAME).read_text())
        self.assertEqual(cfg, {"deprecatedapienabled": True, "deprecatedapiport": 3128,
                               "deprecatedapilocalhostonly": True})
        self.assertEqual(manager.heap_mb if hasattr(manager, "heap_mb") else manager._heap_mb, 256)
        self.assertEqual(sorted(p.name for p in manager.download_dir.iterdir()), [])
        self.assertEqual(self.runs, [[str(manager.java_bin), "-version"]])
        # Resume with a fresh manager reading state.json.
        again = self.manager()
        again.run_setup("native")
        self.assertEqual(len(self.fetched), 2)
        steps = self.statuses(again)
        self.assertEqual(steps["java_fetch"], "skipped")
        self.assertEqual(steps["jd2_fetch"], "skipped")
        self.assertEqual(steps["api_config"], "skipped")
        self.assertEqual(steps["done"], "done")

    def test_resume_after_partial_failure_reuses_verified_download(self):
        manager = self.manager()
        calls = {"n": 0}
        real = manager._java_version_check

        def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise es.SetupFailure("java -version の結果が想定と一致しません")
            real()
        manager._java_version_check = flaky
        manager.run_setup("native")
        self.assertEqual(self.statuses(manager)["java_extract"], "failed")
        self.assertFalse(manager.installed)
        self.assertEqual(len(self.fetched), 1)
        manager.run_setup("native")
        self.assertTrue(manager.installed)
        self.assertEqual(len(self.fetched), 2)  # java once + jd2 once, no re-download

    def test_hash_mismatch_fails_at_fetch_step_with_message(self):
        self.java_data_served = self.java_data
        self.manifest["java"]["sha256"] = "0" * 64
        manager = self.manager()
        manager.run_setup("native")
        status = manager.status()
        self.assertEqual(self.statuses(manager)["java_fetch"], "failed")
        self.assertIn("公式の値と一致しません", status["error"])
        self.assertFalse(status["installed"])
        self.assertEqual(status["phase"], "failed")
        self.assertFalse(manager.java_dir.exists())

    def test_existing_api_config_is_never_overwritten(self):
        manager = self.manager()
        cfg = manager.jd2_dir / "cfg"
        cfg.mkdir(parents=True)
        (cfg / es.API_CONFIG_NAME).write_text('{"deprecatedapienabled": false}')
        manager.run_setup("native")
        self.assertEqual(self.statuses(manager)["api_config"], "failed")
        self.assertEqual((cfg / es.API_CONFIG_NAME).read_text(), '{"deprecatedapienabled": false}')
        self.assertFalse(manager.installed)

    def test_precheck_requires_folder_platform_and_space(self):
        manager = self.manager()
        manager.store = Store("")
        manager.run_setup("native")
        self.assertIn("保存先", manager.status()["error"])
        manager = self.manager(); manager._machine = "aarch64"
        manager.run_setup("native")
        self.assertIn("x86_64", manager.status()["error"])
        manager = self.manager(); manager._disk_usage = lambda p: SimpleNamespace(free=1)
        manager.run_setup("native")
        self.assertIn("空き容量", manager.status()["error"])
        self.assertEqual(self.fetched, [])

    def test_concurrent_setup_refused(self):
        manager = self.manager()
        manager._setup_active = True
        with self.assertRaises(es.SetupBusy):
            manager.start_setup()
        with self.assertRaises(es.SetupBusy):
            manager.run_setup("native")
        self.assertEqual(manager.phase(), "setup")

    def test_paths_never_come_from_requests(self):
        manager = self.manager()
        command = manager.command()
        self.assertEqual(command[0], str(self.data / "engine" / "java" / "jdk-t-jre" / "bin" / "java"))
        self.assertIn(str(self.data / "engine" / "jd2" / "JDownloader.jar"), command)
        self.assertEqual(command[-1], "-norestart")
        for flag in ("-XX:+UseSerialGC", "-Djava.awt.headless=true"):
            self.assertIn(flag, command)
        import inspect
        for name in ("start", "stop", "start_setup", "set_autostart"):
            self.assertEqual(list(inspect.signature(getattr(es.EngineManager, name)).parameters)[:1], ["self"])
            self.assertLessEqual(len(inspect.signature(getattr(es.EngineManager, name)).parameters), 2)


class StaticTests(unittest.TestCase):
    def test_no_myjd_left_in_code(self):
        root = Path(__file__).resolve().parents[1]
        targets = list(root.glob("*.py")) + list((root / "web").iterdir()) + \
            list((root / "qpkg" / "shared").iterdir()) + list((root / "scripts").iterdir())
        for path in targets:
            if path.name == "engine_supervisor.py":
                # v0.1.16: the only mention is the MyJDownloader auto-connect switch-off
                # (headless JD2 otherwise asks for logins on a missing console).
                continue
            if path.suffix in (".py", ".js", ".html", ".css", ".sh", ".svg") or path.name in ("python3-path", "downloader-diagnose"):
                with self.subTest(file=path.name):
                    self.assertNotIn("myjd", path.read_text(encoding="utf-8", errors="ignore").lower())
        self.assertFalse((root / "jd2_myjd.py").exists())
        self.assertFalse((root / "requirements-jd2-build.txt").exists())


if __name__ == "__main__":
    unittest.main()
