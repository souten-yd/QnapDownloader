"""E2E scenarios. Each one is a function ``fn(ph)`` taking a ``harness.Phone``; it raises AssertionError to fail
and ``harness.Skip`` to skip. Register new ones with the ``@scenario`` decorator (see docs/testing.md).

Phone input is always touch (Playwright touchscreen / CDP touch), never mouse clicks: taps on iOS have no
``click`` for elements without href, which is the class of bug this suite exists to catch.
"""
import time

from harness import BASE, PASSWORD, Skip

SCENARIOS = []


def scenario(name, engines=("chromium", "webkit"), display="mirror"):
    """Register a scenario. ``display`` is the Settings default display the phone starts with."""
    def deco(fn):
        SCENARIOS.append({"name": name, "fn": fn, "engines": engines, "display": display})
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


@scenario("display-mirror", display="mirror")
def display_mirror(ph):
    ph.open("/article.html")
    ph.wait(lambda: ph.display() == "mirror", 5, "display mirror")
    ph.wait(lambda: "E2E テスト記事" in ph.mirror_text(), 15, "mirror content")
    ph.wait(lambda: ph.js("(()=>{const d=mirrorDoc();const i=d&&d.querySelector('img');return !!i&&i.complete&&i.naturalWidth>0})()"),
            15, "mirror image loaded")
    assert ph.js("getComputedStyle(document.getElementById('mirrorWrap')).display") != "none"


@scenario("display-tiles", display="fast")
def display_tiles(ph):
    ph.open("/article.html")
    ph.wait(lambda: ph.display() == "tiles", 5, "display tiles")
    ph.wait(lambda: ph.js("[...document.querySelectorAll('#tileScroll img')].filter(i=>i.complete&&i.naturalWidth>0).length") >= 1,
            20, "tile images loaded")


LIVE_SHOWN = "(()=>{const i=document.getElementById('remoteImg');return !i.hidden&&i.naturalWidth>0})()"


@scenario("display-live", display="live")
def display_live(ph):
    """The live picture arrives over whichever transport works here (MJPEG first, then SSE, then polling)."""
    tab = ph.open("/article.html")
    ph.wait(lambda: ph.display() == "live", 5, "display live")
    ph.wait(lambda: ph.js(LIVE_SHOWN), 20, "live frame visible")
    ph.wait(lambda: ph.env.tabs()[tab].seq > 0, 10, "server has produced frames (seq>0)")
    kind = ph.js("state.rbStream ? state.rbStream.kind : 'poll'")
    assert kind in ("mjpeg", "sse", "poll"), kind
    ph.notes.append("transport " + kind)
    perf = ph.js("(()=>{setPerf(true);return perfText()})()")
    assert "ライブ(" in perf, perf


@scenario("live-sse-fallback", display="live")
def live_sse_fallback(ph):
    """MJPEG never delivers a byte (buffering proxy): after 2.5 s the client reads the event stream and draws
    the frames; the working transport is remembered for the next start."""
    ph.env.blocked.add("mjpeg")
    t0 = time.monotonic()
    tab = ph.open("/article.html")
    ph.wait(lambda: ph.display() == "live", 5, "display live")
    ph.wait(lambda: ph.js("state.rbStream && state.rbStream.kind") == "sse", 10, "switched to SSE")
    ph.wait(lambda: ph.js(LIVE_SHOWN), 15, "SSE frame visible")
    assert ph.js("document.getElementById('remoteImg').src").startswith("blob:"), "frames are drawn as blob images"
    ph.wait(lambda: ph.env.tabs()[tab].seq > 0 and ph.env.tabs()[tab].streams >= 1, 10, "server streams to the phone")
    ph.notes.append("SSE after %.1fs" % (time.monotonic() - t0))
    assert ph.env.stream_hits["sse"] >= 1 and ph.env.stream_hits["mjpeg"] >= 1
    perf = ph.js("(()=>{setPerf(true);return perfText()})()")
    assert "ライブ(SSE: MJPEGの最初の画面が届かない)" in perf, perf
    assert ph.js("JSON.parse(localStorage.getItem(RB_LIVE_PREF_KEY)).t") == "sse"
    # at most two object URLs alive: the shown one (and the one being decoded)
    # remembered: a new app start goes straight to SSE and does not touch MJPEG again today
    hits = ph.env.stream_hits["mjpeg"]
    ph.page.reload()
    ph.page.wait_for_selector("#omnibox", state="attached")
    ph.wait(lambda: ph.js("state.authed === true"), 10, "session after reload")
    ph.wait(lambda: ph.js("state.rbStream && state.rbStream.kind") == "sse", 20, "SSE again after reload")
    ph.wait(lambda: ph.js(LIVE_SHOWN), 15, "SSE frame visible after reload")
    assert ph.env.stream_hits["mjpeg"] == hits, "MJPEG was retried although SSE was remembered"


