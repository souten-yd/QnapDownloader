"""Atomic update state and safe online backups, adapted from QnapSelfCare."""
import json
import os
from pathlib import Path
import sqlite3
import tempfile


def sync_dir(path):
    fd = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".atomic-", delete=False) as output:
            tmp = Path(output.name)
            os.chmod(tmp, 0o600)
            json.dump(value, output, ensure_ascii=False, allow_nan=False)
            output.flush()
            os.fsync(output.fileno())
        os.replace(tmp, path)
        sync_dir(path.parent)
    finally:
        if tmp:
            tmp.unlink(missing_ok=True)


def snapshot(source, destination):
    """SQLite online backup, includes committed WAL records."""
    source, destination = Path(source), Path(destination)
    if not source.is_file() or not source.stat().st_size:
        raise ValueError("設定DBがないため更新を中止しました")
    with sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True) as db:
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("設定DBの整合性に異常があります")
        with sqlite3.connect(destination) as target:
            db.backup(target, pages=128, sleep=0.1)
    with sqlite3.connect(destination.resolve().as_uri() + "?mode=ro", uri=True) as check:
        if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("バックアップの整合性に異常があります")
        tables = {x[0] for x in check.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"settings", "accounts", "bookmarks", "link_intents", "meta"} <= tables:
            raise ValueError("バックアップに必須テーブルがありません")
    os.chmod(destination, 0o600)
    with destination.open("rb") as output:
        os.fsync(output.fileno())
    sync_dir(destination.parent)
    return destination
