"""v0.1.57: the passthrough listener (origin B) and its manager.

A second HTTP listener on 127.0.0.1 (default port 17865; the admin exposes it with
`tailscale serve --bg --https=8443 http://127.0.0.1:17865`). It serves ONLY passthrough routes: no Hub API, no app
files, never the admin session cookie. Pages proxied through it run on the phone under this origin, so everything a
site's JavaScript can reach here is limited to what this file serves (see docs/passthrough.md).

Routes: /pt/ping, /pt/enter (ticket -> own cookie + Service Worker bootstrap), /pt-sw.js, /pt/helper.js, /pt/boot.js,
/pt/download.js, /pt/save/<id>, and /p/<s|h>/<host>/<path> (the proxy). Everything else answers 404.
"""
import html
import http.cookies
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

import debuglog
import pt_core
import pt_proxy

PORT = 17865
PUBLIC_PORT = 8443
ROOT = Path(__file__).resolve().parent
COOKIE = "qd_pt"
MAX_CONNECTIONS = 64
STATIC = {"/pt-sw.js": "pt-sw.js", "/pt/helper.js": "pt-helper.js", "/pt/boot.js": "pt-boot.js",
          "/pt/download.js": "pt-download.js"}
_STATIC_CACHE = {}
LOG_GATE = 1.0


def static_bytes(name):
    data = _STATIC_CACHE.get(name)
    if data is None:
        data = _STATIC_CACHE[name] = (ROOT / "web" / name).read_bytes()
    return data


def page(title, body, extra_head=""):
    return ("<!doctype html><html lang=\"ja\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" "
            "content=\"width=device-width,initial-scale=1\"><title>%s</title>"
            "<style>body{font:16px/1.6 -apple-system,sans-serif;margin:0;padding:24px 16px;color:#222;background:#fff}"
            "@media(prefers-color-scheme:dark){body{color:#eee;background:#111}}"
            ".btn{display:block;box-sizing:border-box;width:100%%;margin:10px 0;padding:14px;border:0;border-radius:10px;"
            "background:#2f6fed;color:#fff;font:inherit;text-align:center;text-decoration:none}"
            ".btn.q{background:#8884}code{word-break:break-all}.m{color:#888;font-size:14px}</style>%s</head>"
            "<body>%s</body></html>" % (html.escape(title), extra_head, body)).encode("utf-8")


class PtServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, address, handler, manager):
        super().__init__(address, handler)
        self.mgr = manager
        self.active = 0
        self.active_lock = threading.Lock()

    def process_request(self, request, client_address):
        with self.active_lock:
            over = self.active >= MAX_CONNECTIONS
            if not over:
                self.active += 1
        if over:
            self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self.active_lock:
                self.active -= 1

    def handle_error(self, request, client_address):
        pass            # a viewer that left mid-response is not an error


