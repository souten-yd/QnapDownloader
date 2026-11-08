"""v0.1.57: the Hub's HTTP client for the passthrough display.

http.client over TLS (certificates verified with ssl.create_default_context), HTTP/1.1, a small keep-alive pool and
a global limit of concurrent upstream connections (the NAS is a Celeron). The connection goes to the IP address the
SSRF guard checked, with SNI / Host set to the host name, so a DNS answer that changes later cannot redirect it.
Response bodies are streamed in chunks and never buffered whole.

`test_upstream` ({host: (ip, port)}) exists for the unit and E2E tests only: it is a constructor argument that nothing in
the production start path sets (the QPKG contract test checks that), and the guard's host-name rules still apply.
"""
import http.client
import socket
import ssl
import threading
import time

import pt_core

CONNECT_TIMEOUT = 10.0
READ_TIMEOUT = 30.0
MAX_CONNECTIONS = 12              # upstream connections in use at once
SLOT_WAIT = 8.0                   # a request waits this long for a free connection slot
POOL_IDLE = 20.0
POOL_PER_KEY = 2
POOL_TOTAL = 16
CHUNK = 64 * 1024


class UpstreamError(Exception):
    """Could not talk to the upstream server (status to answer with in .status)."""

    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status


class NasChallenge(Exception):
    """v0.1.74: the NAS browser met a bot check (Cloudflare) for `url`: it must be done once in the NAS browser."""

    def __init__(self, url):
        super().__init__("このサイトの確認が必要です")
        self.url = url


class _Pinned(http.client.HTTPConnection):
    def __init__(self, host, port, ip, timeout):
        super().__init__(host, port, timeout=timeout)
        self._pin = ip

    def connect(self):
        self.sock = socket.create_connection((self._pin, self.port), CONNECT_TIMEOUT)
        self.sock.settimeout(self.timeout)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)


class _PinnedTLS(http.client.HTTPSConnection):
    def __init__(self, host, port, ip, timeout, context):
        super().__init__(host, port, timeout=timeout, context=context)
        self._pin = ip

    def connect(self):
        raw = socket.create_connection((self._pin, self.port), CONNECT_TIMEOUT)
        raw.settimeout(self.timeout)
        raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except Exception:
            raw.close()
            raise


class Response:
    """An open upstream answer. Use as a context manager; call iter_body() to stream, close() to give up."""

    def __init__(self, status, reason, headers, resp=None, conn=None, client=None, key=None, body=None):
        self.status, self.reason, self.headers = status, reason, headers
        self._resp, self._conn, self._client, self._key, self._body = resp, conn, client, key, body
        self._done = False
        self._released = False

    def header(self, name, default=""):
        lowered = name.lower()
        for key, value in self.headers:
            if key.lower() == lowered:
                return value
        return default

    def iter_body(self, chunk=CHUNK):
        if self._body is not None:
            for index in range(0, len(self._body), chunk):
                yield self._body[index:index + chunk]
            self._done = True
            return
        while self._resp is not None:
            data = self._resp.read1(chunk)       # returns what has arrived: a live stream is passed on at once
            if not data:
                self._done = True
                return
            yield data

    def close(self, reuse=True):
        if self._released:
            return
        self._released = True
        conn, resp = self._conn, self._resp
        keep = bool(reuse and self._done and conn is not None and resp is not None and not resp.will_close)
        if self._client is not None:
            if keep:
                self._client._give_back(self._key, conn)
            else:
                if conn is not None:
                    try:
                        conn.close()
                    except OSError:
                        pass
            self._client._slots.release()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close(reuse=exc[0] is None)
        return False


