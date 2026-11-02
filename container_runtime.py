"""Fixed-function Docker CLI wrapper for Container Station (docs/container-runtime.md).

The Hub never passes arbitrary arguments, images or commands to Docker.
Only the constants below can be pulled, only the Hub's own labelled
containers/networks can be inspected, stopped or removed, and every call is
a list-argument subprocess (no shell) with a timeout and a minimal
environment. Other containers (including an existing Docker JD2) are never
listed or touched. Real Container Station behaviour is unverified.

Stdlib only; Python 3.8 compatible. The command runner is injectable.
"""
import hashlib
import ipaddress
import json
import os
import re
import subprocess
import time
from types import SimpleNamespace

import debuglog
import ttlcache

IMAGE_JD2 = ("eclipse-temurin:21-jre@sha256:"
             "0b99b3376fbb50dbf9c0af8535fdd5b8eb0d3fdfc8b1e4967f383b6919f6901a")
IMAGE_DEBIAN = ("debian:bookworm-slim@sha256:"
                "7ff6ba31527b9e336162fa061746c366c4b2032f4d2cff2078bea43f78efe3f8")
PINNED_IMAGES = frozenset((IMAGE_JD2, IMAGE_DEBIAN))

LABEL_KEY = "org.qnapdownloader.managed"
LABEL_VALUE = "1"
LABEL = LABEL_KEY + "=" + LABEL_VALUE
NAME_PREFIX = "qnapdl-"
JD2_NAME = "qnapdl-jd2"
JD2_NET = "qnapdl-jd2-net"
BROWSER_NAME = "qnapdl-browser"
BROWSER_NET = "qnapdl-browser-net"
BROWSER_REPO = "qnapdl-browser"
ALLOWED_NAMES = frozenset((JD2_NAME, BROWSER_NAME))
ALLOWED_NETWORKS = frozenset((JD2_NET, BROWSER_NET))
NAME_NETWORK = {JD2_NAME: JD2_NET, BROWSER_NAME: BROWSER_NET}

GETCFG = "/sbin/getcfg"
QPKG_CONF = "/etc/config/qpkg.conf"
FALLBACK_DOCKER = "/usr/local/bin/docker"

JD2_MEMORY = "1g"
JD2_PIDS = "512"
JD2_STOP_GRACE = 30
JD2_HEAP = "-Xmx512m"
MIN_AVAILABLE_KIB = int(1.5 * 1024 * 1024)   # MemAvailable >= 1.5 GiB to start JD2
CONTAINER_DOWNLOADS = "/downloads"
CONTAINER_JD2 = "/jd2"
DEFAULT_UID = 1000
DEFAULT_GID = 1000

# Remote browser container (docs/container-runtime.md section 4).
BROWSER_MEMORY = "2g"
BROWSER_SHM = "512m"
BROWSER_PIDS = "1024"
BROWSER_STOP_GRACE = 5
BROWSER_CDP_PORT = 9223
BROWSER_UID = 1000
BROWSER_MIN_AVAILABLE_KIB = 2 * 1024 * 1024   # MemAvailable >= 2 GiB to open a tab
BROWSER_CONTEXT_FILES = frozenset(("Dockerfile", "entrypoint.sh"))
BROWSER_EXIT_XVFB_TIMEOUT = 70  # entrypoint exit code: Xvfb socket never appeared
BROWSER_EXIT_XVFB = 71         # entrypoint exit code: Xvfb died
BROWSER_EXIT_CHROMIUM = 72     # entrypoint exit code: Chromium died
BROWSER_EXIT_SOCAT = 73        # entrypoint exit code: socat relay died
BROWSER_EXIT_NO_CHROOT = 77    # entrypoint exit code: chroot unavailable in a user namespace
BROWSER_EXIT_NO_USERNS = 78    # entrypoint exit code: user namespaces unavailable
BROWSER_EXIT_CAPS = 79         # entrypoint exit code: process has effective capabilities
BROWSER_TAG = re.compile(r"qnapdl-browser:[0-9a-f]{12}\Z")
BROWSER_BUILD_TIMEOUT = 3600
BROWSER_LOG_LINES = 80          # `docker logs --tail` after an early exit
BROWSER_LOG_CHARS = 2000        # sanitized tail kept for the RAM log / message
BROWSER_EXIT_MESSAGES = {
    BROWSER_EXIT_XVFB_TIMEOUT: "画面サーバーの起動待ちが時間切れになりました",
    BROWSER_EXIT_XVFB: "画面サーバー(Xvfb)が終了しました",
    BROWSER_EXIT_CHROMIUM: "Chromiumが終了しました",
    BROWSER_EXIT_SOCAT: "中継(socat)が終了しました",
}
_CONTROL_CHARS = re.compile(r"[\x00-\x09\x0b-\x1f\x7f-\x9f]")