class PassthroughManager:
    """Owns the passthrough state, the upstream client and the listener (runs only while the setting is ON)."""

    poll_interval = 0.5          # serve_forever wake-up; the unit tests make it shorter

    def __init__(self, store, version, hooks, port=PORT, test_upstream=None, tls_context=None):
        self.store, self.version, self.hooks = store, version, hooks
        self.state = pt_core.PassthroughState()
        self.client = pt_proxy.UpstreamClient(own_names=lambda: self.state.own_names, test_upstream=test_upstream,
                                              tls_context=tls_context, fakes=pt_proxy.selftest_site)
        self.requested_port = port
        self.bound_port = 0
        self.public_port = PUBLIC_PORT
        self.enabled = False
        self.error = ""
        self.httpd = None
        self.thread = None
        self.lock = threading.Lock()
        self.log_gate = {}
        self.stats = {"requests": 0, "blocked": 0, "errors": 0, "bytes": 0}

    # -- lifecycle
    def sync(self):
        """Follow the settings: start the listener when pt_enabled is on, stop it (and forget every session) when off."""
        settings = self.store.settings()
        port = settings.get("pt_public_port")
        self.public_port = port if type(port) is int and 0 < port < 65536 else PUBLIC_PORT
        if settings.get("pt_enabled") is True:
            self.start()
        else:
            self.stop()
        return self.status()

    def start(self):
        with self.lock:
            if self.httpd is not None:
                return True
            try:
                httpd = PtServer(("127.0.0.1", self.requested_port), PtHandler, self)
            except OSError as exc:
                self.error = "ポート%d で待ち受けできません (%s)" % (self.requested_port, exc.strerror or exc.errno)
                self.enabled = False
                self.log("warn", "listener failed", {"port": self.requested_port})
                return False
            self.httpd = httpd
            self.bound_port = httpd.server_address[1]
            self.error = ""
            self.enabled = True
            self.thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": self.poll_interval}, daemon=True)
            self.thread.start()
            self.log("info", "listener started", {"port": self.bound_port})
            return True

    def stop(self):
        with self.lock:
            httpd, self.httpd = self.httpd, None
            self.enabled = False
            self.error = ""
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
            self.log("info", "listener stopped")
        self.state.revoke_all()
        self.client.close()
        self.bound_port = 0

    def status(self):
        return {"enabled": self.enabled, "listening": self.httpd is not None, "error": self.error,
                "port": self.bound_port or self.requested_port, "public_port": self.public_port,
                "requests": self.stats["requests"], "blocked": self.stats["blocked"],
                "sessions": len(self.state.sessions)}

    # -- logging (category "pt"; one entry per key per second)
    def log(self, level, message, data=None, key=None):
        if not debuglog.LOG._enabled:
            return
        if key is not None:
            now = time.monotonic()
            if now - self.log_gate.get(key, -1e9) < LOG_GATE:
                return
            if len(self.log_gate) > 200:
                self.log_gate.clear()
            self.log_gate[key] = now
        debuglog.log("pt", level, message, data)


class PtHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "QnapDownloader"
    sys_version = ""
    timeout = 60

    def log_message(self, format, *args):
        pass                       # never log URLs

    @property
    def mgr(self):
        return self.server.mgr

    # -- small helpers
    def reply(self, status, body=b"", content_type="text/html; charset=utf-8", extra=(), head=False):
        self.send_response(status)
        if status not in (204, 304):
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
        for name, value in extra:
            self.send_header(name, value)
        if not any(n.lower() == "cache-control" for n, _ in extra):
            self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if body and not head and status not in (204, 304):
            self.wfile.write(body)

    def error_page(self, status, title, message, head=False):
        body = page(title, "<h1>%s</h1><p>%s</p>" % (html.escape(title), html.escape(message)))
        self.close_if_body_unread()
        self.reply(status, body, extra=(("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'"),
                                        ("Referrer-Policy", "no-referrer")), head=head)

    def close_if_body_unread(self):
        if getattr(self, "_body_read", False):
            return
        if self.headers.get("Content-Length", "0") not in ("", "0") or self.headers.get("Transfer-Encoding"):
            self.close_connection = True

    def is_https(self):
        return bool(self.mgr.hooks["request_is_https"](self.client_address[0], self.headers))

    def own_origin(self):
        return ("https" if self.is_https() else "http") + "://" + self.headers.get("Host", "")

    def session_token(self):
        jar = http.cookies.SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie", ""))
        except http.cookies.CookieError:
            return ""
        return jar[COOKIE].value if COOKIE in jar else ""

    def host_ok(self):
        """Loopback peers only (Tailscale Serve); Host must be *.ts.net, localhost or 127.0.0.1 (DNS rebinding)."""
        hooks = self.mgr.hooks
        if not hooks["loopback_peer"](self.client_address[0]):
            return False
        host = self.headers.get("Host", "")
        try:
            parts = urlsplit("http://" + host)
            name, port = (parts.hostname or "").lower().rstrip("."), parts.port
        except ValueError:
            return False
        if (not host or len(host) > 255 or any(c.isspace() for c in host) or parts.username is not None or
                parts.path not in ("", "/") or parts.query or parts.fragment or (port is not None and not 0 < port < 65536)):
            return False
        return name in ("localhost", "127.0.0.1") or hooks["tailscale_name"](name)

    # -- dispatch
    def do_GET(self): self.dispatch("GET")
    def do_HEAD(self): self.dispatch("HEAD")
    def do_POST(self): self.dispatch("POST")
    def do_PUT(self): self.dispatch("PUT")
    def do_PATCH(self): self.dispatch("PATCH")
    def do_DELETE(self): self.dispatch("DELETE")
    def do_OPTIONS(self): self.dispatch("OPTIONS")

    def dispatch(self, method):
        self._body_read = False
        try:
            if not self.mgr.enabled:
                self.close_connection = True
                return self.reply(404, b"", "text/plain")
            if not self.host_ok():
                self.close_connection = True
                self.mgr.log("warn", "host refused", {"host": self.headers.get("Host", "")[:60]}, key="host")
                return self.reply(403, b"", "text/plain")
            self.mgr.state.note_own_name(urlsplit("http://" + self.headers.get("Host", "")).hostname or "")
            path = urlsplit(self.path).path
            if self.path.startswith("/p/"):
                return self.proxy(method)
            if method in ("GET", "HEAD"):
                head = method == "HEAD"
                if path in STATIC:
                    return self.static(path, head)
                if path == "/pt/ping":
                    return self.ping(head)
                if path == "/pt/enter":
                    return self.enter(head)
                if path.startswith("/pt/save/"):
                    return self.save(path[len("/pt/save/"):])
            self.close_if_body_unread()
            return self.reply(404, b"", "text/plain", head=method == "HEAD")
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True

    # -- simple routes
    def static(self, path, head):
        extra = [("Cache-Control", "no-cache")]
        if path == "/pt-sw.js":
            extra.append(("Service-Worker-Allowed", "/"))
        self.reply(200, static_bytes(STATIC[path]), "text/javascript; charset=utf-8", extra, head)

    def ping(self, head):
        origin = self.headers.get("Origin", "")
        extra = [("Vary", "Origin")]
        if origin and origin in self.mgr.state.app_origins:
            extra.append(("Access-Control-Allow-Origin", origin))
        body = json.dumps({"ok": True, "https": self.is_https(), "version": self.mgr.version}).encode()
        self.reply(200, body, "application/json; charset=utf-8", extra, head)

    def enter(self, head):
        """Redeem the one-time ticket, set our own cookie, hand over to the Service Worker bootstrap."""
        query = parse_qs(urlsplit(self.path).query)
        ticket, url = query.get("t", [""])[0], query.get("u", [""])[0]
        state = self.mgr.state
        redeemed = state.redeem_ticket(ticket) if ticket else None
        session = state.session_for(self.session_token()) if redeemed is None else None
        if redeemed is None and session is None:
            self.mgr.log("warn", "ticket refused", key="ticket")
            return self.error_page(401, "パススルーを開けません",
                                   "チケットが無効か期限切れです。アプリからもう一度開いてください。", head)
        try:
            target = pt_core.parse_target(url)
        except pt_core.TargetError as exc:
            return self.error_page(400, "URLを開けません", str(exc), head)
        app_origin = (redeemed[1].app_origin if redeemed else session.app_origin)
        extra = [("Referrer-Policy", "no-referrer"),
                 ("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; "
                  "frame-ancestors %s" % (app_origin or "'none'"))]
        if redeemed:
            cookie = "%s=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=%d" % (COOKIE, redeemed[0], int(pt_core.SESSION_ABSOLUTE))
            if self.is_https():
                cookie += "; Secure"
            extra.append(("Set-Cookie", cookie))
        body = page("読み込み中…", "<p id=\"m\">パススルーを準備しています…</p>"
                    "<script src=\"/pt/boot.js\" data-target=\"%s\"></script>" % html.escape(target.proxy_path, quote=True))
        self.reply(200, body, extra=extra, head=head)

    # -- request body
    def read_body(self):
        """The request body (bytes) or None for none; raises ValueError('...') with an HTTP status in args[1] on refusal."""
        self._body_read = True
        if self.headers.get("Transfer-Encoding"):
            raise _Refuse(411, "Content-Lengthが必要です")
        text = self.headers.get("Content-Length", "")
        if not text:
            return None
        if not text.isdigit() or len(text) > 10:
            raise _Refuse(400, "Content-Lengthが不正です")
        size = int(text)
        if size > pt_core.BODY_MAX:
            raise _Refuse(413, "送信データが大きすぎます（上限16MiB）")
        return self.rfile.read(size) if size else None

    # -- the proxy
    def proxy(self, method):
        mgr, state = self.mgr, self.mgr.state
        session = state.session_for(self.session_token())
        if session is None:
            self.close_if_body_unread()
            return self.error_page(401, "パススルーのセッションがありません",
                                   "アプリのパススルー表示から開き直してください。", method == "HEAD")
        if self.headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
            self.close_if_body_unread()
            return self.error_page(403, "拒否しました", "他のサイトからの要求は受け付けません。", method == "HEAD")
        try:
            target = pt_core.decode_target(self.path)
        except pt_core.TargetError as exc:
            self.close_if_body_unread()
            return self.error_page(400, "URLが不正です", str(exc), method == "HEAD")
        try:
            body = self.read_body()
        except _Refuse as exc:
            self.close_connection = True
            return self.error_page(exc.status, "送れません", str(exc))
        dest = self.headers.get("X-QD-PT-Dest") or self.headers.get("Sec-Fetch-Dest") or ""
        nav = pt_core.is_navigation(dest, self.headers.get("Sec-Fetch-Mode", ""), self.headers.get("Accept", ""))
        top = pt_core.top_site(self.headers.get("X-QD-PT-Top"))
        first_party = bool(top) and top == pt_core.site_of(target.host)
        self.relay(method, target, body, session, nav, first_party, dest)

    def relay(self, method, target, body, session, nav, first_party, dest, force_stream=False, referer=None):
        mgr, state = self.mgr, self.mgr.state
        started = time.monotonic()
        head = method == "HEAD"
        https_up = target.scheme == "https"
        cookie = session.jar.header_for(target.host, target.path, https_up) if first_party else ""
        if referer is None:
            referer = pt_core.real_referer(self.headers.get("Referer", ""), target)
        incoming = [(k, v) for k, v in self.headers.items()]
        upstream_headers = pt_core.build_upstream_headers(
            incoming, target, referer, cookie, self.headers.get("User-Agent"),
            len(body) if body is not None else (0 if method in ("POST", "PUT", "PATCH") else None), method)
        mgr.stats["requests"] += 1
        try:
            resp = mgr.client.open(method, target, upstream_headers, body)
        except (pt_core.Blocked, pt_core.ResolveError) as exc:
            blocked = isinstance(exc, pt_core.Blocked)
            mgr.stats["blocked" if blocked else "errors"] += 1
            mgr.log("warn", "blocked" if blocked else "unresolved", {"host": target.host, "why": str(exc)[:80]},
                    key="blk:" + target.host)
            return self.error_page(403 if blocked else 502, "接続できません" if blocked else "名前を解決できません",
                                   "このアドレスにはパススルーで接続できません。" if blocked else str(exc), head)
        except pt_proxy.UpstreamError as exc:
            mgr.stats["errors"] += 1
            mgr.log("warn", "upstream error", {"host": target.host, "status": exc.status}, key="err:" + target.host)
            return self.error_page(exc.status, "接続できません", str(exc), head)
        with resp:
            self.answer(resp, method, target, session, nav, first_party, dest, started, force_stream, referer)

    def answer(self, resp, method, target, session, nav, first_party, dest, started, force_stream, referer):
        mgr, state = self.mgr, self.mgr.state
        head = method == "HEAD"
        app_origin = session.app_origin
        page_origin = target.origin
        if referer:
            ref = urlsplit(referer)
            page_origin = "%s://%s" % (ref.scheme, ref.netloc)
        out, cookies, failed = pt_core.rewrite_response_headers(resp.headers, target, self.own_origin(), page_origin)
        if cookies and first_party:
            session.jar.store(target.host, target.path, target.scheme == "https", cookies)
        if failed:
            return self.error_page(502, "リダイレクトを扱えません", "リダイレクト先を開けないURLです。", head)
        if not force_stream:
            decision = pt_core.download_decision(method, resp.status, resp.headers, nav, target,
                                                 bool(self.headers.get("Range")))
            if decision:
                ident = state.add_download(target.url, decision["name"], decision["size"], decision["type"], session)
                mgr.log("info", "download page", {"host": target.host, "size": decision["size"]})
                return self.download_page(ident, decision, target, app_origin)
        no_body = head or resp.status in (204, 304) or 100 <= resp.status < 200
        injecting = (nav and not no_body and not force_stream and pt_core.is_html(resp.header("Content-Type")) and
                     resp.status != 206 and not resp.header("Location"))
        decoder = None
        if injecting:
            try:
                decoder = pt_core.Decoder(resp.header("Content-Encoding"))
            except ValueError:
                return self.error_page(502, "表示できません", "未対応の圧縮形式です。", head)
            # The body is decoded and changed: its old validators, length and encoding no longer describe it.
            out = [(k, v) for k, v in out
                   if k.lower() not in ("content-encoding", "etag", "content-md5", "cache-control", "expires", "last-modified")]
            out.append(("Cache-Control", "no-store"))
            out.append(("Content-Security-Policy", pt_core.html_csp(app_origin)))
        elif force_stream and not resp.header("Content-Disposition"):
            out.append(("Content-Disposition", "attachment; filename*=UTF-8''%s" % quote(pt_core.filename_from("", target))))
        length = resp.header("Content-Length")
        self.send_response(resp.status, resp.reason or None)
        for name, value in out:
            self.send_header(name, value)
        chunked = False
        if no_body:
            if head and length:
                self.send_header("Content-Length", length)
        elif not injecting and length.isdigit():
            self.send_header("Content-Length", length)        # untouched body of known size (a gzip body stays gzip)
        else:
            self.send_header("Transfer-Encoding", "chunked")
            chunked = True
        self.end_headers()
        if no_body:
            return
        sent = 0
        injector = pt_core.HtmlInjector(pt_core.inject_snippet(app_origin, mgr.version)) if injecting else None

        def emit(data):
            nonlocal sent
            if not data:
                return
            sent += len(data)
            self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data) if chunked else data)

        try:
            for part in resp.iter_body():
                if injecting:
                    for piece in decoder.feed(part):
                        emit(injector.feed(piece))
                else:
                    emit(part)
            if injecting:
                emit(injector.feed(decoder.flush()))
                emit(injector.finish())
            if chunked:
                self.wfile.write(b"0\r\n\r\n")
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True
            resp.close(reuse=False)
        except Exception as exc:
            # The upstream died or sent a corrupt body mid-stream: the framing promise is broken, drop the connection.
            self.close_connection = True
            resp.close(reuse=False)
            mgr.stats["errors"] += 1
            mgr.log("warn", "stream aborted", {"host": target.host, "err": type(exc).__name__}, key="abort:" + target.host)
        mgr.stats["bytes"] += sent
        mgr.log("debug", "%s %s" % (method, resp.status),
                {"host": target.host, "ms": int((time.monotonic() - started) * 1000), "bytes": sent, "nav": nav,
                 "fp": first_party}, key="req:" + target.host)

    # -- downloads
    def download_page(self, ident, decision, target, app_origin):
        size = decision["size"]
        size_text = ("%.1f MiB" % (size / 1048576)) if size >= 0 else "サイズ不明"
        body = page("ダウンロード", (
            "<h1>ダウンロード</h1><p><b>%s</b></p><p class=\"m\">%s ・ %s</p>"
            "<button type=\"button\" id=\"jd2\" class=\"btn\">JD2で落とす</button>"
            "<a id=\"save\" class=\"btn q\" href=\"/pt/save/%s\">iPhoneに保存</a>"
            "<p id=\"msg\" class=\"m\" role=\"status\"></p>"
            "<p class=\"m\">JD2にはURLだけを渡します（このパススルーのCookieは渡りません）。IPで縛られるリンクは、NASから取得するJD2で落とせます。</p>"
            "<script src=\"/pt/download.js\" data-id=\"%s\" data-app=\"%s\" data-name=\"%s\" data-size=\"%d\" data-host=\"%s\"></script>"
            % (html.escape(decision["name"]), html.escape(target.host), html.escape(size_text), html.escape(ident, quote=True),
               html.escape(ident, quote=True), html.escape(app_origin or "", quote=True),
               html.escape(decision["name"], quote=True), size, html.escape(target.host, quote=True))))
        self.reply(200, body, extra=(("Content-Security-Policy", "default-src 'none'; script-src 'self'; "
                                      "style-src 'unsafe-inline'; frame-ancestors %s" % (app_origin or "'none'")),
                                     ("Referrer-Policy", "no-referrer")))

    def save(self, ident):
        session = self.mgr.state.session_for(self.session_token())
        record = self.mgr.state.get_download(ident, session) if session else None
        if session is None or record is None:
            return self.error_page(404, "ダウンロードが見つかりません", "有効期限が切れました。もう一度リンクを開いてください。")
        try:
            target = pt_core.parse_target(record["url"])
        except pt_core.TargetError:
            return self.error_page(404, "ダウンロードが見つかりません", "")
        self.relay("GET", target, None, session, False, True, "", force_stream=True, referer=target.origin + "/")