@scenario("live-polling-fallback", display="live")
def live_polling_fallback(ph):
    """Neither stream delivers: the client ends on frame.jpg polling and says why."""
    ph.env.blocked.update(("mjpeg", "sse"))
    tab = ph.open("/article.html")
    ph.wait(lambda: ph.display() == "live", 5, "display live")
    ph.wait(lambda: ph.js("(()=>{const t=activeWebTab();return !!(t&&t.rb&&t.rb.streamFailed)})()"), 20, "gave up on streams")
    ph.wait(lambda: ph.js(LIVE_SHOWN), 20, "polled frame visible")
    perf = ph.js("(()=>{setPerf(true);return perfText()})()")
    assert "ライブ(ポーリング: " in perf, perf
    assert ph.js("JSON.parse(localStorage.getItem(RB_LIVE_PREF_KEY)).t") == "poll"


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
    for cid in ("builtin.reader", "builtin.mirror", "builtin.mirror_safe", "builtin.menu", "builtin.tiles", "builtin.fps"):
        assert cid in checks, "missing check " + cid
    client = [c for c in snap["checks"] if c["section"] == "client"]
    assert snap["client"] == "received" and len(client) >= 8, "client section: %s" % [(c["id"], c["status"]) for c in client]
    bad = [(c["id"], c["status"], c["value"]) for c in client if c["status"] == "fail"]
    assert not bad, "failed client checks (this browser): %s" % bad
    for cid in ("client.live", "client.secure", "client.clipboard", "env.cgroup"):
        assert cid in checks, "missing check " + cid
    live = checks["client.live"]
    assert live["status"] != "fail" and "経路:" in live["value"], live
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
    assert checks["cleanup.tabs"]["status"] == "pass"
    assert ph.env.rb.selftest_tabs() == [] and ph.env.tabs() == {}, "tabs left behind: %s" % list(ph.env.tabs())
    # the view shows summary and tables; the copied text is a self-contained report
    assert "合格" in ph.js("document.getElementById('selftestSummary').textContent")
    assert ph.js("document.querySelectorAll('#selftestTables table').length") >= 4
    ph.page.click("#selftestCopy")
    ph.wait(lambda: len(ph.js("selftestUi.text")) > 500, 10, "report text")
    text = ph.js("selftestUi.text")
    for needle in ("== QnapDownloader 自己テスト", "app: v" + ph.env.version, "[NAS] cpu=", "[Phone] ua=", "viewport=",
                   "## 環境", "## 内蔵テストページ", "## この端末", "## 操作の応答", "## 実サイト", "## 後片付け", "builtin.reader", "client.mirror_events",
                   "value:", "threshold:", "## debug log (last 50 lines", "[Source] cgroup=", "[Live] transport=", "env.cgroup",
                   "client.secure"):
        assert needle in text, "report lacks %r" % needle
    assert "a.example.com" in text and "/article.html" not in text, "real-site URLs must be reduced to the host by default"
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
    assert ph.env.rb.selftest_tabs() == [] and ph.env.tabs() == {}, "tabs left behind"
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
            ok = ("pass", "warn", "skip") if (step == "hscroll" and mode != "mirror") else ("pass", "warn")
            assert row["status"] in ok, "%s.%s: %s | %s | %s" % (mode, step, row["status"], row["value"], row["hint"])
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
    ph.page.locator("#navForward").tap()
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



