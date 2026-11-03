"""Disk write measurement (v0.1.35): where do the NAS disk writes come from?

Sources, all read-only and RAM-only:
  * the Hub process: /proc/self/io (the thread-group file, so every Hub thread is included);
  * our containers (JD2, NAS browser): cgroup v2 io.stat or cgroup v1 blkio.throttle.io_service_bytes,
    else `docker stats --no-stream` BlockIO (through container_runtime);
  * the NAS disks: /proc/diskstats sectors * 512 for whole physical disks and md arrays, reported
    separately (dm-* only when there is no md array; partitions, loop and ram are skipped).

A small ring of samples (one per SAMPLE_INTERVAL, taken by a lazy thread that only runs while
somebody asked in the last IDLE_STOP seconds) gives the rates over the last 1 and 10 minutes.
The counters are cumulative, so totals since "start" stay correct even while sampling paused.
Nothing here is ever written to disk.
"""
import os
import re
import threading
import time
from collections import deque

import debuglog
import ttlcache

SAMPLE_INTERVAL = 10.0       # seconds; the TTL cache guarantees at most one collection per interval
IDLE_STOP = 300.0            # stop sampling when nobody asked for this long
WINDOWS = (("rate_1m", 60.0), ("rate_10m", 600.0))
RING = 100                   # samples kept per source (> 10 minutes at 10 s)
MIN_SPAN_FRACTION = 0.5      # a window needs at least this part of its length as data
SECTOR = 512
DEFAULT_CGROUP_ROOT = "/sys/fs/cgroup"

_DISK = re.compile(r"(sd[a-z]+|hd[a-z]+|vd[a-z]+|nvme\d+n\d+|mmcblk\d+)\Z")
_MD = re.compile(r"md\d+\Z")
_DM = re.compile(r"dm-\d+\Z")


# ---------------------------------------------------------------- parsers (pure)
def parse_proc_io(text):
    """{'read_bytes': n, 'write_bytes': n} from the text of /proc/<pid>/io, or None."""
    found = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep and key in ("read_bytes", "write_bytes"):
            try:
                number = int(value.strip())
            except ValueError:
                return None
            if number < 0:
                return None
            found[key] = number
    return found if len(found) == 2 else None


def read_hub_io(path="/proc/self/io", task_glob="/proc/self/task"):
    """Hub process counters. /proc/self/io already sums the threads; the per-thread files are the fallback."""
    try:
        with open(path, "r", encoding="ascii") as handle:
            data = parse_proc_io(handle.read())
        if data:
            return data["read_bytes"], data["write_bytes"]
    except (OSError, UnicodeError):
        pass
    total = [0, 0]
    seen = False
    try:
        for tid in os.listdir(task_glob):
            try:
                with open(os.path.join(task_glob, tid, "io"), "r", encoding="ascii") as handle:
                    data = parse_proc_io(handle.read())
            except (OSError, UnicodeError):
                continue
            if data:
                total[0] += data["read_bytes"]
                total[1] += data["write_bytes"]
                seen = True
    except OSError:
        pass
    if not seen:
        raise OSError("proc io unavailable")
    return total[0], total[1]


def parse_diskstats(text):
    """[(name, read_bytes, write_bytes)] of every device line (sectors * 512)."""
    rows = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 10:
            continue
        try:
            rows.append((parts[2], int(parts[5]) * SECTOR, int(parts[9]) * SECTOR))
        except ValueError:
            continue
    return rows


def classify_disks(rows):
    """[(source id, label, kind, read, write)]: whole disks and md arrays; dm-* only without any md."""
    disks = [r for r in rows if _DISK.match(r[0])]
    arrays = [r for r in rows if _MD.match(r[0])]
    if not arrays:
        arrays = [r for r in rows if _DM.match(r[0])]
    result = [("disk:" + n, n, "disk", r, w) for n, r, w in disks]
    result += [("array:" + n, n, "array", r, w) for n, r, w in arrays]
    return result


