"""Durable state for QnapDownloader. No progress polling writes."""
import hashlib
from contextlib import closing, contextmanager
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import threading
import time
from urllib.parse import urlsplit

DEFAULTS = {
    "download_dir": "",
    "auto_start": False,
    "auto_extract": False,
    "create_package_folders": False,
    "auto_delete": False,
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS accounts(id INTEGER PRIMARY KEY CHECK(id=1),
salt TEXT NOT NULL, password_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS bookmarks(id INTEGER PRIMARY KEY AUTOINCREMENT,
title TEXT NOT NULL,url TEXT NOT NULL UNIQUE,created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS link_intents(id INTEGER PRIMARY KEY AUTOINCREMENT,
url TEXT NOT NULL UNIQUE,state TEXT NOT NULL DEFAULT 'staged',
created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value INTEGER NOT NULL);
INSERT OR IGNORE INTO meta(key,value) VALUES('revision',0);
"""


class Store:
    def __init__(self, data_root):
        self.root = Path(data_root).resolve()
        self.directory = self.root / "data"
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = self.directory / "downloader.sqlite3"
        marker = self.directory / ".initialized"
        if marker.exists() and (not self.path.is_file() or self.path.stat().st_size == 0):
            raise RuntimeError("設定DBが消失または空になっています。初期化せずバックアップから復旧してください")
        fresh = not self.path.exists()
        self._guard = threading.RLock()
        with self.connect(initial=True) as db:
            if fresh:
                db.executescript(SCHEMA)
                for k, v in DEFAULTS.items():
                    db.execute("INSERT OR IGNORE INTO settings VALUES(?,?)",
                               (k, json.dumps(v)))
                db.commit()
            else:
                tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not {"settings", "accounts", "bookmarks", "link_intents", "meta"} <= tables:
                    raise RuntimeError("設定DBの必須テーブルが不足しています")
                if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise RuntimeError("設定DBの整合性に異常があります")
        if fresh:
            marker.write_text("v1\n", encoding="utf-8")
            os.chmod(marker, 0o600)
        # Keep one connection alive to avoid avoidable WAL/SHM create/remove churn.
        self._keeper = self._open_connection()
        self._keeper.execute("PRAGMA query_only=ON")

    def _open_connection(self, initial=False):
        db = sqlite3.connect(self.path, timeout=5, isolation_level="DEFERRED")
        try:
            db.execute("PRAGMA busy_timeout=5000")
            db.execute("PRAGMA foreign_keys=ON")
            if initial:
                db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            return db
        except Exception:
            db.close()
            raise

    @contextmanager
    def connect(self, initial=False):
        # sqlite3.Connection.__exit__ only commits/rolls back; it DOES NOT
        # close the connection. Close it for every short-lived API operation
        # to avoid FD accumulation under the threaded QNAP HTTP server.
        with closing(self._open_connection(initial=initial)) as db:
            with db:
                yield db

    def close(self):
        self._keeper.close()

    def revision(self):
        with self.connect() as db:
            return db.execute("SELECT value FROM meta WHERE key='revision'").fetchone()[0]

    def settings(self):
        with self.connect() as db:
            values = {key: json.loads(value) for key, value in
                      db.execute("SELECT key,value FROM settings")}
        return {**DEFAULTS, **values}

    def set_download_dir(self, value):
        with self._guard, self.connect() as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES('download_dir',?)",
                       (json.dumps(value),))
            db.execute("UPDATE meta SET value=value+1 WHERE key='revision'")
        return self.settings()

    @staticmethod
    def _hash(password, salt):
        return hashlib.scrypt(password.encode("utf-8"), salt=salt,
                              n=2**14, r=8, p=1, dklen=32).hex()

    def has_admin(self):
        with self.connect() as db:
            return db.execute("SELECT 1 FROM accounts WHERE id=1").fetchone() is not None

    def create_admin(self, password):
        if not isinstance(password, str) or len(password) < 12 or len(password) > 256:
            raise ValueError("パスワードは12～256文字にしてください")
        salt = secrets.token_bytes(16)
        digest = self._hash(password, salt)
        with self._guard, self.connect() as db:
            if db.execute("SELECT 1 FROM accounts WHERE id=1").fetchone():
                raise ValueError("管理者はすでに登録されています")
            db.execute("INSERT INTO accounts VALUES(1,?,?)", (salt.hex(), digest))
            db.execute("UPDATE meta SET value=value+1 WHERE key='revision'")

    def check_password(self, password):
        if not isinstance(password, str) or len(password) > 256:
            return False
        with self.connect() as db:
            row = db.execute("SELECT salt,password_hash FROM accounts WHERE id=1").fetchone()
        return bool(row) and hmac.compare_digest(
            self._hash(password, bytes.fromhex(row[0])), row[1])

    @staticmethod
    def validate_url(value):
        if not isinstance(value, str) or len(value) > 4096:
            raise ValueError("URLは4096文字以下で指定してください")
        url = urlsplit(value)
        if url.scheme != "https" or not url.hostname or url.username or url.password:
            raise ValueError("httpsの通常URLだけ登録できます")
        return value

    def bookmarks(self):
        with self.connect() as db:
            return [dict(id=row[0], title=row[1], url=row[2]) for row in
                    db.execute("SELECT id,title,url FROM bookmarks ORDER BY id DESC LIMIT 500")]

    def add_bookmark(self, title, url):
        url = self.validate_url(url)
        if not isinstance(title, str) or not 1 <= len(title.strip()) <= 200:
            raise ValueError("ブックマーク名は1～200文字です")
        with self._guard, self.connect() as db:
            cur = db.execute("INSERT OR IGNORE INTO bookmarks(title,url,created_at) VALUES(?,?,?)",
                             (title.strip(), url, time.time()))
            row = db.execute("SELECT id FROM bookmarks WHERE url=?", (url,)).fetchone()
            if cur.rowcount:
                db.execute("UPDATE meta SET value=value+1 WHERE key='revision'")
            return row[0]

    def delete_bookmark(self, identifier):
        if type(identifier) is not int or identifier <= 0:
            raise ValueError("ブックマークIDが不正です")
        with self._guard, self.connect() as db:
            deleted = db.execute("DELETE FROM bookmarks WHERE id=?", (identifier,))
            if deleted.rowcount != 1:
                raise ValueError("ブックマークが見つかりません")
            db.execute("UPDATE meta SET value=value+1 WHERE key='revision'")

    def delete_link(self, identifier):
        # Only never-sent rows may be removed. sending/sent/uncertain rows are
        # evidence of a JD2 handoff and stay until JD2 itself is checked.
        if type(identifier) is not int or identifier <= 0:
            raise ValueError("リンクIDが不正です")
        with self._guard, self.connect() as db:
            deleted = db.execute("DELETE FROM link_intents WHERE id=? AND state='staged'",
                                 (identifier,))
            if deleted.rowcount != 1:
                raise ValueError("削除できるのは未送信のリンクだけです")
            db.execute("UPDATE meta SET value=value+1 WHERE key='revision'")

    def add_link(self, url):
        # Link intents are inert in v0.1: no automatic URL fetch or JD2 execution.
        url = self.validate_url(url)
        with self._guard, self.connect() as db:
            db.execute("INSERT OR IGNORE INTO link_intents(url,created_at) VALUES(?,?)",
                       (url, time.time()))
            row = db.execute("SELECT id,state FROM link_intents WHERE url=?", (url,)).fetchone()
            return dict(id=row[0], state=row[1])

    def add_links(self, urls):
        # All-or-nothing staging of manually previewed URLs. Never sends to JD2.
        if not isinstance(urls, list) or len(urls) > 100:
            raise ValueError("リンク登録件数が上限を超えています")
        normalized = [self.validate_url(value) for value in urls]
        with self._guard, self.connect() as db:
            count = 0
            for url in normalized:
                cur = db.execute(
                    "INSERT OR IGNORE INTO link_intents(url,created_at) VALUES(?,?)",
                    (url, time.time()))
                count += cur.rowcount
            if count:
                db.execute("UPDATE meta SET value=value+1 WHERE key='revision'")
            return count

    def begin_send(self, identifier):
        if type(identifier) is not int or identifier <= 0:
            raise ValueError("リンクIDが不正です")
        with self._guard, self.connect() as db:
            row = db.execute("SELECT url,state FROM link_intents WHERE id=?",
                             (identifier,)).fetchone()
            if row is None or row[1] != "staged":
                raise ValueError("リンクは保留中ではありません。JD2で確認してください")
            changed = db.execute(
                "UPDATE link_intents SET state='sending' WHERE id=? AND state='staged'",
                (identifier,))
            if changed.rowcount != 1:
                raise ValueError("リンクの送信状態が変化しました")
            db.execute("UPDATE meta SET value=value+1 WHERE key='revision'")
            return row[0]

    def finish_send(self, identifier, state):
        if state not in ("sent", "uncertain"):
            raise ValueError("送信結果が不正です")
        with self._guard, self.connect() as db:
            updated = db.execute("UPDATE link_intents SET state=? WHERE id=? AND state='sending'",
                                 (state, identifier))
            if updated.rowcount != 1:
                raise ValueError("リンク送信の結果を確定できません")
            db.execute("UPDATE meta SET value=value+1 WHERE key='revision'")

    def links(self):
        with self.connect() as db:
            return [dict(id=r[0],url=r[1],state=r[2]) for r in
                    db.execute("SELECT id,url,state FROM link_intents ORDER BY id DESC LIMIT 300")]
