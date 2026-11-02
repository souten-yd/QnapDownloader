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
import re
import urllib.parse

from jd2_policy import KNOWN_KEYS, REQUIRED, valid_value

API_PORT = 3128
TIMEOUT = 5.0
MAX_RESPONSE = 4 * 1024 * 1024

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
))


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

    # -- properties used by controller/transfer
    @property
    def allow_update(self):
        return bool(self._engine.is_ours_running())

    @property
    def can_send(self):
        return bool(self._engine.is_ours_running() and self._engine.preset_applied)

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

    def _call(self, namespace, method, *args):
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

    def list_downloads(self, page=0):
        from jd2_downloads import LINK_FIELDS, PACKAGE_FIELDS, query_options, snapshot
        packages = self._call("downloadsV2", "queryPackages",
                              *query_options(PACKAGE_FIELDS, page))
        links = self._call("downloadsV2", "queryLinks", *query_options(LINK_FIELDS, page))
        return snapshot(packages, links, page)

    def add_link(self, url, destination):
        # No auto-start. This adapter only ever talks to the Hub-owned engine, so
        # the explicit destination wins over any Packagizer rule (flat layout).
        if not isinstance(url, str) or not url.startswith("https://"):
            raise LocalUnavailable("HTTPSリンクのみ転送できます")
        if not isinstance(destination, str) or not destination.startswith("/"):
            raise LocalUnavailable("JD2側の保存先が不正です")
        return self._call("linkgrabberv2", "addLinks", {
            "links": url,
            "autostart": False,
            "destinationFolder": destination,
            "overwritePackagizerRules": True,
            "priority": "DEFAULT",
        })
