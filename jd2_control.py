"""JD2 independent update coordinator for the Hub-managed local engine.

The adapter (jd2_local.LocalAdapter) only talks to the verified loopback
listener of the engine the Hub started. Fails closed when the engine is not
running.
"""
import posixpath
import re
import threading

import jd2_transfer
from jd2_downloads import check_page
from jd2_policy import build_preview, apply_preset, PresetUnavailable, PresetChanged


ID_PATTERN = re.compile(r"(?:0|[1-9][0-9]{0,18})")
ID_MAX = 2 ** 63 - 1
BULK_MAX = 100


def parse_ids(values):
    """Decimal strings only (JS numbers lose 64-bit precision); returns ints."""
    if not isinstance(values, list) or not 1 <= len(values) <= BULK_MAX:
        raise ValueError("対象は1〜%d件で指定してください" % BULK_MAX)
    result = []
    for value in values:
        if (type(value) is not str or not ID_PATTERN.fullmatch(value) or
                int(value) > ID_MAX):
            raise ValueError("IDの形式が不正です")
        if int(value) not in result:
            result.append(int(value))
    return result


def inside_folder(path, folder):
    """Lexical containment of a JD2-side path (no '..', absolute, normalised)."""
    if (not isinstance(path, str) or not isinstance(folder, str) or
            not path.startswith("/") or "\x00" in path or ".." in path.split("/")):
        return False
    path, folder = posixpath.normpath(path), posixpath.normpath(folder)
    return folder != "/" and (path == folder or path.startswith(folder + "/"))


