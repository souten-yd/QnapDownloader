"""QnapDownloader: authenticated QNAP downloader management and link staging."""
import argparse
import hmac
import http.cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import secrets
import sqlite3
import threading
import time
from urllib.parse import parse_qs, urlsplit

import self_update
import storage
import jd2_control
import jd2_transfer
import rapidgator_extract
import jd2_myjd
import updater

VERSION = "0.1.4"
PORT = 17864
ROOT = Path(__file__).resolve().parent
FILES = {"/": ("web/index.html", "text/html; charset=utf-8"),
         "/app.js": ("web/app.js", "text/javascript; charset=utf-8"),
         "/style.css": ("web/style.css", "text/css; charset=utf-8"),
         "/icon.svg": ("web/icon.svg", "image/svg+xml"),
         "/apple-touch-icon.png": ("web/apple-touch-icon.png", "image/png"),
         "/icon-192.png": ("web/icon-192.png", "image/png")}
SESSIONS = {}
FAILED = {}
SESSION_LOCK = threading.RLock()


def allowed_roots():
    """Only existing QNAP shared folders, never arbitrary system directories.

    /share/Download is often a QTS symlink to a mounted CACHEDEV volume.
    Resolve it only after validating that the lexical root belongs to /share.
    """
    result = []
    values = os.environ.get("DOWNLOADER_ALLOWED_ROOTS", "/share/Download").split(":")
    for value in values:
        # Roots from the environment are trusted administrator configuration,
        # not client-supplied folder names. Reject obvious system roots.
        if not value or not value.startswith("/"):
            continue
        original = Path(value)
        if ".." in original.parts:
            continue
        try:
            resolved = original.resolve(strict=True)
            system_roots = ("/etc", "/root", "/proc", "/sys", "/dev",
                            "/usr", "/bin", "/sbin", "/boot", "/run", "/var", "/lib")
            if any(resolved == Path(p) or Path(p) in resolved.parents
                   for p in system_roots):
                continue
            if resolved.is_dir() and resolved != Path("/") and resolved not in result:
                result.append(resolved)
        except (ValueError, OSError):
            continue
    return result


def checked_directory(path, roots=None):
    roots = allowed_roots() if roots is None else roots
    if not roots:
        raise ValueError("使用可能な共有フォルダがありません")
    if not isinstance(path, str) or not path.startswith("/") or "\x00" in path:
        raise ValueError("NAS上の絶対パスを指定してください")
    p = Path(path).resolve(strict=True)
    if not p.is_dir() or not any(p == root or root in p.parents for root in roots):
        raise ValueError("許可された共有フォルダの範囲外です")
    return p


def public_host_allowed(host):
    """Reject malformed or public Host values, including ambiguous IPv6 ports."""
    if not isinstance(host, str) or len(host) > 255 or any(ch.isspace() for ch in host):
        return False
    try:
        parsed = urlsplit("http://" + host)
        hostname = parsed.hostname
        port = parsed.port  # Parse/validate malformed port; it may be None.
        if (not hostname or parsed.username is not None or parsed.password is not None or
                parsed.path not in ("", "/") or parsed.query or parsed.fragment or
                (port is not None and not 0 < port < 65536)):
            return False
    except (ValueError, TypeError):
        return False
    hostname = hostname.lower()
    if hostname == "localhost":
        return True
    try:
        address = ipaddress.ip_address(hostname)
        return (address.is_private or address.is_loopback or
                address in ipaddress.ip_network("100.64.0.0/10"))
    except ValueError:
        configured = os.environ.get("DOWNLOADER_ALLOWED_HOSTS", "")
        return hostname in {item.strip().lower() for item in configured.split(",") if item.strip()}


# Only RFC1918, loopback and explicitly recognized private VPN peer addresses
# can access an HTTP-only LAN listener. No external/WAN address is accepted,
# even if it forges an allowed Host header.
PRIVATE_PEERS = tuple(ipaddress.ip_network(value) for value in (
    "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
    "100.64.0.0/10", "::1/128", "fc00::/7"))


