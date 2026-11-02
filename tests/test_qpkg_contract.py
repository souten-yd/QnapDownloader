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
        self.assertEqual(cfg["QPKG_WEBUI"], "/")
        self.assertEqual(cfg["QPKG_WEB_PORT"], "17864")
        self.assertEqual(cfg["QPKG_WEB_SSL_PORT"], "-2")
        self.assertEqual(cfg["QPKG_DESKTOP_APP"], "0")
        self.assertEqual(cfg["QPKG_SERVICE_PORT"], "17864")
        self.assertEqual(cfg["QDK_DATA_DIR_ICONS"], "icons")
        self.assertEqual(cfg["QDK_DATA_DIR_SHARED"], "shared")
        self.assertIn("X-Frame-Options", (ROOT / "webapp.py").read_text())
        self.assertIn("frame-ancestors 'none'", (ROOT / "webapp.py").read_text())


    def test_no_docker_share_needed_and_data_outside_install(self):
        script = (ROOT / "qpkg/shared/downloader.sh").read_text()
        self.assertIn('$(dirname "$root")/QnapDownloader-data', script)
        self.assertNotIn("data_dir=${DOWNLOADER_DATA_DIR:-/share/Container", script)
        self.assertIn("DOWNLOADER_LAN_ENABLED", script)
        self.assertIn("--secure-cookie", script)
        self.assertNotIn("docker run", script)
        self.assertNotIn("npm build", script)

    def test_qts_app_center_private_lan_default_and_override(self):
        launch = (ROOT / "qpkg/shared/downloader.sh").read_text()
        self.assertIn("network_mode=lan", launch)
        self.assertIn('config/network-mode', launch)
        self.assertIn('DOWNLOADER_LAN_ENABLED', launch)
        self.assertIn('if [ "$network_mode" = lan ]; then', launch)
        self.assertIn('--lan --port 17864', launch)
        self.assertIn('network_mode=loopback', launch)
        self.assertIn('--secure-cookie --port 17864', launch)
        self.assertNotIn("docker restart", launch)

    def test_default_logging_never_appends_to_hdd(self):
        script = (ROOT / "qpkg/shared/downloader.sh").read_text()
        self.assertIn('DOWNLOADER_LOG_MODE:-memory', script)
        self.assertIn('memory) log_output=/dev/null', script)
        self.assertIn('disk) log_output="$logfile"', script)
        self.assertIn('> "$log_output" 2>&1 &', script)
        self.assertNotIn('> "$logfile" 2>&1 &', script)
        self.assertIn('DOWNLOADER_LOG_MODE must be memory or disk', script)

    def test_icons_source_and_build_contract(self):
        build = (ROOT / "scripts/build-qpkg.sh").read_text()
        renderer = (ROOT / "scripts/render-icons.py").read_text()
        html = (ROOT / "web/index.html").read_text()
        self.assertIn('scripts/render-icons.py', build)
        self.assertIn('"$WORK/icons"', build)
        self.assertIn('apple-touch-icon.png', renderer)
        self.assertIn('QnapDownloader_80.png', renderer)
        self.assertIn('QnapDownloader_gray.png', renderer)
        self.assertIn('href="/apple-touch-icon.png"', html)
        self.assertIn('href="/icon.svg"', html)
        self.assertTrue((ROOT / "web/icon.svg").exists())
        self.assertIn('downloader-diagnose', build)

    def test_all_imported_modules_are_packaged(self):
        script = (ROOT / "scripts/build-qpkg.sh").read_text()
        for name in ("webapp.py", "ttlcache.py", "state_hub.py", "diskio.py", "debuglog.py", "storage.py", "durability.py",
                     "updater.py", "self_update.py", "jd2_policy.py",
                     "jd2_control.py", "jd2_local.py", "engine_components.py", "engine_supervisor.py", "container_runtime.py", "jd2_transfer.py", "jd2_downloads.py", "jd2_accounts.py", "runtime_diagnostics.py", "rapidgator_extract.py", "browser_safety.py", "rb_blocklist.py", "remote_browser.py", "downloader_pid.py", "python3-path", "downloader-diagnose"):
            with self.subTest(name=name):
                self.assertIn(name, script)
        self.assertIn('cp -R "$ROOT/web"', script)
        self.assertIn('cp -R "$ROOT/container"', script)

    def test_scripts_are_syntactically_valid(self):
        for name in ("qpkg/shared/downloader.sh", "qpkg/shared/python3-path", "qpkg/shared/downloader-diagnose"):
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
