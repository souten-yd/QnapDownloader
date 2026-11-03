"""v0.1.38 resident NAS browser: watchdog state machine, memory figures, resident start, client checks."""
import os
import re
import tempfile
import unittest
from pathlib import Path

import container_runtime as cr
import rb_resident as rr
import remote_browser as rb
import test_remote_browser as base

ROOT = Path(__file__).resolve().parent.parent
MIB = 1024 * 1024


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class ReasonTests(unittest.TestCase):
    def test_reason_texts(self):
        self.assertEqual(rr.reason_text({"oom": True, "exit_code": 137}), "メモリ不足で強制終了 / 終了コード 137")
        self.assertEqual(rr.reason_text({"exit_code": 137}), "強制終了（SIGKILL） / 終了コード 137")
        self.assertEqual(rr.reason_text({"exit_code": 1, "error": "boom\nline"}), "boom line / 終了コード 1")
        self.assertEqual(rr.reason_text({"lost": True}), "ブラウザとの接続が切れました")
        self.assertEqual(rr.reason_text({}), "予期しない停止")
        self.assertEqual(rr.reason_text({"exit_code": 72}), "予期しない停止 / 終了コード 72")


class WatchdogTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.wd = rr.Watchdog(self.clock, lambda: 1700000000)

    def cycle(self, info=None):
        """One unexpected stop and the following restart attempt; returns the delay used."""
        before = self.clock.now
        self.wd.exited(info or {"exit_code": 137, "oom": True})
        if self.wd.state == rr.STATE_GAVE_UP:
            return None
        delay = self.wd.next_at - before
        self.assertFalse(self.wd.due())
        self.clock.now = self.wd.next_at
        self.assertTrue(self.wd.due())
        self.wd.begin_attempt()
        self.wd.succeeded()
        return delay

    def test_backoff_5_15_60_and_cap_of_five_per_window(self):
        delays = [self.cycle() for _ in range(5)]
        self.assertEqual(delays, [5, 15, 60, 60, 60])
        self.assertEqual(self.wd.restarts, 5)
        self.assertIsNone(self.cycle())            # the sixth stop inside 15 minutes
        self.assertEqual(self.wd.state, rr.STATE_GAVE_UP)
        self.assertEqual(self.wd.message(),
                         "NASブラウザが繰り返し停止したため自動再起動を止めました"
                         "（理由: メモリ不足で強制終了 / 終了コード 137）")
        self.assertFalse(self.wd.due())

    def test_window_slides_and_retry_clears(self):
        for _ in range(5):
            self.cycle()
        self.clock.now += rr.WINDOW
        self.assertEqual(self.cycle(), 5)           # old attempts left the window
        for _ in range(5):
            self.cycle()
        self.assertEqual(self.wd.state, rr.STATE_GAVE_UP)
        self.wd.retry()
        self.assertEqual(self.wd.state, rr.STATE_SCHEDULED)
        self.assertTrue(self.wd.due())
        self.assertEqual(len(self.wd.attempts), 0)

    def test_memory_wait_uses_no_attempt(self):
        self.wd.exited({"exit_code": 137})
        self.clock.now = self.wd.next_at
        self.wd.memory_blocked()
        self.assertEqual(self.wd.state, rr.STATE_MEMORY)
        self.assertEqual(self.wd.message(), "メモリ不足のため待機中")
        self.assertEqual(len(self.wd.attempts), 0)
        self.assertFalse(self.wd.due())
        self.clock.now += rr.MEMORY_RECHECK
        self.assertTrue(self.wd.due())

    def test_failed_start_counts_like_a_stop_and_events_are_capped(self):
        self.wd.exited({"exit_code": 1})
        self.clock.now = self.wd.next_at
        self.wd.begin_attempt()
        self.wd.failed("起動できません")
        self.assertEqual(self.wd.next_at - self.clock.now, 15)
        for _ in range(40):
            self.wd.event("x", "y")
        self.assertEqual(len(self.wd.snapshot()["events"]), rr.EVENT_CAP)

    def test_cancel_and_blocked(self):
        self.wd.exited({"exit_code": 1})
        self.wd.cancel()
        self.assertEqual(self.wd.state, rr.STATE_IDLE)
        self.wd.disarm("image")
        self.assertEqual(self.wd.state, rr.STATE_BLOCKED)
        self.assertFalse(self.wd.due())
        self.wd.unblock()
        self.assertEqual(self.wd.state, rr.STATE_IDLE)