def read_disks(path="/proc/diskstats"):
    with open(path, "r", encoding="ascii", errors="replace") as handle:
        return classify_disks(parse_diskstats(handle.read()))


def parse_cgroup_v2_io(text):
    """(read, write) bytes summed over the devices of an io.stat file."""
    read = write = 0
    for line in text.splitlines():
        for field in line.split()[1:]:
            key, _, value = field.partition("=")
            if key in ("rbytes", "wbytes") and value.isdigit():
                if key == "rbytes":
                    read += int(value)
                else:
                    write += int(value)
    return read, write        # an idle cgroup has an empty io.stat: 0, 0


def parse_cgroup_v1_io(text):
    """(read, write) bytes of blkio.throttle.io_service_bytes ('8:0 Read 123' lines)."""
    read = write = 0
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[2].isdigit():
            if parts[1] == "Read":
                read += int(parts[2])
            elif parts[1] == "Write":
                write += int(parts[2])
    return read, write


def parse_proc_cgroup(text):
    """[(controllers, path)] from /proc/<pid>/cgroup ('0::/path' is cgroup v2)."""
    rows = []
    for line in text.splitlines():
        parts = line.split(":", 2)
        if len(parts) == 3:
            rows.append((parts[1], parts[2].strip()))
    return rows


# ------------------------------------------------- container cgroup lookup (reusable)
def _read(path):
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return handle.read()


_V1_CONTROLLERS = ("blkio", "memory")
SCAN_DEPTH = 3               # directories below a hierarchy root searched for the container's id
SCAN_LIMIT = 400             # directories looked at in total (the scan is only a last resort)


def _unescape_mount(text):
    """Mountinfo escapes space, tab, newline and backslash as octal (\\040)."""
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), text)


def parse_mountinfo_cgroups(text):
    """[(version, mountpoint, controllers, root)] of every cgroup / cgroup2 mount in /proc/self/mountinfo.

    `controllers` is a frozenset of the v1 controller names ({'memory'}, {'cpu', 'cpuacct'}, ...), empty for v2.
    `root` is the mount's root inside the hierarchy ('/' for a mount of the whole tree).
    """
    mounts = []
    for line in text.splitlines():
        left, sep, right = line.partition(" - ")
        if not sep:
            continue
        fields = left.split()
        after = right.split()
        if len(fields) < 5 or not after:
            continue
        root, point = _unescape_mount(fields[3]), _unescape_mount(fields[4])
        if after[0] == "cgroup2":
            mounts.append((2, point, frozenset(), root))
        elif after[0] == "cgroup":
            options = after[2].split(",") if len(after) > 2 else []
            names = frozenset(o for o in options if o and not o.startswith(("release_agent=", "xattr", "noprefix", "clone_children"))
                              and o not in ("rw", "ro"))
            mounts.append((1, point, names, root))
    return mounts


def read_cgroup_mounts(root=DEFAULT_CGROUP_ROOT, proc="/proc"):
    """Cgroup mounts from `<proc>/self/mountinfo`; without that file the conventional layout under `root`."""
    try:
        mounts = parse_mountinfo_cgroups(_read("%s/self/mountinfo" % proc))
    except OSError:
        mounts = []
    if mounts:
        return mounts, "mountinfo"
    guess = [(2, root, frozenset(), "/")]
    guess += [(1, "%s/%s" % (root, name), frozenset([name]), "/") for name in _V1_CONTROLLERS]
    return guess, "default"


def _join_hierarchy(point, hroot, path):
    """Directory for a cgroup `path` (as /proc/<pid>/cgroup shows it) inside a mount, or None."""
    if ".." in path.split("/"):
        return None
    if hroot not in ("", "/"):
        if path == hroot:
            path = "/"
        elif path.startswith(hroot.rstrip("/") + "/"):
            path = path[len(hroot.rstrip("/")):]
        else:
            return None            # the container's cgroup is outside what this mount shows
    return os.path.join(point, path.lstrip("/")) if path.strip("/") else point


