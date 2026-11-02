import unittest
from unittest.mock import Mock

import jd2_policy as p


class FakeJD2:
    def __init__(self):
        self.version = "core-1"
        self.values = {key: ("ALWAYS" if key == "downloads.auto_start" else True)
                       for key in p.REQUIRED}
        self.initial = dict(self.values)
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
        self.assertTrue(all(x["compliant"] for x in post["entries"]))
        self.assertEqual(engine.values["downloads.auto_start"], "ONLY_IF_EXIT_WITH_RUNNING_DOWNLOADS")
        self.assertIs(engine.values["linkgrabber.auto_start"], False)
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
        self.assertEqual(engine.values, engine.initial)

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
        self.assertEqual(engine.values, engine.initial)

    def test_enum_entry_compliance_and_target(self):
        for current, change in (("ALWAYS", True), ("NEVER", False),
                                ("ONLY_IF_EXIT_WITH_RUNNING_DOWNLOADS", False)):
            with self.subTest(current=current):
                engine = FakeJD2()
                engine.values["downloads.auto_start"] = current
                entry = [e for e in p.build_preview(engine)["entries"]
                         if e["key"] == "downloads.auto_start"][0]
                self.assertEqual(entry["change"], change)
                self.assertEqual(entry["compliant"], not change)
                self.assertEqual(entry["target"],
                                 "ONLY_IF_EXIT_WITH_RUNNING_DOWNLOADS" if change else current)

    def test_compliant_enum_is_not_written(self):
        engine = FakeJD2()
        engine.values["downloads.auto_start"] = "NEVER"
        p.apply_preset(engine, p.build_preview(engine))
        self.assertNotIn("downloads.auto_start", [k for k, _ in engine.calls])
        self.assertEqual(engine.values["downloads.auto_start"], "NEVER")

    def test_invalid_enum_value_is_unsupported(self):
        engine = FakeJD2()
        engine.values["downloads.auto_start"] = "SOMETIMES"
        self.assertIn("downloads.auto_start", p.build_preview(engine)["missing"])
        engine.values["downloads.auto_start"] = True
        self.assertIn("downloads.auto_start", p.build_preview(engine)["missing"])

    def test_exact_interfaces_labels_and_no_subfolder_keys(self):
        self.assertEqual(p.LABELS["linkgrabber.auto_confirm"], "リンク自動確定")
        self.assertEqual(p.LABELS["linkgrabber.auto_start"], "追加後の自動開始")
        self.assertEqual(p.LABELS["extraction.auto_extract"], "自動解凍")
        self.assertIn("「常に」以外ならOK", p.LABELS["downloads.auto_start"])
        self.assertNotIn("packagizer.package_subfolders", p.REQUIRED)
        self.assertNotIn("general.package_subfolders", p.REQUIRED)
        self.assertEqual(p.KNOWN_KEYS["downloads.auto_start"]["interface"],
                         "org.jdownloader.settings.GeneralSettings")


if __name__ == "__main__":
    unittest.main()
