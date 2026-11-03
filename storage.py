"""Durable state for QnapDownloader. No progress polling writes."""
import hashlib
from contextlib import closing, contextmanager
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
from urllib.parse import urlsplit

DEFAULTS = {
    "download_dir": "",
    "auto_start": False,
    "auto_extract": False,
    "auto_delete": False,
    # v0.1.24: ask JD2 to start downloads right after the Hub sends a link.
    "send_autostart": True,
    # v0.1.24: block known ad / tracker / popunder requests in the NAS browser.
    "block_ads": True,
    # v0.1.25: a long press that finds hoster links sends them to JD2 right away.
    "probe_autosend": True,
    # v0.1.30: how a NAS browser tab is displayed unless the site has its own remembered choice
    # (reader / fast = full-page tiles / live = screencast), and the optional link-box outlines.
    "rb_default_mode": "fast",
    "rb_link_boxes": False,
    # v0.1.38: keep the NAS browser container running with no tabs (starts with the Hub).
    "rb_resident": False,
}
BOOL_OPTIONS = ("send_autostart", "block_ads", "probe_autosend", "rb_link_boxes", "rb_resident")
ENUM_OPTIONS = {"rb_default_mode": ("reader", "mirror", "fast", "live")}
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
# v0.1.13: iPhone share tokens. Only a SHA-256 of the token is stored. Created
# with IF NOT EXISTS for both new and upgraded databases.
SHARE_SCHEMA = """
CREATE TABLE IF NOT EXISTS share_tokens(id INTEGER PRIMARY KEY AUTOINCREMENT,
name TEXT NOT NULL,token_hash TEXT NOT NULL UNIQUE,created_at REAL NOT NULL,
last_used_at REAL);
"""
# v0.1.24: per-site rules of the NAS browser, keyed by the top-level page host.
# A fresh table has every column; an older one (a development build without
# block_ads) is upgraded by the guarded ALTER in Store._upgrade_site_rules.
SITE_RULES_SCHEMA = """
CREATE TABLE IF NOT EXISTS site_rules(host TEXT PRIMARY KEY,
auto_send INTEGER NOT NULL DEFAULT 0,
popup TEXT NOT NULL DEFAULT 'ask' CHECK(popup IN ('ask','allow','block')),
block_ads INTEGER NOT NULL DEFAULT 1);
"""
MAX_SITE_RULES = 200
POPUP_POLICIES = ("ask", "allow", "block")
HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
MAX_SHARE_TOKENS = 10
SHARE_TOUCH_INTERVAL = 3600


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
            db.executescript(SHARE_SCHEMA)
            db.executescript(SITE_RULES_SCHEMA)
            self._upgrade_site_rules(db)
        self._rules = None
        if fresh:
            marker.write_text("v1\n", encoding="utf-8")
            os.chmod(marker, 0o600)
        # Keep one connection alive to avoid avoidable WAL/SHM create/remove churn.
        self._keeper = self._open_connection()
        self._keeper.execute("PRAGMA query_only=ON")

    @staticmethod
    def _upgrade_site_rules(db):
        columns = {row[1] for row in db.execute("PRAGMA table_info(site_rules)")}
        if "block_ads" not in columns:
            db.execute("ALTER TABLE site_rules ADD COLUMN block_ads INTEGER NOT NULL DEFAULT 1")

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

    def set_option(self, key, value):
        """Boolean (or fixed-choice) option in the settings table; written only when it changes."""
        if key in ENUM_OPTIONS:
            if not isinstance(value, str) or value not in ENUM_OPTIONS[key]:
                raise ValueError("設定の項目または値が不正です")
        elif key not in BOOL_OPTIONS or type(value) is not bool:
            raise ValueError("設定の項目または値が不正です")
        with self._guard, self.connect() as db:
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            current = json.loads(row[0]) if row else DEFAULTS[key]
            if row is not None and current == value and type(current) is type(value):
                return self.settings()
            if row is None and value == DEFAULTS[key] and type(value) is type(DEFAULTS[key]):
                return self.settings()
            db.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (key, json.dumps(value)))
            db.execute("UPDATE meta SET value=value+1 WHERE key='revision'")
        return self.settings()

    # ---- NAS browser site rules (v0.1.24) -----------------------------------
    @staticmethod
    def valid_host(value):
        """Lower-case DNS name (no scheme, port, path or IP address), else ValueError."""
        if not isinstance(value, str):
            raise ValueError("ホスト名が不正です")
        host = value.strip().lower().rstrip(".")
        labels = host.split(".")
        if (not 3 <= len(host) <= 253 or len(labels) < 2 or
                not re.fullmatch("[a-z]{2,63}|xn--[a-z0-9-]{1,59}", labels[-1]) or
                any(HOST_LABEL.match(label) is None for label in labels)):
            raise ValueError("ホスト名はDNS名（例: example.com）で指定してください")
        return host

    def _rules_map(self):
        with self._guard:
            if self._rules is None:
                with self.connect() as db:
                    self._rules = {r[0]: {"host": r[0], "auto_send": bool(r[1]), "popup": r[2],
                                          "block_ads": bool(r[3])} for r in db.execute(
                        "SELECT host,auto_send,popup,block_ads FROM site_rules ORDER BY host")}
            return self._rules

    def site_rules(self):
        return [dict(rule) for rule in self._rules_map().values()]

    def site_rule_for(self, page_host):
        """Rule for a page host: the host itself, then its parent domains (not the TLD)."""
        if not isinstance(page_host, str) or not page_host:
            return None
        rules = self._rules_map()
        labels = page_host.lower().rstrip(".").split(".")
        for start in range(max(len(labels) - 1, 0)):
            rule = rules.get(".".join(labels[start:]))
            if rule is not None:
                return dict(rule)
        return None

    def set_site_rule(self, host, auto_send=None, popup=None, block_ads=None):
        """Create or update one rule; only given fields change, nothing is written
        when the stored values already match."""
        host = self.valid_host(host)
        for value in (auto_send, block_ads):
            if value is not None and type(value) is not bool:
                raise ValueError("ルールの値が不正です")
        if popup is not None and popup not in POPUP_POLICIES:
            raise ValueError("ポップアップの設定が不正です")
        with self._guard:
            current = self._rules_map().get(host)
            new = {"host": host,
                   "auto_send": current["auto_send"] if current else False,
                   "popup": current["popup"] if current else "ask",
                   "block_ads": current["block_ads"] if current else True}
            if auto_send is not None:
                new["auto_send"] = auto_send
            if popup is not None:
                new["popup"] = popup
            if block_ads is not None:
                new["block_ads"] = block_ads
            if current is not None and current == new:
                return dict(current)
            if current is None and len(self._rules_map()) >= MAX_SITE_RULES:
                raise ValueError("サイトごとの設定は%d件までです。不要な設定を削除してください" % MAX_SITE_RULES)
            with self.connect() as db:
                db.execute("INSERT OR REPLACE INTO site_rules(host,auto_send,popup,block_ads) "
                           "VALUES(?,?,?,?)", (host, int(new["auto_send"]), new["popup"],
                                               int(new["block_ads"])))
                db.execute("UPDATE meta SET value=value+1 WHERE key='revision'")
            self._rules = None
            return new

    def delete_site_rule(self, host):
        host = self.valid_host(host)
        with self._guard, self.connect() as db:
            if db.execute("DELETE FROM site_rules WHERE host=?", (host,)).rowcount != 1:
                raise ValueError("設定が見つかりません")
            db.execute("UPDATE meta SET value=value+1 WHERE key='revision'")
            self._rules = None

    def link_state(self, url):
        with self.connect() as db:
            row = db.execute("SELECT state FROM link_intents WHERE url=?", (url,)).fetchone()
        return row[0] if row else None

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

    def clear_links(self, include_staged=False):
        """Forget Hub link records already handed to JD2 (sent/uncertain, optionally staged).

        'sending' rows stay (a hand-off is in flight). Never touches JD2 or files.
        """
        if type(include_staged) is not bool:
            raise ValueError("include_stagedは真偽値で指定してください")
        states = ("sent", "uncertain") + (("staged",) if include_staged else ())
        with self._guard, self.connect() as db:
            removed = db.execute("DELETE FROM link_intents WHERE state IN (%s)" %
                                 ",".join("?" * len(states)), states).rowcount
            if removed:
                db.execute("UPDATE meta SET value=value+1 WHERE key='revision'")
        return removed

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

    # ---- iPhone share tokens (v0.1.13) -------------------------------------
    @staticmethod
    def _token_hash(raw):
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def share_tokens(self):
        with self.connect() as db:
            return [dict(id=r[0], name=r[1], created_at=r[2], last_used_at=r[3]) for r in
                    db.execute("SELECT id,name,created_at,last_used_at FROM share_tokens ORDER BY id")]

    def create_share_token(self, name):
        """Return the raw token exactly once; only its SHA-256 is stored."""
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 40:
            raise ValueError("端末名は1～40文字にしてください")
        raw = secrets.token_urlsafe(32)
        with self._guard, self.connect() as db:
            if db.execute("SELECT COUNT(*) FROM share_tokens").fetchone()[0] >= MAX_SHARE_TOKENS:
                raise ValueError("登録できる端末は%d台までです。不要な端末を削除してください" % MAX_SHARE_TOKENS)
            cur = db.execute("INSERT INTO share_tokens(name,token_hash,created_at) VALUES(?,?,?)",
                             (name.strip(), self._token_hash(raw), time.time()))
            return dict(id=cur.lastrowid, name=name.strip(), token=raw)

    def delete_share_token(self, identifier):
        if type(identifier) is not int or identifier <= 0:
            raise ValueError("端末IDが不正です")
        with self._guard, self.connect() as db:
            if db.execute("DELETE FROM share_tokens WHERE id=?", (identifier,)).rowcount != 1:
                raise ValueError("端末が見つかりません")

    def verify_share_token(self, raw, now=None):
        """Token id for a valid token, else None.

        Every stored hash is compared in constant time (no early exit).
        last_used_at is written at most once per SHARE_TOUCH_INTERVAL per
        token so routine use does not cause HDD writes.
        """
        if not isinstance(raw, str) or not 1 <= len(raw) <= 200:
            return None
        digest = self._token_hash(raw)
        matched = None
        with self.connect() as db:
            for ident, stored, used in db.execute(
                    "SELECT id,token_hash,last_used_at FROM share_tokens"):
                if hmac.compare_digest(digest, stored):
                    matched = (ident, used)
        if matched is None:
            return None
        now = time.time() if now is None else now
        ident, used = matched
        if used is None or now - used >= SHARE_TOUCH_INTERVAL:
            with self._guard, self.connect() as db:
                db.execute("UPDATE share_tokens SET last_used_at=? WHERE id=?", (now, ident))
        return ident
