"""E2E harness: fixture https server + "NAS" Chrome + the real webapp + phone browsers.

Moving parts (all local, nothing leaves the machine):

  phone (Playwright chromium / webkit, touch)  -->  real webapp (webapp.Handler, in-process)
        --> real RemoteBrowser (its own CDP client)  -->  "NAS" Chrome (headless, real engine)
        --> https fixture server (self-signed cert)

Only the container layer is faked (tests/fake_docker.py), exactly like the unit tests do.
The NAS Chrome resolves the fixture hosts (a.example.com / *.example.net) to the fixture server with
``--host-resolver-rules`` and trusts its certificate with ``--ignore-certificate-errors``; both flags are
used for that test browser only. The app itself keeps validating URLs as in production, so fixtures
use real-looking public https names without a port.
"""
import re
import hashlib
import json
import os
import select
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
FIXTURES = HERE / "fixtures"
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

PASSWORD = "correct-horse-battery"
HOST = "a.example.com"
BASE = "https://" + HOST


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def find_chrome(explicit=None):
    """Chrome/Chromium binary for the NAS browser: --nas-chrome, $E2E_NAS_CHROME, PATH, else None (Playwright's)."""
    for cand in (explicit, os.environ.get("E2E_NAS_CHROME")):
        if cand:
            return cand
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    return None


# ---------------------------------------------------------------- fixture https server

