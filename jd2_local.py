"""Local-only adapter for the Hub-managed JDownloader 2 engine.

Native mode: loopback 127.0.0.1 only (see below). Container mode: the
unpublished bridge-network IP of the Hub's own labelled container, verified by
docker inspect on every connection (engine_supervisor / container_runtime).

JD2's deprecated RemoteAPI has no authentication. It is therefore only used
when (1) every LISTEN socket on the API port is loopback and (2) the socket
inode is owned by the Java process the Hub itself started (see
docs/engine-setup.md). Only a fixed allowlist of methods can be called; the
browser can never reach JD2 directly. Real JD2 behaviour is unverified.
"""
import http.client
import ipaddress
import json
import os
import posixpath
import re
import threading
import time
import urllib.parse

import debuglog

from jd2_accounts import LIST_QUERY
from jd2_policy import KNOWN_KEYS, REQUIRED, valid_value

API_PORT = 3128
TIMEOUT = 5.0
MAX_RESPONSE = 4 * 1024 * 1024

# v0.1.25: "remove finished from the list". REMOVE_LINKS_ONLY never deletes files.
CLEANUP_ACTION = "DELETE_FINISHED"
CLEANUP_MODE = "REMOVE_LINKS_ONLY"
CLEANUP_SELECTION = "ALL"

# v0.1.29: per-item removal. REMOVE_LINKS_ONLY never deletes files; the
# DELETE_* triple is the only file-deleting call and is reachable only through
# delete_with_files (the controller checks the typed name and the save path).
DELETE_ACTION = "DELETE_ALL"
DELETE_MODE = "REMOVE_LINKS_AND_DELETE_FILES"
DELETE_SELECTION = "SELECTED"
ITEM_IDS_MAX = 100
ID_LIMIT = 2 ** 63   # JD2 ids are Java longs; they stay Python ints (exact in JSON)

ALLOWED_CALLS = frozenset((
    ("jd", "getCoreRevision"),
    ("update", "isUpdateAvailable"),
    ("update", "runUpdateCheck"),
    ("update", "restartAndUpdate"),
    ("config", "list"),
    ("config", "get"),
    ("config", "set"),
    ("downloadsV2", "queryPackages"),
    ("downloadsV2", "queryLinks"),
    ("linkgrabberv2", "addLinks"),
    # v0.1.24: start/stop the download controller and move LinkGrabber links.
    ("downloadcontroller", "start"),
    # v0.1.25: remove finished entries from the list only (never deletes files).
    ("downloadsV2", "cleanup"),
    ("downloadcontroller", "stop"),
    ("linkgrabberv2", "queryLinks"),
    ("linkgrabberv2", "moveToDownloadlist"),
    # v0.1.29: per-item start/stop/remove. removeLinks only edits the list;
    # cleanup(DELETE_ALL, REMOVE_LINKS_AND_DELETE_FILES, SELECTED) deletes files.
    ("downloadsV2", "setEnabled"),
    ("downloadsV2", "removeLinks"),
    ("downloadcontroller", "forceDownload"),
    ("linkgrabberv2", "removeLinks"),
    # v0.1.47: keep every package flat in the chosen download folder. The package
    # queries only read saveTo/uuid; setDownloadDirectory(directory, packageIds) is
    # JD2's own call (DownloadsAPIV2 / LinkCollectorAPIV2) and is only ever sent
    # with the Hub's download folder as the directory.
    ("linkgrabberv2", "queryPackages"),
    ("linkgrabberv2", "setDownloadDirectory"),
    ("linkgrabberv2", "queryLinkCrawlerJobs"),
    ("linkgrabberv2", "isCollecting"),
    ("downloadsV2", "setDownloadDirectory"),
    # Premium accounts (v0.1.23). Secrets travel in the request line of these
    # calls, so the adapter routes the whole namespace through _call_private.
    ("accountsV2", "listAccounts"),
    ("accountsV2", "listPremiumHoster"),
    ("accountsV2", "addAccount"),
    ("accountsV2", "refreshAccounts"),
    ("accountsV2", "enableAccounts"),
    ("accountsV2", "disableAccounts"),
    ("accountsV2", "setUserNameAndPassword"),
    ("accountsV2", "removeAccounts"),
))
PRIVATE_NAMESPACE = "accountsV2"
RAW_ACCOUNT_LIMIT = 5000


# ---- flat layout (v0.1.47) ---------------------------------------------------
FLATTEN_POLL = 0.5          # seconds between LinkGrabber checks after addLinks
FLATTEN_WINDOW = 15.0       # give up this long after the send
FLATTEN_QUERY_MAX = 500
FLATTEN_CHUNK = 50
FOLDER_THRESHOLD_BIG = 100000
FOLDER_KEY_SKIP = ("disable", "dont", "without", "avoid", "skip", "ignore", "delete")
FOLDER_THRESHOLD_WORDS = ("threshold", "count", "minimum", "limit")
FOLDER_INTERFACES = ("generalsettings", "linkgrabbersettings", "packagizer")


def subfolder_of(path, folder):
    """True when `path` lies strictly inside `folder` (lexical, both absolute, no '..')."""
    if (not isinstance(path, str) or not isinstance(folder, str) or
            not path.startswith("/") or not folder.startswith("/") or
            "\x00" in path or "\x00" in folder or
            ".." in path.split("/") or ".." in folder.split("/")):
        return False
    path, folder = posixpath.normpath(path), posixpath.normpath(folder)
    return folder != "/" and path.startswith(folder + "/")