MAX_OUTPUT = 1024 * 1024
_BAD_PATH_CHARS = set(',"\'\n\r\0')


_NOT_FOUND_RE = re.compile(r"\bnot found\b")


def _not_found(result):
    """True when a failed docker call means the object does not exist.

    Docker variants word it differently ("No such network", Container Station's
    "network X not found"); anything else (permission denied, daemon down) is a
    real error and must not be treated as absence.
    """
    if result.returncode == 0:
        return False
    text = ((result.stderr or "") + " " + (result.stdout or "")).lower()
    return "no such" in text or bool(_NOT_FOUND_RE.search(text))


class ContainerError(RuntimeError):
    """Container Station unavailable, unsafe target, or a Docker failure."""


def _default_runner(argv, timeout, env):
    try:
        done = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=timeout, env=env,
                              shell=False, check=False)
    except subprocess.TimeoutExpired:
        raise ContainerError("Dockerの応答がタイムアウトしました") from None
    except OSError as error:
        raise ContainerError("Dockerを実行できません: %s" % type(error).__name__) from None
    return SimpleNamespace(
        returncode=done.returncode,
        stdout=done.stdout[:MAX_OUTPUT].decode("utf-8", "replace"),
        stderr=done.stderr[:MAX_OUTPUT].decode("utf-8", "replace"))


def read_mem_available_kib(path="/proc/meminfo"):
    try:
        with open(path, "r", encoding="ascii", errors="replace") as handle:
            for line in handle:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


