#!/usr/bin/env python3
"""End-to-end suite: real webapp + real RemoteBrowser + real NAS-side Chrome + phone browsers (Playwright).

    python3 tests/e2e/run.py --engines chromium,webkit --out /tmp/e2e [--only mirror,reader] [--nas-chrome PATH]

Exit code 0 = all run scenarios passed (skips are fine), 1 = a scenario failed, 2 = could not start.
See docs/testing.md.
"""
import argparse
import sys
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def phone_options(playwright, engine):
    """(browser type, launch kwargs, context kwargs) for one engine; iPhone-like touch device."""
    if engine == "chromium":
        return (playwright.chromium, {},
                dict(viewport={"width": 390, "height": 844}, device_scale_factor=3, has_touch=True, is_mobile=True))
    if engine == "webkit":
        return playwright.webkit, {}, dict(playwright.devices["iPhone 13"])
    raise SystemExit("unknown engine: %s" % engine)


def device_options(playwright, engine, device="phone"):
    """v0.1.63: (context kwargs) for a scenario's device. phone = iPhone (above); ipad = touch tablet 820x1180;
    desktop = mouse + keyboard 1280x800 (no touch)."""
    if device == "phone":
        return phone_options(playwright, engine)[2]
    if device == "ipad":
        if engine == "webkit":
            return dict(playwright.devices["iPad Pro 11"], viewport={"width": 820, "height": 1180})
        return dict(viewport={"width": 820, "height": 1180}, device_scale_factor=2, has_touch=True, is_mobile=True)
    if device == "desktop":
        return dict(viewport={"width": 1280, "height": 800}, device_scale_factor=1, has_touch=False, is_mobile=False)
    raise SystemExit("unknown device: %s" % device)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--engines", default="chromium", help="comma list: chromium,webkit (webkit skips cleanly when not installed)")
    ap.add_argument("--out", default="e2e-artifacts", help="directory for screenshots / logs of failed scenarios")
    ap.add_argument("--only", default="", help="comma list of substrings; run scenarios whose name contains one")
    ap.add_argument("--nas-chrome", default=None, help="Chrome/Chromium binary for the NAS-side browser (default: google-chrome on PATH, else Playwright's)")
    ap.add_argument("--phone-chrome", default=None, help="Chrome binary for the chromium phone (default: Playwright's, else the NAS Chrome)")
    ap.add_argument("--require-engines", action="store_true",
                    help="fail (exit 1) when an engine is not installed instead of skipping it (CI/releases)")
    ap.add_argument("--keep-shots", action="store_true", help="screenshot every scenario, not only failures")
    args = ap.parse_args(argv)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright is not installed (pip install playwright==<version in .github/workflows/e2e.yml>)", file=sys.stderr)
        return 2
    import harness
    import scenarios

    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    engines = [e.strip() for e in args.engines.split(",") if e.strip()]
    only = [s.strip() for s in args.only.split(",") if s.strip()]
    t0 = time.monotonic()
    try:
        env = harness.Env(nas_chrome=args.nas_chrome)
    except Exception as exc:
        traceback.print_exc()
        print("E2E: could not start the environment: %s" % exc, file=sys.stderr)
        return 2
    print("E2E: app v%s on %s, NAS Chrome %s, fixtures https://%s (-> 127.0.0.1:%d)" %
          (env.version, env.url, env.nas_chrome_path, harness.HOST, env.fx.port))
    missing = []
    rows = []     # (engine, name, status, seconds, detail)
    try:
        with sync_playwright() as p:
            for engine in engines:
                btype, launch_kw, ctx_kw = phone_options(p, engine)
                if engine == "chromium":
                    exe = args.phone_chrome
                    if exe:
                        launch_kw = dict(launch_kw, executable_path=exe)
                try:
                    browser = btype.launch(**launch_kw)
                except Exception as exc:
                    if engine == "chromium" and not args.phone_chrome:
                        try:       # Playwright's chromium missing: fall back to the NAS Chrome binary
                            browser = btype.launch(executable_path=env.nas_chrome_path)
                        except Exception:
                            browser = None
                    else:
                        browser = None
                    if browser is None:
                        reason = "%s is not installed here (%s). Install: python -m playwright install --with-deps %s" % (
                            engine, str(exc).strip().splitlines()[0][:100], engine)
                        for sc in scenarios.SCENARIOS:
                            if not only or any(o in sc["name"] for o in only):
                                rows.append((engine, sc["name"], "SKIP", 0.0, "%s browser not installed" % engine))
                        missing.append(reason)
                        continue
                for sc in scenarios.SCENARIOS:
                    if only and not any(o in sc["name"] for o in only):
                        continue
                    if engine not in sc["engines"]:
                        rows.append((engine, sc["name"], "SKIP", 0.0, "not applicable to %s" % engine))
                        continue
                    rows.append(run_one(env, browser, device_options(p, engine, sc.get("device", "phone")), engine, sc, out, args.keep_shots))
                browser.close()
    finally:
        env.close()
    return report(rows, time.monotonic() - t0, out, missing, require=args.require_engines)