def make_png(width, height, color):
    """Solid-colour PNG (pure python, no Pillow)."""
    import struct
    import zlib

    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    r, g, b = (int(color[i:i + 2], 16) for i in (0, 2, 4))
    row = b"\x00" + bytes((r, g, b)) * width
    raw = row * height
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def make_first_load_page(query):
    """/firstload.html: a page shaped like the slow real ones. Query: nodes (default 3000), ads (blocking third-party
    scripts/images on ad hosts, default 6), fonts (web fonts, default 4), imgs (default 24), slowload (ms the
    load event is held back, default 4000), delay (ms every first-party resource takes, default 150)."""
    def num(key, default, high):
        try:
            return max(0, min(high, int(query.get(key, [default])[0])))
        except ValueError:
            return default
    nodes, ads, fonts = num("nodes", 3000, 20000), num("ads", 6, 40), num("fonts", 4, 12)
    imgs, slowload, delay = num("imgs", 24, 200), num("slowload", 4000, 20000), num("delay", 150, 3000)
    sections = max(1, nodes // 10)
    every = max(1, sections // max(1, imgs))
    head = ["<!doctype html><html lang='ja'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>",
            "<title>First load fixture</title>",
            "<link rel='stylesheet' href='/css/site.css?fonts=%d&delay=%d'>" % (fonts, delay)]
    # third-party scripts: the first ones block parsing (a sync tag), the rest are async tags / pixels
    hosts = ["ads.doubleclick.net", "www.googletagmanager.com", "securepubads.g.doubleclick.net", "www.google-analytics.com",
             "static.criteo.net", "cdn.taboola.com", "connect.facebook.net", "platform.twitter.com"]
    for i in range(ads):
        host = hosts[i % len(hosts)]
        if i % 3 == 0:
            head.append("<script src='https://%s/tag%d.js?delay=900'></script>" % (host, i))
        else:
            head.append("<script async src='https://%s/tag%d.js?delay=1200'></script>" % (host, i))
    head.append("</head><body>")
    body = ["<header class='top'><h1>E2E 初回読み込みテスト</h1><nav><a href='/article.html'>記事</a></nav></header>",
            "<img class='hero' width='390' height='260' src='/img/780/520.png?c=3366aa&delay=%d' alt='hero'>" % (delay * 2)]
    for i in range(sections):
        body.append("<section class='s'><h2>見出し %d</h2><p>これは本文の段落です。%d 番目のセクションの文章。<a href='/p/%d'>リンク</a></p>"
                    "<ul><li>項目 A</li><li>項目 B</li><li>項目 C</li></ul></section>" % (i, i, i))
        if i % every == every - 1:
            body.append("<img width='390' height='160' loading='lazy' src='/img/780/320.png?c=%06x&delay=%d' alt='img %d'>" % (
                (i * 7919) % 0xFFFFFF, delay, i))
    if slowload:
        body.append("<img width='1' height='1' src='/img/10/10.png?delay=%d' alt=''>" % slowload)
    body.append("</body></html>")
    return "".join(head + body).encode("utf-8")


def make_first_load_css(query):
    try:
        fonts = max(0, min(12, int(query.get("fonts", ["4"])[0])))
    except ValueError:
        fonts = 4
    rules = []
    for i in range(fonts):
        rules.append("@font-face{font-family:'Fx%d';src:url('/font/f%d.woff2?delay=350') format('woff2');font-weight:%d}" % (i, i, 400 + (i % 3) * 100))
    rules.append("body{margin:0;font:16px/1.6 'Fx0',sans-serif}.top{background:#223;color:#fff;padding:8px 12px}"
                 ".hero{display:block;width:100%;height:auto}.s{padding:6px 12px;border-bottom:1px solid #ddd}"
                 ".s h2{font:600 18px 'Fx1',sans-serif;margin:4px 0}.top{background-image:url('/img/40/40.png?c=223344&delay=200')}")
    for i in range(600):
        rules.append(".c%d .x%d>a:hover{color:#%06x;margin:%dpx}@media (min-width:%dpx){.c%d{padding:%dpx}}" % (
            i, i, (i * 40503) % 0xFFFFFF, i % 9, 600 + i, i, i % 7))
    return "".join(rules).encode("utf-8")


class FixtureServer:
    def __init__(self, workdir):
        cert, key = Path(workdir) / "cert.pem", Path(workdir) / "key.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
                        "-nodes", "-keyout", str(key), "-out", str(cert), "-days", "2", "-subj", "/CN=a.example.com",
                        "-addext", "subjectAltName=DNS:*.example.com,DNS:*.example.net"],
                       check=True, capture_output=True)
        self.hits = Counter()          # path -> GET count (a reload shows up here)
        self.times = {}                # path -> monotonic time of the last GET
        self.beacons = []              # (monotonic, event, detail) from the pages' own scripts
        self.pt_seen = []              # v0.1.57: (Host, path, Cookie) of every /ptx/ request (what the NAS sent upstream)
        fx = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_GET(self):
                parts = urlsplit(self.path)
                path = parts.path
                if path == "/__beacon":
                    q = parse_qs(parts.query)
                    fx.beacons.append((time.monotonic(), q.get("e", [""])[0], q.get("p", [""])[0]))
                    return self.reply(204, b"", "text/plain")
                if path.startswith("/ptx/"):
                    return self.pt_route(path)
                fx.hits[path] += 1
                fx.times[path] = time.monotonic()
                q = parse_qs(parts.query)
                try:
                    wait = min(20000, max(0, int(q.get("delay", ["0"])[0])))
                except ValueError:
                    wait = 0
                if wait:
                    time.sleep(wait / 1000.0)         # v0.1.54: a slow resource (the NAS's line is slower than ours)
                if path.startswith("/go/"):
                    # v0.1.65: a hoster redirect for the long-press probe scenarios: /go/<n>[?delay=ms] answers 302 to the
                    # n-th Rapidgator file (the NAS Chrome intercepts that request before any network access).
                    # /go/none ends on a plain page without hoster links.
                    ident = path[4:]
                    if ident == "none":
                        return self.reply(302, b"", "text/plain", [("Location", "/article2.html")])
                    return self.reply(302, b"", "text/plain", [(
                        "Location", "https://rapidgator.net/file/%s" % hashlib.md5(ident.encode()).hexdigest())])
                if path.endswith(".js"):
                    return self.reply(200, b"/* tag */", "application/javascript")
                if path.startswith("/font/"):
                    return self.reply(200, b"wOF2" + b"\x00" * 2048, "font/woff2")
                if path == "/css/site.css":
                    return self.reply(200, make_first_load_css(q), "text/css")
                if path == "/firstload.html":
                    return self.reply(200, make_first_load_page(q), "text/html; charset=utf-8")
                if path.startswith("/img/"):
                    try:
                        w, h = (int(v) for v in path[5:].replace(".png", "").split("/"))
                        color = parse_qs(parts.query).get("c", ["888888"])[0]
                        return self.reply(200, make_png(min(w, 2000), min(h, 2000), color), "image/png")
                    except (ValueError, IndexError):
                        return self.reply(404, b"", "text/plain")
                name = "article2.html" if path.startswith("/p/") else path.lstrip("/") or "article.html"
                file = FIXTURES / name
                if "/" in name or not file.is_file() or file.suffix != ".html":
                    return self.reply(404, b"not found", "text/plain")
                return self.reply(200, file.read_bytes(), "text/html; charset=utf-8")

            def pt_route(self, path):
                """v0.1.57: pages for the passthrough scenarios (the same server answers every fixture host name)."""
                host, cookie = self.headers.get("Host", ""), self.headers.get("Cookie", "")
                fx.pt_seen.append((host, path, cookie))
                extra = []
                if path == "/ptx/page.html":
                    extra.append(("Set-Cookie", "ptc=A1; Path=/"))
                    body = ("<!doctype html><html><head><meta charset='utf-8'><title>PT Fixture A</title></head><body>"
                            "<h1 id=t>PT-FIXTURE-PAGE</h1><img id=pic width=20 height=20 src='https://b.example.net/ptx/pic.png'>"
                            "<p><a id=l2 href='/ptx/page2.html'>next</a> <a id=lredir href='/ptx/redir'>redir</a> "
                            "<a id=ldl href='/ptx/dl.bin'>download</a></p>"
                            "<script src='/ptx/app.js'></script></body></html>").encode()
                    return self.reply(200, body, "text/html; charset=utf-8", extra)
                if path == "/ptx/page2.html":
                    body = ("<!doctype html><html><head><meta charset='utf-8'><title>PT Fixture A2</title></head><body>"
                            "<h1 id=t>PT-FIXTURE-PAGE-2</h1></body></html>").encode()
                    return self.reply(200, body, "text/html; charset=utf-8")
                if path == "/ptx/app.js":
                    js = ("document.documentElement.setAttribute('data-pt-js','1');"
                          "fetch('/ptx/echo').then(r=>r.text()).then(t=>document.documentElement.setAttribute('data-pt-echo-a',t));"
                          "fetch('https://b.example.net/ptx/echo').then(r=>r.text()).then(t=>document.documentElement.setAttribute('data-pt-echo-b',t))"
                          ".catch(e=>document.documentElement.setAttribute('data-pt-echo-b','ERR '+e));")
                    return self.reply(200, js.encode(), "application/javascript")
                if path == "/ptx/echo":
                    return self.reply(200, ("host=%s;cookie=%s" % (host, cookie)).encode(), "text/plain; charset=utf-8",
                                      [("Access-Control-Allow-Origin", "*")])
                if path == "/ptx/pic.png":
                    return self.reply(200, make_png(4, 4, "3366aa"), "image/png")
                if path == "/ptx/redir":
                    return self.reply(302, b"", "text/plain", [("Location", "/ptx/page2.html")])
                if path == "/ptx/dl.bin":
                    return self.reply(200, b"x" * 2048, "application/octet-stream",
                                      [("Content-Disposition", 'attachment; filename="e2e.bin"')])
                return self.reply(404, b"not found", "text/plain")

            def reply(self, status, body, ctype, extra=()):
                self.send_response(status)
                for name, value in extra:
                    self.send_header(name, value)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

        self.port = free_port()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self.httpd.daemon_threads = True
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(cert), str(key))
        self.httpd.socket = ctx.wrap_socket(self.httpd.socket, server_side=True)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def count(self, path):
        return self.hits[path]

    def beacon_count(self, event, detail=None):
        return sum(1 for _, e, d in self.beacons if e == event and (detail is None or d == detail))

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