def _scan_for_id(top, ident):
    """Directories named `ident` / `docker-<ident>.scope` / `cri-containerd-<ident>.scope` below `top`."""
    wanted = {ident, "docker-%s.scope" % ident}
    found, seen, queue = [], 0, [(top, 0)]
    while queue and seen < SCAN_LIMIT:
        directory, depth = queue.pop(0)
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        if not entry.is_dir(follow_symlinks=False):
                            continue
                    except OSError:
                        continue
                    seen += 1
                    if entry.name in wanted:
                        found.append(entry.path)
                    elif depth + 1 < SCAN_DEPTH and seen < SCAN_LIMIT:
                        queue.append((entry.path, depth + 1))
        except OSError:
            continue
    return found


def container_cgroup_sources(ref, root=DEFAULT_CGROUP_ROOT, proc="/proc"):
    """Candidate cgroup directories [(version, dir, how)] for a container ref {id, pid}.

    How: "proc" = the container's own /proc/<pid>/cgroup joined with each mounted hierarchy from
    /proc/self/mountinfo (exact; works for any prefix such as docker/, system.slice/ or
    container-station/); "guess" = the usual Docker locations; "scan" = a bounded search for the
    container id below each hierarchy. Nothing is validated for a particular controller here.
    """
    found = []
    mounts, mount_source = read_cgroup_mounts(root, proc)
    pid = ref.get("pid") or 0
    if isinstance(pid, int) and pid > 0:
        try:
            entries = parse_proc_cgroup(_read("%s/%d/cgroup" % (proc, pid)))
        except OSError:
            entries = []
        for controllers, path in entries:
            names = set(c for c in controllers.split(",") if c)
            for version, point, mount_controllers, hroot in mounts:
                if version == 2 and not names:
                    pass
                elif version == 1 and names and (names & mount_controllers):
                    pass
                else:
                    continue
                if version == 1 and not (mount_controllers & set(_V1_CONTROLLERS)):
                    continue           # cpu, devices, freezer ...: no I/O or memory counters
                directory = _join_hierarchy(point, hroot, path)
                if directory:
                    found.append((version, directory, "proc"))
    ident = ref.get("id") or ""
    if re.fullmatch(r"[0-9a-f]{12,64}", ident):
        found += [(2, "%s/system.slice/docker-%s.scope" % (root, ident), "guess"),
                  (2, "%s/docker/%s" % (root, ident), "guess")]
        for name in _V1_CONTROLLERS:
            found += [(1, "%s/%s/docker/%s" % (root, name, ident), "guess"),
                      (1, "%s/%s/system.slice/docker-%s.scope" % (root, name, ident), "guess"),
                      (1, "%s/%s/container-station/docker/%s" % (root, name, ident), "guess"),
                      (1, "%s/%s/container-station/%s" % (root, name, ident), "guess")]
        for version, point, mount_controllers, hroot in mounts:
            if version == 1 and not (mount_controllers & set(_V1_CONTROLLERS)):
                continue
            if hroot not in ("", "/") or (version == 2 and mount_source != "mountinfo"):
                continue           # (a guessed v2 root would also list the v1 hierarchies below it)
            for directory in _scan_for_id(point, ident):
                found.append((version, directory, "scan"))
    unique, seen = [], set()
    for version, directory, how in found:
        if directory not in seen:
            seen.add(directory)
            unique.append((version, directory, how))
    return unique


def container_cgroup_candidates(ref, root=DEFAULT_CGROUP_ROOT, proc="/proc"):
    """Candidate cgroup directories [(version, dir)] for a container ref {id, pid} (see container_cgroup_sources)."""
    return [(v, d) for v, d, _ in container_cgroup_sources(ref, root, proc)]


