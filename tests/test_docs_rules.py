"""README / CHANGELOG rules (DEVELOPMENT_WORKFLOW.md "Documentation rules")."""
import re
import unittest
from pathlib import Path

import webapp

ROOT = Path(__file__).resolve().parents[1]


class DocsRulesTests(unittest.TestCase):
    def setUp(self):
        self.readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    def test_readme_has_no_version_log(self):
        for banned in ("現在の状態", "直前の v", "実機未検証"):
            self.assertNotIn(banned, self.readme, "per-version notes belong in CHANGELOG.md")
        headings = [line for line in self.readme.splitlines() if line.startswith("#")]
        for heading in headings:
            self.assertIsNone(re.search(r"v\d+\.\d+\.\d+", heading), "version tag in a README heading: " + heading)

    def test_readme_stays_short_and_conventional(self):
        self.assertLess(len(self.readme.encode("utf-8")), 16 * 1024, "README.md should stay short")
        for section in ("## 主な機能", "## 動作環境", "## インストール", "## 初期設定", "## 使い方", "## トラブルシューティング", "## 開発"):
            self.assertIn(section, self.readme)
        self.assertIn("CHANGELOG.md", self.readme)

    def test_readme_links_exist(self):
        for target in re.findall(r"\]\(((?!https?://)[^)#]+)\)", self.readme):
            self.assertTrue((ROOT / target).exists(), "README links to a missing file: " + target)

    def test_changelog_starts_with_the_current_version(self):
        first = re.search(r"^## (v\d+\.\d+\.\d+)\s*$", self.changelog, re.M)
        self.assertIsNotNone(first)
        self.assertEqual(first.group(1), "v" + webapp.VERSION, "add a CHANGELOG.md entry for this version at the top")


if __name__ == "__main__":
    unittest.main()
