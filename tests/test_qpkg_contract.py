"""Cross-check QDK manifest and runtime entrypoints before producing a QPKG."""
from pathlib import Path
import re
import subprocess
import unittest

import webapp
import jd2_control
from qpkg.shared import downloader_pid

ROOT = Path(__file__).resolve().parents[1]


class QpkgContractTests(unittest.TestCase):
    def cfg(self):
        return dict(re.findall(r'^([A-Z_]+)="([^"]*)"$',
                       (ROOT / "qpkg/qpkg.cfg").read_text(), re.MULTILINE))

    def test_qdk_identity_version_and_port_match_runtime(self):
        cfg = self.cfg()
        self.assertEqual(cfg["QPKG_NAME"], "QnapDownloader")
        self.assertEqual(cfg["QPKG_VER"], webapp.VERSION)
        self.assertEqual(int(cfg["QPKG_WEB_PORT"]), webapp.PORT)
        self.assertEqual(webapp.PORT, downloader_pid.PORT)
        self.assertEqual(cfg["QPKG_SERVICE_PROGRAM"], "downloader.sh")

    def test_no_docker_share_needed_and_data_outside_install(self):
        script = (ROOT / "qpkg/shared/downloader.sh").read_text()
        self.assertIn('$(dirname "$root")/QnapDownloader-data', script)
        self.assertNotIn("data_dir=${DOWNLOADER_DATA_DIR:-/share/Container", script)
        self.assertIn("DOWNLOADER_LAN_ENABLED", script)
        self.assertIn("--secure-cookie", script)
        self.assertNotIn("docker run", script)
        self.assertNotIn("npm build", script)

    def test_all_imported_modules_are_packaged(self):
        script = (ROOT / "scripts/build-qpkg.sh").read_text()
        for name in ("webapp.py", "storage.py", "durability.py",
                     "updater.py", "self_update.py", "jd2_policy.py",
                     "jd2_control.py", "downloader_pid.py", "python3-path"):
            with self.subTest(name=name):
                self.assertIn(name, script)
        self.assertIn('cp -R "$ROOT/web"', script)

    def test_scripts_are_syntactically_valid(self):
        for name in ("qpkg/shared/downloader.sh", "qpkg/shared/python3-path"):
            with self.subTest(path=name):
                subprocess.run(["/bin/sh", "-n", str(ROOT / name)], check=True)

    def test_jd2_controls_fail_closed_without_adapter(self):
        controller = jd2_control.JD2Controller()
        state = controller.status()
        self.assertFalse(state["connected"])
        self.assertFalse(state["can_update"])
        self.assertFalse(state["can_preset"])


if __name__ == "__main__":
    unittest.main()