def run_one(env, browser, ctx_kw, engine, sc, out, keep_shots):
    import harness
    name = sc["name"]
    start = time.monotonic()
    env.reset_tabs()
    context = browser.new_context(**ctx_kw)
    page = context.new_page()
    ph = harness.Phone(env, page, context, engine, out, name)
    status, detail = "PASS", ""
    try:
        if name != "settings-routes":
            ph.start(sc["display"])
        else:
            ph.login()
        sc["fn"](ph)
        if ph.errors:
            raise AssertionError("unexpected console errors:\n  " + "\n  ".join(ph.errors[:8]))
        detail = "; ".join(ph.notes)
    except harness.Skip as skip:
        status, detail = "SKIP", str(skip)
    except Exception as exc:
        status = "FAIL"
        detail = ("%s: %s" % (type(exc).__name__, exc)).strip()
        shot = ph.shot("fail")
        log = Path(out) / ("%s-%s-fail.txt" % (engine, name))
        try:
            log.write_text("%s\n\n%s\nconsole errors: %s\nNAS tabs: %s\nfixture hits: %s\n" % (
                detail, traceback.format_exc(), ph.errors, {k: v.url for k, v in env.tabs().items()}, dict(env.fx.hits)))
        except OSError:
            pass
        detail += "  [shot: %s]" % shot.name
    else:
        if keep_shots:
            ph.shot("pass")
    finally:
        try:
            context.close()
        except Exception:
            pass
    return (engine, name, status, time.monotonic() - start, detail)


def report(rows, total, out, missing=(), require=False):
    width = max([len(r[1]) for r in rows] + [8])
    print()
    print("%-9s %-*s %-5s %6s  %s" % ("engine", width, "scenario", "", "secs", "detail"))
    print("-" * (width + 40))
    for engine, name, status, secs, detail in rows:
        first = detail.splitlines()[0] if detail else ""
        print("%-9s %-*s %-5s %6.1f  %s" % (engine, width, name, status, secs, first[:160]))
    counts = {k: sum(1 for r in rows if r[2] == k) for k in ("PASS", "FAIL", "SKIP")}
    print("-" * (width + 40))
    print("E2E: %d passed, %d failed, %d skipped in %.0fs (artifacts: %s)" % (counts["PASS"], counts["FAIL"], counts["SKIP"], total, out))
    for s in missing:
        print("SKIPPED: %s" % s)
    # Full detail of each failure in the log itself (CI artifacts may be unreachable).
    for engine, name, status, secs, detail in rows:
        if status == "FAIL" and detail:
            print("\n--- %s %s ---\n%s" % (engine, name, "\n".join(detail.splitlines()[:25])))
    if missing and require:
        print("FAIL: a required browser engine is missing (--require-engines)")
        return 1
    return 1 if counts["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
