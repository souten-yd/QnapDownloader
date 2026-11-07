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
    # v0.1.54: keep one blank tab ready so a page opens faster (closed when memory is short or idle) ...
    "rb_spare_tab": True,
    # ... and leave text web fonts out of the mirror copy (system fonts show at once; icon fonts stay).
    "rb_no_webfonts": True,
    # v0.1.56: both OFF. Forget Links-tab rows once JD2 took them; clear finished/skipped JD2 entries.
    "auto_clear_links": False,
    "auto_clear_downloads": False,
    # v0.1.57: passthrough display (the page is drawn by the phone, every request goes out from the NAS). OFF by default; the
    # listener (127.0.0.1:17865) only runs while this is on. pt_public_port is the port `tailscale serve --https=` exposes it on.
    "pt_enabled": False,
    "pt_public_port": 8443,
    # v0.1.65: the most NAS-browser tabs open at once (a ceiling, not a promise: the memory guard still refuses new tabs).
    "rb_max_tabs": 3,
    # v0.1.65: the browse home's 「最近見たページ」 (kept on each device only). OFF: not shown and nothing recorded.
    "home_recent": True,
}
BOOL_OPTIONS = ("send_autostart", "block_ads", "probe_autosend", "rb_link_boxes", "rb_resident",
                "rb_spare_tab", "rb_no_webfonts", "auto_clear_links", "auto_clear_downloads", "pt_enabled",
                "home_recent")
