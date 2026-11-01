"""Explicit JD2 LinkGrabber handoff; never auto-fetch or start downloads.

All paths must be configured by a NAS administrator. A host path is not
assumed to be visible to an external Docker JD2 instance. The Hub-managed
engine runs on the same host (same_host option).
"""
from pathlib import Path, PurePosixPath
import os


class TransferBlocked(ValueError):
    pass


def jd2_destination(selected, options):
    if not isinstance(selected, str) or not selected.startswith("/"):
        raise TransferBlocked("NASの保存先を選択してください")
    if options.get("same_host") is True:
        # Hub-managed engine runs on the same host: no path mapping needed.
        try:
            chosen = Path(selected).resolve(strict=True)
        except (OSError, ValueError):
            raise TransferBlocked("保存先フォルダが存在しません") from None
        if not chosen.is_dir() or not os.access(chosen, os.W_OK):
            raise TransferBlocked("NAS保存先が書き込み可能ではありません")
        return str(chosen)
    host_base = options.get("nas_root")
    target_base = options.get("jd2_root")
    if not all(isinstance(x, str) and x.startswith("/") for x in (host_base, target_base)):
        raise TransferBlocked("NASとJD2の保存先対応を管理者が設定してください")
    if ".." in PurePosixPath(target_base).parts or target_base == "/":
        raise TransferBlocked("JD2側の保存先が安全に指定されていません")
    try:
        host = Path(host_base).resolve(strict=True)
        chosen = Path(selected).resolve(strict=True)
        if not chosen.is_dir() or not host.is_dir():
            raise TransferBlocked("保存先フォルダが存在しません")
        offset = chosen.relative_to(host)
    except (OSError, ValueError):
        raise TransferBlocked("選択したNAS保存先がJD2共有ルートから外れています") from None
    if not os.access(chosen, os.W_OK):
        raise TransferBlocked("NAS保存先が書き込み可能ではありません")
    return str(PurePosixPath(target_base).joinpath(*offset.parts))


def prepare_transfer(adapter, nas_directory):
    options = getattr(adapter, "_options", {})
    if options.get("allow_link_submit") is not True:
        raise TransferBlocked("JD2リンク転送が管理者に許可されていません")
    destination = jd2_destination(nas_directory, options)
    # Important: both LinkGrabber auto-confirm and auto-start must be
    # *observed* false immediately before transfer.
    state = adapter.inspect_preset()
    for key in ("linkgrabber.auto_confirm", "linkgrabber.auto_start"):
        knob = state.get("entries", {}).get(key)
        if not isinstance(knob, dict) or knob.get("supported") is not True or knob.get("current") is not False:
            raise TransferBlocked("JD2の自動確定・自動開始OFFが確認できません")
    # Packagizer cannot be asserted flat until its real rules are inspected.
    # An explicit separate opt-in is required to allow manual link handoff
    # with pre-existing custom rules. This does not change those rules.
    if options.get("allow_unverified_packagizer") is not True:
        raise TransferBlocked("Packagizer未検証のため転送を停止しました。JD2側のルールを確認してください")
    return destination


def transfer(adapter, url, nas_directory):
    # Check preflight again in case the real JD2 settings changed.
    destination = prepare_transfer(adapter, nas_directory)
    result = adapter.add_link(url, destination)
    if result is False:
        raise TransferBlocked("JD2がリンク登録を拒否しました。JD2の状態を確認してください")
    return {"state": "sent", "note": "JD2 LinkGrabberへ自動開始OFFで送信。既存ルールは変更しません"}
