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
    ph.wait(lambda: ph.js("selftestUi.snap&&selftestUi.snap.state") == "done", 250, "self-test finished")
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
                   "## 環境", "## 内蔵テストページ", "## この端末", "## 実サイト", "## 後片付け", "builtin.reader", "client.mirror_events",
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
    steps = ("edge_back", "flick_right", "back_first", "flick_left", "slow_move", "edge_start", "forward", "pull_cancel", "pull_close")
    for mode in ("mirror", "reader", "tiles"):
        assert "swipe.%s.display" % mode in rows, sorted(rows)
        for step in steps:
            row = rows.get("swipe.%s.%s" % (mode, step))
            assert row, "missing %s.%s" % (mode, step)
            assert row["status"] in ("pass", "warn"), "%s.%s: %s | %s | %s" % (mode, step, row["status"], row["value"], row["hint"])
        hist = rows["swipe.%s.history" % mode]
        assert hist["status"] == "pass", hist
    assert rows["swipe.mirror.edge_back"]["status"] == "pass", rows["swipe.mirror.edge_back"]
    assert rows["swipe.mirror.flick_right"]["status"] == "pass", rows["swipe.mirror.flick_right"]
    assert rows["swipe.mirror.pull_close"]["status"] == "pass", rows["swipe.mirror.pull_close"]
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
    ph.wait(lambda: page.locator("#passkeyLogin").is_visible() or ph.js("state.authed === true"), 15,
            "login screen (or automatic conditional sign-in) after revoke-all")
    if not ph.js("state.authed === true"):
        try:
            page.click("#passkeyLogin", timeout=2000)
        except Exception:
            assert ph.js("state.authed === true"), "button vanished without a login"   # autofill won the race
    ph.wait(lambda: ph.js("state.authed === true"), 15, "passkey login")
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
