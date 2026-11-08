"""E2E scenarios. Each one is a function ``fn(ph)`` taking a ``harness.Phone``; it raises AssertionError to fail
and ``harness.Skip`` to skip. Register new ones with the ``@scenario`` decorator (see docs/testing.md).

Phone input is always touch (Playwright touchscreen / CDP touch), never mouse clicks: taps on iOS have no
``click`` for elements without href, which is the class of bug this suite exists to catch.
"""
import time

from harness import BASE, PASSWORD, Skip

SCENARIOS = []


def scenario(name, engines=("chromium", "webkit"), display="mirror", device="phone"):
    """Register a scenario. ``display`` is the Settings default display the phone starts with.
    v0.1.63: ``device`` = "phone" (iPhone, touch), "ipad" (820x1180 touch) or "desktop" (1280x800, mouse + keyboard)."""
    def deco(fn):
        SCENARIOS.append({"name": name, "fn": fn, "engines": engines, "display": display, "device": device})
        return fn
    return deco


# ------------------------------------------------------------------ app shell

ROUTES = ("#/home", "#/downloads", "#/links", "#/settings/storage", "#/settings/updates",
          "#/settings/diagnostics", "#/settings/diag/selftest", "#/settings/engine", "#/settings/https", "#/settings/share",
          "#/settings/browser", "#/settings/accounts", "#/settings/system")


@scenario("settings-routes")
def settings_routes(ph):
    """Every management route renders without console errors or error toasts."""
    for route in ROUTES:
        ph.js("(r)=>{location.hash=r}", route)
        ph.page.wait_for_timeout(350)
        toasts = ph.page.eval_on_selector_all("#notice .toast.error", "els=>els.map(e=>e.dataset.message||e.textContent)")
        assert not toasts, "%s: error toast %s" % (route, toasts)
    assert ph.js("state.authed === true")


# ------------------------------------------------------------------ display modes

@scenario("display-reader", display="reader")
def display_reader(ph):
    ph.open("/article.html")
    ph.wait(lambda: ph.display() == "reader", 5, "display reader")
    ph.wait(lambda: ph.js("document.querySelectorAll('#readerBody .reader-p').length") >= 3, 15, "reader paragraphs")
    assert ph.js("document.getElementById('readerTitle').textContent").strip() != ""
    assert "E2E" in ph.js("document.getElementById('readerBody').innerText")
    # the picture is loaded through the Hub (srcset image chosen by the NAS browser)
    ph.wait(lambda: ph.js("(()=>{const i=document.querySelector('#readerBody img.reader-img');return !!i&&i.complete&&i.naturalWidth>0})()"),
            15, "reader image loaded")


@scenario("mirror-late-content", display="mirror")
def mirror_late_content(ph):
    """A page that is empty at DOMContentLoaded and draws its content a moment later stays in the mirror display
    (an early empty copy switched it to the fast display with 「このページはミラー表示に対応していない」)."""
    ph.open("/late.html")
    ph.wait(lambda: "E2E 後から描くページ" in ph.mirror_text(), 15, "late content in the mirror")
    ph.page.wait_for_timeout(500)
    assert ph.display() == "mirror", "switched to %s" % ph.display()
    toasts = ph.page.eval_on_selector_all("#notice .toast", "els=>els.map(e=>e.dataset.message||e.textContent)")
    assert not any("ミラー表示に対応していない" in t for t in toasts), toasts


@scenario("display-mirror", display="mirror")
def display_mirror(ph):
    ph.open("/article.html")
    ph.wait(lambda: ph.display() == "mirror", 5, "display mirror")
    ph.wait(lambda: "E2E テスト記事" in ph.mirror_text(), 15, "mirror content")
    ph.wait(lambda: ph.js("(()=>{const d=mirrorDoc();const i=d&&d.querySelector('img');return !!i&&i.complete&&i.naturalWidth>0})()"),
            15, "mirror image loaded")
    assert ph.js("getComputedStyle(document.getElementById('mirrorWrap')).display") != "none"


@scenario("mirror-images-kept", display="mirror")
def mirror_images_kept(ph):
    """v0.1.73: later copies of the page keep the shown image elements and never ask for an image again (the
    image URL gained the content hash on the second copy and the whole body was rebuilt, so images reloaded)."""
    ph.open("/article.html")
    ph.wait(lambda: "E2E テスト記事" in ph.mirror_text(), 15, "mirror content")
    ph.wait(lambda: ph.js("(()=>{const d=mirrorDoc();return !!d&&d.images.length>0&&"
                          "Array.from(d.images).every(i=>i.complete)})()"), 15, "mirror images loaded")
    ph.page.wait_for_timeout(1500)            # the Hub's prefetch and the follow-up full copy
    before = ph.js("(()=>{const d=mirrorDoc();window.__qdImgs=Array.from(d.images);return window.__qdImgs.length})()")
    res0 = ph.js("mirrorResEntries(mirrorDoc())")
    for _ in range(2):
        ph.js("(()=>{runMirror(remoteTab(),{immediate:true,force:true,quiet:true});return 1})()")
        ph.page.wait_for_timeout(1200)
    same = ph.js("(()=>{const d=mirrorDoc();const s=new Set(Array.from(d.images));"
                 "return window.__qdImgs.filter(i=>s.has(i)).length})()")
    res = ph.js("mirrorResEntries(mirrorDoc())")
    summary = ph.js("mirrorDiagSummary(remoteTab().rb)")
    ph.notes.append("imgs %d kept %d res %s diag %s" % (before, same, res, summary and summary.get("last")))
    assert same == before, "image elements replaced: %d of %d kept" % (same, before)
    assert res["refetch"] == res0["refetch"] == 0, "images asked again: %s -> %s" % (res0, res)
    perf = ph.js("(()=>{setPerf(true);return perfText()})()")
    assert "コピー " in perf and "端末: 画像要求" in perf and "Hub: 配信" in perf, "%s / %s" % (perf, ph.js(
        "(()=>{const rb=remoteTab().rb;return JSON.stringify({d:rb.mirrorDiag||null,lines:mirrorDiagLines(rb)})})()"))


@scenario("display-tiles", display="fast")
def display_tiles(ph):
    ph.open("/article.html")
    ph.wait(lambda: ph.display() == "tiles", 5, "display tiles")
    ph.wait(lambda: ph.js("[...document.querySelectorAll('#tileScroll img')].filter(i=>i.complete&&i.naturalWidth>0).length") >= 1,
            20, "tile images loaded")


LIVE_SHOWN = "(()=>{const i=document.getElementById('remoteImg');return !i.hidden&&i.naturalWidth>0})()"


@scenario("display-live", display="live")
def display_live(ph):
    """The live picture arrives over whichever transport works here (v0.1.68: WebSocket first)."""
    tab = ph.open("/article.html")
    ph.wait(lambda: ph.display() == "live", 5, "display live")
    ph.wait(lambda: ph.js(LIVE_SHOWN), 20, "live frame visible")
    ph.wait(lambda: ph.env.tabs()[tab].seq > 0, 10, "server has produced frames (seq>0)")
    kind = ph.js("state.rbStream ? state.rbStream.kind : 'poll'")
    assert kind == "ws", "expected the WebSocket transport here, got %s" % kind
    ph.notes.append("transport " + kind)
    perf = ph.js("(()=>{setPerf(true);return perfText()})()")
    assert "ライブ(WebSocket)" in perf, perf


def blob_tracker(ph):
    """Counts the object URLs the app has alive (created and not yet revoked)."""
    ph.js("""()=>{
      if (window.__urls) return;
      window.__urls = new Set(); window.__urlMax = 0;
      const make = URL.createObjectURL.bind(URL), drop = URL.revokeObjectURL.bind(URL);
      URL.createObjectURL = function (b) { const u = make(b); window.__urls.add(u); window.__urlMax = Math.max(window.__urlMax, window.__urls.size); return u; };
      URL.revokeObjectURL = function (u) { window.__urls.delete(u); return drop(u); };
    }""")


@scenario("live-ws", display="live")
def live_ws(ph):
    """v0.1.68: the live display reads the screencast from the Hub's WebSocket: >= 3 fps on a page that animates, the
    JPEG frames are drawn as blob images without leaking object URLs, the transport is named in the speed line, and the
    socket is closed while the page is hidden and opened again when it is shown."""
    blob_tracker(ph)
    tab = ph.open("/anim.html")
    ph.wait(lambda: ph.display() == "live", 5, "display live")
    ph.wait(lambda: ph.js("state.rbStream && state.rbStream.kind") == "ws", 10, "WebSocket transport")
    ph.wait(lambda: ph.js(LIVE_SHOWN), 15, "WebSocket frame visible")
    assert ph.js("document.getElementById('remoteImg').src").startswith("blob:"), "frames are drawn as blob images"
    ph.wait(lambda: ph.js("state.rbStream.loaded") is True, 5, "first frame seen")
    f0, t0 = ph.js("state.rbStream.frames"), time.monotonic()
    ph.page.wait_for_timeout(3000)
    f1, t1 = ph.js("state.rbStream.frames"), time.monotonic()
    fps = (f1 - f0) / (t1 - t0)
    ph.notes.append("ws %.1f fps" % fps)
    assert fps >= 3, "only %.1f fps over WebSocket" % fps
    assert ph.env.stream_hits["ws"] >= 1 and ph.env.stream_hits["sse"] == 0 and ph.env.stream_hits["mjpeg"] == 0, dict(ph.env.stream_hits)
    perf = ph.js("(()=>{setPerf(true);return perfText()})()")
    assert perf.startswith("ライブ(WebSocket)") and "fps" in perf, perf
    pref = ph.js("JSON.parse(localStorage.getItem(RB_LIVE_PREF_KEY))")
    assert pref["t"] == "ws" and not pref.get("fail"), pref
    assert ph.js("window.__urlMax") <= 3 and ph.js("window.__urls.size") <= 2, (ph.js("window.__urlMax"), ph.js("window.__urls.size"))
    assert ph.env.tabs()[tab].streams == 1
    # hidden: the socket is closed and the Hub's stream ends
    ph.js("""()=>{Object.defineProperty(document,'visibilityState',{value:'hidden',configurable:true});
      document.dispatchEvent(new Event('visibilitychange'));}""")
    ph.wait(lambda: ph.js("state.rbStream") is None, 5, "stream closed while hidden")
    ph.wait(lambda: ph.env.tabs()[tab].streams == 0, 8, "Hub stream ended")
    assert ph.js("window.__urls.size") == 0, "object URLs alive after closing: %s" % ph.js("window.__urls.size")
    # shown again: a new socket and frames again
    hits = ph.env.stream_hits["ws"]
    ph.js("""()=>{Object.defineProperty(document,'visibilityState',{value:'visible',configurable:true});
      document.dispatchEvent(new Event('visibilitychange'));}""")
    ph.wait(lambda: ph.js("state.rbStream && state.rbStream.kind") == "ws", 10, "WebSocket again")
    ph.wait(lambda: (ph.js("state.rbStream.frames") or 0) >= 3, 10, "frames after showing again")
    assert ph.env.stream_hits["ws"] == hits + 1


@scenario("live-sse-fallback", display="live")
def live_sse_fallback(ph):
    """The WebSocket never delivers (nothing answers the upgrade): after 2.5 s the client reads the event stream and
    draws the frames; the failure is remembered, so a new app start goes straight to SSE."""
    ph.env.blocked.add("ws")
    t0 = time.monotonic()
    tab = ph.open("/article.html")
    ph.wait(lambda: ph.display() == "live", 5, "display live")
    ph.wait(lambda: ph.js("state.rbStream && state.rbStream.kind") == "sse", 10, "switched to SSE")
    ph.wait(lambda: ph.js(LIVE_SHOWN), 15, "SSE frame visible")
    assert ph.js("document.getElementById('remoteImg').src").startswith("blob:"), "frames are drawn as blob images"
    ph.wait(lambda: ph.env.tabs()[tab].seq > 0 and ph.env.tabs()[tab].streams >= 1, 10, "server streams to the phone")
    ph.notes.append("SSE after %.1fs" % (time.monotonic() - t0))
    assert ph.env.stream_hits["sse"] >= 1 and ph.env.stream_hits["ws"] >= 1 and ph.env.stream_hits["mjpeg"] == 0
    perf = ph.js("(()=>{setPerf(true);return perfText()})()")
    assert "ライブ(SSE: WebSocketの最初の画面が届かない)" in perf, perf
    pref = ph.js("JSON.parse(localStorage.getItem(RB_LIVE_PREF_KEY))")
    assert pref["t"] == "sse" and "ws" in pref["fail"], pref
    # remembered: a new app start goes straight to SSE and does not touch the WebSocket again today
    hits = ph.env.stream_hits["ws"]
    ph.page.reload()
    ph.page.wait_for_selector("#omnibox", state="attached")
    ph.wait(lambda: ph.js("state.authed === true"), 10, "session after reload")
    ph.wait(lambda: ph.js("state.rbStream && state.rbStream.kind") == "sse", 20, "SSE again after reload")
    ph.wait(lambda: ph.js(LIVE_SHOWN), 15, "SSE frame visible after reload")
    assert ph.env.stream_hits["ws"] == hits, "the WebSocket was retried although it failed today"


@scenario("live-ws-refused", display="live")
def live_ws_refused(ph):
    """The WebSocket endpoint answers the upgrade with an error at once (a proxy without upgrade support): two errors in
    a row and the client moves on to SSE without waiting for the first-frame limit."""
    ph.env.refused.add("ws")
    t0 = time.monotonic()
    ph.open("/article.html")
    ph.wait(lambda: ph.js("state.rbStream && state.rbStream.kind") == "sse", 10, "switched to SSE")
    ph.notes.append("SSE after %.1fs" % (time.monotonic() - t0))
    assert time.monotonic() - t0 < 2.4 + 6, "waited for the first-frame limit"
    ph.wait(lambda: ph.js(LIVE_SHOWN), 15, "SSE frame visible")
    perf = ph.js("(()=>{setPerf(true);return perfText()})()")
    assert "ライブ(SSE: WebSocketのエラー)" in perf, perf
    assert ph.env.stream_hits["ws"] >= 2


@scenario("live-mjpeg-fallback", display="live")
def live_mjpeg_fallback(ph):
    """Neither WebSocket nor SSE delivers: MJPEG (the <img> stream) is the third try."""
    ph.env.blocked.update(("ws", "sse"))
    # an animating page: a screencast that starts late (after 5.5 s of failed tries) only gets frames from a page that changes
    ph.open("/anim.html")
    ph.wait(lambda: ph.display() == "live", 5, "display live")
    ph.wait(lambda: ph.js("state.rbStream && state.rbStream.kind") == "mjpeg", 20, "switched to MJPEG")
    ph.wait(lambda: ph.js("(()=>{const i=document.getElementById('remoteImg');return !i.hidden&&i.naturalWidth>0&&i.currentSrc.indexOf('/api/rb/stream.mjpg')>=0})()"),
            15, "MJPEG frame visible")
    perf = ph.js("(()=>{setPerf(true);return perfText()})()")
    assert "ライブ(MJPEG: SSEの最初の画面が届かない)" in perf, perf
    pref = ph.js("JSON.parse(localStorage.getItem(RB_LIVE_PREF_KEY))")
    assert pref["t"] == "mjpeg" and set(pref["fail"]) == {"ws", "sse"}, pref


@scenario("live-polling-fallback", display="live")
def live_polling_fallback(ph):
    """No stream delivers: the client ends on frame.jpg polling and says why."""
    ph.env.blocked.update(("ws", "mjpeg", "sse"))
    tab = ph.open("/article.html")
    ph.wait(lambda: ph.display() == "live", 5, "display live")
    ph.wait(lambda: ph.js("(()=>{const t=activeWebTab();return !!(t&&t.rb&&t.rb.streamFailed)})()"), 30, "gave up on streams")
    ph.wait(lambda: ph.js(LIVE_SHOWN), 20, "polled frame visible")
    perf = ph.js("(()=>{setPerf(true);return perfText()})()")
    assert "ライブ(ポーリング: " in perf, perf
    assert ph.js("JSON.parse(localStorage.getItem(RB_LIVE_PREF_KEY)).t") == "poll"
    assert set(ph.js("JSON.parse(localStorage.getItem(RB_LIVE_PREF_KEY)).fail")) == {"ws", "sse", "mjpeg"}


@scenario("live-ws-proxy", display="live")
def live_ws_proxy(ph):
    """The real-device problem in a box (v0.1.68): the app is reached through a reverse proxy that buffers every ordinary
    response until it ends (so MJPEG and SSE deliver nothing) but tunnels WebSocket upgrades, like `tailscale serve`.
    The live display still runs at >= 3 fps, over the WebSocket."""
    import harness
    proxy = harness.BufferingProxy(ph.env.port)
    try:
        ph.page.goto("http://127.0.0.1:%d/" % proxy.port)
        ph.wait(lambda: ph.js("state.authed === true"), 15, "session through the proxy (cookies are shared by host name)")
        ph.page.wait_for_selector("#omnibox", state="attached")
        tab = ph.open("/anim.html")
        ph.wait(lambda: ph.display() == "live", 5, "display live")
        ph.wait(lambda: ph.js("state.rbStream && state.rbStream.kind") == "ws", 15, "WebSocket transport through the proxy")
        ph.wait(lambda: ph.js(LIVE_SHOWN), 15, "frame visible through the proxy")
        ph.wait(lambda: ph.js("state.rbStream.loaded") is True, 5, "first frame seen")
        f0, t0 = ph.js("state.rbStream.frames"), time.monotonic()
        ph.page.wait_for_timeout(3000)
        fps = (ph.js("state.rbStream.frames") - f0) / (time.monotonic() - t0)
        ph.notes.append("ws through the buffering proxy: %.1f fps" % fps)
        assert fps >= 3, "only %.1f fps through the proxy" % fps
        assert proxy.seen["upgrade"] >= 1, dict(proxy.seen)
        # and the proxy really is a buffering one: the event stream does not start through it
        got = ph.js("""async (id)=>{const ctl=new AbortController();setTimeout(()=>ctl.abort(),1500);
          try{const r=await fetch('/api/rb/stream.sse?tab='+encodeURIComponent(id),{signal:ctl.signal,credentials:'same-origin'});
          return 'status '+r.status}catch(e){return 'nothing in 1.5s'}}""", tab)
        assert got == "nothing in 1.5s", "the proxy did not buffer the event stream: %s" % got
        assert proxy.seen["stream-buffered:/api/rb/stream.sse"] >= 1, dict(proxy.seen)
        perf = ph.js("(()=>{setPerf(true);return perfText()})()")
        assert perf.startswith("ライブ(WebSocket)"), perf
    finally:
        proxy.close()


@scenario("ws-security", display="live")
def ws_security(ph):
    """The Hub's WebSocket endpoint from outside the app (a raw RFC 6455 client): another site's Origin is refused
    (403), so is a missing Origin, a missing session (401) and an unknown tab (404); the right Origin gets frames
    (JPEG) and misbehaving frames are closed with the RFC codes."""
    import struct

    import harness
    import remote_browser as rb
    tab = ph.open("/anim.html")
    ph.wait(lambda: ph.display() == "live", 5, "display live")
    cookie = "; ".join("%s=%s" % (c["name"], c["value"]) for c in ph.context.cookies() if c["name"] == "qdl_session")
    assert cookie, "no session cookie"
    port = ph.env.port
    path = "/api/rb/stream.ws?tab=" + tab
    own = "http://127.0.0.1:%d" % port
    for what, kwargs, want in (
            ("cross-site origin", dict(origin="http://evil.example", cookie=cookie), 403),
            ("origin of another port", dict(origin="http://127.0.0.1:1", cookie=cookie), 403),
            ("no origin", dict(origin=None, cookie=cookie), 403),
            ("no session", dict(origin=own, cookie=""), 401),
            ("unknown tab", dict(origin=own, cookie=cookie, path="/api/rb/stream.ws?tab=b999"), 404)):
        status, headers, sock = harness.raw_ws_client(port, kwargs.get("path", path), kwargs["origin"], kwargs["cookie"])
        sock.close()
        assert status == want, "%s: %s instead of %s" % (what, status, want)
    status, headers, sock = harness.raw_ws_client(port, path, own, cookie)
    assert status == 101 and headers["sec-websocket-accept"] == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=", (status, headers)
    fp = sock.makefile("rb")
    fin, opcode, payload = rb.read_frame(lambda n: fp.read(n) if n else b"")
    assert opcode == rb.OP_BINARY and payload[:2] == b"\xff\xd8", "first message is not a JPEG: %r" % payload[:8]
    sock.sendall(rb.encode_frame(rb.OP_PING, b"x", mask=False))             # a client frame without a mask
    while True:
        fin, opcode, payload = rb.read_frame(lambda n: fp.read(n) if n else b"")
        if opcode == rb.OP_CLOSE:
            break
    assert struct.unpack(">H", payload[:2])[0] == 1002, payload
    sock.close()
    ph.wait(lambda: ph.env.tabs()[tab].streams <= 1, 5, "the refused sockets left no stream behind")
    ph.take_errors("/api/rb/viewport")      # the app's own viewport post can race the raw sockets above (400 while the tab settles)


@scenario("copy-fallback")
def copy_fallback(ph):
    """No navigator.clipboard (plain http): the copy buttons use a hidden textarea + execCommand('copy')."""
    ph.js("""()=>{
      Object.defineProperty(navigator,'clipboard',{value:undefined,configurable:true});
      window.__copied=null;
      document.execCommand=function(cmd){const a=document.activeElement;
        window.__copied={cmd,text:a&&a.value,tag:a&&a.tagName};return true;};
    }""")
    assert ph.js("copyText('こんにちは hello')")
    got = ph.js("window.__copied")
    assert got == {"cmd": "copy", "text": "こんにちは hello", "tag": "TEXTAREA"}, got
    assert ph.js("document.querySelectorAll('body > textarea').length") == 0, "the helper textarea is removed"
    # and when even that is refused the caller is told (the long-press hint is shown)
    ph.js("()=>{document.execCommand=function(){return false}}")
    assert ph.js("copyText('x')") is False


# ------------------------------------------------------------------ mirror: taps, navigation, menus

@scenario("mirror-tap-link")
def mirror_tap_link(ph):
    """iOS-like tap (no synthesized click) on a link far down a long page: NAS navigates, new page starts at the top."""
    tab = ph.open("/long1.html")
    ph.wait(lambda: "長いページ 1" in ph.mirror_text(), 15, "long page in mirror")
    ph.suppress_ios_click()
    ph.js("(()=>{const d=mirrorDoc();mirrorScroller(d).scrollTop=2400})()")
    ph.wait(lambda: ph.mirror_scroll_top() >= 2000, 5, "mirror scrolled")
    ph.page.wait_for_timeout(1500)     # a recapture while scrolled must keep the place
    assert ph.mirror_scroll_top() >= 2000, "scroll position lost on recapture: %s" % ph.mirror_scroll_top()
    pt = ph.wait(lambda: ph.mirror_point(
        "doc=>[...doc.querySelectorAll('a[data-href]')].find(a=>{const r=a.getBoundingClientRect();return r.top>120&&r.bottom<innerHeight-60})"),
        5, "a visible link")
    ph.tap(pt["x"], pt["y"])
    ph.wait(lambda: ph.env.nas_url(tab).endswith("/long2.html"), 10, "NAS navigated to long2 (url=%s)" % ph.env.nas_url(tab))
    ph.wait(lambda: "長いページ 2" in ph.mirror_text(), 10, "mirror shows the new page")
    deadline = time.monotonic() + 2.5      # and stays at the top (the old offset must not come back)
    while time.monotonic() < deadline:
        assert ph.mirror_scroll_top() == 0, "new page did not start at the top: scrollTop=%s" % ph.mirror_scroll_top()
        ph.page.wait_for_timeout(250)


