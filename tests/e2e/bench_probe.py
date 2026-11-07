#!/usr/bin/env python3
"""Long-press probe benchmark (v0.1.65): N probes started in quick succession, how many finish and how long it takes.

    python3 tests/e2e/bench_probe.py --count 12 [--mode old|new] [--base-percent 55] [--per-target-percent 8.5] [--hold-ms 700]

  --mode old   v0.1.63 and earlier: POST /api/rb/probe is held open until the probe is finished (results come in the reply)
  --mode new   v0.1.65: POST /api/rb/probe enqueues; results come from GET /api/rb/probe/jobs
  --base-percent / --per-target-percent
               a model of the container's memory (the fake `docker stats`): percent = base + per_target * live hidden
               probe targets. Defaults: 55 % with the user's tabs + 8.5 % (about 170 MiB of 2 GiB) per hidden target.
               A SIMULATION: the real Chromium here is not limited to 2 GiB. Use --base-percent 0 --per-target-percent 0 for none.
  --hold-ms    how long each fixture redirect takes (the fixture's /go/<n>?delay=)

Everything is measured from the phone page itself, so the same script can be copied (with harness.py, fake_docker.py and
fixtures/probe.html) into an older checkout to get the "before" numbers.
"""
import argparse
import json
import statistics
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

OBSERVE = """() => {
  window.__probe = {calls: [], toasts: []};
  const original = window.api;
  window.api = async function (path, body, extra) {
    const started = performance.now();
    if (path.indexOf('/api/rb/probe') !== 0 || body === null || body === undefined) return original.apply(this, arguments);
    try {
      const r = await original.apply(this, arguments);
      window.__probe.calls.push({path, ms: Math.round(performance.now() - started), ok: true,
        links: r && r.links ? r.links.length : null, job: r && r.job ? r.job : null});
      return r;
    } catch (e) {
      window.__probe.calls.push({path, ms: Math.round(performance.now() - started), ok: false, status: e.status || 0, error: String(e.message).slice(0, 120)});
      throw e;
    }
  };
  const t = window.toast;
  window.toast = function (text, kind) { if (kind === 'error') window.__probe.toasts.push(String(text).slice(0, 120)); return t.apply(this, arguments); };
}"""


def run(args):
    from playwright.sync_api import sync_playwright
    import harness
    env = harness.Env(nas_chrome=args.nas_chrome)
    stop = threading.Event()
    peak = {"targets": 0, "percent": 0.0, "running": 0}
    try:
        def model():
            while not stop.wait(0.1):
                n = len(env.rb._probe_targets)
                percent = args.base_percent + args.per_target_percent * n
                env.fake_docker.stats_out = "%dMiB / 2GiB|%.2f%%" % (int(2048 * percent / 100), percent)
                peak["targets"] = max(peak["targets"], n)
                peak["percent"] = max(peak["percent"], percent)
        threading.Thread(target=model, daemon=True).start()
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch()
            except Exception:
                browser = p.chromium.launch(executable_path=env.nas_chrome_path)
            context = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=3, has_touch=True,
                                          is_mobile=True)
            page = context.new_page()
            ph = harness.Phone(env, page, context, "chromium", args.out, "bench-probe")
            ph.start("mirror")
            ph.open("/probe.html")
            ph.wait(lambda: "リンク12" in ph.mirror_text(), 20, "probe page")
            page.evaluate(OBSERVE)
            hrefs = page.evaluate("""(()=>{const d=mirrorDoc();return Array.from(d.querySelectorAll('a[data-href]')).map(a=>a.dataset.href.replace('delay=700','delay=%d'))})()""" % args.hold_ms)
            assert len(hrefs) >= args.count, hrefs
            # a trivial request every 700 ms while the probes run: how long does it wait? (connection pool, see the doc)
            page.evaluate("""()=>{window.__lat=[];window.__latStop=false;(async()=>{while(!window.__latStop){const s=performance.now();
              try{await fetch('/api/session',{credentials:'same-origin'});}catch(e){}
              window.__lat.push(Math.round(performance.now()-s));await new Promise(r=>setTimeout(r,700));}})()}""")
            t0 = time.monotonic()
            for i in range(args.count):
                page.evaluate("(h)=>{const t=activeWebTab();perform(()=>startProbe(t,{href:h}))}", hrefs[i])
                page.wait_for_timeout(150)
            end = t0 + args.timeout
            jobs = None
            while time.monotonic() < end:
                page.wait_for_timeout(500)
                info = page.evaluate("window.__probe")
                if args.mode == "old":
                    if len(info["calls"]) >= args.count:
                        break
                else:
                    jobs = page.evaluate("""async()=>{const t=activeWebTab();const r=await fetch('/api/rb/probe/jobs?tab='+t.rb.id,{credentials:'same-origin'});return (await r.json()).jobs}""")
                    if len(jobs) >= args.count and all(j["state"] in ("done", "failed", "cancelled") for j in jobs):
                        break
            total = time.monotonic() - t0
            lat = page.evaluate("()=>{window.__latStop=true;return window.__lat}")
            info = page.evaluate("window.__probe")
            if args.mode == "new":
                ok = sum(1 for j in jobs if j["state"] == "done")
                failed = [j.get("error") for j in jobs if j["state"] == "failed"]
                per_job = [round(j["finished"] - j["started"], 1) for j in jobs if j.get("finished") and j.get("started")]
            else:
                ok = sum(1 for c in info["calls"] if c["ok"] and c.get("links"))
                failed = [c.get("error") for c in info["calls"] if not c["ok"]]
                per_job = [round(c["ms"] / 1000.0, 1) for c in info["calls"]]
            unanswered = args.count - (len(jobs) if args.mode == "new" and jobs else len(info["calls"]))
            result = {"mode": args.mode, "count": args.count, "ok": ok, "failed": len(failed), "failures": sorted(set(map(str, failed))),
                      "unfinished": max(0, args.count - ok - len(failed)), "seconds": round(total, 1),
                      "per_job_s": per_job, "peak_targets": peak["targets"], "peak_model_percent": round(peak["percent"], 1),
                      "toasts": sorted(set(info["toasts"])),
                      "session_request_ms": {"max": max(lat) if lat else None, "median": int(statistics.median(lat)) if lat else None,
                                             "over_1s": sum(1 for v in lat if v > 1000), "n": len(lat)}}
            browser.close()
            return result
    finally:
        stop.set()
        env.close()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--count", type=int, default=12)
    ap.add_argument("--mode", default="new", choices=("old", "new"))
    ap.add_argument("--base-percent", type=float, default=55.0)
    ap.add_argument("--per-target-percent", type=float, default=8.5)
    ap.add_argument("--hold-ms", type=int, default=700)
    ap.add_argument("--timeout", type=float, default=150.0)
    ap.add_argument("--out", default="/tmp/qd-bench-probe")
    ap.add_argument("--nas-chrome", default=None)
    args = ap.parse_args(argv)
    Path(args.out).mkdir(parents=True, exist_ok=True)
    print(json.dumps(run(args), ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
