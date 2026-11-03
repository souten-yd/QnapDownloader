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
import json
import os
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
                fx.hits[path] += 1
                fx.times[path] = time.monotonic()
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

            def reply(self, status, body, ctype):
                self.send_response(status)
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
             "--host-resolver-rules=MAP *.example.com 127.0.0.1:%d, MAP *.example.net 127.0.0.1:%d" % (port, port),
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
        server.engine = engine_supervisor.EngineManager(root, self.store)
        server.jd2 = jd2_control.JD2Controller(server.engine.adapter)
        server.secure_cookies = False
        ctx = root / "ctx"
        ctx.mkdir()
        (ctx / "Dockerfile").write_text("FROM x\n")
        (ctx / "entrypoint.sh").write_text("#!/bin/sh\n")
        mem = root / "meminfo"
        mem.write_text("MemTotal: 16000000 kB\nMemAvailable: 9000000 kB\n")
        fd = FakeDocker()
        runtime = container_runtime.Runtime(fd, isfile=fd.isfile, access=lambda p, m: True, meminfo_path=str(mem))
        fd.images.add(runtime.browser_tag(str(ctx)))
        cdp = self.cdp_port
        server.rb = rb.RemoteBrowser(
            runtime, ctx, meminfo_path=str(mem),
            connect=lambda ip: rb.WebSocket.connect("127.0.0.1", cdp, rb.fetch_ws_path("127.0.0.1", cdp)),
            monitor=False, frame_interval=0.05, start_timeout=10.0)
        webapp.wire_remote_browser(server)
        server.updates = self_update.UpdateManager(root, webapp.ROOT, webapp.VERSION, "x86_64", self.port)
        self.rb = server.rb
        self.version = webapp.VERSION
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
        self.url = "http://127.0.0.1:%d/" % self.port

    # -- helpers for scenarios
    def tabs(self):
        return dict(self.rb._tabs)

    def reset_tabs(self):
        """Between scenarios: close every NAS tab and forget the fixture counters."""
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
            self.page.screenshot(path=str(path))
        except Exception:
            pass
        return path

    # -- touch input
    def tap(self, x, y):
        self.page.touchscreen.tap(x, y)

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
