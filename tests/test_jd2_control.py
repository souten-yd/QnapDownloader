import unittest

import jd2_control
import jd2_policy


class Adapter:
    def __init__(self):
        self.revision = "jd2-core-2026"
        self.values = {k: ("ALWAYS" if k == "downloads.auto_start" else True)
                       for k in jd2_policy.REQUIRED}
        self.available = True
        self.update_count = 0

    def core_revision(self):
        return self.revision

    def run_update_check(self):
        return None

    def is_update_available(self):
        return self.available

    def restart_and_update(self):
        self.update_count += 1
        self.revision = "jd2-core-new"
        self.available = False

    def inspect_preset(self):
        return {"jd_revision": self.revision, "entries": {
            k: {"supported": True, "writable": True, "handle": k, "current": v}
            for k, v in self.values.items()}}

    def update_preset(self, key, value):
        self.values[key] = value


class ControllerTests(unittest.TestCase):
    def test_disconnected_fails_closed(self):
        ctrl = jd2_control.JD2Controller()
        self.assertFalse(ctrl.status()["connected"])
        with self.assertRaises(jd2_policy.PresetUnavailable):
            ctrl.update_check()
        with self.assertRaises(jd2_policy.PresetUnavailable):
            ctrl.preset_preview()
        with self.assertRaises(jd2_policy.PresetUnavailable):
            ctrl.restart_and_update(confirmed=True)

    def test_explicit_connection_probe_is_read_only(self):
        adapter=Adapter()
        ctrl=jd2_control.JD2Controller(adapter)
        status=ctrl.connection_test()
        self.assertEqual(status["revision"], "jd2-core-2026")
        self.assertTrue(status["connected"])
        self.assertTrue(status["update_available"])
        self.assertEqual(adapter.update_count, 0)
        self.assertEqual(adapter.values["linkgrabber.auto_confirm"], True)

    def test_jd_update_does_not_call_hub_updater(self):
        jd = Adapter()
        ctl = jd2_control.JD2Controller(jd)
        self.assertTrue(ctl.status()["update_available"])
        with self.assertRaises(ValueError):
            ctl.restart_and_update(confirmed=False)
        result = ctl.restart_and_update(confirmed=True)
        self.assertTrue(result["started"])
        self.assertEqual(jd.update_count, 1)
        self.assertEqual(ctl.status()["revision"], "jd2-core-new")
        with self.assertRaises(ValueError):
            ctl.restart_and_update(confirmed=True)

    def test_update_invalidates_outdated_preset_preview(self):
        jd = Adapter()
        ctl = jd2_control.JD2Controller(jd)
        ctl.preset_preview()
        ctl.restart_and_update(confirmed=True)
        with self.assertRaises(jd2_policy.PresetUnavailable):
            ctl.preset_reapply(confirmed=True)
        preview = ctl.preset_preview()
        self.assertTrue(preview["applicable"])
        self.assertTrue(ctl.preset_reapply(confirmed=True)["applicable"])
        self.assertTrue(all(v is False for k, v in jd.values.items() if k != "downloads.auto_start"))
        self.assertEqual(jd.values["downloads.auto_start"], "ONLY_IF_EXIT_WITH_RUNNING_DOWNLOADS")
        with self.assertRaises(jd2_policy.PresetUnavailable):
            ctl.preset_reapply(confirmed=True)

    def test_reapply_requires_explicit_confirmation(self):
        ctl = jd2_control.JD2Controller(Adapter())
        ctl.preset_preview()
        with self.assertRaises(ValueError):
            ctl.preset_reapply()
        with self.assertRaises(ValueError):
            ctl.restart_and_update()


class LinkGrabberAdapter(Adapter):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.items = [{"id": "11", "package_id": "5", "name": "a", "host": "", "bytes_total": 1},
                      {"id": "12", "package_id": "5", "name": "b", "host": "", "bytes_total": 1},
                      {"id": "13", "package_id": "6", "name": "c", "host": "", "bytes_total": 1}]

    def list_linkgrabber(self):
        self.calls.append("list")
        return {"items": list(self.items), "count": len(self.items), "has_more": False}

    def move_to_downloadlist(self, links, packages):
        self.calls.append(("move", links, packages))

    def start_downloads(self):
        self.calls.append("start")

    def stop_downloads(self):
        self.calls.append("stop")


class DownloadControlTests(unittest.TestCase):
    def setUp(self):
        self.adapter = LinkGrabberAdapter()
        self.ctrl = jd2_control.JD2Controller(self.adapter)

    def test_start_waiting_moves_everything_then_starts(self):
        out = self.ctrl.start_waiting(confirmed=True)
        self.assertEqual(out, {"moved": 3, "started": True})
        self.assertEqual(self.adapter.calls, ["list", ("move", [11, 12, 13], [5, 6]), "start"])

    def test_start_waiting_requires_confirmation_and_does_nothing_otherwise(self):
        for value in (False, None, "yes", 1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.ctrl.start_waiting(confirmed=value)
        self.assertEqual(self.adapter.calls, [])

    def test_empty_linkgrabber_just_starts(self):
        self.adapter.items = []
        self.assertEqual(self.ctrl.start_waiting(confirmed=True), {"moved": 0, "started": True})
        self.assertEqual(self.adapter.calls, ["list", "start"])

    def test_start_and_stop(self):
        self.assertEqual(self.ctrl.start_downloads(), {"started": True})
        with self.assertRaises(ValueError):
            self.ctrl.stop_downloads()
        self.assertEqual(self.ctrl.stop_downloads(confirmed=True), {"stopped": True})
        self.assertEqual(self.adapter.calls, ["start", "stop"])

    def test_without_an_engine_everything_fails_closed(self):
        ctrl = jd2_control.JD2Controller()
        for call in (ctrl.linkgrabber_snapshot, ctrl.start_downloads,
                     lambda: ctrl.start_waiting(confirmed=True), lambda: ctrl.stop_downloads(confirmed=True)):
            with self.assertRaises(jd2_policy.PresetUnavailable):
                call()


if __name__ == "__main__":
    unittest.main()