class IdleBaselineTests(unittest.TestCase):
    def test_min_latest_time_and_percent(self):
        idle = rr.IdleBaseline(lambda: 1700000000.9)
        self.assertIsNone(idle.snapshot())
        self.assertFalse(idle.record(0))
        idle.record(420 * MIB, 300 * MIB)
        idle.record(450 * MIB)
        snap = idle.snapshot(16000000)
        self.assertEqual((snap["latest_mib"], snap["min_mib"], snap["samples"], snap["at"]),
                         (450, 420, 2, 1700000000))
        self.assertEqual(snap["percent_of_total"], round(450 * MIB / (16000000 * 1024) * 100, 1))
        self.assertIsNone(snap["anon_mib"])


class CgroupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def test_v2_current_with_anon_and_file(self):
        self.write("a/memory.current", "524288000\n")
        self.write("a/memory.stat", "anon 314572800\nfile 100000000\nshmem 5\nbadline\n")
        got = rr.read_cgroup_memory([(2, str(self.root / "a"))])
        self.assertEqual(got, {"used_bytes": 524288000, "anon_bytes": 314572800,
                               "file_bytes": 100000000, "source": "cgroup v2"})

    def test_v1_usage_minus_inactive_file(self):
        self.write("memory/docker/x/memory.usage_in_bytes", "600000000\n")
        self.write("memory/docker/x/memory.stat", "total_cache 200\ntotal_rss 400\ntotal_inactive_file 100000000\n")
        got = rr.read_cgroup_memory([(1, str(self.root / "memory/docker/x"))])
        self.assertEqual((got["used_bytes"], got["anon_bytes"], got["source"]), (500000000, 400, "cgroup v1"))

    def test_v1_blkio_directory_is_mapped_to_memory(self):
        self.write("memory/docker/y/memory.usage_in_bytes", "10\n")
        got = rr.read_cgroup_memory([(1, str(self.root / "blkio/docker/y"))])
        self.assertEqual(got["used_bytes"], 10)

    def test_unreadable_or_garbage_is_none(self):
        self.write("g/memory.current", "max\n")
        self.assertIsNone(rr.read_cgroup_memory([(2, str(self.root / "g")), (2, str(self.root / "none"))]))
        self.assertIsNone(rr.read_cgroup_memory([]))

    def test_hub_rss_and_total(self):
        self.write("status", "Name:\tx\nVmRSS:\t  87040 kB\n")
        self.write("meminfo", "MemTotal: 16000000 kB\n")
        self.assertEqual(rr.hub_rss_bytes(str(self.root / "status")), 87040 * 1024)
        self.assertIsNone(rr.hub_rss_bytes(str(self.root / "missing")))
        self.assertEqual(rr.mem_total_kib(str(self.root / "meminfo")), 16000000)


