import hashlib
import json
import os
import shutil
import signal
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import container_runtime as cr
import engine_supervisor as es
import jd2_local
from fake_docker import FakeDocker
from test_engine_components import FakeResponse
from test_engine_supervisor import NoContainerStation, Store, stat_line


class FakeAdapter:
    """Stands in for LocalAdapter: JD2 answers; one official update on the first run."""

    def __init__(self, fake, updates=0):
        self.fake = fake
        self.updates = updates
        self.revision_calls = 0
        self.default_folders = []

    def core_revision(self):
        self.revision_calls += 1
        return 4242

    def run_update_check(self):
        pass

    def is_update_available(self):
        return self.updates > 0

    def restart_and_update(self):
        self.updates -= 1
        self.fake.containers[cr.JD2_NAME]["running"] = False   # JD2 exits to self-update

    def set_default_download_folder(self, folder):
        self.default_folders.append(folder)
        return True


class ContainerBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.folder = self.root / "dl"
        self.folder.mkdir()
        os.chmod(str(self.folder), 0o777)
        self.data = self.root / "data"
        self.data.mkdir()
        self.meminfo = self.root / "meminfo"
        self.meminfo.write_text("MemTotal: 16000000 kB\nMemAvailable: 8000000 kB\n")
        self.jar_data = b"PK-fake-jar" * 50
        self.manifest = {
            "java": {"label": "j", "version": "21.0.1", "url": "https://x.test/java.tar.gz",
                     "sha256": "1" * 64, "size": 10, "filename": "java.tar.gz", "dirname": "jdk-t-jre"},
            "jd2": {"label": "d", "version": "installer", "url": "https://x.test/JDownloader.jar",
                    "sha256": hashlib.sha256(self.jar_data).hexdigest(),
                    "size": len(self.jar_data), "filename": "JDownloader.jar"},
        }
        self.fetched = []
        self.events = []
        self.clock = 1000.0
        self.fake = FakeDocker()
        patcher = patch.object(cr, "prepare_data_owner", lambda d: (1000, 1000))
        patcher.start()
        self.addCleanup(patcher.stop)

    def runner(self, argv, timeout, env):
        if len(argv) > 1 and argv[0] != cr.GETCFG:
            self.events.append("docker:" + argv[1])
        return self.fake(argv, timeout, env)

    def opener(self):
        outer = self

        class Opener:
            def open(self, request, timeout=None):
                outer.fetched.append(request.full_url)
                return FakeResponse(outer.jar_data, url=request.full_url, length=len(outer.jar_data))
        return Opener()

    def sleep(self, seconds):
        self.clock += seconds

    def manager(self, runtime=None, **extra):
        runtime = runtime or cr.Runtime(self.runner, isfile=self.fake.isfile,
                                        access=lambda p, m: True, meminfo_path=str(self.meminfo))
        manager = es.EngineManager(
            self.data, Store(str(self.folder)), manifest=self.manifest, opener=self.opener(),
            machine="x86_64", disk_usage=lambda p: SimpleNamespace(free=10 ** 12),
            meminfo_path=str(self.meminfo), runtime=runtime, sleep=self.sleep,
            monotonic=lambda: self.clock, **extra)
        manager.adapter = FakeAdapter(self.fake)
        self.addCleanup(lambda: (manager._stop_evt.set()))
        return manager

    def statuses(self, manager):
        return {s["id"]: s["status"] for s in manager.status()["steps"]}

    def cfg(self, manager):
        return json.loads((manager.jd2_dir / "cfg" / es.API_CONFIG_NAME).read_text())

    def setup_ok(self, manager, mode=None):
        with patch.object(es.jd2_policy, "build_preview", return_value={"applicable": False, "missing": []}):
            manager.run_setup(mode)
        self.assertIsNone(manager.status()["error"], manager.status())


