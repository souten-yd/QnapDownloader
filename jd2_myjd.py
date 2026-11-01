"""Opt-in authenticated MyJDownloader adapter; no deprecated HTTP remote API.

The adapter never modifies existing JD2 settings unless the authenticated
Hub administrator requests it. Direct connection is explicitly disabled.
"""
import json
import os
from pathlib import Path
import stat
import sys

# These two keys are documented by JD2; all other settings remain unsupported
# until their exact AdvancedConfig metadata is observed on the real device.
KNOWN_BOOLEAN_KEYS = {
    "linkgrabber.auto_confirm": ("linkgrabber", "linkgrabberautoconfirmenabled"),
    "linkgrabber.auto_start": ("linkgrabber", "linkgrabberautostartenabled"),
}


class MyJDUnavailable(RuntimeError):
    pass


def load_configuration(data_root):
    path = Path(data_root) / "config" / "myjd.json"
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise MyJDUnavailable("myjd.jsonは通常ファイルで指定してください")
    mode = path.stat().st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise MyJDUnavailable("myjd.jsonの権限を0600にしてください")
    if path.stat().st_size > 8192:
        raise MyJDUnavailable("MyJD設定ファイルが大きすぎます")
    options = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(options, dict) or
            not isinstance(options.get("email"), str) or
            not isinstance(options.get("password"), str) or
            not isinstance(options.get("device_name"), str) or
            not options["email"].strip() or not options["password"] or
            not options["device_name"].strip()):
        raise MyJDUnavailable("MyJDのemail/password/device_nameが必要です")
    if options.get("allow_jd2_update") not in (None, False, True):
        raise MyJDUnavailable("allow_jd2_updateの値はbooleanのみです")
    return options


class MyJDAdapter:
    """Compatible with myjdapi 1.1.x, uses remote authenticated MyJD only."""
    def __init__(self, options, api_factory=None):
        self._options = options
        self._factory = api_factory
        self._client = None
        self._device = None
        self._handles = {}

    @property
    def allow_update(self):
        return self._options.get("allow_jd2_update") is True

    def _connect(self):
        if self._device is not None:
            return self._device
        try:
            if self._factory is None:
                # Vendor dependencies are installed at QPKG build time.
                # Never run pip, npm, or network installs at server startup.
                packaged = Path(__file__).resolve().parent / "vendor"
                if packaged.is_dir() and str(packaged) not in sys.path:
                    sys.path.insert(0, str(packaged))
                import myjdapi
                # myjdapi 1.1.11 exposes the class as Myjdapi.
                # Using lower-case myjdapi here silently passed mock-only
                # tests but breaks real QNAP MyJD login.
                self._factory = myjdapi.Myjdapi
            client = self._factory()
            # Only encrypted authenticated MyJDownloader; NEVER direct_connect.
            client.connect(self._options["email"], self._options["password"])
            client.update_devices()
            names = [x for x in client.list_devices()
                     if isinstance(x, dict) and x.get("name") == self._options["device_name"]]
            if len(names) != 1:
                raise MyJDUnavailable("一致するJD2デバイスを一意に指定できません")
            device = client.get_device(device_id=names[0]["id"])
            if device is None:
                raise MyJDUnavailable("指定JD2デバイスに接続できません")
            self._client, self._device = client, device
            return device
        except MyJDUnavailable:
            raise
        except Exception as exc:
            raise MyJDUnavailable("認証済みMyJDownloaderに接続できません") from exc

    def _call(self, attr, method, *args):
        dev = self._connect()
        try:
            return getattr(getattr(dev, attr), method)(*args)
        except Exception as exc:
            self._device = None
            raise MyJDUnavailable("JD2への通信に失敗しました。接続を再確認してください") from exc

    def core_revision(self):
        revision = self._call("jd", "get_core_revision")
        if isinstance(revision, bool) or not isinstance(revision, (int, str)) or not str(revision).strip():
            raise MyJDUnavailable("JD2のCoreリビジョンが取得できません")
        return revision

    def is_update_available(self):
        return self._call("update", "is_update_available") is True

    def run_update_check(self):
        self._call("update", "run_update_check")

    def restart_and_update(self):
        if self._options.get("allow_jd2_update") is not True:
            raise MyJDUnavailable("既存JD2の自動更新には管理者の明示的な許可が必要です")
        self._call("update", "restart_and_update")
        # During restart previous connection is invalid and any preset cached
        # before the update must be discarded by the controller.
        self._device = None
        self._handles = {}

    def inspect_preset(self):
        revision = self.core_revision()
        raw = self._call("config", "list")
        if not isinstance(raw, list) or len(raw) > 50000:
            raise MyJDUnavailable("JD2設定一覧の形式が不正です")
        found = {}
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            interface = entry.get("interfaceName") or entry.get("configInterface")
            key = entry.get("key")
            storage = entry.get("storage")
            if not isinstance(interface, str) or not isinstance(key, str):
                continue
            for logical, (family, expected_key) in KNOWN_BOOLEAN_KEYS.items():
                if (family not in interface.lower() or
                        key.lower() != expected_key or
                        logical in found):
                    continue
                if type(entry.get("value")) is not bool:
                    continue
                # Some JD2 variants use null storage, others an explicit cfg path.
                if storage is not None and not isinstance(storage, str):
                    continue
                found[logical] = (interface, storage, key)
        entries = {}
        self._handles = found
        from jd2_policy import REQUIRED
        for logical in REQUIRED:
            handle = found.get(logical)
            if handle is None:
                entries[logical] = {"supported": False, "writable": False}
                continue
            value = self._call("config", "get", *handle)
            entries[logical] = {"supported": type(value) is bool,
                                "writable": type(value) is bool,
                                "current": value, "handle": logical}
        return {"jd_revision": revision, "entries": entries}

    def add_link(self, url, destination):
        # No automatic download or Packagizer overwrite. JD2 may still have
        # user-authored rules, which the transfer preflight independently checks.
        if not isinstance(url, str) or not url.startswith("https://"):
            raise MyJDUnavailable("HTTPSリンクのみ転送できます")
        if not isinstance(destination, str) or not destination.startswith("/"):
            raise MyJDUnavailable("JD2側の保存先が不正です")
        return self._call("linkgrabber", "add_links", [{
            "links": url,
            "autostart": False,
            "destinationFolder": destination,
            "overwritePackagizerRules": False,
            "priority": "DEFAULT"
        }])

    def update_preset(self, logical, value):
        if logical not in KNOWN_BOOLEAN_KEYS or type(value) is not bool:
            raise MyJDUnavailable("未検証のJD2設定を変更できません")
        handle = self._handles.get(logical)
        if not handle:
            raise MyJDUnavailable("設定項目を再読込してから適用してください")
        changed = self._call("config", "set", *handle, value)
        if changed is not True:
            raise MyJDUnavailable("JD2が設定変更を拒否しました")


class UnavailableAdapter:
    """Keep the Hub Web UI available when optional JD2 configuration is invalid."""
    allow_update = False

    def __init__(self, reason):
        self.reason = reason

    def core_revision(self):
        raise MyJDUnavailable(self.reason)


def configured_adapter(data_root):
    try:
        options = load_configuration(data_root)
        return None if options is None else MyJDAdapter(options)
    except (OSError, ValueError, MyJDUnavailable, json.JSONDecodeError):
        return UnavailableAdapter("MyJD設定を確認してください（パス/権限/内容）")
