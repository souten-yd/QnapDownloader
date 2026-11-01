"""Independent JD2 update and safe preset re-application contract.

JD2's own updater is NEVER replaced by the Hub QPKG updater.
A concrete authenticated MyJDownloader adapter must be connected before applying.
"""
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
    # Never permit applying settings without a stable, actual JD2 revision.
    revision = info.get("jd_revision")
    if isinstance(revision, bool) or not isinstance(revision, (str, int)) or not str(revision).strip():
        raise PresetUnavailable("JD2 Coreリビジョンが確認できないため設定を変更できません")
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
    return {"jd_revision": revision, "entries": entries,
            "missing": missing, "applicable": not missing,
            "note": "JD2本体は独立更新。設定差分を確認してから再適用します。"}


def apply_preset(adapter, preview):
    """Apply only after an unchanged explicit preview; verify rollback too.

    An adapter may mutate a setting and then raise; record the key *before*
    invoking the adapter so recovery covers a partially completed write.
    A failed rollback is surfaced, never described as successful.
    """
    if not isinstance(preview, dict) or preview.get("applicable") is not True:
        raise PresetUnavailable("すべての必須設定が確認できるまで適用できません")
    previous_entries = preview.get("entries")
    if not isinstance(previous_entries, list) or len(previous_entries) != len(REQUIRED):
        raise PresetUnavailable("プレビューが不完全です")
    if [entry.get("key") for entry in previous_entries if isinstance(entry, dict)] != list(REQUIRED):
        raise PresetUnavailable("プレビューの設定キーが一致しません")
    if any(type(entry.get("current")) is not bool for entry in previous_entries):
        raise PresetUnavailable("プレビューの設定値が不正です")

    current = build_preview(adapter)
    if not current["applicable"] or current["jd_revision"] != preview.get("jd_revision"):
        raise PresetChanged("JD2更新または設定変更を検出しました。プレビューをやり直してください")
    previous = {entry["key"]: entry["current"] for entry in previous_entries}
    for entry in current["entries"]:
        if entry["current"] is not previous[entry["key"]]:
            raise PresetChanged("プレビュー後に設定が変更されました")

    attempted = []
    try:
        for entry in current["entries"]:
            if not entry["change"]:
                continue
            attempted.append(entry["key"])
            adapter.update_preset(entry["key"], False)
        post = build_preview(adapter)
        if (not post["applicable"] or
                post["jd_revision"] != current["jd_revision"] or
                any(entry["current"] is not False for entry in post["entries"])):
            raise PresetChanged("適用後の値またはJD2リビジョンが一致しません")
        return post
    except Exception:
        failures = []
        for key in reversed(attempted):
            try:
                adapter.update_preset(key, previous[key])
            except Exception:
                failures.append(key)
        # A successful API return alone does not prove restoration.
        try:
            restored = build_preview(adapter)
            if (restored["jd_revision"] != current["jd_revision"] or
                    not restored["applicable"] or
                    any(entry["current"] is not previous[entry["key"]]
                        for entry in restored["entries"])):
                failures.append("readback")
        except Exception:
            failures.append("readback")
        if failures:
            raise PresetChanged(
                "JD2設定適用に失敗し、元の状態を確認できませんでした。"
                "更新処理を止めてJD2設定を手動で確認してください") from None
        raise
