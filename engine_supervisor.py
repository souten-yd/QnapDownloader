"""Hub-managed JDownloader 2 engine: setup, supervised start, stop.

Two modes (docs/container-runtime.md): "container" (default when Container
Station is available; JD2 runs in the Hub's own labelled Docker container) and
"native" (fallback; Java process on QTS). Only one of them may run at a time.
Setup follows docs/engine-setup.md steps 1-9 and resumes by skipping already
verified artifacts. The java/jar paths come only from the pinned manifest and
engine/state.json, never from a request. State is RAM-only except
engine/state.json, which is written atomically and only when it changes.
Real JD2 behaviour is unverified (no NAS available during development).
"""
from collections import deque
import json
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import stat as stat_module
import subprocess
import sys
import threading
import time

import container_runtime
import debuglog
import durability
import engine_components as components
import jd2_local
import jd2_policy
import jd2_transfer

RING_LINES = 300
MAX_FIRST_RUNS = 6
FIRST_RUN_LIMIT = 600.0
TERM_WAIT = 30.0
KILL_WAIT = 5.0
RESTART_MAX = 5
RESTART_WINDOW = 600.0
AUTO_CLEAR_INTERVAL = 45.0     # v0.1.56: seconds between automatic finished/skipped sweeps
BACKOFF = (5.0, 15.0, 30.0, 60.0, 60.0)
READY_TIMEOUT = 180.0
API_CONFIG = {"deprecatedapienabled": True, "deprecatedapiport": 3128,
              "deprecatedapilocalhostonly": True}
# Container mode: JD2 must listen on the container's own interface so the Hub
# (host) can reach it. This is acceptable only because the container sits alone
# on the dedicated, unpublished bridge network (no -p, no LAN route) and the
# Hub re-verifies the target with docker inspect before every connection.
API_CONFIG_CONTAINER = {"deprecatedapienabled": True, "deprecatedapiport": 3128,
                        "deprecatedapilocalhostonly": False}
MODE_NATIVE = "native"
MODE_CONTAINER = "container"
CONTAINER_STATE_TTL = 2.0
CONTAINER_IP_TTL = 10.0
AVAILABLE_TTL = 60.0
NATIVE_LEFTOVER_TTL = 60.0
NO_CONTAINER_STATION = ("Container Stationが見つかりません。"
                        "App CenterでContainer Stationをインストールしてください")
API_CONFIG_NAME = "org.jdownloader.api.RemoteAPIConfig.json"
MYJD_CONFIG_NAME = "org.jdownloader.api.myjdownloader.MyJDownloaderSettings.json"
MYJD_CONFIG = {"autoconnectenabledv2": False}

STEP_DEFS = (
    ("precheck", "事前確認"),
    ("java_fetch", "Javaの取得と検証"),
    ("java_extract", "Javaの展開と動作確認"),
    ("jd2_fetch", "JD2の取得・検証・配置"),
    ("api_config", "ローカル接続設定の作成・MyJDownloader自動接続の無効化"),
    ("first_run", "初回起動・公式アップデート"),
    ("resident", "常駐起動"),
    ("verify", "ローカル接続の安全確認"),
    ("preset", "保存先と推奨設定"),
    ("done", "完了"),
)
STEP_DEFS_CONTAINER = (
    ("precheck", "Container Stationの検出と事前確認"),
    ("migrate", "内蔵エンジンの停止（移行時のみ）"),
    ("memory", "メモリの確認"),
    ("image", "JD2イメージの取得"),
    ("network", "専用ネットワークの作成"),
    ("jd2_fetch", "JD2の取得・検証・配置"),
    ("api_config", "ローカル接続設定の作成・MyJDownloader自動接続の無効化"),
    ("first_run", "初回起動・公式アップデート"),
    ("resident", "常駐起動"),
    ("verify", "接続確認（コンテナの検証）"),
    ("preset", "保存先と推奨設定"),
    ("done", "完了"),
)

_SECRET = re.compile(
    r"(?i)(pass(?:word|wd)?|pwd|cookie|set-cookie|token|secret|authorization|"
    r"api[_-]?key|session(?:id)?|sid)([\"']?\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;&]+)")
_BEARER = re.compile(r"(?i)(bearer|basic)\s+[A-Za-z0-9._~+/=-]+")


class EngineError(RuntimeError):
    pass


class SetupBusy(EngineError):
    """Setup already running (HTTP 409)."""


class SetupFailure(EngineError):
    pass


def scrub(line):
    line = _BEARER.sub(lambda m: m.group(1) + " ***", line)
    line = _SECRET.sub(lambda m: m.group(1) + m.group(2) + "***", line)
    return line[:500]


def select_heap(mem_total_kib):
    """Java heap (MiB) by installed RAM: <3GiB 256, <6GiB 384, else 512."""
    gib = mem_total_kib / (1024.0 * 1024.0)
    if gib < 3:
        return 256
    if gib < 6:
        return 384
    return 512


def read_mem_total_kib(path="/proc/meminfo"):
    try:
        with open(path, "r", encoding="ascii", errors="replace") as handle:
            for line in handle:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


def _default_state():
    return {"schema": 1, "autostart": False, "setup_complete": False,
            "java": None, "jd2": {"placed": False, "initialized": False},
            "preset_applied": False, "process": None,
            "mode": None, "container": None}


