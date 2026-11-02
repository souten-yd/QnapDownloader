"""Independent JD2 update and safe preset re-application contract.

JD2's own updater is NEVER replaced by the Hub QPKG updater.
The Hub-managed local engine adapter (jd2_local.py) must be connected before applying.
"""
LINKGRABBER_IFACE = "org.jdownloader.gui.views.linkgrabber.addlinksdialog.LinkgrabberSettings"
GENERAL_IFACE = "org.jdownloader.settings.GeneralSettings"
START_ALWAYS = "ALWAYS"
START_RESUME = "ONLY_IF_EXIT_WITH_RUNNING_DOWNLOADS"
START_ENUM = frozenset((START_ALWAYS, START_RESUME, "NEVER"))

# Exact interface names and keys observed on a real JD2 (config-keys output on
# the user's NAS, v0.1.16). The interface is matched by its full name, the key
# case-insensitively. "bool" entries must end up false. The "enum" entry
# (AutoDownloadStartOption) is compliant for any value except ALWAYS: resuming
# after a reboot is wanted, starting newly added links is not.
KNOWN_KEYS = {
    "linkgrabber.auto_confirm": {"kind": "bool", "interface": LINKGRABBER_IFACE,
                                 "key": "linkgrabberautoconfirmenabled"},
    "linkgrabber.auto_start": {"kind": "bool", "interface": LINKGRABBER_IFACE,
                               "key": "linkgrabberautostartenabled"},
    "extraction.auto_extract": {"kind": "bool", "interface": LINKGRABBER_IFACE,
                                "key": "autoextractionenabled"},
    "downloads.auto_start": {"kind": "enum", "interface": GENERAL_IFACE,
                             "key": "autostartdownloadoption",
                             "bad": START_ALWAYS, "target": START_RESUME},
}
REQUIRED = tuple(KNOWN_KEYS)

# Package subfolders are NOT settings here: for the Hub-owned engine they are
# enforced at send time (addLinks overwritePackagizerRules=true together with
# the explicit destinationFolder), documented as
# "packagizer.package_subfolders" / "general.package_subfolders".

LABELS = {
    "linkgrabber.auto_confirm": "リンク自動確定",
    "linkgrabber.auto_start": "追加後の自動開始",
    "extraction.auto_extract": "自動解凍",
    "downloads.auto_start": "起動時の自動開始（「常に」以外ならOK）",
}


def valid_value(key, value):
    spec = KNOWN_KEYS[key]
    if spec["kind"] == "bool":
        return type(value) is bool
    return isinstance(value, str) and value in START_ENUM


def compliant(key, value):
    spec = KNOWN_KEYS[key]
    if spec["kind"] == "bool":
        return value is False
    return value != spec["bad"]


def target_for(key, value):
    """The value the preset wants given the current one (unchanged if compliant)."""
    spec = KNOWN_KEYS[key]
    if compliant(key, value):
        return value
    return False if spec["kind"] == "bool" else spec["target"]


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
        if not valid or not valid_value(key, raw.get("current")) or not raw.get("handle"):
            missing.append(key)
            entries.append({"key": key, "label": LABELS[key], "supported": False})
            continue
        ok = compliant(key, raw["current"])
        entries.append({"key": key, "label": LABELS[key],
                        "current": raw["current"], "target": target_for(key, raw["current"]),
                        "change": not ok, "compliant": ok, "supported": True})
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
    if any(not isinstance(entry, dict) or not valid_value(entry.get("key"), entry.get("current"))
           for entry in previous_entries):
        raise PresetUnavailable("プレビューの設定値が不正です")

    current = build_preview(adapter)
    if not current["applicable"] or current["jd_revision"] != preview.get("jd_revision"):
        raise PresetChanged("JD2更新または設定変更を検出しました。プレビューをやり直してください")
    previous = {entry["key"]: entry["current"] for entry in previous_entries}
    for entry in current["entries"]:
        if entry["current"] != previous[entry["key"]] or type(entry["current"]) is not type(previous[entry["key"]]):
            raise PresetChanged("プレビュー後に設定が変更されました")

    attempted = []
    try:
        for entry in current["entries"]:
            if not entry["change"]:
                continue
            attempted.append(entry["key"])
            adapter.update_preset(entry["key"], entry["target"])
        post = build_preview(adapter)
        if (not post["applicable"] or
                post["jd_revision"] != current["jd_revision"] or
                any(not entry["compliant"] for entry in post["entries"])):
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
                    any(entry["current"] != previous[entry["key"]]
                        for entry in restored["entries"])):
                failures.append("readback")
        except Exception:
            failures.append("readback")
        if failures:
            raise PresetChanged(
                "JD2設定適用に失敗し、元の状態を確認できませんでした。"
                "更新処理を止めてJD2設定を手動で確認してください") from None
        raise
