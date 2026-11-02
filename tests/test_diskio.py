"""v0.1.35 disk write measurement: parsers (fixtures), sources, fallbacks, rates, caching."""
import json
import os
import tempfile
import unittest
from pathlib import Path

import container_runtime as cr
import diskio

PROC_IO = """rchar: 100
wchar: 200
syscr: 3
syscw: 4
read_bytes: 4096
write_bytes: 8192
cancelled_write_bytes: 0
"""

DISKSTATS = """   8       0 sda 100 0 2000 50 300 0 4000 80 0 90 130
   8       1 sda1 90 0 1800 40 290 0 3900 70 0 80 110
   8      16 sdb 10 0 200 5 30 0 400 8 0 9 13
 259       0 nvme0n1 10 0 160 5 20 0 800 8 0 9 13
 259       1 nvme0n1p1 10 0 160 5 20 0 800 8 0 9 13
   9       1 md1 5 0 80 1 60 0 1200 8 0 9 13
   9       9 md9 5 0 80 1 10 0 100 8 0 9 13
   7       0 loop0 1 0 8 1 1 0 8 1 0 1 2
   1       0 ram0 0 0 0 0 0 0 0 0 0 0 0
 253       0 dm-0 5 0 80 1 70 0 1400 8 0 9 13
 253       1 dm-1 5 0 80 1 70 0 1400 8 0 9 13
 179       0 mmcblk0 1 0 8 1 5 0 40 1 0 1 2
 179       1 mmcblk0p1 1 0 8 1 5 0 40 1 0 1 2
broken line
"""

CGROUP_V2 = """8:0 rbytes=1000 wbytes=5000 rios=3 wios=4 dbytes=0 dios=0
8:16 rbytes=24 wbytes=100 rios=1 wios=1 dbytes=0 dios=0
"""

CGROUP_V1 = """8:0 Read 1000
8:0 Write 5000
8:0 Sync 3000
8:0 Async 3000
8:0 Total 6000
8:16 Read 24
8:16 Write 100
8:16 Total 124
Total 6124
"""

PROC_CGROUP_V2 = "0::/system.slice/docker-%s.scope\n"
PROC_CGROUP_V1 = "11:blkio:/docker/%s\n10:memory:/docker/%s\n1:name=systemd:/docker/%s\n"
CID = "ab12" * 16


class ParserTests(unittest.TestCase):
    def test_proc_io(self):
        self.assertEqual(diskio.parse_proc_io(PROC_IO), {"read_bytes": 4096, "write_bytes": 8192})
        self.assertIsNone(diskio.parse_proc_io("read_bytes: x\nwrite_bytes: 1\n"))
        self.assertIsNone(diskio.parse_proc_io("write_bytes: 1\n"))
        self.assertIsNone(diskio.parse_proc_io("read_bytes: -1\nwrite_bytes: 1\n"))

    def test_hub_io_reads_the_process_file_else_sums_the_threads(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "io").write_text(PROC_IO)
            self.assertEqual(diskio.read_hub_io(str(root / "io"), str(root / "task")), (4096, 8192))
            for tid, w in (("1", 100), ("2", 50)):
                (root / "task" / tid).mkdir(parents=True)
                (root / "task" / tid / "io").write_text("read_bytes: 1\nwrite_bytes: %d\n" % w)
            self.assertEqual(diskio.read_hub_io(str(root / "missing"), str(root / "task")), (2, 150))
            with self.assertRaises(OSError):
                diskio.read_hub_io(str(root / "missing"), str(root / "none"))

    def test_diskstats_whole_disks_and_arrays_are_separate_and_partitions_skipped(self):
        found = diskio.classify_disks(diskio.parse_diskstats(DISKSTATS))
        ids = [item[0] for item in found]
        self.assertEqual(ids, ["disk:sda", "disk:sdb", "disk:nvme0n1", "disk:mmcblk0", "array:md1", "array:md9"])
        sda = next(item for item in found if item[0] == "disk:sda")
        self.assertEqual((sda[2], sda[3], sda[4]), ("disk", 2000 * 512, 4000 * 512))
        md1 = next(item for item in found if item[0] == "array:md1")
        self.assertEqual((md1[2], md1[4]), ("array", 1200 * 512))
        for skipped in ("sda1", "nvme0n1p1", "loop0", "ram0", "dm-0", "mmcblk0p1"):
            self.assertFalse([i for i in ids if i.endswith(skipped)])

    def test_dm_devices_are_used_only_without_any_md_array(self):
        rows = [r for r in diskio.parse_diskstats(DISKSTATS) if not r[0].startswith("md")]
        ids = [item[0] for item in diskio.classify_disks(rows)]
        self.assertIn("array:dm-0", ids)
        self.assertIn("array:dm-1", ids)

    def test_cgroup_files(self):
        self.assertEqual(diskio.parse_cgroup_v2_io(CGROUP_V2), (1024, 5100))
        self.assertEqual(diskio.parse_cgroup_v2_io(""), (0, 0))
        self.assertEqual(diskio.parse_cgroup_v1_io(CGROUP_V1), (1024, 5100))
        self.assertEqual(diskio.parse_cgroup_v1_io("Total 0\n"), (0, 0))

    def test_docker_stats_block_io(self):
        self.assertEqual(cr.parse_block_io("1.5MB / 3.2MB"), (1500000, 3200000))
        self.assertEqual(cr.parse_block_io("0B / 12.3kB"), (0, 12300))
        self.assertEqual(cr.parse_block_io("4KiB / 1GiB"), (4096, 1024 ** 3))
        for bad in ("", "12", "a / b", "1MB"):
            with self.assertRaises(cr.ContainerError):
                cr.parse_block_io(bad)


