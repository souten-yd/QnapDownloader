#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

python3 -m py_compile webapp.py storage.py durability.py updater.py self_update.py jd2_policy.py jd2_control.py qpkg/shared/downloader_pid.py
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

if grep -En 'no-sandbox|disable-web-security|synchronous[[:space:]]*=[[:space:]]*OFF|npm[[:space:]]+(run[[:space:]]+)?build' webapp.py storage.py self_update.py jd2_policy.py jd2_control.py qpkg/shared/downloader.sh; then
  echo "Safety grep matched prohibited runtime pattern" >&2
  exit 1
fi
echo "All local code and safety checks passed."