def container_cgroup_paths(runtime, name, root=DEFAULT_CGROUP_ROOT, proc="/proc"):
    """Cgroup directories of one of our containers: [(version, dir)] that exist (best first).

    `runtime.container_ref(name)` supplies id/pid. Returns [] when the container is not running or
    nothing is readable. Directories are not validated for a particular controller.
    """
    return [(v, d) for v, d, _ in container_cgroup_found(runtime, name, root, proc)]


def container_cgroup_found(runtime, name, root=DEFAULT_CGROUP_ROOT, proc="/proc"):
    """Like container_cgroup_paths but [(version, dir, how)]; how is "proc", "guess" or "scan"."""
    try:
        ref = runtime.container_ref(name)
    except Exception:
        return []
    if not ref or not ref.get("running"):
        return []
    return [item for item in container_cgroup_sources(ref, root, proc) if os.path.isdir(item[1])]


def cgroup_layout_summary(root=DEFAULT_CGROUP_ROOT, proc="/proc"):
    """Short text of the mounted cgroup hierarchies (for the self-test report): 'v1: blkio,memory' / 'v2'."""
    mounts, source = read_cgroup_mounts(root, proc)
    v1 = sorted(set(c for version, _, controllers, _ in mounts if version == 1 for c in controllers
                    if c in _V1_CONTROLLERS))
    parts = []
    if any(version == 2 for version, _, _, _ in mounts):
        parts.append("v2")
    if any(version == 1 for version, _, _, _ in mounts):
        parts.append("v1(%s)" % (",".join(v1) or "なし"))
    return ("+".join(parts) or "不明") + ("" if source == "mountinfo" else "（推測）")


def cgroup_report(runtime, name, root=DEFAULT_CGROUP_ROOT, proc="/proc", read_memory=None):
    """What can be read for one container (self-test row env.cgroup): {running, io, mem, dir, how, pid_cgroup, mounts}.

    `io` / `mem` name the working source ("cgroup v1", "cgroup v2") or None (then docker stats is the fallback);
    `pid_cgroup` (first lines of /proc/<pid>/cgroup) and `mounts` are the facts needed to learn an unknown layout.
    """
    out = {"running": False, "io": None, "mem": None, "dir": None, "how": None, "pid_cgroup": [], "mounts": []}
    try:
        ref = runtime.container_ref(name)
    except Exception:
        ref = None
    if not ref or not ref.get("running"):
        return out
    out["running"] = True
    found = [item for item in container_cgroup_sources(ref, root, proc) if os.path.isdir(item[1])]
    paths = [(v, d) for v, d, _ in found]
    got = read_container_cgroup_io(paths) if paths else None
    out["io"] = None if got is None else got[2]
    if read_memory is not None and paths:
        mem = read_memory(paths)
        out["mem"] = None if mem is None else mem.get("source")
    if found:
        out["dir"], out["how"] = found[0][1], found[0][2]
    pid = ref.get("pid") or 0
    if isinstance(pid, int) and pid > 0:
        try:
            out["pid_cgroup"] = [("%s:%s" % (c, p))[:90] for c, p in parse_proc_cgroup(_read("%s/%d/cgroup" % (proc, pid)))[:4]]
        except OSError:
            pass
    mounts, _ = read_cgroup_mounts(root, proc)
    out["mounts"] = ["v%d@%s(%s)" % (v, point, ",".join(sorted(c)) or "-") for v, point, c, _ in mounts[:6]]
    return out


def read_container_cgroup_io(paths):
    """(read, write, how) from the first cgroup directory with readable block I/O counters, or None."""
    for version, directory in paths:
        names = (["io.stat"] if version == 2 else
                 ["blkio.throttle.io_service_bytes_recursive", "blkio.throttle.io_service_bytes"])
        for filename in names:
            try:
                text = _read(os.path.join(directory, filename))
            except OSError:
                continue
            read, write = (parse_cgroup_v2_io if version == 2 else parse_cgroup_v1_io)(text)
            return read, write, "cgroup v%d" % version
    return None