def classify_folder_key(entry):
    """(interface, storage, key, value, target) for a JD2 key that makes package
    subfolders, else None. Only boolean "...subfolder..." keys (-> False) and integer
    threshold/count style "...subfolder..." keys (-> a very large value) qualify; the
    type of the listed value must match. No key name is assumed to exist."""
    interface = entry.get("interfaceName") or entry.get("configInterface")
    key = entry.get("key")
    storage = entry.get("storage")
    if not isinstance(interface, str) or not isinstance(key, str):
        return None
    if storage is not None and not isinstance(storage, str):
        return None
    lower_i, lower_k = interface.lower(), key.lower()
    if not any(word in lower_i for word in FOLDER_INTERFACES):
        return None
    if any(word in lower_k for word in KEY_EXCLUDE + FOLDER_KEY_SKIP):
        return None
    if not ("subfolder" in lower_k or ("packagename" in lower_k and "folder" in lower_k)):
        return None
    value = entry.get("value")
    if type(value) is bool:
        return interface, storage, key, value, False
    if type(value) is int and any(word in lower_k for word in FOLDER_THRESHOLD_WORDS):
        return interface, storage, key, value, max(value, FOLDER_THRESHOLD_BIG)
    return None


class LocalUnavailable(RuntimeError):
    """Engine not running, listener unsafe, or JD2 not answering."""


class ListenerError(LocalUnavailable):
    pass


# ---------------------------------------------------------------- listeners

def _decode_address(text, family):
    raw = text.strip()
    if family == 4:
        if len(raw) != 8:
            raise ValueError("bad ipv4")
        return ipaddress.IPv4Address(bytes.fromhex(raw)[::-1])
    if len(raw) != 32:
        raise ValueError("bad ipv6")
    data = b"".join(bytes.fromhex(raw[i:i + 8])[::-1] for i in range(0, 32, 8))
    return ipaddress.IPv6Address(data)


def parse_listeners(text, family):
    """Return [(ip_address, port, inode)] for LISTEN (state 0A) rows."""
    result = []
    for line in text.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 10 or fields[3] != "0A":
            continue
        try:
            host, _, port = fields[1].partition(":")
            result.append((_decode_address(host, family), int(port, 16), int(fields[9])))
        except (ValueError, TypeError):
            raise ListenerError("待受情報を解析できません") from None
    return result


def is_loopback_only(address):
    """Only 127.0.0.1, ::1 and ::ffff:127.0.0.1 count as safe. 0.0.0.0 never."""
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        else:
            return address == ipaddress.IPv6Address("::1")
    return address == ipaddress.IPv4Address("127.0.0.1")


def _read(path):
    try:
        with open(path, "r", encoding="ascii", errors="replace") as handle:
            return handle.read(2 * 1024 * 1024)
    except OSError:
        return None


def socket_inodes(pid, proc_root="/proc"):
    base = os.path.join(proc_root, str(int(pid)), "fd")
    found = set()
    try:
        names = os.listdir(base)
    except OSError:
        raise ListenerError("エンジンのプロセス情報を読めません") from None
    for name in names:
        try:
            match = re.fullmatch(r"socket:\[(\d+)\]", os.readlink(os.path.join(base, name)))
        except OSError:
            continue
        if match:
            found.add(int(match.group(1)))
    return found


def verify_listener(port, pid, proc_root="/proc"):
    """Raise ListenerError unless the API port is loopback-only and owned by pid."""
    tcp4 = _read(os.path.join(proc_root, "net", "tcp"))
    if tcp4 is None:
        raise ListenerError("/proc/net/tcpを読めません")
    rows = parse_listeners(tcp4, 4)
    tcp6 = _read(os.path.join(proc_root, "net", "tcp6"))
    if tcp6 is not None:
        rows += parse_listeners(tcp6, 6)
    rows = [row for row in rows if row[1] == port]
    if not rows:
        raise ListenerError("ポート%dの待受が見つかりません" % port)
    for address, _, _ in rows:
        if not is_loopback_only(address):
            raise ListenerError("ポート%dがloopback以外で待受されています" % port)
    owned = socket_inodes(pid, proc_root)
    for _, _, inode in rows:
        if inode == 0 or inode not in owned:
            raise ListenerError("ポート%dの待受がHubが起動したエンジンのものではありません" % port)
    return True


# ------------------------------------------------------------------ adapter

def encode_call(namespace, method, args):
    if (namespace, method) not in ALLOWED_CALLS:
        raise LocalUnavailable("許可されていないJD2メソッドです")
    path = "/%s/%s" % (namespace, method)
    if args:
        path += "?" + "&".join(urllib.parse.quote(
            json.dumps(a, ensure_ascii=False, separators=(",", ":")), safe="")
            for a in args)
    return path


KEY_INCLUDE = ("autostart", "autoconfirm", "confirm", "extract", "subfolder",
               "folder", "packagizer", "startdownload")
KEY_EXCLUDE = ("password", "account", "cookie", "token", "captcha", "proxy",
               "auth", "email")
KEY_LIMIT = 200