class _Refuse(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


# ------------------------------------------------------------------------------------ self-test (v0.1.57)
def run_selftest(mgr):
    """[(id, name, status, value, threshold)] for the self-test's パススルー section.

    Talks to the listener over loopback with a throw-away session and the in-process fake site (pt-selftest.invalid): no
    outside network, nothing is kept. The phone side (Service Worker control) is covered by the E2E test, not here."""
    import http.client
    if not mgr.enabled:
        return [("pt.off", "パススルー", "skip", "設定がオフです", "")]
    if mgr.httpd is None or not mgr.bound_port:
        return [("pt.listen", "パススルーの待ち受け", "skip", mgr.error or "待ち受けていません", "")]
    token, _session = mgr.state.create_session("")
    out = []

    def get(path, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", mgr.bound_port, timeout=10)
        try:
            conn.request("GET", path, headers=dict({"Host": "localhost", "Cookie": "%s=%s" % (COOKIE, token)}, **(headers or {})))
            response = conn.getresponse()
            return response.status, dict((k.lower(), v) for k, v in response.getheaders()), response.read(200000)
        finally:
            conn.close()

    host = pt_core.SELFTEST_HOST
    base = "/p/s/%s" % host
    top = {"X-QD-PT-Top": host}
    try:
        started = time.monotonic()
        status, headers, body = get(base + "/", dict(top, **{"X-QD-PT-Dest": "document", "Accept": "text/html"}))
        ms = int((time.monotonic() - started) * 1000)
        text = body.decode("utf-8", "replace")
        ok = status == 200 and "PT-SELFTEST-PAGE" in text and "/pt/helper.js" in text
        out.append(("pt.load", "ページの読み込み（ヘルパー注入）", "pass" if ok else "fail",
                    "%d ms" % ms if ok else "HTTP %d" % status, "ヘルパー入りのHTML"))
        subs = 0
        for path in ("/s.js", "/i.png"):
            sub_status, _h, _b = get(base + path, dict(top, **{"X-QD-PT-Dest": "script" if path.endswith("js") else "image"}))
            subs += sub_status == 200
        out.append(("pt.sub", "サブリソース", "pass" if subs == 2 else "fail", "%d / 2 件" % subs, "2 / 2 件"))
        _s, _h, own = get(base + "/echo-cookie", top)
        _s, _h, other = get("/p/s/pt-selftest-b.invalid/echo-cookie", top)
        mine, theirs = own.decode(), other.decode()
        out.append(("pt.cookie", "Cookie（同じサイトにだけ送る）", "pass" if "st=first" in mine and "st=first" not in theirs else "fail",
                    "同サイト: %s / 別サイト: %s" % (mine[:40], theirs[:40]), "同サイトだけに送る"))
        r_status, r_headers, _b = get(base + "/redir", {"X-QD-PT-Dest": "document", "Accept": "text/html"})
        location = r_headers.get("location", "")
        out.append(("pt.redirect", "リダイレクト（/p/ に書き換え）", "pass" if r_status == 302 and location.startswith(base + "/final") else "fail",
                    "%d %s" % (r_status, location[:80]), base + "/final"))
        d_status, _h, d_body = get(base + "/file.bin", {"X-QD-PT-Dest": "document", "Accept": "text/html"})
        out.append(("pt.download", "ダウンロードの確認ページ", "pass" if d_status == 200 and "JD2" in d_body.decode("utf-8", "replace") else "fail",
                    "HTTP %d" % d_status, "JD2ボタン付きのページ"))
    except (OSError, http.client.HTTPException) as error:
        out.append(("pt.error", "パススルーの確認", "fail", "%s: %s" % (type(error).__name__, str(error)[:100]), ""))
    finally:
        mgr.state.sessions.pop(pt_core._digest(token), None)
    return out
