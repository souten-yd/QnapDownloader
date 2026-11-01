"""Hub-managed JDownloader 2 engine: setup, supervised start, stop.

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
import subprocess
import sys
import threading
import time

import durability
import engine_components as components
import jd2_local
import jd2_policy

RING_LINES = 300
MAX_FIRST_RUNS = 6
FIRST_RUN_LIMIT = 600.0
TERM_WAIT = 30.0
KILL_WAIT = 5.0
RESTART_MAX = 5
RESTART_WINDOW = 600.0
BACKOFF = (5.0, 15.0, 30.0, 60.0, 60.0)
READY_TIMEOUT = 180.0
API_CONFIG = {"deprecatedapienabled": True, "deprecatedapiport": 3128,
              "deprecatedapilocalhostonly": True}
API_CONFIG_NAME = "org.jdownloader.api.RemoteAPIConfig.json"

STEP_DEFS = (
    ("precheck", "事前確認"),
    ("java_fetch", "Javaの取得と検証"),
    ("java_extract", "Javaの展開と動作確認"),
    ("jd2_fetch", "JD2の取得・検証・配置"),
    ("api_config", "ローカル接続設定の作成"),
    ("first_run", "初回起動・公式アップデート"),
    ("resident", "常駐起動"),
    ("verify", "ローカル接続の安全確認"),
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
            "preset_applied": False, "process": None}


class EngineManager:
    def __init__(self, data_dir, store, *, manifest=None, opener=None,
                 popen=subprocess.Popen, run=subprocess.run, kill=os.kill,
                 proc_root="/proc", api_port=jd2_local.API_PORT,
                 meminfo_path="/proc/meminfo", sleep=time.sleep,
                 monotonic=time.monotonic, machine=None, disk_usage=shutil.disk_usage):
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
        self.adapter = jd2_local.LocalAdapter(self)
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

    def _step(self, sid, status, detail=None, progress=None):
        with self._lock:
            for item in self._steps:
                if item["id"] == sid:
                    item["status"] = status
                    if detail is not None:
                        item["detail"] = detail
                    item["progress"] = progress
                    return

    def _reset_steps(self):
        with self._lock:
            for item in self._steps:
                item.update(status="pending", detail="", progress=None)

    def phase(self):
        with self._lock:
            if self._setup_active:
                return "setup"
            if self._pid is not None and self._alive():
                return "running" if self._ready else "starting"
            if self._error:
                return "failed"
            return "stopped" if self.installed else "not_installed"

    def status(self):
        with self._lock:
            java = self._state.get("java") or {}
            comps = []
            for name in ("java", "jd2"):
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
            phase = self.phase()
            return {
                "phase": phase,
                "installed": self.installed,
                "running": phase in ("running", "starting"),
                "ready": phase == "running",
                "setup_running": self._setup_active,
                "autostart": self.autostart,
                "preset_applied": self.preset_applied,
                "steps": [dict(step) for step in self._steps],
                "error": self._error,
                "components": comps,
                "heap_mb": self._heap_mb,
                "core_revision": self._core_revision,
                "restart_count": self._restart_count,
            }

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
            return False
        if popen is not None and popen.poll() is not None:
            return False
        return self._identity_ok(pid, ticks)

    def is_ours_running(self):
        return self._alive()

    def connection_target(self):
        with self._lock:
            pid, ticks = self._pid, self._ticks
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
            return False
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
            if not self._ready:
                try:
                    revision = self.adapter.core_revision()
                    with self._lock:
                        self._ready = True
                        self._core_revision = str(revision)
                except jd2_local.LocalUnavailable:
                    if self._now() - started > READY_TIMEOUT and not self._error:
                        self._error = "エンジンの起動確認がタイムアウトしました。RAMログを確認してください"

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
    def start_setup(self):
        with self._lock:
            if self._setup_active:
                raise SetupBusy("初期設定はすでに実行中です")
            if self._alive():
                raise EngineError("エンジンを停止してから再実行してください")
            self._setup_active = True
        threading.Thread(target=self._setup_thread, daemon=True).start()

    def run_setup(self):
        """Synchronous variant (tests); same guard as start_setup."""
        with self._lock:
            if self._setup_active:
                raise SetupBusy("初期設定はすでに実行中です")
            if self._alive():
                raise EngineError("エンジンを停止してから再実行してください")
            self._setup_active = True
        self._setup_thread()

    def _setup_thread(self):
        try:
            self._execute_setup()
        finally:
            with self._lock:
                self._setup_active = False

    def _execute_setup(self):
        self._reset_steps()
        self._error = None
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
                self._update_state(setup_complete=True)
            else:
                # First completion enables autostart (can be switched off).
                self._update_state(setup_complete=True, autostart=True)
            self._step("done", "done", "初期設定が完了しました")
        except Exception as error:
            if isinstance(error, (SetupFailure, components.DownloadError,
                                  components.ExtractError, jd2_local.LocalUnavailable,
                                  jd2_policy.PresetUnavailable, jd2_policy.PresetChanged)):
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

    def _s_precheck(self):
        if not sys.platform.startswith("linux") or self._machine_name().lower() not in ("x86_64", "amd64"):
            raise SetupFailure("x86_64 Linux以外では初期設定を開始できません")
        folder = self.store.settings().get("download_dir")
        if not folder or not os.path.isdir(folder):
            raise SetupFailure("先にダウンロード保存先フォルダを選択してください")
        if not os.access(folder, os.W_OK):
            raise SetupFailure("保存先フォルダに書き込み権限がありません")
        self.engine_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        components.check_disk_free(self.engine_dir, usage=self._disk_usage)
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
        path = self.jd2_dir / "cfg" / API_CONFIG_NAME
        if path.exists() or path.is_symlink():
            try:
                same = (not path.is_symlink() and json.loads(path.read_text(encoding="utf-8")) == API_CONFIG)
            except (OSError, ValueError):
                same = False
            if same:
                return "skipped", "作成済みです"
            raise SetupFailure("既存の接続設定ファイルがあるため上書きせず中止しました: cfg/" + API_CONFIG_NAME)
        durability.write_json(path, API_CONFIG)
        return "done", "127.0.0.1:%d のみで有効化する設定を作成しました" % API_CONFIG["deprecatedapiport"]

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
        return "done", "loopback限定・Hub起動プロセスの待受を確認 / Core revision %s" % revision

    def _s_preset(self):
        folder = self.store.settings().get("download_dir")
        notes = []
        found = self.adapter.set_default_download_folder(folder)
        if not found:
            notes.append("既定の保存先キー(defaultdownloadfolder)が見つからず未設定")
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
