"""v0.1.47: JD2 must save flat into the chosen folder (no per-package subfolders)."""
import json
import tempfile
import threading
import unittest
import unittest.mock
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import jd2_control
import jd2_downloads
import jd2_local as jl
import jd2_transfer

DEST = "/downloads"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        name, _, query = self.path.partition("?")
        args = [json.loads(urllib.parse.unquote(a)) for a in query.split("&")] if query else []
        self.server.calls.append((name, args))
        body = json.dumps({"data": self.server.respond(name, args)}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Engine:
    preset_applied = True

    def __init__(self, port):
        self.port = port

    def connection_target(self):
        return self.port

    def is_ours_running(self):
        return True

    def path_mapping(self):
        return None


class FakeServerCase(unittest.TestCase):
    def setUp(self):
        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.server.calls = []
        self.lg_packages = []
        self.dl_packages = []
        self.crawling = [True]
        self.server.respond = self.respond
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close()))
        self.adapter = jl.LocalAdapter(Engine(self.server.server_port))
        self.adapter._flatten_sleep = lambda seconds: None

    def respond(self, name, args):
        if name == "/linkgrabberv2/queryPackages":
            return self.lg_packages
        if name == "/downloadsV2/queryPackages":
            return self.dl_packages
        if name == "/linkgrabberv2/queryLinkCrawlerJobs":
            alive = self.crawling.pop(0) if len(self.crawling) > 1 else self.crawling[0]
            return [{"jobId": 9, "crawling": alive, "checking": False}] if alive else []
        if name == "/linkgrabberv2/isCollecting":
            return False
        return None

    def calls(self, name):
        return [a for n, a in self.server.calls if n == name]


class LinkGrabberFlattenTests(FakeServerCase):
    def test_subfolder_packages_are_pointed_at_the_destination(self):
        self.lg_packages = [
            {"uuid": 1, "name": "a", "saveTo": DEST + "/a", "childCount": 1},
            {"uuid": 2, "name": "b", "saveTo": DEST, "childCount": 1},                 # already flat
            {"uuid": 3, "name": "c", "saveTo": "/downloads2/c", "childCount": 1},      # sibling prefix
            {"uuid": 4, "name": "d", "saveTo": "/elsewhere/d", "childCount": 1},
            {"uuid": 5, "name": "e", "saveTo": DEST + "/x/../../etc", "childCount": 1},
            {"uuid": True, "saveTo": DEST + "/t"}, {"uuid": 6, "saveTo": 5},
            {"uuid": 7, "name": "g", "saveTo": DEST + "/deep/er", "childCount": 2},
        ]
        self.assertEqual(self.adapter.flatten_linkgrabber(DEST), 2)
        query = self.calls("/linkgrabberv2/queryPackages")[0][0]
        for field in ("uuid", "name", "saveTo", "childCount"):
            self.assertIs(query[field], True)
        self.assertEqual(self.calls("/linkgrabberv2/setDownloadDirectory"), [[DEST, [1, 7]]])
        self.assertEqual(self.calls("/downloadsV2/setDownloadDirectory"), [])

    def test_nothing_to_do_makes_no_write(self):
        self.lg_packages = [{"uuid": 2, "saveTo": DEST}]
        self.assertEqual(self.adapter.flatten_linkgrabber(DEST), 0)
        self.assertEqual(self.calls("/linkgrabberv2/setDownloadDirectory"), [])

    def test_bad_destination_is_refused_before_any_call(self):
        for bad in ("/", "relative", None):
            with self.assertRaises(jl.LocalUnavailable):
                self.adapter.flatten_linkgrabber(bad)
        self.assertEqual(self.server.calls, [])

    def test_background_worker_polls_until_the_crawler_is_done(self):
        self.lg_packages = [{"uuid": 1, "saveTo": DEST + "/pkg"}]
        self.crawling = [True, True, False]
        self.adapter.schedule_flatten(DEST, {"id": 9})
        self.adapter.flatten_join()
        self.assertGreaterEqual(len(self.calls("/linkgrabberv2/setDownloadDirectory")), 1)
        self.assertEqual(self.calls("/linkgrabberv2/queryLinkCrawlerJobs")[0], [{"jobIds": [9]}])
        self.assertIsNone(self.adapter._flatten_thread)
        self.assertEqual(self.adapter._flatten_jobs, [])

    def test_without_a_job_id_isCollecting_decides(self):
        self.adapter.schedule_flatten(DEST, True)
        self.adapter.flatten_join()
        self.assertTrue(self.calls("/linkgrabberv2/isCollecting"))
        self.assertFalse(self.calls("/linkgrabberv2/queryLinkCrawlerJobs"))

    def test_worker_gives_up_after_the_window(self):
        clock = [0.0]
        self.adapter._flatten_now = lambda: clock[0]
        self.adapter._flatten_sleep = lambda seconds: clock.__setitem__(0, clock[0] + 5)
        self.crawling = [True]
        self.adapter.schedule_flatten(DEST, {"id": 9})
        self.adapter.flatten_join()
        self.assertLessEqual(len(self.calls("/linkgrabberv2/queryPackages")), 5)
        self.assertEqual(self.adapter._flatten_jobs, [])

    def test_errors_in_the_worker_do_not_leave_it_stuck(self):
        attempts = []

        def cycle(jobs):
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("boom")
            self.adapter._flatten_jobs.clear()      # what the real cycle does at the deadline

        self.adapter._flatten_cycle = cycle
        self.adapter.schedule_flatten(DEST, None)
        self.adapter.flatten_join(5)
        self.assertEqual(len(attempts), 2)
        self.assertIsNone(self.adapter._flatten_thread)


