"""QnapDownloader: authenticated QNAP downloader management and link staging."""
import argparse
import hmac
import http.cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
import re
from pathlib import Path
import secrets
import signal
import sqlite3
import threading
import time
from urllib.parse import parse_qs, urlsplit

import self_update
import storage
import jd2_control
import jd2_transfer
import rapidgator_extract
import browser_safety
import runtime_diagnostics
import engine_supervisor
import remote_browser
import updater

VERSION = "0.1.21"
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
SHARE_FAILED = {}    # per-address failed share-token attempts (RAM only)
SHARE_RATE = {}      # token id -> request timestamps (RAM only)
SHARE_FAIL_LIMIT, SHARE_FAIL_WINDOW = 20, 300
SHARE_RATE_LIMIT, SHARE_RATE_WINDOW = 30, 600
SHARE_LINK_LIMIT = 50
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


LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def host_name(host):
    """Lower-cased hostname of a Host header value (port ignored), or ''."""
    try:
        return (urlsplit("http://" + host).hostname or "").lower().rstrip(".")
    except (ValueError, TypeError, AttributeError):
        return ""


def tailscale_name(hostname):
    """True for a syntactically valid MagicDNS name such as nas.tail1234.ts.net."""
    if (not isinstance(hostname, str) or len(hostname) > 253 or
            not hostname.endswith(".ts.net")):
        return False
    labels = hostname.split(".")
    return len(labels) >= 3 and all(LABEL.fullmatch(label) for label in labels)


def magicdns_short_name(hostname):
    """Single-label MagicDNS short name such as "nas" (no dots, not numeric).

    Dotted non-ts.net names are never accepted this way (DNS rebinding).
    """
    return (isinstance(hostname, str) and "." not in hostname and
            LABEL.fullmatch(hostname) is not None and not hostname.isdigit())


def loopback_peer(value):
    try:
        peer = ipaddress.ip_address(value)
        if isinstance(peer, ipaddress.IPv6Address) and peer.ipv4_mapped:
            peer = peer.ipv4_mapped
        return peer.is_loopback
    except (ValueError, TypeError):
        return False


