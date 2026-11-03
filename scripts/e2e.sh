#!/usr/bin/env bash
# End-to-end suite (real webapp + real NAS-side Chrome + phone browsers). See docs/testing.md.
#   scripts/e2e.sh                      chromium phone, artifacts in ./e2e-artifacts
#   scripts/e2e.sh --engines chromium,webkit --out /tmp/e2e --only mirror
# Extra arguments go to tests/e2e/run.py. Exit code: 0 pass (skips allowed), 1 failure, 2 could not start.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
ARGS=("$@")
have() { local a; for a in "${ARGS[@]+"${ARGS[@]}"}"; do [ "$a" = "$1" ] && return 0; done; return 1; }
have --engines || ARGS+=(--engines chromium,webkit)
have --out || ARGS+=(--out "$ROOT/e2e-artifacts")
exec python3 tests/e2e/run.py "${ARGS[@]}"