def private_peer_allowed(value):
    try:
        peer = ipaddress.ip_address(value)
        if isinstance(peer, ipaddress.IPv6Address) and peer.ipv4_mapped:
            peer = peer.ipv4_mapped
        return any(peer in network for network in PRIVATE_PEERS)
    except (ValueError, TypeError):
        return False


class Handler(BaseHTTPRequestHandler):
    server_version = "QnapDownloader"
    sys_version = ""

    @property
    def store(self):
        return self.server.store

    def log_message(self, format, *args):
        # No URLs, cookie values, account data, or login bodies in normal logs.
        if self.path.startswith("/api/") and not self.path.startswith("/api/health"):
            return
        super().log_message(format, *args)

    def send_data(self, status, data, content_type="application/json; charset=utf-8"):
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8") if not isinstance(data, bytes) else data
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "geolocation=(),camera=(),microphone=(),payment=()")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        if getattr(self, "_cookie", None):
            self.send_header("Set-Cookie", self._cookie)
        self.end_headers()
        self.wfile.write(raw)

    def reject(self, message, code=400):
        self.send_data(code, {"error": message})

    def check_host(self):
        host = self.headers.get("Host", "")
        return (private_peer_allowed(self.client_address[0]) and
                bool(host) and public_host_allowed(host))

    def origin_ok(self):
        origin = self.headers.get("Origin")
        if not origin:
            return False
        try:
            url = urlsplit(origin)
            return url.scheme in ("http", "https") and url.netloc.lower() == self.headers.get("Host", "").lower()
        except ValueError:
            return False

    def session(self):
        jar = http.cookies.SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie", ""))
        except http.cookies.CookieError:
            return None
        sid = jar["qdl_session"].value if "qdl_session" in jar else ""
        with SESSION_LOCK:
            data = SESSIONS.get(sid)
            if data and data["until"] > time.time():
                return data
            SESSIONS.pop(sid, None)
        return None

    def authorize(self, write=False):
        data = self.session()
        if not data:
            self.reject("管理者ログインが必要です", 401)
            return False
        if write and not hmac.compare_digest(self.headers.get("X-CSRF-Token", ""), data["csrf"]):
            self.reject("CSRFトークンを確認できません", 403)
            return False
        return True

    def set_cookie(self, value, age):
        cookie = f"qdl_session={value}; Path=/; HttpOnly; SameSite=Strict; Max-Age={age}"
        # Set Secure when the app is accessed over HTTPS via a trusted reverse proxy.
        if self.server.secure_cookies:
            cookie += "; Secure"
        self._cookie = cookie

    def body(self):
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            raise ValueError("JSON形式で送信してください")
        size = int(self.headers.get("Content-Length", "-1"))
        maximum = 72000 if urlsplit(self.path).path == "/api/rapidgator/extract" else 16384
        if not 0 <= size <= maximum:
            raise ValueError("リクエストが大きすぎます")
        value = json.loads(self.rfile.read(size))
        if not isinstance(value, dict):
            raise ValueError("JSONオブジェクトが必要です")
        return value

    def do_GET(self):
        if not self.check_host():
            return self.reject("アクセス元Hostが許可されていません", 403)
        path = urlsplit(self.path).path
        if path in FILES:
            filename, typ = FILES[path]
            return self.send_data(200, (ROOT / filename).read_bytes(), typ)
        if path == "/api/health":
            return self.send_data(200, {"version": VERSION,
                         "settings_revision": self.store.revision()})
        if path == "/api/setup/status":
            return self.send_data(200, {"configured": self.store.has_admin()})
        if path == "/api/session":
            data = self.session()
            return self.send_data(200, {"authenticated": bool(data),
                             "csrf": data["csrf"] if data else None})
        if not self.authorize():
            return
        try:
            if path == "/api/status":
                return self.send_data(200, {"version": VERSION, "configured": self.store.has_admin(),
                    "settings_revision": self.store.revision(),
                    "jd2": self.server.jd2.status(),
                    "mode": "management-shell"})
            if path == "/api/settings":
                return self.send_data(200, self.store.settings())
            if path == "/api/jd2/status":
                return self.send_data(200, self.server.jd2.status())
            if path == "/api/jd2/preset/preview":
                return self.send_data(200, self.server.jd2.preset_preview())
            if path == "/api/bookmarks":
                return self.send_data(200, {"items": self.store.bookmarks()})
            if path == "/api/links":
                return self.send_data(200, {"items": self.store.links(),
                           "notice": "登録済みリンクは保留中です。JD2にはまだ送信されません"})
            if path == "/api/folders":
                query = parse_qs(urlsplit(self.path).query)
                roots = allowed_roots()
                selected = checked_directory(query["path"][0], roots) if query.get("path") else None
                if selected is None:
                    return self.send_data(200, {"items": [
                        {"path": str(p), "name": str(p), "writable": os.access(p, os.W_OK)}
                        for p in roots]})
                dirs = []
                for item in sorted(selected.iterdir(), key=lambda p: p.name.lower()):
                    if item.is_dir() and not item.name.startswith("."):
                        try:
                            child = checked_directory(str(item), roots)
                            dirs.append({"path": str(child), "name": item.name,
                                         "writable": os.access(child, os.W_OK)})
                        except (OSError, ValueError):
                            continue
                    if len(dirs) >= 200:
                        break
                return self.send_data(200, {"path": str(selected), "items": dirs,
                      "writable": os.access(selected, os.W_OK)})
            if path == "/api/update/status":
                return self.send_data(200, self.server.updates.status())
            if path == "/api/update/log":
                # Admin-authenticated, bounded tail; never expose an arbitrary
                # caller-provided file path or the updater's internal directory.
                return self.send_data(200, {"log": self.server.updates.log_tail()})
            if path == "/api/update/check":
                release = updater.latest(VERSION, "x86_64")
                return self.send_data(200, {"available": release is not None,
                  "release": {"version": release["version"], "size": release["size"]}
                            if release else None, "current": VERSION})
        except (OSError, ValueError, RuntimeError, KeyError, sqlite3.Error) as exc:
            return self.reject(str(exc))
        self.reject("APIが存在しません", 404)

    def do_POST(self):
        if not self.check_host() or not self.origin_ok():
            return self.reject("HostまたはOriginが許可されていません", 403)
        path = urlsplit(self.path).path
        try:
            body = self.body()
            if path == "/api/setup":
                # Initial setup is first-claim-only on a private LAN.
                # The account INSERT is protected by the Store lock and
                # single-row database constraint, including concurrent POSTs.
                # No one-time SSH token is read, sent or generated.
                if self.store.has_admin():
                    return self.reject("管理者は登録済みです", 403)
                self.store.create_admin(body.get("password"))
                return self.send_data(201, {"ok": True})
            if path == "/api/login":
                address = self.client_address[0]
                with SESSION_LOCK:
                    attempts = [t for t in FAILED.get(address, []) if time.time() - t < 300]
                    if len(attempts) >= 10:
                        return self.reject("ログイン試行が多すぎます", 429)
                if not self.store.check_password(body.get("password")):
                    with SESSION_LOCK:
                        # Bound the per-address retry tracker against memory abuse.
                        if address not in FAILED and len(FAILED) >= 256:
                            oldest = min(FAILED, key=lambda key: FAILED[key][-1] if FAILED[key] else 0)
                            FAILED.pop(oldest, None)
                        FAILED[address] = attempts + [time.time()]
                    return self.reject("パスワードが正しくありません", 401)
                with SESSION_LOCK:
                    FAILED.pop(address, None)
                    now = time.time()
                    for key, data in list(SESSIONS.items()):
                        if data["until"] <= now:
                            SESSIONS.pop(key, None)
                    if len(SESSIONS) >= 128:
                        oldest = min(SESSIONS, key=lambda key: SESSIONS[key]["until"])
                        SESSIONS.pop(oldest, None)
                    sid = secrets.token_urlsafe(32)
                    csrf = secrets.token_urlsafe(32)
                    SESSIONS[sid] = {"csrf": csrf, "until": now+8*3600}
                self.set_cookie(sid, 8*3600)
                return self.send_data(200, {"ok": True, "csrf": csrf})
            if not self.authorize(write=True):
                return
            if path == "/api/logout":
                jar = http.cookies.SimpleCookie()
                jar.load(self.headers.get("Cookie", ""))
                with SESSION_LOCK:
                    if "qdl_session" in jar:
                        SESSIONS.pop(jar["qdl_session"].value, None)
                self.set_cookie("", 0)
                return self.send_data(200, {"ok": True})
            if path == "/api/settings/folder":
                chosen = checked_directory(body.get("path"))
                if not os.access(chosen, os.W_OK):
                    raise ValueError("フォルダに書き込み権限がありません")
                return self.send_data(200, self.store.set_download_dir(str(chosen)))
            if path == "/api/bookmarks":
                identifier = self.store.add_bookmark(body.get("title"), body.get("url"))
                return self.send_data(201, {"id": identifier})
            if path == "/api/links":
                return self.send_data(201, self.store.add_link(body.get("url")))
            if path == "/api/rapidgator/extract":
                pasted = body.get("content")
                limit = body.get("limit", rapidgator_extract.DEFAULT_LIMIT)
                found = rapidgator_extract.extract(pasted, limit)
                evidence = rapidgator_extract.digest(pasted)
                if body.get("commit") is True:
                    previous = body.get("digest")
                    if (not isinstance(previous, str) or
                            not hmac.compare_digest(previous, evidence)):
                        raise ValueError("貼り付け内容がプレビュー後に変更されました。再確認してください")
                    stored = self.store.add_links(found)
                    return self.send_data(200, {
                        "count": len(found), "staged": stored, "saved": True,
                        "digest": evidence,
                        "note": "Hubの保留キューに保存しました。JD2には送信していません"})
                return self.send_data(200, {
                    "items": found, "count": len(found), "digest": evidence,
                    "saved": False, "limit": limit})
            if path == "/api/links/send":
                chosen = checked_directory(self.store.settings().get("download_dir"))
                ident = body.get("id")
                # Serialize with JD2 independent update and preset reapply.
                # The read-only preflight runs before the intent is marked
                # sending; there is no automatic retry after uncertain sends.
                with self.server.jd2.lock:
                    adapter = self.server.jd2._require()
                    jd2_transfer.prepare_transfer(adapter, str(chosen))
                    url = self.store.begin_send(ident)
                    try:
                        outcome = jd2_transfer.transfer(adapter, url, str(chosen))
                    except Exception:
                        self.store.finish_send(ident, "uncertain")
                        raise ValueError("JD2への送信結果が不明です。JD2で確認してください") from None
                    self.store.finish_send(ident, "sent")
                return self.send_data(200, outcome)
            if path == "/api/update/apply":
                return self.send_data(202, self.server.updates.apply(body.get("version")))
            if path == "/api/jd2/update/check":
                return self.send_data(200, self.server.jd2.update_check())
            if path == "/api/jd2/update/apply":
                return self.send_data(202, self.server.jd2.restart_and_update(
                    confirmed=body.get("confirmed") is True))
            if path == "/api/jd2/preset/apply":
                return self.send_data(200, self.server.jd2.preset_reapply(
                    confirmed=body.get("confirmed") is True))
        except (OSError, ValueError, KeyError, RuntimeError, json.JSONDecodeError, sqlite3.Error) as exc:
            return self.reject(str(exc))
        self.reject("APIが存在しません", 404)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--lan", action="store_true", help="Serve private LAN IPs; never port-forward to the Internet")
    parser.add_argument("--secure-cookie", action="store_true", help="For TLS reverse proxy deployments")
    parser.add_argument("--data-dir", default=os.environ.get("DOWNLOADER_DATA_DIR", "./runtime"))
    args = parser.parse_args()
    data = Path(args.data_dir).resolve()
    os.umask(0o077)
    store = storage.Store(data)
    server = ThreadingHTTPServer(("0.0.0.0" if args.lan else "127.0.0.1", args.port), Handler)
    server.store = store
    server.secure_cookies = args.secure_cookie
    # One-time cleanup of legacy, now-unused first-run token. Never read
    # token content and never create a replacement.
    try:
        (data / "config" / "bootstrap-token").unlink(missing_ok=True)
    except OSError:
        pass
    server.updates = self_update.UpdateManager(data, ROOT, VERSION, "x86_64", args.port)
    server.jd2 = jd2_control.JD2Controller(jd2_myjd.configured_adapter(data))
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        store.close()


if __name__ == "__main__":
    main()
