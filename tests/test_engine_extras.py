"""v0.1.16: MyJDownloader auto-connect switch-off and native leftover removal."""
import json
import os
from types import SimpleNamespace
from unittest.mock import patch

import engine_supervisor as es
from test_engine_container import ContainerBase


class MyJdAutoconnectTests(ContainerBase):
    def path(self, manager):
        return manager.jd2_dir / "cfg" / es.MYJD_CONFIG_NAME

    def test_created_when_absent_with_only_the_one_key(self):
        manager = self.manager()
        self.assertEqual(manager.disable_myjd_autoconnect("native"), "created")
        self.assertEqual(json.loads(self.path(manager).read_text()), {"autoconnectenabledv2": False})

    def test_other_keys_preserved_and_idempotent(self):
        manager = self.manager()
        self.path(manager).parent.mkdir(parents=True)
        self.path(manager).write_text(json.dumps({"autoconnectenabledv2": True, "devicename": "x", "n": 1}))
        self.assertEqual(manager.disable_myjd_autoconnect("container"), "rewritten")
        self.assertEqual(json.loads(self.path(manager).read_text()),
                         {"autoconnectenabledv2": False, "devicename": "x", "n": 1})
        self.assertEqual(manager.disable_myjd_autoconnect("container"), "same")

    def test_invalid_json_or_non_object_is_refused_without_overwrite(self):
        manager = self.manager()
        self.path(manager).parent.mkdir(parents=True)
        for text in ("{not json", "[1, 2]"):
            self.path(manager).write_text(text)
            with self.assertRaises(es.SetupFailure):
                manager.disable_myjd_autoconnect("native")
            self.assertEqual(self.path(manager).read_text(), text)

    def test_symlink_is_refused(self):
        manager = self.manager()
        self.path(manager).parent.mkdir(parents=True)
        target = manager.jd2_dir / "elsewhere.json"
        target.write_text("{}")
        self.path(manager).symlink_to(target)
        with self.assertRaises(es.SetupFailure):
            manager.disable_myjd_autoconnect("native")
        self.assertEqual(target.read_text(), "{}")

    def test_switch_api_config_writes_it_and_never_credentials(self):
        manager = self.manager()
        manager.switch_api_config("container")
        data = json.loads(self.path(manager).read_text())
        self.assertEqual(data, {"autoconnectenabledv2": False})
        self.assertFalse({"email", "password"} & {k.lower() for k in data})

    def test_invalid_api_config_does_not_touch_myjd_file(self):
        manager = self.manager()
        api = manager.jd2_dir / "cfg" / es.API_CONFIG_NAME
        api.parent.mkdir(parents=True)
        api.write_text('{"deprecatedapienabled": false}')
        with self.assertRaises(es.SetupFailure):
            manager.switch_api_config("native")
        self.assertFalse(self.path(manager).exists())

    def test_setup_step_label_mentions_it(self):
        labels = dict(es.STEP_DEFS + es.STEP_DEFS_CONTAINER)
        self.assertIn("MyJDownloader自動接続の無効化", labels["api_config"])