def request_is_https(peer, headers):
    """True when this request reached the Hub through a local TLS terminator.

    Tailscale Serve connects from loopback and keeps the ts.net Host header.
    X-Forwarded-Proto is honoured ONLY from a loopback TCP peer; any other
    peer could forge it. Used for the Secure cookie flag and /api/session.
    """
    if not loopback_peer(peer):
        return False
    forwarded = (headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
    if forwarded == "https":
        return True
    name = host_name(headers.get("Host") or "")
    return name.endswith(".ts.net")


def public_host_allowed(host, peer=None):
    """Reject malformed or public Host values, including ambiguous IPv6 ports.

    *.ts.net names are accepted only for a loopback peer (Tailscale Serve) or
    a tailnet peer, never for other LAN peers.
    """
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
        bare = hostname.rstrip(".")
        if (peer is not None and (loopback_peer(peer) or tailnet_peer(peer)) and
                (tailscale_name(bare) or magicdns_short_name(bare))):
            return True
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


TAILNET_PEERS = (ipaddress.ip_network("100.64.0.0/10"),
                 ipaddress.ip_network("fd7a:115c:a1e0::/48"))


def tailnet_peer(value):
    """True when the TCP peer is a Tailscale address (WireGuard-encrypted)."""
    try:
        peer = ipaddress.ip_address(value)
        if isinstance(peer, ipaddress.IPv6Address) and peer.ipv4_mapped:
            peer = peer.ipv4_mapped
        return any(peer in network for network in TAILNET_PEERS
                   if network.version == peer.version)
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
        # No Strict-Transport-Security on purpose: the same host name is also
        # reached over plain HTTP on the LAN/tailnet, and HSTS would make
        # browsers refuse that URL.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "geolocation=(),camera=(),microphone=(),payment=()")
        # img-src data: is needed ONLY for the remote-browser screencast: the UI
        # sets one <img> to data:image/jpeg;base64,... built from /api/rb/frame
        # (Hub-controlled bytes). 'self' alone cannot display it; blob: is not
        # allowed. No other directive changes (default-src 'none', no inline).
        self.send_header("Content-Security-Policy", "default-src 'none'; img-src 'self' data:; script-src 'self'; style-src 'self'; connect-src 'self'; frame-src https:; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        if getattr(self, "_cookie", None):
            self.send_header("Set-Cookie", self._cookie)
        self.end_headers()
        self.wfile.write(raw)

    def reject(self, message, code=400):
        self.send_data(code, {"error": message})

    def check_host(self):
        host = self.headers.get("Host", "")
        return (private_peer_allowed(self.client_address[0]) and
                bool(host) and public_host_allowed(host, self.client_address[0]))

    def reject_hub_origin(self, checked_url):
        """Never allow an embedded page to share the Hub's own origin.

        The UI's compatibility mode lets a framed page keep its own origin
        (cookies/storage). That is only safe while the page can never be the
        Hub itself, so the Hub's own hostname (from the Host header, any port)
        and Tailscale MagicDNS names are refused for every mode.
        """
        target = (urlsplit(checked_url).hostname or "").lower().rstrip(".")
        try:
            own = (urlsplit("http://" + self.headers.get("Host", "")).hostname or "").lower().rstrip(".")
        except ValueError:
            own = ""
        if target == "ts.net" or target.endswith(".ts.net") or (own and target == own):
            raise ValueError("このHub自身またはTailscaleのアドレスは埋め込み表示できません")

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
        # Secure when configured, or when this request came through a local TLS
        # terminator (Tailscale Serve): see request_is_https.
        if self.server.secure_cookies or request_is_https(self.client_address[0], self.headers):
            cookie += "; Secure"
        self._cookie = cookie

    def body(self):
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            raise ValueError("JSON形式で送信してください")
        size = int(self.headers.get("Content-Length", "-1"))
        maximum = 72000 if urlsplit(self.path).path in ("/api/rapidgator/extract", "/api/share") else 16384
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
                             "csrf": data["csrf"] if data else None,
                             "tailnet": tailnet_peer(self.client_address[0]),
                             "https": request_is_https(self.client_address[0], self.headers)})
        if not self.authorize():
            return
        try:
            if path == "/api/status":
                return self.send_data(200, {"version": VERSION, "configured": self.store.has_admin(),
                    "settings_revision": self.store.revision(),
                    "jd2": self.server.jd2.status(),
                    "engine": self.server.engine.status(),
                    "mode": "management-shell"})
            if path == "/api/settings":
                return self.send_data(200, self.store.settings())
            if path == "/api/diagnostics/io":
                # Reads /proc/self/io only; no writes or background polling.
                return self.send_data(200, runtime_diagnostics.snapshot())
            if path == "/api/jd2/local-preflight":
                # No legacy API command or Docker socket interaction: TCP
                # presence alone never authorizes unauthenticated JD2 control.
                return self.send_data(200, runtime_diagnostics.local_jd2_preflight())
            if path == "/api/engine/status":
                return self.send_data(200, self.server.engine.status())
            if path == "/api/engine/config-keys":
                # Read-only diagnostic; requires a running, listener-verified engine.
                with self.server.jd2.lock:
                    return self.send_data(200, {
                        "items": self.server.jd2._require().config_keys()})
            if path == "/api/engine/log":
                # RAM ring only; secrets are scrubbed when lines are recorded.
                return self.send_data(200, {"lines": self.server.engine.log_lines()})
            if path == "/api/jd2/status":
                return self.send_data(200, self.server.jd2.status())
            if path == "/api/jd2/downloads":
                # Explicit manual refresh only. No scheduled poll of remote JD2
                # and no raw JD2 response or secret fields go to the browser.
                query = parse_qs(urlsplit(self.path).query)
                page_text = query.get("page", ["0"])[0]
                if not page_text.isdecimal() or len(page_text) > 2:
                    raise ValueError("表示ページが不正です")
                page = int(page_text)
                return self.send_data(200, self.server.jd2.downloads_snapshot(page))
            if path == "/api/jd2/preset/preview":
                return self.send_data(200, self.server.jd2.preset_preview())
            if path == "/api/bookmarks":
                return self.send_data(200, {"items": self.store.bookmarks()})
            if path == "/api/share/tokens":
                # Names and dates only; hashes and tokens are never returned.
                return self.send_data(200, {"items": self.store.share_tokens()})
            if path == "/api/links":
                return self.send_data(200, {"items": self.store.links(),
                           "notice": "追加したリンクは未送信です。JD2へは手動で送ります"})
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
            if path == "/api/rb/status":
                return self.send_data(200, self.server.rb.status())
            if path == "/api/rb/log":
                # RAM ring only (early-exit reasons from the browser container).
                return self.send_data(200, {"lines": self.server.rb.log_lines()})
            if path == "/api/rb/frame":
                query = parse_qs(urlsplit(self.path).query)
                since_text = query.get("since", ["0"])[0]
                if not since_text.isdecimal() or len(since_text) > 12:
                    raise ValueError("sinceが不正です")
                wait_text = query.get("wait", [str(int(remote_browser.FRAME_WAIT_MAX))])[0]
                if not wait_text.isdecimal() or len(wait_text) > 2 or int(wait_text) > 10:
                    raise ValueError("waitが不正です")
                return self.send_data(200, self.server.rb.frame(
                    query.get("tab", [""])[0], int(since_text), wait=int(wait_text)))
            if path == "/api/rb/links":
                query = parse_qs(urlsplit(self.path).query)
                return self.send_data(200, self.server.rb.links(query.get("tab", [""])[0]))
            if path == "/api/browser/validate":
                query = parse_qs(urlsplit(self.path).query)
                value = query.get("url", [""])[0]
                checked = browser_safety.validate(value)
                self.reject_hub_origin(checked)
                return self.send_data(200, {"url": checked,
                  "mode": "sandboxed-client-only", "server_fetched": False})
            if path == "/api/update/check":
                query = parse_qs(urlsplit(self.path).query)
                channel = query.get("channel", ["rc"])[0]
                release = updater.latest(VERSION, "x86_64", channel)
                return self.send_data(200, {"available": release is not None,
                  "release": {"version": release["version"], "size": release["size"]}
                            if release else None, "current": VERSION, "channel": channel})
        except remote_browser.TabGone as exc:
            return self.reject(str(exc), 404)
        except (OSError, ValueError, RuntimeError, KeyError, sqlite3.Error) as exc:
            return self.reject(str(exc))
        self.reject("APIが存在しません", 404)

    def share_attempt_failed(self, address):
        with SESSION_LOCK:
            now = time.time()
            if address not in SHARE_FAILED and len(SHARE_FAILED) >= 256:
                oldest = min(SHARE_FAILED, key=lambda key: SHARE_FAILED[key][-1] if SHARE_FAILED[key] else 0)
                SHARE_FAILED.pop(oldest, None)
            SHARE_FAILED[address] = [t for t in SHARE_FAILED.get(address, [])
                                     if now - t < SHARE_FAIL_WINDOW] + [now]

    def handle_share(self):
        """Bearer-token endpoint for the iOS Shortcut. Stages links only.

        Cookies are never consulted (no ambient browser credential), any
        Origin that is not the Hub's own is refused, and the result never goes
        to JD2. Failed attempts are rate limited per address.
        """
        if not self.check_host():
            return self.reject("アクセス元Hostが許可されていません", 403)
        if self.headers.get("Origin") is not None and not self.origin_ok():
            return self.reject("Originが許可されていません", 403)
        address = self.client_address[0]
        with SESSION_LOCK:
            recent = [t for t in SHARE_FAILED.get(address, []) if time.time() - t < SHARE_FAIL_WINDOW]
            if len(recent) >= SHARE_FAIL_LIMIT:
                return self.reject("認証の試行が多すぎます", 429)
        auth = self.headers.get("Authorization", "")
        scheme, _, supplied = auth.partition(" ")
        ident = None
        if scheme.lower() == "bearer" and supplied.strip():
            ident = self.store.verify_share_token(supplied.strip())
        if ident is None:
            self.share_attempt_failed(address)
            return self.reject("認証に失敗しました", 401)
        with SESSION_LOCK:
            now = time.time()
            hits = [t for t in SHARE_RATE.get(ident, []) if now - t < SHARE_RATE_WINDOW]
            if len(hits) >= SHARE_RATE_LIMIT:
                SHARE_RATE[ident] = hits
                return self.reject("送信回数が多すぎます。しばらく待ってください", 429)
            SHARE_RATE[ident] = hits + [now]
        try:
            body = self.body()
            text = body.get("text", body.get("url"))
            if not isinstance(text, str):
                raise ValueError("textを文字列で指定してください")
            found = rapidgator_extract.extract(text, SHARE_LINK_LIMIT)
            if not found:
                single = text.strip()
                if single and not re.search(r"\s", single):
                    # A lone https URL (Safari's share of the page itself).
                    found = [self.store.validate_url(single)]
            added = self.store.add_links(found) if found else 0
        except (OSError, ValueError, KeyError, RuntimeError, json.JSONDecodeError, sqlite3.Error) as exc:
            return self.reject(str(exc))
        return self.send_data(200, {"added": added, "found": len(found)})

    def do_POST(self):
        if urlsplit(self.path).path == "/api/share":
            return self.handle_share()
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
                    runtime_diagnostics.event("login_failed")
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
            if path == "/api/bookmarks/delete":
                self.store.delete_bookmark(body.get("id"))
                return self.send_data(200, {"deleted": True})
            if path == "/api/links":
                return self.send_data(201, self.store.add_link(body.get("url")))
            if path == "/api/share/tokens":
                # The raw token is in this response only; it is not recoverable.
                return self.send_data(201, self.store.create_share_token(body.get("name")))
            if path == "/api/share/tokens/delete":
                self.store.delete_share_token(body.get("id"))
                with SESSION_LOCK:
                    SHARE_RATE.pop(body.get("id"), None)
                return self.send_data(200, {"deleted": True})
            if path == "/api/links/delete":
                self.store.delete_link(body.get("id"))
                return self.send_data(200, {"deleted": True})
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
                        "note": "追加したリンクに保存しました。JD2には送信していません"})
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
                return self.send_data(202, self.server.updates.apply(body.get("version"), channel=body.get("channel", "rc")))
            if path == "/api/engine/setup":
                if body.get("confirmed") is not True:
                    raise ValueError("初期設定の開始には確認が必要です")
                # The destination must still be inside the allowed shares.
                checked_directory(self.store.settings().get("download_dir"))
                requested = body.get("mode")
                if requested not in (None, "container", "native"):
                    raise ValueError("modeはcontainerまたはnativeで指定してください")
                try:
                    self.server.engine.start_setup(requested)
                except engine_supervisor.SetupBusy as exc:
                    return self.reject(str(exc), 409)
                return self.send_data(202, {"started": True})
            if path == "/api/engine/native/remove":
                if body.get("confirmed") is not True:
                    raise ValueError("従来方式のJava資産の削除には確認が必要です")
                return self.send_data(200, self.server.engine.remove_native_leftovers())
            if path == "/api/rb/setup":
                if body.get("confirmed") is not True:
                    raise ValueError("初期設定の開始には確認が必要です")
                try:
                    self.server.rb.start_setup()
                except remote_browser.SetupBusy as exc:
                    return self.reject(str(exc), 409)
                return self.send_data(202, {"started": True})
            if path == "/api/rb/stop":
                if body.get("confirmed") is not True:
                    raise ValueError("NASブラウザの停止には確認が必要です")
                return self.send_data(200, {"stopped": self.server.rb.stop()})
            if path == "/api/rb/tabs":
                checked = browser_safety.validate(body.get("url"))
                self.reject_hub_origin(checked)
                return self.send_data(201, self.server.rb.open_tab(checked))
            if path == "/api/rb/tabs/close":
                return self.send_data(200, {"closed": self.server.rb.close_tab(body.get("id"))})
            if path == "/api/rb/navigate":
                checked = browser_safety.validate(body.get("url"))
                self.reject_hub_origin(checked)
                return self.send_data(200, self.server.rb.navigate(body.get("tab"), checked))
            if path == "/api/rb/input":
                return self.send_data(200, self.server.rb.send_input(body.get("tab"), body))
            if path == "/api/engine/start":
                self.server.engine.start()
                return self.send_data(202, {"started": True})
            if path == "/api/engine/stop":
                if body.get("confirmed") is not True:
                    raise ValueError("エンジンの停止には確認が必要です")
                return self.send_data(200, self.server.engine.stop())
            if path == "/api/engine/autostart":
                return self.send_data(200, {"autostart": self.server.engine.set_autostart(
                    body.get("enabled"))})
            if path == "/api/jd2/connect/test":
                return self.send_data(200, self.server.jd2.connection_test())
            if path == "/api/jd2/update/check":
                return self.send_data(200, self.server.jd2.update_check())
            if path == "/api/jd2/update/apply":
                return self.send_data(202, self.server.jd2.restart_and_update(
                    confirmed=body.get("confirmed") is True))
            if path == "/api/jd2/preset/apply":
                return self.send_data(200, self.server.jd2.preset_reapply(
                    confirmed=body.get("confirmed") is True))
        except remote_browser.TabGone as exc:
            return self.reject(str(exc), 404)
        except (OSError, ValueError, KeyError, RuntimeError, json.JSONDecodeError, sqlite3.Error) as exc:
            return self.reject(str(exc))
        self.reject("APIが存在しません", 404)



