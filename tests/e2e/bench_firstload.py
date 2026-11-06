#!/usr/bin/env python3
"""First-load benchmark (v0.1.54): tap -> content visible on the phone, median of N runs.

    python3 tests/e2e/bench_firstload.py --runs 3 [--display mirror|reader] [--cdp-delay-ms 40] [--stats-delay 1.5]
        [--path "/firstload.html?nodes=3000"] [--setting rb_spare_tab=false ...] [--label name]

Everything is measured from the phone page itself (a 20 ms poller inside the page), so the same script runs on any
revision of the app: copy it, tests/e2e/harness.py and tests/fake_docker.py into an older checkout to get the "before".

  --cdp-delay-ms  one-way delay added to every CDP message from the NAS browser (a Celeron answers in tens of ms)
  --stats-delay   seconds `docker stats --no-stream` takes (it samples for 1-2 s on a NAS)
  --origin-delay  fixture resources take this long (the fixture's `delay` query default is 150 ms)

Output: one line per run and the medians; `--json` prints the result as JSON.
"""
import argparse
import json
import queue
import statistics
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


class DelayedWS:
    """Websocket wrapper: every message from the browser arrives `delay` seconds after it was received."""

    def __init__(self, ws, delay):
        self._ws, self._delay = ws, delay
        self._q = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        while True:
            try:
                item = self._ws.recv()
            except Exception:
                item = None
            self._q.put((time.monotonic() + self._delay, item))
            if item is None:
                return

    def recv(self):
        due, item = self._q.get()
        wait = due - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        return item

    def send_text(self, text):
        return self._ws.send_text(text)

    def close(self):
        return self._ws.close()


OBSERVER = """(display) => {
  const b = window.__bench = {t0: 0, tabsMs: null, text: null, hero: null, full: null, tabsAt: null};
  const f = window.fetch;
  window.fetch = function (input, init) {
    const url = typeof input === 'string' ? input : input.url;
    const method = (init && init.method) || 'GET';
    const isTab = method === 'POST' && /\\/api\\/rb\\/tabs$/.test(url);
    const s = performance.now();
    const p = f.apply(this, arguments);
    if (isTab) p.then(() => { b.tabsMs = performance.now() - s; b.tabsAt = performance.now() - b.t0; }, () => {});
    return p;
  };
  const tick = () => {
    const now = performance.now() - b.t0;
    let doc = null, text = '', imgs = [];
    try {
      if (display === 'mirror') {
        const d = mirrorDoc();
        if (d) { text = d.body.innerText; imgs = Array.from(d.images); doc = d; }
      } else {
        text = document.getElementById('readerBody').innerText;
        imgs = Array.from(document.querySelectorAll('#readerBody img.reader-img'));
      }
    } catch (e) { /* mid navigation */ }
    if (b.t0 && b.text === null && text.indexOf('E2E') >= 0) b.text = now;
    if (b.t0 && b.text !== null && b.hero === null) {
      const first = imgs[0];
      if (first && first.complete && first.naturalWidth > 0) b.hero = now;
    }
    if (b.t0 && b.hero !== null && b.full === null) {
      const h = (doc && doc.defaultView ? doc.defaultView.innerHeight : innerHeight) * 3;
      const open = imgs.filter(i => !i.complete && i.getBoundingClientRect().top < h);
      if (!open.length) b.full = now;
    }
    if (b.full === null) setTimeout(tick, 20);
  };
  tick();
}"""


