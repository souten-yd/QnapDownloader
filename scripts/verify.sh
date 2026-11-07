#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

python3 -m py_compile webapp.py webauthn.py ttlcache.py state_hub.py diskio.py debuglog.py storage.py durability.py updater.py self_update.py jd2_policy.py jd2_control.py jd2_local.py jd2_transfer.py jd2_downloads.py jd2_accounts.py engine_components.py engine_supervisor.py container_runtime.py runtime_diagnostics.py browser_safety.py rb_blocklist.py rb_firstload.py rb_probe_queue.py rb_resident.py rb_mirror.py remote_browser.py ws_hub.py selftest.py pt_core.py pt_proxy.py passthrough.py qpkg/shared/downloader_pid.py
python3 -m unittest discover -s tests -v
# tests/e2e is not a package and has no test_*.py files: unittest discovery skips it; it only gets a syntax check here.
python3 -m py_compile tests/e2e/harness.py tests/e2e/scenarios.py tests/e2e/run.py

sh -n qpkg/shared/downloader.sh
sh -n qpkg/shared/python3-path
bash -n scripts/build-qpkg.sh
if command -v node >/dev/null 2>&1; then
  node --check web/app.js
  for js in web/app-sw.js web/pt-sw.js web/pt-helper.js web/pt-boot.js web/pt-download.js; do node --check "$js"; done
else
  echo "node missing: web/app.js syntax validation is required before merging" >&2
  exit 1
fi

if grep -En 'no-sandbox|disable-web-security|user-agent|user_agent|setUserAgentOverride|synchronous[[:space:]]*=[[:space:]]*OFF|npm[[:space:]]+(run[[:space:]]+)?build' webapp.py webauthn.py diskio.py debuglog.py storage.py self_update.py jd2_policy.py jd2_control.py jd2_local.py jd2_accounts.py engine_components.py engine_supervisor.py container_runtime.py runtime_diagnostics.py browser_safety.py rb_blocklist.py rb_firstload.py rb_probe_queue.py rb_resident.py rb_mirror.py remote_browser.py ws_hub.py selftest.py qpkg/shared/downloader.sh; then
  echo "Safety grep matched prohibited runtime pattern" >&2
  exit 1
fi
# v0.1.57: the passthrough modules are not in the list above only because the proxy must send a User-Agent (the
# phone's, or a fixed iPhone one) to the sites; every other pattern is checked for them too.
if grep -En 'no-sandbox|disable-web-security|setUserAgentOverride|synchronous[[:space:]]*=[[:space:]]*OFF|npm[[:space:]]+(run[[:space:]]+)?build' pt_core.py pt_proxy.py passthrough.py; then
  echo "Safety grep matched prohibited runtime pattern in the passthrough modules" >&2
  exit 1
fi
if grep -rEin 'no-sandbox|disable-web-security|user-agent|user_agent' container/; then
  echo "Safety grep matched prohibited browser flag in container/" >&2
  exit 1
fi
sh -n container/browser/entrypoint.sh
echo "All local code and safety checks passed."
