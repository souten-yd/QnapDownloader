"""v0.1.48: Packagizer rules that create per-package folders are found and disabled."""
import unittest

import jd2_local


class FakeConfig(jd2_local.LocalAdapter):
    def __init__(self, rules, listed=True, accept=True):
        self.rules = rules
        self.listed = listed
        self.accept = accept
        self.calls = []

    def _config_entries(self):
        entries = [{"interfaceName": "org.jdownloader.settings.GeneralSettings", "storage": None,
                    "key": "defaultdownloadfolder", "value": "/downloads"}]
        if self.listed:
            entries.append({"interfaceName": "org.jdownloader.controlling.packagizer.PackagizerSettings",
                            "storage": None, "key": "RuleList", "value": None})
        return entries

    def _call(self, namespace, method, *args):
        self.calls.append((namespace, method, args))
        if (namespace, method) == ("config", "get"):
            return [dict(r) for r in self.rules]
        if (namespace, method) == ("config", "set"):
            if self.accept:
                self.rules = [dict(r) for r in args[3]]
            return self.accept
        raise AssertionError((namespace, method))


RULES = [
    {"name": "Subfolder by package", "enabled": True, "downloadDestination": "<jd:packagename>"},
    {"name": "Archive", "enabled": True, "downloadDestination": "", "staticrule": True},
    {"name": "Off already", "enabled": False, "downloadDestination": "<jd:packagename>"},
    {"name": "Plugin folder", "enabled": True, "downloadDestination": "/x/<jd:subfolderbyplugin>"},
]


class PackagizerRulesTest(unittest.TestCase):
    def test_reports_without_writing(self):
        fake = FakeConfig(RULES)
        item = fake.packagizer_folder_rules(False)
        self.assertFalse(item["compliant"])
        self.assertIn("Subfolder by package", item["current"])
        self.assertNotIn(("config", "set"), [(c[0], c[1]) for c in fake.calls])

    def test_disables_only_folder_rules_and_reads_back(self):
        fake = FakeConfig(RULES)
        item = fake.packagizer_folder_rules(True)
        self.assertTrue(item["compliant"])
        by_name = {r["name"]: r for r in fake.rules}
        self.assertFalse(by_name["Subfolder by package"]["enabled"])
        self.assertFalse(by_name["Plugin folder"]["enabled"])
        self.assertTrue(by_name["Archive"]["enabled"])            # other rules untouched
        self.assertEqual(by_name["Archive"].get("staticrule"), True)

    def test_rejected_write_is_not_compliant(self):
        fake = FakeConfig(RULES, accept=False)
        self.assertFalse(fake.packagizer_folder_rules(True)["compliant"])

    def test_no_rule_list_returns_none(self):
        self.assertIsNone(FakeConfig(RULES, listed=False).packagizer_folder_rules(True))

    def test_nothing_to_do(self):
        fake = FakeConfig([RULES[1]])
        item = fake.packagizer_folder_rules(True)
        self.assertTrue(item["compliant"])
        self.assertNotIn(("config", "set"), [(c[0], c[1]) for c in fake.calls])


if __name__ == "__main__":
    unittest.main()
