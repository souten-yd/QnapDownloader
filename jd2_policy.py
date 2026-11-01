"""Independent JD2 update and safe preset re-application contract.

JD2's own updater is NEVER replaced by the Hub QPKG updater.
A concrete authenticated MyJDownloader adapter must be connected before applying.
"""
from dataclasses import dataclass
from typing import Any

REQUIRED = (
    "linkgrabber.auto_confirm",
    "linkgrabber.auto_start",
    "packagizer.package_subfolders",
    "general.package_subfolders",
    "extraction.auto_extract",
    "downloads.auto_start",
)

LABELS = {
    "linkgrabber.auto_confirm": "リンク自動確定",
    "linkgrabber.auto_start": "LinkGrabber自動開始",
    "packagizer.package_subfolders": "Packagizerのパッケージ別フォルダ",
    "general.package_subfolders": "保存先のパッケージ別フォルダ",
    "extraction.auto_extract": "自動解凍",
    "downloads.auto_start": "ダウンロード自動開始",
}


class PresetUnavailable(RuntimeError):
    """No trusted JD2 connection or unsupported settings."""


class PresetChanged(RuntimeError):
    """Settings changed between preview and apply."""


def build_preview(adapter):
    """Adapter inspects actual JD2 settings, never guessed Advanced Settings keys.

    inspect_preset returns a dict:
    - jd_revision: stable actual version/core revision
    - entries: map of logical ID -> dict(current, supported, writable, handle)
    'handle' is an opaque adapter-owned identifier used by update_preset.
    """
    info = adapter.inspect_preset()
    if not isinstance(info, dict) or not isinstance(info.get("entries"), dict):
        raise PresetUnavailable("JD2設定情報を取得できません")
    observed = info["entries"]
    entries = []
    missing = []
    for key in REQUIRED:
        raw = observed.get(key)
        valid = isinstance(raw, dict) and raw.get("supported") is True and raw.get("writable") is True
        if not valid or type(raw.get("current")) is not bool or not raw.get("handle"):
            missing.append(key)
            entries.append({"key": key, "label": LABELS[key], "supported": False})
            continue
        entries.append({"key": key, "label": LABELS[key],
                        "current": raw["current"], "target": False,
                        "change": raw["current"] is not False, "supported": True})
    return {"jd_revision": info.get("jd_revision"), "entries": entries,
            "missing": missing, "applicable": not missing,
            "note": "JD2本体は独立更新。設定差分を確認してから再適用します。"}


def apply_preset(adapter, preview):
    """Explicitly requested change. Abort if any knob unsupported or drifted.

    Rollback is best effort and reported, never falsely claimed. Caller must
    run while the engine is stable; updating JD2 and applying cannot overlap.
    """
    if not isinstance(preview, dict) or not preview.get("applicable"):
        raise PresetUnavailable("すべての必須設定が確認できるまで適用できません")
    current = build_preview(adapter)
    if not current["applicable"] or current["jd_revision"] != preview["jd_revision"]:
        raise PresetChanged("JD2更新または設定変更を検出しました。プレビューをやり直してください")
    previous = {x["key"]: x.get("current") for x in preview["entries"]}
    for x in current["entries"]:
        if x["current"] is not previous.get(x["key"]):
            raise PresetChanged("プレビュー後に設定が変更されました")
    changed = []
    try:
        for entry in current["entries"]:
            if not entry["change"]:
                continue
            # Backend adapter must map to a VERIFIED JD2 key and type.
            adapter.update_preset(entry["key"], False)
            changed.append(entry["key"])
        post = build_preview(adapter)
        if not post["applicable"] or any(x["current"] is not False for x in post["entries"]):
            raise PresetChanged("再読込した設定が期待値と一致しません")
    except Exception:
        for key in reversed(changed):
            try:
                adapter.update_preset(key, previous[key])
            except Exception:
                # The caller must report partially applied/rollback failure.
                raise PresetChanged("設定反映に失敗し、復旧にも失敗しました。JD2を確認してください")
        raise
    return post