@scenario("mirror-menu-toggle")
def mirror_menu_toggle(ph):
    """A JS dropdown toggle (<a href="#" aria-expanded>) opens the menu on the NAS page; it must not reload it."""
    fx = ph.env.fx
    tab = ph.open("/menu.html")
    ph.wait(lambda: "JS メニューのテスト" in ph.mirror_text(), 15, "menu page in mirror")
    assert "メニュー項目" not in ph.mirror_text(), "menu should start closed"
    loads, hits = fx.beacon_count("load", "/menu.html"), fx.count("/menu.html")
    ph.suppress_ios_click()
    pt = ph.wait(lambda: ph.mirror_point("doc=>doc.querySelector('[aria-expanded]')"), 5, "toggle in mirror")
    ph.tap(pt["x"], pt["y"])
    ph.wait(lambda: fx.beacon_count("toggle", "open") == 1, 8, "the page's own script ran the toggle")
    ph.wait(lambda: "メニュー項目 記事" in ph.mirror_text(), 8, "menu items in the mirror copy")
    assert ph.env.nas_url(tab).endswith("/menu.html"), "NAS navigated away: " + ph.env.nas_url(tab)
    assert fx.count("/menu.html") == hits and fx.beacon_count("load", "/menu.html") == loads, "the page was reloaded"
    # and it closes again the same way
    pt = ph.mirror_point("doc=>doc.querySelector('[aria-expanded]')")
    ph.tap(pt["x"], pt["y"])
    ph.wait(lambda: fx.beacon_count("toggle", "close") == 1, 8, "second toggle ran")
    ph.wait(lambda: "メニュー項目" not in ph.mirror_text(), 8, "menu closed in the mirror copy")
    assert fx.count("/menu.html") == hits, "reloaded on the second tap"
    # a toggle that is a link to this very page (href="/menu.html" + aria-expanded) must be clicked, not followed
    pt = ph.mirror_point("doc=>doc.querySelector('[aria-controls=dd2]')")
    assert pt, "toggle B not in the mirror"
    ph.tap(pt["x"], pt["y"])
    ph.wait(lambda: fx.beacon_count("toggle", "open-b") == 1, 8, "toggle B ran on the NAS page (link to this page)")
    ph.wait(lambda: "B項目" in ph.mirror_text(), 8, "menu B items in the mirror copy")
    assert fx.count("/menu.html") == hits and fx.beacon_count("load", "/menu.html") == loads, "toggle B reloaded the page"


@scenario("mirror-dynamic")
def mirror_dynamic(ph):
    """Script-driven changes reach the mirror copy: after load, and after a tap on a JS button."""
    ph.open("/dynamic.html")
    ph.wait(lambda: "LATE-ADDED" in ph.mirror_text(), 15, "script-added row in mirror")
    ph.suppress_ios_click()
    pt = ph.wait(lambda: ph.mirror_point("doc=>doc.querySelector('#more')||[...doc.querySelectorAll('button')][0]"), 5, "button")
    ph.tap(pt["x"], pt["y"])
    ph.wait(lambda: "MORE-CLICKED 1" in ph.mirror_text(), 10, "button effect in mirror")


@scenario("mirror-security")
def mirror_security(ph):
    """The mirror document cannot run code and ships a deny-all CSP."""
    ph.open("/article.html")
    ph.wait(lambda: "E2E テスト記事" in ph.mirror_text(), 15, "mirror content")
    doc_url = ph.js("(f=>f.dataset.doc||f.getAttribute('src'))(document.getElementById('mirrorFrame'))")
    assert doc_url and doc_url.startswith("/api/rb/mirror/doc?"), doc_url
    res = ph.js("""async(u)=>{const r=await fetch(u,{credentials:'same-origin'});return {csp:r.headers.get('content-security-policy')||'',
        text:await r.text()}}""", doc_url)
    assert "default-src 'none'" in res["csp"], "CSP header: %r" % res["csp"]
    assert "<script" not in res["text"].lower(), "script tag in the mirror document"
    assert "default-src 'none'" in res["text"], "no CSP <meta> in the mirror document"
    assert ph.js("mirrorDoc().querySelectorAll('script,iframe,object,embed,form[action]').length") == 0, "live code elements in the mirror"
    out = ph.js("""async()=>{const f=document.getElementById('mirrorFrame');const d=f.contentDocument;
        const s=d.createElement('script');s.textContent='window.pwned=1';d.body.appendChild(s);
        const i=d.createElement('img');i.setAttribute('onerror','window.pwned2=1');i.src='x:';d.body.appendChild(i);
        await new Promise(r=>setTimeout(r,400));
        const r={inline:f.contentWindow.pwned===undefined,handler:f.contentWindow.pwned2===undefined};s.remove();i.remove();return r}""")
    assert out["inline"] and out["handler"], "injected script / handler ran in the mirror: %s" % out
    # Chrome reports each blocked attempt as a console error (the CSP doing its job). WebKit words
    # it differently and may not surface frame messages at all: there the "did not run" check above
    # is the assertion that matters.
    blocked = ph.take_errors("Content Security Policy") + ph.take_errors("Refused to execute")
    if ph.engine == "chromium":
        assert blocked, "the CSP did not report the blocked inline script / handler"


# ------------------------------------------------------------------ reader

@scenario("reader-cards", display="reader")
def reader_cards(ph):
    """Hoster links become cards (label/size present or missing); page-number rows become chips."""
    ph.open("/hoster.html")
    ph.wait(lambda: ph.js("document.querySelectorAll('#readerBody .reader-card').length") == 3, 15, "3 link cards")
    cards = ph.js("""[...document.querySelectorAll('#readerBody .reader-card')].map(a=>({
        label:(a.querySelector('.reader-card-label')||{}).textContent||'', size:(a.querySelector('.reader-card-size')||{}).textContent||'',
        title:(a.querySelector('.reader-card-title')||{}).textContent||'', host:(a.querySelector('.reader-card-host')||{}).textContent||''}))""")
    assert cards[0]["label"] == "RapidGator" and cards[0]["size"] == "66.76 MB", cards[0]
    assert cards[1]["size"] == "12.3 MB" and cards[1]["label"] == "dl.example.net", "label missing -> host: %s" % cards[1]
    assert cards[2]["label"] == "FreeDL" and cards[2]["size"] == "5.5 MB", "size from the next block: %s" % cards[2]
    assert all(c["title"] for c in cards), cards
    chips = ph.js("document.querySelectorAll('#readerBody .reader-chips .reader-chip').length")
    assert chips >= 4, "pagination chips: %s" % chips


# ------------------------------------------------------------------ gestures