# ---------------------------------------------------------------- the whole environment

class Env:
    """Fixture server + NAS Chrome + real webapp. One per run; scenarios share it (tabs are reset between them)."""

    def __init__(self, nas_chrome=None, workdir=None):
        self.tmp = Path(workdir or tempfile.mkdtemp(prefix="qd-e2e-"))
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.fx = FixtureServer(self.tmp)
        self.cdp_port = free_port()
        self.setup_done = False
        self.blocked = set()        # {"mjpeg", "sse"}: the stream never sends a byte (what a buffering proxy does)
        self.stream_hits = Counter()
        self._start_nas_chrome(find_chrome(nas_chrome))
        self._start_webapp()

    # -- NAS browser
    def _start_nas_chrome(self, binary):
        if binary is None:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                binary = p.chromium.executable_path
        self.nas_chrome_path = binary
        self.chrome_udd = tempfile.mkdtemp(prefix="qd-e2e-chrome-")
        port = self.fx.port
        self.chrome = subprocess.Popen(
            [binary, "--headless=new", "--no-sandbox", "--remote-debugging-port=%d" % self.cdp_port,
             "--user-data-dir=" + self.chrome_udd, "--no-proxy-server", "--disable-gpu",
             "--no-first-run", "--no-default-browser-check", "--ignore-certificate-errors",
             "--host-resolver-rules=" + ", ".join("MAP %s 127.0.0.1:%d" % (h, port) for h in (
                 "*.example.com", "*.example.net", "*.doubleclick.net", "*.googletagmanager.com", "*.google-analytics.com",
                 "*.criteo.net", "*.taboola.com", "*.facebook.net", "*.twitter.com")),
             "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        import remote_browser as rb
        for _ in range(100):
            try:
                rb.fetch_ws_path("127.0.0.1", self.cdp_port)
                return
            except Exception:
                if self.chrome.poll() is not None:
                    raise RuntimeError("NAS Chrome exited early: " + binary)
                time.sleep(0.2)
        raise RuntimeError("NAS Chrome did not open its debugging port")

    # -- real webapp, fake container layer
    def _start_webapp(self):
        import container_runtime
        import engine_supervisor
        import jd2_control
        import remote_browser as rb
        import self_update
        import storage
        import webapp
        from fake_docker import FakeDocker
        webapp.Handler.log_message = lambda *a, **k: None   # keep the run output compact
        ThreadingHTTPServer.handle_error = lambda *a, **k: None   # viewers that leave mid-response (BrokenPipe)

        root = self.tmp / "data"
        root.mkdir()
        allowed = root / "allowed"
        allowed.mkdir()
        os.environ["DOWNLOADER_ALLOWED_ROOTS"] = str(allowed)
        self.store = storage.Store(root)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), webapp.Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        server = self.server
        server.store = self.store
        server.data_dir = root
        server.engine = engine_supervisor.EngineManager(root, self.store)
        server.jd2 = jd2_control.JD2Controller(server.engine.adapter)
        server.secure_cookies = False
        # TEST ONLY (v0.1.57): passthrough on http://localhost:<listener port>, upstream = the fixture server. These plain
        # attributes are never set by main() or the start scripts (the QPKG contract test checks that).
        server.pt_port = 0
        server.pt_test_host = "localhost"
        server.pt_test_upstream = {h: ("127.0.0.1", self.fx.port) for h in ("a.example.com", "b.example.net")}
        server.pt_tls_context = ssl.create_default_context(cafile=str(self.tmp / "cert.pem"))
        # TEST ONLY (same as the hidden --test-passkey-host flag): the passkey scenario uses http://localhost.
        server.passkey_test_hosts = frozenset(["localhost"])
        ctx = root / "ctx"
        ctx.mkdir()
        (ctx / "Dockerfile").write_text("FROM x\n")
        (ctx / "entrypoint.sh").write_text("#!/bin/sh\n")
        mem = root / "meminfo"
        mem.write_text("MemTotal: 16000000 kB\nMemAvailable: 9000000 kB\n")
        fd = FakeDocker()
        self.fake_docker = fd
        runtime = container_runtime.Runtime(fd, isfile=fd.isfile, access=lambda p, m: True, meminfo_path=str(mem))
        fd.images.add(runtime.browser_tag(str(ctx)))
        cdp = self.cdp_port
        server.rb = rb.RemoteBrowser(
            runtime, ctx, meminfo_path=str(mem),
            connect=lambda ip: rb.WebSocket.connect("127.0.0.1", cdp, rb.fetch_ws_path("127.0.0.1", cdp)),
            monitor=False, frame_interval=0.05, start_timeout=10.0)
        webapp.wire_remote_browser(server)
        self._wrap_streams(webapp)
        server.updates = self_update.UpdateManager(root, webapp.ROOT, webapp.VERSION, "x86_64", self.port)
        self.rb = server.rb
        self._meminfo_path, self._shake = mem, None
        self.version = webapp.VERSION
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
        self.url = "http://127.0.0.1:%d/" % self.port

    def shake(self, on=True, period=0.3):
        """v0.1.59: makes what changes on a real NAS change here too: free memory (/proc/meminfo) and the container's
        `docker stats` figure every `period` s. With the state hub's stable signature this must not wake /api/state."""
        if not on:
            if self._shake is not None:
                self._shake.set()
                self._shake = None
            return
        if self._shake is not None:
            return
        stop = self._shake = threading.Event()

        def run():
            n = 0
            while not stop.wait(period):
                n += 1
                try:
                    self._meminfo_path.write_text("MemTotal: 16000000 kB\nMemAvailable: %d kB\n" % (9000000 - (n * 7919) % 400000))
                    self.fake_docker.stats_out = "%dMiB / 2GiB|%.2f%%" % (500 + n % 40, 24.0 + (n % 17) / 3.0)
                except OSError:
                    pass
        threading.Thread(target=run, daemon=True).start()

    def chrome_pss_mib(self):
        """v0.1.65: proportional memory (Pss, MiB) of every process of the NAS-side Chrome (found by its user-data-dir)."""
        total = 0
        marker = ("--user-data-dir=" + self.chrome_udd).encode()
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open("/proc/%s/cmdline" % entry, "rb") as handle:
                    if marker not in handle.read():
                        continue
                with open("/proc/%s/smaps_rollup" % entry) as handle:
                    for line in handle:
                        if line.startswith("Pss:"):
                            total += int(line.split()[1])
                            break
            except (OSError, ValueError):
                continue
        return total / 1024.0

    def model_container_memory(self, on=True, limit_mib=2048, base_mib=0.0):
        """v0.1.65: the fake `docker stats` shows the real memory (Pss) of the NAS-side Chrome plus `base_mib`, against a
        `limit_mib` container limit, every 100 ms (the real container's cgroup is not there). Scenarios that measure
        memory (the self-test 「タブの容量」) switch it on."""
        if not on:
            stop = getattr(self, "_mem_model", None)
            if stop is not None:
                stop.set()
                self._mem_model = None
            return
        if getattr(self, "_mem_model", None) is not None:
            return
        stop = self._mem_model = threading.Event()

        def run():
            while not stop.wait(0.1):
                used = self.chrome_pss_mib() + base_mib
                self.fake_docker.stats_out = "%dMiB / %dGiB|%.2f%%" % (used, limit_mib // 1024, used * 100.0 / limit_mib)
        threading.Thread(target=run, daemon=True).start()

    def _wrap_streams(self, webapp):
        """Scenarios can make a live transport 'buffered by a proxy': the request is accepted and the response
        never starts (no headers, no byte), until the phone gives up and closes the connection."""
        env = self
        for attr, kind in (("stream_mjpeg", "mjpeg"), ("stream_sse", "sse")):
            original = getattr(webapp.Handler, attr)

            def wrapper(handler, tab_id, _original=original, _kind=kind):
                env.stream_hits[_kind] += 1
                if _kind not in env.blocked:
                    return _original(handler, tab_id)
                handler.close_connection = True
                end = time.monotonic() + 20
                while time.monotonic() < end and _kind in env.blocked:
                    readable, _, _ = select.select([handler.connection], [], [], 0.2)
                    if readable:
                        try:
                            if not handler.connection.recv(1, socket.MSG_PEEK):
                                return        # the phone closed the connection
                        except OSError:
                            return
                return
            setattr(webapp.Handler, attr, wrapper)

    # -- helpers for scenarios
    def tabs(self):
        return dict(self.rb._tabs)

    def reset_tabs(self):
        """Between scenarios: close every NAS tab and forget the fixture counters."""
        self.blocked.clear()
        self.stream_hits.clear()
        try:                           # v0.1.57: every scenario starts with the passthrough off and no sessions
            import webapp
            self.store.set_option("pt_enabled", False)
            manager = webapp.server_pt(self.server)
            manager.sync()
            manager.state.revoke_all()
            del self.fx.pt_seen[:]
        except Exception:
            pass
        self.fx.hits.clear()
        self.fx.times.clear()
        del self.fx.beacons[:]
        for ident in list(self.rb._tabs):
            try:
                self.rb.close_tab(ident)
            except Exception:
                pass

    def nas_url(self, ident=None):
        tabs = self.tabs()
        tab = tabs.get(ident) if ident else (list(tabs.values())[-1] if tabs else None)
        return tab.url if tab else ""

    def close(self):
        self.shake(False)
        self.model_container_memory(False)
        for fn in (lambda: self.rb.shutdown(), lambda: self.server.shutdown(), lambda: self.server.server_close(),
                   lambda: self.store.close(), lambda: self.fx.close()):
            try:
                fn()
            except Exception:
                pass
        try:
            self.chrome.terminate()
            self.chrome.wait(timeout=10)
        except Exception:
            self.chrome.kill()
        shutil.rmtree(self.chrome_udd, ignore_errors=True)
        shutil.rmtree(self.tmp, ignore_errors=True)


# ---------------------------------------------------------------- phone (Playwright)

class Skip(Exception):
    """A scenario that cannot run on this engine / machine; reported as SKIP with the reason."""


# Console noise that is expected from the fixtures (unreadable resources, favicon, ...), never a real app error.
CONSOLE_ALLOW = (
    "favicon",
    "the server responded with a status of 502",     # a fixture resource the NAS browser cannot read
    "the server responded with a status of 404",     # missing fixture resource (e.g. favicon via the mirror)
    # browser back restores the mirror frame's earlier address for an instant before the app puts the
    # current copy back (keepMirrorFrame): an expired copy (410) or the blocked app root in the frame
    "the server responded with a status of 410",
    "violates the following Content Security Policy directive: \"frame-ancestors 'none'\"",
)
# WebKit reports a request the app itself aborted (the /api/state long-poll when the page or view
# changes) as "Fetch API cannot load <own /api/...> due to access control checks", sometimes as a
# page error. Only our own API on the app origin is allowed; anything else still fails the scenario.
WEBKIT_ABORT = re.compile(r"/127\.0\.0\.1:\d+/api/[a-z/]+\?[^ ]* due to access control checks")


class Phone:
    """One phone page: console error collection, login, helpers. Created by the runner per scenario."""

    def __init__(self, env, page, context, engine, out_dir, name):
        self.env, self.page, self.context, self.engine = env, page, context, engine
        self.out, self.name = out_dir, name
        self.errors = []
        self.notes = []
        self.csrf = None
        page.set_default_timeout(15000)
        page.on("console", self._console)
        page.on("pageerror", self._pageerror)

    def _pageerror(self, e):
        text = str(e)
        if self.engine == "webkit" and WEBKIT_ABORT.search(text):
            return
        self.errors.append("pageerror: %s" % text)

    def _console(self, msg):
        if msg.type != "error":
            return
        text = msg.text or ""
        if any(token in text for token in CONSOLE_ALLOW):
            return
        if self.engine == "webkit" and WEBKIT_ABORT.search(text):
            return
        self.errors.append("console.error: %s @ %s" % (text[:200], json.dumps(msg.location)))

    def take_errors(self, *needles):
        """Remove and return collected console errors containing any needle (errors a scenario provokes on purpose)."""
        hit = [e for e in self.errors if any(n in e for n in needles)]
        self.errors = [e for e in self.errors if e not in hit]
        return hit

    def log(self, text):
        self.notes.append(text)

    # -- waiting
    def wait(self, fn, timeout=10.0, what="condition", interval=0.1):
        """Poll fn() until truthy. AssertionError names what was awaited and the last value."""
        end = time.monotonic() + timeout
        last = None
        while True:
            try:
                last = fn()
            except Exception as exc:       # a page in the middle of navigating, etc.
                last = "exception: %s" % str(exc)[:120]
            if last and not (isinstance(last, str) and last.startswith("exception:")):
                return last
            if time.monotonic() >= end:
                raise AssertionError("timeout after %.1fs waiting for %s (last: %r)" % (timeout, what, last))
            self.page.wait_for_timeout(int(interval * 1000))

    def js(self, expr, arg=None):
        return self.page.evaluate(expr, arg) if arg is not None else self.page.evaluate(expr)

    # -- app session
    def login(self):
        page = self.page
        page.goto(self.env.url)
        self.wait(lambda: page.locator("#setupPassword").is_visible() or page.locator("#password").is_visible()
                  or page.evaluate("state.authed === true"), 15, "login form or session")
        if page.locator("#setupPassword").is_visible():
            page.fill("#setupPassword", PASSWORD)
            page.click("#setupButton")
            self.wait(lambda: page.locator("#password").is_visible() or page.evaluate("state.authed === true"), 15,
                      "login form after setup")
        if page.locator("#password").is_visible():
            page.fill("#password", PASSWORD)
            page.click("#loginButton")
        self.wait(lambda: self.js("state.authed === true"), 10, "login")
        self.csrf = self.js("fetch('/api/session').then(r=>r.json()).then(j=>j.csrf)")

    def post(self, path, body):
        return self.page.evaluate(
            """async([p,b,c])=>{const r=await fetch(p,{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':c},
            body:JSON.stringify(b)});return {status:r.status,text:(await r.text()).slice(0,300)}}""",
            [path, body, self.csrf])

    def setting(self, key, value):
        res = self.post("/api/settings/options", {key: value})
        assert res["status"] == 200, "setting %s failed: %s" % (key, res)

    def nas_ready(self):
        """Run the (fake-container) setup once per run; the real Chrome is started by the first tab that is opened."""
        if self.env.setup_done:
            return
        res = self.post("/api/rb/setup", {"confirmed": True})
        assert res["status"] in (202, 409), res
        self.wait(lambda: not self.env.rb._setup_active, 30, "NAS browser setup finished")
        assert self.env.rb._error is None, "setup error: %s" % self.env.rb._error
        self.env.setup_done = True

    def start(self, display="mirror"):
        """Log in, choose the display, start the NAS browser, land on the web tab page."""
        self.login()
        self.nas_ready()
        self.setting("rb_default_mode", display)
        self.page.reload()
        self.page.wait_for_selector("#omnibox", state="attached")
        self.wait(lambda: self.js("state.authed === true && state.settings && state.settings.rb_default_mode") == display,
                  10, "settings loaded")

    def open(self, path_or_url, wait_tab=True):
        url = path_or_url if path_or_url.startswith("http") else BASE + path_or_url
        omni = self.page.locator("#omnibox").first
        omni.click()
        omni.fill(url)
        omni.press("Enter")
        if wait_tab:
            self.wait(lambda: self.js("(()=>{const t=activeWebTab();return !!(t&&t.rb)})()"), 15, "NAS tab attached")
            self.wait(lambda: self.tab_id() in self.env.tabs(), 5, "tab known to the server")
        return self.tab_id()

    def tab_id(self):
        return self.js("(()=>{const t=activeWebTab();return t&&t.rb?t.rb.id:null})()")

    def display(self):
        return self.js("(()=>{const t=activeWebTab();return t&&t.rb?tabDisplay(t):null})()")

    def shot(self, name):
        path = Path(self.out) / ("%s-%s-%s.png" % (self.engine, self.name, name))
        try:
            # caret="initial": the default caret hiding injects a <style>, which WebKit reports as a CSP error
            self.page.screenshot(path=str(path), caret="initial", animations="allow")
        except Exception:
            pass
        return path

    # -- touch input
    def tap(self, x, y):
        self.page.touchscreen.tap(x, y)

    # -- v0.1.63: mouse / keyboard input (desktop and iPad-with-keyboard scenarios)
    def click(self, x, y, button="left"):
        self.page.mouse.click(x, y, button=button)

    def press(self, combo):
        self.page.keyboard.press(combo)

    def require_touch(self):
        """iPad scenarios: the engine's touch emulation must report a coarse primary pointer (the layout follows it)."""
        if not self.js("matchMedia('(pointer: coarse)').matches"):
            raise Skip("this engine's touch emulation does not report (pointer: coarse)")

    def require_mouse(self):
        if not self.js("matchMedia('(pointer: fine)').matches"):
            raise Skip("this engine does not report (pointer: fine) for the desktop context")

    def cdp_touch(self, points):
        """points: [(x, y, wait_ms), ...]; one finger, touchStart..touchEnd (Chromium only)."""
        if self.engine != "chromium":
            raise Skip("synthetic multi-step touch needs CDP (Chromium only)")
        cdp = self.context.new_cdp_session(self.page)
        try:
            x, y, _ = points[0]
            cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": x, "y": y}]})
            for x, y, wait in points[1:]:
                self.page.wait_for_timeout(wait)
                cdp.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [{"x": x, "y": y}]})
            cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
        finally:
            cdp.detach()

    # -- mirror helpers
    def mirror_text(self):
        return self.js("(()=>{try{const d=mirrorDoc();return d?d.body.innerText:''}catch(e){return ''}})()")

    def mirror_scroll_top(self):
        return self.js("(()=>{const d=mirrorDoc();return d?mirrorScroller(d).scrollTop:-1})()")

    def mirror_point(self, selector_js):
        """Phone-page coordinates (centre) of the first mirror element for which selector_js(doc) returns an element."""
        return self.page.evaluate(
            """(src)=>{const f=document.getElementById('mirrorFrame');const d=mirrorDoc();if(!d)return null;
            const el=(new Function('doc','return ('+src+')(doc)'))(d);if(!el)return null;
            const r=el.getBoundingClientRect();const fr=f.getBoundingClientRect();
            if(r.width<=0||r.height<=0)return null;
            return {x:fr.left+r.left+r.width/2,y:fr.top+r.top+r.height/2,text:(el.textContent||'').trim().slice(0,40)}}""",
            selector_js)

    def suppress_ios_click(self):
        """iOS Safari sends no click for elements without href: emulate that so taps must work from pointer events."""
        self.page.evaluate(
            "()=>{const w=document.getElementById('mirrorFrame').contentWindow;"
            "w.addEventListener('click',e=>e.stopImmediatePropagation(),true)}")