def one_run(env, browser, ctx_kw, display, path, settings, timeout):
    import harness
    env.reset_tabs()
    context = browser.new_context(**ctx_kw)
    page = context.new_page()
    ph = harness.Phone(env, page, context, "chromium", env.tmp, "bench")
    try:
        ph.start(display)
        for key, value in settings.items():
            ph.setting(key, value)
        page.evaluate(OBSERVER, display)
        omni = page.locator("#omnibox").first
        omni.click()
        omni.fill(harness.BASE + path)
        env.rb._stats_cache = None          # the browser has been idle for a while: the memory guard must look again
        spare = getattr(env.rb, "spare_info", lambda: None)()
        page.evaluate("window.__bench.t0 = performance.now()")
        omni.press("Enter")
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if page.evaluate("window.__bench.full") is not None:
                break
            page.wait_for_timeout(50)
        result = page.evaluate("window.__bench")
        try:
            tl = page.evaluate("(()=>{const t=activeWebTab();return t&&t.rb&&t.rb.tl?t.rb.tl.result:null})()")
        except Exception:
            tl = None
        if tl is None:
            # the result arrives a moment after `full`
            page.wait_for_timeout(600)
            try:
                tl = page.evaluate("(()=>{const t=activeWebTab();return t&&t.rb&&t.rb.tl?t.rb.tl.result:null})()")
            except Exception:
                tl = None
        result["timeline"] = tl
        result["spare"] = spare
        return result
    finally:
        context.close()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--display", default="mirror", choices=("mirror", "reader"))
    ap.add_argument("--path", default="/firstload.html")
    ap.add_argument("--cdp-delay-ms", type=float, default=0.0)
    ap.add_argument("--stats-delay", type=float, default=0.0)
    ap.add_argument("--setting", action="append", default=[], help="key=value (true/false/text) applied after login")
    ap.add_argument("--label", default="")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--timeout", type=float, default=40.0)
    ap.add_argument("--nas-chrome", default=None)
    args = ap.parse_args(argv)

    from playwright.sync_api import sync_playwright
    import harness
    settings = {}
    for item in args.setting:
        key, _, value = item.partition("=")
        settings[key] = {"true": True, "false": False}.get(value.lower(), value)

    env = harness.Env(nas_chrome=args.nas_chrome)
    try:
        import remote_browser as rb
        if args.cdp_delay_ms:
            real = env.rb._connect
            env.rb._connect = lambda ip: DelayedWS(real(ip), args.cdp_delay_ms / 1000.0)
        if args.stats_delay:
            env.fake_docker.stats_delay = args.stats_delay
        runs = []
        with sync_playwright() as p:
            ctx_kw = dict(viewport={"width": 390, "height": 844}, device_scale_factor=3, has_touch=True, is_mobile=True)
            try:
                browser = p.chromium.launch()
            except Exception:
                browser = p.chromium.launch(executable_path=env.nas_chrome_path)
            # warm-up: the first run starts the NAS browser and logs in; it is not counted
            one_run(env, browser, ctx_kw, args.display, args.path, settings, args.timeout)
            for _ in range(args.runs):
                runs.append(one_run(env, browser, ctx_kw, args.display, args.path, settings, args.timeout))
            browser.close()
    finally:
        env.close()

    def med(key):
        values = [r[key] for r in runs if r.get(key) is not None]
        return round(statistics.median(values)) if values else None
    summary = {"label": args.label, "display": args.display, "runs": len(runs), "cdp_delay_ms": args.cdp_delay_ms,
               "stats_delay": args.stats_delay,
               "median_ms": {k: med(k) for k in ("tabsMs", "tabsAt", "text", "hero", "full")},
               "each": [{k: (round(r[k]) if r.get(k) is not None else None) for k in ("tabsMs", "tabsAt", "text", "hero", "full")}
                        for r in runs],
               "spare": [r.get("spare") for r in runs],
               "timeline": [r.get("timeline") for r in runs]}
    if args.json:
        print(json.dumps(summary, ensure_ascii=False))
    else:
        print("== %s display=%s cdp-delay=%sms stats-delay=%ss runs=%d" % (
            args.label or "bench", args.display, args.cdp_delay_ms, args.stats_delay, len(runs)))
        for i, row in enumerate(summary["each"]):
            print("run %d: %s" % (i + 1, row))
        print("median:", summary["median_ms"])
        print("spare tab ready at start:", summary["spare"])
        for tl in summary["timeline"]:
            if tl:
                print("  ", tl.get("line"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