class JD2Controller:
    def __init__(self, adapter=None):
        self.adapter = adapter
        self.lock = threading.RLock()
        self._preview = None

    def _require(self):
        if self.adapter is None:
            raise PresetUnavailable("エンジン未起動/未導入")
        return self.adapter

    def require_adapter(self):
        """The engine adapter, or PresetUnavailable. Callers hold self.lock."""
        return self._require()

    def status(self):
        if self.adapter is None:
            return {"connected": False, "reason": "エンジン未起動/未導入",
                    "can_update": False, "can_preset": False, "can_send": False}
        with self.lock:
            try:
                adapter = self._require()
                return {"connected": True, "can_update": getattr(adapter, "allow_update", True), "can_preset": True,
                        "can_send": getattr(adapter, "can_send", False) is True,
                        "revision": adapter.core_revision(),
                        "update_available": adapter.is_update_available()}
            except Exception as error:
                return {"connected": False, "reason": str(error),
                        "can_update": False, "can_preset": False, "can_send": False}

    def downloads_snapshot(self, page=0):
        """Read-only; never triggers JD2 updates, queue mutations or downloads."""
        with self.lock:
            return self._require().list_downloads(page)

    def linkgrabber_snapshot(self):
        """Read-only list of links JD2 holds in the LinkGrabber (max 50)."""
        with self.lock:
            return self._require().list_linkgrabber()

    def start_waiting(self, *, confirmed=False):
        """Move every listed LinkGrabber link to the download list, then start."""
        if confirmed is not True:
            raise ValueError("待機中のリンクの開始には確認が必要です")
        with self.lock:
            adapter = self._require()
            waiting = adapter.list_linkgrabber()
            links = [int(i["id"]) for i in waiting["items"] if i["id"] is not None]
            packages = sorted({int(i["package_id"]) for i in waiting["items"]
                               if i["package_id"] is not None})
            if links or packages:
                adapter.move_to_downloadlist(links, packages)
            adapter.start_downloads()
            return {"moved": len(links), "started": True}

    # ---- per-item actions (v0.1.29) ----------------------------------------
    def _selected(self, adapter, kind, ids, page):
        """Resolve ids against a snapshot fetched right now; unknown ids refuse."""
        if kind not in ("package", "link"):
            raise ValueError("種別が不正です")
        wanted = parse_ids(ids)
        snap = adapter.list_downloads(check_page(page))
        pool = snap["packages"] if kind == "package" else snap["links"]
        known = {int(i["id"]): i for i in pool if i.get("id") is not None}
        if any(i not in known for i in wanted):
            raise ValueError("一覧にない項目です。画面を更新してからやり直してください")
        return wanted, [known[i] for i in wanted]

    @staticmethod
    def _split(kind, ids):
        return (ids, []) if kind == "link" else ([], ids)

    def item_start(self, kind, ids, *, page=0):
        with self.lock:
            adapter = self._require()
            wanted, _ = self._selected(adapter, kind, ids, page)
            links, packages = self._split(kind, wanted)
            adapter.set_enabled(True, links, packages)
            adapter.force_download(links, packages)
            return {"ok": True, "count": len(wanted)}

    def item_stop(self, kind, ids, *, page=0):
        with self.lock:
            adapter = self._require()
            wanted, _ = self._selected(adapter, kind, ids, page)
            links, packages = self._split(kind, wanted)
            adapter.set_enabled(False, links, packages)
            return {"ok": True, "count": len(wanted)}

    def item_remove(self, kind, ids, *, page=0, delete_files=False,
                    confirm_name=None, download_dir=None):
        if type(delete_files) is not bool:
            raise ValueError("delete_filesは真偽値で指定してください")
        with self.lock:
            adapter = self._require()
            wanted, items = self._selected(adapter, kind, ids, page)
            links, packages = self._split(kind, wanted)
            if not delete_files:
                adapter.remove_links(links, packages)   # list only; files stay
                return {"ok": True, "count": len(wanted), "files_deleted": False}
            if len(wanted) != 1:
                raise ValueError("ファイルも削除できるのは1件ずつです")
            item = items[0]
            if type(confirm_name) is not str or confirm_name != item["name"]:
                raise ValueError("確認用の名前が一致しません。ファイルは削除していません")
            owner = item["id"] if kind == "package" else item.get("package_id")
            if owner is None:
                raise ValueError("保存先を確認できないためファイルを削除できません")
            allowed = jd2_transfer.jd2_destination(
                download_dir, getattr(adapter, "_options", {}))
            if not inside_folder(adapter.package_save_path(int(owner)), allowed):
                raise ValueError("選択した保存先フォルダの外にあるためファイルを削除できません")
            adapter.delete_with_files(links, packages)
            return {"ok": True, "count": 1, "files_deleted": True}

    def _waiting_selected(self, adapter, ids):
        wanted = parse_ids(ids)
        known = {int(i["id"]) for i in adapter.list_linkgrabber()["items"]
                 if i["id"] is not None}
        if any(i not in known for i in wanted):
            raise ValueError("待機中の一覧にない項目です。画面を更新してください")
        return wanted

    def waiting_remove(self, ids):
        with self.lock:
            adapter = self._require()
            wanted = self._waiting_selected(adapter, ids)
            adapter.remove_waiting(wanted)
            return {"ok": True, "count": len(wanted)}

    def waiting_start(self, ids):
        with self.lock:
            adapter = self._require()
            wanted = self._waiting_selected(adapter, ids)
            adapter.move_to_downloadlist(wanted, [])
            adapter.start_downloads()
            return {"ok": True, "count": len(wanted)}

    def start_downloads(self):
        with self.lock:
            self._require().start_downloads()
            return {"started": True}

    def stop_downloads(self, *, confirmed=False):
        if confirmed is not True:
            raise ValueError("ダウンロードの停止には確認が必要です")
        with self.lock:
            self._require().stop_downloads()
            return {"stopped": True}

    def clear_finished(self, *, confirmed=False):
        """Drop finished entries from JD2's list; saved files are never touched."""
        if confirmed is not True:
            raise ValueError("一覧から消すには確認が必要です")
        with self.lock:
            self._require().cleanup_finished()
            return {"cleared": True}

    def connection_test(self):
        """Explicit read-only probe; never starts an update/download."""
        with self.lock:
            adapter = self._require()
            return {"connected": True, "revision": adapter.core_revision(),
                    "update_available": adapter.is_update_available(),
                    "can_update": getattr(adapter, "allow_update", False)}

    def update_check(self):
        with self.lock:
            adapter = self._require()
            adapter.run_update_check()
            return {"update_available": adapter.is_update_available(),
                    "revision": adapter.core_revision()}

    def restart_and_update(self, *, confirmed=False):
        if confirmed is not True:
            raise ValueError("JDownloader 2の単独更新には明示的な承認が必要です")
        with self.lock:
            adapter = self._require()
            if not adapter.is_update_available():
                raise ValueError("JDownloader 2の適用可能な更新がありません")
            self._preview = None
            # JD2's own official update engine; Hub QPKG is never touched.
            adapter.restart_and_update()
            stale = getattr(adapter, "note_preset_stale", None)
            if stale is not None:
                stale()  # JD2 may change settings while updating
            return {"started": True, "note": "JD2本体のみ更新中です。更新後に推奨設定を再確認してください"}

    def preset_preview(self):
        with self.lock:
            adapter = self._require()
            result = build_preview(adapter)
            self._preview = result if result["applicable"] else None
            return result

    def preset_reapply(self, *, confirmed=False):
        if confirmed is not True:
            raise ValueError("設定再適用にはプレビュー確認と承認が必要です")
        with self.lock:
            adapter = self._require()
            old = self._preview
            self._preview = None  # Single-use snapshot to prevent repeated writes.
            if old is None:
                raise PresetUnavailable("推奨設定のプレビューをやり直してください")
            result = apply_preset(adapter, old)
            applied = getattr(adapter, "note_preset_applied", None)
            if applied is not None:
                applied()
            return result