class ResidentBase(base.BrowserBase):
    def setUp(self):
        super().setUp()
        root = Path(self.temp.name)
        self.cg = root / "cg"
        self.status_file = root / "status"
        self.status_file.write_text("VmRSS:\t 90000 kB\n")
        self.wall = Clock()
        self.rb = rb.RemoteBrowser(self.rt, self.ctx, meminfo_path=str(self.meminfo),
                                   connect=self.connect, clock=self.clock, sleep=lambda s: None,
                                   monitor=False, frame_interval=0.0, start_timeout=3.0,
                                   spawn=lambda fn: fn(), wall=self.wall,
                                   cgroup_root=str(self.cg), proc_root=str(root / "proc"),
                                   hub_status_path=str(self.status_file))
        self.addCleanup(self.rb.shutdown)

    def set_cgroup(self, used, anon=None):
        scope = self.cg / "system.slice" / ("docker-%s.scope" % ("ab" * 32))
        scope.mkdir(parents=True, exist_ok=True)
        (scope / "memory.current").write_text("%d\n" % used)
        (scope / "memory.stat").write_text("anon %d\nfile 1000\n" % (anon if anon is not None else used // 2))

    def next_ws(self):
        self.ws = base.FakeWS()
        self.rb._connect = lambda ip: self.ws

    def crash(self, **kwargs):
        self.fake.crash("qnapdl-browser", **kwargs)
        self.clock.now += rr_health()
        self.rb.tick()


def rr_health():
    return rb.HEALTH_INTERVAL + 1


class ResidentStartTests(ResidentBase):
    def test_default_is_off_and_nothing_starts(self):
        self.assertFalse(self.rb.resident())
        self.rb.tick()
        self.assertEqual([c for c in self.fake.docker_calls() if c[0] == "run"], [])

    def test_boot_starts_container_without_tabs_and_trims(self):
        self.rb.set_resident(True)
        self.assertEqual(self.rb.status()["phase"], "running")
        self.assertEqual(len([c for c in self.fake.docker_calls() if c[0] == "run"]), 1)
        self.assertEqual(self.ws.calls("Target.createTarget"), [])      # only the about:blank anchor
        self.assertEqual(self.rb.status()["tabs"], [])
        self.assertEqual(self.ws.calls("Memory.simulatePressureNotification")[0]["params"],
                         {"level": "critical"})
        self.assertTrue(self.ws.calls("HeapProfiler.collectGarbage"))
        self.assertTrue(self.ws.calls("Target.detachFromTarget"))
        self.assertEqual(self.rb.status()["resident"]["state"], "resident")

    def test_memory_guard_blocks_resident_start_until_free(self):
        self.set_mem(1024 * 1024)
        self.rb.set_resident(True)
        status = self.rb.status()
        self.assertEqual(status["phase"], "stopped")
        self.assertEqual(status["resident"]["state"], "memory_wait")
        self.set_mem(8 * 1024 * 1024)
        self.clock.now += rr.MEMORY_RECHECK
        self.rb.tick()
        self.assertEqual(self.rb.status()["phase"], "running")
        self.assertEqual(self.rb.wd.restarts, 0)

    def test_image_missing_does_not_start(self):
        self.fake.images.clear()
        self.rb.set_resident(True)
        self.assertEqual(self.rb.status()["phase"], "stopped")
        self.assertEqual(self.rb.wd.state, rr.STATE_BLOCKED)
        self.assertEqual([c for c in self.fake.docker_calls() if c[0] == "run"], [])
        self.clock.now += 100
        self.rb.tick()
        self.assertEqual([c for c in self.fake.docker_calls() if c[0] == "run"], [])

    def test_resident_keeps_running_without_tabs_and_without_viewers(self):
        self.rb.set_resident(True)
        self.clock.now += 3600
        self.rb.tick()
        self.assertEqual(self.rb.status()["phase"], "running")

    def test_idle_view_timeout_closes_tabs_but_keeps_container_when_resident(self):
        self.rb.set_resident(True)
        self.open()
        self.clock.now += 601
        self.rb.tick()
        self.assertEqual(self.rb.status()["phase"], "running")
        self.assertEqual(self.rb.status()["tabs"], [])
        self.assertEqual(len(self.ws.calls("Target.closeTarget")), 1)

    def test_turning_off_restores_idle_stop(self):
        self.rb.set_resident(True)
        self.rb.set_resident(False)
        self.clock.now += 299
        self.rb.tick()
        self.assertEqual(self.rb.status()["phase"], "running")
        self.clock.now += 2
        self.rb.tick()
        self.assertEqual(self.rb.status()["phase"], "stopped")

    def test_closing_last_tab_trims_memory(self):
        self.rb.set_resident(True)
        self.open()
        before = len(self.ws.calls("Memory.simulatePressureNotification"))
        self.rb.close_tab("b1")
        self.assertEqual(len(self.ws.calls("Memory.simulatePressureNotification")), before + 1)
        self.assertGreaterEqual(len(self.ws.calls("HeapProfiler.collectGarbage")), 2)

    def test_trim_errors_are_ignored(self):
        self.ws.fail["Memory.simulatePressureNotification"] = "not supported"
        self.ws.fail["HeapProfiler.collectGarbage"] = "nope"
        self.rb.set_resident(True)
        self.assertEqual(self.rb.status()["phase"], "running")

    def test_non_resident_close_does_not_trim(self):
        self.open()
        self.rb.close_tab("b1")
        self.assertEqual(self.ws.calls("Memory.simulatePressureNotification"), [])

    def test_user_stop_is_respected_until_toggled(self):
        self.rb.set_resident(True)
        self.rb.stop()
        self.next_ws()
        for _ in range(3):
            self.clock.now += 100
            self.rb.tick()
        self.assertEqual(self.rb.status()["phase"], "stopped")
        self.assertEqual(len([c for c in self.fake.docker_calls() if c[0] == "run"]), 1)
        self.rb.set_resident(True)
        self.assertEqual(self.rb.status()["phase"], "running")


class IdleSampleTests(ResidentBase):
    def test_sample_recorded_60_seconds_after_trim_and_shown(self):
        self.set_cgroup(420 * MIB, 300 * MIB)
        self.rb.set_resident(True)
        self.clock.now += 59
        self.rb.tick()
        self.assertIsNone(self.rb.status()["resident"]["idle"])
        self.clock.now += 2
        self.rb.tick()
        res = self.rb.status()["resident"]
        self.assertEqual((res["idle"]["latest_mib"], res["idle"]["min_mib"]), (420, 420))
        self.assertEqual(res["idle"]["percent_of_total"], round(420 * MIB / (16000000 * 1024) * 100, 1))
        self.assertEqual(res["memory"]["source"], "cgroup v2")
        self.assertEqual((res["memory"]["used_mib"], res["memory"]["anon_mib"]), (420, 300))
        self.assertEqual(res["hub_rss_mib"], round(90000 * 1024 / MIB))
        self.assertEqual(res["total_mib"], 16000000 // 1024)
        # refreshed every five minutes, the minimum is kept
        self.set_cgroup(380 * MIB)
        self.clock.now += 100
        self.rb.tick()
        self.assertEqual(self.rb.status()["resident"]["idle"]["latest_mib"], 420)
        self.clock.now += 300
        self.rb.tick()
        idle = self.rb.status()["resident"]["idle"]
        self.assertEqual((idle["latest_mib"], idle["min_mib"], idle["samples"]), (380, 380, 2))

    def test_no_sample_while_a_tab_is_open_and_resample_after_close(self):
        self.set_cgroup(420 * MIB)
        self.rb.set_resident(True)
        self.open()
        self.clock.now += 120
        self.rb.tick()
        self.assertIsNone(self.rb.status()["resident"]["idle"])
        self.rb.close_tab("b1")
        self.clock.now += 30
        self.rb.tick()
        self.assertIsNone(self.rb.status()["resident"]["idle"])
        self.clock.now += 31
        self.rb.tick()
        self.assertEqual(self.rb.status()["resident"]["idle"]["latest_mib"], 420)

    def test_falls_back_to_docker_stats_and_reports_unavailable(self):
        self.rb.set_resident(True)
        self.fake.stats_out = "512MiB / 2GiB|25.00%"
        memory = self.rb.status(fresh_memory=True)["resident"]["memory"]
        self.assertEqual((memory["used_mib"], memory["source"], memory["anon_mib"]), (512, "docker stats", None))
        self.fake.stats_fail = True
        self.clock.now += 40
        self.assertIsNone(self.rb.status(fresh_memory=True)["resident"]["memory"])
        # the default status (the /api/state path) never starts docker stats
        self.fake.stats_fail = False
        calls = len([c for c in self.fake.docker_calls() if c[0] == "stats"])
        self.clock.now += 40
        self.assertIsNone(self.rb.status()["resident"]["memory"])
        self.assertEqual(len([c for c in self.fake.docker_calls() if c[0] == "stats"]), calls)

    def test_memory_reads_are_cached_five_seconds(self):
        self.set_cgroup(400 * MIB)
        self.rb.set_resident(True)
        first = self.rb.container_memory()
        self.set_cgroup(800 * MIB)
        self.assertEqual(self.rb.container_memory(), first)
        self.clock.now += 6
        self.assertEqual(self.rb.container_memory()["used_bytes"], 800 * MIB)

    def test_v1_layout_through_the_runtime(self):
        mem = self.cg / "memory" / "docker" / ("ab" * 32)
        mem.mkdir(parents=True)
        (mem / "memory.usage_in_bytes").write_text("%d\n" % (500 * MIB))
        (mem / "memory.stat").write_text("total_inactive_file %d\ntotal_rss %d\n" % (100 * MIB, 250 * MIB))
        self.rb.set_resident(True)
        got = self.rb.container_memory(fresh=True)
        self.assertEqual((got["used_bytes"], got["source"]), (400 * MIB, "cgroup v1"))


class WatchdogIntegrationTests(ResidentBase):
    def test_oom_exit_restarts_after_five_seconds_and_marks_tabs_lost(self):
        self.rb.set_resident(True)
        self.open()
        self.crash(code=137, oom=True)
        status = self.rb.status()
        self.assertEqual(status["phase"], "stopped")
        self.assertEqual(status["tabs"], [])
        self.assertEqual(status["resident"]["state"], "restarting")
        self.assertEqual(status["resident"]["watchdog"]["restart_n"], 1)
        self.assertEqual(status["resident"]["watchdog"]["lost_seq"], 1)
        self.assertNotIn("qnapdl-browser", self.fake.containers)
        self.next_ws()
        self.clock.now += 4
        self.rb.tick()
        self.assertEqual(self.rb.status()["phase"], "stopped")
        self.clock.now += 1.5
        self.rb.tick()
        status = self.rb.status()
        self.assertEqual(status["phase"], "running")
        self.assertEqual(status["resident"]["watchdog"]["restarts"], 1)
        self.assertEqual(status["resident"]["state"], "resident")
        texts = " ".join(e["text"] for e in status["resident"]["watchdog"]["events"])
        self.assertIn("メモリ不足で強制終了 / 終了コード 137", texts)

    def test_lost_connection_with_running_container_is_an_unexpected_exit(self):
        import time
        self.rb.set_resident(True)
        self.ws.close()
        for _ in range(100):
            if self.rb._conn.closed:
                break
            time.sleep(0.02)
        self.next_ws()
        self.rb.tick()
        wd = self.rb.status()["resident"]["watchdog"]
        self.assertEqual(wd["state"], "scheduled")
        self.assertIn("接続が切れました", wd["reason"])

    def test_gives_up_after_five_restarts_and_retry_restarts(self):
        self.rb.set_resident(True)
        delays = []
        for _ in range(5):
            self.next_ws()
            self.fake.crash("qnapdl-browser", code=137, oom=True)
            self.clock.now += rr_health()
            self.rb.tick()
            delays.append(round(self.rb.wd.next_at - self.clock.now))
            self.clock.now = self.rb.wd.next_at
            self.rb.tick()
            self.assertEqual(self.rb.status()["phase"], "running")
        self.assertEqual(delays, [5, 15, 60, 60, 60])
        self.fake.crash("qnapdl-browser", code=137, oom=True)
        self.clock.now += rr_health()
        self.rb.tick()
        status = self.rb.status()
        self.assertEqual(status["resident"]["state"], "gave_up")
        self.assertEqual(status["reason"], status["resident"]["watchdog"]["message"])
        self.assertIn("自動再起動を止めました（理由: メモリ不足で強制終了 / 終了コード 137）", status["reason"])
        self.assertFalse(status["can_open"])
        before = len([c for c in self.fake.docker_calls() if c[0] == "run"])
        self.clock.now += 1000
        self.rb.tick()
        self.assertEqual(len([c for c in self.fake.docker_calls() if c[0] == "run"]), before)
        with self.assertRaises(rb.BrowserError) as ctx:
            self.open()
        self.assertIn("自動再起動を止めました", str(ctx.exception))
        self.next_ws()
        self.assertTrue(self.rb.retry())
        self.assertEqual(self.rb.status()["phase"], "running")

    def test_exit_code_is_reported_without_oom(self):
        self.rb.set_resident(True)
        self.crash(code=9)
        self.assertIn("終了コード 9", self.rb.status()["resident"]["watchdog"]["reason"])

    def test_memory_guard_before_each_restart(self):
        self.rb.set_resident(True)
        self.crash(code=137, oom=True)
        self.set_mem(512 * 1024)
        self.next_ws()
        self.clock.now += 5
        self.rb.tick()
        status = self.rb.status()
        self.assertEqual(status["phase"], "stopped")
        self.assertEqual(status["resident"]["state"], "memory_wait")
        self.assertEqual(self.rb.wd.message(), "メモリ不足のため待機中")
        self.assertEqual(len(self.rb.wd.attempts), 0)
        for _ in range(3):
            self.clock.now += 5
            self.rb.tick()
        self.assertEqual(self.rb.status()["phase"], "stopped")
        self.set_mem(8 * 1024 * 1024)
        self.clock.now += 5
        self.rb.tick()
        self.assertEqual(self.rb.status()["phase"], "running")
        self.assertEqual(self.rb.wd.restarts, 1)

    def test_tabs_open_without_resident_also_restart(self):
        self.open()
        self.crash(code=137)
        self.next_ws()
        self.clock.now += 5
        self.rb.tick()
        self.assertEqual(self.rb.status()["phase"], "running")
        self.assertEqual(self.rb.wd.restarts, 1)

    def test_no_tabs_and_not_resident_does_not_restart(self):
        self.open()
        self.rb.close_tab("b1")
        self.crash(code=137)
        self.clock.now += 100
        self.rb.tick()
        self.assertEqual(self.rb.status()["phase"], "stopped")
        self.assertEqual(self.rb.wd.state, rr.STATE_IDLE)

    def test_no_restart_after_intentional_stop(self):
        self.rb.set_resident(True)
        self.open()
        self.rb.stop()
        self.clock.now += 1000
        self.rb.tick()
        self.assertEqual(self.rb.status()["phase"], "stopped")
        self.assertEqual(self.rb.wd.state, rr.STATE_IDLE)
        self.assertEqual(self.rb.wd.restarts, 0)

    def test_stop_cancels_a_pending_restart(self):
        self.rb.set_resident(True)
        self.crash(code=137)
        self.assertEqual(self.rb.wd.state, rr.STATE_SCHEDULED)
        self.rb.stop()
        self.clock.now += 100
        self.rb.tick()
        self.assertEqual(self.rb.status()["phase"], "stopped")

    def test_no_restart_during_setup_or_when_image_vanished(self):
        self.rb.set_resident(True)
        self.crash(code=137)
        with self.rb._lock:
            self.rb._setup_active = True
        self.clock.now += 100
        self.rb.tick()
        self.assertEqual(self.rb.status()["phase"], "setup")
        with self.rb._lock:
            self.rb._setup_active = False
        self.fake.images.clear()
        self.rb._image_cache = None
        self.rb.tick()
        self.assertEqual(self.rb.wd.state, rr.STATE_BLOCKED)
        self.assertEqual(len([c for c in self.fake.docker_calls() if c[0] == "run"]), 1)

    def test_user_open_during_wait_does_the_restart_and_counts_once(self):
        self.rb.set_resident(True)
        self.crash(code=137)
        self.next_ws()
        self.open()
        self.assertEqual(self.rb.status()["phase"], "running")
        self.assertEqual(self.rb.wd.restarts, 1)
        self.assertEqual(self.rb.wd.state, rr.STATE_IDLE)

    def test_events_are_kept_to_twenty(self):
        self.rb.set_resident(True)
        for _ in range(30):
            self.rb.wd.event("x", "y")
        self.assertEqual(len(self.rb.status()["resident"]["watchdog"]["events"]), 20)

    def test_docker_restart_policy_is_never_used(self):
        args = self.rt.browser_run_args(self.tag)
        self.assertEqual(args[args.index("--restart") + 1], "no")     # the Hub restarts, not docker


class ClientStaticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = (ROOT / "web/app.js").read_text()
        cls.html = (ROOT / "web/index.html").read_text()

    def test_switch_quick_button_and_retry_exist(self):
        for ident in ("rbResident", "rbRetry", "rbResidentLine", "rbWatchdog", "rbWdLog"):
            self.assertIn('id="%s"' % ident, self.html)
        self.assertIn("NASブラウザを常駐", self.html)
        row = self.html[self.html.index("NASブラウザを常駐"):][:900]
        self.assertIn("ⓘ", row)

    def test_client_wiring(self):
        for needle in ('saveOption("rb_resident"', '"/api/rb/retry"', "resetTabsAfterCrash",
                       "NASブラウザを再起動しました", "再起動中（", "自動再起動を停止", "常駐中",
                       "待機時 約 ", "取得できません", "autoReopen"):
            self.assertIn(needle, self.js)
        self.assertEqual(self.js.count("NASブラウザを再起動しました"), 1)
        self.assertIn('sched.add("rb-mem"', self.js)
        self.assertFalse(re.search(r"setInterval\(", self.js))

    def test_new_module_is_packaged_and_checked(self):
        self.assertIn("rb_resident.py", (ROOT / "scripts/build-qpkg.sh").read_text())
        self.assertIn("rb_resident.py", (ROOT / "scripts/verify.sh").read_text())


if __name__ == "__main__":
    unittest.main()