class ContainerSetupTests(ContainerBase):
    def test_default_mode_is_container_and_full_flow(self):
        manager = self.manager()
        status = manager.status()
        self.assertEqual(status["mode"], "container")
        self.assertEqual(status["preferred_mode"], "container")
        self.assertTrue(status["container"]["available"])
        self.assertEqual([s["id"] for s in status["steps"]][:3], ["precheck", "migrate", "memory"])
        self.setup_ok(manager)
        status = manager.status()
        self.assertTrue(status["installed"])
        self.assertEqual(status["mode"], "container")
        self.assertEqual(status["phase"], "running")
        self.assertEqual(status["container"]["state"], "running")
        self.assertTrue(manager.autostart)
        steps = self.statuses(manager)
        self.assertEqual(steps["migrate"], "skipped")
        self.assertEqual(steps["image"], "done")
        self.assertEqual(steps["network"], "done")
        self.assertEqual(steps["done"], "done")
        self.assertEqual([step for step in steps if step in ("java_fetch", "java_extract")], [])
        # Java download skipped: only the JD2 installer is fetched.
        self.assertEqual(self.fetched, ["https://x.test/JDownloader.jar"])
        self.assertFalse(manager.java_dir.exists())
        self.assertEqual(self.cfg(manager), {"deprecatedapienabled": True, "deprecatedapiport": 3128,
                                             "deprecatedapilocalhostonly": False})
        self.assertEqual(json.loads((manager.engine_dir / "state.json").read_text())["mode"], "container")
        # Order: detect -> image -> network -> run.
        kinds = [e for e in self.events if e in ("docker:version", "docker:pull", "docker:network", "docker:run")]
        self.assertEqual(kinds[0], "docker:version")
        self.assertLess(self.events.index("docker:pull"), self.events.index("docker:run"))
        runs = [c for c in self.fake.docker_calls() if c[0] == "run"]
        self.assertEqual(len(runs), 1)
        self.assertIn("type=bind,source=%s,target=/downloads" % self.folder.resolve(), runs[0])
        self.assertIn("type=bind,source=%s,target=/jd2" % manager.jd2_dir, runs[0])
        self.assertNotIn("-p", runs[0])
        # JD2 sees only /downloads.
        self.assertEqual(manager.adapter.default_folders, ["/downloads"])
        self.assertEqual(manager.path_mapping(),
                         {"nas_root": str(self.folder.resolve()), "jd2_root": "/downloads"})

    def test_resume_reuses_image_network_jar_and_running_state(self):
        manager = self.manager()
        # First attempt fails at the connection config (unknown file is never overwritten).
        cfg = manager.jd2_dir / "cfg"
        cfg.mkdir(parents=True)
        (cfg / es.API_CONFIG_NAME).write_text('{"deprecatedapienabled": false}')
        with patch.object(es.jd2_policy, "build_preview", return_value={"applicable": False, "missing": []}):
            manager.run_setup()
        self.assertEqual(self.statuses(manager)["api_config"], "failed")
        self.assertIn("中止", manager.status()["error"])
        self.assertEqual((cfg / es.API_CONFIG_NAME).read_text(), '{"deprecatedapienabled": false}')
        self.assertFalse(manager.installed)
        self.assertEqual(len(self.fetched), 1)
        (cfg / es.API_CONFIG_NAME).unlink()
        again = self.manager()
        self.setup_ok(again)
        steps = self.statuses(again)
        self.assertEqual(steps["image"], "skipped")
        self.assertEqual(steps["network"], "skipped")
        self.assertEqual(steps["jd2_fetch"], "skipped")
        self.assertEqual(len(self.fetched), 1)
        self.assertEqual(len([c for c in self.fake.docker_calls() if c[0] == "pull"]), 1)
        self.assertTrue(again.installed)

    def test_first_run_reruns_container_after_official_update_exit(self):
        manager = self.manager()
        manager.adapter = FakeAdapter(self.fake, updates=1)
        self.setup_ok(manager)
        runs = [c for c in self.fake.docker_calls() if c[0] == "run"]
        self.assertEqual(len(runs), 2)           # exited for the update, then run again
        self.assertIn(["rm", "-f", cr.JD2_NAME], self.fake.docker_calls())
        self.assertEqual(self.statuses(manager)["first_run"], "done")
        self.assertEqual(self.statuses(manager)["resident"], "done")

    def test_memory_guard_blocks_before_any_pull_or_run(self):
        self.meminfo.write_text("MemTotal: 16000000 kB\nMemAvailable: 1000000 kB\n")
        manager = self.manager()
        with patch.object(es.jd2_policy, "build_preview", return_value={"applicable": False, "missing": []}):
            manager.run_setup()
        self.assertEqual(self.statuses(manager)["memory"], "failed")
        self.assertIn("空きメモリが不足", manager.status()["error"])
        self.assertFalse([c for c in self.fake.docker_calls() if c[0] in ("pull", "run")])
        self.assertFalse(manager.installed)

    def test_container_station_missing(self):
        self.fake.has_cs = False
        manager = self.manager()
        status = manager.status()
        self.assertFalse(status["container"]["available"])
        self.assertEqual(status["preferred_mode"], "native")
        for mode in (None, "container"):
            with self.subTest(mode=mode), self.assertRaises(es.EngineError) as ctx:
                manager.run_setup(mode)
            self.assertIn("App CenterでContainer Stationをインストールしてください", str(ctx.exception))
        self.assertFalse(manager.status()["setup_running"])
        with self.assertRaises(ValueError):
            manager.run_setup("docker")

    def test_foreign_container_with_our_name_blocks_launch_and_survives(self):
        self.fake.add_foreign(cr.JD2_NAME)   # unlabeled
        manager = self.manager()
        with patch.object(es.jd2_policy, "build_preview", return_value={"applicable": False, "missing": []}):
            manager.run_setup()
        self.assertEqual(self.statuses(manager)["first_run"], "failed")
        self.assertIn(cr.JD2_NAME, self.fake.containers)
        self.assertEqual(self.fake.containers[cr.JD2_NAME]["labels"], {})
        self.assertNotIn(["rm", "-f", cr.JD2_NAME], self.fake.docker_calls())
        self.assertFalse([c for c in self.fake.docker_calls() if c[0] == "stop"])
        self.assertFalse(manager.installed)

    def test_failed_setup_stops_our_container(self):
        manager = self.manager()
        manager.adapter = FakeAdapter(self.fake)
        with patch.object(es.jd2_policy, "build_preview", side_effect=jd2_local.LocalUnavailable("x")):
            manager.run_setup()
        self.assertEqual(self.statuses(manager)["preset"], "failed")
        self.assertNotIn(cr.JD2_NAME, self.fake.containers)   # stopped and removed
        self.assertIn(["stop", "-t", "30", cr.JD2_NAME], self.fake.docker_calls())