class DownloadsSweepTests(FakeServerCase):
    def package(self, uuid, save_to, loaded=0, running=False, finished=False):
        return {"uuid": uuid, "saveTo": save_to, "bytesLoaded": loaded,
                "running": running, "finished": finished}

    def test_only_not_started_packages_inside_the_folder_move(self):
        self.dl_packages = [
            self.package(1, DEST + "/idle"),                           # moves
            self.package(2, DEST + "/started", loaded=10),             # partial files in old folder
            self.package(3, DEST + "/running", running=True),
            self.package(4, DEST + "/done", finished=True),
            self.package(5, "/other/idle"),                            # outside
            self.package(6, DEST),                                     # already flat
            self.package(7, DEST + "/n/m"),                            # moves (nested)
            {"uuid": 8, "saveTo": DEST + "/missing-flags"},            # unknown state: leave it
            self.package(9, DEST + "/none", loaded=None),
            self.package(10, DEST + "/str", loaded="0"),
        ]
        self.assertEqual(self.adapter.flatten_waiting_downloads(DEST), 2)
        self.assertEqual(self.calls("/downloadsV2/setDownloadDirectory"), [[DEST, [1, 7]]])
        query = self.calls("/downloadsV2/queryPackages")[0][0]
        for field in ("uuid", "saveTo", "bytesLoaded", "running", "finished"):
            self.assertIs(query[field], True)
        self.assertEqual(self.calls("/linkgrabberv2/setDownloadDirectory"), [])

    def test_nothing_to_move_makes_no_write(self):
        self.dl_packages = [self.package(1, DEST), self.package(2, DEST + "/x", loaded=1)]
        self.assertEqual(self.adapter.flatten_waiting_downloads(DEST), 0)
        self.assertEqual(self.calls("/downloadsV2/setDownloadDirectory"), [])

    def test_large_batches_are_chunked(self):
        self.dl_packages = [self.package(i, DEST + "/p%d" % i) for i in range(120)]
        self.assertEqual(self.adapter.flatten_waiting_downloads(DEST), 120)
        self.assertEqual([len(c[1]) for c in self.calls("/downloadsV2/setDownloadDirectory")], [50, 50, 20])

    def test_flatten_waiting_resolves_the_destination_through_the_folder_mapping(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "Download"
            (root / "Manga").mkdir(parents=True)
            self.adapter._engine.path_mapping = lambda: {"nas_root": str(root), "jd2_root": "/downloads"}
            self.dl_packages = [self.package(1, "/downloads/Manga/pkg"),
                                self.package(2, "/downloads/pkg")]       # outside the chosen subfolder
            self.assertEqual(jd2_transfer.flatten_waiting(self.adapter, str(root / "Manga")), 1)
            self.assertEqual(self.calls("/downloadsV2/setDownloadDirectory"),
                             [["/downloads/Manga", [1]]])
            self.server.calls.clear()
            self.assertEqual(jd2_transfer.flatten_waiting(self.adapter, "/nonexistent"), 0)
            self.assertEqual(self.server.calls, [])


class TransferHookTests(unittest.TestCase):
    def test_transfer_schedules_flatten_after_a_successful_add(self):
        calls = []

        class Adapter:
            _options = {"allow_link_submit": True, "allow_unverified_packagizer": True,
                        "same_host": True}

            def inspect_preset(self):
                return {"entries": {k: {"supported": True, "current": False}
                                    for k in ("linkgrabber.auto_confirm", "linkgrabber.auto_start")}}

            def add_link(self, url, dest, autostart=False):
                calls.append("add")
                return {"id": 3}

            def schedule_flatten(self, destination, result):
                calls.append(("flatten", destination, result))

        with tempfile.TemporaryDirectory() as temp:
            resolved = str(Path(temp).resolve())
            jd2_transfer.transfer(Adapter(), "https://example.com/a", temp)
        self.assertEqual(calls, ["add", ("flatten", resolved, {"id": 3})])

    def test_refused_add_does_not_schedule_and_a_broken_hook_never_fails_the_send(self):
        class Adapter:
            _options = {"allow_link_submit": True, "allow_unverified_packagizer": True,
                        "same_host": True}
            result = False
            scheduled = 0

            def inspect_preset(self):
                return {"entries": {k: {"supported": True, "current": False}
                                    for k in ("linkgrabber.auto_confirm", "linkgrabber.auto_start")}}

            def add_link(self, url, dest, autostart=False):
                return self.result

            def schedule_flatten(self, destination, result):
                Adapter.scheduled += 1
                raise RuntimeError("hook broke")

        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(jd2_transfer.TransferBlocked):
                jd2_transfer.transfer(Adapter(), "https://example.com/a", temp)
            self.assertEqual(Adapter.scheduled, 0)
            Adapter.result = True
            self.assertEqual(jd2_transfer.transfer(Adapter(), "https://example.com/a", temp)["state"], "sent")
            self.assertEqual(Adapter.scheduled, 1)


class PackageFolderKeyTests(FakeServerCase):
    GS = "org.jdownloader.settings.GeneralSettings"
    LG = "org.jdownloader.gui.views.linkgrabber.addlinksdialog.LinkgrabberSettings"
    PZ = "org.jdownloader.controlling.packagizer.PackagizerSettings"

    def listing(self, entries):
        self.entries = entries
        self.values = {e["key"]: e["value"] for e in entries}

        def respond(name, args):
            if name == "/config/list":
                return self.entries
            if name == "/config/get":
                return self.values.get(args[2])
            if name == "/config/set":
                self.values[args[2]] = args[3]
                return True
            return None
        self.server.respond = respond

    def test_boolean_and_threshold_keys_are_found_and_turned_off(self):
        self.listing([
            {"interfaceName": self.LG, "key": "CreateSubFoldersEnabled", "storage": "cfg/lg", "value": True},
            {"interfaceName": self.GS, "key": "PackageNameSubfolderThreshold", "storage": None, "value": 2},
            {"interfaceName": self.PZ, "key": "SubFolderByPackageName", "storage": None, "value": False},
            {"interfaceName": self.GS, "key": "DefaultDownloadFolder", "storage": None, "value": "/x"},
            {"interfaceName": self.GS, "key": "DeleteEmptySubFoldersAfterDeletingDownloadedFilesEnabled",
             "storage": None, "value": True},                      # handled by the required preset
            {"interfaceName": self.LG, "key": "DisableSubfolders", "storage": None, "value": False},
            {"interfaceName": "org.other.Settings", "key": "SubFolder", "storage": None, "value": True},
            {"interfaceName": self.GS, "key": "SubfolderName", "storage": None, "value": "abc"},  # wrong type
        ])
        report = self.adapter.package_folder_settings()
        self.assertEqual([(r["key"], r["current"], r["target"], r["compliant"]) for r in report], [
            ("CreateSubFoldersEnabled", True, False, False),
            ("PackageNameSubfolderThreshold", 2, 100000, False),
            ("SubFolderByPackageName", False, False, True)])
        self.assertEqual(self.calls("/config/set"), [])               # read-only without apply
        applied = self.adapter.package_folder_settings(True)
        self.assertTrue(all(r["compliant"] for r in applied))
        self.assertEqual(self.values["CreateSubFoldersEnabled"], False)
        self.assertEqual(self.values["PackageNameSubfolderThreshold"], 100000)
        sets = self.calls("/config/set")
        self.assertEqual(sets[0], [self.LG, "cfg/lg", "CreateSubFoldersEnabled", False])
        self.assertEqual(len(sets), 2)                                # compliant key not rewritten

    def test_no_such_keys_means_no_report_and_no_write(self):
        self.listing([{"interfaceName": self.GS, "key": "DefaultDownloadFolder", "storage": None, "value": "/x"}])
        self.assertEqual(self.adapter.package_folder_settings(True), [])
        self.assertEqual(self.calls("/config/set"), [])

    def test_rejected_write_is_reported_not_hidden(self):
        self.listing([{"interfaceName": self.LG, "key": "CreateSubfolders", "storage": None, "value": True}])
        base = self.server.respond
        self.server.respond = lambda n, a: False if n == "/config/set" else base(n, a)
        report = self.adapter.package_folder_settings(True)
        self.assertFalse(report[0]["compliant"])

    def test_controller_attaches_the_report_to_preview_and_apply(self):
        adapter = unittest.mock.Mock()
        adapter.package_folder_settings.return_value = [{"key": "k", "compliant": False}]
        ctrl = jd2_control.JD2Controller(adapter)
        with unittest.mock.patch.object(jd2_control, "build_preview",
                                        return_value={"applicable": True, "entries": []}), \
                unittest.mock.patch.object(jd2_control, "apply_preset", return_value={"entries": []}):
            self.assertEqual(ctrl.preset_preview()["package_folders"], [{"key": "k", "compliant": False}])
            adapter.package_folder_settings.assert_called_with(False)
            result = ctrl.preset_reapply(confirmed=True)
            adapter.package_folder_settings.assert_called_with(True)
            self.assertEqual(result["package_folders"], [{"key": "k", "compliant": False}])
            adapter.package_folder_settings.side_effect = RuntimeError("x")
            ctrl._preview = {"applicable": True}
            self.assertEqual(ctrl.preset_reapply(confirmed=True)["package_folders"], [])


class SnapshotTests(unittest.TestCase):
    def test_folder_marker_is_relative_and_never_an_absolute_path(self):
        packages = [
            {"uuid": 1, "name": "flat", "saveTo": "/downloads"},
            {"uuid": 2, "name": "sub", "saveTo": "/downloads/Some Pack"},
            {"uuid": 3, "name": "outside", "saveTo": "/private/secret"},
            {"uuid": 4, "name": "none"},
            {"uuid": 5, "name": "dots", "saveTo": "/downloads/../etc"},
            {"uuid": 6, "name": "ctl", "saveTo": "/downloads/a\x00b\nc"},
        ]
        out = jd2_downloads.snapshot(packages, [], 0, "/downloads")
        folders = [p["folder"] for p in out["packages"]]
        self.assertEqual(folders[0], {"flat": True})
        self.assertEqual(folders[1], {"flat": False, "name": "Some Pack"})
        self.assertIsNone(folders[2])
        self.assertIsNone(folders[3])
        self.assertIsNone(folders[4])
        self.assertIsNone(folders[5])
        text = json.dumps(out, ensure_ascii=False)
        self.assertNotIn("/downloads", text)
        self.assertNotIn("/private", text)
        self.assertNotIn("saveTo", text)

    def test_without_a_base_no_folder_is_reported(self):
        out = jd2_downloads.snapshot([{"uuid": 1, "name": "x", "saveTo": "/downloads/x"}], [], 0)
        self.assertIsNone(out["packages"][0]["folder"])
        self.assertIs(jd2_downloads.PACKAGE_FIELDS["saveTo"], True)

    def test_controller_passes_the_resolved_base(self):
        with tempfile.TemporaryDirectory() as temp:
            adapter = unittest.mock.Mock()
            adapter._options = {"same_host": True}
            adapter.list_downloads.return_value = {"packages": []}
            ctrl = jd2_control.JD2Controller(adapter)
            ctrl.downloads_snapshot(2, temp)
            adapter.list_downloads.assert_called_with(2, str(Path(temp).resolve()))
            ctrl.downloads_snapshot(1, None)
            adapter.list_downloads.assert_called_with(1)

    def test_adapter_list_downloads_uses_save_to_but_returns_no_path(self):
        class Case(FakeServerCase):
            def runTest(self):
                pass
        case = Case()
        case.setUp()
        try:
            case.server.respond = lambda name, args: (
                [{"uuid": 4, "name": "p", "saveTo": "/downloads/p", "childCount": 1}]
                if name == "/downloadsV2/queryPackages" else [])
            out = case.adapter.list_downloads(0, "/downloads")
            self.assertEqual(out["packages"][0]["folder"], {"flat": False, "name": "p"})
            self.assertIs(case.calls("/downloadsV2/queryPackages")[0][0]["saveTo"], True)
            self.assertNotIn("saveTo", json.dumps(out))
        finally:
            case.doCleanups()


class EngineSweepTests(unittest.TestCase):
    def setUp(self):
        import engine_supervisor
        self.es = engine_supervisor
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name) / "dl"
        self.folder.mkdir()

        class Store:
            def settings(inner):
                return {"download_dir": str(self.folder)}
        self.manager = engine_supervisor.EngineManager(self.temp.name, Store())
        self.adapter = unittest.mock.Mock()
        self.adapter._options = {"same_host": True}
        self.adapter.flatten_waiting_downloads.return_value = 3
        self.manager.adapter = self.adapter

    def test_sweep_runs_for_our_running_engine_against_the_hub_folder(self):
        with unittest.mock.patch.object(self.manager, "is_ours_running", return_value=True):
            self.assertEqual(self.manager.flatten_sweep(), 3)
        self.adapter.flatten_waiting_downloads.assert_called_once_with(str(self.folder.resolve()))

    def test_sweep_does_nothing_when_the_engine_is_not_ours_or_not_running(self):
        with unittest.mock.patch.object(self.manager, "is_ours_running", return_value=False):
            self.assertEqual(self.manager.flatten_sweep(), 0)
        self.adapter.flatten_waiting_downloads.assert_not_called()

    def test_sweep_never_raises(self):
        self.adapter.flatten_waiting_downloads.side_effect = jl.LocalUnavailable("down")
        with unittest.mock.patch.object(self.manager, "is_ours_running", return_value=True):
            self.assertEqual(self.manager.flatten_sweep(), 0)

    def test_supervisor_sweeps_every_sixty_seconds_while_ready(self):
        clock = [0.0]
        loops = [0]
        manager = self.manager
        manager._now = lambda: clock[0]
        manager._ready = True

        def wait_exit(timeout):
            clock[0] += 20.0
            loops[0] += 1
            if loops[0] >= 10:
                manager._stop_evt.set()
            return False
        manager._wait_exit = wait_exit
        with unittest.mock.patch.object(manager, "_alive", return_value=True), \
                unittest.mock.patch.object(manager, "flatten_sweep") as sweep:
            manager._supervise()
        self.assertEqual(sweep.call_count, 3)       # at t=60,120,180 of 200 s

    def test_the_interval_constant(self):
        self.assertEqual(self.es.FLATTEN_SWEEP_INTERVAL, 60.0)


if __name__ == "__main__":
    unittest.main()