def filter_config_keys(entries):
    result = []
    for entry in entries:
        interface = entry.get("interfaceName") or entry.get("configInterface")
        key = entry.get("key")
        if not isinstance(interface, str) or not isinstance(key, str):
            continue
        name = (interface + " " + key).lower()
        if any(word in name for word in KEY_EXCLUDE):
            continue
        if not any(word in name for word in KEY_INCLUDE):
            continue
        value = entry.get("value")
        if not (isinstance(value, (bool, int)) or
                (isinstance(value, str) and len(value) <= 64 and "/" not in value)):
            value = None
        kind = entry.get("type")
        result.append({"interface": interface[:200], "key": key[:100],
                       "type": kind[:60] if isinstance(kind, str) else None,
                       "value": value})
        if len(result) >= KEY_LIMIT:
            break
    return result


def packagizer_rule_makes_folder(rule):
    """An enabled Packagizer rule whose download destination is a per-package folder."""
    if not isinstance(rule, dict) or rule.get("enabled") is not True:
        return False
    dest = rule.get("downloadDestination")
    if not isinstance(dest, str):
        return False
    lowered = dest.lower()
    return "<jd:packagename>" in lowered or "<jd:subfolderbyplugin>" in lowered


class LocalAdapter:
    """Same interface the old remote adapter had; talks to 127.0.0.1 only.

    `engine` must provide connection_target() -> port, or (host, port) in
    container mode (raising LocalUnavailable unless the engine is ours,
    running and the listener/container is verified), is_ours_running() and
    preset_applied. Optional path_mapping() -> {"nas_root", "jd2_root"} in
    container mode.
    """

    def __init__(self, engine, host="127.0.0.1", timeout=TIMEOUT):
        self._engine = engine
        self._host = host
        self._timeout = timeout
        self._handles = {}
        self._flatten_lock = threading.Lock()
        self._flatten_jobs = []
        self._flatten_thread = None
        self._flatten_sleep = time.sleep
        self._flatten_now = time.monotonic

    # -- properties used by controller/transfer
    @property
    def allow_update(self):
        return bool(self._engine.is_ours_running())

    @property
    def can_send(self):
        return bool(self._engine.is_ours_running() and self._engine.preset_applied)

    @property
    def engine_running(self):
        return bool(self._engine.is_ours_running())

    def note_preset_applied(self):
        self._engine.note_preset_applied()

    def note_preset_stale(self):
        self._engine.note_preset_stale()

    @property
    def _options(self):
        # Link submission is an explicit admin click and only allowed after
        # the preset was applied.
        mapping = getattr(self._engine, "path_mapping", None)
        mapping = mapping() if callable(mapping) else None
        if mapping:
            # Container mode: only the mounted download folder is visible to
            # JD2, as /downloads. jd2_transfer refuses anything outside it.
            return {"allow_link_submit": self.can_send,
                    "allow_unverified_packagizer": True,
                    "nas_root": mapping.get("nas_root"),
                    "jd2_root": mapping.get("jd2_root")}
        # Native engine on the same host: no path mapping.
        return {"allow_link_submit": self.can_send,
                "allow_unverified_packagizer": True, "same_host": True}

    def _call_private(self, method, args):
        """accountsV2 calls: credentials are in the GET request line.

        Residual risk (documented in docs/premium-accounts-design.md): the
        request goes only to loopback (native) or the private unpublished
        bridge IP of the Hub's own container, never over a LAN. The path and
        arguments are never logged, stored, or put into an exception: every
        failure says only the method name (and an HTTP status). The new
        exception is raised outside any `except` block so no chained context
        carries the request either.
        """
        target = self._engine.connection_target()   # fixed texts, no arguments
        host, port = (self._host, target) if isinstance(target, int) else target
        status = body = None
        try:
            path = encode_call(PRIVATE_NAMESPACE, method, args)
            connection = http.client.HTTPConnection(host, port, timeout=self._timeout)
            try:
                connection.request("GET", path, headers={"Accept": "application/json"})
                response = connection.getresponse()
                body = response.read(MAX_RESPONSE + 1)
                status = response.status
            finally:
                connection.close()
                path = None
        except Exception:
            body = None
        label = "JD2アカウントAPI（%s）" % method
        if body is None:
            raise LocalUnavailable(label + "の呼び出しに失敗しました")
        if status != 200:
            raise LocalUnavailable(label + "がエラーを返しました（HTTP %d）" % status)
        if len(body) > MAX_RESPONSE:
            raise LocalUnavailable(label + "の応答が大きすぎます")
        if not body.strip():
            return None
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeError, ValueError):
            value = body = None
        if body is None:
            raise LocalUnavailable(label + "の応答を解析できません")
        if isinstance(value, dict) and "data" in value:
            return value["data"]
        return value

    def _call(self, namespace, method, *args):
        """One RemoteAPI call; with the debug log on, only names, duration and the error text are logged."""
        if not debuglog.LOG._enabled:
            return self._call_inner(namespace, method, *args)
        started = time.monotonic()
        try:
            value = self._call_inner(namespace, method, *args)
        except Exception as error:
            text = type(error).__name__ if namespace == PRIVATE_NAMESPACE else str(error)[:120]
            debuglog.log("jd2", "error", "JD2 %s/%s 失敗" % (namespace, method),
                         {"ms": int((time.monotonic() - started) * 1000), "error": text})
            raise
        debuglog.log("jd2", "debug", "JD2 %s/%s" % (namespace, method),
                     {"ms": int((time.monotonic() - started) * 1000)})
        return value

    def _call_inner(self, namespace, method, *args):
        if namespace == PRIVATE_NAMESPACE:
            return self._call_private(method, args)
        path = encode_call(namespace, method, args)
        target = self._engine.connection_target()
        # Native: a port on 127.0.0.1. Container: (verified container IP, port).
        host, port = (self._host, target) if isinstance(target, int) else target
        connection = http.client.HTTPConnection(host, port, timeout=self._timeout)
        try:
            connection.request("GET", path, headers={"Accept": "application/json"})
            response = connection.getresponse()
            body = response.read(MAX_RESPONSE + 1)
            if response.status != 200:
                raise LocalUnavailable("JD2がエラーを返しました（HTTP %d）" % response.status)
        except LocalUnavailable:
            raise
        except (OSError, http.client.HTTPException):
            raise LocalUnavailable("JD2への通信に失敗しました。エンジンの状態を確認してください") from None
        finally:
            connection.close()
        if len(body) > MAX_RESPONSE:
            raise LocalUnavailable("JD2の応答が大きすぎます")
        if not body.strip():
            return None
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeError, ValueError):
            raise LocalUnavailable("JD2の応答を解析できません") from None
        if isinstance(value, dict) and "data" in value:
            return value["data"]
        return value

    def core_revision(self):
        revision = self._call("jd", "getCoreRevision")
        if isinstance(revision, bool) or not isinstance(revision, (int, str)) or not str(revision).strip():
            raise LocalUnavailable("JD2のCoreリビジョンが取得できません")
        return revision

    def is_update_available(self):
        return self._call("update", "isUpdateAvailable") is True

    def run_update_check(self):
        self._call("update", "runUpdateCheck")

    def restart_and_update(self):
        if not self.allow_update:
            raise LocalUnavailable("Hubが起動したエンジンのみ更新できます")
        self._call("update", "restartAndUpdate")
        self._handles = {}

    def _config_entries(self):
        # ConfigInterface.list(pattern, returnDescription, returnValues,
        # returnDefaultValues, returnEnumInfo): values only come back when asked.
        try:
            raw = self._call("config", "list", ".*", False, True, False, True)
        except ListenerError:
            raise
        except LocalUnavailable:
            raw = self._call("config", "list")
        if not isinstance(raw, list) or len(raw) > 50000:
            raise LocalUnavailable("JD2設定一覧の形式が不正です")
        return [entry for entry in raw if isinstance(entry, dict)]

    def config_keys(self):
        """Read-only, filtered key listing so real JD2 names can be discovered."""
        return filter_config_keys(self._config_entries())

    def inspect_preset(self):
        revision = self.core_revision()
        found = {}
        for entry in self._config_entries():
            interface = entry.get("interfaceName") or entry.get("configInterface")
            key = entry.get("key")
            storage = entry.get("storage")
            if not isinstance(interface, str) or not isinstance(key, str):
                continue
            for logical, spec in KNOWN_KEYS.items():
                if (interface != spec["interface"] or key.lower() != spec["key"] or
                        logical in found):
                    continue
                if not valid_value(logical, entry.get("value")):
                    continue
                if storage is not None and not isinstance(storage, str):
                    continue
                found[logical] = (interface, storage, key)
        self._handles = found
        entries = {}
        for logical in REQUIRED:
            handle = found.get(logical)
            if handle is None:
                entries[logical] = {"supported": False, "writable": False}
                continue
            value = self._call("config", "get", *handle)
            ok = valid_value(logical, value)
            entries[logical] = {"supported": ok, "writable": ok,
                                "current": value, "handle": logical}
        return {"jd_revision": revision, "entries": entries}

    def update_preset(self, logical, value):
        if logical not in KNOWN_KEYS or not valid_value(logical, value):
            raise LocalUnavailable("未検証のJD2設定を変更できません")
        handle = self._handles.get(logical)
        if not handle:
            raise LocalUnavailable("設定項目を再読込してから適用してください")
        if self._call("config", "set", *handle, value) is not True:
            raise LocalUnavailable("JD2が設定変更を拒否しました")

    def set_default_download_folder(self, folder):
        """Set GeneralSettings.defaultdownloadfolder if (and only if) listed.

        Returns False when the key is not found (caller reports a warning).
        """
        if not isinstance(folder, str) or not folder.startswith("/"):
            raise LocalUnavailable("保存先が不正です")
        for entry in self._config_entries():
            interface = entry.get("interfaceName") or entry.get("configInterface")
            key = entry.get("key")
            storage = entry.get("storage")
            if (isinstance(interface, str) and isinstance(key, str) and
                    "generalsettings" in interface.lower() and
                    key.lower() == "defaultdownloadfolder" and
                    (storage is None or isinstance(storage, str))):
                if self._call("config", "set", interface, storage, key, folder) is not True:
                    raise LocalUnavailable("JD2が保存先の設定変更を拒否しました")
                if self._call("config", "get", interface, storage, key) != folder:
                    raise LocalUnavailable("保存先の設定をreadbackで確認できません")
                return True
        return False

    def list_downloads(self, page=0, base=None):
        from jd2_downloads import LINK_FIELDS, PACKAGE_FIELDS, query_options, snapshot
        packages = self._call("downloadsV2", "queryPackages",
                              *query_options(PACKAGE_FIELDS, page))
        links = self._call("downloadsV2", "queryLinks", *query_options(LINK_FIELDS, page))
        return snapshot(packages, links, page, base)

    # ------------------------------------------------ flat layout (v0.1.47)
    @staticmethod
    def _package_ids(entries, destination, extra=None):
        ids = []
        for entry in entries:
            uuid = entry.get("uuid")
            if (type(uuid) is int and 0 <= uuid < ID_LIMIT and
                    subfolder_of(entry.get("saveTo"), destination) and
                    (extra is None or extra(entry))):
                ids.append(uuid)
        return ids

    def _package_query(self, namespace, fields):
        raw = self._call(namespace, "queryPackages",
                         dict(fields, maxResults=FLATTEN_QUERY_MAX, startAt=0))
        if not isinstance(raw, list):
            return []
        return [entry for entry in raw[:FLATTEN_QUERY_MAX] if isinstance(entry, dict)]

    def _set_directory(self, namespace, destination, ids):
        for start in range(0, len(ids), FLATTEN_CHUNK):
            self._call(namespace, "setDownloadDirectory", destination,
                       ids[start:start + FLATTEN_CHUNK])

    @staticmethod
    def _flatten_destination(destination):
        if (not isinstance(destination, str) or not destination.startswith("/") or
                posixpath.normpath(destination) == "/"):
            raise LocalUnavailable("保存先が不正です")
        return posixpath.normpath(destination)

    def flatten_linkgrabber(self, destination):
        """Packages waiting in the LinkGrabber that would save below `destination`
        are pointed at `destination` itself. Returns how many were changed."""
        destination = self._flatten_destination(destination)
        entries = self._package_query("linkgrabberv2", {
            "uuid": True, "name": True, "saveTo": True, "childCount": True})
        ids = self._package_ids(entries, destination)
        if ids:
            self._set_directory("linkgrabberv2", destination, ids)
            debuglog.log("jd2", "info", "LinkGrabberのパッケージ別フォルダを解除", {"packages": len(ids)})
        return len(ids)

    def flatten_waiting_downloads(self, destination):
        """Download-list packages that have not started (nothing loaded, not running,
        not finished) and sit in a subfolder of `destination` are pointed at it.
        Started or finished packages (their files live in the old folder) and anything
        outside `destination` are never touched. Returns how many were changed."""
        destination = self._flatten_destination(destination)
        entries = self._package_query("downloadsV2", {
            "uuid": True, "saveTo": True, "bytesLoaded": True,
            "running": True, "finished": True})
        ids = self._package_ids(entries, destination, lambda e: (
            e.get("finished") is False and e.get("running") is False and
            type(e.get("bytesLoaded")) is int and e.get("bytesLoaded") == 0))
        if ids:
            self._set_directory("downloadsV2", destination, ids)
            debuglog.log("jd2", "info", "待機中パッケージのフォルダをまとめました", {"packages": len(ids)})
        return len(ids)

    def schedule_flatten(self, destination, add_result=None):
        """Background, bounded (every 0.5 s for up to 15 s) flattening after addLinks.

        One worker serves all sends; a send while it runs only adds a job. Never raises.
        """
        try:
            destination = self._flatten_destination(destination)
        except LocalUnavailable:
            return
        job_id = add_result.get("id") if isinstance(add_result, dict) else None
        job = {"destination": destination,
               "job": job_id if type(job_id) is int and not isinstance(job_id, bool) else None,
               "deadline": self._flatten_now() + FLATTEN_WINDOW, "done_seen": False}
        with self._flatten_lock:
            self._flatten_jobs.append(job)
            if self._flatten_thread is not None:
                return
            thread = threading.Thread(target=self._flatten_worker, daemon=True)
            self._flatten_thread = thread
        thread.start()

    def flatten_join(self, timeout=30.0):
        """Wait for the background worker (tests, shutdown)."""
        with self._flatten_lock:
            thread = self._flatten_thread
        if thread is not None:
            thread.join(timeout)

    def _flatten_worker(self):
        while True:
            with self._flatten_lock:
                jobs = list(self._flatten_jobs)
                if not jobs:
                    self._flatten_thread = None
                    return
            try:
                self._flatten_cycle(jobs)
            except Exception as error:      # the worker must never die with a stuck slot
                debuglog.log("jd2", "error", "フォルダ整理に失敗", {"error": str(error)[:120]})
            self._flatten_sleep(FLATTEN_POLL)

    def _flatten_cycle(self, jobs):
        for destination in sorted({job["destination"] for job in jobs}):
            for step in (self.flatten_linkgrabber, self.flatten_waiting_downloads):
                try:
                    step(destination)
                except LocalUnavailable as error:
                    debuglog.log("jd2", "debug", "フォルダ整理を一時的にスキップ", {"error": str(error)[:120]})
        now = self._flatten_now()
        finished = []
        for job in jobs:
            if now >= job["deadline"]:
                finished.append(job)
            elif self._crawler_done(job["job"]):
                if job["done_seen"]:
                    finished.append(job)
                job["done_seen"] = True     # one more pass after the crawler finished
        if finished:
            with self._flatten_lock:
                self._flatten_jobs = [j for j in self._flatten_jobs if j not in finished]

    def _crawler_done(self, job_id):
        if job_id is not None:
            try:
                raw = self._call("linkgrabberv2", "queryLinkCrawlerJobs", {"jobIds": [job_id]})
                if isinstance(raw, list):
                    return not any(isinstance(e, dict) and (e.get("crawling") is True or
                                                            e.get("checking") is True) for e in raw)
            except LocalUnavailable:
                pass
        try:
            return self._call("linkgrabberv2", "isCollecting") is False
        except LocalUnavailable:
            return False

    def package_folder_settings(self, apply=False):
        """JD2 keys (General/Linkgrabber/Packagizer settings) that create package
        subfolders, with the value they should have. With apply=True each one that is
        not compliant is written and read back. Keys that do not exist are not invented."""
        report = []
        for entry in self._config_entries():
            found = classify_folder_key(entry)
            if found is None or len(report) >= 20:
                continue
            interface, storage, key, value, target = found
            item = {"interface": interface.rsplit(".", 1)[-1][:80], "key": key[:100],
                    "current": value, "target": target, "compliant": value == target}
            if apply and not item["compliant"]:
                try:
                    done = self._call("config", "set", interface, storage, key, target) is True
                    item["current"] = self._call("config", "get", interface, storage, key)
                    item["compliant"] = done and item["current"] == target
                except LocalUnavailable:
                    item["compliant"] = False
            report.append(item)
        rules = self.packagizer_folder_rules(apply)
        if rules is not None:
            report.append(rules)
        return report

    def packagizer_folder_rules(self, apply=False):
        """v0.1.48: Packagizer rules that send files into a per-package folder.

        JD2's Packagizer (PackagizerSettings.rulelist) ships rules whose download
        destination contains <jd:packagename> (e.g. "subfolder by package name");
        they win over addLinks' destinationFolder. Such enabled rules are reported and,
        with apply=True, disabled (the whole list is written back and read back).
        Returns a report item, or None when JD2 has no Packagizer rule list."""
        handle, rules = None, None
        for entry in self._config_entries():
            interface = entry.get("interfaceName") or entry.get("configInterface")
            key = entry.get("key")
            storage = entry.get("storage")
            if (isinstance(interface, str) and "packagizer" in interface.lower() and
                    isinstance(key, str) and key.lower() == "rulelist" and
                    (storage is None or isinstance(storage, str))):
                handle = (interface, storage, key)
                break
        if handle is None:
            return None
        rules = self._call("config", "get", *handle)
        if not isinstance(rules, list) or len(rules) > 500:
            return {"interface": "PackagizerSettings", "key": "rulelist", "current": "読めません",
                    "target": "パッケージ名フォルダのルールを無効", "compliant": False}
        hits = [i for i, rule in enumerate(rules) if packagizer_rule_makes_folder(rule)]
        names = [str(rules[i].get("name") or "")[:60] for i in hits]
        item = {"interface": "PackagizerSettings", "key": "rulelist",
                "current": ("有効: " + "、".join(names)) if hits else "該当ルールなし",
                "target": "パッケージ名フォルダのルールを無効", "compliant": not hits}
        if apply and hits:
            updated = [dict(rule) for rule in rules]
            for i in hits:
                updated[i]["enabled"] = False
            try:
                done = self._call("config", "set", *handle, updated) is True
                again = self._call("config", "get", *handle)
                still = [r for r in again if packagizer_rule_makes_folder(r)] if isinstance(again, list) else [None]
                item["compliant"] = done and not still
                item["current"] = ("無効にしました: " + "、".join(names)) if item["compliant"] else "変更を確認できません"
            except LocalUnavailable:
                item["compliant"] = False
        return item

    def add_link(self, url, destination, autostart=False):
        # `autostart` is the per-call flag of addLinks (Hub setting send_autostart);
        # JD2's own auto-confirm/auto-start settings stay OFF. This adapter only ever
        # talks to the Hub-owned engine, so the explicit destination wins over any
        # Packagizer rule (flat layout).
        if not isinstance(url, str) or not url.startswith("https://"):
            raise LocalUnavailable("HTTPSリンクのみ転送できます")
        if not isinstance(destination, str) or not destination.startswith("/"):
            raise LocalUnavailable("JD2側の保存先が不正です")
        return self._call("linkgrabberv2", "addLinks", {
            "links": url,
            "autostart": autostart is True,
            "destinationFolder": destination,
            "overwritePackagizerRules": True,
            "priority": "DEFAULT",
        })

    # ------------------------------------------- download controller / LinkGrabber
    def start_downloads(self):
        self._call("downloadcontroller", "start")

    def stop_downloads(self):
        self._call("downloadcontroller", "stop")

    def cleanup_finished(self):
        """Remove finished downloads from JD2's list. The mode is a constant: files stay."""
        assert CLEANUP_MODE == "REMOVE_LINKS_ONLY"
        self._call("downloadsV2", "cleanup", [], [], CLEANUP_ACTION, CLEANUP_MODE, CLEANUP_SELECTION)

    # v0.1.51: links JD2 skipped because the file already exists ("File already exists",
    # finalLinkState FAILED_EXISTS). The cleanup actions do not cover them; they are removed from
    # the list only (removeLinks never touches files).
    EXISTS_STATE = "FAILED_EXISTS"
    EXISTS_TEXT = re.compile(r"exist|既に|すでに|存在", re.I)

    def _scan_links(self):
        """(finished_count, ids of "already exists" skips) from the link list (paged, read-only)."""
        return self._scan_links_full()[:2]

    def _scan_links_full(self):
        """v0.1.70: (finished_count, "already exists" ids, "file not found" ids), one paged read-only scan."""
        from jd2_downloads import PAGE_SIZE, MAX_PAGE, is_missing_link
        fields = {"uuid": True, "status": True, "skipped": True, "finalLinkState": True, "finished": True,
                  "availability": True, "running": True}
        found, missing, finished = [], [], 0
        for page in range(MAX_PAGE + 1):
            batch = self._call("downloadsV2", "queryLinks",
                               dict(fields, maxResults=PAGE_SIZE, startAt=page * PAGE_SIZE))
            if not isinstance(batch, list) or not batch:
                break
            for link in batch:
                if not isinstance(link, dict) or type(link.get("uuid")) is not int:
                    continue
                if link.get("finished") is True:
                    finished += 1
                state = link.get("finalLinkState")
                status = link.get("status") if isinstance(link.get("status"), str) else ""
                if state == self.EXISTS_STATE or (link.get("skipped") is True and self.EXISTS_TEXT.search(status)):
                    if 0 <= link["uuid"] < ID_LIMIT:
                        found.append(link["uuid"])
                elif is_missing_link(link) and 0 <= link["uuid"] < ID_LIMIT:
                    missing.append(link["uuid"])
            if len(batch) < PAGE_SIZE:
                break
        return finished, found, missing

    def sweep_missing_downloads(self):
        """v0.1.70: remove download-list entries whose file is gone from the hoster (list only, files stay).
        Returns how many were removed; nothing is sent when there are none."""
        missing = self._scan_links_full()[2]
        for i in range(0, len(missing), ITEM_IDS_MAX):
            self._call("downloadsV2", "removeLinks", missing[i:i + ITEM_IDS_MAX], [])
        return len(missing)

    def remove_skipped_existing(self):
        found = self._scan_links()[1]
        for i in range(0, len(found), ITEM_IDS_MAX):
            self._call("downloadsV2", "removeLinks", found[i:i + ITEM_IDS_MAX], [])
        return len(found)

    def sweep_finished(self):
        """v0.1.56: automatic version of the "clear finished/skipped" button.

        One read-only query when nothing is finished; only then the same two list-only calls the
        button makes (cleanup_finished + remove_skipped_existing). Files are never touched.
        Returns (finished_seen, skipped_removed); (0, 0) when there was nothing to clear."""
        finished, exists = self._scan_links()
        if not finished and not exists:
            return 0, 0
        self.cleanup_finished()
        return finished, self.remove_skipped_existing()

    @staticmethod
    def _id_list(values):
        if (not isinstance(values, list) or len(values) > ITEM_IDS_MAX or
                any(type(i) is not int or not 0 <= i < ID_LIMIT for i in values)):
            raise LocalUnavailable("JD2のIDが不正です")
        return list(values)

    def _item_ids(self, link_ids, package_ids):
        links, packages = self._id_list(link_ids), self._id_list(package_ids)
        if not links and not packages:
            raise LocalUnavailable("対象がありません")
        return links, packages

    def set_enabled(self, enabled, link_ids, package_ids):
        if type(enabled) is not bool:
            raise LocalUnavailable("有効／無効の指定が不正です")
        links, packages = self._item_ids(link_ids, package_ids)
        self._call("downloadsV2", "setEnabled", enabled, links, packages)

    def force_download(self, link_ids, package_ids):
        links, packages = self._item_ids(link_ids, package_ids)
        self._call("downloadcontroller", "forceDownload", links, packages)

    def remove_links(self, link_ids, package_ids):
        """Remove from JD2's list only; saved files are never touched."""
        links, packages = self._item_ids(link_ids, package_ids)
        self._call("downloadsV2", "removeLinks", links, packages)

    def delete_with_files(self, link_ids, package_ids):
        """Remove from the list AND delete the saved files (caller must verify)."""
        links, packages = self._item_ids(link_ids, package_ids)
        self._require_empty_folder_cleanup_off()
        self._call("downloadsV2", "cleanup", links, packages,
                   DELETE_ACTION, DELETE_MODE, DELETE_SELECTION)

    def _require_empty_folder_cleanup_off(self):
        """JD2 may delete the emptied download folder itself; refuse unless that is OFF."""
        refusal = ("JD2の『空フォルダ削除』がONのため、ファイル削除を中止しました"
                   "（推奨設定を再適用してください）")
        spec = KNOWN_KEYS["general.delete_empty_folders"]
        handle = None
        for entry in self._config_entries():
            interface = entry.get("interfaceName") or entry.get("configInterface")
            key = entry.get("key")
            storage = entry.get("storage")
            if (interface == spec["interface"] and isinstance(key, str) and
                    key.lower() == spec["key"] and (storage is None or isinstance(storage, str))):
                handle = (interface, storage, key)
                break
        if handle is None:
            raise LocalUnavailable(refusal)
        if self._call("config", "get", *handle) is False:
            return
        try:
            self._call("config", "set", *handle, False)
            confirmed = self._call("config", "get", *handle) is False
        except LocalUnavailable:
            confirmed = False
        if not confirmed:
            raise LocalUnavailable(refusal)

    def remove_waiting(self, link_ids):
        links, _ = self._item_ids(link_ids, [])
        self._call("linkgrabberv2", "removeLinks", links, [])

    def package_save_path(self, package_id):
        """JD2-side save folder of one package (never sent to the browser)."""
        package_id, = self._id_list([package_id])
        raw = self._call("downloadsV2", "queryPackages", {
            "uuid": True, "saveTo": True, "packageUUIDs": [package_id],
            "maxResults": 1, "startAt": 0})
        if isinstance(raw, list):
            for entry in raw:
                if (isinstance(entry, dict) and entry.get("uuid") == package_id and
                        isinstance(entry.get("saveTo"), str)):
                    return entry["saveTo"]
        return None

    def _download_keys(self):
        """v0.1.70: (name, host) of the download list for the duplicate check; empty when unavailable."""
        from jd2_downloads import download_keys, download_keys_query_options
        try:
            return download_keys(self._call("downloadsV2", "queryLinks", *download_keys_query_options()))
        except LocalUnavailable:
            return frozenset()

    def list_linkgrabber(self):
        from jd2_downloads import linkgrabber_query_options, linkgrabber_snapshot
        raw = self._call("linkgrabberv2", "queryLinks", *linkgrabber_query_options())
        # one more cheap query (names only) and only when something waits
        keys = self._download_keys() if isinstance(raw, list) and raw else frozenset()
        return linkgrabber_snapshot(raw, keys)

    def sweep_waiting(self, start=True, clear_missing=False):
        """v0.1.70: housekeeping of the LinkGrabber in one cheap query.

        clear_missing: remove OFFLINE ("file not found") links from the list (never UNKNOWN / TEMP_UNKNOWN, which
        JD2 may still be checking; never duplicates or disabled ones; files are never touched).
        start: move links that are ONLINE, enabled and not already in the download list to the download list
        and start the controller. An empty LinkGrabber costs exactly one query; the download list is only read
        when a link is ONLINE. Returns {"seen", "moved", "removed"}."""
        from jd2_downloads import linkgrabber_query_options, waiting_state
        raw = self._call("linkgrabberv2", "queryLinks", *linkgrabber_query_options())
        entries = [e for e in raw if isinstance(e, dict)] if isinstance(raw, list) else []
        result = {"seen": len(entries), "moved": 0, "removed": 0}
        valid = lambda e: type(e.get("uuid")) is int and 0 <= e["uuid"] < ID_LIMIT
        if clear_missing:
            gone = [e["uuid"] for e in entries if valid(e) and waiting_state(e) == "offline"]
            for i in range(0, len(gone), 50):
                self.remove_waiting(gone[i:i + 50])
            result["removed"] = len(gone)
        if not start or not any(waiting_state(e) == "online" for e in entries):
            return result
        keys = self._download_keys()
        ids = [e["uuid"] for e in entries if valid(e) and waiting_state(e, keys) == "online"]
        for i in range(0, len(ids), 50):
            self.move_to_downloadlist(ids[i:i + 50], [])
        if ids:
            self.start_downloads()
        result["moved"] = len(ids)
        return result

    def move_to_downloadlist(self, link_ids, package_ids):
        def check(values, maximum):
            if (not isinstance(values, list) or len(values) > maximum or
                    any(type(i) is not int or not 0 <= i < 2 ** 63 for i in values)):
                raise LocalUnavailable("LinkGrabberのIDが不正です")
            return list(values)
        self._call("linkgrabberv2", "moveToDownloadlist",
                   check(link_ids, 50), check(package_ids, 50))

    # ---------------------------------------------------- premium accounts
    # list_* return JD2's raw objects for jd2_accounts to project onto the
    # public model; they never leave the Hub process as they are.
    def list_accounts(self):
        raw = self._call("accountsV2", "listAccounts", dict(LIST_QUERY))
        if not isinstance(raw, list) or len(raw) > RAW_ACCOUNT_LIMIT:
            raise LocalUnavailable("JD2アカウントAPI（listAccounts）の応答形式が不正です")
        return [entry for entry in raw if isinstance(entry, dict)]

    def list_premium_hosters(self):
        raw = self._call("accountsV2", "listPremiumHoster")
        if not isinstance(raw, list) or len(raw) > RAW_ACCOUNT_LIMIT:
            raise LocalUnavailable("JD2アカウントAPI（listPremiumHoster）の応答形式が不正です")
        return raw

    @staticmethod
    def _ids(ids):
        if (not isinstance(ids, list) or not 1 <= len(ids) <= 50 or
                any(type(i) is not int or not 0 <= i < 2 ** 63 for i in ids)):
            raise LocalUnavailable("アカウントIDが不正です")
        return list(ids)

    def add_account(self, host, username, password):
        if not all(isinstance(v, str) and v for v in (host, username, password)):
            raise LocalUnavailable("アカウント情報が不正です")
        if self._call("accountsV2", "addAccount", host, username, password) is False:
            raise LocalUnavailable("JD2がアカウントの追加を拒否しました")

    def refresh_accounts(self, ids):
        self._call("accountsV2", "refreshAccounts", self._ids(ids))

    def enable_accounts(self, ids):
        self._call("accountsV2", "enableAccounts", self._ids(ids))

    def disable_accounts(self, ids):
        self._call("accountsV2", "disableAccounts", self._ids(ids))

    def set_credentials(self, ident, username, password):
        if type(ident) is not int or not all(isinstance(v, str) and v for v in (username, password)):
            raise LocalUnavailable("アカウント情報が不正です")
        if self._call("accountsV2", "setUserNameAndPassword", ident, username, password) is False:
            raise LocalUnavailable("JD2が認証情報の更新を拒否しました")

    def remove_accounts(self, ids):
        self._call("accountsV2", "removeAccounts", self._ids(ids))
