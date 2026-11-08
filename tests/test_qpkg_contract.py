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
        for name in ("webapp.py", "webauthn.py", "ttlcache.py", "state_hub.py", "diskio.py", "debuglog.py", "storage.py", "durability.py",
                     "updater.py", "self_update.py", "jd2_policy.py",
                     "jd2_control.py", "jd2_local.py", "engine_components.py", "engine_supervisor.py", "container_runtime.py", "jd2_transfer.py", "jd2_downloads.py", "jd2_accounts.py", "runtime_diagnostics.py", "rapidgator_extract.py", "browser_safety.py", "rb_blocklist.py", "rb_firstload.py", "rb_probe_queue.py", "rb_ptfetch.py", "rb_resident.py", "rb_mirror.py", "remote_browser.py", "ws_hub.py", "selftest.py", "pt_core.py", "pt_proxy.py", "passthrough.py", "downloader_pid.py", "python3-path", "downloader-diagnose"):
            with self.subTest(name=name):
                self.assertIn(name, script)
        self.assertIn('cp -R "$ROOT/web"', script)
        self.assertIn('cp -R "$ROOT/container"', script)

    def test_passthrough_test_hooks_cannot_be_switched_on_in_a_package(self):
        """v0.1.57: the test-only upstream mapping / origin override are plain attributes set by the E2E harness. No start
        script, no command line flag and no environment variable can set them."""
        import inspect
        import re as _re
        shared = [ROOT / "qpkg/shared" / n for n in ("downloader.sh", "downloader_pid.py", "python3-path", "downloader-diagnose")]
        shared.append(ROOT / "qpkg/package_routines")
        for path in shared:
            text = path.read_text(encoding="utf-8")
            for token in ("QD_PT", "pt_test", "test_upstream", "pt-test", "--pt-port", "PT_PORT"):
                self.assertNotIn(token, text, "%s mentions %s" % (path.name, token))
        main = inspect.getsource(webapp.main)
        for token in ("pt_test_host", "pt_test_upstream", "pt_tls_context"):
            self.assertNotIn(token, main)
        for name in ("webapp.py", "passthrough.py", "pt_core.py", "pt_proxy.py"):
            source = (ROOT / name).read_text(encoding="utf-8")
            self.assertFalse(_re.search(r"environ[^\n]*(QD_PT|TEST_UPSTREAM|PT_TEST)", source, _re.I), name)
        self.assertFalse(_re.search(r"add_argument\([^)]*(test.upstream|pt.test)", (ROOT / "webapp.py").read_text()))
        # the listener only ever binds the loopback address
        self.assertIn('("127.0.0.1", self.requested_port)', (ROOT / "passthrough.py").read_text())
        self.assertIn("web/pt-sw.js", "web/pt-sw.js")
        for name in ("app-sw.js", "pt-sw.js", "pt-helper.js", "pt-boot.js", "pt-download.js", "pt-challenge.js"):
            self.assertTrue((ROOT / "web" / name).is_file())
        self.assertIn('cp -R "$ROOT/web"', (ROOT / "scripts/build-qpkg.sh").read_text())      # web/ (and so the pt-*.js files) is packaged

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


class PackageImportClosureTest(unittest.TestCase):
    """v0.1.45: every local module webapp.py imports (transitively) is copied by build-qpkg.sh."""

    def test_build_copies_every_imported_module(self):
        import re as _re
        import subprocess as _sp
        import tempfile as _tf
        import shutil as _sh
        root = Path(__file__).resolve().parents[1]
        build = (root / "scripts" / "build-qpkg.sh").read_text(encoding="utf-8")
        listed = _re.search(r'cp "\$ROOT"/\{([^}]*)\} "\$WORK/shared/"', build).group(1).split(",")
        with _tf.TemporaryDirectory() as tmp:
            for name in listed:
                if name.endswith(".py"):
                    _sh.copy(root / name, Path(tmp) / name)
            result = _sp.run(["python3", "-I", str(root / "scripts" / "check-package-imports.py"), tmp, "webapp.py"],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
