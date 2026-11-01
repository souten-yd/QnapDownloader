import unittest

import jd2_control
import jd2_policy


class Adapter:
    def __init__(self):
        self.revision = "jd2-core-2026"
        self.values = {k: True for k in jd2_policy.REQUIRED}
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
        self.assertTrue(all(adapter.values.values()))

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
        self.assertTrue(all(v is False for v in jd.values.values()))
        with self.assertRaises(jd2_policy.PresetUnavailable):
            ctl.preset_reapply(confirmed=True)

    def test_reapply_requires_explicit_confirmation(self):
        ctl = jd2_control.JD2Controller(Adapter())
        ctl.preset_preview()
        with self.assertRaises(ValueError):
            ctl.preset_reapply()
        with self.assertRaises(ValueError):
            ctl.restart_and_update()


if __name__ == "__main__":
    unittest.main()