class FakeRuntime:
    def __init__(self, ref=None, block=None):
        self.ref = ref
        self.block = block
        self.stats_calls = 0

    def container_ref(self, name):
        return self.ref

    def block_io(self, name):
        self.stats_calls += 1
        if self.block is None:
            raise cr.ContainerError("x")
        return self.block


class CgroupLookupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "cg"
        self.proc = Path(self.tmp.name) / "proc"
        self.root.mkdir()
        self.proc.mkdir()

    def ref(self, pid=4242):
        return {"id": CID, "pid": pid, "running": True}

    def test_v2_directory_comes_from_proc_pid_cgroup(self):
        (self.proc / "4242").mkdir()
        (self.proc / "4242" / "cgroup").write_text(PROC_CGROUP_V2 % CID)
        d = self.root / "system.slice" / ("docker-%s.scope" % CID)
        d.mkdir(parents=True)
        (d / "io.stat").write_text(CGROUP_V2)
        paths = diskio.container_cgroup_paths(FakeRuntime(self.ref()), "qnapdl-jd2", str(self.root), str(self.proc))
        self.assertEqual(paths[0], (2, str(d)))
        self.assertEqual(diskio.read_container_cgroup_io(paths), (1024, 5100, "cgroup v2"))

    def test_v1_blkio_directory_and_static_fallback_without_pid(self):
        (self.proc / "4242").mkdir()
        (self.proc / "4242" / "cgroup").write_text(PROC_CGROUP_V1 % (CID, CID, CID))
        d = self.root / "blkio" / "docker" / CID
        d.mkdir(parents=True)
        (d / "blkio.throttle.io_service_bytes").write_text(CGROUP_V1)
        for pid in (4242, 0):
            paths = diskio.container_cgroup_paths(FakeRuntime(self.ref(pid)), "qnapdl-jd2", str(self.root), str(self.proc))
            self.assertEqual(paths, [(1, str(d))])
            self.assertEqual(diskio.read_container_cgroup_io(paths), (1024, 5100, "cgroup v1"))

    def test_not_running_or_unknown_container_has_no_paths(self):
        self.assertEqual(diskio.container_cgroup_paths(FakeRuntime(None), "n", str(self.root)), [])
        ref = dict(self.ref(), running=False)
        self.assertEqual(diskio.container_cgroup_paths(FakeRuntime(ref), "n", str(self.root)), [])
        self.assertIsNone(diskio.read_container_cgroup_io([]))

    def test_path_traversal_in_proc_cgroup_is_ignored(self):
        (self.proc / "4242").mkdir()
        (self.proc / "4242" / "cgroup").write_text("0::/../../etc\n")
        self.assertEqual(diskio.container_cgroup_candidates({"id": "", "pid": 4242}, str(self.root), str(self.proc)), [])


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class MeterTests(unittest.TestCase):
    def meter(self, runtime=None, hub=None, disks=None, **kw):
        self.clock = Clock()
        self.hub_values = hub if hub is not None else [(0, 0)]
        self.disk_values = disks if disks is not None else []
        meter = diskio.DiskIO(
            runtime, kw.pop("containers", ()), clock=self.clock, wall=lambda: 5000.0,
            proc_io=lambda: self.hub_values[0], disks=lambda: self.disk_values, start_thread=False,
            cgroup_root="/nonexistent", proc_dir="/nonexistent", **kw)
        return meter

    def step(self, meter, seconds, write):
        self.clock.t += seconds
        self.hub_values[0] = (0, write)
        meter.sample()

    def test_rates_over_one_and_ten_minutes_and_totals_since_start(self):
        meter = self.meter()
        meter.sample()                        # t=0, counter 0
        meter.start()
        for i in range(1, 61):                # 10 minutes at 100 KiB per 10 s
            self.step(meter, 10.0, i * 1024 * 1000)
        snap = meter.snapshot(touch=False)
        hub = snap["sources"][0]
        self.assertEqual(hub["id"], "hub")
        self.assertTrue(snap["measuring"])
        self.assertAlmostEqual(hub["write"]["rate_1m"], 102400.0)
        self.assertAlmostEqual(hub["write"]["rate_10m"], 102400.0)
        self.assertEqual(hub["write"]["total"], 60 * 1024 * 1000)
        # a burst only shows in the one-minute window by its share
        self.step(meter, 10.0, 60 * 1024 * 1000 + 6_000_000)
        hub = meter.snapshot(touch=False)["sources"][0]
        self.assertAlmostEqual(hub["write"]["rate_1m"], (5 * 1024000 + 6_000_000) / 60.0, delta=20000)
        self.assertGreater(hub["write"]["rate_1m"], hub["write"]["rate_10m"])

    def test_windows_need_enough_data(self):
        meter = self.meter()
        meter.sample()
        self.step(meter, 10.0, 1000)
        hub = meter.snapshot(touch=False)["sources"][0]
        self.assertIsNone(hub["write"]["rate_1m"])      # 10 s of data < half a minute
        self.assertIsNone(hub["write"]["rate_10m"])
        self.assertIsNone(hub["write"]["total"])        # not measuring yet

    def test_counter_reset_is_bridged_and_stop_freezes_the_total(self):
        meter = self.meter()
        meter.start()
        self.step(meter, 10.0, 1000)
        self.step(meter, 10.0, 3000)
        self.step(meter, 10.0, 500)                     # the container restarted: counter back to 500
        total = meter.snapshot(touch=False)["sources"][0]["write"]["total"]
        self.assertEqual(total, 3500)
        stopped = meter.stop()
        self.assertFalse(stopped["measuring"])
        self.assertEqual(stopped["sources"][0]["write"]["total"], 3500)
        self.step(meter, 10.0, 900)
        self.assertEqual(meter.snapshot(touch=False)["sources"][0]["write"]["total"], 3500)
        meter.start()                                   # baselines reset
        self.assertEqual(meter.snapshot(touch=False)["sources"][0]["write"]["total"], 0)

    def test_sampling_is_cached_for_the_interval(self):
        calls = []
        meter = self.meter()
        meter._proc_io = lambda: calls.append(1) or (0, 0)
        meter.sample()
        meter.sample()
        self.clock.t += 9.9
        meter.sample()
        self.assertEqual(len(calls), 1)
        self.clock.t += 0.2
        meter.sample()
        self.assertEqual(len(calls), 2)

    def test_container_prefers_cgroup_then_docker_stats_then_says_unavailable(self):
        runtime = FakeRuntime({"id": CID, "pid": 0, "running": True}, block=(7, 4096))
        meter = self.meter(runtime, containers=(("jd2", "JD2コンテナ", "qnapdl-jd2"),))
        meter.sample()
        item = next(s for s in meter.snapshot(touch=False)["sources"] if s["id"] == "jd2")
        self.assertTrue(item["available"])
        self.assertEqual(item["how"], "docker stats")
        self.assertEqual(runtime.stats_calls, 1)
        # container gone: reported as unavailable instead of failing the whole sample
        runtime.ref = None
        self.clock.t += 11
        meter.sample()
        item = next(s for s in meter.snapshot(touch=False)["sources"] if s["id"] == "jd2")
        self.assertFalse(item["available"])
        self.assertTrue(item["reason"])
        self.assertIsNone(item["write"]["rate_1m"])
        self.assertTrue(next(s for s in meter.snapshot(touch=False)["sources"] if s["id"] == "hub")["available"])

    def test_missing_proc_files_make_those_sources_unavailable(self):
        def broken():
            raise OSError("nope")
        meter = self.meter()
        meter._proc_io = broken
        meter._disks = broken
        meter.sample()
        sources = {s["id"]: s for s in meter.snapshot(touch=False)["sources"]}
        self.assertFalse(sources["hub"]["available"])
        self.assertFalse(sources["disks"]["available"])

    def test_disks_and_arrays_are_listed_separately(self):
        meter = self.meter(disks=diskio.classify_disks(diskio.parse_diskstats(DISKSTATS)))
        meter.sample()
        kinds = {s["id"]: s["kind"] for s in meter.snapshot(touch=False)["sources"]}
        self.assertEqual(kinds["disk:sda"], "disk")
        self.assertEqual(kinds["array:md1"], "array")
        self.assertNotIn("disk:sda1", kinds)

    def test_sampler_thread_stops_when_nobody_asks(self):
        meter = diskio.DiskIO(None, (), proc_io=lambda: (0, 0), disks=lambda: [])
        self.addCleanup(meter._wake.set)
        meter.touch()
        self.assertTrue(meter._thread.is_alive())
        thread = meter._thread
        meter._asked = meter._clock() - diskio.IDLE_STOP - 1      # nobody asked for 5 minutes
        for _ in range(40):
            meter._wake.set()
            thread.join(0.05)
            if not thread.is_alive():
                break
        self.assertFalse(thread.is_alive())
        self.assertIsNone(meter._thread)


