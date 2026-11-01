import unittest
from unittest.mock import Mock

import jd2_policy as p


class FakeJD2:
    def __init__(self):
        self.version = "core-1"
        self.values = {key: True for key in p.REQUIRED}
        self.calls = []

    def inspect_preset(self):
        return {
            "jd_revision": self.version,
            "entries": {key: {"supported": True, "writable": True,
                              "handle": key, "current": value}
                        for key, value in self.values.items()},
        }

    def update_preset(self, key, value):
        self.calls.append((key, value))
        self.values[key] = value


class Jd2PolicyTests(unittest.TestCase):
    def test_preview_and_explicit_reapply(self):
        engine = FakeJD2()
        preview = p.build_preview(engine)
        self.assertTrue(preview["applicable"])
        self.assertEqual(len(engine.calls), 0)
        self.assertEqual(len(preview["entries"]), len(p.REQUIRED))
        post = p.apply_preset(engine, preview)
        self.assertTrue(all(x["current"] is False for x in post["entries"]))
        self.assertEqual(len(engine.calls), len(p.REQUIRED))
        self.assertEqual(len(p.apply_preset(engine, p.build_preview(engine))["entries"]),
                         len(p.REQUIRED))
        self.assertEqual(len(engine.calls), len(p.REQUIRED))

    def test_unknown_config_prevents_any_change(self):
        engine = FakeJD2()
        del engine.values["extraction.auto_extract"]
        preview = p.build_preview(engine)
        self.assertIn("extraction.auto_extract", preview["missing"])
        with self.assertRaises(p.PresetUnavailable):
            p.apply_preset(engine, preview)
        self.assertEqual(engine.calls, [])

    def test_update_revision_invalidates_preview(self):
        engine = FakeJD2()
        preview = p.build_preview(engine)
        engine.version = "core-2"
        with self.assertRaises(p.PresetChanged):
            p.apply_preset(engine, preview)
        self.assertEqual(engine.calls, [])

    def test_setting_drift_invalidates_preview(self):
        engine = FakeJD2()
        preview = p.build_preview(engine)
        engine.values[p.REQUIRED[0]] = False
        with self.assertRaises(p.PresetChanged):
            p.apply_preset(engine, preview)
        self.assertEqual(engine.calls, [])

    def test_missing_jd_revision_rejects_preset(self):
        engine = FakeJD2()
        engine.version = None
        with self.assertRaises(p.PresetUnavailable):
            p.build_preview(engine)

    def test_partial_write_then_exception_is_rolled_back(self):
        engine = FakeJD2()
        original = engine.update_preset
        def partially_write_then_fail(key, value):
            original(key, value)
            if key == p.REQUIRED[1] and value is False:
                raise RuntimeError("write succeeded but response failed")
        engine.update_preset = partially_write_then_fail
        with self.assertRaises(RuntimeError):
            p.apply_preset(engine, p.build_preview(engine))
        self.assertTrue(all(value is True for value in engine.values.values()))

    def test_unverified_rollback_reports_failure(self):
        engine = FakeJD2()
        original = engine.update_preset
        def rollback_lies(key, value):
            if value is True:
                return  # API acknowledges but value was not restored
            original(key, value)
            if key == p.REQUIRED[0]:
                raise RuntimeError("failed after changing JD2")
        engine.update_preset = rollback_lies
        with self.assertRaisesRegex(p.PresetChanged, "元の状態を確認できません"):
            p.apply_preset(engine, p.build_preview(engine))

    def test_apply_failure_rolls_back(self):
        engine = FakeJD2()
        original = engine.update_preset
        def fail(key, value):
            if key == p.REQUIRED[2] and value is False:
                raise RuntimeError("not writable")
            original(key, value)
        engine.update_preset = fail
        with self.assertRaises(RuntimeError):
            p.apply_preset(engine, p.build_preview(engine))
        self.assertTrue(all(v is True for v in engine.values.values()))


if __name__ == "__main__":
    unittest.main()