class EngineManager:
    def __init__(self, data_dir, store, *, manifest=None, opener=None,
                 popen=subprocess.Popen, run=subprocess.run, kill=os.kill,
                 proc_root="/proc", api_port=jd2_local.API_PORT,
                 meminfo_path="/proc/meminfo", sleep=time.sleep,
                 monotonic=time.monotonic, machine=None, disk_usage=shutil.disk_usage,
                 runtime=None):
        self.store = store
        self.manifest = manifest if manifest is not None else components.COMPONENTS
        self.engine_dir = Path(data_dir) / "engine"
        self.download_dir = self.engine_dir / ".download"
        self.java_root = self.engine_dir / "java"
        self.java_dir = self.java_root / self.manifest["java"].get("dirname", components.JAVA_DIR_NAME)
        self.java_bin = self.java_dir / "bin" / "java"
        self.jd2_dir = self.engine_dir / "jd2"
        self.jar = self.jd2_dir / "JDownloader.jar"
        self.state_path = self.engine_dir / "state.json"
        self.api_port = api_port
        self.proc_root = proc_root
        self.meminfo_path = meminfo_path
        self._opener = opener
        self._popen_factory = popen
        self._run = run
        self._kill = kill
        self._sleep = sleep
        self._now = monotonic
        self._machine = machine
        self._disk_usage = disk_usage
        self._lock = threading.RLock()
        self._packagizer_checked = False   # Packagizer folder rules checked for this engine run
        self._ring = deque(maxlen=RING_LINES)
        self._ring_lock = threading.Lock()
        self._steps = [{"id": i, "label": l, "status": "pending", "detail": "", "progress": None}
                       for i, l in STEP_DEFS]
        self._error = None
        self._setup_active = False
        self._popen = None
        self._pid = None
        self._ticks = None
        self._ready = False
        self._core_revision = None
        self._want_running = False
        self._stop_evt = threading.Event()
        self._supervisor = None
        self._restart_times = deque()
        self._restart_count = 0
        self._heap_mb = None
        self.runtime = runtime if runtime is not None else container_runtime.Runtime(
            meminfo_path=meminfo_path, config_dir=str(self.engine_dir / "docker-config"))
        self._setup_mode = None
        self._c_alive = False
        self._c_status = None
        self._c_checked = None
        self._ip_cache = None
        self._avail = None
        self._leftover_cache = None
        self.adapter = jd2_local.LocalAdapter(self)
        self._auto_clear_at = -AUTO_CLEAR_INTERVAL    # v0.1.56
        self.sweep_lock = threading.RLock()           # webapp sets it to the JD2 controller lock
        self._saved = None
        self._state = self._load_state()

    # ------------------------------------------------------------- state file
    def _load_state(self):
        state = _default_state()
        try:
            loaded = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and loaded.get("schema") == 1:
                for key in state:
                    if key in loaded:
                        state[key] = loaded[key]
        except (OSError, ValueError):
            pass
        self._saved = json.dumps(state, sort_keys=True)
        return state

    def _save_state(self):
        with self._lock:
            text = json.dumps(self._state, sort_keys=True)
            if text == self._saved:
                return False
            durability.write_json(self.state_path, self._state)
            self._saved = text
            return True

    def _update_state(self, **values):
        with self._lock:
            self._state.update(values)
            self._save_state()

    # -------------------------------------------------------------- RAM log
    def log(self, line):
        clean = scrub(str(line).rstrip("\r\n"))
        with self._ring_lock:
            self._ring.append(clean)
        debuglog.log("engine", "info", clean)

    def log_lines(self):
        with self._ring_lock:
            return list(self._ring)

    # ---------------------------------------------------------------- status
    @property
    def preset_applied(self):
        return self._state.get("preset_applied") is True

    @property
    def installed(self):
        return self._state.get("setup_complete") is True

    @property
    def autostart(self):
        return self._state.get("autostart") is True

    def note_preset_applied(self):
        self._update_state(preset_applied=True)

    def note_preset_stale(self):
        self._update_state(preset_applied=False)

    # ------------------------------------------------------------------ mode
    @property
    def mode(self):
        """Mode in effect: the one being set up, else the installed one."""
        return self._setup_mode or self._state.get("mode") or MODE_NATIVE

    def container_available(self, refresh=False):
        """Container Station usable? Cached so status polling stays cheap."""
        now = self._now()
        with self._lock:
            cached = self._avail
        if cached is not None and not refresh and now - cached[0] < AVAILABLE_TTL:
            return cached[1]
        version = self.runtime.version()
        with self._lock:
            self._avail = (now, version)
        return version

    def path_mapping(self):
        """Container mode: the NAS folder mounted as /downloads, else None."""
        if self.mode != MODE_CONTAINER:
            return None
        mounted = (self._state.get("container") or {}).get("mounted_dir")
        return {"nas_root": mounted, "jd2_root": container_runtime.CONTAINER_DOWNLOADS}

    def _step(self, sid, status, detail=None, progress=None):
        with self._lock:
            for item in self._steps:
                if item["id"] == sid:
                    item["status"] = status
                    if detail is not None:
                        item["detail"] = detail
                    item["progress"] = progress
                    return

    def _reset_steps(self, mode=MODE_NATIVE):
        defs = STEP_DEFS_CONTAINER if mode == MODE_CONTAINER else STEP_DEFS
        with self._lock:
            self._steps = [{"id": i, "label": l, "status": "pending", "detail": "", "progress": None}
                           for i, l in defs]

    def _steps_view(self, mode):
        """Step list for the UI; before any run it matches the mode that would run."""
        if all(step["status"] == "pending" for step in self._steps) and not self._setup_active:
            defs = STEP_DEFS_CONTAINER if mode == MODE_CONTAINER else STEP_DEFS
            return [{"id": i, "label": l, "status": "pending", "detail": "", "progress": None}
                    for i, l in defs]
        return [dict(step) for step in self._steps]

    def phase(self):
        with self._lock:
            if self._setup_active:
                return "setup"
        if self._alive():
            return "running" if self._ready else "starting"
        with self._lock:
            if self._error:
                return "failed"
            return "stopped" if self.installed else "not_installed"

    def status(self):
        phase = self.phase()
        cs_version = self.container_available()
        mode = self._setup_mode or self._state.get("mode") or (
            MODE_CONTAINER if cs_version else MODE_NATIVE)
        with self._lock:
            java = self._state.get("java") or {}
            comps = []
            if mode == MODE_CONTAINER:
                comps.append({
                    "name": "image",
                    "label": "JD2コンテナイメージ (Eclipse Temurin 21 JRE・ダイジェスト固定)",
                    "pinned_version": "21-jre",
                    "installed": (self._state.get("container") or {}).get("image") ==
                    container_runtime.IMAGE_JD2,
                    "version": None, "sha256": container_runtime.IMAGE_JD2.split(":")[-1][:12],
                    "size": None, "source": "docker.io/library/eclipse-temurin"})
            for name in (("jd2",) if mode == MODE_CONTAINER else ("java", "jd2")):
                item = self.manifest[name]
                if name == "java":
                    ok = bool(java) and self.java_bin.exists()
                    version = java.get("version") if ok else None
                else:
                    ok = bool(self._state["jd2"].get("placed")) and self.jar.exists()
                    version = self._core_revision if ok else None
                comps.append({"name": name, "label": item["label"],
                              "pinned_version": item["version"],
                              "installed": ok, "version": version,
                              "sha256": item["sha256"][:12], "size": item["size"],
                              "source": item["url"]})
            native_installed = (self._state.get("mode") or MODE_NATIVE) == MODE_NATIVE and \
                self.installed
            return {
                "mode": mode,
                "preferred_mode": MODE_CONTAINER if cs_version else MODE_NATIVE,
                "container": {"available": bool(cs_version), "version": cs_version,
                              "state": self._c_status or ("running" if self._c_alive else "unknown"),
                              "name": container_runtime.JD2_NAME,
                              "network": container_runtime.JD2_NET,
                              "image": container_runtime.IMAGE_JD2.split("@")[0],
                              "mounted_dir": (self._state.get("container") or {}).get("mounted_dir")},
                "native_leftovers": self.native_leftovers(),
                "can_migrate": bool(native_installed and cs_version and not self._setup_active),
                "phase": phase,
                "installed": self.installed,
                "running": phase in ("running", "starting"),
                "ready": phase == "running",
                "setup_running": self._setup_active,
                "autostart": self.autostart,
                "preset_applied": self.preset_applied,
                "steps": self._steps_view(mode),
                "error": self._error,
                "components": comps,
                "heap_mb": self._heap_mb,
                "core_revision": self._core_revision,
                "restart_count": self._restart_count,
            }

    # ------------------------------------------------- native leftovers
    def _leftover_dirs(self):
        """Native-only artifacts: the extracted JRE and download leftovers.

        engine/jd2 is NEVER listed here: the container bind-mounts and reuses it.
        """
        return (self.java_root, self.download_dir)

    @staticmethod
    def _tree_bytes(path):
        """Sum of regular-file sizes below path; symlinks are never followed."""
        total = 0
        try:
            if os.path.islink(path):
                return 0
            for base, dirs, files in os.walk(path, followlinks=False):
                for name in files:
                    try:
                        info = os.lstat(os.path.join(base, name))
                    except OSError:
                        continue
                    if stat_module.S_ISREG(info.st_mode):
                        total += info.st_size
        except OSError:
            pass
        return total

    def native_leftovers(self, refresh=False):
        now = self._now()
        with self._lock:
            cached = self._leftover_cache
        if cached is not None and not refresh and now - cached[0] < NATIVE_LEFTOVER_TTL:
            return dict(cached[1])
        present = any(os.path.lexists(str(d)) for d in self._leftover_dirs())
        size = sum(self._tree_bytes(str(d)) for d in self._leftover_dirs()) if present else 0
        value = {"present": present, "bytes": size}
        with self._lock:
            self._leftover_cache = (now, value)
        return dict(value)

    def remove_native_leftovers(self):
        with self._lock:
            if self._setup_active:
                raise EngineError("初期設定の実行中は削除できません")
            mode = self._state.get("mode")
            pid, ticks = self._pid, self._ticks
        if mode != MODE_CONTAINER or not self.installed:
            raise EngineError("コンテナ方式の初期設定が完了していないため削除できません")
        if pid is not None and self._identity_ok(pid, ticks):
            raise EngineError("QTS直接実行のエンジンが動作中のため削除できません")
        for target in self._leftover_dirs():
            if os.path.islink(str(target)):
                raise EngineError("%s がシンボリックリンクのため削除しません" % target.name)
        before = self.native_leftovers(refresh=True)
        failures = []

        def collect(function, path, excinfo):
            failures.append(str(path))
        for target in self._leftover_dirs():
            if os.path.isdir(str(target)) and not os.path.islink(str(target)):
                shutil.rmtree(str(target), onerror=collect)
        with self._lock:
            self._leftover_cache = None
            if self._state.get("java") is not None:
                self._state["java"] = None
                self._save_state()
        if failures:
            raise EngineError("一部を削除できませんでした（%d件）" % len(failures))
        self.log("[hub] 従来方式のJava資産を削除しました（engine/jd2は保持）")
        return {"removed": True, "freed_bytes": before["bytes"]}

    # --------------------------------------------------------- process facts
    def _read_proc(self, pid, name):
        with open(os.path.join(self.proc_root, str(int(pid)), name), "rb") as handle:
            return handle.read(65536)

    def _start_ticks(self, pid):
        try:
            raw = self._read_proc(pid, "stat").decode("ascii", "replace")
            fields = raw.rsplit(")", 1)[1].split()
            if fields[0] == "Z":
                return None
            return int(fields[19])
        except (OSError, ValueError, IndexError):
            return None

    def _identity_ok(self, pid, ticks):
        """pid is alive, same start time, and runs our java with our jar."""
        if pid is None or ticks is None:
            return False
        if self._start_ticks(pid) != ticks:
            return False
        try:
            args = [os.fsdecode(a) for a in self._read_proc(pid, "cmdline").split(b"\0") if a]
        except OSError:
            return False
        return (bool(args) and args[0] == str(self.java_bin) and str(self.jar) in args)

    def _alive(self):
        with self._lock:
            popen, pid, ticks = self._popen, self._pid, self._ticks
        if pid is None:
            return self._container_alive() if self.mode == MODE_CONTAINER else False
        if popen is not None and popen.poll() is not None:
            return False
        return self._identity_ok(pid, ticks)

    def _container_alive(self, force=False):
        now = self._now()
        with self._lock:
            if not force and self._c_checked is not None and \
                    now - self._c_checked < CONTAINER_STATE_TTL:
                return self._c_alive
            previous = self._c_alive
        try:
            info = self.runtime.container_state(container_runtime.JD2_NAME)
        except container_runtime.ContainerError:
            return previous   # unknown: keep the last known answer
        alive = bool(info["exists"] and info["managed"] and info["running"])
        with self._lock:
            self._c_alive, self._c_status, self._c_checked = alive, info["status"], now
        return alive

    def _forget_container(self):
        with self._lock:
            self._c_alive, self._c_checked, self._ip_cache = False, None, None
            self._c_status = None
            self._ready = False

    def _container_ip(self):
        """Verified container IP (docker inspect; label, network, no ports), cached 10 s."""
        now = self._now()
        with self._lock:
            cached = self._ip_cache
        if cached is not None and now - cached[1] < CONTAINER_IP_TTL:
            return cached[0]
        try:
            address = self.runtime.inspect_ip(container_runtime.JD2_NAME, container_runtime.JD2_NET)
        except container_runtime.ContainerError as error:
            with self._lock:
                self._ip_cache = None
            raise jd2_local.LocalUnavailable("コンテナの接続先を検証できません: %s" % error) from None
        with self._lock:
            self._ip_cache = (address, now)
        return address

    def is_ours_running(self):
        return self._alive()

    def connection_target(self):
        with self._lock:
            pid, ticks = self._pid, self._ticks
        if pid is None and self.mode == MODE_CONTAINER:
            # No /proc listener-owner check: the target is re-verified by
            # docker inspect instead (labelled container, our network only).
            return (self._container_ip(), self.api_port)
        if pid is None or not self._identity_ok(pid, ticks):
            raise jd2_local.LocalUnavailable("エンジン未起動/未導入")
        jd2_local.verify_listener(self.api_port, pid, self.proc_root)
        return self.api_port

    def command(self):
        if self._heap_mb is None:
            kib = read_mem_total_kib(self.meminfo_path)
            self._heap_mb = select_heap(kib) if kib else 256
        return [str(self.java_bin), "-Xmx%dm" % self._heap_mb, "-XX:+UseSerialGC",
                "-Djava.awt.headless=true", "-jar", str(self.jar), "-norestart"]

    # ----------------------------------------------------------- process ctl
    def _launch_process(self):
        if self.mode == MODE_CONTAINER:
            return self._launch_container()
        self._guard_single_writer_native()
        try:
            self.switch_api_config(MODE_NATIVE)
        except SetupFailure as error:
            # Unknown content: leave it. The native listener check still
            # refuses anything that is not loopback-only.
            self.log("[hub] 接続設定は変更しません: %s" % error)
        with self._lock:
            if self._pid is not None and self._alive():
                raise EngineError("エンジンはすでに起動しています")
            if not self.java_bin.is_file() or not self.jar.is_file():
                raise EngineError("エンジンが導入されていません。初期設定を実行してください")
            command = self.command()
            env = {"PATH": "/usr/bin:/bin", "HOME": str(self.jd2_dir), "LANG": "C.UTF-8"}
            popen = self._popen_factory(
                command, cwd=str(self.jd2_dir), stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                start_new_session=True, close_fds=True, env=env)
            ticks = self._start_ticks(popen.pid)
            if ticks is None:
                try:
                    popen.kill()
                except OSError:
                    pass
                raise EngineError("プロセスの開始時刻を確認できないため起動を中止しました")
            self._popen, self._pid, self._ticks = popen, popen.pid, ticks
            self._ready = False
            self._state["process"] = {"pid": popen.pid, "start_ticks": ticks}
            self._save_state()
        self.log("[hub] エンジンを起動しました (pid %d, -Xmx%dm)" % (popen.pid, self._heap_mb))
        threading.Thread(target=self._pump, args=(popen,), daemon=True).start()

    def _guard_single_writer_native(self):
        """Never run the native engine while our container may hold engine/jd2."""
        if self._state.get("container") is None:
            return
        try:
            if self._container_alive(force=True):
                raise EngineError("コンテナ版のエンジンが稼働中のため内蔵エンジンは起動できません")
        except container_runtime.ContainerError:
            pass

    # ------------------------------------------------------ container control
    def _launch_container(self):
        if self._pid is not None and self._alive():
            raise EngineError("内蔵エンジンが稼働中です。停止してから起動してください")
        if self._container_alive(force=True):
            raise EngineError("エンジンはすでに起動しています")
        if not self.jar.is_file():
            raise EngineError("エンジンが導入されていません。初期設定を実行してください")
        folder = self.store.settings().get("download_dir")
        if not folder or not os.path.isdir(folder):
            raise EngineError("ダウンロード保存先フォルダが選択されていません")
        mounted = str(Path(folder).resolve())
        try:
            self.switch_api_config(MODE_CONTAINER)
            uid, gid = container_runtime.prepare_data_owner(str(self.jd2_dir))
            container_runtime.check_memory(self.meminfo_path)
            self.runtime.network_ensure(container_runtime.JD2_NET)
            self.runtime.remove(container_runtime.JD2_NAME)   # stale, ours only
            self.runtime.run_jd2(str(self.jd2_dir), mounted, uid, gid)
        except (container_runtime.ContainerError, SetupFailure) as error:
            self.log("[hub] コンテナ起動失敗: %s" % error)
            raise EngineError(str(error)) from None
        finally:
            self._forget_container()
        info = dict(self._state.get("container") or {})
        info.update(image=container_runtime.IMAGE_JD2, mounted_dir=mounted)
        self._update_state(container=info, process=None)
        self._container_alive(force=True)
        with self._lock:
            self._ready = False
        self.log("[hub] JD2コンテナを起動しました (%s, 保存先 %s -> /downloads)" % (
            container_runtime.JD2_NAME, mounted))

    def _terminate_container(self):
        try:
            stopped = self.runtime.stop(container_runtime.JD2_NAME)
            self.runtime.remove(container_runtime.JD2_NAME)
        except container_runtime.ContainerError as error:
            self.log("[hub] コンテナ停止失敗: %s" % error)
            raise EngineError("コンテナを停止できませんでした: %s" % error) from None
        finally:
            self._forget_container()
        self._clear_process()
        return stopped

    # -------------------------------------------------- RemoteAPI config file
    def _match_owner(self, *paths):
        """Hub is root: give files it wrote to the container user (the jd2 dir owner)."""
        try:
            if os.geteuid() != 0:
                return
            info = os.stat(str(self.jd2_dir))
            if info.st_uid == 0:
                return
            for path in paths:
                os.lchown(str(path), info.st_uid, info.st_gid)
        except OSError:
            pass

    def switch_api_config(self, mode):
        """RemoteAPIConfig for `mode`, then the MyJDownloader auto-connect switch-off."""
        action = self._switch_api_config(mode)
        self.disable_myjd_autoconnect(mode)
        return action

    def disable_myjd_autoconnect(self, mode):
        """Merge autoconnectenabledv2=false into the MyJDownloader settings file.

        Headless JD2 otherwise asks for MyJDownloader logins on a console that
        does not exist (IllegalStateException: No Console Available!). Only that
        one key is set; other keys are preserved and no credential key is ever
        written. Same refusal rules as the API config. Returns "created",
        "same" or "rewritten". Unverified on a real JD2.
        """
        path = self.jd2_dir / "cfg" / MYJD_CONFIG_NAME
        name = "cfg/" + MYJD_CONFIG_NAME
        if not (path.exists() or path.is_symlink()):
            durability.write_json(path, MYJD_CONFIG)
            if mode == MODE_CONTAINER:
                self._match_owner(path.parent, path)
            return "created"
        if path.is_symlink() or not path.is_file():
            raise SetupFailure("MyJDownloader設定を安全に更新できないため中止しました（通常のファイルではありません）: " + name)
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise SetupFailure("MyJDownloader設定を安全に更新できないため中止しました（JSONとして読めません）: " + name) from None
        if not isinstance(current, dict):
            raise SetupFailure("MyJDownloader設定を安全に更新できないため中止しました（JSONオブジェクトではありません）: " + name)
        if current.get("autoconnectenabledv2") is False:
            return "same"
        merged = dict(current)
        merged.update(MYJD_CONFIG)
        durability.write_json(path, merged)
        if mode == MODE_CONTAINER:
            self._match_owner(path.parent, path)
        return "rewritten"

    def _switch_api_config(self, mode):
        """Create or update cfg/RemoteAPIConfig.json for `mode`.

        JD2 rewrites its cfg files with extra keys after the first start, so
        the file is merged, not compared: it must be a JSON object whose
        deprecatedapienabled is true (or missing) and deprecatedapiport is
        3128 (or missing). Only those two keys and deprecatedapilocalhostonly
        are set (false in container mode, true in native mode); every other key
        is preserved. Written atomically with fsync. Anything else is refused
        with a reason. Returns "created", "same" or "rewritten".
        """
        path = self.jd2_dir / "cfg" / API_CONFIG_NAME
        wanted = API_CONFIG_CONTAINER if mode == MODE_CONTAINER else API_CONFIG
        if not (path.exists() or path.is_symlink()):
            durability.write_json(path, wanted)
            if mode == MODE_CONTAINER:
                self._match_owner(path.parent, path)
            return "created"
        if path.is_symlink() or not path.is_file():
            raise SetupFailure("既存の接続設定ファイルを安全に更新できないため中止しました（通常のファイルではありません）: cfg/" + API_CONFIG_NAME)
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise SetupFailure("既存の接続設定ファイルを安全に更新できないため中止しました（JSONとして読めません）: cfg/" + API_CONFIG_NAME) from None
        if not isinstance(current, dict):
            raise SetupFailure("既存の接続設定ファイルを安全に更新できないため中止しました（JSONオブジェクトではありません）: cfg/" + API_CONFIG_NAME)
        enabled = current.get("deprecatedapienabled", True)
        port = current.get("deprecatedapiport", wanted["deprecatedapiport"])
        if enabled is not True:
            raise SetupFailure("既存の接続設定でdeprecatedapienabledがtrueではないため中止しました: cfg/" + API_CONFIG_NAME)
        if type(port) is not int or port != wanted["deprecatedapiport"]:
            raise SetupFailure("既存の接続設定のdeprecatedapiportが%dではないため中止しました: cfg/%s" % (
                wanted["deprecatedapiport"], API_CONFIG_NAME))
        merged = dict(current)
        merged.update(wanted)
        if merged == current:
            return "same"
        durability.write_json(path, merged)
        if mode == MODE_CONTAINER:
            self._match_owner(path.parent, path)
        return "rewritten"

    def _pump(self, popen):
        stream = popen.stdout
        try:
            for raw in iter(stream.readline, b""):
                self.log(raw.decode("utf-8", "replace"))
        except (OSError, ValueError):
            pass
        finally:
            try:
                stream.close()
            except (OSError, ValueError):
                pass

    def _clear_process(self):
        with self._lock:
            self._popen = self._pid = self._ticks = None
            self._ready = False
            self._state["process"] = None
            self._save_state()

    def _terminate(self, term_wait=TERM_WAIT, kill_wait=KILL_WAIT):
        """SIGTERM, wait, SIGKILL; each signal only after identity re-check."""
        with self._lock:
            pid, ticks, popen = self._pid, self._ticks, self._popen
        if pid is None:
            return self._terminate_container() if self.mode == MODE_CONTAINER else False
        if not self._identity_ok(pid, ticks):
            self.log("[hub] pid %d は記録したエンジンと一致しないためシグナルを送りません" % pid)
            self._clear_process()
            return False
        try:
            self._kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        deadline = self._now() + term_wait
        while self._alive() and self._now() < deadline:
            self._sleep(0.2)
        if self._alive():
            if self._identity_ok(pid, ticks):
                self.log("[hub] 終了しないためSIGKILLを送ります")
                try:
                    self._kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            deadline = self._now() + kill_wait
            while self._alive() and self._now() < deadline:
                self._sleep(0.1)
            if self._alive():
                raise EngineError("エンジンを停止できませんでした")
        if popen is not None:
            try:
                popen.wait(timeout=1)
            except Exception:
                pass
        self._clear_process()
        return True

    def start(self):
        if not self.installed:
            raise EngineError("エンジンが導入されていません。初期設定を実行してください")
        with self._lock:
            if self._setup_active:
                raise EngineError("初期設定の実行中です")
            if self._alive():
                raise EngineError("エンジンはすでに起動しています")
            self._error = None
            self._want_running = True
            self._stop_evt.clear()
            self._restart_times.clear()
        try:
            self._launch_process()
        except Exception:
            self._want_running = False
            raise
        self._begin_supervision()

    def _begin_supervision(self):
        with self._lock:
            self._packagizer_checked = False
            self._want_running = True
            self._stop_evt.clear()
            thread = threading.Thread(target=self._supervise, daemon=True)
            self._supervisor = thread
        thread.start()

    def stop(self):
        with self._lock:
            self._want_running = False
            self._stop_evt.set()
            supervisor = self._supervisor
        try:
            result = self._terminate()
        finally:
            if supervisor is not None and supervisor is not threading.current_thread():
                supervisor.join(timeout=10)
        return {"stopped": True, "signalled": result}

    def set_autostart(self, enabled):
        if type(enabled) is not bool:
            raise ValueError("enabledはbooleanで指定してください")
        self._update_state(autostart=enabled)
        return enabled

    def _wait_exit(self, timeout):
        popen = self._popen
        if popen is not None:
            try:
                popen.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                return False
            return True
        self._stop_evt.wait(timeout)
        return not self._alive()

    def _supervise(self):
        started = self._now()
        while not self._stop_evt.is_set():
            exited = self._wait_exit(3.0 if not self._ready else 5.0)
            if self._stop_evt.is_set():
                return
            if exited or not self._alive():
                self.log("[hub] エンジンが終了しました")
                self._clear_process()
                if not (self._want_running and self.autostart):
                    self._error = "エンジンが停止しました（自動再起動は無効）"
                    return
                now = self._now()
                while self._restart_times and now - self._restart_times[0] > RESTART_WINDOW:
                    self._restart_times.popleft()
                if len(self._restart_times) >= RESTART_MAX:
                    self._error = "エンジンが短時間に繰り返し終了したため再起動を止めました"
                    self._want_running = False
                    return
                delay = BACKOFF[min(len(self._restart_times), len(BACKOFF) - 1)]
                self._restart_times.append(now)
                self._restart_count += 1
                self.log("[hub] %d秒後に再起動します" % delay)
                if self._stop_evt.wait(delay):
                    return
                try:
                    self._launch_process()
                except EngineError as error:
                    self._error = str(error)
                    self._want_running = False
                    return
                started = self._now()
                continue
            if self._ready and not self._packagizer_checked:
                self.check_packagizer()       # once per engine run (no periodic sweep: v0.1.49)
            if self._ready:
                self.auto_sweep()
            if not self._ready:
                try:
                    revision = self.adapter.core_revision()
                    with self._lock:
                        self._ready = True
                        self._core_revision = str(revision)
                except jd2_local.LocalUnavailable:
                    if self._now() - started > READY_TIMEOUT and not self._error:
                        self._error = "エンジンの起動確認がタイムアウトしました。RAMログを確認してください"

    def check_packagizer(self):
        """Once per engine run: disable Packagizer rules that save into per-package folders
        (they override addLinks' destination). Never raises."""
        self._packagizer_checked = True
        try:
            if not self.is_ours_running():
                return None
            rules = self.adapter.packagizer_folder_rules(True)
            if rules:
                debuglog.log("jd2", "info", "Packagizerのフォルダ規則", {"result": str(rules.get("current"))[:200]})
            return rules
        except Exception as error:
            debuglog.log("jd2", "debug", "Packagizerの確認に失敗", {"error": str(error)[:120]})
            return None

    def auto_sweep(self):
        """v0.1.56: periodic housekeeping while the engine runs, at most once per AUTO_CLEAR_INTERVAL.

        option auto_clear_downloads: drop finished / "already exists" entries from JD2's list (same
        calls as the clear button; list only, files stay; one read-only query when nothing is done).
        option auto_clear_links: forget Hub link rows already handed to JD2 (state "sent" only).
        Returns (finished, skipped) removed from JD2; never raises."""
        now = self._now()
        if now - self._auto_clear_at < AUTO_CLEAR_INTERVAL:
            return (0, 0)
        self._auto_clear_at = now
        try:
            settings = self.store.settings()
            if settings.get("auto_clear_links") is True:
                self.store.remove_sent_links()
            if settings.get("auto_clear_downloads") is not True or not self.is_ours_running():
                return (0, 0)
            with self.sweep_lock:
                done = self.adapter.sweep_finished()
            if done[0] or done[1]:
                debuglog.log("engine", "debug", "完了・スキップの自動消去", {"finished": done[0], "skipped": done[1]})
            return done
        except Exception as error:
            debuglog.log("engine", "debug", "完了・スキップの自動消去に失敗", {"error": str(error)[:120]})
            return (0, 0)

    def flatten_sweep(self):
        """Move not-yet-started packages out of per-package subfolders (cheap, 2 calls).

        Returns the number moved; never raises. Only runs against our own running engine."""
        try:
            folder = self.store.settings().get("download_dir")
            if not folder or not self.is_ours_running():
                return 0
            moved = jd2_transfer.flatten_waiting(self.adapter, folder)
        except Exception as error:
            debuglog.log("jd2", "debug", "フォルダ整理の巡回に失敗", {"error": str(error)[:120]})
            return 0
        return moved

    def startup(self):
        """Hub start: adopt a surviving engine, or autostart in the background."""
        pid = (self._state.get("process") or {}).get("pid")
        ticks = (self._state.get("process") or {}).get("start_ticks")
        if pid is not None and self._identity_ok(pid, ticks):
            with self._lock:
                self._pid, self._ticks, self._popen = pid, ticks, None
            self.log("[hub] 稼働中のエンジン (pid %d) を引き継ぎました" % pid)
            self._begin_supervision()
            return "adopted"
        if pid is not None:
            self._clear_process()
        if self.installed and self.mode == MODE_CONTAINER and self._container_alive(force=True):
            self.log("[hub] 稼働中のJD2コンテナを引き継ぎました")
            self._begin_supervision()
            return "adopted"
        if self.installed and self.autostart:
            threading.Thread(target=self._autostart, daemon=True).start()
            return "autostart"
        return "idle"

    def _autostart(self):
        try:
            self.start()
        except EngineError as error:
            self._error = str(error)

    def shutdown(self):
        try:
            self.stop()
        except EngineError as error:
            self.log("[hub] 停止失敗: %s" % error)

    # ----------------------------------------------------------------- setup
    def resolve_mode(self, requested=None):
        """Mode for a setup run: explicit, else the installed one, else the best available."""
        if requested not in (None, MODE_CONTAINER, MODE_NATIVE):
            raise ValueError("modeはcontainerまたはnativeで指定してください")
        if requested is None:
            if self.installed:
                requested = self._state.get("mode") or MODE_NATIVE
            else:
                # Never fall back to native silently: it must be chosen explicitly.
                requested = MODE_CONTAINER
        if requested == MODE_CONTAINER and not self.container_available(refresh=True):
            raise EngineError(NO_CONTAINER_STATION)
        return requested

    def _begin_setup(self, requested):
        with self._lock:
            if self._setup_active:
                raise SetupBusy("初期設定はすでに実行中です")
        mode = self.resolve_mode(requested)
        # Migration: a running native engine is stopped by the container setup.
        migrating = mode == MODE_CONTAINER and self._pid is not None
        alive = self._alive()
        with self._lock:
            if self._setup_active:
                raise SetupBusy("初期設定はすでに実行中です")
            if alive and not migrating:
                raise EngineError("エンジンを停止してから再実行してください")
            self._setup_active = True
            self._setup_mode = mode
            self._error = None
        return mode

    def start_setup(self, mode=None):
        self._begin_setup(mode)
        threading.Thread(target=self._setup_thread, daemon=True).start()

    def run_setup(self, mode=None):
        """Synchronous variant (tests); same guard as start_setup."""
        self._begin_setup(mode)
        self._setup_thread()

    def _setup_thread(self):
        try:
            self._execute_setup()
        finally:
            with self._lock:
                self._setup_active = False
                self._setup_mode = None

    def _execute_setup(self):
        mode = self._setup_mode or MODE_NATIVE
        self._reset_steps(mode)
        self._error = None
        if mode == MODE_CONTAINER:
            steps = (("precheck", self._c_precheck), ("migrate", self._c_migrate),
                     ("memory", self._c_memory), ("image", self._c_image),
                     ("network", self._c_network), ("jd2_fetch", self._s_jd2_fetch),
                     ("api_config", self._c_api_config), ("first_run", self._s_first_run),
                     ("resident", self._s_resident), ("verify", self._s_verify),
                     ("preset", self._s_preset))
        else:
            steps = (("precheck", self._s_precheck), ("java_fetch", self._s_java_fetch),
                     ("java_extract", self._s_java_extract), ("jd2_fetch", self._s_jd2_fetch),
                     ("api_config", self._s_api_config), ("first_run", self._s_first_run),
                     ("resident", self._s_resident), ("verify", self._s_verify),
                     ("preset", self._s_preset))
        current = "precheck"
        try:
            for sid, function in steps:
                current = sid
                self._step(sid, "running")
                status, detail = function()
                self._step(sid, status, detail)
            current = "done"
            self._step("done", "running")
            if self.installed:
                self._update_state(setup_complete=True, mode=mode)
            else:
                # First completion enables autostart (can be switched off).
                self._update_state(setup_complete=True, autostart=True, mode=mode)
            self._step("done", "done", "初期設定が完了しました")
        except Exception as error:
            if isinstance(error, (SetupFailure, components.DownloadError,
                                  components.ExtractError, jd2_local.LocalUnavailable,
                                  jd2_policy.PresetUnavailable, jd2_policy.PresetChanged,
                                  container_runtime.ContainerError, EngineError)):
                reason = str(error)
            else:
                reason = "予期しないエラー: %s" % type(error).__name__
            self.log("[hub] 失敗(%s): %s" % (current, reason))
            self._step(current, "failed", reason)
            self._error = reason
            try:
                self._want_running = False
                self._stop_evt.set()
                self._terminate()
            except Exception:
                pass

    def _machine_name(self):
        return self._machine if self._machine is not None else platform.machine()

    def _precheck_common(self):
        if not sys.platform.startswith("linux") or self._machine_name().lower() not in ("x86_64", "amd64"):
            raise SetupFailure("x86_64 Linux以外では初期設定を開始できません")
        folder = self.store.settings().get("download_dir")
        if not folder or not os.path.isdir(folder):
            raise SetupFailure("先にダウンロード保存先フォルダを選択してください")
        if not os.access(folder, os.W_OK):
            raise SetupFailure("保存先フォルダに書き込み権限がありません")
        self.engine_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        components.check_disk_free(self.engine_dir, usage=self._disk_usage)
        return folder

    # ---- container mode steps (docs/container-runtime.md section 3)
    def _c_precheck(self):
        self._precheck_common()
        version = self.container_available(refresh=True)
        if not version:
            raise SetupFailure(NO_CONTAINER_STATION)
        return "done", "Container Station (Docker %s) を検出しました" % version

    def _c_migrate(self):
        if self._pid is None:
            return "skipped", "停止が必要な内蔵エンジンはありません"
        self.log("[hub] 移行のため内蔵エンジンを停止します（単一ライター保証）")
        self.stop()
        if self._pid is not None or self._alive():
            raise SetupFailure("内蔵エンジンを停止できないため移行を中止しました")
        return "done", "内蔵エンジンを停止しました（engine/jd2のデータはそのまま使います）"

    def _c_memory(self):
        available = container_runtime.check_memory(self.meminfo_path)
        return "done", "空きメモリ %d MiB（必要 %d MiB 以上）" % (
            available // 1024, container_runtime.MIN_AVAILABLE_KIB // 1024)

    def _c_image(self):
        ref = container_runtime.IMAGE_JD2
        if self.runtime.image_present(ref):
            detail = "取得済みのイメージを使います"
            status = "skipped"
        else:
            self._step("image", "running", "取得中（ダイジェスト固定）…")
            self.runtime.pull(ref)
            detail, status = "ダイジェスト固定のイメージを取得しました", "done"
        info = dict(self._state.get("container") or {})
        info["image"] = ref
        self._update_state(container=info)
        return status, detail

    def _c_network(self):
        created = self.runtime.network_ensure(container_runtime.JD2_NET)
        return ("done" if created else "skipped"), \
            ("%s を作成しました（ポート非公開）" % container_runtime.JD2_NET if created
             else "作成済みです")

    def _c_api_config(self):
        action = self.switch_api_config(MODE_CONTAINER)
        uid, gid = container_runtime.prepare_data_owner(str(self.jd2_dir))
        folder = self.store.settings().get("download_dir")
        if not container_runtime.writable_by(os.stat(folder), uid, gid):
            raise SetupFailure(
                "保存先フォルダにコンテナ利用者(uid %d)の書き込み権限がありません。"
                "共有フォルダの権限を確認してください" % uid)
        text = {"created": "作成しました", "rewritten": "内蔵エンジン用から切り替えました",
                "same": "作成済みです"}[action]
        return ("skipped" if action == "same" else "done"), \
            "コンテナ用の接続設定（専用ネットワーク内のみ）を%s / 実行uid %d:%d" % (text, uid, gid)

    def _s_precheck(self):
        self._precheck_common()
        kib = read_mem_total_kib(self.meminfo_path)
        self._heap_mb = select_heap(kib) if kib else 256
        return "done", "RAM %s MiB / Javaヒープ -Xmx%dm" % (
            (kib // 1024) if kib else "不明", self._heap_mb)

    def _fetch(self, name):
        item = self.manifest[name]

        def progress(done, total):
            self._step("java_fetch" if name == "java" else "jd2_fetch", "running",
                       "%.1f / %.1f MB" % (done / 1048576.0, total / 1048576.0),
                       int(done * 100 / total))
        return components.fetch_verified(item, self.download_dir, opener=self._opener,
                                         progress=progress)

    def _verified_file(self, name):
        item = self.manifest[name]
        path = self.download_dir / (item["filename"] + ".verified")
        try:
            if path.is_file() and path.stat().st_size == item["size"] and \
                    components.file_sha256(path) == item["sha256"]:
                return path
        except OSError:
            pass
        return None

    def _s_java_fetch(self):
        item = self.manifest["java"]
        java = self._state.get("java")
        if java and java.get("sha256") == item["sha256"] and self.java_bin.is_file():
            return "skipped", "検証済みのJavaがあるためスキップしました"
        if self._verified_file("java"):
            return "skipped", "検証済みの取得ファイルがあるためスキップしました"
        components.check_disk_free(self.engine_dir, usage=self._disk_usage)
        self.download_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._fetch("java")
        return "done", "SHA-256とサイズを検証しました"

    def _java_version_check(self):
        try:
            result = self._run([str(self.java_bin), "-version"], stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=20,
                               env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"})
        except (OSError, subprocess.SubprocessError) as error:
            raise SetupFailure("java -version を実行できません: %s" % type(error).__name__)
        text = (result.stdout or b"").decode("utf-8", "replace")
        for line in text.splitlines()[:3]:
            self.log("[java] " + line)
        if result.returncode != 0 or "21" not in text:
            raise SetupFailure("java -version の結果が想定と一致しません")

    def _s_java_extract(self):
        item = self.manifest["java"]
        java = self._state.get("java")
        if not (java and java.get("sha256") == item["sha256"] and self.java_bin.is_file()):
            archive = self._verified_file("java")
            if archive is None:
                raise SetupFailure("検証済みのJavaアーカイブがありません")
            staging = self.java_root / ".staging"
            self.java_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            shutil.rmtree(str(staging), ignore_errors=True)
            components.safe_extract(archive, staging)
            source = staging / item["dirname"]
            if not (source / "bin" / "java").is_file():
                shutil.rmtree(str(staging), ignore_errors=True)
                raise SetupFailure("展開したJavaに bin/java がありません")
            shutil.rmtree(str(self.java_dir), ignore_errors=True)
            os.rename(str(source), str(self.java_dir))
            shutil.rmtree(str(staging), ignore_errors=True)
        self._java_version_check()
        self._update_state(java={"version": item["version"], "sha256": item["sha256"],
                                 "dirname": item["dirname"]})
        # Keep the verified archive until the extracted JRE actually works.
        try:
            os.unlink(str(self.download_dir / (item["filename"] + ".verified")))
        except OSError:
            pass
        return "done", "Java %s を確認しました" % item["version"]

    def _s_jd2_fetch(self):
        item = self.manifest["jd2"]
        jd2 = self._state.get("jd2") or {}
        if jd2.get("initialized") and self.jar.is_file():
            return "skipped", "初回起動済みのためスキップしました"
        self.jd2_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.jar.is_file() and components.file_sha256(self.jar) == item["sha256"]:
            self._update_state(jd2={"placed": True, "initialized": False})
            return "skipped", "検証済みのJDownloader.jarがあるためスキップしました"
        archive = self._verified_file("jd2")
        if archive is None:
            components.check_disk_free(self.engine_dir, usage=self._disk_usage)
            self.download_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            archive = self._fetch("jd2")
        os.replace(str(archive), str(self.jar))
        self._update_state(jd2={"placed": True, "initialized": False})
        return "done", "SHA-256とサイズを検証して配置しました"

    def _s_api_config(self):
        action = self.switch_api_config(MODE_NATIVE)
        if action == "same":
            return "skipped", "作成済みです"
        return "done", "127.0.0.1:%d のみで有効化する設定を%sしました" % (
            API_CONFIG["deprecatedapiport"], "作成" if action == "created" else "切り替え")

    def _s_first_run(self):
        if (self._state.get("jd2") or {}).get("initialized") and self._alive():
            return "skipped", "起動済みです"
        adapter = self.adapter
        for run in range(1, MAX_FIRST_RUNS + 1):
            self._step("first_run", "running", "初回起動 %d/%d" % (run, MAX_FIRST_RUNS))
            self._launch_process()
            deadline = self._now() + FIRST_RUN_LIMIT
            outcome = "timeout"
            while self._now() < deadline:
                if not self._alive():
                    outcome = "exited"
                    break
                try:
                    adapter.core_revision()
                    outcome = "ready"
                    break
                except jd2_local.LocalUnavailable:
                    self._sleep(3.0)
            if outcome == "ready":
                updating = False
                try:
                    adapter.run_update_check()
                    self._sleep(5.0)
                    updating = adapter.is_update_available()
                except jd2_local.LocalUnavailable:
                    self.log("[hub] 更新確認に失敗しました。更新なしとして続行します")
                if not updating:
                    jd2 = dict(self._state.get("jd2") or {})
                    jd2["initialized"] = True
                    self._update_state(jd2=jd2)
                    return "done", "%d回目の起動で安定しました" % run
                adapter.restart_and_update()
                while self._now() < deadline and self._alive():
                    self._sleep(2.0)
            if self._alive():
                self._terminate()
            else:
                self._clear_process()
        raise SetupFailure("初回起動・公式アップデートが%d回では完了しませんでした" % MAX_FIRST_RUNS)

    def _s_resident(self):
        if not self._alive():
            self._launch_process()
        self._begin_supervision()
        return "done", "エンジンをHubの監視下に置きました"

    def _s_verify(self):
        revision = self.adapter.core_revision()
        with self._lock:
            self._core_revision = str(revision)
            self._ready = True
        if self.mode == MODE_CONTAINER:
            return "done", "コンテナ（専用ネットワーク・ポート非公開）をinspectで検証 / Core revision %s" % revision
        return "done", "loopback限定・Hub起動プロセスの待受を確認 / Core revision %s" % revision

    def _s_preset(self):
        folder = self.store.settings().get("download_dir")
        if self.mode == MODE_CONTAINER:
            folder = container_runtime.CONTAINER_DOWNLOADS   # JD2 sees only /downloads
        notes = []
        found = self.adapter.set_default_download_folder(folder)
        if not found:
            notes.append("既定の保存先キー(defaultdownloadfolder)が見つからず未設定")
        try:
            folder_keys = self.adapter.package_folder_settings(True)
        except Exception:
            folder_keys = []
        if isinstance(folder_keys, list) and folder_keys:
            fixed = sum(1 for item in folder_keys if item["compliant"])
            notes.append("パッケージ別フォルダ関連の設定 %d件中 %d件がOFF" % (len(folder_keys), fixed))
        preview = jd2_policy.build_preview(self.adapter)
        if preview["applicable"]:
            jd2_policy.apply_preset(self.adapter, preview)
            self._update_state(preset_applied=True)
            notes.append("推奨設定を適用しreadbackで確認")
            return ("done" if found else "warn"), " / ".join(notes)
        self._update_state(preset_applied=False)
        names = ", ".join(preview.get("missing", []))
        notes.append("未特定の設定があるため推奨設定は未適用: " + names)
        return "warn", " / ".join(notes)