class RuntimeHelpersTests(unittest.TestCase):
    def runtime(self, handler):
        from types import SimpleNamespace

        def runner(argv, timeout, env):
            out = handler(argv)
            return SimpleNamespace(returncode=out[0], stdout=out[1], stderr="")
        return cr.Runtime(runner, isfile=lambda p: True, access=lambda p, m: True)

    def test_container_ref_and_block_io_use_fixed_docker_calls(self):
        calls = []

        def handler(argv):
            calls.append(argv[1:])
            if argv[1:3] == ["container", "inspect"]:
                return 0, json.dumps([{"Name": "/qnapdl-jd2", "Id": CID, "Config": {"Labels": {cr.LABEL_KEY: cr.LABEL_VALUE}},
                                       "State": {"Status": "running", "Running": True, "Pid": 77},
                                       "NetworkSettings": {}, "HostConfig": {}}])
            if argv[1] == "stats":
                return 0, "1.5MB / 3.2MB\n"
            return 1, ""
        rt = self.runtime(handler)
        self.assertEqual(rt.container_ref("qnapdl-jd2"), {"id": CID, "pid": 77, "running": True})
        self.assertEqual(rt.block_io("qnapdl-jd2"), (1500000, 3200000))
        self.assertIn(["stats", "--no-stream", "--format", "{{.BlockIO}}", "qnapdl-jd2"], calls)
        with self.assertRaises(cr.ContainerError):
            rt.block_io("someone-elses-container")
        with self.assertRaises(cr.ContainerError):
            rt.container_ref("someone-elses-container")

    def test_unmanaged_or_absent_container_has_no_ref(self):
        rt = self.runtime(lambda argv: (1, "Error: No such container"))
        self.assertIsNone(rt.container_ref("qnapdl-browser"))


if __name__ == "__main__":
    unittest.main()
