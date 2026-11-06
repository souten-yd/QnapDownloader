"""v0.1.56: auto-clear options (Links tab sent rows, Downloads tab finished/skipped entries)."""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import engine_supervisor
import jd2_local as jl
import storage
import webapp


class Adapter(jl.LocalAdapter):
    """Fake JD2 link list; records the list-only calls."""
    def __init__(self, links):
        self.links, self.calls = links, []

    def _call(self, namespace, method, *args):
        self.calls.append((namespace, method))
        if (namespace, method) == ("downloadsV2", "queryLinks"):
            return list(self.links) if args[0]["startAt"] == 0 else []
        if (namespace, method) in (("downloadsV2", "cleanup"), ("downloadsV2", "removeLinks")):
            return None
        raise AssertionError((namespace, method))

    def n(self, method):
        return len([c for c in self.calls if c[1] == method])


class SweepFinishedTest(unittest.TestCase):
    def test_nothing_finished_is_one_read_only_query(self):
        a = Adapter([{"uuid": 1, "status": "Downloading", "finished": False},
                     {"uuid": 2, "skipped": True, "status": "Skipped - No account"},
                     {"uuid": 3, "finalLinkState": "FAILED", "status": "Failed"}])
        self.assertEqual(a.sweep_finished(), (0, 0))
        self.assertEqual(a.calls, [("downloadsV2", "queryLinks")])

    def test_finished_and_exists_use_the_button_calls_only(self):
        a = Adapter([{"uuid": 1, "finished": True}, {"uuid": 2, "finalLinkState": "FAILED_EXISTS"},
                     {"uuid": 3, "status": "Waiting"}])
        self.assertEqual(a.sweep_finished(), (1, 1))
        self.assertEqual((a.n("cleanup"), a.n("removeLinks")), (1, 1))
        removed = [c for c in a.calls if c[1] == "removeLinks"]
        self.assertEqual(len(removed), 1)


class Manager(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = storage.Store(Path(tmp.name))
        self.addCleanup(self.store.close)
        self.m = engine_supervisor.EngineManager(tmp.name, self.store)
        self.adapter = mock.Mock()
        self.adapter.sweep_finished.return_value = (2, 1)
        self.m.adapter = self.adapter
        self.clock = [1000.0]
        self.m._now = lambda: self.clock[0]
        patch = mock.patch.object(self.m, "is_ours_running", return_value=True)
        self.running = patch.start()
        self.addCleanup(patch.stop)


class AutoSweepTest(Manager):
    def test_defaults_off_nothing_happens(self):
        self.assertIs(self.store.settings()["auto_clear_downloads"], False)
        self.assertIs(self.store.settings()["auto_clear_links"], False)
        self.assertEqual(self.m.auto_sweep(), (0, 0))
        self.adapter.sweep_finished.assert_not_called()

    def test_on_sweeps_logs_and_respects_interval(self):
        self.store.set_option("auto_clear_downloads", True)
        with mock.patch.object(engine_supervisor.debuglog, "log") as log:
            self.assertEqual(self.m.auto_sweep(), (2, 1))
            self.assertEqual(self.m.auto_sweep(), (0, 0))        # too soon
            self.clock[0] += engine_supervisor.AUTO_CLEAR_INTERVAL - 1
            self.assertEqual(self.m.auto_sweep(), (0, 0))
            self.clock[0] += 2
            self.assertEqual(self.m.auto_sweep(), (2, 1))
        self.assertEqual(self.adapter.sweep_finished.call_count, 2)
        self.assertEqual(log.call_args_list[0].args[:2], ("engine", "debug"))
        self.assertEqual(log.call_args_list[0].args[3], {"finished": 2, "skipped": 1})

    def test_no_jd2_calls_when_engine_not_running(self):
        self.store.set_option("auto_clear_downloads", True)
        self.running.return_value = False
        self.assertEqual(self.m.auto_sweep(), (0, 0))
        self.adapter.sweep_finished.assert_not_called()

    def test_nothing_finished_logs_nothing_and_errors_are_swallowed(self):
        self.store.set_option("auto_clear_downloads", True)
        self.adapter.sweep_finished.return_value = (0, 0)
        with mock.patch.object(engine_supervisor.debuglog, "log") as log:
            self.assertEqual(self.m.auto_sweep(), (0, 0))
            log.assert_not_called()
            self.clock[0] += 100
            self.adapter.sweep_finished.side_effect = jl.LocalUnavailable("down")
            self.assertEqual(self.m.auto_sweep(), (0, 0))

    def test_links_sweep_removes_only_sent(self):
        ids = [self.store.add_link("https://example.com/%d" % i)["id"] for i in range(4)]
        self.store.begin_send(ids[0]); self.store.finish_send(ids[0], "sent")
        self.store.begin_send(ids[1]); self.store.finish_send(ids[1], "uncertain")
        self.store.begin_send(ids[2])
        self.m.auto_sweep()
        self.assertEqual(len(self.store.links()), 4)              # option OFF: nothing removed
        self.store.set_option("auto_clear_links", True)
        self.clock[0] += 100
        self.m.auto_sweep()
        self.assertEqual(sorted(l["state"] for l in self.store.links()), ["sending", "staged", "uncertain"])


class LinksAutoRemoveTest(Manager):
    def sent_and_failed(self):
        ids = [self.store.add_link("https://example.com/%d" % i)["id"] for i in range(3)]
        self.store.begin_send(ids[0]); self.store.finish_send(ids[0], "sent")
        self.store.begin_send(ids[1]); self.store.finish_send(ids[1], "uncertain")
        return ids

    def test_off_removes_nothing(self):
        ids = self.sent_and_failed()
        self.assertEqual(self.store.remove_sent_links(ids[0]), 0)
        with mock.patch.object(threading, "Timer") as timer:
            webapp.schedule_sent_removal(self.store, ids[0])
            timer.assert_not_called()
        self.assertEqual(len(self.store.links()), 3)

    def test_on_removes_only_the_sent_row(self):
        ids = self.sent_and_failed()
        self.store.set_option("auto_clear_links", True)
        self.assertEqual(self.store.remove_sent_links(ids[1]), 0)   # uncertain stays
        self.assertEqual(self.store.remove_sent_links(ids[2]), 0)   # staged stays
        with mock.patch.object(webapp, "LINK_AUTOREMOVE_DELAY", 0.0):
            webapp.schedule_sent_removal(self.store, ids[0])
            deadline = time.time() + 3
            while time.time() < deadline and len(self.store.links()) == 3:
                time.sleep(0.02)
        self.assertEqual(sorted(l["state"] for l in self.store.links()), ["staged", "uncertain"])


if __name__ == "__main__":
    unittest.main()