def remove_legacy_setup_token(data):
    """Remove only obsolete one-time setup token on upgrade.

    Not used as an authentication factor. Unlink does not follow symlinks;
    failures do not prevent the Hub from starting.
    """
    try:
        (Path(data) / "config" / "bootstrap-token").unlink(missing_ok=True)
        return True
    except OSError:
        return False


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
    remove_legacy_setup_token(data)
    server.updates = self_update.UpdateManager(data, ROOT, VERSION, "x86_64", args.port)
    server.engine = engine_supervisor.EngineManager(data, store)
    server.jd2 = jd2_control.JD2Controller(server.engine.adapter)
    server.rb = remote_browser.RemoteBrowser(
        server.engine.runtime, ROOT / "container" / "browser")
    # A labelled browser container surviving a Hub crash would hold memory.
    threading.Thread(target=server.rb.startup_cleanup, daemon=True).start()
    runtime_diagnostics.event("web_started")

    def request_shutdown(signum, frame):
        # server.shutdown() blocks until serve_forever exits: run it off the
        # signal-handling (main) thread to avoid a deadlock.
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)
    # Non-blocking: adopts a surviving engine or autostarts it in a thread.
    server.engine.startup()
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        server.rb.shutdown()
        server.engine.shutdown()
        store.close()


if __name__ == "__main__":
    main()