class UpstreamClient:
    def __init__(self, own_names=lambda: (), resolver=socket.getaddrinfo, local=pt_core.is_local_address,
                 test_upstream=None, tls_context=None, max_connections=MAX_CONNECTIONS, fakes=None):
        self.own_names = own_names
        self.resolver, self.local = resolver, local
        self.test_upstream = dict(test_upstream or {})
        self.tls_context = tls_context
        self._slots = threading.BoundedSemaphore(max_connections)
        self._pool = {}
        self._lock = threading.Lock()
        self.fakes = fakes          # callable(method, target, headers, body) -> Response | None (the in-process self-test site)

    # -- pool
    def _take(self, key):
        now = time.monotonic()
        with self._lock:
            idle = self._pool.get(key) or []
            while idle:
                conn, stamp = idle.pop()
                if now - stamp <= POOL_IDLE:
                    return conn
                try:
                    conn.close()
                except OSError:
                    pass
        return None

    def _give_back(self, key, conn):
        now = time.monotonic()
        with self._lock:
            total = sum(len(v) for v in self._pool.values())
            idle = self._pool.setdefault(key, [])
            if len(idle) >= POOL_PER_KEY or total >= POOL_TOTAL:
                try:
                    conn.close()
                except OSError:
                    pass
                return
            idle.append((conn, now))
            for k in [k for k, v in self._pool.items() if not v]:
                del self._pool[k]

    def close(self):
        with self._lock:
            pools, self._pool = self._pool, {}
        for idle in pools.values():
            for conn, _ in idle:
                try:
                    conn.close()
                except OSError:
                    pass

    # -- resolve
    def addresses(self, target):
        """[(ip, port)] to try, after the SSRF guard (or the test mapping)."""
        reason = pt_core.host_blocked_reason(target.host, self.own_names()) if target.host in self.test_upstream else None
        if target.host in self.test_upstream:
            if reason:
                raise pt_core.Blocked(reason)
            ip, port = self.test_upstream[target.host]
            return [(ip, port)]
        return [(ip, target.real_port) for ip in
                pt_core.check_target(target, self.own_names(), self.resolver, self.local)]

    def _make(self, target, ip, port):
        if target.scheme == "https":
            context = self.tls_context or ssl.create_default_context()
            return _PinnedTLS(target.host, port, ip, READ_TIMEOUT, context)
        return _Pinned(target.host, port, ip, READ_TIMEOUT)

    # -- request
    def open(self, method, target, headers, body=None):
        """Send the request and return a Response (headers read, body not). Raises Blocked / ResolveError /
        UpstreamError. The caller must close() it. Redirects are never followed here: the phone follows them, and every
        hop comes back through the guard."""
        if self.fakes is not None:
            fake = self.fakes(method, target, headers, body)
            if fake is not None:
                return fake
        addresses = self.addresses(target)
        if not self._slots.acquire(timeout=SLOT_WAIT):
            raise UpstreamError("同時接続数の上限です。しばらくしてからもう一度", 503)
        try:
            last = None
            for ip, port in addresses:
                key = (target.scheme, target.host, port, ip)
                for attempt in (0, 1):
                    conn = self._take(key) if attempt == 0 else None
                    reused = conn is not None
                    if conn is None:
                        conn = self._make(target, ip, port)
                    try:
                        conn.putrequest(method, target.request_target, skip_host=True, skip_accept_encoding=True)
                        for name, value in headers:
                            conn.putheader(name, value)
                        conn.endheaders(body if body else None)
                        resp = conn.getresponse()
                    except (http.client.RemoteDisconnected, BrokenPipeError, ConnectionResetError) as exc:
                        conn.close()
                        last = exc
                        if reused:
                            continue                       # a pooled connection the server had closed: once more, fresh
                        break
                    except (OSError, http.client.HTTPException, ssl.SSLError, ValueError) as exc:
                        conn.close()
                        last = exc
                        break
                    else:
                        return Response(resp.status, resp.reason, resp.getheaders(), resp, conn, self, key)
            if isinstance(last, ssl.SSLCertVerificationError):
                raise UpstreamError("証明書を確認できません", 502)
            if isinstance(last, (socket.timeout, TimeoutError)):
                raise UpstreamError("接続がタイムアウトしました", 504)
            raise UpstreamError("接続できません", 502)
        except BaseException:
            self._slots.release()
            raise


# ------------------------------------------------------------------------------------ in-process self-test site
FAKE_HOSTS = (pt_core.SELFTEST_HOST, "pt-selftest-b.invalid")
_PNG = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c6360f8cfc0"
                     "f01f00050001ff89993d1d0000000049454e44ae426082")


def selftest_site(method, target, headers, body):
    """The canned site the self-test and the E2E check use; a Response, or None for any other host. Never touches
    the network, so the only way to reach it is the reserved name below."""
    if target.host not in FAKE_HOSTS:
        return None
    lowered = {k.lower(): v for k, v in headers}
    path = target.path
    items = [("Content-Type", "text/plain; charset=utf-8"), ("Cache-Control", "no-store")]
    status, data = 200, b"ok"
    if path == "/":
        data = ("<!doctype html><html><head><meta charset=\"utf-8\"><title>pt-selftest</title></head><body>"
                "<h1 id=t>PT-SELFTEST-PAGE</h1><img id=i src=\"/i.png\"><script src=\"/s.js\"></script>"
                "<a id=redir href=\"/redir\">redir</a> <a id=file href=\"/file.bin\">file</a> "
                "<a id=other href=\"http://pt-selftest-b.invalid/\">other</a></body></html>").encode()
        items[0] = ("Content-Type", "text/html; charset=utf-8")
        items.append(("Content-Security-Policy", "default-src 'none'"))
        items.append(("Set-Cookie", "st=first; Path=/"))
    elif path == "/s.js":
        data = b"document.documentElement.setAttribute('data-pt-js','1');"
        items[0] = ("Content-Type", "text/javascript")
    elif path == "/i.png":
        data = _PNG
        items[0] = ("Content-Type", "image/png")
    elif path == "/redir":
        status, data = 302, b""
        items.append(("Location", "/final"))
    elif path == "/final":
        data = b"final"
    elif path == "/echo-cookie":
        data = ("cookie=" + lowered.get("cookie", "")).encode()
    elif path == "/set-cookie":
        items.append(("Set-Cookie", "st=second; Path=/"))
    elif path == "/file.bin":
        data = b"x" * 1024
        items[0] = ("Content-Type", "application/octet-stream")
        items.append(("Content-Disposition", 'attachment; filename="selftest.bin"'))
    else:
        status, data = 404, b"not found"
    items.append(("Content-Length", str(len(data))))
    return Response(status, "OK" if status == 200 else "Fake", items, body=b"" if method == "HEAD" else data)