def check_memory(path="/proc/meminfo", need_kib=MIN_AVAILABLE_KIB):
    available = read_mem_available_kib(path)
    if available is None:
        raise ContainerError("空きメモリ(MemAvailable)を確認できないためコンテナを起動しません")
    if available < need_kib:
        raise ContainerError(
            "空きメモリが不足しています（MemAvailable %d MiB < 必要 %d MiB）。"
            "他のアプリを止めずに起動を中止しました" % (available // 1024, need_kib // 1024))
    return available


def _safe_path(path, what):
    if (not isinstance(path, str) or not path.startswith("/") or
            any(ch in _BAD_PATH_CHARS for ch in path) or ".." in path.split("/")):
        raise ContainerError("%sのパスがコンテナへ渡せない形式です" % what)
    return path


def writable_by(stat_result, uid, gid):
    """Mode-bit check only (ACLs are not evaluated)."""
    mode = stat_result.st_mode
    if uid == 0:
        return True
    if stat_result.st_uid == uid:
        return bool(mode & 0o200) and bool(mode & 0o100)
    if stat_result.st_gid == gid:
        return bool(mode & 0o020) and bool(mode & 0o010)
    return bool(mode & 0o002) and bool(mode & 0o001)


def prepare_data_owner(jd2_dir, *, euid=None, stat=os.stat, lstat=os.lstat, lchown=os.lchown, walk=os.walk):
    """Return (uid, gid) the container runs as: the owner of engine/jd2.

    When the Hub is root and the directory is root-owned (the native engine
    created it), hand the tree to 1000:1000 so the container never runs as
    root. Symlinks are never followed. Only engine/jd2 is touched.
    """
    euid = os.geteuid() if euid is None else euid
    try:
        info = stat(jd2_dir)
    except OSError:
        raise ContainerError("JD2のデータフォルダを確認できません") from None
    uid, gid = info.st_uid, info.st_gid
    if uid == 0 or gid == 0:
        if euid != 0:
            raise ContainerError("JD2のデータフォルダがrootの所有で、Hubにも所有者を変更する権限がありません")
        uid, gid = DEFAULT_UID, DEFAULT_GID
        for base, dirs, files in walk(jd2_dir, followlinks=False):
            for target in [base] + [os.path.join(base, n) for n in list(dirs) + list(files)]:
                try:
                    current = lstat(target)
                    if current.st_uid != uid or current.st_gid != gid:
                        lchown(target, uid, gid)
                except OSError:
                    raise ContainerError("JD2のデータフォルダの所有者を変更できません") from None
    return uid, gid


class Runtime:
    def __init__(self, runner=None, *, isfile=os.path.isfile, access=os.access,
                 meminfo_path="/proc/meminfo", config_dir=None, stat=os.stat):
        self._runner = runner if runner is not None else _default_runner
        self._isfile = isfile
        self._access = access
        self._meminfo = meminfo_path
        self._config_dir = config_dir
        self._stat = stat
        self._flights = ttlcache.SingleFlightCache()   # concurrent status reads share one docker call
        self._docker_path = False   # False = not yet looked up

    # ---------------------------------------------------------- docker CLI
    def _env(self, docker=None):
        path = "/usr/bin:/bin:/usr/sbin:/sbin"
        if docker:
            path = os.path.dirname(docker) + ":" + path
        env = {"PATH": path, "HOME": "/tmp", "LANG": "C.UTF-8"}
        if self._config_dir:
            env["DOCKER_CONFIG"] = str(self._config_dir)
        return env

    def locate(self, refresh=False):
        """Absolute path of the docker CLI or None (QTS getcfg, then fallback)."""
        if self._docker_path is not False and not refresh:
            return self._docker_path
        found = None
        candidates = []
        try:
            result = self._runner([GETCFG, "container-station", "Install_Path",
                                   "-f", QPKG_CONF], 5, self._env())
            text = (result.stdout or "").strip()
            if (result.returncode == 0 and text.startswith("/") and "\n" not in text and
                    ".." not in text.split("/") and "\0" not in text):
                candidates.append(text.rstrip("/") + "/bin/docker")
        except ContainerError:
            pass
        candidates.append(FALLBACK_DOCKER)
        for candidate in candidates:
            if self._isfile(candidate) and self._access(candidate, os.X_OK):
                found = candidate
                break
        self._docker_path = found
        return found

    def _docker(self, args, timeout=30):
        docker = self.locate()
        if docker is None:
            raise ContainerError("Container Stationが見つかりません")
        if not debuglog.LOG._enabled:
            return self._runner([docker] + list(args), timeout, self._env(docker))
        started = time.monotonic()
        try:
            result = self._runner([docker] + list(args), timeout, self._env(docker))
        except Exception as error:
            debuglog.log("engine", "error", "docker %s 失敗" % " ".join(map(str, args[:2])),
                         {"ms": int((time.monotonic() - started) * 1000), "error": type(error).__name__})
            raise
        debuglog.log("engine", "debug" if result.returncode == 0 else "warn",
                     "docker %s" % " ".join(map(str, args[:2])),
                     {"rc": result.returncode, "ms": int((time.monotonic() - started) * 1000)})
        return result

    def _tail(self, result):
        """Sanitized Docker error text (first 300 chars) for the RAM log and step error.

        Whitespace is collapsed and any environment value we passed is redacted.
        """
        text = " ".join(((result.stderr or "") + " " + (result.stdout or "")).split())
        for value in self._env().values():
            if len(value) >= 4:
                text = text.replace(value, "***")
        return text[:300] or "不明なエラー"

    def available(self):
        return self.version() is not None

    def version(self):
        if self.locate() is None:
            return None
        try:
            result = self._docker(["version", "--format", "{{.Server.Version}}"], 10)
        except ContainerError:
            return None
        text = (result.stdout or "").strip()
        if result.returncode != 0 or not text or "\n" in text or len(text) > 40:
            return None
        return text

    # -------------------------------------------------------------- images
    @staticmethod
    def _check_image(ref):
        if ref not in PINNED_IMAGES:
            raise ContainerError("固定されたイメージ以外は扱えません")

    def image_present(self, ref):
        self._check_image(ref)
        return self._docker(["image", "inspect", "--format", "{{.Id}}", ref], 20).returncode == 0

    def pull(self, ref):
        self._check_image(ref)
        result = self._docker(["pull", ref], 3600)
        if result.returncode != 0:
            raise ContainerError("イメージを取得できません: " + self._tail(result))
        if not self.image_present(ref):
            raise ContainerError("取得したイメージを確認できません")
        return True

    # ------------------------------------------------------------ networks
    def network_ensure(self, name):
        if name not in ALLOWED_NETWORKS:
            raise ContainerError("許可されていないネットワーク名です")
        result = self._docker(["network", "inspect", name], 20)
        if result.returncode == 0:
            try:
                info = json.loads(result.stdout)[0]
                labels = info.get("Labels") or {}
                ok = (info.get("Driver") == "bridge" and labels.get(LABEL_KEY) == LABEL_VALUE and
                      not info.get("Internal"))
            except (ValueError, IndexError, AttributeError, TypeError):
                ok = False
            if not ok:
                raise ContainerError("同名のネットワークがHub管理外のため使用しません: " + name)
            return False
        if not _not_found(result):
            raise ContainerError("ネットワークを確認できません: " + self._tail(result))
        created = self._docker(["network", "create", "--driver", "bridge",
                                "--label", LABEL, name], 30)
        if created.returncode != 0:
            raise ContainerError("ネットワークを作成できません: " + self._tail(created))
        return True

    # ---------------------------------------------------------- containers
    @staticmethod
    def _check_name(name):
        if name not in ALLOWED_NAMES or not name.startswith(NAME_PREFIX):
            raise ContainerError("許可されていないコンテナ名です")

    def _inspect(self, name):
        """Parsed facts about our named container, or None if it does not exist."""
        self._check_name(name)
        result = self._docker(["container", "inspect", name], 20)
        if result.returncode != 0:
            if _not_found(result):
                return None
            raise ContainerError("コンテナを確認できません: " + self._tail(result))
        try:
            data = json.loads(result.stdout)
            item = data[0]
            if item.get("Name") != "/" + name:
                raise ValueError("name")
            labels = (item.get("Config") or {}).get("Labels") or {}
            state = item.get("State") or {}
            settings = item.get("NetworkSettings") or {}
            return {
                "exists": True,
                "managed": labels.get(LABEL_KEY) == LABEL_VALUE,
                "status": str(state.get("Status") or "unknown"),
                "running": state.get("Running") is True,
                "networks": settings.get("Networks") or {},
                "published": bool((item.get("HostConfig") or {}).get("PortBindings")),
                "exit_code": state.get("ExitCode") if isinstance(state.get("ExitCode"), int) else None,
                "id": item.get("Id") if isinstance(item.get("Id"), str) else "",
                "pid": state.get("Pid") if isinstance(state.get("Pid"), int) else 0,
                # v0.1.38: why it stopped (watchdog reason text); short, display-only.
                "oom": state.get("OOMKilled") is True,
                "error": _clip_text(state.get("Error")),
                "finished_at": _clip_text(state.get("FinishedAt"), 40),
            }
        except (ValueError, IndexError, AttributeError, TypeError):
            raise ContainerError("コンテナ情報を解析できません") from None

    def container_state(self, name):
        # Status reads only (never used to authorise an action): callers that overlap in time
        # share one `docker inspect`. _require_ours / inspect_ip always inspect for themselves.
        info = self._flights.get("inspect:" + str(name), 0.0, lambda: self._inspect(name))
        if info is None:
            return {"exists": False, "managed": False, "running": False, "status": "absent"}
        return {"exists": True, "managed": info["managed"], "running": info["running"],
                "status": info["status"], "exit_code": info["exit_code"], "oom": info["oom"],
                "error": info["error"], "finished_at": info["finished_at"]}

    def _require_ours(self, name):
        info = self._inspect(name)
        if info is not None and not info["managed"]:
            raise ContainerError("Hub管理外のコンテナのため操作しません: " + name)
        return info

    def inspect_ip(self, name, network):
        """IP of our running container on our network; verified on every call."""
        if network not in ALLOWED_NETWORKS or NAME_NETWORK.get(name) != network:
            raise ContainerError("許可されていないネットワーク名です")
        info = self._inspect(name)
        if info is None or not info["running"]:
            raise ContainerError("コンテナが起動していません")
        if not info["managed"]:
            raise ContainerError("Hub管理外のコンテナには接続しません")
        if info["published"]:
            raise ContainerError("コンテナのポートが公開されているため接続しません")
        if list(info["networks"]) != [network]:
            raise ContainerError("コンテナが想定外のネットワークに接続されています")
        try:
            ip = ipaddress.ip_address(str(info["networks"][network].get("IPAddress")))
        except (ValueError, AttributeError):
            raise ContainerError("コンテナのIPアドレスを取得できません") from None
        if ip.version != 4 or not ip.is_private or ip.is_loopback or ip.is_unspecified:
            raise ContainerError("コンテナのIPアドレスが想定外です")
        return str(ip)

    def jd2_run_args(self, jd2_dir, download_dir, uid, gid):
        """Exact `docker run` argument list for the JD2 container (no -p)."""
        _safe_path(jd2_dir, "JD2データ")
        _safe_path(download_dir, "保存先")
        if not (isinstance(uid, int) and isinstance(gid, int) and uid > 0 and gid > 0):
            raise ContainerError("コンテナをrootで実行することはできません")
        return [
            "run", "-d",
            "--name", JD2_NAME,
            "--label", LABEL,
            "--network", JD2_NET,
            "--restart", "no",
            "--read-only",
            "--log-driver", "none",
            "--memory", JD2_MEMORY, "--memory-swap", JD2_MEMORY,
            "--pids-limit", JD2_PIDS,
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--user", "%d:%d" % (uid, gid),
            "--workdir", CONTAINER_JD2,
            "--env", "HOME=" + CONTAINER_JD2,
            "--env", "LANG=C.UTF-8",
            "--mount", "type=bind,source=%s,target=%s" % (jd2_dir, CONTAINER_JD2),
            "--mount", "type=bind,source=%s,target=%s" % (download_dir, CONTAINER_DOWNLOADS),
            "--tmpfs", CONTAINER_JD2 + "/logs:size=64m",
            "--tmpfs", "/tmp:rw,exec,nosuid,nodev,size=64m",
            IMAGE_JD2,
            "java", JD2_HEAP, "-XX:+UseSerialGC", "-Djava.awt.headless=true",
            "-jar", CONTAINER_JD2 + "/JDownloader.jar", "-norestart",
        ]

    def run_jd2(self, jd2_dir, download_dir, uid, gid):
        args = self.jd2_run_args(jd2_dir, download_dir, uid, gid)
        try:
            for path, label in ((jd2_dir, "JD2データ"), (download_dir, "保存先")):
                if not os.path.isdir(path):
                    raise ContainerError("%sフォルダが存在しません" % label)
            if not writable_by(self._stat(download_dir), uid, gid):
                raise ContainerError(
                    "保存先フォルダにコンテナ利用者(uid %d)の書き込み権限がありません。"
                    "共有フォルダの権限を確認してください" % uid)
        except OSError:
            raise ContainerError("フォルダを確認できません") from None
        if self._inspect(JD2_NAME) is not None:
            raise ContainerError("同名のコンテナが残っています。停止してから起動してください")
        if not self.image_present(IMAGE_JD2):
            raise ContainerError("JD2のイメージがありません。初期設定を実行してください")
        check_memory(self._meminfo)
        result = self._docker(args, 120)
        if result.returncode != 0:
            raise ContainerError("コンテナを起動できません: " + self._tail(result))
        return (result.stdout or "").strip()[:80]

    def stop(self, name, grace=JD2_STOP_GRACE):
        info = self._require_ours(name)
        if info is None:
            return False
        if not info["running"]:
            return False
        result = self._docker(["stop", "-t", str(int(grace)), name], int(grace) + 30)
        if result.returncode != 0:
            raise ContainerError("コンテナを停止できません: " + self._tail(result))
        return True

    def remove(self, name):
        if not name.startswith(NAME_PREFIX):
            raise ContainerError("許可されていないコンテナ名です")
        info = self._require_ours(name)
        if info is None:
            return False
        result = self._docker(["rm", "-f", name], 60)
        if result.returncode != 0:
            raise ContainerError("コンテナを削除できません: " + self._tail(result))
        return True

    # ------------------------------------------------- remote browser image
    @staticmethod
    def browser_context_files(context_dir):
        """(Dockerfile bytes, entrypoint bytes) of the shipped build context.

        The directory must hold exactly the shipped files: nothing else can
        reach `docker build`, and the context is never a client-supplied path.
        """
        try:
            names = set(os.listdir(context_dir))
            if names != set(BROWSER_CONTEXT_FILES):
                raise ContainerError("ブラウザイメージのビルド用ファイルが想定と異なります")
            data = []
            for name in ("Dockerfile", "entrypoint.sh"):
                path = os.path.join(context_dir, name)
                if os.path.islink(path) or not os.path.isfile(path):
                    raise ContainerError("ブラウザイメージのビルド用ファイルが不正です")
                with open(path, "rb") as handle:
                    data.append(handle.read(1024 * 1024))
            return data[0], data[1]
        except OSError:
            raise ContainerError("ブラウザイメージのビルド用ファイルを読めません") from None

    def browser_tag(self, context_dir):
        dockerfile, entrypoint = self.browser_context_files(context_dir)
        digest = hashlib.sha256(dockerfile + entrypoint).hexdigest()[:12]
        return BROWSER_REPO + ":" + digest

    @staticmethod
    def _check_browser_tag(tag):
        if not isinstance(tag, str) or not BROWSER_TAG.match(tag):
            raise ContainerError("許可されていないブラウザイメージです")

    def browser_image_present(self, tag):
        self._check_browser_tag(tag)
        return self._docker(["image", "inspect", "--format", "{{.Id}}", tag], 20).returncode == 0

    def browser_image_tags(self):
        """Hub-built browser image tags present locally (label + repository checked)."""
        result = self._docker(["images", "--filter", "label=" + LABEL,
                               "--filter", "reference=" + BROWSER_REPO + ":*",
                               "--format", "{{.Repository}}:{{.Tag}}"], 30)
        if result.returncode != 0:
            return []
        return [line for line in (result.stdout or "").split() if BROWSER_TAG.match(line)]

    def browser_build_args(self, tag, context_dir):
        self._check_browser_tag(tag)
        _safe_path(context_dir, "ビルド用フォルダ")
        return ["build", "--label", LABEL, "--tag", tag, context_dir]

    def browser_build(self, context_dir):
        tag = self.browser_tag(context_dir)
        args = self.browser_build_args(tag, context_dir)
        result = self._docker(args, BROWSER_BUILD_TIMEOUT)
        if result.returncode != 0:
            raise ContainerError("ブラウザイメージを作成できません: " + self._tail(result))
        if not self.browser_image_present(tag):
            raise ContainerError("作成したイメージを確認できません")
        return tag

    def browser_prune_old(self, keep_tag):
        """Remove older Hub-built browser images (label + repository checked)."""
        self._check_browser_tag(keep_tag)
        result = self._docker(["images", "--filter", "label=" + LABEL,
                               "--filter", "reference=" + BROWSER_REPO + ":*",
                               "--format", "{{.Repository}}:{{.Tag}}"], 30)
        removed = 0
        if result.returncode != 0:
            return removed
        for line in (result.stdout or "").split():
            if BROWSER_TAG.match(line) and line != keep_tag:
                if self._docker(["rmi", line], 60).returncode == 0:
                    removed += 1
        return removed

    @staticmethod
    def _browser_hardening(log_args=("--log-driver", "none")):
        """Shared hardening for the browser container and its selftest.

        WHY `--cap-drop ALL` IS FOLLOWED BY `--cap-add SYS_ADMIN` (browser only,
        never JD2): Chromium's sandbox needs unprivileged user namespaces
        (clone/unshare with CLONE_NEWUSER etc.). Docker's default seccomp
        profile allows those namespace flags only when the container's
        capability set includes CAP_SYS_ADMIN; the rule is evaluated from the
        capability set given at container creation. Without it the sandbox
        cannot start and the only "fix" would be switching the sandbox off, which is
        forbidden here.

        Why this does not give the browser real privileges: the process runs as
        uid 1000 (--user) with no-new-privileges. For a non-root user Docker
        puts SYS_ADMIN into the *bounding* set only; the effective, permitted
        and ambient sets stay empty, and with no-new-privileges (and no setuid
        or file-capability binary able to raise them) they cannot be raised
        later. The process can therefore only create *unprivileged* user
        namespaces, which is exactly what Chromium's namespace sandbox uses.
        WHY `--cap-add SYS_CHROOT` TOO: the namespace sandbox also chroots
        (to /proc/self/fdinfo/) inside its own user namespace, and Docker's
        default seccomp allows chroot only when the container's capability set
        includes CAP_SYS_CHROOT. Without it Chromium dies with
        `Check failed: sys_chroot("/proc/self/fdinfo/") == 0`. Like SYS_ADMIN
        it lands in the bounding set only for uid 1000 + no-new-privileges;
        the effective set stays empty (CapEff guard below). The entrypoint
        selftest probes unshare + chroot (exit 77 if chroot is refused).
        Everything else stays dropped (ALL minus SYS_ADMIN/SYS_CHROOT), the default
        seccomp profile stays on, and the root filesystem is read-only.

        entrypoint.sh enforces the assumption at runtime: it exits 79 if
        CapEff in /proc/self/status is not all zero, so Chromium never runs
        with real capabilities even if a Docker version behaves differently.
        Residual risk: SYS_ADMIN in the bounding set widens what a process
        could do if it ever obtained capabilities (for example through a kernel
        bug), compared with an empty set. See docs/container-runtime.md.
        """
        return [
            "--read-only",
        ] + list(log_args) + [
            "--memory", BROWSER_MEMORY, "--memory-swap", BROWSER_MEMORY,
            "--shm-size", BROWSER_SHM,
            "--pids-limit", BROWSER_PIDS,
            "--cap-drop", "ALL",
            "--cap-add", "SYS_ADMIN",
            "--cap-add", "SYS_CHROOT",
            "--security-opt", "no-new-privileges",
            "--user", "%d:%d" % (BROWSER_UID, BROWSER_UID),
            "--tmpfs", "/profile:rw,nosuid,nodev,size=512m,uid=%d,gid=%d,mode=0700" %
            (BROWSER_UID, BROWSER_UID),
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=256m,mode=1777",
        ]

    def browser_run_args(self, tag):
        """Exact `docker run` list: no -p, no mounts, default seccomp."""
        self._check_browser_tag(tag)
        # Minimal disk writes: Chromium/Xvfb output goes to tmpfs files inside
        # the container, so normal operation logs nothing. Only a dying
        # entrypoint prints the tail of those files. `local` with a 256k cap and
        # one file bounds that to a tiny, rotated file (JD2 keeps `none`), and
        # lets the Hub read the failure reason via `docker logs` before removal.
        # The local driver compresses rotated files by default, which Docker
        # rejects together with max-file=1 ("compression cannot be enabled when
        # max file count is 1"), so compression is turned off explicitly.
        log_args = ("--log-driver", "local", "--log-opt", "max-size=256k",
                    "--log-opt", "max-file=1",
                    "--log-opt", "compress=false")
        return (["run", "-d", "--name", BROWSER_NAME, "--label", LABEL,
                 "--network", BROWSER_NET, "--restart", "no"] +
                self._browser_hardening(log_args) + [tag])

    def browser_selftest_args(self, tag):
        self._check_browser_tag(tag)
        return ["run", "--rm", "--label", LABEL, "--network", "none"] + \
            self._browser_hardening() + [tag, "selftest"]

    def browser_selftest(self, tag):
        """Can Chromium's sandbox start here? Raises a clear error if not."""
        result = self._docker(self.browser_selftest_args(tag), 90)
        if result.returncode == BROWSER_EXIT_NO_CHROOT:
            raise ContainerError(NO_CHROOT_MESSAGE)
        if result.returncode == BROWSER_EXIT_NO_USERNS:
            raise ContainerError(NO_USERNS_MESSAGE)
        if result.returncode == BROWSER_EXIT_CAPS:
            raise ContainerError(CAPS_MESSAGE)
        if result.returncode != 0:
            raise ContainerError("サンドボックスの確認に失敗しました: " + self._tail(result))
        return True

    def run_browser(self, tag):
        args = self.browser_run_args(tag)
        if self._inspect(BROWSER_NAME) is not None:
            raise ContainerError("同名のコンテナが残っています。停止してから起動してください")
        if not self.browser_image_present(tag):
            raise ContainerError("ブラウザイメージがありません。初期設定を実行してください")
        check_memory(self._meminfo, BROWSER_MIN_AVAILABLE_KIB)
        result = self._docker(args, 120)
        if result.returncode != 0:
            message = "コンテナを起動できません: " + self._tail(result)
            # A failed `docker run -d` can leave a container in "created" state;
            # remove it (label-checked) so the next attempt has no name conflict.
            try:
                self.remove(BROWSER_NAME)
            except ContainerError:
                pass
            raise ContainerError(message)
        return (result.stdout or "").strip()[:80]

    def logs_tail(self, name):
        """Sanitized last lines of our own labelled browser container's logs.

        Fixed operation: only the browser container name, only when the
        container carries the Hub label. Control characters are removed and the
        result is capped at BROWSER_LOG_CHARS (the end is kept).
        """
        if name != BROWSER_NAME:
            raise ContainerError("許可されていないコンテナ名です")
        info = self._inspect(name)
        if info is None:
            raise ContainerError("コンテナがありません")
        if not info["managed"]:
            raise ContainerError("Hub管理外のコンテナのログは読みません")
        result = self._docker(["logs", "--tail", str(BROWSER_LOG_LINES), name], 20)
        if result.returncode != 0:
            raise ContainerError("コンテナのログを取得できません")
        text = (result.stdout or "") + (result.stderr or "")
        text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
        text = _CONTROL_CHARS.sub("", text).strip()
        return text[-BROWSER_LOG_CHARS:]

    def stats(self, name):
        """Memory use of our own named container: {used_bytes, limit_bytes, percent}."""
        self._check_name(name)
        return self._flights.get("stats:" + name, 0.0, lambda: self._stats_once(name))

    def container_ref(self, name):
        """{id, pid, running} of our own container (for cgroup lookups), or None if absent."""
        self._check_name(name)
        info = self._flights.get("ref:" + name, 5.0, lambda: self._inspect(name))
        if info is None or not info.get("managed"):
            return None
        return {"id": info.get("id", ""), "pid": info.get("pid", 0), "running": info["running"]}

    def block_io(self, name):
        """(read_bytes, write_bytes) from `docker stats --no-stream` BlockIO (fallback of diskio)."""
        self._check_name(name)
        return self._flights.get("blockio:" + name, 5.0, lambda: self._block_io_once(name))

    def _block_io_once(self, name):
        result = self._docker(["stats", "--no-stream", "--format", "{{.BlockIO}}", name], 30)
        if result.returncode != 0:
            raise ContainerError("コンテナのディスクI/Oを取得できません")
        return parse_block_io((result.stdout or "").strip())

    def _stats_once(self, name):
        result = self._docker(["stats", "--no-stream", "--format",
                               "{{.MemUsage}}|{{.MemPerc}}", name], 30)
        if result.returncode != 0:
            raise ContainerError("コンテナのメモリ使用量を取得できません")
        return parse_stats((result.stdout or "").strip())


def _clip_text(value, limit=120):
    """Short single-line display text from a docker State field ('' when absent)."""
    if not isinstance(value, str):
        return ""
    return " ".join("".join(c if c.isprintable() else " " for c in value).split())[:limit]


NO_USERNS_MESSAGE = (
    "このNASのカーネルまたはContainer Stationではユーザー名前空間が使えず、Chromiumのサンドボックスを"
    "有効にできません。安全のためサンドボックスを無効にする方法へは切り替えません。"
    "カーネル設定（ユーザー名前空間）またはContainer Stationのseccomp設定を確認してください")
NO_CHROOT_MESSAGE = (
    "サンドボックスに必要なchrootが許可されていません。Container StationのDocker設定（seccomp/capability）を"
    "確認してください。安全のためサンドボックスを無効にする方法へは切り替えません")
CAPS_MESSAGE = (
    "ブラウザのプロセスが実効的な権限(capability)を持っていたため、安全のため起動を中止しました"
    "（CapEffが0であることを起動時に確認しています）。Container Station/Dockerの権限の扱いを確認してください。"
    "ユーザー名前空間が使えない場合も同様に中止します")

_UNITS = {"b": 1, "kb": 1000, "mb": 1000 ** 2, "gb": 1000 ** 3, "tb": 1000 ** 4, "kib": 1024,
          "mib": 1024 ** 2, "gib": 1024 ** 3, "tib": 1024 ** 4}


def _bytes(text):
    match = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z]+)\s*", text)
    if not match or match.group(2).lower() not in _UNITS:
        raise ValueError("size")
    return int(float(match.group(1)) * _UNITS[match.group(2).lower()])


def parse_block_io(line):
    """Parse '1.5MB / 3.2MB' (docker stats BlockIO) into (read_bytes, write_bytes)."""
    try:
        read_text, sep, write_text = line.partition("/")
        if not sep:
            raise ValueError("sep")
        return _bytes(read_text), _bytes(write_text)
    except ValueError:
        raise ContainerError("ディスクI/Oの形式を解析できません") from None


def parse_stats(line):
    """Parse '512.3MiB / 2GiB|25.00%' (docker stats --format) defensively."""
    try:
        usage, _, perc = line.partition("|")
        used_text, _, limit_text = usage.partition("/")
        used, limit = _bytes(used_text), _bytes(limit_text)
        if limit <= 0:
            raise ValueError("limit")
        percent = used * 100.0 / limit
        try:
            percent = max(percent, float(perc.strip().rstrip("%")))
        except ValueError:
            pass
        return {"used_bytes": used, "limit_bytes": limit, "percent": round(percent, 1)}
    except ValueError:
        raise ContainerError("メモリ使用量の形式を解析できません") from None
