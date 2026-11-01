"""JD2 independent update coordinator using opt-in authenticated MyJD.

No unprotected LAN legacy API. Does not install or configure JD2 automatically.
"""
import threading

from jd2_policy import build_preview, apply_preset, PresetUnavailable, PresetChanged


class JD2Controller:
    def __init__(self, adapter=None):
        self.adapter = adapter
        self.lock = threading.RLock()
        self._preview = None

    def _require(self):
        if self.adapter is None:
            raise PresetUnavailable("JDownloader 2への接続が未設定です")
        return self.adapter

    def status(self):
        if self.adapter is None:
            return {"connected": False, "reason": "MyJDownloader未設定。設定画面の手順を確認してください",
                    "can_update": False, "can_preset": False}
        with self.lock:
            try:
                adapter = self._require()
                return {"connected": True, "can_update": getattr(adapter, "allow_update", True), "can_preset": True,
                        "revision": adapter.core_revision(),
                        "update_available": adapter.is_update_available()}
            except Exception as error:
                return {"connected": False, "reason": str(error),
                        "can_update": False, "can_preset": False}

    def downloads_snapshot(self, page=0):
        """Read-only; never triggers JD2 updates, queue mutations or downloads."""
        with self.lock:
            return self._require().list_downloads(page)

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
            return apply_preset(adapter, old)
