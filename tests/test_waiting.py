"""v0.1.70: why LinkGrabber links wait, the automatic start / clean-up sweeps, "file not found" clean-up."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import engine_supervisor
import jd2_control
import jd2_downloads as dl
import jd2_local as jl
import storage

NAME = "a.part1.rar"


def lg(uuid, availability="ONLINE", name=None, host="rapidgator.net", **extra):
    entry = {"uuid": uuid, "packageUUID": 1, "name": name or "f%d.rar" % uuid, "host": host,
             "availability": availability, "enabled": True,
             "url": "https://rapidgator.net/file/secret", "saveTo": "/share/private"}
    entry.update(extra)
    return entry


class StateTest(unittest.TestCase):
    def test_classification(self):
        keys = dl.download_keys([{"name": "Dup.RAR", "host": "rapidgator.net"}])
        cases = [
            (lg(1), "online"), (lg(2, "OFFLINE"), "offline"), (lg(3, "UNKNOWN"), "unknown"),
            (lg(4, "TEMP_UNKNOWN"), "unknown"), (lg(5, None), "unknown"), (lg(6, enabled=False), "disabled"),
            (lg(7, name="dup.rar"), "duplicate"), (lg(8, name="dup.rar", host="other.com"), "online"),
            (lg(9, name="dup.rar", host=""), "duplicate"), (lg(10, "ONLINE", enabled=False, name="dup.rar"), "disabled"),
        ]
        for entry, want in cases:
            with self.subTest(uuid=entry["uuid"]):
                self.assertEqual(dl.waiting_state(entry, keys), want)

    def test_snapshot_has_reasons_stuck_count_and_no_urls(self):
        keys = dl.download_keys([{"name": "dup.rar", "host": "rapidgator.net"}])
        out = dl.linkgrabber_snapshot([lg(1), lg(2, "OFFLINE"), lg(3, name="dup.rar"), lg(4, "UNKNOWN")], keys)
        self.assertEqual([i["state"] for i in out["items"]], ["online", "offline", "duplicate", "unknown"])
        self.assertEqual(out["items"][0]["reason"], "開始待ち（確認済み）")
        self.assertEqual(out["items"][1]["reason"], "オフライン（ファイルが消えている可能性）")
        self.assertEqual(out["items"][2]["reason"], "同じファイルがダウンロード一覧にあります")
        self.assertEqual(out["stuck"], 2)
        for secret in ("secret", "/share/private", "rapidgator.net/file"):
            self.assertNotIn(secret, str(out))

    def test_missing_download_classification(self):
        yes = [{"finalLinkState": "OFFLINE"}, {"availability": "OFFLINE"},
               {"finalLinkState": "FAILED", "status": "File not found"},
               {"finalLinkState": "FAILED", "status": "File has been removed"}]
        no = [{"finalLinkState": "FAILED_EXISTS", "status": "File already exists"},
              {"finalLinkState": "FAILED", "status": "Connection timeout"},
              {"finalLinkState": "FAILED", "status": "No account / premium needed"},
              {"availability": "UNKNOWN"}, {"availability": "TEMP_UNKNOWN"}, {"availability": "ONLINE"},
              {"finalLinkState": "OFFLINE", "running": True}, {"finalLinkState": "OFFLINE", "finished": True},
              {"status": "File not found"},        # not FAILED: leave alone
              {"finalLinkState": "FINISHED"}, None]
        for link in yes:
            self.assertTrue(dl.is_missing_link(link), link)
        for link in no:
            self.assertFalse(dl.is_missing_link(link), link)


class Fake(jl.LocalAdapter):
    def __init__(self, grabber, downloads=()):
        self.grabber, self.downloads, self.calls = grabber, list(downloads), []

    def _call(self, namespace, method, *args):
        self.calls.append((namespace, method, args))
        key = (namespace, method)
        if key == ("linkgrabberv2", "queryLinks"):
            return list(self.grabber)
        if key == ("downloadsV2", "queryLinks"):
            return list(self.downloads) if args[0].get("startAt") == 0 else []
        return None

    def n(self, namespace, method):
        return [c for c in self.calls if c[:2] == (namespace, method)]


class SweepWaitingTest(unittest.TestCase):
    def test_moves_only_online_non_duplicates_and_starts(self):
        a = Fake([lg(1), lg(2, "OFFLINE"), lg(3, "UNKNOWN"), lg(4, name="dup.rar"), lg(5, enabled=False), lg(6)],
                 [{"name": "dup.rar", "host": "rapidgator.net", "uuid": 99}])
        self.assertEqual(a.sweep_waiting(), {"seen": 6, "moved": 2, "removed": 0})
        self.assertEqual(a.n("linkgrabberv2", "moveToDownloadlist")[0][2], ([1, 6], []))
        self.assertEqual(len(a.n("downloadcontroller", "start")), 1)
        self.assertEqual(a.n("linkgrabberv2", "removeLinks"), [])

    def test_empty_linkgrabber_is_exactly_one_query(self):
        a = Fake([])
        self.assertEqual(a.sweep_waiting(True, True), {"seen": 0, "moved": 0, "removed": 0})
        self.assertEqual([c[:2] for c in a.calls], [("linkgrabberv2", "queryLinks")])

    def test_nothing_online_never_reads_the_download_list_or_starts(self):
        a = Fake([lg(1, "UNKNOWN"), lg(2, "TEMP_UNKNOWN")])
        self.assertEqual(a.sweep_waiting(True, True)["moved"], 0)
        self.assertEqual(len(a.calls), 1)

    def test_clear_missing_removes_only_offline(self):
        a = Fake([lg(1), lg(2, "OFFLINE"), lg(3, "UNKNOWN"), lg(4, "TEMP_UNKNOWN"), lg(5, "OFFLINE", enabled=False),
                  lg(6, name="dup.rar")], [{"name": "dup.rar", "host": "rapidgator.net"}])
        out = a.sweep_waiting(start=False, clear_missing=True)
        self.assertEqual(out, {"seen": 6, "moved": 0, "removed": 1})
        self.assertEqual(a.n("linkgrabberv2", "removeLinks")[0][2], ([2], []))
        self.assertEqual(a.n("linkgrabberv2", "moveToDownloadlist"), [])
        self.assertEqual(a.n("downloadcontroller", "start"), [])
        self.assertEqual(a.n("downloadsV2", "removeLinks"), [])      # no file or download-list call

    def test_download_list_sweep_removes_only_missing_entries(self):
        a = Fake([], [
            {"uuid": 1, "finalLinkState": "OFFLINE"}, {"uuid": 2, "finalLinkState": "FAILED", "status": "File not found"},
            {"uuid": 3, "finalLinkState": "FAILED_EXISTS"}, {"uuid": 4, "finalLinkState": "FAILED", "status": "timeout"},
            {"uuid": 5, "availability": "UNKNOWN"}, {"uuid": 6, "running": True, "availability": "OFFLINE"},
            {"uuid": 7, "finished": True}])
        self.assertEqual(a.sweep_missing_downloads(), 2)
        self.assertEqual(a.n("downloadsV2", "removeLinks")[0][2], ([1, 2], []))
        self.assertEqual(a.n("downloadsV2", "cleanup"), [])
        b = Fake([], [{"uuid": 4, "status": "ok"}])
        self.assertEqual(b.sweep_missing_downloads(), 0)
        self.assertEqual(b.n("downloadsV2", "removeLinks"), [])

    def test_existing_scan_still_separates_exists_from_missing(self):
        a = Fake([], [{"uuid": 1, "finalLinkState": "FAILED_EXISTS"}, {"uuid": 2, "finalLinkState": "OFFLINE"}])
        self.assertEqual(a._scan_links(), (0, [1]))


class SupervisorTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = storage.Store(Path(tmp.name))
        self.addCleanup(self.store.close)
        self.m = engine_supervisor.EngineManager(tmp.name, self.store)
        self.adapter = mock.Mock()
        self.adapter.sweep_waiting.return_value = {"seen": 3, "moved": 2, "removed": 1}
        self.adapter.sweep_missing_downloads.return_value = 4
        self.m.adapter = self.adapter
        self.clock = [1000.0]
        self.m._now = lambda: self.clock[0]
        patch = mock.patch.object(self.m, "is_ours_running", return_value=True)
        self.running = patch.start()
        self.addCleanup(patch.stop)

    def test_defaults_sweep_starts_and_clears(self):
        with mock.patch.object(engine_supervisor.debuglog, "log") as log:
            self.assertEqual(self.m.auto_start_waiting(), 2)
        self.adapter.sweep_waiting.assert_called_once_with(True, True)
        self.assertEqual(log.call_args.args[:2], ("engine", "debug"))
        self.assertEqual(log.call_args.args[3]["moved"], 2)
        self.assertEqual(self.m.pop_missing_cleared(), 1)
        self.assertEqual(self.m.pop_missing_cleared(), 0)

    def test_interval_is_respected(self):
        self.m.auto_start_waiting()
        self.m.auto_start_waiting()
        self.clock[0] += engine_supervisor.WAITING_INTERVAL - 1
        self.m.auto_start_waiting()
        self.assertEqual(self.adapter.sweep_waiting.call_count, 1)
        self.clock[0] += 2
        self.m.auto_start_waiting()
        self.assertEqual(self.adapter.sweep_waiting.call_count, 2)

    def test_autostart_off_does_not_start_but_may_clear(self):
        self.store.set_option("send_autostart", False)
        self.m.auto_start_waiting()
        self.adapter.sweep_waiting.assert_called_once_with(False, True)

    def test_both_off_or_engine_not_running_sends_nothing(self):
        self.store.set_option("send_autostart", False)
        self.store.set_option("auto_clear_missing", False)
        self.assertEqual(self.m.auto_start_waiting(), 0)
        self.assertEqual(self.m.auto_clear_missing_downloads(), 0)
        self.store.set_option("auto_clear_missing", True)
        self.store.set_option("send_autostart", True)
        self.running.return_value = False
        self.clock[0] += 1000
        self.assertEqual(self.m.auto_start_waiting(), 0)
        self.assertEqual(self.m.auto_clear_missing_downloads(), 0)
        self.adapter.sweep_waiting.assert_not_called()
        self.adapter.sweep_missing_downloads.assert_not_called()

    def test_missing_downloads_counted_logged_interval_and_option(self):
        with mock.patch.object(engine_supervisor.debuglog, "log") as log:
            self.assertEqual(self.m.auto_clear_missing_downloads(), 4)
            self.assertEqual(self.m.auto_clear_missing_downloads(), 0)       # too soon
        self.assertEqual(log.call_args.args[3], {"downloads": 4})
        self.assertEqual(self.m.pop_missing_cleared(), 4)
        self.store.set_option("auto_clear_missing", False)
        self.clock[0] += 1000
        self.assertEqual(self.m.auto_clear_missing_downloads(), 0)
        self.assertEqual(self.adapter.sweep_missing_downloads.call_count, 1)

    def test_errors_never_raise(self):
        self.adapter.sweep_waiting.side_effect = jl.LocalUnavailable("down")
        self.assertEqual(self.m.auto_start_waiting(), 0)

    def test_default_is_on(self):
        self.assertIs(self.store.settings()["auto_clear_missing"], True)


class Ctl(jd2_control.JD2Controller):
    pass


class CleanTest(unittest.TestCase):
    def setUp(self):
        self.a = mock.Mock()
        self.a.list_linkgrabber.return_value = {"items": [
            {"id": "1", "state": "online"}, {"id": "2", "state": "offline"}, {"id": "3", "state": "duplicate"},
            {"id": "4", "state": "unknown"}, {"id": "5", "state": "disabled"}, {"id": None, "state": "offline"}]}
        self.c = jd2_control.JD2Controller(self.a)

    def test_needs_confirmation(self):
        for v in (False, None, "yes"):
            with self.assertRaises(ValueError):
                self.c.waiting_clean(confirmed=v)
        self.a.remove_waiting.assert_not_called()

    def test_removes_only_offline_and_duplicate_from_a_fresh_snapshot(self):
        self.assertEqual(self.c.waiting_clean(confirmed=True), {"ok": True, "count": 2})
        self.a.remove_waiting.assert_called_once_with([2, 3])
        self.a.delete_with_files.assert_not_called()


if __name__ == "__main__":
    unittest.main()
