"""Fixed-function Docker CLI wrapper for Container Station (docs/container-runtime.md).

The Hub never passes arbitrary arguments, images or commands to Docker.
Only the constants below can be pulled, only the Hub's own labelled
containers/networks can be inspected, stopped or removed, and every call is
a list-argument subprocess (no shell) with a timeout and a minimal
environment. Other containers (including an existing Docker JD2) are never
listed or touched. Real Container Station behaviour is unverified.

Stdlib only; Python 3.8 compatible. The command runner is injectable.
"""
import ipaddress
import json
import os
import subprocess
from types import SimpleNamespace

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
ALLOWED_NAMES = frozenset((JD2_NAME,))
ALLOWED_NETWORKS = frozenset((JD2_NET,))

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

MAX_OUTPUT = 1024 * 1024
_BAD_PATH_CHARS = set(',"\'\n\r\0')


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
        return self._runner([docker] + list(args), timeout, self._env(docker))

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
        if "no such" not in ((result.stderr or "") + (result.stdout or "")).lower():
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
            if "no such" in ((result.stderr or "") + (result.stdout or "")).lower():
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
            }
        except (ValueError, IndexError, AttributeError, TypeError):
            raise ContainerError("コンテナ情報を解析できません") from None

    def container_state(self, name):
        info = self._inspect(name)
        if info is None:
            return {"exists": False, "managed": False, "running": False, "status": "absent"}
        return {"exists": True, "managed": info["managed"], "running": info["running"],
                "status": info["status"]}

    def _require_ours(self, name):
        info = self._inspect(name)
        if info is not None and not info["managed"]:
            raise ContainerError("Hub管理外のコンテナのため操作しません: " + name)
        return info

    def inspect_ip(self, name, network):
        """IP of our running container on our network; verified on every call."""
        if network not in ALLOWED_NETWORKS:
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

    def stop(self, name):
        info = self._require_ours(name)
        if info is None:
            return False
        if not info["running"]:
            return False
        result = self._docker(["stop", "-t", str(JD2_STOP_GRACE), name], JD2_STOP_GRACE + 30)
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