def _flick(ph, dx):
    x0 = 120 if dx > 0 else 300
    y = 420
    ph.cdp_touch([(x0, y, 0), (x0 + dx // 2, y, 40), (x0 + dx, y, 40)])


@scenario("gesture-flick-mirror", engines=("chromium",))
def gesture_flick_mirror(ph):
    """Horizontal flick over the mirror = browser back / forward on the NAS tab."""
    tab = ph.open("/article.html")
    ph.wait(lambda: "E2E テスト記事" in ph.mirror_text(), 15, "mirror content")
    ph.post("/api/rb/navigate", {"tab": tab, "url": BASE + "/article2.html"})
    ph.wait(lambda: ph.env.nas_url(tab).endswith("/article2.html"), 10, "navigated to article2")
    ph.wait(lambda: "E2E 二つ目の記事" in ph.mirror_text(), 10, "article2 in mirror")
    _flick(ph, 200)                       # right = back
    ph.wait(lambda: ph.env.nas_url(tab).endswith("/article.html"), 8, "back to article (url=%s)" % ph.env.nas_url(tab))
    ph.wait(lambda: "E2E テスト記事" in ph.mirror_text(), 10, "article in mirror again")
    _flick(ph, -200)                      # left = forward
    ph.wait(lambda: ph.env.nas_url(tab).endswith("/article2.html"), 8, "forward to article2 (url=%s)" % ph.env.nas_url(tab))


@scenario("gesture-flick-tiles", engines=("chromium",), display="fast")
def gesture_flick_tiles(ph):
    tab = ph.open("/article.html")
    ph.wait(lambda: ph.display() == "tiles", 5, "tiles")
    ph.wait(lambda: ph.js("document.querySelectorAll('#tileScroll img').length") >= 1, 15, "tiles present")
    ph.post("/api/rb/navigate", {"tab": tab, "url": BASE + "/article2.html"})
    ph.wait(lambda: ph.env.nas_url(tab).endswith("/article2.html"), 10, "navigated to article2")
    ph.page.wait_for_timeout(800)
    _flick(ph, 200)
    ph.wait(lambda: ph.env.nas_url(tab).endswith("/article.html"), 8, "back (url=%s)" % ph.env.nas_url(tab))


@scenario("gesture-close-tab", engines=("chromium",))
def gesture_close_tab(ph):
    """Drag down from the toolbar closes the tab."""
    tab = ph.open("/article.html")
    ph.wait(lambda: "E2E テスト記事" in ph.mirror_text(), 15, "mirror content")
    before = ph.js("state.tabs.length")
    box = ph.page.locator(".toolbar").bounding_box()
    x, y = box["x"] + 60, box["y"] + box["height"] / 2
    ph.cdp_touch([(x, y, 0)] + [(x, y + k * 25, 20) for k in range(1, 8)])
    ph.wait(lambda: ph.js("state.tabs.length") == before - 1, 8, "tab removed from the app")
    ph.wait(lambda: tab not in ph.env.tabs(), 8, "NAS tab closed on the server")


# ------------------------------------------------------------------ reload, auto-live

@scenario("reload-reattach")
def reload_reattach(ph):
    """Reloading the app reattaches the same NAS tab; no extra tab is opened (not now, not a few seconds later)."""
    tab = ph.open("/article.html")
    ph.wait(lambda: "E2E テスト記事" in ph.mirror_text(), 15, "mirror content")
    before = sorted(ph.env.tabs())
    assert before == [tab], before
    opens_before = ph.env.fx.count("/article.html")
    ph.page.reload()
    ph.page.wait_for_selector("#omnibox", state="attached")
    ph.wait(lambda: ph.js("state.authed === true"), 10, "still logged in")
    ph.wait(lambda: ph.tab_id() == tab, 15, "same NAS tab re-attached (now %s)" % ph.tab_id())
    ph.wait(lambda: "E2E テスト記事" in ph.mirror_text(), 15, "mirror content after reload")
    ph.page.wait_for_timeout(4000)
    after = sorted(ph.env.tabs())
    assert after == before, "tabs before %s after %s" % (before, after)
    assert ph.env.fx.count("/article.html") <= opens_before + 1, \
        "page loaded %d times after reload" % (ph.env.fx.count("/article.html") - opens_before)


CHALLENGE_BUDGET_S = 6.0


def _challenge_wait(ph, display):
    """v0.1.52: a check page is never shown and never switches to live; the status line says it is running and the
    real page appears in the chosen display right after the redirect, without adding a history entry."""
    tab = ph.open("/challenge.html?s=4", wait_tab=True)
    ph.wait(lambda: ph.js("(()=>{const t=activeWebTab();return !!(t&&t.rb&&t.rb.chal)})()"), 10, "the check is held")
    history_len = ph.js("history.length")
    hash_ = ph.js("location.hash")
    assert "安全性の確認中" in ph.js("document.getElementById('chalStatus').textContent")
    assert ph.js("!document.getElementById('chalStatus').hidden")
    assert ph.js("document.getElementById('chalLiveBtn').hidden"), "the live button must not show before 25 s"
    deadline = time.monotonic() + 14
    while not ph.env.fx.times.get("/after-challenge.html"):
        assert ph.display() == display, "display became %s while the check ran (want %s)" % (ph.display(), display)
        if display == "mirror":
            assert "Checking your browser" not in ph.mirror_text(), "the check page itself was rendered"
        assert time.monotonic() < deadline, "the check page never redirected"
        ph.page.wait_for_timeout(200)
    redirected = ph.env.fx.times["/after-challenge.html"]
    if display == "mirror":
        ph.wait(lambda: "確認が終わりました" in ph.mirror_text(), CHALLENGE_BUDGET_S + 2, "real page in the mirror")
    else:
        ph.wait(lambda: ph.js("(()=>{const t=activeWebTab();return !!(t&&t.rb&&!t.rb.chal&&t.rb.view&&"
                              "t.rb.url.endsWith('/after-challenge.html'))})()"), CHALLENGE_BUDGET_S + 2, "real page as tiles")
    took = time.monotonic() - redirected
    assert took <= CHALLENGE_BUDGET_S, "real page %.1fs after the redirect (budget %.1fs)" % (took, CHALLENGE_BUDGET_S)
    assert ph.display() == display
    assert ph.js("document.getElementById('chalStatus').hidden")
    assert ph.js("history.length") == history_len, "history entries were added during the check"
    assert ph.js("location.hash") == hash_ and ph.js("state.route.view") == "tab"
    return took


@scenario("challenge-wait-fast", display="fast")
def challenge_wait_fast(ph):
    ph.log("real page %.1fs after redirect" % _challenge_wait(ph, "tiles"))


@scenario("challenge-wait-mirror", display="mirror")
def challenge_wait_mirror(ph):
    ph.log("real page %.1fs after redirect" % _challenge_wait(ph, "mirror"))


@scenario("history-skip-and-first-page", engines=("chromium",), display="mirror")
def history_skip_and_first_page(ph):
    """v0.1.52: A -> B -> B?token (check redirect) -> C. One back from C lands on B, one more on A; the app stays on the
    tab route; at the first page a toast says so and nothing else moves (no fall back to the previous app screen)."""
    tab = ph.open("/article.html")
    ph.wait(lambda: "E2E テスト記事" in ph.mirror_text(), 15, "mirror content")
    for path in ("/article2.html", "/article2.html?__cf_chl_tk=abc", "/long1.html"):
        ph.post("/api/rb/navigate", {"tab": tab, "url": BASE + path})
        ph.wait(lambda: ph.env.nas_url(tab).endswith(path), 10, "navigated to " + path)
        ph.page.wait_for_timeout(500)
    route = ph.js("location.hash")
    ph.js("history.back()")
    ph.wait(lambda: ph.env.nas_url(tab).endswith("/article2.html"), 8, "one back skips the token entry (%s)" % ph.env.nas_url(tab))
    ph.page.wait_for_timeout(900)
    assert ph.js("location.hash") == route and ph.js("state.route.view") == "tab"
    ph.js("history.back()")
    ph.wait(lambda: ph.env.nas_url(tab).endswith("/article.html"), 8, "second back reaches the first page")
    ph.page.wait_for_timeout(900)
    ph.js("history.back()")                           # nothing earlier: toast, no route change, no NAS move
    ph.wait(lambda: "これより前のページはありません" in ph.js("document.body.innerText"), 8, "toast at the first page")
    ph.page.wait_for_timeout(900)
    assert ph.js("location.hash") == route and ph.js("state.route.view") == "tab", ph.js("location.hash")
    assert ph.env.nas_url(tab).endswith("/article.html")
    assert ph.js("history.state && history.state.qdGuard") == ph.js("state.route.tabId"), "the guard must be put again"


# ------------------------------------------------------------------ self-test (v0.1.44)

@scenario("selftest")
def selftest(ph):
    """The 診断 › 自己テスト button runs the whole self-test (Hub part, built-in pages, THIS phone's part and one
    real-looking site) and the copied report names every section. Nothing of it may be left behind."""
    ph.js("(r)=>{location.hash=r}", "#/settings/diag/selftest")
    ph.wait(lambda: ph.js("(()=>{const b=document.getElementById('selftestRun');return !!b&&!b.hidden&&b.offsetParent!==null})()"),
            10, "self-test button")
    ph.page.fill("#selftestUrls", BASE + "/article.html")
    ph.env.model_container_memory(True, limit_mib=2048)       # v0.1.65: the fake `docker stats` follows the real Chrome's memory
    ph.page.click("#selftestRun")
    ph.wait(lambda: ph.js("!!(selftestUi.snap&&selftestUi.snap.state==='running')"), 10, "run started")
    # while it runs the progress list is shown and cancel is offered
    assert ph.js("!document.getElementById('selftestProgress').hidden")
    ph.wait(lambda: ph.js("selftestUi.snap&&selftestUi.snap.state") == "done", 320, "self-test finished")
    snap = ph.js("selftestUi.snap")
    checks = {c["id"]: c for c in snap["checks"]}
    builtin = [c for c in snap["checks"] if c["section"] == "builtin"]
    assert len(builtin) >= 10, "built-in checks: %s" % [c["id"] for c in builtin]
    bad = [(c["id"], c["status"], c["value"]) for c in builtin if c["status"] == "fail"]
    assert not bad, "failed built-in checks: %s" % bad
    for cid in ("builtin.reader", "builtin.mirror", "builtin.mirror_safe", "builtin.mirror_stable", "builtin.menu", "builtin.tiles", "builtin.fps"):
        assert cid in checks, "missing check " + cid
    client = [c for c in snap["checks"] if c["section"] == "client"]
    assert snap["client"] == "received" and len(client) >= 8, "client section: %s" % [(c["id"], c["status"]) for c in client]
    bad = [(c["id"], c["status"], c["value"]) for c in client if c["status"] == "fail"]
    assert not bad, "failed client checks (this browser): %s" % bad
    for cid in ("client.live", "client.secure", "client.clipboard", "client.mirror_reuse", "env.cgroup"):
        assert cid in checks, "missing check " + cid
    live = checks["client.live"]
    assert live["status"] != "fail" and "経路:" in live["value"], live
    # v0.1.68: the page under test really moves (it used to be a still page below the fold: ~0.3 fps on every path), and the
    # report line names the WebSocket first
    assert live["status"] == "pass" and live["value"].startswith("経路: WebSocket / "), live
    assert "[Live] transport=websocket" in ph.js("fetch('/api/selftest/report').then(r=>r.json()).then(j=>j.text)"), "report header"
    # v0.1.73: the real site's address is hidden by default (screen and report); the switch shows it again
    host = BASE.split("://", 1)[1].split("/")[0].split(":")[0]
    assert ph.js("document.getElementById('selftestHideSites').checked"), "hide switch is on by default"
    shown = ph.js("document.getElementById('selftestTables').innerText")
    hidden = ph.js("selftestReport().then(r=>r.text)")
    assert host not in shown and "サイト1" in shown, "screen shows the site address"
    assert host not in hidden and "サイト1" in hidden, "report shows the site address"
    ph.page.click("label[for=selftestHideSites]")
    ph.wait(lambda: host in ph.js("document.getElementById('selftestTables').innerText"), 5, "address shown again")
    assert host in ph.js("selftestReport().then(r=>r.text)")
    ph.page.click("label[for=selftestHideSites]")
    ph.notes.append("builtin.fps " + checks["builtin.fps"]["value"][:30])
    ph.notes.append("live: " + live["value"][:60])
    # v0.1.59: 「操作の応答」: route switches, switch feedback, idle traffic and the request count of opening a page
    response = {c["id"]: c for c in snap["checks"] if c["section"] == "response"}
    assert set(response) == {"client.route", "client.toggle", "client.idle", "client.open_rtt"}, list(response)
    for cid, check in response.items():
        assert check["status"] == "pass", "response check %s: %s %s" % (cid, check["status"], check["value"])
    ph.notes.append("response: " + " | ".join(response[c]["value"][:48] for c in sorted(response)))
    events = checks["client.mirror_events"]
    assert events["status"] == "pass", "mirror taps did not reach the app's listeners here: %s" % events
    sites = [c for c in snap["checks"] if c["section"] == "sites"]
    assert [c["id"] for c in sites][:1] == ["site.1.open"] and not [c for c in sites if c["status"] == "fail"], sites
    assert "DOMContentLoaded" in checks["site.1.open"]["value"], checks["site.1.open"]
    # v0.1.65: 「タブの容量」: one line per page (4 built-in pages + the real-looking site) and a recommendation
    cap = [c for c in snap["checks"] if c["section"] == "capacity"]
    pages = [c for c in cap if c["id"].startswith("capacity.page.")]
    assert "capacity.base" in checks and "capacity.summary" in checks, [c["id"] for c in cap]
    assert len(pages) == 5 and all("MiB" in c["value"] and "JSヒープ" in c["value"] for c in pages), [c["value"] for c in pages]
    summary = checks["capacity.summary"]
    assert summary["status"] in ("pass", "warn") and "1ページあたり 平均" in summary["value"] and "推奨タブ枚数" in summary["value"], summary
    assert ph.env.rb.tab_reco and 1 <= ph.env.rb.tab_reco["tabs"] <= 20, ph.env.rb.tab_reco
    for c in pages:
        ph.notes.append("cap: " + c["value"][:80])
    ph.notes.append("cap: " + summary["value"][:90])
    # v0.1.65: 「リンク解析キュー」: 10 virtual presses queued at once finish one after another
    assert checks["queue.enqueue"]["status"] == "pass", checks["queue.enqueue"]
    assert checks["queue.finish"]["status"] == "pass" and "10/10件完了" in checks["queue.finish"]["value"], checks["queue.finish"]
    assert checks["queue.sequential"]["status"] == "pass", checks["queue.sequential"]
    ph.notes.append("queue: " + checks["queue.finish"]["value"][:90] + " / " + checks["queue.sequential"]["value"][:60])
    assert checks["cleanup.tabs"]["status"] == "pass"
    # a NAS tab the app itself shows (a web tab re-attached while the self-test switched screens) is not a leftover
    ph.page.wait_for_timeout(500)
    owned = set(ph.js("state.tabs.filter(t=>t.rb).map(t=>t.rb.id)") or [])
    leftover = [t for t in ph.env.tabs() if t not in owned]
    assert ph.env.rb.selftest_tabs() == [] and not leftover, "tabs left behind: %s (app shows %s)" % (leftover, sorted(owned))
    # the view shows summary and tables; the copied text is a self-contained report
    assert "合格" in ph.js("document.getElementById('selftestSummary').textContent")
    assert ph.js("document.querySelectorAll('#selftestTables table').length") >= 4
    ph.page.click("#selftestCopy")
    ph.wait(lambda: len(ph.js("selftestUi.text")) > 500, 10, "report text")
    text = ph.js("selftestUi.text")
    for needle in ("== QnapDownloader 自己テスト", "app: v" + ph.env.version, "[NAS] cpu=", "[Phone] ua=", "viewport=",
                   "## 環境", "## 内蔵テストページ", "## この端末", "## 操作の応答", "## 実サイト", "## タブの容量", "## リンク解析キュー", "queue.finish", "capacity.summary", "推奨タブ枚数", "## 後片付け", "builtin.reader", "client.mirror_events",
                   "value:", "threshold:", "## debug log (last 50 lines", "[Source] cgroup=", "[Live] transport=", "env.cgroup",
                   "client.secure"):
        assert needle in text, "report lacks %r" % needle
    # v0.1.73: by default the real site is サイト1 and no address of it is left
    assert "a.example.com" not in text and "/article.html" not in text and "サイト1" in text, \
        "real-site addresses must be hidden by default"
    # v0.1.52: the report also holds the automatic swipe section with its timelines
    assert text.count("\n") < 1200, "report too long: %d lines" % text.count("\n")
    # a second click while idle starts a new run only after this one is over; cancel is hidden now
    assert ph.js("document.getElementById('selftestCancel').hidden")
    assert not ph.js("document.getElementById('selftestRun').disabled")


def _run_swipe_button(ph, button, timeout):
    ph.js("(r)=>{location.hash=r}", "#/settings/diag/selftest")
    ph.wait(lambda: ph.js("(()=>{const b=document.getElementById('%s');return !!b&&b.offsetParent!==null})()" % button),
            10, "button " + button)
    before_hash = ph.js("location.hash")
    ph.page.click("#" + button)
    ph.wait(lambda: ph.js("!!(selftestUi.snap&&selftestUi.snap.state==='running')"), 10, "run started")
    return before_hash


def _after_swipe(ph, before_hash):
    assert ph.js("location.hash") == before_hash, "route not restored: %s" % ph.js("location.hash")
    assert ph.js("state.tabs.filter(t=>t.diag).length") == 0 and ph.js("document.getElementById('swipeBanner').hidden")
    assert ph.js("swipeRec === null && swipeDiag === null")
    ph.page.wait_for_timeout(500)
    owned = set(ph.js("state.tabs.filter(t=>t.rb).map(t=>t.rb.id)") or [])
    left = ph.env.tabs()
    assert ph.env.rb.selftest_tabs() == [] and not [t for t in left if t not in owned], \
        "tabs left behind: %s (app shows %s, selftest %s)" % (left, sorted(owned), ph.env.rb.selftest_tabs())
    assert ph.js("document.getElementById('gestureHint').hidden")


@scenario("swipe-diagnostic", engines=("chromium",))
def swipe_diagnostic(ph):
    """設定›診断›自己テスト›「スワイプだけ確認」: the app's own handlers get synthetic input in mirror / reader / fast and the Hub
    judges what happened; the route, the tab list and the NAS tabs are restored."""
    before = _run_swipe_button(ph, "selftestSwipe", 60)
    ph.wait(lambda: ph.js("selftestUi.snap&&selftestUi.snap.phase") == "swipe", 30, "swipe phase")
    ph.wait(lambda: ph.js("!document.getElementById('swipeBanner').hidden"), 20, "banner")
    ph.wait(lambda: ph.js("selftestUi.snap&&selftestUi.snap.state") == "done", 200, "swipe diagnostic finished")
    snap = ph.js("selftestUi.snap")
    rows = {c["id"]: c for c in snap["checks"] if c["section"] == "swipe"}
    steps = ("edge_back", "flick_right", "back_first", "flick_left", "slow_move", "edge_start", "forward", "pull_cancel", "pull_close",
             "pull_omnibox", "hscroll", "pop_stay")
    for mode in ("mirror", "reader", "tiles"):
        assert "swipe.%s.display" % mode in rows, sorted(rows)
        for step in steps:
            row = rows.get("swipe.%s.%s" % (mode, step))
            if step == "pop_stay" and mode != "tiles":
                assert not row, "pop_stay runs in the last display only"
                continue
            assert row, "missing %s.%s" % (mode, step)
            # v0.1.55: the scrolling table (#wide) exists only in the mirror copy; the other displays skip that step
            # v0.1.71: the table step only builds synthetic input; on a busy runner it can fail to (skip). The behaviour
            # itself (a flick on a scrolling table never navigates) is checked by the flick-on-scroller scenario.
            ok = ("pass", "warn", "skip") if step == "hscroll" else ("pass", "warn")
            if row["status"] not in ok:
                import json as _json
                tl = ph.js("""async()=>{try{const r=await fetch('/api/selftest/report',{credentials:'same-origin'});const t=await r.text();
                    const i=t.indexOf('[SKIP] hscroll');return i<0?t.slice(-2500):t.slice(i,i+2500)}catch(e){return String(e)}}""")
                raise AssertionError("%s.%s: %s | %s | %s | row=%s | timelines=%s" % (
                    mode, step, row["status"], row["value"], row["hint"], _json.dumps(row, ensure_ascii=False)[:1500], (tl or "")[:3000]))
        hist = rows["swipe.%s.history" % mode]
        assert hist["status"] == "pass", hist
    assert rows["swipe.mirror.edge_back"]["status"] == "pass", rows["swipe.mirror.edge_back"]
    assert rows["swipe.mirror.flick_right"]["status"] == "pass", rows["swipe.mirror.flick_right"]
    assert rows["swipe.mirror.pull_close"]["status"] == "pass", rows["swipe.mirror.pull_close"]
    for step in ("pull_omnibox", "hscroll"):                       # v0.1.55
        assert rows["swipe.mirror.%s" % step]["status"] == "pass", rows["swipe.mirror.%s" % step]
    assert rows["swipe.tiles.pop_stay"]["status"] == "pass", rows["swipe.tiles.pop_stay"]
    _after_swipe(ph, before)
    ph.page.click("#selftestCopy")
    ph.wait(lambda: len(ph.js("selftestUi.text")) > 500, 10, "report text")
    text = ph.js("selftestUi.text")
    for needle in ("## スワイプ（自動）", "swipe.mirror.edge_back", "touch.start", "popstate", "step.moved", "flick.accepted",
                   "guard.push", "### mirror", "### reader", "### tiles"):
        assert needle in text, "report lacks %r" % needle
    assert "https://" not in text.split("## スワイプ（自動）のタイムライン", 1)[1].split("## debug log", 1)[0]
    assert len(text) < 400000


@scenario("swipe-manual", engines=("chromium",))
def swipe_manual(ph):
    """「手動で確認」: banner with answer buttons; step 1 is done with the browser back, the rest are skipped."""
    before = _run_swipe_button(ph, "selftestManual", 60)
    ph.wait(lambda: ph.js("selftestUi.snap&&selftestUi.snap.phase") == "swipe", 30, "swipe phase")
    ph.wait(lambda: ph.js("!document.getElementById('swipeBanner').hidden&&!document.getElementById('swipeOk').disabled"), 40, "first step ready")
    assert "手順 1/6" in ph.js("document.getElementById('swipeBannerStep').textContent")
    ph.shot("banner")
    ph.js("history.back()")
    ph.page.wait_for_timeout(1500)
    ph.page.click("#swipeOk")
    for number in range(2, 7):
        ph.wait(lambda: ph.js("(()=>{const e=document.getElementById('swipeBannerStep').textContent;"
                              "return e.startsWith('手順 %d/6')&&!e.includes('準備中')&&!document.getElementById('swipeSkip').disabled})()" % number),
                40, "step %d ready" % number)
        ph.page.click("#swipeSkip")
    ph.wait(lambda: ph.js("selftestUi.snap&&selftestUi.snap.state") == "done", 60, "manual check finished")
    snap = ph.js("selftestUi.snap")
    rows = {c["id"]: c for c in snap["checks"] if c["section"] == "swipe_manual"}
    first = [c for c in rows.values() if c["id"].endswith(".m_edge_back")]
    assert first and first[0]["status"] == "pass", rows
    assert len([c for c in rows.values() if c["status"] == "skip"]) == 5, rows
    _after_swipe(ph, before)


@scenario("omnibox-touch-type")
def omnibox_touch_type(ph):
    """On an open web tab, tapping the address bar (touch) and typing a URL navigates (v0.1.50)."""
    tab = ph.open("/article.html")
    ph.wait(lambda: "E2E テスト記事" in ph.mirror_text(), 15, "mirror content")
    box = ph.page.locator("#omnibox").first.bounding_box()
    ph.tap(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    ph.page.wait_for_timeout(300)
    focused = ph.js("document.activeElement && document.activeElement.id")
    assert focused == "omnibox", "tapping the address bar does not focus it (active=%s)" % focused
    ph.page.keyboard.press("Control+A")
    ph.page.keyboard.type(BASE + "/article2.html")
    ph.page.keyboard.press("Enter")
    ph.wait(lambda: ph.env.nas_url(tab).endswith("/article2.html"), 10, "navigated by typing in the address bar")


@scenario("browser-back-is-page-back")
def browser_back_is_page_back(ph):
    """The browser's own back (iOS edge swipe) on a web page goes back in the page, then stays with a toast (v0.1.52)."""
    tab = ph.open("/article.html")
    ph.wait(lambda: "E2E テスト記事" in ph.mirror_text(), 15, "mirror content")
    ph.post("/api/rb/navigate", {"tab": tab, "url": BASE + "/article2.html"})
    ph.wait(lambda: ph.env.nas_url(tab).endswith("/article2.html"), 10, "navigated to article2")
    hash_before = ph.js("location.hash")
    ph.log("before 1st back: hash=%s len=%s state=%s route=%s" % (hash_before, ph.js("history.length"), ph.js("JSON.stringify(history.state)"), ph.js("JSON.stringify(state.route)")))
    ph.js("history.back()")
    ph.page.wait_for_timeout(1500)
    ph.log("after 1st back: hash=%s state=%s nas=%s" % (ph.js("location.hash"), ph.js("JSON.stringify(history.state)"), ph.env.nas_url(tab)))
    ph.wait(lambda: ph.env.nas_url(tab).endswith("/article.html"), 8, "browser back = page back")
    assert ph.js("location.hash") == hash_before, "the app left the tab (%s)" % ph.js("location.hash")
    ph.log("before 2nd back: hash=%s len=%s state=%s" % (ph.js("location.hash"), ph.js("history.length"), ph.js("JSON.stringify(history.state)")))
    ph.js("history.back()")               # the page has nothing earlier now: the app's previous screen
    ph.page.wait_for_timeout(1500)
    ph.log("after 2nd back: hash=%s state=%s nas=%s" % (ph.js("location.hash"), ph.js("JSON.stringify(history.state)"), ph.env.nas_url(tab)))
    # v0.1.52: no fallback to history.back(); the app stays on the tab and tells the user
    assert ph.js("location.hash") == hash_before, "the app left the tab on a back with nothing earlier (%s)" % ph.js("location.hash")
    ph.wait(lambda: "これより前のページはありません" in (ph.js("document.body.innerText") or ""), 5, "first-page toast")


# ------------------------------------------------------------------ v0.1.55: real-device swipe findings

@scenario("back-lands-on-older-app-entry", engines=("chromium",), display="mirror")
def back_lands_on_older_app_entry(ph):
    """v0.1.55: WebKit may skip our guard entry, so a page back can land on an OLDER APP ENTRY (another route). A back that
    is not the app's own navigation must still stay on the web tab and step the NAS page back once."""
    tab = ph.open("/article.html")
    ph.wait(lambda: "E2E テスト記事" in ph.mirror_text(), 15, "mirror content")
    ph.post("/api/rb/navigate", {"tab": tab, "url": BASE + "/article2.html"})
    ph.wait(lambda: ph.env.nas_url(tab).endswith("/article2.html"), 10, "navigated to article2")
    ph.page.wait_for_timeout(900)                          # past the app-navigation window of opening the tab
    route = ph.js("location.hash")
    # [.., tab, guard] -> [.., #/home, tab, guard]: one back would now skip over the guard AND the tab entry (go(-2))
    ph.js("(()=>{const h=location.hash;history.back();return new Promise(r=>setTimeout(()=>{history.replaceState(null,'','#/home');"
          "history.pushState(null,'',h);history.pushState({qdGuard:state.route.tabId},'',h);r()},400))})()")
    ph.page.wait_for_timeout(900)
    assert ph.js("location.hash") == route and ph.js("state.route.view") == "tab", ph.js("location.hash")
    ph.js("history.go(-2)")
    ph.wait(lambda: ph.env.nas_url(tab).endswith("/article.html"), 8, "page back (url=%s)" % ph.env.nas_url(tab))
    ph.page.wait_for_timeout(1200)
    assert ph.js("location.hash") == route and ph.js("state.route.view") == "tab", "left the tab: %s" % ph.js("location.hash")
    assert ph.js("history.state && history.state.qdGuard") == ph.js("state.route.tabId"), "the guard must be put again"
    assert not ph.js("document.getElementById('view-web').hidden"), "the tab screen must stay shown"
    # the app's own navigation is still a real route change: Settings from the bottom bar / hash, and back to the tab
    ph.js("navigate('#/settings/diag')")
    ph.wait(lambda: ph.js("state.route.view") == "settings", 5, "settings route")
    ph.js("navigate('#/home')")
    ph.wait(lambda: ph.js("state.route.view") == "home", 5, "home route by the app's own navigation")


@scenario("flick-on-scroller", engines=("chromium",), display="mirror")
def flick_on_scroller(ph):
    """v0.1.55: a quick sideways swipe that starts on a horizontally scrolling table scrolls it; it is never back / forward."""
    tab = ph.open("/article.html")
    ph.wait(lambda: "E2E テスト記事" in ph.mirror_text(), 15, "mirror content")
    ph.post("/api/rb/navigate", {"tab": tab, "url": BASE + "/scroller.html"})
    ph.wait(lambda: ph.env.nas_url(tab).endswith("/scroller.html"), 10, "navigated to scroller")
    ph.wait(lambda: "E2E 横スクロール" in ph.mirror_text(), 10, "scroller in mirror")
    ph.page.wait_for_timeout(600)
    pt = ph.mirror_point("doc=>doc.querySelector('#wide td')")
    assert pt, "no table in the mirror"
    _flick_at(ph, pt["x"] + 60, pt["y"], -200)             # quick, would be a forward flick anywhere else
    _flick_at(ph, pt["x"] - 60, pt["y"], 200)              # and a back flick
    ph.page.wait_for_timeout(1500)
    assert ph.env.nas_url(tab).endswith("/scroller.html"), "the table swipe moved the NAS page: %s" % ph.env.nas_url(tab)
    # the same flick on plain text still goes back
    p = ph.mirror_point("doc=>doc.querySelector('article p')")
    _flick_at(ph, 120, p["y"], 200)
    ph.wait(lambda: ph.env.nas_url(tab).endswith("/article.html"), 8, "flick on text still goes back (%s)" % ph.env.nas_url(tab))


def _flick_at(ph, x0, y, dx):
    ph.cdp_touch([(x0, y, 0), (x0 + dx // 2, y, 40), (x0 + dx, y, 40)])
    ph.page.wait_for_timeout(700)


@scenario("pull-from-omnibox", engines=("chromium",), display="mirror")
def pull_from_omnibox(ph):
    """v0.1.55: on a phone the toolbar is mostly the address bar: a pull that starts on the (unfocused) address bar shows the
    hint, going back up cancels it, pulling all the way closes the tab; a plain tap still focuses it."""
    tab = ph.open("/article.html")
    ph.wait(lambda: "E2E テスト記事" in ph.mirror_text(), 15, "mirror content")
    ph.page.wait_for_timeout(600)
    box = ph.page.locator("#omnibox").first.bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    assert ph.js("document.activeElement && document.activeElement.id") != "omnibox"
    cdp = ph.context.new_cdp_session(ph.page)
    try:
        cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": x, "y": y}]})
        for k in range(1, 7):
            ph.page.wait_for_timeout(25)
            cdp.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [{"x": x, "y": y + k * 25}]})
        ph.wait(lambda: not ph.js("document.getElementById('gestureHint').hidden"), 3, "hint while pulling from the address bar")
        assert ph.js("document.activeElement && document.activeElement.id") != "omnibox", "the keyboard must not come up"
        for k in range(5, -1, -1):
            cdp.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [{"x": x, "y": y + k * 25}]})
        ph.page.wait_for_timeout(100)
        assert ph.js("document.getElementById('gestureHint').hidden"), "the hint must go away when the finger returns"
        cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
    finally:
        cdp.detach()
    ph.page.wait_for_timeout(400)
    assert ph.js("state.tabs.length") == 1 and tab in ph.env.tabs(), "a cancelled pull closed the tab"
    ph.tap(x, y)                                           # a tap still focuses the address bar
    ph.page.wait_for_timeout(300)
    assert ph.js("document.activeElement && document.activeElement.id") == "omnibox", "tap no longer focuses the address bar"
    ph.js("document.getElementById('omnibox').blur()")
    ph.cdp_touch([(x, y, 0)] + [(x, y + k * 25, 20) for k in range(1, 8)])
    ph.wait(lambda: ph.js("state.tabs.length") == 0, 8, "pull from the address bar closes the tab")


# ------------------------------------------------------------------ passkeys (v0.1.53)

@scenario("passkey-login", engines=("chromium",))
def passkey_login(ph):
    """Register a (virtual, CDP) platform passkey in Settings, log out everywhere, log in with it.

    http://localhost is a secure context, so WebAuthn works there; the Hub only allows rp id "localhost"
    because the E2E app sets passkey_test_hosts (production accepts only https *.ts.net hosts).
    """
    page = ph.page
    local = ph.env.url.replace("127.0.0.1", "localhost")
    cdp = ph.context.new_cdp_session(page)
    cdp.send("WebAuthn.enable")
    cdp.send("WebAuthn.addVirtualAuthenticator", {"options": {
        "protocol": "ctap2", "transport": "internal", "hasResidentKey": True, "hasUserVerification": True,
        "isUserVerified": True, "automaticPresenceSimulation": True}})
    page.on("dialog", lambda dialog: dialog.accept())
    page.goto(local)
    ph.wait(lambda: page.locator("#password").is_visible(), 15, "login form on localhost")
    assert ph.js("window.isSecureContext === true")
    assert not page.locator("#passkeyLogin").is_visible(), "no passkey yet: no face-login button"
    page.fill("#password", PASSWORD)
    assert page.locator("#loginRemember").is_visible()
    page.click("#loginButton")
    ph.wait(lambda: ph.js("state.authed === true"), 10, "password login on localhost")
    ph.csrf = ph.js("fetch('/api/session').then(r=>r.json()).then(j=>j.csrf)")
    # register from Settings > System > Login
    page.evaluate("()=>{location.hash='#/settings/system/login'}")
    ph.wait(lambda: page.locator("#passkeyRegister").is_visible() and not page.locator("#passkeyRegister").is_disabled(),
            10, "register button enabled")
    page.fill("#passkeyLabel", "E2Eテスト")
    page.click("#passkeyRegister")
    ph.wait(lambda: "E2Eテスト" in page.locator("#passkeyList").inner_text(), 15, "passkey listed")
    listing = ph.js("fetch('/api/passkeys').then(r=>r.json())")
    assert len(listing["items"]) == 1 and listing["items"][0]["rp_id"] == "localhost", listing
    # log out of every device. The login screen then offers the face-login button, and where conditional
    # mediation is available (Chrome with the virtual authenticator) it may sign in by itself at once.
    old = [c for c in ph.context.cookies() if c["name"] == "qdl_session" and c["domain"].lstrip(".") == "localhost"][0]["value"]
    page.click("#revokeAll")
    cookie_now = lambda: ([c["value"] for c in ph.context.cookies() if c["name"] == "qdl_session" and c["domain"].lstrip(".") == "localhost"] or [""])[0]
    ph.wait(lambda: cookie_now() != old, 15, "session replaced or cleared by revoke-all")
    # "authed" alone is not enough: right after revoke-all the app may still hold state.authed === true
    # from the old session for a moment, so a conditional sign-in only counts once a new cookie exists.
    signed_in = lambda: ph.js("state.authed === true") and cookie_now() not in ("", old)
    ph.wait(lambda: page.locator("#passkeyLogin").is_visible() or signed_in(), 15,
            "login screen (or automatic conditional sign-in) after revoke-all")
    if not signed_in():
        try:
            page.click("#passkeyLogin", timeout=2000)
        except Exception:
            ph.wait(signed_in, 5, "button vanished: autofill sign-in")   # autofill won the race
    ph.wait(signed_in, 15, "passkey login with a new session cookie")
    ph.log("conditional UI signed in automatically" if page.locator("#passkeyLogin").count() and
           not page.locator("#login").is_visible() else "")
    fresh = [c for c in ph.context.cookies() if c["name"] == "qdl_session" and c["domain"].lstrip(".") == "localhost"][0]["value"]
    assert fresh != old, "revoke-all must have replaced the session"
    cookie = [c for c in ph.context.cookies() if c["name"] == "qdl_session" and c["domain"].lstrip(".") == "localhost"][0]
    days = (cookie["expires"] - time.time()) / 86400
    assert 29 < days <= 30.01, "passkey sessions last 30 days (got %.2f)" % days
    # the explicit button (mediation "optional"): disable the autofill path first so it cannot sign in by itself
    ph.csrf = ph.js("fetch('/api/session').then(r=>r.json()).then(j=>j.csrf)")
    ph.js("()=>{window.startConditionalLogin = async () => {}}")
    assert ph.post("/api/sessions/revoke-all", {})["status"] == 200
    ph.js("()=>{state.authed = false; return go()}")
    ph.wait(lambda: page.locator("#passkeyLogin").is_visible(), 10, "face-login button")
    assert ph.js("state.authed === false")
    page.click("#passkeyLogin")
    ph.wait(lambda: ph.js("state.authed === true"), 15, "login with the button")
    # clean up the credential so later scenarios start clean
    ph.csrf = ph.js("fetch('/api/session').then(r=>r.json()).then(j=>j.csrf)")
    item = ph.js("fetch('/api/passkeys').then(r=>r.json())")["items"][0]
    res = ph.post("/api/passkeys/delete", {"id": item["id"]})
    assert res["status"] == 200, res
    cdp.send("WebAuthn.disable")


@scenario("first-load", engines=("chromium",))
def first_load(ph):
    """v0.1.54: a heavy page whose load event is held back shows its text in the mirror long before it is fully
    loaded, and every first-load stage is measured (server stages, the phone's marks, blocked ad requests)."""
    tab = ph.open("/firstload.html?nodes=3000&slowload=6000&ads=6&delay=150")
    ph.wait(lambda: "E2E" in ph.mirror_text(), 15, "page text in the mirror")
    rb_tab = ph.env.rb._tabs.get(tab)
    assert rb_tab is not None and rb_tab.loading, "the page should still be loading when its text is already shown"
    assert rb_tab.dcl, "the copy was made before DOMContentLoaded was seen"
    ph.wait(lambda: ph.js("(()=>{const t=activeWebTab();return !!(t&&t.rb&&t.rb.tl&&t.rb.tl.result)})()"), 25,
            "the first-load result arrived")
    result = ph.js("activeWebTab().rb.tl.result")
    stages = {s["id"]: s for s in result["stages"]}
    for sid in ("create", "navigate", "parse", "capture"):
        assert stages[sid]["ms"] is not None, "stage %s was not measured: %s" % (sid, result["stages"])
    client = result["client"]
    for key in ("created", "choose", "doc", "paint", "full"):
        assert key in client, "phone mark %s missing: %s" % (key, client)
    assert client["created"] <= client["choose"] <= client["doc"] <= client["paint"] <= client["full"], client
    assert result["blocked"] >= 1, "no ad/tracker request was blocked: %s" % result
    assert result["load_ms"] is None or result["load_ms"] >= stages["parse"]["ms"], result
    assert result["line"] and "接続・遷移" in result["line"], result.get("line")
    assert isinstance(result.get("rtts"), int) and result["rtts"] <= 3, "requests until the first paint: %s" % result.get("rtts")
    assert ph.env.rb._tabs[tab].mirror_partials >= 1, "the first copy was not a partial (above the fold) copy"
    ph.wait(lambda: not (ph.env.rb._tabs[tab].mirror_cache or {}).get("partial", True), 15,
            "the full copy replaced the partial one")


# ------------------------------------------------------------------ v0.1.56: downloads header layout

@scenario("downloads-header-layout")
def downloads_header_layout(ph):
    """At phone width the Downloads title stays on one line (v0.1.55 squeezed it to 3 lines) and the
    header buttons stay tappable; the auto-clear switches are in settings and default OFF."""
    ph.js("()=>{location.hash='#/downloads'}")
    ph.wait(lambda: ph.js("!!document.querySelector('#view-downloads:not([hidden]) h1')"), 8, "downloads view")
    # JD2 is not running in the harness: reveal the header controls the way a running engine does
    ph.js("()=>{for(const id of ['downloadsToggle','downloadsClear','downloadsUpdated','downloadsRefresh'])$(id).hidden=false;"
          "$('downloadsUpdated').textContent='更新: 08:08:42'}")
    for width, height in ((390, 844), (430, 873), (1280, 800)):
        ph.page.set_viewport_size({"width": width, "height": height})
        ph.page.wait_for_timeout(250)
        m = ph.js("""()=>{const h=document.querySelector('#view-downloads h1'), r=h.getBoundingClientRect();
          const range=document.createRange(); range.selectNodeContents(h); const t=range.getBoundingClientRect();
          const lh=parseFloat(getComputedStyle(h).lineHeight)||parseFloat(getComputedStyle(h).fontSize)*1.4;
          const btn=id=>{const b=$(id).getBoundingClientRect();return {w:b.width,h:b.height,l:b.left,r:b.right,t:b.top}};
          return {h:r.height,w:r.width,textW:t.width,lh:lh,vw:innerWidth,
                  clear:btn('downloadsClear'),toggle:btn('downloadsToggle'),refresh:btn('downloadsRefresh'),
                  scrollW:document.documentElement.scrollWidth}}""")
        assert m["h"] <= m["lh"] * 1.6, "title wraps: %r" % m
        assert m["w"] >= m["textW"] - 1, "title squeezed: %r" % m
        assert m["scrollW"] <= m["vw"] + 1, "horizontal overflow: %r" % m
        for key in ("clear", "toggle", "refresh"):
            b = m[key]
            assert b["h"] >= 28 and b["w"] >= 28 and b["l"] >= 0 and b["r"] <= m["vw"] + 1, "%s not tappable: %r" % (key, b)
        if width < 760:
            assert m["clear"]["t"] > m["h"], "buttons should sit below the title on a phone: %r" % m
    # the settings switches exist and default OFF
    ph.js("()=>{location.hash='#/settings/jd2'}")
    ph.wait(lambda: ph.js("!!$('autoClearLinks')"), 8, "settings")
    assert ph.js("()=>$('autoClearLinks').checked===false&&$('autoClearDownloads').checked===false")
    assert ph.js("()=>$('autoClearMissing').checked===true")     # v0.1.70: default ON


# ------------------------------------------------------------------ passthrough display (v0.1.57)
# The app runs on http://localhost:P and the passthrough listener ("origin B") on http://localhost:Q: the same site
# (as in production, where both are the same *.ts.net name on two ports) and a secure context, which Service Workers
# need. The upstream "sites" are the fixture server (https, a.example.com / b.example.net). WebKit was not available
# where these were written: they are kept engine-neutral, but the WebKit run is unverified.

PT_PAGE = "https://a.example.com/ptx/page.html"


def _pt_local(ph, enable=True):
    """Turn the passthrough on (Settings API) and open the app on http://localhost:P, logged in."""
    page = ph.page
    if enable:
        res = ph.post("/api/settings/options", {"pt_enabled": True})
        assert res["status"] == 200, res
    local = ph.env.url.replace("127.0.0.1", "localhost")
    page.goto(local)
    ph.wait(lambda: page.locator("#password").is_visible() or ph.js("state.authed === true"), 15, "login form on localhost")
    if page.locator("#password").is_visible():
        page.fill("#password", PASSWORD)
        page.click("#loginButton")
    ph.wait(lambda: ph.js("state.authed === true"), 10, "login on localhost")
    ph.csrf = ph.js("fetch('/api/session').then(r=>r.json()).then(j=>j.csrf)")
    ph.js("refreshPt()")                  # the app asks for the status lazily (menu, settings, restored pt tab)
    if enable:
        ph.wait(lambda: ph.js("ptAvailable()"), 10, "passthrough available in the app")
    return ph.js("state.pt && state.pt.origin")


def _pt_frame(ph, timeout=15):
    """The Playwright Frame of the passthrough iframe (origin B)."""
    origin = ph.js("state.pt.origin")

    def find():
        for fr in ph.page.frames:
            if fr.url.startswith(origin + "/p/"):
                return fr
        return None
    return ph.wait(find, timeout, "passthrough frame under /p/")


def _pt_open(ph, url=PT_PAGE):
    """A passthrough tab at `url` (created the way the menu does it: history + tab.pt)."""
    ph.js("""(u)=>{const tab=createTab();pushHistory(tab,u);tab.current=u;tab.pt=true;saveTabs();navigate('#/tab/'+tab.id)}""", url)
    ph.wait(lambda: ph.js("(()=>{const t=activeWebTab();return !!(t&&t.pt&&t.history.length)})()"), 5, "passthrough tab active")
    fr = _pt_frame(ph)
    ph.wait(lambda: fr.locator("#t").count() > 0, 15, "page rendered in the frame")
    return fr


def _pt_cur(ph):
    return ph.js("(()=>{const t=activeWebTab();return t?t.current:''})()")


@scenario("pt-settings-ping")
def pt_settings_ping(ph):
    """The Settings switch turns the listener on (the app reloads for its CSP), and 接続を確認 reaches origin B."""
    ph.js("()=>{location.hash='#/settings/browser/pt'}")
    ph.wait(lambda: ph.page.locator("#ptEnabled").count() > 0, 8, "pt settings")
    assert ph.js("state.settings.pt_enabled === false"), "off by default"
    assert "tailscale serve --bg --https=8443 http://127.0.0.1:17865" in ph.js("$('ptCmd').textContent")
    ph.js("()=>{window.__beforeReload=1}")
    ph.page.locator("label[for=ptEnabled]").tap()
    ph.wait(lambda: ph.js("window.__beforeReload === undefined") and ph.js("state.pt && !!state.pt.origin"),
            15, "switch on and the app reloaded")
    ph.js("()=>{location.hash='#/settings/browser/pt'}")
    ph.page.locator("#ptCheck").tap()
    ph.wait(lambda: "接続できました" in ph.js("$('ptCheckResult').textContent"), 10, "ping ok")
    # the app's CSP names origin B only now
    csp = ph.js("fetch(location.href).then(r=>r.headers.get('content-security-policy'))")
    assert ph.js("state.pt.origin") in csp, csp


@scenario("pt-open-page")
def pt_open_page(ph):
    """A page is drawn in the frame; the Service Worker controls it; a second host's image comes through /p/."""
    _pt_local(ph)
    fr = _pt_open(ph)
    assert fr.locator("#t").inner_text() == "PT-FIXTURE-PAGE"
    ph.wait(lambda: fr.evaluate("!!navigator.serviceWorker.controller"), 5, "frame controlled by the Service Worker")
    ph.wait(lambda: fr.evaluate("document.documentElement.getAttribute('data-pt-js')") == "1", 5, "site script ran")
    ph.wait(lambda: fr.evaluate("(()=>{const i=document.getElementById('pic');return i.complete&&i.naturalWidth>0})()"), 10,
            "second-host image loaded")
    assert any(h == "b.example.net" and p == "/ptx/pic.png" for h, p, _c in ph.env.fx.pt_seen), ph.env.fx.pt_seen
    ph.wait(lambda: _pt_cur(ph) == PT_PAGE, 5, "tab address follows the page")
    ph.wait(lambda: ph.js("activeWebTab().ptTitle") == "PT Fixture A", 5, "tab title follows the page")


@scenario("pt-back-forward")
def pt_back_forward(ph):
    """A link click inside the frame is a new history entry in the app; the toolbar back / forward step it."""
    _pt_local(ph)
    fr = _pt_open(ph)
    fr.locator("#l2").tap()
    ph.wait(lambda: _pt_cur(ph).endswith("/ptx/page2.html"), 10, "link followed")
    ph.wait(lambda: "PT-FIXTURE-PAGE-2" in (_pt_frame(ph).locator("#t").inner_text() if _pt_frame(ph).locator("#t").count() else ""),
            10, "second page drawn")
    assert ph.js("activeWebTab().history.length") == 2
    ph.page.locator("#navBack").tap()
    ph.wait(lambda: _pt_cur(ph) == PT_PAGE, 10, "back")
    ph.wait(lambda: ph.js("activeWebTab().index") == 0, 5, "index 0")
    ph.js("openMenu()")      # v0.1.60: → lives in the menu on a phone-width bar
    ph.page.locator("#menu [data-action='forward']").tap()
    ph.wait(lambda: _pt_cur(ph).endswith("/ptx/page2.html"), 10, "forward")
    ph.page.locator("#navBack").tap()
    ph.wait(lambda: _pt_cur(ph) == PT_PAGE, 10, "back again")
    ph.page.locator("#navBack").tap()                # nothing earlier: a toast, the tab stays
    ph.wait(lambda: "これより前のページはありません" in ph.js("document.getElementById('notice').innerText"), 5, "toast")
    assert ph.js("state.route.view") == "tab"


@scenario("pt-flick", engines=("chromium",))
def pt_flick(ph):
    """A sideways flick over the framed page is back / forward (the helper reports it to the app)."""
    _pt_local(ph)
    fr = _pt_open(ph)
    fr.locator("#l2").tap()
    ph.wait(lambda: _pt_cur(ph).endswith("/ptx/page2.html"), 10, "link followed")
    ph.page.wait_for_timeout(600)
    _flick(ph, 200)
    ph.wait(lambda: _pt_cur(ph) == PT_PAGE, 8, "flick back (url=%s)" % _pt_cur(ph))
    ph.page.wait_for_timeout(600)
    _flick(ph, -200)
    ph.wait(lambda: _pt_cur(ph).endswith("/ptx/page2.html"), 8, "flick forward (url=%s)" % _pt_cur(ph))


@scenario("pt-redirect")
def pt_redirect(ph):
    """A redirect stays inside /p/ and the app's address becomes the final page."""
    _pt_local(ph)
    fr = _pt_open(ph)
    fr.locator("#lredir").tap()
    ph.wait(lambda: _pt_cur(ph) == "https://a.example.com/ptx/page2.html", 10, "final address (now %s)" % _pt_cur(ph))
    ph.wait(lambda: _pt_frame(ph).url.startswith(ph.js("state.pt.origin") + "/p/s/a.example.com/ptx/page2.html"), 5,
            "frame still under /p/")


@scenario("pt-cookie-first-party")
def pt_cookie_first_party(ph):
    """A's cookie goes back to A on the next request, never to the other site, and never into the phone's own jar."""
    _pt_local(ph)
    fr = _pt_open(ph)
    ph.wait(lambda: fr.evaluate("document.documentElement.getAttribute('data-pt-echo-a')"), 10, "first-party echo")
    echo_a = fr.evaluate("document.documentElement.getAttribute('data-pt-echo-a')")
    assert "host=a.example.com" in echo_a and "ptc=A1" in echo_a, echo_a
    ph.wait(lambda: fr.evaluate("document.documentElement.getAttribute('data-pt-echo-b')"), 10, "cross-site echo")
    echo_b = fr.evaluate("document.documentElement.getAttribute('data-pt-echo-b')")
    assert "host=b.example.net" in echo_b and "ptc" not in echo_b, echo_b
    assert not [c for c in ph.context.cookies() if c["name"] == "ptc"], "the site's cookie must stay on the NAS"
    assert [c for c in ph.context.cookies() if c["name"] == "qd_pt"], "only the passthrough session cookie is set"


@scenario("pt-download-page")
def pt_download_page(ph):
    """A download link shows the JD2 / save page; the JD2 button goes through the app's own confirm."""
    _pt_local(ph)
    ph.page.on("dialog", lambda dialog: dialog.accept())
    fr = _pt_open(ph)
    fr.locator("#ldl").tap()
    ph.wait(lambda: _pt_frame(ph).locator("#jd2").count() > 0, 10, "download page")
    dl = _pt_frame(ph)
    assert "e2e.bin" in dl.locator("body").inner_text()
    dl.locator("#jd2").tap()
    ph.wait(lambda: "追加したリンクに入れました" in _pt_frame(ph).locator("#msg").inner_text(), 10, "staged through the app")
    links = ph.js("fetch('/api/links').then(r=>r.json())")
    assert "ptx/dl.bin" in str(links), links


@scenario("pt-off-refuses")
def pt_off_refuses(ph):
    """Turning the switch off stops the listener: origin B no longer answers."""
    import urllib.error
    import urllib.request
    _pt_local(ph)
    origin = ph.js("state.pt.origin")
    with urllib.request.urlopen(origin + "/pt/ping", timeout=5) as res:
        assert res.status == 200
    assert ph.post("/api/settings/options", {"pt_enabled": False})["status"] == 200

    def refused():
        try:
            urllib.request.urlopen(origin + "/pt/ping", timeout=2)
            return False
        except (urllib.error.URLError, OSError):
            return True
    ph.wait(refused, 8, "listener stopped")
    try:
        urllib.request.urlopen(ph.env.url.replace("127.0.0.1", "localhost") + "pt/ping", timeout=3)
        raise AssertionError("the Hub's own port must not serve passthrough routes")
    except urllib.error.HTTPError as exc:
        assert exc.code in (401, 403, 404), exc.code


@scenario("pt-menu-switch")
def pt_menu_switch(ph):
    """メニュー › パススルー moves the page from the NAS browser into the frame (the NAS tab is closed); another
    display moves it back."""
    ph.setting("pt_enabled", False)
    ph.js("()=>{location.hash='#/home'}")
    tab = ph.open("/article.html")
    ph.js("refreshPt()")
    ph.wait(lambda: ph.js("state.pt !== null"), 5, "status loaded")
    ph.js("()=>{configureMenu()}")
    assert ph.js("$('menuPt').disabled === true"), "greyed out while the setting is off"
    _pt_local(ph)
    tab = ph.open("/article.html")
    ph.js("()=>{configureMenu()}")
    assert ph.js("$('menuPt').disabled === false")
    ph.js("()=>{setUserDisplay(activeWebTab(),'pt')}")
    ph.wait(lambda: ph.js("(()=>{const t=activeWebTab();return !!(t&&t.pt&&!t.rb)})()"), 8, "moved to passthrough")
    ph.wait(lambda: tab not in ph.env.tabs(), 8, "NAS tab closed")
    fr = _pt_frame(ph)
    ph.wait(lambda: fr.locator("h1").count() > 0, 15, "article drawn by the phone")
    ph.js("()=>{setUserDisplay(activeWebTab(),'mirror')}")
    ph.wait(lambda: ph.js("(()=>{const t=activeWebTab();return !!(t&&!t.pt&&t.rb)})()"), 15, "back on the NAS browser")
    ph.take_errors("/api/rb/mirror")          # a mirror request in flight while its NAS tab was closed (400)




# ------------------------------------------------------------------ v0.1.59: request budgets and optimistic UI
# Requests that do not count towards "tap -> first content": the long-poll of the state, the debug log, the report of
# the first-load line, the page's own images/fonts and releases of the display that was left.
BUDGET_PASSIVE = ("/api/state", "/api/debug/log", "/api/rb/firstload", "/api/rb/mirror/res", "/api/rb/mirror/release",
                  "/api/rb/snapshot/release", "/api/rb/reader/img")


class ApiCounter:
    """Collects the /api/ requests (method + path) the phone page makes; `budget()` leaves out the passive ones."""

    def __init__(self, page):
        self.items = []
        self.states = []          # times (monotonic) at which a /api/state answer arrived
        self.seen = []            # counter.mark() at the moment a watched text appeared on the phone
        self.page = page
        page.on("request", self._request)
        page.on("response", self._response)
        page.expose_function("__qdSeen", lambda: self.seen.append(self.mark()))

    def watch(self, kind, needle):
        """A 10 ms poller inside the page calls back the moment `needle` is in the mirror copy / reader text."""
        self.seen.clear()
        self.page.evaluate("""([kind, needle]) => {
          const tick = () => {
            let text = '';
            try { text = kind === 'mirror' ? (mirrorDoc() ? mirrorDoc().body.innerText : '') : document.getElementById('readerBody').innerText; }
            catch (e) { /* mid navigation */ }
            if (text.indexOf(needle) >= 0) { window.__qdSeen(); return; }
            setTimeout(tick, 10);
          };
          tick();
        }""", [kind, needle])

    def _request(self, request):
        url = request.url
        if "/api/" not in url:
            return
        path = "/api/" + url.split("/api/", 1)[1].split("?", 1)[0]
        self.items.append((request.method, path))

    def _response(self, response):
        if response.url.split("?", 1)[0].endswith("/api/state"):
            self.states.append(time.monotonic())

    def mark(self):
        return len(self.items)

    def budget(self, since=0, until=None):
        return [item for item in self.items[since:until] if item[1] not in BUDGET_PASSIVE]


@scenario("budget-open-mirror", display="mirror")
def budget_open_mirror(ph):
    """v0.1.59: opening a page from the address bar needs at most 3 HTTP requests before its text is on the phone
    (tab creation with validation, viewport and the first copy inline; then the copy document)."""
    counter = ApiCounter(ph.page)
    since = counter.mark()
    counter.watch("mirror", "E2E テスト記事")
    started = time.monotonic()
    ph.open("/article.html")
    ph.wait(lambda: counter.seen, 15, "mirror text", interval=0.02)
    elapsed = time.monotonic() - started
    used = counter.budget(since, counter.seen[0])      # only what was asked before the text was there
    ph.log("requests before the text: %s (%.2fs)" % (used, elapsed))
    assert len(used) <= 3, "opening a page took %d requests: %s" % (len(used), used)
    assert not any(path == "/api/browser/validate" for _, path in used), "validation is part of the tabs request: %s" % used
    ph.wait(lambda: ph.js("(()=>{const t=activeWebTab();return !!(t&&t.rb&&t.rb.tl&&t.rb.tl.result)})()"), 20, "first-load result")
    assert ph.js("activeWebTab().rb.tl.result.rtts") is not None, "the request count did not reach the speed line"
    assert "往復" in ph.js("activeWebTab().rb.tl.result.line")
    # following a link in an open tab: the new page's copy also comes with the navigation answer
    pt = ph.wait(lambda: ph.mirror_point("doc=>doc.querySelector('a[data-href$=\"/article2.html\"]')"), 8, "a link in the mirror")
    ph.suppress_ios_click()
    since = counter.mark()
    counter.watch("mirror", "二つ目の記事")
    ph.tap(pt["x"], pt["y"])
    ph.wait(lambda: counter.seen, 10, "second page", interval=0.02)
    # (the metadata long-poll that is always outstanding is re-issued as the page loads: not a step of this chain)
    used = [i for i in counter.budget(since, counter.seen[0]) if i[1] != "/api/rb/meta"]
    ph.log("link tap requests before the text: %s" % (used,))
    assert len(used) <= 3, "a link tap took %d requests: %s" % (len(used), used)


@scenario("budget-open-reader", display="reader")
def budget_open_reader(ph):
    """v0.1.59: the same for the reader display (tab creation answers with the extracted text)."""
    counter = ApiCounter(ph.page)
    since = counter.mark()
    counter.watch("reader", "E2E テスト記事")
    ph.open("/article.html")
    ph.wait(lambda: counter.seen, 15, "reader text", interval=0.02)
    used = counter.budget(since, counter.seen[0])
    ph.log("requests before the text: %s" % (used,))
    assert len(used) <= 3, "opening a page in the reader took %d requests: %s" % (len(used), used)
    assert not any(path in ("/api/browser/validate", "/api/rb/viewport", "/api/rb/reader") for _, path in used), used


@scenario("budget-idle-state", engines=("chromium", "webkit"))
def budget_idle_state(ph):
    """v0.1.59: with nothing happening, the Downloads view makes at most one /api/state answer in 10 s (memory
    figures, uptimes and counters used to wake the long-poll every ~2 s)."""
    counter = ApiCounter(ph.page)
    ph.env.shake(True)                          # free memory and the container's memory figure change every 0.3 s
    try:
        ph.js("()=>{location.hash='#/downloads'}")
        ph.wait(lambda: ph.js("!!document.querySelector('#view-downloads:not([hidden]) h1')"), 8, "downloads view")
        ph.page.wait_for_timeout(2500)          # the first answers after the view change settle
        start = time.monotonic()
        before = len(counter.states)
        ph.page.wait_for_timeout(10000)
    finally:
        ph.env.shake(False)
    answers = [t for t in counter.states[before:] if t >= start]
    others = [i for i in counter.items if i[1] != "/api/state"]
    ph.log("state answers in 10 s: %d; other requests so far: %d" % (len(answers), len(others)))
    assert len(answers) <= 1, "/api/state answered %d times in 10 s while idle" % len(answers)


@scenario("optimistic-switch", engines=("chromium", "webkit"))
def optimistic_switch(ph):
    """v0.1.59: a settings switch changes on the phone before the Hub has answered; a failed save puts it back
    and says why."""
    ph.js("()=>{location.hash='#/settings/browser'}")
    ph.wait(lambda: ph.js("!!document.querySelector('#view-settings:not([hidden])') && !!document.getElementById('rbLinkBoxes')"),
            8, "browser settings")
    ph.page.wait_for_timeout(500)
    held = []
    ph.page.route("**/api/settings/options", lambda route: held.append(route))
    start = ph.js("document.getElementById('rbLinkBoxes').checked")
    key_start = ph.js("state.settings.rb_link_boxes === true")
    ph.page.locator("label.switch[for=rbLinkBoxes]").scroll_into_view_if_needed()
    ph.page.locator("label.switch[for=rbLinkBoxes]").tap()
    ph.page.wait_for_timeout(200)
    assert held, "the save request was not sent"
    now = ph.js("({checked: document.getElementById('rbLinkBoxes').checked, setting: state.settings.rb_link_boxes === true,"
                " busy: document.getElementById('rbLinkBoxes').getAttribute('aria-busy')})")
    assert now["checked"] != start and now["setting"] != key_start, "the switch did not change before the reply: %s" % now
    assert now["busy"] == "true", "no busy mark while saving: %s" % now
    ph.page.wait_for_timeout(1300)              # the reply is held for 1.5 s in total
    held.pop().continue_()
    ph.wait(lambda: ph.js("document.getElementById('rbLinkBoxes').getAttribute('aria-busy') !== 'true'"), 5, "saved")
    assert ph.js("document.getElementById('rbLinkBoxes').checked") != start
    # a failing save: the switch changes at once, then goes back, with an error toast
    ph.page.unroute("**/api/settings/options")
    ph.page.route("**/api/settings/options", lambda route: held.append(route))
    was = ph.js("document.getElementById('rbLinkBoxes').checked")
    ph.page.locator("label.switch[for=rbLinkBoxes]").tap()
    ph.page.wait_for_timeout(200)
    assert held and ph.js("document.getElementById('rbLinkBoxes').checked") != was, "no change before the failing reply"
    held.pop().fulfill(status=500, content_type="application/json", body='{"error": "保存できません（テスト）"}')
    ph.wait(lambda: ph.js("document.getElementById('rbLinkBoxes').checked") == was, 5, "the switch went back")
    assert ph.js("(state.settings.rb_link_boxes === true) === document.getElementById('rbLinkBoxes').checked"), "state and switch differ"
    ph.wait(lambda: ph.page.eval_on_selector_all("#notice .toast.error", "els=>els.length") >= 1, 5, "error toast")
    ph.take_errors("status of 500")
    ph.page.unroute("**/api/settings/options")
    # put the setting back the way it was
    ph.setting("rb_link_boxes", start)





# ------------------------------------------------------------------ v0.1.60: one top bar, collapse, domain, tap feedback, app shell cache

NARROW = {"width": 430, "height": 873}
MENU_ACTIONS = {"newTab", "reopen", "forward", "reload", "display:reader", "display:mirror", "display:tiles", "display:live",
                "display:pt", "quality:speed", "quality:standard", "quality:high", "perf", "forgetSite", "stageUrl", "bookmark", "closeTab",
                "bookmarks", "settings", "logout"}


def _visible(ph, selector):
    return ph.js("(s)=>[...document.querySelectorAll(s)].filter(e=>{const r=e.getBoundingClientRect();"
                 "const cs=getComputedStyle(e);return r.width>0&&r.height>0&&cs.visibility!=='hidden'&&cs.display!=='none'}).length", selector)


@scenario("bar-single-row")
def bar_single_row(ph):
    """v0.1.60: a web tab has ONE bar (<= 64 px at 430 wide, one ⋮ menu button), no floating reload button, a status
    banner that takes no room while there is nothing to say, and every item of the old two-row menus in the one menu."""
    ph.page.set_viewport_size(NARROW)
    tab = ph.open("/article.html")
    ph.wait(lambda: "E2E テスト記事" in ph.mirror_text(), 15, "mirror content")
    ph.page.wait_for_timeout(300)
    bar = ph.js("(()=>{const r=document.querySelector('.toolbar').getBoundingClientRect();return {top:r.top,h:r.height}})()")
    assert bar["h"] <= 64, "the top bar is %.0f px high" % bar["h"]
    stage = ph.js("(()=>{const r=document.getElementById('remoteStage').getBoundingClientRect();return r.top})()")
    assert stage <= 70, "the page starts %.0f px below the top" % stage          # was ~97 px with the second row
    assert _visible(ph, "#menuButton, #webStatus .icon-btn, [aria-label='メニューを開く']") == 1, "more than one ⋮ menu button"
    assert _visible(ph, "use[href='#i-more']") == 1, "more than one ⋮ icon"
    assert ph.js("document.getElementById('webStatus').getBoundingClientRect().height") == 0, "the status row still takes space"
    assert ph.js("document.getElementById('tileRefresh')") is None and _visible(ph, "[aria-label='再取得']") == 0, "a floating reload button is left"
    assert _visible(ph, "#navBack") == 1 and _visible(ph, "#navForward, #navReload, #starBtn") == 0, "≤430 px keeps ← only"
    chip = ph.js("document.getElementById('modeBtn').textContent")
    assert chip == "ミラー", chip
    # the chip opens the display chooser (the menu with the "表示" group)
    ph.tap(*_centre(ph, "#modeBtn"))
    ph.wait(lambda: not ph.js("document.getElementById('menu').hidden"), 3, "menu opened by the display chip")
    actions = set(ph.js("[...document.querySelectorAll('#menu [data-action]')].map(e=>e.dataset.action)"))
    assert MENU_ACTIONS <= actions, "menu items missing: %s" % sorted(MENU_ACTIONS - actions)
    assert _visible(ph, "#menu [data-action='forward'], #menu [data-action='reload']") == 2, "→ / ⟳ not offered in the menu"
    assert "a.example.com" in ph.js("document.getElementById('menuPage').textContent"), "page line missing from the menu"
    ph.js("closeMenu(false)")
    # ⋮ menu: reload from the menu reloads the page on the NAS
    ph.js("openMenu()")
    hits = ph.env.fx.count("/article.html")
    ph.tap(*_centre(ph, "#menu [data-action='reload']"))
    ph.wait(lambda: ph.env.fx.count("/article.html") > hits, 10, "reload from the menu reached the page")
    # wide screens keep the full bar
    ph.page.set_viewport_size({"width": 1180, "height": 820})
    ph.page.wait_for_timeout(300)
    assert _visible(ph, "#navBack, #navForward, #navReload, #starBtn") == 4, "wide bar lost its buttons"
    assert ph.js("document.querySelector('.toolbar').getBoundingClientRect().height") <= 64


def _centre(ph, selector):
    box = ph.page.locator(selector).first.bounding_box()
    return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2


@scenario("omnibox-domain")
def omnibox_domain(ph):
    """v0.1.60: the address bar shows the domain while idle (https lock, no path), the full URL when focused, and
    the domain again after leaving it; app screens keep their qnap:// address."""
    ph.page.set_viewport_size(NARROW)
    ph.open("/article.html")
    ph.wait(lambda: "E2E テスト記事" in ph.mirror_text(), 15, "mirror content")
    value = lambda: ph.js("document.getElementById('omnibox').value")
    assert value() == "a.example.com", value()
    assert ph.js("document.getElementById('omniIcon').getAttribute('href')") == "#i-lock"
    ph.tap(*_centre(ph, "#omnibox"))
    ph.wait(lambda: ph.js("document.activeElement && document.activeElement.id") == "omnibox", 3, "omnibox focused")
    ph.wait(lambda: value().endswith("/article.html"), 3, "full URL while editing")
    assert value() == "https://a.example.com/article.html", value()
    assert ph.js("(()=>{const o=document.getElementById('omnibox');return o.selectionStart===0&&o.selectionEnd===o.value.length})()"), "URL not selected"
    ph.js("document.getElementById('omnibox').blur()")
    assert value() == "a.example.com", value()
    ph.js("()=>{location.hash='#/downloads'}")
    ph.wait(lambda: value() == "qnap://downloads", 3, "qnap:// address on an app screen")


@scenario("bar-collapse-mirror")
def bar_collapse_mirror(ph):
    """v0.1.60: scrolling the mirror down hides the top bar and the tab bar, scrolling up shows them, the top shows
    them, focusing the address bar or opening the menu shows them; the size the NAS browser is told does not change."""
    ph.page.set_viewport_size(NARROW)
    ph.open("/long1.html")
    ph.wait(lambda: "長いページ 1" in ph.mirror_text(), 15, "long page in mirror")
    ph.page.wait_for_timeout(600)
    collapsed = lambda: ph.js("document.body.classList.contains('chrome-collapsed')")
    bar_bottom = lambda: ph.js("document.querySelector('.toolbar').getBoundingClientRect().bottom")
    scroll = lambda y: ph.js("(y)=>{const d=mirrorDoc();mirrorScroller(d).scrollTop=y}", y)
    assert not collapsed() and bar_bottom() > 40
    view0 = ph.js("stageViewport()")
    stage0 = ph.js("document.getElementById('remoteStage').clientHeight")
    for y in (60, 140, 260, 380, 520):             # a finger drag: many small steps down
        scroll(y)
        ph.page.wait_for_timeout(60)
    ph.wait(collapsed, 3, "bars collapse when the page is scrolled down")
    ph.page.wait_for_timeout(450)
    assert bar_bottom() <= 1, "the top bar is still on screen (bottom=%.0f)" % bar_bottom()
    assert ph.js("document.querySelector('.mobile-bar').getBoundingClientRect().top") >= 860, "the tab bar is still on screen"
    assert ph.js("document.getElementById('remoteStage').clientHeight") > stage0 + 40, "the page did not use the freed space"
    assert ph.js("stageViewport()") == view0, "the NAS viewport changed with the collapse: %s vs %s" % (ph.js("stageViewport()"), view0)
    assert ph.js("document.getElementById('remoteStage').getBoundingClientRect().top") <= 1, "page does not start at the top"
    # scrolling up shows them again
    for y in (500, 440, 380, 330):
        scroll(y)
        ph.page.wait_for_timeout(60)
    ph.wait(lambda: not collapsed(), 3, "bars return when scrolling up")
    ph.page.wait_for_timeout(450)
    assert bar_bottom() > 40 and ph.js("document.querySelector('.mobile-bar').getBoundingClientRect().top") < 860
    assert ph.js("stageViewport()") == view0
    # collapse again, then the top of the page
    for y in (420, 520, 640, 780):
        scroll(y)
        ph.page.wait_for_timeout(60)
    ph.wait(collapsed, 3, "collapsed again")
    scroll(0)
    ph.wait(lambda: not collapsed(), 3, "the bars come back at the top of the page")
    # the address bar and the menu are never hidden under a collapse
    for y in (200, 340, 480, 620):
        scroll(y)
        ph.page.wait_for_timeout(60)
    ph.wait(collapsed, 3, "collapsed (3)")
    ph.js("openMenu()")
    assert not collapsed(), "opening the menu must show the bar"
    ph.js("closeMenu(false)")
    for y in (700, 800, 900, 1000):
        scroll(y)
        ph.page.wait_for_timeout(60)
    ph.wait(collapsed, 3, "collapsed (4)")
    ph.js("focusOmnibox()")
    assert not collapsed(), "focusing the address bar must show the bar"
    scroll(1300); scroll(1500); scroll(1700)
    ph.page.wait_for_timeout(500)
    assert not collapsed(), "scrolling must not hide the bar while the address bar has focus"
    # v0.1.63: the width no longer decides (this context is a touch device): a wide touch screen (iPad) collapses too.
    # The mouse case (a PC keeps its bars) is in desktop-layout, the iPad one in ipad-layout.
    ph.js("document.getElementById('omnibox').blur()")
    ph.page.set_viewport_size({"width": 1180, "height": 820})
    ph.page.wait_for_timeout(300)
    for y in (0, 100, 300, 500, 700):
        scroll(y)
        ph.page.wait_for_timeout(60)
    ph.wait(collapsed, 3, "a wide touch screen collapses its bars as well")


@scenario("bar-collapse-reader", display="reader")
def bar_collapse_reader(ph):
    """v0.1.60: the same collapse for the reader display."""
    ph.page.set_viewport_size({"width": 430, "height": 500})       # short, so that the article scrolls
    ph.open("/article.html")
    ph.wait(lambda: ph.js("document.querySelectorAll('#readerBody .reader-p').length") >= 3, 15, "reader paragraphs")
    ph.page.wait_for_timeout(500)
    scroller = "document.getElementById('readerScroll')"
    assert ph.js("%s.scrollHeight - %s.clientHeight" % (scroller, scroller)) > 200, "the article does not scroll in this window"
    for y in (50, 120, 200, 280):
        ph.js("(y)=>{document.getElementById('readerScroll').scrollTop=y}", y)
        ph.page.wait_for_timeout(60)
    ph.wait(lambda: ph.js("document.body.classList.contains('chrome-collapsed')"), 3, "reader collapses the bars")
    ph.page.wait_for_timeout(300)
    for y in (240, 200, 160):
        ph.js("(y)=>{document.getElementById('readerScroll').scrollTop=y}", y)
        ph.page.wait_for_timeout(60)
    ph.wait(lambda: not ph.js("document.body.classList.contains('chrome-collapsed')"), 3, "reader shows the bars again")


@scenario("link-tap-feedback")
def link_tap_feedback(ph):
    """v0.1.60: a link tap in the mirror shows feedback within 100 ms (the tapped link is highlighted, the progress
    line under the bar is on) and the old page stays until the new copy arrives; the request goes out on pointerup."""
    ph.page.set_viewport_size(NARROW)
    tab = ph.open("/article.html")
    ph.wait(lambda: "E2E テスト記事" in ph.mirror_text(), 15, "mirror content")
    ph.page.wait_for_timeout(1500)
    ph.suppress_ios_click()
    pt = ph.wait(lambda: ph.mirror_point("doc=>doc.querySelector('a[data-href$=\"/article2.html\"]')"), 8, "a link in the mirror")
    ph.js("""()=>{
      const rec = window.__fb = {};
      const d = mirrorDoc();
      d.addEventListener('pointerup', () => { rec.up = performance.now(); }, true);
      const of = window.fetch;
      window.fetch = function(u) { if (String(u).indexOf('/api/rb/navigate') >= 0 && !rec.req) rec.req = performance.now(); return of.apply(this, arguments); };
      const iv = setInterval(() => {
        if (!rec.up) return;
        const dd = mirrorDoc();
        if (!rec.line && !document.getElementById('webLoading').hidden) rec.line = performance.now();
        if (!rec.mark && dd && dd.querySelector('.qd-tap')) { rec.mark = performance.now(); rec.oldText = dd.body.innerText.indexOf('E2E テスト記事') >= 0; }
        if (!rec.copy && dd && dd.body.innerText.indexOf('二つ目の記事') >= 0) { rec.copy = performance.now(); clearInterval(iv); }
      }, 2);
    }""")
    ph.tap(pt["x"], pt["y"])
    ph.wait(lambda: ph.js("window.__fb.copy || 0"), 10, "the new page's copy")
    rec = ph.js("window.__fb")
    ph.log("tap->request %.1f ms, ->line %.1f ms, ->highlight %.1f ms, ->copy %.0f ms" % (
        rec["req"] - rec["up"], rec["line"] - rec["up"], rec["mark"] - rec["up"], rec["copy"] - rec["up"]))
    assert rec["req"] - rec["up"] < 50, "the navigate request was sent %.0f ms after pointerup" % (rec["req"] - rec["up"])
    assert rec["line"] - rec["up"] < 100, "no progress line within 100 ms (%.0f ms)" % (rec["line"] - rec["up"])
    assert rec["mark"] - rec["up"] < 100 and rec["oldText"], "the tapped link was not marked on the old page in time: %s" % rec
    assert rec["copy"] > rec["mark"], "the page was swapped before the feedback was shown"
    ph.wait(lambda: ph.env.nas_url(tab).endswith("/article2.html"), 10, "NAS navigated")


@scenario("reader-link-feedback", display="reader")
def reader_link_feedback(ph):
    """v0.1.60: the reader marks a tapped link and starts the progress line at once; the old article stays."""
    ph.page.set_viewport_size(NARROW)
    tab = ph.open("/article.html")
    ph.wait(lambda: ph.js("document.querySelectorAll('#readerBody a[data-href]').length") >= 1, 15, "reader links")
    ph.page.wait_for_timeout(1000)
    ph.js("""()=>{
      const rec = window.__fb = {};
      const a = document.querySelector('#readerBody a[data-href$="/article2.html"]');
      a.addEventListener('click', () => { rec.up = performance.now(); }, true);
      const iv = setInterval(() => {
        if (!rec.up) return;
        if (!rec.line && !document.getElementById('webLoading').hidden) rec.line = performance.now();
        if (!rec.mark && a.classList.contains('is-tapped')) { rec.mark = performance.now(); rec.old = document.getElementById('readerBody').innerText.indexOf('E2E テスト記事') >= 0 || a.isConnected; }
        if (rec.line && rec.mark) clearInterval(iv);
      }, 2);
    }""")
    ph.tap(*_centre(ph, "#readerBody a[data-href$='/article2.html']"))
    ph.wait(lambda: ph.js("window.__fb.line && window.__fb.mark"), 5, "reader feedback")
    rec = ph.js("window.__fb")
    assert rec["line"] - rec["up"] < 100 and rec["mark"] - rec["up"] < 100, rec
    ph.wait(lambda: ph.env.nas_url(tab).endswith("/article2.html"), 10, "NAS navigated")


def _shell_state(ph):
    return ph.js("""async()=>{
      const names = await caches.keys();
      const urls = [];
      for (const n of names) { const c = await caches.open(n); for (const r of await c.keys()) urls.push(new URL(r.url).pathname); }
      const nav = performance.getEntriesByType('navigation')[0];
      return {names, urls, controller: !!navigator.serviceWorker.controller, workerStart: nav ? nav.workerStart : -1};
    }""")


@scenario("app-shell-cache", engines=("chromium",))
def app_shell_cache(ph):
    """v0.1.60: the app shell is cached by a Service Worker on the app origin: the second load is served by it (no
    /api/* or mirror document is ever stored), a version change drops the old shell and loads the new one, and
    「アプリのキャッシュを消す」 removes the worker and the caches. (Chromium: Playwright's WebKit has no Service
    Worker support for 127.0.0.1 that we could verify here.)"""
    import webapp
    ph.page.reload()
    ph.page.wait_for_selector("#omnibox", state="attached")
    ph.wait(lambda: ph.js("state.authed === true"), 10, "session")
    ph.js("navigator.serviceWorker.ready.then(()=>true)")
    ph.wait(lambda: ph.js("(async()=>{const r=await navigator.serviceWorker.getRegistration();return !!(r&&r.active)})()"), 15, "worker active")
    version = webapp.VERSION
    first = _shell_state(ph)
    assert "qd-shell-" + version in first["names"], first
    for path in ("/", "/app.js", "/style.css"):
        assert path in first["urls"], "%s is not in the shell cache: %s" % (path, first["urls"])
    # the second load comes out of the worker
    ph.open("/article.html")
    ph.wait(lambda: "E2E テスト記事" in ph.mirror_text(), 15, "mirror content (API traffic happened)")
    ph.page.reload()
    ph.page.wait_for_selector("#omnibox", state="attached")
    ph.wait(lambda: ph.js("state.authed === true"), 10, "session after reload")
    second = _shell_state(ph)
    assert second["controller"], "the page is not controlled by the worker on the second load"
    assert second["workerStart"] > 0, "the navigation did not go through the worker (workerStart=%s)" % second["workerStart"]
    bad = [u for u in second["urls"] if u.startswith("/api") or "mirror" in u or u.startswith("/p/") or u.startswith("/pt")]
    assert not bad and len(second["urls"]) <= 8, "the cache holds more than the shell: %s" % second["urls"]
    assert second["names"] == ["qd-shell-" + version], second["names"]
    # a stale shell: the Hub is newer than this page -> drop the cache and load the new shell (once)
    ph.js("window.__marker = 1")
    old = webapp.VERSION
    webapp.VERSION = "9.9.9"
    try:
        ph.js("checkBootVersion('9.9.9')")
        ph.wait(lambda: ph.js("window.__marker") is None, 15, "page reloaded for the new version")
        ph.page.wait_for_selector("#omnibox", state="attached")
        ph.wait(lambda: ph.js("state.authed === true"), 10, "session after the version reload")
        ph.wait(lambda: "qd-shell-9.9.9" in _shell_state(ph)["names"], 15, "new shell cached")
        names = _shell_state(ph)["names"]
        assert names == ["qd-shell-9.9.9"], "old shell cache survived: %s" % names
        assert ph.js("sessionStorage.getItem('qd.shellReload')") == "9.9.9"
        # (once per Hub version: the same mismatch again only offers the toast, no reload loop)
        ph.js("window.__marker = 2")
        ph.js("checkBootVersion('9.9.9')")
        ph.page.wait_for_timeout(1500)
        assert ph.js("window.__marker") == 2, "reload loop"
    finally:
        webapp.VERSION = old
    # 「アプリのキャッシュを消す」
    ph.js("window.__marker = 3")
    ph.js("navigate('#/settings/system/updates')")
    ph.wait(lambda: _visible(ph, "#appCacheClear") == 1, 8, "the cache reset button")
    ph.page.locator("#appCacheClear").tap()
    ph.wait(lambda: ph.js("window.__marker") is None, 10, "page reloaded after clearing the cache")
    ph.page.wait_for_selector("#omnibox", state="attached")
    after = _shell_state(ph)
    assert after["workerStart"] == 0 and not after["controller"], "the reset page was still served by the worker: %s" % after
    ph.take_errors("Failed to load resource")


# ------------------------------------------------------------------ v0.1.62: home, links, downloads, JD2 settings, site display, feedback
RG1 = "https://rapidgator.net/file/0123456789abcdef0123456789abcdef/a.rar"
RG2 = "https://rapidgator.net/file/fedcba9876543210fedcba9876543210/b.rar"
RG3 = "https://rapidgator.net/file/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/c.rar"


def _go(ph, hash_):
    ph.js("(h)=>{location.hash=h}", hash_)


def _fake_clipboard(ph, text=None):
    """Replace navigator.clipboard.readText with a counter (text=None: the read is refused). Returns nothing; the count
    is window.__clipReads."""
    ph.js("""(t)=>{window.__clipReads=0;
      Object.defineProperty(navigator,'clipboard',{configurable:true,value:{readText:()=>{window.__clipReads++;
        return t===null?Promise.reject(new Error('denied')):Promise.resolve(t)}}})}""", text)


@scenario("home-tabs-recent")
def home_tabs_recent(ph):
    """v0.1.62: the browse home lists the open tabs as cards (tap = switch, x = close) and the recent pages (this device
    only; cleared by the button), and the 「コピーしたURLを開く」 chip reads the clipboard only when it is tapped."""
    ph.page.set_viewport_size(NARROW)
    ph.js("()=>{localStorage.removeItem('qnapdl.recent.v1')}")
    _fake_clipboard(ph, "  https://a.example.com/article2.html  ")
    first = ph.open("/article.html")
    ph.wait(lambda: "E2E" in ph.mirror_text(), 15, "first page")
    _go(ph, "#/home")
    ph.wait(lambda: _visible(ph, "#view-home:not([hidden]) .tab-card") == 1, 8, "one tab card")
    second = None
    ph.page.wait_for_timeout(300)
    assert ph.js("window.__clipReads") == 0, "the clipboard was read without a tap"
    # the chip opens the copied address in a NEW tab (the home has no tab of its own)
    ph.page.locator("#homePasteOpen").tap()
    ph.wait(lambda: ph.js("state.tabs.length") == 2, 15, "the copied URL opened a tab")
    assert ph.js("window.__clipReads") == 1
    ph.wait(lambda: "E2E" in ph.mirror_text(), 15, "second page")
    ph.wait(lambda: ph.js("state.tabs.filter(t=>t.rb).length") == 2, 10, "both tabs have a NAS tab")
    _go(ph, "#/home")
    ph.wait(lambda: _visible(ph, "#view-home:not([hidden]) .tab-card") == 2, 8, "two tab cards")
    card = ph.js("""()=>{const c=document.querySelector('#homeTabGrid .tab-card');
      return {host:c.querySelector('.tab-card-host').textContent, mode:(c.querySelector('.tab-card-mode')||{}).textContent||'',
              title:c.querySelector('.tab-card-title').textContent}}""")
    assert card["host"] == "a.example.com" and card["mode"] and card["title"], card
    # tap = switch
    ph.page.locator("#homeTabGrid .tab-card .tab-card-main").first.tap()
    ph.wait(lambda: ph.js("state.route.view === 'tab'"), 5, "switched to the tab")
    assert ph.js("state.route.tabId") == ph.js("state.tabs[0].id")
    # x = close (the NAS tab goes with it)
    _go(ph, "#/home")
    ph.wait(lambda: _visible(ph, "#view-home:not([hidden]) .tab-card") == 2, 8, "cards again")
    gone = ph.js("state.tabs[1].rb && state.tabs[1].rb.id")
    ph.page.locator("#homeTabGrid .tab-card .tab-card-close").nth(1).tap()
    ph.wait(lambda: _visible(ph, "#view-home:not([hidden]) .tab-card") == 1, 5, "one card left")
    assert ph.js("state.tabs.length") == 1
    if gone:
        ph.wait(lambda: gone not in ph.env.tabs(), 8, "the NAS tab was closed")
    # recent pages: listed, tappable, stored on this device only, cleared by the button
    ph.wait(lambda: _visible(ph, "#homeRecentList .recent-row") >= 1, 10, "recent pages")
    stored = ph.js("JSON.parse(localStorage.getItem('qnapdl.recent.v1')||'[]')")
    assert stored and all(set(e) == {"u", "t", "at"} for e in stored) and len(stored) <= 20, stored
    assert any(e["u"].endswith("/article.html") for e in stored), stored
    ph.page.locator("#homeRecentClear").tap()
    ph.wait(lambda: ph.js("document.getElementById('homeRecent').hidden"), 3, "recent list hidden")
    assert ph.js("localStorage.getItem('qnapdl.recent.v1')") is None
    # v0.1.65: the list can be switched off (Settings > Browser): nothing shown, nothing recorded
    ph.post("/api/settings/options", {"home_recent": False})
    ph.js("()=>loadSettings()")
    ph.wait(lambda: ph.js("state.settings && state.settings.home_recent === false"), 5, "home_recent off")
    ph.js("()=>noteVisit('https://a.example.com/off.html', 'off')")
    assert ph.js("localStorage.getItem('qnapdl.recent.v1')") is None, "recorded while off"
    assert ph.js("document.getElementById('homeRecent').hidden"), "shown while off"
    ph.post("/api/settings/options", {"home_recent": True})
    ph.js("()=>loadSettings()")
    # a refused clipboard read focuses the address bar (no error, no tab)
    _fake_clipboard(ph, None)
    before = ph.js("state.tabs.length")
    ph.page.locator("#homePasteOpen").tap()
    ph.wait(lambda: ph.js("document.activeElement && document.activeElement.id") == "omnibox", 5, "address bar focused")
    assert ph.js("state.tabs.length") == before
    # the setup card stays while setup is incomplete (the harness has no download folder)
    assert ph.js("!document.getElementById('onboarding').hidden"), "setup card missing"


@scenario("links-paste")
def links_paste(ph):
    """v0.1.62: 「貼り付け」 reads the clipboard on the tap, shows the count and the send button at once, and every link shows
    送信中 while it is handed over and 失敗 when that fails."""
    ph.page.set_viewport_size(NARROW)
    ph.post("/api/links/clear", {"include_staged": True})        # the Hub's store is shared by the scenarios of a run
    _go(ph, "#/links")
    ph.wait(lambda: _visible(ph, "#pasteButton") == 1, 8, "links view")
    ph.js("()=>{state.live=Object.assign({},state.live,{engine:{can_send:true}})}")
    _fake_clipboard(ph, "see these:\n%s\n%s\nbye" % (RG1, RG2))
    ph.page.wait_for_timeout(300)
    assert ph.js("window.__clipReads") == 0
    ph.page.locator("#pasteButton").tap()
    ph.wait(lambda: "2件見つかりました" in ph.js("document.getElementById('pasteSummary').textContent"), 3, "count (no 400 ms wait)")
    assert ph.js("window.__clipReads") == 1
    assert _visible(ph, "#pasteSend") == 1 and ph.js("!document.getElementById('pasteSend').disabled"), "send button"
    assert "2件" in ph.js("document.getElementById('pasteSend').textContent")
    # a refused read leaves the box alone and focuses it
    _fake_clipboard(ph, None)
    ph.js("()=>{document.getElementById('pasteBox').value='';schedulePaste(0)}")
    ph.page.locator("#pasteButton").tap()
    ph.wait(lambda: ph.js("document.activeElement && document.activeElement.id") == "pasteBox", 5, "box focused")
    # per-link state: stage 3 links, send them; the second one fails
    for url in (RG1, RG2, RG3):
        assert ph.post("/api/links", {"url": url})["status"] == 201
    ph.js("()=>{swrDrop('links');loadQueue(true)}")
    ph.wait(lambda: ph.js("document.querySelectorAll('#queueItems .link-item').length") >= 3, 8, "three links")
    held = []

    def handler(route):
        held.append(route)
    ph.page.route("**/api/links/send", handler)
    ph.js("()=>{sendLinks(state.queueData.items.filter(l=>l.state==='staged')).catch(()=>{})}")
    ph.wait(lambda: held, 5, "the first send request")
    states = ph.js("[...document.querySelectorAll('#queueItems .link-item')].map(e=>e.querySelector('.state-chip').textContent)")
    assert states.count("送信中") == 3, "every link of the hand-off shows 送信中: %s" % states
    held.pop().fulfill(status=500, content_type="application/json", body='{"error": "送信できません（テスト）"}')
    ph.wait(lambda: ph.js("[...document.querySelectorAll('#queueItems .state-chip')].some(e=>e.textContent==='失敗')"), 8, "失敗 shown")
    states = ph.js("[...document.querySelectorAll('#queueItems .link-item')].map(e=>e.querySelector('.state-chip').textContent)")
    assert states.count("失敗") == 1 and states.count("送信中") == 0, states
    assert "再送信" in ph.js("document.querySelector('#queueItems .link-item .btn').textContent")
    ph.page.unroute("**/api/links/send")
    ph.take_errors("status of 500")
    ph.post("/api/links/clear", {"include_staged": True})


@scenario("downloads-summary")
def downloads_summary(ph):
    """v0.1.62: the summary (total speed, remaining time, counts) is computed from the list that is already there; a
    swipe to the left offers 一時停止/再開 and 消す (the JD2 harness is not running: the list is injected)."""
    ph.page.set_viewport_size(NARROW)
    _go(ph, "#/downloads")
    ph.wait(lambda: ph.js("!!document.querySelector('#view-downloads:not([hidden]) h1')"), 8, "downloads view")
    sums = ph.js("""()=>{
      const mk=(o)=>Object.assign({name:'x',status:'',bytes_loaded:0,bytes_total:1000,speed_bytes:null,eta_seconds:null,running:false,finished:false,progress:0},o);
      const snap={packages:[mk({running:true,speed_bytes:100,eta_seconds:5,bytes_loaded:500}),
        mk({running:true,speed_bytes:300,eta_seconds:60,bytes_loaded:900}),
        mk({finished:true,bytes_loaded:1000}),mk({}),mk({status:'Offline'}),mk({status:'ファイルが見つかりません'})],links:[],has_more:false};
      const a=summarizeDownloads(snap);
      const idle=summarizeDownloads({packages:[mk({})],links:[],has_more:false});
      const nodata=summarizeDownloads({packages:[mk({running:true,bytes_total:null})],links:[],has_more:true});
      return {a,idle,nodata}}""")
    a = sums["a"]
    # v0.1.63: a status of just "Offline" is a waiting link (the hoster's state), only real failure words are failures
    assert (a["running"], a["waiting"], a["finished"], a["failed"]) == (2, 2, 1, 1), a
    assert a["speed"] == 400, a
    # remaining: (1000-500) + (1000-900) + 1000 + 1000 (the two waiting ones) = 2600 bytes at 400 B/s -> 6.5 -> 7 s
    assert a["eta"] == 7, a
    assert sums["idle"]["eta"] is None and sums["idle"]["speed"] == 0, sums["idle"]
    assert sums["nodata"]["eta"] is None and sums["nodata"]["hasMore"] is True, sums["nodata"]
    # the numbers on the page (no request is made for them)
    ph.js("""()=>{state.engine=Object.assign({},state.engine,{phase:'running'});
      const mk=(n,o)=>Object.assign({name:n,status:'',bytes_loaded:1048576*100,bytes_total:1048576*500,speed_bytes:null,eta_seconds:null,running:false,
        finished:false,progress:20,id:'1'+n.length+Math.random().toString().slice(2,7),child_count:1,folder:{flat:true}},o);
      const p=[mk('Pack A',{running:true,speed_bytes:2097152,eta_seconds:200}),mk('Pack B',{}),mk('Pack C',{finished:true,progress:100,bytes_loaded:1048576*500}),mk('Pack D',{status:'エラー'})];
      const l=p.map(x=>Object.assign({},x,{package_id:x.id,id:x.id+'9',host:'rapidgator.net'}));
      window.__apiBefore=reqStats.all;
      state.snapshot={page:0,packages:p,links:l,has_more:false};state.snapshotJson=JSON.stringify(state.snapshot);
      state.downloadsSig='';renderDownloads();}""")
    txt = ph.js("""()=>({speed:$('dlSpeed').textContent,eta:$('dlEta').textContent,r:$('dlCountRunning').textContent,w:$('dlCountWaiting').textContent,
      f:$('dlCountFinished').textContent,x:$('dlCountFailed').textContent,shown:!$('downloadsSummary').hidden,calls:reqStats.all-window.__apiBefore})""")
    assert txt["shown"] and txt["speed"].startswith("合計 2") and "/秒" in txt["speed"] and txt["eta"].startswith("残り"), txt
    assert (txt["r"], txt["w"], txt["f"], txt["x"]) == ("1", "1", "1", "1"), txt
    assert txt["calls"] == 0, "the summary made requests: %s" % txt
    # failed filter
    ph.js("()=>{setFilter('failed')}")
    assert ph.js("document.querySelectorAll('#downloadList .download-card').length") == 1
    ph.js("()=>{setFilter('all')}")
    # swipe actions exist on the rows: one-file packages stay single rows
    rows = ph.js("""()=>[...document.querySelectorAll('#downloadList .swipe')].map(w=>({
      pause:(w.querySelector('.swipe-under-right .swipe-btn.stop,.swipe-under-right .swipe-btn.start')||{}).textContent,
      del:(w.querySelector('.swipe-under-right .swipe-btn.delete')||{}).textContent,
      single:!!w.querySelector('.pkg-head') && w.querySelector('.pkg-head').tagName==='DIV'}))""")
    assert len(rows) == 4 and rows[0]["pause"] == "一時停止" and rows[1]["pause"] == "再開" and rows[3]["pause"] == "再開", rows
    assert rows[2]["pause"] is None and [r["del"] for r in rows] == ["消す", "消す", "削除", "消す"], rows     # a finished row can only be removed
    assert ph.js("document.querySelectorAll('#downloadList .row-menu').length") == 4, "the ⋯ menu per item"
    if ph.engine == "chromium":
        box = ph.js("""()=>{const r=document.querySelector('#downloadList .swipe .swipe-body').getBoundingClientRect();return {x:r.left,y:r.top+r.height/2,w:r.width}}""")
        x0 = box["x"] + box["w"] * 0.7
        ph.cdp_touch([(x0, box["y"], 0), (x0 - 40, box["y"], 40), (x0 - 80, box["y"], 40), (x0 - 130, box["y"], 40)])
        ph.wait(lambda: ph.js("(()=>{const b=document.querySelector('#downloadList .swipe .swipe-body');"
                              "return b && /translateX\\(-/.test(b.style.transform)})()"), 4, "row swiped to the left")
        pos = ph.js("()=>{const w=document.querySelector('#downloadList .swipe');const b=w.querySelector('.swipe-body').getBoundingClientRect();"
                    "const p=w.querySelector('.swipe-under-right .swipe-btn.stop').getBoundingClientRect();"
                    "return {bodyLeft:b.left,btnLeft:p.left,btnRight:p.right,wrapRight:w.getBoundingClientRect().right}}")
        assert pos["btnRight"] <= pos["wrapRight"] + 1 and pos["btnLeft"] >= pos["bodyLeft"] - 1 and pos["btnRight"] > pos["bodyLeft"], pos
        assert ph.js("document.querySelectorAll('#downloadList .swipe-under-right .swipe-btn').length") == 7   # 2 per row, 1 for the finished one
    ph.js("()=>{state.engine=Object.assign({},state.engine,{phase:'stopped'});state.snapshot=null;state.downloadsSig='';renderDownloads()}")


@scenario("settings-jd2-layout")
def settings_jd2_layout(ph):
    """v0.1.62: Settings › JD2 = status card, accounts, save folder, send switches, ONE collapsed 詳細・ログ; every control
    of the old page is still there; an error is one line with a 詳しく toggle."""
    ph.page.set_viewport_size(NARROW)
    _go(ph, "#/settings/jd2")
    ph.wait(lambda: _visible(ph, "#view-settings:not([hidden]) #engineState") == 1, 8, "jd2 settings")
    order = ph.js("""()=>{const sec=document.querySelector('.settings-section[data-section=jd2]');
      const subs=[...sec.querySelectorAll(':scope > .sub, :scope > details')].map(e=>e.dataset.sub||e.id);
      const folds=[...sec.querySelectorAll('details.fold')].map(d=>({id:d.id,open:d.open}));
      return {subs,folds}}""")
    assert order["subs"] == ["engine", "accounts", "storage", "send", "engineAdvanced"], order
    assert order["folds"] == [{"id": "engineAdvanced", "open": False}], order
    ids = ("engineState engineSetup engineStart engineStop engineMode engineMigrate engineNoContainer engineProgressText engineProgress "
           "engineError engineErrorMore engineTestResult engineAutostartRow engineAutostart engineJd2 jd2Status jd2Check jd2Update jd2Preset "
           "engineStepsBox engineSteps engineDetails engineComponents engineMeta engineNativeRow engineNativeRemove engineSetupNative "
           "engineResetup engineKeysRow engineKeys engineKeysCopy engineKeysOut engineLogBox engineLog accountsState accountsReload "
           "accountsList accountEdit accountEditForm accountEditUser accountEditPass accountEditSave accountEditCancel accountsSecure "
           "accountForm accountHost accountUser accountPass accountAdd accountResult accountSchemaBox accountSchema accountSchemaCopy "
           "accountSchemaOut selectedFolder folderChange flattenNow flattenResult sendAutostart probeAutosend autoClearLinks "
           "autoClearDownloads autoClearMissing").split()
    missing = [i for i in ids if not ph.js("(i)=>!!document.getElementById(i)", i)]
    assert not missing, "controls missing: %s" % missing
    # the sections are in the agreed order on the page
    pos = ph.js("""()=>['engineState','accountForm','selectedFolder','sendAutostart','engineAdvanced'].map(id=>document.getElementById(id).getBoundingClientRect().top)""")
    assert pos == sorted(pos), pos
    # the log and the engine details live inside the one fold; the two old 「詳細」 folds are gone
    assert ph.js("['engineDetails','engineLogBox','accountSchemaBox','engineStepsBox'].every(id=>document.getElementById(id).closest('#engineAdvanced'))")
    # one error line + 詳しく
    ph.js("""()=>{renderEngine({phase:'failed',installed:true,mode:'container',container:{available:true},steps:[],components:[],
      error:'JD2を起動できませんでした。' + 'とても長い理由の説明。'.repeat(14)})}""")
    m = ph.js("()=>{const e=document.getElementById('engineError'),r=e.getBoundingClientRect();return {h:r.height,lh:parseFloat(getComputedStyle(e).lineHeight)||20,clamped:e.classList.contains('is-clamped'),vw:innerWidth}}")
    assert m["clamped"] and m["h"] <= m["lh"] + 20, "the error is not one line: %s" % m
    assert _visible(ph, "#engineErrorMore") == 1
    ph.page.locator("#engineErrorMore").tap()
    m2 = ph.js("()=>{const e=document.getElementById('engineError');return {h:e.getBoundingClientRect().height,clamped:e.classList.contains('is-clamped')}}")
    assert not m2["clamped"] and m2["h"] > m["h"] * 1.5, (m, m2)
    # exactly one primary action per state
    for phase, installed in (("stopped", True), ("running", True), ("not_installed", False)):
        ph.js("(a)=>{renderEngine({phase:a[0],installed:a[1],mode:'container',container:{available:true},steps:[],components:[],error:''})}", [phase, installed])
        n = _visible(ph, "#engineSetup,#engineStart,#engineStop")
        assert n == 1, "%s: %d primary buttons" % (phase, n)
    # opening 詳細・ログ shows the log (and nothing else is needed to find it)
    ph.page.locator("#engineAdvanced > summary").tap()
    ph.wait(lambda: ph.js("document.getElementById('engineAdvanced').open"), 3, "fold open")


@scenario("site-display-memory")
def site_display_memory(ph):
    """v0.1.62: the display chosen for a tab is remembered per site (registrable domain) and used the next time a page
    of that site is opened - decided on the phone, so it rides in the same tabs request (first = reader); the menu's
    「このサイトの表示を忘れる」 goes back to the default."""
    ph.page.set_viewport_size(NARROW)
    ph.js("()=>{localStorage.removeItem('qnapdl.hostmodes.v1')}")
    keys = ph.js("""()=>['www.example.com','a.b.example.com','example.co.jp','shop.example.co.jp','a.example.com','127.0.0.1','[::1]']
      .map(h=>siteKey(h))""")
    assert keys == ["example.com", "example.com", "example.co.jp", "example.co.jp", "example.com", "127.0.0.1", "[::1]"], keys
    posted = []
    ph.page.on("request", lambda r: posted.append(r.post_data or "") if r.url.endswith("/api/rb/tabs") else None)
    ph.open("/article.html")
    ph.wait(lambda: "E2E" in ph.mirror_text(), 15, "default (mirror)")
    assert ph.display() == "mirror"
    ph.js("()=>{configureMenu()}")
    assert ph.js("document.getElementById('menuForgetSite').hidden"), "nothing to forget yet"
    ph.js("()=>{setUserDisplay(activeWebTab(),'reader')}")
    ph.wait(lambda: ph.display() == "reader", 8, "reader chosen")
    ph.js("()=>{configureMenu()}")
    assert not ph.js("document.getElementById('menuForgetSite').hidden"), "forget item missing"
    stored = ph.js("JSON.parse(localStorage.getItem('qnapdl.hostmodes.v1'))")
    assert stored == [["example.com", "remote-reader"]], stored
    # another page of the same site (a sub-domain), in a new tab: reader straight away, asked for in the tabs request
    _go(ph, "#/home")
    posted.clear()
    omni = ph.page.locator("#omnibox").first
    omni.click()
    omni.fill("https://sub.a.example.com/article2.html")
    omni.press("Enter")
    ph.wait(lambda: ph.js("(()=>{const t=activeWebTab();return !!(t&&t.rb)})()"), 15, "second tab")
    assert ph.display() == "reader", ph.display()
    assert posted and '"first":"reader"' in posted[-1].replace(" ", ""), posted
    # a different site keeps the default
    _go(ph, "#/home")
    omni = ph.page.locator("#omnibox").first
    omni.click()
    omni.fill("https://b.example.net/article.html")
    omni.press("Enter")
    ph.wait(lambda: ph.js("(()=>{const t=activeWebTab();return !!(t&&t.rb&&t.current.includes('example.net'))})()"), 15, "third tab")
    assert ph.display() == "mirror", ph.display()
    # forget on the example.com tab -> default again
    ph.js("()=>{navigate('#/tab/'+state.tabs[1].id)}")
    ph.wait(lambda: ph.js("(()=>{const t=activeWebTab();return !!(t&&t.rb&&t.current.includes('example.com'))})()"), 8, "back on tab 2")
    ph.js("()=>{configureMenu()}")
    ph.js("()=>{menuAction('forgetSite')}")
    ph.wait(lambda: ph.js("localStorage.getItem('qnapdl.hostmodes.v1')") == "[]", 3, "entry removed")
    ph.wait(lambda: ph.display() == "mirror", 8, "default display again")
    ph.js("()=>{configureMenu()}")
    assert ph.js("document.getElementById('menuForgetSite').hidden")
    # an old exact-host entry keeps working
    ph.js("()=>{localStorage.setItem('qnapdl.hostmodes.v1', JSON.stringify([['a.example.com','remote-fast']]))}")
    assert ph.js("rememberedMode('a.example.com')") == "remote-fast"
    assert ph.js("rememberedMode('www.a.example.com')") == "remote-fast"
    assert ph.js("rememberedMode('b.example.net')") is None
    ph.js("()=>{localStorage.removeItem('qnapdl.hostmodes.v1')}")


@scenario("site-display-pt")
def site_display_pt(ph):
    """v0.1.62: passthrough is remembered for a site too: the next page of that site opens in the frame (no NAS tab)
    while it is available, falls back to the default display when it is not, and 「忘れる」 returns to the NAS browser."""
    ph.setting("pt_enabled", False)
    _pt_local(ph)
    ph.js("()=>{localStorage.removeItem('qnapdl.hostmodes.v1')}")
    ph.open("/article.html")
    ph.js("()=>{setUserDisplay(activeWebTab(),'pt')}")
    ph.wait(lambda: ph.js("(()=>{const t=activeWebTab();return !!(t&&t.pt&&!t.rb)})()"), 8, "passthrough chosen")
    assert ph.js("JSON.parse(localStorage.getItem('qnapdl.hostmodes.v1'))") == [["example.com", "remote-pt"]]
    _go(ph, "#/home")
    omni = ph.page.locator("#omnibox").first
    omni.click()
    omni.fill("https://a.example.com/ptx/page.html")
    omni.press("Enter")
    ph.wait(lambda: ph.js("(()=>{const t=activeWebTab();return !!(t&&t.pt&&!t.rb&&t.history.length)})()"), 8, "a new passthrough tab")
    fr = _pt_frame(ph)
    ph.wait(lambda: fr.locator("#t").count() > 0, 15, "page drawn in the frame")
    assert ph.env.tabs() == {} or not [t for t in ph.env.tabs().values() if "ptx" in t.url], "a NAS tab was opened for it"
    # passthrough not available (setting off): the default display is used
    ph.setting("pt_enabled", False)
    ph.js("refreshPt()")
    ph.wait(lambda: ph.js("!ptAvailable()"), 8, "passthrough off")
    ph.js("()=>{rememberedPtUrl('https://a.example.com/x')}")
    assert ph.js("rememberedPtUrl('https://a.example.com/x')") == ""
    ph.setting("pt_enabled", True)
    ph.js("refreshPt()")
    ph.wait(lambda: ph.js("ptAvailable()"), 8, "passthrough on")
    assert ph.js("rememberedPtUrl('https://sub.a.example.com/x')") == "https://sub.a.example.com/x"
    assert ph.js("rememberedPtUrl('https://b.example.net/x')") == ""
    # forget -> the NAS browser again
    ph.js("()=>{forgetSiteDisplay(activeWebTab())}")
    assert ph.js("rememberedPtUrl('https://a.example.com/x')") == ""
    ph.js("()=>{localStorage.removeItem('qnapdl.hostmodes.v1')}")


@scenario("toast-above-bar")
def toast_above_bar(ph):
    """v0.1.62: toasts sit above the bottom tab bar and never cover it (430x873), however many there are, also on a web tab;
    the thin progress line shows only for loads longer than 300 ms."""
    ph.page.set_viewport_size(NARROW)
    for view in ("#/home", "#/downloads", "#/links"):
        _go(ph, view)
        # one ordinary toast at a time + sticky ones (e.g. 「Hubが更新されています」): a long error and a sticky one together
        ph.js("""()=>{toast('エラーの通知です。'.repeat(8),'error');stickyToast('t62','更新があります。再読み込みしてください','info',[{label:'再読み込み',run:()=>{}}])}""")
        ph.wait(lambda: ph.js("document.querySelectorAll('#notice .toast').length") >= 2, 3, "toasts")
        m = ph.js("""()=>{const bar=document.getElementById('mobileBar').getBoundingClientRect();
          const ts=[...document.querySelectorAll('#notice .toast')].map(t=>t.getBoundingClientRect());
          return {barTop:bar.top,barH:bar.height,maxBottom:Math.max(...ts.map(r=>r.bottom)),minTop:Math.min(...ts.map(r=>r.top)),vh:innerHeight}}""")
        assert m["barH"] > 30 and m["maxBottom"] <= m["barTop"] - 4 and m["minTop"] >= 0, "%s: %s" % (view, m)
        ph.js("()=>{clearToast();document.querySelectorAll('#notice .toast').forEach(t=>t.remove())}")
    # on a web tab as well
    ph.open("/article.html")
    ph.wait(lambda: "E2E" in ph.mirror_text(), 15, "page")
    ph.js("()=>{flash('Webタブの通知')}")
    m = ph.js("""()=>{const bar=document.getElementById('mobileBar').getBoundingClientRect();const t=document.querySelector('#notice .toast').getBoundingClientRect();
      return {barTop:bar.top,bottom:t.bottom}}""")
    assert m["bottom"] <= m["barTop"] - 4, m
    # the progress line: not for a fast load, shown for a slow one
    _go(ph, "#/home")
    ph.page.wait_for_timeout(300)
    ph.js("()=>{window.__seen=false;new MutationObserver(()=>{if(!document.getElementById('appLoading').hidden)window.__seen=true})"
          ".observe(document.getElementById('appLoading'),{attributes:true})}")
    ph.js("()=>{swrDrop('bookmarks');loadBookmarks(true)}")
    ph.page.wait_for_timeout(500)
    assert not ph.js("window.__seen"), "the line showed for a fast load"
    held = []
    ph.page.route("**/api/bookmarks", lambda route: held.append(route))
    ph.js("()=>{swrDrop('bookmarks');loadBookmarks(true)}")
    ph.wait(lambda: held, 3, "slow request held")
    assert ph.js("document.getElementById('appLoading').hidden"), "shown too early"
    ph.wait(lambda: not ph.js("document.getElementById('appLoading').hidden"), 2, "the line appears after 300 ms")
    held.pop().continue_()
    ph.wait(lambda: ph.js("document.getElementById('appLoading').hidden"), 5, "the line is gone when the load ends")
    ph.page.unroute("**/api/bookmarks")


# ------------------------------------------------------------------ v0.1.63: iPad and PC
# Two more kinds of device: "ipad" (820x1180, touch, hardware keyboard possible) and "desktop" (1280x800, mouse + keyboard,
# no touch). An iPad scenario is skipped on an engine whose touch emulation does not report (pointer: coarse).

def _app(ph, hash_):
    """An in-app navigation (the way a tap on a #-link does it); a bare location.hash from a web tab would be a page back."""
    ph.js("(h)=>navigate(h)", hash_)


def _no_hscroll(ph, label):
    m = ph.js("()=>({sw:document.documentElement.scrollWidth,cw:document.documentElement.clientWidth,bw:document.body.scrollWidth})")
    assert m["sw"] <= m["cw"] + 1 and m["bw"] <= m["cw"] + 1, "%s: the page scrolls sideways %s" % (label, m)


def _rect(ph, selector):
    return ph.js("""(s)=>{const e=document.querySelector(s);if(!e)return null;const r=e.getBoundingClientRect();
      return {l:r.left,t:r.top,r:r.right,b:r.bottom,w:r.width,h:r.height}}""", selector)


def _fake_downloads(ph, extra=None):
    """The JD2 harness is not running: inject a list the way a running engine would deliver it."""
    ph.js("""(extra)=>{state.engine=Object.assign({},state.engine,{phase:'running'});
      const mk=(n,o)=>Object.assign({name:n,status:'',bytes_loaded:1048576*100,bytes_total:1048576*500,speed_bytes:null,eta_seconds:null,
        running:false,finished:false,progress:20,id:'1'+n.length+Math.random().toString().slice(2,7),child_count:1,folder:{flat:true}},o);
      const p=[mk('Pack A',{running:true,speed_bytes:2097152,eta_seconds:200}),mk('Pack B',{}),mk('Pack C',{finished:true,progress:100,bytes_loaded:1048576*500})]
        .concat((extra||[]).map(e=>mk(e.name,e)));
      const l=p.map(x=>Object.assign({},x,{package_id:x.id,id:x.id+'9',host:'rapidgator.net'}));
      state.snapshot={page:0,packages:p,links:l,has_more:false};state.snapshotJson=JSON.stringify(state.snapshot);
      setShown('downloadsEmpty',false);setShown('downloadsBody',true);setShown('downloadsTools',true);
      state.downloadsSig='';renderDownloads();}""", extra or [])


def _scroll_steps(ph, ys):
    for y in ys:
        ph.js("(y)=>{const d=mirrorDoc();mirrorScroller(d).scrollTop=y}", y)
        ph.page.wait_for_timeout(60)


def _collapse_check(ph, label):
    """Scroll the mirror down: the bars (tab strip + toolbar when wide) slide away, the NAS viewport does not change; scroll
    up: they return."""
    collapsed = lambda: ph.js("document.body.classList.contains('chrome-collapsed')")
    view0 = ph.js("stageViewport()")
    _scroll_steps(ph, (0, 60, 140, 260, 380, 520))
    ph.wait(collapsed, 3, "%s: the bars collapse when the page is scrolled down" % label)
    ph.page.wait_for_timeout(450)
    assert ph.js("document.querySelector('.toolbar').getBoundingClientRect().bottom") <= 1, "%s: the toolbar is still on screen" % label
    if ph.js("getComputedStyle(document.getElementById('tabstrip')).display") != "none":
        assert ph.js("document.getElementById('tabstrip').getBoundingClientRect().bottom") <= 1, "%s: the tab strip is still on screen" % label
    assert ph.js("document.getElementById('remoteStage').getBoundingClientRect().top") <= 1, "%s: the page does not start at the top" % label
    assert ph.js("stageViewport()") == view0, "%s: the NAS viewport changed with the collapse" % label
    _scroll_steps(ph, (500, 440, 380, 330))
    ph.wait(lambda: not collapsed(), 3, "%s: the bars return when scrolling up" % label)
    ph.page.wait_for_timeout(450)
    assert ph.js("document.querySelector('.toolbar').getBoundingClientRect().top") >= 0 and ph.js("stageViewport()") == view0
    _scroll_steps(ph, (0,))


@scenario("ipad-layout", device="ipad")
def ipad_layout(ph):
    """v0.1.63 iPad (touch): portrait 820x1180 and landscape 1180x820 keep the wide layout with finger-sized targets (>= 44 px),
    one top row, no sideways scrolling, and the bars collapse on scroll (a mouse would keep them); Split View widths
    (500-700) use the phone layout."""
    ph.require_touch()
    tab = ph.open("/long1.html")
    ph.wait(lambda: "長いページ 1" in ph.mirror_text(), 15, "long page in mirror")
    ph.page.wait_for_timeout(500)
    web_hash = ph.js("location.hash")
    for width, height in ((820, 1180), (1180, 820)):
        ph.page.set_viewport_size({"width": width, "height": height})
        ph.page.wait_for_timeout(350)
        label = "%dx%d" % (width, height)
        # top bar: one row, every button of the wide bar present, nothing under 44 px
        bar = _rect(ph, ".toolbar")
        assert bar["h"] <= 66, "%s: the toolbar is %.0f px high" % (label, bar["h"])
        ids = ("navBack", "navForward", "navReload", "omniForm", "starBtn", "dlButton", "menuButton", "newTab")
        tops = []
        for ident in ids:
            r = _rect(ph, "#" + ident)
            assert r and r["w"] > 0, "%s: #%s is not shown" % (label, ident)
            assert r["w"] >= 43.5 and r["h"] >= 43.5, "%s: #%s is %.0fx%.0f px (a finger needs 44)" % (label, ident, r["w"], r["h"])
            if ident != "newTab":
                tops.append(r["t"] + r["h"] / 2)
        assert max(tops) - min(tops) <= 6, "%s: the bar buttons are not on one row: %s" % (label, tops)
        for selector in (".pinned-tab", ".web-tab .tab-link", ".web-tab .tab-close", ".brand"):
            r = _rect(ph, selector)
            assert r and r["h"] >= 43.5 and r["w"] >= 43.5, "%s: %s is %s" % (label, selector, r)
        assert ph.js("document.querySelector('.web-tab .tab-label').getBoundingClientRect().width") > 20, "%s: the web tab lost its name" % label
        assert bar["t"] >= ph.js("document.getElementById('tabstrip').getBoundingClientRect().bottom") - 1, "%s: the tab strip is not above the bar" % label
        # the menu: touch-sized rows
        ph.js("openMenu()")
        small = ph.js("""()=>[...document.querySelectorAll('#menu button')].filter(b=>b.offsetParent!==null).map(b=>b.getBoundingClientRect().height).filter(h=>h<43.5)""")
        assert not small, "%s: menu rows under 44 px: %s" % (label, small)
        ph.js("closeMenu(false)")
        _no_hscroll(ph, label + " web tab")
        # the collapse follows the finger, not the width
        _collapse_check(ph, label)
    # app screens: no sideways scroll, targets, settings as two columns
    ph.page.set_viewport_size({"width": 1180, "height": 820})
    _fake_downloads(ph)
    for route in ("#/home", "#/downloads", "#/links", "#/settings/jd2", "#/settings/browser", "#/settings/system"):
        _app(ph, route)
        ph.page.wait_for_timeout(300)
        _no_hscroll(ph, route)
        if route == "#/downloads":
            small = ph.js("()=>[...document.querySelectorAll('#downloadsTools .chip,#downloadsTools .search')].map(e=>e.getBoundingClientRect().height).filter(h=>h<43.5)")
            assert not small, "downloads filters under 44 px: %s" % small
        if route.startswith("#/settings"):
            nav, body = _rect(ph, ".settings-nav"), _rect(ph, ".settings-body")
            assert nav["r"] <= body["l"] + 1 and abs(nav["t"] - body["t"]) <= 40, "settings is not two columns: %s %s" % (nav, body)
            assert ph.js("[...document.querySelectorAll('.settings-nav a')].every(a=>a.getBoundingClientRect().height>=43.5)"), "settings nav under 44 px"
    # Split View / Slide Over widths: the phone layout (bottom tab bar, no tab strip), still one bar and the collapse
    for width in (500, 600, 700):
        ph.page.set_viewport_size({"width": width, "height": 900})
        _app(ph, web_hash)
        ph.page.wait_for_timeout(350)
        assert ph.js("getComputedStyle(document.getElementById('tabstrip')).display") == "none", "%d: the tab strip is shown" % width
        assert _visible(ph, ".mobile-bar") == 1, "%d: no bottom bar" % width
        assert _rect(ph, ".toolbar")["h"] <= 66, "%d: toolbar too high" % width
        _no_hscroll(ph, "split %d" % width)
        r = _rect(ph, ".mobile-bar")
        assert r["h"] >= 44 and abs(r["b"] - 900) <= 1, "%d: bottom bar %s" % (width, r)
    _collapse_check(ph, "split 600")
    ph.take_errors("status of 400", "status of 503")     # the injected 'running' engine / a JD2 that is off


@scenario("ipad-keys", device="ipad")
def ipad_keys(ph):
    """v0.1.63: an iPad with a hardware keyboard has the same shortcuts (Alt+arrows, Cmd+[ / ], Ctrl/Cmd+L, Esc)."""
    ph.require_touch()
    _keys_back_forward(ph)


def _keys_back_forward(ph):
    tab = ph.open("/article.html")
    ph.wait(lambda: "E2E テスト記事" in ph.mirror_text(), 15, "mirror content")
    ph.post("/api/rb/navigate", {"tab": tab, "url": BASE + "/article2.html"})
    ph.wait(lambda: ph.env.nas_url(tab).endswith("/article2.html"), 10, "navigated to article2")
    ph.wait(lambda: "E2E 二つ目の記事" in ph.mirror_text(), 10, "article2 in mirror")
    ph.js("document.activeElement && document.activeElement.blur()")
    tabs_before, hash_before = ph.js("state.tabs.length"), ph.js("location.hash")
    ph.press("Alt+ArrowLeft")
    ph.wait(lambda: ph.env.nas_url(tab).endswith("/article.html"), 8, "Alt+← went back (url=%s)" % ph.env.nas_url(tab))
    ph.wait(lambda: "E2E テスト記事" in ph.mirror_text(), 10, "article in mirror again")
    ph.press("Alt+ArrowRight")
    ph.wait(lambda: ph.env.nas_url(tab).endswith("/article2.html"), 8, "Alt+→ went forward")
    ph.wait(lambda: "E2E 二つ目の記事" in ph.mirror_text(), 10, "article2 in mirror again")
    ph.press("Meta+BracketLeft")
    ph.wait(lambda: ph.env.nas_url(tab).endswith("/article.html"), 8, "Cmd+[ went back")
    ph.wait(lambda: "E2E テスト記事" in ph.mirror_text(), 10, "article in mirror (3)")
    ph.press("Meta+BracketRight")
    ph.wait(lambda: ph.env.nas_url(tab).endswith("/article2.html"), 8, "Cmd+] went forward")
    ph.wait(lambda: "E2E 二つ目の記事" in ph.mirror_text(), 10, "article2 in mirror (4)")
    # the same with the keyboard focus INSIDE the mirror frame (its key events do not reach the app document)
    ph.js("mirrorDoc().defaultView.focus()")
    ph.press("Alt+ArrowLeft")
    ph.wait(lambda: ph.env.nas_url(tab).endswith("/article.html"), 8, "Alt+← with the focus in the mirror went back")
    ph.wait(lambda: "E2E テスト記事" in ph.mirror_text(), 10, "article in mirror (5)")
    # at the first page: only a toast, the app stays on the tab
    ph.press("Alt+ArrowLeft")
    ph.wait(lambda: ph.js("[...document.querySelectorAll('#notice .toast')].some(t=>t.textContent.includes('前のページはありません'))"), 5, "no earlier page: a toast")
    assert ph.js("location.hash") == hash_before and ph.js("state.tabs.length") == tabs_before, "the app left the tab"
    assert ph.env.nas_url(tab).endswith("/article.html")
    # the address bar: Ctrl+L / Cmd+L / "/" ; Esc gives it back
    for combo in ("Control+l", "Meta+l"):
        ph.js("document.activeElement && document.activeElement.blur()")
        ph.press(combo)
        assert ph.js("document.activeElement && document.activeElement.id") == "omnibox", "%s did not focus the address bar" % combo
        ph.press("Escape")
        assert ph.js("document.activeElement && document.activeElement.id") != "omnibox"
    ph.js("document.getElementById('app').focus && document.body.focus()")
    ph.press("Slash")
    assert ph.js("document.activeElement && document.activeElement.id") == "omnibox", "/ did not focus the address bar"
    ph.press("Escape")
    # Esc closes the menu
    ph.js("openMenu()")
    assert not ph.js("document.getElementById('menu').hidden")
    ph.press("Escape")
    assert ph.js("document.getElementById('menu').hidden"), "Esc did not close the menu"
    # typing in the address bar keeps its own Alt+arrows
    ph.js("focusOmnibox()")
    ph.press("Alt+ArrowRight")
    ph.page.wait_for_timeout(500)
    assert ph.env.nas_url(tab).endswith("/article.html"), "a shortcut fired while typing in the address bar"


@scenario("desktop-keys", device="desktop")
def desktop_keys(ph):
    """v0.1.63 PC: Alt+←/→, Cmd+[ / ], Ctrl/Cmd+L, / and Esc; Ctrl+R reloads the NAS page and not the app."""
    ph.require_mouse()
    _keys_back_forward(ph)
    tab = ph.tab_id()
    # Ctrl+R inside a web tab reloads the NAS page, never the app page
    ph.js("window.__noReload = 'still here'")
    ph.js("document.activeElement && document.activeElement.blur()")
    hits = ph.env.fx.count("/article.html")
    ph.press("Control+r")
    ph.wait(lambda: ph.env.fx.count("/article.html") > hits, 10, "Ctrl+R reloaded the NAS page")
    ph.page.wait_for_timeout(300)
    assert ph.js("window.__noReload") == "still here", "Ctrl+R reloaded the app"
    hits = ph.env.fx.count("/article.html")
    ph.press("F5")
    ph.wait(lambda: ph.env.fx.count("/article.html") > hits, 10, "F5 reloaded the NAS page")
    assert ph.js("window.__noReload") == "still here", "F5 reloaded the app"
    assert ph.tab_id() == tab


def _mouse_nav(ph, button, in_mirror=False):
    """Mouse button 3 (back) / 4 (forward) as the browser delivers it to a page: mousedown, mouseup, auxclick. (Chromium's CDP
    'back' button goes straight to the browser's history without any DOM event, so a real event cannot be made here.)
    `in_mirror` sends them inside the mirror frame's document. Returns the list of [type, cancelled]."""
    return ph.js("""(a)=>{const d=a.mirror?mirrorDoc():document;const out=[];
      for(const t of ['mousedown','mouseup','auxclick']){const e=new MouseEvent(t,{button:a.b,bubbles:true,cancelable:true,view:d.defaultView});
        d.body.dispatchEvent(e);out.push([t,e.defaultPrevented])}
      return out}""", {"b": button, "mirror": in_mirror})


@scenario("desktop-mouse-nav", device="desktop")
def desktop_mouse_nav(ph):
    """v0.1.63 PC: mouse buttons 4 / 5 = back / forward of the NAS page, the app stays on the tab; the trackpad's sideways
    swipe (wheel deltaX) does the same, but not over a horizontally scrolling element and not on a page that scrolls
    sideways itself."""
    ph.require_mouse()
    tab = ph.open("/article.html")
    ph.wait(lambda: "E2E テスト記事" in ph.mirror_text(), 15, "mirror content")
    ph.post("/api/rb/navigate", {"tab": tab, "url": BASE + "/article2.html"})
    ph.wait(lambda: ph.env.nas_url(tab).endswith("/article2.html"), 10, "navigated to article2")
    ph.wait(lambda: "E2E 二つ目の記事" in ph.mirror_text(), 10, "article2 in mirror")
    hash_before, tabs_before = ph.js("location.hash"), ph.js("state.tabs.length")
    # the events are cancelled (the browser must not also go back) and move the NAS page
    seen = _mouse_nav(ph, 3)
    assert all(prevented for _, prevented in seen), "mouse button 4 was not cancelled: %s" % seen
    ph.wait(lambda: ph.env.nas_url(tab).endswith("/article.html"), 8, "mouse button 4 went back (url=%s)" % ph.env.nas_url(tab))
    ph.wait(lambda: "E2E テスト記事" in ph.mirror_text(), 10, "article in mirror")
    seen = _mouse_nav(ph, 4)
    assert all(prevented for _, prevented in seen), "mouse button 5 was not cancelled: %s" % seen
    ph.wait(lambda: ph.env.nas_url(tab).endswith("/article2.html"), 8, "mouse button 5 went forward")
    ph.wait(lambda: "E2E 二つ目の記事" in ph.mirror_text(), 10, "article2 in mirror (2)")
    # the same inside the mirror frame's document
    _mouse_nav(ph, 3, in_mirror=True)
    ph.wait(lambda: ph.env.nas_url(tab).endswith("/article.html"), 8, "mouse button 4 inside the mirror went back")
    ph.wait(lambda: "E2E テスト記事" in ph.mirror_text(), 10, "article in mirror (2)")
    _mouse_nav(ph, 4, in_mirror=True)
    ph.wait(lambda: ph.env.nas_url(tab).endswith("/article2.html"), 8, "mouse button 5 inside the mirror went forward")
    ph.wait(lambda: "E2E 二つ目の記事" in ph.mirror_text(), 10, "article2 in mirror (3)")
    assert ph.js("location.hash") == hash_before and ph.js("state.tabs.length") == tabs_before, "the app left the tab"
    # trackpad: a sideways wheel swipe over the mirror = back (fingers to the right = deltaX < 0), forward the other way
    pt = ph.mirror_point("doc=>doc.querySelector('h1')")
    ph.page.mouse.move(pt["x"] + 200, pt["y"] + 100)
    for _ in range(4):
        ph.page.mouse.wheel(-70, 0)
        ph.page.wait_for_timeout(25)
    ph.wait(lambda: ph.env.nas_url(tab).endswith("/article.html"), 8, "a sideways swipe went back (url=%s)" % ph.env.nas_url(tab))
    ph.wait(lambda: "E2E テスト記事" in ph.mirror_text(), 10, "article in mirror (3)")
    ph.page.wait_for_timeout(900)                   # the swipe's inertia is over
    for _ in range(4):
        ph.page.mouse.wheel(70, 0)
        ph.page.wait_for_timeout(25)
    ph.wait(lambda: ph.env.nas_url(tab).endswith("/article2.html"), 8, "a sideways swipe went forward")
    ph.wait(lambda: "E2E 二つ目の記事" in ph.mirror_text(), 10, "article2 in mirror (4)")
    ph.page.wait_for_timeout(900)
    # a vertical wheel with a little sideways drift is a scroll, not a swipe
    for _ in range(6):
        ph.page.mouse.wheel(40, 60)
        ph.page.wait_for_timeout(25)
    ph.page.wait_for_timeout(500)
    assert ph.env.nas_url(tab).endswith("/article2.html"), "a diagonal scroll moved the page"
    # over a horizontally scrolling element: it scrolls, the NAS page does not move
    ph.post("/api/rb/navigate", {"tab": tab, "url": BASE + "/scroller.html"})
    ph.wait(lambda: ph.env.nas_url(tab).endswith("/scroller.html"), 10, "navigated to scroller")
    ph.wait(lambda: "E2E 横スクロールの表" in ph.mirror_text(), 10, "scroller in mirror")
    ph.page.wait_for_timeout(900)
    pt = ph.wait(lambda: ph.mirror_point("doc=>doc.querySelector('#wide')"), 5, "the scrolling table")
    ph.page.mouse.move(pt["x"], pt["y"])
    for _ in range(5):
        ph.page.mouse.wheel(-90, 0)
        ph.page.wait_for_timeout(25)
    ph.page.wait_for_timeout(700)
    assert ph.env.nas_url(tab).endswith("/scroller.html"), "a swipe over the scrolling table moved the NAS page: %s; wheel log: %s" % (ph.env.nas_url(tab), ph.js("JSON.stringify(wheelLog.slice(-8))"))
    # a page that is itself wider than the window: the sideways wheel scrolls it
    ph.post("/api/rb/navigate", {"tab": tab, "url": BASE + "/hwide.html"})
    ph.wait(lambda: ph.env.nas_url(tab).endswith("/hwide.html"), 10, "navigated to hwide")
    ph.wait(lambda: "E2E 横に長いページ" in ph.mirror_text(), 10, "hwide in mirror")
    ph.page.wait_for_timeout(900)
    assert ph.js("(()=>{const e=mirrorScroller(mirrorDoc());return e.scrollWidth>e.clientWidth+50})()"), "the fixture does not scroll sideways in the mirror"
    pt = ph.mirror_point("doc=>doc.querySelector('h1')")
    ph.page.mouse.move(pt["x"] + 100, pt["y"] + 100)
    for _ in range(5):
        ph.page.mouse.wheel(-90, 0)
        ph.page.wait_for_timeout(25)
    ph.page.wait_for_timeout(700)
    assert ph.env.nas_url(tab).endswith("/hwide.html"), "a sideways swipe on a page that scrolls sideways moved the NAS page; wheel log: %s" % ph.js("JSON.stringify(wheelLog.slice(-8))")
    assert ph.js("location.hash") == hash_before


@scenario("desktop-link-menu", device="desktop")
def desktop_link_menu(ph):
    """v0.1.63 PC: a right click on a link in the mirror opens the app's own menu (新しいタブで開く / リンクに追加 / URLをコピー),
    not the browser's; Esc closes it; an ordinary right click on text opens nothing."""
    ph.require_mouse()
    tab = ph.open("/article.html")
    ph.wait(lambda: "E2E テスト記事" in ph.mirror_text(), 15, "mirror content")
    ph.page.wait_for_timeout(800)
    # not on a link: no menu
    pt = ph.mirror_point("doc=>doc.querySelector('h1')")
    ph.click(pt["x"], pt["y"], "right")
    ph.page.wait_for_timeout(200)
    assert ph.js("document.getElementById('linkMenu').hidden"), "a menu opened for a right click on text"
    link = ph.wait(lambda: ph.mirror_point("doc=>doc.querySelector('a[data-href$=\"/article2.html\"]')"), 8, "a link in the mirror")
    ph.click(link["x"], link["y"], "right")
    ph.wait(lambda: not ph.js("document.getElementById('linkMenu').hidden"), 3, "the link menu")
    labels = ph.js("[...document.querySelectorAll('#linkMenu button')].map(b=>b.textContent.trim())")
    assert labels == ["新しいタブで開く", "リンクに追加", "URLをコピー"], labels
    box = _rect(ph, "#linkMenu")
    assert box["l"] >= 0 and box["r"] <= ph.js("innerWidth") and box["t"] >= 0 and box["b"] <= ph.js("innerHeight"), box
    assert ph.env.nas_url(tab).endswith("/article.html"), "a right click followed the link"
    ph.press("Escape")
    assert ph.js("document.getElementById('linkMenu').hidden"), "Esc did not close the link menu"
    # URLをコピー: the app's copy path (clipboard or the hidden-textarea fallback) reports success and the menu closes
    ph.click(link["x"], link["y"], "right")
    ph.wait(lambda: not ph.js("document.getElementById('linkMenu').hidden"), 3, "the link menu (2)")
    ph.page.click("#linkMenu [data-link-act=copy]")
    ph.wait(lambda: ph.js("document.getElementById('linkMenu').hidden"), 3, "the menu closed after copy")
    ph.wait(lambda: ph.js("[...document.querySelectorAll('#notice .toast')].some(t=>t.textContent.includes('コピー'))"), 5, "a copy toast")
    # リンクに追加: the same staging as the ⋮ menu
    ph.click(link["x"], link["y"], "right")
    ph.wait(lambda: not ph.js("document.getElementById('linkMenu').hidden"), 3, "the link menu (3)")
    ph.page.click("#linkMenu [data-link-act=add]")
    ph.wait(lambda: ph.js("[...document.querySelectorAll('#notice .toast')].some(t=>t.textContent.includes('追加したリンクに入れました'))"), 5, "added toast")
    staged = ph.js("fetch('/api/links').then(r=>r.json()).then(j=>JSON.stringify(j))")
    assert "/article2.html" in staged, staged
    # 新しいタブで開く: a second web tab with that page, the first one stays
    ph.click(link["x"], link["y"], "right")
    ph.wait(lambda: not ph.js("document.getElementById('linkMenu').hidden"), 3, "the link menu (4)")
    ph.page.click("#linkMenu [data-link-act=open]")
    ph.wait(lambda: ph.js("state.tabs.length") == 2, 8, "a new web tab")
    ph.wait(lambda: any(t.url.endswith("/article2.html") for t in ph.env.tabs().values()), 15, "the new tab loaded the link")
    assert ph.env.tabs()[tab].url.endswith("/article.html"), "the first tab was navigated"
    ph.post("/api/links/clear", {"include_staged": True})


@scenario("desktop-layout", device="desktop")
def desktop_layout(ph):
    """v0.1.63 PC (mouse): one dense top row, tooltips naming the keys, NO collapse on scroll, two-column settings /
    downloads / links on wide windows, the mirror centred at <= 1600 px without sideways scrolling."""
    ph.require_mouse()
    ph.open("/long1.html")
    ph.wait(lambda: "長いページ 1" in ph.mirror_text(), 15, "long page in mirror")
    ph.page.wait_for_timeout(500)
    for width, height in ((1280, 800), (1920, 1080)):
        ph.page.set_viewport_size({"width": width, "height": height})
        ph.page.wait_for_timeout(350)
        label = "%dx%d" % (width, height)
        assert _rect(ph, ".toolbar")["h"] <= 56, "%s: the bar is %.0f px high" % (label, _rect(ph, ".toolbar")["h"])
        assert _visible(ph, "#navBack, #navForward, #navReload, #starBtn, #dlButton, #menuButton") == 6, "%s: a bar button is missing" % label
        # tooltips: every icon button says what it does
        missing = ph.js("""()=>[...document.querySelectorAll('.icon-btn, .tab-close, .mode-chip')].filter(b=>b.offsetParent!==null&&!(b.title||'').trim()).map(b=>b.id||b.className)""")
        assert not missing, "%s: icon buttons without a tooltip: %s" % (label, missing)
        titles = ph.js("({b:navBack.title,f:navForward.title,r:navReload.title,o:omnibox.title})")
        assert "Alt" in titles["b"] or "⌘" in titles["b"], titles
        assert "L" in titles["o"], titles
        # the page: centred, at most as wide as the NAS laid it out, no sideways scroll, wheel scrolls it
        fr = _rect(ph, "#mirrorFrame")
        assert fr["w"] <= 1600 + 1 and abs(fr["l"] - (width - fr["r"])) <= 2, "%s: the mirror is not centred / too wide: %s" % (label, fr)
        assert ph.js("stageViewport().width") == min(width, 1600), "%s: viewport %s" % (label, ph.js("stageViewport()"))
        assert ph.js("(()=>{const e=mirrorScroller(mirrorDoc());return e.scrollWidth<=e.clientWidth+1})()"), "%s: the mirror scrolls sideways" % label
        _no_hscroll(ph, label + " mirror")
        ph.page.mouse.move(fr["l"] + 300, fr["t"] + 300)
        ph.page.mouse.wheel(0, 500)
        ph.wait(lambda: ph.mirror_scroll_top() > 100, 3, "%s: the wheel scrolls the mirror" % label)
        # a mouse keeps its bars
        _scroll_steps(ph, (100, 300, 500, 700, 900))
        ph.page.wait_for_timeout(400)
        assert not ph.js("document.body.classList.contains('chrome-collapsed')"), "%s: a mouse layout collapsed its bars" % label
        _scroll_steps(ph, (0,))
        if width > 1600:                              # the empty sides still scroll the page
            before = ph.mirror_scroll_top()
            ph.page.mouse.move(40, 400)
            ph.page.mouse.wheel(0, 400)
            ph.wait(lambda: ph.mirror_scroll_top() > before + 100, 3, "the wheel over the side gutter scrolls the mirror")
            _scroll_steps(ph, (0,))
    # app screens on a wide window
    _fake_downloads(ph)
    for width, height in ((1280, 800), (1920, 1080)):
        ph.page.set_viewport_size({"width": width, "height": height})
        label = "%dx%d" % (width, height)
        for route in ("#/home", "#/downloads", "#/links", "#/settings/jd2", "#/settings/browser", "#/settings/system"):
            _app(ph, route)
            if route == "#/downloads":
                _fake_downloads(ph)
            ph.page.wait_for_timeout(300)
            _no_hscroll(ph, "%s %s" % (label, route))
            if route == "#/downloads":
                side, first = _rect(ph, ".dl-side"), _rect(ph, "#downloadList .download-card")
                assert side["l"] >= first["r"] + 10 and abs(side["t"] - first["t"]) <= 60, "%s: downloads is not two columns: %s %s" % (label, side, first)
                assert side["r"] <= width and first["w"] >= 500, (side, first)
            elif route == "#/links":
                a, b = _rect(ph, ".links-paste"), _rect(ph, ".links-queue")
                assert b["l"] >= a["r"] + 10 and abs(a["t"] - b["t"]) <= 30, "%s: links is not two columns: %s %s" % (label, a, b)
            elif route.startswith("#/settings"):
                nav, body = _rect(ph, ".settings-nav"), _rect(ph, ".settings-body")
                assert nav["r"] <= body["l"] + 1 and abs(nav["t"] - body["t"]) <= 40, "%s: settings is not two columns: %s %s" % (label, nav, body)
                assert nav["w"] <= 260 and body["w"] >= 600, (nav, body)
    # a narrow desktop window falls back to one column
    ph.page.set_viewport_size({"width": 1000, "height": 800})
    _app(ph, "#/downloads")
    ph.page.wait_for_timeout(300)
    side, first = _rect(ph, ".dl-side"), _rect(ph, "#downloadList .download-card")
    assert side["b"] <= first["t"] + 1, "at 1000 px the downloads side column should sit above the list: %s %s" % (side, first)
    _no_hscroll(ph, "1000 downloads")
    ph.take_errors("status of 400", "status of 503")     # the injected 'running' engine / a JD2 that is off


@scenario("reader-wide", device="desktop", display="reader")
def reader_wide(ph):
    """v0.1.63: on a 1920 px window the reader keeps a readable, centred column (<= ~780 px) for every text-size option;
    the head lines up with it."""
    ph.require_mouse()
    ph.page.set_viewport_size({"width": 1920, "height": 1080})
    ph.open("/article.html")
    ph.wait(lambda: ph.js("document.querySelectorAll('#readerBody .reader-p').length") >= 3, 15, "reader paragraphs")
    for size in (13, 16, 22, 26):
        ph.js("(n)=>{readerPrefs=Object.assign({},readerPrefs,{size:n});applyReaderPrefs()}", size)
        ph.page.wait_for_timeout(150)
        body, scroller = _rect(ph, "#readerBody"), _rect(ph, "#readerScroll")
        assert 300 <= body["w"] <= 781, "size %d: the reader column is %.0f px wide" % (size, body["w"])
        left, right = body["l"] - scroller["l"], scroller["r"] - body["r"]
        assert abs(left - right) <= 18, "size %d: the column is not centred (%.0f / %.0f)" % (size, left, right)
        sc = ph.js("(()=>{const e=document.getElementById('readerScroll');return e.scrollWidth-e.clientWidth})()")
        assert sc <= 1, "size %d: the reader scrolls sideways" % size
    ph.js("()=>{readerPrefs=Object.assign({},readerPrefs,{size:16});applyReaderPrefs()}")
    ph.page.wait_for_timeout(150)
    title = ph.js("()=>{const r=document.getElementById('readerTitle').getBoundingClientRect();return r.left}")
    body = _rect(ph, "#readerBody")
    assert abs(title - body["l"]) <= 4, "the head does not line up with the column (%.0f vs %.0f)" % (title, body["l"])


@scenario("waiting-reasons")
def waiting_reasons(ph):
    """v0.1.70: the 「JD2で待機中のリンク」 card says why each link waits (the reason text comes from the Hub), explains
    when they start by themselves, and offers 「重複・オフラインを消す」 only when such links exist. The JD2 list is injected."""
    ph.page.set_viewport_size(NARROW)
    _go(ph, "#/downloads")
    ph.wait(lambda: ph.js("!!document.querySelector('#view-downloads:not([hidden]) h1')"), 8, "downloads view")
    _fake_downloads(ph)
    ph.js("""()=>{
      const it=(id,name,state,reason)=>({id:String(id),package_id:'1',name,host:'rapidgator.net',bytes_total:5,state,reason});
      state.waiting={count:3,has_more:false,stuck:2,items:[it(11,'ok.rar','online','開始待ち（確認済み）'),
        it(12,'gone.rar','offline','オフライン（ファイルが消えている可能性）'),it(13,'dup.rar','duplicate','同じファイルがダウンロード一覧にあります')]};
      renderWaiting()}""")
    res = ph.js("""()=>({
      shown:!$('waitingBox').hidden, clean:!$('waitingClean').hidden, note:$('waitingNote').textContent,
      reasons:[...document.querySelectorAll('#waitingList .waiting-reason')].map(e=>e.textContent),
      stuck:[...document.querySelectorAll('#waitingList .waiting-reason.is-stuck')].length})""")
    assert res["shown"] and res["clean"], res
    assert res["reasons"] == ["開始待ち（確認済み）", "オフライン（ファイルが消えている可能性）", "同じファイルがダウンロード一覧にあります"], res
    assert res["stuck"] == 2 and "自動" in res["note"], res
    ph.js("()=>{state.waiting.stuck=0;state.waiting.items=state.waiting.items.slice(0,1);state.waiting.count=1;renderWaiting()}")
    assert ph.js("()=>$('waitingClean').hidden") is True
    ph.js("()=>{state.waiting=null;renderWaiting();state.engine=Object.assign({},state.engine,{phase:'stopped'});state.snapshot=null;state.downloadsSig='';renderDownloads()}")


@scenario("downloads-offline-waiting")
def downloads_offline_waiting(ph):
    """v0.1.63: a status of just "Offline" is the hoster's state of a link that is waiting: 待機, not 失敗. Real failure
    words (also together with Offline) still count as 失敗; a running or finished item is never a failure."""
    ph.page.set_viewport_size(NARROW)
    _go(ph, "#/downloads")
    ph.wait(lambda: ph.js("!!document.querySelector('#view-downloads:not([hidden]) h1')"), 8, "downloads view")
    res = ph.js("""()=>{
      const mk=(o)=>Object.assign({name:'x',status:'',bytes_loaded:0,bytes_total:1000,speed_bytes:null,eta_seconds:null,running:false,finished:false,progress:0},o);
      const count=(items)=>{const s=summarizeDownloads({packages:items,links:[],has_more:false});return [s.waiting,s.failed]};
      return {
        offline:count([mk({status:'Offline'})]), offlineJa:count([mk({status:'オフライン'})]), offlineLower:count([mk({status:' offline '})]),
        notfound:count([mk({status:'ファイルが見つかりません'})]), both:count([mk({status:'File not found (Offline)'})]),
        err:count([mk({status:'エラー: オフライン'})]), failed:count([mk({status:'Failed'})]), plain:count([mk({status:''})]),
        runningOffline:(()=>{const s=summarizeDownloads({packages:[mk({status:'Offline',running:true,speed_bytes:5})],links:[],has_more:false});return [s.running,s.failed]})(),
        finishedErr:(()=>{const s=summarizeDownloads({packages:[mk({status:'Error',finished:true})],links:[],has_more:false});return [s.finished,s.failed]})()}}""")
    assert res["offline"] == [1, 0] and res["offlineJa"] == [1, 0] and res["offlineLower"] == [1, 0], res
    assert res["notfound"] == [0, 1] and res["both"] == [0, 1] and res["err"] == [0, 1] and res["failed"] == [0, 1], res
    assert res["plain"] == [1, 0] and res["runningOffline"] == [1, 0] and res["finishedErr"] == [1, 0], res
    # on the page: the counts, the 待機 filter shows the Offline item, the 失敗 filter does not
    _fake_downloads(ph, [{"name": "Pack Offline", "status": "Offline"}, {"name": "Pack Gone", "status": "ファイルが見つかりません"}])
    txt = ph.js("()=>({w:$('dlCountWaiting').textContent,f:$('dlCountFailed').textContent})")
    assert txt == {"w": "2", "f": "1"}, txt
    ph.js("()=>{setFilter('waiting')}")
    names = ph.js("[...document.querySelectorAll('#downloadList .download-card .row-name')].map(e=>e.textContent)")
    assert "Pack Offline" in names and "Pack Gone" not in names, names
    ph.js("()=>{setFilter('failed')}")
    names = ph.js("[...document.querySelectorAll('#downloadList .download-card .row-name')].map(e=>e.textContent)")
    assert names == ["Pack Gone"], names
    ph.js("()=>{setFilter('all');state.engine=Object.assign({},state.engine,{phase:'stopped'});state.snapshot=null;state.downloadsSig='';renderDownloads()}")


@scenario("recent-cleared-on-logout")
def recent_cleared_on_logout(ph):
    """v0.1.63: 最近見たページ is this device's history; logging out clears it (a shared device), and the next login
    starts with none. The tab strip / session data go as before."""
    ph.open("/article.html")
    ph.wait(lambda: "E2E テスト記事" in ph.mirror_text(), 15, "mirror content")
    ph.wait(lambda: ph.js("(localStorage.getItem('qnapdl.recent.v1')||'').includes('article.html')"), 8, "the visited page is remembered")
    _app(ph, "#/home")
    ph.wait(lambda: ph.js("document.querySelectorAll('#homeRecentList .recent-row').length") >= 1, 5, "a recent page on the home screen")
    ph.js("()=>logout()")
    ph.wait(lambda: not ph.js("state.authed"), 10, "logged out")
    ph.take_errors("status of 401")            # the state long-poll answers 401 once the session is gone
    assert ph.js("localStorage.getItem('qnapdl.recent.v1')") is None, "the history survived the logout"
    ph.login()
    ph.wait(lambda: ph.js("state.authed === true"), 10, "logged in again")
    _app(ph, "#/home")
    ph.page.wait_for_timeout(400)
    assert ph.js("document.querySelectorAll('#homeRecentList .recent-row').length") == 0, "recent pages after a new login"
    assert ph.js("document.getElementById('homeRecent').hidden"), "the recent block is shown with nothing in it"


@scenario("desktop-pt-keys", device="desktop")
def desktop_pt_keys(ph):
    """v0.1.63 PC: inside the passthrough frame (another origin, so its key / mouse / wheel events never reach the app) Alt+←/→,
    mouse buttons 4 / 5 and a sideways wheel swipe step the app's history, Ctrl+L focuses the app's address bar and Ctrl+R
    reloads through the app. pt-helper.js posts the intent; the app checks origin and source as for every message."""
    ph.require_mouse()
    _pt_local(ph)
    fr = _pt_open(ph)
    fr.locator("#l2").click()
    ph.wait(lambda: _pt_cur(ph).endswith("/ptx/page2.html"), 10, "link followed")
    ph.wait(lambda: "PT-FIXTURE-PAGE-2" in (_pt_frame(ph).locator("#t").inner_text() if _pt_frame(ph).locator("#t").count() else ""), 10, "second page drawn")
    ph.page.wait_for_timeout(600)

    def frame():
        return _pt_frame(ph)
    frame().locator("#t").click()                       # keyboard focus inside the frame
    ph.press("Alt+ArrowLeft")
    ph.wait(lambda: _pt_cur(ph) == PT_PAGE, 8, "Alt+← inside the frame went back (url=%s)" % _pt_cur(ph))
    ph.wait(lambda: frame().locator("#t").count() > 0 and frame().locator("#t").inner_text() == "PT-FIXTURE-PAGE", 10, "first page drawn")
    ph.page.wait_for_timeout(500)
    frame().locator("#t").click()
    ph.press("Alt+ArrowRight")
    ph.wait(lambda: _pt_cur(ph).endswith("/ptx/page2.html"), 8, "Alt+→ inside the frame went forward")
    ph.wait(lambda: frame().locator("#t").count() > 0 and "PAGE-2" in frame().locator("#t").inner_text(), 10, "second page drawn (2)")
    ph.page.wait_for_timeout(500)
    # mouse buttons 4 / 5 inside the frame: cancelled, one step
    out = frame().evaluate("""()=>{const o=[];for(const t of ['mousedown','mouseup','auxclick']){const e=new MouseEvent(t,{button:3,bubbles:true,cancelable:true});
      document.body.dispatchEvent(e);o.push(e.defaultPrevented)}return o}""")
    assert all(out), "mouse button 4 in the frame was not cancelled: %s" % out
    ph.wait(lambda: _pt_cur(ph) == PT_PAGE, 8, "mouse button 4 inside the frame went back")
    ph.wait(lambda: frame().locator("#t").count() > 0 and frame().locator("#t").inner_text() == "PT-FIXTURE-PAGE", 10, "first page drawn (2)")
    ph.page.wait_for_timeout(500)
    # a sideways wheel swipe over the frame = forward (fingers to the left = deltaX > 0). Playwright's WebKit hands a
    # synthetic wheel over a cross-origin frame to the top document, never to the frame, so it is checked on Chromium;
    # on WebKit the same step is driven by the helper's own message path (mouse button 5 above covers it).
    if ph.engine == "chromium":
        box = ph.page.locator("#ptFrame").bounding_box()
        ph.page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        for _ in range(4):
            ph.page.mouse.wheel(70, 0)
            ph.page.wait_for_timeout(25)
    else:
        frame().locator("#t").click()
        ph.press("Alt+ArrowRight")
    ph.wait(lambda: _pt_cur(ph).endswith("/ptx/page2.html"), 8, "a sideways swipe over the frame went forward (url=%s)" % _pt_cur(ph))
    ph.wait(lambda: frame().locator("#t").count() > 0 and "PAGE-2" in frame().locator("#t").inner_text(), 10, "second page drawn (3)")
    # Ctrl+L: the app's address bar gets the focus from inside the frame; Ctrl+R reloads through the app (no app reload)
    ph.js("window.__noReload = 'still here'")
    frame().locator("#t").click()
    ph.press("Control+l")
    ph.wait(lambda: ph.js("document.activeElement && document.activeElement.id") == "omnibox", 5, "Ctrl+L in the frame focused the address bar")
    ph.press("Escape")
    frame().locator("#t").click()
    hits = len(ph.env.fx.pt_seen)
    ph.press("Control+r")
    ph.wait(lambda: len(ph.env.fx.pt_seen) > hits, 10, "Ctrl+R in the frame reloaded the page through the passthrough")
    assert ph.js("window.__noReload") == "still here", "Ctrl+R reloaded the app"
    assert ph.js("state.route.view") == "tab"


# ------------------------------------------------------------------ v0.1.65: link-probe queue, tab limit setting

@scenario("probe-queue-burst")
def probe_queue_burst(ph):
    """v0.1.65: 12 long presses in quick succession. Each one only adds a job on the Hub (answered at once, with its
    place in the queue); the Hub runs them one after another; all 12 end "done"; the phone shows the progress (pill
    "リンク解析 n/12") and a sheet that lists them. At no time are two hidden pages open for the probes (the old design
    ran up to 4 at once and held one HTTP request per press)."""
    import threading
    ph.page.set_viewport_size(NARROW)
    ph.open("/probe.html")
    ph.wait(lambda: "リンク12" in ph.mirror_text(), 20, "probe page")
    hrefs = ph.js("(()=>{const d=mirrorDoc();return Array.from(d.querySelectorAll('a[data-href]')).map(a=>a.dataset.href)})()")
    assert len(hrefs) >= 12, hrefs
    peak = {"targets": 0}
    stop = threading.Event()

    def watch():
        while not stop.wait(0.05):
            peak["targets"] = max(peak["targets"], len(ph.env.rb._probe_targets))
    threading.Thread(target=watch, daemon=True).start()
    try:
        started = ph.js("""async(hrefs)=>{
          const t = activeWebTab(); const t0 = performance.now(); const answers = [];
          for (const h of hrefs.slice(0, 12)) {
            const s = performance.now();
            await startProbe(t, {href: h});
            answers.push(Math.round(performance.now() - s));
          }
          return {answers, total: Math.round(performance.now() - t0)};
        }""", hrefs)
        ph.log("12 presses answered in %s ms (each %s)" % (started["total"], started["answers"]))
        assert max(started["answers"]) < 2000, "a long press waited for the probe itself: %s" % started
        ph.wait(lambda: ph.js("(()=>{const c=document.getElementById('probeChip');return !!c && !c.hidden && /\\/12/.test(c.textContent)})()"),
                10, "the pill with the progress /12")
        chip = ph.js("document.getElementById('probeChip').textContent")
        ph.log("pill: %s" % chip)
        ph.wait(lambda: ph.js("(()=>{const t=activeWebTab();const p=t&&t.rb&&t.rb.probes;return !!p && p.total===12 && p.done===12})()"),
                90, "all 12 probes done")
        peak_now = peak["targets"]
    finally:
        stop.set()
    assert peak_now <= 1, "%d probe pages were open at once (the queue runs one job at a time)" % peak_now
    jobs = ph.js("""async()=>{const t=activeWebTab();const r=await fetch('/api/rb/probe/jobs?tab='+t.rb.id,{credentials:'same-origin'});return (await r.json()).jobs}""")
    assert len(jobs) == 12 and all(j["state"] == "done" and j["links"] for j in jobs), [(j["state"], j.get("error")) for j in jobs]
    spans = sorted((j["started"], j["finished"]) for j in jobs)
    assert all(b[0] >= a[1] - 0.05 for a, b in zip(spans, spans[1:])), "jobs overlapped: %s" % spans
    # the pill opens the list; every row has its links and the send / add actions
    ph.js("document.getElementById('probeChip').click()")
    ph.wait(lambda: ph.js("!document.getElementById('probeSheet').hidden"), 5, "the sheet")
    # the sheet opens at once and fills when the job list arrives
    ph.wait(lambda: ph.js("document.querySelectorAll('#probeList .probe-job').length") == 12, 5,
            "the sheet lists the 12 jobs (now %s)" % ph.js("document.querySelectorAll('#probeList .probe-job').length"))
    text = ph.js("document.getElementById('probeSheet').innerText")
    assert "追加" in text and "リンク解析" in text, text[:300]
    ph.take_errors("status of 429", "ERR_ABORTED", "Failed to fetch", "Load failed")
    assert not ph.js("document.querySelectorAll('#notice .toast.error').length"), "an error toast appeared"


@scenario("probe-autosend-quiet")
def probe_autosend_quiet(ph):
    """v0.1.67: with automatic sending on and JD2 able to take links, a finished long-press analysis does not open the
    result sheet (it opened for a moment-before-the-send read and showed 送信済み with send buttons); only a send that
    still failed afterwards opens it."""
    ph.page.set_viewport_size(NARROW)
    ph.open("/probe.html")
    ph.wait(lambda: "リンク1" in ph.mirror_text(), 20, "probe page")
    href = ph.js("(()=>{const d=mirrorDoc();return d.querySelector('a[data-href]').dataset.href})()")
    ph.js("()=>{window.jd2CanSend = () => true; state.settings.probe_autosend = true}")    # the harness has no JD2
    ph.js("async(h)=>{await startProbe(activeWebTab(), {href: h})}", href)
    ph.wait(lambda: ph.js("(()=>{const t=activeWebTab();const p=t&&t.rb&&t.rb.probes;return !!p && p.done>=1})()"), 30, "probe done")
    ph.page.wait_for_timeout(1500)
    assert ph.js("document.getElementById('probeSheet').hidden"), "the sheet opened although the result is being sent automatically"
    # the harness cannot send: the fallback's failure opens the sheet with the reason
    ph.wait(lambda: not ph.js("document.getElementById('probeSheet').hidden"), 10, "the sheet after the failed fallback send")
    ph.take_errors("status of 400", "status of 409", "status of 429", "Failed to fetch", "Load failed")


@scenario("probe-queue-cancel")
def probe_queue_cancel(ph):
    """v0.1.65: 待ちを取り消す cancels what has not started; closing the tab drops that tab's jobs."""
    ph.page.set_viewport_size(NARROW)
    tab = ph.open("/probe.html")
    ph.wait(lambda: "リンク12" in ph.mirror_text(), 20, "probe page")
    hrefs = ph.js("(()=>{const d=mirrorDoc();return Array.from(d.querySelectorAll('a[data-href]')).map(a=>a.dataset.href.replace('delay=700','delay=2500'))})()")
    for h in hrefs[:8]:
        res = ph.post("/api/rb/probe", {"tab": tab, "href": h, "label": "x"})
        assert res["status"] == 202, res
    again = ph.post("/api/rb/probe", {"tab": tab, "href": hrefs[1]})
    assert again["status"] == 202 and '"existing": true' in again["text"].replace('":true', '": true'), again   # the same link is not queued twice
    res = ph.post("/api/rb/probe/cancel", {"tab": tab})
    assert res["status"] == 200, res
    jobs = ph.js("""async(t)=>{const r=await fetch('/api/rb/probe/jobs?tab='+t,{credentials:'same-origin'});return (await r.json()).jobs}""", tab)
    assert not [j for j in jobs if j["state"] == "queued"], "queued jobs remain after the cancel: %s" % [j["state"] for j in jobs]
    ph.post("/api/rb/probe", {"tab": tab, "href": hrefs[9], "label": "y"})
    res = ph.post("/api/rb/tabs/close", {"id": tab})
    assert res["status"] == 200, res
    assert not ph.env.rb.probes.jobs(tab), "the closed tab still has jobs"
    ph.take_errors("status of 429", "ERR_ABORTED", "Failed to fetch", "Load failed", "status of 400", "status of 404")


@scenario("tab-limit-setting")
def tab_limit_setting(ph):
    """v0.1.65: Settings › ブラウザ › 「NASブラウザのタブの上限」. With 5 the Hub opens 5 NAS tabs and refuses the 6th with the
    message that names the setting; the control round-trips (reload keeps it); 「推奨値にする」 applies the self-test's number."""
    ph.js("()=>{location.hash='#/settings/browser'}")
    ph.wait(lambda: ph.js("!!document.querySelector('#view-settings:not([hidden])') && !!document.getElementById('rbMaxTabs')"), 8, "browser settings")
    assert ph.js("document.getElementById('rbMaxTabs').value") == "3", "default is not 3"
    assert ph.js("document.getElementById('rbMaxTabs').options.length") == 20
    ph.page.select_option("#rbMaxTabs", "5")
    ph.wait(lambda: ph.js("state.settings.rb_max_tabs") == 5, 5, "saved on the phone")
    ph.page.reload()
    ph.wait(lambda: ph.js("typeof state !== 'undefined' && state.authed === true && !!document.getElementById('rbMaxTabs')"), 15, "app reloaded")
    ph.js("()=>{location.hash='#/settings/browser'}")
    ph.wait(lambda: ph.js("document.getElementById('rbMaxTabs').value") == "5", 8, "the setting survived a reload")
    ids = []
    for n in range(5):
        res = ph.post("/api/rb/tabs", {"url": "https://a.example.com/article.html?n=%d" % n})
        assert res["status"] == 201, "tab %d: %s" % (n + 1, res)
        ids.append(res["text"])
    assert len(ph.env.tabs()) == 5, ph.env.tabs()
    res = ph.post("/api/rb/tabs", {"url": "https://a.example.com/article.html?n=6"})
    assert res["status"] >= 400 and "タブは最大5枚です（設定で変更できます）。不要なタブを閉じてください" in res["text"], res
    # the recommendation (as the self-test stores it) is offered and applied by the button
    ph.env.rb.tab_reco = {"tabs": 9, "avg_mib": 80.0, "max_mib": 120.0, "at": 0}
    ph.js("()=>{state.rbStatus = Object.assign({}, state.rbStatus || {}, {tab_reco: {tabs: 9, avg_mib: 80, max_mib: 120}}); renderMaxTabs()}")
    ph.wait(lambda: ph.js("!document.getElementById('rbMaxTabsRecoRow').hidden"), 5, "recommendation row")
    assert "9枚" in ph.js("document.getElementById('rbMaxTabsReco').textContent")
    ph.page.locator("#rbMaxTabsApply").click()
    ph.wait(lambda: ph.js("state.settings.rb_max_tabs") == 9 and ph.js("document.getElementById('rbMaxTabs').value") == "9", 5, "the recommendation applied")
    ph.setting("rb_max_tabs", 3)
    ph.take_errors("status of 4", "status of 5")