# ------------------------------------------------------------------- the meter
class _Series:
    __slots__ = ("label", "kind", "last", "adj", "base", "ring", "seen", "reason", "how")

    def __init__(self, label, kind):
        self.label = label
        self.kind = kind
        self.last = None        # (read, write) raw counters of the previous sample
        self.adj = [0, 0]       # monotonic counters (resets of the raw counter are bridged)
        self.base = None        # adj at measurement start
        self.ring = deque(maxlen=RING)    # (t, read_adj, write_adj)
        self.seen = 0.0
        self.reason = ""
        self.how = ""


class DiskIO:
    """Disk write meter. `containers` is [(id, label, container name)]."""

    def __init__(self, runtime=None, containers=(), *, clock=time.monotonic, wall=time.time,
                 proc_io=read_hub_io, disks=read_disks, cgroup_root=DEFAULT_CGROUP_ROOT,
                 proc_dir="/proc", start_thread=True):
        self._runtime = runtime
        self._containers = tuple(containers)
        self._clock = clock
        self._wall = wall
        self._proc_io = proc_io
        self._disks = disks
        self._root = cgroup_root
        self._proc_dir = proc_dir
        self._start_thread = start_thread
        self._cache = ttlcache.SingleFlightCache(clock=clock)
        self._lock = threading.RLock()
        self._series = {}
        self._order = []
        self._measuring = False
        self._since = None
        self._frozen = None          # totals at stop
        self._stopped_at = None
        self._asked = None
        self._thread = None
        self._wake = threading.Event()
        self._last_sample = None

    # ---- activity ---------------------------------------------------------------------
    def touch(self):
        with self._lock:
            self._asked = self._clock()
            alive = self._thread is not None and self._thread.is_alive()
            if self._start_thread and not alive:
                self._thread = threading.Thread(target=self._loop, daemon=True, name="diskio")
                self._thread.start()
        self._wake.set()

    def _loop(self):
        while True:
            try:
                self.sample()
            except Exception:
                pass
            self._wake.clear()
            self._wake.wait(SAMPLE_INTERVAL)
            with self._lock:
                idle = self._clock() - (self._asked or 0)
                if idle > IDLE_STOP:
                    self._thread = None
                    return

    # ---- collection -------------------------------------------------------------------
    def sample(self):
        """One collection (at most once per SAMPLE_INTERVAL, shared by concurrent callers)."""
        return self._cache.get("sample", SAMPLE_INTERVAL, self._collect)

    def _container_counters(self, name):
        runtime = self._runtime
        if runtime is None:
            raise RuntimeError("Container Stationを使えません")
        paths = container_cgroup_paths(runtime, name, self._root, self._proc_dir)
        got = read_container_cgroup_io(paths) if paths else None
        if got is not None:
            return got[0], got[1], got[2]
        ref = runtime.container_ref(name)
        if not ref or not ref.get("running"):
            raise RuntimeError("コンテナが動いていません")
        read, write = runtime.block_io(name)
        return read, write, "docker stats"

    def _collect(self):
        now = self._clock()
        seen = set()
        try:
            read, write = self._proc_io()
            self._record("hub", "Hub（本体）", "hub", now, read, write, "/proc/self/io")
            seen.add("hub")
        except Exception:
            self._fail("hub", "Hub（本体）", "hub", "/proc/self/io を読めません")
        for ident, label, name in self._containers:
            try:
                read, write, how = self._container_counters(name)
                self._record(ident, label, "container", now, read, write, how)
                seen.add(ident)
            except Exception as error:
                self._fail(ident, label, "container", str(error)[:80] or "取得できません")
        try:
            for ident, label, kind, read, write in self._disks():
                self._record(ident, label, kind, now, read, write, "/proc/diskstats")
                seen.add(ident)
        except Exception:
            self._fail("disks", "NASのディスク", "disk", "/proc/diskstats を読めません")
        with self._lock:
            self._last_sample = now
            for ident in [i for i, s in self._series.items() if i not in seen and now - s.seen > 1800]:
                del self._series[ident]
                if ident in self._order:
                    self._order.remove(ident)
        if debuglog.enabled("disk"):
            debuglog.log("disk", "debug", "disk sample", self._summary())
        return now

    def _entry(self, ident, label, kind):
        series = self._series.get(ident)
        if series is None:
            series = self._series[ident] = _Series(label, kind)
            self._order.append(ident)
        return series

    def _record(self, ident, label, kind, now, read, write, how):
        with self._lock:
            series = self._entry(ident, label, kind)
            if series.last is not None:
                for i, (new, old) in enumerate(zip((read, write), series.last)):
                    series.adj[i] += new - old if new >= old else new      # counter restarted
            series.last = (read, write)
            series.seen = now
            series.reason = ""
            series.how = how
            series.ring.append((now, series.adj[0], series.adj[1]))
            if series.base is None:
                series.base = list(series.adj) if self._measuring else None

    def _fail(self, ident, label, kind, reason):
        with self._lock:
            series = self._entry(ident, label, kind)
            series.reason = reason
            series.last = None
            series.seen = self._clock()

    def _summary(self):
        out = {}
        with self._lock:
            for ident, series in self._series.items():
                rate = self._rate(series, 1, 60.0)
                if rate is not None:
                    out[ident] = int(rate)
        return {"write_bps_1m": out}

    # ---- measurement control ------------------------------------------------------------
    def start(self):
        """Reset the baselines: totals count from now."""
        self.touch()
        try:
            self.sample()
        except Exception:
            pass
        with self._lock:
            self._measuring = True
            self._since = self._wall()
            self._frozen = None
            self._stopped_at = None
            for series in self._series.values():
                series.base = list(series.adj)
        return self.snapshot(touch=False)

    def stop(self):
        with self._lock:
            if self._measuring:
                self._frozen = {i: self._totals(s) for i, s in self._series.items()}
            self._measuring = False
            self._stopped_at = self._wall()
        return self.snapshot(touch=False)

    # ---- rates ------------------------------------------------------------------------
    def _rate(self, series, column, window):
        ring = series.ring
        if len(ring) < 2:
            return None
        latest = ring[-1]
        ref = None
        for sample in reversed(ring):
            ref = sample
            if latest[0] - sample[0] >= window:
                break
        span = latest[0] - ref[0]
        if span <= 0 or span < window * MIN_SPAN_FRACTION:
            return None
        return max(0.0, (latest[column] - ref[column]) / span)

    def _totals(self, series):
        if series.base is None:
            return None
        return [max(0, series.adj[0] - series.base[0]), max(0, series.adj[1] - series.base[1])]

    def snapshot(self, touch=True):
        if touch:
            self.touch()
        now = self._clock()
        with self._lock:
            sources = []
            for ident in self._order:
                series = self._series[ident]
                ok = series.last is not None and not series.reason
                totals = self._totals(series) if self._measuring else (
                    (self._frozen or {}).get(ident))
                item = {"id": ident, "label": series.label, "kind": series.kind,
                        "available": ok, "reason": series.reason or None, "how": series.how or None}
                for column, key in ((2, "write"), (1, "read")):
                    item[key] = {name: (self._rate(series, column, window) if ok else None)
                                 for name, window in WINDOWS}
                    item[key]["total"] = totals[column - 1] if (ok or totals) and totals else None
                sources.append(item)
            return {"measuring": self._measuring, "since": self._since,
                    "stopped_at": self._stopped_at, "interval": SAMPLE_INTERVAL,
                    "age": None if self._last_sample is None else round(now - self._last_sample, 1),
                    "sources": sources}