class DockerFailureTests(ContainerBase):
    def test_pull_failure_text_reaches_step_error_and_ram_log(self):
        self.fake.fail_pull = True
        manager = self.manager()
        with patch.object(es.jd2_policy, "build_preview", return_value={"applicable": False, "missing": []}):
            manager.run_setup()
        self.assertEqual(self.statuses(manager)["image"], "failed")
        self.assertIn("pull access denied", manager.status()["error"])
        self.assertIn("pull access denied", [s for s in manager.status()["steps"] if s["id"] == "image"][0]["detail"])
        self.assertTrue(any("pull access denied" in l for l in manager.log_lines()))


class MigrationTests(ContainerBase):
    def make_native_install(self):
        """An installed native engine with a (fake) running Java process."""
        native = self.manager(runtime=NoContainerStation())
        native.manifest = self.manifest
        native.jd2_dir.mkdir(parents=True)
        native.jar.write_bytes(self.jar_data)
        (native.jd2_dir / "cfg").mkdir()
        (native.jd2_dir / "cfg" / es.API_CONFIG_NAME).write_text(json.dumps(es.API_CONFIG))
        native._state.update(setup_complete=True, mode="native", autostart=True,
                             jd2={"placed": True, "initialized": True})
        native._save_state()
        (native.jd2_dir / "keep.txt").write_text("queue data")
        return native

    def adopt_running_native(self, manager):
        proc = self.root / "proc"
        folder = proc / "4242"
        folder.mkdir(parents=True)
        (folder / "stat").write_text(stat_line(4242, 5555))
        argv = [str(manager.java_bin), "-jar", str(manager.jar)]
        (folder / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
        manager.proc_root = str(proc)

        def kill(pid, sig):
            self.events.append("native-kill")
            shutil.rmtree(str(folder), ignore_errors=True)
        manager._kill = kill
        manager._pid, manager._ticks = 4242, 5555

    def test_migration_stops_native_first_and_keeps_data(self):
        native = self.make_native_install()
        manager = self.manager()
        manager.manifest = self.manifest
        self.adopt_running_native(manager)
        self.assertTrue(manager.is_ours_running())
        status = manager.status()
        self.assertEqual(status["mode"], "native")
        self.assertTrue(status["can_migrate"])
        self.setup_ok(manager, "container")
        self.assertLess(self.events.index("native-kill"), self.events.index("docker:run"))
        self.assertLess(self.events.index("native-kill"), self.events.index("docker:pull"))
        self.assertEqual(self.statuses(manager)["migrate"], "done")
        self.assertEqual(manager.status()["mode"], "container")
        self.assertFalse(manager.status()["can_migrate"])
        self.assertEqual((manager.jd2_dir / "keep.txt").read_text(), "queue data")
        self.assertEqual(self.cfg(manager)["deprecatedapilocalhostonly"], False)
        self.assertEqual(self.fetched, [])   # existing JD2 reused
        self.assertEqual(self.statuses(manager)["jd2_fetch"], "skipped")
        self.assertIsNone(manager._pid)

    def test_migrate_not_offered_without_container_station_or_when_already_container(self):
        self.make_native_install()
        self.fake.has_cs = False
        manager = self.manager()
        self.assertFalse(manager.status()["can_migrate"])

    def test_native_setup_refused_while_running_without_migration(self):
        self.make_native_install()
        manager = self.manager()
        self.adopt_running_native(manager)
        with self.assertRaises(es.EngineError):
            manager.run_setup("native")

    def test_native_engine_never_starts_while_container_runs(self):
        manager = self.manager()
        self.setup_ok(manager)
        manager._state["mode"] = "native"      # simulate switching back by hand
        manager._forget_container()
        with self.assertRaises(es.EngineError) as ctx:
            manager.java_bin.parent.mkdir(parents=True, exist_ok=True)
            manager.java_bin.write_text("x")
            manager._launch_process()
        self.assertIn("コンテナ版のエンジンが稼働中", str(ctx.exception))


class ApiConfigSwitchTests(ContainerBase):
    def write(self, manager, text):
        path = manager.jd2_dir / "cfg" / es.API_CONFIG_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def test_create_switch_and_same(self):
        manager = self.manager()
        self.assertEqual(manager.switch_api_config("container"), "created")
        self.assertEqual(manager.switch_api_config("container"), "same")
        self.assertEqual(manager.switch_api_config("native"), "rewritten")
        self.assertEqual(self.cfg(manager), es.API_CONFIG)
        self.assertEqual(manager.switch_api_config("container"), "rewritten")
        self.assertEqual(self.cfg(manager), es.API_CONFIG_CONTAINER)

    def test_extra_keys_are_preserved_and_missing_keys_filled(self):
        manager = self.manager()
        extra = {"deprecatedapienabled": True, "deprecatedapiport": 3128,
                 "deprecatedapilocalhostonly": True, "deprecatedapiglobalheaders": [1],
                 "cleanupasyncoutputstream": "x"}
        self.write(manager, json.dumps(extra))
        self.assertEqual(manager.switch_api_config("container"), "rewritten")
        expected = dict(extra, deprecatedapilocalhostonly=False)
        self.assertEqual(self.cfg(manager), expected)
        self.assertEqual(manager.switch_api_config("native"), "rewritten")
        self.assertEqual(self.cfg(manager), extra)
        self.write(manager, json.dumps({"other": 1}))   # enabled/port missing is fine
        manager.switch_api_config("container")
        self.assertEqual(self.cfg(manager), dict(es.API_CONFIG_CONTAINER, other=1))

    def test_wrong_port_disabled_or_invalid_json_refused_and_untouched(self):
        manager = self.manager()
        for text in ('{"deprecatedapienabled": true, "deprecatedapiport": 3129}',
                     '{"deprecatedapienabled": true, "deprecatedapiport": "3128"}',
                     '{"deprecatedapienabled": false}', '{"deprecatedapienabled": null}',
                     "not json", "[]", "3128", ""):
            path = self.write(manager, text)
            for mode in ("container", "native"):
                with self.subTest(text=text, mode=mode), self.assertRaises(es.SetupFailure) as ctx:
                    manager.switch_api_config(mode)
                self.assertIn("中止", str(ctx.exception))
                self.assertEqual(path.read_text(), text)

    def test_symlink_is_refused(self):
        manager = self.manager()
        target = self.root / "other.json"
        target.write_text(json.dumps(es.API_CONFIG))
        path = manager.jd2_dir / "cfg" / es.API_CONFIG_NAME
        path.parent.mkdir(parents=True)
        os.symlink(str(target), str(path))
        with self.assertRaises(es.SetupFailure):
            manager.switch_api_config("container")
        self.assertEqual(json.loads(target.read_text()), es.API_CONFIG)

    def test_only_the_one_cfg_file_is_written(self):
        manager = self.manager()
        self.write(manager, json.dumps(es.API_CONFIG))
        (manager.jd2_dir / "cfg" / "other.json").write_text("{}")
        manager.switch_api_config("container")
        self.assertEqual(sorted(p.name for p in (manager.jd2_dir / "cfg").iterdir()),
                         ["org.jdownloader.api.RemoteAPIConfig.json", "other.json"])
        self.assertEqual((manager.jd2_dir / "cfg" / "other.json").read_text(), "{}")

    def test_container_launch_refuses_wrong_port_and_logs_reason(self):
        manager = self.manager()
        self.setup_ok(manager)
        manager.stop()
        path = manager.jd2_dir / "cfg" / es.API_CONFIG_NAME
        path.write_text('{"deprecatedapienabled": true, "deprecatedapiport": 9999}')
        with self.assertRaises(es.EngineError) as ctx:
            manager.start()
        self.assertIn("中止", str(ctx.exception))
        self.assertTrue(any("コンテナ起動失敗" in l for l in manager.log_lines()))
        self.assertNotIn(cr.JD2_NAME, self.fake.containers)


class ConnectionTargetTests(ContainerBase):
    def installed_manager(self):
        manager = self.manager()
        self.setup_ok(manager)
        return manager

    def test_target_is_verified_container_ip_and_skips_proc_check(self):
        manager = self.installed_manager()
        with patch.object(jd2_local, "verify_listener", side_effect=AssertionError("native check")):
            self.assertEqual(manager.connection_target(), ("172.30.0.2", 3128))

    def test_inspect_result_cached_for_ten_seconds_only(self):
        manager = self.installed_manager()
        manager._ip_cache = None
        before = len([c for c in self.fake.docker_calls() if c[:2] == ["container", "inspect"]])
        self.assertEqual(manager.connection_target()[0], "172.30.0.2")
        self.fake.containers[cr.JD2_NAME]["ip"] = "172.30.0.9"
        self.clock += 9
        self.assertEqual(manager.connection_target()[0], "172.30.0.2")
        self.clock += 2
        self.assertEqual(manager.connection_target()[0], "172.30.0.9")
        after = len([c for c in self.fake.docker_calls() if c[:2] == ["container", "inspect"]])
        self.assertEqual(after - before, 2)

    def test_unsafe_container_is_never_a_target(self):
        manager = self.installed_manager()
        box = self.fake.containers[cr.JD2_NAME]
        for change in ({"labels": {}}, {"ports": {"3128/tcp": [{}]}}, {"extra_nets": ["bridge"]},
                       {"running": False}):
            saved = dict(box)
            box.update(change)
            manager._ip_cache = None
            with self.subTest(change=change), self.assertRaises(jd2_local.LocalUnavailable):
                manager.connection_target()
            box.clear()
            box.update(saved)

    def test_adapter_connects_to_container_ip(self):
        manager = self.installed_manager()
        seen = {}

        class Conn:
            def __init__(self, host, port, timeout=None):
                seen["target"] = (host, port)

            def request(self, *a, **k):
                pass

            def getresponse(self):
                return SimpleNamespace(status=200, read=lambda n: b'{"data": 7}')

            def close(self):
                pass
        real = jd2_local.LocalAdapter(manager)
        with patch.object(jd2_local.http.client, "HTTPConnection", Conn):
            self.assertEqual(real.core_revision(), 7)
        self.assertEqual(seen["target"], ("172.30.0.2", 3128))
        options = real._options
        self.assertEqual(options["nas_root"], str(self.folder.resolve()))
        self.assertEqual(options["jd2_root"], "/downloads")
        self.assertNotIn("same_host", options)


class LifecycleTests(ContainerBase):
    def test_stop_is_graceful_and_removes_only_our_container(self):
        self.fake.add_foreign("jdownloader2")
        manager = self.manager()
        self.setup_ok(manager)
        manager.stop()
        calls = self.fake.docker_calls()
        self.assertIn(["stop", "-t", "30", cr.JD2_NAME], calls)
        self.assertIn(["rm", "-f", cr.JD2_NAME], calls)
        self.assertLess(calls.index(["stop", "-t", "30", cr.JD2_NAME]),
                        calls.index(["rm", "-f", cr.JD2_NAME]))
        self.assertIn("jdownloader2", self.fake.containers)
        self.assertTrue(self.fake.containers["jdownloader2"]["running"])
        self.assertEqual(manager.phase(), "stopped")

    def test_start_after_stop_and_startup_adoption(self):
        manager = self.manager()
        self.setup_ok(manager)
        manager.stop()
        manager.start()
        self.assertIn(cr.JD2_NAME, self.fake.containers)
        manager._stop_evt.set()
        # A new Hub process finds the container still running and adopts it.
        fresh = self.manager()
        self.assertEqual(fresh.startup(), "adopted")
        fresh._stop_evt.set()
        runs = [c for c in self.fake.docker_calls() if c[0] == "run"]
        self.assertEqual(len(runs), 2)

    def test_startup_autostarts_when_container_gone(self):
        manager = self.manager()
        self.setup_ok(manager)
        manager.stop()
        fresh = self.manager()
        self.assertEqual(fresh.startup(), "autostart")
        for _ in range(100):
            if cr.JD2_NAME in self.fake.containers:
                break
            import time
            time.sleep(0.05)
        self.assertIn(cr.JD2_NAME, self.fake.containers)
        fresh._stop_evt.set()

    def test_launch_refuses_low_memory(self):
        manager = self.manager()
        self.setup_ok(manager)
        manager.stop()
        self.meminfo.write_text("MemTotal: 16000000 kB\nMemAvailable: 100 kB\n")
        with self.assertRaises(es.EngineError) as ctx:
            manager.start()
        self.assertIn("空きメモリが不足", str(ctx.exception))
        self.assertNotIn(cr.JD2_NAME, self.fake.containers)


if __name__ == "__main__":
    unittest.main()
