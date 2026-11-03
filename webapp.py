"""QnapDownloader: authenticated QNAP downloader management and link staging."""
import argparse
import hmac
import http.cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import gzip
import json
import os
import re
from pathlib import Path
import secrets
import signal
import sqlite3
import threading
import time
from urllib.parse import parse_qs, quote, urlsplit

import self_update
import storage
import jd2_accounts
import jd2_control
import jd2_transfer
import rapidgator_extract
import browser_safety
import container_runtime
import debuglog
import diskio
import runtime_diagnostics
import engine_supervisor
import rb_mirror
import remote_browser
import selftest
import state_hub
import ttlcache
import updater

VERSION = "0.1.48"
PORT = 17864
ROOT = Path(__file__).resolve().parent
FILES = {"/": ("web/index.html", "text/html; charset=utf-8"),
         "/app.js": ("web/app.js", "text/javascript; charset=utf-8"),
         "/style.css": ("web/style.css", "text/css; charset=utf-8"),
         "/icon.svg": ("web/icon.svg", "image/svg+xml"),
         "/apple-touch-icon.png": ("web/apple-touch-icon.png", "image/png"),
         "/icon-192.png": ("web/icon-192.png", "image/png")}
# No frame-src https: since v0.1.35 (the in-phone iframe browser is gone). v0.1.39 adds frame-src 'self'
# for ONE thing: the mirror display's sandboxed iframe, whose document is /api/rb/mirror/doc of this very
# origin (a srcdoc iframe would inherit this policy and lose its inline styles; see docs/browser-shell-ui.md).
# That response carries its own, stricter policy (rb_mirror.DOC_CSP_HEADER). External pages are never framed.
CSP = ("default-src 'none'; img-src 'self' data: blob:; script-src 'self'; style-src 'self'; "
       "connect-src 'self'; frame-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
SESSIONS = {}
FAILED = {}
SHARE_FAILED = {}    # per-address failed share-token attempts (RAM only)
SHARE_RATE = {}      # token id -> request timestamps (RAM only)
SHARE_FAIL_LIMIT, SHARE_FAIL_WINDOW = 20, 300
SHARE_RATE_LIMIT, SHARE_RATE_WINDOW = 30, 600
SHARE_LINK_LIMIT = 50
SESSION_LOCK = threading.RLock()
CREDENTIALS_TRANSPORT_ERROR = "パスワードはHTTPSまたはTailscale経由でのみ送信できます"
ACCOUNT_BODY_MAX = 4096
DEBUG_BODY_MAX = 320000     # 100 entries of at most ~3 KB each
DEBUG_SKIP_HTTP = ("/api/debug/", "/api/selftest/status")
HTTP_LOG_GATE = 1.0         # seconds between debug entries of the same method+path


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


def credentials_transport_ok(peer, headers):
    """Premium-account credentials may only arrive over an encrypted hop.

    HTTPS through a trusted local TLS terminator (request_is_https) or a
    Tailscale peer (WireGuard). Plain-HTTP LAN peers are refused.
    """
    return request_is_https(peer, headers) or tailnet_peer(peer)


class AlreadySent(ValueError):
    """The link is already being or has been handed to JD2 (never sent twice)."""


def send_link(server, ident=None, url=None, dedupe=False):
    """Send one staged link to JD2 (the only send path).

    With `url` (remote-browser intercepts) the link is staged first and the
    engine must be able to send. Serialized with JD2 independent update and
    preset reapply. The read-only preflight runs before the intent is marked
    sending; there is no automatic retry after uncertain sends. With `dedupe`
    a link that was already sent raises AlreadySent instead of ValueError text.
    The Hub setting send_autostart decides whether JD2 starts it right away.
    """
    store = server.store
    settings = store.settings()
    chosen = checked_directory(settings.get("download_dir"))
    autostart = settings.get("send_autostart", True) is True
    with server.jd2.lock:
        adapter = server.jd2._require()
        if url is not None and getattr(adapter, "can_send", False) is not True:
            raise ValueError("JD2へ送信できる状態ではありません（エンジンの状態を確認してください）")
        if url is not None and dedupe and store.link_state(url) in ("sending", "sent", "uncertain"):
            raise AlreadySent("このリンクはすでにJD2へ送信済みです")
        jd2_transfer.prepare_transfer(adapter, str(chosen))
        if url is not None:
            ident = store.add_link(url)["id"]
        url = store.begin_send(ident)
        try:
            if autostart:
                outcome = jd2_transfer.transfer(adapter, url, str(chosen), autostart=True)
            else:
                outcome = jd2_transfer.transfer(adapter, url, str(chosen))
        except Exception:
            store.finish_send(ident, "uncertain")
            raise ValueError("JD2への送信結果が不明です。JD2で確認してください") from None
        store.finish_send(ident, "sent")
    return outcome


def auto_send_link(server, url):
    """Site rule "auto_send": "sent", "dup", or None when sending is not possible now
    (the viewer then gets the normal sheet). Other failures raise."""
    try:
        with server.jd2.lock:
            adapter = server.jd2._require()
        if getattr(adapter, "can_send", False) is not True:
            return None
    except Exception:
        return None
    try:
        send_link(server, url=url, dedupe=True)
    except AlreadySent:
        return "dup"
    return "sent"


PROBE_TTL = 300
PROBE_MAX_LINKS = 50
PROBE_MAX_TABS = 16
PROBE_KEEP_PER_TAB = 20
_PROBE_LOCK = threading.Lock()


def remember_probe(server, tab, links, now=None):
    """Keep probe results per probe id in RAM (last 20 per tab, <= 50 links, 5 minutes).

    Returns the opaque probe id, or None when there are no links. The send
    endpoint only ever uses what is stored here, never client-supplied URLs.
    """
    now = time.time() if now is None else now
    with _PROBE_LOCK:
        store = server.__dict__.setdefault("probe_results", {})
        for key in list(store):
            for pid in [k for k, v in store[key].items() if now - v["at"] > PROBE_TTL]:
                del store[key][pid]
            if not store[key]:
                del store[key]
        if not links:
            return None
        if tab not in store and len(store) >= PROBE_MAX_TABS:
            oldest = min(store, key=lambda k: max(v["at"] for v in store[k].values()))
            del store[oldest]
        results = store.setdefault(tab, {})
        probe_id = secrets.token_urlsafe(12)
        results[probe_id] = {"links": list(links)[:PROBE_MAX_LINKS], "at": now}
        while len(results) > PROBE_KEEP_PER_TAB:
            del results[next(iter(results))]
        return probe_id


def send_probe_links(server, tab, probe_id, now=None):
    """Send the stored links of one probe to JD2 (deduped); never takes URLs from the client."""
    now = time.time() if now is None else now
    links = []
    with _PROBE_LOCK:
        results = server.__dict__.get("probe_results", {}).get(tab) if isinstance(tab, str) else None
        if results is not None and isinstance(probe_id, str):
            for stored_id, entry in results.items():
                if hmac.compare_digest(stored_id.encode(), probe_id.encode()) and now - entry["at"] <= PROBE_TTL:
                    links = list(entry["links"])
                    break
    if not links:
        raise ValueError("確認結果の有効期限が切れました。もう一度長押ししてください")
    sent = skipped = 0
    error = None
    for url in links:
        try:
            send_link(server, url=url, dedupe=True)
            sent += 1
        except AlreadySent:
            skipped += 1
        except ValueError as exc:
            error = str(exc)
            break
    autostart = server.store.settings().get("send_autostart", True) is True
    return {"sent": sent, "skipped": skipped, "total": len(links), "error": error,
            "autostart": autostart}


def wire_remote_browser(server):
    """Give the NAS browser its site rules (DB) and the existing JD2 send path."""
    server.rb.rules = server.store
    server.rb.auto_send = lambda url: auto_send_link(server, url)


def boot_remote_browser(server):
    """Hub start: drop a leftover browser container, then start the resident one if the switch is on."""
    try:
        server.rb.startup_cleanup()
    finally:
        try:
            server.rb.set_resident(server.store.settings().get("rb_resident") is True)
        except Exception:
            pass


def server_cache(server):
    """Per-server short-TTL single-flight cache (engine/JD2/NAS browser reads, v0.1.33)."""
    cache = getattr(server, "cache", None)
    if cache is None:
        with SESSION_LOCK:
            cache = getattr(server, "cache", None)
            if cache is None:
                cache = server.cache = ttlcache.SingleFlightCache()
    return cache


def server_diskio(server):
    """Per-server disk write meter (v0.1.35); created on first use, samples only while asked."""
    meter = getattr(server, "diskio", None)
    if meter is None:
        with SESSION_LOCK:
            meter = getattr(server, "diskio", None)
            if meter is None:
                engine = getattr(server, "engine", None)
                runtime = getattr(engine, "runtime", None)
                meter = server.diskio = diskio.DiskIO(runtime, (
                    ("jd2", "JD2コンテナ", container_runtime.JD2_NAME),
                    ("browser", "NASブラウザコンテナ", container_runtime.BROWSER_NAME)))
    return meter


def server_selftest(server):
    """Per-server self-test runner (v0.1.44); created on first use. Results live in RAM only."""
    runner = getattr(server, "selftest", None)
    if runner is None:
        with SESSION_LOCK:
            runner = getattr(server, "selftest", None)
            if runner is None:
                runner = server.selftest = selftest.SelfTest(
                    server.rb, engine=getattr(server, "engine", None), jd2=getattr(server, "jd2", None),
                    version=VERSION, data_dir=getattr(server, "data_dir", None))
    return runner


def server_state_hub(server):
    hub = getattr(server, "state_hub", None)
    if hub is None:
        with SESSION_LOCK:
            hub = getattr(server, "state_hub", None)
            if hub is None:
                hub = server.state_hub = state_hub.StateHub(server, VERSION, server_cache(server))
    return hub


# Writes under these prefixes change nothing the cached reads show (screen input only).
RB_QUIET_POSTS = ("/api/debug/log", "/api/diag/diskio/start", "/api/diag/diskio/stop", "/api/rb/input", "/api/rb/viewport", "/api/rb/snapshot", "/api/rb/snapshot/release",
                  "/api/rb/tap-page", "/api/rb/reader", "/api/rb/probe", "/api/rb/intercepts/open",
                  "/api/rb/mirror", "/api/rb/mirror/click", "/api/rb/mirror/release",
                  "/api/selftest/start", "/api/selftest/cancel", "/api/selftest/client")


GZIP_MIN = 1024


def accepts_gzip(header):
    """True when an Accept-Encoding header allows gzip (q=0 forbids it)."""
    for part in (header or "").split(","):
        name, _, params = part.strip().partition(";")
        if name.strip().lower() in ("gzip", "x-gzip"):
            match = re.search(r"q\s*=\s*([0-9.]+)", params)
            try:
                return float(match.group(1)) > 0 if match else True
            except ValueError:
                return False
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

    def security_headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "geolocation=(),camera=(),microphone=(),payment=()")
        self.send_header("Content-Security-Policy", CSP)

    def stream_sse(self, tab_id):
        """Live display as an event stream (v0.1.46). Runs until the viewer leaves (or after 10 minutes)."""
        started = []

        def write(part):
            if not started:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("X-Accel-Buffering", "no")
                self.send_header("Connection", "close")
                self.security_headers()
                self.end_headers()
                started.append(True)
            self.wfile.write(part)
            self.wfile.flush()

        self.connection.settimeout(10)
        self.close_connection = True
        try:
            self.server.rb.stream(tab_id, write, sse=True)
        except OSError:
            pass
        if not started:
            raise remote_browser.TabGone("このタブはすでに閉じられています")

    def stream_mjpeg(self, tab_id):
        """Live display push stream (multipart/x-mixed-replace). Runs until the viewer leaves."""
        started = []

        def write(part):
            if not started:
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=" + remote_browser.STREAM_BOUNDARY)
                self.send_header("Connection", "close")
                self.security_headers()
                self.end_headers()
                started.append(True)
            self.wfile.write(part)
            self.wfile.flush()

        self.connection.settimeout(10)        # a stalled viewer must not hold the thread
        self.close_connection = True
        try:
            self.server.rb.stream(tab_id, write)
        except OSError:
            pass
        if not started:
            # Ended before the first part (tab closed or container stopped).
            raise remote_browser.TabGone("このタブはすでに閉じられています")

    def send_data(self, status, data, content_type="application/json; charset=utf-8", extra=None,
                  compress=False, csp=None, frame="DENY"):
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8") if not isinstance(data, bytes) else data
        encoded = False
        if compress and len(raw) > GZIP_MIN and accepts_gzip(self.headers.get("Accept-Encoding")):
            raw = gzip.compress(raw, 5)       # v0.1.39: only the mirror endpoints ask for this
            encoded = True
        self.send_response(status)
        if status != 204:       # a 204 has neither a body nor a Content-Length
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            if encoded:
                self.send_header("Content-Encoding", "gzip")
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        if encoded or compress:
            self.send_header("Vary", "Accept-Encoding")
        if "Cache-Control" not in (extra or {}):
            self.send_header("Cache-Control", "no-store")
        # No Strict-Transport-Security on purpose: the same host name is also
        # reached over plain HTTP on the LAN/tailnet, and HSTS would make
        # browsers refuse that URL.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", frame)
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "geolocation=(),camera=(),microphone=(),payment=()")
        # img-src blob: is needed ONLY for the remote-browser screencast: the UI
        # fetches /api/rb/frame.jpg (Hub-controlled JPEG bytes), wraps them in a
        # Blob and sets one <img> to URL.createObjectURL(blob). 'self' alone
        # cannot display a blob: URL. data: is kept unchanged (security invariant); the
        # JSON frame endpoint that used it was removed in v0.1.33. No other directive
        # changes (default-src 'none', no inline). Mirror documents and resources pass their own `csp`.
        self.send_header("Content-Security-Policy", csp or CSP)
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
        """Never let the NAS browser be pointed at the Hub itself or at a tailnet name.

        The Hub's own hostname (from the Host header, any port) and Tailscale MagicDNS
        names are refused (SSRF guard; the NAS browser container could otherwise reach them).
        """
        target = (urlsplit(checked_url).hostname or "").lower().rstrip(".")
        try:
            own = (urlsplit("http://" + self.headers.get("Host", "")).hostname or "").lower().rstrip(".")
        except ValueError:
            own = ""
        if target == "ts.net" or target.endswith(".ts.net") or (own and target == own):
            raise ValueError("このHub自身またはTailscaleのアドレスはNASブラウザで開けません")

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
        path = urlsplit(self.path).path
        maximum = 72000 if path in ("/api/rapidgator/extract", "/api/share") else 16384
        if path.startswith("/api/accounts/"):
            maximum = ACCOUNT_BODY_MAX
        if path == "/api/debug/log":
            maximum = DEBUG_BODY_MAX
        if path == "/api/selftest/client":
            maximum = selftest.CLIENT_BODY_MAX
        if not 0 <= size <= maximum:
            raise ValueError("リクエストが大きすぎます")
        value = json.loads(self.rfile.read(size))
        if not isinstance(value, dict):
            raise ValueError("JSONオブジェクトが必要です")
        return value

    @property
    def accounts(self):
        service = getattr(self.server, "accounts", None)
        if service is None:
            with SESSION_LOCK:
                service = getattr(self.server, "accounts", None)
                if service is None:
                    service = self.server.accounts = jd2_accounts.AccountService()
        return service

    @staticmethod
    def body_ids(body):
        """`ids` (list) or a single `id`; the controller validates every entry."""
        if "ids" in body:
            return body["ids"]
        return [body.get("id")]

    def item_action(self, action, body):
        jd2, page = self.server.jd2, body.get("page", 0)
        kind, ids = body.get("kind"), self.body_ids(body)
        if action == "start":
            return jd2.item_start(kind, ids, page=page)
        if action == "stop":
            return jd2.item_stop(kind, ids, page=page)
        folder = None
        if body.get("delete_files") is True:
            folder = str(checked_directory(self.store.settings().get("download_dir")))
        return jd2.item_remove(kind, ids, page=page,
                               delete_files=body.get("delete_files", False),
                               confirm_name=body.get("confirm_name"), download_dir=folder)

    def send_state(self):
        """GET /api/state?rev=N&wait=S[&full=1]: the unified client state (long-poll)."""
        query = parse_qs(urlsplit(self.path).query)
        rev_text = query.get("rev", [None])[0]
        if rev_text is not None and (not rev_text.isdecimal() or len(rev_text) > 12):
            raise ValueError("revが不正です")
        wait_text = query.get("wait", [str(int(state_hub.WAIT_MAX))])[0]
        if not wait_text.isdecimal() or len(wait_text) > 2 or int(wait_text) > state_hub.WAIT_MAX:
            raise ValueError("waitが不正です")
        rev, state = server_state_hub(self.server).poll(
            None if rev_text is None else int(rev_text), int(wait_text))
        data = dict(state, rev=rev, https=request_is_https(self.client_address[0], self.headers),
                    tailnet=tailnet_peer(self.client_address[0]))
        debug = dict(data.get("debug") or {})
        if debug.get("enabled"):
            debug["remaining"] = max(0, int((debug.get("until") or 0) - time.time()))
        data["debug"] = debug
        if query.get("full", ["0"])[0] == "1":
            # First load only: the data the home view needs, in the same round trip.
            data["settings"] = self.store.settings()
            data["bookmarks"] = self.store.bookmarks()
        return self.send_data(200, data)

    def cached(self, key, ttl, loader):
        return server_cache(self.server).get(key, ttl, loader)

    def send_response(self, code, message=None):
        self._status = code
        super().send_response(code, message)

    def _log_http(self, method, started):
        path = urlsplit(self.path).path
        if not path.startswith("/api/") or path.startswith(DEBUG_SKIP_HTTP) or not debuglog.enabled("http"):
            return
        now = time.monotonic()
        gate = self.server.__dict__.setdefault("http_log_gate", {})
        key = method + path
        last, skipped = gate.get(key, (0.0, 0))
        if now - last < HTTP_LOG_GATE:
            gate[key] = (last, skipped + 1)
            return
        if len(gate) > 200:
            gate.clear()
        gate[key] = (now, 0)
        data = {"method": method, "path": path, "status": getattr(self, "_status", 0),
                "ms": int((now - started) * 1000)}
        if skipped:
            data["skipped"] = skipped
        debuglog.log("http", "debug", "%s %s %s" % (method, path, data["status"]), data)

    def do_GET(self):
        if not debuglog.LOG._enabled:
            return self.handle_get()
        started = time.monotonic()
        try:
            return self.handle_get()
        finally:
            self._log_http("GET", started)

    def handle_get(self):
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
                    "jd2": self.cached("jd2:status", state_hub.COUNTS_TTL, self.server.jd2.status),
                    "engine": self.cached("engine:status", state_hub.ENGINE_TTL, self.server.engine.status),
                    "mode": "management-shell"})
            if path == "/api/state":
                return self.send_state()
            if path == "/api/settings":
                return self.send_data(200, self.store.settings())
            if path == "/api/diag/diskio":
                return self.send_data(200, server_diskio(self.server).snapshot())
            if path == "/api/debug/log":
                query = parse_qs(urlsplit(self.path).query)
                since_text = query.get("since", ["0"])[0]
                limit_text = query.get("limit", ["200"])[0]
                if (not since_text.isdecimal() or len(since_text) > 12 or
                        not limit_text.isdecimal() or len(limit_text) > 4 or not 1 <= int(limit_text) <= 1000):
                    raise ValueError("sinceまたはlimitが不正です")
                result = debuglog.LOG.entries(int(since_text), int(limit_text))
                result["status"] = debuglog.LOG.status()
                return self.send_data(200, result)
            if path == "/api/selftest/status":
                query = parse_qs(urlsplit(self.path).query)
                since_text = query.get("since", [None])[0]
                wait_text = query.get("wait", ["0"])[0]
                if ((since_text is not None and (not since_text.isdecimal() or len(since_text) > 12)) or
                        not wait_text.isdecimal() or len(wait_text) > 2 or int(wait_text) > int(selftest.STATUS_WAIT_MAX)):
                    raise ValueError("sinceまたはwaitが不正です")
                return self.send_data(200, server_selftest(self.server).status(
                    None if since_text is None else int(since_text), int(wait_text)))
            if path == "/api/selftest/results":
                query = parse_qs(urlsplit(self.path).query)
                ident = query.get("id", [None])[0]
                runner = server_selftest(self.server)
                if ident is None:
                    return self.send_data(200, {"items": runner.history()})
                if not selftest.RUN_ID.match(ident):
                    raise ValueError("idが不正です")
                result = runner.result(ident)
                if result is None:
                    return self.reject("この結果は見つかりません（HubのRAMには直近5件だけ残ります）", 404)
                return self.send_data(200, result)
            if path == "/api/selftest/report":
                query = parse_qs(urlsplit(self.path).query)
                ident = query.get("id", [None])[0]
                if ident is not None and not selftest.RUN_ID.match(ident):
                    raise ValueError("idが不正です")
                report = server_selftest(self.server).report(ident, query.get("urls", ["0"])[0] == "1")
                if report is None:
                    return self.reject("この結果は見つかりません（HubのRAMには直近5件だけ残ります）", 404)
                return self.send_data(200, report)
            if path == "/api/diagnostics/io":
                # Reads /proc/self/io only; no writes or background polling.
                return self.send_data(200, runtime_diagnostics.snapshot())
            if path == "/api/engine/status":
                return self.send_data(200, self.cached("engine:status", state_hub.ENGINE_TTL,
                                                       self.server.engine.status))
            if path == "/api/engine/config-keys":
                # Read-only diagnostic; requires a running, listener-verified engine.
                with self.server.jd2.lock:
                    return self.send_data(200, {
                        "items": self.server.jd2._require().config_keys()})
            if path == "/api/engine/log":
                # RAM ring only; secrets are scrubbed when lines are recorded.
                return self.send_data(200, {"lines": self.server.engine.log_lines()})
            if path == "/api/jd2/status":
                return self.send_data(200, self.cached("jd2:status", state_hub.COUNTS_TTL,
                                                       self.server.jd2.status))
            if path == "/api/accounts/schema":
                # Key names and JSON type names only (no values), for confirming
                # the real accountsV2 field names on the NAS.
                return self.send_data(200, self.accounts.schema(self.server.jd2))
            if path in ("/api/accounts", "/api/accounts/hosts"):
                # Masked, normalized accounts only (reads work on any authenticated
                # session). RAM caches: 30 s accounts, 10 min hosters; ?refresh=1 skips them.
                query = parse_qs(urlsplit(self.path).query)
                refresh = query.get("refresh", ["0"])[0] == "1"
                if path == "/api/accounts":
                    return self.send_data(200, self.accounts.list(self.server.jd2, refresh))
                return self.send_data(200, self.accounts.hosts(self.server.jd2, refresh))
            if path == "/api/jd2/downloads":
                # Explicit manual refresh only. No scheduled poll of remote JD2
                # and no raw JD2 response or secret fields go to the browser.
                query = parse_qs(urlsplit(self.path).query)
                page_text = query.get("page", ["0"])[0]
                if not page_text.isdecimal() or len(page_text) > 2:
                    raise ValueError("表示ページが不正です")
                page = int(page_text)
                return self.send_data(200, self.cached(
                    "jd2:downloads:%d" % page, 2.0, lambda: self.server.jd2.downloads_snapshot(
                        page, self.store.settings().get("download_dir"))))
            if path == "/api/jd2/linkgrabber":
                # Names and ids of links JD2 holds but has not started (max 50).
                return self.send_data(200, self.cached("jd2:linkgrabber", 2.0,
                                                       self.server.jd2.linkgrabber_snapshot))
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
                return self.send_data(200, self.cached(
                    "rb:status:fresh", state_hub.RB_TTL, lambda: self.server.rb.status(fresh_memory=True)))
            if path == "/api/rb/log":
                # RAM ring only (early-exit reasons from the browser container).
                return self.send_data(200, {"lines": self.server.rb.log_lines()})
            if path == "/api/rb/frame.jpg":
                query = parse_qs(urlsplit(self.path).query)
                since_text = query.get("since", ["0"])[0]
                if not since_text.isdecimal() or len(since_text) > 12:
                    raise ValueError("sinceが不正です")
                wait_text = query.get("wait", [str(int(remote_browser.FRAME_WAIT_MAX))])[0]
                if not wait_text.isdecimal() or len(wait_text) > 2 or int(wait_text) > 10:
                    raise ValueError("waitが不正です")
                rev_text = query.get("rev", [None])[0]
                if rev_text is not None and (not rev_text.isdecimal() or len(rev_text) > 12):
                    raise ValueError("revが不正です")
                rev = None if rev_text is None else int(rev_text)
                tab_id = query.get("tab", [""])[0]
                result = self.server.rb.frame_image(tab_id, int(since_text), wait=int(wait_text), rev=rev)
                headers = {
                    "X-Frame-Seq": str(result["seq"]),
                    "X-Frame-Width": str(result["width"]),
                    "X-Frame-Height": str(result["height"]),
                    "X-Frame-Url": quote(result["url"], safe=""),
                    "X-Frame-Title": quote(result["title"][:200], safe=""),
                    "X-Frame-Loading": "1" if result["loading"] else "0",
                    "X-Frame-Intercepts": str(result["intercepts"]),
                    "X-Frame-Rev": str(result["rev"]),
                    "X-Frame-Blocked": quote((result["blocked"] or "")[:200], safe=""),
                    "X-Frame-Popups": str(result["popups"]),
                    "X-Frame-Editable": "1" if result.get("editable") else "0",
                    "X-Frame-Challenge": "1" if result.get("challenge") else "0",
                    "X-Frame-Links-Rev": str(result["links_rev"]),
                }
                if result["jpeg"] is None:
                    return self.send_data(204, b"", extra=headers)
                return self.send_data(200, result["jpeg"], "image/jpeg", extra=headers)
            if path == "/api/rb/tile.jpg":
                # Fast full-page display: one JPEG tile of a snapshot (image bytes only).
                query = parse_qs(urlsplit(self.path).query)
                index_text = query.get("i", [""])[0]
                if not index_text.isdecimal() or len(index_text) > 5:
                    raise ValueError("iが不正です")
                data = self.server.rb.tile(query.get("tab", [""])[0],
                                           query.get("snap", [""])[0], int(index_text))
                return self.send_data(200, data, "image/jpeg")
            if path == "/api/rb/meta":
                query = parse_qs(urlsplit(self.path).query)
                wait_text = query.get("wait", [str(int(remote_browser.META_WAIT_MAX))])[0]
                if not wait_text.isdecimal() or len(wait_text) > 2 or int(wait_text) > 10:
                    raise ValueError("waitが不正です")
                rev_text = query.get("rev", [None])[0]
                if rev_text is not None and (not rev_text.isdecimal() or len(rev_text) > 12):
                    raise ValueError("revが不正です")
                vp_text = query.get("vp", [None])[0]
                if vp_text is not None and (not vp_text.isdecimal() or len(vp_text) > 12):
                    raise ValueError("vpが不正です")
                return self.send_data(200, self.server.rb.page_meta(
                    query.get("tab", [""])[0], None if rev_text is None else int(rev_text),
                    wait=int(wait_text), links=query.get("links", ["0"])[0] == "1",
                    vp=None if vp_text is None else int(vp_text),
                    fresh=query.get("fresh", ["0"])[0] == "1"))
            if path == "/api/rb/stream.mjpg":
                query = parse_qs(urlsplit(self.path).query)
                return self.stream_mjpeg(query.get("tab", [""])[0])
            if path == "/api/rb/stream.sse":
                # v0.1.46: the same live frames as Server-Sent Events (base64 JPEG), for proxies that
                # buffer multipart/x-mixed-replace but flush text/event-stream at once.
                query = parse_qs(urlsplit(self.path).query)
                return self.stream_sse(query.get("tab", [""])[0])
            if path == "/api/rb/reader/img":
                # Reader mode: one image, read through the NAS browser (never a remote URL).
                query = parse_qs(urlsplit(self.path).query)
                index_text = query.get("i", [""])[0]
                if not index_text.isdecimal() or len(index_text) > 4:
                    raise ValueError("iが不正です")
                data, kind = self.server.rb.reader_image(query.get("tab", [""])[0], int(index_text))
                return self.send_data(200, data, kind)
            if path == "/api/rb/mirror/doc":
                # Mirror display: the sanitized document of the newest capture (sandboxed iframe, no scripts).
                query = parse_qs(urlsplit(self.path).query)
                rev_text = query.get("rev", [""])[0]
                if not rev_text.isdecimal() or len(rev_text) > 12:
                    raise ValueError("revが不正です")
                text = self.server.rb.mirror_document(query.get("tab", [""])[0], int(rev_text))
                return self.send_data(200, text.encode("utf-8"), "text/html; charset=utf-8", compress=True,
                                      csp=rb_mirror.DOC_CSP_HEADER, frame="SAMEORIGIN")
            if path == "/api/rb/mirror/res":
                # Mirror display: one image / font, read through the NAS browser (never a remote URL).
                query = parse_qs(urlsplit(self.path).query)
                data, mime = self.server.rb.mirror_resource(query.get("tab", [""])[0], query.get("r", [""])[0])
                svg = mime == "image/svg+xml"
                return self.send_data(
                    200, data, mime, csp=rb_mirror.RES_SVG_CSP if svg else "default-src 'none'",
                    extra={"Cache-Control": "private, max-age=300", "Content-Disposition": "inline"})
            if path == "/api/rb/site-rules":
                return self.send_data(200, {"items": self.store.site_rules(),
                                            "block_ads": self.store.settings().get("block_ads", True),
                                            "max": storage.MAX_SITE_RULES})
            if path == "/api/rb/intercepts":
                query = parse_qs(urlsplit(self.path).query)
                since_text = query.get("since", ["0"])[0]
                if not since_text.isdecimal() or len(since_text) > 12:
                    raise ValueError("sinceが不正です")
                return self.send_data(200, {"items": self.server.rb.intercepts(
                    query.get("tab", [""])[0], int(since_text))})
            if path == "/api/rb/links":
                query = parse_qs(urlsplit(self.path).query)
                return self.send_data(200, self.server.rb.links(query.get("tab", [""])[0]))
            if path == "/api/browser/validate":
                query = parse_qs(urlsplit(self.path).query)
                value = query.get("url", [""])[0]
                checked = browser_safety.validate(value)
                self.reject_hub_origin(checked)
                return self.send_data(200, {"url": checked})
            if path == "/api/update/check":
                query = parse_qs(urlsplit(self.path).query)
                channel = query.get("channel", ["rc"])[0]
                release = updater.latest(VERSION, "x86_64", channel)
                return self.send_data(200, {"available": release is not None,
                  "release": {"version": release["version"], "size": release["size"]}
                            if release else None, "current": VERSION, "channel": channel})
        except jd2_accounts.ServiceError as exc:
            return self.reject(str(exc), exc.status)
        except remote_browser.TabGone as exc:
            return self.reject(str(exc), 404)
        except remote_browser.MirrorGone as exc:
            return self.reject(str(exc), 410)
        except remote_browser.ProbeQueueFull as exc:
            return self.reject(str(exc), 429)
        except remote_browser.ImageRefused as exc:
            return self.reject(str(exc), 415)
        except remote_browser.ImageUnavailable as exc:
            return self.reject(str(exc), 502)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True     # the viewer left (e.g. a held long-poll): nothing to answer
            return None
        except (OSError, ValueError, RuntimeError, KeyError, sqlite3.Error) as exc:
            return self.reject(str(exc))
        self.reject("APIが存在しません", 404)

    def send_staged_link(self, ident=None, url=None):
        return send_link(self.server, ident=ident, url=url)

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
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True     # the viewer left (e.g. a held long-poll): nothing to answer
            return None
        except (OSError, ValueError, KeyError, RuntimeError, json.JSONDecodeError, sqlite3.Error) as exc:
            return self.reject(str(exc))
        return self.send_data(200, {"added": added, "found": len(found)})

    def do_POST(self):
        if not debuglog.LOG._enabled:
            return self.post_and_refresh()
        started = time.monotonic()
        try:
            return self.post_and_refresh()
        finally:
            self._log_http("POST", started)

    def post_and_refresh(self):
        path = urlsplit(self.path).path
        if path == "/api/share" or path in RB_QUIET_POSTS:
            return self.handle_post()
        try:
            return self.handle_post()
        finally:
            # Any other write may change what the cached reads show: forget them and let the
            # waiting /api/state pollers re-read now.
            server_cache(self.server).invalidate()
            server_state_hub(self.server).poke()

    def handle_post(self):
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
            if path == "/api/settings/options":
                options = storage.BOOL_OPTIONS + tuple(storage.ENUM_OPTIONS)
                known = [key for key in options if key in body]
                if not known or set(body) - set(options):
                    raise ValueError("変更できる設定項目を指定してください")
                for key in known:
                    self.store.set_option(key, body[key])
                if "rb_resident" in known:
                    # v0.1.38: apply at once (start / let go of the resident NAS browser)
                    self.server.rb.set_resident(self.store.settings().get("rb_resident") is True)
                return self.send_data(200, self.store.settings())
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
            if path == "/api/links/clear":
                include = body.get("include_staged", False)
                return self.send_data(200, {"removed": self.store.clear_links(include)})
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
                return self.send_data(200, self.send_staged_link(ident=body.get("id")))
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
            if path == "/api/rb/retry":
                # v0.1.38: after the watchdog gave up. Starts the browser again (memory guard applies).
                started = self.server.rb.retry()
                return self.send_data(202, {"retrying": started})
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
            if path == "/api/rb/viewport":
                return self.send_data(200, self.server.rb.set_viewport(
                    body.get("tab"), body.get("width"), body.get("height"), body.get("dpr"),
                    body.get("quality")))
            if path == "/api/rb/snapshot":
                return self.send_data(200, self.server.rb.snapshot(
                    body.get("tab"), force=body.get("force") is True,
                    if_changed=body.get("if_changed") is True))
            if path == "/api/rb/snapshot/release":
                return self.send_data(200, self.server.rb.snapshot_release(body.get("tab")))
            if path == "/api/rb/tap-page":
                return self.send_data(200, self.server.rb.tap_page(
                    body.get("tab"), body.get("snap"), body.get("x"), body.get("y")))
            if path == "/api/rb/reader":
                return self.send_data(200, self.server.rb.reader(body.get("tab")))
            if path == "/api/rb/mirror":
                return self.send_data(200, self.server.rb.mirror(
                    body.get("tab"), full=body.get("full") is not False, force=body.get("force") is True),
                    compress=True)
            if path == "/api/rb/mirror/click":
                result = self.server.rb.mirror_click(
                    body.get("tab"), body.get("m"), body.get("fx", 0.5), body.get("fy", 0.5))
                href = result.pop("href", None)
                if href:
                    # A link: the NAS browser goes there, through the same checks as every navigation.
                    checked = browser_safety.validate(href)
                    self.reject_hub_origin(checked)
                    self.server.rb.navigate(body.get("tab"), checked)
                    result = {"ok": True, "navigated": True, "url": checked}
                return self.send_data(200, result)
            if path == "/api/rb/mirror/release":
                return self.send_data(200, self.server.rb.mirror_release(body.get("tab")))
            if path == "/api/rb/probe":
                result = self.server.rb.probe(
                    body.get("tab"), body.get("x"), body.get("y"), url_check=self.reject_hub_origin,
                    href=body.get("href"), snap=body.get("snap"))
                result = dict(result, probe_id=remember_probe(self.server, body.get("tab"), result.get("links")))
                return self.send_data(200, result)
            if path == "/api/rb/probe/send":
                return self.send_data(200, send_probe_links(
                    self.server, body.get("tab"), body.get("probe_id")))
            if path == "/api/rb/site-rules":
                return self.send_data(200, self.store.set_site_rule(
                    body.get("host"), auto_send=body.get("auto_send"), popup=body.get("popup"),
                    block_ads=body.get("block_ads")))
            if path == "/api/rb/site-rules/delete":
                self.store.delete_site_rule(body.get("host"))
                return self.send_data(200, {"deleted": True})
            if path == "/api/rb/intercepts/open":
                return self.send_data(200, self.server.rb.open_intercept(body.get("tab"), body.get("id")))
            if path == "/api/rb/intercepts/add":
                item = self.server.rb.intercept(body.get("tab"), body.get("id"))
                return self.send_data(201, self.store.add_link(item["url"]))
            if path == "/api/rb/intercepts/send":
                item = self.server.rb.intercept(body.get("tab"), body.get("id"))
                # Same path as /api/links/send; the link is staged inside the JD2 lock.
                try:
                    return self.send_data(200, send_link(self.server, url=item["url"], dedupe=True))
                except AlreadySent:
                    return self.send_data(200, {"state": "dup"})
            if path == "/api/selftest/start":
                urls = body.get("urls", [])
                if not isinstance(urls, list) or len(urls) > selftest.MAX_URLS:
                    raise ValueError("実サイトのURLは最大%d件です" % selftest.MAX_URLS)
                checked_urls = []
                for number, raw in enumerate(urls, 1):
                    try:
                        checked = browser_safety.validate(raw)
                        self.reject_hub_origin(checked)
                    except ValueError as exc:
                        raise ValueError("%d件目のURL: %s" % (number, exc)) from None
                    if checked not in [item["url"] for item in checked_urls]:
                        checked_urls.append({"url": checked})
                try:
                    run_id = server_selftest(self.server).start({
                        "urls": checked_urls, "viewport": body.get("viewport"), "client": body.get("client", True)})
                except selftest.Busy as exc:
                    return self.reject(str(exc), 409)
                return self.send_data(202, {"run_id": run_id})
            if path == "/api/selftest/cancel":
                return self.send_data(200, {"cancelled": server_selftest(self.server).cancel()})
            if path == "/api/selftest/client":
                return self.send_data(200, server_selftest(self.server).submit_client(body))
            if path == "/api/diag/diskio/start":
                return self.send_data(200, server_diskio(self.server).start())
            if path == "/api/diag/diskio/stop":
                return self.send_data(200, server_diskio(self.server).stop())
            if path == "/api/debug/log":
                stored = debuglog.LOG.log_client(body.get("entries"))
                return self.send_data(200, {"accepted": stored})
            if path == "/api/debug/config":
                if body.get("enabled") is True:
                    debuglog.LOG.enable(body.get("level", "debug"), body.get("categories"))
                    debuglog.log("app", "info", "debug log enabled")
                elif body.get("enabled") is False:
                    debuglog.LOG.disable()
                else:
                    raise ValueError("enabledを指定してください")
                return self.send_data(200, debuglog.LOG.status())
            if path == "/api/debug/clear":
                debuglog.LOG.clear()
                return self.send_data(200, debuglog.LOG.status())
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
            if path.startswith("/api/accounts/"):
                action = path[len("/api/accounts/"):]
                if action not in ("add", "update", "enable", "refresh", "delete"):
                    return self.reject("APIが存在しません", 404)
                if action in ("add", "update") and not credentials_transport_ok(
                        self.client_address[0], self.headers):
                    body.clear()
                    return self.reject(CREDENTIALS_TRANSPORT_ERROR, 403)
                service = self.accounts
                handler = {"add": service.add, "update": service.update,
                           "enable": service.set_enabled, "refresh": service.refresh,
                           "delete": service.delete}[action]
                # The password is never echoed; the service clears the body dict.
                return self.send_data(200, handler(self.server.jd2, body))
            if path == "/api/jd2/linkgrabber/start":
                return self.send_data(200, self.server.jd2.start_waiting(
                    confirmed=body.get("confirmed") is True))
            if path in ("/api/jd2/items/start", "/api/jd2/items/stop", "/api/jd2/items/remove"):
                return self.send_data(200, self.item_action(path.rsplit("/", 1)[1], body))
            if path in ("/api/jd2/waiting/remove", "/api/jd2/waiting/start"):
                ids = self.body_ids(body)
                if path.endswith("/remove"):
                    return self.send_data(200, self.server.jd2.waiting_remove(ids))
                return self.send_data(200, self.server.jd2.waiting_start(ids))
            if path == "/api/jd2/downloads/start":
                return self.send_data(200, self.server.jd2.start_downloads())
            if path == "/api/jd2/downloads/cleanup":
                return self.send_data(200, self.server.jd2.clear_finished(
                    confirmed=body.get("confirmed") is True))
            if path == "/api/jd2/downloads/stop":
                return self.send_data(200, self.server.jd2.stop_downloads(
                    confirmed=body.get("confirmed") is True))
            if path == "/api/jd2/flatten":
                # Admin action (authorize(write=True): session + CSRF + Origin above):
                # move not-yet-started packages out of per-package subfolders now.
                folder = str(checked_directory(self.store.settings().get("download_dir")))
                return self.send_data(200, self.server.jd2.flatten_waiting(folder))
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
        except jd2_accounts.ServiceError as exc:
            return self.reject(str(exc), exc.status)
        except remote_browser.TabGone as exc:
            return self.reject(str(exc), 404)
        except remote_browser.MirrorGone as exc:
            return self.reject(str(exc), 410)
        except remote_browser.ProbeQueueFull as exc:
            return self.reject(str(exc), 429)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True     # the viewer left (e.g. a held long-poll): nothing to answer
            return None
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
    server.data_dir = data
    server.secure_cookies = args.secure_cookie
    remove_legacy_setup_token(data)
    server.updates = self_update.UpdateManager(data, ROOT, VERSION, "x86_64", args.port)
    server.engine = engine_supervisor.EngineManager(data, store)
    server.jd2 = jd2_control.JD2Controller(server.engine.adapter)
    server.accounts = jd2_accounts.AccountService()
    server.rb = remote_browser.RemoteBrowser(
        server.engine.runtime, ROOT / "container" / "browser")
    wire_remote_browser(server)
    # A labelled browser container surviving a Hub crash would hold memory.
    threading.Thread(target=boot_remote_browser, args=(server,), daemon=True).start()
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