ENUM_OPTIONS = {"rb_default_mode": ("reader", "mirror", "fast", "live")}
INT_OPTIONS = {"pt_public_port": (1, 65535), "rb_max_tabs": (1, 20)}
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
# v0.1.53: passkeys (public keys only) and sessions (SHA-256 of the session id only; the raw id exists
# only in the browser cookie). hub_text holds small Hub-owned strings: the stable WebAuthn user handle
# and the last seen Tailscale name.
PASSKEY_SCHEMA = """
CREATE TABLE IF NOT EXISTS passkeys(id INTEGER PRIMARY KEY AUTOINCREMENT,
credential_id BLOB NOT NULL UNIQUE,public_key BLOB NOT NULL,alg INTEGER NOT NULL,
sign_count INTEGER NOT NULL DEFAULT 0,label TEXT NOT NULL,rp_id TEXT NOT NULL,
transports TEXT NOT NULL DEFAULT '',created_at REAL NOT NULL,last_used_at REAL);
CREATE TABLE IF NOT EXISTS hub_text(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(id_hash TEXT PRIMARY KEY,created_at REAL NOT NULL,
last_seen REAL NOT NULL,idle_seconds INTEGER NOT NULL,absolute_seconds INTEGER NOT NULL,
method TEXT NOT NULL);
"""
MAX_PASSKEYS = 10
SESSION_SHORT = 8 * 3600                 # password login without "remember this device": fixed 8 h
SESSION_IDLE = 30 * 86400                # remembered / passkey sessions: idle timeout ...
SESSION_ABSOLUTE = 90 * 86400            # ... and absolute lifetime
SESSION_TOUCH_INTERVAL = 3600            # last_seen is written at most hourly (HDD writes)
SESSION_LIMIT = 128
SESSION_METHODS = ("password", "passkey")
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
            db.executescript(PASSKEY_SCHEMA)
            db.executescript(SITE_RULES_SCHEMA)
            self._upgrade_site_rules(db)
        self._rules = None
        self._sessions = {}      # id_hash -> row cache; every change goes through this process
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
        elif key in INT_OPTIONS:
            low, high = INT_OPTIONS[key]
            if type(value) is not int or not low <= value <= high:
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

    def remove_sent_links(self, identifier=None):
        """v0.1.56: option auto_clear_links. Delete Hub rows whose hand-off to JD2 succeeded
        (state 'sent'; never staged / sending / uncertain), one row or all. Returns the count."""
        if self.settings().get("auto_clear_links") is not True:
            return 0
        sql, args = "DELETE FROM link_intents WHERE state='sent'", ()
        if identifier is not None:
            sql, args = sql + " AND id=?", (identifier,)
        with self._guard, self.connect() as db:
            removed = db.execute(sql, args).rowcount
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

    # ---- sessions (v0.1.53) -------------------------------------------------------------------
    @staticmethod
    def _session_hash(raw):
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def csrf_token(raw):
        """CSRF token derived from the session id (HMAC), so the database never holds it."""
        return hmac.new(raw.encode("utf-8"), b"qdl-csrf-v1", hashlib.sha256).hexdigest()

    @staticmethod
    def session_expiry(created, last_seen, idle, absolute):
        return min(created + absolute, last_seen + idle)

    def session_hash(self, raw):
        return self._session_hash(raw) if isinstance(raw, str) and 1 <= len(raw) <= 200 else ""

    def create_session(self, method="password", remember=False, now=None):
        """Create a session. Returns (raw_id, csrf, max_age). Only the hash of raw_id is stored."""
        if method not in SESSION_METHODS:
            raise ValueError("unknown session method")
        now = time.time() if now is None else now
        idle, absolute = (SESSION_IDLE, SESSION_ABSOLUTE) if remember else (SESSION_SHORT, SESSION_SHORT)
        raw = secrets.token_urlsafe(32)
        digest = self._session_hash(raw)
        with self._guard, self.connect() as db:
            for row in db.execute("SELECT id_hash,created_at,last_seen,idle_seconds,absolute_seconds FROM sessions"):
                if self.session_expiry(row[1], row[2], row[3], row[4]) <= now:
                    db.execute("DELETE FROM sessions WHERE id_hash=?", (row[0],))
                    self._sessions.pop(row[0], None)
            while db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] >= SESSION_LIMIT:
                oldest = db.execute("SELECT id_hash FROM sessions ORDER BY last_seen LIMIT 1").fetchone()[0]
                db.execute("DELETE FROM sessions WHERE id_hash=?", (oldest,))
                self._sessions.pop(oldest, None)
            db.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?)", (digest, now, now, idle, absolute, method))
        self._sessions[digest] = (now, now, idle, absolute, method)
        return raw, self.csrf_token(raw), int(min(idle, absolute))

    def lookup_session(self, raw, now=None):
        """The live session for a raw cookie value, else None.

        Returns {"hash", "csrf", "method", "remember", "max_age", "touched"}. A remembered session's
        last_seen is advanced at most once per SESSION_TOUCH_INTERVAL ("touched" then asks the caller to
        re-issue the cookie with the new Max-Age); short sessions are never written.
        """
        digest = self.session_hash(raw)
        if not digest:
            return None
        now = time.time() if now is None else now
        with self._guard:
            row = self._sessions.get(digest)
            if row is None:
                with self.connect() as db:
                    found = db.execute("SELECT created_at,last_seen,idle_seconds,absolute_seconds,method "
                                       "FROM sessions WHERE id_hash=?", (digest,)).fetchone()
                if found is None:
                    return None
                row = self._sessions[digest] = tuple(found)
                if len(self._sessions) > 4 * SESSION_LIMIT:
                    self._sessions = {digest: row}
            created, last_seen, idle, absolute, method = row
            if self.session_expiry(created, last_seen, idle, absolute) <= now or now < created - 60:
                self._drop_session(digest)
                return None
            remember = absolute > SESSION_SHORT
            touched = False
            if remember and now - last_seen >= SESSION_TOUCH_INTERVAL:
                with self.connect() as db:
                    db.execute("UPDATE sessions SET last_seen=? WHERE id_hash=?", (now, digest))
                row = self._sessions[digest] = (created, now, idle, absolute, method)
                last_seen, touched = now, True
            remaining = int(self.session_expiry(created, last_seen, idle, absolute) - now)
        return {"hash": digest, "csrf": self.csrf_token(raw), "method": method, "remember": remember,
                "max_age": max(0, remaining), "touched": touched}

    def _drop_session(self, digest):
        self._sessions.pop(digest, None)
        with self.connect() as db:
            db.execute("DELETE FROM sessions WHERE id_hash=?", (digest,))

    def revoke_session(self, raw):
        digest = self.session_hash(raw)
        if digest:
            with self._guard:
                self._drop_session(digest)

    def revoke_all_sessions(self):
        with self._guard, self.connect() as db:
            count = db.execute("DELETE FROM sessions").rowcount
            self._sessions.clear()
        return count

    def session_count(self):
        with self.connect() as db:
            return db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]

    # ---- passkeys (v0.1.53): public keys only ---------------------------------------------------
    def hub_text(self, key):
        with self.connect() as db:
            row = db.execute("SELECT value FROM hub_text WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_hub_text(self, key, value):
        """Write only when the value changed (no routine HDD writes)."""
        if self.hub_text(key) == value:
            return
        with self._guard, self.connect() as db:
            db.execute("INSERT INTO hub_text VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (key, value))

    def passkey_user_handle(self):
        """Random 16-byte WebAuthn user handle, created once and then stable."""
        with self._guard:
            current = self.hub_text("passkey_user_handle")
            if current is None:
                current = secrets.token_bytes(16).hex()
                self.set_hub_text("passkey_user_handle", current)
        return bytes.fromhex(current)

    @staticmethod
    def _passkey_row(row):
        return dict(id=row[0], label=row[1], rp_id=row[2], created_at=row[3], last_used_at=row[4])

    def passkeys(self, rp_id=None):
        query = "SELECT id,label,rp_id,created_at,last_used_at FROM passkeys"
        args = ()
        if rp_id is not None:
            query += " WHERE rp_id=?"
            args = (rp_id,)
        with self.connect() as db:
            return [self._passkey_row(r) for r in db.execute(query + " ORDER BY id", args)]

    def passkey_credentials(self, rp_id):
        """[(credential_id, transports)] for excludeCredentials."""
        with self.connect() as db:
            return [(bytes(r[0]), r[1]) for r in
                    db.execute("SELECT credential_id,transports FROM passkeys WHERE rp_id=? ORDER BY id", (rp_id,))]

    @staticmethod
    def _label(value):
        if not isinstance(value, str) or not 1 <= len(value.strip()) <= 40 or \
                any(ord(ch) < 32 for ch in value):
            raise ValueError("名前は1～40文字にしてください")
        return value.strip()

    def add_passkey(self, credential_id, public_key, alg, sign_count, label, rp_id, transports=""):
        label = self._label(label)
        with self._guard, self.connect() as db:
            if db.execute("SELECT COUNT(*) FROM passkeys").fetchone()[0] >= MAX_PASSKEYS:
                raise ValueError("登録できるパスキーは%d個までです。不要なものを削除してください" % MAX_PASSKEYS)
            try:
                cur = db.execute("INSERT INTO passkeys(credential_id,public_key,alg,sign_count,label,rp_id,"
                                 "transports,created_at) VALUES(?,?,?,?,?,?,?,?)",
                                 (credential_id, public_key, alg, sign_count, label, rp_id, transports, time.time()))
            except sqlite3.IntegrityError:
                raise ValueError("このパスキーはすでに登録されています")
            return cur.lastrowid

    def passkey_by_credential(self, credential_id, rp_id):
        with self.connect() as db:
            row = db.execute("SELECT id,public_key,alg,sign_count FROM passkeys WHERE credential_id=? AND rp_id=?",
                             (credential_id, rp_id)).fetchone()
        return dict(id=row[0], public_key=bytes(row[1]), alg=row[2], sign_count=row[3]) if row else None

    def passkey_used(self, identifier, sign_count):
        with self._guard, self.connect() as db:
            db.execute("UPDATE passkeys SET last_used_at=?,sign_count=? WHERE id=?",
                       (time.time(), sign_count, identifier))

    def rename_passkey(self, identifier, label):
        if type(identifier) is not int or identifier <= 0:
            raise ValueError("パスキーIDが不正です")
        label = self._label(label)
        with self._guard, self.connect() as db:
            if db.execute("UPDATE passkeys SET label=? WHERE id=?", (label, identifier)).rowcount != 1:
                raise ValueError("パスキーが見つかりません")

    def delete_passkey(self, identifier):
        if type(identifier) is not int or identifier <= 0:
            raise ValueError("パスキーIDが不正です")
        with self._guard, self.connect() as db:
            if db.execute("DELETE FROM passkeys WHERE id=?", (identifier,)).rowcount != 1:
                raise ValueError("パスキーが見つかりません")