class NativeLeftoverTests(ContainerBase):
    def prepare(self, manager, mode="container", complete=True):
        manager.engine_dir.mkdir(parents=True, exist_ok=True)
        (manager.java_root / "jdk" / "bin").mkdir(parents=True)
        (manager.java_root / "jdk" / "bin" / "java").write_bytes(b"x" * 1000)
        manager.download_dir.mkdir()
        (manager.download_dir / "part").write_bytes(b"y" * 500)
        (manager.jd2_dir / "cfg").mkdir(parents=True)
        (manager.jd2_dir / "JDownloader.jar").write_bytes(b"jar")
        (manager.jd2_dir / "cfg" / "queue.json").write_text("{}")
        manager._update_state(mode=mode, setup_complete=complete, java={"version": "21"})

    def test_size_presence_and_status(self):
        manager = self.manager()
        self.prepare(manager)
        self.assertEqual(manager.native_leftovers(refresh=True), {"present": True, "bytes": 1500})
        self.assertEqual(manager.status()["native_leftovers"], {"present": True, "bytes": 1500})

    def test_size_ignores_symlinked_content(self):
        manager = self.manager()
        self.prepare(manager)
        outside = self.root / "big"
        outside.write_bytes(b"z" * 10000)
        os.symlink(str(outside), str(manager.java_root / "link"))
        self.assertEqual(manager.native_leftovers(refresh=True)["bytes"], 1500)

    def test_size_is_cached_for_60_seconds(self):
        manager = self.manager()
        self.prepare(manager)
        manager.native_leftovers(refresh=True)
        (manager.download_dir / "more").write_bytes(b"q" * 100)
        self.assertEqual(manager.native_leftovers()["bytes"], 1500)
        self.clock += 61
        self.assertEqual(manager.native_leftovers()["bytes"], 1600)

    def test_remove_keeps_jd2_and_updates_state(self):
        manager = self.manager()
        self.prepare(manager)
        result = manager.remove_native_leftovers()
        self.assertEqual(result, {"removed": True, "freed_bytes": 1500})
        self.assertFalse(manager.java_root.exists())
        self.assertFalse(manager.download_dir.exists())
        self.assertTrue((manager.jd2_dir / "JDownloader.jar").exists())
        self.assertTrue((manager.jd2_dir / "cfg" / "queue.json").exists())
        self.assertIsNone(manager._state["java"])
        self.assertEqual(json.loads(manager.state_path.read_text())["java"], None)
        self.assertEqual(manager.native_leftovers(refresh=True), {"present": False, "bytes": 0})

    def test_state_file_not_rewritten_when_unchanged(self):
        manager = self.manager()
        self.prepare(manager)
        manager._update_state(java=None)
        before = manager.state_path.stat().st_mtime_ns
        with patch.object(es.durability, "write_json") as write:
            manager.remove_native_leftovers()
        write.assert_not_called()
        self.assertEqual(manager.state_path.stat().st_mtime_ns, before)

    def test_refused_in_native_mode_or_without_completed_setup(self):
        manager = self.manager()
        self.prepare(manager, mode="native")
        with self.assertRaises(es.EngineError):
            manager.remove_native_leftovers()
        self.assertTrue(manager.java_root.exists())
        manager._update_state(mode="container", setup_complete=False)
        with self.assertRaises(es.EngineError):
            manager.remove_native_leftovers()
        self.assertTrue(manager.java_root.exists())

    def test_refused_while_setup_runs_or_native_process_alive(self):
        manager = self.manager()
        self.prepare(manager)
        manager._setup_active = True
        with self.assertRaisesRegex(es.EngineError, "初期設定"):
            manager.remove_native_leftovers()
        manager._setup_active = False
        manager._pid, manager._ticks = 4242, 1
        with patch.object(manager, "_identity_ok", return_value=True):
            with self.assertRaisesRegex(es.EngineError, "動作中"):
                manager.remove_native_leftovers()
        self.assertTrue(manager.java_root.exists())

    def test_symlinked_java_dir_is_refused_and_target_untouched(self):
        manager = self.manager()
        self.prepare(manager)
        import shutil
        shutil.rmtree(str(manager.java_root))
        real = self.root / "realjava"
        real.mkdir()
        (real / "keep").write_text("keep")
        os.symlink(str(real), str(manager.java_root))
        with self.assertRaises(es.EngineError):
            manager.remove_native_leftovers()
        self.assertEqual((real / "keep").read_text(), "keep")
        self.assertTrue(os.path.islink(str(manager.java_root)))
        self.assertTrue(manager.download_dir.exists())

    def test_nothing_left_is_reported_absent(self):
        manager = self.manager()
        self.assertEqual(manager.native_leftovers(refresh=True), {"present": False, "bytes": 0})
