#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

python3 -m py_compile webapp.py storage.py durability.py updater.py self_update.py jd2_policy.py jd2_control.py jd2_local.py jd2_transfer.py jd2_downloads.py jd2_accounts.py engine_components.py engine_supervisor.py container_runtime.py runtime_diagnostics.py browser_safety.py rb_blocklist.py remote_browser.py qpkg/shared/downloader_pid.py
python3 -m unittest discover -s tests -v

sh -n qpkg/shared/downloader.sh
sh -n qpkg/shared/python3-path
bash -n scripts/build-qpkg.sh
if command -v node >/dev/null 2>&1; then
  node --check web/app.js
else
  echo "node missing: web/app.js syntax validation is required before merging" >&2
  exit 1
fi

if grep -En 'no-sandbox|disable-web-security|user-agent|user_agent|setUserAgentOverride|synchronous[[:space:]]*=[[:space:]]*OFF|npm[[:space:]]+(run[[:space:]]+)?build' webapp.py storage.py self_update.py jd2_policy.py jd2_control.py jd2_local.py jd2_accounts.py engine_components.py engine_supervisor.py container_runtime.py runtime_diagnostics.py browser_safety.py rb_blocklist.py remote_browser.py qpkg/shared/downloader.sh; then
  echo "Safety grep matched prohibited runtime pattern" >&2
  exit 1
fi
if grep -rEin 'no-sandbox|disable-web-security|user-agent|user_agent' container/; then
  echo "Safety grep matched prohibited browser flag in container/" >&2
  exit 1
fi
sh -n container/browser/entrypoint.sh
echo "All local code and safety checks passed."
